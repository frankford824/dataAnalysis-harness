import json

import pytest

from ledger import agent_authority, agent_questions as qa, agent_upgrade, automation_store as journal
from ledger.automation_deterministic import RULE, approved_rule
from ledger.automation_rules import contracts
from ledger.model.schema import Store
from test_agent_authority import HUMAN, ungranted
from test_agent_questions import WORKER, computed


def consent(ws, model, request='standing', period='2026-07'):
    preview = agent_authority.preview(ws, model, request_id=request+'-preview', period_from=period, period_until='',
        valid_days=365, standing=True, reason='长期授权 Agent 按已发布规则自动处理', actor=HUMAN)
    return preview, agent_authority.apply(ws, model, request_id=request+'-apply', proposal_id=preview['proposal_id'],
        proposal_sha=preview['proposal_sha'], input_sha=preview['input_sha'], actor=HUMAN)


def rerecord(ws, value=None):
    row = ws.latest_run('s', '2026-07');result = json.loads(row['result'])
    if value is not None:
        result['statement'] = [{**n, 'available': True, 'value': value} if n['id'] == 'n_receipt' else n for n in result['statement']]
    return ws.record('s', '2026-07', result, [], model_revision=row['model_revision'])


@pytest.fixture
def upgraded(tmp_path, monkeypatch):
    monkeypatch.setattr('ledger.workspace.engine_version', lambda: 'engine-old')
    ws, model, rid = ungranted(tmp_path)
    monkeypatch.setattr('ledger.workspace.engine_version', lambda: 'engine-new')
    monkeypatch.setattr(agent_upgrade, '_engine', lambda: 'engine-new')
    return ws, model


def test_one_consent_covers_every_future_month_without_expiry(tmp_path):
    ws, model, rid = ungranted(tmp_path)
    preview, applied = consent(ws, model)
    assert preview['effect']['standing'] is True and applied['valid_until'] == agent_authority.STANDING_UNTIL
    grant = journal.active_decisions(ws.conn, '2031-05')[('s', 'rule_grant:'+RULE)]
    assert grant['values']['period_until'] == agent_authority.OPEN_END
    status = agent_authority.status(ws, model, '2026-07')
    assert status['standing'] == {'active': True, 'actor': 'finance', 'at': status['standing']['at'], 'outdated_rules': []}
    assert status['active'] is True and status['delegated'] == len(contracts())


def test_new_store_is_covered_by_the_standing_consent_and_stops_with_it(tmp_path):
    ws, model, rid = ungranted(tmp_path)
    consent(ws, model)
    grown = model.model_copy(update={'stores': (*model.stores, Store(id='t', name='new shop', platform='douyin'))})
    assert agent_authority.extend(ws, grown, '2026-08') == len(contracts())
    assert agent_authority.extend(ws, grown, '2026-08') == 0
    rows = ws.conn.execute("SELECT a.actor,a.identity_kind,a.scope_json FROM automation_decision d JOIN automation_authorization a ON a.request_id=d.request_id WHERE d.store_id='t'").fetchall()
    assert rows and all(r['actor'] == 'finance' and r['identity_kind'] == 'human' and json.loads(r['scope_json'])['agent_authority'] for r in rows)
    result = agent_authority.revoke(ws, grown, request_id='stop', period='2026-08', reason='人工停止 Agent 自动处理', actor=HUMAN)
    assert result['status'] == 'revoked' and result['revoked'] == 2*len(contracts())
    assert agent_authority.standing(ws.conn) is None
    assert agent_authority.extend(ws, grown, '2026-09') == 0
    assert agent_authority.status(ws, grown, '2026-07')['standing']['active'] is False


def test_a_changed_rule_contract_waits_for_the_person_again(tmp_path, monkeypatch):
    ws, model, rid = ungranted(tmp_path)
    consent(ws, model)
    changed = [{**r, 'sha': 'f'*64} if r['id'] == RULE else r for r in agent_authority._rules()]
    monkeypatch.setattr(agent_authority, '_rules', lambda: changed)
    assert agent_authority.status(ws, model, '2026-07')['standing']['outdated_rules']
    grown = model.model_copy(update={'stores': (*model.stores, Store(id='t', name='new shop', platform='douyin'))})
    agent_authority.extend(ws, grown, '2026-07')
    kinds = {json.loads(r[0])['rule_id'] for r in ws.conn.execute("SELECT values_json FROM automation_decision WHERE store_id='t'")}
    assert RULE not in kinds and kinds


def test_identical_results_after_an_upgrade_need_nobody(upgraded):
    ws, model = upgraded
    rerecord(ws)
    view = agent_upgrade.check(ws, model, '2026-07')
    assert view['same'] == 1 and view['needs_confirmation'] is False
    assert agent_upgrade.blocking(ws, None, '2026-07') is False


def test_changed_results_wait_for_one_confirmation(upgraded):
    ws, model = upgraded
    pending = agent_upgrade.check(ws, model, '2026-07')
    assert pending['pending_recompute'] == 1 and pending['needs_confirmation'] is True
    with pytest.raises(agent_upgrade.Conflict):
        agent_upgrade.confirm(ws, model, request_id='early', period='2026-07', input_sha=pending['input_sha'], actor=HUMAN)
    rerecord(ws, 128.2)
    view = agent_upgrade.check(ws, model, '2026-07')
    diff = view['items'][0]['differences']
    assert view['changed'] == 1 and diff == [{'node_id': 'n_receipt', 'name': '销售收入', 'before': None, 'after': 128.2, 'display': diff[0]['display']}]
    with pytest.raises(PermissionError, match='新版本'):approved_rule(ws, 's', '2026-07')
    with pytest.raises(agent_upgrade.Conflict):
        agent_upgrade.confirm(ws, model, request_id='stale', period='2026-07', input_sha=pending['input_sha'], actor=HUMAN)
    with pytest.raises(PermissionError):
        agent_upgrade.confirm(ws, model, request_id='svc', period='2026-07', input_sha=view['input_sha'], actor=WORKER)
    assert agent_upgrade.confirm(ws, model, request_id='ok', period='2026-07', input_sha=view['input_sha'], actor=HUMAN)['status'] == 'confirmed'
    assert agent_upgrade.blocking(ws, None, '2026-07') is False
    rerecord(ws, 99.0)
    assert agent_upgrade.blocking(ws, model, '2026-07') is False


def test_dispute_reaches_the_inbox_without_identifiers_and_closes_on_acknowledge(tmp_path):
    ws, model, rid = computed(tmp_path)
    subject = {'node_id': 'n_receipt', 'row': {'metric_id': 'trade_receipt_douyin', 'file_name': '13800138000-流水.xlsx',
        'sheet': 'sheet', 'row_no': 7, 'accounting_status': '已计入'}}
    asked = qa.ask(ws, model, request_id='d1', store_id='s', period='2026-07', question='这单已退款，不应计入收入', actor=HUMAN, subject=subject)
    assert asked['subject']['node_name'] == '销售收入'
    with pytest.raises(qa.QuestionError):
        qa.ask(ws, model, request_id='d2', store_id='s', period='2026-07', question='x', actor=HUMAN, subject={'node_id': 'nope'})
    claimed = qa.claim(ws, model, WORKER)
    raw = json.dumps(claimed, ensure_ascii=False)
    assert claimed['context']['dispute']['node_name'] == '销售收入' and '13800138000' not in raw
    assert qa.open_disputes(ws, model, '2026-07') == []
    qa.answer(ws, model, claimed['question_id'], WORKER, lease_token=claimed['lease_token'],
        text='这笔按结算流水计入{{node:n_receipt}}；如已退款，退款流水到达后会自动冲减。')
    inbox = qa.open_disputes(ws, model, '2026-07')
    assert len(inbox) == 1 and inbox[0]['store_name'] == 'shop' and '128.20 元' in inbox[0]['answer']
    with pytest.raises(PermissionError):qa.acknowledge(ws, inbox[0]['question_id'], actor=WORKER)
    assert qa.acknowledge(ws, inbox[0]['question_id'], actor=HUMAN)['acknowledged_by'] == 'finance'
    assert qa.open_disputes(ws, model, '2026-07') == []


def test_inbox_routes_require_a_person_for_writes(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ledger import automation_api
    ws, model, rid = ungranted(tmp_path)
    app = FastAPI()
    automation_api.install(app, lambda: ws, lambda: model, actor=lambda request: {'id': 'worker', 'kind': 'service', 'authenticated': True})
    client = TestClient(app)
    inbox = client.get('/api/automation/inbox', params={'period': '2026-07'}).json()
    assert inbox['upgrade']['needs_confirmation'] is False and inbox['disputes'] == []
    assert client.post('/api/automation/upgrade/confirm', json={'request_id': 'x', 'period': '2026-07', 'input_sha': '0'*64}).status_code == 403
    assert client.post('/api/automation/agent-authority/preview', json={'request_id': 'y', 'period_from': '2026-07',
        'standing': True, 'reason': '自授权'}).status_code == 403
