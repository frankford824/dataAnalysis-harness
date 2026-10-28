"""Post-calculation, period-scoped reuse with immutable equivalence receipts.

The full store is still calculated. Only after that calculation can unrelated
month changes be proven irrelevant. This module never filters raw input by date.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import automation_store as journal
from .storage_integrity import digest as file_digest, verified,registered_archive_link
from .version import engine_version,reproducible

_CLOCK_COLUMNS={'file_sha','__sha__','spine_row','registry_revision','rule_key','rule_version','calculation_id'}


def _logical(value):
    if isinstance(value,dict):return {k:_logical(v) for k,v in value.items() if k not in _CLOCK_COLUMNS}
    if isinstance(value,list):return [_logical(v) for v in value]
    if isinstance(value,str):return re.sub(r'order-feed:[^:;\s]+:\d+','order-feed:captured',value)
    return value


def _frame(frame):
    if frame is None:return None
    columns=sorted(c for c in frame.columns if c not in _CLOCK_COLUMNS)
    rows=[_logical(row) for row in frame.select(columns).to_dicts()]
    return {'schema':[(c,str(frame.schema[c])) for c in columns],
            'rows':sorted(rows,key=journal.encoded)}


COLLECTABLE_COLUMNS=frozenset({'metric_id','coverage_key','context_sha','order_id','sub_order_id',
    'internal_order_id','internal_sub_order_id','sku','cost_company_id','source_pricing_day','order_date',
    'quantity','quantities','product_ids','order_count','product_count','goods_scope_version','partial_cost',
    'pending_cost_skus','reason','category','issue_type','issue_code','required_evidence','status'})


def collectable_scope(pricing,coverage,issues,payload):
    """Nonfinancial source demand, independent of totals and personnel."""
    approved={r['coverage_key'] for r in (payload.get('cost_line_posting') or {}).get('reviews',[]) if r.get('coverage_key')}
    values={};counts={}
    for name,frame in (('pricing',pricing),('coverage',coverage),('issues',issues)):
        if frame is None:values[name]=None;counts[name]=None;continue
        if name in {'pricing','coverage'} and approved and 'coverage_key' in frame.columns:
            import polars as pl
            frame=frame.filter(~pl.col('coverage_key').is_in(approved))
        counts[name]=frame.height
        values[name]=_frame(frame.select(sorted(set(frame.columns)&COLLECTABLE_COLUMNS)))
    cost_required=(bool((counts['pricing'] or 0)+(counts['coverage'] or 0))
        if counts['pricing'] is not None and counts['coverage'] is not None else None)
    order_required=(bool((counts['pricing'] or 0)+(counts['coverage'] or 0)+(counts['issues'] or 0))
        if all(value is not None for value in counts.values()) else None)
    cost={'sha':journal.digest({k:values[k] for k in ('pricing','coverage')}),'required':cost_required}
    order={'sha':journal.digest(values),'required':order_required}
    return {'history_cost':cost,'order':order,'seller_member':order}


def source_manifest(ws,store_id):
    slots=[tuple(r) for r in ws.conn.execute('SELECT store_id,name,sha FROM slot WHERE store_id IN (?,?) ORDER BY store_id,name',(store_id,'__shared__'))]
    version=ws.conn.execute('SELECT coalesce(max(id),0) FROM version WHERE store_id IN (?,?)',(store_id,'__shared__')).fetchone()[0]
    manual={r[0]:r[1] for r in ws.conn.execute('SELECT period,max(id) FROM cost_line_log WHERE store_id=? GROUP BY period',(store_id,))}
    return {'slots':slots,'version':version,'manual_versions':manual}


def effective_model(model,store,template_ids=(),subjects=()):
    return {'store':store.model_dump(mode='json'),
        'platforms':[p.model_dump(mode='json') for p in model.platforms if p.id==store.platform],
        'metrics':[m.model_dump(mode='json') for raw in model.metrics if (m:=raw.for_platform(store.platform)) is not None],
        'statement':[n.model_dump(mode='json') for n in model.statement],
        'templates':[t.model_dump(mode='json') for t in model.templates if t.id in set(template_ids)],
        'sources':[s.model_dump(mode='json') for s in model.sources if s.platform in ('any',store.platform)],
        'dictionary':[d.model_dump(mode='json') for d in model.dictionary if d.platform==store.platform and d.raw in set(subjects)],
        'fee_rules':[r.model_dump(mode='json') for r in model.fee_rules if r.platform in ('*',store.platform)]}


def _cost_sources(ws,store_id,period,payload,ingestion=None,sl=None):
    revisions={r['revision'] for r in (payload.get('cost_line_posting') or {}).get('reviews',[])}
    values=[]
    for row in ws.conn.execute("SELECT id,values_json FROM automation_decision WHERE store_id=? AND period=? AND kind='history_cost_single_component.v1' AND action='apply' ORDER BY rowid",(store_id,period)):
        value=json.loads(row['values_json'])
        if value.get('line_log_id') not in revisions:continue
        refs=[]
        for ref in value.get('source_refs',[]):
            path=Path(ref['path'])
            relative=path.resolve().relative_to(ws.root.resolve()).as_posix()
            refs.append({'path':relative,'sha':ref['sha256'],'sealed':False})
        values.append({'decision_id':row['id'],'line_log_id':value['line_log_id'],
            'quote_sha':value['quote_sha'],'artifacts':refs})
    returns=set()
    frame=getattr(sl,'facts',None)
    if frame is not None and {'source_id','internal_order_id','sku'}<=set(frame.columns):
        returns={(str(order),str(sku)) for order,sku in frame.filter(frame['source_id']=='cost_return').select('internal_order_id','sku').unique().iter_rows()}
    for fact in getattr(ingestion,'historical_cost_evidence',[]) if ingestion is not None else []:
        if fact['store_id']!=store_id or (fact['period']!=period and (fact['internal_order_id'],fact['sku']) not in returns):continue
        refs=[{'path':Path(ref['path']).resolve().relative_to(ws.root.resolve()).as_posix(),
               'sha':ref['sha256'],'sealed':False} for ref in fact['source_refs']]
        values.append({'decision_id':fact['decision_id'],'source_fact':True,'coverage_key':fact['coverage_key'],
            'quote_sha':fact['quote_sha'],'origin_period':fact['period'],'dependent_period':period,'artifacts':refs})
    for fact in getattr(ingestion,'physical_cost_evidence',[]) if ingestion is not None else []:
        if fact['store_id']!=store_id or (fact['period']!=period and period not in fact.get('dependent_periods',[])):continue
        refs=[{'path':Path(ref['path']).resolve().relative_to(ws.root.resolve()).as_posix(),
               'sha':ref['sha256'],'sealed':False} for ref in fact['source_refs']]
        values.append({'decision_id':fact['decision_id'],'source_fact':True,'rule_id':fact['rule_id'],
            'coverage_key':fact['target']['coverage_key'],'quote_shas':[q['quote_sha'] for q in fact['quotes']],
            'source_refresh_binding':fact.get('source_refresh_binding'),'source_inputs':fact.get('source_inputs',[]),
            'frozen_origin_run_id':fact.get('frozen_origin_run_id'),'frozen_origin_proof_sha':fact.get('frozen_origin_proof_sha'),
            'close_quote_keys':fact.get('close_quote_keys'),
            'origin_period':fact['period'],'dependent_period':period,'artifacts':refs})
    return values


def valid_artifact(ws,artifact):
    path=ws.root/artifact['path']
    try:
        local=(path.resolve().is_relative_to(ws.root.resolve()) or registered_archive_link(ws.root,path,artifact['sha']))
        return (local and path.is_file()
            and (artifact.get('sealed',True) is False or verified(path)) and file_digest(path)==artifact['sha'])
    except (OSError,ValueError,KeyError):return False


def prepare(ws,model,store,ingestion,sl,payload,allocation,source_context=None):
    # Effective output and complete in-period evidence decide equality, not the
    # global feed seq, whole-file SHA, or unrelated registry audit revision.
    if not hasattr(sl.facts,'to_dicts'):return None
    used_templates=set(sl.facts['template_id'].drop_nulls().to_list()) if 'template_id' in sl.facts.columns else set()
    subjects=set(sl.facts['subject'].drop_nulls().to_list()) if 'subject' in sl.facts.columns else set()
    model_part=effective_model(model,store,used_templates,subjects)
    details=allocation[1] if allocation else None
    evidence={name:_frame(getattr(sl,name,None)) for name in
              ('facts','pricing_gaps','raw_pricing_gaps','coverage_gap_rows','order_issue_rows','allocation_evidence','operations_evidence')}
    evidence['commission']=_frame(details)
    cost_sources=_cost_sources(ws,store.id,sl.period,payload,ingestion,sl)
    evidence['approved_cost_sources']=cost_sources
    date_sources=[]
    frame=getattr(sl,'facts',None)
    if frame is not None and 'source_note' in frame.columns:
        used=frame.filter(frame['source_note'].cast(str).str.contains('原订单日期依据：',literal=True).fill_null(False))
        keys=set()
        for column in ('order_id','link_key'):
            if column in used.columns:keys.update(str(v) for v in used[column].drop_nulls().to_list())
        for value in getattr(ingestion,'original_date_source_evidence',[]):
            fact=value['source_fact']
            if fact['store_id']!=store.id or fact['platform_order_id'] not in keys:continue
            refs=[value['input_ref'],*fact['source_refs']]
            artifacts=[{'path':Path(r['path']).resolve().relative_to(ws.root.resolve()).as_posix(),
                        'sha':r['sha256'],'sealed':False} for r in refs]
            date_sources.append({'platform_order_id':fact['platform_order_id'],'original_day':fact['original_day'],
                                 'source_task_id':fact['source_task_id'],'artifacts':artifacts})
    evidence['verified_original_date_sources']=date_sources
    from .source_dates import potential_parents,fingerprint as date_fingerprint
    date_dependencies=potential_parents(ingestion,model,store,sl.period,getattr(sl,'facts',None))
    date_source_sha=date_fingerprint(ws,store.id,sl.period,date_dependencies)
    evidence['original_date_facts_sha']=date_source_sha
    code=engine_version()
    from .version import assert_code_integrity
    code_state=assert_code_integrity()
    source_code=code_state['source_code_sha']
    evidence_sha=journal.digest(evidence)
    result_sha=journal.digest(_logical(payload))
    dep=journal.digest({'engine':code,'source_code_sha':source_code,'model':model_part,'result_sha':result_sha,'evidence_sha':evidence_sha})
    from .frozen_refund_guard import protected_artifacts,event_index
    frozen_refund_artifacts=protected_artifacts(sl.facts)
    manifest={**(source_context or source_manifest(ws,store.id)),'engine':code,'source_code_sha':source_code,'model_sha':journal.digest(model.model_dump(mode='json')),
        'source_refs':sorted({r.ref.sha256 for r in ingestion.items}),
        'period':sl.period,'store_id':store.id,'evidence_sha':evidence_sha,
        'effective_model_sha':journal.digest(model_part),'template_ids':sorted(used_templates),'subjects':sorted(subjects),
        'approved_cost_sources':cost_sources,'frozen_refund_artifacts':frozen_refund_artifacts,
        'frozen_refund_event_index':event_index(ingestion,sl.facts,store.id,sl.period),
        'verified_original_date_sources':date_sources,
        'original_date_dependencies':date_dependencies,'original_date_facts_sha':date_source_sha,
        'collectable_scope':collectable_scope(getattr(sl,'pricing_gaps',None),getattr(sl,'coverage_gap_rows',None),getattr(sl,'order_issue_rows',None),payload),
        'registry_revision':(payload.get('commission') or {}).get('registry_revision'),
        'manual_revision':source_context.get('manual_versions',{}).get(sl.period,0) if source_context is not None else
            ws.conn.execute('SELECT coalesce(max(id),0) FROM cost_line_log WHERE store_id=? AND period=?',(store.id,sl.period)).fetchone()[0]}
    from .source_recovery import anchor_index
    manifest['source_recovery_index']=anchor_index(ingestion,store,sl.period,
        getattr(sl,'pricing_gaps',None),getattr(sl,'coverage_gap_rows',None),getattr(sl,'facts',None))
    sparse=manifest['source_recovery_index'];original_shas=set()
    for item in sparse.get('orders',{}).values():
        for kind in ('native','cost'):
            original_shas.update(a['sha256'] for a in item.get(kind,[]))
    original_shas.update(d['anchor']['sha256'] for d in sparse.get('date_demands',{}).values())
    manifest['source_refs']=sorted(set(manifest['source_refs'])|original_shas)
    manifest['source_recovery_original_artifacts']=[{'path':ws.path_of(sha).relative_to(ws.root).as_posix(),
        'sha':sha,'sealed':False,'role':'parsed_original_evidence_not_price_fallback'} for sha in sorted(original_shas)]
    return {'dependency_sha':dep,'result_sha':result_sha,'evidence_sha':evidence_sha,'manifest':manifest}


def reusable(ws,store_id,period,proof,registry=None):
    if not reproducible(proof['manifest']['engine']):return None
    row=ws.latest_run(store_id,period)
    if not row or row['engine']!=proof['manifest']['engine'] or not row['evidence_ready'] or row['evidence_error']:return None
    old=ws.conn.execute('SELECT * FROM automation_run_proof WHERE run_id=?',(row['id'],)).fetchone()
    if not old or any(old[k]!=proof[k] for k in ('dependency_sha','result_sha','evidence_sha')):return None
    recorded=json.loads(old['manifest_json'])
    for artifact in recorded.get('artifacts',[]):
        if not valid_artifact(ws,artifact):return None
    calculation=(json.loads(row['result']).get('commission') or {}).get('calculation_id')
    if calculation:
        if registry is None:return None
        with registry.connect() as conn:
            item=conn.execute('SELECT path,sha FROM calculation WHERE id=? AND finance_run=?',(calculation,row['id'])).fetchone()
        if not item:return None
        path=registry.root/'calculations'/item['path']
        if not path.is_file() or file_digest(path)!=item['sha']:return None
    from .finance_guard import guard
    from .version import assert_code_integrity
    with guard(ws.root,store_id,period),ws.transaction() as conn:
        assert_code_integrity()
        conn.execute('INSERT INTO automation_run_validation VALUES(?,?,?,?,?,?)',
            (journal.ident(),row['id'],proof['dependency_sha'],journal.digest(proof['manifest']),journal.encoded(proof['manifest']),journal.now()))
    return row


def save(ws,run_id,proof):
    row=ws.conn.execute('SELECT evidence_ready,evidence_error FROM run WHERE id=?',(run_id,)).fetchone()
    if not row or not row['evidence_ready'] or row['evidence_error']:return
    main=ws.facts_path(run_id)
    artifacts=[]
    for path in [main,*[main.with_suffix(s) for s in ('.pricing.parquet','.pricing-raw.parquet','.coverage.parquet','.allocation.parquet','.operations.parquet','.order-issues.parquet')]]:
        if path.exists():
            if not verified(path):return
            artifacts.append({'path':path.relative_to(ws.root).as_posix(),'sha':file_digest(path)})
    manifest={**proof['manifest'],'artifacts':artifacts}
    for artifact in proof['manifest'].get('source_recovery_original_artifacts',[]):
        if not valid_artifact(ws,artifact):
            ws.mark_evidence(run_id,ready=False,error='原始取证文件缺失或哈希不一致，未将旧成本价格重新计入')
            return
        artifacts.append(artifact)
    for source in [*proof['manifest'].get('approved_cost_sources',[]),*proof['manifest'].get('verified_original_date_sources',[])]:
        for artifact in source['artifacts']:
            if not valid_artifact(ws,artifact):
                ws.mark_evidence(run_id,ready=False,error='已采用历史成本的原始凭据缺失或哈希不一致')
                return
            artifacts.append(artifact)
    for artifact in proof['manifest'].get('frozen_refund_artifacts',[]):
        if not valid_artifact(ws,artifact):
            ws.mark_evidence(run_id,ready=False,error='已结退款归属档案失效，当前结果保持待核')
            return
        artifacts.append(artifact)
    from .finance_guard import guard
    from .version import assert_code_integrity
    with guard(ws.root,proof['manifest']['store_id'],proof['manifest']['period']),ws.transaction() as conn:
        assert_code_integrity()
        conn.execute('INSERT OR IGNORE INTO automation_run_proof VALUES(?,?,?,?,?,?)',
            (run_id,proof['dependency_sha'],proof['result_sha'],proof['evidence_sha'],journal.encoded(manifest),journal.now()))
