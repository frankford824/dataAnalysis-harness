"""V3: independently proved physical sources, consumed by the original engine.

A source proof or a preview is not monetary authority. Public requests contain
only current case/target identities; provider bytes, units and prices are read
by the backend, and writes require the distinct human-approved V3 rule.
"""
from __future__ import annotations
from decimal import Decimal, DecimalException, Inexact, Rounded, Underflow, localcontext
import math
import os

from . import automation_store as journal
from .automation_cost_actions import Pending

RULE='jst_physical_source_cost_facts.v3'
RULE_CONTRACT={'id':RULE,
 'scope':'one exactly identified native cost target in an approved open store/booked month',
 'source':'immutable original XML anchors and typed source graph retain containers; actual ordinary Sent physical members supply exact stock units',
 'quantity':'logical source quantity and physical stock pieces remain distinct; native identity, unit and Decimal conservation must be proved',
 'price':'independent positive company/SKU/original-day history intervals; raw quote and quantity retained without per-piece cent rounding',
 'consumption':'only proved missing components become a distinct original-engine source item; existing price and human authority have priority',
 'freshness':'explicit server-controlled current capture and complete financial-dependency semantic comparison; no mtime, TTL, first or latest guessing',
 'exclusions':'closed or confirmed payouts; unresolved source, lifecycle, quantity, ownership or partial-price conflicts',
 'authority':'distinct authenticated-human V3 scope grant; default off; no close, reopen, payout or policy approval',
 'established_facts':'kill switch or natural expiry stops new adoption, never silently removes existing facts; explicit withdrawal cannot be undone by an old grant',
 'frozen_reference':'only an actually counted nonzero frozen-run member input and original native basis may feed the unchanged return engine read-only; no whole-period manual total decomposition or current-quote fallback'}
RULE_SHA=journal.digest(RULE_CONTRACT)
SCHEMA='ledger-source-cost-fact.v3'
TEMPLATE='jst_source_cost_v3'
PROOF_BASIS=('literal_original_xml_quantity','typed_native_and_physical_identity',
             'explicit_normal_sku_enum','explicit_not_gift','physical_created_and_sent',
             'logical_and_physical_unit_conservation','independent_dated_company_quote')


def enabled():
    from .automation_deterministic import enabled as automatic_enabled
    return automatic_enabled() and os.environ.get('LEDGER_SOURCE_COST_V3_ENABLED','').lower() in {'1','true','yes','on'}


def positive(value):
    if isinstance(value,(bool,float)):
        raise Pending('source_decimal_lexeme_required')
    try:
        d=Decimal(value)
        if (not d.is_finite() or d<=0 or len(d.as_tuple().digits)>100
            or not -100<=d.adjusted()<=100 or abs(d.as_tuple().exponent)>100):
            raise ValueError()
        if not math.isfinite(float(d)) or float(d)<=0:raise ValueError()
        return d
    except (DecimalException,TypeError,ValueError,OverflowError):
        raise Pending('source_decimal_value_unrepresentable') from None


def decimal_semantic(value):
    d=positive(value);sign,digits,exponent=d.as_tuple();digits=list(digits)
    while len(digits)>1 and digits[-1]==0:digits.pop();exponent+=1
    return ''.join(map(str,digits))+'e'+str(exponent)


def exact_product(quantity,unit_cost):
    qty,unit=positive(quantity),positive(unit_cost)
    try:
        with localcontext() as context:
            context.prec=256
            context.traps[Inexact]=context.traps[Rounded]=context.traps[Underflow]=True
            amount=qty*unit
        if not math.isfinite(float(amount)) or float(amount)<=0:raise ValueError()
        # A representability check, not a new rounding policy. Keep the raw
        # Decimal product, and refuse a Float64/source projection that already
        # loses the engine's existing six-decimal currency precision.
        if abs(Decimal.from_float(float(amount))-amount)>Decimal('.0000005'):raise ValueError()
        from .money import money_float
        money_float(amount)
        return amount
    except (DecimalException,OverflowError,ValueError):
        raise Pending('source_amount_precision_unrepresentable') from None


def selected_members(verified,target,platform):
    """Internal pure selector, after verified_source has reread original bytes.

    No endpoint accepts this object from an agent. Optional raw flags retain
    absence; eligibility relies on explicit independent positive proof basis.
    """
    proof=verified['source_proof'];plan=verified['plan']
    if proof.get('schema')!='jst_source_members.v1' or proof.get('status')!='proved':
        raise Pending('source_member_proof_incomplete')
    if str(plan.get('platform_order_id'))!=str(target['order_id']):
        raise Pending('native_source_parent_conflict')
    child=str(target.get('sub_order_id') or '')
    if platform in {'taobao','douyin'} and not child:raise Pending('native_child_identity_unproved')
    members=[m for m in proof.get('members',[]) if str(m.get('platform_order_id'))==str(target['order_id'])
             and (platform not in {'taobao','douyin'} or str(m.get('platform_sub_order_id') or '')==child)]
    if not members or len(members)>2000:raise Pending('source_member_inventory_missing_or_unbounded')
    keys=set()
    for m in members:
        key=(str(m['internal_order_id']),str(m['internal_sub_order_id']))
        if key in keys:raise Pending('physical_source_member_duplicated')
        keys.add(key)
        if str(m.get('shop_id')) not in set(map(str,plan['shop_ids'])):
            raise Pending('physical_source_shop_conflict')
        positive(m['quantity']);positive(m['logical_quantity'])
        flags=m.get('observed_flags') or {}
        for layer in ('source_member','physical_member'):
            observed=flags.get(layer,{})
            if observed.get('sku_type')!={'present':True,'value':'normal'}:
                raise Pending('normal_sku_positive_basis_missing')
            gift=observed.get('is_gift',{})
            if gift.get('present') is not True or gift.get('value') is not False:
                raise Pending('non_gift_positive_basis_missing')
            for name in ('is_virtual','is_suspect'):
                value=observed.get(name,{'present':False,'value':None})
                if value.get('present') and value.get('value') is not False:
                    raise Pending('explicit_source_member_risk_unresolved')
        physical=flags['physical_member']
        if physical.get('item_status')!={'present':True,'value':'Sent'} or not physical.get('created',{}).get('value'):
            raise Pending('physical_source_lifecycle_unproved')
    return members


def quote_semantic(quote):
    intervals=[]
    for row in quote['intervals']:
        intervals.append({'begin_date':row['begin_date'],'begin_unbounded':row['begin_unbounded'],
                          'end_date':row['end_date'],'unit_cost':decimal_semantic(row['unit_cost'])})
    return {'schema':quote['schema'],'sku':quote['sku'],'company':quote['company'],
            'pricing_day':quote['pricing_day'],'unit_cost':decimal_semantic(quote['unit_cost']),
            'intervals':sorted({journal.encoded(row) for row in intervals})}

# Persistence uses the existing append-only decision/authorization/outbox
# transaction. There is no second financial ledger or source snapshot.
def active(conn,store_id,period=None):
    import json
    rows=conn.execute("""SELECT d.*,a.scope_json,a.proposal_sha AS authorization_sha,a.input_sha AS authorization_input FROM automation_decision d
        JOIN automation_authorization a ON a.request_id=d.request_id
        WHERE d.store_id=? AND d.kind=? AND d.action='apply'
        AND a.action=? AND a.identity_kind='delegated_service'
        AND NOT EXISTS(SELECT 1 FROM automation_decision r WHERE r.action='revoke' AND r.ref_id=d.id)
        ORDER BY d.rowid""",(store_id,RULE,RULE)).fetchall()
    result=[]
    for row in rows:
        value=json.loads(row['values_json'])
        if value.get('rule_sha')!=RULE_SHA or (period is not None and row['period']!=period):continue
        fact=value.get('fact')
        if not isinstance(fact,dict) or fact.get('schema')!=SCHEMA:continue
        if journal.digest(fact)!=row['proposal_sha'] or row['authorization_sha']!=row['proposal_sha'] or row['authorization_input']!=row['input_sha']:continue
        result.append({**fact,'decision_id':row['id'],'authorization':json.loads(row['scope_json']),'adopted_at':row['at']})
    return result


def fingerprint(ws,store_id,period=None):
    facts=active(ws.conn,store_id,period)
    withdrawn=[r['ref_id'] for r in ws.conn.execute("SELECT ref_id FROM automation_decision WHERE action='revoke' ORDER BY rowid") if r['ref_id'] in {f['authorization'].get('approved_rule_grant') for f in facts}]
    return journal.digest({'facts':facts,'withdrawn_grants':withdrawn})


def _retained(ws,refs):
    from pathlib import Path
    from .storage_integrity import digest
    root=ws.root.resolve()
    for ref in refs:
        path=Path(ref['path'])
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file() or digest(path)!=ref['sha256']:
            raise Pending('physical_source_artifact_missing_or_changed')


def _open(ws,store_id,period,*,allow_confirmed=False):
    """No SQLite constructor/migration is run against the separate Registry."""
    from contextlib import closing
    import sqlite3
    state=ws.state(store_id,period)
    if state and state.closed:raise Pending('closed_period_preserved')
    path=ws.root/'commission'/'registry.db'
    if path.is_file() and not allow_confirmed:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='payout_confirmation'").fetchone():
                if conn.execute('SELECT 1 FROM payout_confirmation WHERE store_id=? AND period=? LIMIT 1',(store_id,period)).fetchone():
                    raise Pending('confirmed_payout_preserved')


def _target(ws,request):
    import json
    from .automation_actions import Conflict
    from . import cost_lines
    _open(ws,request['store_id'],request['period'])
    row=ws.latest_run(request['store_id'],request['period'])
    if not row or row['id']!=request['run_id']:raise Conflict('当前核算已变化')
    if not row['evidence_ready'] or row['evidence_error']:raise Pending('run_evidence_unavailable')
    manual=ws.conn.execute('SELECT result_json FROM manual_finance WHERE run_id=?',(row['id'],)).fetchone()
    if manual and json.loads(manual['result_json']).get('manual_cost'):raise Pending('existing_period_manual_cost_preserved')
    view=cost_lines.current(ws,row['id'],request['store_id'],request['period'])
    target=next((r for r in view['items'] if r['coverage_key']==request['coverage_key']),None)
    if not target or target['context_sha']!=request['context_sha'] or target.get('order_count')!=1:
        raise Pending('native_cost_target_unproved')
    latest=cost_lines._latest(ws,request['store_id'],request['period'],row['id']).get(request['coverage_key'])
    if (latest['id'] if latest else 0)!=request['expected_line_revision']:raise Conflict('成本决定已变化')
    if target.get('manual_amount') is not None or any(r['coverage_key']==request['coverage_key'] for r in view['reviewed']):
        raise Pending('existing_cost_decision_preserved')
    return target


def _candidate(ws,model,request,history):
    from .source_recovery import verified_source,SourcePending
    target=_target(ws,request)
    source=verified_source(ws,model,request)
    members=selected_members(source,target,model.store(request['store_id']).platform)
    quotes={}
    for member in members:
        key=(member['company'],member['sku'],member['pricing_day'])
        if key not in quotes:
            quote=history.quote(key[1],key[0],key[2])
            if (quote.get('sku'),quote.get('company'),quote.get('pricing_day'))!=(key[1],key[0],key[2]):
                raise Pending('source_history_quote_unbound')
            positive(quote['unit_cost'])
            quotes[key]=quote
        exact_product(member['quantity'],quotes[key]['unit_cost'])
    refs=list(source['source_refs'])
    refs.extend(r for quote in quotes.values() for r in quote['artifact_refs'])
    _retained(ws,refs)
    return {'schema':SCHEMA,'rule_id':RULE,'rule_sha':RULE_SHA,
        'store_id':request['store_id'],'period':request['period'],
        'source_run_id':request['run_id'],'source_input_sha':request['input_sha'],
        'target':{k:target.get(k) for k in ('coverage_key','context_sha','order_id','sub_order_id')},
        'source_binding':source['source_binding'],'source_proof_sha':journal.digest(source['source_proof']),
        'members':members,'quotes':list(quotes.values()),'source_refs':refs,
        'proof_basis':list(PROOF_BASIS),'financial_applied':False}


def apply(ws,model,request,actor,*,history_client=None):
    """Only references enter here. Money/units/identities come from raw providers."""
    from .automation_actions import Conflict,_request,deliver
    from .automation_deterministic import approved_rule
    from .source_recovery import SourcePending
    from .upstream_history import HistoryClient,HistoryError
    from . import automation_checker as checker
    from .finance_guard import guard
    from .service import _store_lock
    from .version import assert_code_integrity
    assert_code_integrity()
    if (not actor or actor.get('kind')!='service' or not actor.get('authenticated')
        or not actor.get('id') or 'business' not in actor.get('scopes',[])
        or 'approved_action' not in actor.get('capabilities',[])):
        raise PermissionError('需要已授权的确定性业务服务身份')
    if request.get('rule_id')!=RULE:raise Conflict('需要独立批准的v3物理来源事实规则')
    _request(request['request_id']);journal.period(request['period'])
    key='approved-physical-source:'+request['request_id'];signature=journal.digest(request)
    if ws.conn.execute("SELECT 1 FROM automation_decision r JOIN automation_decision d ON d.id=r.ref_id WHERE r.action='revoke' AND d.request_id=?",(key,)).fetchone():
        raise Conflict('该来源事实已经撤回，旧请求不能重新采用')
    old=journal.receipt(ws.conn,key,signature)
    if old:return old
    grant=approved_rule(ws,request['store_id'],request['period'],rule_id=RULE,rule_sha=RULE_SHA)
    current=checker.check(ws,request['store_id'],request['period'],model=model)
    case=ws.conn.execute('SELECT * FROM automation_case WHERE id=?',(request['case_id'],)).fetchone()
    if (current['input_sha']!=request['input_sha'] or current['run_id']!=request['run_id'] or not case
        or (case['store_id'],case['period'],case['input_sha'])!=(request['store_id'],request['period'],request['input_sha'])):
        raise Conflict('案件或核算输入已变化')
    fact=None;reason=None;refs=[]
    withdrawn=ws.conn.execute(
        """SELECT 1 FROM automation_decision d JOIN automation_decision r ON r.ref_id=d.id AND r.action='revoke'
        WHERE d.kind=? AND d.store_id=? AND d.period=? AND d.action='apply'
        AND json_extract(d.values_json,'$.fact.target.coverage_key')=?
        AND r.rowid>(SELECT rowid FROM automation_decision WHERE id=?) LIMIT 1""",
        (RULE,request['store_id'],request['period'],request['coverage_key'],grant['id'])).fetchone()
    if not enabled():reason='source_cost_v3_disabled'
    elif withdrawn:reason='withdrawn_physical_source_requires_new_human_grant' 
    elif any(f['target']['coverage_key']==request['coverage_key'] for f in active(ws.conn,request['store_id'],request['period'])):
        reason='existing_physical_source_fact_preserved'
    else:
        try:fact=_candidate(ws,model,request,history_client or HistoryClient.from_env(ws.root))
        except (Pending,SourcePending,HistoryError) as exc:
            reason=getattr(exc,'code',None) or getattr(exc,'reason',None) or getattr(exc,'reason_code',None)
            refs=getattr(exc,'refs',None) or getattr(exc,'artifact_refs',[])
    with _store_lock(request['store_id']),guard(ws.root,request['store_id'],request['period']),ws.transaction() as conn:
        assert_code_integrity()
        old=journal.receipt(conn,key,signature)
        if old:return old
        approved_rule(ws,request['store_id'],request['period'],grant['id'],rule_id=RULE,rule_sha=RULE_SHA)
        if checker.check(ws,request['store_id'],request['period'],model=model)['input_sha']!=request['input_sha']:
            raise Conflict('取证后金融输入已变化，本次没有采用来源事实')
        if fact:
            _target(ws,request);_retained(ws,fact['source_refs'])
            from .source_recovery import verified_fact_source
            fresh=verified_fact_source(ws,model,fact)
            if journal.digest(fresh['source_proof'])!=fact['source_proof_sha']:raise Conflict('采纳前来源证明已变化')
        receipt_key=key if fact else key+':pending:'+journal.ident()
        aid=journal.ident();did=journal.ident() if fact else None
        scope={'approved_rule_grant':grant['id'],'rule_sha':RULE_SHA,'case_id':request['case_id']}
        conn.execute('INSERT INTO automation_authorization VALUES(?,?,?,?,?,?,?,?,?)',
            (aid,receipt_key,actor['id'],'delegated_service',RULE,request['input_sha'],journal.digest(fact),journal.encoded(scope),journal.now()))
        if fact:
            conn.execute('INSERT INTO automation_decision VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (did,key,RULE,request['period'],request['store_id'],'apply','',journal.encoded({'rule_sha':RULE_SHA,'fact':fact}),
                 '独立核验原始物理成员和原日历史价；由原引擎决定实际计入',request['input_sha'],journal.digest(fact),actor['id'],journal.now()))
            conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
                (journal.ident(),request['case_id'],key,request['input_sha'],'dispatch_pending',
                 journal.encoded({'store_id':request['store_id'],'fingerprint':'automation:'+journal.digest([did,request['input_sha']])}),journal.now()))
        result=journal.finish(conn,receipt_key,RULE,signature,aid,{'status':'adopted' if fact else 'pending',
            'decision_id':did,'financial_applied':False,'recompute_required':bool(fact),
            'source_refs':fact['source_refs'] if fact else refs,'reason_codes':[reason] if reason else []})
    if fact:deliver(ws)
    return result


def _adoption_authority(ws,fact,*,frozen_reference=False):
    """Expiry/kill-switch stop NEW adoptions, not established fact consumption."""
    import json
    from .automation_actions import Conflict
    scope=fact['authorization'];grant_id=scope.get('approved_rule_grant')
    grant=ws.conn.execute("""SELECT d.*,a.identity_kind,a.action AS authority_action
        FROM automation_decision d JOIN automation_authorization a ON a.request_id=d.request_id
        WHERE d.id=? AND d.kind='rule_grant' AND d.action='apply'""",(grant_id,)).fetchone()
    if not grant or grant['identity_kind']!='human' or grant['authority_action']!='decision.apply':
        raise Pending('physical_source_human_authority_unverified')
    values=json.loads(grant['values_json'])
    from datetime import datetime
    try:
        stamp=datetime.fromisoformat(fact['adopted_at'])
        if not datetime.fromisoformat(values['valid_from'])<=stamp<datetime.fromisoformat(values['valid_until']):raise ValueError()
    except (ValueError,TypeError,KeyError):raise Pending('physical_source_adoption_outside_grant') from None
    if (grant['store_id']!=fact['store_id'] or values.get('rule_id')!=RULE or values.get('rule_sha')!=RULE_SHA
        or values.get('enabled') is not True
        or not values.get('period_from',grant['period'])<=fact['period']<=values.get('period_until',grant['period'])):
        raise Pending('physical_source_human_authority_unverified')
    # Explicit revocation, unlike natural expiry, is a real financial change.
    if not frozen_reference and ws.conn.execute("SELECT 1 FROM automation_decision WHERE action='revoke' AND ref_id=?",(grant_id,)).fetchone():
        raise Pending('physical_source_grant_withdrawn')


def retained_quotes(ws,fact):
    """Re-run the provider's interval parser against retained exact source bytes."""
    import json
    from pathlib import Path
    from .upstream_history import HistoryClient
    _retained(ws,fact['source_refs'])
    result={}
    for expected in fact['quotes']:
        refs=expected['artifact_refs']
        raw_ref=next((r for r in refs if r['sha256']==expected['source_response_sha']),None)
        if raw_ref is None:raise Pending('physical_quote_original_missing')
        raw=Path(raw_ref['path']).read_bytes()
        class Retained(HistoryClient):
            def _fetch(self,*_):
                return raw,json.loads(raw,parse_float=Decimal,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))['data']
            def _retain(self,*_):return refs
        quote=Retained(ws.root).quote(expected['sku'],expected['company'],expected['pricing_day'])
        if quote_semantic(quote)!=quote_semantic(expected):raise Pending('physical_quote_semantics_changed')
        result[(quote['company'],quote['sku'],quote['pricing_day'])]=quote
    return result


def consume(ws,model,store,ingestion,baseline,publication_scope=None):
    """Only the unchanged engine computes money. No active fact -> no rerun."""
    from .source_cost_posting import render_items,replace_inputs,archive_consumed,merge_frozen_inputs,guard_return_identity
    from .engine.runtime import run
    from .engine.types import Finding
    from .upstream_history import HistoryError
    candidates=active(ws.conn,store.id)
    if not candidates:return baseline
    from .source_recovery import verified_fact_source,SourcePending,proof_semantic
    from .historical_cost_facts import manual_authority
    manual=[dict(r) for r in ws.conn.execute('SELECT * FROM cost_line_log WHERE store_id=? ORDER BY id',(store.id,))]
    protected=manual_authority(ws.conn,ws.root,store.id,{f['period'] for f in candidates},manual) if manual else set()
    pending=[];items=[];anchors=set();physical=set();used_items={};borrowed=[];restored=False
    for fact in candidates:
        period=fact['period'];state=ws.state(store.id,period)
        if state and state.closed:
            dependent=_potential_return_periods(ingestion,store,fact,publication_scope)
            if not dependent:continue
            try:
                guard_return_identity(ingestion,store,fact['members'])
                saved,source=_frozen_basis(ws,model,store,fact)
                merge_frozen_inputs(ingestion,saved);restored=True
                refs=[*fact['source_refs'],*[{'path':r['path'],'sha256':r['sha256']} for r in source['source_inputs']],
                    {'path':str(ws.root/source['frozen_financial_artifact']['path']),'sha256':source['frozen_financial_artifact']['sha']}]
                borrowed.append({**fact,'source_refs':refs,'source_inputs':source['source_inputs'],
                    'frozen_origin_run_id':state.run_id,'frozen_origin_proof_sha':source['frozen_origin_proof_sha'],'dependent_periods':sorted(dependent)})
            except (Pending,SourcePending,HistoryError,OSError,ValueError,KeyError) as exc:
                reason=getattr(exc,'code',None) or getattr(exc,'reason',None) or 'frozen_source_basis_unavailable'
                pending.extend((p,reason) for p in dependent)
            continue
        if publication_scope is not None and not publication_scope.allows(store.id,period):continue
        if (period,fact['target']['coverage_key']) in protected:
            continue  # A valid current human amount has priority.
        try:
            _open(ws,store.id,period,allow_confirmed=True);_adoption_authority(ws,fact)
            fresh=verified_fact_source(ws,model,fact,allow_pending_refresh=True)
            members=selected_members(fresh,fact['target'],store.platform)
            if journal.digest(proof_semantic(members))!=journal.digest(proof_semantic(fact['members'])):raise Pending('physical_source_semantics_changed')
            quotes=retained_quotes(ws,fact)
            made,remove,stock=render_items(ingestion,baseline,store,period,fact['target'],members,quotes,fact_id=fact['decision_id'])
            if physical&stock or anchors&remove:raise Pending('physical_source_fact_overlap')
            items.extend(made);anchors.update(remove);physical.update(stock)
            if made:
                refs={journal.encoded(ref):ref for ref in [*fact['source_refs'],*fresh.get('source_refs',[])]}
                used_items[fact['decision_id']]=({**fact,'source_refs':list(refs.values()),
                    'source_refresh_binding':fresh.get('source_binding')},made)
        except (Pending,SourcePending,HistoryError,OSError,ValueError,KeyError) as exc:
            pending.append((period,getattr(exc,'code',None) or getattr(exc,'reason',None) or getattr(exc,'reason_code',None) or 'physical_source_revalidation_failed'))
    if items:replace_inputs(ingestion,items,anchors,physical)
    result=run(ingestion,store.platform) if items or restored else baseline
    evidence=[]
    for fact,made in used_items.values():
        try:
            captured=archive_consumed(ws,ingestion,result,store,fact,made)
            if captured:
                refs=[*fact['source_refs'],*[{'path':r['path'],'sha256':r['sha256']} for r in captured]]
                evidence.append({**fact,'source_refs':refs,'source_inputs':captured,
                    'close_quote_keys':sorted({tuple(key) for item in captured for key in item.get('quote_keys',[])})})
        except (Pending,OSError,ValueError) as exc:
            evidence.append(fact)  # Keep raw dependencies protected, even on an incomplete freeze archive.
            pending.append((fact['period'],getattr(exc,'code',None) or 'frozen_source_input_archive_failed'))
    ingestion.physical_cost_evidence=[*evidence,*borrowed]
    for period,reason in pending:
        sl=result.slices.get((store.name,period))
        if sl is not None:
            sl.audit.findings.append(Finding('physical_source_fact_pending','物理来源事实待复核',passed=False,blocking=True,
                message='已采用的来源事实未通过当前独立复核，开放账期保持待处理；冻结历史不变',detail={'reason_code':reason}))
    return result

def verify_for_close(ws,model,store_id,period):
    """Local raw revalidation; upstream refresh uses a separate durable barrier."""
    from .source_recovery import verified_fact_source,SourcePending,proof_semantic
    from .upstream_history import HistoryError
    failures=[]
    for fact in used(ws,store_id,period):
        try:
            _adoption_authority(ws,fact)
            fresh=verified_fact_source(ws,model,fact)
            members=selected_members(fresh,fact['target'],model.store(store_id).platform)
            if journal.digest(proof_semantic(members))!=journal.digest(proof_semantic(fact['members'])):raise Pending('physical_source_semantics_changed')
            retained_quotes(ws,fact)
        except (Pending,SourcePending,HistoryError,OSError,ValueError,KeyError) as exc:
            failures.append(getattr(exc,'code',None) or getattr(exc,'reason',None) or getattr(exc,'reason_code',None) or 'physical_source_revalidation_failed')
    return sorted(set(failures))

def run_available(ws,model,period,store_id,*,worker='native:commission-manager'):
    """Bounded, model-free adoption after a durable source task has finished."""
    if not enabled():return []
    from .automation_deterministic import approved_rule
    from .automation_actions import Conflict,sync_month
    from . import automation_checker as checker,cost_lines
    from .source_recovery import verified_source,SourcePending
    from .workspace import WorkspaceError
    try:approved_rule(ws,store_id,period,rule_id=RULE,rule_sha=RULE_SHA)
    except (PermissionError,ValueError):return []
    outcomes=[]
    # No model-loop waits and no repeated scans of unknown raw tasks.
    for _ in range(20):
        sync_month(ws,model,period,worker=worker)
        current=checker.check(ws,store_id,period,model=model)
        if not current['run_id']:break
        existing={f['target']['coverage_key'] for f in active(ws.conn,store_id,period)}
        try:rows=cost_lines.current(ws,current['run_id'],store_id,period)['items']
        except WorkspaceError:break
        selected=None
        for target in rows:
            if target.get('order_count')!=1 or target.get('manual_amount') is not None or target['coverage_key'] in existing:continue
            request={'request_id':journal.digest([RULE,store_id,period,current['input_sha'],target['coverage_key']]),
                'rule_id':RULE,'case_id':journal.digest([store_id,period])[:32],'store_id':store_id,'period':period,
                'run_id':current['run_id'],'input_sha':current['input_sha'],'coverage_key':target['coverage_key'],
                'context_sha':target['context_sha'],'expected_line_revision':target.get('line_revision',0)}
            try:verified_source(ws,model,request)
            except (SourcePending,Conflict):continue
            selected=request;break
        if selected is None:break
        actor={'id':worker,'kind':'service','authenticated':True,'scopes':['business'],'capabilities':['approved_action']}
        try:outcome=apply(ws,model,selected,actor)
        except (Conflict,PermissionError):break
        outcomes.append(outcome)
        if outcome['status']!='adopted':break
    return outcomes


QUOTE_REFRESH='physical_source_quote_refresh.v1'


def _quote_head_id(store_id,period):
    return 'physical-quote-head:'+journal.digest([store_id,period])


def _refresh_facts(ws,period,store_ids):
    return [f for sid in sorted(set(store_ids)) if not (ws.state(sid,period) and ws.state(sid,period).closed) for f in used(ws,sid,period)]


def ensure_quote_refresh(ws,model,period,store_ids,request_id,actor):
    """Create a finite, server-owned refresh; perform no upstream GET here."""
    import json
    from .automation_actions import human,Conflict,_request
    human(actor);_request(request_id);journal.period(period)
    selected=sorted(set(store_ids));facts=_refresh_facts(ws,period,selected)
    if not facts:return {'required':False,'status':'verified','refresh_id':None,'quote_sha':None}
    signature=journal.digest([period,selected,request_id,actor['id']])
    task_id='physical-quote-refresh:'+signature
    facts_sha=journal.digest([fingerprint(ws,sid,period) for sid in selected])
    with ws.transaction() as conn:
        existing=conn.execute('SELECT * FROM automation_attempt WHERE id=?',(task_id,)).fetchone()
        if existing:
            payload=json.loads(existing['result_json'])
            if payload['facts_sha']!=facts_sha:raise Conflict('上游报价刷新期间来源事实已变化，请重新预览')
        else:
            targets={}
            for fact in facts:
                _adoption_authority(ws,fact)
                for quote in fact['quotes']:
                    key=(quote['company'],quote['sku'],quote['pricing_day'])
                    allowed=fact.get('close_quote_keys')
                    if allowed is not None and key not in {tuple(k) for k in allowed}:continue
                    target=targets.setdefault(key,{'company':key[0],'sku':key[1],'pricing_day':key[2],'expected':[]})
                    semantic=quote_semantic(quote)
                    if semantic not in target['expected']:target['expected'].append(semantic)
            active_stores=sorted({f['store_id'] for f in facts})
            payload={'kind':QUOTE_REFRESH,'period':period,'store_ids':selected,'active_store_ids':active_stores,'actor':actor['id'],'facts_sha':facts_sha,
                'started_at':journal.now(),'targets':list(targets.values()),'outcomes':[],'reason_codes':[]}
            conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
                (task_id,'company:'+period,task_id,facts_sha,'physical_quote_refresh_queued',journal.encoded(payload),journal.now()))
            for sid in active_stores:
                head_id=_quote_head_id(sid,period)
                conn.execute("""INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET result_json=excluded.result_json,input_sha=excluded.input_sha,at=excluded.at""",
                    (head_id,journal.digest([sid,period])[:32],head_id,facts_sha,'physical_quote_refresh_head',
                     journal.encoded({'refresh_id':task_id}),journal.now()))
    return quote_refresh_view(ws,task_id)


def quote_refresh_view(ws,refresh_id):
    import json
    from .automation_actions import Conflict
    row=ws.conn.execute('SELECT * FROM automation_attempt WHERE id=?',(refresh_id,)).fetchone()
    if not row:raise Conflict('原日历史价刷新任务不存在')
    payload=json.loads(row['result_json'])
    if payload.get('kind')!=QUOTE_REFRESH:raise Conflict('报价刷新任务类型不匹配')
    status=('verified' if row['state']=='physical_quote_refresh_ready' else
            'processing' if row['state']=='physical_quote_refresh_queued' else 'pending')
    return {'required':True,'status':status,'refresh_id':refresh_id,'quote_sha':journal.digest(payload),
        'completed_quotes':len(payload['outcomes']),'total_quotes':len(payload['targets']),
        'reason_codes':payload['reason_codes'],'capture_started_at':payload['started_at'],
        'capture_finished_at':payload.get('finished_at'),'finite_capture':True}


def process_quote_refresh(ws,model,store_id,period,*,history_client=None):
    """At most ten fixed provider GETs per existing Manager turn."""
    import json
    from .upstream_history import HistoryClient,HistoryError
    from .automation_actions import Conflict
    head=ws.conn.execute('SELECT result_json FROM automation_attempt WHERE id=?',(_quote_head_id(store_id,period),)).fetchone()
    if not head:return
    task_id=json.loads(head[0])['refresh_id']
    row=ws.conn.execute("SELECT * FROM automation_attempt WHERE id=? AND state='physical_quote_refresh_queued'",(task_id,)).fetchone()
    if not row:return
    payload=json.loads(row['result_json'])
    if journal.digest([fingerprint(ws,sid,period) for sid in payload['store_ids']])!=payload['facts_sha']:
        payload['reason_codes']=['quote_refresh_financial_facts_changed'];state='physical_quote_refresh_pending'
    else:
        state='physical_quote_refresh_queued';history=history_client
        for target in payload['targets'][len(payload['outcomes']):len(payload['outcomes'])+10]:
            try:
                if len(target['expected'])!=1:raise Pending('adopted_quote_intervals_conflict')
                if history is None:history=HistoryClient.from_env(ws.root)
                quote=history.quote(target['sku'],target['company'],target['pricing_day'])
                _retained(ws,quote['artifact_refs'])
                if quote_semantic(quote)!=target['expected'][0]:raise Pending('physical_source_quote_changed',quote['artifact_refs'])
                payload['outcomes'].append({'company':target['company'],'sku':target['sku'],'pricing_day':target['pricing_day'],
                    'quote':quote,'status':'verified'})
            except (Pending,HistoryError,OSError,ValueError) as exc:
                reason=getattr(exc,'code',None) or getattr(exc,'reason_code',None) or 'quote_refresh_provider_unavailable'
                payload['reason_codes']=[reason]
                payload['outcomes'].append({'company':target['company'],'sku':target['sku'],'pricing_day':target['pricing_day'],
                    'status':'pending','reason_code':reason,
                    'source_refs':getattr(exc,'refs',None) or getattr(exc,'artifact_refs',[])})
                state='physical_quote_refresh_pending'
                break
        if state=='physical_quote_refresh_queued' and len(payload['outcomes'])==len(payload['targets']):
            state='physical_quote_refresh_ready'
    if state!='physical_quote_refresh_queued':payload['finished_at']=journal.now()
    with ws.transaction() as conn:
        latest=conn.execute('SELECT result_json,state FROM automation_attempt WHERE id=?',(task_id,)).fetchone()
        if not latest or latest['state']!='physical_quote_refresh_queued' or journal.digest(json.loads(latest['result_json']))!=journal.digest(json.loads(row['result_json'])):
            return  # Another Manager generation made progress; no stale overwrite.
        if payload['facts_sha']!=journal.digest([fingerprint(ws,sid,period) for sid in payload['store_ids']]):
            state='physical_quote_refresh_pending'
            payload['reason_codes']=['quote_refresh_financial_facts_changed'];payload['finished_at']=journal.now()
        conn.execute('UPDATE automation_attempt SET state=?,result_json=? WHERE id=?',(state,journal.encoded(payload),task_id))


def verify_quote_refresh(ws,model,period,store_ids,refresh_id,quote_sha):
    """Confirm a proposal's exact completed finite capture, with no HTTP."""
    import json
    from .automation_actions import Conflict
    selected=sorted(set(store_ids));facts=_refresh_facts(ws,period,selected)
    if not facts:
        if refresh_id is not None or quote_sha is not None:raise Conflict('报价刷新范围已变化')
        return True
    view=quote_refresh_view(ws,refresh_id)
    if view['status']!='verified' or view['quote_sha']!=quote_sha:raise Conflict('本批报价上游刷新尚未通过')
    row=ws.conn.execute('SELECT result_json FROM automation_attempt WHERE id=?',(refresh_id,)).fetchone()
    payload=json.loads(row[0])
    if payload['period']!=period or payload['store_ids']!=selected or payload['facts_sha']!=journal.digest([fingerprint(ws,sid,period) for sid in selected]):
        raise Conflict('报价刷新后来源事实范围已变化')
    for sid in payload.get('active_store_ids',selected):
        head=ws.conn.execute('SELECT result_json FROM automation_attempt WHERE id=?',(_quote_head_id(sid,period),)).fetchone()
        if not head or json.loads(head[0])['refresh_id']!=refresh_id:raise Conflict('报价刷新代际已变化')
    for outcome in payload['outcomes']:
        _retained(ws,outcome['quote']['artifact_refs'])
    return True


def quote_close_reasons(ws,store_id,period):
    """Cheap readiness; a local old raw quote is not a new provider capture."""
    import json
    if not used(ws,store_id,period):return []
    head=ws.conn.execute('SELECT result_json FROM automation_attempt WHERE id=?',(_quote_head_id(store_id,period),)).fetchone()
    if not head:return ['physical_quote_refresh_required']
    task_id=json.loads(head[0])['refresh_id']
    try:
        view=quote_refresh_view(ws,task_id)
        row=ws.conn.execute('SELECT result_json FROM automation_attempt WHERE id=?',(task_id,)).fetchone()
        payload=json.loads(row[0])
        if payload['facts_sha']!=journal.digest([fingerprint(ws,sid,period) for sid in payload['store_ids']]):
            return ['physical_quote_refresh_facts_changed']
        if view['status']=='verified':return []
        return view['reason_codes'] or ['physical_quote_refresh_pending']
    except (ValueError,KeyError):return ['physical_quote_refresh_unverified']


def process_one_quote_refresh(ws,model,publication_scope):
    """One bounded technical task; never rerun the financial engine for progress."""
    import json
    rows=ws.conn.execute("SELECT * FROM automation_attempt WHERE state='physical_quote_refresh_queued' ORDER BY rowid").fetchall()
    for row in rows:
        payload=json.loads(row['result_json'])
        active_stores=payload.get('active_store_ids',payload['store_ids'])
        if not active_stores:continue
        if not all(publication_scope.allows(sid,payload['period']) for sid in active_stores):continue
        current=True
        for sid in active_stores:
            head=ws.conn.execute('SELECT result_json FROM automation_attempt WHERE id=?',(_quote_head_id(sid,payload['period']),)).fetchone()
            if not head or json.loads(head[0])['refresh_id']!=row['id']:current=False;break
        if not current:
            payload['reason_codes']=['quote_refresh_generation_superseded'];payload['finished_at']=journal.now()
            with ws.transaction() as conn:
                conn.execute("UPDATE automation_attempt SET state='physical_quote_refresh_pending',result_json=? WHERE id=? AND state='physical_quote_refresh_queued'",
                    (journal.encoded(payload),row['id']))
            continue
        process_quote_refresh(ws,model,active_stores[0],payload['period'])
        return True
    return False


def used(ws,store_id,period):
    """Select only facts actually used by the current financial run proof."""
    import json
    from .automation_checker import current_run
    state=ws.conn.execute('SELECT state,run_id FROM period WHERE store_id=? AND period=?',(store_id,period)).fetchone()
    row=({'id':state['run_id']} if state and state['state']=='closed' else current_run(ws,store_id,period))
    if not row:return []
    proof=ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(row['id'],)).fetchone()
    if not proof:return []
    manifest=json.loads(proof[0])
    sources={r.get('decision_id'):r for r in manifest.get('approved_cost_sources',[]) if r.get('rule_id')==RULE}
    return [{**f,'close_quote_keys':sources[f['decision_id']].get('close_quote_keys')} for f in active(ws.conn,store_id,period) if f['decision_id'] in sources]


def _potential_return_periods(ingestion,store,fact,publication_scope):
    from .engine.cost_returns import day,number
    import polars as pl
    if getattr(store,'cost_return_posting',None)!='transaction':return set()
    ids={str(m['internal_order_id']) for m in fact['members']}|{str(m['source_order_id']) for m in fact['members']}
    result=set()
    for item in ingestion.frames_of('after_sales'):
        if 'internal_order_id' not in item.frame.columns:continue
        for row in item.frame.filter(pl.col('internal_order_id').cast(pl.String).is_in(ids)).to_dicts():
            if row.get('refund_status')!='退款成功' or row.get('goods_status') not in {'买家未收到货','买家已退货','卖家已收到退货'}:continue
            if number(row.get('returned_quantity'))==0:continue
            stamp=day(row.get('settle_date')) or day(row.get('return_time')) or day(row.get('received_time'))
            if stamp:result.add(stamp.strftime('%Y-%m'))
            elif publication_scope is not None:result.update(publication_scope.periods_for_store(store.id))
    return {p for p in result if p>fact['period'] and (publication_scope is None or publication_scope.allows(store.id,p))}


def _frozen_basis(ws,model,store,fact):
    import json
    from .automation_period_proof import valid_artifact
    from .source_cost_posting import frozen_inputs
    state=ws.state(store.id,fact['period'])
    if not state or not state.closed:raise Pending('origin_period_not_frozen')
    run=ws.conn.execute('SELECT * FROM run WHERE id=?',(state.run_id,)).fetchone()
    proof=ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(state.run_id,)).fetchone()
    if not run or not proof:raise Pending('frozen_source_run_proof_missing')
    manifest=json.loads(proof[0])
    source=next((r for r in manifest.get('approved_cost_sources',[]) if r.get('rule_id')==RULE and r.get('decision_id')==fact['decision_id']),None)
    if not source:raise Pending('frozen_source_not_actually_used')
    main=ws.facts_path(state.run_id).resolve().relative_to(ws.root.resolve()).as_posix()
    financial=next((a for a in manifest.get('artifacts',[]) if a.get('path')==main),None)
    if not financial:raise Pending('frozen_counted_cost_archive_unverified')
    for artifact in [financial,*source.get('artifacts',[])]:
        if not valid_artifact(ws,artifact):raise Pending('frozen_source_run_artifact_changed')
    source={**source,'frozen_financial_artifact':financial,'frozen_origin_proof_sha':journal.digest(manifest)}
    _adoption_authority(ws,fact,frozen_reference=True)
    items=frozen_inputs(ws,store,fact,run,source)
    return items,source
