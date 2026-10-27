"""Fixed-argv recovery and receipt-bound deployment with readback and rollback."""
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import uuid

from .state import Busy, digest, now, SESSION
from .probes import backup_manifest, file_sha

FINANCIAL_BACKUP_ROLES=frozenset({'workspace','registry','application','model'})


def verify_financial_backup(path,version,max_age):
    """Use the protected stdlib restore verifier, never self-reported flags."""
    entry=Path(__file__).resolve().parents[2]/'tools/ledger-backup.py'
    spec=importlib.util.spec_from_file_location('ledger_sentinel_backup_verifier',entry)
    tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(tool)
    result=tool.verify(path,version,max_age=max_age,allow_extra_roles=True)
    if result.get('passed') is not True:raise ValueError('Financial restore verification did not pass')
    return result


def verify_accepted_inputs(receipt,corpus_root):
    entry=Path(__file__).resolve().parents[2]/'tools/ledger_acceptance_inputs.py'
    spec=importlib.util.spec_from_file_location('ledger_sentinel_accepted_inputs',entry)
    tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(tool)
    context=receipt
    if receipt.get('input_roots') is None:
        root=Path(corpus_root).resolve()
        if receipt.get('corpus_root') and Path(receipt['corpus_root']).resolve()!=root:
            raise ValueError('Legacy receipt corpus root differs from the configured acceptance scope')
        # Legacy receipts signed relative hashes; their root is supplied by the
        # already trusted plan, rather than mutating/resigning the receipt.
        context={**receipt,'corpus_root':str(root)}
    return tool.verify(context)


class StepFailed(RuntimeError):
    pass


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temporary.write_text(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False),encoding='utf-8')
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


def command(spec,environment):
    argv=spec.get('argv')
    if not isinstance(argv,list) or not argv or any(not isinstance(a,str) or '\x00' in a for a in argv):
        raise ValueError('A configured fixed argv is required')
    executable=Path(argv[0])
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError('Configured executable must be an existing absolute file')
    if any(a.lower() in {'-c','-command','/c','-encodedcommand','-enc'} for a in argv[1:]):
        raise ValueError('Inline shell/code execution is not permitted; configure a script file')
    timeout=spec.get('timeout_seconds')
    if not isinstance(timeout,(int,float)) or not 0<timeout<=3600:
        raise ValueError('Configured command requires a bounded timeout')
    started=time.monotonic()
    try:
        options={'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
        process=subprocess.Popen(argv,shell=False,cwd=spec.get('cwd'),env={**os.environ,**environment},
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,**options)
        try:
            stdout,stderr=process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name=='nt':
                killer=Path(os.environ['SystemRoot'])/'System32/taskkill.exe'
                subprocess.run([str(killer),'/PID',str(process.pid),'/T','/F'],shell=False,
                    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                    timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
            else:os.killpg(process.pid,signal.SIGKILL)
            process.communicate(timeout=10)
            raise
        # Command output may contain credentials. Persist hashes, not output bodies.
        import hashlib
        body={'exit_code':process.returncode,'timed_out':False,
            'stdout_sha256':hashlib.sha256(stdout).hexdigest(),
            'stderr_sha256':hashlib.sha256(stderr).hexdigest()}
    except subprocess.TimeoutExpired:
        body={'exit_code':None,'timed_out':True}
    return {**body,'duration_seconds':time.monotonic()-started,'command_sha':digest(argv)}


def verify_files(root,records,*,pattern=None):
    root=Path(root).resolve()
    if not records:raise ValueError('Artifact/input manifest is empty')
    seen=set()
    for row in records:
        path=(root/row['path']).resolve()
        if path in seen or not path.is_relative_to(root) or not path.is_file():
            raise ValueError('Artifact missing, duplicated or outside approved root')
        seen.add(path)
        if file_sha(path)!=row['sha256']:raise ValueError('Artifact changed after acceptance')
    if pattern is not None:
        actual={p.resolve() for p in root.rglob(pattern) if p.is_file() and not p.name.startswith('.')}
        if actual!=seen:raise ValueError('Accepted artifact/input inventory changed')


class Executor:
    def __init__(self,state,probes,plans,runner=command):
        self.state=state;self.probes=probes;self.plans={p['id']:p for p in plans};self.runner=runner
        if len(self.plans)!=len(plans):raise ValueError('Duplicate action IDs')

    def _step(self,name,spec,body,environment,lease):
        lease()
        result=self.runner(spec,environment)
        body['steps'].append({'name':name,**result})
        self.state.checkpoint(body)
        lease()
        if result['timed_out'] or result['exit_code']!=0:raise StepFailed('Configured step failed: '+name)

    def _accept(self,plan,receipt,version):
        from ledger.release_gate import validate
        if not isinstance(receipt,dict):raise ValueError('Acceptance receipt is required')
        if not re.fullmatch('[0-9a-f]{40}',version or ''):raise ValueError('Full candidate commit is required')
        model_root=Path(plan['model_root']).resolve()
        models=receipt.get('model_manifest')
        if not isinstance(models,list) or digest(models)!=receipt.get('model_sha'):
            raise ValueError('Accepted model manifest is missing or inconsistent')
        validate(receipt,version=version,model_sha=digest(models),evidence_root=Path(plan['evidence_root']))
        verify_files(model_root,models,pattern='*')
        verify_accepted_inputs(receipt,plan['corpus_root'])
        verify_files(plan['frontend_root'],receipt.get('frontend_artifacts'),pattern='*')
        return receipt['receipt_sha']

    def execute(self,action_id,request_id,*,receipt=None,candidate_version='',expected_current=''):
        if action_id not in self.plans:raise ValueError('Only configured operational actions may execute')
        plan=self.plans[action_id]
        if plan.get('kind') not in {'recovery','deployment'} or plan.get('capability')!='technical':
            raise ValueError('Financial policies, close/reopen and payouts are outside the operational executor')
        if not plan.get('preconditions') or not plan.get('postconditions'):
            raise ValueError('Recovery/deployment requires explicit preconditions and postconditions')
        identity=digest({'plan':plan,'receipt':receipt.get('receipt_sha') if isinstance(receipt,dict) else None,
            'candidate':candidate_version,'expected_current':expected_current})
        existing=self.state.begin(request_id,plan['kind'],identity,plan.get('lease_scope','ledger-maintenance'))
        if existing:return existing
        body={'request_id':request_id,'kind':plan['kind'],'state':'running','config_sha':identity,
            'action_id':action_id,'started_at':now(),'steps':[],'financial_write_authorized':False}
        body.update(session_id=SESSION,process_id=os.getpid(),lease_scope=plan.get('lease_scope','ledger-maintenance'),
                    candidate_version=candidate_version,expected_current=expected_current)
        mutated=False;old=None;old_bytes=None
        env={};variables={'candidate_version':candidate_version,'current_version':expected_current,
            'candidate_source_code_sha':receipt.get('source_code_sha') if isinstance(receipt,dict) else None}
        ttl=120+sum(float(plan.get(name,{}).get('timeout_seconds',0)) for name in ('command','backup','deploy','rollback'))
        try:
            with self.state.acquire(plan.get('lease_scope','ledger-maintenance'),ttl) as lease:
                if plan['kind']=='deployment':
                    body['acceptance_receipt_sha']=self._accept(plan,receipt,candidate_version)
                    if not re.fullmatch('[0-9a-f]{40}',expected_current or ''):
                        raise ValueError('Expected current commit is required for compare-and-swap')
                    current=Path(plan['current_state']);old_bytes=current.read_bytes();old=json.loads(old_bytes)
                    if old.get('code_sha')!=expected_current:raise Busy('Deployment compare-and-swap rejected stale current version')
                    body['before']=old
                    env={'LEDGER_CANDIDATE_CODE_SHA':candidate_version,'LEDGER_PREVIOUS_CODE_SHA':expected_current,
                         'LEDGER_ACCEPTANCE_RECEIPT_SHA':receipt['receipt_sha'],'LEDGER_BACKUP_MANIFEST':str(Path(plan['backup_manifest']).resolve())}
                self.probes.require(plan['preconditions'],variables)
                if plan['kind']=='deployment':
                    backup_started=time.time()
                    self._step('backup',plan['backup'],body,env,lease)
                    backup=backup_manifest(plan['backup_manifest'],expected_code=expected_current,not_before=backup_started-1,
                        max_age=plan.get('backup_max_age_seconds',3600))
                    roles=FINANCIAL_BACKUP_ROLES|set(plan.get('required_backup_roles') or [])
                    if not roles<={r.get('role') for r in backup['files']}:
                        raise ValueError('Backup does not cover every required application/database/model role')
                    body['backup_restore_verification']=verify_financial_backup(plan['backup_manifest'],expected_current,
                        plan.get('backup_max_age_seconds',3600))
                    body['backup_manifest_sha']=file_sha(plan['backup_manifest'])
                try:
                    if plan['kind']=='deployment':
                        if Path(plan['current_state']).read_bytes()!=old_bytes:raise Busy('Current deployment changed after backup')
                        # Recheck receipt evidence immediately before crossing the mutation boundary.
                        self._accept(plan,receipt,candidate_version)
                    mutated=True
                    body['mutation_started']=True
                    self.state.checkpoint(body)
                    self._step('deploy' if plan['kind']=='deployment' else 'recover',
                        plan['deploy'] if plan['kind']=='deployment' else plan['command'],body,env,lease)
                    body['readback']=self.probes.require(plan['postconditions'],variables)
                    if plan['kind']=='deployment':
                        lease()
                        if Path(plan['current_state']).read_bytes()!=old_bytes:raise Busy('Current deployment changed before publication')
                        atomic_json(plan['current_state'],{'code_sha':candidate_version,'previous_code_sha':expected_current,
                            'acceptance_receipt_sha':receipt['receipt_sha'],'request_id':request_id,'deployed_at':now()})
                    body['state']='succeeded'
                except Exception as failure:
                    body['failure_type']=type(failure).__name__;body['reason']=str(failure)
                    if mutated and plan.get('rollback'):
                        try:
                            self._step('rollback',plan['rollback'],body,env,lease)
                            body['rollback_readback']=self.probes.require(plan.get('rollback_conditions',[]),variables)
                            if old is not None and Path(plan['current_state']).read_bytes()!=old_bytes:
                                raise Busy('Deployment control pointer changed externally; do not overwrite it')
                            body['state']='rolled_back'
                        except Exception as rollback_failure:
                            body.update(state='rollback_failed',rollback_failure_type=type(rollback_failure).__name__,
                                required_action='恢复脚本或旧版本读回未通过；已升级为站内运维事件，需修复配置后重试')
                    else:
                        body.update(state='failed',required_action='已停止自动执行；没有可验证的固定回滚方案')
        except Exception as failure:
            body.update(state='blocked' if isinstance(failure,Busy) else 'failed',
                failure_type=type(failure).__name__,reason=str(failure),
                required_action='先恢复缺失的运维证据、版本或固定脚本；禁止跳过发布门禁')
        return self.state.finish(body)
