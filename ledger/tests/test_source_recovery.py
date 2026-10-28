"""Declared synthetic source contract. No production/network/financial approval."""
from types import SimpleNamespace
from pathlib import Path
import hashlib
import io
import json

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledger import automation_actions as actions, automation_checker as checker
from ledger import automation_period_proof as period_proof, source_recovery, automation_store as journal
from ledger.model.schema import Overhead
from ledger.storage_integrity import seal
from ledger.worker_control_api import install
from ledger.workspace import Workspace
from test_commission import _model
from test_jst_source_members import fixture, xlsx

PERIOD='2026-05'


@pytest.fixture
def source_case(tmp_path,monkeypatch,request):
    options=getattr(request,'param',{})
    target_period='2026-09' if options.get('date_only') else PERIOD
    monkeypatch.setenv('LEDGER_SOURCE_RECOVERY_PERIODS',target_period)
    original=fixture()
    cost=original[2][original[0].cost_anchors[0].sha256]
    native=xlsx(['主订单编号','子订单编号','订单创建时间'],['202600001','C1','2026-05-15'])
    base=_model(overheads=(Overhead(period=target_period,amount=0),))
    model=base.model_copy(update={
        'metrics':tuple(m.model_copy(update={'id':'goods_cost'}) if m.id=='goods' else m for m in base.metrics),
        'sources':tuple(s.model_copy(update={'is_spine':True}) if s.id=='order' else
                        s.model_copy(update={'provides':('goods_cost',)}) if s.id=='cost' else s for s in base.sources),
        'statement':tuple(n.model_copy(update={'headline':'revenue'}) if n.id=='n_receipt' else
                          n.model_copy(update={'formula':n.formula.model_copy(update={'of':('goods_cost',)})}) if n.id=='n_goods' else n for n in base.statement)})
    if options.get('with_dates'):
        from ledger.model.loader import load_model
        from conftest import MODELS
        complete=load_model(MODELS/'cn-ecommerce')
        contract=next(s for s in complete.sources if s.id=='refund_order_dates')
        template=next(t for t in complete.templates if t.id=='refund_order_dates_v1')
        model=model.model_copy(update={'sources':(*model.sources,contract),'templates':(*model.templates,template)})
    ws=Workspace(tmp_path/'workspace');checker.bind_model(ws,model)
    raw_cost=ws.keep('synthetic-cost.xlsx',io.BytesIO(cost),'s1')
    raw_native=ws.keep('synthetic-native.xlsx',io.BytesIO(native),'s1')
    result={'can_close':False,'missing_sources':[],'findings':[{'id':'goods_cost_gap','passed':False,'blocking':True}],
            'statement':[{'id':'n_receipt','value':100,'available':True}],
            'calculation_inputs':{'metric_totals':{'receipt':100},'unavailable_metrics':['goods_cost']}}
    rid=ws.record('s1',target_period,result,[raw_cost.sha,raw_native.sha])
    coverage=pl.DataFrame({'coverage_key':['synthetic-one'],'context_sha':['a'*64],'order_id':['202600001'],
                          'order_count':[1],'goods_scope_version':[1],'partial_cost':[False]})
    coverage.write_parquet(ws.coverage_gaps_path(rid));seal(ws.coverage_gaps_path(rid))
    facts=pl.DataFrame({'metric_id':['receipt'],'amount':[100.], 'source_id':['order'],'template_id':['t_order'],
                       'store':['测试店'],'period':[target_period],'file_sha':[raw_native.sha],'sheet':['证据'],'row_no':[3]})
    raw_funds=None
    if options.get('date_only'):
        funds=xlsx(['退款原订单号','流水发生日期'],['202600001','2026-09-20'])
        raw_funds=ws.keep('synthetic-funds.xlsx',io.BytesIO(funds),'s1')
        facts=pl.DataFrame({'metric_id':['trade_refund'],'amount':[-10.],'source_id':['funds'],
            'template_id':['declared-synthetic-funds'],'store':['测试店'],'period':['(未知账期)'],
            'source_period':[target_period],'file_sha':[raw_funds.sha],'sheet':['证据'],'row_no':[3],
            'link_key':['202600001'],'order_id':['202600001'],'source_note':['下单月份：待核对']})
    facts.write_parquet(ws.facts_path(rid));seal(ws.facts_path(rid))
    items=[]
    for ref,role,values in [(raw_native,'order',{'order_id':['202600001'],'__sheet__':['证据'],'__row__':[3]}),
                            (raw_cost,'cost',{'original_order_id':['202600001'],'internal_order_id':['100'],
                                             '__sheet__':['证据'],'__row__':[3]})]:
        items.append(SimpleNamespace(ref=SimpleNamespace(sha256=ref.sha),template=SimpleNamespace(source=role),frame=pl.DataFrame(values)))
    ingestion=SimpleNamespace(model=model,items=items,known=items)
    if raw_funds:
        items.append(SimpleNamespace(ref=SimpleNamespace(sha256=raw_funds.sha),template=SimpleNamespace(source='funds'),frame=pl.DataFrame()))
    sl=SimpleNamespace(period=target_period,facts=facts,pricing_gaps=pl.DataFrame(),coverage_gap_rows=coverage)
    proof=period_proof.prepare(ws,model,model.store('s1'),ingestion,sl,result,None)
    period_proof.save(ws,rid,proof)
    actions.sync_month(ws,model,target_period)
    actor={'id':'source-worker','kind':'service','authenticated':True,'scopes':['business']}
    clock=[1000]
    app=FastAPI();install(app,lambda:ws,lambda:model,lambda request:actor,lease_seconds=30,clock=lambda:clock[0])
    client=TestClient(app)
    class Mapping:
        def get(self,path):
            assert path=='stores'
            return {'stores':[{'ledger_store_id':'s1','order_store_id':'77','mapping_status':'confirmed','consumable':True}]}
    monkeypatch.setattr('ledger.order_feed.Client',Mapping)
    case=ws.conn.execute('SELECT * FROM automation_case').fetchone()
    request={'case_id':case['id'],'run_id':rid,'input_sha':case['input_sha'],'coverage_key':'synthetic-one','context_sha':'a'*64}
    yield SimpleNamespace(ws=ws,model=model,client=client,actor=actor,body=request,original=original,tmp_path=tmp_path,clock=clock,period=target_period)
    client.close();ws.close()


def request_task(c):
    response=c.client.post('/api/automation/source-recovery/requests',json=c.body)
    assert response.status_code==200,response.text
    return response.json()


def test_new_run_seals_only_real_original_row_anchors(source_case):
    c=source_case
    proof=json.loads(c.ws.conn.execute('SELECT manifest_json FROM automation_run_proof').fetchone()[0])
    index=proof['source_recovery_index']['orders']['202600001']
    assert len(index['native'])==len(index['cost'])==1
    assert index['internal_order_ids']==['100']
    assert all(set(a)=={'sha256','sheet','row'} for kind in ('native','cost') for a in index[kind])
    assert 'quantity' not in json.dumps(index) and 'amount' not in json.dumps(index)


def test_request_is_persistent_idempotent_and_does_not_book_money(source_case):
    c=source_case;first=request_task(c);second=request_task(c)
    assert first==second and first['state']=='source_queued'
    assert c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='source_queued'").fetchone()[0]==1
    assert not c.ws.conn.execute('SELECT 1 FROM cost_line_log').fetchone()
    assert c.client.post('/api/automation/source-recovery/requests',json={**c.body,'amount':1}).status_code==422
    c.actor['kind']='human'
    assert c.client.post('/api/automation/source-recovery/requests',json=c.body).status_code==403


def test_old_input_and_arbitrary_anchor_or_path_cannot_be_requested(source_case):
    c=source_case
    assert c.client.post('/api/automation/source-recovery/requests',json={**c.body,'input_sha':'old'}).status_code==409
    assert c.client.post('/api/automation/source-recovery/requests',json={**c.body,'native_anchors':[]}).status_code==422
    assert c.client.post('/api/automation/source-recovery/requests',json={**c.body,'path':'/tmp/fake'}).status_code==422


def test_source_task_requires_capable_worker_and_is_not_business_complete(source_case):
    c=source_case;task=request_task(c)
    assert c.client.post('/api/v1/agent-jobs/claim',json={'capabilities':['recompute']}).status_code==204
    response=c.client.post('/api/v1/agent-jobs/claim',json={'capabilities':['recompute','source_recovery']})
    assert response.status_code==200,response.text
    job=response.json();assert job['payload']['source_task']['task_id']==task['task_id']
    body={'lease_token':job['payload']['lease_token'],'input_sha':c.body['input_sha'],'result':{}}
    assert c.client.post('/api/v1/agent-jobs/'+job['id']+'/complete',json=body).status_code==409


def source_claim(c):
    response=c.client.post('/api/v1/agent-jobs/claim',json={'capabilities':['recompute','source_recovery']})
    assert response.status_code==200,response.text
    return response.json()


def test_source_lease_recovery_fences_expired_worker_and_keeps_same_task(source_case):
    c=source_case;task=request_task(c);first=source_claim(c);c.clock[0]+=31;second=source_claim(c)
    assert first['id']!=second['id']
    assert first['payload']['source_task']==second['payload']['source_task']
    assert second['payload']['lease_token']>first['payload']['lease_token']
    response=c.client.post('/api/automation/source-recovery/requests/'+task['task_id']+'/finish',json={
        'job_id':first['id'],'lease_token':first['payload']['lease_token'],
        'input_sha':c.body['input_sha'],'receipt_sha256':'b'*64})
    assert response.status_code==409
    assert c.ws.conn.execute('SELECT state FROM automation_attempt WHERE id=?',(first['id'],)).fetchone()[0]=='interrupted'


def test_worker_supplied_ready_json_or_path_cannot_replace_raw_bytes(source_case):
    c=source_case;task=request_task(c);job=source_claim(c)
    body={'job_id':job['id'],'lease_token':job['payload']['lease_token'],
          'input_sha':c.body['input_sha'],'receipt_sha256':'b'*64}
    endpoint='/api/automation/source-recovery/requests/'+task['task_id']+'/finish'
    assert c.client.post(endpoint,json={**body,'path':'/tmp/elsewhere'}).status_code==422
    assert c.client.post(endpoint,json={**body,'raw_ready':True,'amount':'1'}).status_code==422
    assert c.client.post(endpoint,json=body).status_code==422
    assert c.ws.conn.execute('SELECT state FROM automation_attempt WHERE id=?',(task['task_id'],)).fetchone()[0]=='source_queued'


def test_disabled_or_outside_case_period_stays_pending_without_source_dispatch(source_case,monkeypatch):
    c=source_case;monkeypatch.setenv('LEDGER_SOURCE_RECOVERY_PERIODS','2026-09')
    task=request_task(c)
    assert task['state']=='source_pending' and task['reason']=='source_recovery_scope_not_enabled'
    assert not c.ws.conn.execute("SELECT 1 FROM automation_attempt WHERE state='source_queued'").fetchone()


def test_source_plan_does_not_follow_changed_case_input(source_case):
    c=source_case;task=request_task(c);job=source_claim(c)
    with c.ws.conn:c.ws.conn.execute("UPDATE automation_case SET input_sha='changed'")
    endpoint='/api/automation/source-recovery/requests/'+task['task_id']+'/plan'
    response=c.client.get(endpoint,params={'job_id':job['id'],'lease_token':job['payload']['lease_token']})
    assert response.status_code==409
