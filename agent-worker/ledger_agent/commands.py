"""Configured local/production operations with persistent, auditable receipts."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import uuid


def _sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def _write(path,value):
    temporary=path.with_name('.'+path.name+'-'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,sort_keys=True),encoding='utf-8')
    temporary.chmod(0o600)
    os.replace(temporary,path)


@contextmanager
def _lock(path):
    stream=path.open('a+b')
    if not path.stat().st_size:stream.write(b'0');stream.flush()
    locked=False
    try:
        stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            locked=True
        except (OSError,BlockingIOError):pass
        yield locked
    finally:
        if locked:
            stream.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
        stream.close()


class Catalog:
    def __init__(self,definitions,root:Path):
        self.definitions,self.root=definitions,root.resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.root.chmod(0o700)

    def tools(self,checker):
        return {name:self.bind(name,checker) for name in self.definitions}

    @staticmethod
    def _argv(argv):
        if (not isinstance(argv,list) or not argv or not Path(argv[0]).is_absolute()
            or not Path(argv[0]).is_file() or any(not isinstance(arg,str) or '\x00' in arg for arg in argv)
            or any(arg.lower() in {'-c','/c','-command','-encodedcommand','-enc'} for arg in argv[1:])):
            raise ValueError('Use a configured executable and script file, not inline code')
        return argv

    def _launch(self,folder,case,evidence,argv,spec,operation_id,mode):
        attempt=folder/('attempt-'+uuid.uuid4().hex)
        attempt.mkdir(mode=0o700)
        request,result=attempt/'case.json',attempt/'result.json'
        _write(request,{'case':case,'evidence':evidence,'operation_id':operation_id,
            'idempotency_key':operation_id,'mode':mode})
        environment={**os.environ,'LEDGER_AGENT_CASE_FILE':str(request),
            'LEDGER_AGENT_RESULT_FILE':str(result),'LEDGER_AGENT_OPERATION_ID':operation_id,
            'LEDGER_AGENT_OPERATION_MODE':mode}
        timeout=float(spec.get('timeout_seconds',30))
        if not math.isfinite(timeout) or timeout<=0:raise ValueError('Configure a finite operation timeout')
        options={'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
        receipt={'operation_id':operation_id,'state':'running','mode':mode,
            'input_sha':case['input_sha'],'attempt':attempt.name}
        _write(folder/'receipt.json',receipt)
        with (attempt/'stdout').open('wb') as stdout,(attempt/'stderr').open('wb') as stderr:
            process=subprocess.Popen(argv,shell=False,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,
                cwd=spec.get('cwd'),env=environment,**options)
            receipt['pid']=process.pid;_write(folder/'receipt.json',receipt)
            try:
                code=process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                if os.name=='nt':
                    subprocess.run([str(Path(os.environ['SystemRoot'])/'System32/taskkill.exe'),
                        '/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                        timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    try:os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                process.wait(timeout=10)
                _write(folder/'receipt.json',{**receipt,'state':'unknown_outcome','reason':'timeout'})
                raise
        if code!=0 or not result.is_file() or result.is_symlink() or result.stat().st_size>2*1024*1024:
            _write(folder/'receipt.json',{**receipt,'state':'unknown_outcome','reason':'missing_result','exit_code':code})
            raise ValueError('Operation failed; reconcile its receipt before another execution')
        try:payload=json.loads(result.read_text(encoding='utf-8'))
        except (ValueError,OSError):
            _write(folder/'receipt.json',{**receipt,'state':'unknown_outcome','reason':'unreadable_result'})
            raise ValueError('Operation outcome is unreadable; reconcile before another execution')
        if not isinstance(payload,dict) or payload.get('input_sha')!=case['input_sha']:
            _write(folder/'receipt.json',{**receipt,'state':'unknown_outcome','reason':'unbound_result'})
            raise ValueError('Operation output is not bound to captured inputs')
        return payload,result,receipt

    def bind(self,name,checker):
        def execute(case,evidence):
            spec=self.definitions[name]
            effect=spec.get('effect','read')
            if effect not in {'read','evidence_publish','ops_repair'} or effect=='ops_repair' and case.get('scope')!='ops':
                raise PermissionError('Operation effect is outside this worker scope')
            fresh=checker()
            if fresh.get('lease_active') is False:raise PermissionError('Worker lease is no longer active')
            argv=self._argv(spec.get('argv',[]))
            hashes={arg:_sha(arg) for arg in argv[1:] if Path(arg).is_absolute() and Path(arg).is_file()
                    and Path(arg).suffix.lower() in {'.py','.ps1','.mjs','.js','.sh','.bat'}}
            expected=spec.get('script_sha256')
            if expected and expected not in hashes.values():raise ValueError('Configured operation script changed')
            identity={'case':case['id'],'input_sha':case['input_sha'],'operation':name,'spec':spec,'scripts':hashes}
            operation_id=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
            folder=self.root/operation_id
            folder.mkdir(mode=0o700,exist_ok=True)
            receipt_path=folder/'receipt.json'
            with _lock(folder/'operation.lock') as locked:
                if not locked:return {'input_sha':case['input_sha'],'operation_id':operation_id,'state':'running'}
                receipt=json.loads(receipt_path.read_text()) if receipt_path.is_file() else None
                if receipt and receipt.get('state')=='executed':
                    path=(folder/receipt['result_path']).resolve()
                    if not path.is_relative_to(folder) or not path.is_file() or _sha(path)!=receipt['result_sha']:
                        raise ValueError('Cached operation evidence failed integrity verification')
                    return {**json.loads(path.read_text()),'operation_id':operation_id,
                        'artifact_refs':[str(receipt_path)],'replayed':True}
                if fresh.get('input_sha')!=case['input_sha']:
                    raise ValueError('Case inputs changed before operation execution')
                if receipt and receipt.get('state') in {'running','unknown_outcome'}:
                    reconcile=spec.get('reconcile_argv')
                    if not reconcile:
                        return {'input_sha':case['input_sha'],'operation_id':operation_id,
                            'state':'unknown_outcome','artifact_refs':[str(receipt_path)]}
                    payload,result,record=self._launch(folder,case,evidence,self._argv(reconcile),spec,operation_id,'reconcile')
                    outcome=payload.get('operation_state')
                    if outcome=='applied':
                        _write(receipt_path,{**record,'state':'executed','result_path':result.relative_to(folder).as_posix(),'result_sha':_sha(result)})
                        return {**payload,'operation_id':operation_id,'artifact_refs':[str(receipt_path)]}
                    if outcome!='not_applied':
                        _write(receipt_path,{**record,'state':'unknown_outcome'})
                        return {**payload,'operation_id':operation_id,'artifact_refs':[str(receipt_path)]}
                payload,result,record=self._launch(folder,case,evidence,argv,spec,operation_id,'execute')
                _write(receipt_path,{**record,'state':'executed','result_path':result.relative_to(folder).as_posix(),
                    'result_sha':_sha(result),'script_hashes':hashes,'effect':effect})
                return {**payload,'operation_id':operation_id,'artifact_refs':[str(receipt_path)]}
        return execute
