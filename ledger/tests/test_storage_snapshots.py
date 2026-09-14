import json
import sqlite3
import zlib
from pathlib import Path
import pytest
import polars as pl
from ledger.commission_registry import Registry
from ledger.commission_engine import persist
from ledger.snapshot_store import put,resolve,reference_sha
from ledger.storage_migrate import prepare,catch_up


def test_exact_bytes_shared_and_immutable(tmp_path):
    r=Registry(tmp_path)
    text='{ "中文": [1, 2.00], "space": "kept" }'
    with r.transaction() as c:
        a=put(c,text);b=put(c,text)
        assert a==b and resolve(c,a)==text
        assert c.execute('select count(*) from evidence_blob').fetchone()[0]==1
        with pytest.raises(sqlite3.IntegrityError):c.execute('delete from evidence_blob')
    with r.connect() as c:
        with pytest.raises(ValueError,match='missing'):resolve(c,json.dumps({'$ledger_snapshot_v1':'0'*64},separators=(',',':')))


def test_candidate_migration_and_tail_are_lossless(tmp_path):
    r=Registry(tmp_path/'source')
    model=json.dumps({'model':'甲'*20000},ensure_ascii=False)
    rules=json.dumps({'rules':list(range(1000))})
    with r.transaction() as c:
        for i in range(4):
            c.execute('INSERT INTO calculation VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (str(i),i,'s','2026-06','date',0,'p','sha',model,rules,'{"total":1}'))
    work=tmp_path/'work.db';target=tmp_path/'candidate.db'
    result=prepare(r.path,work,target)
    assert result['calculations']==4 and result['blobs']==2
    with sqlite3.connect(target) as c:
        assert resolve(c,c.execute('select model_json from calculation limit 1').fetchone()[0])==model
        with pytest.raises(sqlite3.IntegrityError):c.execute("update calculation set period='wrong'")
    r.person_save({'name':'新增人员'},'test','迁移期间新增')
    persist(r,9,pl.DataFrame({'amount':[1]}),{'id':'tail','store_id':'s','period':'2026-06',
        'registry_revision':1,'model_json':model,'rules_json':rules,'summary_json':'{"total":1}'})
    copied=catch_up(r.path,target)
    assert copied['counts']['calculation']==5
    with sqlite3.connect(target) as c:
        assert c.execute('select count(*) from person').fetchone()[0]==1
        assert resolve(c,c.execute("select rules_json from calculation where id='tail'").fetchone()[0])==rules
    assert r.calculation_evidence('tail')['model_json']==model


def test_reuse_requires_exact_payload_fingerprint_and_sealed_files(tmp_path):
    from ledger.workspace import Workspace
    from ledger.storage_integrity import seal,verified
    w=Workspace(tmp_path)
    payload={'can_close':True,'commission':{'calculation_id':'old','total':12}}
    ident=w.record('s','2026-06',payload,[],input_fingerprint='input')
    assert w.identical_run('s','2026-06',{'can_close':True,'commission':{'calculation_id':'new','total':12}},'input')[0]==ident
    assert w.identical_run('s','2026-06',payload,'changed') is None
    assert w.identical_run('s','2026-06',{'can_close':True,'commission':{'total':13}},'input') is None
    path=w.facts_path(ident);path.parent.mkdir(exist_ok=True);path.write_bytes(b'facts');seal(path)
    assert verified(path)
    path.write_bytes(b'corrupt');assert not verified(path)


def test_feed_compaction_preserves_checkpoint_and_enables_reclaim(tmp_path):
    from ledger.storage_migrate import compact_feed
    source=tmp_path/'feed.db';target=tmp_path/'compact.db'
    with sqlite3.connect(source) as c:
        for name in ['feed_state','feed_store','feed_entity','feed_pending_store']:
            c.execute('CREATE TABLE '+name+'(id INTEGER PRIMARY KEY, payload TEXT)')
            c.executemany('INSERT INTO '+name+' VALUES(?,?)',[(i,'x'*10000) for i in range(80)])
            c.execute('DELETE FROM '+name+' WHERE id>2')
        c.execute("UPDATE feed_state SET payload='checkpoint-17929491' WHERE id=1")
    result=compact_feed(source,target)
    assert result['candidate_bytes']<result['source_bytes']
    with sqlite3.connect(target) as c:
        assert c.execute('PRAGMA auto_vacuum').fetchone()[0]==2
        assert c.execute('SELECT payload FROM feed_state WHERE id=1').fetchone()[0]=='checkpoint-17929491'
    assert all(n==3 for n in result['counts'].values())
