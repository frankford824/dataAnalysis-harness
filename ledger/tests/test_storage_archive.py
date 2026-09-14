import os
import time
import sqlite3
import polars as pl
from ledger.workspace import Workspace
from ledger.storage_maintenance import run_once
from ledger.storage_integrity import digest


def setup(tmp_path):
    w=Workspace(tmp_path/'home')
    for i in range(5):
        rid=w.record('s','2026-06',{'can_close':True},[])
        path=w.facts_path(rid);path.parent.mkdir(exist_ok=True)
        pl.DataFrame({'amount':[12.34]}).write_parquet(path)
        os.utime(path,(time.time()-4*86400,)*2)
        if i==0:w.close_period('s','2026-06',by='test',note='closed')
    return w,{'archive_root':str(tmp_path/'archive'),'hot_days':2,'hot_versions':2,'max_files':100}


def test_archive_keeps_paths_content_current_and_closed_records(tmp_path):
    w,policy=setup(tmp_path)
    expected=digest(w.facts_path(2))
    result=run_once(w.root,policy)
    assert result['archived_files']==2 and not result['errors']
    assert w.facts_path(2).is_symlink() and w.facts_path(3).is_symlink()
    assert digest(w.facts_path(2))==expected
    assert pl.read_parquet(w.facts_path(3))['amount'].item()==12.34
    assert all(not w.facts_path(i).is_symlink() for i in [1,4,5])
    assert w.state('s','2026-06').run_id==1
    assert w.conn.execute('select count(*) from run').fetchone()[0]==5
    assert len(list((tmp_path/'archive/objects').rglob('*.parquet')))==1
    assert run_once(w.root,policy)['archived_files']==0


def test_archive_copy_failure_never_removes_live_file(tmp_path,monkeypatch):
    w,policy=setup(tmp_path)
    def fail(*args,**kwargs):raise OSError('unavailable destination')
    monkeypatch.setattr('ledger.storage_maintenance.shutil.copyfile',fail)
    result=run_once(w.root,policy)
    assert result['errors'] and result['archived_files']==0
    assert w.facts_path(2).is_file() and not w.facts_path(2).is_symlink()


def test_atomic_link_failure_keeps_live_artifact(tmp_path,monkeypatch):
    w,policy=setup(tmp_path)
    original=os.replace
    def fail(source,destination):
        if str(destination).endswith('2.parquet'):raise PermissionError('reader holds file')
        return original(source,destination)
    monkeypatch.setattr('ledger.storage_maintenance.os.replace',fail)
    result=run_once(w.root,policy)
    assert result['errors']
    assert pl.read_parquet(w.facts_path(2))['amount'].item()==12.34
    assert not w.facts_path(2).is_symlink()


def test_source_objects_keep_provider_and_consumer_snapshots(tmp_path):
    import json
    w,policy=setup(tmp_path)
    provider=tmp_path/'provider';(provider/'objects').mkdir(parents=True);(provider/'current').mkdir()
    for name in ['provider','consumer','cold']:
        p=provider/'objects'/(name+'.parquet');p.write_bytes(name.encode());os.utime(p,(time.time()-4*86400,)*2)
    (provider/'current/manifest.json').write_text(json.dumps({'objects':{'data':{'path':'objects/provider.parquet'}}}))
    with sqlite3.connect(w.root/'order-feed.db') as c:
        c.execute('create table feed_state(id integer,manifest_json text)')
        c.execute('insert into feed_state values(1,?)',(json.dumps({'objects':{'data':{'path':'objects/consumer.parquet'}}}),))
    policy['source_snapshot_root']=str(provider)
    result=run_once(w.root,policy)
    assert not result['errors'] and result['archived_files']==3
    assert (provider/'objects/cold.parquet').is_symlink()
    assert not (provider/'objects/provider.parquet').is_symlink()
    assert not (provider/'objects/consumer.parquet').is_symlink()



def test_archived_feed_path_requires_registered_checksum(tmp_path):
    from ledger.storage_maintenance import archive_one,manifest
    from ledger.storage_integrity import registered_archive_link
    root=tmp_path/'feed';(root/'objects').mkdir(parents=True);(root/'current').mkdir()
    (root/'current/manifest.json').write_text('{}')
    source=root/'objects/data.parquet';source.write_bytes(b'original bytes')
    sha=digest(source)
    with manifest(root) as c:archive_one(root,tmp_path/'cold',source,c)
    assert registered_archive_link(root,source,sha)
    assert not registered_archive_link(root,source,'bad')
    other=root/'objects/unregistered.parquet';other.symlink_to(source.resolve())
    assert not registered_archive_link(root,other,sha)
    assert not registered_archive_link(root,root/'../cold'/source.resolve().name,sha)


def test_bulk_moves_completed_closed_runs_inputs_and_committed_cache(tmp_path):
    from ledger.storage_bulk import run_bulk
    w,policy=setup(tmp_path)
    raw=tmp_path/'input.csv';raw.write_text('amount\n12.34\n')
    kept=w.keep('input.csv',raw,'s');files=w.active_files('s')
    parse=w.root/'cache/parse/ab/key';parse.mkdir(parents=True)
    (parse/'meta.json').write_text('[]');(parse/'frame.parquet').write_bytes(b'cache')
    pending=w.root/'cache/parse/ab/.pending.tmp';pending.mkdir();(pending/'frame.parquet').write_bytes(b'pending')
    for p in [w.path_of(kept.sha),*files,parse/'meta.json',parse/'frame.parquet']:
        os.utime(p,(time.time()-86400,)*2)
    policy.update(archive_all_completed=True,workers=4,max_files=1000)
    result=run_bulk(w.root,policy)
    assert not result['errors'] and result['remaining_files']==0
    assert all(w.facts_path(i).is_symlink() for i in range(1,6))
    assert w.state('s','2026-06').run_id==1
    assert pl.read_parquet(w.facts_path(1))['amount'].item()==12.34
    assert w.path_of(kept.sha).is_symlink() and files[0].is_symlink()
    assert files[0].read_text()==raw.read_text()
    assert (parse/'frame.parquet').is_symlink()
    assert not (pending/'frame.parquet').is_symlink()
    assert run_bulk(w.root,policy)['eligible_files']==0


def test_archiver_rejects_live_registry(tmp_path):
    import pytest
    from ledger.storage_maintenance import archive_one,manifest
    root=tmp_path/'home';(root/'commission').mkdir(parents=True)
    source=root/'commission/registry.db';source.write_bytes(b'live')
    with manifest(root) as c:
        with pytest.raises(ValueError,match='Live commission'):
            archive_one(root,tmp_path/'archive',source,c)
    assert source.read_bytes()==b'live'


def test_transient_windows_sharing_violation_retries(tmp_path,monkeypatch):
    from ledger.storage_maintenance import replace_with_retry
    source=tmp_path/'next';target=tmp_path/'final';source.write_bytes(b'verified')
    original=os.replace;attempts=[]
    def replace(a,b):
        attempts.append(1)
        if len(attempts)==1:
            exc=OSError('temporary sharing violation');exc.winerror=32;raise exc
        original(a,b)
    monkeypatch.setattr('ledger.storage_maintenance.os.replace',replace)
    monkeypatch.setattr('ledger.storage_maintenance.time.sleep',lambda _:None)
    replace_with_retry(source,target)
    assert target.read_bytes()==b'verified' and len(attempts)==2


def test_bulk_defers_locked_file_and_continues(tmp_path,monkeypatch):
    from ledger.storage_bulk import run_bulk
    from ledger.storage_maintenance import archive_one
    w,policy=setup(tmp_path)
    def locked(root,archive,source,conn,**kwargs):
        if source.name=='2.parquet':
            exc=OSError('reader is using file');exc.winerror=32;raise exc
        return archive_one(root,archive,source,conn,**kwargs)
    with monkeypatch.context() as m:
        m.setattr('ledger.storage_bulk.archive_one',locked)
        result=run_bulk(w.root,policy)
    assert not result['errors'] and len(result['deferred'])==1
    assert result['archived_files']==4
    assert not w.facts_path(2).is_symlink()
    assert run_bulk(w.root,policy)['archived_files']==1
