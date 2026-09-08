import csv
import io
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ledger.commission_api import install
from ledger.commission_batch import preview
from ledger.commission_catalog import settings
from ledger.workspace import Workspace
from test_commission_batch import setup
from test_commission_reports import fixture, record


def test_multiselect_settings_export_and_bulk_use_the_same_scope(tmp_path):
    registry, model, a, b = setup(tmp_path)
    c = registry.person_save({'name':'丙'},'test','登记')
    registry.save_setting({'store_id':'s2','product_id':'123456789002','valid_from':'2000-01-01',
                          'allocations':[{'person_id':c['id'],'rate':'.04'}]},'test')
    ws=Workspace(tmp_path);app=FastAPI();install(app,lambda:ws,lambda:model);client=TestClient(app)
    params=[('store_ids','s2'),('person_ids',a['id']),('person_ids',b['id'])]
    rows=client.get('/api/commission-v2/settings',params=params).json()['rows']
    assert [(r['store_id'],r['product_id']) for r in rows]==[('s2','123456789001')]
    text=client.get('/api/commission-v2/export/settings',params=params).text
    exported=list(csv.DictReader(io.StringIO(text.lstrip('\ufeff'))))
    assert len(exported)==2 and {r['店铺'] for r in exported}=={'乙店'}
    assert {r['宝贝ID'] for r in exported}=={"'123456789001"}
    plan=preview(registry,model,{'scope':{'store_ids':['s2'],'person_ids':[a['id'],b['id']]},
        'template':{'valid_from':'2001-01-01','mode':'exclude','allocations':[]}},'test')
    assert plan['count']==1 and plan['rows'][0]['store_id']=='s2'


def test_filtered_cursor_does_not_skip_sparse_matches_or_duplicate_stores(tmp_path):
    registry,model,a,b=setup(tmp_path)
    with registry.transaction() as conn:
        conn.executemany('INSERT INTO catalog VALUES(?,?,?,?,?,?)',[
            ('s1',str(100000000000+i),'未设置商品','','{}','2000-01-01') for i in range(70)])
    page=settings(registry,store_ids=['s1','s2'],person_ids=[a['id']],limit=1)
    assert len(page['rows'])==1 and page['rows'][0]['store_id']=='s1'
    second=settings(registry,store_ids=['s1','s2'],person_ids=[a['id']],after=page['next_after'],limit=1)
    assert len(second['rows'])==1 and second['rows'][0]['store_id']=='s2' and not second['has_more']
    pending=settings(registry,state='pending',limit=60)
    assert len(pending['rows'])==60 and pending['has_more']


def test_search_uses_edited_product_name_and_treats_wildcards_as_text(tmp_path):
    registry,model,a,b=setup(tmp_path)
    registry.save_setting({'store_id':'s1','product_id':'123456789001','expected_revision':1,
        'product_name':'50%礼盒','valid_from':'2001-01-01','allocations':[{'person_id':a['id'],'rate':'.03'}]},'test')
    with registry.transaction() as conn:
        conn.execute('INSERT INTO catalog VALUES(?,?,?,?,?,?)',('s1','123456789001','旧名字','','{}','2000-01-01'))
    assert settings(registry,search='50%',limit=1)['rows'][0]['product_name']=='50%礼盒'
    assert not settings(registry,search='旧名字')['rows']
    assert len(settings(registry,search='%')['rows'])==1


@pytest.mark.parametrize('view,key',[('people','people'),('stores','stores'),('breakdown','rows'),('coverage','coverage')])
def test_report_pages_keep_whole_scope_total_and_export(view,key,tmp_path):
    ws,registry,people,client=fixture(tmp_path)
    record(ws,people,'s1','2026-06',[10,20]);record(ws,people,'s2','2026-06',[-1,2])
    scope={'start':'2026-06','end':'2026-06'}
    full=client.post('/api/commission-v2/reports/query',json=scope).json()
    paged=client.post('/api/commission-v2/reports/query',json={**scope,'view':view,'offset':1,'limit':1}).json()
    assert paged['items']==full[key][1:2] and paged['count']==len(full[key])
    assert paged['total']==31 and paged['fingerprint']==full['fingerprint']
    assert all(k not in paged for k in ['rows','people','stores','coverage'])
    export=client.post('/api/commission-v2/export/reports/'+view,json={**scope,'run_ids':paged['run_ids'],'fingerprint':paged['fingerprint']})
    assert export.status_code==200
    assert len(list(csv.DictReader(io.StringIO(export.text.lstrip('\ufeff')))))==len(full[key])
    business=client.post('/api/commission-v2/export/reports/'+view,json={**scope,'presentation':True}).text
    assert '人员ID' not in business and '计算记录' not in business and '试算' not in business
    exported=list(csv.DictReader(io.StringIO(business.lstrip('\ufeff'))))
    from decimal import Decimal
    assert sum((Decimal(r['提成金额']) for r in exported if r['提成金额']),Decimal(0))==31


def test_detail_drawer_stays_on_the_parent_calculation_after_new_run(tmp_path):
    ws,registry,people,client=fixture(tmp_path)
    closed=record(ws,people,'s1','2026-06',[12.34])
    ws.close_period('s1','2026-06',by='test',note='原结账')
    record(ws,people,'s1','2026-06',[999])
    opened=record(ws,people,'s2','2026-06',[7.89])
    scope={'start':'2026-06','end':'2026-06','view':'stores'}
    parent=client.post('/api/commission-v2/reports/query',json=scope).json()
    assert {s['run_id'] for s in parent['run_scopes']}=={closed,opened}
    record(ws,people,'s2','2026-06',[888])
    chosen=[s['run_id'] for s in parent['run_scopes'] if s['store_id']=='s2']
    detail=client.post('/api/commission-v2/reports/query',json={**scope,'view':'breakdown','store_ids':['s2'],'run_ids':chosen}).json()
    assert detail['total']==7.89
    assert detail['total']==next(row['amount'] for row in parent['items'] if row['store_id']=='s2')
    assert ws.state('s1','2026-06').run_id==closed
