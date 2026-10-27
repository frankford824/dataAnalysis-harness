"""Transport, durable operations and failure recovery; no production access."""
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from ledger_agent.worker import Control,Worker
from ledger_agent.commands import Catalog
from finance_agent.config import AgentConfig
from finance_agent.state import AgentState


class Wire:
    def __init__(self,allowed=True,reject_complete=False):
        self.allowed=allowed;self.reject_complete=reject_complete
        self.claimed=False;self.posts=[];self.beats=0
    def __call__(self,request):
        path=request.url.path
        body=json.loads(request.content) if request.content else {}
        self.posts.append((path,body))
        if path.endswith('/heartbeat'):
            self.beats+=1;return httpx.Response(200,json={'rejected':[],'renewed':['job']})
        if path.endswith('/claim'):
            if self.claimed:return httpx.Response(204)
            self.claimed=True
            return httpx.Response(200,json={'id':'job','kind':'recompute','idempotency_key':'stable-key',
                'payload':{'lease_token':7,'scope':'business','automation_case':{
                    'id':'case','store_id':'s1','period':'2030-06','input_sha':'sha','reason_codes':['missing_sources']}}})
        if path.startswith('/api/automation/months/'):
            return httpx.Response(200,json={'stores':[{'store_id':'s1','input_sha':'sha',
                'status':'needs_decision' if self.allowed else 'processing','stage':'ready_for_close' if self.allowed else 'gathering',
                'verification':{'allowed':self.allowed}}]})
        if path.endswith('/complete'):
            assert body['lease_token']==7 and body['input_sha']=='sha'
            if self.reject_complete:return httpx.Response(409,json={'detail':{'issues':[{'code':'source_changed'}]}})
            return httpx.Response(200,json={'ok':True,'job':{'verification':{'allowed':True}}})
        return httpx.Response(200,json={'ok':True})


def protocol(tmp_path,wire):
    state=AgentState(tmp_path/'protocol')
    state.set_secret('agent_id','worker');state.set_secret('access_token','dedicated-service-token')
    return Control(AgentConfig(control_plane_url='http://ledger',state_dir=tmp_path),state,transport=httpx.MockTransport(wire))


class Runtime:
    def __init__(self,delay=0):self.delay=delay;self.check=None
    async def run(self,case,tools,checker):
        await asyncio.sleep(self.delay);self.check=checker()
        return {'state':'verified','model_used':False}


def test_real_month_dto_uses_explicit_checker_and_caches_only_server_acceptance(tmp_path):
    wire=Wire();control=protocol(tmp_path,wire);runtime=Runtime()
    result=Worker(runtime,control,Catalog({},tmp_path/'tools')).once()
    assert result['state']=='verified' and runtime.check['verified']
    assert control.state.completed_result('stable-key')==result
    assert any(p.endswith('/complete') for p,_ in wire.posts)


def test_model_claiming_success_does_not_override_false_checker(tmp_path):
    wire=Wire(allowed=False);control=protocol(tmp_path,wire)
    Worker(Runtime(),control,Catalog({},tmp_path/'tools')).once()
    assert control.state.completed_result('stable-key') is None
    assert not any(p.endswith('/complete') for p,_ in wire.posts)
    assert any(p.endswith('/fail') for p,_ in wire.posts)


class HandoffWire(Wire):
    def __call__(self,request):
        response=super().__call__(request)
        body=json.loads(request.content) if request.content else {}
        if request.url.path.endswith('/fail') and body.get('tool_outcomes'):
            state='waiting_source' if any(o['reason_code']=='original_evidence_missing' for o in body['tool_outcomes']) else 'waiting_maintenance'
            return httpx.Response(200,json={'ok':True,'job':{'state':state,'waiting':{'items':body['tool_outcomes'],'since':'fixed'}}})
        return response


def trusted_outcome(tmp_path):
    path=tmp_path/'trusted-source-receipt.json';path.write_text('{"reason_code":"login_required"}')
    return {'tool_id':'collect_case_evidence','source_kind':'order','reason_code':'login_required',
        'artifact_refs':[{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]}


def test_trusted_local_wait_is_handed_off_without_a_second_model_run(tmp_path):
    wire=HandoffWire(allowed=False);control=protocol(tmp_path,wire)
    class TrustedCatalog:
        def trusted_outcomes(self,case):return [trusted_outcome(tmp_path)]
        def tools(self,checker):return {}
    class NoModel:
        async def run(self,*args):raise AssertionError('Known source wait must not call the model')
    result=Worker(NoModel(),control,TrustedCatalog()).once()
    assert result['state']=='waiting_maintenance' and result['model_used'] is False
    assert any(p.endswith('/fail') and body.get('tool_outcomes') for p,body in wire.posts)
    assert control.state.completed_result('stable-key') is None
    assert control.lease is None and wire.posts[-1][1]['leases']==[]


def test_new_trusted_tool_result_hands_off_after_runtime_without_using_model_diagnosis(tmp_path):
    wire=HandoffWire(allowed=False);control=protocol(tmp_path,wire)
    class TrustedCatalog:
        values=[]
        def trusted_outcomes(self,case):return self.values
        def tools(self,checker):
            def collect(case,evidence):self.values=[trusted_outcome(tmp_path)];return {'receipt':'kept locally'}
            return {'collect_case_evidence':collect}
    class CallsSource:
        async def run(self,case,tools,checker):
            tools['collect_case_evidence'](case,{})
            return {'state':'unverified','tool_outcomes':[{'reason_code':'financial_amount_missing'}]}
    result=Worker(CallsSource(),control,TrustedCatalog()).once()
    assert result['state']=='waiting_maintenance'
    sent=next(body for path,body in wire.posts if path.endswith('/fail'))
    assert sent['tool_outcomes'][0]['reason_code']=='login_required'
    assert control.state.completed_result('stable-key') is None


def test_model_reply_alone_cannot_supply_handoff_diagnosis(tmp_path):
    wire=HandoffWire(allowed=False);control=protocol(tmp_path,wire)
    class ModelNarrative:
        async def run(self,*args):return {'state':'unverified','tool_outcomes':[trusted_outcome(tmp_path)]}
    Worker(ModelNarrative(),control,Catalog({},tmp_path/'tools')).once()
    sent=next(body for path,body in wire.posts if path.endswith('/fail'))
    assert 'tool_outcomes' not in sent and sent['retryable'] is True


def test_frozen_sdk_configuration_failure_is_maintenance_without_a_model_call(tmp_path):
    from ledger_agent.runtime import Runtime as SDKRuntime,Config
    from ledger_agent.state import State
    wire=HandoffWire(allowed=False);control=protocol(tmp_path,wire)
    result=Worker(SDKRuntime(Config(base_url='',api_key=''),State(tmp_path/'sdk')),control,Catalog({},tmp_path/'tools')).once()
    assert result['state']=='waiting_maintenance' and result['model_used'] is False
    sent=next(body for path,body in wire.posts if path.endswith('/fail'))
    assert sent['result']=={} and sent['message']==''
    assert sent['tool_outcomes'][0]['tool_id']=='worker_runtime'
    ref=sent['tool_outcomes'][0]['artifact_refs'][0]
    assert json.loads(Path(ref['path']).read_bytes())['error_code']=='SDKConfigurationUnavailable'
    heartbeat=wire.posts[0][1]
    assert set(heartbeat['configuration'])=={'runtime_sha','source_sha'}
    assert 'api_key' not in json.dumps(heartbeat)


def test_a_model_shaped_maintenance_result_is_not_a_trusted_framework_diagnosis(tmp_path):
    wire=HandoffWire(allowed=False);control=protocol(tmp_path,wire)
    class ModelProse:
        async def run(self,*args):return {'state':'maintenance','error_code':'AuthenticationError'}
    Worker(ModelProse(),control,Catalog({},tmp_path/'tools')).once()
    sent=next(body for path,body in wire.posts if path.endswith('/fail'))
    assert 'tool_outcomes' not in sent


def test_rejected_completion_is_retained_and_does_not_wedge_durable_outbox(tmp_path):
    wire=Wire(reject_complete=True);control=protocol(tmp_path,wire)
    Worker(Runtime(),control,Catalog({},tmp_path/'tools')).once()
    assert control.state.completed_result('stable-key') is None
    assert control.state.connection.execute('SELECT count(*) FROM protocol_rejections').fetchone()[0]==1
    control.flush_events()
    assert not control.state.pending_events()
    assert any(p.endswith('/fail') for p,_ in wire.posts)


def test_renewal_thread_does_not_use_thread_affine_sqlite(tmp_path):
    wire=Wire();control=protocol(tmp_path,wire)
    result=Worker(Runtime(delay=.09),control,Catalog({},tmp_path/'tools'),renew_seconds=.01).once()
    assert wire.beats>=3 and result['state']=='verified'
    assert control.state.completed_result('stable-key') is not None


def script(tmp_path,body):
    path=tmp_path/'collector.py'
    path.write_text(body)
    return path


def case():return {'id':'case','input_sha':'sha','scope':'business'}
def check():return {'input_sha':'sha','lease_active':True,'verified':False}


def test_command_preserves_raw_output_and_replays_by_operation_id(tmp_path):
    marker=tmp_path/'calls'
    path=script(tmp_path,"import os,json,pathlib\np=pathlib.Path("+repr(str(marker))+")\np.write_text(str(int(p.read_text())+1) if p.exists() else '1')\nprint('local raw evidence')\npathlib.Path(os.environ['LEDGER_AGENT_RESULT_FILE']).write_text(json.dumps({'input_sha':'sha','count':3}))\n")
    catalog=Catalog({'collect':{'argv':[sys.executable,str(path)]}},tmp_path/'operations')
    first=catalog.bind('collect',check)(case(),None)
    second=catalog.bind('collect',check)(case(),None)
    assert marker.read_text()=='1' and second['replayed']
    assert first['operation_id']==second['operation_id']
    folder=tmp_path/'operations'/first['operation_id']
    assert list(folder.glob('attempt-*/stdout'))[0].read_text().strip()=='local raw evidence'
    assert json.loads((folder/'receipt.json').read_text())['state']=='executed'


def test_timeout_is_unknown_and_is_not_blindly_reexecuted(tmp_path):
    marker=tmp_path/'calls'
    path=script(tmp_path,"import pathlib,time\np=pathlib.Path("+repr(str(marker))+")\np.write_text('called')\ntime.sleep(1)\n")
    catalog=Catalog({'collect':{'argv':[sys.executable,str(path)],'timeout_seconds':.05}},tmp_path/'operations')
    with pytest.raises(subprocess.TimeoutExpired):catalog.bind('collect',check)(case(),None)
    result=catalog.bind('collect',check)(case(),None)
    assert result['state']=='unknown_outcome'
    assert len(list((tmp_path/'operations').glob('*/attempt-*')))==1


def test_script_digest_and_worker_scope_are_enforced(tmp_path):
    path=script(tmp_path,'pass\n')
    catalog=Catalog({'repair':{'argv':[sys.executable,str(path)],'effect':'ops_repair'}},tmp_path/'operations')
    with pytest.raises(PermissionError):catalog.bind('repair',check)(case(),None)
    catalog=Catalog({'collect':{'argv':[sys.executable,str(path)],'script_sha256':'wrong'}},tmp_path/'other')
    with pytest.raises(ValueError,match='script changed'):catalog.bind('collect',check)(case(),None)


def test_lost_lease_and_changed_input_do_not_start_commands(tmp_path):
    path=script(tmp_path,'pass\n')
    catalog=Catalog({'collect':{'argv':[sys.executable,str(path)]}},tmp_path/'operations')
    with pytest.raises(PermissionError):catalog.bind('collect',lambda:{'lease_active':False})(case(),None)
    with pytest.raises(ValueError,match='inputs changed'):catalog.bind('collect',lambda:{'input_sha':'changed'})(case(),None)
