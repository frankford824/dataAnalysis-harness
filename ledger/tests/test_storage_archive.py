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
