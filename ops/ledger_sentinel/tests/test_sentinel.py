from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import threading

import pytest

from ops.ledger_sentinel.executor import Executor, atomic_json, command
from ops.ledger_sentinel.probes import Probes, file_sha
from ops.ledger_sentinel.readiness import Readiness, REQUIRED_OBSERVATIONS
from ops.ledger_sentinel.sentinel import Sentinel
from ops.ledger_sentinel.state import State, digest


def stamp(seconds=0):
    return (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def command_script(tmp_path):
    script=tmp_path/'fixed_action.py'
    script.write_text('''import hashlib,json,os,sys,time
from datetime import datetime,timezone
from pathlib import Path
mode,home=sys.argv[1:3];root=Path(home)
with (root/'calls').open('a') as f:f.write(mode+'\\n')
if mode=='fail':raise SystemExit(23)
if mode=='timeout':time.sleep(3)
if mode=='backup':
 import shutil
 sys.path.insert(0,__BACKUP_FIXTURE_DIR__)
 from backup_fixture import create_verified_backup
 manifest=create_verified_backup(root/('backup-fixture-'+str(time.time_ns())),os.environ['LEDGER_PREVIOUS_CODE_SHA'])
 shutil.copytree(manifest.parent,root/'backup',dirs_exist_ok=True)
elif mode in ('deploy','bad_readback'):
 version=os.environ['LEDGER_CANDIDATE_CODE_SHA'] if mode=='deploy' else 'unexpected-version'
 (root/'actual.json').write_text(json.dumps({'version':version,'ok':True}))
elif mode=='rollback':
 (root/'actual.json').write_text(json.dumps({'version':os.environ.get('LEDGER_PREVIOUS_CODE_SHA','old'),'ok':True}))
elif mode=='recover':(root/'actual.json').write_text(json.dumps({'ok':True}))
'''.replace('__BACKUP_FIXTURE_DIR__',repr(str(Path(__file__).parent))),encoding='utf-8')
    def spec(mode,timeout=5):
        return {'argv':[sys.executable,str(script),mode,str(tmp_path)],'timeout_seconds':timeout}
    return spec


@pytest.fixture
def deployment(tmp_path,command_script):
    from ledger.release_gate import REQUIRED_CHECKS, validate
    current,candidate='a'*40,'b'*40
    roots={name:tmp_path/name for name in ('models','corpus','frontend','evidence')}
    for root in roots.values():root.mkdir()
    (roots['models']/'rules.yaml').write_text('approved: true')
    (roots['corpus']/'golden.xlsx').write_bytes(b'unit fixture input only; not a production acceptance signature')
    (roots['frontend']/'index.html').write_text('<html>fixture</html>')
    def identities(root):
        return [{'path':p.relative_to(root).as_posix(),'sha256':file_sha(p)} for p in sorted(root.rglob('*')) if p.is_file()]
    checks=[]
    for name in sorted(REQUIRED_CHECKS):
        report=roots['evidence']/(name+'.xml')
        report.write_text('<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
            '<testcase classname="synthetic_receipt_contract" name="'+name+'" />'
            '</testsuite></testsuites>')
        checks.append({'name':name,'exit_code':0,'tests':1,'failures':0,'errors':0,'skipped':0,
            'report':report.name,'report_sha256':file_sha(report)})
    models,inputs=identities(roots['models']),identities(roots['corpus'])
    receipt={'schema':1,'code_sha':candidate,'model_sha':digest(models),'model_manifest':models,
        'inputs':inputs,'input_sha':digest(inputs),'checks':checks,'frontend_artifacts':identities(roots['frontend']),
        'unresolved_differences':0,'inputs_unchanged':True}
    receipt['receipt_sha']=digest(receipt)
    validate(receipt,version=candidate,evidence_root=roots['evidence'])
    atomic_json(tmp_path/'current.json',{'code_sha':current})
    atomic_json(tmp_path/'actual.json',{'version':current,'ok':True})
    probes=Probes([
        {'id':'disk','kind':'disk','path':str(tmp_path),'min_free_bytes':1},
        {'id':'old','kind':'json','path':str(tmp_path/'actual.json'),'assertions':[{'field':'version','value':'$current_version'}]},
        {'id':'new','kind':'json','path':str(tmp_path/'actual.json'),'assertions':[{'field':'version','value':'$candidate_version'}]},
        {'id':'healthy','kind':'json','path':str(tmp_path/'actual.json'),'assertions':[{'field':'ok','value':True}]},
    ])
    plan={'id':'release','kind':'deployment','capability':'technical','preconditions':['disk','old'],
        'postconditions':['new','healthy'],'rollback_conditions':['old','healthy'],
        'backup':command_script('backup'),'deploy':command_script('deploy'),'rollback':command_script('rollback'),
        'backup_manifest':str(tmp_path/'backup/manifest.json'),'required_backup_roles':['application'],
        'current_state':str(tmp_path/'current.json'),'model_root':str(roots['models']),
        'corpus_root':str(roots['corpus']),'frontend_root':str(roots['frontend']),'evidence_root':str(roots['evidence'])}
    state=State(tmp_path/'sentinel')
    return state,probes,plan,receipt,current,candidate,roots


def test_unknown_is_not_healthy_and_session_expiry_is_explicit(tmp_path):
    values={'manager':{'pending':[]},'feed':{'snapshot_id':'s','consumed_seq':9,'source_latest_seq':10,'last_error':'','last_success':stamp()},
        'jst':{'authenticated':True,'checked_at':stamp(),'expires_at':stamp(100)},
        'worker':{'alive':True,'checked_at':'2026-01-01 00:00:00'}}
    probes=Probes([
        {'id':'manager','kind':'manager','url':'fixture'},
        {'id':'feed','kind':'feed','url':'fixture'},
        {'id':'jst','kind':'jst_session','url':'fixture'},
        {'id':'worker','kind':'worker','url':'fixture'},
        {'id':'backup','kind':'backup','path':str(tmp_path/'absent.json')},
    ],lambda spec:values[spec['id']])
    assert probes.read('manager')['status']=='unknown'
    assert probes.read('feed')['status']=='degraded'
    assert probes.read('jst')['reason']=='jst_session_expiring'
    values['jst']['authenticated']=False
    assert probes.read('jst')['status']=='failed'
    assert probes.read('worker')['status']=='unknown'
    assert probes.read('backup')['status']=='unknown'


def test_database_probe_is_readonly_and_disk_failure_is_recorded(tmp_path):
    path=tmp_path/'workspace.db'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE money(amount INTEGER)')
    before=path.read_bytes()
    probes=Probes([{'id':'db','kind':'sqlite','path':str(path)},
        {'id':'disk','kind':'disk','path':str(tmp_path),'min_free_bytes':10**30}])
    assert probes.read('db')['status']=='ok' and path.read_bytes()==before
    assert probes.read('disk')['status']=='failed'
    state=State(tmp_path/'sentinel');state.observe(probes.read('disk'));state.observe(probes.read('disk'))
    with state.connect() as db:
        assert db.execute('SELECT count(*) FROM notification').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM event').fetchone()[0]==1


def test_recovery_incidents_are_idempotent_but_a_new_outage_can_recover_again(tmp_path,command_script):
    current={'ok':False};calls=[]
    def runner(spec,environment):
        calls.append(spec);current['ok']=True
        return {'exit_code':0,'timed_out':False}
    config={'schema_version':1,'probes':[
        {'id':'service','kind':'http_json','url':'fixture','assertions':[{'field':'ok','value':True}],
         'recovery_action':'restart'},
        {'id':'disk','kind':'disk','path':str(tmp_path),'min_free_bytes':1}],
        'actions':[{'id':'restart','kind':'recovery','capability':'technical',
                    'preconditions':['disk'],'postconditions':['service'],'command':command_script('recover')}]}
    sentinel=Sentinel(tmp_path/'sentinel',config,fetch=lambda _:current.copy(),runner=runner)
    assert sentinel.once()['recoveries'][0]['state']=='succeeded'
    assert sentinel.once()['status']=='ok'
    current['ok']=False
    assert sentinel.once()['recoveries'][0]['state']=='succeeded'
    assert len(calls)==2


def test_failed_script_and_timeout_have_durable_failure_and_verified_rollback(tmp_path,command_script):
    atomic_json(tmp_path/'actual.json',{'ok':True})
    state=State(tmp_path/'sentinel');probes=Probes([
        {'id':'healthy','kind':'json','path':str(tmp_path/'actual.json'),'assertions':[{'field':'ok','value':True}]}])
    plan={'id':'repair','kind':'recovery','capability':'technical','preconditions':['healthy'],
        'postconditions':['healthy'],'command':command_script('fail')}
    executor=Executor(state,probes,[plan])
    failed=executor.execute('repair','failure-1')
    assert failed['state']=='failed' and failed['steps'][0]['exit_code']==23
    assert failed['receipt_sha']==digest({k:v for k,v in failed.items() if k!='receipt_sha'})
    plan['command']=command_script('timeout',.03);plan['rollback']=command_script('rollback');plan['rollback_conditions']=['healthy']
    rolled=Executor(state,probes,[plan]).execute('repair','timeout-1')
    assert rolled['state']=='rolled_back' and rolled['steps'][0]['timed_out']
    plan['rollback']=command_script('fail')
    failed_rollback=Executor(state,probes,[plan]).execute('repair','timeout-2')
    assert failed_rollback['state']=='rollback_failed' and failed_rollback['required_action']
    with state.connect() as db:assert db.execute('SELECT count(*) FROM notification').fetchone()[0]==3


def test_concurrent_recovery_is_leased_and_same_request_does_not_execute_twice(tmp_path,command_script):
    state=State(tmp_path/'sentinel');entered,released=threading.Event(),threading.Event();calls=[]
    probes=Probes([{'id':'disk','kind':'disk','path':str(tmp_path),'min_free_bytes':1}])
    plan={'id':'repair','kind':'recovery','capability':'technical','preconditions':['disk'],'postconditions':['disk'],'command':command_script('recover')}
    def runner(*_):
        calls.append(1);entered.set();assert released.wait(5)
        return {'exit_code':0,'timed_out':False}
    executor=Executor(state,probes,[plan],runner)
    with ThreadPoolExecutor(1) as pool:
        future=pool.submit(executor.execute,'repair','one')
        try:
            assert entered.wait(5)
            assert executor.execute('repair','one')['state']=='running'
            assert executor.execute('repair','two')['state']=='blocked'
        finally:released.set()
        assert future.result()['state']=='succeeded'
    assert len(calls)==1 and executor.execute('repair','one')['state']=='succeeded'


def test_executor_rejects_inline_shell_and_unconfigured_financial_actions(tmp_path,command_script):
    with pytest.raises(ValueError,match='Inline'):
        command({'argv':[sys.executable,'-c','print(1)'],'timeout_seconds':1},{})
    state=State(tmp_path/'sentinel');probes=Probes([])
    executor=Executor(state,probes,[{'id':'close','kind':'financial','capability':'technical'}])
    with pytest.raises(ValueError,match='Financial'):executor.execute('close','no')
    with pytest.raises(ValueError,match='configured'):executor.execute('unknown','no')


def test_deployment_requires_receipt_current_cas_and_real_artifacts(deployment,tmp_path):
    state,probes,plan,receipt,current,candidate,roots=deployment
    executor=Executor(state,probes,[plan])
    assert executor.execute('release','missing',candidate_version=candidate,expected_current=current)['state']=='failed'
    stale=executor.execute('release','stale',receipt=receipt,candidate_version=candidate,expected_current='f'*40)
    assert stale['state']=='blocked' and not (tmp_path/'calls').exists()
    (roots['corpus']/'golden.xlsx').write_bytes(b'changed')
    assert executor.execute('release','changed',receipt=receipt,candidate_version=candidate,expected_current=current)['state']=='failed'
    assert not (tmp_path/'calls').exists()


def test_readback_failure_rolls_back_and_success_publishes_after_verification(deployment,tmp_path,command_script):
    state,probes,plan,receipt,current,candidate,_roots=deployment
    plan['deploy']=command_script('bad_readback')
    failed=Executor(state,probes,[plan]).execute('release','bad-readback',receipt=receipt,candidate_version=candidate,expected_current=current)
    assert failed['state']=='rolled_back'
    assert json.loads((tmp_path/'current.json').read_text())['code_sha']==current
    assert json.loads((tmp_path/'actual.json').read_text())['version']==current
    plan['deploy']=command_script('deploy');executor=Executor(state,probes,[plan])
    ready=executor.execute('release','good',receipt=receipt,candidate_version=candidate,expected_current=current)
    assert ready['state']=='succeeded' and ready['backup_manifest_sha']
    assert ready['backup_restore_verification']['roles']==['application','model','registry','workspace']
    assert json.loads((tmp_path/'current.json').read_text())['code_sha']==candidate
    count=len((tmp_path/'calls').read_text().splitlines())
    assert executor.execute('release','good',receipt=receipt,candidate_version=candidate,expected_current=current)==ready
    assert len((tmp_path/'calls').read_text().splitlines())==count


def test_backup_corruption_stops_before_deployment_and_skipped_receipt_is_rejected(deployment,tmp_path):
    state,probes,plan,receipt,current,candidate,_roots=deployment
    def runner(spec,environment):
        result=command(spec,environment)
        (tmp_path/'backup/application.zip').write_bytes(b'corrupt backup')
        return result
    report=Executor(state,probes,[plan],runner).execute('release','bad-backup',receipt=receipt,candidate_version=candidate,expected_current=current)
    assert report['state']=='failed' and [s['name'] for s in report['steps']]==['backup']
    receipt['checks'][0]['skipped']=1;receipt['receipt_sha']=digest({k:v for k,v in receipt.items() if k!='receipt_sha'})
    report=Executor(state,probes,[plan]).execute('release','skip',receipt=receipt,candidate_version=candidate,expected_current=current)
    assert report['state']=='failed' and not report['steps']


@pytest.mark.parametrize('legacy_only', [True,False])
def test_financial_deployment_rejects_missing_roles_or_four_text_files(deployment,tmp_path,legacy_only):
    state,probes,plan,receipt,current,candidate,_roots=deployment
    def runner(spec,environment):
        result=command(spec,environment)
        folder=tmp_path/'backup';manifest=json.loads((folder/'manifest.json').read_text())
        if legacy_only:
            manifest['files']=[r for r in manifest['files'] if r['role']=='application']
        else:
            for record in manifest['files']:
                file=folder/record['path'];file.write_text('synthetic text is not a restored SQLite/archive')
                record['sha256']=file_sha(file)
        (folder/'manifest.json').write_text(json.dumps(manifest));return result
    report=Executor(state,probes,[plan],runner).execute('release','missing-or-fake-backup',
        receipt=receipt,candidate_version=candidate,expected_current=current)
    assert report['state']=='failed' and [s['name'] for s in report['steps']]==['backup']
    assert (tmp_path/'calls').read_text().splitlines()==['backup']


def test_configured_extra_backup_role_is_required_and_only_hash_checked(deployment,tmp_path):
    state,probes,plan,receipt,current,candidate,_roots=deployment
    plan['required_backup_roles']=['search_index']
    def runner(spec,environment):
        result=command(spec,environment)
        if 'backup' in spec['argv']:
            folder=tmp_path/'backup';index=folder/'search-index.bin';index.write_bytes(b'synthetic ancillary index')
            manifest=json.loads((folder/'manifest.json').read_text())
            manifest['files'].append({'role':'search_index','path':index.name,'sha256':file_sha(index)})
            (folder/'manifest.json').write_text(json.dumps(manifest))
        return result
    report=Executor(state,probes,[plan],runner).execute('release','extra-role',
        receipt=receipt,candidate_version=candidate,expected_current=current)
    assert report['state']=='succeeded'
    verified=report['backup_restore_verification']
    assert verified['restore_verified_roles']==['application','model','registry','workspace']
    assert verified['extra_roles_hash_checked']==['search_index']


def test_acceptance_rechecks_approved_csv_cas_and_delta_inputs(deployment,tmp_path):
    state,probes,plan,receipt,_current,candidate,_roots=deployment
    import importlib.util
    entry=Path(__file__).resolve().parents[3]/'tools/ledger_acceptance_inputs.py'
    spec=importlib.util.spec_from_file_location('sentinel_input_fixture',entry)
    tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(tool)
    root=tmp_path/'approved-source';root.mkdir()
    for name,data in [('cost.csv',b'synthetic,cost\nitem,2\n'),('a'*64,b'synthetic CAS original'),('deltas.json',b'[]')]:
        (root/name).write_bytes(data)
    roots={'approved':{'path':str(root),'kind':'explicit_files',
        'files':{p.name:file_sha(p) for p in root.iterdir()}}}
    receipt={**receipt,'input_roots':roots,'inputs':tool.inventory(roots)}
    receipt['input_sha']=digest(receipt['inputs']);receipt['receipt_sha']=digest({k:v for k,v in receipt.items() if k!='receipt_sha'})
    executor=Executor(state,probes,[plan])
    assert executor._accept(plan,receipt,candidate)==receipt['receipt_sha']
    (root/'deltas.json').write_text('[{"synthetic_new_delta":true}]')
    with pytest.raises(ValueError,match='input bytes changed'):executor._accept(plan,receipt,candidate)


def test_rollout_switches_do_not_count_as_two_observed_cycles(deployment,tmp_path):
    state,probes,plan,receipt,_current,candidate,_roots=deployment
    readiness=Readiness(state,Executor(state,probes,[plan]),minimum_seconds=60)
    assert readiness.assess('shadow',candidate,receipt['model_sha'])['missing_cycles']==1
    flag=readiness.promote('shadow','release',receipt,candidate)
    assert flag['shadow_enabled'] and flag['observation_claimed_by_switch'] is False
    with pytest.raises(ValueError,match='incomplete'):
        readiness.promote('canary','release',receipt,candidate)
    legacy=tmp_path/'legacy-observation.json'
    legacy.write_text(json.dumps({'schema':1,'phase':'shadow','sample_count':999,'manual_close':True}))
    with pytest.raises(ValueError,match='self-reported'):
        readiness.record(legacy)
    assert readiness.assess('shadow',candidate,receipt['model_sha'])['observed_periods']==[]
    with pytest.raises(ValueError,match='incomplete'):
        readiness.promote('production','release',receipt,candidate)


def test_readonly_api_reports_unobserved_state_and_does_not_create_database(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ledger.sentinel_api import install
    app=FastAPI();home=tmp_path/'not-started';install(app,home);client=TestClient(app)
    assert client.get('/api/operations/sentinel/status').json()['health']=='unknown'
    assert client.get('/api/operations/sentinel/notifications').json()['status']=='unknown'
    assert not home.exists()
    state=State(home);state.observe({'id':'worker','status':'unknown','reason':'missing','at':stamp(),'metrics':{}})
    assert client.get('/api/operations/sentinel/status').json()['health']=='attention'
    assert len(client.get('/api/operations/sentinel/notifications').json()['items'])==1
    assert client.post('/api/operations/sentinel/recover').status_code==404
    state.observe({'id':'worker','status':'ok','reason':'old','at':stamp(-3600),'metrics':{}})
    assert client.get('/api/operations/sentinel/status').json()['health']=='unknown'


def test_worker_last_seen_and_manager_dependency_are_explicit(tmp_path):
    values={'workers':{'workers':[{'alive':True,'status':'idle','last_seen':stamp()}]},'manager':{'pending':[]}}
    probes=Probes([
        {'id':'workers','kind':'worker','url':'fixture','field':'workers.0','timestamp_field':'last_seen'},
        {'id':'manager','kind':'manager','url':'fixture','worker_probe':'workers'},
    ],lambda spec:values[spec['id']])
    assert probes.read('workers')['status']=='ok'
    assert probes.read('manager')['status']=='ok'
    values['workers']['workers'][0]['last_seen']=stamp(-3600)
    assert probes.read('workers')['status']=='unknown' and probes.read('manager')['status']=='unknown'


def test_local_fault_injection_command_never_claims_production_observation(tmp_path):
    from ops.ledger_sentinel.drill import run
    folder=tmp_path/'new-drill';result=run(folder)
    assert result['passed'] and result['production_observed'] is False
    assert (folder/'drill-receipt.json').is_file()
    assert result['results']['timeout_rollback']['state']=='rolled_back'
    with pytest.raises(FileExistsError):run(folder)


def test_inputs_are_rechecked_after_backup_before_mutation(deployment,tmp_path):
    state,probes,plan,receipt,current,candidate,roots=deployment
    def runner(spec,environment):
        result=command(spec,environment)
        (roots['frontend']/'index.html').write_text('changed while backing up')
        return result
    result=Executor(state,probes,[plan],runner).execute('release','changed-after-backup',receipt=receipt,
        candidate_version=candidate,expected_current=current)
    assert result['state']=='failed'
    assert [s['name'] for s in result['steps']]==['backup']
    assert json.loads((tmp_path/'actual.json').read_text())['version']==current


def test_crash_checkpoint_is_not_reinterpreted_as_success(tmp_path):
    state=State(tmp_path/'sentinel');state.begin('crashed','deployment','a'*64)
    body={'request_id':'crashed','kind':'deployment','state':'running','started_at':stamp(-100),
        'session_id':'previous-process','config_sha':'a'*64,'mutation_started':True,
        'steps':[{'name':'backup','exit_code':0,'timed_out':False}]}
    state.checkpoint(body)
    recovered=state.reconcile_interrupted()
    assert len(recovered)==1 and recovered[0]['state']=='interrupted'
    assert recovered[0]['mutation_started'] and recovered[0]['steps'][0]['name']=='backup'
    assert state.reconcile_interrupted()==[]
