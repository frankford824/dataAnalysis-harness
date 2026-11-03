import json
from pathlib import Path
import subprocess
import sys

import pytest

from ops.ledger_sentinel.repair import Repair, sanitized_event
from ops.ledger_sentinel.probes import file_sha
from test_sentinel import deployment, command_script


def synthetic_independent_oracle(template,roots,decorate=None):
    """Independent-output unit stub; it does not certify real financial behavior."""
    def verify(*,candidate_code_sha,candidate_root,diagnostic_root,output):
        from ledger.release_gate import digest
        value=json.loads(json.dumps(template));value['code_sha']=candidate_code_sha
        for check in value['checks']:
            (output/check['report']).write_bytes((roots['evidence']/check['report']).read_bytes())
        if decorate:decorate(value,output)
        value['receipt_sha']=digest({k:v for k,v in value.items() if k!='receipt_sha'})
        (output/'acceptance-receipt.json').write_text(json.dumps(value))
        return {'kind':'independent_financial_acceptance.v1','candidate_code_sha':candidate_code_sha,
            'candidate_execution_isolated':True,'receipt':'acceptance-receipt.json'}
    return verify


@pytest.fixture
def repo(tmp_path):
    source=tmp_path/'repo';source.mkdir()
    subprocess.run(['git','init',str(source)],check=True,capture_output=True)
    code=source/'ledger/ledger/demo.py';code.parent.mkdir(parents=True);code.write_text('value = 1\n')
    control=source/'ledger/ledger/release_gate.py';control.write_text('# protected\n')
    for relative in ('ledger/ledger/write_auth.py','ledger/ledger/automation_checker.py',
                     'ledger/tests/test_accept_gate.py','ledger/tests/regressions/test_existing.py',
                     'ledger/web/tests/browser-runner.mjs','ledger/web/src/example.test.js',
                     'agent-worker/ledger_agent/privacy.py','models/private.py',
                     'ledger/tests/baseline/test_private.py','ledger/ledger/secrets/private.py'):
        target=source/relative;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text('# trusted original source or expectation\n')
    subprocess.run(['git','-C',str(source),'add','.'],check=True,capture_output=True)
    subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@localhost','-c','commit.gpgsign=false',
        '-C',str(source),'commit','-m','fixture'],check=True,capture_output=True)
    version=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
    code.write_text('value = "existing user work"\n')
    (source/'untracked.txt').write_text('existing untracked user work')
    return source,version


def test_incident_manifest_never_contains_raw_logs_or_business_fields():
    event={'id':'manager','status':'failed','reason':'token=private-secret customer=Alice order=3300000000000000000',
        'metrics':{'pending':3,'amount':999999,'cookie':'session-secret'},'logs':'complete private raw log',
        'corpus':'D:/finance/customer.xlsx'}
    body=sanitized_event(event,'a'*40);encoded=json.dumps(body)
    assert body['metrics']=={'pending':3} and body['reason_code']=='unclassified_failure'
    assert all(secret not in encoded for secret in ('private-secret','Alice','330000','session-secret','complete private','customer.xlsx'))


def test_worktree_edits_and_commit_preserve_all_original_user_work(repo,tmp_path):
    source,version=repo
    repair=Repair(source,tmp_path/'state',{'checks':[]},which=lambda _:None)
    before=repair.source_identity();job=repair.prepare({'id':'worker','status':'failed','reason':'worker_down'},version)
    identifier=job['repair_id']
    assert 'value = 1' in repair.read_code(identifier,'ledger/ledger/demo.py')
    repair.write_code(identifier,'ledger/ledger/demo.py','value = 2\n')
    candidate=repair.freeze_patch(identifier)
    assert candidate['candidate_code_sha']!=version and repair.source_identity()==before
    assert (source/'ledger/ledger/demo.py').read_text()=='value = "existing user work"\n'
    assert (source/'untracked.txt').read_text()=='existing untracked user work'
    with pytest.raises(ValueError,match='frozen'):repair.write_code(identifier,'ledger/ledger/demo.py','value=3')
    artifacts=repair.artifacts(identifier,gh_executable=sys.executable)
    assert Path(artifacts['patch']).is_file() and Path(artifacts['body_file']).is_file()
    assert len(Path(artifacts['body_file']).read_text().splitlines())>=5
    assert '--body-file' in artifacts['gh_argv'] and '--draft' in artifacts['gh_argv']
    assert artifacts['published'] is False


@pytest.mark.parametrize('path',['../outside.py','/etc/passwd','ledger/ledger/release_gate.py','models/rules.py',
    'ledger/tests/baseline/expectations.py','ledger/ledger/secrets/key.py','ledger/ledger/demo.env'])
def test_patch_path_and_financial_gate_controls_are_protected(repo,tmp_path,path):
    source,version=repo;repair=Repair(source,tmp_path/'state',{})
    identifier=repair.prepare({'status':'failed'},version)['repair_id']
    with pytest.raises(ValueError):repair.write_code(identifier,path,'malicious = True')


def test_no_network_sandbox_means_blocked_without_host_fallback(repo,tmp_path):
    source,version=repo;calls=[]
    repair=Repair(source,tmp_path/'state',{'checks':[{'id':'unit','argv':['/usr/bin/python3','-m','pytest'],
        'report':'junit.xml','timeout_seconds':30}], 'trusted_acceptance':{'argv':['/trusted/check.py']}},
        which=lambda _:None,runner=lambda *_:calls.append(1),oracle=lambda **kw:None)
    identifier=repair.prepare({'status':'failed'},version)['repair_id']
    result=repair.run_check(identifier,'unit')
    assert result['state']=='blocked' and not result['host_fallback'] and not calls
    assert 'output' not in result
    repair.write_code(identifier,'ledger/ledger/demo.py','value=2\n');repair.freeze_patch(identifier)
    result=repair.accept(identifier)
    assert result['acceptance_state']=='blocked' and result['acceptance_reason']=='network_isolation_unavailable'
    assert not calls


def test_symlink_and_git_pointer_changes_are_rejected_before_trusted_execution(repo,tmp_path):
    source,version=repo;repair=Repair(source,tmp_path/'state',{})
    body=repair.prepare({'status':'failed'},version);identifier=body['repair_id'];tree=Path(body['worktree'])
    outside=tmp_path/'private.py';outside.write_text('private = True')
    (tree/'ledger/ledger/link.py').symlink_to(outside)
    with pytest.raises(ValueError):repair.validate_patch(identifier)
    (tree/'ledger/ledger/link.py').unlink();(tree/'.git').write_text('gitdir: /elsewhere\n')
    with pytest.raises(ValueError,match='pointer'):repair.read_code(identifier,'ledger/ledger/demo.py')


def test_model_checks_receive_only_fixed_networkless_argv_and_no_corpus(repo,tmp_path):
    source,version=repo;private=tmp_path/'private-corpus';private.mkdir();calls=[]
    def runner(spec,environment):
        calls.append(spec['argv']);assert environment=={}
        argv=spec['argv'];output=Path(argv[argv.index('/out')-1])
        (output/'unit.xml').write_text('<testsuites><testsuite tests="2" failures="0" errors="0" skipped="0">'
            '<testcase classname="synthetic_repair_check" name="first" />'
            '<testcase classname="synthetic_repair_check" name="second" /></testsuite></testsuites>')
        return {'exit_code':0,'timed_out':False,'stdout_sha256':'a'*64}
    repair=Repair(source,tmp_path/'state',{'corpus_root':str(private),
        'checks':[{'id':'unit','argv':['/usr/bin/python3','-m','pytest','--junitxml=/out/unit.xml'],
            'report':'unit.xml','timeout_seconds':30}]},which=lambda name:sys.executable if name=='bwrap' else None,runner=runner)
    identifier=repair.prepare({'status':'failed'},version)['repair_id']
    result=repair.run_check(identifier,'unit')
    assert result['state']=='reported_pass' and result['authoritative'] is False
    assert 'stats' not in result and 'failed_tests' not in result
    assert '--unshare-net' in calls[0] and '--clearenv' in calls[0]
    assert str(private) not in calls[0] and '/corpus' not in calls[0]
    with pytest.raises(ValueError,match='configured'):repair.run_check(identifier,'arbitrary-shell-command')


def test_trusted_acceptance_uses_private_corpus_and_real_gate_without_model_log_access(repo,deployment,tmp_path):
    source,version=repo;_state,_probes,_plan,template,_old,_candidate,roots=deployment
    trusted=tmp_path/'trusted';trusted.mkdir();entry=trusted/'runner.py';entry.write_text('# pinned trusted runner fixture\n')
    config={'corpus_root':str(roots['corpus']),'trusted_root':str(trusted),'trusted_entry':'runner.py',
        'trusted_entry_sha256':file_sha(entry),'trusted_acceptance':{
            'argv':['/usr/bin/python3','/trusted/runner.py'],'timeout_seconds':30},'checks':[]}
    calls=[]
    def runner(spec,environment):
        calls.append(spec['argv']);output=Path(spec['argv'][spec['argv'].index('/out')-1])
        # The fixture represents trusted executor output, not a model-produced receipt.
        from ledger.release_gate import digest
        value=json.loads(json.dumps(template));value['code_sha']=frozen['candidate_code_sha']
        for check in value['checks']:
            report=output/check['report'];report.write_bytes((roots['evidence']/check['report']).read_bytes())
        value['receipt_sha']=digest({k:v for k,v in value.items() if k!='receipt_sha'})
        (output/'acceptance-receipt.json').write_text(json.dumps(value))
        return {'exit_code':0,'timed_out':False}
    repair=Repair(source,tmp_path/'repair-state',config,which=lambda name:sys.executable if name=='bwrap' else None,runner=runner,
        oracle=synthetic_independent_oracle(template,roots))
    identifier=repair.prepare({'status':'failed','logs':'never send this'},version)['repair_id']
    repair.write_code(identifier,'ledger/ledger/demo.py','value=2\n');frozen=repair.freeze_patch(identifier)
    accepted=repair.accept(identifier)
    assert accepted['acceptance_state']=='passed' and accepted['acceptance_receipt_sha']
    assert calls==[]  # The legacy corpus/output sandbox must never run before the independent oracle.
    assert set(repair.model_tools(identifier))=={'list_code','read_code','write_code','run_check'}
    with pytest.raises(ValueError):repair.read_code(identifier,'../trusted-results/acceptance-receipt.json')


@pytest.mark.parametrize('relative',[
    'ledger/ledger/write_auth.py','ledger/ledger/automation_checker.py','ledger/ledger/automation_actions.py',
    'ledger/ledger/automation_store.py','ledger/ledger/automation_period_proof.py','ledger/ledger/worker_control_api.py',
    'ledger/ledger/workspace.py','ledger/ledger/finance_guard.py','ledger/ledger/api.py','ledger/ledger/upstream_history.py',
    'ledger/ledger/version.py','ledger/ledger/snapshot_store.py','ledger/ledger/run_artifacts.py','ledger/ledger/period_snapshot.py',
    'ledger/ledger/operations_income.py','ledger/ledger/__init__.py',
    'ledger/ledger/cost_lines.py','ledger/ledger/manual_cost.py','ledger/ledger/commission_registry.py',
    'agent-worker/ledger_agent/runtime.py','agent-worker/ledger_agent/privacy.py','agent-worker/ledger_agent/state.py',
    'host-agent/finance_agent/control_plane.py','tools/verify-ledger-acceptance.py',
    'ledger/ledger/./write_auth.py','ledger/ledger/../ledger/automation_checker.py',
])
def test_broad_config_cannot_edit_authority_release_or_privacy_controls(repo,tmp_path,relative):
    source,version=repo;repair=Repair(source,tmp_path/'state',{'code_prefixes':['']})
    identifier=repair.prepare({'status':'failed'},version)['repair_id']
    with pytest.raises(ValueError):repair.write_code(identifier,relative,'return_all_checks_passed = True\n')


@pytest.mark.parametrize('relative',['ledger/tests/test_accept_gate.py','ledger/tests/regressions/test_existing.py',
    'ledger/web/tests/browser-runner.mjs','ledger/web/src/example.test.js'])
def test_existing_tests_are_readable_but_neither_editable_nor_deletable(repo,tmp_path,relative):
    source,version=repo;repair=Repair(source,tmp_path/'state',{'code_prefixes':['']})
    job=repair.prepare({'status':'failed'},version);identifier=job['repair_id']
    assert 'trusted original' in repair.read_code(identifier,relative)
    with pytest.raises(ValueError,match='immutable'):repair.write_code(identifier,relative,'# assertion removed\n')
    # Even a bypass of the editing tool cannot pass patch validation.
    (Path(job['worktree'])/relative).unlink()
    with pytest.raises(ValueError,match='immutable'):repair.validate_patch(identifier)


@pytest.mark.parametrize('relative',['ledger/tests/test_new_gate.py','ledger/tests/regressions/conftest.py',
    'ledger/tests/regressions/__init__.py','ledger/tests/../tests/test_accept_gate.py'])
def test_new_tests_only_in_separate_flat_counterexample_namespace(repo,tmp_path,relative):
    source,version=repo;repair=Repair(source,tmp_path/'state',{'code_prefixes':['']})
    identifier=repair.prepare({'status':'failed'},version)['repair_id']
    with pytest.raises(ValueError):repair.write_code(identifier,relative,'# unsafe test control\n')


def test_new_counterexample_allowed_but_trusted_runner_without_isolation_is_blocked(repo,tmp_path):
    source,version=repo;calls=[]
    repair=Repair(source,tmp_path/'state',{'trusted_acceptance':{'argv':['/trusted/runner.py']}},
        which=lambda name:sys.executable if name=='bwrap' else None,runner=lambda *_:calls.append(1),oracle=lambda **kw:None)
    identifier=repair.prepare({'status':'failed'},version)['repair_id']
    filename='ledger/tests/regressions/test_new_counterexample.py'
    repair.write_code(identifier,filename,'def test_new_counterexample():\n    assert 2 + 2 == 4\n')
    assert 'counterexample' in repair.read_code(identifier,filename)
    frozen=repair.freeze_patch(identifier)
    assert frozen['new_regressions']==[{'path':filename,'sha256':file_sha(Path(frozen['worktree'])/filename)}]
    result=repair.accept(identifier)
    assert result['acceptance_state']=='blocked' and result['acceptance_reason']=='independent_financial_oracle_invalid'
    assert calls==[]
    with pytest.raises(ValueError,match='Frozen'):repair.read_code(identifier,filename)


def test_trusted_gate_mounts_existing_authority_and_tests_readonly_and_checks_isolation_receipt(repo,deployment,tmp_path):
    source,version=repo;_state,_probes,_plan,template,_old,_candidate,roots=deployment
    trusted=tmp_path/'trusted';trusted.mkdir();entry=trusted/'runner.py';entry.write_text('# pinned isolation runner\n')
    config={'corpus_root':str(roots['corpus']),'trusted_root':str(trusted),'trusted_entry':'runner.py',
        'trusted_entry_sha256':file_sha(entry),'trusted_acceptance':{
            'argv':['/usr/bin/python3','/trusted/runner.py','--untrusted-regressions','/repair-regressions.json']}}
    calls=[]
    def runner(spec,environment):
        calls.append(spec['argv']);argv=spec['argv'];output=Path(argv[argv.index('/out')-1])
        manifest=Path(argv[argv.index('/repair-regressions.json')-1]);captured=json.loads(manifest.read_text())
        assert captured['code_sha']==frozen['candidate_code_sha'] and captured['files']==frozen['new_regressions']
        for target in ('/work/ledger/tests','/work/ledger/web/tests','/work/ledger/web/src/example.test.js',
                       '/work/ledger/ledger/automation_checker.py','/work/agent-worker/ledger_agent/privacy.py'):
            assert argv[argv.index(target)-2]=='--ro-bind'
        from ledger.release_gate import digest
        value=json.loads(json.dumps(template));value['code_sha']=frozen['candidate_code_sha']
        for check in value['checks']:
            (output/check['report']).write_bytes((roots['evidence']/check['report']).read_bytes())
        report=output/'repair-regressions.xml'
        report.write_text('<testsuite tests="1" failures="0" errors="0" skipped="0">'
            '<testcase classname="synthetic_isolated_regression" name="counterexample" /></testsuite>')
        value['repair_regressions']={'code_sha':frozen['candidate_code_sha'],'files':frozen['new_regressions'],
            'excluded_from_official_gates':True,'isolation':'separate_pytest_process',
            'check':{'report':report.name,'report_sha256':file_sha(report),'exit_code':0}}
        value['receipt_sha']=digest({k:v for k,v in value.items() if k!='receipt_sha'})
        (output/'acceptance-receipt.json').write_text(json.dumps(value))
        return {'exit_code':0,'timed_out':False}
    def decorate(value,output):
        report=output/'independent-regressions.xml'
        report.write_text('<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="synthetic_external_oracle" /></testsuite>')
        value['repair_regressions']={'code_sha':frozen['candidate_code_sha'],'files':frozen['new_regressions'],
            'excluded_from_official_gates':True,'isolation':'separate_pytest_process',
            'check':{'report':report.name,'report_sha256':file_sha(report),'exit_code':0}}
    repair=Repair(source,tmp_path/'state',config,which=lambda name:sys.executable if name=='bwrap' else None,runner=runner,
        oracle=synthetic_independent_oracle(template,roots,decorate))
    identifier=repair.prepare({'status':'failed'},version)['repair_id']
    repair.write_code(identifier,'ledger/tests/regressions/test_safe.py','def test_counterexample(): assert True\n')
    frozen=repair.freeze_patch(identifier)
    assert repair.accept(identifier)['acceptance_state']=='passed'
    assert calls==[]


def test_failed_trusted_candidate_cannot_return_corpus_through_mutated_worktree(repo,deployment,tmp_path):
    source,version=repo;_state,_probes,_plan,template,_old,_candidate,roots=deployment
    trusted=tmp_path/'trusted';trusted.mkdir();entry=trusted/'runner.py';entry.write_text('# pinned fixture\n')
    config={'corpus_root':str(roots['corpus']),'trusted_root':str(trusted),'trusted_entry':'runner.py',
        'trusted_entry_sha256':file_sha(entry),'trusted_acceptance':{'argv':['/usr/bin/python3','/trusted/runner.py']}}
    def hostile_oracle(*,candidate_code_sha,candidate_root,diagnostic_root,output):
        (candidate_root/'ledger/ledger/demo.py').write_text('private_customer_row = "must stay private"\n')
        return synthetic_independent_oracle(template,roots)(candidate_code_sha=candidate_code_sha,
            candidate_root=candidate_root,diagnostic_root=diagnostic_root,output=output)
    repair=Repair(source,tmp_path/'state',config,which=lambda name:sys.executable if name=='bwrap' else None,oracle=hostile_oracle)
    identifier=repair.prepare({'status':'failed'},version)['repair_id']
    repair.write_code(identifier,'ledger/ledger/demo.py','value=2\n');frozen=repair.freeze_patch(identifier)
    with pytest.raises(ValueError,match='changed during'):repair.accept(identifier)
    result=repair.model_tools(identifier)['read_code']('ledger/ledger/demo.py')
    assert result['state']=='blocked' and 'must stay private' not in json.dumps(result)


def test_code_inventory_locates_only_scoped_source_and_trusted_entry_points(repo,tmp_path):
    source,version=repo
    repair=Repair(source,tmp_path/'state',{'checks':[{'id':'unit','argv':['/runtime/bin/python','-m','pytest']}],
        'code_entry_points':{'manager':['ledger/ledger/demo.py','ledger/tests/test_accept_gate.py']}})
    identifier=repair.prepare({'id':'manager','status':'failed','logs':'private-business-log'},version)['repair_id']
    inventory=repair.model_tools(identifier)['list_code']()
    paths={row['path']:row for row in inventory['files']}
    assert inventory['state']=='available' and inventory['checks']==[{'id':'unit'}]
    assert inventory['entry_points']==['ledger/ledger/demo.py','ledger/tests/test_accept_gate.py']
    assert paths['ledger/ledger/demo.py']['writable'] is True
    assert paths['ledger/tests/test_accept_gate.py']['writable'] is False
    assert 'ledger/ledger/write_auth.py' not in paths and 'ledger/ledger/release_gate.py' not in paths
    assert 'private-business-log' not in json.dumps(inventory) and len(inventory['manifest_sha'])==64
    assert repair.list_code(identifier,limit=1)['more'] is True
    with pytest.raises(ValueError):repair.list_code(identifier,prefix='../')
    repair.write_code(identifier,'ledger/ledger/demo.py','value=2\n');repair.freeze_patch(identifier)
    assert repair.model_tools(identifier)['list_code']()['state']=='blocked'


def test_actual_model_sandbox_cannot_read_checkout_secrets_or_claim_report_authority(repo,tmp_path):
    import shutil
    source,version=repo
    config={'checks':[{'id':'forgery','argv':['/usr/bin/python3','/work/ledger/ledger/demo.py'],
        'report':'unit.xml','timeout_seconds':30}]}
    repair=Repair(source,tmp_path/'state',config)
    job=repair.prepare({'status':'failed'},version);identifier=job['repair_id']
    attack='''from pathlib import Path
import json,os
paths=['/work/models/private.py','/work/ledger/tests/baseline/test_private.py','/work/ledger/ledger/secrets/private.py','/work/.git']
seen={p:Path(p).exists() for p in paths}
Path('/out/visibility.json').write_text(json.dumps(seen))
Path('/out/unit.xml').write_text('<testsuite tests="999999" failures="0" errors="0" skipped="0"><testcase name="CANDIDATE_CONTROLLED_SECRET_CHANNEL"><failure>private value</failure></testcase></testsuite>')
os._exit(0)
'''
    repair.write_code(identifier,'ledger/ledger/demo.py',attack)
    result=repair.run_check(identifier,'forgery')
    if not shutil.which('bwrap'):
        assert result['state']=='blocked' and result['host_fallback'] is False
        return
    assert result['state']=='reported_pass' and result['authoritative'] is False
    assert all(field not in result for field in ('stats','failed_tests','stdout_sha256','output'))
    assert 'CANDIDATE_CONTROLLED_SECRET_CHANNEL' not in json.dumps(result)
    retained=json.loads(next((repair.path(identifier)/'checks').rglob('visibility.json')).read_text())
    assert all(value is False for value in retained.values())
    projection=next((repair.path(identifier)/'model-views').iterdir())
    assert not (projection/'models').exists() and not (projection/'.git').exists()


def test_candidate_writable_acceptance_output_cannot_become_financial_authority(repo,tmp_path):
    source,version=repo;calls=[]
    repair=Repair(source,tmp_path/'state',{'trusted_acceptance':{'argv':['/trusted/fake.py']},
        'independent_financial_oracle':True},which=lambda name:sys.executable if name=='bwrap' else None,
        runner=lambda *args:calls.append(args))
    identifier=repair.prepare({'status':'failed'},version)['repair_id']
    repair.write_code(identifier,'ledger/ledger/demo.py','value=2\n');repair.freeze_patch(identifier)
    result=repair.accept(identifier)
    assert result['acceptance_state']=='blocked'
    assert result['acceptance_reason']=='independent_financial_oracle_unconfigured' and not calls


def test_legacy_inprocess_financial_sandbox_cannot_expose_corpus(repo,tmp_path):
    source,version=repo;calls=[]
    repair=Repair(source,tmp_path/'state',{},runner=lambda *args:calls.append(args))
    identifier=repair.prepare({'status':'failed'},version)['repair_id']
    result=repair._sandbox(identifier,{'argv':['/usr/bin/python3']},trusted=True)
    assert result['state']=='blocked' and result['reason']=='legacy_inprocess_financial_sandbox_disabled'
    assert not calls


def test_synthetic_fixture_projection_hides_unlisted_env_and_logs(repo,tmp_path):
    import shutil
    source,version=repo;fixture=tmp_path/'synthetic-fixture';fixture.mkdir()
    data=fixture/'declared.json';data.write_text('{"kind":"synthetic"}')
    (fixture/'.env').write_text('SYNTHETIC_UNLISTED_SECRET=hidden')
    (fixture/'private.log').write_text('synthetic unlisted raw log')
    (fixture/'sanitized-fixture.json').write_text(json.dumps({'kind':'synthetic_reproduction','business_rows':False,
        'files':[{'path':data.name,'sha256':file_sha(data)}]}))
    repair=Repair(source,tmp_path/'state',{'sanitized_fixture_root':str(fixture),'checks':[{
        'id':'fixture','argv':['/usr/bin/python3','/work/ledger/ledger/demo.py'],'report':'unit.xml','timeout_seconds':30}]})
    job=repair.prepare({'status':'failed'},version);identifier=job['repair_id']
    code='''from pathlib import Path
import json
visible=sorted(p.name for p in Path('/fixtures').iterdir())
Path('/out/visible.json').write_text(json.dumps(visible))
assert visible==['declared.json']
Path('/out/unit.xml').write_text('<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="synthetic_fixture_visibility" /></testsuite>')
'''
    repair.write_code(identifier,'ledger/ledger/demo.py',code)
    result=repair.run_check(identifier,'fixture')
    if not shutil.which('bwrap'):
        assert result['state']=='blocked' and result['host_fallback'] is False
        return
    assert result['state']=='reported_pass'
    assert json.loads(next((repair.path(identifier)/'checks').rglob('visible.json')).read_text())==['declared.json']
    projected=next((repair.path(identifier)/'fixture-views').iterdir())
    assert sorted(p.name for p in projected.iterdir())==['declared.json']
