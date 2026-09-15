"""A human cost decision changes the frozen view, never the source run."""
import hashlib
import json
from pathlib import Path

import pytest

from ledger import manual_cost
from ledger.commission_registry import Registry
from ledger.commission_reports import build
from ledger.model.repository import ModelRepository
from ledger.model.schema import Overhead
from ledger.workspace import Workspace, WorkspaceError


def model():
    base = ModelRepository(Path(__file__).resolve().parents[2] / 'models' / 'cn-ecommerce').get().model
    return base.model_copy(update={'overheads': (
        Overhead(period='2026-06', amount=100, name='兼职人工费用'),
    )})


def result(m, person):
    return {
        'store_id': 'taobao_mt9sjmls', 'store': '谷本文-luckyglow旗舰店',
        'period': '2026-06', 'can_close': False,
        'statement': [{'id': 'n_receipt', 'value': 1000, 'available': True}],
        'missing_sources': [],
        'findings': [{'id': 'chk_goods_coverage', 'name': '商品成本核对',
                      'passed': False, 'blocking': True, 'message': '成本覆盖不足'}],
        'cost_coverage': {'coverage': .2, 'threshold': .95, 'passed': False,
                          'covered': 2, 'expected': 10, 'uncovered': 8},
        'commission': {'engine': 'commission-v2', 'total': None,
                       'pricing_threshold_met': False, 'people': [
                           {'person_id': person['id'], 'person': person['name'],
                            'amount': None, 'sales': 1000, 'gross': None},
                       ]},
        'calculation_inputs': {
            'metric_totals': {'trade_receipt': 1000.0, 'goods_cost': -600.0,
                              'freight_cost': -50.0},
            'unavailable_metrics': ['goods_cost'],
            'inapplicable_metrics': [],
        },
    }


def test_manual_close_uses_model_preview_and_preserves_raw_run(tmp_path):
    m = model(); ws = Workspace(tmp_path); registry = Registry(tmp_path)
    person = registry.person_save({'name': '甲'}, 'test', '登记')
    raw = result(m, person)
    run = ws.record(raw['store_id'], raw['period'], raw, [],
                    model_revision=hashlib.sha256(m.model_dump_json().encode()).hexdigest())
    confirmed = {'goods': '700.00', 'dropship': '20.00', 'reshipment': '10.00'}
    preview = manual_cost.preview(m, raw, confirmed)
    assert preview['observed'] == {'goods': 600.0, 'dropship': 0.0, 'reshipment': 0.0}
    assert preview['gross'] == 270.0 and preview['profit'] == 220.0
    decided, audit = manual_cost.certified(
        m, raw, run, confirmed, [{'person_id': person['id'], 'amount': '17.00'}],
        False, '运营已确认三类成本与本人提成',
    )
    assert decided['commission']['total'] == 17.0
    assert decided['commission']['manual_amounts_after_labor']
    closed = ws.close_period(raw['store_id'], raw['period'],
                             note='运营已确认三类成本与本人提成', expected_run_id=run,
                             labor_cut='30.00', manual_result=decided, manual_decision=audit)
    assert closed.closed and closed.run_id == run
    assert next(x for x in closed.result['statement'] if x['id'] == 'net_profit')['value'] == 220
    assert json.loads(ws.conn.execute('SELECT result FROM run WHERE id=?', (run,)).fetchone()[0]) == raw
    assert ws.state_by_run(run).result['manual_cost']['confirmed']['goods'] == 700
    report = build(ws, registry, m, '2026-06', '2026-06', [raw['store_id']], model_root=tmp_path)
    assert report['total'] == 17  # Manual payout already includes labor; no second deduction.
    assert report['stores'][0]['labor_cost'] == 30
    ws.reopen_period(raw['store_id'], raw['period'], note='继续修改数据')
    assert ws.state(raw['store_id'], raw['period']).result.get('manual_cost') is None
    pinned = build(ws, registry, m, '2026-06', '2026-06', [raw['store_id']],
                   run_ids=[run], model_root=tmp_path)
    assert pinned['total'] == 17 and pinned['run_ids'] == [run]
    ws.close()


def test_manual_preview_and_payout_refuse_unverified_or_ambiguous_values():
    m = model()
    raw = {'calculation_inputs': {'metric_totals': {'trade_receipt': 1000},
                                  'unavailable_metrics': ['trade_receipt'],
                                  'inapplicable_metrics': []},
           'store_id': 'taobao_mt9sjmls', 'commission': {'people': []}}
    with pytest.raises(WorkspaceError, match='除成本外仍有资料缺口'):
        manual_cost.certified(m, raw, 1,
            {'goods': '1.00', 'dropship': '0.00', 'reshipment': '0.00'},
            [], True, '人工确认')
    with pytest.raises(WorkspaceError, match='两位小数'):
        manual_cost.preview(m, raw,
            {'goods': '1.001', 'dropship': '0.00', 'reshipment': '0.00'})


def test_http_preview_and_manual_close_require_current_run(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from fastapi.testclient import TestClient
    from ledger import api
    from ledger.model import transaction

    m = model(); ws = Workspace(tmp_path)
    registry = Registry(tmp_path)
    person = registry.person_save({'name':'甲'}, 'test', '登记')
    raw = result(m, person)
    rid = ws.record(raw['store_id'], raw['period'], raw, [],
                    model_revision=hashlib.sha256(m.model_dump_json().encode()).hexdigest())
    monkeypatch.setattr(api, 'WORKSPACE_ROOT', tmp_path)
    monkeypatch.setattr(api, '_ws', ws)
    monkeypatch.setattr(api, '_model', lambda: m)
    monkeypatch.setattr(transaction, 'model_lock', lambda _: nullcontext())
    client = TestClient(api.app)
    path = f"/api/stores/{raw['store_id']}/periods/{raw['period']}"
    costs = {'goods':'700.00','dropship':'20.00','reshipment':'10.00'}
    assert client.post(path+'/manual-cost-preview', json={'run_id':rid-1,'costs':costs}).status_code == 409
    preview = client.post(path+'/manual-cost-preview', json={'run_id':rid,'costs':costs})
    assert preview.status_code == 200, preview.text
    assert preview.json()['profit'] == 220
    closed = client.post(path+'/close', json={
        'run_id': rid, 'costs': costs, 'note': '已由运营确认成本及提成',
        'payouts': [{'person_id': person['id'], 'amount':'17.00'}],
    })
    assert closed.status_code == 200, closed.text
    assert closed.json()['state'] == 'closed'
    assert ws.state(raw['store_id'], raw['period']).result['manual_cost']['profit'] == 220
    ws.close()
