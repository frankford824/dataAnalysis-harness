"""Shared freight source claims: derived indexes, never financial corrections."""
from collections import defaultdict
from contextlib import closing
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
import hashlib,json,sqlite3,time
from pathlib import Path

import polars as pl
from . import automation_store as journal
from .storage_integrity import digest as file_sha, verified, registered_archive_link

EPS=Decimal('.000001')
CENT=Decimal('.01')
VERSION=1
_SCHEMA="""
CREATE TABLE IF NOT EXISTS indexed_run(
 run_id INTEGER PRIMARY KEY,store_id TEXT,period TEXT,facts_sha TEXT,mtime_ns INTEGER,bytes INTEGER,
 totals_sha TEXT,status TEXT,reason TEXT,claims_sha TEXT,at REAL);
CREATE TABLE IF NOT EXISTS claim(
 run_id INTEGER,store_id TEXT,period TEXT,file_sha TEXT,sheet TEXT,row_no INTEGER,metric_id TEXT,
 raw_amount TEXT,contribution TEXT,PRIMARY KEY(run_id,file_sha,sheet,row_no,metric_id));
CREATE INDEX IF NOT EXISTS claim_source ON claim(file_sha,sheet,row_no,metric_id,run_id);
CREATE TABLE IF NOT EXISTS generation(id INTEGER PRIMARY KEY,value INTEGER);
INSERT OR IGNORE INTO generation VALUES(1,0);
"""


def D(value):
    try:
        result=Decimal(str(value))
        if result.is_finite():return result
    except (InvalidOperation,ValueError,TypeError):pass
    raise ValueError('shared_fee_numeric_evidence_invalid')


def canonical(value):
    result=D(value)
    return '0' if not result else format(result.normalize(),'f')


def _path(ws):
    folder=ws.root/'cache'
    if folder.is_symlink():raise ValueError('shared_fee_cache_path_invalid')
    folder.mkdir(parents=True,exist_ok=True)
    path=folder/'shared-fee-source-v1.sqlite3'
    if path.is_symlink():raise ValueError('shared_fee_cache_path_invalid')
    return path


def _connect(ws):
    db=sqlite3.connect(_path(ws),timeout=5);db.row_factory=sqlite3.Row
    db.executescript(_SCHEMA)
    return db


def _metrics(model):
    return {m.id for m in model.metrics if m.source=='freight'} if model else {'freight_cost'}


def _payload(ws,row):
    # Workspace's registered snapshot reader expands immutable JSON references.
    return json.loads(row['result'])


def _artifact(ws,row):
    proof=ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(row['id'],)).fetchone()
    path=ws.facts_path(row['id']);expected=None
    if proof:
        manifest=json.loads(proof['manifest_json'])
        found=[a for a in manifest.get('artifacts',[]) if Path(a['path']).name==str(row['id'])+'.parquet']
        if len(found)!=1:raise ValueError('shared_fee_original_capture_missing')
        expected=found[0]['sha']
    if not path.is_file() or not verified(path):raise ValueError('shared_fee_facts_missing_or_unsealed')
    if not path.resolve().is_relative_to(ws.root.resolve()) and not registered_archive_link(ws.root,path,expected or file_sha(path)):
        raise ValueError('shared_fee_artifact_not_registered')
    actual=file_sha(path)
    if expected and expected!=actual:raise ValueError('shared_fee_original_capture_changed')
    return path,actual


def index_run(ws,run_id,*,model=None,facts=None,payload=None):
    """Index one effective sealed artifact. Reads no original workbook or old DB blobs."""
    row=ws.conn.execute('SELECT * FROM run WHERE id=?',(run_id,)).fetchone()
    if not row:return {'status':'missing','reason':'shared_fee_run_missing'}
    row=dict(row);payload=payload if payload is not None else _payload(ws,row)
    if not row['evidence_ready'] or row['evidence_error']:
        return {'run_id':run_id,'status':'pending','reason':'shared_fee_run_not_published'}
    metrics=_metrics(model);totals=(payload.get('calculation_inputs') or {}).get('metric_totals') or {}
    selected={k:totals.get(k) for k in metrics};totals_sha=journal.digest(selected)
    cached=None
    with closing(_connect(ws)) as db:
        cached=db.execute('SELECT * FROM indexed_run WHERE run_id=?',(run_id,)).fetchone()
    try:
        path=ws.facts_path(run_id)
        # First-proof SHA and stat bind reuse. Final close also verifies the run's
        # sealed original artifacts through the existing Checker.
        proof=ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(run_id,)).fetchone()
        expected=None
        if proof:
            expected=next((a['sha'] for a in json.loads(proof['manifest_json']).get('artifacts',[])
                if Path(a['path']).name==str(run_id)+'.parquet'),None)
        stat=path.stat()
        if (cached and cached['status']=='ready' and cached['totals_sha']==totals_sha
                and cached['mtime_ns']==stat.st_mtime_ns and cached['bytes']==stat.st_size
                and (expected is None or expected==cached['facts_sha'])):
            return dict(cached)
        if stat.st_size>256*1024*1024:raise ValueError('shared_fee_index_budget_requires_background')
        path,actual=_artifact(ws,row);stat=path.stat()
        schema=pl.scan_parquet(path).collect_schema().names()
        columns={'metric_id','file_sha','sheet','row_no','counted','contribution','amount','store','period'}
        claims=[]
        if 'metric_id' in schema:
            frame=pl.scan_parquet(path).filter(pl.col('metric_id').is_in(sorted(metrics)))
            if columns<=set(schema):
                frame=frame.filter(pl.col('counted')&(pl.col('contribution')!=0)).select(sorted(columns)).collect()
                aggregated={};sums=defaultdict(Decimal)
                for item in frame.iter_rows(named=True):
                    if item.get('period')!=row['period']:raise ValueError('shared_fee_period_identity_invalid')
                    if model:
                        store=model.store(row['store_id'])
                        if item.get('store') not in {store.id,store.name,*store.aliases}:
                            raise ValueError('shared_fee_store_identity_invalid')
                    sha=str(item.get('file_sha') or '');sheet=str(item.get('sheet') or '')
                    ordinal=item.get('row_no')
                    if len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha) or not isinstance(ordinal,int) or ordinal<1:
                        raise ValueError('shared_fee_source_identity_unverified')
                    raw=D(item['amount']);contribution=D(item['contribution'])
                    if raw*contribution<0:raise ValueError('shared_fee_claim_sign_conflict')
                    key=(sha,sheet,ordinal,item['metric_id'])
                    if key in aggregated and abs(aggregated[key][0]-raw)>EPS:raise ValueError('shared_fee_raw_budget_conflict')
                    prior=aggregated.get(key,(raw,Decimal(0)));aggregated[key]=(raw,prior[1]+contribution)
                    sums[item['metric_id']]+=contribution
                for metric,total in sums.items():
                    booked=totals.get(metric)
                    # counted is not enough: the original actual metric/node
                    # must also certify that this scope booked the source money.
                    if booked is None or abs(total.quantize(CENT,rounding=ROUND_HALF_UP)-D(booked))>EPS:
                        raise ValueError('shared_fee_actual_posting_unverified')
                claims=[(run_id,row['store_id'],row['period'],*key,canonical(raw),canonical(value))
                    for key,(raw,value) in sorted(aggregated.items())]
            elif frame.select(pl.len()).collect().item():
                raise ValueError('shared_fee_source_identity_unverified')
        if not claims and any(value is not None and abs(D(value))>EPS for value in selected.values()):
            raise ValueError('shared_fee_source_identity_unverified')
        claims_sha=journal.digest(claims)
        status,reason='ready',''
    except (OSError,ValueError,pl.exceptions.PolarsError) as exc:
        actual=expected or '';stat=None;claims=[];claims_sha=''
        status,reason='pending',str(exc) if isinstance(exc,ValueError) else 'shared_fee_artifact_unavailable'
    with closing(_connect(ws)) as db,db:
        db.execute('DELETE FROM claim WHERE run_id=?',(run_id,))
        if claims:db.executemany('INSERT INTO claim VALUES(?,?,?,?,?,?,?,?,?)',claims)
        db.execute('INSERT OR REPLACE INTO indexed_run VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (run_id,row['store_id'],row['period'],actual,stat.st_mtime_ns if stat else 0,stat.st_size if stat else 0,
             totals_sha,status,reason,claims_sha,time.time()))
        db.execute('UPDATE generation SET value=value+1 WHERE id=1')
    return {'run_id':run_id,'status':status,'reason':reason,'claims_sha':claims_sha}


def _effective(ws,model):
    rows=ws.conn.execute("""SELECT p.store_id,p.period,p.state,
       CASE WHEN p.state='closed' AND p.run_id IS NOT NULL THEN p.run_id ELSE
        (SELECT max(r.id) FROM run r WHERE r.store_id=p.store_id AND r.period=p.period) END run_id
       FROM period p ORDER BY p.store_id,p.period""").fetchall()
    included={}
    if model:
        from .automation_checker import operating_stores
        for period in {r['period'] for r in rows}:included[period]={s.id for s in operating_stores(ws,model,period)}
    result=[dict(r) for r in rows if r['run_id'] and (not model or r['store_id'] in included[r['period']])]
    for row in result:
        source=ws.conn.execute('SELECT shas,evidence_ready,evidence_error FROM run WHERE id=?',(row['run_id'],)).fetchone()
        row['source_shas']=json.loads(source['shas'] or '[]') if source else []
        row['published']=bool(source and source['evidence_ready'] and not source['evidence_error'])
    return result


def warm(ws,*,model=None,budget=1,source_files=None):
    """Bounded migration of current/frozen run artifacts, not a history recompute."""
    rows=_effective(ws,model);attempts=[]
    with closing(_connect(ws)) as db:
        known={r['run_id']:dict(r) for r in db.execute('SELECT * FROM indexed_run')}
    for row in rows:
        if source_files and row['source_shas'] and not set(source_files)&set(row['source_shas']):continue
        old=known.get(row['run_id'])
        if old and (old['status']=='ready' or time.time()-old['at']<300):continue
        attempts.append(index_run(ws,row['run_id'],model=model))
        if len(attempts)>=budget:break
    return attempts


def capture(ws,run_id,*,model=None,payload=None):
    """A failed derived index delays readiness; it never rewrites the run."""
    try:return index_run(ws,run_id,model=model,payload=payload)
    except (OSError,ValueError,sqlite3.Error,pl.exceptions.PolarsError):
        return {'status':'pending','reason':'shared_fee_index_unavailable'}


def _issue(code,message,**extra):
    return {'code':code,'message':message,'hard':True,'blocking':True,**extra}


def _evaluate(ws,store_id,period,run_id,*,model=None,closed=False,payload=None):
    """Only open scopes are blocked. Frozen history receives a risk description."""
    if not run_id or not _metrics(model):return {'status':'not_applicable','issues':[],'risks':[],'input_sha':journal.digest([])}
    current=index_run(ws,run_id,model=model,payload=payload)
    if current['status']!='ready':
        problem=_issue('shared_fee_evidence_unverified','共享运费原收费与实际计入证据尚未核验，系统将继续建立索引。',reason_code=current['reason'])
        return {'status':'risk_only' if closed else 'pending','issues':[] if closed else [problem],
            'risks':[problem] if closed else [],'input_sha':journal.digest(['pending',run_id,current['reason']])}
    with closing(_connect(ws)) as db:
        own=[dict(r) for r in db.execute('SELECT * FROM claim WHERE run_id=?',(run_id,))]
    if not own:return {'status':'clear','issues':[],'risks':[],'input_sha':journal.digest([])}
    files=sorted({r['file_sha'] for r in own})
    warm(ws,model=model,budget=1,source_files=files)
    effective=_effective(ws,model)
    with closing(_connect(ws)) as db:
        indexed={r['run_id']:dict(r) for r in db.execute('SELECT * FROM indexed_run')}
        target_files=set(files)
        relevant=[r for r in effective if not r['source_shas'] or target_files&set(r['source_shas']) or r['run_id']==run_id]
        missing=[r for r in relevant if not r['published'] or r['run_id'] not in indexed or indexed[r['run_id']]['status']!='ready']
        # Cold neighbors cannot silently become zero claims.
        if missing:
            problem=_issue('shared_fee_index_pending','共享运费跨店制品索引尚未完成，开放账期将等待自动核验。',pending_scope_count=len(missing))
            return {'status':'risk_only' if closed else 'pending','issues':[] if closed else [problem],
                'risks':[problem] if closed else [],'input_sha':journal.digest(['pending',[(r['store_id'],r['period']) for r in missing]])}
        ids=sorted({r['run_id'] for r in relevant})
        placeholders=','.join('?' for _ in ids)
        # The current run's sparse counted keys drive indexed lookups. Never
        # scan every contribution sharing a whole company carrier workbook.
        all_claims=[dict(r) for r in db.execute('''SELECT other.* FROM claim own JOIN claim other
            ON own.file_sha=other.file_sha AND own.sheet=other.sheet AND own.row_no=other.row_no
              AND own.metric_id=other.metric_id
            WHERE own.run_id=? AND other.run_id IN ('''+placeholders+')',[run_id,*ids])]
    targets={(r['file_sha'],r['sheet'],r['row_no'],r['metric_id']) for r in own};buckets=defaultdict(list)
    for row in all_claims:
        key=(row['file_sha'],row['sheet'],row['row_no'],row['metric_id'])
        if key in targets:buckets[key].append(row)
    conflicts=[];semantic=[]
    for key,claims in sorted(buckets.items()):
        claims.sort(key=lambda r:(r['store_id'],r['period'],r['run_id']))
        values={canonical(r['raw_amount']) for r in claims};raw=D(claims[0]['raw_amount'])
        total=sum((D(r['contribution']) for r in claims),Decimal(0))
        identity=len(values)>1
        excessive=abs(total)>abs(raw)+EPS or total*raw<0
        semantic.append([key,sorted((r['store_id'],r['period'],canonical(r['raw_amount']),canonical(r['contribution'])) for r in claims)])
        if identity or excessive:
            conflicts.append({'source_key':{'file_sha':key[0],'sheet':key[1],'row_no':key[2],'metric_id':key[3]},
                'raw_amount':canonical(raw),'effective_contribution':canonical(total),
                'reason_code':'shared_fee_identity_conflict' if identity else 'shared_fee_source_overcount','claims':claims})
    signature=journal.digest(semantic)
    if not conflicts:return {'status':'clear','issues':[],'risks':[],'input_sha':signature}
    source_keys=sorted(journal.digest(c['source_key']) for c in conflicts)
    body={'schema':VERSION,'kind':'effective_shared_freight_source_conflicts','conflicts':conflicts,
        'source_input_sha':journal.digest(conflicts),'financial_history_modified':False}
    digest=journal.digest(body);folder=ws.root/'cache/shared-fee-conflicts'
    if folder.is_symlink():raise ValueError('shared_fee_cache_path_invalid')
    folder.mkdir(parents=True,exist_ok=True);path=folder/(digest+'.json')
    if not path.exists():path.write_text(journal.encoded(body),encoding='utf-8')
    problem=_issue('shared_fee_source_conflict','同一原始运费收费被有效店期重复认领或身份冲突，已合并证据待核；开放账期不能确认完成。',
        conflict_count=len(conflicts),evidence_ref={'path':path.relative_to(ws.root).as_posix(),'sha256':file_sha(path)},
        owner_scope={'kind':'shared_source','source_group_sha':journal.digest(source_keys),'source_key_shas':source_keys})
    return {'status':'risk_only' if closed else 'blocked','issues':[] if closed else [problem],
        'risks':[problem] if closed else [],'input_sha':signature}


def evaluate(ws,store_id,period,run_id,*,model=None,closed=False,payload=None):
    try:return _evaluate(ws,store_id,period,run_id,model=model,closed=closed,payload=payload)
    except (OSError,ValueError,sqlite3.Error,pl.exceptions.PolarsError):
        problem=_issue('shared_fee_evidence_unverified','共享运费源制品暂不可验证，系统将继续取证；不能把未知视为已完成。')
        return {'status':'risk_only' if closed else 'pending','issues':[] if closed else [problem],
            'risks':[problem] if closed else [],'input_sha':journal.digest(['unverified',run_id])}
