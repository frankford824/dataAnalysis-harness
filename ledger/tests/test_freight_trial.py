import csv
import io
import json
import threading
from decimal import Decimal

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledger import freight_trial as trial
from ledger.freight_trial_inputs import InputError
from ledger.freight_review_api import install
from ledger.model.schema import Store
from ledger.workspace import Workspace
from test_commission import _model


def member(key,amount,sid='s1'):
    return {'target_key':key,'store_id':sid,'order_id':'3310000000000000000','child_id':'331000000000000000'+key,
        'product_id':'p'+key,'identity_verified':True,'amount_verified':True,'basis_amount':amount,
        'whole_child':True,'basis_unit':'amount','identity_kind':'platform_child'}


@pytest.fixture
def result(tmp_path,monkeypatch):
    ws=Workspace(tmp_path);ws.record('s1','2026-06',{'statement':[]},[])
    model=_model(stores=(Store(id='s1',name='甲',platform='taobao'),Store(id='s2',name='乙',platform='taobao')))
    inputs={'period':'2026-06','files':[],'runs':[],'snapshot_id':'snap','consumed_seq':12,'source_latest_seq':12,
            'mapping':[],'model_hash':'model','captured_at':'2026-10-16T08:00:00+00:00'}
    charges=[{'charge_id':f'{i:032x}','tracking_no':f'W{i}','raw_tracking_no':f'W{i}','amount':amount,
        'scan_time':'2026-06-01','file_sha':'a'*64,'file':'运费.xlsx','sheet':'export','row_no':i+1,'source_issue':'','carrier':''}
        for i,amount in enumerate(('12','6','9','5','-.123'),1)]
    packets={f'W{i}':{'packet_id':f'packet{i}','tracking_no':f'W{i}','store_ids':['s1'],
        'carrier':'carrier','ship_time':'2026-06-01','internal_order_ids':['e1'],'members':members,'issue':''}
        for i,members in ((1,[member('1','40'),member('2','20')]),(2,[member('1','40')]),(5,[member('1','40')]))}
    packets['W4']={'packet_id':'packet4','tracking_no':'W4','store_ids':['s1','s2'],'carrier':'carrier',
        'ship_time':'2026-06-01','internal_order_ids':['e1','e2'],'members':[member('1','40'),member('2','20','s2')],'issue':'cross_store'}
    monkeypatch.setattr(trial,'capture',lambda *_:(inputs,[]))
    monkeypatch.setattr(trial,'parse_sources',lambda *_:(charges,[],{},[]))
    monkeypatch.setattr(trial,'platform_bases',lambda *_:({},{},{}))
    monkeypatch.setattr(trial,'feed_frames',lambda *_:(pl.DataFrame(),pl.DataFrame(),pl.DataFrame()))
    monkeypatch.setattr(trial,'build_packages',lambda *_:packets)
    jobs=trial.Jobs(tmp_path);job_id='a'*32;folder=jobs.path(job_id)
    trial.build(tmp_path,tmp_path,model,'2026-06',folder/'result')
    trial._write_json(folder/'job.json',{'job_id':job_id,'period':'2026-06','state':'ready'})
    yield ws,model,jobs,job_id
    jobs.pool.shutdown(wait=True)


def test_charge_conservation_and_store_filter_do_not_recalculate_denominators(result):
    ws,model,jobs,jid=result
    before=[tuple(r) for r in ws.conn.execute('SELECT * FROM run')]
    all_rows=trial.query(jobs,jid)
    assert all_rows['summary']['charge_count']==5
    assert Decimal(all_rows['summary']['source_amount'])==Decimal('31.877')
    assert Decimal(all_rows['summary']['allocated_amount'])==Decimal('18')
    assert all_rows['summary']['pending_count']==3
    selected=trial.query(jobs,jid,store_id='s1')
    assert selected['summary']['charge_count']==4  # Unassigned W3 never becomes every shop's expense.
    detail=trial.detail(jobs,jid,f'{1:032x}',store_id='s1')
    assert [Decimal(r['amount']) for r in detail['charge']['allocations']]==[Decimal(8),Decimal(4)]
    assert trial.query(jobs,jid,store_id='s2')['summary']['allocated_count']==0
    assert [tuple(r) for r in ws.conn.execute('SELECT * FROM run')]==before


def test_csv_preserves_ids_pending_unknowns_and_only_counts_source_charge_once(result):
    _ws,_model,jobs,jid=result
    page=trial.query(jobs,jid)
    csv_text=trial.export(jobs,jid,scope_fingerprint=page['scope_fingerprint'])
    rows=list(csv.DictReader(io.StringIO(csv_text.lstrip('\ufeff'))))
    assert len(rows)==6
    assert sum(Decimal(r['来源运费（每笔仅首行）']) for r in rows if r['来源运费（每笔仅首行）'])==Decimal('31.877')
    assert sum(Decimal(r['商品分摊运费']) for r in rows if r['商品分摊运费'])==Decimal('18')
    pending=next(r for r in rows if r['运单号']=='W3')
    assert pending['商品分摊运费']=='' and pending['待核原因']=='未找到实际发货记录'
    assert rows[0]['主订单号'].startswith("'")
    refund=next(r for r in rows if r['运单号']=='W5')
    assert refund['商品分摊运费']=='' and Decimal(refund['来源运费（每笔仅首行）'])==Decimal('-.123')


def test_export_is_bound_to_exact_visible_group_and_snapshot(result):
    _ws,_model,jobs,jid=result
    page=trial.query(jobs,jid,reason='no_shipment')
    with pytest.raises(InputError,match='范围'):
        trial.export(jobs,jid,reason='allocated',scope_fingerprint=page['scope_fingerprint'])
    assert len(list(csv.DictReader(io.StringIO(trial.export(jobs,jid,reason='no_shipment',scope_fingerprint=page['scope_fingerprint']).lstrip('\ufeff')))))==1


def test_partial_or_modified_artifacts_are_not_published_as_complete_results(result):
    _ws,_model,jobs,jid=result
    first=next((jobs.path(jid)/'result/charges').glob('*.parquet'))
    first.write_bytes(first.read_bytes()+b'changed')
    with pytest.raises(InputError,match='校验'):
        trial.query(jobs,jid)
    other='b'*32;trial._write_json(jobs.path(other)/'job.json',{'state':'running','session':trial.SESSION})
    with pytest.raises(InputError,match='未完成'):jobs.manifest(other)
    with pytest.raises(InputError):jobs.path('../outside')


def test_api_exposes_proposals_but_no_apply_or_close_endpoint(result,monkeypatch):
    ws,model,jobs,jid=result
    monkeypatch.setattr(trial,'manager',lambda *_:jobs)
    app=FastAPI();install(app,lambda:ws,lambda:model,lambda:{})
    client=TestClient(app)
    page=client.get(f'/api/freight-review/shipments/{jid}').json()
    assert client.get(f'/api/freight-review/shipments/{jid}/export.csv',params={'scope_fingerprint':page['scope_fingerprint']}).status_code==200
    assert client.get(f'/api/freight-review/shipments/{jid}/charges/{1:032x}').json()['charge']['status']=='allocated'
    assert client.post(f'/api/freight-review/shipments/{jid}/apply').status_code==404
    assert client.post('/api/freight-review/shipments',json={'period':'2026-13'}).status_code==400


def test_empty_latest_read_does_not_create_state_or_start_a_job(tmp_path):
    jobs=trial.Jobs(tmp_path)
    try:assert jobs.latest('2026-06')=={'job':None,'result':None} and not (tmp_path/'freight-shadow').exists()
    finally:jobs.pool.shutdown(wait=True)


def test_duplicate_start_is_coalesced_and_failure_retains_the_previous_result(result,monkeypatch):
    _ws,model,jobs,jid=result
    trial._write_json(jobs.area/'latest/2026-06.json',{'job_id':jid,'result_id':jid})
    entered,released=threading.Event(),threading.Event()
    def failing_build(*_args):
        entered.set()
        assert released.wait(5)
        raise InputError('测试输入不完整')
    monkeypatch.setattr(trial,'build',failing_build)
    try:
        new=jobs.start('2026-06',model,jobs.root)
        assert entered.wait(5)
        assert jobs.start('2026-06',model,jobs.root)['job_id']==new['job_id']
        assert jobs.latest('2026-06')['result']['job_id']==jid
    finally:
        released.set()
        jobs.pool.shutdown(wait=True)
    state=jobs.latest('2026-06')
    assert state['job']['state']=='failed' and state['result']['job_id']==jid
    assert trial.query(jobs,jid)['summary']['charge_count']==5
