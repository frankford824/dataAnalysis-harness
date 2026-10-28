"""Original-order dates from raw ERP nodes, independently of cost/quantity DAG.

Only single-parent original-sale identity and its literal date are proved here.
No quantity, price, shipping, sales or monetary authority is produced.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
import csv,hashlib,io,json,re
from pathlib import Path
import polars as pl

from . import automation_store as journal
from .jst_source_members import Node,parse_order,MemberPending

VERSION='verified_refund_order_dates.v1'
HEADERS=('退款原订单号','原订单下单时间','核对店铺名称','核对聚水潭订单号')


def potential_parents(ingestion,model,store,period,facts=None):
    """Current source-period identifiers, including unposted/excluded rows.

    Original sale month is deliberately not a filter: a September transaction
    may need a July original date. Missing occurrence dates remain possible
    dependencies rather than being guessed into or out of a month.
    """
    from .engine.link import normalize_key
    keys=set()
    if facts is not None:
        for column in ('order_id','link_key'):
            if column in facts.columns:
                keys.update(normalize_key(v) for v in facts[column].drop_nulls().to_list())
    source_items=getattr(ingestion,'source_recovery_originals',getattr(ingestion,'known',()))
    metrics=[m for raw in model.metrics if raw.id in ('trade_refund','trade_refund_douyin')
             and (m:=raw.for_platform(store.platform)) is not None and m.link]
    for metric in metrics:
        for item in source_items:
            if item.template is None or item.template.source!=metric.source:continue
            frame=item.frame;key=metric.link.key
            if frame is None or key not in frame.columns:continue
            if 'store_name' in frame.columns:
                frame=frame.filter(pl.col('store_name').is_null()|pl.col('store_name').is_in(['',store.id,store.name,*store.aliases]))
            slot=str(metric.orderless_time_basis or metric.time_basis)
            if slot not in frame.columns:slot=item.template.time_slots.get(slot,slot)
            if slot in frame.columns:
                month=pl.col(slot).cast(pl.String).str.slice(0,7)
                valid=month.str.contains(r'^\d{4}-(0[1-9]|1[0-2])$',literal=False).fill_null(False)
                frame=frame.filter((month==period)|~valid)
            elif '__hint_period__' in frame.columns:
                frame=frame.filter(pl.col('__hint_period__').is_null()|(pl.col('__hint_period__')==period))
            keys.update(normalize_key(v) for v in frame[key].drop_nulls().to_list())
    return {'schema':VERSION,'store_id':store.id,'period':period,
            'potential_parent_ids':sorted(k for k in keys if k)}



def _selected_date_rows(ws,store_id):
    """Explicit trusted heads version dates; legacy equal facts stay readable."""
    rows=ws.conn.execute("SELECT a.id,a.result_json,c.store_id AS origin_store FROM automation_attempt a LEFT JOIN automation_case c ON c.id=a.case_id WHERE a.state='date_source_fact'").fetchall()
    heads={}
    for row in ws.conn.execute("SELECT result_json FROM automation_attempt WHERE state='date_source_head'"):
        value=json.loads(row[0])
        if value.get('schema')==VERSION and value.get('store_id')==store_id:
            heads[str(value['platform_order_id'])]=value
    selected=[];found=set()
    for row in rows:
        try:value=json.loads(row['result_json'])
        except (ValueError,TypeError):
            if row['origin_store']==store_id:selected.append(row)
            continue
        if value.get('store_id')!=store_id:continue
        pid=str(value.get('platform_order_id') or '');head=heads.get(pid)
        if head:
            if head['fact_id']!=row['id']:continue
            if head['fact_sha']!=journal.digest(value):raise MemberPending('date_source_head_fact_changed')
            found.add(pid)
        selected.append(row)
    if set(heads)-found:raise MemberPending('date_source_head_fact_missing')
    return selected

def fingerprint(ws,store_id,period,dependencies=None):
    """Semantic facts intersected with this run's potential input identities."""
    if dependencies is None:
        row=ws.latest_run(store_id,period)
        proof=ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(row['id'],)).fetchone() if row else None
        validation=ws.conn.execute('SELECT manifest_json FROM automation_run_validation WHERE run_id=? ORDER BY rowid DESC LIMIT 1',(row['id'],)).fetchone() if row else None
        manifest=json.loads((validation or proof)[0]) if validation or proof else {}
        dependencies=manifest.get('original_date_dependencies')
        if dependencies is None:
            # A previous source-index release can still supply its explicitly
            # sealed demanded identities; never manufacture new raw anchors.
            index=manifest.get('source_recovery_index',{})
            ids=set(index.get('orders',{}))|{d['platform_order_id'] for d in index.get('date_demands',{}).values()}
            dependencies={'potential_parent_ids':sorted(ids)}
    relevant=set(map(str,dependencies.get('potential_parent_ids',[])));values={};invalid=[]
    rows=_selected_date_rows(ws,store_id)
    for row in rows:
        try:value=json.loads(row['result_json'])
        except (ValueError,TypeError):
            if row['origin_store']==store_id:invalid.append(row['id'])
            continue
        if value.get('schema')!=VERSION or value.get('store_id')!=store_id:continue
        pid=str(value.get('platform_order_id') or '')
        if pid not in relevant:continue
        day=value.get('original_day')
        if not isinstance(day,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day):
            invalid.append(row['id']);continue
        values.setdefault(pid,set()).add(day)
    return journal.digest({'schema':VERSION,'store_id':store_id,'period':period,
        'date_facts':[(pid,sorted(days)) for pid,days in sorted(values.items())],'unverified_rows':sorted(invalid)})


def _parent(value,shop):
    text=str(value or '').strip()
    if ':' in text:
        prefix,text=text.split(':',1)
        if prefix!=shop:return None
    return text if re.fullmatch(r'[1-9][0-9]{0,30}',text) else None


def _true(value):
    return value is True or str(value).casefold() in ('1','true')


def prove_original_day(platform_order_id,shop_ids,nodes,blobs):
    """Typed raw references only; Worker state/prose is never a date proof."""
    target=str(platform_order_id);shops=set(map(str,shop_ids));accepted=[];pending=[]
    if not re.fullmatch(r'[1-9][0-9]{0,30}',target) or not shops:
        return {'schema':VERSION,'status':'pending','reasons':['original_date_identity_missing'],'candidates':[]}
    for node in nodes:
        control={'order_id':node.order_id,'raw_sha256':node.sha256,'accepted':False}
        try:
            if not isinstance(node,Node) or node.sha256 not in blobs or hashlib.sha256(blobs[node.sha256]).hexdigest()!=node.sha256:
                raise MemberPending('original_date_raw_sha_changed')
            raw,span=parse_order(blobs[node.sha256],node.order_id)
            shop=str(raw.get('shop_id') or '')
            if shop not in shops:raise MemberPending('original_date_shop_conflict')
            if _parent(raw.get('so_id'),shop)!=target:raise MemberPending('source_parent_not_exact_single_target')
            if raw.get('type') not in ('普通订单','销售订单'):raise MemberPending('non_original_sale_header')
            # A node later marked Merged can still be the original order.
            # Explicit foreign/opaque merge parents cannot supply its date.
            merged=raw.get('merge_so_id')
            if merged not in (None,'',0,'0',False):
                tokens=[t for t in re.split(r'[,，;；\s]+',str(merged)) if t]
                if not tokens or any(_parent(t,shop)!=target for t in tokens):
                    raise MemberPending('merged_container_has_other_or_opaque_parents')
            items=raw.get('items',[]);bound_items=0
            for item in items:
                parents=[item[k] for k in ('so_id','raw_so_id','oi_so_id','outer_so_id') if item.get(k) not in (None,'')]
                if parents:
                    if any(_parent(v,str(item.get('shop_id') or shop))!=target for v in parents):
                        raise MemberPending('source_item_has_other_original_parent')
                    bound_items+=1
            if _true(raw.get('is_merge')) and (not items or bound_items!=len(items)):
                raise MemberPending('merged_node_original_members_not_enumerated')
            literal=raw.get('order_date')
            if not isinstance(literal,str):raise MemberPending('original_date_literal_missing')
            try:
                stamp=datetime.fromisoformat(literal.replace('Z','+00:00'))
            except ValueError as exc:raise MemberPending('original_date_literal_invalid') from exc
            if stamp.tzinfo:stamp=stamp.astimezone(ZoneInfo('Asia/Shanghai'))
            control.update(accepted=True,original_day=stamp.date().isoformat(),original_time=literal,
                original_month=stamp.strftime('%Y-%m'),source_span=span,
                source_status=raw.get('status'),source_is_merge=raw.get('is_merge'),
                basis='single exact original platform parent and no foreign observed original members')
            accepted.append(control)
        except (MemberPending,KeyError,TypeError) as exc:
            control['reason']=getattr(exc,'reason','original_date_source_unverified');pending.append(control)
    days={r['original_day'] for r in accepted}
    if len(days)!=1:
        return {'schema':VERSION,'status':'pending','reasons':['original_date_conflict' if len(days)>1 else 'original_date_not_proved'],
                'candidates':accepted,'excluded':pending,'quantity_or_cost_admitted':False}
    return {'schema':VERSION,'status':'proved','platform_order_id':target,'original_day':next(iter(days)),
            'source_order_ids':sorted({r['order_id'] for r in accepted}),'candidates':accepted,'excluded':pending,
            'worker_status_used':False,'quantity_or_cost_admitted':False,'financial_applied':False}


def input_csv(store_name,proofs):
    rows={}
    for proof in proofs:
        if proof.get('schema')!=VERSION or proof.get('status')!='proved':continue
        key=str(proof['platform_order_id']);day=proof['original_day']
        if key in rows and rows[key][0]!=day:raise MemberPending('source_date_input_conflict')
        rows[key]=(day,','.join(proof['source_order_ids']))
    stream=io.StringIO(newline='');writer=csv.writer(stream,lineterminator='\n');writer.writerow(HEADERS)
    for key,(day,oids) in sorted(rows.items()):writer.writerow((key,day,store_name,oids))
    return ('\ufeff'+stream.getvalue()).encode('utf-8')


def attach_input(ingestion,path,model,store):
    """The existing refund_order_dates importer remains the only input format."""
    from .engine.runtime import ingest
    parsed=ingest([Path(path)],model,[store.name,*store.aliases],default_store=store.name)
    if len(parsed.known)!=1 or parsed.known[0].template.source!='refund_order_dates':
        raise MemberPending('refund_date_input_template_not_recognized')
    existing={r.ref.sha256 for r in ingestion.items}
    for item in parsed.known:
        if item.ref.sha256 not in existing:ingestion.items.append(item)
    return parsed.known[0]


def from_fixed_audit(capture_path,expected_sha,raw_root,*,expected_store_id,shop_ids):
    """Trusted provider import, not an HTTP/model-supplied JSON/path action.

    Both the captured envelope and EVERY used raw byte are independently read.
    A framework's raw_ready/pending label is deliberately irrelevant to dates.
    The caller binds this configured provider to its current approved case scope.
    """
    path=Path(capture_path);raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected_sha:raise MemberPending('source_capture_sha_changed')
    value=json.loads(raw);proofs=[]
    for record in value.get('records',[]):
        if record.get('kind')!='audit_source':continue
        plan=record['plan']
        if plan.get('purpose')!='original_day' or plan.get('store_id')!=expected_store_id:
            raise MemberPending('source_date_capture_scope_changed')
        permitted=set(map(str,shop_ids))
        if not set(map(str,plan['shop_ids']))<=permitted:raise MemberPending('source_date_mapping_not_confirmed')
        nodes=[];blobs={}
        for ref in record['receipt']['nodes']:
            sha=ref['sha256']
            if not re.fullmatch(r'[0-9a-f]{64}',sha):raise MemberPending('source_date_blob_identity_invalid')
            source=Path(raw_root)/(sha+'.raw')
            if source.is_symlink() or not source.resolve().is_relative_to(Path(raw_root).resolve()):
                raise MemberPending('source_date_blob_path_changed')
            body=source.read_bytes()
            if hashlib.sha256(body).hexdigest()!=sha:raise MemberPending('source_date_raw_sha_changed')
            nodes.append(Node(str(ref['order_id']),sha));blobs[sha]=body
        proof=prove_original_day(plan['platform_order_id'],plan['shop_ids'],nodes,blobs)
        proof.update(task_id=record['task_id'],job_id=record['job_id'],capture_sha256=expected_sha,
                     original_receipt_state=record['receipt']['state'])
        proofs.append(proof)
    return proofs


def publish_from_task(ws,model,case,plan,task_id,job_id,receipt_sha256,actor):
    """Verified date source facts, with the same case scope and durable outbox.

    Called only by the trusted source-receipt handler. Worker JSON dates are
    ignored; original bytes are reopened independently. No financial amount,
    order row or historical run is written.
    """
    from .publication_scope import load_automatic,PublicationScopeError
    from .source_recovery import _json,_current
    from .finance_guard import guard
    try:
        scope=load_automatic(ws.root,model=model);scope.require(case['store_id'],case['period'],workspace_root=ws.root)
    except PublicationScopeError:
        return {'status':'pending','reason':'source_date_publication_scope_unapproved','financial_applied':False}
    if (not actor or actor.get('kind')!='service' or not actor.get('authenticated')
        or 'business' not in actor.get('scopes',[])):
        raise PermissionError('日期取证需要已认证的服务身份')
    if not any(s.id=='refund_order_dates' for s in model.sources):
        return {'status':'pending','reason':'refund_date_source_contract_unconfigured','financial_applied':False}
    folder=ws.root/'source-recovery'/task_id/job_id;path=folder/'receipt.json'
    raw=path.read_bytes()
    if path.is_symlink() or hashlib.sha256(raw).hexdigest()!=receipt_sha256:
        raise MemberPending('source_date_receipt_changed')
    receipt=_json(raw);blobs={};nodes=[]
    for ref in receipt['nodes']:
        sha=ref['sha256']
        if not re.fullmatch(r'[0-9a-f]{64}',sha):raise MemberPending('source_date_blob_identity_invalid')
        source=folder/(sha+'.raw')
        if source.is_symlink():raise MemberPending('source_date_blob_path_changed')
        body=source.read_bytes()
        if hashlib.sha256(body).hexdigest()!=sha:raise MemberPending('source_date_raw_sha_changed')
        blobs[sha]=body;nodes.append(Node(str(ref['order_id']),sha))
    proof=prove_original_day(plan['platform_order_id'],plan['shop_ids'],nodes,blobs)
    if proof['status']!='proved':return {**proof,'financial_applied':False}
    source_refs=[{'path':str(folder/(n.sha256+'.raw')),'sha256':n.sha256,'order_id':n.order_id} for n in nodes]
    value={'schema':VERSION,'store_id':case['store_id'],'case_period':case['period'],
           'platform_order_id':proof['platform_order_id'],'original_day':proof['original_day'],
           'shop_ids':plan['shop_ids'],
           'source_order_ids':proof['source_order_ids'],'proof':proof,'source_refs':source_refs,
           'source_task_id':task_id,'source_attempt_id':job_id,'publication_scope_identity':dict(scope.identity)}
    with guard(ws.root,case['store_id'],case['period']),ws.transaction() as conn:
        scope.require(case['store_id'],case['period'],workspace_root=ws.root)
        _current(ws,model,case['id'],case['run_id'],case['input_sha'])
        current=[]
        for row in _selected_date_rows(ws,case['store_id']):
            saved=json.loads(row['result_json'])
            if saved.get('platform_order_id')==proof['platform_order_id']:current.append((row['id'],saved))
        if current and {saved['original_day'] for _,saved in current}=={proof['original_day']}:
            # New raw bytes are a technical observation of the same date,
            # not another financial input or a reason to rerun the month.
            observed={**value,'source_refs':[*source_refs,
                {'path':str(path),'sha256':receipt_sha256,'role':'source_receipt'}],
                'canonical_fact_ids':sorted(i for i,_ in current),'financial_applied':False}
            key='date-source-observation:'+journal.digest(observed);oid=journal.digest(key)[:32]
            conn.execute('INSERT OR IGNORE INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
                (oid,case['id'],key,case['input_sha'],'date_source_observation',journal.encoded(observed),journal.now()))
            return {'status':'source_input_reused','source_fact_ids':sorted(i for i,_ in current),
                'original_day':proof['original_day'],'observation_id':oid,'source_refs':observed['source_refs'],
                'recompute_required':False,'financial_applied':False,'business_complete':False}
        value['supersedes_fact_ids']=sorted(i for i,_ in current)
        key='date-source:'+journal.digest(value)
        existing=conn.execute("SELECT id FROM automation_attempt WHERE request_id=? AND state='date_source_fact'",(key,)).fetchone()
        fact_id=existing[0] if existing else journal.ident()
        if not existing:
            conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
                (fact_id,case['id'],key,case['input_sha'],'date_source_fact',journal.encoded(value),journal.now()))
            head_id='date-source-head:'+journal.digest([case['store_id'],proof['platform_order_id']])
            head={'schema':VERSION,'store_id':case['store_id'],'platform_order_id':proof['platform_order_id'],
                  'fact_id':fact_id,'fact_sha':journal.digest(value),'source_task_id':task_id}
            conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET input_sha=excluded.input_sha,result_json=excluded.result_json,at=excluded.at',
                (head_id,case['id'],head_id,case['input_sha'],'date_source_head',journal.encoded(head),journal.now()))
            conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
                (journal.ident(),case['id'],key,case['input_sha'],'dispatch_pending',
                 journal.encoded({'store_id':case['store_id'],'fingerprint':'automation:'+journal.digest([key,case['input_sha']]),
                                  'publication_scope_identity':dict(scope.identity),'target_period':case['period']}),journal.now()))
        return {'status':'source_input_queued','source_fact_key':key,'source_fact_id':fact_id,'original_day':proof['original_day'],
                'recompute_required':True,'financial_applied':False,'business_complete':False}


def attach_saved(ws,model,store,ingestion,publication_scope):
    """Read reusable date evidence; service separately fences publication.

    Originating case month is provenance, not permanent source read authority.
    A legitimate human recalculation also reads these independently rechecked
    facts. Automatic publication still requires the service's current scope.
    """
    if publication_scope is not None:publication_scope.verify(ws.root,model)
    if not any(s.id=='refund_order_dates' for s in model.sources):return {'attached':0}
    if not ws.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='automation_attempt'").fetchone():
        return {'attached':0}
    facts=[]
    for row in _selected_date_rows(ws,store.id):
        value=json.loads(row['result_json'])
        if value.get('schema')!=VERSION or value.get('store_id')!=store.id:continue
        # Source date/current case month never truncates source evidence.
        # Only service.record/reuse/case publication uses current authority.
        nodes=[];blobs={}
        for ref in value['source_refs']:
            source=Path(ref['path'])
            if source.is_symlink() or not source.resolve().is_relative_to(ws.root.resolve()):
                raise MemberPending('saved_date_source_path_changed')
            raw=source.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=ref['sha256']:raise MemberPending('saved_date_source_raw_changed')
            nodes.append(Node(ref['order_id'],ref['sha256']));blobs[ref['sha256']]=raw
        proof=prove_original_day(value['platform_order_id'],value['shop_ids'],nodes,blobs)
        if proof['status']!='proved' or proof['original_day']!=value['original_day']:
            raise MemberPending('saved_date_source_no_longer_verified')
        facts.append((value,proof))
    if not facts:return {'attached':0}
    # One stable original-parent input prevents another month's added date
    # from changing this parent's filename/row citations or readiness digest.
    unique={}
    for value,proof in facts:
        key=proof['platform_order_id']
        if key in unique and unique[key][1]['original_day']!=proof['original_day']:
            raise MemberPending('saved_date_sources_conflict')
        unique.setdefault(key,(value,proof))
    folder=ws.root/'automation-evidence'/'refund-order-dates';folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    evidence=[]
    for key,(value,proof) in sorted(unique.items()):
        body=input_csv(store.name,[proof]);sha=hashlib.sha256(body).hexdigest();path=folder/(sha+'.csv')
        if path.is_symlink():raise MemberPending('date_input_path_changed')
        if path.exists():
            if path.read_bytes()!=body:raise MemberPending('date_input_bytes_changed')
        else:
            with path.open('xb') as out:out.write(body);out.flush();__import__('os').fsync(out.fileno())
            path.chmod(0o600)
        # Register a retrievable derived blob, without changing upload slots,
        # source versions or any historical run. Per-run proof protects it.
        blob=ws.path_of(sha);blob.parent.mkdir(parents=True,exist_ok=True)
        if blob.is_symlink():raise MemberPending('date_input_blob_path_changed')
        if not blob.exists():
            with blob.open('xb') as out:out.write(body);out.flush();__import__('os').fsync(out.fileno())
            blob.chmod(0o600)
        if hashlib.sha256(blob.read_bytes()).hexdigest()!=sha:raise MemberPending('date_input_blob_changed')
        with ws.transaction() as conn:
            conn.execute('INSERT OR IGNORE INTO file(sha,name,size,first_seen) VALUES(?,?,?,?)',
                (sha,'退款原订单日期-'+store.id+'-'+key+'.csv',len(body),journal.now()))
        attach_input(ingestion,path,model,store)
        evidence.append({'source_fact':value,'input_ref':{'path':str(blob),'sha256':sha}})
    ingestion.original_date_source_evidence=evidence
    return {'attached':len(unique),'financial_applied':False}
