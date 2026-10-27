import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledger import automation_actions as actions,automation_checker as checker
from ledger.worker_control_api import install
from ledger.commission_registry import Registry
from ledger.workspace import Workspace
from ledger.model.schema import Overhead
from test_commission import _model


@pytest.fixture
def running(tmp_path):
    ws=Workspace(tmp_path/'ws');model=_model()
    model=model.model_copy(update={'statement':tuple(n.model_copy(update={'headline':'revenue'}) if n.id=='n_receipt' else n for n in model.statement),
                                  'overheads':(Overhead(period='2030-06',amount=0),)})
    checker.bind_model(ws,model)
    human={'id':'finance','kind':'human','authenticated':True}
    actions.advance(ws,model,'2030-06','advance',human)
    timer=[1000]
    service={'id':'worker','kind':'service','authenticated':True,'scopes':['business']}
    app=FastAPI();install(app,lambda:ws,lambda:model,lambda req:service,lease_seconds=30,clock=lambda:timer[0])
    yield ws,model,TestClient(app),timer,service
    ws.close()


def claim(client):
    result=client.post('/api/v1/agent-jobs/claim',json={'agent_id':'worker','capabilities':['recompute']})
    assert result.status_code==200,result.text
    return result.json()


def terminal(job):
    return {'lease_token':job['payload']['lease_token'],'input_sha':job['payload']['automation_case']['input_sha'],
            'result':{'narrative':'the model says everything is done'}}


def test_claim_is_scope_bound_and_not_a_financial_queue(running):
    ws,model,client,timer,service=running
    job=claim(client)
    assert job['kind']=='recompute' and job['payload']['automation_case']['store_id']=='s1'
    assert client.post('/api/v1/agent-jobs/claim',json={}).status_code==204
    with Registry(ws.root).connect() as conn:assert conn.execute('SELECT count(*) FROM pending').fetchone()[0]==1
    assert ws.conn.execute('SELECT count(*) FROM automation_attempt WHERE state=\'worker_running\'').fetchone()[0]==1


def test_model_success_is_rejected_without_independent_evidence(running):
    ws,model,client,timer,service=running;job=claim(client)
    result=client.post(f"/api/v1/agent-jobs/{job['id']}/complete",json=terminal(job))
    assert result.status_code==409
    assert ws.conn.execute('SELECT state FROM automation_attempt WHERE id=?',(job['id'],)).fetchone()[0]=='worker_running'
    assert not ws.conn.execute("SELECT 1 FROM period WHERE state='closed'").fetchone()


def test_progress_and_heartbeat_renew_only_matching_lease(running):
    ws,model,client,timer,service=running;job=claim(client)
    timer[0]+=20
    body={'agent_id':'worker','leases':[{'job_id':job['id'],'lease_token':job['payload']['lease_token']}]}
    assert client.post('/api/v1/agents/heartbeat',json=body).json()['renewed']==[job['id']]
    timer[0]+=20
    result=client.post(f"/api/v1/agent-jobs/{job['id']}/progress",json={'sequence':1,'stage':'materializing','message':'读取原始证据',
        'detail':{'lease_token':job['payload']['lease_token']}})
    assert result.status_code==200
    assert client.get('/api/automation/workers/status').json()['worker_alive']


def test_expired_claim_is_recovered_and_old_worker_is_fenced(running):
    ws,model,client,timer,service=running;first=claim(client)
    timer[0]+=31;second=claim(client)
    assert second['payload']['lease_token']>first['payload']['lease_token']
    assert client.post(f"/api/v1/agent-jobs/{first['id']}/fail",json=terminal(first)).status_code==409
    assert ws.conn.execute('SELECT state FROM automation_attempt WHERE id=?',(first['id'],)).fetchone()[0]=='interrupted'


def test_changed_input_rejects_terminal_even_before_expiration(running):
    ws,model,client,timer,service=running;job=claim(client)
    with ws.conn:ws.conn.execute("UPDATE automation_case SET input_sha='new-input'")
    assert client.post(f"/api/v1/agent-jobs/{job['id']}/complete",json=terminal(job)).status_code==409


def test_newly_ready_input_cannot_complete_an_older_claim(running):
    ws,model,client,timer,service=running;job=claim(client)
    ws.record('s1','2030-06',{'can_close':True,'findings':[],'missing_sources':[],
        'statement':[{'id':'n_receipt','value':100,'available':True}]},[])
    with Registry(ws.root).transaction() as conn:conn.execute('DELETE FROM pending')
    assert checker.check(ws,'s1','2030-06',model=model)['allowed']
    assert client.post(f"/api/v1/agent-jobs/{job['id']}/complete",json=terminal(job)).status_code==409
    assert client.post(f"/api/v1/agent-jobs/{job['id']}/fail",json=terminal(job)).status_code==409
    assert ws.conn.execute('SELECT state FROM automation_attempt WHERE id=?',(job['id'],)).fetchone()[0]=='worker_running'


def test_ready_work_is_left_for_human_month_close_instead_of_claimed_by_worker(running):
    ws,model,client,timer,service=running
    ws.record('s1','2030-06',{'can_close':True,'findings':[],'missing_sources':[],
        'statement':[{'id':'n_receipt','value':100,'available':True}]},[])
    with Registry(ws.root).transaction() as conn:conn.execute('DELETE FROM pending')
    verified=checker.check(ws,'s1','2030-06',model=model)
    assert verified['allowed']
    assert client.post('/api/v1/agent-jobs/claim',json={}).status_code==204
    assert ws.state('s1','2030-06').state=='open'
    assert ws.conn.execute('SELECT status FROM automation_case').fetchone()[0]=='needs_decision'


def test_human_or_wrong_scope_cannot_claim(running):
    ws,model,client,timer,service=running
    service.update(kind='human')
    assert client.post('/api/v1/agent-jobs/claim',json={}).status_code==403
    service.update(kind='service',scopes=['unrelated'])
    assert client.post('/api/v1/agent-jobs/claim',json={}).status_code==403
