"""Offline candidate migration. Never rewrites the live registry in place."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from .snapshot_store import PREFIX, SCHEMA, put, resolve, reference_sha


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


# Columns the running ledger already knows how to read back from evidence_blob.
# result_json is expanded by the same reader. Anything shorter than 1024 bytes
# stays inline, matching snapshot_store.store.
WORKSPACE_JSON={
    'run':('result',),
    'run_report_slice':('commission_json','products_slim_json','products_json','statement_json','overview_json','manual_cost_json'),
    'manual_finance':('result_json',),
}
_TOUCH='_slim_touch'
_MIN_INLINE=1024


def _q(name):
    return '"'+str(name).replace('"','""')+'"'


def _tables(conn):
    rows=conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    found=[]
    for name, sql in rows:
        if name==_TOUCH or name.startswith('_slim_'):continue
        text=(sql or '').lstrip().upper()
        if text.startswith('CREATE VIRTUAL'):continue
        found.append((name, sql or ''))
    return found


def _rowid_table(sql):
    return 'WITHOUT ROWID' not in sql.upper()


def install_touch(conn):
    """Remember every row the live database changes after the copy starts."""
    conn.execute(f'CREATE TABLE IF NOT EXISTS {_q(_TOUCH)} (name TEXT NOT NULL, pk TEXT NOT NULL, op TEXT NOT NULL, PRIMARY KEY(name, pk))')
    have={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    made=[]
    for name, sql in _tables(conn):
        if not _rowid_table(sql):
            raise ValueError('瘦身增量日志还不支持 WITHOUT ROWID 表 '+name)
        literal=name.replace("'","''")
        for kind, when, token, op in (
            ('i','AFTER INSERT','NEW','upsert'),
            ('u','AFTER UPDATE','NEW','upsert'),
            ('d','AFTER DELETE','OLD','delete'),
        ):
            trigger=f'_slim_{kind}_{name}'
            if trigger in have:continue
            conn.execute(
                f"CREATE TRIGGER {_q(trigger)} {when} ON {_q(name)} BEGIN "
                f"INSERT INTO {_q(_TOUCH)}(name,pk,op) VALUES('{literal}', CAST({token}.rowid AS TEXT), '{op}') "
                f"ON CONFLICT(name,pk) DO UPDATE SET op='{op}'; END")
            made.append(trigger)
    return made


def uninstall_touch(conn):
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE '_slim_%'"):
        conn.execute('DROP TRIGGER IF EXISTS '+_q(name))
    conn.execute('DROP TABLE IF EXISTS '+_q(_TOUCH))


def _saved_triggers(conn):
    return [(name, sql) for name, sql in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL AND name NOT LIKE '_slim_%'")]


def _drop_triggers(conn, saved):
    for name, _sql in saved:
        conn.execute('DROP TRIGGER IF EXISTS '+_q(name))


def _restore_triggers(conn, saved):
    have={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    for name, sql in saved:
        if name in have or name.startswith('evidence_blob_'):continue
        conn.execute(sql)


def _compact_text(conn, text):
    """Store full text as a reference. A cell over the evidence limit stays inline."""
    try:
        return put(conn, text)
    except ValueError as exc:
        if 'exceeds size limit' not in str(exc):
            raise
        return None


def _convert_workspace(conn):
    """Turn stored full text into references. The copy is offline, so its triggers are parked first."""
    saved=_saved_triggers(conn)
    _drop_triggers(conn, saved)
    conn.executescript(SCHEMA)
    converted={}
    for table, columns in WORKSPACE_JSON.items():
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
            continue
        present={r[1] for r in conn.execute('PRAGMA table_info('+_q(table)+')')}
        for column in columns:
            if column not in present:continue
            done=0;skipped=0;cursor=0
            while True:
                rows=conn.execute(
                    f"SELECT rowid, {_q(column)} FROM {_q(table)} WHERE rowid>? AND typeof({_q(column)})='text' "
                    f"AND length({_q(column)})>=? AND substr({_q(column)},1,23)!=? ORDER BY rowid LIMIT 100",
                    (cursor,_MIN_INLINE,PREFIX[:23])).fetchall()
                if not rows:break
                for attempt in range(5):
                    try:
                        with conn:
                            for rowid, text in rows:
                                stored=_compact_text(conn, text)
                                if stored is None:
                                    skipped+=1
                                    print(json.dumps({'table':table,'column':column,'rowid':rowid,'skipped':'too_large','chars':len(text)}),flush=True)
                                    continue
                                conn.execute(f'UPDATE {_q(table)} SET {_q(column)}=? WHERE rowid=?',(stored,rowid))
                        break
                    except sqlite3.OperationalError as exc:
                        if 'disk I/O' not in str(exc) or attempt==4:
                            raise
                        print(json.dumps({'phase':'retry','table':table,'column':column,'attempt':attempt+1,'error':'disk I/O'}),flush=True)
                        try:
                            conn.rollback()
                        except sqlite3.Error:
                            pass
                        time.sleep(5*(attempt+1))
                cursor=rows[-1][0];done+=len(rows)
                if done%1000==0:print(json.dumps({'table':table,'column':column,'converted':done,'skipped':skipped}),flush=True)
            converted[table+'.'+column]=done-skipped
    _restore_triggers(conn, saved)
    conn.commit()
    return converted


def _digest_columns(conn):
    h=hashlib.sha256();counts={}
    for table, columns in WORKSPACE_JSON.items():
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
            continue
        present={r[1] for r in conn.execute('PRAGMA table_info('+_q(table)+')')}
        for column in columns:
            if column not in present:continue
            n=0
            for (text,) in conn.execute(f'SELECT {_q(column)} FROM {_q(table)} ORDER BY rowid'):
                if not isinstance(text,str):continue
                h.update(resolve(conn,text).encode());h.update(b'\n');n+=1
            counts[table+'.'+column]=n
    return counts,h.hexdigest()


def _table_sizes(conn):
    try:
        return {name:total for name,total in conn.execute('SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC')}
    except sqlite3.OperationalError:
        return {}


def backup_workspace(source: Path, work: Path, pages: int = 1024, pause: float = 0.05):
    """Consistent copy. With writers stopped, pass pages=0 so it finishes in one step."""
    if work.exists():raise ValueError('候选文件已存在，不覆盖')
    work.parent.mkdir(parents=True,exist_ok=True)
    live=sqlite3.connect(source,timeout=30)
    copy=None
    try:
        live.isolation_level=None
        live.execute('PRAGMA busy_timeout=120000')
        live.execute('BEGIN IMMEDIATE')
        try:
            install_touch(live)
            live.execute('COMMIT')
        except Exception:
            live.execute('ROLLBACK')
            raise
        live.execute('PRAGMA wal_checkpoint(PASSIVE)')
        copy=sqlite3.connect(work)
        def progress(_status, remaining, total):
            done=total-remaining
            step=max(pages,1)*50
            if total and (remaining==0 or done%step==0):
                print(json.dumps({'backup_done_pages':done,'backup_pages':total}),flush=True)
        live.backup(copy,pages=pages,progress=progress,sleep=pause)
    finally:
        if copy is not None:copy.close()
        live.close()
    return {'source_bytes':source.stat().st_size,'work_bytes':work.stat().st_size}


def _spot_check(conn, limit=40):
    """Confirm a handful of stored references still expand to the original bytes."""
    rows=conn.execute('SELECT sha FROM evidence_blob ORDER BY rowid DESC LIMIT ?',(limit,)).fetchall()
    for (sha,) in rows:
        resolve(conn, json.dumps({'$ledger_snapshot_v1':sha},separators=(',',':')))
    return len(rows)


def compact_remaining(work: Path, output: Path):
    """Finish rows still stored in full, then write the new file. No full-library reread."""
    print(json.dumps({'phase':'compact-remaining','work':str(work)}),flush=True)
    if output.exists():raise ValueError('候选文件已存在，不覆盖')
    with sqlite3.connect(work) as conn:
        conn.execute('PRAGMA journal_mode=DELETE')
        converted=_convert_workspace(conn)
        checked=_spot_check(conn)
        conn.commit()
        print(json.dumps({'phase':'writing','converted':converted,'spot_checked':checked}),flush=True)
        conn.execute('VACUUM INTO ?',(str(output),))
    with sqlite3.connect(output) as conn:
        if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('候选库完整性检查没通过')
        uninstall_touch(conn)
        conn.commit()
        result={'converted':converted,'spot_checked':checked,'candidate_bytes':output.stat().st_size}
    output.with_suffix('.manifest.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({'phase':'copy finished',**result}),flush=True)
    return result


def finish_workspace(work: Path, output: Path):
    """Compact a finished copy. Does not touch the live ledger."""
    if output.exists():raise ValueError('候选文件已存在，不覆盖')
    copied=work.stat().st_size
    with sqlite3.connect(work) as conn:
        conn.execute('PRAGMA journal_mode=DELETE')
        before=_digest_columns(conn)
        sizes=_table_sizes(conn)
        converted=_convert_workspace(conn)
        after=_digest_columns(conn)
        if before!=after:raise ValueError('压缩前后正文不一致')
        conn.commit()
        conn.execute('VACUUM INTO ?',(str(output),))
    with sqlite3.connect(output) as conn:
        if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('候选库完整性检查没通过')
        if _digest_columns(conn)!=before:raise ValueError('换出的候选库正文不一致')
        uninstall_touch(conn)
        conn.commit()
        result={'logical_counts':before[0],'logical_sha256':before[1],'converted':converted,
                'table_bytes':sizes,'source_bytes':copied,'candidate_bytes':output.stat().st_size,
                'blobs':conn.execute('SELECT count(*) FROM evidence_blob').fetchone()[0]}
    output.with_suffix('.manifest.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


def prepare_workspace(source: Path, work: Path, output: Path):
    """Copy the live ledger aside, compact that copy, and leave the live file untouched."""
    backup_workspace(source,work)
    return finish_workspace(work,output)


def catch_up_workspace(source: Path, candidate: Path):
    """Call only while ledger writers are stopped. Copy rows changed since the copy, then compact them too."""
    with sqlite3.connect(source,timeout=600) as live, sqlite3.connect(candidate) as dst:
        live.execute('PRAGMA busy_timeout=600000')
        checkpoint=live.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if checkpoint[0]:raise ValueError('台账仍在写入，不能补增量')
        if not live.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(_TOUCH,)).fetchone():
            raise ValueError('没有增量日志，不能补')
        saved=_saved_triggers(dst)
        _drop_triggers(dst, saved)
        touched=0
        for name, pk, op in live.execute(f'SELECT name,pk,op FROM {_q(_TOUCH)}'):
            if not live.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone():continue
            if op=='delete':
                dst.execute(f'DELETE FROM {_q(name)} WHERE rowid=?',(int(pk),))
            else:
                cols=[r[1] for r in live.execute('PRAGMA table_info('+_q(name)+')')]
                row=live.execute(f'SELECT * FROM {_q(name)} WHERE rowid=?',(int(pk),)).fetchone()
                if row is None:
                    dst.execute(f'DELETE FROM {_q(name)} WHERE rowid=?',(int(pk),))
                    continue
                values=[]
                for column, value in zip(cols, row):
                    if (column in WORKSPACE_JSON.get(name,()) and isinstance(value,str)
                            and len(value)>=_MIN_INLINE and not value.startswith(PREFIX)):
                        stored=_compact_text(dst, value)
                        if stored is None:
                            print(json.dumps({'caught_up_skipped':name,'column':column,'rowid':pk,'chars':len(value)}),flush=True)
                        else:
                            value=stored
                    values.append(value)
                dst.execute(f'INSERT OR REPLACE INTO {_q(name)} VALUES('+','.join('?'*len(values))+')',values)
            touched+=1
            if touched%1000==0:
                dst.commit();print(json.dumps({'caught_up':touched}),flush=True)
        _restore_triggers(dst, saved)
        uninstall_touch(dst)
        dst.commit()
        counts={}
        for (name, _sql) in _tables(live):
            a=live.execute('SELECT count(*) FROM '+_q(name)).fetchone()[0]
            b=dst.execute('SELECT count(*) FROM '+_q(name)).fetchone()[0]
            if name!='evidence_blob' and a!=b:raise ValueError('行数对不上: '+name)
            counts[name]=[a,b]
        if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('补增量后完整性检查没通过')
        return {'touched':touched,'counts':counts,'candidate_bytes':candidate.stat().st_size,'checkpoint':list(checkpoint)}


def self_check():
    """Exercise the copy, compact and catch-up on a throwaway file. Does not touch production."""
    import tempfile
    folder=Path(tempfile.mkdtemp(prefix='ledger-slim-'))
    source=folder/'source.db';work=folder/'work.db';output=folder/'candidate.db'
    with sqlite3.connect(source) as conn:
        conn.execute('CREATE TABLE run(id INTEGER PRIMARY KEY, result TEXT NOT NULL)')
        conn.execute('CREATE TABLE manual_finance(run_id INTEGER PRIMARY KEY, result_json TEXT NOT NULL)')
        conn.execute("CREATE TRIGGER manual_finance_no_update BEFORE UPDATE ON manual_finance BEGIN SELECT RAISE(ABORT,'immutable manual finance'); END")
        conn.execute("INSERT INTO run VALUES(1,?)",('{'+'"k":"'+('x'*2000)+'"}',))
        conn.execute('INSERT INTO manual_finance VALUES(1,?)',('{'+'"m":"'+('y'*2000)+'"}',))
        original=[r[0] for r in conn.execute('SELECT result FROM run')]
    prepare_workspace(source, work, output)
    with sqlite3.connect(source) as conn:
        conn.execute('INSERT INTO run VALUES(2,?)',('{'+'"k":"'+('z'*2000)+'"}',))
    caught=catch_up_workspace(source, output)
    with sqlite3.connect(output) as conn:
        from .snapshot_store import install_reader
        install_reader(conn)
        seen=[r['result'] for r in conn.execute('SELECT result FROM run ORDER BY id')]
        manual=conn.execute('SELECT result_json FROM manual_finance').fetchone()['result_json']
    uninstall_touch(sqlite3.connect(source))
    if seen!=original+['{'+'"k":"'+('z'*2000)+'"}'] or '"m":' not in manual:
        raise ValueError('self-check readback mismatch')
    if not caught['touched']:raise ValueError('self-check did not catch the new row')
    return {'ok':True,'folder':str(folder),'touched':caught['touched']}


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('operation',choices=['prepare','catch-up','compact-feed','prepare-workspace','backup-workspace','finish-workspace','compact-remaining','catch-up-workspace','uninstall-touch','self-check'])
    p.add_argument('--source',type=Path);p.add_argument('--work',type=Path);p.add_argument('--output',type=Path);a=p.parse_args()
    if a.operation=='self-check':
        print(json.dumps(self_check()),flush=True)
    elif a.operation=='prepare':
        print(json.dumps(prepare(a.source,a.work,a.output)),flush=True)
    elif a.operation=='catch-up':
        print(json.dumps(catch_up(a.source,a.output)),flush=True)
    elif a.operation=='compact-feed':
        print(json.dumps(compact_feed(a.source,a.output)),flush=True)
    elif a.operation=='prepare-workspace':
        print(json.dumps(prepare_workspace(a.source,a.work,a.output)),flush=True)
    elif a.operation=='backup-workspace':
        print(json.dumps(backup_workspace(a.source,a.work,pages=int(os.environ.get('LEDGER_BACKUP_PAGES','-1')),pause=float(os.environ.get('LEDGER_BACKUP_PAUSE','0')))),flush=True)
    elif a.operation=='finish-workspace':
        print(json.dumps(finish_workspace(a.work,a.output)),flush=True)
    elif a.operation=='compact-remaining':
        compact_remaining(a.work,a.output)
    elif a.operation=='catch-up-workspace':
        print(json.dumps(catch_up_workspace(a.source,a.output)),flush=True)
    else:
        with sqlite3.connect(a.source) as conn:
            uninstall_touch(conn);conn.commit()
        print(json.dumps({'uninstalled':True}),flush=True)
