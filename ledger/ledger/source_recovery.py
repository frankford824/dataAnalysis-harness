"""Sealed source demand -> existing case attempts -> independent raw proof.

This is a read capability. A source receipt never authorizes a monetary entry,
finishes a business case, changes a source date/quantity, or reopens a period.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import time
from types import SimpleNamespace
from copy import copy

import polars as pl

from . import automation_store as journal
from .automation_actions import Conflict
from .storage_integrity import digest as file_sha

CAPABILITY = 'jst_source_recovery.v1'
MAX_KEYS = 2000
RETRY_DELAYS=(60,300,900,3600)
RETRY_DAILY_LIMIT=16
RETRY_HEARTBEAT_LIMIT=1
RETRY_REASONS={'source_transport_unavailable','source_http_unavailable','source_rate_limited',
    'source_runtime_unavailable','source_node_runtime_unavailable','source_jst_module_unavailable'}
PARSER_SHA = '0c636672b044492c249572b7f3940eaa4cdf9c3f0d05b8761ac7ddd3929477b8'
_SHA = re.compile(r'^[0-9a-f]{64}$')
_ID = re.compile(r'^[1-9][0-9]{0,23}$')


def capture_originals(ingestion):
    """Read-only parsed evidence before financial feed replacement.

    No item is reinserted into ingestion.items. A cheap Polars clone isolates
    later header/flag transformations while preserving only locator access.
    """
    ingestion.source_recovery_originals=tuple(SimpleNamespace(ref=copy(item.ref),
        template=item.template,frame=item.frame.clone(),derivative=getattr(item,'derivative',None)) for item in ingestion.known
        if item.template is not None and item.frame is not None)
    return len(ingestion.source_recovery_originals)


class SourcePending(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _json(raw):
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise SourcePending('source_json_duplicate_keys')
            value[key]=item
        return value
    def constant(_):raise SourcePending('source_json_nonfinite')
    return json.loads(raw,object_pairs_hook=unique,parse_constant=constant)


def execution_scope(store_id, period):
    periods=sorted(set(v.strip() for v in os.environ.get('LEDGER_SOURCE_RECOVERY_PERIODS','').split(',') if v.strip()))
    if not periods or any(not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',v) for v in periods) or period not in periods:
        raise SourcePending('source_recovery_scope_not_enabled')
    return {'store_id':store_id,'case_period':period,'enabled_periods':periods,
            'policy_sha256':journal.digest(periods),'authority':'read_only_source_capture_not_financial_approval'}


def publication_observation(ws, model, store_id, period):
    try:
        from .publication_scope import load_automatic,PublicationScopeError
    except ImportError:
        return {'publication_scope_identity':None,'publication_allowed':False}
    try:
        scope=load_automatic(ws.root,model=model)
        return {'publication_scope_identity':dict(scope.identity),'publication_allowed':scope.allows(store_id,period)}
    except PublicationScopeError:
        return {'publication_scope_identity':None,'publication_allowed':False}


def durable(path, value):
    raw = journal.encoded(value).encode()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name('.' + path.name + '-' + journal.ident() + '.tmp')
    with temporary.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)
    if os.name != 'nt':
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return hashlib.sha256(raw).hexdigest()


def anchor_index(ingestion, store, period, pricing, coverage, facts=None):
    """Index only currently demanded, parsed originals; never infer raw authority.

    It is deliberately written into a NEW immutable run manifest. An old run
    without this index stays pending until independently recomputed.
    """
    wanted = set()
    for frame in (pricing, coverage):
        if frame is None or 'order_id' not in frame.columns:
            continue
        if 'period' in frame.columns:
            frame = frame.filter(pl.col('period') == period)
        wanted.update(str(v).strip() for v in frame['order_id'].drop_nulls().to_list()
                      if _ID.fullmatch(str(v).strip()))
    if len(wanted) > MAX_KEYS:
        return {'schema': CAPABILITY, 'status': 'pending', 'reason': 'source_anchor_key_budget', 'orders': {}}
    date_demands={}
    required={'metric_id','source_note','link_key','file_sha','sheet','row_no','store'}
    if facts is not None and required<=set(facts.columns):
        selected=facts.filter(pl.col('metric_id').is_in(['trade_refund','trade_refund_douyin'])
            &pl.col('source_note').cast(pl.String).str.contains('下单月份：待核对',literal=True).fill_null(False))
        for row in selected.to_dicts():
            target=str(row.get('link_key') or '');sha=str(row.get('file_sha') or '')
            if (row.get('store') not in (store.id,store.name,*store.aliases) or not _ID.fullmatch(target)
                or not _SHA.fullmatch(sha) or not row.get('sheet') or not row.get('row_no')):continue
            value={'platform_order_id':target,'anchor':{'sha256':sha,'sheet':row['sheet'],'row':int(row['row_no'])},
                   'metric_id':row['metric_id'],'template_id':row.get('template_id')}
            key='date:'+journal.digest(value)
            date_demands[key]={**value,'context_sha':journal.digest([store.id,period,value])}
    if not wanted:
        return {'schema': CAPABILITY, 'status': 'indexed', 'orders': {},'date_demands':date_demands}
    if not hasattr(ingestion,'model') or not hasattr(ingestion,'known'):
        return {'schema': CAPABILITY, 'status': 'pending', 'reason': 'parsed_original_index_unavailable', 'orders': {}}
    native_sources = {s.id for s in ingestion.model.sources if s.is_spine and s.platform in ('*', 'any', store.platform)}
    cost_sources = {m.source for m in ingestion.model.metrics if m.id == 'goods_cost'}
    result = {key: {'native': [], 'cost': [], 'separate_cost_anchors': [], 'internal_order_ids': []} for key in wanted}
    for item in getattr(ingestion,'source_recovery_originals',ingestion.known):
        template, frame = item.template, item.frame
        if template is None or frame is None or template.source not in native_sources | cost_sources:
            continue
        sha = str(item.ref.sha256)
        # A generated order-feed identity is not an original workbook SHA.
        if not _SHA.fullmatch(sha):
            continue
        role = 'native' if template.source in native_sources else 'cost'
        parent = 'original_order_id' if role == 'cost' and 'original_order_id' in frame.columns else 'order_id'
        if not {parent, '__sheet__', '__row__'} <= set(frame.columns):
            continue
        rows = frame.filter(pl.col(parent).cast(pl.Utf8).is_in(sorted(wanted)))
        columns = [parent, '__sheet__', '__row__'] + [k for k in ('internal_order_id', 'store_name', 'order_type') if k in frame.columns]
        for row in rows.select(columns).to_dicts():
            if row.get('store_name') not in (None, '', store.id, store.name, *store.aliases):
                continue
            key = str(row[parent])
            anchor = {'sha256': sha, 'sheet': str(row['__sheet__']), 'row': int(row['__row__'])}
            if anchor['row'] <= 2:
                continue
            # A replacement shipment has its own cost metric. Sharing a
            # native parent ID does not make it an ordinary sale's original
            # ERP source root. Retain its original anchor in the sealed index
            # without mixing it into the ordinary member-proof request.
            # Missing/unknown types remain candidates and fail closed later.
            if role == 'cost' and row.get('order_type') == '补发订单':
                separate = {**anchor, 'reason': 'reshipment_cost_separate'}
                if separate not in result[key]['separate_cost_anchors']:
                    result[key]['separate_cost_anchors'].append(separate)
                continue
            if anchor not in result[key][role]:
                result[key][role].append(anchor)
            oid = str(row.get('internal_order_id') or '')
            if role == 'cost' and _ID.fullmatch(oid) and oid not in result[key]['internal_order_ids']:
                result[key]['internal_order_ids'].append(oid)
    return {'schema': CAPABILITY, 'status': 'indexed', 'orders': result,'date_demands':date_demands}


def _current(ws, model, case_id, run_id, input_sha):
    from .automation_checker import check
    row = ws.conn.execute('SELECT * FROM automation_case WHERE id=?', (case_id,)).fetchone()
    if not row or row['input_sha'] != input_sha or row['run_id'] != run_id:
        raise Conflict('取证案件或输入版本已变化')
    if row['stage'] in {'closed','excluded'}:
        raise Conflict('当前店月不在自动处理范围')
    state = ws.state(row['store_id'], row['period'])
    if state and state.closed:
        raise Conflict('已结账账期不能创建或接收新自动取证')
    latest = ws.latest_run(row['store_id'], row['period'])
    if not latest or latest['id'] != run_id:
        raise Conflict('当前核算已变化')
    checked = check(ws, row['store_id'], row['period'], model=model, verify_artifacts=True)
    if checked['input_sha'] != input_sha:
        raise Conflict('取证输入已过期')
    return dict(row)


def _plan(ws, model, case, body, order_client):
    from .automation_period_proof import valid_artifact
    from . import cost_lines
    allowed_scope=execution_scope(case['store_id'],case['period'])
    proof = ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?', (case['run_id'],)).fetchone()
    if not proof:
        raise SourcePending('sealed_run_proof_missing')
    manifest = json.loads(proof['manifest_json'])
    index = manifest.get('source_recovery_index')
    if not isinstance(index, dict) or index.get('schema') != CAPABILITY or index.get('status') != 'indexed':
        raise SourcePending('sealed_original_anchor_index_missing')
    date_demand=index.get('date_demands',{}).get(body['coverage_key'])
    coverage_path = ws.facts_path(case['run_id']) if date_demand else ws.coverage_gaps_path(case['run_id'])
    artifacts = {a['path']: a for a in manifest.get('artifacts', [])}
    rel = coverage_path.relative_to(ws.root).as_posix()
    if rel not in artifacts or not valid_artifact(ws, artifacts[rel]):
        raise SourcePending('sealed_gap_artifact_invalid')
    if date_demand:
        if date_demand.get('context_sha')!=body['context_sha']:raise SourcePending('sealed_date_context_changed')
        anchor=date_demand['anchor'];frame=pl.read_parquet(coverage_path)
        required={'file_sha','sheet','row_no','metric_id','link_key','source_note'}
        if not required<=set(frame.columns):raise SourcePending('sealed_date_identity_contract_missing')
        matches=frame.filter((pl.col('file_sha')==anchor['sha256'])&(pl.col('sheet')==anchor['sheet'])
            &(pl.col('row_no')==anchor['row'])&(pl.col('metric_id')==date_demand['metric_id'])
            &(pl.col('link_key')==date_demand['platform_order_id'])
            &pl.col('source_note').str.contains('下单月份：待核对',literal=True).fill_null(False))
        if not matches.height:raise SourcePending('sealed_date_source_row_changed')
        if anchor['sha256'] not in manifest.get('source_refs',[]):raise SourcePending('date_original_not_in_sealed_run')
        source=ws.path_of(anchor['sha256'])
        if not source.is_file() or file_sha(source)!=anchor['sha256']:raise SourcePending('date_original_source_bytes_changed')
        gap={'order_id':date_demand['platform_order_id']}
    else:
        gaps = cost_lines._source_rows(ws, case['run_id'], for_edit=True)
        rows = [r for r in gaps if r.get('coverage_key') == body['coverage_key'] and r.get('context_sha') == body['context_sha']]
        if len(rows) != 1 or rows[0].get('order_count') != 1:
            raise SourcePending('sealed_single_original_identity_missing')
        gap = rows[0]
    platform_id = str(gap.get('order_id') or '')
    if not _ID.fullmatch(platform_id):
        raise SourcePending('sealed_platform_identity_missing')
    original = {'native':[],'cost':[],'internal_order_ids':[]} if date_demand else index.get('orders', {}).get(platform_id)
    if not isinstance(original, dict):
        raise SourcePending('sealed_original_demand_index_missing')
    source_refs = set(manifest.get('source_refs', []))
    native_days = set()
    store = model.store(case['store_id'])
    from .jst_source_members import _Workbook, MemberPending
    books = {}
    for kind in ('native', 'cost'):
        if len(original.get(kind, [])) > 100:
            raise SourcePending('original_anchor_budget')
        for anchor in original.get(kind, []):
            sha = anchor.get('sha256')
            if not isinstance(sha, str) or not _SHA.fullmatch(sha) or sha not in source_refs:
                raise SourcePending('original_source_not_in_sealed_run')
            path = ws.path_of(sha)
            if not path.is_file() or file_sha(path) != sha or path.stat().st_size > 128 * 1024 * 1024:
                raise SourcePending('original_source_bytes_unavailable')
            if kind != 'native':
                continue
            try:
                if sha not in books:
                    books[sha] = _Workbook(path.read_bytes())
                book = books[sha]
                header = book.headers(anchor['sheet'])
                parent = '订单号' if store.platform == 'alibaba1688' else '主订单编号'
                stamp = {'alibaba1688': '订单创建时间', 'taobao': '订单创建时间', 'douyin': '订单提交时间'}.get(store.platform)
                if not stamp or parent not in header or stamp not in header:
                    raise SourcePending('native_original_schema_pending')
                if book.value(anchor['sheet'], anchor['row'], header[parent]) != platform_id:
                    raise SourcePending('native_original_identity_conflict')
                native_days.add(book.day(anchor['sheet'], anchor['row'], header[stamp]))
            except (MemberPending, KeyError, TypeError) as exc:
                raise SourcePending('native_original_anchor_unverified') from exc
    if original.get('native') and len(native_days) != 1:
        raise SourcePending('native_original_day_conflict')
    try:
        mapping = order_client.get('stores')
    except Exception as exc:
        raise SourcePending('confirmed_store_source_unavailable') from exc
    if not isinstance(mapping, dict) or not isinstance(mapping.get('stores'), list):
        raise SourcePending('confirmed_store_source_unavailable')
    shops = sorted({str(r.get('order_store_id')) for r in mapping['stores'] if isinstance(r, dict)
                    and r.get('ledger_store_id') == store.id and r.get('mapping_status') == 'confirmed'
                    and r.get('consumable') is True and _ID.fullmatch(str(r.get('order_store_id') or ''))})
    if not shops:
        raise SourcePending('confirmed_store_mapping_missing')
    internals = sorted(set(original.get('internal_order_ids', [])))
    if len(internals) > 1:
        raise SourcePending('original_internal_identity_ambiguous')
    return {'schema': CAPABILITY, 'case_id': case['id'], 'store_id': store.id, 'period': case['period'],
            'purpose':'source_members' if original.get('native') else 'original_day',
            **publication_observation(ws,model,store.id,case['period']),
            'run_id': case['run_id'], 'input_sha': case['input_sha'],
            'coverage_key': body['coverage_key'], 'coverage_context_sha': body['context_sha'],
            'platform': store.platform, 'platform_order_id': platform_id,
            'source_order_id': internals[0] if internals and original.get('native') else None,
            'execution_scope':allowed_scope,
            'shop_ids': shops, 'store_names': [store.name, *store.aliases],
            'original_day': next(iter(native_days)) if native_days else None, 'native_anchors': original['native'],
            'demand_anchor':{'kind':'sealed_run_demand_not_original_workbook','run_id':case['run_id'],
                             'artifact_sha256':artifacts[rel]['sha'],'coverage_key':body['coverage_key'],
                             'context_sha':body['context_sha']},
            'original_identifier_anchor':date_demand['anchor'] if date_demand else None,
            'cost_anchors': original.get('cost', []), 'parser_sha256': PARSER_SHA,
            'limits': {'nodes': 12, 'relations': 6, 'depth': 6, 'body_bytes': 32 * 1024 * 1024,
                       'request_interval_ms': 1000, 'seconds': 120},
            'financial_application_allowed': False}


def submit(ws, model, body, *, order_client=None):
    from .automation_handoff import context_sha
    from .order_feed import Client
    case = _current(ws, model, body['case_id'], body['run_id'], body['input_sha'])
    source_context = context_sha(ws, model, case['store_id'], case['period'], case['input_sha'])
    task_id = 'source-' + journal.digest([case['id'], case['input_sha'], source_context,
                                       body['coverage_key'], body['context_sha']])[:40]
    old = ws.conn.execute('SELECT * FROM automation_attempt WHERE id=?', (task_id,)).fetchone()
    if old:
        return status(ws, task_id)
    try:
        plan = _plan(ws, model, case, body, order_client or Client())
        state, reason = 'source_queued', None
    except SourcePending as exc:
        plan, state, reason = None, 'source_pending', exc.reason
    payload = {'kind': CAPABILITY, 'source_context_sha': source_context, 'plan': plan,
               'plan_sha256': journal.digest(plan) if plan else None, 'reason': reason,
               'financial_applied': False}
    # Rebinding never blesses an old financial proposal. The new current CAS
    # above still applies; only immutable source bytes may be reused after an
    # independent backend proof against the exact same sealed target.
    if plan:
        cached={}
        for row in ws.conn.execute("SELECT * FROM automation_attempt WHERE case_id=? AND state='source_raw_ready'",(case['id'],)):
            previous=_json(row['result_json']);old_plan=previous.get('plan') or {}
            if previous.get('kind')!=CAPABILITY or source_semantic(old_plan)!=source_semantic(plan):continue
            try:
                binding=_source_binding(row,previous);verified=_verified_binding(ws,binding)
            except (SourcePending,ValueError,OSError,KeyError,TypeError):continue
            # Distinct captures with different normalized facts cannot be
            # resolved by first/last capture or timestamps.
            identity=journal.digest(proof_semantic(verified['source_proof']))
            cached.setdefault(identity,[]).append(binding)
        if len(cached)==1:
            payload['source_binding']=min(next(iter(cached.values())),key=journal.encoded)
            payload['cache_revalidated']=True
            state='source_raw_ready'
    with ws.transaction() as conn:
        _current(ws, model, body['case_id'], body['run_id'], body['input_sha'])
        if context_sha(ws, model, case['store_id'], case['period'], case['input_sha']) != source_context:
            raise Conflict('取证来源上下文已变化')
        if plan and any(plan[k]!=v for k,v in publication_observation(ws,model,case['store_id'],case['period']).items()):
            raise Conflict('自动发布范围在取证计划封存前已变化')
        created=conn.execute('INSERT OR IGNORE INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
                     (task_id, case['id'], task_id, case['input_sha'], state, journal.encoded(payload), journal.now())).rowcount
        if created and plan:_activate_generation(conn,case,task_id,plan)

    return status(ws, task_id)


def ensure_date_demands(ws,model,*,maximum=16):
    """Queue sealed date-only demands before a qualified Worker enters a model.

    No standalone financial scheduler: these remain ordinary case attempts and
    use the same claim/heartbeat. Expensive source reads happen in the child.
    """
    created=0
    cases=ws.conn.execute("SELECT * FROM automation_case WHERE status='processing' AND stage IN ('gathering','queued','retry_wait') ORDER BY updated_at,id LIMIT 32").fetchall()
    for case in cases:
        try:execution_scope(case['store_id'],case['period'])
        except SourcePending:continue
        proof=ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(case['run_id'],)).fetchone()
        if not proof:continue
        index=_json(proof['manifest_json']).get('source_recovery_index',{})
        from .automation_handoff import context_sha
        context=context_sha(ws,model,case['store_id'],case['period'],case['input_sha'])
        for key,value in index.get('date_demands',{}).items():
            if created>=maximum:return created
            task_id='source-'+journal.digest([case['id'],case['input_sha'],context,key,value['context_sha']])[:40]
            if ws.conn.execute('SELECT 1 FROM automation_attempt WHERE id=?',(task_id,)).fetchone():continue
            body={'case_id':case['id'],'run_id':case['run_id'],'input_sha':case['input_sha'],
                  'coverage_key':key,'context_sha':value['context_sha']}
            try:
                old_count=ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE case_id=? AND state='source_queued'",(case['id'],)).fetchone()[0]
                submit(ws,model,body)
                new_count=ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE case_id=? AND state='source_queued'",(case['id'],)).fetchone()[0]
                created+=max(0,new_count-old_count)
            except (Conflict,SourcePending):continue
        # One store's bounded demands per claim; other stores retain priority
        # ordering and are considered after this batch is consumed.
        if created:return created
    return created


def status(ws, task_id):
    row = ws.conn.execute('SELECT * FROM automation_attempt WHERE id=?', (task_id,)).fetchone()
    if not row:
        return None
    payload = json.loads(row['result_json'])
    if payload.get('kind') != CAPABILITY:
        return None
    return {'task_id': row['id'], 'state': row['state'], 'reason': payload.get('reason'),
            'plan_sha256': payload.get('plan_sha256'), 'proof_sha256': payload.get('proof_sha256'),
            'retry':payload.get('automatic_retry'),
            'financial_applied': False, 'business_complete': False}


def queued(ws, case_id, input_sha):
    rows = ws.conn.execute("SELECT * FROM automation_attempt WHERE case_id=? AND input_sha=? AND state='source_queued' ORDER BY rowid", (case_id, input_sha)).fetchall()
    return next((dict(r) for r in rows if json.loads(r['result_json']).get('kind') == CAPABILITY), None)


def ready(ws,case_id,input_sha):
    rows=ws.conn.execute("SELECT * FROM automation_attempt WHERE case_id=? AND input_sha=? AND state='source_raw_ready' ORDER BY rowid",
                         (case_id,input_sha)).fetchall()
    return next((dict(r) for r in rows if _json(r['result_json']).get('kind')==CAPABILITY),None)


def _retry_snapshot(payload):
    return {k:payload.get(k) for k in ('reason','source_attempt_id','proof_sha256','receipt_sha256',
        'worker_configuration_sha','automatic_retry')}


def _retry_current(ws,model,row,payload,stamp):
    if model is None:raise SourcePending('source_retry_model_unavailable')
    plan=payload['plan'];case=_current(ws,model,row['case_id'],plan['run_id'],row['input_sha'])
    if plan.get('execution_scope')!=execution_scope(case['store_id'],case['period']):
        raise SourcePending('source_execution_scope_changed')
    if any(plan.get(k)!=v for k,v in publication_observation(ws,model,case['store_id'],case['period']).items()):
        raise SourcePending('publication_scope_context_changed')
    from .automation_handoff import context_sha
    if payload.get('source_context_sha')!=context_sha(ws,model,case['store_id'],case['period'],case['input_sha']):
        raise SourcePending('source_retry_target_context_changed')
    head=generation_head(ws,case['id'],plan['platform_order_id'])
    if head and head['active_task_id']!=row['id']:raise SourcePending('source_capture_generation_superseded')
    if case.get('lease_owner') and case.get('lease_until',0)>stamp:
        raise SourcePending('source_retry_case_busy')
    return case


def _queue_retry(ws,row,payload,case,*,reason,stamp,configuration_sha=None):
    previous=_retry_snapshot(payload)
    payload.setdefault('prior_source_receipts',[]).append(previous)
    if configuration_sha is not None:
        payload['worker_configuration_sha']=configuration_sha
        retry={'scheduled_count':0,'state':'scheduled','next_attempt_at':None,
               'reason':'worker_configuration_changed','policy_epoch':configuration_sha}
    else:
        retry={**payload['automatic_retry'],'scheduled_count':payload['automatic_retry']['scheduled_count']+1,
               'state':'scheduled','next_attempt_at':None,'reason':reason}
    retry.update(maximum_automatic_retries=len(RETRY_DELAYS),daily_limit=RETRY_DAILY_LIMIT)
    payload.update(reason=None,automatic_retry=retry)
    ws.conn.execute('UPDATE automation_attempt SET state=?,result_json=? WHERE id=?',
        ('source_queued',journal.encoded(payload),row['id']))
    # The old source lease cannot complete this new execution. No monetary
    # context/CAS is made current by changing this technical queue state.
    ws.conn.execute("UPDATE automation_case SET status='processing',stage='gathering',lease_owner='',lease_until=0,lease_token=lease_token+1,worker_attempt_id='' WHERE id=? AND input_sha=?",
        (case['id'],case['input_sha']))
    value={'source_task_id':row['id'],'previous':previous,'worker_id':payload['captured_worker_id'],
        'day':int(stamp)//86400,'scheduled_at':int(stamp),'reason':reason,
        'configuration_sha':configuration_sha,'retry_number':retry['scheduled_count']}
    ws.conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
        (journal.ident(),row['case_id'],row['id'],row['input_sha'],
         'source_configuration_recovery' if configuration_sha is not None else 'source_retry_scheduled',
         journal.encoded(value),journal.now()))


def configuration_changed(ws, worker_id, configuration_sha,*,model=None,stamp=None):
    """Genuine local session/config epochs wake once, independently of time."""
    stamp=int(time.time() if stamp is None else stamp)
    rows=ws.conn.execute("SELECT * FROM automation_attempt WHERE state='source_pending'").fetchall()
    for row in rows:
        payload=_json(row['result_json'])
        if (payload.get('kind')!=CAPABILITY or not payload.get('plan')
            or payload.get('captured_worker_id')!=worker_id
            or payload.get('worker_configuration_sha') in (None,'',configuration_sha)):continue
        try:case=_retry_current(ws,model,row,payload,stamp)
        except (Conflict,SourcePending,ValueError,KeyError):continue
        _queue_retry(ws,row,payload,case,reason='worker_configuration_changed',stamp=stamp,configuration_sha=configuration_sha)


def retry_due(ws,model,worker_id,stamp):
    """At most one bounded technical retry per authenticated heartbeat."""
    stamp=int(stamp);scheduled=0;day=stamp//86400
    daily=ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='source_retry_scheduled' AND json_extract(result_json,'$.worker_id')=? AND json_extract(result_json,'$.day')=?",(worker_id,day)).fetchone()[0]
    rows=ws.conn.execute("SELECT * FROM automation_attempt WHERE state='source_pending' ORDER BY json_extract(result_json,'$.automatic_retry.next_attempt_at'),rowid").fetchall()
    for row in rows:
        if scheduled>=RETRY_HEARTBEAT_LIMIT:break
        payload=_json(row['result_json']);retry=payload.get('automatic_retry') or {}
        if (payload.get('kind')!=CAPABILITY or payload.get('captured_worker_id')!=worker_id
            or not retry.get('eligible') or retry.get('state') not in {'waiting','daily_limit'}
            or retry.get('next_attempt_at') is None or int(retry['next_attempt_at'])>stamp):continue
        try:case=_retry_current(ws,model,row,payload,stamp)
        except (Conflict,SourcePending,ValueError,KeyError) as error:
            if getattr(error,'reason',None)=='source_retry_case_busy':continue
            retry.update(state='held_current_context',next_attempt_at=None,
                         hold_reason=getattr(error,'reason',None) or 'source_retry_current_input_changed')
            payload['automatic_retry']=retry
            ws.conn.execute('UPDATE automation_attempt SET result_json=? WHERE id=?',(journal.encoded(payload),row['id']))
            continue
        if retry.get('scheduled_count',0)>=len(RETRY_DELAYS):
            retry.update(state='exhausted',next_attempt_at=None);payload['automatic_retry']=retry
            ws.conn.execute('UPDATE automation_attempt SET result_json=? WHERE id=?',(journal.encoded(payload),row['id']))
            continue
        if daily>=RETRY_DAILY_LIMIT:
            retry.update(state='daily_limit',next_attempt_at=(day+1)*86400,
                         hold_reason='source_retry_daily_limit');payload['automatic_retry']=retry
            ws.conn.execute('UPDATE automation_attempt SET result_json=? WHERE id=?',(journal.encoded(payload),row['id']))
            continue
        _queue_retry(ws,row,payload,case,reason=payload['reason'],stamp=stamp)
        daily+=1;scheduled+=1
    return scheduled


def _record_retry_policy(payload,result,folder,stamp):
    reason=result.get('reason');eligible=reason in RETRY_REASONS;failure_kind='transport' if eligible else 'semantic_or_manual'
    receipt=_json((folder/'receipt.json').read_bytes());responses=receipt.get('responses') or []
    if reason=='source_session_refresh_required':eligible=False;failure_kind='login_required'
    if responses:
        last=responses[-1];status=last.get('status');sha=last.get('sha256');raw=folder/(str(sha)+'.raw')
        proved=bool(isinstance(sha,str) and _SHA.fullmatch(sha) and not raw.is_symlink()
            and raw.is_file() and raw.stat().st_size<=16*1024*1024 and file_sha(raw)==sha)
        if not proved:eligible=False;failure_kind='response_unverified'
        else:
            # The fixed reader's transport catch can collapse a login page or
            # HTTP failure to source_transport_unavailable. Classify from its
            # actual retained response, rather than retrying login forever.
            head=raw.read_bytes()[:10000].decode('utf-8',errors='replace')
            login=status in {401,403} or bool(re.search(r'loginReact|erp-login|无访问权限|未登录|请登录',head))
            if login:eligible=False;failure_kind='login_required'
            elif status!=200:
                eligible=(reason in RETRY_REASONS|{'source_session_or_http_unavailable'}
                    and status in {405,408,425,429,500,502,503,504})
                failure_kind='transport' if eligible else 'http_manual_review'
    previous=payload.get('automatic_retry') or {};count=int(previous.get('scheduled_count',0))
    state='recovered' if result['state']=='source_raw_ready' else 'waiting' if eligible and count<len(RETRY_DELAYS) else 'exhausted' if eligible else 'not_retryable'
    payload['automatic_retry']={**previous,'eligible':eligible,'state':state,'scheduled_count':count,
        'maximum_automatic_retries':len(RETRY_DELAYS),'daily_limit':RETRY_DAILY_LIMIT,
        'next_attempt_at':int(stamp)+RETRY_DELAYS[count] if state=='waiting' else None,
        'last_reason':reason,'failure_kind':failure_kind,'last_finished_at':int(stamp)}


def retry_queued(ws,case_id,input_sha):
    return bool(ws.conn.execute("SELECT 1 FROM automation_attempt WHERE case_id=? AND input_sha=? AND state='source_queued' AND json_extract(result_json,'$.automatic_retry.state')='scheduled' LIMIT 1",(case_id,input_sha)).fetchone())


def verify_receipt(ws, plan, task_id, job_id, expected_sha):
    """Read only the server-determined local bundle, never a submitted path/JSON."""
    if plan.get('execution_scope')!=execution_scope(plan['store_id'],plan['period']):
        raise SourcePending('source_execution_scope_changed')
    if not re.fullmatch(r'source-[0-9a-f]{40}', task_id) or not re.fullmatch(r'[0-9a-f]{32}', job_id):
        raise SourcePending('source_receipt_identity_invalid')
    folder = ws.root / 'source-recovery' / task_id / job_id
    canonical = folder.resolve()
    if not canonical.is_relative_to(ws.root.resolve()) or any(p.is_symlink() for p in (folder, folder.parent, folder.parent.parent)):
        raise SourcePending('source_receipt_storage_invalid')
    path = folder / 'receipt.json'
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024 or file_sha(path) != expected_sha:
        raise SourcePending('source_receipt_bytes_unverified')
    receipt = _json(path.read_bytes())
    collector = Path(__file__).resolve().parents[2] / 'agent-worker/ledger_agent/jst_source_cli.mjs'
    expected_collector = os.environ.get('LEDGER_SOURCE_RECOVERY_COLLECTOR_SHA') or (file_sha(collector) if collector.is_file() else '')
    if not expected_collector or receipt.get('collector_sha256') != expected_collector:
        raise SourcePending('source_collector_identity_unverified')
    if (receipt.get('schema') != CAPABILITY or receipt.get('task_id') != task_id
            or receipt.get('job_id') != job_id or receipt.get('plan_sha256') != journal.digest(plan)
            or receipt.get('input_sha') != plan['input_sha'] or receipt.get('parser_sha256') != PARSER_SHA):
        raise SourcePending('source_receipt_scope_changed')
    refs = receipt.get('nodes', []) + receipt.get('relations', [])
    if len(receipt.get('nodes', [])) > 12 or len(receipt.get('relations', [])) > 6:
        raise SourcePending('source_graph_budget')
    blobs = {}
    for ref in refs:
        sha = ref.get('sha256')
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise SourcePending('source_raw_identity_invalid')
        raw = folder / (sha + '.raw')
        if raw.is_symlink() or not raw.is_file() or raw.stat().st_size > 16 * 1024 * 1024 or file_sha(raw) != sha:
            raise SourcePending('source_raw_bytes_unverified')
        blobs[sha] = raw.read_bytes()
    from .jst_source_members import parse_order
    originals = []
    for ref in receipt.get('nodes', []):
        data, _ = parse_order(blobs[ref['sha256']], str(ref['order_id']))
        if str(data.get('shop_id')) not in plan['shop_ids']:
            raise SourcePending('source_raw_shop_conflict')
        tokens={v.strip() for v in str(data.get('so_id') or '').split(',')}
        target_tokens={plan['platform_order_id'],str(data.get('shop_id'))+':'+plan['platform_order_id']}
        if ((plan['purpose']=='original_day' or str(data.get('order_date') or '')[:10] == plan['original_day'])
                and tokens & target_tokens):
            originals.append(str(ref['order_id']))
    if receipt.get('state') == 'raw_ready' and (not originals or (plan['source_order_id'] and plan['source_order_id'] not in originals)):
        raise SourcePending('source_original_raw_not_observed')
    from .jst_source_members import Anchor, Request, Capture, Node, normalize_members
    for anchor in plan['native_anchors'] + plan['cost_anchors']:
        path = ws.path_of(anchor['sha256'])
        if not path.is_file() or file_sha(path) != anchor['sha256']:
            raise SourcePending('source_original_bytes_changed')
        blobs[anchor['sha256']] = path.read_bytes()
    capture = Capture(tuple(Node(str(r['order_id']), r['sha256']) for r in receipt.get('nodes', [])),
                      tuple(Node(str(r['order_id']), r['sha256']) for r in receipt.get('relations', [])))
    if not plan['source_order_id'] or not plan['cost_anchors']:
        # Native-only capture is useful evidence. Its quantity/SKU admission
        # needs its own contract; it is not silently promoted into history v2.
        proof = {'schema': 'jst_source_members.v1', 'status': 'pending', 'members': [],
                 'reasons': ['native_only_quantity_contract_pending']}
    else:
        request = Request(plan['source_order_id'], plan['platform_order_id'], plan['platform'],
                          tuple(plan['store_names']), tuple(plan['shop_ids']),
                          tuple(Anchor(**a) for a in plan['cost_anchors']), tuple(Anchor(**a) for a in plan['native_anchors']))
        proof = normalize_members(request, capture, blobs)
    result = {'schema': CAPABILITY, 'state': 'source_raw_ready' if receipt.get('state') == 'raw_ready' else 'source_pending',
              'reason': receipt.get('reason'), 'source_proof': proof, 'receipt_sha256': expected_sha,
              'raw_refs': refs, 'financial_applied': False, 'business_complete': False}
    return result



def proof_semantic(value,field=''):
    """Financial proof content, separately from immutable raw lineage.

    Every raw blob remains SHA checked and retained. A capture's HTML nonce,
    graph-blob SHA or reordered items pointer does not change a proved member.
    Identity, exact units and all observed lifecycle/risk flags do.
    """
    if isinstance(value,dict):
        return {k:proof_semantic(v,k) for k,v in value.items()
            if k not in {'graph_refs','source_raw','physical_raw','physical_item_pointer'}}
    if isinstance(value,list):return sorted((proof_semantic(v,field) for v in value),key=journal.encoded)
    if field in {'quantity','logical_quantity'} and isinstance(value,str):
        from decimal import Decimal,DecimalException
        try:
            d=Decimal(value)
            if d.is_finite():
                sign,digits,exponent=d.as_tuple();digits=list(digits)
                while len(digits)>1 and digits[-1]==0:digits.pop();exponent+=1
                return ('-' if sign else '')+''.join(map(str,digits))+'e'+str(exponent)
        except DecimalException:pass
    return value

def source_semantic(plan):
    """Source identity excludes financial CAS and receipt delivery nonces."""
    fields=('schema','store_id','platform','platform_order_id','source_order_id',
            'purpose','shop_ids','store_names','original_day','native_anchors',
            'cost_anchors','original_identifier_anchor','parser_sha256')
    return journal.digest({key:plan.get(key) for key in fields})


def _source_binding(task,payload):
    return payload.get('source_binding') or {
        'task_id':task['id'],'plan':payload['plan'],
        'source_attempt_id':payload['source_attempt_id'],
        'receipt_sha256':payload['receipt_sha256'],
        'source_context_sha':payload['source_context_sha'],
        'source_semantic_sha':source_semantic(payload['plan'])}


def _verified_binding(ws,binding):
    """A binding is a server journal reference, never worker-supplied truth."""
    row=ws.conn.execute('SELECT * FROM automation_attempt WHERE id=?',(binding['task_id'],)).fetchone()
    if not row or row['state']!='source_raw_ready':raise SourcePending('source_binding_not_ready')
    payload=_json(row['result_json']);canonical=_source_binding(row,payload)
    if canonical!=binding:raise SourcePending('source_binding_journal_changed')
    plan=binding['plan']
    if source_semantic(plan)!=binding['source_semantic_sha']:raise SourcePending('source_binding_semantic_changed')
    result=verify_receipt(ws,plan,binding['task_id'],binding['source_attempt_id'],binding['receipt_sha256'])
    if result['state']!='source_raw_ready':raise SourcePending('source_binding_raw_unavailable')
    folder=ws.root/'source-recovery'/binding['task_id']/binding['source_attempt_id']
    from .jst_source_members import parse_order
    refs=[{'path':str(folder/(r['sha256']+'.raw')),'sha256':r['sha256'],
           'source':'fixed_jst_source_reader'} for r in result['raw_refs']]
    refs.append({'path':str(folder/'receipt.json'),'sha256':binding['receipt_sha256'],'source':'fixed_jst_source_receipt'})
    receipt=_json((folder/'receipt.json').read_bytes());raw_orders={}
    for ref in receipt['nodes']:
        raw_orders[str(ref['order_id'])]=parse_order((folder/(ref['sha256']+'.raw')).read_bytes(),str(ref['order_id']))[0]
    for ref in plan['native_anchors']+plan['cost_anchors']:
        refs.append({'path':str(ws.path_of(ref['sha256'])),'sha256':ref['sha256'],'source':'sealed_original_workbook'})
    return {'task_id':binding['task_id'],'plan':plan,'source_proof':result['source_proof'],
        'source_attempt_id':binding['source_attempt_id'],'receipt_sha256':binding['receipt_sha256'],
        'source_context_sha':binding['source_context_sha'],'source_binding':binding,
        'raw_orders':raw_orders,'source_refs':refs,'financial_applied':False,'business_complete':False}


def verified_source(ws, model, request):
    """Current financial CAS first, then independently reopen server raw bytes."""
    case=_current(ws,model,request['case_id'],request['run_id'],request['input_sha'])
    from .automation_handoff import context_sha
    current_context=context_sha(ws,model,case['store_id'],case['period'],case['input_sha'])
    candidates=[]
    for row in ws.conn.execute("SELECT * FROM automation_attempt WHERE case_id=? AND input_sha=? AND state='source_raw_ready'",(case['id'],case['input_sha'])):
        payload=_json(row['result_json']);plan=payload.get('plan') or {}
        if (payload.get('kind')==CAPABILITY and payload.get('source_context_sha')==current_context
            and plan.get('coverage_key')==request['coverage_key'] and plan.get('coverage_context_sha')==request['context_sha']):
            candidates.append((dict(row),payload,plan))
    if len(candidates)!=1:raise SourcePending('current_source_receipt_not_unique_or_missing')
    row,payload,plan=candidates[0]
    if any(plan.get(k)!=v for k,v in publication_observation(ws,model,case['store_id'],case['period']).items()):
        raise SourcePending('publication_scope_context_changed')
    return _verified_binding(ws,_source_binding(row,payload))


def verified_fact_source(ws,model,fact,*,allow_pending_refresh=False):
    """An adopted fact survives its own run nonce, not real source changes.

    Immutable raw/originals are reopened. Active source membership and current
    store semantics are checked separately from the original financial CAS.
    No old lease, proposal or write request becomes current through this API.
    """
    binding=fact.get('source_binding')
    if not isinstance(binding,dict):raise SourcePending('adopted_source_binding_missing')
    plan=binding['plan'];store=model.store(plan['store_id'])
    if (store.platform!=plan['platform'] or [store.name,*store.aliases]!=plan['store_names']):
        raise SourcePending('adopted_source_store_contract_changed')
    current=publication_observation(ws,model,plan['store_id'],plan['period'])
    if not current['publication_allowed'] or any(plan.get(k)!=v for k,v in current.items()):
        raise SourcePending('publication_scope_context_changed')
    slots=ws.conn.execute('SELECT name,sha FROM slot WHERE store_id IN (?,?) ORDER BY CASE WHEN store_id=? THEN 0 ELSE 1 END,name',(store.id,'__shared__',store.id))
    selected={}
    for slot in slots:selected.setdefault(slot['name'],slot['sha'])
    active=set(selected.values())
    if any(a['sha256'] not in active for a in plan['native_anchors']+plan['cost_anchors']):
        raise SourcePending('adopted_source_original_membership_changed')
    original=_verified_binding(ws,binding)
    head=generation_head(ws,plan['case_id'],plan['platform_order_id'])
    if head:
        row=ws.conn.execute('SELECT * FROM automation_attempt WHERE id=?',(head['active_task_id'],)).fetchone()
        if not row:raise SourcePending('source_capture_generation_not_ready')
        payload=_json(row['result_json']);active_plan=payload.get('plan') or {}
        if row['state']!='source_raw_ready':
            # An in-flight authenticated refresh is not counter-evidence to
            # the previously proved basis. Ordinary recompute may retain it
            # as a stage result; every close verifier remains strict.
            control=ws.conn.execute("SELECT result_json FROM automation_attempt WHERE id=? AND state='source_close_refresh'",(payload.get('close_refresh',''),)).fetchone()
            controlled=bool(control and any(t['task_id']==row['id'] and t['generation']==head['generation']
                for t in _json(control[0]).get('tasks',[])))
            if (allow_pending_refresh and controlled and row['state'] in {'source_queued','source_pending'}
                and source_semantic(active_plan)==binding['source_semantic_sha']):
                return {**original,'basis_status':'previously_verified_awaiting_close_refresh',
                    'close_eligible':False,'source_generation':head}
            raise SourcePending('source_capture_generation_not_ready')
        if source_semantic(active_plan)!=binding['source_semantic_sha']:
            raise SourcePending('adopted_source_target_semantics_changed')
        active=_verified_binding(ws,_source_binding(row,payload))
        if journal.digest(proof_semantic(active['source_proof']))!=journal.digest(proof_semantic(original['source_proof'])):
            raise SourcePending('adopted_source_physical_facts_changed')
        active['source_generation']=head
        return active
    return original


def _generation_id(case_id,platform_order_id):
    return 'source-generation-'+journal.digest([case_id,str(platform_order_id)])[:40]


def generation_head(ws,case_id,platform_order_id):
    row=ws.conn.execute("SELECT result_json FROM automation_attempt WHERE id=? AND state='source_generation'",
        (_generation_id(case_id,platform_order_id),)).fetchone()
    return _json(row[0]) if row else None


def _activate_generation(conn,case,task_id,plan):
    """The trusted writer chooses a capture, never timestamp/order heuristics."""
    key=_generation_id(case['id'],plan['platform_order_id'])
    row=conn.execute('SELECT result_json FROM automation_attempt WHERE id=?',(key,)).fetchone()
    before=_json(row[0]) if row else {}
    head={'schema':'ledger_source_generation.v1','active_task_id':task_id,
          'generation':int(before.get('generation',0))+1,'platform_order_id':plan['platform_order_id'],
          'source_semantic_sha':source_semantic(plan)}
    conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET input_sha=excluded.input_sha,result_json=excluded.result_json',
        (key,case['id'],key,case['input_sha'],'source_generation',journal.encoded(head),journal.now()))
    return head



def _close_refresh_status(ws,model,record):
    generations=[];pending=[]
    for entry in record['tasks']:
        row=ws.conn.execute('SELECT * FROM automation_attempt WHERE id=?',(entry['task_id'],)).fetchone()
        head=generation_head(ws,entry['case_id'],entry['platform_order_id'])
        if not head or head['active_task_id']!=entry['task_id'] or head['generation']!=entry['generation']:
            raise Conflict('月结取证代际已被后续任务替代，请重新预览')
        if not row or row['state']!='source_raw_ready':
            detail=_json(row['result_json']) if row else {}
            pending.append({'task_id':entry['task_id'],'state':row['state'] if row else 'missing',
                'reason':detail.get('reason'),'retry':detail.get('automatic_retry')});continue
        payload=_json(row['result_json']);binding=_source_binding(row,payload)
        verified=_verified_binding(ws,binding)
        if journal.digest(proof_semantic(verified['source_proof']))!=entry['expected_source_proof_sha']:
            pending.append({'task_id':entry['task_id'],'state':'physical_source_changed'});continue
        generations.append({'case_id':entry['case_id'],'platform_order_id':entry['platform_order_id'],
            'generation':entry['generation'],'active_task_id':entry['task_id'],
            'receipt_sha256':binding['receipt_sha256'],
            'capture_observation':payload.get('capture_observation'),
            'source_proof_sha':journal.digest(proof_semantic(verified['source_proof']))})
    terminal=any(p['state'] not in ('source_queued','worker_running')
        and (p.get('retry') or {}).get('state')!='waiting' for p in pending)
    return {'refresh_id':record['refresh_id'],'required':bool(record['tasks']),
        'status':'pending' if terminal else 'processing' if pending else 'verified',
        'source_generations':generations,'generation_sha':journal.digest(generations) if not pending else None,
        'pending':pending,'financial_applied':False}


def ensure_close_refresh(ws,model,*,period,store_ids,request_id,actor):
    """A human preview seals one bounded, persistent source-read generation.

    Same request retries reuse its exact generation. No caller can name an old
    source generation or supply financial facts. Only the fixed source reader
    performs network requests; this path never waits in a model loop.
    """
    from .automation_actions import human,_request,sync_month
    from .automation_source_cost_v3 import used
    human(actor);_request(request_id);journal.period(period)
    selected=sorted(set(store_ids));facts=[]
    if not selected:raise Conflict('月结取证需要明确的店铺范围')
    for sid in selected:
        state=ws.state(sid,period)
        if not state or not state.closed:facts.extend(used(ws,sid,period))
    signature=journal.digest({'period':period,'store_ids':selected,'actor':actor['id'],
        'facts':sorted((f['decision_id'],journal.digest(f)) for f in facts)})
    key='source-close-refresh:'+request_id
    old=ws.conn.execute("SELECT result_json FROM automation_attempt WHERE id=? AND state='source_close_refresh'",(key,)).fetchone()
    if old:
        record=_json(old[0])
        if record['signature']!=signature:raise Conflict('月结取证请求的店铺、事实或确认身份已变化，请重新预览')
        return _close_refresh_status(ws,model,record)
    # Establish current case identities without inventing an approval.
    if facts:sync_month(ws,model,period)
    tasks=[]
    with ws.transaction() as conn:
        # Another request can cross the optimistic read while sync_month is
        # establishing current cases. BEGIN IMMEDIATE serializes the writer;
        # repeat the identity check inside it before creating tasks or heads.
        raced=conn.execute("SELECT result_json FROM automation_attempt WHERE id=? AND state='source_close_refresh'",(key,)).fetchone()
        if raced:
            record=_json(raced[0])
            if record['signature']!=signature:raise Conflict('月结取证请求的店铺、事实或确认身份已变化，请重新预览')
            return _close_refresh_status(ws,model,record)
        for fact in facts:
            verified=verified_fact_source(ws,model,fact,allow_pending_refresh=True);original=verified['plan']
            case=conn.execute('SELECT * FROM automation_case WHERE store_id=? AND period=?',(fact['store_id'],period)).fetchone()
            if not case:raise Conflict('月结取证案件不存在')
            case=dict(case);_current(ws,model,case['id'],case['run_id'],case['input_sha'])
            tid='source-'+journal.digest([key,case['id'],original['platform_order_id'],signature])[:40]
            if any(t['task_id']==tid for t in tasks):continue
            plan={**original,'case_id':case['id'],'run_id':case['run_id'],'input_sha':case['input_sha'],
                'demand_anchor':{'kind':'authenticated_close_preview_source_refresh',
                    'request_id':request_id,'decision_id':fact['decision_id'],'fact_sha':journal.digest(fact)}}
            from .automation_handoff import context_sha
            context=context_sha(ws,model,case['store_id'],period,case['input_sha'])
            payload={'kind':CAPABILITY,'source_context_sha':context,'plan':plan,
                'plan_sha256':journal.digest(plan),'reason':None,'financial_applied':False,
                'close_refresh':key,'cache_reuse_allowed':False}
            conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
                (tid,case['id'],tid,case['input_sha'],'source_queued',journal.encoded(payload),journal.now()))
            head=_activate_generation(conn,case,tid,plan)
            tasks.append({'task_id':tid,'case_id':case['id'],'platform_order_id':plan['platform_order_id'],
                'generation':head['generation'],'input_sha':case['input_sha'],
                'expected_source_proof_sha':journal.digest(proof_semantic(verified['source_proof']))})
            conn.execute("UPDATE automation_case SET status='processing',stage='gathering' WHERE id=?",(case['id'],))
        record={'refresh_id':key,'signature':signature,'period':period,'store_ids':selected,
                'actor':actor['id'],'tasks':tasks}
        # Even an empty source set is sealed: adding a fact later must not
        # silently change the meaning of an already used human request ID.
        lineage=tasks[0]['case_id'] if tasks else '__company__'
        conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
            (key,lineage,key,signature,'source_close_refresh',journal.encoded(record),journal.now()))
    return _close_refresh_status(ws,model,record)


def verify_close_refresh(ws,model,*,refresh_id,generation_sha):
    row=ws.conn.execute("SELECT result_json FROM automation_attempt WHERE id=? AND state='source_close_refresh'",(refresh_id,)).fetchone()
    if not row:raise Conflict('月结取证收据不存在')
    value=_close_refresh_status(ws,model,_json(row[0]))
    if value['status']!='verified' or value['generation_sha']!=generation_sha:
        raise Conflict('月结来源仍在刷新或已经变化，不能确认旧预览')
    return value


def close_refresh_pending(ws,case_id,input_sha):
    row=ws.conn.execute("SELECT 1 FROM automation_attempt WHERE case_id=? AND input_sha=? AND state='source_queued' AND json_extract(result_json,'$.close_refresh') IS NOT NULL LIMIT 1",(case_id,input_sha)).fetchone()
    return bool(row)



def _equivalent_close_capture(ws,model,case,task_id,payload,result):
    """Only a server-sealed used-fact refresh can avoid a finance dispatch."""
    control_id=payload.get('close_refresh')
    control=ws.conn.execute("SELECT result_json FROM automation_attempt WHERE id=? AND state='source_close_refresh'",(control_id,)).fetchone() if control_id else None
    if not control or result['source_proof'].get('status')!='proved':return False
    record=_json(control[0]);head=generation_head(ws,case['id'],payload['plan']['platform_order_id'])
    if not head or head['active_task_id']!=task_id:return False
    entries=[e for e in record.get('tasks',[]) if e['task_id']==task_id and e['case_id']==case['id'] and e['generation']==head['generation']]
    if len(entries)!=1 or entries[0]['expected_source_proof_sha']!=journal.digest(proof_semantic(result['source_proof'])):return False
    from .automation_source_cost_v3 import used
    anchor=payload['plan'].get('demand_anchor') or {}
    return any(f['decision_id']==anchor.get('decision_id') and journal.digest(f)==anchor.get('fact_sha')
               for f in used(ws,case['store_id'],case['period']))


def _capture_observation(ws,case,task_id,job_id,payload,result,receipt_sha,proof_path,proof_sha,equivalent):
    folder=proof_path.parent
    refs=[{'path':str(folder/(r['sha256']+'.raw')),'sha256':r['sha256'],'role':'source_raw'} for r in result['raw_refs']]
    refs.extend({'path':str(ws.path_of(a['sha256'])),'sha256':a['sha256'],'role':'original_workbook'}
        for a in payload['plan']['native_anchors']+payload['plan']['cost_anchors'])
    refs.extend([{'path':str(folder/'receipt.json'),'sha256':receipt_sha,'role':'source_receipt'},
                 {'path':str(proof_path),'sha256':proof_sha,'role':'independent_source_proof'}])
    value={'schema':'ledger_source_capture_observation.v1','store_id':case['store_id'],'period':case['period'],
        'task_id':task_id,'source_attempt_id':job_id,'source_refs':refs,
        'source_proof_semantic_sha':journal.digest(proof_semantic(result['source_proof'])),
        'equivalent_used_fact_refresh':equivalent,'financial_applied':False}
    key='source-observation:'+journal.digest([task_id,job_id,receipt_sha]);oid=journal.digest(key)[:32]
    ws.conn.execute('INSERT OR IGNORE INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
        (oid,case['id'],key,case['input_sha'],'source_capture_observation',journal.encoded(value),journal.now()))
    return {'observation_id':oid,'source_refs':refs,'equivalent_used_fact_refresh':equivalent}


def install_routes(router, workspace, model, service, owned, service_auth, prior, clock):
    from fastapi import HTTPException, Request as HTTPRequest
    from pydantic import BaseModel, ConfigDict, Field

    class Submit(BaseModel):
        model_config = ConfigDict(extra='forbid')
        case_id: str = Field(min_length=1,max_length=128)
        run_id: int = Field(gt=0)
        input_sha: str = Field(min_length=1,max_length=128)
        coverage_key: str = Field(min_length=1,max_length=512)
        context_sha: str = Field(min_length=1,max_length=128)

    class Finish(BaseModel):
        model_config = ConfigDict(extra='forbid')
        job_id: str
        lease_token: int
        input_sha: str
        receipt_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    def translate(operation):
        try:
            return operation()
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise HTTPException(422, getattr(exc, 'reason', 'source_receipt_or_scope_unverified')) from exc

    @router.post('/api/automation/source-recovery/requests')
    def request_source(body: Submit, request: HTTPRequest):
        service(request)
        return translate(lambda: submit(workspace(), model(), body.model_dump()))

    @router.get('/api/automation/source-recovery/requests/{task_id}')
    def read_source(task_id: str, request: HTTPRequest):
        service(request)
        value = status(workspace(), task_id)
        if value is None:
            raise HTTPException(404, '取证任务不存在')
        return value

    @router.get('/api/automation/source-recovery/requests/{task_id}/plan')
    def source_plan(task_id: str, job_id: str, lease_token: int, request: HTTPRequest):
        identity = service(request)
        ws = workspace()
        case, attempt = owned(ws, job_id, identity, lease_token)
        metadata = json.loads(attempt['result_json'])
        task = ws.conn.execute('SELECT * FROM automation_attempt WHERE id=?', (task_id,)).fetchone()
        if (not task or metadata.get('source_task_id') != task_id or task['case_id'] != case['id']
                or task['input_sha'] != case['input_sha'] or task['state'] != 'source_queued'):
            raise HTTPException(409, '取证任务与当前租约不一致')
        payload = json.loads(task['result_json'])
        head=generation_head(ws,case['id'],payload['plan']['platform_order_id'])
        if head and head['active_task_id']!=task_id:raise HTTPException(409,'上游取证代际已被替代')
        return {'task_id': task_id, 'plan': payload['plan'], 'plan_sha256': payload['plan_sha256']}

    @router.post('/api/automation/source-recovery/requests/{task_id}/finish')
    def finish_source(task_id: str, body: Finish, request: HTTPRequest):
        identity = service(request)
        ws = workspace()
        key = 'source-finish:' + body.job_id
        signature = journal.digest([task_id, body.model_dump(), identity['id']])
        old=prior(ws.conn,key,signature)
        if old:return old
        preview,_=owned(ws,body.job_id,identity,body.lease_token,body.input_sha)
        from .finance_guard import guard
        with guard(ws.root,preview['store_id'],preview['period']),ws.transaction() as conn:
            old = prior(conn, key, signature)
            if old:
                return old
            case, attempt = owned(ws, body.job_id, identity, body.lease_token, body.input_sha)
            metadata = json.loads(attempt['result_json'])
            task = conn.execute('SELECT * FROM automation_attempt WHERE id=?', (task_id,)).fetchone()
            if not task or metadata.get('source_task_id') != task_id or task['case_id'] != case['id'] or task['input_sha'] != case['input_sha']:
                raise HTTPException(409, '取证任务已过期')
            payload = json.loads(task['result_json'])
            head=generation_head(ws,case['id'],payload['plan']['platform_order_id'])
            if head and head['active_task_id']!=task_id:raise HTTPException(409,'上游取证代际已被替代')
            translate(lambda:_current(ws, model(), case['id'], case['run_id'], case['input_sha']))
            if any(payload['plan'].get(k)!=v for k,v in publication_observation(ws,model(),case['store_id'],case['period']).items()):
                raise HTTPException(409,'自动发布范围在取证期间已变化')
            result = translate(lambda: verify_receipt(ws, payload['plan'], task_id, body.job_id, body.receipt_sha256))
            from .source_dates import publish_from_task
            dates=translate(lambda:publish_from_task(ws,model(),case,payload['plan'],task_id,body.job_id,body.receipt_sha256,identity))
            proof_path = ws.root / 'source-recovery' / task_id / body.job_id / 'independent-proof.json'
            proof_sha = durable(proof_path, result)
            payload.update(reason=result['reason'], proof_sha256=proof_sha, source_attempt_id=body.job_id,
                           receipt_sha256=body.receipt_sha256,captured_worker_id=identity['id'],
                           worker_configuration_sha=metadata.get('worker_configuration_sha'))
            payload['original_date_input']=dates
            equivalent_close=_equivalent_close_capture(ws,model(),case,task_id,payload,result)
            payload['capture_observation']=_capture_observation(ws,case,task_id,body.job_id,payload,result,body.receipt_sha256,proof_path,proof_sha,equivalent_close)
            _record_retry_policy(payload,result,proof_path.parent,int(clock()))
            if (result['state']=='source_raw_ready' and dates.get('status')!='source_input_queued'
                and not equivalent_close
                and publication_observation(ws,model(),case['store_id'],case['period'])['publication_allowed']):
                dispatch_key='source-raw-dispatch:'+journal.digest([task_id,body.receipt_sha256])
                if not conn.execute('SELECT 1 FROM automation_attempt WHERE request_id=?',(dispatch_key,)).fetchone():
                    conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
                        (journal.ident(),case['id'],dispatch_key,case['input_sha'],'dispatch_pending',
                         journal.encoded({'store_id':case['store_id'],'target_period':case['period'],
                             'fingerprint':'automation:'+journal.digest(dispatch_key),
                             'publication_scope_identity':payload['plan']['publication_scope_identity'],
                             'purpose':'independently_verify_source_cost_without_model'}),journal.now()))
            stage='gathering'
            # Publish this technical outcome inside the same transaction
            # before registering a wait. A just-failed retry is no longer a
            # queued attempt whose old handoff should be suppressed.
            conn.execute('UPDATE automation_attempt SET state=?,result_json=? WHERE id=?',
                         (result['state'], journal.encoded(payload), task_id))
            if result['state']=='source_pending':
                from .automation_handoff import record
                receipt_path=ws.root/'source-recovery'/task_id/body.job_id/'receipt.json'
                login=payload['automatic_retry'].get('failure_kind')=='login_required'
                record(ws,model(),case,body.job_id,identity['id'],[{
                    'tool_id':'collect_case_evidence','source_kind':'order',
                    'reason_code':'login_required' if login else 'source_unavailable',
                    'artifact_refs':[{'path':str(receipt_path),'sha256':body.receipt_sha256}]}])
                stage='waiting_maintenance'
            conn.execute('UPDATE automation_attempt SET state=?,result_json=? WHERE id=?',
                         (result['state'], journal.encoded({**metadata, 'source_receipt_sha256': body.receipt_sha256,
                                                         'proof_sha256': proof_sha}), body.job_id))
            # Raw availability is neither Checker completion nor a financial write.
            conn.execute("UPDATE automation_case SET stage=?,lease_owner='',lease_until=0,updated_at=? WHERE id=? AND lease_token=?",
                         (stage,journal.now(), case['id'], body.lease_token))
            aid = service_auth(ws, key, identity, 'worker.source_raw_receipt', case['input_sha'],
                               {'task_id': task_id, 'job_id': body.job_id, 'lease_token': body.lease_token})
            return journal.finish(conn, key, 'worker.source_raw_receipt', signature, aid,
                                  {'ok': True, 'task': status(ws, task_id), 'business_complete': False})
