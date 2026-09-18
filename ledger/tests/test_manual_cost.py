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


def test_payout_only_freezes_confirmed_amount_without_changing_profit_or_raw_run(tmp_path):
    m = model(); ws = Workspace(tmp_path); registry = Registry(tmp_path)
    person = registry.person_save({'name': '甲'}, 'test', '登记')
    raw = result(m, person)
    raw.update(can_close=True, findings=[], statement=[
        {'id': 'n_receipt', 'value': 1000, 'available': True},
        {'id': 'net_profit', 'value': 13269.90, 'available': True},
    ])
    raw['commission'].update(total=-140.95, base_total=13269.90,
                             amount_complete=False, unassigned_orders=2401,
                             unassigned_base=16087.19)
    raw['commission']['people'][0]['amount'] = -140.95
    run = ws.record(raw['store_id'], raw['period'], raw, [],
                    model_revision=hashlib.sha256(m.model_dump_json().encode()).hexdigest())
    with pytest.raises(WorkspaceError, match='全部提成人员'):
        manual_cost.payout_only(raw, run, [{'person_id': 'other', 'amount': '17.00'}],
                                False, '核对提成')
    confirmed, audit = manual_cost.payout_only(
        raw, run, [{'person_id': person['id'], 'amount': '17.00'}], False,
        '原商品关系未覆盖，本人提成由人工确认')
    audit['line_revision'] = 0
    ws.close_period(raw['store_id'], raw['period'], note=audit['reason'],
                    expected_run_id=run, labor_cut='30.00',
                    manual_result=confirmed, manual_decision=audit)
    frozen = ws.state(raw['store_id'], raw['period']).result
    assert frozen['statement'] == raw['statement']
    assert frozen['commission']['total'] == 17
    assert frozen['commission']['unassigned_orders'] == 2401
    assert frozen['manual_payout']['source_run_id'] == run
    assert frozen.get('manual_cost') is None
    assert json.loads(ws.conn.execute('SELECT result FROM run WHERE id=?', (run,)).fetchone()[0]) == raw
    report = build(ws, registry, m, '2026-06', '2026-06', [raw['store_id']], model_root=tmp_path)
    assert report['total'] == 17  # Already includes labor; no second deduction.
    assert report['trial_periods'] == 0
    ws.close()


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


def test_http_payout_only_rejects_stale_run_and_does_not_change_store_profit(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from fastapi.testclient import TestClient
    from ledger import api
    from ledger.model import transaction

    m = model(); ws = Workspace(tmp_path); registry = Registry(tmp_path)
    person = registry.person_save({'name': '甲'}, 'test', '登记')
    raw = result(m, person)
    raw.update(can_close=True, findings=[], statement=[
        {'id': 'n_receipt', 'value': 39777.59, 'available': True},
        {'id': 'net_profit', 'value': 13269.90, 'available': True},
    ])
    raw['commission'].update(total=-140.95, base_total=13269.90,
                             amount_complete=False, unassigned_orders=2401)
    raw['commission']['people'][0]['amount'] = -140.95
    run = ws.record(raw['store_id'], raw['period'], raw, [],
                    model_revision=hashlib.sha256(m.model_dump_json().encode()).hexdigest())
    monkeypatch.setattr(api, 'WORKSPACE_ROOT', tmp_path)
    monkeypatch.setattr(api, '_ws', ws)
    monkeypatch.setattr(api, '_model', lambda: m)
    monkeypatch.setattr(transaction, 'model_lock', lambda _: nullcontext())
    client = TestClient(api.app)
    endpoint = f"/api/stores/{raw['store_id']}/periods/{raw['period']}/close"
    body = {'run_id': run, 'payout_only': True,
            'note': '核对未分配商品后人工确认本人提成',
            'payouts': [{'person_id': person['id'], 'amount': '17.00'}],
            'line_revision': 0}
    assert client.post(endpoint, json={**body, 'run_id': run - 1}).status_code == 409
    assert client.post(endpoint, json={**body, 'payouts': [{'person_id': 'other', 'amount': '17.00'}]}).status_code == 409
    response = client.post(endpoint, json=body)
    assert response.status_code == 200, response.text
    frozen = ws.state(raw['store_id'], raw['period']).result
    assert frozen['statement'] == raw['statement']
    assert frozen['commission']['total'] == 17
    assert frozen.get('manual_cost') is None
    report = client.post('/api/commission-v2/reports/query', json={
        'start': '2026-06', 'end': '2026-06', 'store_ids': [raw['store_id']],
        'view': 'store_people'})
    assert report.status_code == 200, report.text
    assert report.json()['assignment_gaps'] == []  # Human payout is no longer a partial trial.
    ws.close()


def test_accepting_raw_trial_payouts_deducts_labor_before_freeze(tmp_path):
    m = model(); ws = Workspace(tmp_path); registry = Registry(tmp_path)
    person = registry.person_save({'name': '甲'}, 'test', '登记')
    raw = result(m, person)
    raw['statement'] = [
        {'id': 'n_receipt', 'value': 1000, 'available': True},
        {'id': 'net_profit', 'value': 500, 'available': True},
    ]
    raw.update(can_close=True, findings=[], missing_sources=[],
               cost_coverage={'coverage': 1, 'threshold': .95, 'passed': True,
                              'covered': 10, 'expected': 10, 'uncovered': 0})
    raw['commission'].update(engine='commission-v2', total=25, base_total=500,
                             amount_complete=False)
    raw['commission']['people'][0]['amount'] = 25
    run = ws.record(raw['store_id'], raw['period'], raw, [],
                    model_revision=hashlib.sha256(m.model_dump_json().encode()).hexdigest())
    decided, _ = manual_cost.payout_only(
        raw, run, [{'person_id': person['id'], 'amount': '25.00'}], False,
        '接受系统试算', labor_cut='100.00')
    assert decided['commission']['total'] == 20
    assert decided['commission']['people'][0]['amount'] == 20
    assert decided['commission']['manual_amounts_after_labor']
    assert any('扣兼职前试算' in note for note in decided['commission']['notes'])
    custom, _ = manual_cost.payout_only(
        raw, run, [{'person_id': person['id'], 'amount': '17.00'}], False,
        '另行确认', labor_cut='100.00')
    assert custom['commission']['total'] == 17
    ws.close_period(raw['store_id'], raw['period'], note='接受系统试算',
                    expected_run_id=run, labor_cut='100.00',
                    manual_result=decided, manual_decision={
                        'source_run_id': run, 'reason': '接受系统试算',
                        'payouts': [{'person_id': person['id'], 'amount': 20}],
                        'no_payout': False, 'line_revision': 0})
    report = build(ws, registry, m, '2026-06', '2026-06', [raw['store_id']],
                   model_root=tmp_path)
    assert report['total'] == 20
    ws.close()


def test_period_snapshot_exposes_after_labor_trial_for_manual_close(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from fastapi.testclient import TestClient
    from ledger import api
    from ledger.model import transaction

    m = model(); ws = Workspace(tmp_path); registry = Registry(tmp_path)
    person = registry.person_save({'name': '甲'}, 'test', '登记')
    raw = result(m, person)
    raw['statement'] = [
        {'id': 'n_receipt', 'value': 1000, 'available': True},
        {'id': 'net_profit', 'value': 500, 'available': True},
    ]
    raw.update(can_close=True, findings=[], missing_sources=[],
               cost_coverage={'coverage': 1, 'threshold': .95, 'passed': True,
                              'covered': 10, 'expected': 10, 'uncovered': 0})
    raw['commission'].update(engine='commission-v2', total=25, base_total=500)
    raw['commission']['people'][0]['amount'] = 25
    run = ws.record(raw['store_id'], raw['period'], raw, [],
                    model_revision=hashlib.sha256(m.model_dump_json().encode()).hexdigest())
    snap = api._build_period_detail(ws, m, raw['store_id'], raw['period'],
                                    ws.state(raw['store_id'], raw['period']))
    assert snap['pending_labor_cut'] == 100
    assert snap['commission']['people'][0]['amount'] == 25
    assert snap['commission']['people'][0]['amount_after_labor'] == 20
    monkeypatch.setattr(api, 'WORKSPACE_ROOT', tmp_path)
    monkeypatch.setattr(api, '_ws', ws)
    monkeypatch.setattr(api, '_model', lambda: m)
    monkeypatch.setattr(transaction, 'model_lock', lambda _: nullcontext())
    client = TestClient(api.app)
    closed = client.post(f"/api/stores/{raw['store_id']}/periods/{raw['period']}/close", json={
        'run_id': run, 'payout_only': True, 'note': '接受系统试算',
        'payouts': [{'person_id': person['id'], 'amount': '25.00'}],
        'line_revision': 0,
    })
    assert closed.status_code == 200, closed.text
    frozen = ws.state(raw['store_id'], raw['period']).result
    assert frozen['commission']['total'] == 20
    report = client.post('/api/commission-v2/reports/query', json={
        'start': '2026-06', 'end': '2026-06', 'store_ids': [raw['store_id']],
        'view': 'store_people'})
    assert report.status_code == 200, report.text
    assert report.json()['total'] == 20
    ws.close()


def test_period_snapshot_survives_month_without_labor_overhead(tmp_path):
    from ledger import api
    from ledger.model.repository import ModelRepository

    m = ModelRepository(Path(__file__).resolve().parents[2] / 'models' / 'cn-ecommerce').get().model
    assert m.overhead('2026-06') is None
    ws = Workspace(tmp_path)
    registry = Registry(tmp_path)
    person = registry.person_save({'name': '甲'}, 'test', '登记')
    raw = result(m, person)
    raw['statement'] = [
        {'id': 'n_receipt', 'value': 1000, 'available': True},
        {'id': 'net_profit', 'value': 500, 'available': True},
    ]
    raw.update(can_close=True, findings=[], missing_sources=[],
               cost_coverage={'coverage': 1, 'threshold': .95, 'passed': True,
                              'covered': 10, 'expected': 10, 'uncovered': 0})
    raw['commission'].update(engine='commission-v2', total=25, base_total=500)
    raw['commission']['people'][0]['amount'] = 25
    ws.record(raw['store_id'], raw['period'], raw, [],
              model_revision=hashlib.sha256(m.model_dump_json().encode()).hexdigest())
    snap = api._build_period_detail(ws, m, raw['store_id'], raw['period'],
                                    ws.state(raw['store_id'], raw['period']))
    assert 'pending_labor_cut' not in snap
    assert snap['commission']['people'][0]['amount'] == 25
    assert 'amount_after_labor' not in snap['commission']['people'][0]
    ws.close()


def test_accepting_keep_scaled_payouts_uses_unique_rate_after_cost_confirm(tmp_path):
    from decimal import Decimal
    from ledger.commission_reports import labor_keep, money_float

    m = model(); ws = Workspace(tmp_path); registry = Registry(tmp_path)
    members = [registry.person_save({'name': name}, 'test', '登记')
               for name in ('陈慨', '石紫莹')]
    raw = result(m, members[0])
    raw['commission']['people'] = [
        {'person_id': members[0]['id'], 'person': '陈慨', 'amount': 2347.44,
         'allocated_sales': 132634.54, 'allocated_gross': 85990.03,
         'allocated_profit': 46947.61},
        {'person_id': members[1]['id'], 'person': '石紫莹', 'amount': 29.28,
         'allocated_sales': 2759.80, 'allocated_gross': 1532.69,
         'allocated_profit': 586.77},
    ]
    raw['commission'].update(
        engine='commission-v2', base_node='net_profit', base_total=47861.18,
        total=2376.72, amount_complete=False,
        products=[{'product_id': '1', 'total_rate': 0.05, 'people': [
            {'person_id': person['id']} for person in members]}],
    )
    raw['calculation_inputs']['metric_totals'].update({
        'goods_cost': -40000.0, 'goods_return_cost': 0.0,
        'dropship_cost': 0.0, 'reshipment_cost': 0.0,
    })
    keep = labor_keep(47861.18, 6090.41)
    keep_payouts = [
        {'person_id': person['person_id'],
         'amount': f"{money_float(Decimal(str(person['amount'])) * keep):.2f}"}
        for person in raw['commission']['people']
    ]
    keep_total = sum(Decimal(item['amount']) for item in keep_payouts)
    decided, _ = manual_cost.certified(
        m, raw, 1,
        {'goods': '49670.17', 'dropship': '164.60', 'reshipment': '221.89'},
        keep_payouts, False, '页面预填的扣兼职后试算', labor_cut='6090.41')
    total = decided['commission']['total']
    profit = decided['manual_cost']['profit']
    assert profit is not None
    after = round(profit - 6090.41, 2)
    assert total <= after * 0.05 + 0.01
    assert Decimal(str(total)) != keep_total
    ws.close()
