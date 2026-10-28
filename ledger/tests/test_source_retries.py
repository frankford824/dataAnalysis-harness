"""Server-bounded technical recovery; model output never schedules a retry."""
from pathlib import Path
import json,hashlib
import pytest
from ledger import source_recovery as source,automation_store as journal
from test_source_recovery import source_case,request_task,source_claim
from test_source_recovery_current import raw_receipt


def failed(c,task,job,*,reason='source_transport_unavailable',status=None):
    body=raw_receipt(c,task,job)
    folder=c.ws.root/'source-recovery'/task['task_id']/job['id']
    data=json.loads((folder/'receipt.json').read_bytes())
    responses=[]
    if status is not None:
        raw=b'declared synthetic HTTP response';sha=hashlib.sha256(raw).hexdigest();(folder/(sha+'.raw')).write_bytes(raw)
        responses=[{'status':status,'sha256':sha}]
    data.update(state='pending',reason=reason,nodes=[],relations=[],responses=responses)
    body['receipt_sha256']=source.durable(folder/'receipt.json',data)
    response=c.client.post('/api/automation/source-recovery/requests/'+task['task_id']+'/finish',json=body)
    assert response.status_code==200,response.text
    return body,response.json()['task']


def beat(c):
    response=c.client.post('/api/v1/agents/heartbeat',json={})
    assert response.status_code==200,response.text
    return response.json()


def test_due_retry_keeps_same_task_fences_old_lease_and_repeated_heartbeats_are_idempotent(source_case):
    c=source_case;task=request_task(c);old=source_claim(c);body,pending=failed(c,task,old)
    assert pending['retry']['next_attempt_at']==c.clock[0]+60
    c.clock[0]+=59;beat(c)
    assert source.status(c.ws,task['task_id'])['state']=='source_pending'
    c.clock[0]+=1;beat(c);beat(c)
    assert source.status(c.ws,task['task_id'])['state']=='source_queued'
    assert c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='source_retry_scheduled'").fetchone()[0]==1
    job=source_claim(c)
    assert job['id']!=old['id'] and job['payload']['lease_token']>old['payload']['lease_token']
    bad={**body,'receipt_sha256':'c'*64}
    response=c.client.post('/api/automation/source-recovery/requests/'+task['task_id']+'/finish',json=bad)
    assert response.status_code==409
    assert len(json.loads(c.ws.conn.execute('SELECT result_json FROM automation_attempt WHERE id=?',(task['task_id'],)).fetchone()[0])['prior_source_receipts'])==1


def test_four_retries_then_truthful_maintenance_no_infinite_get(source_case):
    c=source_case;task=request_task(c);job=source_claim(c)
    for scheduled,delay in enumerate(source.RETRY_DELAYS):
        _,pending=failed(c,task,job)
        assert pending['retry']['scheduled_count']==scheduled
        assert pending['retry']['next_attempt_at']==c.clock[0]+delay
        c.clock[0]+=delay;beat(c);job=source_claim(c)
    _,terminal=failed(c,task,job)
    assert terminal['retry']['state']=='exhausted' and terminal['retry']['next_attempt_at'] is None
    assert terminal['retry']['scheduled_count']==4
    c.clock[0]+=100000;beat(c)
    assert source.status(c.ws,task['task_id'])['state']=='source_pending'
    assert c.ws.conn.execute('SELECT stage FROM automation_case').fetchone()[0]=='waiting_maintenance'
    assert c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='source_retry_scheduled'").fetchone()[0]==4


@pytest.mark.parametrize('reason,status',[('source_byte_budget',None),('typed_source_relation_unproved',None),
    ('original_not_found_in_this_scope',None),('source_session_refresh_required',None),
    ('source_session_or_http_unavailable',200),('source_session_or_http_unavailable',401)])
def test_semantics_missing_original_or_login_are_never_retried_by_time(source_case,reason,status):
    c=source_case;task=request_task(c);job=source_claim(c);_,pending=failed(c,task,job,reason=reason,status=status)
    assert pending['retry']['state']=='not_retryable'
    c.clock[0]+=1000000;beat(c)
    assert source.status(c.ws,task['task_id'])['state']=='source_pending'
    assert not c.ws.conn.execute("SELECT 1 FROM automation_attempt WHERE state='source_retry_scheduled'").fetchone()


@pytest.mark.parametrize('status',[408,429,500,502,503,504])
def test_only_transient_http_statuses_have_timer_policy(source_case,status):
    c=source_case;task=request_task(c);job=source_claim(c)
    _,pending=failed(c,task,job,reason='source_session_or_http_unavailable',status=status)
    assert pending['retry']['eligible'] and pending['retry']['next_attempt_at']==c.clock[0]+60


def test_daily_cap_defers_without_resetting_attempt_history(source_case,monkeypatch):
    c=source_case;monkeypatch.setattr(source,'RETRY_DAILY_LIMIT',1)
    task=request_task(c);job=source_claim(c);failed(c,task,job)
    c.clock[0]+=60;beat(c);job=source_claim(c);failed(c,task,job)
    c.clock[0]+=300;beat(c)
    value=source.status(c.ws,task['task_id'])
    assert value['state']=='source_pending' and value['retry']['state']=='daily_limit'
    assert value['retry']['scheduled_count']==1 and value['retry']['next_attempt_at']==86400
    beat(c);assert c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='source_retry_scheduled'").fetchone()[0]==1
    c.clock[0]=86400;beat(c)
    assert source.status(c.ws,task['task_id'])['state']=='source_queued'
    assert c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='source_retry_scheduled'").fetchone()[0]==2


@pytest.mark.parametrize('change',['input','closed','excluded','scope'])
def test_retry_does_not_restore_stale_frozen_or_outside_scope_case(source_case,monkeypatch,change):
    c=source_case;task=request_task(c);job=source_claim(c);failed(c,task,job)
    with c.ws.conn:
        if change=='input':c.ws.conn.execute("UPDATE automation_case SET input_sha='changed'")
        elif change=='closed':c.ws.conn.execute("UPDATE period SET state='closed'")
        elif change=='excluded':c.ws.conn.execute("UPDATE automation_case SET stage='excluded'")
    if change=='scope':monkeypatch.setenv('LEDGER_SOURCE_RECOVERY_PERIODS','2026-09')
    c.clock[0]+=60;beat(c)
    assert source.status(c.ws,task['task_id'])['state']=='source_pending'
    assert not c.ws.conn.execute("SELECT 1 FROM automation_attempt WHERE state='source_retry_scheduled'").fetchone()
