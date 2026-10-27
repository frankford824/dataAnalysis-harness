"""Create/verify a local recoverable release backup; never restore over live data."""
from __future__ import annotations
import argparse
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import zipfile

ROLES={'application','model','workspace','registry'}
CHECKS={'workspace_integrity','registry_integrity','frozen_history_equal','application_restored','model_restored'}


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(4*1024*1024),b''):h.update(block)
    return h.hexdigest()


def identity(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def readback():
    path=Path(__file__).with_name('verify-ledger-release.py')
    spec=importlib.util.spec_from_file_location('ledger_backup_history',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def files(root,*,application=False):
    root=Path(root).resolve()
    if not root.is_dir():raise ValueError('Backup source directory is missing')
    allowed=('ledger/ledger/','ops/ledger_sentinel/','agent-worker/ledger_agent/',
        'agent-worker/skills/','host-agent/finance_agent/','tools/')
    metadata={'VERSION','RELEASE-ACCEPTANCE.json','agent-worker/requirements.lock','agent-worker/pyproject.toml',
        'host-agent/pyproject.toml'}
    result=[]
    for path in sorted(root.rglob('*')):
        relative=path.relative_to(root).as_posix()
        if any(part.startswith('.') or part in {'__pycache__','node_modules','venv'} for part in path.relative_to(root).parts):continue
        if application and relative not in metadata and not relative.startswith(allowed):continue
        if path.is_symlink():raise ValueError('Backup source contains an unregistered symbolic link')
        if path.is_file():result.append({'path':relative,'sha256':sha(path)})
    if not result:raise ValueError('Empty source cannot certify a recoverable backup')
    if application and not any(row['path']=='ledger/ledger/api.py' for row in result):
        raise ValueError('Application backup does not contain the actual ledger service')
    return result


def archive(root,target,manifest):
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as stream:
        for row in manifest:
            path=Path(root)/row['path']
            if sha(path)!=row['sha256']:raise ValueError('Source changed during backup')
            stream.write(path,row['path'])
    verify_archive(target,manifest)


def verify_archive(path,manifest):
    with zipfile.ZipFile(path) as stream:
        if len(stream.namelist())!=len(manifest) or set(stream.namelist())!={row['path'] for row in manifest}:
            raise ValueError('Restored archive scope differs from its source')
        for row in manifest:
            h=hashlib.sha256()
            with stream.open(row['path']) as source:
                for block in iter(lambda:source.read(4*1024*1024),b''):h.update(block)
            if h.hexdigest()!=row['sha256']:raise ValueError('Restored archive differs from its source')


def sqlite_copy(source,target):
    if not source.is_file():raise ValueError('Required financial database is missing')
    target.parent.mkdir(parents=True,exist_ok=True)
    # SQLite backup includes committed WAL contents. Copying the main file alone
    # would silently omit transactions in a running service.
    with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)) as origin:
        with closing(sqlite3.connect(target)) as destination:
            origin.backup(destination)
    with closing(sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True)) as restored:
        if restored.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:
            raise ValueError('Restored financial database failed integrity check')


def private_directory(target):
    target.mkdir(mode=0o700)
    if os.name=='nt':
        # Backups include financial records. Do not inherit a shared Windows ACL.
        import csv,io
        text=subprocess.check_output(['whoami','/user','/fo','csv','/nh'],text=True)
        sid=next(csv.reader(io.StringIO(text)))[1]
        subprocess.run(['icacls',str(target),'/inheritance:r','/grant:r','*'+sid+':(OI)(CI)F'],
            check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    else:target.chmod(0o700)


def create(root,application,model,target,version):
    root,application,model,target=(Path(p).resolve() for p in (root,application,model,target))
    if not re.fullmatch('[0-9a-f]{7,40}',version):raise ValueError('Explicit deployed version required')
    if target.exists() or any(target.is_relative_to(source) for source in (root,application,model)):
        raise ValueError('A new backup directory outside application/workspace is required')
    stamped=(application/'VERSION').read_text().strip()
    if stamped!=version:raise ValueError('Application version changed before backup')
    tool=readback();version_reader=lambda _:{'version':version,'source':'backup_source'}
    before=tool.snapshot(root,reader=version_reader)
    code,configuration=files(application,application=True),files(model)
    sizes=0
    for name in ('workspace.db','commission/registry.db'):
        with closing(sqlite3.connect((root/name).as_uri()+'?mode=ro',uri=True)) as db:
            sizes+=db.execute('PRAGMA page_count').fetchone()[0]*db.execute('PRAGMA page_size').fetchone()[0]
    sizes+=sum((application/row['path']).stat().st_size for row in code)
    sizes+=sum((model/row['path']).stat().st_size for row in configuration)
    if shutil.disk_usage(target.parent).free < sizes*1.1+64*1024*1024:
        raise ValueError('Backup destination has insufficient free space')
    private_directory(target)
    # A failure intentionally retains its diagnostic partial backup, without a
    # manifest. It cannot be mistaken for a completed/recoverable release backup.
    sqlite_copy(root/'workspace.db',target/'workspace.db')
    sqlite_copy(root/'commission/registry.db',target/'commission/registry.db')
    archive(application,target/'application.zip',code)
    archive(model,target/'model.zip',configuration)
    restored=tool.snapshot(target,reader=version_reader)
    after=tool.snapshot(root,reader=version_reader)
    tool.compare_history(before,restored,version);tool.compare_history(before,after,version)
    if files(application,application=True)!=code or files(model)!=configuration or (application/'VERSION').read_text().strip()!=version:
        raise ValueError('Application/model source changed during backup')
    records=[{'role':role,'path':path,'sha256':sha(target/path)} for role,path in
        [('application','application.zip'),('model','model.zip'),('workspace','workspace.db'),('registry','commission/registry.db')]]
    proof={'schema':1,'code_sha':version,'completed_at':datetime.now(timezone.utc).isoformat(),
        'files':records,'checks':{name:True for name in CHECKS},
        'application_files':code,'model_files':configuration,
        'source_frozen_history_sha':identity(before),'restored_frozen_history_sha':identity(restored),
        'database_consistency':'sqlite_online_backup_per_database_with_frozen_financial_layers_equal',
        'live_restore_performed':False,'passed':True}
    proof_path=target/'restore-check.json';proof_path.write_text(json.dumps(proof,ensure_ascii=False,indent=2))
    body={'schema':1,'code_sha':version,'completed_at':proof['completed_at'],'files':records,
          'restore_check':{'path':proof_path.name,'sha256':sha(proof_path)}}
    temporary=target/'manifest.pending';temporary.write_text(json.dumps(body,ensure_ascii=False,indent=2));temporary.rename(target/'manifest.json')
    return verify(target/'manifest.json',version)


def verify(path,version,max_age=86400,compare_root=None,allow_extra_roles=False):
    path=Path(path).resolve();body=json.loads(path.read_text())
    if body.get('schema')!=1 or body.get('code_sha')!=version:raise ValueError('Backup version/schema mismatch')
    stamp=datetime.fromisoformat(body['completed_at']);now=datetime.now(timezone.utc)
    if stamp.tzinfo is None or not -30 <= (now-stamp).total_seconds() <= max_age:
        raise ValueError('Backup is stale or lacks an actual completion time')
    records=body.get('files')
    if (not isinstance(records,list) or any(not isinstance(row,dict) for row in records)
            or any(not isinstance(row.get('role'),str) or not re.fullmatch('[a-z][a-z0-9_-]{0,63}',row['role']) for row in records)
            or not ROLES<={row['role'] for row in records}
            or len({row['role'] for row in records})!=len(records)):
        raise ValueError('Backup does not cover application, model and both financial databases')
    core=[row for row in records if row['role'] in ROLES]
    extra={row['role'] for row in records}-ROLES
    if extra and not allow_extra_roles:raise ValueError('Extra backup roles require an explicit hash-only verification policy')
    expected={'application':'application.zip','model':'model.zip','workspace':'workspace.db','registry':'commission/registry.db'}
    if any(row.get('path')!=expected[row['role']] for row in core):raise ValueError('Backup role points at another artifact')
    if len({row.get('path') for row in records})!=len(records):raise ValueError('Backup artifacts are duplicated across roles')
    for row in [*records,body['restore_check']]:
        source=path.parent/row['path']
        if source.is_symlink() or not source.resolve().is_relative_to(path.parent) or not source.is_file() or sha(source)!=row['sha256']:
            raise ValueError('Backup artifact changed, missing or outside its protected directory')
    proof=json.loads((path.parent/body['restore_check']['path']).read_text())
    if (proof.get('schema')!=1 or proof.get('passed') is not True or proof.get('code_sha')!=version
            or proof.get('files')!=core or proof.get('completed_at')!=body['completed_at']
            or set(proof.get('checks',{}))!=CHECKS or any(value is not True for value in proof['checks'].values())
            or proof.get('source_frozen_history_sha')!=proof.get('restored_frozen_history_sha')):
        raise ValueError('Backup has no matching successful restore verification')
    by_role={r['role']:path.parent/r['path'] for r in records}
    verify_archive(by_role['application'],proof['application_files']);verify_archive(by_role['model'],proof['model_files'])
    tool=readback();restored=tool.snapshot(path.parent,reader=lambda _:{'version':version,'source':'backup_source'})
    if identity(restored)!=proof['restored_frozen_history_sha']:raise ValueError('Restored financial history differs from backup proof')
    if compare_root is not None:
        current=tool.snapshot(Path(compare_root).resolve(),reader=lambda _:{'version':version,'source':'backup_source'})
        tool.compare_history(restored,current,version)
    for role in ('workspace','registry'):
        with closing(sqlite3.connect(by_role[role].resolve().as_uri()+'?mode=ro',uri=True)) as db:
            if db.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('Restored database is not readable')
    return {'passed':True,'manifest_sha256':sha(path),'code_sha':version,'roles':sorted(ROLES|extra),
        'restore_verified_roles':sorted(ROLES),'extra_roles_hash_checked':sorted(extra),'live_restore_performed':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['create','verify']);parser.add_argument('--version',required=True)
    parser.add_argument('--manifest',type=Path);parser.add_argument('--root',type=Path)
    parser.add_argument('--application',type=Path);parser.add_argument('--model',type=Path);parser.add_argument('--output',type=Path)
    parser.add_argument('--max-age-seconds',type=int,default=86400)
    parser.add_argument('--compare-root',type=Path,help='Require backup to cover the current frozen financial layers')
    parser.add_argument('--allow-extra-roles',action='store_true',help='Verify extra artifact hashes without claiming their restore was tested')
    args=parser.parse_args()
    if args.mode=='create':
        if any(value is None for value in (args.root,args.application,args.model,args.output)):parser.error('create requires explicit root/application/model/output')
        result=create(args.root,args.application,args.model,args.output,args.version)
    else:
        if not args.manifest:parser.error('verify requires manifest')
        result=verify(args.manifest,args.version,args.max_age_seconds,args.compare_root,args.allow_extra_roles)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
