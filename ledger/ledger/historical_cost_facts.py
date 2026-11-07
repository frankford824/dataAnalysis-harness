"""Explicit v2: trusted dated unit-price facts, never manual monetary postings."""
from __future__ import annotations
from contextlib import closing
from copy import copy
from decimal import Decimal
import json
import math
from pathlib import Path
import sqlite3

import polars as pl

from . import automation_store as journal, automation_checker as checker, cost_lines
from .automation_actions import Conflict, _request
from .automation_deterministic import approved_rule, enabled
from .automation_cost_actions import Pending, _gap, _prove, _positive, _day, _id, _retain
from .finance_guard import guard
from .storage_integrity import digest as file_digest
from .upstream_history import HistoryClient, HistoryError
from .order_feed import OrderFeedError
from .workspace import WorkspaceError

RULE='history_cost_source_facts.v2'
RULE_CONTRACT={'id':RULE,
    'scope':'up to 100 sealed component gaps in one open store/month/run; normal components or independently bound original reship members',
    'quantity':'immutable gap, current ERP item, company, original day, parent/member and raw quantity must agree; no unit conversion',
    'money':'retain original dated quote and quantity without per-line rounding; original cost adapter and engine arithmetic/aggregation apply',
    'exclusions':'existing source price/manual authority; partial totals without exact component inventory; unproved original reship member or kit unit conversion; merge/split/gift/identity conflicts',
    'authority':'source facts only; distinct v2 human grant; no financial close, reopening, payout or policy approval'}
RULE_SHA=journal.digest(RULE_CONTRACT)


def live_status(order):
    # Published live API uses status_code; Parquet/legacy adapters use raw.
    if 'status_code' in order:
        value=order['status_code']
        return value if order.get('order_status_raw') in (None,'',value) else None
    return order.get('order_status_raw')


def quantity_admitted(order,item,cost,quantity):
    """Exactly the current cost-frame placeholder validator, not a new waiver."""
    from .cost_input_evidence import verified_placeholder
    evidence=cost.get('pricing_evidence') if isinstance(cost,dict) else None
    if isinstance(evidence,dict):evidence=journal.encoded(evidence)
    return verified_placeholder({'order_id':str(order.get('order_id') or ''),
        'sub_order_id':str(item.get('sub_order_id') or ''),'sku_id':str(item.get('sku_id') or ''),
        'quantity':quantity,'pricing_evidence':evidence,'__current_item_sku':str(item.get('sku_id') or ''),
        '__current_item_quantity':item.get('quantity'),'__current_sale_price':item.get('unit_price'),
        '__current_order_hash':order.get('content_hash'),'__identity_changed':False})


def company_identity(item,cost):
    """The two published fields: raw item company and cost evidence company."""
    evidence=cost.get('pricing_evidence') if isinstance(cost,dict) else None
    try:
        evidence=json.loads(evidence) if isinstance(evidence,str) and evidence else evidence or {}
        if not isinstance(evidence,dict):return None
    except (ValueError,TypeError):return None
    values=[]
    for value in (item.get('cost_company_id'),evidence.get('cost_company_id')):
        if value in (None,''):continue
        parsed=_id(value)
        if parsed is None:return None
        values.append(parsed)
    return values[0] if values and len(set(values))==1 else None


def manual_authority(conn,root,store_id,periods,manual_rows):
    """Use cost_lines.current's actual save/context/remove semantics, read-only."""
    from types import SimpleNamespace
    from .run_artifacts import path as artifact_path
    from .storage_integrity import verified
    effective=set()
    for period in periods:
        saves=[r for r in manual_rows if r['period']==period and r['action']=='save']
        if not saves:continue
        state=conn.execute('SELECT state,run_id FROM period WHERE store_id=? AND period=?',(store_id,period)).fetchone()
        row=(conn.execute('SELECT id FROM run WHERE id=?',(state['run_id'],)).fetchone() if state and state['state']=='closed' else
             conn.execute('SELECT id FROM run WHERE store_id=? AND period=? ORDER BY id DESC LIMIT 1',(store_id,period)).fetchone())
        if not row:continue
        run_id=row['id'];source=artifact_path(Path(root),run_id).with_suffix('.coverage.parquet')
        proof=conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(run_id,)).fetchone()
        try:
            expected=next(a['sha'] for a in json.loads(proof['manifest_json'])['artifacts'] if a['path'].endswith(source.name)) if proof else None
            if not expected or not verified(source) or file_digest(source)!=expected:raise WorkspaceError('人工成本上下文尚未验证')
            view=SimpleNamespace(root=Path(root),conn=conn,coverage_gaps_path=lambda rid:artifact_path(Path(root),rid).with_suffix('.coverage.parquet'))
            current=cost_lines.current(view,run_id,store_id,period)
            effective.update((period,r['coverage_key']) for r in current['reviewed'])
        except (WorkspaceError,OSError,ValueError,KeyError,StopIteration,pl.exceptions.PolarsError):
            # Unverifiable current context never silently discards a human save.
            latest={r['coverage_key']:r for r in manual_rows if r['period']==period}
            effective.update((period,key) for key,r in latest.items() if r['action']=='save')
    return effective


def component_reference(gap,coverage=None):
    """Source-fact identity is separate from the human goods-cost form."""
    fields=('metric_id','store','period','internal_order_id','internal_sub_order_id','sku','quantity',
        'order_id','order_date','source_pricing_day','cost_company_id','cost_source','cost_status','cost_as_of')
    context={k:gap.get(k) for k in fields}
    key='component:'+journal.digest([context[k] for k in ('metric_id','store','period','internal_order_id','internal_sub_order_id','sku')])
    parent=dict(coverage or {})
    return {**parent,'coverage_key':key,'context_sha':journal.digest(context),'order_id':gap.get('order_id'),
        'sub_order_id':parent.get('sub_order_id'),'order_count':1,'partial_cost':bool(parent.get('partial_cost')),
        'manual_coverage_key':gap.get('coverage_key'),'manual_context_sha':parent.get('context_sha'),'source_component':True}


def source_gap(frame,request,coverage_rows):
    candidates=[];parents={r['coverage_key']:r for r in coverage_rows}
    for gap in frame.filter(pl.col('period')==request['period']).to_dicts():
        if gap.get('metric_id') not in {'goods_cost','reshipment_cost'}:continue
        reference=component_reference(gap,parents.get(gap.get('coverage_key')))
        if reference['coverage_key']==request['coverage_key'] and reference['context_sha']==request['context_sha']:
            candidates.append((reference,gap))
    if len(candidates)!=1:raise Pending('sealed_component_context_unproved')
    return candidates[0]


def original_reship_member(ws,request,detail,coverage,gap,client,refs):
    """A header relation alone cannot authorize an original sales member."""
    current=detail['order'];target=str(current['order_id']);sku=str(gap['sku']);day=_day(gap.get('source_pricing_day')) or _day(gap['order_date'])
    relations=detail.get('relations')
    if not isinstance(relations,list) or any(not isinstance(r,dict) for r in relations):raise Pending('reship_original_relation_missing',refs)
    origins={str(r.get('source_order_id')) for r in relations if r.get('relation_type')=='reship' and str(r.get('target_order_id'))==target and _id(r.get('source_order_id'))}
    if len(origins)!=1 or target in origins:raise Pending('reship_original_relation_not_unique',refs)
    source=next(iter(origins));original=client.get('orders/'+source);refs.append(_retain(ws,'order-os:original-sale',original))
    if not isinstance(original,dict) or not isinstance(original.get('order'),dict) or not isinstance(original.get('items'),list):raise Pending('reship_original_source_contract_invalid',refs)
    order=original['order'];members=order.get('platform_order_ids')
    if live_status(order)!='Sent':raise Pending('reship_original_sale_status_unproved',refs)
    if (str(order.get('order_id'))!=source or str(order.get('order_store_id'))!=str(current.get('order_store_id'))
        or order.get('is_merge') or order.get('is_split') or order.get('order_type') not in {'普通订单','销售订单','normal','sale'}
        or order.get('platform_order_identity_complete') is not True or not isinstance(members,list)
        or set(map(str,members))!={str(coverage.get('order_id') or '')}):raise Pending('reship_original_store_or_parent_conflict',refs)
    candidates=[r for r in original['items'] if isinstance(r,dict) and str(r.get('sku_id'))==sku]
    if len(candidates)!=1:raise Pending('reship_original_member_not_unique',refs)
    member=candidates[0];quantity=_positive(member.get('quantity'));wanted=_positive(gap.get('quantity'))
    if str(member.get('order_id') or '')!=source or member.get('order_store_id') not in (None,'',str(order['order_store_id'])):
        raise Pending('reship_original_member_order_or_store_conflict',refs)
    if member.get('online_order_no') not in (None,'',coverage['order_id'],str(order['order_store_id'])+':'+str(coverage['order_id'])):
        raise Pending('reship_original_member_parent_conflict',refs)
    if (str(member.get('sku_type') or '') not in {'normal','普通','普通商品'} or any(member.get(k) is True for k in ('is_gift','is_virtual'))
        or (member.get('is_suspect') is True and not quantity_admitted(order,member,member.get('cost'),member.get('quantity')))
        or not quantity or not wanted or wanted>quantity or member.get('src_combine_sku_id') not in (None,'',sku)
        or not _id(member.get('sub_order_id')) or not member.get('platform_sub_order_id')):raise Pending('reship_original_component_unit_or_quantity_unproved',refs)
    original_day=_day(member.get('original_order_date')) or _day(order.get('order_date')) or _day(order.get('order_time'))
    if original_day!=day or company_identity(member,member.get('cost'))!=str(gap.get('cost_company_id') or ''):raise Pending('reship_original_day_or_company_conflict',refs)
    return {'internal_order_id':source,'internal_sub_order_id':str(member['sub_order_id']),
        'platform_order_id':coverage['order_id'],'platform_sub_order_id':str(member['platform_sub_order_id']),
        'quantity':str(quantity),'sku':sku,'company':str(gap['cost_company_id']),'pricing_day':day}


def granted(ws,store_id,period):
    try:approved_rule(ws,store_id,period,rule_id=RULE,rule_sha=RULE_SHA);return True
    except (PermissionError,ValueError):return False


def active(conn,store_id,period=None):
    rows=conn.execute("SELECT d.id,d.period,d.values_json,EXISTS(SELECT 1 FROM automation_decision r WHERE r.action='revoke' AND r.ref_id=d.id) AS revoked FROM automation_decision d WHERE d.store_id=? AND d.kind=? AND d.action='apply' AND EXISTS(SELECT 1 FROM automation_authorization a WHERE a.request_id=d.request_id AND a.action=? AND a.identity_kind='delegated_service') ORDER BY d.rowid",(store_id,RULE,RULE)).fetchall()
    latest={}
    for row in rows:
        values=json.loads(row['values_json'])
        if values.get('rule_sha')!=RULE_SHA:continue
        for fact in values.get('facts',[]):
            key=(fact['internal_order_id'],fact['internal_sub_order_id'])
            latest[key]=None if row['revoked'] else {**fact,'decision_id':row['id'],'period':row['period']}
    return [fact for fact in latest.values() if fact is not None and (period is None or fact['period']==period)]


def fingerprint(ws,store_id,period):
    return journal.digest(active(ws.conn,store_id,period))


def withdrawn(conn,store_id,period,grant_id):
    rows=conn.execute("SELECT d.values_json FROM automation_decision d JOIN automation_decision r ON r.ref_id=d.id AND r.action='revoke' WHERE d.store_id=? AND d.period=? AND d.kind=? AND r.rowid>(SELECT rowid FROM automation_decision WHERE id=?)",(store_id,period,RULE,grant_id)).fetchall()
    return {f['coverage_key'] for row in rows for f in json.loads(row['values_json']).get('facts',[])}


def _retained(ws,fact):
    for ref in fact['source_refs']:
        path=Path(ref['path'])
        if path.is_symlink() or not path.resolve().is_relative_to(ws.root.resolve()) or not path.is_file() or file_digest(path)!=ref['sha256']:
            raise Conflict('历史报价原件缺失或哈希不一致，未采用该事实')


class _Cached:
    def __init__(self,client):self.client=client;self.cache={};self.failure=None
    def get(self,key):
        if self.failure:raise self.failure
        if key not in self.cache:
            try:self.cache[key]=self.client.get(key)
            except (OrderFeedError,OSError,ValueError) as exc:self.failure=exc;raise
        return self.cache[key]
    def quote(self,*key):
        def again(error):
            fresh=copy(error);fresh.artifact_refs=list(error.artifact_refs);raise fresh
        if self.failure:again(self.failure)
        if key in self.cache and isinstance(self.cache[key],HistoryError):again(self.cache[key])
        if key not in self.cache:
            try:self.cache[key]=self.client.quote(*key)
            except HistoryError as exc:
                saved=copy(exc);saved.artifact_refs=list(exc.artifact_refs);self.cache[key]=saved
                if exc.reason_code.startswith('history_source_'):self.failure=saved
                raise
        return self.cache[key]


def _candidate(ws,model,request,order,history):
    fact=_prove(ws,model,request,order,history,source_fact=True)
    parent_store=str(fact['live_order'].get('order_store_id') or '')
    item_store=str(fact['live_item'].get('order_store_id') or '')
    if item_store and item_store!=parent_store:raise Pending('live_member_store_conflict',fact['source_refs'])
    if fact['live_item'].get('order_id') not in (None,'',str(fact['gap']['internal_order_id'])):
        raise Pending('live_member_order_conflict',fact['source_refs'])
    raw=fact['live_item'].get('cost')
    if not isinstance(raw,dict):raise Pending('live_cost_capture_missing',fact['source_refs'])
    if (_positive(raw.get('unit_cost')) or _positive(raw.get('cost_amount'))
        or raw.get('status',raw.get('cost_status'))=='priced' or raw.get('frozen') is True):
        raise Pending('existing_source_cost_preserved',fact['source_refs'])
    if fact['coverage'].get('partial_cost'):
        child=fact['coverage'].get('sub_order_id');siblings=fact['live_components']
        identifiers=[str(i.get('sub_order_id') or '') for i in siblings]
        priced=[i for i in siblings if str(i.get('sub_order_id'))!=str(fact['gap']['internal_sub_order_id'])
            and child and str(i.get('platform_sub_order_id') or '')==child
            and str(i.get('sku_type') or '') in {'normal','普通','普通商品'}
            and _positive(i.get('quantity')) and isinstance(i.get('cost'),dict) and _positive(i['cost'].get('unit_cost'))]
        if len(set(identifiers))!=len(identifiers) or not priced:raise Pending('partial_cost_component_inventory_incomplete',fact['source_refs'])
    # Admission to the unchanged original Float64 source contract must remain
    # positive and finite; tiny/overflowing quotes never turn into zero/infinity.
    if any(not math.isfinite(float(fact[k])) or float(fact[k])<=0 for k in ('quantity','unit_cost','exact_amount')):
        raise Pending('original_engine_numeric_contract_unrepresentable',fact['source_refs'])
    quantity_evidence=raw.get('pricing_evidence')
    if isinstance(quantity_evidence,dict):quantity_evidence=journal.encoded(quantity_evidence)
    platform_ids=list(fact['live_order']['platform_order_ids']);platform_child=str(fact['live_item'].get('platform_sub_order_id') or '')
    fact={key:value for key,value in fact.items() if key not in {'live_order','live_item','live_components'}}
    return {**fact,'coverage_key':request['coverage_key'],'context_sha':request['context_sha'],
        'order_store_id':parent_store,'store_id':request['store_id'],
        'current_platform_order_ids':platform_ids,
        'current_platform_sub_order_id':platform_child,
        'quantity_evidence':quantity_evidence,
        'internal_order_id':str(fact['gap']['internal_order_id']),
        'internal_sub_order_id':str(fact['gap']['internal_sub_order_id']),
        'source_run_id':request['run_id'],'source_input_sha':request['input_sha'],
        'source_line_revision':request['expected_line_revision']}


def apply(ws,model,request,actor,*,order_client=None,history_client=None):
    from .version import assert_code_integrity
    assert_code_integrity()
    if (not actor or actor.get('kind')!='service' or not actor.get('authenticated') or not actor.get('id')
        or 'business' not in actor.get('scopes',[]) or 'approved_action' not in actor.get('capabilities',[])):
        raise PermissionError('需要已授权的确定性业务服务身份')
    if request.get('rule_id')!=RULE:raise Conflict('需要独立批准的v2历史报价事实规则')
    _request(request['request_id']);journal.period(request['period'])
    items=request.get('items')
    if not isinstance(items,list) or not 1<=len(items)<=100 or len({r['coverage_key'] for r in items})!=len(items):raise Conflict('请提交1至100个互不重复的缺口引用')
    key='approved-source-facts:'+request['request_id'];signature=journal.digest(request)
    if ws.conn.execute("SELECT 1 FROM automation_decision r JOIN automation_decision d ON d.id=r.ref_id WHERE r.action='revoke' AND d.request_id=?",(key,)).fetchone():raise Conflict('该批报价事实已被人工撤回，旧请求不能重新采用')
    old=journal.receipt(ws.conn,key,signature)
    if old:return old
    grant=approved_rule(ws,request['store_id'],request['period'],rule_id=RULE,rule_sha=RULE_SHA)
    current=checker.check(ws,request['store_id'],request['period'],model=model)
    case=ws.conn.execute('SELECT * FROM automation_case WHERE id=?',(request['case_id'],)).fetchone()
    if (current['input_sha']!=request['input_sha'] or current['run_id']!=request['run_id'] or not case
        or (case['store_id'],case['period'],case['input_sha'])!=(request['store_id'],request['period'],request['input_sha'])):raise Conflict('案件或核算输入已变化，请重新绑定当前缺口')
    facts=[];outcomes=[]
    if enabled():
        order=history=None
        existing={(f['coverage_key'],f['context_sha']) for f in active(ws.conn,request['store_id'],request['period'])}
        rejected=withdrawn(ws.conn,request['store_id'],request['period'],grant['id'])
        for item in items:
            scoped={**request,**item}
            try:
                if (item['coverage_key'],item['context_sha']) in existing:raise Pending('existing_history_fact_preserved')
                if item['coverage_key'] in rejected:raise Pending('withdrawn_history_fact_requires_new_grant')
                if order is None:
                    from .order_feed import Client
                    order=_Cached(order_client or Client())
                if history is None:history=_Cached(history_client or HistoryClient.from_env(ws.root))
                fact=_candidate(ws,model,scoped,order,history);facts.append(fact)
                outcomes.append({'coverage_key':item['coverage_key'],'status':'adopted','quote_sha':fact['quote_sha']})
            except (Pending,HistoryError) as exc:
                outcomes.append({'coverage_key':item['coverage_key'],'status':'pending',
                    'reason_codes':[getattr(exc,'code',None) or exc.reason_code],
                    'source_refs':getattr(exc,'refs',None) or getattr(exc,'artifact_refs',[])})
            except (OSError,ValueError,OrderFeedError,pl.exceptions.PolarsError):
                outcomes.append({'coverage_key':item['coverage_key'],'status':'pending','reason_codes':['upstream_evidence_unavailable']})
    else:outcomes=[{'coverage_key':r['coverage_key'],'status':'pending','reason_codes':['automation_disabled']} for r in items]
    from .service import _store_lock
    with _store_lock(request['store_id']),guard(ws.root,request['store_id'],request['period']),ws.transaction() as conn:
        assert_code_integrity()
        old=journal.receipt(conn,key,signature)
        if old:return old
        approved_rule(ws,request['store_id'],request['period'],grant['id'],rule_id=RULE,rule_sha=RULE_SHA)
        if checker.check(ws,request['store_id'],request['period'],model=model)['input_sha']!=request['input_sha']:raise Conflict('取证后输入已变化，本批没有采用任何成本事实')
        identities=[(f['internal_order_id'],f['internal_sub_order_id']) for f in facts]
        if len(set(identities))!=len(identities):raise Conflict('同一原始组件被多个缺口重复引用，本批未采用')
        for fact in facts:
            _gap(ws,{**request,**{k:fact[k] for k in ('coverage_key','context_sha')},'expected_line_revision':fact['source_line_revision']},source_fact=True)
            _retained(ws,fact)
        receipt_key=key if facts else key+':pending:'+journal.ident()
        aid=journal.ident();did=journal.ident()
        conn.execute('INSERT INTO automation_authorization VALUES(?,?,?,?,?,?,?,?,?)',
            (aid,receipt_key,actor['id'],'delegated_service',RULE,request['input_sha'],journal.digest(facts),
             journal.encoded({'approved_rule_grant':grant['id'],'rule_sha':RULE_SHA}),journal.now()))
        if facts:
            conn.execute('INSERT INTO automation_decision VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (did,key,RULE,request['period'],request['store_id'],'apply','',journal.encoded({'rule_sha':RULE_SHA,'facts':facts}),
                 '可信原始历史报价补齐源事实；由原核算引擎计算',request['input_sha'],journal.digest(facts),actor['id'],journal.now()))
            conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
                (journal.ident(),request['case_id'],key,request['input_sha'],'dispatch_pending',
                 journal.encoded({'store_id':request['store_id'],'fingerprint':'automation:'+journal.digest([did,request['input_sha']])}),journal.now()))
        refs=[ref for fact in facts for ref in fact['source_refs']]
        refs.extend(ref for outcome in outcomes for ref in outcome.get('source_refs',[]))
        conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
            (journal.ident(),request['case_id'],receipt_key,request['input_sha'],'source_facts_evaluated',journal.encoded({'outcomes':outcomes}),journal.now()))
        result=journal.finish(conn,receipt_key,RULE,signature,aid,{'status':'applied' if facts else 'pending',
            'decision_id':did if facts else None,'applied_count':len(facts),'outcomes':outcomes,'source_refs':refs,
            'reason_codes':sorted({c for o in outcomes for c in o.get('reason_codes',[])}),'recompute_required':bool(facts)})
    if facts:
        from .automation_actions import deliver
        deliver(ws)
    return result


def overlay(root,store,orders,items,costs,relations=None):
    """Fill only exact still-missing source rows; immutable H0 is never edited."""
    db_path=Path(root)/'workspace.db'
    if not db_path.is_file():return costs,[]
    with closing(sqlite3.connect(db_path.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
        conn.row_factory=sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='automation_decision'").fetchone():return costs,[]
        facts=active(conn,store.id)
        # Natural expiry and kill switches stop adoption, not existing cost
        # evidence. Explicit grant withdrawal invalidates open owned inputs;
        # historical frozen origins remain read-only return/cost evidence.
        withdrawn_grants={row['id'] for row in conn.execute("""SELECT d.id FROM automation_decision d
            JOIN automation_authorization a ON a.request_id=d.request_id
            JOIN automation_decision r ON r.ref_id=json_extract(a.scope_json,'$.approved_rule_grant') AND r.action='revoke'
            WHERE d.store_id=? AND d.kind=? AND d.action='apply'""",(store.id,RULE))}
        closed_periods={row[0] for row in conn.execute("SELECT period FROM period WHERE store_id=? AND state='closed'",(store.id,))}
        facts=[fact for fact in facts if fact['decision_id'] not in withdrawn_grants or fact['period'] in closed_periods]
        manual=[dict(r) for r in conn.execute('SELECT * FROM cost_line_log WHERE store_id=? ORDER BY id',(store.id,))]
        effective_manual=manual_authority(conn,root,store.id,{f['period'] for f in facts},manual) if manual and facts else set()
    if not facts:return costs,[]
    needed_orders={f['internal_order_id'] for f in facts}|{f['origin']['internal_order_id'] for f in facts if f.get('origin')}
    needed_members={f['internal_sub_order_id'] for f in facts}|{f['origin']['internal_sub_order_id'] for f in facts if f.get('origin')}
    item_rows={};order_rows={};cost_rows={};uses=[];changes=[]
    for row in items.filter(pl.col('order_id').cast(pl.String).is_in(needed_orders)&pl.col('sub_order_id').cast(pl.String).is_in(needed_members)).to_dicts():
        item_rows.setdefault((str(row['order_id']),str(row['sub_order_id'])),[]).append(row)
    for row in orders.filter(pl.col('order_id').cast(pl.String).is_in(needed_orders)).to_dicts():order_rows.setdefault(str(row['order_id']),[]).append(row)
    numbered=costs.with_row_index('__history_fact_order').with_columns(pl.col('order_id','sub_order_id').cast(pl.String))
    for row in numbered.filter(pl.col('order_id').is_in(needed_orders)&pl.col('sub_order_id').is_in(needed_members)).to_dicts():
        cost_rows.setdefault((row['order_id'],row['sub_order_id']),[]).append(row)
    selected={}
    for fact in facts:
        key=(fact['internal_order_id'],fact['internal_sub_order_id']);matching_items=item_rows.get(key,[]);matching_orders=order_rows.get(key[0],[])
        if len(matching_items)!=1 or len(matching_orders)!=1:continue
        item,order=matching_items[0],matching_orders[0]
        originals=cost_rows.get(key,[])
        if len(originals)>1:continue
        original=originals[0] if originals else {}
        if (str(item.get('sku_id'))!=fact['sku'] or _positive(item.get('quantity'))!=_positive(fact['quantity'])
            or company_identity(item,original)!=fact['company'] or _day(item.get('original_order_date'))!=fact['pricing_day']):continue
        if str(order.get('order_store_id') or '')!=fact['order_store_id'] or item.get('order_store_id') not in (None,'',fact['order_store_id']):continue
        origin=fact.get('origin');reship=bool(origin and order.get('order_type') in {'补发订单','补发'})
        if ((order.get('order_type') not in {'普通订单','销售订单','normal','sale'} and not reship) or order.get('is_merge') or order.get('is_split')
            or order.get('order_status_raw')!='Sent'
            or item.get('sku_type') not in {'normal','普通','普通商品'} or any(item.get(k) is True for k in ('is_virtual','is_gift'))):continue
        members=order.get('platform_order_ids')
        if order.get('platform_order_identity_complete') is not True or not isinstance(members,list) or set(map(str,members))!=set(fact['current_platform_order_ids']):continue
        if fact['current_platform_sub_order_id'] and str(item.get('outer_sku') or '')!=fact['current_platform_sub_order_id']:continue
        if reship:
            if relations is None or not {'relation_type','source_order_id','target_order_id'}<=set(relations.columns):continue
            links=relations.filter((pl.col('relation_type')=='reship')&(pl.col('target_order_id').cast(pl.String)==key[0]))
            if set(links['source_order_id'].cast(pl.String).to_list())!={origin['internal_order_id']}:continue
            original_items=item_rows.get((origin['internal_order_id'],origin['internal_sub_order_id']),[])
            if original_items:
                if len(original_items)!=1:continue
                old=original_items[0]
                if (str(old.get('sku_id'))!=origin['sku'] or _positive(old.get('quantity'))!=_positive(origin['quantity'])
                    or _day(old.get('original_order_date'))!=origin['pricing_day']
                    or company_identity(old,(cost_rows.get((origin['internal_order_id'],origin['internal_sub_order_id'])) or [{}])[0])!=origin['company']):continue
        manual_key=fact['coverage'].get('manual_coverage_key') or fact['coverage_key']
        if (fact['period'],manual_key) in effective_manual:continue
        if _positive(original.get('unit_cost')) or _positive(original.get('cost_amount')) or original.get('cost_status')=='priced' or original.get('frozen') is True:continue
        if original and (str(original.get('sku_id'))!=fact['sku'] or _positive(original.get('quantity'))!=_positive(fact['quantity'])):continue
        evidence=original.get('pricing_evidence') or fact.get('quantity_evidence') or '{}'
        if item.get('is_suspect') is True:
            witnessed={**original,'pricing_evidence':evidence}
            if not quantity_admitted(order,item,witnessed,fact['quantity']):
                evidence=fact.get('quantity_evidence') or evidence
                if not quantity_admitted(order,item,{**original,'pricing_evidence':evidence},fact['quantity']):continue
        try:source_evidence=json.loads(evidence) if isinstance(evidence,str) else dict(evidence)
        except (ValueError,TypeError):source_evidence={}
        if key in selected:raise OrderFeedError('同一商品成员存在重复历史报价事实，请维护核查')
        for reference in fact['source_refs']:
            path=Path(reference['path'])
            if path.is_symlink() or not path.resolve().is_relative_to(Path(root).resolve()) or not path.is_file() or file_digest(path)!=reference['sha256']:
                raise OrderFeedError('已采用历史报价事实的原始凭据缺失或SHA变化')
        row={**original,'order_id':key[0],'sub_order_id':key[1],'sku_id':fact['sku'],'quantity':fact['quantity'],
            'unit_cost':fact['unit_cost'],'cost_amount':fact['exact_amount'],'cost_source':'history','cost_status':'priced',
            'cost_as_of':fact['pricing_day'],'failure_reason':None,'__ledger_history_fact_period':fact['period'],
            'pricing_evidence':journal.encoded({**source_evidence,'order_date':fact['pricing_day'],'history_quote_sha':fact['quote_sha'],
                'source_fact_decision':fact['decision_id'],'raw_quantity':fact['quantity'],'raw_unit_cost':fact['unit_cost']})}
        row['__history_fact_order']=original.get('__history_fact_order',costs.height+len(changes))
        changes.append(row)
        selected[key]=fact;uses.append(fact)
    if not uses:return costs,[]
    changed=pl.from_dicts(changes,infer_schema_length=None,strict=False)
    untouched=numbered.join(changed.select('order_id','sub_order_id'),on=['order_id','sub_order_id'],how='anti')
    result=pl.concat([untouched,changed],how='diagonal_relaxed').sort('__history_fact_order').drop('__history_fact_order')
    return result,uses


def run_available(ws,model,period,store_id,*,worker='native:commission-manager'):
    if not enabled() or not granted(ws,store_id,period):return []
    from .automation_actions import sync_store_month
    sync_store_month(ws,model,period,store_id,worker=worker)
    current=checker.check(ws,store_id,period,model=model)
    if not current['run_id']:return []
    state=ws.state(store_id,period)
    if not state or state.closed:return []
    from .automation_handoff import active as waiting
    if any(r['source_kind'] in {'order','history_cost','seller_member'} for r in waiting(ws,model,store_id,period,current['input_sha'])):return []
    try:rows=cost_lines.current(ws,current['run_id'],store_id,period)['items']
    except WorkspaceError:return []
    existing={(f['coverage_key'],f['context_sha']) for f in active(ws.conn,store_id,period)}
    pricing_path=ws.pricing_gaps_path(current['run_id'])
    from .storage_integrity import verified
    if not pricing_path.is_file() or not verified(pricing_path):return []
    try:pricing=pl.read_parquet(pricing_path)
    except (OSError,pl.exceptions.PolarsError):return []
    from collections import Counter
    counts=Counter(g.get('coverage_key') for g in pricing.to_dicts() if g.get('metric_id')=='goods_cost')
    candidates=[r for r in rows if r.get('order_count')==1 and r.get('manual_amount') is None and counts[r['coverage_key']]==1 and (r['coverage_key'],r['context_sha']) not in existing]
    parents={r['coverage_key']:r for r in rows};latest=cost_lines._latest(ws,store_id,period,current['run_id'])
    for gap in pricing.to_dicts():
        if gap.get('period')!=period or gap.get('metric_id') not in {'goods_cost','reshipment_cost'}:continue
        if gap['metric_id']=='goods_cost' and gap.get('coverage_key') in parents and counts[gap.get('coverage_key')]==1:continue
        reference=component_reference(gap,parents.get(gap.get('coverage_key')))
        prior=latest.get(gap.get('coverage_key'))
        if prior and any(r['coverage_key']==gap.get('coverage_key') for r in cost_lines.current(ws,current['run_id'],store_id,period)['reviewed']):continue
        reference.update(line_revision=prior['id'] if prior else 0,manual_amount=None)
        if (reference['coverage_key'],reference['context_sha']) not in existing:candidates.append(reference)
    candidates=candidates[:100]
    if not candidates:return []
    request={'request_id':journal.digest([RULE,store_id,period,current['input_sha'],[r['coverage_key'] for r in candidates]]),
        'rule_id':RULE,'case_id':journal.digest([store_id,period])[:32],'store_id':store_id,'period':period,
        'run_id':current['run_id'],'input_sha':current['input_sha'],
        'items':[{k:r[k] for k in ('coverage_key','context_sha')}|{'expected_line_revision':r['line_revision']} for r in candidates]}
    actor={'id':worker,'kind':'service','authenticated':True,'scopes':['business'],'capabilities':['approved_action']}
    try:return [apply(ws,model,request,actor)]
    except (Conflict,PermissionError):return []
