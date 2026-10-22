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


def refund_charge(key,amount,when,**extra):
    return {'charge_id':key,'tracking_no':'SF5197274462604','amount':amount,'scan_time':when,
        'file_sha':'a'*64,'file':'运费.xlsx','sheet':'Sheet1','row_no':943,
        'source_issue':'','carrier':'',**extra}


def test_distinct_return_fees_allocate_330_with_each_source_row_preserved(result,monkeypatch):
    from ledger.freight_trial_inputs import flag_charge_overlaps
    ws,model,jobs,_jid=result;new_id='f'*32;tracking='YT7625530235832'
    charges=[refund_charge(f'{i:032x}',amount,stamp,tracking_no=tracking,scan_time_raw=stamp,
                row_no=row,settlement_object=obj) for i,amount,stamp,row,obj in (
        (1,'1.70','2026-06-05',154983,'皇莉诗旗舰店'),
        (2,'1.00','2026-06-22 11:23:24',233629,'江苏省南京市江宁区江开'),
        (3,'0.60','2026-06-22 11:23:24',235729,'江苏省南京市江宁区江开'))]
    flag_charge_overlaps(charges)
    monkeypatch.setattr(trial,'parse_sources',lambda *_:(charges,[],{},[]))
    monkeypatch.setattr(trial,'build_packages',lambda *_:{tracking:refund_packet(tracking_no=tracking)})
    before=[tuple(r) for r in ws.conn.execute('SELECT * FROM run')]
    trial.build(ws.root,ws.root,model,'2026-06',jobs.path(new_id)/'result')
    trial._write_json(jobs.path(new_id)/'job.json',{'state':'ready','job_id':new_id,'period':'2026-06'})
    page=trial.query(jobs,new_id)
    assert page['summary']['allocated_count']==3 and page['summary']['pending_count']==0
    assert Decimal(page['summary']['source_amount'])==Decimal(page['summary']['allocated_amount'])==Decimal('3.30')
    rows=list(csv.DictReader(io.StringIO(trial.export(jobs,new_id,scope_fingerprint=page['scope_fingerprint']).lstrip('\ufeff'))))
    assert len(rows)==3 and sum(Decimal(r['商品分摊运费']) for r in rows)==Decimal('3.30')
    assert sorted(r['账单结算对象'] for r in rows)==sorted(r['settlement_object'] for r in charges)
    for charge in charges:
        detail=trial.detail(jobs,new_id,charge['charge_id'])['charge']
        assert detail['status']=='allocated' and detail['row_no']==charge['row_no']
        assert len(detail['allocations'])==1
    assert [tuple(r) for r in ws.conn.execute('SELECT * FROM run')]==before


def refund_packet(members=None,**extra):
    return {'packet_id':'sf-parcel','tracking_no':'SF5197274462604','store_ids':['s1'],
        'carrier':'顺丰','ship_time':'2026-06-15 09:32:24','internal_order_ids':['15432477'],
        'members':members or [member('1','68.14')],'issue':'',**extra}


def test_sf_credit_is_counted_with_original_source_evidence_and_net_1238(result,monkeypatch):
    ws,model,jobs,_jid=result;new_id='c'*32
    positive=refund_charge('b'*32,'15.48','2026-06-15 19:44:34')
    credit=refund_charge('a'*32,'-3.10','2026-06-18 13:58:09',row_no=1174)
    # Credit intentionally sorts first. File/hash order cannot decide linkage.
    monkeypatch.setattr(trial,'parse_sources',lambda *_:([credit,positive],[],{},[]))
    monkeypatch.setattr(trial,'build_packages',lambda *_:{positive['tracking_no']:refund_packet()})
    before=[tuple(r) for r in ws.conn.execute('SELECT * FROM run')]
    trial.build(ws.root,ws.root,model,'2026-06',jobs.path(new_id)/'result')
    trial._write_json(jobs.path(new_id)/'job.json',{'state':'ready','job_id':new_id,'period':'2026-06'})
    page=trial.query(jobs,new_id)
    assert Decimal(page['summary']['source_amount'])==Decimal('12.38')
    assert Decimal(page['summary']['allocated_amount'])==Decimal('12.38')
    assert Decimal(page['summary']['allocated_refund'])==Decimal('3.10')
    assert page['summary']['pending_count']==0 and page['summary']['allocated_count']==2
    detail=trial.detail(jobs,new_id,credit['charge_id'])['charge']
    assert detail['method']=='original_reversal' and detail['allocations'][0]['amount']=='-3.100000'
    assert detail['refund_basis']['original_charges'][0]['charge_id']==positive['charge_id']
    assert detail['refund_basis']['original_charges'][0]['row_no']==943
    rows=list(csv.DictReader(io.StringIO(trial.export(jobs,new_id,scope_fingerprint=page['scope_fingerprint']).lstrip('\ufeff'))))
    assert sum(Decimal(r['商品分摊运费']) for r in rows)==Decimal('12.38')
    assert next(r for r in rows if Decimal(r['来源运费（每笔仅首行）'])<0)['冲减依据收费记录']==positive['charge_id']
    assert [tuple(r) for r in ws.conn.execute('SELECT * FROM run')]==before


@pytest.mark.parametrize('original,packet',[
    (None,refund_packet()),
    (refund_charge('p','15.48','2026-06-19'),refund_packet()),
    (refund_charge('p','15.48','invalid'),refund_packet()),
    (refund_charge('p','15.48','2026-06-15',source_issue='duplicate_charge'),refund_packet()),
    (refund_charge('p','15.48','2026-06-15'),refund_packet(issue='cross_store')),
    (refund_charge('p','15.48','2026-06-15'),refund_packet([member('1','0'),member('2','0')])),
])
def test_unproven_original_is_not_used_for_credit(original,packet):
    credit=refund_charge('n','-3.10','2026-06-18')
    assert trial.refund_bases([credit,*([original] if original else [])],{credit['tracking_no']:packet})=={}


def test_multiple_prior_charges_keep_all_consistent_references_and_exclude_future_charge():
    rows=[refund_charge('p2','6','2026-06-16'),refund_charge('n','-3','2026-06-18'),
          refund_charge('p1','12','2026-06-15'),refund_charge('future','8','2026-06-20')]
    packet=refund_packet([member('1','40'),member('2','20')])
    bases=trial.refund_bases(rows,{rows[0]['tracking_no']:packet})
    assert [r['charge_id'] for r in bases['n']['original_charges']]==['p1','p2']
    credit=trial.allocate('-3',packet['members'],original_factors=bases['n']['factors'])
    assert [Decimal(a['amount']) for a in credit['allocations']]==[Decimal(-2),Decimal(-1)]


def test_legacy_artifact_without_credit_evidence_remains_readable(result):
    _ws,_model,jobs,jid=result
    path=jobs.path(jid)/'result';manifest=json.loads((path/'manifest.json').read_text())
    for record in manifest['artifact_files']:
        file=path/record['path']
        if record['path'].startswith('charges/'):
            pl.read_parquet(file).drop('refund_basis_json').write_parquet(file)
            record['sha256']=trial._hash_file(file)
    manifest['rule_version']='shipment-erp-goods-amount.v2'
    trial._write_json(path/'manifest.json',manifest)
    page=trial.query(jobs,jid)
    assert page['summary']['allocated_refund']=='0.000000000000'
    assert trial.detail(jobs,jid,f'{1:032x}')['charge']['refund_basis'] is None
    rows=list(csv.DictReader(io.StringIO(trial.export(jobs,jid,scope_fingerprint=page['scope_fingerprint']).lstrip('\ufeff'))))
    assert all(r['冲减依据收费记录']=='' for r in rows)


def test_credit_only_month_uses_proven_erp_parcel_without_fabricating_original(result,monkeypatch):
    ws,model,jobs,_jid=result;new_id='d'*32
    credit=refund_charge('a'*32,'-.50','2026-06-18')
    m=member('1','8.33');m['basis_source']='erp_goods_amount'
    monkeypatch.setattr(trial,'parse_sources',lambda *_:([credit],[],{},[]))
    monkeypatch.setattr(trial,'build_packages',lambda *_:{credit['tracking_no']:refund_packet([m])})
    trial.build(ws.root,ws.root,model,'2026-06',jobs.path(new_id)/'result')
    trial._write_json(jobs.path(new_id)/'job.json',{'state':'ready','job_id':new_id,'period':'2026-06'})
    page=trial.query(jobs,new_id)
    assert Decimal(page['summary']['allocated_amount'])==Decimal('-.50')
    assert page['summary']['pending_count']==0
    detail=trial.detail(jobs,new_id,credit['charge_id'])['charge']
    assert detail['method']=='direct_credit' and detail['refund_basis'] is None
    assert detail['allocations'][0]['amount']=='-0.500000'


def test_store_exclusion_keeps_unknown_and_mixed_candidates_and_binds_export(result):
    _ws,_model,jobs,jid=result
    page=trial.query(jobs,jid,exclude_store_ids='s1')
    assert {r['tracking_no'] for r in page['rows']}=={'W3','W4'}
    assert page['summary']['charge_count']==2 and Decimal(page['summary']['source_amount'])==Decimal('14')
    assert page['excluded_summary']['charge_count']==3
    assert Decimal(page['summary']['source_amount'])+Decimal(page['excluded_summary']['source_amount'])==Decimal('31.877')
    assert sum(g['count'] for g in page['groups'])==2
    assert {s['id'] for s in page['stores']}=={'s1','s2'}
    rows=list(csv.DictReader(io.StringIO(trial.export(jobs,jid,exclude_store_ids='s1',scope_fingerprint=page['scope_fingerprint']).lstrip('\ufeff'))))
    assert {r['运单号'] for r in rows}=={'W3','W4'}
    with pytest.raises(InputError,match='范围'):
        trial.export(jobs,jid,scope_fingerprint=page['scope_fingerprint'])
    with pytest.raises(InputError,match='没有找到'):
        trial.detail(jobs,jid,f'{1:032x}',exclude_store_ids='s1')
    assert trial.detail(jobs,jid,f'{4:032x}',exclude_store_ids='s1')['charge']['reason']=='cross_store'
    all_excluded=trial.query(jobs,jid,exclude_store_ids='s2,s1,s1')
    assert all_excluded['count']==1 and all_excluded['rows'][0]['tracking_no']=='W3'
    assert all_excluded['scope']['exclude_store_ids']=='s1,s2'
    assert trial.query(jobs,jid,exclude_store_ids='s1,s2')['scope_fingerprint']==all_excluded['scope_fingerprint']
    selected=trial.query(jobs,jid,store_id='s1',exclude_store_ids='s1')
    assert selected['count']==1 and selected['rows'][0]['tracking_no']=='W4'
    original=trial.detail(jobs,jid,f'{1:032x}')['charge']['allocations']
    assert trial.detail(jobs,jid,f'{1:032x}',exclude_store_ids='s2')['charge']['allocations']==original
    with pytest.raises(InputError,match='未登记'):
        trial.query(jobs,jid,exclude_store_ids='not-a-store')


def test_store_exclusion_api_passes_same_scope_to_page_details_and_export(result,monkeypatch):
    ws,model,jobs,jid=result
    monkeypatch.setattr(trial,'manager',lambda *_:jobs)
    app=FastAPI();install(app,lambda:ws,lambda:model,lambda:{})
    client=TestClient(app);base=f'/api/freight-review/shipments/{jid}'
    page=client.get(base,params={'exclude_store_ids':'s1'}).json()
    assert page['count']==2
    assert client.get(base+f'/charges/{1:032x}',params={'exclude_store_ids':'s1'}).status_code==400
    assert client.get(base+'/export.csv',params={'exclude_store_ids':'s1','scope_fingerprint':page['scope_fingerprint']}).status_code==200
    assert client.get(base+'/export.csv',params={'scope_fingerprint':page['scope_fingerprint']}).status_code==400
