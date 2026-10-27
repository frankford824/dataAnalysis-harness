import csv
import io
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledger.operations_api import install
from operations_fixture import create_fixture


def client_for(tmp_path):
    ws,model,registry,runs=create_fixture(tmp_path,overhead=10)
    app=FastAPI();install(app,lambda:ws,lambda:model)
    return TestClient(app),ws,model,registry,runs


def test_saved_report_export_and_drill_share_input_identity(tmp_path):
    client,ws,model,registry,runs=client_for(tmp_path)
    result=client.get('/api/operations/month?period=2030-01').json()
    assert result['summary']['net_income']=='320.00'
    assert result['summary']['profit_after_labor']=='182.00'
    assert result['summary']['promotion_cost']=='20.00'
    assert result['summary']['aftersales_cost']=='12.00'
    assert result['is_final'] is False
    assert result['mom']['profit_after_labor']['direction']=='up'
    people=client.get('/api/operations/month?period=2030-01&level=person').json()
    assert [row['id'] for row in people['rows']]==['doer']
    assert people['summary']['net_income']=='320.00'
    assert people['summary']['labor_cost']=='10.00'
    assert len(people['reference_rows'])==2
    assert people['input_sha']==result['input_sha']
    export=client.get('/api/operations/export.csv',params={'period':'2030-01','expected_input_sha':result['input_sha']})
    assert export.status_code==200
    assert export.headers['x-operations-scope']==result['scope_sha']
    exported=list(csv.reader(io.StringIO(export.text.lstrip('\ufeff'))))
    assert len(exported)==5
    assert [row[4] for row in exported[1:]]==['归属业绩','归属业绩','抽点参考（不计业绩）','抽点参考（不计业绩）']
    compared=client.get('/api/operations/export.csv',params={'period':'2030-01','comparison':'mom'})
    comparison_rows=list(csv.reader(io.StringIO(compared.text.lstrip('\ufeff'))))
    assert '销售收入（到账）变化额' in comparison_rows[0]
    assert all(len(row)==len(comparison_rows[0]) for row in comparison_rows)
    evidence=client.get('/api/operations/evidence',params={'period':'2030-01','store_id':'s1','product_id':'100000000001','expected_input_sha':result['input_sha']})
    assert evidence.status_code==200
    assert evidence.json()['total']==10
    assert evidence.json()['items'][0]['file_name']=='经营测试原表.csv'
    assert evidence.json()['input_sha']==result['input_sha']


def test_export_rejects_stale_input_and_invalid_scope(tmp_path):
    client,ws,model,registry,runs=client_for(tmp_path)
    current=client.get('/api/operations/month?period=2030-01').json()
    old=ws.state('s1','2030-01').result
    ws.record('s1','2030-01',old,[],input_fingerprint='new')
    assert client.get('/api/operations/export.csv',params={'period':'2030-01','expected_input_sha':current['input_sha']}).status_code==409
    assert client.get('/api/operations/month?period=2030-13').status_code==400
    assert client.get('/api/operations/month?period=2030-01&level=unsafe').status_code==400


def test_corrupt_detail_does_not_become_complete_zero_detail(tmp_path):
    client,ws,model,registry,runs=client_for(tmp_path)
    assert client.get('/api/operations/month?period=2030-01&level=product').json()['summary']['goods_cost']=='80.00'
    path=ws.facts_path(runs['2030-01','s1']).with_suffix('.operations.parquet')
    path.write_bytes(b'damaged')
    report=client.get('/api/operations/month?period=2030-01&level=product').json()
    row=next(row for row in report['rows'] if row['store_id']=='s1')
    assert row['metrics']['goods_cost'] is None
    assert row['metrics']['profit_after_labor'] is None
    assert report['is_final'] is False
    raw=ws.facts_path(runs['2030-01','s1']);raw.write_bytes(b'damaged')
    assert client.get('/api/operations/evidence?period=2030-01&store_id=s1').status_code==409


def test_pagination_and_store_filter_keep_company_allocation_and_full_totals(tmp_path):
    client,_,_,_,_=client_for(tmp_path)
    first=client.get('/api/operations/month',params={'period':'2030-01','limit':1,'sort_key':'net_income'}).json()
    second=client.get('/api/operations/month',params={'period':'2030-01','limit':1,'offset':1,'sort_key':'net_income'}).json()
    assert first['total']==second['total']==2
    assert first['rows'][0]['store_id']=='s2'
    assert second['rows'][0]['store_id']=='s1'
    assert first['summary']['net_income']==second['summary']['net_income']=='320.00'
    assert first['scope_sha']==second['scope_sha']
    selected=client.get('/api/operations/month?period=2030-01&store_id=s1&level=person').json()
    assert selected['summary']['labor_cost']=='2.50'
    assert selected['summary']['profit_after_labor']=='13.50'


def test_company_summary_does_not_decode_order_projection(tmp_path,monkeypatch):
    client,_,_,_,_=client_for(tmp_path)
    from ledger import operations_readmodel
    def forbidden(*args,**kwargs):raise AssertionError('summary decoded order evidence')
    monkeypatch.setattr(operations_readmodel,'_read_projection',forbidden)
    result=client.get('/api/operations/month?period=2030-01').json()
    assert result['summary']['net_income']=='320.00'
