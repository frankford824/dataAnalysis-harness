"""Approved whole-period costs are inputs, independent of payout and close.

There is no per-order allocation here. Documentary history is accepted only by
an explicitly scoped disposable replay, and never represents a current login.
"""
from __future__ import annotations
from pathlib import Path
from copy import deepcopy
import hashlib,json,os,tempfile
from . import automation_store as journal
from .workspace import WorkspaceError

KIND='period_cost_input'
KEYS={'goods','dropship','reshipment'}
SCHEMA="""
CREATE TABLE IF NOT EXISTS period_cost_input (
 id TEXT PRIMARY KEY,store_id TEXT NOT NULL,period TEXT NOT NULL,source_run_id INTEGER NOT NULL,
 source_binding_sha TEXT NOT NULL,costs_json TEXT NOT NULL,evidence_json TEXT NOT NULL,
 authorization_id TEXT NOT NULL,actor TEXT NOT NULL,identity_kind TEXT NOT NULL,
 request_id TEXT NOT NULL UNIQUE,reason TEXT NOT NULL,at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS period_cost_input_scope ON period_cost_input(store_id,period);
CREATE TRIGGER IF NOT EXISTS period_cost_input_no_replace BEFORE INSERT ON period_cost_input
 WHEN EXISTS(SELECT 1 FROM period_cost_input WHERE id=NEW.id OR request_id=NEW.request_id)
 BEGIN SELECT RAISE(ABORT,'immutable period cost input'); END;
CREATE TRIGGER IF NOT EXISTS period_cost_input_no_update BEFORE UPDATE ON period_cost_input
 BEGIN SELECT RAISE(ABORT,'immutable period cost input'); END;
CREATE TRIGGER IF NOT EXISTS period_cost_input_no_delete BEFORE DELETE ON period_cost_input
 BEGIN SELECT RAISE(ABORT,'immutable period cost input'); END;
"""


def install(conn):conn.executescript(SCHEMA)

def revision(ws,store_id,period):
    return int(ws.conn.execute("SELECT coalesce(max(rowid),0) FROM automation_decision WHERE kind='period_cost_input' AND store_id=? AND period=?",(store_id,period)).fetchone()[0])

def latest(ws,store_id,period):
    row=ws.conn.execute('SELECT rowid,* FROM period_cost_input WHERE store_id=? AND period=? ORDER BY rowid DESC LIMIT 1',(store_id,period)).fetchone()
    if not row:return None
    value=dict(row)
    decision=ws.conn.execute("SELECT id FROM automation_decision WHERE kind=? AND action='apply' AND json_extract(values_json,'$.cost_input_id')=? ORDER BY rowid DESC LIMIT 1",(KIND,row['id'])).fetchone()
    value['decision_id']=decision['id'] if decision else None
    value['revoked']=bool(decision and ws.conn.execute("SELECT 1 FROM automation_decision WHERE action='revoke' AND ref_id=?",(decision['id'],)).fetchone())
    return value

def costs(values):
    from .manual_cost import _cents
    if not isinstance(values,dict) or set(values)!=KEYS:raise WorkspaceError('必须分别批准商品、代发和补发成本，缺项不能补零')
    return {key:format(_cents(values[key])+0,'.2f') for key in sorted(KEYS)}

def raw_binding(ws,run):
    proof=ws.conn.execute('SELECT * FROM automation_run_proof WHERE run_id=?',(run['id'],)).fetchone()
    if not proof:raise WorkspaceError('本次结果缺少封存输入身份，请先重算')
    manifest=json.loads(proof['manifest_json'])
    value=manifest.get('period_cost_business_binding_sha')
    if not value:raise WorkspaceError('旧结果尚无独立成本业务输入身份，请先重算')
    return value

BINDING_COLUMNS=frozenset({'metric_id','source_id','store','period','order_id','sub_order_id','base_order_id',
    'internal_order_id','internal_sub_order_id','online_order_no','product_id','sku','sku_id','merchant_sku',
    'quantity','quantities','quantity_unit','unit_cost','unit_price','cost_amount','amount','buyer_paid','line_amount',
    'order_date','order_time','pay_time','ship_time','order_status_raw','order_state','order_type','tracking_no',
    'refund_amount','refund_status','after_sale_status','is_gift','is_suspect','is_reshipment','is_cancelled',
    'link_key','coverage_key','product_ids','order_count','product_count','issue_type',
    'source_pricing_day','cost_status','cost_source','cost_as_of','raw_cost_amount','raw_unit_cost',
    'cost_company_id','company_id','parent_order_id','platform_order_ids','member_order_ids',
    'platform_order_identity_complete','partial_cost','pending_cost_skus','order_flag','order_type',
    'remark','seller_remark','platform_seller_evidence_json'})
TECHNICAL_FIELDS={'path','sha','sha256','quote_sha','captured_at','fetched_at','recorded_at','at','actor',
    'rule_sha','grant_id','decision_id','source_task_id','attempt_id','run_id','source_run_id','source_code_sha',
    'source_refs','source_inputs','source_refresh_binding','frozen_origin_proof_sha','context_sha','code_sha','code_version'}


def _business(value):
    if isinstance(value,dict):return {k:_business(v) for k,v in value.items() if k not in TECHNICAL_FIELDS}
    if isinstance(value,list):return [_business(v) for v in value]
    return value


def business_binding(model,store,result,sl,ingestion):
    """Business source data and cost policy only; no code/report/person identity.

    Normalized target rows omit file hashes/row offsets, so replacing an archive
    with equivalent rows or changing another shop does not reapprove amounts.
    Provenance and complete raw artifacts remain independently checked by proof.
    """
    import polars as pl
    from .manual_cost import COST_METRICS
    active=[m for raw in model.metrics if (m:=raw.for_platform(store.platform)) is not None and m.id in COST_METRICS]
    sources={m.source for m in active}
    weight_roles={m.allocate.by for m in active if m.allocate and m.allocate.by}
    weight_roles.update(value for m in active if m.link for value in (m.link.key,m.link.to) if value)
    def dependencies(value,key=''):
        if isinstance(value,dict):return {r for k,v in value.items() for r in dependencies(v,k)}
        if isinstance(value,list):return {r for v in value for r in dependencies(v,key)}
        return {value} if isinstance(value,str) and key in {'of','field','from_role','key','to','by','role','source_key','target_key'} else set()
    for metric in active:
        for key in ('value','where','expect','exclude_when'):
            weight_roles.update(dependencies(metric.model_dump(mode='json').get(key)))
    business_columns=BINDING_COLUMNS|{role for value in weight_roles for role in (value,value.rsplit('.',1)[-1])}
    def policy_value(value):
        if isinstance(value,dict):return {k:policy_value(v) for k,v in value.items() if k not in {'name','note','label','description'}}
        if isinstance(value,list):return [policy_value(v) for v in value]
        return value
    metric_fields={'id','source','value','where','where_any','exclude','link','allocate','sign','time_basis',
        'posting_basis','orderless_time_basis','keep_zero_rows','brushing_scope','expect','exclude_when','major','naturally_unlinked','platform'}
    def business_cell(key,value):
        if key.endswith('_json') and isinstance(value,str):
            try:value=json.loads(value)
            except (ValueError,TypeError):return value
            from .source_recovery import proof_semantic
            value=proof_semantic(value)
        return _business(value)
    def rows(frame):
        if frame is None or frame.is_empty():return []
        values=[{k:business_cell(k,row.get(k)) for k in sorted(business_columns)} for row in frame.to_dicts()]
        for row in values:
            if row.get('store') in {store.id,store.name,*store.aliases}:row['store']=store.id
        return sorted(values,key=journal.encoded)
    native=result.spine
    if 'store' in native.columns:native=native.filter(pl.col('store').is_in([store.id,store.name]))
    if 'period' in native.columns:native=native.filter(pl.col('period').is_null()|pl.col('period').is_in([sl.period,'(未知账期)','(无账期)']))
    fact=sl.facts.filter(pl.col('metric_id').is_in(sorted(COST_METRICS))) if 'metric_id' in sl.facts.columns else sl.facts.head(0)
    policy={'platform':{'id':store.platform,'cost_pricing':model.platform(store.platform).cost_pricing},
        'store':{'id':store.id,'cost_return_posting':store.cost_return_posting},
        'metrics':[policy_value({k:v for k,v in m.model_dump(mode='json').items() if k in metric_fields}) for m in active]}
    native_sources={src.id for src in model.sources if src.is_spine and src.platform in ('any',store.platform)}
    used_templates=set(fact['template_id'].drop_nulls().to_list()) if 'template_id' in fact.columns else set()
    used_templates.update(item.template.id for item in ingestion.known if item.template and item.recognition.source_id in native_sources)
    template_fields={'source','bindings','sign','direction_role','direction_outflow_values','direction_inflow_values',
        'dedup','time_slots','time_fallbacks','key_rules','identifier_extractions','classify_rules','reclassify','total_row_marker'}
    templates={t.id:t for t in model.templates if t.id in used_templates}
    templates.update({item.template.id:item.template for item in ingestion.known if item.template and item.template.id in used_templates})
    template_rules=[]
    for template in templates.values():
        rule={k:v for k,v in template.model_dump(mode='json').items() if k in template_fields}
        rule['bindings']=[binding for binding in rule.get('bindings',[]) if binding['role'] in business_columns]
        template_rules.append(policy_value(rule))
    policy['templates']=sorted(template_rules,key=journal.encoded)
    policy['sources']=[{k:v for k,v in src.model_dump(mode='json').items() if k in {'id','derived','provides','is_spine','company_wide','dedupe_key','shared_upload'}}
        for src in model.sources if src.id in sources|native_sources]
    adopted=[]
    for key in ('historical_cost_evidence','physical_cost_evidence'):
        for item in getattr(ingestion,key,[]) or []:
            if item.get('store_id')==store.id and (item.get('period')==sl.period or sl.period in item.get('dependent_periods',[])):
                if key=='physical_cost_evidence':
                    from .automation_source_cost_v3 import quote_semantic
                    from .source_recovery import proof_semantic
                    adopted.append({'target':_business(item.get('target',{})),
                        'members':proof_semantic(item.get('members',[])),
                        'quotes':sorted([quote_semantic(q) for q in item.get('quotes',[])],key=journal.encoded)})
                else:
                    adopted.append(_business({k:item[k] for k in ('store_id','period','coverage_key','internal_order_id',
                        'internal_sub_order_id','sku','company_id','pricing_day','quantity','unit_cost','exact_amount') if k in item}))
    body={'schema':'period_cost_business_input.v1','scope':{'store_id':store.id,'period':sl.period,'cost_keys':sorted(KEYS)},
        'policy':policy,'native':rows(native),'cost_facts':rows(fact),'pricing':rows(sl.pricing_gaps),
        'coverage':rows(sl.coverage_gap_rows),'issues':rows(sl.order_issue_rows),
        'source_arrived':sorted(sources & set(sl.completeness.arrived)),
        'source_missing':sorted(sources & set(sl.completeness.missing)),
        'adopted_source_business':sorted(adopted,key=journal.encoded)}
    return journal.digest(body)


def _mutable(ws,model,store_id,period):
    from .publication_scope import resolve
    scope=resolve(ws.root,model)
    if scope is not None:scope.require(store_id,period,workspace_root=ws.root)
    state=ws.state(store_id,period)
    if state and state.closed:raise WorkspaceError('已结账成本保持冻结，不能批准新的整期成本输入')
    from .commission_registry import Registry
    with Registry(ws.root).connect() as db:
        if (db.execute('SELECT 1 FROM payout_confirmation WHERE store_id=? AND period=? LIMIT 1',(store_id,period)).fetchone()
            or any(not json.loads(r['selection_json']).get('store_ids') or store_id in json.loads(r['selection_json'])['store_ids'] for r in db.execute('SELECT selection_json FROM settlement WHERE start_period<=? AND end_period>=?',(period,period)))):
            raise WorkspaceError('本期已有核定实发或结算，不能改变成本输入')

def _current(ws,model,store_id,period,run_id):
    from . import automation_checker as checker
    from .automation_period_proof import valid_artifact
    from .version import assert_code_integrity
    assert_code_integrity();_mutable(ws,model,store_id,period)
    run=ws.latest_run(store_id,period)
    if not run or type(run_id) is not int or run['id']!=run_id:raise WorkspaceError('核算结果已更新，请重新预览成本输入')
    if run['model_revision']!=hashlib.sha256(model.model_dump_json().encode()).hexdigest():raise WorkspaceError('核算模型已变化，请先重算')
    checked=checker.check(ws,store_id,period,model=model,check_company=False,check_pending=False,verify_artifacts=True)
    forbidden={'stale_run','evidence_archive','input_capture_unverified','unverified_code_change','unverified_model_change',
      'unverified_input_change','manual_input_unverified','source_sync_pending','original_date_input_unverified',
      'source_fact_input_unverified','code_source_drift','period_cost_input_unverified','period_cost_input_source_unverified'}
    if forbidden & {i['code'] for i in checked['issues']}:raise WorkspaceError('批准成本前的实际输入尚未复核，不能使用过期结果')
    proof=ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(run_id,)).fetchone()
    if not proof or not run['evidence_ready'] or run['evidence_error']:raise WorkspaceError('核算事实证据尚未完成封存')
    if any(not valid_artifact(ws,a) for a in json.loads(proof[0]).get('artifacts',[])):raise WorkspaceError('封存原件已变化，不能批准成本输入')
    from .cost_lines import revision as line_revision
    if line_revision(ws,store_id,period):raise WorkspaceError('已有逐笔补录记录，禁止再叠加整期成本；请先核对原批准基数')
    return run

def normalize(ws,model,store_id,period,values):
    if not isinstance(values,dict) or set(values)!={'run_id','costs'}:raise WorkspaceError('整期成本输入只允许运行号和完整三成本，不包含提成或月结')
    run=_current(ws,model,store_id,period,values['run_id'])
    return {'run_id':run['id'],'costs':costs(values['costs']),'source_binding_sha':raw_binding(ws,run),
        'source_result_sha256':hashlib.sha256(run['result'].encode()).hexdigest()}

def preview_effect(ws,model,store_id,period,values):
    from .manual_cost import preview
    run=ws.latest_run(store_id,period);body=json.loads(run['result'])
    trial=preview(model,body,values['costs'])
    before={n['id']:n for n in body.get('statement',[])}
    after={n['id']:n for n in trial['statement']}
    metrics={}
    for key in ('n_goods','n_dropship','n_reshipment','gross_profit','net_profit'):
        old,new=before.get(key,{}),after.get(key,{})
        known=old.get('available') is True and old.get('value') is not None
        metrics[key]={'before':old.get('value') if known else None,'after':new.get('value') if new.get('available') else None,
            'delta':round(float(new['value'])-float(old['value']),6) if known and new.get('available') and new.get('value') is not None else None}
    return {'stage':'preview_not_final','cost_scope':'store_period_total','metrics':metrics,
        'other_missing':trial['other_missing'],'payouts_confirmed':False,'period_closed':False,
        'order_allocation_available':False}


def revoke_input(ws,model,store_id,period,values):
    _mutable(ws,model,store_id,period)
    row=latest(ws,store_id,period)
    if not row or row['id']!=values.get('cost_input_id') or row['revoked']:raise WorkspaceError('该成本批准已被替换或撤销')
    return journal.digest({'store_id':store_id,'period':period,'cost_input_id':row['id'],'revision':revision(ws,store_id,period)})


def approval_input(ws,model,store_id,period,values):
    _current(ws,model,store_id,period,values['run_id'])
    from .automation_checker import check
    return check(ws,store_id,period,model=model,verify_artifacts=True)['input_sha']


def _archive(ws,body):
    raw=body if isinstance(body,bytes) else journal.encoded(body).encode()
    sha=hashlib.sha256(raw).hexdigest();path=ws.root/'period-cost-input-evidence'/(sha+'.json')
    root=ws.root.resolve()
    if path.parent.is_symlink() or not path.parent.resolve().is_relative_to(root):raise WorkspaceError('批准凭据目录必须位于本工作区，不能经过符号链接')
    path.parent.mkdir(parents=True,exist_ok=True)
    if not path.parent.resolve().is_relative_to(root) or path.parent.is_symlink():raise WorkspaceError('批准凭据目录已变化')
    if path.is_symlink():raise WorkspaceError('批准凭据文件不能是符号链接')
    if path.exists():
        if not path.is_file() or path.is_symlink() or path.read_bytes()!=raw:raise WorkspaceError('批准凭据归档身份冲突')
    else:
        fd,tmp=tempfile.mkstemp(prefix='.approval-',dir=path.parent)
        try:
            with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
            os.replace(tmp,path)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
    if path.parent.is_symlink() or path.is_symlink() or not path.resolve().is_relative_to(root):raise WorkspaceError('批准凭据目录在归档期间发生变化')
    return {'path':path.relative_to(ws.root).as_posix(),'sha':sha,'sealed':False}

def _replay_context(ws,store_id,period):
    from .publication_scope import resolve
    root=ws.root.resolve()
    try:scope=resolve(root)
    except (ValueError,RuntimeError) as exc:raise WorkspaceError('隔离回放范围已失效，不能读取其成本批准') from exc
    if (root.parent!=Path(tempfile.gettempdir()).resolve() or not root.name.startswith('ledger-service-replay-')
        or scope is None or not scope.isolated):
        raise WorkspaceError('原档投影成本只允许在原隔离回放范围读取，不能作为生产批准')
    scope.require(store_id,period,workspace_root=root)
    return {'workspace_root_sha256':journal.digest(str(root)),'publication_scope_sha256':scope.identity}


def _capture_unchanged(reference,root):
    path=Path(reference['path']);root=Path(root).resolve()
    if (path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root)
        or path.stat().st_size>32*1024*1024):raise WorkspaceError('原档捕获路径或范围在提交期间变化')
    for parent in path.parents:
        if parent==root:break
        if parent.is_symlink():raise WorkspaceError('原档捕获路径不能经过符号链接')
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    if h.hexdigest()!=reference['sha256']:raise WorkspaceError('原档捕获在提交期间变化，本次未批准')


def _write(ws,model,store_id,period,values,*,key,signature,actor,reason,input_sha,proposal_sha,documentary=None,capture_bytes=None,capture_root=None):
    # Caller owns financial guard + Workspace transaction. No Registry business
    # write happens here; durable dispatch is committed beside the input/receipt.
    from .automation_actions import _case
    conn=ws.conn;run=_current(ws,model,store_id,period,values['run_id'])
    if raw_binding(ws,run)!=values['source_binding_sha'] or hashlib.sha256(run['result'].encode()).hexdigest()!=values['source_result_sha256']:
        raise WorkspaceError('原始批准依据与本次运行不一致')
    normalized=costs(values['costs'])
    aid=journal.ident();identity_kind='documentary_history_replay' if documentary else 'human'
    if documentary:
        scope={'store_id':store_id,'period':period,'historical_authenticated_actor_proved':False,'production_authority':False}
        conn.execute('INSERT INTO automation_authorization VALUES(?,?,?,?,?,?,?,?,?)',
            (aid,key,actor['id'],identity_kind,'period_cost_input.apply',input_sha,proposal_sha,journal.encoded(scope),journal.now()))
    else:aid=journal.authorize(conn,key,actor,'period_cost_input.apply',input_sha,proposal_sha,{'store_id':store_id,'period':period})
    from .publication_scope import resolve
    publication=resolve(ws.root,model)
    evidence={'kind':'documentary_history_replay' if documentary else 'authenticated_human_confirmation',
        'historical_authenticated_actor_proved':False if documentary else None,'production_authority':False if documentary or (publication is not None and publication.isolated) else True}
    if documentary:
        reference=documentary['reference']
        if not isinstance(capture_bytes,bytes) or hashlib.sha256(capture_bytes).hexdigest()!=reference['sha256']:
            raise WorkspaceError('原档归档必须使用同一份已校验捕获字节')
        _capture_unchanged(reference,capture_root)
        evidence.update(documentary)
        evidence['replay_context']=_replay_context(ws,store_id,period)
        evidence['artifacts']=[_archive(ws,capture_bytes)]
        if evidence['artifacts'][0]['sha']!=reference['sha256']:raise WorkspaceError('原档归档SHA与原批准不一致')
        _capture_unchanged(reference,capture_root)
    else:
        if publication is not None and publication.isolated:evidence['replay_context']=_replay_context(ws,store_id,period)
        evidence['artifacts']=[_archive(ws,{'schema':1,'store_id':store_id,'period':period,'costs':normalized,
            'run_id':run['id'],'source_binding_sha':values['source_binding_sha'],'source_result_sha256':values['source_result_sha256'],
            'actor':actor['id'],'reason':reason,'proposal_sha':proposal_sha})]
    did=journal.ident()
    conn.execute('INSERT INTO period_cost_input VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (did,store_id,period,run['id'],values['source_binding_sha'],journal.encoded(normalized),journal.encoded(evidence),aid,actor['id'],identity_kind,key,reason,journal.now()))
    decision_id=journal.ident()
    conn.execute('INSERT INTO automation_decision VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (decision_id,key,KIND,period,store_id,'apply','',journal.encoded({'cost_input_id':did,'costs':normalized}),reason,input_sha,proposal_sha,actor['id'],journal.now()))
    from .automation_checker import check
    checked=check(ws,store_id,period,model=model)
    case_id=journal.digest([store_id,period])[:32]
    _case(conn,{'case_id':case_id,'store_id':store_id,'period':period,'status':'processing','stage':'queued',
               'input_sha':checked['input_sha'],'run_id':run['id'],'issues':checked['issues']})
    conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
        (journal.ident(),case_id,key,checked['input_sha'],'dispatch_pending',journal.encoded({'store_id':store_id,'fingerprint':'automation:'+journal.digest(['period-cost-input',did])}),journal.now()))
    if documentary:_capture_unchanged(documentary['reference'],capture_root)
    return journal.finish(conn,key,'period_cost_input.apply',signature,aid,
        {'status':'applied','decision_id':decision_id,'cost_input_id':did,'store_id':store_id,'period':period,'payouts_confirmed':False,'period_closed':False,
         'revoke_input_sha':revoke_input(ws,model,store_id,period,{'cost_input_id':did})})

def apply_proposal(ws,model,*,request_id,proposal_id,proposal_sha,input_sha,actor):
    from .automation_actions import human,_request,_load_proposal,deliver
    from .finance_guard import guard
    human(actor);_request(request_id)
    row=ws.conn.execute('SELECT store_id,period FROM automation_proposal WHERE id=?',(proposal_id,)).fetchone()
    if not row:raise WorkspaceError('成本预览不存在')
    key='decision-apply:'+request_id;signature=journal.digest([key,proposal_id,proposal_sha,input_sha,actor['id']])
    with guard(ws.root,row['store_id'],row['period']),ws.transaction() as conn:
        old=journal.receipt(conn,key,signature)
        if old:return old
        proposal,payload=_load_proposal(conn,proposal_id,proposal_sha,input_sha,actor,'decision')
        if payload['kind']!=KIND:raise WorkspaceError('成本输入预览类型不匹配')
        sid,period=proposal['store_id'],proposal['period'];value=payload['values']
        if approval_input(ws,model,sid,period,value)!=input_sha:raise WorkspaceError('预览后输入已变化，成本输入未应用')
        result=_write(ws,model,sid,period,value,key=key,signature=signature,actor=actor,reason=payload['reason'],input_sha=input_sha,proposal_sha=proposal_sha)
    deliver(ws)
    return result

def restore_documentary(ws,model,store_id,period,*,reference,decision,bundle,approval_reference,fixture=False):
    from .finance_guard import guard
    from .publication_scope import resolve
    from .automation_actions import deliver
    root=ws.root.resolve();temporary=Path(tempfile.gettempdir()).resolve();scope=resolve(root,model)
    if (root.parent!=temporary or not root.name.startswith('ledger-service-replay-')
        or scope is None or not scope.isolated or not Path(reference['path']).resolve().is_relative_to(Path(bundle).resolve())):
        raise PermissionError('原档成本恢复仅允许明确范围的隔离回放，不能写入生产')
    scope.require(store_id,period,workspace_root=root)
    if not isinstance(approval_reference,str) or not approval_reference.strip():raise PermissionError('隔离回放缺少明确依据')
    key='documentary-cost:'+journal.digest([store_id,period,reference,approval_reference]);signature=journal.digest([key,reference,approval_reference])
    with guard(root,store_id,period),ws.transaction() as conn:
        old=journal.receipt(conn,key,signature)
        if old:return old
        # Revalidate the complete raw capture at the write boundary too.
        from .replay_service import load_manual_finance_decision
        fresh,document,capture=load_manual_finance_decision(reference,store_id,period,fixture=fixture,return_raw=True)
        if costs(fresh['confirmed'])!=costs(decision['confirmed']):raise WorkspaceError('原档成本已变化')
        run=ws.latest_run(store_id,period)
        values=normalize(ws,model,store_id,period,{'run_id':run['id'] if run else None,'costs':fresh['confirmed']})
        result=_write(ws,model,store_id,period,values,key=key,signature=signature,actor={'id':'documentary:'+reference['decision_sha256']},
            reason=fresh['reason'],input_sha=approval_input(ws,model,store_id,period,values),proposal_sha=reference['decision_sha256'],
            documentary={'reference':reference,'document':document,'replay_approval_reference':approval_reference,'source_kind':'fixture' if fixture else 'real_history'},capture_bytes=capture,capture_root=bundle)
    deliver(ws)
    return result

def validate_evidence(ws,row):
    evidence=json.loads(row['evidence_json'])
    auth=ws.conn.execute('SELECT * FROM automation_authorization WHERE id=?',(row['authorization_id'],)).fetchone()
    if not auth or auth['identity_kind']!=row['identity_kind'] or auth['action']!='period_cost_input.apply' or auth['actor']!=row['actor']:
        raise WorkspaceError('整期成本批准身份记录失效')
    expected_kind={'human':'authenticated_human_confirmation','documentary_history_replay':'documentary_history_replay'}.get(row['identity_kind'])
    if expected_kind is None or evidence.get('kind')!=expected_kind:raise WorkspaceError('整期成本批准身份类型不匹配')
    if row['identity_kind']=='documentary_history_replay' and (evidence.get('production_authority') is not False or evidence.get('historical_authenticated_actor_proved') is not False):
        raise WorkspaceError('原档投影不能冒充生产或原始人工认证')
    if evidence.get('production_authority') is not True:
        if evidence.get('replay_context')!=_replay_context(ws,row['store_id'],row['period']):raise WorkspaceError('隔离成本批准不能跨工作区或隔离范围使用')
    from .automation_period_proof import valid_artifact
    if not evidence.get('artifacts') or any(not valid_artifact(ws,a) for a in evidence['artifacts']):raise WorkspaceError('批准成本的原始凭据缺失或已变化')
    return evidence

def verify_period_records(ws,store_id,period):
    if not ws.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='period_cost_input'").fetchone():return
    for row in ws.conn.execute('SELECT * FROM period_cost_input WHERE store_id=? AND period=?',(store_id,period)):
        validate_evidence(ws,dict(row))


def verify_applied(ws,store_id,period,metadata):
    row=latest(ws,store_id,period)
    if not row or row['id']!=metadata.get('id') or row['revoked'] or row['source_binding_sha']!=metadata.get('approved_source_binding_sha' if metadata.get('status')=='superseded_by_complete_sources' else 'business_input_sha'):
        raise WorkspaceError('整期成本批准已变化或撤回')
    validate_evidence(ws,row)


def complete_source_control_match(model,store,payload,approved,sl=None,ingestion=None):
    """Only original complete costs can retire an obsolete aggregate override."""
    from decimal import Decimal,ROUND_HALF_UP
    from .manual_cost import COST_METRICS
    if sl is None or ingestion is None or payload.get('period_cost_input') or payload.get('manual_cost'):return False
    review=payload.get('cost_review') or {}
    if review.get('authority')!='source_evidence' or review.get('amount_complete')!={k:True for k in KEYS}:return False
    if payload.get('source_sync_pending') is not False or payload.get('pricing_pending_count')!=0:return False
    coverage=payload.get('cost_coverage') or {}
    if coverage.get('uncovered') or (coverage.get('expected') and coverage.get('passed') is not True):return False
    if not sl.pricing_gaps.is_empty() or not sl.coverage_gap_rows.is_empty() or not sl.order_issue_rows.is_empty():return False
    cost_sources={m.source for raw in model.metrics if (m:=raw.for_platform(store.platform)) is not None and m.id in COST_METRICS}
    relevant=cost_sources|{src.id for src in model.sources if src.is_spine and src.platform in ('any',store.platform)}
    for item in ingestion.items:
        if not item.error:continue
        source=item.recognition.source_id
        if source in relevant:return False
        if not source:
            hints={src.id for src in model.sources if any(hint and hint in item.ref.filename for hint in src.filename_hints)}
            if not hints or hints&relevant:return False
    bad={f.check_id for f in sl.audit.findings if f.blocking and not f.passed}
    required={'required_cost_unresolved','historical_cost_evidence','dropship_scope_evidence','dropship_cost_evidence','cost_return_evidence','chk_goods_coverage'}
    required.update(c.id for c in model.checks if c.kind=='spine_coverage' and c.metric in COST_METRICS)
    if bad&required:return False
    inputs=payload.get('calculation_inputs') or {};unavailable=set(inputs.get('unavailable_metrics',[]))
    if unavailable&COST_METRICS:return False
    active={m.id for raw in model.metrics if (m:=raw.for_platform(store.platform)) is not None}
    totals=inputs.get('metric_totals') or {};inapplicable=set(inputs.get('inapplicable_metrics',[]))
    def amount(key):
        if key not in active or key in inapplicable:return Decimal(0)
        if key not in totals or isinstance(totals[key],bool):raise ValueError()
        result=Decimal(str(totals[key]))
        if not result.is_finite():raise ValueError()
        return result
    try:
        actual={'goods':-amount('goods_cost')-amount('goods_return_cost'),'dropship':-amount('dropship_cost'),'reshipment':-amount('reshipment_cost')}
        from .money import money_float,decimal_amount
        if all(decimal_amount(money_float(v))==Decimal(approved[k]) for k,v in actual.items()):
            # Formal metric_totals already use Decimal accumulation and cent
            # rounding. Preserve the original admitted contributions separately;
            # do not replace source micro amounts with the control's display.
            raw={m:Decimal(0) for m in COST_METRICS}
            if {'metric_id','counted','contribution'}<=set(sl.facts.columns):
                for metric,counted,contribution in sl.facts.select('metric_id','counted','contribution').iter_rows():
                    if metric in raw and counted and contribution is not None:raw[metric]+=decimal_amount(contribution)
            source={'goods':-raw['goods_cost']-raw['goods_return_cost'],'dropship':-raw['dropship_cost'],'reshipment':-raw['reshipment_cost']}
            return {'confirmation_precision':'cent_round_half_up','booked_totals':{k:str(v) for k,v in actual.items()},
                    'raw_source_totals':{k:str(v) for k,v in source.items()},
                    'control_totals':dict(approved),'source_amounts_retained':True}
        return False
    except (ValueError,ArithmeticError):return False


def apply_payload(ws,model,store_id,period,payload,business_input_sha,sl=None,ingestion=None):
    row=latest(ws,store_id,period)
    if not row:return None
    validate_evidence(ws,row)
    if row['revoked']:
        payload['period_cost_input']={'id':row['id'],'decision_id':row['decision_id'],'status':'revoked','business_input_sha':business_input_sha}
        return row
    approved=costs(json.loads(row['costs_json']))
    source_match=complete_source_control_match(model,model.store(store_id),payload,approved,sl,ingestion)
    if source_match:
        _mutable(ws,model,store_id,period);evidence=validate_evidence(ws,row)
        payload['period_cost_input']={'id':row['id'],'decision_id':row['decision_id'],
            'status':'superseded_by_complete_sources','business_input_sha':business_input_sha,
            'approved_source_binding_sha':row['source_binding_sha'],'old_override_applied':False,
            'source_control_match':source_match,'artifacts':evidence['artifacts'],'new_approval_created':False,
            'payouts_confirmed':False,'period_closed':False,
            'revoke_input_sha':revoke_input(ws,model,store_id,period,{'cost_input_id':row['id']})}
        return row
    if row['source_binding_sha']!=business_input_sha:
        payload['period_cost_input']={'id':row['id'],'decision_id':row['decision_id'],'status':'stale','business_input_sha':business_input_sha,'approved_source_binding_sha':row['source_binding_sha']}
        return row
    _mutable(ws,model,store_id,period)
    evidence=validate_evidence(ws,row)
    from .manual_cost import preview
    approved=costs(json.loads(row['costs_json']));trial=preview(model,payload,approved)
    payload['statement']=trial['statement']
    for node in payload['statement']:
        if node.get('id') in {'n_goods','n_dropship','n_reshipment','g_goods','gross_profit','net_profit'}:
            node.update(drillable=False,amount_scope='approved_store_period_total',order_allocation_available=False)
    payload['period_cost_input']={'id':row['id'],'decision_id':row['decision_id'],'revoke_input_sha':revoke_input(ws,model,store_id,period,{'cost_input_id':row['id']}),'status':'applied','business_input_sha':business_input_sha,
        'costs':approved,'source_run_id':row['source_run_id'],'identity_kind':row['identity_kind'],
        'historical_authenticated_actor_proved':False if row['identity_kind']=='documentary_history_replay' else None,
        'artifacts':evidence['artifacts'],'payouts_confirmed':False,'period_closed':False}
    inputs=deepcopy(payload['calculation_inputs']);inputs['metric_totals'].update(goods_cost=-float(approved['goods']),goods_return_cost=0.,dropship_cost=-float(approved['dropship']),reshipment_cost=-float(approved['reshipment']))
    from .manual_cost import COST_METRICS
    inputs['unavailable_metrics']=sorted(set(inputs['unavailable_metrics'])-COST_METRICS);payload['calculation_inputs']=inputs
    payload['cost_review']={**(payload.get('cost_review') or {}),'amount_complete':{k:True for k in KEYS},
        'authority':'approved_period_cost_input','confirmed':trial['confirmed'],'requires_human':False,
        'individual_order_costs_proved':False,'input_id':row['id']}
    resolved={'required_cost_unresolved','historical_cost_evidence','dropship_cost_evidence','chk_goods_coverage'}
    resolved.update(c.id for c in model.checks if c.kind=='spine_coverage' and c.metric=='goods_cost')
    for finding in payload.get('findings',[]):
        if finding.get('id') in resolved and not finding.get('passed'):
            payload.setdefault('raw_cost_findings',[]).append(deepcopy(finding))
            finding.update(blocking=False,resolution='approved_period_total',financial_scope='store_period_only',raw_evidence_complete=False)
    from . import view
    payload['missing_sources']=[x for x in payload.get('missing_sources',[]) if x!=view.source_name(model,'order_cost')]
    commission=payload.get('commission') or {};commission['amount_complete']=False
    commission.setdefault('notes',[]).append('整期成本已批准；金额未按订单或人员推分，个人提成仍须按独立证据核定')
    basis=next((n for n in payload['statement'] if n['id']==commission.get('base_node')),None)
    commission['period_cost_input_scope']='store_period_only'
    if basis and basis.get('value') is not None and commission.get('base_total') is not None:
        commission['base_reconciliation_delta']=round(float(commission['base_total'])-float(basis['value']),2)
    for person in commission.get('people',[]):
        person.update(amount_complete=False,gross=None,period_cost_input_scope='not_allocated_to_person')
    payload['can_close']=bool(trial['profit'] is not None and not payload.get('missing_sources') and not payload.get('source_sync_pending')
        and not any(f.get('blocking') and not f.get('passed') for f in payload.get('findings',[])))
    return row

def conflict_details(ws,model,store_id,period):
    from decimal import Decimal
    row=latest(ws,store_id,period);run=ws.latest_run(store_id,period)
    if not row or not run:return None
    body=json.loads(run['result']);review=body.get('cost_review') or {};inputs=body.get('calculation_inputs') or {}
    totals=inputs.get('metric_totals') or {};inactive=set(inputs.get('inapplicable_metrics',[]))
    active={m.id for raw in model.metrics if (m:=raw.for_platform(model.store(store_id).platform)) is not None}
    def number(key):
        if key not in active or key in inactive:return Decimal(0)
        if key not in totals or isinstance(totals[key],bool):raise ValueError()
        value=Decimal(str(totals[key]))
        if not value.is_finite():raise ValueError()
        return value
    current={}
    for key,metrics in {'goods':('goods_cost','goods_return_cost'),'dropship':('dropship_cost',),'reshipment':('reshipment_cost',)}.items():
        try:current[key]=format(-sum((number(m) for m in metrics),Decimal(0)),'.2f') if (review.get('amount_complete') or {}).get(key) is True else None
        except (ValueError,ArithmeticError):current[key]=None
    complete=all(v is not None for v in current.values())
    try:token=revoke_input(ws,model,store_id,period,{'cost_input_id':row['id']});can_revoke=True
    except (WorkspaceError,PermissionError,ValueError):token=None;can_revoke=False
    return {'cost_input_id':row['id'],'decision_id':row['decision_id'],'approved_costs':json.loads(row['costs_json']),
        'current_source_costs':current,'current_cost_amounts_complete':complete,
        'approved_business_input_sha':row['source_binding_sha'],
        'current_business_input_sha':(body.get('period_cost_input') or {}).get('business_input_sha'),
        'revoke_input_sha':token,'can_revoke':can_revoke,'requires_amount_reentry':False,
        'recommended_action':'revoke_and_use_complete_sources' if complete and can_revoke else 'continue_source_evidence',
        'summary':'完整新来源与旧批准控制不符，可撤回旧批准采用新来源；无需重录三个金额' if complete else '新来源仍有成本缺口，系统继续补证；无需按未知部分重录金额'}


def assert_captured(ws,store_id,period,expected):
    if revision(ws,store_id,period)!=int(expected or 0):raise WorkspaceError('整期成本批准在核算期间变化，本次结果未发布')
