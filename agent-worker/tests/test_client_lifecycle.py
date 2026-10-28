"""Real SDK client ownership across the Worker's separate asyncio.run loops."""
import asyncio
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import threading
import time
from pathlib import Path

import httpx
import pytest
from ledger_agent.runtime import Runtime,Config,ModelSpec
from ledger_agent.worker import Worker,Control
from ledger_agent.state import State
from ledger_agent.commands import Catalog
from finance_agent.config import AgentConfig
from finance_agent.state import AgentState


@pytest.mark.parametrize('mode',['success','http_failure','timeout','agent_construction_failure'])
def test_owned_sdk_client_is_closed_before_worker_starts_the_next_loop(tmp_path,monkeypatch,mode):
    from agentscope.model import OpenAIChatModel
    import agentscope.model as models
    import agentscope.agent as agents
    closed=[];instances=[]
    class Tracked(OpenAIChatModel):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs);instances.append(self)
            made_in=asyncio.get_running_loop()
            actual=self.client.close
            async def close():
                loop=asyncio.get_running_loop();assert not loop.is_closed()
                assert loop is made_in
                await actual();closed.append((loop,self.client.is_closed()))
            self.client.close=close
    monkeypatch.setattr(models,'OpenAIChatModel',Tracked)
    if mode=='agent_construction_failure':
        class Broken:
            def __init__(self,*args,**kwargs):raise RuntimeError('synthetic agent construction failure')
        monkeypatch.setattr(agents,'Agent',Broken)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            if mode=='timeout':time.sleep(.08)
            status=500 if mode=='http_failure' else 200
            payload=({'error':{'message':'synthetic model failure','type':'server_error'}} if status==500 else
                {'id':'loopback-lifecycle','object':'chat.completion','created':0,'model':'fixture',
                 'choices':[{'index':0,'message':{'role':'assistant','content':'来源仍待独立复核。'},'finish_reason':'stop'}],
                 'usage':{'prompt_tokens':8,'completion_tokens':8,'total_tokens':16}})
            raw=json.dumps(payload).encode();self.send_response(status);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)));self.end_headers()
            try:self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    jobs=[0]
    def wire(request):
        path=request.url.path
        if path.endswith('/heartbeat'):return httpx.Response(200,json={'rejected':[]})
        if path.endswith('/claim'):
            jobs[0]+=1;number=jobs[0]
            return httpx.Response(200,json={'id':'job-'+str(number),'kind':'recompute','idempotency_key':'key-'+str(number),
                'payload':{'lease_token':number,'automation_case':{'id':'case','store_id':'s1','period':'2030-06','input_sha':'a'*64}}})
        if path.startswith('/api/automation/months/'):
            return httpx.Response(200,json={'stores':[{'store_id':'s1','input_sha':'a'*64,
                'status':'processing','stage':'gathering','verification':{'allowed':False}}]})
        body=json.loads(request.content) if request.content else {}
        if path.endswith('/fail') and body.get('tool_outcomes'):
            return httpx.Response(200,json={'ok':True,'job':{'state':'waiting_maintenance','waiting':{'items':body['tool_outcomes']}}})
        assert not path.endswith('/complete'),'No source result can create a financial completion'
        return httpx.Response(200,json={'ok':True})
    state=AgentState(tmp_path/'protocol');state.set_secret('agent_id','worker');state.set_secret('access_token','unit-key')
    control=Control(AgentConfig(control_plane_url='http://ledger',state_dir=tmp_path),state,transport=httpx.MockTransport(wire))
    config=Config('http://127.0.0.1:'+str(server.server_port)+'/v1','unit-lifecycle-key',(ModelSpec('fixture'),),
                  evaluation=True,timeout_seconds=.01 if mode=='timeout' else 5)
    running=Worker(Runtime(config,State(tmp_path/'audit')),control,Catalog({},tmp_path/'tools'))
    try:
        for _ in range(2):
            value=running.once();assert value['state']!='verified'
        assert len(instances)==2 and len(closed)==2
        assert all(value for _,value in closed)
        assert closed[0][0] is not closed[1][0], 'The actual Worker starts separate loops; clients must close in their owner loops'
    finally:
        control.close();state.connection.close();server.shutdown();server.server_close();thread.join(timeout=2)


def test_ops_phase_closes_its_own_public_client_in_each_owner_loop(tmp_path,monkeypatch):
    from agentscope.model import OpenAIChatModel
    import agentscope.model as models
    from ledger_agent.ops_runtime import _model_phase
    closed=[]
    class Tracked(OpenAIChatModel):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs);actual=self.client.close
            made_in=asyncio.get_running_loop()
            async def close():
                loop=asyncio.get_running_loop();assert not loop.is_closed()
                assert loop is made_in
                await actual();closed.append((loop,self.client.is_closed()))
            self.client.close=close
    monkeypatch.setattr(models,'OpenAIChatModel',Tracked)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            body={'id':'loopback-ops-close','object':'chat.completion','created':0,'model':'fixture',
                  'choices':[{'index':0,'message':{'role':'assistant','content':'没有提出发布。'},'finish_reason':'stop'}],
                  'usage':{'prompt_tokens':8,'completion_tokens':8,'total_tokens':16}}
            raw=json.dumps(body).encode();self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    (tmp_path/'event.json').write_text('{"incident_ref":"synthetic-lifecycle-only"}')
    class Repair:
        config={'checks':[]}
        def path(self,identifier):return tmp_path
        def model_tools(self,identifier):return {}
    config=Config('http://127.0.0.1:'+str(server.server_port)+'/v1','unit-ops-key',
                  (ModelSpec('fixture',skills=('ops',)),),evaluation=True,max_iters=1)
    try:
        for _ in range(2):
            asyncio.run(_model_phase(Repair(),'synthetic',config,State(tmp_path/'audit-ops'),config.models[0],None))
        assert len(closed)==2 and all(value for _,value in closed)
        assert closed[0][0] is not closed[1][0]
    finally:server.shutdown();server.server_close();thread.join(timeout=2)
