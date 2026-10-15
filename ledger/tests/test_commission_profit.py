from __future__ import annotations

import hashlib
import uuid

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledger.commission_engine import persist
from ledger.commission_profit import compose, display_person_id, save
from ledger.commission_registry import Registry, RegistryError, RevisionConflict, json_text
from ledger.workspace import Workspace
from test_commission import _model


def _persist(registry, person_id, person='甲', rows=None, run_id=11, store_id='s1', period='2026-06'):
    frame = pl.DataFrame(rows or {
        'status': ['distribute', 'distribute', 'distribute', 'exclude'],
        'duty': ['produce', 'produce', 'produce', 'cut'],
        'person_id': [person_id, person_id, person_id, person_id],
        'person': [person, person, person, person],
        'product_id': ['111', '111', '222', '333'],
        'product_name': ['正品A', '正品A', '样品B', '不提成'],
        'share': [0.05, 0.05, 0.05, 0.0],
        'total_rate': [0.05, 0.05, 0.05, 0.0],
        'original_base': [100.0, 50.0, 80.0, 20.0],
        'amount': [5.0, 2.5, 4.0, None],
        'participation_sales': [200.0, 100.0, 160.0, 40.0],
        'participation_gross': [120.0, 60.0, 90.0, 10.0],
        'participation_profit': [100.0, 50.0, 80.0, 20.0],
        'spine_row': [1, 2, 3, 4],
        'order_id': ['o1', 'o2', 'o3', 'o4'],
    })
    meta = {
        'id': str(uuid.uuid4()), 'store_id': store_id, 'period': period,
        'registry_revision': 1, 'model_json': '{}', 'rules_json': '{}',
        'summary_json': json_text({'total': 11.5}),
    }
    persist(registry, run_id, frame, meta)
    return meta['id']


def test_compose_groups_allocated_profit_and_skips_excluded_orders(tmp_path):
    registry = Registry(tmp_path)
    person = registry.person_save({'name': '甲'}, 'tester', '登记')
    _persist(registry, person['id'])
    result = compose(registry, 's1', '2026-06', person['id'], 11, store_name='测试店')
    assert result['store'] == '测试店'
    assert result['commission_trial'] == 11.5
    assert result['allocated_profit'] == 230
    assert [row['product_id'] for row in result['products']] == ['111', '222']
    first, second = result['products']
    assert first['profit'] == 150 and first['orders'] == 2 and first['sales'] == 300
    assert second['profit'] == 80 and len(second['lines']) == 1
    assert result['included_profit'] == 230
    assert result['excluded_product_ids'] == []
    summary = compose(registry, 's1', '2026-06', person['id'], 11, include_orders=False)
    assert summary['included_profit'] == result['included_profit']
    assert all(row['lines'] == [] for row in summary['products'])
    assert [{k:v for k,v in row.items() if k!='lines'} for row in summary['products']] == [{k:v for k,v in row.items() if k!='lines'} for row in result['products']]
    detail = compose(registry, 's1', '2026-06', person['id'], 11, product_id='111')
    assert detail['products'] == [first]


def test_shared_link_only_keeps_this_persons_share(tmp_path):
    registry = Registry(tmp_path)
    a = registry.person_save({'name': '甲'}, 'tester', '登记')
    b = registry.person_save({'name': '乙'}, 'tester', '登记')
    _persist(registry, a['id'], rows={
        'status': ['distribute', 'distribute'],
        'person_id': [a['id'], b['id']],
        'person': ['甲', '乙'],
        'product_id': ['111', '111'],
        'product_name': ['共享', '共享'],
        'share': [0.03, 0.02],
        'total_rate': [0.05, 0.05],
        'original_base': [100.0, 100.0],
        'amount': [3.0, 2.0],
        'participation_sales': [200.0, 200.0],
        'participation_gross': [120.0, 120.0],
        'participation_profit': [100.0, 100.0],
        'spine_row': [1, 1],
        'order_id': ['o1', 'o1'],
    })
    mine = compose(registry, 's1', '2026-06', a['id'], 11)
    other = compose(registry, 's1', '2026-06', b['id'], 11)
    assert mine['products'][0]['profit'] == 60
    assert mine['products'][0]['product_sales'] == 200
    assert mine['products'][0]['sales'] is None
    assert mine['sales_pending_products'] == ['111']
    assert other['products'][0]['profit'] == 40
    assert other['products'][0]['sales'] is None
    assert other['sales_pending_products'] == ['111']
    assert mine['products'][0]['rate'] == 0.03
    assert other['products'][0]['rate'] == 0.02
    assert mine['included_profit'] == 60


def test_product_duties_control_output_but_not_commission_entitlement(tmp_path):
    registry=Registry(tmp_path)
    a=registry.person_save({'name':'王岩'},'test','register')
    b=registry.person_save({'name':'刘露'},'test','register')
    _persist(registry,a['id'],rows={
        'status':['distribute']*4,'person_id':[a['id'],b['id'],a['id'],b['id']],
        'person':['王岩','刘露','王岩','刘露'],'product_id':['111','111','222','222'],
        'product_name':['A','A','B','B'],'duty':['produce','cut','cut','produce'],
        'share':[.03,.02,.02,.03],'total_rate':[.05]*4,
        'original_base':[73.27,73.27,100.,100.],'amount':[2.20,1.47,2.,3.],
        'participation_sales':[175.87,175.87,200.,200.],
        'participation_gross':[105.04,105.04,120.,120.],
        'participation_profit':[73.27,73.27,100.,100.],
        'spine_row':[1,1,2,2],'order_id':['o1','o1','o2','o2']})
    mine=compose(registry,'s1','2026-06',a['id'],11,duties={a['id']:{'duty':'cut'}})
    other=compose(registry,'s1','2026-06',b['id'],11)
    by_id={p['product_id']:p for p in mine['products']}
    assert by_id['111']['sales']==175.87
    assert by_id['111']['gross']==105.04
    assert by_id['111']['profit']==73.27
    assert by_id['222']['sales']==0
    assert mine['commission_trial']==4.20
    assert other['commission_trial']==4.47
    assert mine['included_profit']==73.27
    assert other['included_profit']==100


def test_multiple_producers_exclude_cut_from_denominator():
    from ledger.commission_engine import allocated_outputs
    frame=pl.DataFrame({'status':['distribute']*3,'spine_row':[1]*3,'product_id':['p']*3,
        'person_id':['a','b','leader'],'duty':['produce','produce','cut'],
        'share':[.03,.01,.02],'total_rate':[.06]*3,
        'participation_sales':[200.]*3,'participation_gross':[120.]*3,'participation_profit':[-40.]*3})
    result=allocated_outputs(frame,production=True)
    assert result['a']=={'sales':150.,'gross':90.,'profit':-30.}
    assert result['b']=={'sales':50.,'gross':30.,'profit':-10.}
    assert result['leader']=={'sales':0.,'gross':0.,'profit':0.}
    only_cut=frame.with_columns(pl.lit('cut').alias('duty'))
    assert all(r['sales']==0 for r in allocated_outputs(only_cut,production=True).values())


def test_verified_output_cache_survives_memory_cache_clear(tmp_path,monkeypatch):
    from ledger.commission_reports import _archived_allocated_outputs,_profit_cache
    registry=Registry(tmp_path)
    person=registry.person_save({'name':'A'},'test','register')
    calc=_persist(registry,person['id'])
    commission={'calculation_id':calc,'base_node':'net_profit','on_loss':'deduct'}
    first=_archived_allocated_outputs(registry,commission,production=True)
    _profit_cache.clear()
    def fail(*a,**k):raise AssertionError('Verified unchanged evidence should reuse durable totals')
    monkeypatch.setattr(pl,'read_parquet',fail)
    assert _archived_allocated_outputs(registry,commission,production=True)==first
    _profit_cache.clear()
    with registry.connect() as conn:row=conn.execute('SELECT path FROM calculation WHERE id=?',(calc,)).fetchone()
    path=registry.root/'calculations'/row['path']
    path.write_bytes(path.read_bytes()+b'corrupt')
    assert _archived_allocated_outputs(registry,commission,production=True) is None


def test_compose_keeps_one_and_a_half_percent_share(tmp_path):
    registry = Registry(tmp_path)
    person = registry.person_save({'name': '王岩'}, 'tester', '登记')
    other = registry.person_save({'name': '刘露'}, 'tester', '登记')
    _persist(registry, person['id'], person='王岩', rows={
        'status': ['distribute', 'distribute', 'distribute'],
        'person_id': [person['id'], person['id'], other['id']],
        'person': ['王岩', '王岩', '刘露'],
        'product_id': ['111', '222', '111'],
        'product_name': ['对半', '对半后改', '对半'],
        'share': [0.015, 0.015, 0.015],
        'total_rate': [0.03, 0.03, 0.03],
        'original_base': [100.0, 80.0, 100.0],
        'amount': [1.5, 1.2, 1.5],
        'participation_sales': [200.0, 160.0, 200.0],
        'participation_gross': [120.0, 90.0, 120.0],
        'participation_profit': [100.0, 80.0, 100.0],
        'spine_row': [1, 2, 1],
        'order_id': ['o1', 'o2', 'o1'],
    })
    mine = compose(registry, 's1', '2026-06', person['id'], 11)
    by_id = {row['product_id']: row for row in mine['products']}
    assert by_id['111']['rate'] == 0.015
    assert by_id['111']['rates'] == [0.015]
    assert by_id['222']['rate'] == 0.015


def test_save_exclusions_is_run_bound_and_rejects_stale_source(tmp_path):
    registry = Registry(tmp_path)
    person = registry.person_save({'name': '甲'}, 'tester', '登记')
    _persist(registry, person['id'])
    current = compose(registry, 's1', '2026-06', person['id'], 11)
    saved = save(registry, store_id='s1', period='2026-06', person_id=person['id'],
                 run_id=11, source_sha=current['source_sha'],
                 excluded_product_ids=['222'], note='样品不进阶梯', actor='tester')
    assert saved['included_profit'] == 150
    assert saved['excluded_profit'] == 80
    assert saved['excluded_product_ids'] == ['222']
    again = compose(registry, 's1', '2026-06', person['id'], 11)
    assert again['saved']['note'] == '样品不进阶梯'
    assert again['included_profit'] == 150
    with pytest.raises(RevisionConflict):
        save(registry, store_id='s1', period='2026-06', person_id=person['id'],
             run_id=11, source_sha='0' * 64, excluded_product_ids=['222'],
             note='过期', actor='tester')
    with pytest.raises(RegistryError):
        save(registry, store_id='s1', period='2026-06', person_id=person['id'],
             run_id=11, source_sha=current['source_sha'],
             excluded_product_ids=['999'], note='不存在', actor='tester')
    with pytest.raises(RegistryError, match='没有逐单'):
        compose(registry, 's1', '2026-06', person['id'], 12)


def test_display_person_id_matches_report_remap(tmp_path):
    registry = Registry(tmp_path)
    engine_pid = 'legacy:old'
    _persist(registry, engine_pid)
    display = display_person_id('s1', engine_pid, '甲', set())
    assert display == 'legacy:' + hashlib.sha256('s1\0甲'.encode()).hexdigest()
    result = compose(registry, 's1', '2026-06', display, 11)
    assert result['included_profit'] == 230
    assert result['person'] == '甲'


def test_profit_composition_api_round_trip(tmp_path):
    ws = Workspace(tmp_path)
    registry = Registry(tmp_path)
    person = registry.person_save({'name': '甲'}, 'tester', '登记')
    _persist(registry, person['id'])
    app = FastAPI()
    from ledger.commission_api import install
    install(app, lambda: ws, _model)
    client = TestClient(app)
    query = f"/api/commission-v2/profit-composition?store_id=s1&period=2026-06&person_id={person['id']}&run_id=11"
    found = client.get(query).json()
    assert found['included_profit'] == 230
    saved = client.post('/api/commission-v2/profit-exclusions', json={
        'store_id': 's1', 'period': '2026-06', 'person_id': person['id'],
        'run_id': 11, 'source_sha': found['source_sha'],
        'excluded_product_ids': ['222'], 'note': '样品不进阶梯',
    }).json()
    assert saved['included_profit'] == 150
    assert client.get(query).json()['excluded_product_ids'] == ['222']
    stale = client.post('/api/commission-v2/profit-exclusions', json={
        'store_id': 's1', 'period': '2026-06', 'person_id': person['id'],
        'run_id': 11, 'source_sha': '0' * 64,
        'excluded_product_ids': ['222'], 'note': '过期',
    })
    assert stale.status_code == 409


def _cost_fixture(tmp_path, *, captured=()):
    from ledger.storage_integrity import seal
    ws = Workspace(tmp_path)
    run = ws.record('s1', '2026-06', {'can_close':True,
        'cost_line_posting': {'reviews':list(captured)}}, [])
    registry = Registry(tmp_path)
    person = registry.person_save({'name':'甲'}, 'tester', '登记')
    _persist(registry, person['id'], run_id=run, rows={
        'status':['distribute']*3, 'duty':['produce']*3,
        'person_id':[person['id']]*3, 'person':['甲']*3,
        'product_id':['known','missing','free'], 'product_name':['有成本','缺成本','真实零成本'],
        'share':[.05]*3, 'total_rate':[.05]*3, 'original_base':[45.,38.27,9.],
        'amount':[2.25,1.91,.45], 'participation_sales':[50.,41.65,10.],
        'participation_gross':[49.03,41.65,10.], 'participation_profit':[45.,38.27,9.],
        'spine_row':[1,2,3], 'order_id':['M1','M1','M2'], 'sub_order_id':['S1','S2','S3'],
    })
    path=ws.coverage_gaps_path(run)
    pl.DataFrame([{'coverage_key':'S2','context_sha':'ctx2','order_id':'M1',
        'sub_order_id':'S2','product_ids':'missing','order_count':1,'product_count':1}]).write_parquet(path)
    seal(path)
    return ws, registry, person['id'], run


def test_cost_gap_is_not_zero_and_parent_sibling_cost_is_not_borrowed(tmp_path):
    ws, registry, person, run = _cost_fixture(tmp_path)
    result=compose(registry,'s1','2026-06',person,run,workspace=ws)
    products={p['product_id']:p for p in result['products']}
    assert result['cost_evidence_available'] and result['cost_pending_products']==['missing']
    assert products['known']['creator_cost']==.97 and not products['known']['cost_pending']
    assert products['free']['creator_cost']==0 and not products['free']['cost_pending']
    missing=products['missing']
    assert missing['creator_cost'] is None and missing['cost'] is None
    assert missing['booked_creator_cost']==0 and missing['cost_pending_sub_order_ids']==['S2']
    assert missing['creator_profit']==38.27 and missing['profit']==38.27
    assert missing['lines'][0]['sub_order_id']=='S2' and missing['lines'][0]['cost_pending']
    assert not products['known']['lines'][0]['cost_pending']
    # Financial snapshot, calculation file and payout calculation remain unchanged.
    assert result['commission_trial']==4.61 and result['included_profit']==92.27
    with registry.connect() as c:
        assert c.execute('SELECT count(*) FROM payout_confirmation').fetchone()[0]==0


def test_cost_status_uses_captured_context_not_live_cost_edits(tmp_path, monkeypatch):
    from ledger import cost_lines
    from ledger.commission_profit import _saved_cost_gaps
    ws, registry, person, run = _cost_fixture(tmp_path, captured=[{
        'coverage_key':'S2','context_sha':'other-context','amount':0}])
    def refuse_live(*args, **kwargs):
        raise AssertionError('historical composition must not read latest cost decisions')
    monkeypatch.setattr(cost_lines,'_latest',refuse_live)
    assert len(_saved_cost_gaps(ws,'s1','2026-06',run))==1
    decision={'source_run_id':run,'line_reviews':[
        {'coverage_key':'S2','context_sha':'ctx2','amount':0}]}
    ws.conn.execute('INSERT INTO manual_finance VALUES (?,?,?,?,?,?,?,?)',
        (run,'s1','2026-06',json_text({'manual_cost':{'source_run_id':run}}),
         json_text(decision),'sha','now','test'))
    ws.conn.commit()
    result=compose(registry,'s1','2026-06',person,run,workspace=ws)
    # The original calculation captured a different order context. Even a
    # later frozen zero must not silently certify that archived product row.
    assert result['cost_pending_products']==['missing']
    assert next(p for p in result['products'] if p['product_id']=='missing')['cost_status']=='成本口径待核对'
    assert _saved_cost_gaps(ws,'another-store','2026-06',run) is None


def test_absent_cost_coverage_archive_is_not_claimed_as_verified(tmp_path):
    ws, registry, person, run = _cost_fixture(tmp_path)
    ws.coverage_gaps_path(run).unlink()
    result=compose(registry,'s1','2026-06',person,run,workspace=ws)
    assert result['cost_evidence_available'] is False
    assert all(p['cost_status']=='成本覆盖留档不可用' for p in result['products'])


def test_explicit_frozen_zero_without_conflicting_capture_stays_zero(tmp_path):
    ws, registry, person, run = _cost_fixture(tmp_path)
    decision={'source_run_id':run,'line_reviews':[
        {'coverage_key':'S2','context_sha':'ctx2','amount':0}]}
    ws.conn.execute('INSERT INTO manual_finance VALUES (?,?,?,?,?,?,?,?)',
        (run,'s1','2026-06',json_text({'manual_cost':{'source_run_id':run}}),
         json_text(decision),'sha','now','test'))
    ws.conn.commit()
    result=compose(registry,'s1','2026-06',person,run,workspace=ws)
    assert result['cost_pending_products']==[]
    assert next(p for p in result['products'] if p['product_id']=='missing')['creator_cost']==0


@pytest.mark.parametrize('booked', [[], [{'coverage_key':'S2','context_sha':'ctx2','amount':1.0}]])
def test_frozen_supplement_does_not_certify_older_commission_cost(tmp_path, booked):
    ws, registry, person, run = _cost_fixture(tmp_path, captured=booked)
    decision={'source_run_id':run,'line_reviews':[
        {'coverage_key':'S2','context_sha':'ctx2','amount':2.0}]}
    ws.conn.execute('INSERT INTO manual_finance VALUES (?,?,?,?,?,?,?,?)',
        (run,'s1','2026-06',json_text({'manual_cost':{'source_run_id':run}}),
         json_text(decision),'sha','now','test'))
    ws.conn.commit()
    result=compose(registry,'s1','2026-06',person,run,workspace=ws)
    row=next(p for p in result['products'] if p['product_id']=='missing')
    assert row['cost_status']=='成本口径待核对' and row['creator_cost'] is None
    assert row['creator_profit']==38.27 and result['commission_trial']==4.61


def test_cost_status_api_keeps_booked_amounts_and_previous_profit_contract(tmp_path):
    ws, registry, person, run = _cost_fixture(tmp_path)
    app=FastAPI()
    from ledger.commission_api import install
    install(app,lambda:ws,_model)
    found=TestClient(app).get('/api/commission-v2/profit-composition',params={
        'store_id':'s1','period':'2026-06','person_id':person,'run_id':run}).json()
    row=next(p for p in found['products'] if p['product_id']=='missing')
    assert row['cost_pending'] and row['creator_cost'] is None
    assert row['booked_creator_cost']==0 and row['creator_profit']==38.27
    assert found['included_profit']==92.27
