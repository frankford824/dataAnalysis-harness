"""Stored payout reporting must not change money or substitute closed snapshots."""
import csv
import io
import json
from decimal import Decimal
from uuid import uuid4

import pytest
import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledger.commission_api import install
from ledger.commission_registry import Registry
from ledger.model.schema import Overhead, StatementNode, Store
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


def test_report_names_unassigned_profit_behind_partial_negative_payout(tmp_path):
    ws, _, people, client = fixture(tmp_path)
    ws.record('s1', '2026-06', {'statement': [], 'commission': {
        'engine': 'commission-v2', 'total': -140.95, 'base_total': 13269.90,
        'amount_complete': False, 'unassigned_orders': 2401,
        'unassigned_base': 16087.19,
        'people': [{'person_id': people[0]['id'], 'person': '甲',
                    'amount': -140.95, 'base': -2817.29}]}}, [])
    reply = client.post('/api/commission-v2/reports/query', json={
        'start': '2026-06', 'end': '2026-06', 'store_ids': ['s1'],
        'view': 'store_people'})
    assert reply.status_code == 200, reply.text
    report = reply.json()
    assert report['trial_periods'] == 1
    assert report['assignment_gaps'] == [
        {'store_id': 's1', 'store': '店铺1', 'period': '2026-06',
         'orders': 2401, 'base': 16087.19}]
    assert report['total'] == -140.95  # Audit preserves the partial trial amount.


def test_configuration_shows_unassigned_links_from_selected_order_month(tmp_path):
    from test_commission_v2 import segment
    ws, registry, people, client = fixture(tmp_path)
    scheme = registry.save_scheme('s1', '123456789001', {
        'segments': [segment('2026-09-01', people[0]['id'])]},
        'test', '后来才设置提成', publish=True)
    ws.record('s1', '2026-06', {'commission': {
        'engine': 'commission-v2', 'unassigned_orders': 4,
        'unassigned_base': 851.91, 'products': [
            {'product_id': '123456789001', 'product_name': '商品A',
             'unassigned': True, 'sub_orders': 3, 'base': 629.73},
            {'product_id': '', 'product_name': '',
             'unassigned': True, 'sub_orders': 1, 'base': 222.18}] }}, [])
    result = client.get('/api/commission-v2/unassigned', params={
        'store_id': 's1', 'period': '2026-06'})
    assert result.status_code == 200, result.text
    body = result.json()
    assert body['orders'] == 4 and body['base'] == 851.91
    assert body['link_count'] == 1 and body['without_product'] == 1
    assert body['links'][0]['scheme_id'] == scheme['id']
    assert body['links'][0]['product_id'] == '123456789001'


def test_all_unassigned_product_links_are_returned_for_bulk_assignment(tmp_path):
    ws, _, _, client = fixture(tmp_path)
    products=[{'product_id':str(100000000000+i),'product_name':f'商品{i}',
               'unassigned':True,'sub_orders':1,'base':1}
              for i in range(205)]
    ws.record('s1','2026-06',{'commission':{'engine':'commission-v2',
        'unassigned_orders':205,'unassigned_base':205,'products':products}},[])
    body=client.get('/api/commission-v2/unassigned',params={
        'store_id':'s1','period':'2026-06'}).json()
    assert body['link_count']==205 and len(body['links'])==205


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
    assert '成本待人工确认' in store['status']


def test_configured_people_scan_is_reused_and_published_changes_invalidate_it(tmp_path, monkeypatch):
    from ledger import commission_reports
    from test_commission_v2 import segment
    _, registry, people, _ = fixture(tmp_path)
    registry.save_scheme('s1','p1',{'segments':[segment('2026-06-01',people[0]['id'])]},
                         'tester','初版',publish=True)
    original = commission_reports._scan_configured_people
    scans = []
    def counted(*args, **kwargs):
        scans.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(commission_reports,'_scan_configured_people',counted)
    for _ in range(2):
        assert commission_reports.configured_people(registry,'2026-06','2026-06',('s1',)) == {
            's1':{people[0]['id']},
        }
    assert len(scans) == 1
    registry.save_scheme('s1','p1',{'segments':[segment('2026-06-01',people[1]['id'])]},
                         'tester','已改人员',expected=1,publish=True)
    assert commission_reports.configured_people(registry,'2026-06','2026-06',('s1',)) == {
        's1':{people[1]['id']},
    }
    assert len(scans) == 2


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
    assert '成本待人工确认' not in report['stores'][0]['status']


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


def test_store_person_composition_shows_store_amounts_once_and_filters_people(tmp_path):
    ws, _, people, client = fixture(tmp_path)
    rid = ws.record('s1', '2026-06', {
        'can_close': True, 'findings': [], 'missing_sources': [],
        'statement': [
            {'id': 'n_receipt', 'value': 1000, 'available': True},
            {'id': 'gross', 'value': 400, 'available': True},
        ],
        'commission': {'engine': 'commission-v2', 'people': [
            {'person_id': people[0]['id'], 'person': '甲', 'amount': 12.34, 'base': 250,
             'sales': 1000, 'gross': 400, 'allocated_sales':700, 'allocated_gross':280,
             'allocated_profit':250},
            {'person_id': people[1]['id'], 'person': '乙', 'amount': 3.21, 'base': 100,
             'sales': 1000, 'gross': 400, 'allocated_sales':300, 'allocated_gross':120,
             'allocated_profit':100},
        ], 'total': 15.55, 'amount_complete': True, 'base_name': '利润'},
    }, [])
    scope = {'start': '2026-06', 'end': '2026-06', 'store_ids': ['s1'], 'view': 'store_people'}
    query = client.post('/api/commission-v2/reports/query', json=scope)
    assert query.status_code == 200, query.text
    rows = query.json()['items']
    assert len(rows) == 3
    assert rows[0]['kind'] == 'store' and rows[0]['sales'] == 1000 and rows[0]['gross'] == 400
    assert rows[0]['amount'] is None and rows[0]['store_amount'] == 15.55
    assert [r['amount'] for r in rows[1:]] == [12.34, 3.21]
    assert [(r['sales'],r['gross'],r['labor_cost']) for r in rows[1:]]==[
        (700,280,None),(300,120,None)]
    assert all(r['finance_run'] == rid for r in rows)
    filtered = client.post('/api/commission-v2/reports/query', json={
        **scope, 'person_ids': [people[0]['id']],
    }).json()['items']
    assert len(filtered) == 2 and filtered[0]['store_amount'] == 12.34
    assert filtered[0]['sales'] == 1000 and filtered[1]['sales']==700 and filtered[1]['amount'] == 12.34
    record(ws, [people[1]], 's2', '2026-06', [3.21])
    own_shops = client.post('/api/commission-v2/reports/query', json={
        'start': '2026-06', 'end': '2026-06',
        'person_ids': [people[0]['id']], 'view': 'store_people',
    }).json()
    assert own_shops['store_count'] == 1 and own_shops['count'] == 2
    assert {row['store_id'] for row in own_shops['items']} == {'s1'}
    assert own_shops['total'] == 12.34
    export = client.post('/api/commission-v2/export/reports/store_people', json={
        'start': '2026-06', 'end': '2026-06', 'store_ids': ['s1'], 'presentation': True,
    })
    assert export.status_code == 200, export.text
    export_rows = list(csv.DictReader(io.StringIO(export.text.lstrip('\ufeff'))))
    assert [r['销售额/参与销售额'] for r in export_rows] == ['1000', '700.0', '300.0']
    assert [r['提成额'] for r in export_rows] == ['', '12.34', '3.21']


def test_store_person_profit_after_labor_keeps_full_participation_and_export(tmp_path):
    ws = Workspace(tmp_path)
    registry = Registry(tmp_path)
    model = _model(stores=(Store(id='s1', name='店铺1', platform='taobao'),
                           Store(id='s2', name='店铺2', platform='taobao')))
    model = model.model_copy(update={
        'overheads': (Overhead(period='2026-06', amount=100, name='兼职人工费用'),),
        'statement': (model.statement[0].model_copy(update={'headline': 'revenue'}), *model.statement[1:],
                      StatementNode(id='net_profit', name='利润', level=1,
                                    is_total=True, headline='profit',
                                    formula={'op':'add','of':['gross']})),
    })
    people = [registry.person_save({'name': name}, 'test', '登记') for name in ('甲', '乙')]
    ws.record('s2', '2026-06', {
        'statement': [{'id': model.statement[0].id, 'value': 1000, 'available': True}],
    }, [])
    ws.record('s1', '2026-06', {
        'statement': [{'id': model.statement[0].id, 'value': 1000, 'available': True},
                      {'id': 'gross', 'value': 400, 'available': True},
                      {'id': 'net_profit', 'value': 200, 'available': True}],
        'commission': {'engine': 'commission-v2', 'base_node':'net_profit',
                       'base_total': 100, 'total': 15,
                       'amount_complete': True, 'people': [
                           {'person_id': p['id'], 'person': p['name'], 'amount': amount,
                            'sales': 1000, 'gross': 400, 'profit': 200,
                            'allocated_sales': allocated_sales,
                            'allocated_gross': allocated_gross,
                            'allocated_profit': allocated}
                           for p, amount, allocated_sales,allocated_gross,allocated in zip(
                               people,(10,5),(600,400),(240,160),(120,80))],
                       'products':[{'product_id':'p1','total_rate':.05,'people':[
                           {'person_id':p['id'],'amount':amount}
                           for p,amount in zip(people,(10,5))]}]},
    }, [])
    app = FastAPI(); install(app, lambda: ws, lambda: model)
    client = TestClient(app)
    scope = {'start': '2026-06', 'end': '2026-06', 'store_ids': ['s1'], 'view': 'store_people'}
    report = client.post('/api/commission-v2/reports/query', json=scope).json()
    store, first, second = report['items']
    assert store['gross'] == 400 and store['labor_cost'] == 50
    assert store['profit_after_labor'] == 150
    assert [(p['sales'],p['gross'], p['profit_after_labor'], p['labor_cost']) for p in (first, second)] == [
        (600,240, 90, None), (400,160, 60, None)]
    assert [first['amount'],second['amount']]==[4.5,3.0]
    assert report['total'] == 7.5  # existing payout calculation remains unchanged
    filtered = client.post('/api/commission-v2/reports/query', json={
        **scope, 'person_ids': [people[0]['id']],
    }).json()['items']
    assert len(filtered) == 2 and filtered[1]['profit_after_labor'] == 90
    export = client.post('/api/commission-v2/export/reports/store_people', json={
        **scope, 'presentation': True,
    })
    assert export.status_code == 200, export.text
    exported = list(csv.DictReader(io.StringIO(export.text.lstrip('\ufeff'))))
    assert [r['利润额'] for r in exported] == ['150.0', '90.0', '60.0']
    assert [r['兼职额'] for r in exported] == ['50.0', '', '']
    raw_export = client.post('/api/commission-v2/export/reports/store_people', json=scope)
    assert raw_export.status_code == 200
    raw_rows = list(csv.DictReader(io.StringIO(raw_export.text.lstrip('\ufeff'))))
    assert [r['利润额'] for r in raw_rows] == ['150.0', '90.0', '60.0']


def test_store_person_report_exposes_unattributed_output_instead_of_hiding_gap(tmp_path):
    ws=Workspace(tmp_path);registry=Registry(tmp_path)
    model=_model(stores=(Store(id='s1',name='店铺1',platform='taobao'),))
    model=model.model_copy(update={
        'statement':(model.statement[0].model_copy(update={'headline':'revenue'}),
                     *model.statement[1:],
                     StatementNode(id='net_profit',name='利润',level=1,is_total=True,
                                   headline='profit',formula={'op':'add','of':['gross']})),
    })
    people=[registry.person_save({'name':name},'test','登记') for name in ('甲','乙')]
    ws.record('s1','2026-06',{
        'statement':[{'id':model.statement[0].id,'value':100,'available':True},
                     {'id':'gross','value':80,'available':True},
                     {'id':'net_profit','value':60,'available':True}],
        'commission':{'engine':'commission-v2','base_node':'net_profit',
                      'base_total':60,'total':4,'amount_complete':False,
                      'unassigned_orders':2,'unassigned_base':12,
                      'people':[
                          {'person_id':people[0]['id'],'person':'甲','amount':3,
                           'allocated_sales':60,'allocated_gross':48,'allocated_profit':36},
                          {'person_id':people[1]['id'],'person':'乙','amount':1,
                           'allocated_sales':20,'allocated_gross':16,'allocated_profit':12},
                      ]},
    },[])
    app=FastAPI();install(app,lambda:ws,lambda:model)
    rows=TestClient(app).post('/api/commission-v2/reports/query',json={
        'start':'2026-06','end':'2026-06','store_ids':['s1'],
        'view':'store_people'}).json()['items']
    store,*members=rows
    assert [row['kind'] for row in members]==['person','person','unassigned']
    assert members[-1]['person']=='未分配（2笔订单信息不完整）'
    assert (members[-1]['sales'],members[-1]['gross'],members[-1]['profit_after_labor'])==(20,16,12)
    assert sum(row['profit_after_labor'] for row in members)==store['profit_after_labor']==60


@pytest.mark.parametrize('store_id,store_profit,labor,assigned_profit,sales,trial,rate,expected', [
    ('douyin_mszr2dhn', 46686.67, 5477.63, 46108.09, 123571.79, 2302.64, .05, 2060.45),
    ('douyin_mt9sbkne', 3460.11, 647.24, 2608.20, 10804.25, 130.50, .05, 140.64),
    ('pdd_yidali', 45000, 500, 44322.17, 12000, 1326.95, .03, 1335.00),
])
def test_single_owner_uniform_rate_uses_full_store_profit_after_labor(
        tmp_path, store_id, store_profit, labor, assigned_profit, sales, trial, rate, expected):
    ws=Workspace(tmp_path);registry=Registry(tmp_path)
    model=_model(stores=(Store(id=store_id,name='蔡果店',platform='taobao'),))
    model=model.model_copy(update={
        'overheads':(Overhead(period='2026-06',amount=labor,name='兼职人工费用'),),
        'statement':(model.statement[0].model_copy(update={'headline':'revenue'}),
                     *model.statement[1:],
                     StatementNode(id='net_profit',name='利润',level=1,is_total=True,
                                   headline='profit',formula={'op':'add','of':['gross']})),
    })
    pid='legacy:5811db93188b314a53ce01f5'
    registry.person_save({'id':pid,'name':'蔡果'},'test','登记')
    run=ws.record(store_id,'2026-06',{
        'statement':[{'id':model.statement[0].id,'value':sales+500,'available':True},
                     {'id':'gross','value':assigned_profit+400,'available':True},
                     {'id':'net_profit','value':store_profit,'available':True}],
        'commission':{'engine':'commission-v2','base_node':'net_profit',
                      'base_total':store_profit,'on_loss':'deduct',
                      'amount_complete':False,'unassigned_orders':1,
                      'total':trial,'people':[{'person_id':pid,'person':'蔡果',
                          'amount':trial,'base':assigned_profit,
                          'allocated_profit':assigned_profit,'profit':assigned_profit,
                          'sales':sales,'gross':assigned_profit+100}],
                      'products':[{'product_id':'12345678901','total_rate':rate,
                          'people':[{'person_id':pid,'person':'蔡果','amount':trial}]}]},
    },[])
    app=FastAPI();install(app,lambda:ws,lambda:model)
    client=TestClient(app)
    report=client.post('/api/commission-v2/reports/query',json={
        'start':'2026-06','end':'2026-06','store_ids':[store_id],
        'view':'store_people'}).json()
    assert report['trial_periods']==1
    assert report['items'][1]['profit_after_labor']==round(store_profit-labor,2)
    assert report['items'][1]['amount']==expected
    assert report['total']==expected
    context=client.get('/api/commission-v2/payout-confirmations/context',params={
        'store_id':store_id,'period':'2026-06','run_id':run}).json()
    assert context['people'][0]['suggested']==expected
    human=client.post('/api/commission-v2/payout-confirmations',json={
        'store_id':store_id,'period':'2026-06','run_id':run,
        'source_sha':context['source_sha'],'reason':'运营另行确认本期提成',
        'payouts':[{'person_id':pid,'amount':'99.00'}]})
    assert human.status_code==200,human.text
    confirmed=client.post('/api/commission-v2/reports/query',json={
        'start':'2026-06','end':'2026-06','store_ids':[store_id],
        'view':'store_people'}).json()
    assert confirmed['items'][1]['amount']==99
    assert confirmed['trial_periods']==0


def test_single_owner_attributes_full_profit_even_with_legacy_skip_loss(tmp_path):
    ws=Workspace(tmp_path);registry=Registry(tmp_path)
    store_id='douyin_qianhuajian';pid='legacy:5811db93188b314a53ce01f5'
    registry.person_save({'id':pid,'name':'蔡果'},'test','登记')
    model=_model(stores=(Store(id=store_id,name='抖音浅花涧',platform='taobao'),))
    model=model.model_copy(update={
        'overheads':(Overhead(period='2026-06',amount=393.18,name='兼职人工费用'),),
        'statement':(model.statement[0].model_copy(update={'headline':'revenue'}),
                     *model.statement[1:],
                     StatementNode(id='net_profit',name='利润',level=1,is_total=True,
                                   headline='profit',formula={'op':'add','of':['gross']})),
    })
    ws.record(store_id,'2026-06',{
        'statement':[{'id':model.statement[0].id,'value':9000,'available':True},
                     {'id':'gross','value':6000,'available':True},
                     {'id':'net_profit','value':2374.86,'available':True}],
        'commission':{'engine':'commission-v2','base_node':'net_profit',
            'base_total':2374.86,'on_loss':'skip','total':165.45,
            'amount_complete':False,'unassigned_orders':1,
            'people':[{'person_id':pid,'person':'蔡果','amount':165.45,
                       'base':3309.78,'allocated_profit':2344.94,
                       'sales':8900,'gross':5900,'profit':2344.94}],
            'products':[{'product_id':'12345678901','total_rate':0.05,
                         'people':[{'person_id':pid,'amount':165.45}]}]},
    },[])
    app=FastAPI();install(app,lambda:ws,lambda:model)
    report=TestClient(app).post('/api/commission-v2/reports/query',json={
        'start':'2026-06','end':'2026-06','store_ids':[store_id],
        'view':'store_people'}).json()
    assert report['items'][1]['profit_after_labor']==1981.68
    assert report['items'][1]['amount']==99.08


def test_single_owner_with_multiple_effective_rates_keeps_order_payout(tmp_path):
    from ledger.commission_reports import confirmed_profit_rate
    person_id='person:one'
    commission={'base_node':'net_profit','people':[{'person_id':person_id}],
        'products':[{'total_rate':.03,'people':[{'person_id':person_id}]},
                    {'total_rate':.05,'people':[{'person_id':person_id}]}]}
    assert confirmed_profit_rate(commission,person_id,'any_store') is None
    commission['products'][0]['total_rate']=0
    assert float(confirmed_profit_rate(commission,person_id,'any_store'))==0.05


def test_manual_cost_unique_rate_uses_store_profit_after_labor(tmp_path):
    from ledger.commission_reports import suggested_payouts
    ws=Workspace(tmp_path);registry=Registry(tmp_path)
    model=_model(stores=(Store(id='s1',name='拾梦小屋',platform='taobao'),))
    model=model.model_copy(update={
        'overheads':(Overhead(period='2026-06',amount=6090.41,name='兼职人工费用'),),
        'statement':(model.statement[0].model_copy(update={'headline':'revenue'}),
                     *model.statement[1:],
                     StatementNode(id='net_profit',name='利润',level=1,is_total=True,
                                   headline='profit',formula={'op':'add','of':['gross']})),
    })
    members=[registry.person_save({'name':name},'test','登记')
             for name in ('陈慨','石紫莹','黄颖','杨舒')]
    people=[{'person_id':p['id'],'person':p['name'],'amount':amount,
             'allocated_sales':sales,'allocated_gross':gross,'allocated_profit':profit,
             'sales':sales,'gross':gross,'profit':profit}
            for p,amount,sales,gross,profit in zip(
                members,(2347.44,29.28,13.58,1.18),
                (132634.54,2759.80,1407.28,54.06),
                (85990.03,1532.69,1184.18,42.35),
                (46947.61,586.77,265.74,23.84))]
    commission={'engine':'commission-v2','base_node':'net_profit','base_total':46870.46,
                'on_loss':'deduct','total':2391.48,'amount_complete':True,
                'unassigned_orders':88,'unassigned_base':-217.22,
                'manual_confirmed':True,'manual_amounts_after_labor':True,
                'people':[{**row,'amount':after} for row,after in zip(people,(2044.95,25.51,11.83,1.03))],
                'products':[{'product_id':'1','total_rate':0.05,'people':[
                    {'person_id':p['id']} for p in members]},
                            {'product_id':'2','total_rate':0.0,'people':[]}]}
    ws.record('s1','2026-06',{
        'statement':[{'id':model.statement[0].id,'value':138115.17,'available':True},
                     {'id':'gross','value':88058.51,'available':True},
                     {'id':'net_profit','value':46870.46,'available':True}],
        'manual_cost':{'profit':46870.46,'confirmed':{'goods':49670.17}},
        'commission':commission,
    },[])
    app=FastAPI();install(app,lambda:ws,lambda:model)
    report=TestClient(app).post('/api/commission-v2/reports/query',json={
        'start':'2026-06','end':'2026-06','store_ids':['s1'],
        'view':'store_people'}).json()
    store,*rows=report['items']
    people_rows=[row for row in rows if row['kind']=='person']
    assert store['profit_after_labor']==40780.05
    assert abs(report['total']-2039)<=0.01
    assert abs(sum(row['amount'] for row in people_rows)-2039)<=0.01
    assert abs(sum(row['profit_after_labor'] for row in people_rows)-40780.05)<=0.01
    assert report['total']<=40780.05*0.05+0.01
    source={**commission,'people':people,'manual_amounts_after_labor':False}
    suggested=suggested_payouts(source,6090.41,operating=46870.46)
    assert abs(sum(suggested.values())-2039)<=0.01


def test_unattributed_store_loss_is_shared_without_assigning_unknown_orders(tmp_path):
    ws = Workspace(tmp_path); registry = Registry(tmp_path)
    model = _model(stores=(Store(id='s1', name='1688南京朗歆', platform='taobao'),))
    model = model.model_copy(update={
        'overheads':(Overhead(period='2026-06',amount=3511.01,name='兼职人工费用'),),
        'statement':(model.statement[0].model_copy(update={'headline':'revenue'}),
                     *model.statement[1:],
                     StatementNode(id='net_profit',name='利润',level=1,is_total=True,
                                   headline='profit',formula={'op':'add','of':['gross']})),
    })
    members=[registry.person_save({'name':n},'test','登记') for n in ('姜慧卉','邱倩倩')]
    ws.record('s1','2026-06',{
        'statement':[{'id':model.statement[0].id,'value':79620.89,'available':True},
                     {'id':'gross','value':50834.99,'available':True},
                     {'id':'net_profit','value':44367.84,'available':True}],
        'commission':{'engine':'commission-v2','base_node':'net_profit',
                      'base_total':44367.84,'amount_complete':False,
                      'unassigned_orders':4,'unassigned_base':-72.76,
                      'total':2046.04,'people':[
                              {'person_id':p['id'],'person':p['name'],'amount':amount,
                               'base':basis,'allocated_sales':sales,
                               'allocated_gross':gross,'allocated_profit':basis,
                               'sales':sales,'gross':gross,'profit':basis}
                          for p,amount,basis,sales,gross in zip(
                              members,(1483.68,562.36),(32228.45,12212.14),
                              (58636.06,20932.91),(37015.81,13860.2))]},
    },[])
    app=FastAPI(); install(app,lambda:ws,lambda:model)
    rows=TestClient(app).post('/api/commission-v2/reports/query',json={
        'start':'2026-06','end':'2026-06','store_ids':['s1'],
        'view':'store_people'}).json()['items']
    store,*people=rows
    assert store['profit_after_labor']==40856.83
    assert [p['profit_after_labor'] for p in people]==[29587.50,11269.33,0]
    assert sum(p['profit_after_labor'] for p in people)==store['profit_after_labor']
    assert [p['sales'] for p in people]==[58636.06,20932.91,51.92]
    assert people[-1]['kind']=='unassigned'
    assert people[-1]['person']=='未分配（4笔订单信息不完整）'
    assert round(sum(p['gross'] for p in people),2)==store['gross']
    assert [p['gross'] for p in people]==[36985.58,13849.41,0]


def test_archived_order_details_supply_additive_profit_without_mutating_run(tmp_path, monkeypatch):
    from ledger.commission_engine import persist
    ws=Workspace(tmp_path); registry=Registry(tmp_path)
    model=_model(stores=(Store(id='s1',name='共享链接店',platform='taobao'),))
    model=model.model_copy(update={
        'overheads':(Overhead(period='2026-06',amount=20,name='兼职人工费用'),),
        'statement':(model.statement[0].model_copy(update={'headline':'revenue'}),
                     *model.statement[1:],
                     StatementNode(id='net_profit',name='利润',level=1,is_total=True,
                                   headline='profit',formula={'op':'add','of':['gross']})),
    })
    crew=[registry.person_save({'name':name},'test','登记') for name in ('甲','乙')]
    calc=str(uuid4())
    archived={'statement':[{'id':model.statement[0].id,'value':100,'available':True},
                           {'id':'gross','value':80,'available':True},
                           {'id':'net_profit','value':60,'available':True}],
              'commission':{'engine':'commission-v2','base_node':'net_profit',
                            'calculation_id':calc,
                            'base_total':60,'total':5,'amount_complete':True,
                            'people':[{'person_id':p['id'],'person':p['name'],
                                       'amount':amount,'sales':100,'gross':80,
                                       'profit':60,'base':60}
                                      for p,amount in zip(crew,(3,2))]}}
    run=ws.record('s1','2026-06',archived,[])
    # Save a separate immutable calculation archive, as the production v2
    # engine does; the old finance snapshot contains no allocated-profit field.
    details=pl.DataFrame({'status':['distribute','distribute'],
                          'person_id':[p['id'] for p in crew],
                          'share':['0.03','0.02'],'total_rate':['0.05','0.05'],
                          'participation_sales':[100.,100.],
                          'participation_gross':[80.,80.],
                          'participation_profit':[60.,60.],
                          'original_base':[60.,60.]})
    persist(registry,run,details,{'id':calc,'store_id':'s1','period':'2026-06',
        'registry_revision':registry.revision(),'model_json':model.model_dump_json(),
        'rules_json':'{}','summary_json':'{}'})
    app=FastAPI();install(app,lambda:ws,lambda:model)
    client=TestClient(app)
    reads=[]; original_read=pl.read_parquet
    def counted_read(*args,**kwargs):
        reads.append(True)
        return original_read(*args,**kwargs)
    monkeypatch.setattr(pl,'read_parquet',counted_read)
    values=[client.post('/api/commission-v2/reports/query',json={
        'start':'2026-06','end':'2026-06','store_ids':['s1'],
        'view':'store_people'}).json()['items'] for _ in range(2)]
    assert all([row['profit_after_labor'] for row in visible]==[40,24,16]
               for visible in values)
    assert len(reads)==1  # Immutable detail archive is reused across refreshed reads.
    assert json.loads(ws.conn.execute('SELECT result FROM run WHERE id=?',(run,)).fetchone()[0])==archived




def test_pending_payout_keeps_personal_sales_without_inventing_commission(tmp_path):
    ws, _, people, client = fixture(tmp_path)
    ws.record('s1', '2026-06', {
        'can_close': False,
        'statement': [{'id': 'n_receipt', 'value': 1000, 'available': True},
                      {'id': 'gross', 'value': None, 'available': False}],
        'commission': {'engine': 'commission-v2', 'total': None,
                       'pricing_pending_count': 0, 'pricing_threshold_met': False, 'people': [
                           {'person_id': people[0]['id'], 'person': '甲',
                            'amount': None, 'sales': 1000, 'gross': None},
                       ]},
    }, [])
    response = client.post('/api/commission-v2/reports/query', json={
        'start': '2026-06', 'end': '2026-06', 'store_ids': ['s1'],
        'view': 'store_people',
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['total'] is None
    assert result['items'][0]['sales'] == 1000
    assert result['items'][1]['person'] == '甲'
    assert result['items'][1]['sales'] == 1000
    assert result['items'][1]['amount'] is None and result['items'][1]['gross'] is None
    assert result['items'][0]['profit_after_labor'] is None
    assert result['items'][1]['profit_after_labor'] is None


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


def test_new_close_labor_cut_overrides_old_csv_and_later_config(tmp_path):
    ws = Workspace(tmp_path)
    registry = Registry(tmp_path)
    root = tmp_path / 'model'; root.mkdir()
    model = _model(stores=(Store(id='s1', name='店铺1', platform='taobao'),))
    model = model.model_copy(update={
        'overheads': (Overhead(period='2026-06', amount=100, name='兼职人工费用'),),
        'statement': (model.statement[0].model_copy(update={'headline': 'revenue'}), *model.statement[1:]),
    })
    person = registry.person_save({'name': '甲'}, 'test', '登记')
    run_id = ws.record('s1', '2026-06', {
        'can_close': True, 'findings': [], 'missing_sources': [],
        'statement': [{'id': model.statement[0].id, 'value': 1000, 'available': True}],
        'commission': {'engine': 'commission-v2', 'base_total': 100,
                       'total': 10, 'amount_complete': True, 'people': [
                           {'person_id': person['id'], 'person': '甲', 'amount': 10},
                       ]},
    }, [])
    ws.close_period('s1', '2026-06', labor_cut='30.00')
    (root / 'labor-closed-shares.csv').write_text(
        f'period,store_id,run_id,amount\n2026-06,s1,{run_id},20\n', encoding='utf-8',
    )
    app = FastAPI(); install(app, lambda: ws, lambda: model, root)
    report = TestClient(app).post('/api/commission-v2/reports/query', json={
        'start': '2026-06', 'end': '2026-06', 'store_ids': ['s1'],
    }).json()
    assert report['total'] == 7 and report['stores'][0]['labor_cost'] == 30
    ws.reopen_period('s1', '2026-06', note='回看旧结账金额')
    pinned = TestClient(app).post('/api/commission-v2/reports/query', json={
        'start': '2026-06', 'end': '2026-06', 'store_ids': ['s1'], 'run_ids': [run_id],
    }).json()
    assert pinned['total'] == 7 and pinned['stores'][0]['labor_cost'] == 30


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


def test_report_confirms_person_payout_without_closing_store_and_freezes_settlement(tmp_path):
    ws, registry, people, client = fixture(tmp_path)
    original_run = record(ws, people, 's1', '2026-06', [10, 20], complete=False)
    selection = {'start': '2026-06', 'end': '2026-06',
                 'store_ids': ['s1'], 'view': 'store_people'}
    before = client.post('/api/commission-v2/reports/query', json=selection).json()
    assert before['trial_periods'] == 1
    context = client.get('/api/commission-v2/payout-confirmations/context',
                         params={'store_id': 's1', 'period': '2026-06',
                                 'run_id': original_run}).json()
    assert [row['suggested'] for row in context['people']] == [10, 20]
    decision = {'store_id': 's1', 'period': '2026-06', 'run_id': original_run,
                'source_sha': context['source_sha'], 'reason': '已与两位运营核对提成',
                'payouts': [{'person_id': row['person_id'], 'amount': value}
                            for row, value in zip(context['people'], ('12.00', '13.00'))]}
    assert client.post('/api/commission-v2/payout-confirmations',
                       json={**decision, 'run_id': original_run + 1}).status_code == 409
    assert client.post('/api/commission-v2/payout-confirmations',
                       json={**decision, 'payouts': [{'person_id': 'unknown',
                                                     'amount': '12.00'}]}).status_code == 400
    assert client.post('/api/commission-v2/payout-confirmations',
                       json={**decision, 'payouts': [decision['payouts'][0],
                                                     {**decision['payouts'][1],
                                                      'amount': '13.001'}]}).status_code == 400
    saved = client.post('/api/commission-v2/payout-confirmations', json=decision)
    assert saved.status_code == 200, saved.text
    assert ws.state('s1', '2026-06').closed is False
    assert ws.latest_run('s1', '2026-06')['id'] == original_run
    assert json.loads(ws.conn.execute('SELECT result FROM run WHERE id=?',
                                      (original_run,)).fetchone()[0])['commission']['total'] == 30
    revised = client.post('/api/commission-v2/reports/query', json=selection).json()
    assert revised['trial_periods'] == 0 and revised['total'] == 25
    assert [row['amount'] for row in revised['items'][1:]] == [12, 13]
    assert all(row['status'] == '已人工确认' for row in revised['items'])
    assert client.post('/api/commission-v2/payout-confirmations', json=decision).status_code == 409
    with registry.connect() as conn:
        row = conn.execute('SELECT count(*) FROM payout_confirmation').fetchone()[0]
        assert row == 1
        with pytest.raises(Exception):
            conn.execute('UPDATE payout_confirmation SET confirmed_total=0')
    settlement = client.post('/api/commission-v2/settlements', json={
        'start': '2026-06', 'end': '2026-06', 'store_ids': ['s1'],
        'run_ids': revised['run_ids'], 'fingerprint': revised['fingerprint'],
        'note': '本店人员确认金额已结算'})
    assert settlement.status_code == 200, settlement.text
    settled_id = settlement.json()['id']
    record(ws, people, 's1', '2026-06', [40, 50], complete=False)
    live = client.post('/api/commission-v2/reports/query', json=selection).json()
    assert live['trial_periods'] == 1 and live['total'] == 90
    assert client.get('/api/commission-v2/settlements/' + settled_id).json()['total'] == 25
    refreshed = client.get('/api/commission-v2/payout-confirmations/context', params={
        'store_id': 's1', 'period': '2026-06',
        'run_id': ws.latest_run('s1', '2026-06')['id']}).json()
    assert refreshed['latest'] is None
    assert refreshed['history'][0]['confirmed_total'] == '25.00'


def test_every_report_tab_receives_confirmation_scope_relationships(tmp_path):
    ws, _, people, client = fixture(tmp_path)
    run1=record(ws,people,'s1','2026-06',[10,20],complete=False)
    run2=record(ws,people,'s1','2026-07',[30,40],complete=False)
    record(ws,people,'s2','2026-06',[3],complete=False)
    base={'start':'2026-06','end':'2026-07'}
    for view in ('store_people','people','stores','breakdown','coverage'):
        reply=client.post('/api/commission-v2/reports/query',json={
            **base,'view':view,'limit':200})
        assert reply.status_code==200,reply.text
        data=reply.json()
        assert data['view']==view
        scopes=data['confirmation_scopes']
        assert {(row['store_id'],row['period'],row['run_id']) for row in scopes} >= {
            ('s1','2026-06',run1),('s1','2026-07',run2)}
        person_scopes=data['person_confirmation_scopes']
        assert {(row['person_id'],row['store_id'],row['period'])
                for row in person_scopes} >= {
            (people[0]['id'],'s1','2026-06'),
            (people[0]['id'],'s1','2026-07')}


def test_incomplete_store_close_stays_pending_until_person_payout_is_confirmed(tmp_path):
    ws, _, people, client = fixture(tmp_path)
    run = record(ws, people, 's1', '2026-06', [10], complete=False)
    ws.close_period('s1', '2026-06', by='test', note='店铺经营账已人工确认')
    scope = {'start': '2026-06', 'end': '2026-06',
             'store_ids': ['s1'], 'view': 'store_people'}
    report = client.post('/api/commission-v2/reports/query', json=scope).json()
    assert report['trial_periods'] == 1
    context = client.get('/api/commission-v2/payout-confirmations/context',
                         params={'store_id': 's1', 'period': '2026-06',
                                 'run_id': run}).json()
    assert context['store_closed'] is True
    response = client.post('/api/commission-v2/payout-confirmations', json={
        'store_id': 's1', 'period': '2026-06', 'run_id': run,
        'source_sha': context['source_sha'], 'reason': '员工提成另行核对',
        'payouts': [{'person_id': context['people'][0]['person_id'],
                     'amount': '11.00'}]})
    assert response.status_code == 200, response.text
    confirmed = client.post('/api/commission-v2/reports/query', json=scope).json()
    assert confirmed['trial_periods'] == 0 and confirmed['total'] == 11
    assert ws.state('s1', '2026-06').closed is True


def test_registered_person_can_confirm_payout_with_goods_cost_still_missing(tmp_path):
    from test_commission_v2 import segment
    ws, registry, people, client = fixture(tmp_path)
    registry.save_scheme('s1', 'p1', {
        'segments': [segment('2026-06-01', people[0]['id'])]},
        'test', '登记运营', publish=True)
    run = ws.record('s1', '2026-06', {'commission': {
        'engine': 'commission-v2', 'total': None, 'people': [],
        'pricing_threshold_met': False, 'pricing_pending_count': 3,
        'amount_complete': False}, 'statement': []}, [])
    context = client.get('/api/commission-v2/payout-confirmations/context', params={
        'store_id': 's1', 'period': '2026-06', 'run_id': run}).json()
    assert len(context['people']) == 1 and context['people'][0]['suggested'] is None
    confirmed = client.post('/api/commission-v2/payout-confirmations', json={
        'store_id': 's1', 'period': '2026-06', 'run_id': run,
        'source_sha': context['source_sha'], 'reason': '成本仍待补，员工提成已人工核对',
        'payouts': [{'person_id': people[0]['id'], 'amount': '17.00'}]})
    assert confirmed.status_code == 200, confirmed.text
    report = client.post('/api/commission-v2/reports/query', json={
        'start': '2026-06', 'end': '2026-06', 'store_ids': ['s1'],
        'view': 'store_people'}).json()
    assert report['total'] == 17 and report['trial_periods'] == 0
    assert report['missing_periods'] == 0
    assert report['items'][0]['status'] == '已人工确认'
    assert ws.state('s1', '2026-06').closed is False
    assert json.loads(ws.latest_run('s1', '2026-06')['result'])['commission']['total'] is None


def test_corrected_human_payout_keeps_the_previous_decision(tmp_path):
    ws, registry, people, client = fixture(tmp_path)
    run = record(ws, people, 's1', '2026-06', [10], complete=False)
    params = {'store_id': 's1', 'period': '2026-06', 'run_id': run}
    first_context = client.get('/api/commission-v2/payout-confirmations/context',
                               params=params).json()
    body = {**params, 'source_sha': first_context['source_sha'],
            'reason': '人工核对',
            'payouts': [{'person_id': people[0]['id'], 'amount': '11.00'}]}
    first = client.post('/api/commission-v2/payout-confirmations', json=body).json()
    second_context = client.get('/api/commission-v2/payout-confirmations/context',
                                params=params).json()
    assert second_context['latest']['id'] == first['id']
    correction = client.post('/api/commission-v2/payout-confirmations', json={
        **body, 'expected_confirmation_id': first['id'],
        'payouts': [{'person_id': people[0]['id'], 'amount': '12.00'}],
        'reason': '运营复核后更正金额'})
    assert correction.status_code == 200, correction.text
    with registry.connect() as conn:
        decisions = conn.execute('SELECT id,confirmed_total FROM payout_confirmation '
                                 'ORDER BY at,id').fetchall()
        assert [row['confirmed_total'] for row in decisions] == ['11.00', '12.00']
    live = client.post('/api/commission-v2/reports/query', json={
        'start': '2026-06', 'end': '2026-06', 'store_ids': ['s1']}).json()
    assert live['total'] == 12 and live['trial_periods'] == 0
