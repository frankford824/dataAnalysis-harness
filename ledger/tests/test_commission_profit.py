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
    assert mine['products'][0]['sales'] == 120
    assert other['products'][0]['profit'] == 40
    assert other['products'][0]['sales'] == 80
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
