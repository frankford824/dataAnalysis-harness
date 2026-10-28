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

import polars as pl

from . import automation_store as journal
from .automation_actions import Conflict
from .storage_integrity import digest as file_sha

CAPABILITY = 'jst_source_recovery.v1'
MAX_KEYS = 2000
PARSER_SHA = '0c636672b044492c249572b7f3940eaa4cdf9c3f0d05b8761ac7ddd3929477b8'
_SHA = re.compile(r'^[0-9a-f]{64}$')
_ID = re.compile(r'^[1-9][0-9]{0,23}$')


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
    native_sources = {s.id for s in ingestion.model.sources if s.is_spine and s.platform in ('*', store.platform)}
    cost_sources = {m.source for m in ingestion.model.metrics if m.id == 'goods_cost'}
    result = {key: {'native': [], 'cost': [], 'internal_order_ids': []} for key in wanted}
    for item in ingestion.known:
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
        columns = [parent, '__sheet__', '__row__'] + [k for k in ('internal_order_id', 'store_name') if k in frame.columns]
        for row in rows.select(columns).to_dicts():
            if row.get('store_name') not in (None, '', store.id, store.name, *store.aliases):
                continue
            key = str(row[parent])
            anchor = {'sha256': sha, 'sheet': str(row['__sheet__']), 'row': int(row['__row__'])}
            if anchor['row'] <= 2:
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
    with ws.transaction() as conn:
        _current(ws, model, body['case_id'], body['run_id'], body['input_sha'])
        if context_sha(ws, model, case['store_id'], case['period'], case['input_sha']) != source_context:
            raise Conflict('取证来源上下文已变化')
        if plan and any(plan[k]!=v for k,v in publication_observation(ws,model,case['store_id'],case['period']).items()):
            raise Conflict('自动发布范围在取证计划封存前已变化')
        conn.execute('INSERT OR IGNORE INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
                     (task_id, case['id'], task_id, case['input_sha'], state, journal.encoded(payload), journal.now()))
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
            'financial_applied': False, 'business_complete': False}


def queued(ws, case_id, input_sha):
    rows = ws.conn.execute("SELECT * FROM automation_attempt WHERE case_id=? AND input_sha=? AND state='source_queued' ORDER BY rowid", (case_id, input_sha)).fetchall()
    return next((dict(r) for r in rows if json.loads(r['result_json']).get('kind') == CAPABILITY), None)


def ready(ws,case_id,input_sha):
    rows=ws.conn.execute("SELECT * FROM automation_attempt WHERE case_id=? AND input_sha=? AND state='source_raw_ready' ORDER BY rowid",
                         (case_id,input_sha)).fetchall()
    return next((dict(r) for r in rows if _json(r['result_json']).get('kind')==CAPABILITY),None)


def configuration_changed(ws, worker_id, configuration_sha):
    """Retry an actual tool failure once per new, server observed host epoch."""
    rows=ws.conn.execute("SELECT * FROM automation_attempt WHERE state='source_pending'").fetchall()
    for row in rows:
        payload=_json(row['result_json'])
        if (payload.get('kind')!=CAPABILITY or not payload.get('plan')
            or payload.get('captured_worker_id')!=worker_id
            or payload.get('worker_configuration_sha') in (None,'',configuration_sha)):
            continue
        case=ws.conn.execute('SELECT input_sha FROM automation_case WHERE id=?',(row['case_id'],)).fetchone()
        if not case or case['input_sha']!=row['input_sha']:continue
        previous={k:payload.get(k) for k in ('reason','source_attempt_id','proof_sha256','receipt_sha256','worker_configuration_sha')}
        payload.setdefault('prior_source_receipts',[]).append(previous)
        payload.update(reason=None,worker_configuration_sha=configuration_sha)
        ws.conn.execute('UPDATE automation_attempt SET state=?,result_json=? WHERE id=?',
                        ('source_queued',journal.encoded(payload),row['id']))
        ws.conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
                        (journal.ident(),row['case_id'],row['id'],row['input_sha'],'source_configuration_recovery',
                         journal.encoded({'source_task_id':row['id'],'previous':previous,
                                          'configuration_sha':configuration_sha}),journal.now()))


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


def verified_source(ws, model, request):
    """Backend-only resolver for a subsequent trusted provider/source consumer.

    It always reopens original bytes and reruns the independent normalizer.
    Raw status/flags remain verbatim (including missing/unknown); callers still
    owe their own rule grant, price interval, quantity policy, CAS and freeze
    checks. No DTO silently turns an original Split into Sent or fills flags.
    """
    case=_current(ws,model,request['case_id'],request['run_id'],request['input_sha'])
    from .automation_handoff import context_sha
    current_context=context_sha(ws,model,case['store_id'],case['period'],case['input_sha'])
    candidates=[]
    for row in ws.conn.execute("SELECT * FROM automation_attempt WHERE case_id=? AND input_sha=? AND state='source_raw_ready'",
                               (case['id'],case['input_sha'])):
        payload=_json(row['result_json']);plan=payload.get('plan') or {}
        if (payload.get('kind')==CAPABILITY and payload.get('source_context_sha')==current_context
            and plan.get('coverage_key')==request['coverage_key'] and plan.get('coverage_context_sha')==request['context_sha']):
            candidates.append((dict(row),payload,plan))
    if len(candidates)!=1:raise SourcePending('current_source_receipt_not_unique_or_missing')
    row,payload,plan=candidates[0]
    if any(plan.get(k)!=v for k,v in publication_observation(ws,model,case['store_id'],case['period']).items()):
        raise SourcePending('publication_scope_context_changed')
    result=verify_receipt(ws,plan,row['id'],payload['source_attempt_id'],payload['receipt_sha256'])
    folder=ws.root/'source-recovery'/row['id']/payload['source_attempt_id']
    from .jst_source_members import parse_order
    raw_orders={};refs=[]
    for ref in result['raw_refs']:
        path=folder/(ref['sha256']+'.raw')
        refs.append({'path':str(path),'sha256':ref['sha256'],'source':'fixed_jst_source_reader'})
    receipt=_json((folder/'receipt.json').read_bytes())
    for ref in receipt['nodes']:
        data,_=parse_order((folder/(ref['sha256']+'.raw')).read_bytes(),str(ref['order_id']))
        raw_orders[str(ref['order_id'])]=data
    for ref in plan['native_anchors']+plan['cost_anchors']:
        refs.append({'path':str(ws.path_of(ref['sha256'])),'sha256':ref['sha256'],'source':'sealed_original_workbook'})
    return {'task_id':row['id'],'plan':plan,'source_proof':result['source_proof'],
            'raw_orders':raw_orders,'source_refs':refs,'financial_applied':False,'business_complete':False}


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
            _current(ws, model(), case['id'], case['run_id'], case['input_sha'])
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
            stage='gathering'
            if result['state']=='source_pending':
                from .automation_handoff import record
                receipt_path=ws.root/'source-recovery'/task_id/body.job_id/'receipt.json'
                login=result['reason'] in {'source_session_or_http_unavailable','source_session_refresh_required'}
                record(ws,model(),case,body.job_id,identity['id'],[{
                    'tool_id':'collect_case_evidence','source_kind':'order',
                    'reason_code':'login_required' if login else 'source_unavailable',
                    'artifact_refs':[{'path':str(receipt_path),'sha256':body.receipt_sha256}]}])
                stage='waiting_maintenance'
            conn.execute('UPDATE automation_attempt SET state=?,result_json=? WHERE id=?',
                         (result['state'], journal.encoded(payload), task_id))
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
