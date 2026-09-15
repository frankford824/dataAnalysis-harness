"""Stored payout reporting must not change money or substitute closed snapshots."""
import csv
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledger.commission_api import install
from ledger.commission_registry import Registry
from ledger.model.schema import Overhead, Store
from ledger.workspace import Workspace
from test_commission import _model


def fixture(tmp_path):
    ws = Workspace(tmp_path)
    registry = Registry(tmp_path)
    model = _model(stores=tuple(Store(id=f's{i}', name=f'店铺{i}', platform='taobao') for i in range(1, 4)))
    people = [registry.person_save({'name': name, 'employee_no': f'000{i}'}, 'test', '登记')
              for i, name in enumerate(['甲', '乙', '无记录人员'])]
    app = FastAPI()
    install(app, lambda: ws, lambda: model)
    return ws, registry, people, TestClient(app)


def record(ws, people, store, period, amounts, *, complete=True, legacy=False):
    rows = [{'person_id': p['id'], 'person': p['name'], 'amount': amount, 'base': 100}
            for p, amount in zip(people, amounts)]
    if legacy:
        for row in rows:
            row.pop('person_id')
    from decimal import Decimal
    return ws.record(store, period, {'can_close': True, 'findings': [], 'missing_sources': [],
        'commission': {'engine': 'legacy' if legacy else 'commission-v2', 'people': rows,
                       'total': float(sum((Decimal(str(a)) for a in amounts), Decimal(0))),
                       'amount_complete': complete, 'base_name': '利润', 'unassigned_orders': 0 if complete else 2}}, [])


@pytest.mark.parametrize('stores,indices,expected', [
    ([], [0], 13.32), ([], [0, 1], 36.65), (['s1'], [], 33.32),
    (['s1', 's2'], [], 36.65), (['s2'], [0], 3.33), (['s2'], [1], 0),
])
def test_people_and_store_filters_intersect_without_duplicate_runs(tmp_path, stores, indices, expected):
    ws, _, people, client = fixture(tmp_path)
    record(ws, people, 's1', '2026-06', [999, 888])  # superseded
    record(ws, people, 's1', '2026-06', [10.01, 20.02])
    record(ws, people, 's1', '2026-07', [-.02, 3.31])
    record(ws, people, 's2', '2026-06', [3.33], complete=False)
    result = client.post('/api/commission-v2/reports/query', json={
        'start': '2026-06', 'end': '2026-07', 'store_ids': stores,
        'person_ids': [people[i]['id'] for i in indices]})
    assert result.status_code == 200, result.text
    report = result.json()
    assert report['total'] == expected
    assert len(report['run_ids']) == (1 if stores == ['s2'] else 2 if stores == ['s1'] else 3)
    if stores == ['s2'] and indices == [1]:
        assert report['people'][0]['amount'] is None


def test_export_pins_viewed_runs_and_never_replaces_closed_result(tmp_path):
    ws, _, people, c = fixture(tmp_path)
    closed = record(ws, people, 's1', '2026-06', [12.34])
    ws.close_period('s1', '2026-06', by='test', note='结账')
    record(ws, people, 's1', '2026-06', [999])
    opened = record(ws, people, 's2', '2026-06', [-1.23])
    scope = {'start': '2026-06', 'end': '2026-06', 'store_ids': ['s1', 's2']}
    report = c.post('/api/commission-v2/reports/query', json=scope).json()
    assert report['total'] == 11.11
    assert report['run_ids'] == [closed, opened]
    record(ws, people, 's2', '2026-06', [888])
    export_scope = {**scope, 'run_ids': report['run_ids'], 'fingerprint': report['fingerprint']}
    for kind, amount_column in [('people', '提成金额'), ('stores', '提成金额'),
                                ('breakdown', '提成金额'), ('coverage', '筛选范围提成金额')]:
        response = c.post('/api/commission-v2/export/reports/'+kind, json=export_scope)
        assert response.status_code == 200, response.text
        assert response.content.startswith(b'\xef\xbb\xbf')
        rows = list(csv.DictReader(io.StringIO(response.text.lstrip('\ufeff'))))
        from decimal import Decimal
        assert sum(Decimal(r[amount_column]) for r in rows) == Decimal('11.11')
        if kind == 'people':
            assert rows[0]['工号'] == "'0000"
    assert ws.state('s1', '2026-06').run_id == closed
    ws.close_period('s2', '2026-06', by='test', note='状态变化')
    # The displayed open record remains historical; it must not silently become a closed payout.
    # Closing to a newer run doesn't change its open status or amounts, so export is still safe.
    assert c.post('/api/commission-v2/export/reports/people', json=export_scope).status_code == 200


def test_missing_zero_legacy_names_and_changed_state(tmp_path):
    ws, registry, people, c = fixture(tmp_path)
    zero = record(ws, people, 's1', '2026-06', [])
    scope = {'start': '2026-06', 'end': '2026-06'}
    report = c.post('/api/commission-v2/reports/query', json=scope).json()
    assert report['total'] == 0
    assert report['stores'][1]['amount'] is None
    assert report['missing_periods'] == 2
    ws.close_period('s1', '2026-06', by='test', note='结账')
    assert c.post('/api/commission-v2/export/reports/people', json={
        **scope, 'run_ids': [zero], 'fingerprint': report['fingerprint']}).status_code == 409
    record(ws, people, 's1', '2026-07', [1], legacy=True)
    record(ws, people, 's2', '2026-07', [2], legacy=True)
    scope = {'start': '2026-07', 'end': '2026-07'}
    report = c.post('/api/commission-v2/reports/query', json=scope).json()
    assert len(report['people']) == 2  # same name is not proof of identity
    assert len({p['person_id'] for p in report['people']}) == 2
    scope['person_ids'] = [report['people'][0]['person_id']]
    assert len(c.post('/api/commission-v2/reports/query', json=scope).json()['rows']) == 1


@pytest.mark.parametrize('patch', [
    {'start': '2026-13'}, {'end': '2026-05'}, {'end': '2037-01'},
    {'store_ids': ['unknown']}, {'person_ids': ['unknown']}, {'run_ids': [999]},
    {'run_ids': [1, 1]},
])
def test_invalid_scopes_fail_without_exporting_misleading_amounts(tmp_path, patch):
    _, _, _, c = fixture(tmp_path)
    scope = {'start': '2026-06', 'end': '2026-06', **patch}
    assert c.post('/api/commission-v2/reports/query', json=scope).status_code == 400
    assert c.post('/api/commission-v2/export/reports/people', json=scope).status_code == 400


def test_empty_commission_and_unknown_export_kind(tmp_path):
    ws, _, _, c = fixture(tmp_path)
    ws.record('s1', '2026-06', {'commission': {'notes': ['未算']}}, [])
    scope = {'start': '2026-06', 'end': '2026-06'}
    report = c.post('/api/commission-v2/reports/query', json=scope).json()
    assert report['total'] is None and report['missing_periods'] == 3
    assert c.post('/api/commission-v2/export/reports/unknown', json=scope).status_code == 400


def test_v2_legacy_name_ids_do_not_merge_across_stores(tmp_path):
    ws, _, _, c = fixture(tmp_path)
    historical = [{'id': 'legacy:same-name-hash', 'name': '同名人员'}]
    record(ws, historical, 's1', '2026-06', [10])
    record(ws, historical, 's2', '2026-06', [20])
    report = c.post('/api/commission-v2/reports/query', json={
        'start': '2026-06', 'end': '2026-06'}).json()
    assert len(report['people']) == 2
    assert sorted(p['amount'] for p in report['people']) == [10, 20]


def test_configured_people_remain_visible_when_pricing_has_no_amount(tmp_path):
    from test_commission_v2 import segment
    ws,registry,people,client=fixture(tmp_path)
    registry.save_scheme('s1','p1',{'segments':[segment('2026-06-01',people[0]['id'])]},'test','配置',publish=True)
    registry.save_scheme('s1','p2',{'segments':[segment('2026-07-01',people[1]['id'])]},'test','下月配置',publish=True)
    ws.record('s1','2026-06',{'commission':{'engine':'commission-v2','total':None,'people':[],
        'amount_complete':False,'pricing_pending_count':2}},[])
    report=client.post('/api/commission-v2/reports/query',json={'start':'2026-06','end':'2026-06','store_ids':['s1']}).json()
    store=report['stores'][0]
    assert store['configured_people']==1
    assert store['people']==0 and store['amount'] is None
    assert '成本覆盖不足' in store['status']


def test_threshold_met_cost_gaps_keep_commission_amount_available(tmp_path):
    ws,_,people,client=fixture(tmp_path)
    rid=record(ws,people,'s1','2026-06',[12.34])
    row=ws.conn.execute('SELECT result FROM run WHERE id=?',(rid,)).fetchone()
    import json
    payload=json.loads(row['result'])
    payload['commission']['pricing_pending_count']=8
    payload['commission']['pricing_threshold_met']=True
    payload['commission']['amount_complete']=True
    ws.conn.execute('UPDATE run SET result=? WHERE id=?',(json.dumps(payload),rid));ws.conn.commit()
    report=client.post('/api/commission-v2/reports/query',json={'start':'2026-06','end':'2026-06','store_ids':['s1']}).json()
    assert report['total']==12.34
    assert '成本覆盖不足' not in report['stores'][0]['status']


def test_labor_edit_changes_current_commission_report_and_is_visible_by_store(tmp_path):
    ws = Workspace(tmp_path)
    registry = Registry(tmp_path)
    stores = tuple(Store(id=f's{i}', name=f'店铺{i}', platform='taobao') for i in range(1, 3))
    model = _model(stores=stores)
    model = model.model_copy(update={
        'overheads': (Overhead(period='2026-06', amount=100, name='兼职人工费用'),),
        'statement': (model.statement[0].model_copy(update={'headline': 'revenue'}), *model.statement[1:]),
    })
    person = registry.person_save({'name': '甲', 'employee_no': '001'}, 'test', '登记')
    revenue = next(n.id for n in model.statement if n.headline == 'revenue')
    for sid in ('s1', 's2'):
        ws.record(sid, '2026-06', {
            'can_close': True, 'findings': [], 'missing_sources': [],
            'statement': [{'id': revenue, 'value': 1000, 'available': True}],
            'commission': {'engine': 'commission-v2', 'base_total': 100, 'total': 10,
                           'amount_complete': True, 'people': [
                               {'person_id': person['id'], 'person': '甲', 'amount': 10, 'base': 100},
                           ]},
        }, [])
    app = FastAPI(); install(app, lambda: ws, lambda: model)
    report = TestClient(app).post('/api/commission-v2/reports/query', json={
        'start': '2026-06', 'end': '2026-06', 'store_ids': ['s1'],
    }).json()
    assert report['total'] == 5
    assert report['stores'][0]['labor_cost'] == 50
    assert '兼职人工费用已分摊 50.00 元' in report['coverage'][0]['notes']


def test_closed_store_uses_frozen_labor_share(tmp_path):
    ws = Workspace(tmp_path)
    registry = Registry(tmp_path)
    model_root = tmp_path / 'model'; model_root.mkdir()
    model = _model(stores=(Store(id='s1', name='店铺1', platform='taobao'),))
    model = model.model_copy(update={
        'overheads': (Overhead(period='2026-06', amount=50, name='兼职人工费用'),),
        'statement': (model.statement[0].model_copy(update={'headline': 'revenue'}), *model.statement[1:]),
    })
    person = registry.person_save({'name': '甲'}, 'test', '登记')
    revenue = next(n.id for n in model.statement if n.headline == 'revenue')
    run_id = ws.record('s1', '2026-06', {
        'can_close': True, 'findings': [], 'missing_sources': [],
        'statement': [{'id': revenue, 'value': 1000, 'available': True}],
        'commission': {'engine': 'commission-v2', 'base_total': 100, 'total': 10,
                       'amount_complete': True, 'people': [
                           {'person_id': person['id'], 'person': '甲', 'amount': 10, 'base': 100},
                       ]},
    }, [])
    ws.close_period('s1', '2026-06', by='test', note='结账')
    (model_root / 'labor-closed-shares.csv').write_text(
        f'period,store_id,run_id,amount\n2026-06,s1,{run_id},20\n', encoding='utf-8',
    )
    app = FastAPI(); install(app, lambda: ws, lambda: model, model_root)
    report = TestClient(app).post('/api/commission-v2/reports/query', json={
        'start': '2026-06', 'end': '2026-06', 'store_ids': ['s1'],
    }).json()
    assert report['total'] == 8
    assert report['stores'][0]['labor_cost'] == 20


def test_employee_settlement_freezes_viewed_runs_and_reports_later_difference(tmp_path):
    ws, registry, people, client = fixture(tmp_path)
    original = record(ws, people, 's1', '2026-06', [10.01, 20.02])
    scope = {'start': '2026-06', 'end': '2026-06', 'store_ids': ['s1']}
    viewed = client.post('/api/commission-v2/reports/query', json=scope).json()
    settled = client.post('/api/commission-v2/settlements', json={
        **scope, 'run_ids': viewed['run_ids'], 'fingerprint': viewed['fingerprint'], 'note': '已与员工核对并发放',
    })
    assert settled.status_code == 200, settled.text
    body = settled.json()
    assert body['total'] == 30.03 and body['run_ids'] == [original]

    registry.person_save({**people[0], 'name': '甲（后来改名）'}, 'test', '人员资料更新', people[0]['revision'])
    record(ws, people, 's1', '2026-06', [15.01, 25.02])
    detail = client.get('/api/commission-v2/settlements/' + body['id']).json()
    assert detail['total'] == 30.03
    assert detail['current_total'] == 40.03
    assert detail['difference'] == 10
    assert detail['report']['run_ids'] == [original]
    assert {row['person'] for row in detail['report']['people']} == {'甲', '乙'}
    exported = client.get('/api/commission-v2/export/settlements/' + body['id'])
    assert exported.status_code == 200
    assert '甲（后来改名）' not in exported.text and '甲' in exported.text

    duplicate = client.post('/api/commission-v2/settlements', json={
        **scope, 'run_ids': viewed['run_ids'], 'fingerprint': viewed['fingerprint'], 'note': '重复点击',
    }).json()
    assert duplicate['id'] == body['id'] and duplicate['duplicate'] is True
    with pytest.raises(Exception, match='immutable'):
        with registry.transaction() as conn:
            conn.execute("UPDATE settlement SET total='1' WHERE id=?", (body['id'],))


def test_employee_settlement_rejects_incomplete_amounts(tmp_path):
    ws, _, people, client = fixture(tmp_path)
    record(ws, people, 's1', '2026-06', [3.33], complete=False)
    scope = {'start': '2026-06', 'end': '2026-06', 'store_ids': ['s1']}
    viewed = client.post('/api/commission-v2/reports/query', json=scope).json()
    response = client.post('/api/commission-v2/settlements', json={
        **scope, 'run_ids': viewed['run_ids'], 'fingerprint': viewed['fingerprint'], 'note': '不应结算',
    })
    assert response.status_code == 400
    assert '待核对' in response.json()['detail'] or '未完成' in response.json()['detail']
