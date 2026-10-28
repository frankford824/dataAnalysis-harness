"""Approved single-component historical cost evidence -> existing cost-line log.

Service payloads select a sealed gap. Amount, quantity, dates and origins are
never accepted from a model. Both upstream identities and the dated quote are
re-read using server configuration before the short atomic application phase.
"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime,timezone
from decimal import Decimal,DecimalException,Inexact,InvalidOperation,Rounded,Underflow,localcontext
import hashlib
import json
from pathlib import Path
import re
import time
import os
import math

import polars as pl

from . import automation_store as journal,automation_checker as checker,cost_lines
from .automation_actions import Conflict,_request
from .automation_deterministic import approved_rule,enabled
from .finance_guard import guard
from .storage_integrity import verified
from .storage_integrity import digest as file_digest
from .upstream_history import HistoryClient,HistoryError
from .workspace import WorkspaceError
from .order_feed import OrderFeedError

RULE='history_cost_single_component.v1'
RULE_CONTRACT={'id':RULE,'scope':'one current sales-order gap, one ERP component, confirmed store/company/original day',
    'quantity':'sealed ERP quantity must equal the re-read exact item quantity; no unit conversion',
    'money':'positive dated history quote × proven quantity must already be exact cents; never round an individual source amount',
    'exclusions':'partial cost, reship, merge, kit conversion, gift, conflicting identities and existing decisions',
    'authority':'source evidence supplement only; no close, reopening, payout or policy approval'}
RULE_SHA=journal.digest(RULE_CONTRACT)


class Pending(Exception):
    def __init__(self,code,refs=()):self.code=code;self.refs=list(refs)


def _day(value):
    from datetime import date
    text=str(value or '')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:[ T].*)?',text):return None
    try:return date.fromisoformat(text[:10]).isoformat()
    except ValueError:return None


def _positive(value):
    try:
        result=Decimal(str(value))
        return result if result.is_finite() and result>0 else None
    except (InvalidOperation,ValueError):return None


def _id(value):
    text=str(value or '')
    return text if re.fullmatch(r'[1-9]\d{0,18}',text) and int(text)<2**63 else None


def _retain(ws,source,payload):
    # Content-address the canonical source payload itself. Capture metadata is
    # retained in the immutable attempt/decision reference, so repeated source
    # outages do not duplicate a full store directory merely for a new clock.
    raw=journal.encoded(payload).encode();sha=hashlib.sha256(raw).hexdigest()
    folder=ws.root/'automation-evidence'
    if folder.is_symlink():raise Pending('source_evidence_path_invalid')
    folder.mkdir(parents=True,exist_ok=True,mode=0o700);folder.chmod(0o700)
    path=folder/(sha+'.json')
    if path.is_symlink():raise Pending('source_evidence_path_invalid')
    if not path.exists():
        with path.open('xb') as stream:stream.write(raw)
        path.chmod(0o600)
    elif file_digest(path)!=sha:raise Pending('source_evidence_integrity_failed')
    return {'path':str(path),'sha256':sha,'source':source,'captured_at':journal.now(),'encoding':'canonical_json'}


def _gap(ws,request,*,source_fact=False):
    rid=request['run_id'];sid=request['store_id'];period=request['period']
    row=ws.latest_run(sid,period);state=ws.state(sid,period)
    if not row or row['id']!=rid or not state or state.closed:raise Conflict('当前店月或原始核算已变化')
    if not row['evidence_ready'] or row['evidence_error']:raise Pending('run_evidence_unavailable')
    if source_fact:
        manual=ws.conn.execute('SELECT result_json FROM manual_finance WHERE run_id=?',(rid,)).fetchone()
        if manual and json.loads(manual['result_json']).get('manual_cost'):raise Pending('existing_period_manual_cost_preserved')
    captured=ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(rid,)).fetchone()
    if not captured:raise Pending('run_input_capture_unverified')
    from .automation_period_proof import valid_artifact
    artifacts={a['path']:a for a in json.loads(captured['manifest_json']).get('artifacts',[])}
    for source_path in (ws.pricing_gaps_path(rid),ws.coverage_gaps_path(rid)):
        relative=source_path.relative_to(ws.root).as_posix()
        if relative not in artifacts or not valid_artifact(ws,artifacts[relative]):
            raise Pending('pricing_or_coverage_capture_invalid')
    coverage_rows=cost_lines._source_rows(ws,rid,for_edit=True)
    coverage=next((g for g in coverage_rows if g['coverage_key']==request['coverage_key']),None)
    if coverage and coverage.get('partial_cost') and not source_fact:raise Pending('partial_cost_requires_component_reconciliation')
    path=ws.pricing_gaps_path(rid)
    if not path.is_file() or not verified(path):raise Pending('pricing_evidence_unavailable')
    frame=pl.read_parquet(path)
    required={'metric_id','coverage_key','internal_order_id','internal_sub_order_id','sku','quantity','order_date','cost_company_id','store','period'}
    if not required<=set(frame.columns):raise Pending('pricing_identity_contract_incomplete')
    if source_fact and request['coverage_key'].startswith('component:'):
        from .historical_cost_facts import source_gap
        coverage,gap=source_gap(frame,request,coverage_rows)
    else:
        if not coverage or coverage['context_sha']!=request['context_sha'] or coverage['order_count']!=1:raise Pending('coverage_identity_unproved')
        rows=frame.filter((pl.col('metric_id')=='goods_cost')&(pl.col('coverage_key')==request['coverage_key'])
            &(pl.col('period')==period)).to_dicts()
        if len(rows)!=1:raise Pending('single_component_not_proved')
        gap=rows[0]
    if coverage.get('partial_cost') and not source_fact:raise Pending('partial_cost_requires_component_reconciliation')
    latest=cost_lines._latest(ws,sid,period,rid).get(coverage.get('manual_coverage_key') or request['coverage_key'])
    if (latest['id'] if latest else 0)!=request['expected_line_revision']:raise Conflict('成本决定版本已变化')
    if latest:
        effective=(any(r['coverage_key']==(coverage.get('manual_coverage_key') or request['coverage_key'])
            for r in cost_lines.current(ws,rid,sid,period)['reviewed']) if source_fact else True)
        if effective:raise Pending('existing_cost_decision_preserved')
    from .historical_cost_facts import active as adopted_facts
    if any((f['coverage_key'],f['context_sha'])==(request['coverage_key'],request['context_sha']) for f in adopted_facts(ws.conn,sid,period)):
        raise Pending('existing_history_fact_preserved')
    return row,coverage,gap


def _prove(ws,model,request,order_client,history_client,*,source_fact=False):
    row,coverage,gap=_gap(ws,request,source_fact=source_fact)
    store=model.store(request['store_id'])
    if gap['store'] not in (store.id,store.name,*store.aliases):raise Pending('pricing_store_conflict')
    internal,item_id=_id(gap['internal_order_id']),_id(gap['internal_sub_order_id'])
    qty=_positive(gap['quantity']);sku=str(gap.get('sku') or '')
    day=_day(gap.get('source_pricing_day')) or _day(gap.get('order_date'))
    company=str(gap.get('cost_company_id') or '')
    if not all((internal,item_id,qty,sku,day,company)):raise Pending('original_identity_quantity_or_day_missing')
    if _day(gap.get('source_pricing_day')) and _day(gap.get('order_date'))!=day:raise Pending('original_pricing_day_conflict')
    stores=order_client.get('stores')
    refs=[_retain(ws,'order-os:stores',stores)]
    if (not isinstance(stores,dict) or not isinstance(stores.get('stores'),list)
        or any(not isinstance(s,dict) for s in stores['stores'])):raise Pending('store_source_contract_invalid',refs)
    mapping=[s for s in stores.get('stores',[]) if s.get('ledger_store_id')==store.id
        and s.get('mapping_status')=='confirmed' and s.get('consumable') is True]
    detail=order_client.get('orders/'+internal);refs.append(_retain(ws,'order-os:order',detail))
    if (not isinstance(detail,dict) or not isinstance(detail.get('order'),dict)
        or not isinstance(detail.get('items'),list) or any(not isinstance(i,dict) for i in detail['items'])):
        raise Pending('order_source_contract_invalid',refs)
    order=detail.get('order') or {}
    if str(order.get('order_id'))!=internal or str(order.get('order_store_id')) not in {str(s.get('order_store_id')) for s in mapping}:
        raise Pending('confirmed_store_or_order_identity_missing',refs)
    if source_fact:
        from .historical_cost_facts import live_status
        if live_status(order)!='Sent':raise Pending('live_sale_status_unproved',refs)
    reship=source_fact and gap['metric_id']=='reshipment_cost' and order.get('order_type') in {'补发订单','补发'}
    if order.get('is_merge') or order.get('is_split') or (order.get('order_type') not in {'普通订单','销售订单','normal','sale'} and not reship):
        raise Pending('reship_merge_or_split_not_supported',refs)
    members=order.get('platform_order_ids')
    if (order.get('platform_order_identity_complete') is not True or not isinstance(members,list)
        or len(set(map(str,members)))!=1 or (not reship and str(coverage.get('order_id') or '') not in set(map(str,members)))):
        raise Pending('platform_parent_identity_unproved',refs)
    items=[i for i in detail.get('items',[]) if str(i.get('sub_order_id'))==item_id and str(i.get('sku_id'))==sku]
    if len(items)!=1:raise Pending('single_live_component_unproved',refs)
    item=items[0]
    suspect=item.get('is_suspect') is True
    if suspect and source_fact:
        from .historical_cost_facts import quantity_admitted
        if not quantity_admitted(order,item,item.get('cost'),qty):raise Pending('legacy_quantity_evidence_unverified',refs)
        if gap.get('legacy_quantity_guard_pending') is not False:raise Pending('captured_quantity_evidence_unverified',refs)
        suspect=False
    if (item.get('is_virtual') is True or item.get('is_gift') is True or suspect
        or str(item.get('sku_type') or '').lower() not in {'normal','普通','普通商品'}
        or item.get('src_combine_sku_id') not in (None,'',sku)):
        raise Pending('component_or_unit_conversion_unproved',refs)
    live_company=str(item.get('cost_company_id') or '')
    if source_fact:
        from .historical_cost_facts import company_identity
        live_company=company_identity(item,item.get('cost'))
    if (_positive(item.get('quantity'))!=qty or _day(item.get('original_order_date'))!=day
        or live_company!=company):raise Pending('live_quantity_day_or_company_conflict',refs)
    child=str(coverage.get('sub_order_id') or '')
    if child and str(item.get('platform_sub_order_id') or '')!=child:raise Pending('platform_child_identity_unproved',refs)
    origin=None
    if reship:
        from .historical_cost_facts import original_reship_member
        origin=original_reship_member(ws,request,detail,coverage,gap,order_client,refs)
    try:quote=history_client.quote(sku,company,day)
    except HistoryError as exc:
        exc.artifact_refs=[*refs,*exc.artifact_refs]
        raise
    unit=_positive(quote.get('unit_cost'))
    if not unit or str(quote.get('sku'))!=sku or str(quote.get('company'))!=company or quote.get('pricing_day')!=day:
        raise Pending('trusted_history_quote_unbound',refs)
    refs.extend(quote.get('artifact_refs') or [])
    if not quote.get('quote_sha') or not quote.get('artifact_refs'):raise Pending('trusted_history_evidence_not_retained',refs)
    evaluation={**{key:request[key] for key in ('case_id','store_id','period','run_id','coverage_key','context_sha','input_sha','expected_line_revision')},
        'rule_id':RULE,'rule_sha':RULE_SHA,'quantity':str(qty),'unit_cost':str(unit),
        'pricing_day':day,'sku':sku,'company':company,'quote_sha':quote['quote_sha'],'financial_applied':False}
    if source_fact:
        from .historical_cost_facts import RULE as facts_rule,RULE_SHA as facts_sha
        evaluation.update(rule_id=facts_rule,rule_sha=facts_sha)
    try:
        with localcontext() as context:
            context.prec=max(50,len(qty.as_tuple().digits)+len(unit.as_tuple().digits))
            context.traps[Inexact]=context.traps[Rounded]=context.traps[Underflow]=True
            exact=qty*unit
        evaluation['exact_amount']=str(exact)
        if source_fact:
            # v2 is a separately authorized source-fact contract. No financial
            # amount is posted here; the original adapter/engine consumes the
            # original unit and quantity using its existing arithmetic policy.
            return {'quantity':str(qty),'unit_cost':str(unit),'exact_amount':str(exact),
                'pricing_day':day,'sku':sku,'company':company,'quote_sha':quote['quote_sha'],
                'source_refs':refs,'coverage':coverage,'gap':gap,'live_order':order,'live_item':item,'live_components':detail['items'],'origin':origin}
        # Quantization is only an exact-cents test, never a posting rounding
        # policy. It must classify ordinary fractional cents without trapping.
        with localcontext() as context:
            context.prec=max(50,len(qty.as_tuple().digits)+len(unit.as_tuple().digits))
            context.traps[Inexact]=context.traps[Rounded]=context.traps[Underflow]=False
            amount=exact.quantize(Decimal('.01'))
    except DecimalException as exc:
        evaluation.update(reason_code='historical_amount_precision_unproved',arithmetic_error=type(exc).__name__)
        refs.append(_retain(ws,'derived:history-cost-exact-evaluation',evaluation))
        raise Pending('historical_amount_precision_unproved',refs) from None
    if exact!=amount:
        evaluation['reason_code']='historical_amount_not_exact_cents'
        refs.append(_retain(ws,'derived:history-cost-exact-evaluation',evaluation))
        raise Pending('historical_amount_not_exact_cents',refs)
    if amount<=0:raise Pending('historical_amount_not_positive',refs)
    return {'amount':amount,'quantity':str(qty),'unit_cost':str(unit),'exact_amount':str(exact),
        'pricing_day':day,'sku':sku,'company':company,'quote_sha':quote['quote_sha'],'source_refs':refs,'coverage':coverage,'gap':gap}


def _pending(ws,request,actor,grant,code,refs=(),*,native_retry=False):
    # Inspection attempts are append-only but never occupy the successful
    # idempotency key. The same bound request can retry when a source recovers.
    key='approved-pending:'+request['request_id']+':'+journal.ident()
    with ws.transaction() as conn:
        aid=journal.ident()
        conn.execute('INSERT INTO automation_authorization VALUES(?,?,?,?,?,?,?,?,?)',
            (aid,key,actor['id'],'delegated_service',RULE,request['input_sha'],'',
             journal.encoded({'approved_rule_grant':grant['id'],'rule_sha':RULE_SHA}),journal.now()))
        result={'status':'pending','reason_codes':[code],'source_refs':list(refs),'recompute_required':False,
            'scope':{field:request[field] for field in ('case_id','store_id','period','run_id','coverage_key','context_sha','input_sha','expected_line_revision')}}
        conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
            (journal.ident(),request['case_id'],key,request['input_sha'],'pending_evidence',journal.encoded(result),journal.now()))
        result=journal.finish(conn,key,RULE,journal.digest(request),aid,result)
        if native_retry and (code.startswith('history_source_') or code in {
            'upstream_evidence_unavailable','history_price_not_covering_original_day'}):
            conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
                (journal.ident(),request['case_id'],key,request['input_sha'],'dispatch_pending',
                 journal.encoded({'store_id':request['store_id'],
                     'fingerprint':'automation:'+journal.digest(['source-retry',result['receipt_id']]),
                     'not_before':int(time.time())+300}),journal.now()))
        return result


def apply(ws,model,request,actor,*,order_client=None,history_client=None,native_retry=False):
    from .version import assert_code_integrity
    assert_code_integrity()
    if (not actor or not actor.get('id') or actor.get('kind')!='service' or not actor.get('authenticated')
        or 'business' not in actor.get('scopes',[]) or 'approved_action' not in actor.get('capabilities',[])):
        raise PermissionError('需要已授权的确定性业务服务身份')
    if request.get('rule_id')!=RULE:raise Conflict('未知确定性成本规则')
    _request(request.get('request_id'));journal.period(request['period'])
    key='approved-action:'+request['request_id'];signature=journal.digest(request)
    withdrawn=ws.conn.execute("SELECT 1 FROM automation_decision r JOIN automation_decision d ON d.id=r.ref_id WHERE r.action='revoke' AND d.request_id=?",(key,)).fetchone()
    if withdrawn:raise Conflict('该凭据决定已被人工撤回，旧请求不能重新应用')
    old=journal.receipt(ws.conn,key,signature)
    if old:return old
    grant=approved_rule(ws,request['store_id'],request['period'],rule_id=RULE,rule_sha=RULE_SHA)
    current=checker.check(ws,request['store_id'],request['period'],model=model)
    if current['input_sha']!=request['input_sha']:raise Conflict('案件输入已变化，请按新版本重新取证')
    case=ws.conn.execute('SELECT * FROM automation_case WHERE id=?',(request['case_id'],)).fetchone()
    if not case or case['store_id']!=request['store_id'] or case['period']!=request['period'] or case['input_sha']!=request['input_sha']:
        raise Conflict('案件与店月输入绑定已变化')
    if not enabled():return _pending(ws,request,actor,grant,'automation_disabled')
    try:
        if order_client is None:
            from .order_feed import Client
            timeout=float(os.environ.get('LEDGER_AUTOMATION_SOURCE_TIMEOUT','15'))
            if not math.isfinite(timeout) or timeout<=0:raise Pending('upstream_configuration_invalid')
            order_client=Client(timeout=timeout)
        history_client=history_client or HistoryClient.from_env(ws.root)
        proof=_prove(ws,model,request,order_client,history_client)
    except (Pending,HistoryError) as exc:
        return _pending(ws,request,actor,grant,getattr(exc,'code',None) or getattr(exc,'reason_code','history_unavailable'),
            getattr(exc,'refs',None) or getattr(exc,'artifact_refs',[]),native_retry=native_retry)
    except (OSError,ValueError,OrderFeedError,pl.exceptions.PolarsError) as exc:
        return _pending(ws,request,actor,grant,'upstream_evidence_unavailable',native_retry=native_retry)
    from .service import _store_lock
    with _store_lock(request['store_id']),guard(ws.root,request['store_id'],request['period']),ws.transaction() as conn:
        assert_code_integrity()
        old=journal.receipt(conn,key,signature)
        if old:return old
        approved_rule(ws,request['store_id'],request['period'],grant['id'],rule_id=RULE,rule_sha=RULE_SHA)
        if checker.check(ws,request['store_id'],request['period'],model=model)['input_sha']!=request['input_sha']:
            raise Conflict('取证后输入已变化，本次未补录')
        _gap(ws,request)
        for reference in proof['source_refs']:
            path=Path(reference['path'])
            if not path.is_file() or file_digest(path)!=reference['sha256']:
                raise Conflict('上游原始凭据留档校验失败，本次未补录')
        aid=journal.ident()
        conn.execute('INSERT INTO automation_authorization VALUES(?,?,?,?,?,?,?,?,?)',
            (aid,key,actor['id'],'delegated_service',RULE,request['input_sha'],journal.digest(proof),
             journal.encoded({'approved_rule_grant':grant['id'],'rule_sha':RULE_SHA,'case_id':request['case_id']}),journal.now()))
        by=actor['id'] if actor['id'].startswith('service:') else 'service:'+actor['id']
        reason='已验证历史凭据：原始日/SKU/公司/ERP数量精确一致；报价证据 '+proof['quote_sha']
        cost_lines.save(ws,request['store_id'],request['period'],request['run_id'],coverage_key=request['coverage_key'],
            context_sha=request['context_sha'],amount=proof['amount'],reason=reason,
            expected_line_revision=request['expected_line_revision'],by=by)
        line=conn.execute('SELECT max(id) FROM cost_line_log WHERE store_id=? AND period=? AND coverage_key=?',
            (request['store_id'],request['period'],request['coverage_key'])).fetchone()[0]
        did=journal.ident()
        conn.execute('INSERT INTO automation_decision VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (did,key,RULE,request['period'],request['store_id'],'apply','',journal.encoded({**proof,'amount':str(proof['amount']),'line_log_id':line}),
             reason,request['input_sha'],journal.digest(proof),by,journal.now()))
        conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
            (journal.ident(),request['case_id'],key,request['input_sha'],'dispatch_pending',
             journal.encoded({'store_id':request['store_id'],'fingerprint':'automation:'+journal.digest([did,request['input_sha']])}),journal.now()))
        result=journal.finish(conn,key,RULE,signature,aid,{'status':'applied','decision_id':did,'source_refs':proof['source_refs'],
            'recompute_required':True,'case_id':request['case_id'],'run_id':request['run_id']})
    from .automation_actions import deliver
    deliver(ws)
    return result


def run_available(ws,model,period,store_id,*,worker='native:commission-manager'):
    from .automation_source_cost_v3 import run_available as physical_sources
    physical=physical_sources(ws,model,period,store_id,worker=worker)
    if physical:return physical
    if not enabled():return []
    from .historical_cost_facts import run_available as source_facts,granted as source_facts_granted
    if source_facts_granted(ws,store_id,period):
        return source_facts(ws,model,period,store_id,worker=worker)
    try:approved_rule(ws,store_id,period,rule_id=RULE,rule_sha=RULE_SHA)
    except (PermissionError,ValueError):return []
    from .automation_handoff import active
    current_input=checker.check(ws,store_id,period,model=model)['input_sha']
    if any(wait['source_kind'] in {'order','history_cost','seller_member'}
        for wait in active(ws,model,store_id,period,current_input)):return []
    from .automation_actions import sync_month
    results=[];inspected=0
    actor={'id':worker,'kind':'service','authenticated':True,'scopes':['business'],'capabilities':['approved_action']}
    history=None
    for _ in range(20):
        sync_month(ws,model,period,worker=worker)
        current=checker.check(ws,store_id,period,model=model)
        if not current['run_id']:break
        try:rows=cost_lines.current(ws,current['run_id'],store_id,period)['items']
        except WorkspaceError:break
        candidates=[r for r in rows if not r.get('partial_cost') and r.get('order_count')==1 and r.get('manual_amount') is None]
        if not candidates:break
        applied=False
        for gap in candidates:
            if inspected>=20:break
            inspected+=1
            case_id=journal.digest([store_id,period])[:32]
            request={'request_id':journal.digest([case_id,current['input_sha'],gap['coverage_key'],RULE]),'rule_id':RULE,
                'case_id':case_id,'store_id':store_id,'period':period,'run_id':current['run_id'],
                'coverage_key':gap['coverage_key'],'context_sha':gap['context_sha'],
                'expected_line_revision':gap['line_revision'],'input_sha':current['input_sha']}
            try:result=apply(ws,model,request,actor,history_client=history,native_retry=True)
            except (Conflict,PermissionError):continue
            results.append(result)
            if result.get('status')=='applied':applied=True;break
            if any(c.startswith('history_source_') or c in {'upstream_evidence_unavailable','history_price_not_covering_original_day'}
                for c in result.get('reason_codes',[])):break
        if not applied or inspected>=20:break
    return results
