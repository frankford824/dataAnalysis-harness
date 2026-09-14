"""Verified cold-artifact tiering with transparent, atomic filesystem links.

No run, calculation, close, source file or audit row is deleted. Current/frozen
runs remain local. Cold artifacts keep their existing paths through symlinks.
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager, closing
from pathlib import Path
from .storage_integrity import digest


@contextmanager
def lease(root):
    path=root/'storage-maintenance.lock'
    with path.open('a+b') as f:
        if f.tell()==0:f.write(b'0');f.flush()
        f.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:yield
        finally:
            f.seek(0)
            if os.name=='nt':msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(f.fileno(),fcntl.LOCK_UN)


def manifest(root):
    conn=sqlite3.connect(root/'storage.db',timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS artifact_archive (
      source TEXT PRIMARY KEY, sha TEXT NOT NULL, bytes INTEGER NOT NULL,
      target TEXT NOT NULL, state TEXT NOT NULL, archived_at TEXT NOT NULL)""")
    conn.commit()
    return conn


def archive_one(root: Path, archive: Path, source: Path, conn):
    if source.is_symlink():return 0
    relative=source.relative_to(root)
    if relative.parts[0] not in {'runs','commission'} or not source.parent.resolve().is_relative_to(root.resolve()):
        raise ValueError('Artifact outside allowed workspace')
    before=source.stat();sha=digest(source)
    target=archive/'objects'/sha[:2]/(sha+source.suffix)
    target.parent.mkdir(parents=True,exist_ok=True)
    if not target.exists():
        temp=target.with_name(target.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            shutil.copyfile(source,temp)
            if digest(temp)!=sha:raise ValueError('Archive copy failed checksum')
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
    elif digest(target)!=sha:
        raise ValueError('Existing archive object failed checksum')
    after=source.stat()
    if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
        raise ValueError('Artifact changed during archival')
    with conn:
        conn.execute('INSERT OR REPLACE INTO artifact_archive VALUES(?,?,?,?,?,?)',
                     (str(relative),sha,before.st_size,str(target),'copied',str(time.time())))
    link=source.with_name(source.name+'.archive-'+uuid.uuid4().hex+'.tmp')
    try:
        link.symlink_to(target)
        if digest(link)!=sha:raise ValueError('Archive link failed verification')
        # Atomic replacement retains a valid path even for concurrent readers.
        os.replace(link,source)
    finally:link.unlink(missing_ok=True)
    with conn:conn.execute("UPDATE artifact_archive SET state='archived' WHERE source=?",(str(relative),))
    return before.st_size


def protected_runs(root, versions):
    with sqlite3.connect((root/'workspace.db').as_uri()+'?mode=ro',uri=True) as conn:
        keep={r[0] for r in conn.execute('SELECT run_id FROM period WHERE run_id IS NOT NULL')}
        keep.update(r[0] for r in conn.execute('SELECT id FROM (SELECT id,row_number() OVER\n            (PARTITION BY store_id,period ORDER BY id DESC) n FROM run) WHERE n<=?',(versions,)))
        known={r[0] for r in conn.execute('SELECT id FROM run WHERE evidence_ready=1')}
    return keep,known


def run_once(root: Path, policy: dict, stop_event=None):
    root=root.resolve();archive=Path(policy['archive_root']).resolve()
    if archive.is_relative_to(root) or root.is_relative_to(archive):raise ValueError('Archive must be outside the live workspace')
    archive.mkdir(parents=True,exist_ok=True)
    max_bytes=int(policy.get('max_bytes',8*1024**3));max_files=int(policy.get('max_files',2000))
    days=max(1,int(policy.get('hot_days',2)));versions=max(1,int(policy.get('hot_versions',2)))
    cutoff=time.time()-days*86400
    result={'at':time.time(),'archived_files':0,'archived_bytes':0,'errors':[],
            'free_before':shutil.disk_usage(root).free}
    with lease(root):
        keep,known=protected_runs(root,versions)
        candidates=[]
        for source in (root/'runs').glob('*.parquet'):
            try:rid=int(source.name.split('.')[0])
            except ValueError:continue
            if rid not in keep and rid in known and not source.is_symlink() and source.stat().st_mtime<cutoff:
                candidates.append(source)
        reg=root/'commission/registry.db'
        if reg.exists():
            with sqlite3.connect(reg.as_uri()+'?mode=ro',uri=True) as c:
                for rid,name in c.execute('SELECT finance_run,path FROM calculation'):
                    source=root/'commission/calculations'/Path(name).name
                    if rid in known and rid not in keep and not source.is_symlink() and source.is_file() and source.stat().st_mtime<cutoff:
                        candidates.append(source)
        candidates.sort(key=lambda p:p.stat().st_mtime)
        result['eligible_files']=len(candidates)
        with closing(manifest(root)) as conn:
            for source in candidates:
                if stop_event is not None and stop_event.is_set():break
                if result['archived_files']>=max_files or result['archived_bytes']>=max_bytes:break
                try:
                    size=archive_one(root,archive,source,conn)
                    result['archived_files']+=1;result['archived_bytes']+=size
                    if result['archived_files']%100==0:print(json.dumps(result),flush=True)
                except (OSError,ValueError) as exc:
                    result['errors'].append(str(exc))
                    # An unavailable destination must leave remaining hot files untouched.
                    break
        result['free_after']=shutil.disk_usage(root).free
        temp=root/'storage-status.pending.json';temp.write_text(json.dumps(result),encoding='utf8');temp.replace(root/'storage-status.json')
    return result


def status(root):
    root=Path(root);policy=root/'storage-policy.json';last=root/'storage-status.json'
    config=json.loads(policy.read_text(encoding='utf8')) if policy.exists() else {}
    result=json.loads(last.read_text(encoding='utf8')) if last.exists() else {}
    result.update(enabled=bool(config.get('enabled')),free_bytes=shutil.disk_usage(root).free,
                  reserve_bytes=int(config.get('reserve_bytes',15*1024**3)))
    result['low_space']=result['free_bytes']<result['reserve_bytes']
    return result


class Worker:
    def __init__(self,root):self.root=Path(root);self.stop_event=threading.Event();self.thread=None;self.last_run=0
    def start(self):
        self.thread=threading.Thread(target=self.loop,name='storage-maintenance',daemon=True);self.thread.start()
    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=2)
    def loop(self):
        while not self.stop_event.wait(60):
            path=self.root/'storage-policy.json'
            if not path.exists():continue
            try:
                policy=json.loads(path.read_text(encoding='utf8'))
                if not policy.get('enabled'):continue
                interval=max(300,int(policy.get('interval_seconds',3600)))
                pressure=shutil.disk_usage(self.root).free<int(policy.get('reserve_bytes',15*1024**3))
                if time.time()-self.last_run<interval and not pressure:continue
                run_once(self.root,policy,self.stop_event)
                self.last_run=time.time()
                self.stop_event.wait(60 if pressure else 1)
            except Exception as exc:
                print('Storage maintenance deferred: '+str(exc),flush=True)
                self.stop_event.wait(300)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--archive',required=True);p.add_argument('--max-files',type=int,default=2000);p.add_argument('--max-gb',type=int,default=8);p.add_argument('--hot-days',type=int,default=2);a=p.parse_args()
    print(json.dumps(run_once(a.root,{'archive_root':a.archive,'max_files':a.max_files,'max_bytes':a.max_gb*1024**3,'hot_days':a.hot_days})),flush=True)
