"""Typed source waiting, never financial readiness or a second work queue."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
import sqlite3

from . import automation_store as journal

REASONS={'source_unavailable','login_required','original_evidence_missing','sku_contract_unknown','technical_failure'}
SOURCES={'order','history_cost','seller_member','statement','freight','system'}
TOOLS={'collect_case_evidence','request_member_seller_evidence','poll_member_seller_evidence','apply_approved_history_costs','worker_runtime'}
LABELS={'shop_owner':'店铺资料维护角色','warehouse':'仓库资料维护角色','logistics':'物流资料维护角色',
    'operations':'运营资料维护角色','finance':'财务资料维护角色','source_owner':'资料提供角色',
    'system_maintenance':'系统维护角色','source_administrator':'来源登录维护角色','source_contract_owner':'来源接口维护角色'}
ORIGINALS={'order':('original_orders','原始订单资料'),'history_cost':('original_day_cost','原始日有效的成本凭据'),
    'seller_member':('platform_member_evidence','平台成员或原订单卖家凭据'),'statement':('original_statement','原始结算流水'),
    'freight':('carrier_original','承运商运费原件')}


def _health_state(ws):
    path=ws.root/'order-feed.db'
    if not path.exists():return None
    try:
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as conn:
            row=conn.execute('SELECT health_json FROM feed_state WHERE id=1').fetchone()
        value=json.loads(row[0]) if row and row[0] else {}
        # Source liveness flags, not probe timestamps or global sales counts.
        return {'healthy':value.get('healthy'),'degraded':sorted(str(v) for v in value.get('degraded',[]) if isinstance(v,str))}
    except (sqlite3.Error,ValueError,TypeError):return {'state':'unreadable'}


@lru_cache(maxsize=256)
def _read_material(path,expected,stamp):
    import polars as pl
    from .automation_period_proof import COLLECTABLE_COLUMNS
    from .storage_integrity import digest,verified
    target=Path(path)
    try:
        if not verified(target) or digest(target)!=expected:return None
        columns=set(pl.scan_parquet(target).collect_schema().names())&COLLECTABLE_COLUMNS
        return pl.read_parquet(target,columns=sorted(columns))
    except (OSError,ValueError,pl.exceptions.PolarsError):return None


def _material(ws,row,manifest,captured):
    from .automation_period_proof import collectable_scope
    if captured.get('collectable_scope') is not None:return captured['collectable_scope']
    if manifest.get('collectable_scope') is not None:return manifest['collectable_scope']
    if not row:return {'history_cost':{'sha':'no-run','required':True},'order':{'sha':'no-run','required':True},'seller_member':{'sha':'no-run','required':True}}
    artifacts={a['path']:a for a in manifest.get('artifacts',[])}
    def frame(suffix):
        path=ws.facts_path(row['id']).with_suffix(suffix)
        ref=artifacts.get(path.relative_to(ws.root).as_posix())
        if not ref or not path.exists():return None
        stat=path.stat()
        return _read_material(str(path),ref['sha'],(stat.st_mtime_ns,stat.st_size))
    return collectable_scope(frame('.pricing.parquet'),frame('.coverage.parquet'),frame('.order-issues.parquet'),json.loads(row['result']))


def context_sha(ws,model,store_id,period,input_sha,*,source_kind='',reason_code='',worker_id='',tool_id=''):
    """A source/rule change retires the wait without changing the Checker."""
    from datetime import datetime,timezone
    from .automation_checker import current_run,feed_scope
    from .version import engine_version
    from .automation_rules import contracts
    from .automation_deterministic import enabled
    policies=journal.active_decisions(ws.conn,period)
    grants=[];stamp=datetime.now(timezone.utc)
    for key,value in policies.items():
        if key[0] not in ('',store_id) or not (key[1].startswith('rule_grant:') or key[1]=='store_scope'):continue
        body=value['values'];phase='active'
        if key[1].startswith('rule_grant:'):
            try:phase='expired' if datetime.fromisoformat(body['valid_until'])<=stamp else 'not_started' if datetime.fromisoformat(body['valid_from'])>stamp else 'active'
            except (ValueError,KeyError,TypeError):phase='invalid'
        grants.append((key,value['id'],body,phase))
    row=current_run(ws,store_id,period)
    saved=ws.conn.execute('SELECT dependency_sha,manifest_json FROM automation_run_proof WHERE run_id=?',(row['id'],)).fetchone() if row else None
    manifest=json.loads(saved['manifest_json']) if saved else {}
    validation=ws.conn.execute('SELECT manifest_json FROM automation_run_validation WHERE run_id=? ORDER BY rowid DESC LIMIT 1',(row['id'],)).fetchone() if row else None
    captured=json.loads(validation['manifest_json']) if validation else manifest
    material=_material(ws,row,manifest,captured)
    selected=material.get(source_kind,material) if source_kind else material
    slots=[tuple(r) for r in ws.conn.execute('SELECT store_id,name,sha FROM slot WHERE store_id IN (?,?) ORDER BY store_id,name',(store_id,'__shared__'))]
    line_revision=ws.conn.execute('SELECT coalesce(max(id),0) FROM cost_line_log WHERE store_id=? AND period=?',(store_id,period)).fetchone()[0]
    source_version=ws.conn.execute("SELECT coalesce(max(id),0) FROM version WHERE store_id IN (?,?) AND name<>'__commission_rules__'",(store_id,'__shared__')).fetchone()[0]
    verified_period=bool(saved and journal.digest(slots)==journal.digest(captured.get('slots',[]))
        and captured.get('version',0)>=source_version and captured.get('manual_revision',0)==line_revision)
    source_inputs=({'kind':'verified_period'} if verified_period and source_kind not in {'system','statement','freight'}
        else {'kind':'unverified_source','slots':slots,'line_revision':line_revision,'source_version':source_version})
    # A sealed demand's original anchors remain the same when an unrelated
    # company document is added. Replacing/removing a demanded active original
    # still falls back to the conservative whole-source epoch.
    index=manifest.get('source_recovery_index') or {}
    anchors=set()
    for demand in index.get('orders',{}).values():
        for role in ('native','cost'):
            anchors.update(a['sha256'] for a in demand.get(role,[]) if a.get('sha256'))
    anchors.update(d['anchor']['sha256'] for d in index.get('date_demands',{}).values() if d.get('anchor',{}).get('sha256'))
    current_anchors=sorted(row for row in slots if row[2] in anchors)
    recorded_anchors=sorted(tuple(row) for row in captured.get('slots',[]) if row[2] in anchors)
    if (anchors and {row[2] for row in current_anchors}==anchors and current_anchors==recorded_anchors
        and captured.get('manual_revision',0)==line_revision):
        source_inputs={'schema':'ledger_source_demand_context.v1','kind':'sealed_active_originals',
                       'anchors':current_anchors,'line_revision':line_revision}
    feed=feed_scope(ws)
    # Financial input_sha still fences every submission. Queue delivery/ack is
    # not new source evidence, so it must not wake an unchanged missing-file
    # wait and restart model spending after an empty Manager pass.
    store=model.store(store_id)
    sources=[s.model_dump(mode='json') for s in model.sources if s.platform in ('*','any',store.platform)]
    source_ids={s['id'] for s in sources}
    source_model={'store':{'id':store.id,'name':store.name,'aliases':store.aliases,'platform':store.platform},
        'sources':sources,'metrics':[m.model_dump(mode='json') for raw in model.metrics if raw.id in {'goods_cost','goods_return_cost','reshipment_cost','dropship_cost'} and (m:=raw.for_platform(store.platform)) is not None],
        'templates':[t.model_dump(mode='json') for t in model.templates if t.source in source_ids]}
    configuration=ws.conn.execute('SELECT configuration_sha,configuration_json FROM automation_worker WHERE id=?',(worker_id,)).fetchone() if worker_id and reason_code!='original_evidence_missing' else None
    configured=None
    if configuration:
        descriptor=json.loads(configuration['configuration_json'])
        configured=descriptor.get('runtime_sha' if tool_id=='worker_runtime' else 'source_sha',configuration['configuration_sha'])
    return journal.digest({'store_id':store_id,'period':period,'source_inputs':source_inputs,'feed':{key:feed.get(key) for key in ('snapshot_id','last_error')} if feed else None,'source_health':_health_state(ws),
        'collectable':selected,'model':source_model,'grants':sorted(grants),'rules':[(name,value[0]) for name,value in contracts().items()],
        'rollout':enabled(),'engine':engine_version() if reason_code!='original_evidence_missing' else None,
        'worker_configuration':configured})


def _source_role(model,source_kind):
    preferred={'order':('order_detail',),'history_cost':('order_cost','cost'),
        'seller_member':('order_detail',),'statement':('settlement',),'freight':('freight',)}.get(source_kind,())
    source=next((s for name in preferred for s in model.sources if s.id==name),None)
    if source is None and source_kind in {'order','seller_member'}:source=next((s for s in model.sources if s.is_spine),None)
    return source.owner_role if source else 'source_owner'


def describe(model,reason,source):
    if reason not in REASONS or source not in SOURCES:raise ValueError('未知工具取证分类')
    if reason=='original_evidence_missing':
        if source not in ORIGINALS:raise ValueError('原始凭据缺失必须绑定明确资料类型')
        evidence,material=ORIGINALS[source];role=_source_role(model,source)
        return {'kind':'source','evidence_type':evidence,'role':role,'missing_material':material,
            'summary':'等待提供'+material,'delivery_hint':'由'+LABELS[role]+'通过原有交表或来源接口补齐；进入现有核算队列后自动继续。'}
    if reason=='login_required':
        return {'kind':'maintenance','evidence_type':'source_login','role':'source_administrator',
            'missing_material':'可用的来源登录状态','summary':'来源尚未登录，等待登录维护',
            'delivery_hint':'由来源登录维护角色恢复登录或凭证；资料同步产生新输入后系统自动继续。'}
    if reason=='sku_contract_unknown':
        if source not in {'order','history_cost'}:raise ValueError('SKU合同分类不适用于该来源')
        return {'kind':'maintenance','evidence_type':'sku_quantity_contract','role':'source_contract_owner',
            'missing_material':'明确的SKU类型及数量单位合同','summary':'SKU类型或数量单位合同尚未确认',
            'delivery_hint':'由来源接口维护角色核实类型和单位合同；按已批准规则更新后自动继续，不能猜码或补零。'}
    if reason=='source_unavailable':
        return {'kind':'maintenance','evidence_type':'source_availability','role':'system_maintenance',
            'missing_material':'可读取的来源接口或文件','summary':'来源暂不可用，等待系统维护',
            'delivery_hint':'由系统维护角色恢复来源连接或文件访问；新同步输入到达后系统自动继续。'}
    return {'kind':'maintenance','evidence_type':'worker_execution','role':'system_maintenance',
        'missing_material':'可恢复的工具执行环境','summary':'工具执行遇到技术故障，等待系统维护',
        'delivery_hint':'由系统维护角色核对本机执行回执；修复并产生新的资料或规则版本后自动继续。'}


def _view(row,model):
    detail=describe(model,row['reason_code'],row['source_kind'])
    group=journal.digest([row['reason_code'],row['source_kind'],detail['evidence_type'],detail['role']])[:32]
    return {**detail,'handoff_id':row['id'],'reason_code':row['reason_code'],'source_kind':row['source_kind'],
        'role_label':LABELS[detail['role']],'assignee_status':'role_unconfigured','since':row['since'],
        'artifact_refs':json.loads(row['source_refs_json']),'notification_key':'source-wait:'+group,
        'evidence_location':'worker_local','worker_id':row['worker_id']}


def active(ws,model,store_id,period,input_sha):
    from .automation_checker import current_run
    from .source_recovery import retry_queued
    if retry_queued(ws,journal.digest([store_id,period])[:32],input_sha):return []
    case_id=journal.digest([store_id,period])[:32]
    rows=ws.conn.execute('SELECT * FROM automation_source_handoff WHERE case_id=? ORDER BY since,id',(case_id,)).fetchall()
    if not rows:return []
    latest=current_run(ws,store_id,period)
    proof=ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(latest['id'],)).fetchone() if latest else None
    captured=ws.conn.execute('SELECT manifest_json FROM automation_run_validation WHERE run_id=? ORDER BY rowid DESC LIMIT 1',(latest['id'],)).fetchone() if latest else None
    scope=_material(ws,latest,json.loads(proof[0]) if proof else {},json.loads(captured[0]) if captured else {})
    result=[]
    for row in rows:
        current=context_sha(ws,model,store_id,period,input_sha,source_kind=row['source_kind'],reason_code=row['reason_code'],worker_id=row['worker_id'],tool_id=row['tool_id'])
        if row['context_sha']==current:
            if scope.get(row['source_kind'],{}).get('required') is False:continue
            result.append(_view(row,model))
    return result


def record(ws,model,case,attempt_id,worker_id,outcomes):
    """Called inside terminal's authorization/attempt/receipt transaction."""
    created=[]
    for outcome in outcomes:
        if outcome['tool_id'] not in TOOLS:raise ValueError('未知受信工具')
        if outcome['tool_id']=='worker_runtime' and (outcome['source_kind']!='system' or outcome['reason_code']!='technical_failure'):
            raise ValueError('本机执行故障只能交系统维护')
        detail=describe(model,outcome['reason_code'],outcome['source_kind'])
        expected=context_sha(ws,model,case['store_id'],case['period'],case['input_sha'],source_kind=outcome['source_kind'],reason_code=outcome['reason_code'],worker_id=worker_id,tool_id=outcome['tool_id'])
        existing=ws.conn.execute('SELECT * FROM automation_source_handoff WHERE case_id=? AND context_sha=? AND reason_code=? AND source_kind=? AND evidence_type=?',
            (case['id'],expected,outcome['reason_code'],outcome['source_kind'],detail['evidence_type'])).fetchone()
        if not existing:
            identifier=journal.ident()
            ws.conn.execute('INSERT INTO automation_source_handoff VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (identifier,case['id'],case['input_sha'],expected,outcome['reason_code'],outcome['source_kind'],detail['evidence_type'],
                 detail['role'],detail['kind'],outcome['tool_id'],journal.encoded(outcome['artifact_refs']),worker_id,attempt_id,journal.now()))
            existing=ws.conn.execute('SELECT * FROM automation_source_handoff WHERE id=?',(identifier,)).fetchone()
            created.append(identifier)
    views=active(ws,model,case['store_id'],case['period'],case['input_sha'])
    if not views:raise ValueError('当前取证需求已经消失，不能登记无关等待')
    return {'items':views,'new_handoff_ids':created,'since':min(v['since'] for v in views)}


def groups(rows):
    grouped={}
    for row in rows:
        for item in (row.get('waiting') or {}).get('items',[]):
            key=item['notification_key']
            group=grouped.setdefault(key,{k:v for k,v in item.items() if k not in {'artifact_refs','worker_id','handoff_id'}})
            group.setdefault('id',key);group.setdefault('store_ids',[]);group.setdefault('periods',[]);group.setdefault('scopes',[])
            scope={'store_id':row['store_id'],'period':row['period']}
            if scope not in group['scopes']:group['scopes'].append(scope)
            if row['store_id'] not in group['store_ids']:group['store_ids'].append(row['store_id'])
            if row['period'] not in group['periods']:group['periods'].append(row['period'])
            group['since']=min(group['since'],item['since']);group['count']=len(group['scopes'])
    return list(grouped.values())


def refresh_waiting(ws,model):
    """Wake only invalidated source/rule waits before the next worker claim."""
    from .automation_actions import sync_month
    periods=set()
    for row in ws.conn.execute("SELECT store_id,period,input_sha FROM automation_case WHERE stage IN ('waiting_source','waiting_maintenance')"):
        if not active(ws,model,row['store_id'],row['period'],row['input_sha']):periods.add(row['period'])
    for period in sorted(periods):sync_month(ws,model,period)
