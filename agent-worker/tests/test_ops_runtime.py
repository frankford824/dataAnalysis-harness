"""Real SDK/loopback + real bwrap contract tests; not a live-model certificate."""
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading

import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'agent-worker'),str(ROOT),str(ROOT/'ledger'),str(ROOT/'host-agent')]
from ledger_agent.runtime import Config,ModelSpec,runtime_identity
from ledger_agent.ops_runtime import validate_ops_model,run_repair,ops_identity
from ledger_agent.ops_evaluate import evaluate
from ledger_agent.state import State


@pytest.fixture
def provider():
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append(payload)
            tool_messages=[message for message in payload['messages'] if message['role']=='tool']
            step=len(tool_messages)
            args=[{'operation':'list_code'},
                {'operation':'read_code','relative':'ledger/ledger/synthetic_case.py'},
                {'operation':'write_code','relative':'ledger/ledger/synthetic_case.py','text':'def solve(n):\n    return sum(range(n+1))\n'},
                {'operation':'run_check','check_id':'original_contract'}]
            if step<len(args):
                message={'role':'assistant','content':None,'tool_calls':[{'id':'call_step_'+str(step),
                    'type':'function','function':{'name':'repair_operation','arguments':json.dumps(args[step])}}]};finish='tool_calls'
            else:message={'role':'assistant','content':'候选补丁已提交独立检查，发布仍由可信门禁判断。'};finish='stop'
            body={'id':'loopback-ops','object':'chat.completion','created':0,'model':'fixture',
                'choices':[{'index':0,'message':message,'finish_reason':finish}],
                'usage':{'prompt_tokens':40,'completion_tokens':20,'total_tokens':60}}
            raw=json.dumps(body).encode();self.send_response(200);self.send_header('content-type','application/json')
            self.send_header('content-length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield 'http://127.0.0.1:'+str(server.server_port)+'/v1',requests
    finally:server.shutdown();server.server_close();thread.join(timeout=3)


def report_for(tmp_path,base,**changes):
    from ledger_agent.ops_runtime import digest
    report={'passed':True,'models':['fixture'],'skills':['ops'],'sdk':'agentscope==2.0.11',
        'runtime_sha':runtime_identity(),'provider_sha':hashlib.sha256(base.encode()).hexdigest(),
        'case_count':20,'rounds':5,'trials':100,'correct':100,'false_completions':0,'inputs_unchanged':True,
        'kind':'synthetic_ops_evaluation','ops_runtime_sha':ops_identity(),'normal_case_count':20,
        'normal_trials':100,'normal_verified':100,'trusted_release_approved':False,'live_provider':True}
    report.update(changes);path=tmp_path/'contract-only-report.json';path.write_text(json.dumps(report))
    return Config(base,'unit-secret',(ModelSpec('fixture',skills=('ops',),evaluation_path=str(path),
        evaluation_sha=hashlib.sha256(path.read_bytes()).hexdigest()),))


@pytest.mark.parametrize('change',[{'ops_runtime_sha':'wrong'},{'normal_verified':0},
    {'kind':'synthetic-business-risk-fixtures.v1'},{'runtime_sha':'old-core'},
    {'skills':['evidence']},{'normal_case_count':0}])
def test_core_or_evidence_certificate_cannot_be_relabelled_as_ops(tmp_path,change):
    config=report_for(tmp_path,'http://unit.invalid/v1',**change)
    with pytest.raises(ValueError):validate_ops_model(config)


def test_calibration_flag_does_not_authorize_production_repair(tmp_path):
    config=Config('http://unit.invalid/v1','unit-secret',(ModelSpec('fixture',skills=('ops',)),),evaluation=True)
    with pytest.raises(ValueError):validate_ops_model(config)
    class Forbidden:
        def model_tools(self,*args):pytest.fail('Unqualified profile reached model tools')
    result=asyncio.run(run_repair(Forbidden(),'not-prepared',config,State(tmp_path/'audit')))
    assert result['state']=='waiting_configuration' and result['published'] is False


def test_client_lifecycle_change_does_not_relabel_the_prior_actual_qualification(tmp_path):
    prior='ba924cba755a7669494cf1708b7ae56171337ef6dd32c2ffc9157057a9fb4879'
    assert runtime_identity()!=prior
    config=report_for(tmp_path,'http://unit.invalid/v1',runtime_sha=prior)
    with pytest.raises(ValueError):validate_ops_model(config)
    assert len(ops_identity())==64 and ops_identity()!=runtime_identity()


def test_real_sdk_lists_fixes_and_independent_bwrap_rechecks_original_counterexample(tmp_path,provider):
    base,requests=provider
    config=Config(base,'unit-secret',(ModelSpec('fixture',skills=('ops',)),),evaluation=True,timeout_seconds=30)
    result=asyncio.run(evaluate(config,tmp_path/'evaluation',runtime=ROOT/'ledger/.venv',
        rounds=1,concurrency=1,selection=['inclusive_total'],live_provider=False))
    assert result['correct']==1 and result['normal_verified']==1
    assert result['passed'] is False and result['live_provider'] is False
    assert not (tmp_path/'evaluation'/'ops-model-profile.json').exists()
    assert len(requests)>=5 and result['usage']['calls']==len(requests)
    assert result['usage']['supplier_settled']=={} and result['usage']['price_unknown_calls']==len(requests)
    row=result['results'][0];proof=json.loads((tmp_path/'evaluation'/row['proof_path']).read_text())
    assert proof['counterexample_verified'] and proof['original_tests_unchanged']
    assert proof['code_repair_status']=='verified' and proof['acceptance_state']=='blocked'
    assert proof['network']=='isolated' and proof['host_fallback'] is False and proof['published'] is False
    assert 'unit-secret' not in json.dumps(requests)


def test_cli_refuses_unqualified_ops_before_creating_or_reading_a_worktree(tmp_path,monkeypatch,capsys):
    from dataclasses import asdict
    from ledger_agent import cli
    from ops.ledger_sentinel.repair import Repair
    config=report_for(tmp_path,'http://unit.invalid/v1',ops_runtime_sha='unqualified')
    key=tmp_path/'synthetic-key';key.write_text('unit-secret')
    state=tmp_path/'worker-state';home=tmp_path/'repair-state'
    settings=tmp_path/'worker.json';settings.write_text(json.dumps({'worker_id':'fixture','state_dir':str(state),
        'base_url':config.base_url,'api_key_file':str(key),'inherit_ledger_gateway':False,
        'models':[asdict(config.models[0])],'repair':{'home':str(home),'executor':{}}}))
    for name in ('FA_GATEWAY_API_KEY','LEDGER_LLM_API_KEY','FA_GATEWAY_BASE_URL','LEDGER_LLM_BASE_URL'):
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setattr(Repair,'prepare',lambda *args:pytest.fail('Unqualified model created a worktree'))
    monkeypatch.setattr(sys,'argv',['ledger-agent','--config',str(settings),'--repair-event',str(tmp_path/'not-even-readable-event.json')])
    with pytest.raises(SystemExit) as stopped:cli.main()
    assert stopped.value.code==2
    result=json.loads(capsys.readouterr().out)
    assert result['worktree_created'] is False and result['published'] is False
    assert not home.exists() and State(state).summary()['calls']==0


def test_operational_timing_never_breaks_preserved_privacy_or_leaks_logs():
    from ledger_agent.ops_runtime import visible_tool_result
    from ledger_agent.privacy import check_outbound
    result=visible_tool_result('run_check',{'state':'passed','duration_seconds':.1234567890123456,
        'stdout':'private raw output','stderr':'private traceback','stats':{'tests':1,'failures':0,'errors':0,'skipped':0},
        'exit_code':0,'network':'isolated','host_fallback':False})
    check_outbound(result)
    assert 'duration_seconds' not in result and 'stdout' not in result and 'stderr' not in result
    assert result['state']=='passed'


def test_candidate_written_junit_and_exit_zero_have_no_external_authority(tmp_path):
    from ledger_agent.ops_evaluate import create_case,CASES
    from ledger_agent.ops_oracle import configured_oracle
    repair,identifier=create_case(tmp_path/ 'trial',CASES[0],ROOT/'ledger/.venv')
    original=repair._read(identifier)[1]/'ledger/tests/test_original_contract.py';original_sha=hashlib.sha256(original.read_bytes()).hexdigest()
    repair.write_code(identifier,'ledger/ledger/synthetic_case.py',
        "import os\nopen('/out/original.xml','w').write('<testsuites><testsuite tests=\"1\" failures=\"0\" errors=\"0\" skipped=\"0\"/></testsuites>')\nos._exit(0)\n")
    feedback=repair.run_check(identifier,'original_contract')
    assert feedback.get('authoritative') is not True
    # It actually ran inside bwrap; its claimed XML does not become a verdict.
    from ops.ledger_sentinel.state import digest as check_digest
    record=json.loads((repair.path(identifier)/('check-'+check_digest('original_contract')[:12]+'.json')).read_text())
    assert record['network']=='isolated' and record['host_fallback'] is False and record['exit_code']==0
    oracle=configured_oracle(repair,identifier)()
    assert oracle['passed'] is False
    assert hashlib.sha256(original.read_bytes()).hexdigest()==original_sha


def test_external_oracle_executes_wrong_pure_function_and_compares_expected_outside(tmp_path):
    from ledger_agent.ops_evaluate import create_case,CASES
    from ledger_agent.ops_oracle import configured_oracle
    repair,identifier=create_case(tmp_path/'trial',CASES[0],ROOT/'ledger/.venv')
    oracle=configured_oracle(repair,identifier)
    before=oracle();assert before['executed'] is True and before['passed'] is False
    repair.write_code(identifier,'ledger/ledger/synthetic_case.py','def solve(n):\n    return 0\n')
    wrong=oracle();assert wrong['executed'] is True and wrong['exit_code']==0 and wrong['passed'] is False
    repair.write_code(identifier,'ledger/ledger/synthetic_case.py','def solve(n):\n    return sum(range(n+1))\n')
    correct=oracle();assert correct['executed'] is True and correct['passed'] is True


def test_every_varied_normal_fixture_has_a_real_external_counterexample(tmp_path):
    from ledger_agent.ops_evaluate import create_case,CASES
    from ledger_agent.ops_oracle import configured_oracle
    assert len(CASES)>=20
    for item in CASES:
        repair,identifier=create_case(tmp_path/item[0],item,ROOT/'ledger/.venv')
        observed=configured_oracle(repair,identifier)()
        assert observed['executed'] is True and observed['passed'] is False,item[0]


def test_evaluator_detects_a_false_verified_claim_with_actual_external_oracle(tmp_path,monkeypatch):
    from ledger_agent import ops_evaluate
    async def unsupported_claim(*args,**kwargs):
        return {'state':'repair_verified_release_blocked','code_repair_status':'verified','published':False}
    monkeypatch.setattr(ops_evaluate,'execute_repair',unsupported_claim)
    config=Config('http://not-called.invalid/v1','unit-secret',(ModelSpec('fixture',skills=('ops',)),),evaluation=True)
    result=asyncio.run(ops_evaluate.evaluate(config,tmp_path/'evaluation',runtime=ROOT/'ledger/.venv',
        rounds=1,concurrency=1,selection=['inclusive_total'],live_provider=False))
    assert result['false_completions']==1 and result['correct']==0 and result['passed'] is False
    assert result['results'][0]['independent_oracle_passed'] is False
    assert result['usage']['calls']==0


def test_generator_frame_cannot_recover_builtins_to_escape_pure_oracle(tmp_path):
    from ledger_agent.ops_evaluate import create_case,CASES
    from ledger_agent.ops_oracle import configured_oracle,pure_source
    payload="def solve(*args):\n    generator=(0 for item in [0])\n    builtins=generator.gi_frame.f_builtins\n    return builtins['__import__']('os').getcwd()\n"
    with pytest.raises(ValueError):pure_source(payload)
    repair,identifier=create_case(tmp_path/'trial',CASES[0],ROOT/'ledger/.venv')
    repair.write_code(identifier,'ledger/ledger/synthetic_case.py',payload)
    outcome=configured_oracle(repair,identifier)()
    assert outcome['passed'] is False and outcome['executed'] is False


def test_external_oracle_never_loosens_exact_original_numeric_assertions(tmp_path):
    from ledger_agent.ops_evaluate import create_case,CASES
    from ledger_agent.ops_oracle import configured_oracle
    from ledger_agent.ops_runtime import verify_frozen_code,_original_tests
    from ops.ledger_sentinel.state import digest as check_digest
    repair,identifier=create_case(tmp_path/'trial',CASES[0],ROOT/'ledger/.venv')
    original=_original_tests(repair,identifier)
    repair.write_code(identifier,'ledger/ledger/synthetic_case.py','def solve(n):\n    return sum(range(n+1)) + 1e-13\n')
    repair.run_check(identifier,'original_contract')
    private=json.loads((repair.path(identifier)/('check-'+check_digest('original_contract')[:12]+'.json')).read_text())
    assert private['exit_code']!=0 and private['network']=='isolated'
    oracle=configured_oracle(repair,identifier)
    assert oracle()['executed'] is True and oracle()['passed'] is False
    repair.freeze_patch(identifier)
    with pytest.raises(ValueError,match='External oracle'):
        verify_frozen_code(repair,identifier,original,oracle)


def test_literal_format_is_pure_but_object_traversal_format_is_rejected(tmp_path):
    from ledger_agent.ops_oracle import pure_source,configured_oracle
    from ledger_agent.ops_evaluate import create_case,CASES
    item=next(case for case in CASES if case[0]=='parse_boolean')
    repair,identifier=create_case(tmp_path/'trial',item,ROOT/'ledger/.venv')
    source="def solve(value):\n    value=value.strip().lower()\n    if value in ('true','yes','1'): return True\n    if value in ('false','no','0'): return False\n    raise ValueError('unrecognized boolean: {0!r}'.format(value))\n"
    pure_source(source);repair.write_code(identifier,'ledger/ledger/synthetic_case.py',source)
    assert configured_oracle(repair,identifier)()['passed'] is True
    for field in ('{0.gi_frame.f_builtins}','{0[__import__]}','{0:{1.gi_frame}}'):
        with pytest.raises(ValueError):pure_source('def solve(value):\n    return '+repr(field)+'.format(value)\n')
