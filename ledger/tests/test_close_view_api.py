"""Final readiness is a fresh view dependency, never a financial recomputation."""
from copy import deepcopy
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ledger import api,automation_actions,automation_checker
from ledger.automation_store import digest
from ledger.replay_service import Session
from test_replay_service import bundle,seed

PERIOD='2026-07'
HUMAN={'id':'readiness-fixture-human','kind':'human','authenticated':True}


@pytest.fixture
def ready_service(tmp_path,monkeypatch):
    source=tmp_path/'original-inputs';source.mkdir()
    path,missing=bundle.__wrapped__(source)
    with Session(path,seed()) as session:
        session.drain_pending()
        for scope,order in missing.items():
            sid,period=scope.split('/')
            session.supplement(sid,period,amount='2.00',reason='合成原采购凭据已逐单审核',order_id=order)
        session.drain_pending()
        active={'model':session.model}
        monkeypatch.setattr(api,'workspace',lambda:session.ws)
        monkeypatch.setattr(api,'_model',lambda:active['model'])
        monkeypatch.setattr(api,'_snapshot',lambda:SimpleNamespace(model=active['model'],revision=digest(active['model'].model_dump_json())))
        app=FastAPI()
        app.get('/api/overview')(api.overview)
        app.get('/api/stores/{store_id}')(api.store_detail)
        app.get('/api/stores/{store_id}/periods/{period}')(api.period_detail)
        with TestClient(app) as client:
            yield client,session,active


@pytest.mark.parametrize('endpoint',['/api/overview?store_id=s1&period='+PERIOD,'/api/stores/s1','/api/stores/s1/periods/'+PERIOD])
def test_other_store_new_income_changes_readiness_and_etag_without_recomputing_this_run(ready_service,monkeypatch,endpoint):
    client,session,_=ready_service;ws=session.ws
    run=ws.state('s1',PERIOD).run_id
    builds=[];original=api._build_period_detail
    def counted(*args,**kwargs):builds.append(1);return original(*args,**kwargs)
    monkeypatch.setattr(api,'_build_period_detail',counted)
    first=client.get(endpoint)
    assert first.status_code==200
    def cell(body):
        if 'cells' in body:return body['cells'][0]
        if 'periods' in body:return next(row for row in body['periods'] if row['period']==PERIOD)
        return body
    assert cell(first.json())['can_close'] is True
    assert client.get(endpoint,headers={'If-None-Match':first.headers['etag']}).status_code==304
    old_generation=ws.read_generation(store_id='s1')
    peer=ws.state('s2',PERIOD);changed=deepcopy(peer.result)
    next(row for row in changed['statement'] if row['id']=='n_receipt')['value']=300
    # A new candidate has genuinely arrived but lacks a captured input proof.
    # It must not supply a fabricated zero/known company denominator.
    ws.record('s2',PERIOD,changed,[],input_fingerprint='new-peer-income-awaiting-proof')
    before_read=ws.conn.execute('SELECT count(*) FROM run').fetchone()[0]
    again=client.get(endpoint,headers={'If-None-Match':first.headers['etag']})
    assert again.status_code==200 and again.headers['etag']!=first.headers['etag']
    outward=cell(again.json())
    assert outward['engine_can_close'] is True and outward['can_close'] is False
    assert any(issue['code']=='company_revenue_unknown' for issue in outward['close_verification']['issues'])
    assert ws.state('s1',PERIOD).run_id==run
    assert ws.read_generation(store_id='s1')==old_generation
    assert ws.conn.execute('SELECT count(*) FROM run').fetchone()[0]==before_read
    if '/periods/' in endpoint:assert len(builds)==1,'Other-store readiness rebuilt the unchanged financial payload'
    assert ws.state('s1',PERIOD).result['can_close'] is True,'Outward decoration changed archived engine evidence'


def test_approved_overhead_decision_updates_cached_period_and_etag_without_a_new_run(ready_service,monkeypatch):
    client,session,active=ready_service;ws=session.ws
    active['model']=session.model.model_copy(update={'overheads':()})
    endpoint='/api/stores/s1/periods/'+PERIOD
    builds=[];original=api._build_period_detail
    def counted(*args,**kwargs):builds.append(1);return original(*args,**kwargs)
    monkeypatch.setattr(api,'_build_period_detail',counted)
    first=client.get(endpoint);assert first.status_code==200
    assert first.json()['can_close'] is False and first.json()['engine_can_close'] is True
    assert any(row['code']=='overhead_total_unknown' for row in first.json()['close_verification']['issues'])
    count=ws.conn.execute('SELECT count(*) FROM run').fetchone()[0]
    proposal=automation_actions.decision_preview(ws,active['model'],request_id='overhead-read-preview',period=PERIOD,store_id='',
        kind='overhead_total',values={'amount':'0.00'},reason='合成夹具明确批准本月零兼职',actor=HUMAN)
    automation_actions.decision_apply(ws,active['model'],request_id='overhead-read-apply',proposal_id=proposal['proposal_id'],
        proposal_sha=proposal['proposal_sha'],input_sha=proposal['input_sha'],actor=HUMAN)
    again=client.get(endpoint,headers={'If-None-Match':first.headers['etag']})
    assert again.status_code==200 and again.headers['etag']!=first.headers['etag']
    assert again.json()['can_close'] is True
    assert len(builds)==1
    assert ws.conn.execute('SELECT count(*) FROM run').fetchone()[0]==count


def test_company_scope_is_computed_once_per_month_for_overview(ready_service,monkeypatch):
    client,_,_=ready_service;calls=[];original=automation_checker.company_basis
    def counted(ws,model,period):calls.append(period);return original(ws,model,period)
    monkeypatch.setattr(automation_checker,'company_basis',counted)
    response=client.get('/api/overview')
    assert response.status_code==200
    assert sorted(calls)==['2026-06','2026-07']


def test_cached_period_keeps_unique_rate_evidence_until_after_labor_annotation(tmp_path,monkeypatch):
    from ledger.commission_registry import Registry
    from ledger.workspace import Workspace
    from test_manual_cost import model,result,recorded

    ws=Workspace(tmp_path);registry=Registry(tmp_path);m=model()
    person=registry.person_save({'name':'合成费率人员'},'test','合成夹具')
    raw=result(m,person)
    raw.update(can_close=True,findings=[],missing_sources=[],statement=[
        {'id':'n_receipt','value':1000,'available':True},
        {'id':'net_profit','value':500,'available':True},
    ])
    raw['commission'].update(engine='commission-v2',total=50,base_total=1000,base_node='net_profit',
        products=[{'product_id':'synthetic-product','total_rate':.1,
            'people':[{'person_id':person['id']}], 'orders':['must-stay-in-archive']}])
    raw['commission']['people'][0]['amount']=50
    run=recorded(ws,m,raw)
    monkeypatch.setattr(api,'workspace',lambda:ws)
    monkeypatch.setattr(api,'_snapshot',lambda:SimpleNamespace(model=m,revision=digest(m.model_dump_json())))
    builds=[];original=api._build_period_detail
    def counted(*args,**kwargs):
        view=original(*args,**kwargs);builds.append(deepcopy(view));return view
    monkeypatch.setattr(api,'_build_period_detail',counted)
    app=FastAPI();app.get('/api/stores/{store_id}/periods/{period}')(api.period_detail)
    endpoint=f"/api/stores/{raw['store_id']}/periods/{raw['period']}"
    try:
        with TestClient(app) as client:
            for _ in range(2):
                response=client.get(endpoint)
                assert response.status_code==200,response.text
                body=response.json()
                assert body['pending_labor_cut']==100
                assert body['commission']['people'][0]['amount_after_labor']==40
                assert 'products' not in body['commission']
            assert client.get(endpoint,headers={'If-None-Match':response.headers['etag']}).status_code==304
        assert len(builds)==1,'The second read must exercise the cached business payload'
        assert builds[0]['commission']['products']==[{'total_rate':.1,'people':[{'person_id':person['id'],'duty':None}]}]
        assert ws.state(raw['store_id'],raw['period']).run_id==run
        assert ws.state(raw['store_id'],raw['period']).result==raw
    finally:
        ws.close()
