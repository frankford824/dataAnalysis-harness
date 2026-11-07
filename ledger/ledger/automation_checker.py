"""One deterministic source of month readiness, including final close checks."""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from types import SimpleNamespace

from . import automation_store as journal

HARD_FINDINGS=frozenset({'evidence_archive','source_sync_pending','promotion_integrity',
    'promotion_scope_evidence','allocation_basis','shared_store_identity','statement_identity',
    'cost_return_evidence','order_identity','historical_cost_evidence','shared_fee_source_conflict',
    'shared_fee_evidence_unverified','shared_fee_index_pending'})


def issue(code,message,hard=True):
    return {'code':code,'message':message,'hard':hard}


def bind_model(ws,model):
    text=model.model_dump_json()
    # A repeat calculation must not invalidate other periods merely by writing
    # the same model again.
    with ws.conn:
        ws.conn.execute('INSERT INTO automation_context VALUES(1,?) ON CONFLICT(id) DO UPDATE SET model_json=excluded.model_json WHERE model_json<>excluded.model_json',(text,))


def resolve_model(ws,model=None):
    if model is not None:return model
    row=ws.conn.execute('SELECT model_json FROM automation_context WHERE id=1').fetchone()
    if not row:return None
    from .model.schema import Model
    return Model.model_validate_json(row[0])


def pending(ws,store_id):
    path=ws.root/'commission'/'registry.db'
    if not path.exists():return None
    try:
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as conn:
            conn.row_factory=sqlite3.Row
            row=conn.execute('SELECT * FROM pending WHERE store_id=?',(store_id,)).fetchone()
            return dict(row) if row else None
    except sqlite3.Error:
        return {'error':'自动核算队列暂不可读'}


def feed_state(ws):
    from .pricing_status import read_feed
    try:return read_feed(ws.root)
    except sqlite3.Error:return {'last_error':'来源状态暂不可读'}


def feed_scope(ws,store_id=None,period=None,recorded=None):
    if store_id is not None and period is not None:
        if recorded is None:
            run=current_run(ws,store_id,period)
            proof=ws.conn.execute('SELECT manifest_json FROM automation_run_validation WHERE run_id=? ORDER BY rowid DESC LIMIT 1',(run['id'],)).fetchone() if run else None
            if proof is None and run:proof=ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(run['id'],)).fetchone()
            recorded=json.loads(proof['manifest_json']) if proof else None
        from .feed_freshness import evaluate
        return evaluate(ws,store_id,period,recorded)['identity']
    value=feed_state(ws)
    return {k:value.get(k) for k in ('snapshot_id','consumed_seq','source_latest_seq','last_error')} if value else None


def current_run(ws,store_id,period):
    row=ws.conn.execute('''SELECT r.id,r.store_id,r.period,r.engine,r.model_revision,r.evidence_ready,r.evidence_error,
        r.input_fingerprint,r.shas,coalesce(s.overview_json,r.result) result FROM run r
        LEFT JOIN run_report_slice s ON s.run_id=r.id AND s.payload_kind='run'
        WHERE r.store_id=? AND r.period=? ORDER BY r.id DESC LIMIT 1''',(store_id,period)).fetchone()
    return dict(row) if row else None


def pending_input_identity(ws,store_id,period,*,recorded=None,current=None):
    """Fence the receive/enqueue gap until this slice is revalidated.

    Unrelated changes disappear from this identity once a trusted equivalence
    validation records the new source epoch; no unverified input is accepted
    merely because the last financial result has not changed yet.
    """
    if recorded is None:
        run=current_run(ws,store_id,period)
        proof=ws.conn.execute('SELECT manifest_json FROM automation_run_validation WHERE run_id=? ORDER BY rowid DESC LIMIT 1',(run['id'],)).fetchone() if run else None
        if proof is None and run:proof=ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(run['id'],)).fetchone()
        recorded=json.loads(proof['manifest_json']) if proof else None
    if not recorded:return None
    from .automation_period_proof import source_manifest
    current=current if current is not None else source_manifest(ws,store_id)
    revision=current['manual_versions'].get(period,0)
    if (journal.digest(current['slots'])==journal.digest(recorded.get('slots',[]))
        and current['version']==recorded.get('version') and revision==recorded.get('manual_revision',0)):
        return None
    return journal.digest({'slots':current['slots'],'version':current['version'],'manual_revision':revision})


def period_header(ws,store_id,period,latest_id=None):
    row=ws.conn.execute('SELECT state,run_id FROM period WHERE store_id=? AND period=?',(store_id,period)).fetchone()
    return SimpleNamespace(state=row['state'],closed=row['state']=='closed',
        run_id=row['run_id'] if row['state']=='closed' else latest_id) if row else None


def revenue(payload,model):
    node_id=next((n.id for n in model.statement if n.headline=='revenue'),'') if model else ''
    row=next((r for r in payload.get('statement',[]) if r.get('id')==node_id),None)
    if row is None or row.get('available',True) is False or row.get('value') is None:return None
    try:
        amount=Decimal(str(row['value']))
        return amount if amount.is_finite() else None
    except ArithmeticError:return None


def operating_stores(ws,model,period,*,include_excluded=False):
    decisions=journal.active_decisions(ws.conn,period)
    existing={r[0] for r in ws.conn.execute('SELECT store_id FROM period WHERE period=?',(period,))}
    result=[]
    from zoneinfo import ZoneInfo
    from datetime import datetime
    current_month=datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m')
    for store in model.stores:
        decision=decisions.get((store.id,'store_scope'))
        included=decision['values'].get('included') if decision else None
        # Today's archive flag is not a historical opening/closing date.
        if store.archived and period>=current_month and store.id not in existing and included is not True:continue
        if included is False and not include_excluded:continue
        result.append(store)
    return result


def company_basis(ws,model,period):
    """Missing stores and unknown revenue never silently become zero."""
    model=resolve_model(ws,model)
    if model is None:
        return {'status':'needs_decision','total':None,'basis_total':None,'shares':{},'basis':[],
                'missing_store_ids':[],'input_sha':journal.digest(['missing-model',period]),
                'issues':[issue('company_scope_unknown','月度营业范围尚未固定，不能冻结公摊')]}
    decisions=journal.active_decisions(ws.conn,period)
    stores=operating_stores(ws,model,period)
    scope=[]
    for store in stores:
        decision=decisions.get((store.id,'store_scope'))
        if decision and decision['values'].get('included') is False:continue
        scope.append(store)
    configured=model.overhead(period)
    decided=decisions.get(('','overhead_total'))
    total=Decimal(str(decided['values']['amount'])) if decided else Decimal(str(configured)) if configured is not None else None
    source=ws.conn.execute('''SELECT p.store_id,p.state,r.id,r.evidence_ready,r.evidence_error,
        coalesce(json_extract(snapshot_json(s.overview_json),'$.source_sync_pending'),json_extract(snapshot_json(r.result),'$.source_sync_pending'),0) source_pending,
        coalesce(json_type(snapshot_json(s.overview_json),'$.calculation_inputs'),json_type(snapshot_json(r.result),'$.calculation_inputs')) input_contract,
        (SELECT coalesce(max(v.id),0) FROM version v WHERE v.store_id IN (p.store_id,'__shared__')) source_version,
        coalesce(snapshot_json(s.statement_json),json_extract(snapshot_json(coalesce(mf.result_json,r.result)),'$.statement'),'[]') statement_json
        FROM period p LEFT JOIN run r ON r.id=CASE WHEN p.state='closed' AND p.run_id IS NOT NULL THEN p.run_id ELSE
        (SELECT max(id) FROM run rr WHERE rr.store_id=p.store_id AND rr.period=p.period) END
        LEFT JOIN manual_finance mf ON mf.run_id=r.id AND p.state='closed'
        LEFT JOIN run_report_slice s ON s.run_id=r.id AND s.payload_kind=CASE WHEN mf.run_id IS NOT NULL THEN 'manual' ELSE 'run' END
        WHERE p.period=?''',(period,)).fetchall()
    by_store={r['store_id']:r for r in source}
    basis=[];missing=[]
    for store in scope:
        entry=by_store.get(store.id)
        payload={'statement':json.loads(entry['statement_json'])} if entry else {}
        amount=revenue(payload,model)
        if entry and (not entry['evidence_ready'] or entry['evidence_error']
                      or (entry['source_pending'] and entry['state']!='closed')):amount=None
        if entry and entry['state']!='closed':
            proof=ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(entry['id'],)).fetchone()
            validation=ws.conn.execute('SELECT manifest_json FROM automation_run_validation WHERE run_id=? ORDER BY rowid DESC LIMIT 1',(entry['id'],)).fetchone()
            captured=json.loads((validation or proof)['manifest_json']) if validation or proof else None
            if entry['input_contract'] and not captured:amount=None
            if captured and captured.get('version')!=entry['source_version']:amount=None
        if amount is None:missing.append(store.id)
        basis.append({'store_id':store.id,'revenue':str(amount) if amount is not None else None,
                      'run_id':entry['id'] if entry else None})
    denominator=sum((max(Decimal(r['revenue']),Decimal(0)) for r in basis if r['revenue'] is not None),Decimal(0))
    # Income identity, not run IDs or unrelated metadata, controls the company
    # denominator. Equal-income recomputations do not redistribute frozen pay.
    signature=journal.digest({'period':period,'total':str(total),'basis':[{k:v for k,v in r.items() if k!='run_id'} for r in basis]})
    problems=[]
    if not scope:problems.append(issue('company_scope_empty','本月没有已确认的营业店铺'))
    if missing:problems.append(issue('company_revenue_unknown','公司本月仍有营业店铺收入未确定'))
    if total is None:problems.append(issue('overhead_total_unknown','本月兼职核定依据尚未收到，资料到达并核定后继续公摊和月结',False))
    if total is not None and total>0 and denominator<=0:problems.append(issue('overhead_basis_empty','全公司没有正收入，无法确定兼职费用分摊'))
    shares={}
    if not problems:
        from .overhead import allocate
        if total==0:shares={s.id:'0.00' for s in scope}
        else:
            spread=allocate(period,float(total),[(r['store_id'],float(Decimal(r['revenue']))) for r in basis])
            shares={s.id:format(Decimal(str(spread.of(s.id))).quantize(Decimal('.01')),'f') for s in scope}
    frozen=ws.conn.execute('SELECT * FROM automation_overhead_close WHERE period=? ORDER BY rowid DESC LIMIT 1',(period,)).fetchone()
    closed=ws.conn.execute("SELECT 1 FROM period WHERE period=? AND state='closed' LIMIT 1",(period,)).fetchone()
    finance_decision=None
    frozen_basis=json.loads(frozen['basis_json']) if frozen else []
    same_basis=bool(frozen and total is not None and Decimal(frozen['total'])==total
        and {r['store_id']:Decimal(r['revenue']) if r['revenue'] is not None else None for r in frozen_basis}
        =={r['store_id']:Decimal(r['revenue']) if r['revenue'] is not None else None for r in basis})
    if same_basis:
        # Numeric equality, including 100 versus 100.00, reuses the original
        # immutable company identity. Never rewrite a historical signature.
        signature=frozen['input_sha']
        if not problems:shares=json.loads(frozen['shares_json'])
    frozen_unknown=bool(frozen and any(r.get('revenue') is None for r in frozen_basis))
    if frozen and (not same_basis or frozen_unknown) and closed:
        reason='overhead_frozen_basis_unverified' if frozen_unknown else 'overhead_frozen_input_changed'
        problems.append(issue(reason,'已冻结公司分摊基准仍有未验证收入，须核对历史依据' if frozen_unknown else
                              '公司收入或兼职总额已变化，与已冻结公摊不一致；请先核对历史冻结依据'))
        shares={}
        finance_decision={'reason_code':reason,'frozen':{
            'input_sha':frozen['input_sha'],'total':frozen['total'],'basis':json.loads(frozen['basis_json']),
            'shares':json.loads(frozen['shares_json']),'at':frozen['at']}}
    legacy=ws.conn.execute("SELECT p.store_id,p.period,p.run_id,l.amount,r.engine,r.model_revision,r.input_fingerprint FROM period p JOIN run_labor l ON l.run_id=p.run_id JOIN run r ON r.id=p.run_id WHERE p.period=? AND p.state='closed' ORDER BY p.store_id",(period,)).fetchall()
    if not frozen and any(Decimal(r['amount'])!=0 for r in legacy):
        problems.append(issue('overhead_legacy_frozen_basis','本月已有逐次结账的兼职份额，但统一公司基准尚未验证，请公司财务核对历史依据'))
        shares={}
        finance_decision={'reason_code':'overhead_legacy_frozen_basis','frozen':None}
    if finance_decision:
        finance_decision.update(kind='company_finance',scope='company_month',role='company_finance',role_label='公司财务负责人',
            assignee_status='role_unconfigured',period=period,
            summary='公司级冻结公摊依据需要财务核对；已结账与已确认实发保持原记录',
            delivery_hint='请公司财务依据已留档材料决定处理方式；无需各店重复提交已收到的资料。系统不会自动反结账、改实发或生成历史差额。',
            current={'input_sha':signature,'total':str(total) if total is not None else None,'basis_total':str(denominator),'basis':basis},
            unknown_revenue_store_ids=list(missing),closed_allocations=[dict(r) for r in legacy],actions=[])
        old={r['store_id']:r.get('revenue') for r in frozen_basis};now={r['store_id']:r.get('revenue') for r in basis}
        finance_decision['comparison_status']='known_frozen_basis' if frozen else 'legacy_basis_unverified'
        finance_decision['total_evidence_status']=('unverified_legacy_basis' if not frozen else 'unknown' if total is None else
                                                  'same' if Decimal(frozen['total'])==total else 'changed')
        finance_decision['changes']=[{'store_id':sid,'frozen_revenue':old.get(sid),'current_revenue':now.get(sid),
            'kind':'scope_added' if sid not in old else 'scope_removed' if sid not in now else
                   'current_revenue_unverified' if now[sid] is None else 'revenue_evidence_available' if old[sid] is None else 'revenue_changed'}
            for sid in sorted(set(old)|set(now)) if frozen and (sid not in old or sid not in now or
                (None if old[sid] is None else Decimal(old[sid]))!=(None if now[sid] is None else Decimal(now[sid])))]
    return {'status':'needs_decision' if finance_decision else 'processing' if missing else 'needs_decision' if problems else 'completed',
            'total':str(total) if total is not None else None,'basis_total':str(denominator),
            'basis':basis,'shares':shares,'missing_store_ids':missing,'input_sha':signature,'issues':problems,'finance_decision':finance_decision}



def source_fact_dependencies_current(ws,store_id,recorded):
    """Check the exact used IDs before filtering can hide a revoked fact."""
    from datetime import datetime
    from .historical_cost_facts import RULE as v2_rule,RULE_SHA as v2_sha
    from .automation_source_cost_v3 import RULE as v3_rule,RULE_SHA as v3_sha
    rules={v2_rule:v2_sha,v3_rule:v3_sha}
    for source in (recorded or {}).get('approved_cost_sources',[]):
        if not source.get('source_fact'):continue
        row=ws.conn.execute("""SELECT d.*,a.identity_kind,a.action AS authority_action,
            a.input_sha AS authority_input,a.proposal_sha AS authority_proposal,a.scope_json
            FROM automation_decision d LEFT JOIN automation_authorization a ON a.request_id=d.request_id
            WHERE d.id=?""",(source.get('decision_id'),)).fetchone()
        if (not row or row['store_id']!=store_id or row['action']!='apply' or row['kind'] not in rules
            or row['identity_kind']!='delegated_service' or row['authority_action']!=row['kind']
            or row['authority_input']!=row['input_sha'] or row['authority_proposal']!=row['proposal_sha']):return False
        if ws.conn.execute("SELECT 1 FROM automation_decision WHERE action='revoke' AND ref_id=?",(row['id'],)).fetchone():return False
        try:
            value=json.loads(row['values_json']);authority=json.loads(row['scope_json'])
            facts=value.get('facts') if row['kind']==v2_rule else value.get('fact')
            if value.get('rule_sha')!=rules[row['kind']] or journal.digest(facts)!=row['proposal_sha']:return False
            if row['kind']==v2_rule:
                if not isinstance(facts,list) or not any(f.get('coverage_key')==source.get('coverage_key') and f.get('quote_sha')==source.get('quote_sha') for f in facts):return False
            elif (not isinstance(facts,dict) or facts.get('store_id')!=store_id
                or (facts.get('target') or {}).get('coverage_key')!=source.get('coverage_key')):return False
            grant=ws.conn.execute("""SELECT d.*,a.identity_kind,a.action AS authority_action FROM automation_decision d
                LEFT JOIN automation_authorization a ON a.request_id=d.request_id WHERE d.id=?""",(authority.get('approved_rule_grant'),)).fetchone()
            if (not grant or grant['kind']!='rule_grant' or grant['action']!='apply' or grant['store_id']!=store_id
                or grant['identity_kind']!='human' or grant['authority_action']!='decision.apply'):return False
            values=json.loads(grant['values_json']);adopted=datetime.fromisoformat(row['at'])
            if (values.get('enabled') is not True or values.get('rule_id')!=row['kind'] or values.get('rule_sha')!=rules[row['kind']]
                or not datetime.fromisoformat(values['valid_from'])<=adopted<datetime.fromisoformat(values['valid_until'])
                or not values.get('period_from',grant['period'])<=row['period']<=values.get('period_until',grant['period'])):return False
            # Frozen borrowed inputs retain the already verified historical
            # authority contract. No open owned basis survives withdrawal.
            frozen_origin=bool(source.get('frozen_origin_run_id'))
            if source.get('origin_period') and source.get('origin_period')!=recorded.get('period'):
                origin=ws.state(store_id,source['origin_period'])
                frozen_origin=frozen_origin or bool(origin and origin.closed)
            if not frozen_origin and ws.conn.execute("SELECT 1 FROM automation_decision WHERE action='revoke' AND ref_id=?",(grant['id'],)).fetchone():return False
        except (ValueError,TypeError,KeyError,AttributeError):return False
    return True

def check(ws,store_id,period,*,model=None,expected_run_id=None,ignored_blockers=(),
          manual_result=None,check_company=True,check_pending=True,company_context=None,verify_artifacts=False,
          reuse_refund_validation=False):
    model=resolve_model(ws,model)
    row=current_run(ws,store_id,period)
    state=period_header(ws,store_id,period,row['id'] if row else None)
    problems=[]
    from .version import assert_code_integrity,CodeIntegrityError,CODE_MAINTENANCE
    try:code_state=assert_code_integrity()
    except CodeIntegrityError as exc:
        code_state=exc.integrity
        problems.append(issue('code_source_drift',CODE_MAINTENANCE))
    recorded=None
    payload=manual_result or (json.loads(row['result']) if row else {})
    if row is None:problems.append(issue('no_result','尚未取得本店本月完整资料，系统将继续取证核算'))
    else:
        if expected_run_id is not None and row['id']!=expected_run_id:problems.append(issue('stale_run','核算结果已更新，请重新预览'))
        if not row['evidence_ready'] or row['evidence_error']:problems.append(issue('evidence_archive','事实证据尚未完整留档'))
        proof=ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(row['id'],)).fetchone()
        validation=ws.conn.execute('SELECT manifest_json FROM automation_run_validation WHERE run_id=? ORDER BY rowid DESC LIMIT 1',(row['id'],)).fetchone()
        recorded=json.loads((validation or proof)['manifest_json']) if validation or proof else None
        if 'calculation_inputs' in payload and not proof:
            problems.append(issue('input_capture_unverified','本次结果缺少完整输入与账期血缘凭据，请先按当前服务重算'))
        if recorded:
            if not bool(state and state.closed) and not source_fact_dependencies_current(ws,store_id,recorded):
                problems.append(issue('source_fact_input_unverified','本期曾采用的成本来源事实或其授权已撤回或不可验证，系统须重新核算后再月结'))
            if recorded.get('source_code_sha') and recorded['source_code_sha']!=code_state.get('source_code_sha'):
                problems.append(issue('unverified_code_change','适用核算源码已更新，请等待当前批准代码自动重算复核'))
            elif 'calculation_inputs' in payload and not recorded.get('source_code_sha'):
                problems.append(issue('unverified_code_change','本次核算尚无实际源码指纹，请等待批准代码自动重算复核'))
            from .automation_period_proof import source_manifest,effective_model
            if model and recorded.get('effective_model_sha'):
                effective=effective_model(model,model.store(store_id),recorded.get('template_ids',[]),recorded.get('subjects',[]))
                if journal.digest(effective)!=recorded['effective_model_sha']:
                    problems.append(issue('unverified_model_change','本账期适用规则已有变化，请先完成当前规则的自动核算复核'))
            current=source_manifest(ws,store_id)
            if journal.digest(current['slots'])!=journal.digest(recorded.get('slots',[])) or current['version']!=recorded.get('version'):
                problems.append(issue('unverified_input_change','本店新输入尚未完成本账期等价复核'))
            if current.get('period_cost_input_versions',{}).get(period,0)!=recorded.get('period_cost_input_versions',{}).get(period,0):
                problems.append(issue('period_cost_input_unverified','新批准的整期成本尚未进入本次核算'))
            cost_input=payload.get('period_cost_input') or {}
            if cost_input.get('status')=='stale':
                problems.append(issue('period_cost_input_stale','原成本批准所依据的输入已变化，需按新证据重新确认'))
            if cost_input.get('status') in {'applied','superseded_by_complete_sources'}:
                from .period_cost_inputs import verify_applied
                from .workspace import WorkspaceError
                try:verify_applied(ws,store_id,period,cost_input)
                except (WorkspaceError,ValueError,OSError,KeyError):problems.append(issue('period_cost_input_source_unverified','整期成本原始批准凭据失效，请恢复原档并复核'))
            if not manual_result and current['manual_versions'].get(period,0)!=recorded.get('manual_revision',0):
                problems.append(issue('manual_input_unverified','新补录决定尚未全部进入当前账期核算'))
            from .automation_period_proof import valid_artifact
            for artifact in json.loads(proof['manifest_json']).get('artifacts',[]) if proof and verify_artifacts else []:
                if not valid_artifact(ws,artifact):problems.append(issue('evidence_archive','本次核算证据校验失败'))
        if any(d.get('status')=='conflict' for d in payload.get('deduplication',[])):
            problems.append(issue('statement_conflict','原始流水存在冲突或精度丢失'))
        for finding in payload.get('findings',[]):
            if finding.get('blocking') and not finding.get('passed'):
                code=str(finding.get('id') or 'unverified_finding')
                hard=code in HARD_FINDINGS or code not in {'fees','dropship','chk_goods_coverage','unclassified','manual_review'}
                if code in ignored_blockers and not hard:continue
                problems.append(issue(code,str(finding.get('message') or finding.get('name') or '核算依据待核对'),hard))
        if payload.get('missing_sources'):problems.append(issue('missing_sources','还缺：'+'、'.join(map(str,payload['missing_sources']))))
        if payload.get('source_sync_pending'):problems.append(issue('source_sync_pending','本次输入捕获时来源尚未追平'))
        from .period_cost_inputs import verify_period_records
        from .workspace import WorkspaceError
        try:verify_period_records(ws,store_id,period)
        except (WorkspaceError,ValueError,OSError,KeyError):problems.append(issue('period_cost_input_source_unverified','成本审批原档或身份凭据校验失败，需维护恢复；无需重录金额'))
        cost_review=payload.get('cost_review') or {}
        amount_complete=cost_review.get('amount_complete')
        if amount_complete is not None:
            if (not isinstance(amount_complete,dict) or set(amount_complete)!={'goods','dropship','reshipment'}
                or any(type(value) is not bool for value in amount_complete.values())):
                problems.append(issue('cost_amount_completeness_unverified','本次成本完整性凭据无效，请按当前输入重新核算'))
            elif not all(amount_complete.values()):
                problems.append(issue('required_cost_unresolved','仍有必需成本金额或有效价格未核齐；覆盖质量线不能把未知成本当零'))
        elif (payload.get('pricing_pending_count') or (payload.get('cost_coverage') or {}).get('uncovered')):
            # An old summary's 95%-passed flag is not amount authority. Rebuild
            # its scoped economic evidence; never guess missing fields true.
            problems.append(issue('cost_amount_completeness_unverified','旧结果仍有成本缺口，须重新验证金额完整性'))
        profit_id=next((n.id for n in model.statement if n.headline=='profit'),'net_profit') if model else 'net_profit'
        profit=next((n for n in payload.get('statement',[]) if n.get('id')==profit_id or n.get('name')=='利润'),None)
        if profit and (profit.get('value') is None or profit.get('available',True) is False):
            problems.append(issue('profit_unknown','利润金额尚未确定，不能确认月结'))
        if (payload.get('cost_review') or {}).get('requires_human'):
            problems.append(issue('cost_decision_required','本期成本仍需人工确认',False))
        if (payload.get('commission') or {}).get('amount_complete') is False and not payload.get('manual_payout') and not payload.get('manual_cost'):
            problems.append(issue('commission_incomplete','提成人员或金额依据仍未完整',False))
        if not payload.get('can_close') and not problems and not ignored_blockers:problems.append(issue('not_closeable','本次核算尚未通过结账检查'))
    from .automation_shared_fee import evaluate as shared_fee_check
    shared_fee=shared_fee_check(ws,store_id,period,row['id'] if row else None,model=model,
        closed=bool(state and state.closed),payload=payload)
    problems.extend(shared_fee['issues'])
    from .feed_freshness import evaluate as evaluate_feed
    feed=evaluate_feed(ws,store_id,period,recorded)
    if feed['blocked']:
        problems.append(issue('source_sync_pending','本次捕获的来源同步前缀或本店后续变更尚未完成复核，不能确认月结'))
    work=pending(ws,store_id) if check_pending else None
    # Order-only work is bounded per month by the captured-prefix check above;
    # it must not hold back a month none of its changes reach.
    order_only=bool(work and not work.get('error') and feed['mode']=='captured_prefix'
        and work.get('full_revision',1)<=work.get('full_applied_revision',0)
        and work.get('files_revision',0)<=work.get('files_applied_revision',0))
    if work and not order_only:problems.append(issue('recompute_pending',work.get('error') or '本店新资料尚在自动核算，完成后再确认'))
    company=(company_context if company_context is not None else company_basis(ws,model,period)) if check_company else None
    if company:problems.extend(company['issues'])
    # Deduplicate by code while retaining a deterministic order for CAS.
    problems=list({p['code']:p for p in problems}.values())
    scope={'store_id':store_id,'period':period,'run_id':row['id'] if row else None,
        'payload_sha':journal.digest(payload),'engine':row['engine'] if row else None,
        'model_revision':row['model_revision'] if row else None,
        'cost_line_revision':ws.conn.execute('SELECT coalesce(max(id),0) FROM cost_line_log WHERE store_id=? AND period=?',(store_id,period)).fetchone()[0],
        'period_cost_input_revision':ws.conn.execute("SELECT coalesce(max(rowid),0) FROM automation_decision WHERE kind='period_cost_input' AND store_id=? AND period=?",(store_id,period)).fetchone()[0],
        'period_state':state.state if state else 'open','feed':feed['identity'],
        'unverified_source_sha':pending_input_identity(ws,store_id,period,recorded=recorded),
        'pending':{k:work.get(k) for k in ('revision','source_seq','source_fingerprint','files_revision','files_applied_revision')} if work and not order_only else None,
        'company_sha':company['input_sha'] if company else None,'code_integrity':code_state}
    from .automation_source_cost_v3 import used as physical_facts,verify_for_close as verify_physical_facts,_adoption_authority
    from .automation_cost_actions import Pending as SourceFactPending
    physical=physical_facts(ws,store_id,period)
    for fact in physical:
        try:_adoption_authority(ws,fact)
        except (SourceFactPending,ValueError,KeyError):
            problems.append(issue('physical_source_authority_changed','已采用的物理来源规则已撤销或授权依据失效，请等待重新核验'))
    if verify_artifacts and physical and verify_physical_facts(ws,model,store_id,period):
        problems.append(issue('physical_source_fact_pending','实际发货成员或原始报价证据已变化，请等待独立复核'))
    if verify_artifacts and physical:
        from .automation_source_cost_v3 import quote_close_reasons
        if quote_close_reasons(ws,store_id,period):
            problems.append(issue('physical_quote_refresh_required','原日历史价尚未完成本次上游刷新，请等待自动复核后再确认月结'))
    from .historical_cost_facts import fingerprint as history_facts_fingerprint
    scope['historical_source_facts_sha']=history_facts_fingerprint(ws,store_id,period)
    from .automation_source_cost_v3 import fingerprint as physical_facts_fingerprint
    scope['physical_source_facts_sha']=physical_facts_fingerprint(ws,store_id,period)
    from .source_dates import fingerprint as date_facts_fingerprint
    date_dependencies=recorded.get('original_date_dependencies') if recorded else None
    current_date_sha=date_facts_fingerprint(ws,store_id,period,date_dependencies)
    scope['original_date_facts_sha']=current_date_sha
    if (recorded and not bool(state and state.closed)
        and ('original_date_facts_sha' in recorded or (date_dependencies or {}).get('potential_parent_ids'))
        and recorded.get('original_date_facts_sha')!=current_date_sha):
        problems.append(issue('original_date_input_unverified',
            '原订单日期依据已有变化，系统须按本期已封存依赖重新核算或验证等价后再月结'))
    if row is not None and model is not None and not (state and state.closed):
        from .frozen_refund_guard import validate_run as validate_frozen_refunds
        refund_state=validate_frozen_refunds(ws,model,model.store(store_id),period,row['id'],reuse=reuse_refund_validation)
        scope['frozen_refund_binding_sha']=refund_state['sha']
        if refund_state['recompute_required']:
            problems.append(issue('frozen_refund_recheck_required','已结退款归属依据已变化或未完整核实，请等待当前结果重新核算，不能确认旧金额'))
    scope['company_finance_sha']=journal.digest(company.get('finance_decision')) if company else None
    scope['shared_fee_source_sha']=shared_fee['input_sha']
    return {'allowed':not problems,'input_sha':journal.digest(scope),'scope':scope,'run_id':scope['run_id'],
            'issues':problems,'overhead':company,'result':payload,'shared_fee':shared_fee}


def close_view(ws,model,store_id,period,payload,*,company_context=None):
    """Decorate an outward copy; archived engine evidence stays immutable.

    Call this after a cached business payload is loaded. Company readiness can
    change when another store arrives, without changing this store's run.
    """
    value=dict(payload)
    checked=check(ws,store_id,period,model=model,company_context=company_context,
        expected_run_id=payload.get('run_id'),reuse_refund_validation=True)
    state=period_header(ws,store_id,period,checked['run_id'])
    closed=bool(state and state.closed)
    issues=([issue('already_closed','本月已结账，展示冻结结果')] if closed else checked['issues'])
    value['engine_can_close']=bool(payload.get('engine_can_close',payload.get('can_close')))
    value['can_close']=bool(checked['allowed'] and not closed)
    value['close_verification']={'allowed':value['can_close'],'input_sha':checked['input_sha'],'issues':issues}
    value['source_risks']=checked['shared_fee']['risks']
    value['close_verification']['source_risks']=checked['shared_fee']['risks']
    return value


def assert_close(ws,store_id,period,**kwargs):
    from .workspace import WorkspaceError
    result=check(ws,store_id,period,verify_artifacts=True,**kwargs)
    if not result['allowed']:
        raise WorkspaceError('；'.join(p['message'] for p in result['issues']))
    return result


def register_legacy_source(ws,run_id,source_path,*,cutoff,reference,actor):
    """Bind the dedicated importer to verified terminal evidence, never a note override."""
    from pathlib import Path
    from .historical_close import CUTOFF
    from .storage_integrity import digest
    from .workspace import WorkspaceError
    row=ws.conn.execute('SELECT * FROM run WHERE id=?',(run_id,)).fetchone()
    payload=json.loads(row['result']) if row else {}
    archive=payload.get('archive') or {}
    path=Path(source_path)
    expected=archive.get('source_sha256')
    if (not row or cutoff!=CUTOFF or row['period']>cutoff or archive.get('kind')!='legacy_final_summary'
        or int(archive.get('evidence_schema') or 0)<2 or not expected or expected not in json.loads(row['shas'])
        or not path.is_file() or digest(path)!=expected):
        raise WorkspaceError('历史归档只接受截止政策内已验证的终态原件')
    if not reference.strip() or not actor.strip():raise WorkspaceError('历史归档缺少授权来源和执行身份')
    with ws.transaction() as conn:
        conn.execute('INSERT OR IGNORE INTO automation_legacy_source VALUES(?,?,?,?,?,?,?)',
            (run_id,expected,str(path),cutoff,reference,actor,journal.now()))


def assert_legacy_close(ws,store_id,period,cutoff):
    from pathlib import Path
    from .storage_integrity import digest
    from .workspace import WorkspaceError
    row=ws.latest_run(store_id,period)
    proof=ws.conn.execute('SELECT * FROM automation_legacy_source WHERE run_id=?',(row['id'],)).fetchone() if row else None
    if not proof or proof['cutoff']!=cutoff or period>cutoff:
        raise WorkspaceError('普通失败快照不能凭说明按历史政策结账；缺少已验证终态原件授权')
    payload=json.loads(row['result']);archive=payload.get('archive') or {}
    findings=payload.get('findings') or []
    required={'legacy_source_integrity','legacy_scope_mapping','legacy_profit_tie_out','legacy_cutoff'}
    passed={f.get('id') for f in findings if f.get('passed')}
    blocked=[f for f in findings if f.get('blocking') and not f.get('passed')]
    path=Path(proof['source_path'])
    if (not row['evidence_ready'] or row['evidence_error'] or blocked or not required<=passed
        or archive.get('kind')!='legacy_final_summary' or archive.get('source_sha256')!=proof['source_sha']
        or not path.is_file() or digest(path)!=proof['source_sha']):
        raise WorkspaceError('历史终态原件或映射勾稽检查失败，不能归档冻结')
    return {'allowed':True,'input_sha':journal.digest([dict(proof),row['result']]),'run_id':row['id']}
