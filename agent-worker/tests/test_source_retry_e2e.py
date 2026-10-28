"""Actual fixed Node/HTTP child with a fake server clock; no model calls."""
from pathlib import Path
import json
from test_source_recovery_e2e import source_case,request_task,fixture_executor,worker


def test_503_recovery_without_configuration_epoch_retries_at_deadline_only(source_case,monkeypatch):
    c=source_case;task=request_task(c);executor=fixture_executor(c,monkeypatch)
    directory=Path(executor.config.jst_module).parent
    session=directory/'session.json';session.write_text(json.dumps({'synthetic_cookie':'unchanged'}))
    availability=directory/'transport-availability.json';availability.write_text(json.dumps({'available':False}))
    preload=directory/'transport.mjs';text=preload.read_text();needle='globalThis.fetch=async input=>{'
    assert needle in text
    text=text.replace(needle,needle+"if(!JSON.parse(fs.readFileSync(new URL('./transport-availability.json',import.meta.url),'utf8')).available)return new Response('upstream unavailable',{status:503});")
    preload.write_text(text);running=worker(c,executor);epoch=executor.configuration_sha()
    financial_input=c.ws.conn.execute('SELECT input_sha FROM automation_case').fetchone()[0]
    first=running.once()
    assert first['state']=='source_pending' and first['reason'] in {'source_session_or_http_unavailable','source_transport_unavailable'}
    pending=json.loads(c.ws.conn.execute('SELECT result_json FROM automation_attempt WHERE id=?',(task['task_id'],)).fetchone()[0])
    assert pending['automatic_retry']['next_attempt_at']==c.clock[0]+60
    original=json.loads((c.ws.root/'source-recovery'/task['task_id']/pending['source_attempt_id']/'receipt.json').read_text())
    assert original['responses'][-1]['status']==503
    old_job=pending['source_attempt_id'];old_count=c.ws.conn.execute('SELECT count(*) FROM automation_attempt').fetchone()[0]
    availability.write_text(json.dumps({'available':True}))
    assert executor.configuration_sha()==epoch
    c.clock[0]+=59
    assert running.once() is None
    assert c.ws.conn.execute('SELECT count(*) FROM automation_attempt').fetchone()[0]==old_count
    c.clock[0]+=1
    running.control.heartbeat();running.control.heartbeat()
    assert c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='source_retry_scheduled'").fetchone()[0]==1
    second=running.once()
    assert second['state']=='source_raw_ready' and second['task_id']==task['task_id']
    assert second['model_used'] is False and second['financial_applied'] is False and second['business_complete'] is False
    recovered=json.loads(c.ws.conn.execute('SELECT result_json FROM automation_attempt WHERE id=?',(task['task_id'],)).fetchone()[0])
    assert recovered['automatic_retry']['state']=='recovered' and recovered['automatic_retry']['scheduled_count']==1
    assert recovered['source_attempt_id']!=old_job and len(recovered['prior_source_receipts'])==1
    assert c.ws.conn.execute('SELECT input_sha FROM automation_case').fetchone()[0]==financial_input
    assert not c.ws.conn.execute('SELECT 1 FROM cost_line_log').fetchone()
    assert not c.ws.conn.execute("SELECT 1 FROM period WHERE state='closed'").fetchone()


def test_real_login_page_waits_for_session_epoch_and_then_recovers_immediately(source_case,monkeypatch):
    c=source_case;task=request_task(c);executor=fixture_executor(c,monkeypatch)
    directory=Path(executor.config.jst_module).parent;session=directory/'session.json'
    session.write_text(json.dumps({'synthetic_logged_in':False}))
    preload=directory/'transport.mjs';text=preload.read_text();needle='globalThis.fetch=async input=>{'
    assert needle in text
    preload.write_text(text.replace(needle,needle+"if(!JSON.parse(fs.readFileSync(new URL('./session.json',import.meta.url),'utf8')).synthetic_logged_in)return new Response('<html>erp-login</html>',{status:200});"))
    running=worker(c,executor);first=running.once()
    assert first['state']=='source_pending'
    pending=json.loads(c.ws.conn.execute('SELECT result_json FROM automation_attempt WHERE id=?',(task['task_id'],)).fetchone()[0])
    assert pending['automatic_retry']['failure_kind']=='login_required' and not pending['automatic_retry']['eligible']
    assert c.ws.conn.execute('SELECT reason_code FROM automation_source_handoff').fetchone()[0]=='login_required'
    c.clock[0]+=10000
    assert running.once() is None
    assert not c.ws.conn.execute("SELECT 1 FROM automation_attempt WHERE state='source_retry_scheduled'").fetchone()
    session.write_text(json.dumps({'synthetic_logged_in':True}))
    second=running.once()
    assert second['state']=='source_raw_ready' and second['task_id']==task['task_id']
    assert second['model_used'] is False and second['financial_applied'] is False
