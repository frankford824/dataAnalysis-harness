"""Real case/lease/HTTP waiting transitions in temporary workspaces."""
import hashlib
import json

import pytest

from ledger import automation_actions as actions,automation_handoff as handoff
from ledger.commission_manager import Manager
from ledger.commission_registry import Registry
from test_worker_control_api import running,claim,terminal


def observation(ws,reason='original_evidence_missing',source='order',tool='collect_case_evidence'):
    path=ws.root/('worker-receipt-'+reason+'-'+source+'.json');path.write_text(json.dumps({'reason_code':reason,'source_kind':source}))
    return {'tool_id':tool,'source_kind':source,'reason_code':reason,
        'artifact_refs':[{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]}


def submit(client,job,outcomes,**extra):
    return client.post(f"/api/v1/agent-jobs/{job['id']}/fail",json={**terminal(job),'result':{},'tool_outcomes':outcomes,**extra})


def test_source_wait_is_role_level_not_completed_and_repeated_delivery_preserves_since(running):
    ws,model,client,timer,service=running;job=claim(client)
    body=observation(ws)
    first=submit(client,job,[body]);assert first.status_code==200,first.text
    value=first.json();assert value['job']['state']=='waiting_source'
    assert not value['job']['verification']['allowed']
    timer[0]+=1000
    assert submit(client,job,[body]).json()==value
    assert client.post('/api/v1/agent-jobs/claim',json={}).status_code==204
    month=actions.month(ws,model,'2030-06')
    assert month['counts']['waiting']==1 and month['counts']['completed']==0
    assert month['stores'][0]['stage']=='waiting_source'
    wait=month['waiting_groups'][0]
    assert wait['assignee_status']=='role_unconfigured' and not wait.get('person_id')
    assert wait['since']==value['job']['waiting']['since']
    assert wait['count']==1 and wait['notification_key']
    assert ws.conn.execute('SELECT count(*) FROM automation_source_handoff').fetchone()[0]==1
    assert not ws.conn.execute("SELECT 1 FROM period WHERE state='closed'").fetchone()


@pytest.mark.parametrize('reason,source,role,evidence',[
    ('source_unavailable','order','system_maintenance','source_availability'),
    ('login_required','history_cost','source_administrator','source_login'),
    ('sku_contract_unknown','history_cost','source_contract_owner','sku_quantity_contract'),
    ('technical_failure','system','system_maintenance','worker_execution'),
])
def test_technical_sources_go_to_maintenance_not_financial_decision(running,reason,source,role,evidence):
    ws,model,client,timer,service=running;job=claim(client)
    response=submit(client,job,[observation(ws,reason,source)])
    assert response.status_code==200,response.text
    month=actions.month(ws,model,'2030-06');group=month['waiting_groups'][0]
    assert month['stores'][0]['stage']=='waiting_maintenance'
    assert group['kind']=='maintenance' and group['role']==role and group['evidence_type']==evidence
    assert month['counts']['completed']==0 and not month['stores'][0]['verification']['allowed']


def test_two_reasons_remain_one_waiting_case_and_queue_ack_does_not_restart_model_work(running,monkeypatch):
    ws,model,client,timer,service=running;job=claim(client)
    monkeypatch.setenv('LEDGER_ORDER_FEED_ENABLED','0')
    outcomes=[observation(ws),observation(ws,'technical_failure','system','worker_runtime')]
    response=submit(client,job,outcomes);assert response.status_code==200,response.text
    before=actions.month(ws,model,'2030-06')
    assert before['counts']['waiting']==1 and len(before['waiting_groups'])==2
    since=[g['since'] for g in before['waiting_groups']]
    # An actual empty Manager pass retires queue delivery metadata. No file,
    # feed or rule changed, so this must not clear the source wait.
    Manager(lambda:ws,lambda:model).once()
    after=actions.month(ws,model,'2030-06')
    assert after['counts']['waiting']==1 and [g['since'] for g in after['waiting_groups']]==since
    assert client.post('/api/v1/agent-jobs/claim',json={}).status_code==204
    assert not ws.conn.execute('SELECT 1 FROM run').fetchone()


def test_actual_source_file_change_retires_wait_and_old_lease_without_claiming_ready(running,monkeypatch):
    ws,model,client,timer,service=running;job=claim(client)
    assert submit(client,job,[observation(ws)]).status_code==200
    import io
    kept=ws.keep('new-original-orders.csv',io.BytesIO(b'original source delivery'),'s1')
    Registry(ws.root).enqueue_files({'s1'},'nas:'+kept.sha)
    actions.sync_month(ws,model,'2030-06')
    current=actions.month(ws,model,'2030-06')
    assert current['counts']['waiting']==0 and current['waiting_groups']==[]
    assert current['counts']['completed']==0 and not current['stores'][0]['verification']['allowed']
    stale=client.post(f"/api/v1/agent-jobs/{job['id']}/fail",headers={'Idempotency-Key':'different-old-event'},
        json={**terminal(job),'tool_outcomes':[observation(ws)]})
    assert stale.status_code==409
    assert current['stores'][0]['stage']=='calculating'
    assert client.post('/api/v1/agent-jobs/claim',json={}).status_code==204
    monkeypatch.setenv('LEDGER_ORDER_FEED_ENABLED','0')
    Manager(lambda:ws,lambda:model).once()
    assert claim(client)['payload']['automation_case']['input_sha']!=job['payload']['automation_case']['input_sha']
    assert not ws.conn.execute('SELECT 1 FROM run').fetchone()


def test_rule_contract_change_retires_wait_without_altering_financial_checker(running,monkeypatch):
    ws,model,client,timer,service=running;job=claim(client)
    assert submit(client,job,[observation(ws,'sku_contract_unknown','history_cost')]).status_code==200
    from ledger import automation_rules
    original=automation_rules.contracts()
    monkeypatch.setattr(automation_rules,'contracts',lambda:{key:('new-contract',value[1]) for key,value in original.items()})
    actions.sync_month(ws,model,'2030-06')
    month=actions.month(ws,model,'2030-06')
    assert month['counts']['waiting']==0 and not month['stores'][0]['verification']['allowed']
    assert claim(client)['payload']['automation_case']['source_context_sha']!=job['payload']['automation_case']['source_context_sha']


def test_saved_source_health_recovery_wakes_wait_but_probe_clock_does_not(running):
    from ledger.order_feed import OrderFeed
    ws,model,client,timer,service=running
    feed=OrderFeed(ws.root)
    feed._refresh_health({'healthy':False,'degraded':['cost_api_worker'],'checked_at':'first'},0,0,'v0')
    actions.sync_month(ws,model,'2030-06');job=claim(client)
    assert submit(client,job,[observation(ws,'login_required','history_cost')]).status_code==200
    since=actions.month(ws,model,'2030-06')['waiting_groups'][0]['since']
    feed._refresh_health({'healthy':False,'degraded':['cost_api_worker'],'checked_at':'later'},0,0,'v0')
    assert actions.month(ws,model,'2030-06')['waiting_groups'][0]['since']==since
    assert client.post('/api/v1/agent-jobs/claim',json={}).status_code==204
    feed._refresh_health({'healthy':True,'degraded':[],'checked_at':'restored'},0,0,'v0')
    current=actions.month(ws,model,'2030-06')
    assert current['counts']['waiting']==0 and current['counts']['completed']==0
    assert claim(client)['payload']['automation_case']['source_context_sha']!=job['payload']['automation_case']['source_context_sha']


@pytest.mark.parametrize('tool,source,reason,related',[
    ('collect_case_evidence','history_cost','login_required','source_sha'),
    ('worker_runtime','system','technical_failure','runtime_sha'),
])
def test_only_related_worker_configuration_change_wakes_maintenance(running,tool,source,reason,related):
    from ledger import automation_store as journal
    ws,model,client,timer,service=running
    configured={'runtime_sha':'a'*64,'source_sha':'b'*64}
    def heartbeat():
        response=client.post('/api/v1/agents/heartbeat',json={'agent_id':'worker','configuration':configured,
            'configuration_sha':journal.digest(configured)})
        assert response.status_code==200,response.text
    heartbeat();job=claim(client)
    assert submit(client,job,[observation(ws,reason,source,tool)]).status_code==200
    original=actions.month(ws,model,'2030-06')['waiting_groups'][0]
    unrelated='runtime_sha' if related=='source_sha' else 'source_sha'
    configured[unrelated]='c'*64;heartbeat()
    current=actions.month(ws,model,'2030-06')
    assert current['counts']['waiting']==1 and current['waiting_groups'][0]['since']==original['since']
    assert client.post('/api/v1/agent-jobs/claim',json={}).status_code==204
    configured[related]='d'*64;heartbeat()
    assert actions.month(ws,model,'2030-06')['counts']['waiting']==0
    renewed=claim(client)
    assert renewed['payload']['lease_token']>job['payload']['lease_token']
    assert not actions.month(ws,model,'2030-06')['stores'][0]['verification']['allowed']


@pytest.mark.parametrize('change',[
    {'reason_code':'LLM says the amount is fine'}, {'amount':'3.34'}, {'message':'ask a guessed shop manager'},
    {'artifact_refs':[{'path':'https://untrusted.invalid/file','sha256':'a'*64}]},
])
def test_handoff_rejects_free_text_money_and_remote_references(running,change):
    ws,model,client,timer,service=running;job=claim(client)
    assert submit(client,job,[{**observation(ws),**change}]).status_code==422
    assert not ws.conn.execute('SELECT 1 FROM automation_source_handoff').fetchone()


def test_model_narrative_inside_result_cannot_create_a_source_wait(running):
    ws,model,client,timer,service=running;job=claim(client)
    response=client.post(f"/api/v1/agent-jobs/{job['id']}/fail",json={**terminal(job),'retryable':True,
        'result':{'tool_outcomes':[observation(ws)],'narrative':'the model wants the user to supply data'}})
    assert response.status_code==200 and response.json()['job']['state']=='retry_wait'
    assert not ws.conn.execute('SELECT 1 FROM automation_source_handoff').fetchone()


@pytest.mark.parametrize('extra',[
    {'result':{'narrative':'a model asks for an amount'}},{'message':'free diagnosis'},
    {'amount':'3.34'},{'error_code':'model says no financial basis'},
])
def test_source_handoff_envelope_does_not_carry_model_prose_or_money(running,extra):
    ws,model,client,timer,service=running;job=claim(client)
    assert submit(client,job,[observation(ws)],**extra).status_code==422
    assert not ws.conn.execute('SELECT 1 FROM automation_source_handoff').fetchone()


def test_wait_receipt_failure_rolls_back_handoff_attempt_case_and_authorization(running):
    ws,model,client,timer,service=running;job=claim(client)
    count=ws.conn.execute('SELECT count(*) FROM automation_authorization').fetchone()[0]
    ws.conn.execute("CREATE TRIGGER fail_wait_receipt BEFORE INSERT ON automation_receipt WHEN NEW.action='worker.source_handoff' BEGIN SELECT RAISE(ABORT,'receipt unavailable'); END")
    with pytest.raises(Exception,match='receipt unavailable'):submit(client,job,[observation(ws)])
    assert not ws.conn.execute('SELECT 1 FROM automation_source_handoff').fetchone()
    assert ws.conn.execute('SELECT state FROM automation_attempt WHERE id=?',(job['id'],)).fetchone()[0]=='worker_running'
    assert ws.conn.execute('SELECT count(*) FROM automation_authorization').fetchone()[0]==count


def test_other_store_revenue_and_overhead_change_do_not_restart_same_source_wait(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ledger import automation_checker as checker
    from ledger.model.schema import Store,Overhead
    from ledger.workspace import Workspace
    from ledger.worker_control_api import install
    from test_commission import _model
    ws=Workspace(tmp_path);period='2030-06'
    model=_model(stores=(Store(id='s1',name='甲',platform='taobao'),Store(id='s2',name='乙',platform='taobao')),
        overheads=(Overhead(period=period,amount=0),))
    model=model.model_copy(update={'statement':tuple(n.model_copy(update={'headline':'revenue'}) if n.id=='n_receipt' else n for n in model.statement)})
    checker.bind_model(ws,model)
    def income(value):return ws.record('s2',period,{'can_close':True,'statement':[{'id':'n_receipt','value':value,'available':True}],'findings':[]},[])
    income(100);actions.sync_month(ws,model,period)
    app=FastAPI();install(app,lambda:ws,lambda:model,lambda req:{'id':'worker','kind':'service','authenticated':True,'scopes':['business']})
    client=TestClient(app);job=claim(client)
    assert job['payload']['automation_case']['store_id']=='s1'
    assert submit(client,job,[observation(ws,'original_evidence_missing','history_cost')]).status_code==200
    before=actions.month(ws,model,period);wait=before['waiting_groups'][0]
    income(300)
    model=model.model_copy(update={'overheads':(Overhead(period=period,amount=20),)})
    actions.sync_month(ws,model,period)
    after=actions.month(ws,model,period)
    assert before['stores'][0]['input_sha']!=after['stores'][0]['input_sha']
    assert after['counts']['waiting']==1 and after['waiting_groups'][0]['since']==wait['since']
    assert after['waiting_groups'][0]['notification_key']==wait['notification_key']
    assert client.post('/api/v1/agent-jobs/claim',json={}).status_code==204
    assert ws.conn.execute('SELECT count(*) FROM automation_source_handoff').fetchone()[0]==1


def test_real_service_supplement_removes_current_gap_and_retires_wait(bundle,monkeypatch):
    from datetime import datetime,timedelta,timezone
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ledger import replay_service,automation_checker as checker
    from ledger.automation_cost_actions import RULE,RULE_SHA
    from ledger.worker_control_api import install
    from test_replay_service import seed
    path,missing=bundle
    monkeypatch.setenv('LEDGER_DETERMINISTIC_AUTOMATION_ENABLED','1')
    with replay_service.Session(path,seed()) as session:
        session.recompute('s1');session.recompute('s2');session.drain_pending()
        ws,model=session.ws,session.model;stamp=datetime.now(timezone.utc)
        human={'id':'finance','kind':'human','authenticated':True}
        preview=actions.decision_preview(ws,model,request_id='real-grant-p',period='2026-06',store_id='s1',kind='rule_grant',
            values={'rule_id':RULE,'rule_sha':RULE_SHA,'enabled':True,'valid_from':(stamp-timedelta(days=1)).isoformat(),
                'valid_until':(stamp+timedelta(days=1)).isoformat()},reason='固定源服务试验，仅批准原始凭据单组件规则',actor=human)
        actions.decision_apply(ws,model,request_id='real-grant-a',proposal_id=preview['proposal_id'],proposal_sha=preview['proposal_sha'],input_sha=preview['input_sha'],actor=human)
        app=FastAPI();install(app,lambda:ws,lambda:model,lambda req:{'id':'worker','kind':'service','authenticated':True,'scopes':['business']})
        client=TestClient(app)
        for _ in range(4):
            job=claim(client)
            scope=job['payload']['automation_case']
            if scope['store_id']=='s1' and scope['period']=='2026-06':break
        else:raise AssertionError('Approved current source case was not claimable')
        assert submit(client,job,[observation(ws,'original_evidence_missing','history_cost')]).status_code==200
        assert actions.month(ws,model,'2026-06')['counts']['waiting']==1
        session.supplement('s1','2026-06',amount='2.00',reason='按原始凭据审核当前唯一缺口',order_id=missing['s1/2026-06'])
        session.drain_pending()
        latest=ws.latest_run('s1','2026-06')
        manifest=json.loads(ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(latest['id'],)).fetchone()[0])
        assert manifest['collectable_scope']['history_cost']['required'] is False
        after=actions.month(ws,model,'2026-06')
        assert after['counts']['waiting']==0 and after['counts']['completed']==0
        assert ws.state('s1','2026-06').state=='open'
        checked=checker.check(ws,'s1','2026-06',model=model)
        assert not checked['allowed']
        assert 'company_revenue_unknown' in {issue['code'] for issue in checked['issues']}


# Reuse the actual XLSX/feed fixture without copying or faking its source engine.
from test_replay_service import bundle
