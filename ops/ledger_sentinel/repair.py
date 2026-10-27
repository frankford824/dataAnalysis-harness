"""Scoped AgentScope repair tools, isolated execution and reviewable PR artifacts.

The model receives only sanitized incidents and code tools. Real corpus and full
acceptance output are mounted only in the separate, networkless trusted stage.
No GitHub request or merge is performed by this module.
"""
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid
import xml.etree.ElementTree as ET

from .executor import atomic_json, command
from .probes import file_sha
from .state import State, digest, now, Busy

CONTROL_PATHS=('models/','ledger/tests/baseline/','ledger/tests/conftest.py','ledger/tools/',
    'ledger/ledger/release_gate.py','ledger/ledger/replay', 'tools/', 'ops/', 'agent-worker/', 'host-agent/',
    'ledger/ledger/api.py','ledger/ledger/write_auth.py','ledger/ledger/worker_control_api.py',
    'ledger/ledger/automation','ledger/ledger/workspace.py','ledger/ledger/finance_guard.py',
    'ledger/ledger/historical_close.py','ledger/ledger/commission_','ledger/ledger/labor_api.py',
    'ledger/ledger/cost_','ledger/ledger/manual_cost.py','ledger/ledger/statement_review.py',
    'ledger/ledger/upstream_history.py','ledger/ledger/storage_integrity.py',
    'ledger/ledger/version.py','ledger/ledger/snapshot_store.py','ledger/ledger/run_artifacts.py',
    'ledger/ledger/operations_income.py','ledger/ledger/__init__.py',
    'ledger/ledger/recompute_reuse.py','ledger/ledger/service.py','ledger/ledger/model/',
    'ledger/ledger/engine/cost_policy.py','ledger/ledger/sentinel_api.py',
    'ledger/web/vite.config','ledger/web/package.json','ledger/web/pnpm-lock.yaml')
CODE_SUFFIXES={'.py','.js','.mjs','.ts','.vue','.css','.html'}
REGRESSION=re.compile(r'ledger/tests/regressions/test_[A-Za-z0-9_]+\.py')


def protected_path(relative):
    folded=relative.casefold()
    return (any(folded==prefix.casefold() or folded.startswith(prefix.casefold()) for prefix in CONTROL_PATHS)
            or folded.startswith('ledger/ledger/') and folded.endswith('_api.py'))


def is_test_path(relative):
    parts=Path(relative).parts
    name=parts[-1] if parts else ''
    return (any(p.casefold() in {'tests','test'} for p in parts[:-1]) or name.startswith('test_')
            or '.test.' in name or '.spec.' in name)


def sanitized_event(event,base_sha):
    def identifier(value,default):
        text=str(value or '')
        return text if re.fullmatch('[A-Za-z_][A-Za-z0-9_.:-]{0,127}',text) else default
    metrics={}
    for name in ('pending','errors','lag','free_bytes','total_bytes','age_seconds'):
        value=(event.get('metrics') or {}).get(name)
        if not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value):metrics[name]=value
    return {'schema':1,'incident_sha':digest(event),'base_code_sha':base_sha,
        'probe_id':identifier(event.get('scope') or event.get('id'),'unknown_probe'),
        'status':event.get('status') if event.get('status') in {'unknown','failed','degraded','interrupted','rollback_failed'} else 'unknown',
        'reason_code':identifier(event.get('reason'),'unclassified_failure'),
        'error_type':identifier(event.get('error_type'),'UnknownError'),'metrics':metrics,
        'raw_logs_included':False,'business_rows_included':False}


class Repair:
    def __init__(self,repo,home,config,*,which=shutil.which,runner=command,oracle=None):
        self.repo=Path(repo).resolve();self.home=Path(home).resolve();self.config=config
        if self.home.is_relative_to(self.repo):raise ValueError('Repair state/worktrees must be outside the source checkout')
        self.state=State(self.home);self.which=which;self.runner=runner
        # Only the trusted host may inject an independent financial verifier.
        # A path/boolean in model-visible JSON can never enable this authority.
        self.oracle=oracle
        git=shutil.which('git')
        if not git:raise ValueError('Git is required for isolated repair worktrees')
        self.git=str(Path(git).resolve())

    def _git(self,root,*args):
        return subprocess.check_output([self.git,'-c','core.hooksPath='+str(self.home/'disabled-hooks'),
            '-c','commit.gpgsign=false','-C',str(root),*args],stderr=subprocess.PIPE)

    def source_identity(self):
        tracked=self._git(self.repo,'diff','HEAD','--binary')
        others=self._git(self.repo,'ls-files','--others','--exclude-standard','-z').decode().split('\0')
        return digest({'head':self._git(self.repo,'rev-parse','HEAD').decode().strip(),
            'diff_sha':digest(tracked.hex()),'untracked':[(p,file_sha(self.repo/p)) for p in others if p and (self.repo/p).is_file()]})

    def path(self,identifier):
        if not re.fullmatch('[0-9a-f]{32}',identifier or ''):raise ValueError('Invalid repair ID')
        return self.home/'repairs'/identifier

    def _read(self,identifier):
        path=self.path(identifier);body=json.loads((path/'repair.json').read_text(encoding='utf-8'))
        tree=Path(body['worktree']).resolve()
        if not tree.is_relative_to(self.home/'worktrees') or not (tree/'.git').is_file() or (tree/'.git').is_symlink():
            raise ValueError('Worktree identity is invalid')
        if file_sha(tree/'.git')!=body['git_pointer_sha']:raise ValueError('Worktree Git pointer changed')
        return path,tree,body

    def prepare(self,event,base_sha):
        if not re.fullmatch('[0-9a-f]{40}',base_sha or ''):raise ValueError('Repair requires a full base commit SHA')
        resolved=self._git(self.repo,'rev-parse',base_sha+'^{commit}').decode().strip()
        if resolved!=base_sha:raise ValueError('Repair base identity changed')
        identifier=uuid.uuid4().hex;path=self.path(identifier);path.mkdir(parents=True)
        tree=self.home/'worktrees'/identifier;tree.parent.mkdir(parents=True,exist_ok=True)
        before=self.source_identity()
        (self.home/'disabled-hooks').mkdir(exist_ok=True)
        self._git(self.repo,'worktree','add','--detach',str(tree),base_sha)
        atomic_json(path/'event.json',sanitized_event(event,base_sha))
        body={'schema':1,'repair_id':identifier,'phase':'editing','base_code_sha':base_sha,
            'worktree':str(tree),'git_pointer_sha':file_sha(tree/'.git'),'source_identity':before,
            'config_sha':digest(self.config),'created_at':now(),'raw_corpus_exposed_to_model':False,
            'baseline_test_paths':[p for p in self._git(tree,'ls-tree','-r','--name-only',base_sha).decode().splitlines() if is_test_path(p)]}
        atomic_json(path/'repair.json',body)
        if self.source_identity()!=before:raise ValueError('Source checkout changed while preparing repair')
        return body

    def _code_path(self,tree,relative,*,write=False,baseline_tests=()):
        if not isinstance(relative,str) or '\\' in relative:raise ValueError('Use a relative POSIX code path')
        path=(tree/relative).resolve()
        if not path.is_relative_to(tree) or relative.startswith('/') or path.suffix not in CODE_SUFFIXES:
            raise ValueError('Repair path is not scoped code')
        if relative!=path.relative_to(tree).as_posix():
            raise ValueError('Canonical relative code paths are required; aliases cannot bypass controls')
        if protected_path(relative):
            raise ValueError('Acceptance controls, financial policy and baseline paths are protected')
        is_test=is_test_path(relative)
        if is_test and write:
            if relative in baseline_tests:
                raise ValueError('Existing acceptance tests are immutable; add an isolated new counterexample')
            if not REGRESSION.fullmatch(relative):
                raise ValueError('Only new tests/regressions/test_*.py counterexamples may be added')
        prefixes=self.config.get('code_prefixes',['ledger/ledger/'])
        if not is_test and not any(relative.startswith(prefix) for prefix in prefixes):raise ValueError('Repair path is outside approved code prefixes')
        if any(part.startswith('.') or part.casefold() in {'data','secrets','corpus','models','baseline','node_modules'}
               for part in Path(relative).parts):raise ValueError('Data and credentials are not model code tools')
        if (tree/relative).is_symlink():raise ValueError('Symlink code edits are not permitted')
        return path

    def read_code(self,identifier,relative):
        _path,tree,body=self._read(identifier)
        if body['phase']!='editing':raise ValueError('Frozen candidate code cannot return trusted-stage data to a model')
        path=self._code_path(tree,relative)
        if path.stat().st_size>200000:raise ValueError('Code read exceeds the bounded model context')
        return path.read_text(encoding='utf-8')

    def list_code(self,identifier,prefix='',offset=0,limit=100):
        """Bounded approved code inventory; no corpus, controls or raw logs."""
        path,tree,body=self._read(identifier)
        if body['phase']!='editing':raise ValueError('Frozen candidate inventory cannot return trusted-stage data to a model')
        if (not isinstance(prefix,str) or '\\' in prefix or prefix.startswith('/')
            or any(p in {'.','..'} for p in prefix.split('/') if p)):
            raise ValueError('Use an approved relative code prefix')
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
            raise ValueError('Use bounded code inventory pagination')
        event=json.loads((path/'event.json').read_text(encoding='utf-8'))
        configured=self.config.get('code_entry_points',[])
        if isinstance(configured,dict):configured=configured.get(event['probe_id'],configured.get('*',[]))
        if not isinstance(configured,list) or any(not isinstance(v,str) for v in configured):
            raise ValueError('Trusted code entry points must be relative paths')
        entries=[]
        for relative in configured:
            candidate=self._code_path(tree,relative)
            if not candidate.is_file():raise ValueError('Configured code entry point is missing')
            entries.append(relative)
        tracked=self._git(tree,'ls-files','-z').decode().split('\0')
        untracked=self._git(tree,'ls-files','--others','--exclude-standard','-z').decode().split('\0')
        files=[];prefixes=self.config.get('code_prefixes',['ledger/ledger/'])
        for relative in sorted(set(filter(None,[*tracked,*untracked,*entries]))):
            if not (any(relative.startswith(p) for p in prefixes) or relative in entries or REGRESSION.fullmatch(relative)):
                continue
            try:candidate=self._code_path(tree,relative)
            except ValueError:continue
            if not candidate.is_file() or candidate.stat().st_size>200000:continue
            try:
                self._code_path(tree,relative,write=True,baseline_tests=body['baseline_test_paths'])
                writable=True
            except ValueError:writable=False
            files.append({'path':relative,'sha256':file_sha(candidate),'bytes':candidate.stat().st_size,'writable':writable})
        visible=[row for row in files if row['path'].startswith(prefix)]
        checks=[{'id':row['id']} for row in self.config.get('checks',[])
                if isinstance(row.get('id'),str) and re.fullmatch('[A-Za-z_][A-Za-z0-9_.:-]{0,127}',row['id'])]
        return {'state':'available','files':visible[offset:offset+limit],'checks':checks,
            'entry_points':sorted(set(entries)),'manifest_sha':digest(files),'offset':offset,
            'total':len(visible),'more':offset+limit<len(visible),'code_only':True}

    def write_code(self,identifier,relative,text):
        with self.state.acquire('repair-'+identifier,30):
            return self._write_code(identifier,relative,text)

    def _write_code(self,identifier,relative,text):
        path,tree,body=self._read(identifier)
        if body['phase']!='editing':raise ValueError('Repair is frozen; model edits are closed')
        target=self._code_path(tree,relative,write=True,baseline_tests=body['baseline_test_paths'])
        if not isinstance(text,str) or len(text.encode())>1000000:raise ValueError('Code edit exceeds the scoped patch size')
        target.parent.mkdir(parents=True,exist_ok=True);target.write_text(text,encoding='utf-8')
        return {'path':relative,'sha256':file_sha(target)}

    def validate_patch(self,identifier):
        _path,tree,body=self._read(identifier)
        if self.source_identity()!=body['source_identity']:raise ValueError('Original checkout changed; do not overwrite or promote it')
        changed=self._git(tree,'diff','--name-only','--no-renames',body['base_code_sha']).decode().splitlines()
        changed+=filter(None,self._git(tree,'ls-files','--others','--exclude-standard').decode().splitlines())
        changed=sorted(set(changed))
        if not changed:raise ValueError('Repair produced no code patch')
        if len(changed)>self.config.get('max_files',20):raise ValueError('Patch scope exceeds the approved file count')
        for relative in changed:
            self._code_path(tree,relative,write=True,baseline_tests=body['baseline_test_paths'])
        raw=self._git(tree,'diff','--raw','--no-renames',body['base_code_sha']).decode()
        if re.search(r'\b120000\b',raw):raise ValueError('Symlink changes are prohibited')
        return changed

    def _model_view(self,path,tree,spec,body):
        """Project approved code only, rather than expose a read-only checkout."""
        view=path/'model-views'/uuid.uuid4().hex;view.mkdir(parents=True)
        tests=set(self.config.get('model_test_paths',[]))
        tests.update(value.removeprefix('/work/').split('::',1)[0] for value in spec.get('argv',[])
            if isinstance(value,str) and value.removeprefix('/work/').split('::',1)[0].endswith('.py')
            and is_test_path(value.removeprefix('/work/').split('::',1)[0]))
        files=set(filter(None,self._git(tree,'ls-files','-z').decode().split('\0')))
        files.update(filter(None,self._git(tree,'ls-files','--others','--exclude-standard','-z').decode().split('\0')))
        total=0;manifest=[]
        for relative in sorted(files):
            if is_test_path(relative) and relative not in tests:continue
            try:source=self._code_path(tree,relative)
            except ValueError:continue
            if not source.is_file():continue
            size=source.stat().st_size;total+=size
            if size>1000000 or total>20000000:raise ValueError('Model code projection exceeds its bounded scope')
            target=view/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
            manifest.append({'path':relative,'sha256':file_sha(target)})
        for relative in tests:
            if not (view/relative).is_file():raise ValueError('Configured model test needs an explicitly allowed code-only fixture')
        atomic_json(path/('model-view-'+view.name+'.json'),{'files':manifest,'code_only':True})
        return view

    def _sandbox(self,identifier,spec,*,trusted=False,output=None):
        path,tree,body=self._read(identifier)
        output=Path(output or path/'checks'/uuid.uuid4().hex);output.mkdir(parents=True,exist_ok=False)
        if trusted:
            return {'state':'blocked','reason':'legacy_inprocess_financial_sandbox_disabled',
                'host_fallback':False,'output':str(output)}
        bwrap=self.which('bwrap');docker=self.which('docker')
        image=self.config.get('docker_image','')
        if not bwrap and not (docker and re.fullmatch(r'[A-Za-z0-9._/:\-]+@sha256:[0-9a-f]{64}',image)):
            return {'state':'blocked','reason':'network_isolation_unavailable','host_fallback':False,'output':str(output)}
        work=tree if trusted else self._model_view(path,tree,spec,body)
        mounts=[(work,'/work',not trusted),(output,'/out',False)]
        protected=[Path(p).resolve() for p in self.config.get('protected_data_roots',[])]
        protected += [self.home/'repairs',self.home/'sentinel.db']
        if self.config.get('corpus_root'):protected.append(Path(self.config['corpus_root']).resolve())
        fixture=self.config.get('sanitized_fixture_root')
        if fixture:
            fixture=Path(fixture).resolve()
            if any(fixture.is_relative_to(p) or p.is_relative_to(fixture) for p in protected):
                raise ValueError('Reproduction fixture would expose protected data')
            manifest=json.loads((fixture/'sanitized-fixture.json').read_text(encoding='utf-8'))
            if manifest.get('kind')!='synthetic_reproduction' or manifest.get('business_rows') is not False:
                raise ValueError('Only declared synthetic reproduction fixtures are model-visible')
            records=manifest.get('files')
            if not isinstance(records,list) or not records or len(records)>1000:raise ValueError('Synthetic fixture needs a bounded explicit inventory')
            projected=path/'fixture-views'/uuid.uuid4().hex;projected.mkdir(parents=True)
            seen=set();total=0
            for record in records:
                relative=record['path']
                if (not isinstance(relative,str) or '\\' in relative or Path(relative).is_absolute()
                        or any(p in {'.','..'} or p.startswith('.') for p in relative.split('/'))):
                    raise ValueError('Synthetic fixture paths must be canonical relative files')
                source=fixture/relative;file=source.resolve()
                if (relative in seen or source.is_symlink() or not file.is_relative_to(fixture)
                        or file.relative_to(fixture).as_posix()!=relative or not file.is_file() or file_sha(file)!=record['sha256']):
                    raise ValueError('Synthetic fixture identity changed')
                seen.add(relative);total+=file.stat().st_size
                if total>20000000:raise ValueError('Synthetic fixture exceeds its bounded model-visible scope')
                target=projected/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(file,target)
                if file_sha(target)!=record['sha256']:raise ValueError('Synthetic fixture changed during projection')
            mounts.append((projected,'/fixtures',True))
        for mount in self.config.get('runtime_mounts',[]):
            source=Path(mount['source']).resolve()
            target=mount['target']
            if not re.fullmatch(r'/runtime(?:/[A-Za-z0-9_-]+)*',target):raise ValueError('Runtime mounts must stay in the dedicated runtime namespace')
            if source==tree or tree.is_relative_to(source) or source==self.repo or self.repo.is_relative_to(source):
                raise ValueError('Runtime cannot mount the source checkout or its ancestors')
            if ((source.is_relative_to(self.repo) or source.is_relative_to(tree))
                    and not (source/'pyvenv.cfg').is_file()):
                raise ValueError('Checkout runtime mounts must be isolated dependency virtual environments')
            if any(source.is_relative_to(p) or p.is_relative_to(source) for p in protected):raise ValueError('A model check runtime mount would expose protected data')
            mounts.append((source,target,True))
        if bwrap:
            argv=[str(Path(bwrap).resolve()),'--unshare-all','--unshare-net','--die-with-parent','--new-session','--clearenv',
                '--setenv','PATH','/usr/bin:/bin','--setenv','HOME','/tmp',
                '--setenv','PYTHONDONTWRITEBYTECODE','1','--setenv','GIT_OPTIONAL_LOCKS','0',
                '--setenv','PYTHONPATH','/work/ledger:/work',
                '--setenv','GIT_CONFIG_COUNT','1','--setenv','GIT_CONFIG_KEY_0','safe.directory',
                '--setenv','GIT_CONFIG_VALUE_0','/work','--proc','/proc','--dev','/dev','--tmpfs','/tmp']
            for system in ('/usr','/bin','/lib','/lib64'):
                if Path(system).exists():argv+=['--ro-bind',system,system]
            for source,target,readonly in mounts:
                argv+=['--ro-bind' if readonly else '--bind',str(source),target]
            argv+=['--chdir','/work','--',*spec['argv']]
        elif docker and re.fullmatch(r'[A-Za-z0-9._/:\-]+@sha256:[0-9a-f]{64}',image):
            argv=[str(Path(docker).resolve()),'run','--rm','--network','none','--cap-drop','ALL',
                '--security-opt','no-new-privileges','--read-only','--tmpfs','/tmp','--workdir','/work',
                '--env','HOME=/tmp','--env','PYTHONDONTWRITEBYTECODE=1','--env','GIT_OPTIONAL_LOCKS=0',
                '--env','PYTHONPATH=/work/ledger:/work',
                '--env','GIT_CONFIG_COUNT=1','--env','GIT_CONFIG_KEY_0=safe.directory','--env','GIT_CONFIG_VALUE_0=/work']
            for source,target,readonly in mounts:
                if ',' in str(source) or ',' in target:raise ValueError('Invalid container mount')
                argv+=['--mount','type=bind,src='+str(source)+',dst='+target+(',readonly' if readonly else '')]
            argv += [image,*spec['argv']]
        else:
            return {'state':'blocked','reason':'network_isolation_unavailable','host_fallback':False,'output':str(output)}
        result=self.runner({'argv':argv,'timeout_seconds':spec.get('timeout_seconds',300)}, {})
        return {'state':'executed','network':'isolated','host_fallback':False,'output':str(output),**result}

    def run_check(self,identifier,check_id):
        path,_tree,body=self._read(identifier)
        if body['phase']!='editing':raise ValueError('Model checks close when trusted acceptance starts')
        checks={r['id']:r for r in self.config.get('checks',[])}
        if check_id not in checks:raise ValueError('Only configured check IDs may execute')
        result=self._sandbox(identifier,checks[check_id])
        if result['state']=='executed':
            report=Path(result['output'])/checks[check_id].get('report','')
            if not report.resolve().is_relative_to(Path(result['output']).resolve()) or not report.is_file() or report.stat().st_size>4*1024*1024:
                result.update(state='blocked',reason='sandbox_or_test_report_unverified')
            else:
                root=ET.parse(report).getroot();suites=[root] if root.tag=='testsuite' else root.findall('testsuite')
                stats={key:sum(int(s.get(key,0)) for s in suites) for key in ('tests','failures','errors','skipped')}
                result.update(stats=stats,state='reported_pass' if result['exit_code']==0 and not result['timed_out']
                    and stats['tests']>0 and not any(stats[k] for k in ('failures','errors','skipped')) else 'reported_failure')
        result.update(authoritative=False,report_authority='untrusted_candidate_diagnostic')
        # The model sees no raw stdout/stderr or corpus. Report hashes are retained privately.
        safe={k:result[k] for k in ('state','reason','network','host_fallback','authoritative','report_authority') if k in result}
        atomic_json(path/('check-'+digest(check_id)[:12]+'.json'),result)
        return safe

    def model_tools(self,identifier):
        """Register ONLY these callables in the AgentScope ops toolkit, no host shell."""
        def safe(operation):
            try:return operation()
            except Exception as exc:
                return {'state':'blocked','reason':'scoped_repair_tool_rejected','error_type':type(exc).__name__}
        def read_code(relative: str) -> dict:
            """Read one approved relative code file; data, gates and logs are inaccessible."""
            return safe(lambda:{'path':relative,'code':self.read_code(identifier,relative)})
        def list_code(prefix: str='',offset: int=0,limit: int=100) -> dict:
            """List bounded approved source paths and trusted configured entry points."""
            return safe(lambda:self.list_code(identifier,prefix,offset,limit))
        def write_code(relative: str,text: str) -> dict:
            """Replace one approved code file inside the unfrozen isolated worktree."""
            return safe(lambda:self.write_code(identifier,relative,text))
        def run_check(check_id: str) -> dict:
            """Run one configured test ID in a network-isolated sandbox, never on host."""
            return safe(lambda:self.run_check(identifier,check_id))
        return {'list_code':list_code,'read_code':read_code,'write_code':write_code,'run_check':run_check}

    def freeze_patch(self,identifier):
        with self.state.acquire('repair-'+identifier,120):
            return self._freeze_patch(identifier)

    def _freeze_patch(self,identifier):
        path,tree,body=self._read(identifier)
        if body['phase']!='editing':raise ValueError('Repair already frozen')
        files=self.validate_patch(identifier)
        self._git(tree,'add','--all','--',*files)
        patch=self._git(tree,'diff','--cached','--binary')
        (path/'proposal.patch').write_bytes(patch)
        self._git(tree,'-c','user.name=Ledger Sentinel','-c','user.email=sentinel@localhost',
            'commit','-m','codex: scoped operational repair '+identifier)
        version=self._git(tree,'rev-parse','HEAD').decode().strip()
        body.update(phase='frozen',candidate_code_sha=version,files=files,
            new_regressions=[{'path':f,'sha256':file_sha(tree/f)} for f in files if REGRESSION.fullmatch(f)],
            patch_sha256=file_sha(path/'proposal.patch'),frozen_at=now())
        atomic_json(path/'repair.json',body)
        if self.source_identity()!=body['source_identity']:raise ValueError('Source checkout changed during repair commit')
        return body

    def accept(self,identifier):
        timeout=self.config.get('trusted_acceptance',{}).get('timeout_seconds',300)
        with self.state.acquire('repair-'+identifier,float(timeout)+120):
            return self._accept(identifier)

    def _accept(self,identifier):
        path,tree,body=self._read(identifier)
        if body['phase']!='frozen':raise ValueError('Only a frozen candidate may enter trusted acceptance')
        if self.source_identity()!=body['source_identity']:raise ValueError('Original checkout changed after repair preparation')
        if self._git(tree,'status','--porcelain').strip() or self._git(tree,'rev-parse','HEAD').decode().strip()!=body['candidate_code_sha']:
            raise ValueError('Candidate changed after patch freeze')
        if digest(self.config)!=body['config_sha']:raise ValueError('Repair executor configuration changed')
        if not callable(self.oracle):
            body.update(acceptance_state='blocked',acceptance_reason='independent_financial_oracle_unconfigured')
            atomic_json(path/'repair.json',body)
            self.state.event('agent_repair',identifier,'blocked',{'reason':body['acceptance_reason']})
            return {k:v for k,v in body.items() if k!='worktree'}
        image=self.config.get('docker_image','')
        if not self.which('bwrap') and not (self.which('docker') and re.fullmatch(r'[A-Za-z0-9._/:\-]+@sha256:[0-9a-f]{64}',image)):
            body.update(acceptance_state='blocked',acceptance_reason='network_isolation_unavailable')
        else:
            if self._git(tree,'status','--porcelain').strip():raise ValueError('Candidate changed during acceptance')
            # /out belongs to the candidate process. Even perfect-looking XML,
            # JSON and exit=0 are diagnostic, never authority. The host-injected
            # verifier must independently execute/compare candidate outputs,
            # keeping its expected values and receipt writer outside that sandbox.
            authority_output=path/'oracle-authority'/uuid.uuid4().hex
            authority_output.mkdir(parents=True,exist_ok=False)
            diagnostic=path/'checks';diagnostic.mkdir(exist_ok=True)
            try:
                authority=self.oracle(candidate_code_sha=body['candidate_code_sha'],candidate_root=tree,
                    diagnostic_root=diagnostic,output=authority_output)
            except Exception as exc:
                authority={'kind':'independent_financial_acceptance.v1','candidate_code_sha':body['candidate_code_sha'],
                    'status':'blocked','reason_code':'independent_financial_oracle_error'}
                body['acceptance_error_type']=type(exc).__name__
            if (isinstance(authority,dict) and authority.get('kind')=='independent_financial_acceptance.v1'
                    and authority.get('candidate_code_sha')==body['candidate_code_sha'] and authority.get('status')=='blocked'):
                reason=authority.get('reason_code','independent_financial_oracle_blocked')
                if not isinstance(reason,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,100}',reason):reason='independent_financial_oracle_blocked'
                body.update(acceptance_state='blocked',acceptance_reason=reason)
                atomic_json(path/'repair.json',body)
                self.state.event('agent_repair',identifier,'blocked',{'reason':reason})
                return {k:v for k,v in body.items() if k!='worktree'}
            if (not isinstance(authority,dict) or authority.get('kind')!='independent_financial_acceptance.v1'
                    or authority.get('candidate_code_sha')!=body['candidate_code_sha']
                    or authority.get('candidate_execution_isolated') is not True):
                body.update(acceptance_state='blocked',acceptance_reason='independent_financial_oracle_invalid')
                atomic_json(path/'repair.json',body)
                self.state.event('agent_repair',identifier,'blocked',{'reason':body['acceptance_reason']})
                return {k:v for k,v in body.items() if k!='worktree'}
            receipt_path=(authority_output/authority.get('receipt','')).resolve()
            if not receipt_path.is_relative_to(authority_output) or not receipt_path.is_file():
                raise ValueError('Independent oracle receipt is missing or outside its authority output')
            from ledger.release_gate import validate
            receipt=json.loads(receipt_path.read_text(encoding='utf-8'))
            validate(receipt,version=body['candidate_code_sha'],evidence_root=receipt_path.parent)
            if body.get('new_regressions'):
                self._validate_regressions(body,receipt,receipt_path.parent)
            if self._git(tree,'status','--porcelain').strip():raise ValueError('Candidate changed during acceptance')
            body.update(acceptance_state='passed',acceptance_receipt_sha=receipt['receipt_sha'],
                acceptance_authority='independent_financial_acceptance.v1')
        atomic_json(path/'repair.json',body)
        self.state.event('agent_repair',identifier,body['acceptance_state'],
            {k:body.get(k) for k in ('repair_id','candidate_code_sha','acceptance_state','acceptance_reason','patch_sha256')})
        return {k:v for k,v in body.items() if k not in {'worktree'}}

    @staticmethod
    def _validate_regressions(body,receipt,evidence_root):
        proof=receipt.get('repair_regressions') or {}
        if (proof.get('code_sha')!=body['candidate_code_sha'] or proof.get('files')!=body['new_regressions']
            or proof.get('excluded_from_official_gates') is not True or proof.get('isolation')!='separate_pytest_process'):
            raise ValueError('Trusted runner did not isolate untrusted new regressions from official acceptance')
        check=proof.get('check') or {};report=(evidence_root/check.get('report','')).resolve()
        if (not report.is_relative_to(evidence_root.resolve()) or not report.is_file()
            or file_sha(report)!=check.get('report_sha256') or check.get('exit_code')!=0):
            raise ValueError('Isolated regression report is missing or changed')
        root=ET.parse(report).getroot();suites=[root] if root.tag=='testsuite' else root.findall('testsuite')
        stats={key:sum(int(s.get(key,0)) for s in suites) for key in ('tests','failures','errors','skipped')}
        if stats['tests']<=0 or any(stats[k] for k in ('failures','errors','skipped')):
            raise ValueError('Isolated regressions did not pass without failures or skips')

    def artifacts(self,identifier,gh_executable=None):
        path,tree,body=self._read(identifier)
        if body['phase']!='frozen':raise ValueError('PR artifacts require a frozen candidate')
        statistics=self._git(tree,'diff','--numstat',body['base_code_sha'],body['candidate_code_sha']).decode().splitlines()
        impact={'repair_id':identifier,'base_code_sha':body['base_code_sha'],'candidate_code_sha':body['candidate_code_sha'],
            'files':body['files'],'numstat':statistics,'acceptance_state':body.get('acceptance_state','not_run'),
            'acceptance_receipt_sha':body.get('acceptance_receipt_sha'),'raw_data_or_full_logs_included':False,
            'automatic_merge':False,'source_checkout_unchanged':self.source_identity()==body['source_identity']}
        atomic_json(path/'impact.json',impact)
        text=('运维故障的受控代码修复。事件材料已经脱敏，真实财务语料和完整日志保留在可信执行区。\n\n'
            +'代码：'+body['base_code_sha']+' → '+body['candidate_code_sha']+'\n\n'
            +'影响文件：\n'+''.join('- '+f+'\n' for f in body['files'])+'\n'
            +'可信验收：'+impact['acceptance_state']+'。缺失或失败的真实语料不能由模型补记为通过。\n'
            +'本产物不包含财务政策、结账或实发授权；不自动合并。\n')
        (path/'pr-body.md').write_text(text,encoding='utf-8')
        result={'impact':str(path/'impact.json'),'patch':str(path/'proposal.patch'),'body_file':str(path/'pr-body.md'),
                'suggested_branch':'codex/ops-repair-'+identifier,'published':False}
        if gh_executable:
            executable=Path(gh_executable)
            if not executable.is_absolute() or not executable.is_file():raise ValueError('Configure an absolute gh executable')
            result['gh_argv']=[str(executable),'pr','create','--draft','--head',result['suggested_branch'],
                '--title','Scoped operational repair '+identifier,'--body-file',result['body_file']]
        return result


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description='隔离worktree运维修复适配器；不执行任意shell或远端合并')
    parser.add_argument('--repo',required=True,type=Path);parser.add_argument('--home',required=True,type=Path)
    parser.add_argument('--config',required=True,type=Path)
    sub=parser.add_subparsers(dest='operation',required=True)
    prepare=sub.add_parser('prepare');prepare.add_argument('--event',required=True,type=Path);prepare.add_argument('--base',required=True)
    for name in ('freeze','accept','artifacts','check'):
        command_parser=sub.add_parser(name);command_parser.add_argument('--repair-id',required=True)
        if name=='check':command_parser.add_argument('--check-id',required=True)
        if name=='artifacts':command_parser.add_argument('--gh-executable')
    args=parser.parse_args(argv)
    try:
        repair=Repair(args.repo,args.home,json.loads(args.config.read_text(encoding='utf-8')))
        if args.operation=='prepare':result=repair.prepare(json.loads(args.event.read_text(encoding='utf-8')),args.base)
        elif args.operation=='freeze':result=repair.freeze_patch(args.repair_id)
        elif args.operation=='accept':result=repair.accept(args.repair_id)
        elif args.operation=='check':result=repair.run_check(args.repair_id,args.check_id)
        else:result=repair.artifacts(args.repair_id,args.gh_executable)
        print(json.dumps(result,ensure_ascii=False))
        return 1 if result.get('state') in {'failed','blocked','reported_failure'} or result.get('acceptance_state') in {'failed','blocked'} else 0
    except (ValueError,OSError,KeyError,Busy,subprocess.CalledProcessError) as exc:
        print(json.dumps({'state':'blocked','reason':'repair_precondition_not_verified','error_type':type(exc).__name__},ensure_ascii=False))
        return 2


if __name__=='__main__':raise SystemExit(main())
