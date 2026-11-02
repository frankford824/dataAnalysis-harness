import hashlib
import io
import json
from datetime import datetime

import polars as pl
import pytest

from conftest import MODELS, fixture_publication_scope
from ledger import agent_authority, automation_store as journal, statement_review
from ledger.automation_deterministic import RULE, approved_rule, run_available
from ledger.automation_rules import contracts
from ledger.engine.runtime import Ingestion, run, _dedupe_across_files
from ledger.engine.types import FileRef
from ledger.model.loader import load_model
from ledger.model.schema import Model, Overhead, StatementNode, Store
from ledger.storage_integrity import seal
from ledger.view import slice_dict
from ledger.workspace import Workspace
from test_reship_period_and_cost_drill import item as order_item
from test_statement_event_dedupe import douyin_row, item

HUMAN = {'id': 'finance', 'kind': 'human', 'authenticated': True}


@pytest.fixture(autouse=True)
def rollout(monkeypatch):
    monkeypatch.setenv('LEDGER_DETERMINISTIC_AUTOMATION_ENABLED', '1')


def ungranted(tmp_path):
    ws = Workspace(tmp_path/'ws');builtin = load_model(MODELS/'cn-ecommerce')
    metric = builtin.metric('trade_receipt_douyin').model_copy(update={'major': None, 'posting_basis': 'transaction', 'time_basis': 'settle_time'})
    model = Model(id='exact', name='exact', stores=(Store(id='s', name='shop', platform='douyin'),),
        sources=(builtin.source('order_detail'), builtin.source('settlement').model_copy(update={'provides': (metric.id,)})),
        metrics=(metric,), statement=(StatementNode(id='n_receipt', name='销售收入', headline='revenue', formula={'op': 'add', 'of': [metric.id]}),),
        overheads=(Overhead(period='2026-07', amount=0),))
    entries = [item([douyin_row(base_order_id=ref, settle_time=datetime(2026, 7, 14, 9, 14, 6))], name, 'douyin_settlement_v2')
               for ref, name in [('A', 'first'), ('B', 'second')]]
    shas = []
    for entry in entries:
        kept = ws.keep(entry.ref.filename, io.BytesIO(entry.frame.write_ndjson().encode()), 's')
        entry.ref = FileRef(kept.sha, entry.ref.filename, 'sheet');shas.append(kept.sha)
        entry.frame = entry.frame.with_columns(pl.lit(kept.sha).alias('__sha__'))
    rows = [dict(order_id='A', product_id='p', store_name='shop', order_time=datetime(2026, 7, 1), buyer_paid=128.2)]
    ing = Ingestion(model=model, items=[*entries, order_item('order_detail', rows, rows[0].keys())])
    _dedupe_across_files(ing, model);result = run(ing, 'douyin');sl = result.slice('shop', '2026-07')
    rid = ws.record('s', '2026-07', slice_dict(sl, model.store('s'), model), shas,
        model_revision=hashlib.sha256(model.model_dump_json().encode()).hexdigest())
    sl.facts.write_parquet(ws.facts_path(rid));seal(ws.facts_path(rid))
    fixture_publication_scope(ws, model, ['2026-07'])
    return ws, model, rid


def delegate(ws, model, request='delegate', actor=HUMAN):
    preview = agent_authority.preview(ws, model, request_id=request+'-preview', period_from='2026-07', period_until='2026-12',
        valid_days=365, reason='授权 Agent 按已发布确定性规则自动处理，结账仍由财务确认', actor=actor)
    return preview, agent_authority.apply(ws, model, request_id=request+'-apply', proposal_id=preview['proposal_id'],
        proposal_sha=preview['proposal_sha'], input_sha=preview['input_sha'], actor=actor)


def test_one_human_grant_expands_to_checker_compatible_grants_and_runs_engine_rules(tmp_path):
    ws, model, rid = ungranted(tmp_path)
    before = agent_authority.status(ws, model, '2026-07')
    assert before['active'] is False and before['covered'] == 0 and before['missing_count'] == len(contracts())
    with pytest.raises(PermissionError):approved_rule(ws, 's', '2026-07')
    preview, applied = delegate(ws, model)
    assert preview['effect']['grant_count'] == len(contracts()) and preview['effect']['closed_store_months_changed'] is False
    assert applied['status'] == 'applied' and applied['grant_count'] == len(contracts())
    grants = ws.conn.execute("SELECT d.request_id,a.identity_kind,a.action,a.scope_json FROM automation_decision d JOIN automation_authorization a ON a.request_id=d.request_id WHERE d.kind='rule_grant'").fetchall()
    assert len(grants) == len(contracts())
    assert all(g['identity_kind'] == 'human' and g['action'] == 'decision.apply' and json.loads(g['scope_json'])['agent_authority'] for g in grants)
    assert approved_rule(ws, 's', '2026-07')
    # Applying the delegation ran the published statement rule immediately;
    # the money is the engine's reading of the original statement row.
    assert ws.conn.execute('SELECT count(*) FROM automation_decision WHERE kind=?', (RULE,)).fetchone()[0] == 1
    after = agent_authority.status(ws, model, '2026-07')
    assert after['active'] is True and after['delegated'] == len(contracts()) and after['valid_until']
    assert after['financial_close'] == 'human_confirmation'
    change = after['changes'][0]
    assert change['rule_id'] == RULE and change['store_name'] == 'shop' and change['can_revoke'] is True
    assert ws.state('s', '2026-07').state == 'open'


def test_later_months_in_range_are_covered_without_another_grant(tmp_path):
    ws, model, rid = ungranted(tmp_path)
    delegate(ws, model)
    grant = journal.active_decisions(ws.conn, '2026-11')[('s', 'rule_grant:'+RULE)]
    assert grant['values']['period_until'] == '2026-12'
    assert ('s', 'rule_grant:'+RULE) not in journal.active_decisions(ws.conn, '2027-01')


def test_agent_or_service_identity_cannot_grant_itself(tmp_path):
    ws, model, rid = ungranted(tmp_path)
    for actor in ({'id': 'worker', 'kind': 'service', 'authenticated': True}, {'id': 'agent', 'kind': 'agent', 'authenticated': True}):
        with pytest.raises(PermissionError):delegate(ws, model, actor=actor)
    assert not ws.conn.execute("SELECT 1 FROM automation_decision WHERE kind='rule_grant'").fetchone()


def test_another_person_cannot_confirm_a_delegation_preview(tmp_path):
    ws, model, rid = ungranted(tmp_path)
    preview = agent_authority.preview(ws, model, request_id='p', period_from='2026-07', period_until='2026-07',
        valid_days=30, reason='授权', actor=HUMAN)
    with pytest.raises(PermissionError):
        agent_authority.apply(ws, model, request_id='a', proposal_id=preview['proposal_id'], proposal_sha=preview['proposal_sha'],
            input_sha=preview['input_sha'], actor={'id': 'other', 'kind': 'human', 'authenticated': True})


def test_scope_limits_are_enforced(tmp_path):
    ws, model, rid = ungranted(tmp_path)
    for first, last, days in (('2026-08', '2026-07', 30), ('2026-07', '2028-12', 30), ('2026-07', '2026-07', 0)):
        with pytest.raises(agent_authority.Conflict):
            agent_authority.preview(ws, model, request_id=f'{first}{last}{days}', period_from=first, period_until=last,
                valid_days=days, reason='授权', actor=HUMAN)


def test_one_click_withdrawal_revokes_only_delegated_grants_and_stops_rules(tmp_path):
    ws, model, rid = ungranted(tmp_path)
    delegate(ws, model)
    result = agent_authority.revoke(ws, model, request_id='withdraw', period='2026-07', reason='暂停 Agent 自动处理', actor=HUMAN)
    assert result == {'status': 'revoked', 'revoked': len(contracts()), 'failed': []}
    status = agent_authority.status(ws, model, '2026-07')
    assert status['active'] is False and status['covered'] == 0
    with pytest.raises(PermissionError):approved_rule(ws, 's', '2026-07')
    assert run_available(ws, model, '2026-07') == []
    again = agent_authority.revoke(ws, model, request_id='withdraw-again', period='2026-07', reason='重复', actor=HUMAN)
    assert again['revoked'] == 0


def test_human_can_withdraw_an_individual_engine_change(tmp_path):
    from ledger import automation_actions as actions
    ws, model, rid = ungranted(tmp_path)
    delegate(ws, model)
    change = agent_authority.status(ws, model, '2026-07')['changes'][0]
    row = ws.conn.execute('SELECT values_json FROM automation_decision WHERE id=?', (change['decision_id'],)).fetchone()
    sha = actions._decision_input(ws, model, RULE, 's', '2026-07', json.loads(row[0]))
    actions.decision_revoke(ws, model, change['decision_id'], request_id='undo-one', reason='人工核对原始流水后撤回该自动处理',
        input_sha=sha, actor=HUMAN)
    assert agent_authority.status(ws, model, '2026-07')['changes'][0]['status'] == 'revoked'


def test_http_routes_reject_service_identity(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ledger import automation_api
    ws, model, rid = ungranted(tmp_path)
    app = FastAPI()
    automation_api.install(app, lambda: ws, lambda: model, actor=lambda request: {'id': 'worker', 'kind': 'service', 'authenticated': True})
    client = TestClient(app)
    assert client.get('/api/automation/agent-authority', params={'period': '2026-07'}).json()['active'] is False
    response = client.post('/api/automation/agent-authority/preview', json={'request_id': 'x', 'period_from': '2026-07',
        'period_until': '2026-07', 'valid_days': 30, 'reason': '自授权'})
    assert response.status_code == 403
