"""Offline candidate migration. Never rewrites the live registry in place."""
from __future__ import annotations
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from .snapshot_store import SCHEMA, put, resolve, reference_sha


def logical_calculation(row, conn):
    values=list(row)
    for index in (8,9):
        values[index]=reference_sha(values[index]) or hashlib.sha256(values[index].encode()).hexdigest()
    values[10]=hashlib.sha256(values[10].encode()).hexdigest()
    return json.dumps(values,ensure_ascii=False,separators=(',',':')).encode()


def calculation_digest(conn, schema="main"):
    h=hashlib.sha256();n=0
    for row in conn.execute('SELECT * FROM '+schema+'.calculation ORDER BY id'):
        h.update(logical_calculation(row,conn));h.update(b'\n');n+=1
    return n,h.hexdigest()


def verify_blobs(conn):
    for sha, in conn.execute('SELECT sha FROM evidence_blob'):
        resolve(conn,json.dumps({'$ledger_snapshot_v1':sha},separators=(',',':')))
    missing=conn.execute("""SELECT count(*) FROM calculation c WHERE
      (json_type(c.model_json,'$.$ledger_snapshot_v1') IS NOT NULL AND NOT EXISTS
       (SELECT 1 FROM evidence_blob b WHERE b.sha=json_extract(c.model_json,'$.$ledger_snapshot_v1')))
      OR (json_type(c.rules_json,'$.$ledger_snapshot_v1') IS NOT NULL AND NOT EXISTS
       (SELECT 1 FROM evidence_blob b WHERE b.sha=json_extract(c.rules_json,'$.$ledger_snapshot_v1')))""").fetchone()[0]
    if missing:raise ValueError('Dangling calculation evidence references')


def prepare(source: Path, work: Path, output: Path):
    if work.exists() or output.exists():raise ValueError('Candidate already exists')
    work.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True) as src, sqlite3.connect(work) as dst:
        src.backup(dst,pages=4096,sleep=.01)
    with sqlite3.connect(work) as conn:
        conn.execute('PRAGMA journal_mode=DELETE')
        conn.executescript(SCHEMA)
        before=calculation_digest(conn)
        trigger=conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='calculation_no_update'").fetchone()
        conn.execute('DROP TRIGGER IF EXISTS calculation_no_update');conn.commit()
        cursor='';processed=0
        while True:
            rows=conn.execute('SELECT id,model_json,rules_json FROM calculation WHERE id>? ORDER BY id LIMIT 100',(cursor,)).fetchall()
            if not rows:break
            with conn:
                for ident,model,rules in rows:
                    conn.execute('UPDATE calculation SET model_json=?,rules_json=? WHERE id=?',(put(conn,model),put(conn,rules),ident))
            cursor=rows[-1][0];processed+=len(rows)
            if processed%1000==0:print(json.dumps({'converted':processed}),flush=True)
        if trigger:conn.execute(trigger[0])
        conn.commit()
        verify_blobs(conn)
        after=calculation_digest(conn)
        if before!=after:raise ValueError('Calculation evidence changed during conversion')
        conn.execute('VACUUM INTO ?',(str(output),))
    with sqlite3.connect(output) as conn:
        if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Candidate integrity failed')
        if calculation_digest(conn)!=before:raise ValueError('Compacted evidence changed')
        result={'calculations':before[0],'logical_sha256':before[1],
                'source_bytes':source.stat().st_size,'candidate_bytes':output.stat().st_size,
                'blobs':conn.execute('SELECT count(*) FROM evidence_blob').fetchone()[0]}
    output.with_suffix('.manifest.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


def catch_up(source: Path, candidate: Path):
    """Call only while all registry writers are stopped. Preserve immutable rows."""
    immutable={'calculation','scheme_version','event','policy_version','evidence_blob'}
    with sqlite3.connect(candidate,uri=True) as conn:
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('ATTACH DATABASE ? AS live',(source.resolve().as_uri()+'?mode=ro',))
        tables=[r[0] for r in conn.execute("SELECT name FROM live.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        def q(name):return '"'+name.replace('"','""')+'"'
        with conn:
            # Blob references and their payloads are committed together in the source.
            for table in ['evidence_blob']+[t for t in tables if t!='evidence_blob']:
                if table not in tables:continue
                cols=[r[1] for r in conn.execute('PRAGMA live.table_info('+q(table)+')')]
                own=[r[1] for r in conn.execute('PRAGMA main.table_info('+q(table)+')')]
                if cols!=own:raise ValueError('Schema changed during migration: '+table)
                if table in immutable:
                    key='sha' if table=='evidence_blob' else 'id'
                    rows=conn.execute('SELECT s.* FROM live.'+q(table)+' s WHERE NOT EXISTS (SELECT 1 FROM main.'+q(table)+' d WHERE d.'+q(key)+'=s.'+q(key)+')')
                    if table=='calculation':
                        for source_row in rows.fetchall():
                            row=list(source_row);row[8]=put(conn,row[8]);row[9]=put(conn,row[9])
                            conn.execute('INSERT INTO main.calculation VALUES('+','.join('?' for _ in row)+')',row)
                    else:
                        conn.execute('INSERT INTO main.'+q(table)+' SELECT s.* FROM live.'+q(table)+' s WHERE NOT EXISTS (SELECT 1 FROM main.'+q(table)+' d WHERE d.'+q(key)+'=s.'+q(key)+')')
                else:
                    conn.execute('DELETE FROM main.'+q(table))
                    conn.execute('INSERT INTO main.'+q(table)+' SELECT * FROM live.'+q(table))
            conn.execute('DELETE FROM main.sqlite_sequence')
            conn.execute('INSERT INTO main.sqlite_sequence SELECT * FROM live.sqlite_sequence')
        counts={}
        for table in tables:
            a=conn.execute('SELECT count(*) FROM live.'+q(table)).fetchone()[0]
            b=conn.execute('SELECT count(*) FROM main.'+q(table)).fetchone()[0]
            if table!='evidence_blob' and a!=b:raise ValueError('Row count mismatch: '+table)
            counts[table]=b
        print(json.dumps({'phase':'verify calculation evidence against source'}),flush=True)
        if calculation_digest(conn,'live')!=calculation_digest(conn):
            raise ValueError('Source and candidate calculation evidence differ')
        def table_hash(table,schema):
            h=hashlib.sha256()
            cols=conn.execute('PRAGMA '+schema+'.table_info('+q(table)+')').fetchall()
            keys=[r[1] for r in sorted(cols,key=lambda r:r[5]) if r[5]]
            sql='SELECT * FROM '+schema+'.'+q(table)+(' ORDER BY '+','.join(q(k) for k in keys) if keys else '')
            for row in conn.execute(sql):
                h.update(json.dumps(tuple(row),ensure_ascii=False,separators=(',',':'),default=lambda x:x.hex()).encode());h.update(b'\n')
            return h.hexdigest()
        for table in tables:
            if table not in {'calculation','evidence_blob'} and table_hash(table,'main')!=table_hash(table,'live'):
                raise ValueError('Table contents differ: '+table)
        verify_blobs(conn)
        if conn.execute('PRAGMA main.quick_check').fetchone()[0]!='ok':raise ValueError('Integrity failed after catchup')
        return {'counts':counts,'calculation_digest':calculation_digest(conn),'candidate_bytes':candidate.stat().st_size}


def compact_feed(source: Path, output: Path):
    if output.exists():raise ValueError('Feed candidate already exists')
    def signature(c):
        h=hashlib.sha256();counts={}
        for table in ['feed_state','feed_store','feed_entity','feed_pending_store']:
            columns=c.execute('pragma table_info('+table+')').fetchall()
            keys=[r[1] for r in sorted(columns,key=lambda r:r[5]) if r[5]]
            counts[table]=0
            for row in c.execute('SELECT * FROM '+table+' ORDER BY '+','.join(keys)):
                h.update(json.dumps(tuple(row),ensure_ascii=False,separators=(',',':')).encode());h.update(b'\n');counts[table]+=1
        return counts,h.hexdigest()
    with sqlite3.connect(source) as c:
        checkpoint=c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if checkpoint[0]:raise ValueError('Feed writers are still active')
        before=signature(c)
        c.execute('PRAGMA auto_vacuum=INCREMENTAL')
        c.execute('VACUUM INTO ?',(str(output),))
    with sqlite3.connect(output) as c:
        if signature(c)!=before:raise ValueError('Feed checkpoint or entities changed')
        if c.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Feed candidate is corrupt')
        if c.execute('PRAGMA auto_vacuum').fetchone()[0]!=2:raise ValueError('Incremental vacuum not enabled')
    result={'counts':before[0],'logical_sha256':before[1],'source_bytes':source.stat().st_size,'candidate_bytes':output.stat().st_size}
    output.with_suffix('.manifest.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('operation',choices=['prepare','catch-up','compact-feed']);p.add_argument('--source',type=Path,required=True);p.add_argument('--work',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    print(json.dumps(prepare(a.source,a.work,a.output) if a.operation=='prepare' else catch_up(a.source,a.output) if a.operation=='catch-up' else compact_feed(a.source,a.output)),flush=True)
