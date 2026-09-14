"""Drain immutable ledger artifacts to verified NAS objects, without moving live databases."""
from __future__ import annotations
import argparse
import json
import os
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import closing
from pathlib import Path
from .storage_maintenance import archive_one, manifest, lease, source_objects


def candidates(root, policy):
    cutoff=time.time()-max(60,int(policy.get('stable_seconds',600)))
    with closing(sqlite3.connect((root/'workspace.db').as_uri()+'?mode=ro',uri=True)) as c:
        known={r[0] for r in c.execute('SELECT id FROM run WHERE evidence_ready=1')}
        blobs={r[0] for r in c.execute('SELECT sha FROM file')}
    items=[];seen=set()
    def add(base,p):
        if p in seen or p.is_symlink() or not p.is_file():return
        relative=p.relative_to(base)
        if any(part.startswith('.') or part.endswith('.tmp') for part in relative.parts):return
        stat=p.stat()
        if stat.st_mtime>cutoff:return
        seen.add(p);items.append((base,p,stat.st_size))
    for p in (root/'runs').glob('*'):
        try:rid=int(p.name.split('.')[0])
        except ValueError:continue
        if rid in known and (p.name.endswith('.parquet') or p.name.endswith('.parquet.sha256')):add(root,p)
    if (root/'commission/registry.db').exists():
        with closing(sqlite3.connect((root/'commission/registry.db').as_uri()+'?mode=ro',uri=True)) as c:
            for rid,name in c.execute('SELECT finance_run,path FROM calculation'):
                if rid in known:add(root,root/'commission/calculations'/Path(name).name)
    for sha in blobs:add(root,root/'files'/sha[:2]/sha)
    for name in ['cache/parse','work','peek']:
        folder=root/name
        if not folder.exists():continue
        for directory,dirs,files in os.walk(folder,followlinks=False):
            dirs[:]=[d for d in dirs if not d.startswith('.') and not d.endswith('.tmp') and not (Path(directory)/d).is_symlink()]
            parent=Path(directory)
            if name=='cache/parse' and not (parent/'meta.json').is_file():continue
            for filename in files:add(root,parent/filename)
    app=root.parent
    for name in ['incoming','model-backups','commission-inputs']:
        folder=app/name
        if not folder.exists():continue
        for directory,dirs,files in os.walk(folder,followlinks=False):
            dirs[:]=[d for d in dirs if not (Path(directory)/d).is_symlink()]
            for filename in files:
                p=Path(directory)/filename
                if p.stat().st_mtime<time.time()-86400:add(app,p)
    for base,p in source_objects(root,policy,cutoff):add(base,p)
    return sorted(items,key=lambda item:item[2],reverse=True)


def run_bulk(root,policy,stop_event=None):
    root=Path(root).resolve();archive=Path(policy['archive_root']).resolve()
    if archive.is_relative_to(root) or root.is_relative_to(archive):raise ValueError('Archive overlaps workspace')
    archive.mkdir(parents=True,exist_ok=True)
    workers=max(1,min(8,int(policy.get('workers',4))))
    result={'at':time.time(),'mode':'all_completed','archived_files':0,'archived_bytes':0,'errors':[],'deferred':[],
            'free_before':shutil.disk_usage(root).free}
    with lease(root):
        plan=candidates(root,policy)
        result.update(eligible_files=len(plan),eligible_bytes=sum(x[2] for x in plan))
        limit_files=int(policy.get('max_files',1000));limit_bytes=int(policy.get('max_bytes',1024**3))
        selected=[];total=0
        for item in plan:
            if len(selected)>=limit_files or total>=limit_bytes:break
            selected.append(item);total+=item[2]
        if shutil.disk_usage(archive).free<total+1024**3:raise ValueError('Insufficient NAS free space')
        for base in {x[0] for x in selected}:
            with closing(manifest(base)) as c:c.execute('PRAGMA journal_mode=WAL')
        local=threading.local();connections=[];guard=threading.Lock();verified=set()
        def work(item):
            base,p,size=item
            if not hasattr(local,'connections'):local.connections={}
            if base not in local.connections:
                c=sqlite3.connect(base/'storage.db',timeout=60,check_same_thread=False)
                c.execute('PRAGMA synchronous=NORMAL');local.connections[base]=c
                with guard:connections.append(c)
            try:return archive_one(base,archive,p,local.connections[base],verified=verified)
            except OSError as exc:
                if getattr(exc,'winerror',None) in (32,33):return {'path':str(p),'reason':str(exc)}
                raise
        def progress(done=False):
            result.update(free_after=shutil.disk_usage(root).free,remaining_files=result['eligible_files']-result['archived_files'],finished=done)
            target=root/'storage-status.json';temp=root/'storage-bulk-status.tmp'
            temp.write_text(json.dumps(result),encoding='utf8');temp.replace(target)
            print(json.dumps(result),flush=True)
        progress();iterator=iter(selected);pending=set();last_report=time.monotonic()
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for _ in range(workers*2):
                    item=next(iterator,None)
                    if item is None:break
                    pending.add(pool.submit(work,item))
                while pending:
                    done,pending=wait(pending,timeout=10,return_when=FIRST_COMPLETED)
                    for future in done:
                        try:
                            amount=future.result()
                            if isinstance(amount,dict):result['deferred'].append(amount)
                            elif amount is not None:result['archived_files']+=1;result['archived_bytes']+=amount
                        except Exception as exc:result['errors'].append(str(exc))
                        if not result['errors'] and not (stop_event and stop_event.is_set()):
                            item=next(iterator,None)
                            if item is not None:pending.add(pool.submit(work,item))
                    if time.monotonic()-last_report>20:progress();last_report=time.monotonic()
        finally:
            for c in connections:c.close()
        progress(True)
        return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--drain',action='store_true');p.add_argument('--workers',type=int,default=6);a=p.parse_args()
    policy=json.loads((a.root/'storage-policy.json').read_text(encoding='utf8'))
    policy['workers']=a.workers
    if a.drain:policy.update(max_files=10000000,max_bytes=2**62)
    for attempt in range(4):
        result=run_bulk(a.root,policy)
        if result['errors']:raise SystemExit(1)
        if not result['deferred']:break
        time.sleep(2)
    if result['deferred']:raise SystemExit('Files remain temporarily locked; original files retained')
