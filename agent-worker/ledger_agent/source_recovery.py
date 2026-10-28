"""Fixed production-host reader, sharing the existing case lease and heartbeat.

Only operator configuration supplies executable/module/storage locations. A model
can request a sealed task ID; it cannot supply a command, URL, path or amount.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from .commands import _lock

SCHEMA = 'jst_source_recovery.v1'
READER_SHA = '0c636672b044492c249572b7f3940eaa4cdf9c3f0d05b8761ac7ddd3929477b8'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def source_identity():
    root = Path(__file__).resolve().parent
    names = ('source_recovery.py', 'source_evaluate.py', 'upstream.py', 'worker.py', 'cli.py', 'jst_source_cli.mjs', 'jst_member_reader.mjs')
    return digest([(name, hashlib.sha256((root / name).read_bytes()).hexdigest()) for name in names])


def durable(path, value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str).encode()
    temporary = path.with_name('.' + path.name + '-' + str(time.time_ns()) + '.tmp')
    with temporary.open('xb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    temporary.chmod(0o600); os.replace(temporary, path)
    if os.name != 'nt':
        fd = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class Config:
    workspace_root: str
    node_binary: str
    jst_module: str
    jst_sha256: str
    enabled: bool = False
    profile_path: str = ''
    profile_sha256: str = ''
    evaluation: bool = False

    def __post_init__(self):
        if not all(Path(p).is_absolute() for p in (self.workspace_root, self.node_binary, self.jst_module)):
            raise ValueError('Source execution locations must be configured absolute paths')
        if not re.fullmatch(r'[0-9a-f]{64}', self.jst_sha256):
            raise ValueError('Pin the trusted JST client source identity')


class Executor:
    def __init__(self, config: Config):
        self.config = config
        self.root = Path(config.workspace_root).resolve()
        self.entry = Path(__file__).with_name('jst_source_cli.mjs')

    def configuration_sha(self):
        paths = [self.config.node_binary, self.config.jst_module]
        session=Path(self.config.jst_module).with_name('session.json')
        session_sha=hashlib.sha256(session.read_bytes()).hexdigest() if session.is_file() and session.stat().st_size<1024*1024 else None
        return digest({'config': asdict(self.config), 'source_identity': source_identity(),
                       'availability': [(p, Path(p).is_file()) for p in paths],'session_epoch_sha256':session_sha})

    def admitted(self):
        if not self.config.enabled:
            return False
        if self.config.evaluation:
            return True
        try:
            raw = Path(self.config.profile_path).read_bytes()
            profile = json.loads(raw)
            from .runtime import runtime_identity
            from .source_evaluate import CASES
            return (hashlib.sha256(raw).hexdigest() == self.config.profile_sha256
                    and profile.get('passed') is True and profile.get('source_sha') == source_identity()
                    and profile.get('runtime_sha') == runtime_identity()
                    and profile.get('inputs_unchanged') is True and profile.get('case_sha')==digest(CASES)
                    and profile.get('case_count', 0) >= 20 and profile.get('rounds', 0) >= 5
                    and profile.get('trials', 0) == profile.get('case_count', 0) * profile.get('rounds', 0)
                    and profile.get('trials', 0) >= 100 and profile.get('correct') == profile.get('trials') and profile.get('false_completions') == 0
                    and profile.get('sdk') == 'agentscope==2.0.11' and profile.get('live_provider') is True)
        except (OSError, ValueError, TypeError):
            return False

    def qualified_for(self,runtime_config):
        """Compatibility entry for enabling the deterministic read capability."""
        return self.admitted()

    def model_qualified_for(self,runtime_config):
        """A separate gate, used only when a case actually enters a model."""
        if self.config.evaluation:return self.admitted()
        if not self.admitted() or runtime_config is None:return False
        try:
            report=json.loads(Path(self.config.profile_path).read_text())
            selected=runtime_config.select(('evidence',))
            return (selected.name in report.get('models',[]) and report.get('provider_sha')==
                    hashlib.sha256(runtime_config.base_url.rstrip('/').encode()).hexdigest())
        except (OSError,ValueError,TypeError,AttributeError):return False

    def run(self, task, plan, *, job_id, input_sha, current=lambda: True):
        task_id = task.get('task_id', '')
        if not re.fullmatch(r'source-[0-9a-f]{40}', task_id) or not re.fullmatch(r'[0-9a-f]{32}', job_id):
            raise ValueError('Server generated source task and attempt identities are required')
        if (plan.get('schema') != SCHEMA or plan.get('input_sha') != input_sha
                or digest(plan) != task.get('plan_sha256') or plan.get('parser_sha256') != READER_SHA):
            raise ValueError('The sealed source plan identity changed')
        folder = self.root / 'source-recovery' / task_id / job_id
        if not folder.resolve().is_relative_to(self.root) or any(p.is_symlink() for p in (folder, folder.parent, folder.parent.parent)):
            raise ValueError('Source storage cannot be redirected')
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / 'receipt.json'
        source_before = source_identity()
        base = {'schema': SCHEMA, 'task_id': task_id, 'job_id': job_id,
                'plan_sha256': task['plan_sha256'], 'input_sha': input_sha,
                'parser_sha256': READER_SHA, 'collector_sha256': hashlib.sha256(self.entry.read_bytes()).hexdigest(),
                'state': 'pending', 'reason': None, 'nodes': [], 'relations': [],
                'financial_applied': False, 'business_complete': False}
        with _lock(folder / 'execution.lock') as locked:
            if not locked:
                raise ValueError('This source attempt is already executing')
            if path.is_symlink():
                raise ValueError('Source receipt cannot be redirected')
            if path.exists():
                receipt = json.loads(path.read_text())
                if any(receipt.get(k) != base[k] for k in ('task_id', 'job_id', 'input_sha', 'plan_sha256', 'collector_sha256')):
                    raise ValueError('Existing source receipt belongs to another attempt')
                return {'receipt_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'state': receipt['state'],
                        'reason': receipt.get('reason'), 'business_complete': False, 'financial_applied': False}
            reason = None
            if not self.admitted(): reason = 'source_executor_not_qualified'
            elif not current(): reason = 'source_lease_lost'
            elif not Path(self.config.node_binary).is_file(): reason = 'source_node_runtime_unavailable'
            elif not Path(self.config.jst_module).is_file(): reason = 'source_jst_module_unavailable'
            elif hashlib.sha256(Path(self.config.jst_module).read_bytes()).hexdigest() != self.config.jst_sha256:
                reason = 'source_jst_module_identity_changed'
            if reason:
                base['reason'] = reason; durable(path, base)
            else:
                request_file = folder / 'execution-request.json'
                durable(request_file, {'task_id': task_id, 'job_id': job_id, 'plan': plan,
                                      'plan_sha256': task['plan_sha256'], 'folder': str(folder),
                                      'jst_module': self.config.jst_module, 'jst_sha256': self.config.jst_sha256})
                # No environment-based Node preload or proxy can add a second,
                # unrecorded source implementation to this fixed operation.
                environment = {k: v for k, v in os.environ.items() if k not in {'NODE_OPTIONS', 'NODE_PATH'}}
                started = time.monotonic()
                try:
                    with (folder / 'stdout.log').open('xb') as stdout, (folder / 'stderr.log').open('xb') as stderr:
                        process = subprocess.Popen([self.config.node_binary, str(self.entry), str(request_file)],
                                                   stdout=stdout, stderr=stderr, env=environment,
                                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                        while process.poll() is None:
                            if not current() or time.monotonic() - started > 125:
                                process.kill(); process.wait(); reason = 'source_lease_or_time_budget'; break
                            time.sleep(.1)
                        if process.returncode and reason is None: reason = 'source_process_failed'
                except OSError:
                    reason = 'source_runtime_unavailable'
                if source_identity() != source_before:
                    reason = 'source_executor_identity_changed'
                if reason or not path.exists():
                    if path.exists():
                        preserved = folder / ('interrupted-receipt-' + str(time.time_ns()) + '.json')
                        path.rename(preserved)
                    base['reason'] = reason or 'source_receipt_missing'; durable(path, base)
        receipt = json.loads(path.read_text())
        return {'receipt_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'state': receipt.get('state'),
                'reason': receipt.get('reason'), 'financial_applied': False, 'business_complete': False}
