"""Actual loopback HTTP proves event presence never claims a human-time baseline."""
import json
from pathlib import Path
import socket
import threading
import time
from uuid import uuid4

import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace
from fastapi import FastAPI
import pytest
import uvicorn

from ledger import commission_api,telemetry_api
from ledger.commission_registry import Registry
from ledger.model.loader import load_model
from ledger.workspace import Workspace


class HTTPClient:
    def __init__(self,base):
        self.base=base;self.jar=http.cookiejar.CookieJar()
        self.opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
    def request(self,method,path,body=None):
        data=json.dumps(body).encode() if body is not None else None
        request=urllib.request.Request(self.base+path,method=method,data=data,headers={'Content-Type':'application/json'})
        try:response=self.opener.open(request,timeout=10)
        except urllib.error.HTTPError as error:response=error
        with response:raw=response.read();status=response.status
        return SimpleNamespace(status_code=status,json=lambda:json.loads(raw),cookies={cookie.name:cookie.value for cookie in self.jar})
    def post(self,path,*,json):return self.request('POST',path,json)
    def get(self,path,*,params):return self.request('GET',path+'?'+urllib.parse.urlencode(params))


@pytest.fixture
def live_telemetry(tmp_path):
    ws=Workspace(tmp_path/'workspace');registry=Registry(ws.root)
    registry.operator_add('telemetry-synthetic','synthetic-telemetry-password')
    model=load_model(Path(__file__).resolve().parents[2]/'models/cn-ecommerce')
    app=FastAPI();commission_api.install(app,lambda:ws,lambda:model)
    def actor(request):
        found=registry.actor(request.cookies.get('commission_session',''))
        return {**found,'kind':'human','authenticated':True} if found else None
    telemetry_api.install(app,lambda:ws,actor)
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen()
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
    thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
    try:
        deadline=time.monotonic()+10
        while not server.started and time.monotonic()<deadline:time.sleep(.01)
        assert server.started
        client=HTTPClient('http://127.0.0.1:'+str(listener.getsockname()[1]))
        response=client.post('/api/commission-v2/session',json={'name':'telemetry-synthetic','password':'synthetic-telemetry-password'})
        assert response.status_code==200 and response.cookies.get('commission_session')
        yield client,ws
    finally:
        server.should_exit=True;thread.join(10);assert not thread.is_alive();ws.close()


def payload():
    return {'client_id':str(uuid4()),'events':[{'event_id':str(uuid4()),'kind':'manual_amount_edit',
        'at':'2026-10-27T12:00:00Z','view':'labor','store_id':'synthetic-s1','period':'2026-07',
        'action':'manual_amount_edit','duration_ms':123,'outcome':'observed'}]}


def assert_no_baseline(value):
    assert value['baseline_measured'] is False
    assert value['actual_human_time_baseline_measured'] is False
    assert value['actual_reduction_percent'] is None


def test_real_http_events_do_not_claim_human_baseline_or_reduction(live_telemetry):
    client,ws=live_telemetry
    empty=client.get('/api/automation/telemetry/summary',params={'period':'2026-07'})
    assert empty.status_code==200
    assert_no_baseline(empty.json());assert empty.json()['events_present'] is False
    assert not ws.conn.execute("SELECT 1 FROM sqlite_master WHERE name='workload_event'").fetchone()
    body=payload()
    for _ in range(2):assert client.post('/api/automation/telemetry',json=body).status_code==200
    measured=client.get('/api/automation/telemetry/summary',params={'period':'2026-07','client_id':body['client_id']})
    assert measured.status_code==200
    value=measured.json();assert_no_baseline(value);assert value['events_present'] is True
    assert value['events']==[{'kind':'manual_amount_edit','count':1,'duration_ms':123}]
    row=ws.conn.execute('SELECT actor_sha,payload FROM workload_event').fetchone()
    assert len(row['actor_sha'])==64 and row['actor_sha']!='telemetry-synthetic'
    stored=json.loads(row['payload']);assert 'amount' not in stored and 'name' not in stored and 'reason' not in stored


@pytest.mark.parametrize('field,value',[('amount','123.45'),('name','Synthetic private person'),('reason','Synthetic private free note')])
def test_real_http_private_fields_rejected_without_storage(live_telemetry,field,value):
    client,ws=live_telemetry;body=payload();body['events'][0][field]=value
    response=client.post('/api/automation/telemetry',json=body)
    assert response.status_code==422
    assert not ws.conn.execute("SELECT 1 FROM sqlite_master WHERE name='workload_event'").fetchone()
    summary=client.get('/api/automation/telemetry/summary',params={'period':'2026-07'}).json()
    assert_no_baseline(summary);assert summary['events_present'] is False
