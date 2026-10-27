import csv
import io
import json
import shutil
import threading
from decimal import Decimal

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import MODELS, write_xlsx
from test_order_feed import _fixture, _write
from ledger import freight_experiments as experiments, freight_experiments_inputs as frozen
from ledger import freight_trial
from ledger.freight_trial_inputs import InputError
from ledger.freight_review_api import install
from ledger.model.loader import load_model
from ledger.model.schema import Store
from ledger.order_feed import OrderFeed
from ledger.workspace import Workspace, SHARED_STORE_ID


def catalog():
    return {'version':'classification-v1','source_file':'approved-list.xlsx','source_sha256':'a'*64,
            'categories':[{'id':'festival','name':'节日'}],
            'mapping':{'taobao_test':'festival'},'erp_mapping':{}}


@pytest.fixture
def source(tmp_path):
    root, feed_root = tmp_path/'workspace', tmp_path/'feed'
    manifest = _fixture(feed_root,second_unnamed=True)
    headers = pl.read_parquet(feed_root/manifest['objects']['orders.parquet']['path']).with_columns(
        pl.lit('3310000000000000000').alias('online_order_no'),
        pl.lit(['3310000000000000000']).alias('platform_order_ids'),
        pl.lit(True).alias('platform_order_identity_complete'),
        pl.lit('2026-06-02 11:00:00').alias('ship_time'))
    items = pl.read_parquet(feed_root/manifest['objects']['order_items.parquet']['path']).with_columns(
        pl.lit('10').alias('order_store_id'),
        pl.Series('outer_sku',['3310000000000000001','3310000000000000002']),
        pl.Series('shop_item_id',['123456789001','123456789002']),
        pl.Series('line_amount',['30','10']),
        pl.Series('paid_amount',['20','10']),
        pl.lit('3310000000000000000').alias('online_order_no'))
    for name,frame in [('orders.parquet',headers),('order_items.parquet',items)]:
        manifest['objects'][name] = _write(feed_root,name,frame)
    for name in ('after_sales.parquet','after_sale_items.parquet'):
        frame = pl.read_parquet(feed_root/manifest['objects'][name]['path']).clear()
        manifest['objects'][name] = _write(feed_root,name,frame)
    feed = OrderFeed(root,feed_root=feed_root)
    feed._install_snapshot(manifest,{'stores':[{'order_store_id':'10','ledger_store_id':'taobao_test','mapping_status':'confirmed'}]},
        {'healthy':True},source_revision=10,source_latest_seq=10,revision_etag='version10')
    ws = Workspace(root)
    file = write_xlsx(tmp_path/'运费.xlsx',[
        ['运单号码','账单结算对象名称','总金额','扫描时间'],
        ['SF1','测试店',12,'2026/06/03 12:00:00'],
        ['UNKNOWN','未归属',8,'2026/06/03 13:00:00'],
        ['SF1','测试店',-2,'2026/06/04 12:00:00']])
    ws.keep('运费.xlsx',file,SHARED_STORE_ID)
    # Poison historical run amounts: A must execute current rules on raw files.
    ws.record('taobao_test','2026-06',{'statement':[{'id':'n_freight','value':-999999}]},[])
    model = load_model(MODELS/'cn-ecommerce').model_copy(update={'commission':(), 'stores':(
        Store(id='taobao_test',name='淘宝测试店',platform='taobao'),)})
    yield ws,feed_root,model
    ws.close()


@pytest.fixture
def ready(source,tmp_path):
    ws,feed_root,model = source
    before = [tuple(r) for r in ws.conn.execute('SELECT * FROM run')]
    bundle = tmp_path/'bundle'
    frozen.capture(ws.root,feed_root,model,'2026-06',bundle,catalog())
    target = tmp_path/'result'
    manifest = experiments.run_bundle(bundle,target)
    assert [tuple(r) for r in ws.conn.execute('SELECT * FROM run')]==before
    return bundle,target,manifest


def test_original_engine_and_v6_use_same_raw_pool_unknown_fees_and_separate_signs(ready):
    bundle,target,manifest = ready
    assert manifest['same_input'] and not manifest['old_runs_used_for_calculation']
    a,b = manifest['a_summary'],manifest['b_summary']
    assert a['source_count']==b['source_count']==3
    assert Decimal(a['source_abs'])==Decimal(b['source_abs'])==22
    assert Decimal(a['positive_source_abs'])==Decimal(b['positive_source_abs'])==20
    assert Decimal(a['negative_source_abs'])==Decimal(b['negative_source_abs'])==2
    assert Decimal(b['positive_coverage'])==Decimal('.6')
    assert Decimal(b['negative_coverage'])==1
    assert Decimal(b['coverage'])==Decimal(14)/22
    assert Decimal(b['booked_net'])==10
    assert Decimal(b['unallocated_abs'])==8
    assert Decimal(b['conservation_delta'])==0
    rows = pl.read_parquet(target/'diff.parquet').to_dicts()
    assert any(r['tracking_no']=='UNKNOWN' and r['b_reason']=='no_shipment' for r in rows)
    assert abs(Decimal(a['booked_net']))<999999
    candidate = json.loads((target/'b.json').read_text())
    positive = next(r for r in candidate.values() if Decimal(r['amount'])==12)
    assert sorted(Decimal(a['amount']) for a in positive['allocations'])==[Decimal(3),Decimal(9)]
    # Two source fees share one original link group. Per-fee target amounts
    # are not historical facts; compare the original actual group projection.
    assert all(r['a_amount'] is None and r['delta'] is None for r in rows if r['tracking_no']=='SF1')
    groups=json.loads((target/'comparison-components.json').read_text())
    assert any(Decimal(r['delta']) for r in groups if r['delta'] is not None)
    assert json.loads((bundle/'inputs.json').read_text())['allow_zero_equal'] is True


def test_replay_is_independent_of_live_data_model_and_classification(ready,source,tmp_path,monkeypatch):
    import socket
    import urllib.request
    bundle,target,manifest = ready
    ws,feed_root,_model = source
    shutil.rmtree(feed_root)
    for file in (ws.root/'files').rglob('*'):
        if file.is_file():
            file.write_bytes(b'live file changed')
    monkeypatch.setattr(experiments.store_classification,'resolve',lambda *_:pytest.fail('replay read current classification'))
    monkeypatch.setattr(socket,'create_connection',lambda *_a,**_k:pytest.fail('replay accessed network'))
    monkeypatch.setattr(urllib.request,'urlopen',lambda *_a,**_k:pytest.fail('replay accessed HTTP'))
    second = tmp_path/'replayed'
    replay = experiments.run_bundle(bundle,second)
    assert replay['input_fingerprint']==manifest['input_fingerprint']
    for file in ('a.json','b.json','charges.json','packets.json'):
        assert json.loads((target/file).read_text())==json.loads((second/file).read_text())
    assert pl.read_parquet(target/'diff.parquet').to_dicts()==pl.read_parquet(second/'diff.parquet').to_dicts()


@pytest.mark.parametrize('missing',['orders.parquet','order_costs.parquet','after_sale_items.parquet'])
def test_incomplete_raw_snapshot_fails_closed(source,tmp_path,missing):
    ws,feed_root,model = source
    with frozen.readonly(ws.root/'order-feed.db') as db:
        manifest = json.loads(db.execute('SELECT manifest_json FROM feed_state').fetchone()[0])
    (feed_root/manifest['objects'][missing]['path']).unlink()
    with pytest.raises(InputError,match='缺失'):
        frozen.capture(ws.root,feed_root,model,'2026-06',tmp_path/'bundle',catalog())


def test_hash_tampering_or_unlisted_evidence_is_never_a_successful_replay(ready,tmp_path):
    bundle,_target,_manifest = ready
    (bundle/'unlisted.json').write_text('{}')
    with pytest.raises(InputError,match='清单'):
        experiments.run_bundle(bundle,tmp_path/'unlisted-result')
    (bundle/'unlisted.json').unlink()
    (bundle/'model.json').write_text('{}')
    with pytest.raises(InputError,match='校验'):
        experiments.run_bundle(bundle,tmp_path/'bad-result')


def test_signed_coverage_cannot_be_inflated_by_cancellation_or_unknown_shops():
    charges = [{'charge_id':'p','amount':'100'},{'charge_id':'n','amount':'-99'}, {'charge_id':'unknown','amount':'40'}]
    results = {'p':{'status':'allocated','allocations':[{'amount':'100'}]},
        'n':{'status':'pending','allocations':[]},'unknown':{'status':'pending','allocations':[]}}
    summary = experiments.summarize(charges,results)
    assert Decimal(summary['source_abs'])==239
    assert Decimal(summary['coverage'])==Decimal(100)/239
    assert Decimal(summary['positive_coverage'])==Decimal(100)/140
    assert Decimal(summary['negative_coverage'])==0
    results['p']['allocations'][0]['amount']='200'
    summary = experiments.summarize(charges,results)
    assert summary['overbooked_count']==1 and Decimal(summary['overbooked_abs'])==100
    assert Decimal(summary['coverage'])<=1


def test_capture_version_is_stable_and_preserves_zero_policy_and_complete_delta(source,tmp_path):
    ws,feed_root,model = source
    frozen.capture(ws.root,feed_root,model,'2026-06',tmp_path/'one',catalog(),False)
    frozen.capture(ws.root,feed_root,model,'2026-06',tmp_path/'two',catalog(),False)
    one,first = frozen.verify(tmp_path/'one'); _two,second = frozen.verify(tmp_path/'two')
    assert first==second and one['allow_zero_equal'] is False
    frozen.capture(ws.root,feed_root,model,'2026-06',tmp_path/'other-policy',catalog(),True)
    assert frozen.verify(tmp_path/'other-policy')[1]!=first
    assert set(one['feed_manifest']['objects'])>=frozen.REQUIRED_OBJECTS
    assert json.loads((tmp_path/'one/deltas.json').read_text())==[]


def test_blank_tracking_charge_stays_in_common_denominator(source,tmp_path):
    ws,feed_root,model = source
    file = write_xlsx(tmp_path/'unknown.xlsx',[
        ['运单号码','账单结算对象名称','总金额','扫描时间'],
        [None,'未归属但真实收费',8,'2026/06/03 12:00:00']])
    ws.keep('运费.xlsx',file,SHARED_STORE_ID)
    frozen.capture(ws.root,feed_root,model,'2026-06',tmp_path/'bundle',catalog())
    manifest = experiments.run_bundle(tmp_path/'bundle',tmp_path/'proposal')
    assert manifest['a_summary']['source_count']==manifest['b_summary']['source_count']==1
    assert Decimal(manifest['b_summary']['source_abs'])==8
    assert Decimal(manifest['b_summary']['coverage'])==0
    rows = pl.read_parquet(tmp_path/'proposal/diff.parquet').to_dicts()
    assert rows[0]['source_issue']=='identity_missing' and rows[0]['b_status']=='pending'


def test_shared_job_queue_coalesces_work_and_reports_failure_without_partial_result(source,monkeypatch):
    ws,feed_root,model = source
    parent = freight_trial.Jobs(ws.root); jobs = experiments.Jobs(parent)
    entered,released = threading.Event(),threading.Event()
    def fail(*_args):
        entered.set()
        assert released.wait(10)
        raise InputError('原始包缺失测试')
    monkeypatch.setattr(experiments,'run_bundle',fail)
    try:
        first = jobs.start('2026-06',model,feed_root)
        assert entered.wait(10)
        assert jobs.start('2026-06',model,feed_root)['experiment_id']==first['experiment_id']
        assert experiments.query(jobs,first['experiment_id'])['result'] is None
    finally:
        released.set(); parent.pool.shutdown(wait=True)
    job = jobs.read(first['experiment_id'])
    assert job['state']=='failed' and '原始包缺失' in job['error']
    with pytest.raises(InputError,match='未完成'):
        jobs.manifest(first['experiment_id'])


def test_snapshot_object_name_cannot_escape_the_isolated_bundle(source,tmp_path):
    ws,feed_root,model=source
    import sqlite3
    with sqlite3.connect(ws.root/'order-feed.db') as db:
        manifest=json.loads(db.execute('SELECT manifest_json FROM feed_state').fetchone()[0])
        manifest['objects']['../../outside.parquet']=manifest['objects']['orders.parquet']
        db.execute('UPDATE feed_state SET manifest_json=?',(json.dumps(manifest),))
    with pytest.raises(InputError,match='对象名称'):
        frozen.capture(ws.root,feed_root,model,'2026-06',tmp_path/'bundle',catalog())
    assert not (tmp_path/'outside.parquet').exists()


def test_zero_equal_switch_is_applied_to_the_real_candidate_rule():
    charge={'charge_id':'a'*32,'tracking_no':'W','file_sha':'f'*64,'amount':'1','source_issue':'','scan_time':'2026-06-01'}
    members=[{'target_key':key,'store_id':'s','order_id':'main','child_id':key,'product_id':'p'+key,
        'identity_verified':True,'amount_verified':True,'portion_amount':'0','basis_amount':'0',
        'basis_unit':'amount','basis_source':'erp_goods_amount','whole_child':False} for key in ('1','2')]
    def packages():return {'W':{'packet_id':'parcel','store_ids':['s'],'issue':'','allow_zero_equal':True,'members':members}}
    denied=experiments._b_results([charge],packages(),False)[charge['charge_id']]
    allowed=experiments._b_results([charge],packages(),True)[charge['charge_id']]
    assert denied['status']=='pending' and denied['reason']=='zero_basis' and denied['allow_zero_equal'] is False
    assert allowed['method']=='zero_identity_equal' and sum(Decimal(r['amount']) for r in allowed['allocations'])==1


def test_b_plus_without_new_evidence_cannot_claim_an_input_upgrade(ready,source,monkeypatch):
    bundle,target,_manifest=ready;ws,feed_root,model=source
    parent=freight_trial.Jobs(ws.root);jobs=experiments.Jobs(parent);identifier='b'*32
    path=jobs.path(identifier);path.mkdir(parents=True)
    shutil.copytree(bundle,path/'inputs');shutil.copytree(target,path/'result')
    freight_trial._write_json(path/'job.json',{'experiment_id':identifier,'job_id':identifier,'state':'ready','period':'2026-06',
        'result_manifest_sha':freight_trial._hash_file(path/'result/manifest.json')})
    monkeypatch.setattr(experiments.store_classification,'resolve',lambda *_:catalog())
    try:
        attempt=jobs.start('2026-06',model,feed_root,evidence_parent_id=identifier)
        parent.pool.submit(lambda:None).result(timeout=30)
        failed=jobs.read(attempt['experiment_id'])
        assert failed['state']=='failed' and '没有形成新的输入版本' in failed['error']
        assert jobs.read(identifier)['state']=='ready'
    finally:parent.pool.shutdown(wait=True)


def test_experiment_jobs_api_frozen_scope_export_and_no_business_apply(ready,source,monkeypatch):
    bundle,target,manifest = ready
    ws,_feed,model = source
    parent = freight_trial.Jobs(ws.root); jobs = experiments.Jobs(parent); identifier = 'a'*32
    path = jobs.path(identifier); path.mkdir(parents=True)
    shutil.copytree(bundle,path/'inputs'); shutil.copytree(target,path/'result')
    freight_trial._write_json(path/'job.json',{'experiment_id':identifier,'job_id':identifier,'state':'ready','period':'2026-06',
        'result_manifest_sha':freight_trial._hash_file(path/'result/manifest.json')})
    monkeypatch.setattr(experiments,'manager',lambda *_:jobs)
    monkeypatch.setattr(experiments.store_classification,'resolve',lambda *_:pytest.fail('current classification read'))
    app = FastAPI(); install(app,lambda:ws,lambda:model,lambda:{})
    client = TestClient(app); base = '/api/freight-review/experiments/'+identifier
    try:
        page = client.get(base).json()
        assert page['result']['input_fingerprint']==manifest['input_fingerprint']
        filtered = client.get(base,params={'only_category':'festival'}).json()
        assert filtered['result']['b_summary']==page['result']['b_summary']
        assert filtered['count']<page['count']
        bad = client.get(base+'/diff.csv',params={'only_category':'festival','scope_fingerprint':page['scope_fingerprint']})
        assert bad.status_code==400
        good = client.get(base+'/diff.csv',params={'scope_fingerprint':page['scope_fingerprint']})
        assert good.status_code==200
        rows = list(csv.DictReader(io.StringIO(good.text.lstrip('\ufeff'))))
        assert sum(Decimal(r['原始收费（每笔首行）']) for r in rows if r['原始收费（每笔首行）'])==18
        assert sum(Decimal(r['B金额']) for r in rows if r['B金额'])==10
        assert all(r['A金额']=='' and r['B减A']=='' for r in rows)
        grouped=client.get(base+'/groups').json()
        assert grouped['result']['comparison_grain']=='source_component'
        assert client.get(base+'/group-diff.csv',params={'scope_fingerprint':page['scope_fingerprint']}).status_code==400
        grouped_csv=client.get(base+'/group-diff.csv',params={'scope_fingerprint':grouped['scope_fingerprint']})
        assert grouped_csv.status_code==200
        components=list(csv.DictReader(io.StringIO(grouped_csv.text.lstrip('\ufeff'))))
        assert sum(Decimal(r['原收费（组件首行）']) for r in components if r['原收费（组件首行）'])==18
        assert sum(Decimal(r['B减A']) for r in components if r['B减A'])==0
        assert client.post(base+'/apply').status_code==404
        assert client.post('/api/freight-review/experiments',json={'period':'2026-13'}).status_code==400
    finally:
        parent.pool.shutdown(wait=True)


def test_execution_identity_binds_formal_engine_feed_schema_and_production_manifest(tmp_path,monkeypatch):
    package=tmp_path/'runtime/ledger/ledger';(package/'engine').mkdir(parents=True);(package/'model').mkdir()
    for relative in ('freight_experiments.py','engine/runtime.py','order_feed.py','model/schema.py'):
        (package/relative).write_text('value = 1\n')
    monkeypatch.setattr(experiments,'CODE_ROOT',package)
    first=experiments.code_identity()
    assert {r['path'] for r in first['python_sources']}=={'freight_experiments.py','engine/runtime.py','order_feed.py','model/schema.py'}
    (package/'engine/runtime.py').write_text('value = 2\n')
    second=experiments.code_identity()
    assert second['engine']==first['engine'] and second['package_code_sha']!=first['package_code_sha']
    marker=tmp_path/'runtime/PRODUCTION-CODE-MANIFEST.json';marker.write_text('{"files":[]}')
    third=experiments.code_identity()
    assert third['production_code_manifest_sha256']==freight_trial._hash_file(marker)
    assert third!=second


def test_formal_harness_does_not_abort_on_a_declared_empty_helper_sheet(source,tmp_path):
    import openpyxl
    from ledger.engine.runtime import ingest
    ws,feed_root,model=source
    raw=write_xlsx(tmp_path/'carrier-with-helper.xlsx',[
        ['运单号码','账单结算对象名称','总金额','扫描时间'],
        ['SF1','测试店',12,'2026/06/03 12:00:00'],['UNKNOWN','未归属',8,'2026/06/03 13:00:00'],
        ['SF1','测试店',-2,'2026/06/04 12:00:00']])
    workbook=openpyxl.load_workbook(raw)
    workbook.create_sheet('空辅助表').append(['仅说明字段，不是财务原表'])
    workbook.save(raw);workbook.close()
    check=ingest([raw],model,[model.stores[0].name,*model.stores[0].aliases],default_store=model.stores[0].name)
    assert any(item.derivative is not None and item.error for item in check.unknown)
    ws.keep('运费.xlsx',raw,SHARED_STORE_ID)
    bundle=tmp_path/'helper-bundle';target=tmp_path/'helper-result'
    frozen.capture(ws.root,feed_root,model,'2026-06',bundle,catalog())
    result=experiments.run_bundle(bundle,target)
    assert result['a_summary']['source_count']==3 and result['b_summary']['source_count']==3


def test_partial_comparison_keeps_unmapped_scope_explicit_and_all_charges(source,tmp_path):
    ws,feed_root,model=source
    extra=Store(id='unmapped',name='未映射合成店',platform='taobao')
    model=model.model_copy(update={'stores':(*model.stores,extra)})
    raw=write_xlsx(tmp_path/'notes.xlsx',[['仅说明字段'],['合成材料']])
    ws.keep('notes.xlsx',raw,extra.id)
    bundle=tmp_path/'bundle-unmapped'
    frozen.capture(ws.root,feed_root,model,'2026-06',bundle,catalog(),baseline_policy='strict_complete')
    with pytest.raises(InputError,match='已确认店铺映射'):
        experiments.run_bundle(bundle,tmp_path/'strict-result')
    inputs=json.loads((bundle/'inputs.json').read_text())
    inputs['baseline_policy']='confirmed_entities_with_explicit_unavailable'
    (bundle/'inputs.json').write_text(json.dumps(inputs,ensure_ascii=False));frozen.seal(bundle)
    result=experiments.run_bundle(bundle,tmp_path/'partial-result')
    availability=result['baseline_scope']['availability']
    assert availability['unavailable_is_zero'] is False
    missing=next(r for r in availability['scopes'] if r['store_id']==extra.id)
    assert missing['status']=='baseline_unavailable' and missing['reason']=='confirmed_mapping_missing'
    assert result['a_summary']['source_count']==result['b_summary']['source_count']==3
    assert Decimal(result['a_summary']['source_abs'])==Decimal(result['b_summary']['source_abs'])==22


def test_original_service_unknown_table_is_preserved_without_wrapper_only_abort(source,tmp_path,monkeypatch):
    from ledger.engine import runtime
    from ledger.engine.types import FileRef,Recognition
    ws,feed_root,model=source;original=runtime.ingest
    def with_unknown(*args,**kwargs):
        ingestion=original(*args,**kwargs)
        ref=FileRef('f'*64,'synthetic-unknown.xlsx','synthetic')
        ingestion.items.append(runtime.Ingested(ref=ref,
            recognition=Recognition(ref=ref,signature='',header_count=1,reason='synthetic unrecognized table'),
            rows=1,error='合成未知表未映射字段'))
        return ingestion
    monkeypatch.setattr(runtime,'ingest',with_unknown)
    bundle=tmp_path/'unknown-bundle';frozen.capture(ws.root,feed_root,model,'2026-06',bundle,catalog())
    result=experiments.run_bundle(bundle,tmp_path/'unknown-result')
    available=result['baseline_scope']['availability']['scopes'][0]
    assert available['original_unknowns_preserved'] is True and len(available['original_service_unknowns'])==1
    assert result['a_summary']['allocated_count']+result['a_summary']['group_matched_count']>0
