"""Finite prefix and scoped source identity over actual temporary SQLite/HTTP."""
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ledger import feed_freshness as fresh,automation_actions as actions,automation_checker as checker
from ledger.order_feed import OrderFeed
from ledger.automation_api import install
from ledger.automation_period_proof import source_manifest
from ledger.version import assert_code_integrity
from test_automation import sample,HUMAN,PERIOD
from test_source_recovery import source_case


@pytest.fixture
def prefix(sample):
    ws,model=sample;feed=OrderFeed(ws.root,client=object())
    with feed._connect() as db:
        db.execute("UPDATE feed_state SET snapshot_id='snapshot',consumed_seq=100,source_latest_seq=100 WHERE id=1")
        for sid,shop in (('s1','77'),('s2','88')):db.execute('INSERT INTO feed_store VALUES(?,?,?,?)',(shop,sid,'confirmed','{}'))
    manifests={}
    for sid,shop in (('s1','77'),('s2','88')):
        record={**source_manifest(ws,sid),'source_code_sha':assert_code_integrity()['source_code_sha'],
                'feed_boundary':fresh.task_boundary(ws.root,sid,'snapshot',100,100,[shop])}
        rid=ws.latest_run(sid,PERIOD)['id']
        ws.conn.execute('INSERT INTO automation_run_proof VALUES(?,?,?,?,?,?)',(rid,'a'*64,'b'*64,'c'*64,json.dumps(record),'synthetic'))
        manifests[sid]=record
    ws.conn.commit()
    return ws,model,feed,manifests


def event(feed,shop,seq=101):
    with feed._connect() as db:
        db.execute('INSERT INTO feed_entity VALUES(?,?,?,?,?,?,?,?,?,?,?)',('order','changed',seq,1,'upsert',shop,'new-order',None,None,'{}',None))
        db.execute('UPDATE feed_state SET consumed_seq=?,source_latest_seq=? WHERE id=1',(seq,seq))
        if shop:db.execute('INSERT INTO feed_pending_store VALUES(?,?)',('s1' if shop=='77' else 's2',seq))


def test_other_store_and_future_global_tail_do_not_change_scoped_financial_cas(prefix):
    ws,model,feed,m=prefix;before=checker.check(ws,'s1',PERIOD,model=model)
    event(feed,'88')
    with feed._connect() as db:db.execute('UPDATE feed_state SET source_latest_seq=105 WHERE id=1')
    after=checker.check(ws,'s1',PERIOD,model=model)
    assert before['allowed'] and after['allowed'] and before['input_sha']==after['input_sha']
    proof=fresh.evaluate(ws,'s1',PERIOD,m['s1'])
    assert not proof['blocked'] and proof['proof']['future_tail_outside_boundary']
    assert checker.check(ws,'s2',PERIOD,model=model)['allowed'] is False


@pytest.mark.parametrize('shop',['77',None])
def test_consumed_own_or_unscoped_accounting_event_is_never_ignored(prefix,shop):
    ws,model,feed,m=prefix;before=checker.check(ws,'s1',PERIOD,model=model)
    event(feed,shop)
    after=checker.check(ws,'s1',PERIOD,model=model)
    assert not after['allowed'] and after['input_sha']!=before['input_sha']
    assert 'source_sync_pending' in {p['code'] for p in after['issues']}


@pytest.mark.parametrize('change',['snapshot','mapping','error','rewind','bad_proof'])
def test_proof_failures_remain_blocked(prefix,change):
    ws,model,feed,m=prefix;record=m['s1']
    with feed._connect() as db:
        if change=='snapshot':db.execute("UPDATE feed_state SET snapshot_id='new' WHERE id=1")
        elif change=='mapping':db.execute("UPDATE feed_store SET mapping_status='pending' WHERE ledger_store_id='s1'")
        elif change=='error':db.execute("UPDATE feed_state SET last_error='source unavailable' WHERE id=1")
        elif change=='rewind':db.execute('UPDATE feed_state SET consumed_seq=99 WHERE id=1')
        else:record={**record,'feed_boundary':{**record['feed_boundary'],'captured_through_seq':99}}
    assert fresh.evaluate(ws,'s1',PERIOD,record)['blocked']


def test_old_proof_retains_global_fallback(prefix):
    ws,model,feed,m=prefix
    with feed._connect() as db:db.execute('UPDATE feed_state SET source_latest_seq=101 WHERE id=1')
    result=fresh.evaluate(ws,'s1',PERIOD,{})
    assert result['mode']=='legacy_global' and result['blocked']


def test_http_close_retry_keeps_one_server_prefix_and_rejects_scope_rebinding(prefix):
    ws,model,feed,m=prefix;app=FastAPI();install(app,lambda:ws,lambda:model,actor=lambda request:HUMAN)
    client=TestClient(app);body={'request_id':'bounded-close','period':PERIOD,'store_ids':['s1']}
    with feed._connect() as db:db.execute('UPDATE feed_state SET source_latest_seq=101 WHERE id=1')
    response=client.post('/api/automation/close-batches/preview',json=body)
    assert response.status_code==200,response.text
    row=ws.conn.execute("SELECT result_json FROM automation_attempt WHERE state='close_feed_boundary'").fetchone()
    assert json.loads(row[0])['boundaries']['s1']['required_through_seq']==100
    assert client.post('/api/automation/close-batches/preview',json={**body,'store_ids':['s2']}).status_code==409
    proposal=response.json();assert len(proposal['eligible'])==1
    confirm=client.post('/api/automation/close-batches/confirm',json={'request_id':'bounded-confirm',
        **{k:proposal[k] for k in ('proposal_id','proposal_sha','input_sha')}})
    assert confirm.status_code==200,confirm.text
    assert confirm.json()['completed'],confirm.text
    assert confirm.json()['completed'][0]['store_id']=='s1'
    assert ws.state('s1',PERIOD).closed
    assert len(ws.conn.execute("SELECT * FROM automation_attempt WHERE state='close_feed_boundary'").fetchall())==1


def test_task_writer_has_explicit_finite_prefix_and_mapping(prefix):
    ws,model,feed,m=prefix
    with fresh.hold_task(ws.root,'s1','snapshot',100):
        boundary=fresh.task_boundary(ws.root,'s1','snapshot',100,110,['77'])
        assert boundary['required_through_seq']==100 and boundary['observed_head_seq']==110
        with pytest.raises(ValueError):fresh.task_boundary(ws.root,'s1','other-snapshot',100,110,['77'])
    assert fresh.task_boundary(ws.root,'s1','snapshot',100,110,['77'])['required_through_seq']==110


def test_unrelated_shared_original_does_not_retire_sealed_source_context(source_case):
    import io
    from ledger.automation_handoff import context_sha
    from test_jst_source_members import xlsx
    c=source_case;old=context_sha(c.ws,c.model,'s1',c.period,c.body['input_sha'])
    c.ws.keep('unrelated-company.xlsx',io.BytesIO(xlsx(['说明'],['公司非订单资料'])),'__shared__')
    assert context_sha(c.ws,c.model,'s1',c.period,c.body['input_sha'])==old
    c.ws.keep('synthetic-native.xlsx',io.BytesIO(xlsx(['主订单编号','子订单编号','订单创建时间'],['202600001','C1','2026-05-16'])),'s1')
    assert context_sha(c.ws,c.model,'s1',c.period,c.body['input_sha'])!=old


def test_close_after_known_own_change_is_rejected_without_freezing(prefix):
    ws,model,feed,m=prefix
    preview=actions.close_preview(ws,model,request_id='before-own-event',period=PERIOD,store_ids=['s1'],actor=HUMAN)
    event(feed,'77')
    with pytest.raises(actions.Conflict):actions.close_confirm(ws,model,request_id='after-own-event',
        proposal_id=preview['proposal_id'],proposal_sha=preview['proposal_sha'],input_sha=preview['input_sha'],actor=HUMAN)
    assert not ws.state('s1',PERIOD).closed


def test_service_identity_cannot_capture_human_close_boundary(prefix):
    ws,model,feed,m=prefix
    with pytest.raises(PermissionError):actions.close_preview(ws,model,request_id='service-not-human',period=PERIOD,
        store_ids=['s1'],actor={'id':'worker','kind':'service','authenticated':True,'scopes':['business']})
    assert not ws.conn.execute("SELECT 1 FROM automation_attempt WHERE state='close_feed_boundary'").fetchone()
