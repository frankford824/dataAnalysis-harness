"""Actual AgentScope SDK + loopback provider wire; not a live-model benchmark."""
import asyncio
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ledger_agent.runtime import Config,ModelSpec,Runtime
from ledger_agent.state import State
from ledger_agent.privacy import check_outbound,render


@pytest.fixture
def provider():
    class Requests(list):
        final_text='已取得证据，由系统复核。'
    requests = Requests()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(payload)
            if any(message['role']=='tool' for message in payload['messages']):
                message = {'role':'assistant','content':requests.final_text}
                finish = 'stop'
            else:
                message = {'role':'assistant','content':None,'tool_calls':[{'id':'tool_call_a','type':'function',
                    'function':{'name':'evidence_operation','arguments':json.dumps({'operation':'collect'})}}]}
                finish = 'tool_calls'
            response = {'id':'fixture_call','object':'chat.completion','created':0,'model':'fixture',
                'choices':[{'index':0,'message':message,'finish_reason':finish}],
                'usage':{'prompt_tokens':40,'completion_tokens':20,'total_tokens':60}}
            raw = json.dumps(response).encode()
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield 'http://127.0.0.1:'+str(server.server_port)+'/v1',requests
    server.shutdown();server.server_close();thread.join(timeout=3)


def config(url):
    return Config(url,'synthetic-key',(ModelSpec('fixture'),),timeout_seconds=10,evaluation=True)


def test_real_sdk_executes_tool_and_only_checker_proves_completion(tmp_path,provider):
    url,requests = provider
    state = State(tmp_path)
    done = []
    case = {'id':'synthetic','input_sha':'a'*64,'reason_codes':['missing_order'],'skills':['evidence']}
    def collect(case,evidence):
        done.append(True)
        return {'order_id':'3300000000000000000','phone':'13800138000','amount':123.45}
    result = asyncio.run(Runtime(config(url),state).run(case,{'collect':collect},lambda:{'verified':bool(done)}))
    assert result['state']=='verified' and done==[True]
    assert len(requests)==2
    assert requests[0]['tool_choice']=='required'
    wire = json.dumps(requests)
    assert '3300000000000000000' not in wire and '13800138000' not in wire and '123.45' not in wire
    assert 'ev_' in wire
    assert state.summary()['input_tokens']==80
    assert state.summary()['supplier_settled']=={}
    assert state.summary()['price_unknown_calls']==2


def test_successful_model_prose_cannot_override_failed_checker(tmp_path,provider):
    url,_ = provider
    case = {'id':'synthetic','input_sha':'a'*64}
    result = asyncio.run(Runtime(config(url),State(tmp_path)).run(case,{'collect':lambda *a:{'status':'found'}},
                                                         lambda:{'verified':False}))
    assert result['state']=='waiting_evidence'


def test_disabled_model_still_allows_deterministic_completion(tmp_path):
    runtime = Runtime(Config('',''),State(tmp_path))
    assert asyncio.run(runtime.run({'id':'c'},{},lambda:{'verified':True}))['state']=='verified'
    assert asyncio.run(runtime.run({'id':'c'},{},lambda:{'verified':False}))['state']=='waiting_configuration'


def test_unsupported_model_numbers_are_discarded_not_displayed(tmp_path,provider):
    url,requests=provider
    requests.final_text='差额123.45元，已经修复。'
    done=[]
    result=asyncio.run(Runtime(config(url),State(tmp_path)).run({'id':'c','input_sha':'a'*64},
        {'collect':lambda *args:done.append(True) or {'status':'found'}},lambda:{'verified':bool(done)}))
    assert result['state']=='verified' and result['prose_guard']=='rejected_and_replaced'
    assert '123.45' not in result['explanation']


def test_unvalidated_model_is_not_silently_production_enabled(tmp_path,provider):
    url,requests = provider
    result = asyncio.run(Runtime(Config(url,'synthetic',(ModelSpec('fixture'),)),State(tmp_path))
        .run({'id':'c'},{},lambda:{'verified':False}))
    assert result['reason']=='model_not_validated' and not requests


def test_citations_are_exact_and_version_bound(tmp_path):
    state=State(tmp_path)
    ref=state.evidence({'amount':12.34},'input-a')
    assert render('金额{{evidence:'+ref+':amount}}',state,'input-a')=='金额12.34'
    for text,version in [('金额99元','input-a'),('金额{{evidence:'+ref+':missing}}','input-a'),
                         ('金额{{evidence:'+ref+':amount}}','input-b')]:
        with pytest.raises(ValueError):render(text,state,version)
    with pytest.raises(ValueError):check_outbound({'raw_order':'3300000000000000000'})


def test_unknown_usage_and_price_are_distinct_from_supplier_settlement(tmp_path):
    state=State(tmp_path)
    first=state.usage('case','fixture',None,'request-sha',status='failed')
    second=state.usage('case','fixture',SimpleNamespace(input_tokens=100,output_tokens=50,cache_input_tokens=0),
                       'request-sha',{'input_per_million':1,'output_per_million':2,'version':'price-v1','currency':'CNY'})
    state.settle(second,'0.02','CNY','supplier-line-1')
    result=state.summary()
    assert result['usage_unknown_calls']==1 and result['price_unknown_calls']==1
    assert result['estimated_cost']=='0.0002' and result['supplier_settled']=={'CNY':'0.02'}
    assert result['unsettled_calls']==1
    with pytest.raises(Exception):state.settle(first,'0.1','CNY','supplier-line-1')
