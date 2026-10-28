"""Human-confirmed business actions over existing ledger primitives."""
from __future__ import annotations

import json
import time
from decimal import Decimal, InvalidOperation

from . import automation_store as journal
from . import automation_checker as checker
from .workspace import WorkspaceError


class Conflict(WorkspaceError):
    pass


def human(actor):
    if not actor or actor.get('kind')!='human' or not actor.get('id') or not actor.get('authenticated'):
        raise PermissionError('请使用已登录的人工身份确认业务决定')
    return actor


def _request(value):
    if not isinstance(value,str) or not value.strip() or len(value)>128:
        raise Conflict('缺少有效请求编号')
    return value


def _money(value):
    try:
        amount=Decimal(str(value))
        if not amount.is_finite() or amount<0 or amount!=amount.quantize(Decimal('.01')):raise ValueError()
        return format(amount,'f')
    except (ValueError,InvalidOperation):raise Conflict('金额必须是非负、精确到分的有效数值')


def _policy_range(kind,values,anchor):
    if kind=='store_scope':return values.get('valid_from',anchor),values.get('valid_until',anchor)
    if kind=='rule_grant':return values.get('period_from',anchor),values.get('period_until',anchor)
    return anchor,anchor


def _frozen_scope_check(conn,store_id,start,end,included):
    periods=[r[0] for r in conn.execute("SELECT DISTINCT period FROM period WHERE state='closed' AND period BETWEEN ? AND ?",(start,end))]
    for period in periods:
        frozen=conn.execute('SELECT basis_json FROM automation_overhead_close WHERE period=? ORDER BY rowid DESC LIMIT 1',(period,)).fetchone()
        if frozen:
            was_included=store_id in {r['store_id'] for r in json.loads(frozen['basis_json'])}
        else:
            known=conn.execute("SELECT 1 FROM period WHERE store_id=? AND period=? AND state='closed'",(store_id,period)).fetchone()
            if not known:raise Conflict('历史冻结营业范围尚不可证明，不能追改其生效范围')
            was_included=True
        if was_included!=included:raise Conflict('生效范围会改变已冻结月份的营业范围，请改为未来未结月份生效')


def _decision_input(ws,model,kind,store_id,anchor,values):
    base=_scope_input(ws,model,anchor,'' if kind in {'overhead_total','store_scope'} else store_id)
    start,end=_policy_range(kind,values,anchor)
    if start==end==anchor or kind not in {'store_scope','rule_grant'}:return base
    rows=ws.conn.execute('SELECT rowid,* FROM automation_decision WHERE kind=? AND store_id=? ORDER BY rowid',(kind,store_id)).fetchall()
    relevant=[];ids=set()
    for row in rows:
        if row['action']!='apply':continue
        body=json.loads(row['values_json']);first,last=_policy_range(kind,body,row['period'])
        if first<=end and last>=start:
            relevant.append(dict(row));ids.add(row['id'])
    relevant.extend(dict(r) for r in rows if r['action']=='revoke' and r['ref_id'] in ids)
    frozen=[tuple(r) for r in ws.conn.execute('SELECT period,input_sha FROM automation_overhead_close WHERE period BETWEEN ? AND ? ORDER BY period,input_sha',(start,end))]
    return journal.digest({'anchor_input':base,'effective_range':[start,end],'policies':relevant,'frozen':frozen})


def _stores(model):
    return [s for s in model.stores if not s.archived]


def policy_views(ws,period,active=None):
    """Backend-resolved policy status; clients never infer grant authority."""
    from datetime import datetime,timezone
    from .automation_rules import contracts
    active=active if active is not None else journal.active_decisions(ws.conn,period)
    rows=ws.conn.execute("SELECT * FROM automation_decision WHERE kind IN ('store_scope','rule_grant','overhead_total') ORDER BY rowid DESC").fetchall()
    revoked={r['ref_id'] for r in rows if r['action']=='revoke'}
    known=contracts();stamp=datetime.now(timezone.utc);result=[]
    for row in rows:
        if row['action']!='apply':continue
        value=json.loads(row['values_json']);first,last=_policy_range(row['kind'],value,row['period'])
        if not first<=period<=last:continue
        key=(row['store_id'],'rule_grant:'+value.get('rule_id','') if row['kind']=='rule_grant' else row['kind'])
        current=active.get(key)
        status='revoked' if row['id'] in revoked else 'superseded' if not current or current['id']!=row['id'] else 'active'
        if status=='active' and row['kind']=='rule_grant':
            try:
                if not value.get('enabled'):status='disabled'
                elif value.get('rule_sha')!=known.get(value.get('rule_id'),(None,))[0]:status='version_changed'
                elif datetime.fromisoformat(value['valid_until'])<=stamp:status='expired'
                elif datetime.fromisoformat(value['valid_from'])>stamp:status='not_started'
            except (ValueError,KeyError,TypeError):status='invalid'
        frozen=(row['kind']!='rule_grant' and bool(ws.conn.execute("SELECT 1 FROM period WHERE period BETWEEN ? AND ? AND state='closed' LIMIT 1",(first,last)).fetchone()))
        result.append({'decision_id':row['id'],'kind':row['kind'],'store_id':row['store_id'],'anchor_period':row['period'],
            'values':value,'reason':row['reason'],'actor':row['actor'],'at':row['at'],'status':status,
            'effective_month_range':{'from':first,'until':last},'can_revoke':row['id'] not in revoked and not frozen,
            'revoke_blocker':'生效范围已有冻结账，请另设未来范围' if frozen else ''})
    return result


def _scope_input(ws,model,period,store_id=''):
    decisions=journal.active_decisions(ws.conn,period)
    records=[]
    for store in checker.operating_stores(ws,model,period,include_excluded=True):
        if store_id and store.id!=store_id:continue
        row=checker.current_run(ws,store.id,period)
        state=checker.period_header(ws,store.id,period,row['id'] if row else None)
        proof=ws.conn.execute('SELECT dependency_sha FROM automation_run_proof WHERE run_id=?',(row['id'],)).fetchone() if row else None
        records.append({'store_id':store.id,'run_id':row['id'] if row else None,
            'result_sha':proof['dependency_sha'] if proof else journal.digest(json.loads(row['result'])) if row else None,
            'ready':row['evidence_ready'] if row else False,'error':row['evidence_error'] if row else '',
            'unverified_source_sha':checker.pending_input_identity(ws,store.id,period),
            'state':state.state if state else 'open',
            'line_revision':ws.conn.execute('SELECT coalesce(max(id),0) FROM cost_line_log WHERE store_id=? AND period=?',(store.id,period)).fetchone()[0]})
    relevant=[(key,value['id']) for key,value in decisions.items() if not store_id or key[0] in ('',store_id)]
    model_scope=model.model_dump(mode='json')
    model_scope['overheads']=[r for r in model_scope.get('overheads',[]) if r['period']==period]
    if store_id:model_scope['stores']=[r for r in model_scope['stores'] if r['id']==store_id]
    return journal.digest({'period':period,'records':records,'decisions':sorted(relevant),
        'model':model_scope,'feed':{r['store_id']:checker.feed_scope(ws,r['store_id'],period) for r in records}})


def _company_labor_source(ws,model,period,company):
    """One company source wait; only a current human preview is a decision.

    A missing monthly document is not an approved zero and never blocks
    independent store evidence collection. Existing proposal authority and
    approval/CAS remain unchanged; this read does not certify a candidate.
    """
    if not any(p['code']=='overhead_total_unknown' for p in company.get('issues',[])) or company.get('finance_decision'):
        return None,None
    for row in ws.conn.execute("SELECT * FROM automation_proposal WHERE kind='decision' AND period=? AND store_id='' AND expires_at>=? ORDER BY rowid DESC",
                               (period,int(time.time()))):
        try:
            payload=json.loads(row['payload_json'])
            if payload.get('kind')!='overhead_total' or set(payload.get('values',{}))!={'amount'}:continue
            values=payload['values']
            if _money(values['amount'])!=values['amount']:continue
            if _decision_input(ws,model,'overhead_total','',period,values)!=row['input_sha']:continue
            signature=journal.digest({'id':row['id'],'kind':'decision','period':period,'store_id':'',
                'input_sha':row['input_sha'],'payload':payload})
            if signature!=row['proposal_sha']:continue
            provenance=ws.conn.execute("SELECT 1 FROM automation_receipt r JOIN automation_authorization a ON a.id=r.authorization_id WHERE r.action='decision.preview' AND a.action='decision.preview' AND a.identity_kind='human' AND a.actor=? AND a.input_sha=? AND json_extract(r.result_json,'$.proposal_id')=? AND json_extract(r.result_json,'$.proposal_sha')=?",
                (row['actor'],row['input_sha'],row['id'],row['proposal_sha'])).fetchone()
            if not provenance:continue
            return None,{'kind':'overhead_total','title':'核定本月兼职总额','scope':'company','count':1,
                'period':period,'proposal_id':row['id'],'proposal_sha':row['proposal_sha'],
                'input_sha':row['input_sha'],'actor':row['actor'],'values':values,
                'summary':'本月兼职总额已有有效预览，等待财务集中核定；核定前不冻结公摊或月结'}
        except (ValueError,TypeError,KeyError,Conflict):
            continue
    key='company-labor:'+journal.digest([period,'monthly_labor_confirmation'])
    return {'id':key,'notification_key':key,'kind':'source','scope':'company','period':period,
        'role':'finance','role_label':'财务资料维护角色','assignee_status':'role_unconfigured',
        'evidence_type':'monthly_labor_confirmation','evidence_label':'本月兼职核定资料',
        'summary':'等待财务资料维护角色上传本月兼职核定资料；已有订单、成本取证和核算继续自动处理',
        'since':None,'count':1,'financial_applied':False},None


def month(ws,model,period):
    journal.period(period)
    decisions=journal.active_decisions(ws.conn,period)
    company=checker.company_basis(ws,model,period)
    company_waiting,company_source_decision=_company_labor_source(ws,model,period,company)
    rows=[]
    groups={}
    code_maintenance=False
    for store in checker.operating_stores(ws,model,period,include_excluded=True):
        case_id=journal.digest([store.id,period])[:32]
        excluded=decisions.get((store.id,'store_scope'),{}).get('values',{}).get('included') is False
        checked=checker.check(ws,store.id,period,model=model,company_context=company)
        code_maintenance=code_maintenance or any(p['code']=='code_source_drift' for p in checked['issues'])
        from . import automation_handoff as handoff
        waiting_items=handoff.active(ws,model,store.id,period,checked['input_sha'])
        state=checker.period_header(ws,store.id,period,checked['run_id'])
        problems=checked['issues']
        if state and state.closed:
            status,stage,message='completed','closed','本月已结账，展示冻结结果'
            problems=[]
        elif excluded:
            status,stage,message='completed','excluded','已按本月营业范围决定排除'
            problems=[]
        elif checked['allowed']:
            status,stage,message='needs_decision','ready_for_close','核算与复核已完成，请确认本月结账'
        elif any(p['code'] in {'cost_decision_required','commission_incomplete'} for p in problems):
            status,stage,message='needs_decision','business_decision','请确认确实需要业务决定的事项'
            cost_work=(any(p['code']=='cost_decision_required' for p in problems)
                or bool(json.loads(checker.current_run(ws,store.id,period)['result']).get('pricing_pending_count',0) if checked['run_id'] else False))
            if cost_work:
                from .automation_deterministic import approved_rule,enabled
                from .automation_cost_actions import RULE as history_rule,RULE_SHA as history_sha
                try:
                    if enabled():
                        from .historical_cost_facts import granted as facts_granted
                        if not facts_granted(ws,store.id,period):
                            try:approved_rule(ws,store.id,period,rule_id=history_rule,rule_sha=history_sha)
                            except PermissionError:
                                from .automation_source_cost_v3 import enabled as physical_enabled,RULE as physical_rule,RULE_SHA as physical_sha
                                if not physical_enabled():raise
                                approved_rule(ws,store.id,period,rule_id=physical_rule,rule_sha=physical_sha)
                        status,stage,message='processing','gathering','正在按已批准规则查找原始成本凭据并核算'
                except (PermissionError,ValueError):pass
        else:
            status,stage,message='processing','gathering','系统正在取证、核算或等待来源恢复'
            codes={p['code'] for p in problems}
            if codes and codes<={'company_revenue_unknown','overhead_basis_empty','overhead_total_unknown'}:
                stage,message='waiting_company','本店核算已完成，等待公司其他营业店资料与公摊依据完整'
            elif codes and codes<={'recompute_pending','unverified_input_change','input_capture_unverified','manual_input_unverified','unverified_code_change'}:
                stage,message='calculating','新资料已进入现有核算队列，完成后自动更新'
        waiting=None
        if waiting_items and status!='completed' and not checked['allowed']:
            waiting={'items':waiting_items,'since':min(item['since'] for item in waiting_items),
                'summary':'；'.join(dict.fromkeys(item['summary'] for item in waiting_items))}
            stage='waiting_source' if any(item['kind']=='source' for item in waiting_items) else 'waiting_maintenance'
            status='processing'
            message=waiting['summary']
        codes={p['code'] for p in problems}
        local_recompute=(bool(codes&{'unverified_input_change','unverified_model_change','manual_input_unverified','input_capture_unverified','unverified_code_change'})
            or ('no_result' in codes and bool(ws.active_files(store.id))))
        if not waiting and status!='completed' and 'recompute_pending' in codes and local_recompute:
            status='processing'
            stage,message='calculating','资料已收到，正在现有核算队列中复核'
        finance_codes={'overhead_frozen_input_changed','overhead_legacy_frozen_basis','overhead_frozen_basis_unverified'}
        local_work=codes-finance_codes-{'company_revenue_unknown','overhead_basis_empty','overhead_total_unknown'}
        local_income_missing=store.id in company.get('missing_store_ids',[])
        if status!='completed' and company.get('finance_decision') and not local_work and not local_income_missing:
            status,stage,message='needs_decision','company_finance',company['finance_decision']['summary']
            waiting=None
        # Company source readiness and this store's remaining work are
        # independent. Do not park a runnable source/cost job behind payroll.
        if status=='processing' and not waiting and company_waiting and codes and codes<={'company_revenue_unknown','overhead_basis_empty','overhead_total_unknown'}:
            stage,message='waiting_company',company_waiting['summary']
            waiting={'items':[],'since':None,'summary':message,'scope':'company',
                     'company_waiting_id':company_waiting['id']}
        from .source_recovery import close_refresh_pending,retry_queued
        if status!='completed' and (close_refresh_pending(ws,case_id,checked['input_sha']) or retry_queued(ws,case_id,checked['input_sha'])):
            status,stage,message='processing','gathering','正在刷新月结所需的上游凭据，完成后继续预览'
            waiting=None
        if status!='completed' and 'code_source_drift' in codes:
            from .version import CODE_MAINTENANCE
            status,stage,message='processing','maintenance',CODE_MAINTENANCE
        actions=[]
        if stage=='ready_for_close':actions.append({'kind':'close','label':'预览月结'})
        elif stage not in {'closed','maintenance','company_finance','waiting_company'} and not waiting:actions.append({'kind':'advance','label':'继续自动处理'})
        if stage not in {'closed','maintenance','company_finance'}:actions.append({'kind':'store_scope','label':'确认营业范围'})
        rows.append({'case_id':case_id,'store_id':store.id,'store':store.name,'period':period,
            'status':status,'stage':stage,'message':message,'run_id':state.run_id if state else None,
            'input_sha':checked['input_sha'],'issues':problems,'actions':actions,
            'waiting':waiting,'source_context_sha':handoff.context_sha(ws,model,store.id,period,checked['input_sha']),
            'verification':{'allowed':checked['allowed'] and not bool(state and state.closed),
                            'input_sha':checked['input_sha']}})
        if status=='needs_decision' and stage!='company_finance':
            kind='close' if stage=='ready_for_close' else 'business_decision'
            groups.setdefault(kind,[]).append(store.id)
    counts={k:sum(r['status']==k for r in rows) for k in ('completed','processing','needs_decision')}
    counts['total']=len(rows)
    counts['waiting']=sum(bool(row['waiting']) for row in rows)
    finance_decision=company.get('finance_decision') if not code_maintenance else None
    if code_maintenance:company={**company,'finance_decision':None}
    if not any(row['status']!='completed' for row in rows):company_waiting,company_source_decision=None,None
    if code_maintenance:company_waiting,company_source_decision=None,None
    counts['company_finance_decisions']=int(bool(finance_decision))
    counts['company_source_decisions']=int(bool(company_source_decision))
    status='needs_decision' if counts['needs_decision'] or finance_decision or company_source_decision or not rows else 'processing' if counts['processing'] else 'completed'
    eligible=[r['store_id'] for r in rows if r['stage']=='ready_for_close']
    can_advance=any(a['kind']=='advance' for r in rows for a in r['actions'])
    finance_only=bool(finance_decision) and all(r['status']=='completed' or r['stage']=='company_finance' for r in rows)
    decision_groups=[{'kind':k,'title':'确认月结' if k=='close' else '确认本月兼职总额' if k=='overhead_total' else '业务决定',
        'store_ids':v,'count':len(v)} for k,v in groups.items()]
    if finance_decision:decision_groups.append({**finance_decision,'title':'核对公司冻结公摊依据',
        'store_ids':[r['store_id'] for r in rows],'count':1})
    if company_source_decision:decision_groups.append({**company_source_decision,
        'store_ids':[r['store_id'] for r in rows if r['status']!='completed']})
    waiting_groups=handoff.groups(rows)
    if company_waiting:waiting_groups.append({**company_waiting,
        'store_ids':[r['store_id'] for r in rows if r['status']!='completed'],'periods':[period]})
    return {'period':period,'status':status,'input_sha':_scope_input(ws,model,period),
        'counts':counts,'stores':rows,'decision_groups':decision_groups,
        'can_advance':can_advance,'company_finance_only':finance_only,
        'advance_block_reason':'company_finance_decision' if finance_only and not can_advance else None,
        'overhead':company,'company_waiting':company_waiting,'close':{'eligible_store_ids':eligible,
            'blocked_store_ids':[r['store_id'] for r in rows if r['status']!='completed' and r['store_id'] not in eligible],
            'can_preview':bool(eligible)},'policies':policy_views(ws,period,decisions),'waiting_groups':waiting_groups}


def _case(conn,row):
    conn.execute('INSERT INTO automation_case(id,store_id,period,status,stage,input_sha,run_id,issues_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(store_id,period) DO UPDATE SET status=excluded.status,stage=excluded.stage,input_sha=excluded.input_sha,run_id=excluded.run_id,issues_json=excluded.issues_json,updated_at=excluded.updated_at,lease_owner=CASE WHEN automation_case.input_sha<>excluded.input_sha THEN \'\' ELSE lease_owner END,lease_until=CASE WHEN automation_case.input_sha<>excluded.input_sha THEN 0 ELSE lease_until END,lease_token=lease_token+CASE WHEN automation_case.input_sha<>excluded.input_sha THEN 1 ELSE 0 END',
        (row['case_id'],row['store_id'],row['period'],row['status'],row['stage'],row['input_sha'],row['run_id'],journal.encoded(row['issues']),journal.now()))


def _audit_cases(ws,model,period,request_id,action,result):
    """Called inside the same transaction as the decision and its receipt."""
    for row in month(ws,model,period)['stores']:
        _case(ws.conn,row)
        ws.conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
            (journal.ident(),row['case_id'],request_id,row['input_sha'],action,
             journal.encoded(result),journal.now()))


def sync_month(ws,model,period,*,worker='native:commission-manager'):
    """Journal all expected stores after source arrival, never approve or close."""
    value=month(ws,model,period)
    from . import publication_scope
    publication=publication_scope.resolve(ws.root,model)
    observed=value['stores']
    if publication is not None:
        observed=[row for row in observed if publication.allows(row['store_id'],period)]
        if not observed:return {'period':period,'status':'outside_publication_scope','observed':0}
    key_scope=[period,value['input_sha'],[(r['store_id'],r['status'],r['stage'],r['source_context_sha']) for r in observed]]
    if publication is not None:key_scope.append(publication.identity)
    key='checker-refresh:'+journal.digest(key_scope)
    signature=journal.digest([key,period])
    with ws.transaction() as conn:
        if publication is not None:
            for row in observed:publication.require(row['store_id'],period,workspace_root=ws.root)
        old=journal.receipt(conn,key,signature)
        if old:
            for row in observed:_case(conn,row)
            if publication is not None:publication.verify(ws.root,model)
            return old
        aid=journal.ident()
        conn.execute('INSERT INTO automation_authorization VALUES(?,?,?,?,?,?,?,?,?)',
            (aid,key,worker,'builtin_service','checker.refresh',value['input_sha'],'',journal.encoded({'period':period}),journal.now()))
        for row in observed:_case(conn,row)
        if publication is not None:publication.verify(ws.root,model)
        return journal.finish(conn,key,'checker.refresh',signature,aid,{'period':period,'status':value['status'],'observed':len(observed)})


def deliver(ws):
    """Recover the journal-to-existing-pending handoff; not another scheduler."""
    from .commission_registry import Registry
    from . import publication_scope
    publication=publication_scope.resolve(ws.root)
    registry=Registry(ws.root)
    rows=ws.conn.execute("SELECT a.*,c.store_id AS case_store_id,c.period AS case_period FROM automation_attempt a "
        "LEFT JOIN automation_case c ON c.id=a.case_id WHERE a.state='dispatch_pending' ORDER BY a.rowid").fetchall()
    delivered=0
    for row in rows:
        payload=json.loads(row['result_json'])
        if publication is not None:
            sid,period=row['case_store_id'],row['case_period']
            if not sid or not period or payload.get('store_id')!=sid:
                publication_scope.hold(publication,ws.root,row['id'],'dispatch_scope_metadata_unverified',store_id=sid,period=period)
                continue
            if not publication.allows(sid,period):
                publication_scope.hold(publication,ws.root,row['id'],'dispatch_outside_approved_scope',store_id=sid,period=period)
                continue
            publication.require(sid,period,workspace_root=ws.root)
        try:
            registry.enqueue_automation({payload['store_id']},payload['fingerprint'])
            if payload.get('not_before'):
                # Delay only this exact evidence retry. A newer file/feed event
                # keeps its own fingerprint and immediate accounting priority.
                with registry.transaction() as queue:
                    queue.execute('UPDATE pending SET next_attempt=max(next_attempt,?) WHERE store_id=? AND source_fingerprint=?',
                        (int(payload['not_before']),payload['store_id'],payload['fingerprint']))
            with ws.transaction() as conn:
                if publication is not None:publication.require(sid,period,workspace_root=ws.root)
                conn.execute("UPDATE automation_attempt SET state='queued' WHERE id=? AND state='dispatch_pending'",(row['id'],))
            delivered+=1
        except Exception:
            # The durable attempt remains pending; Manager retries the handoff.
            break
    return delivered


def advance(ws,model,period,request_id,actor):
    human(actor);_request(request_id)
    key='advance:'+request_id;signature=journal.digest([key,period,actor['id']])
    with ws.transaction() as conn:
        old=journal.receipt(conn,key,signature)
        if old:return old
        current=month(ws,model,period)
        aid=journal.authorize(conn,key,actor,'advance',current['input_sha'],'',
            {'period':period,'store_ids':[r['store_id'] for r in current['stores'] if r['status']!='completed'],
             'business_rules_require_grant':True})
        for row in current['stores']:
            _case(conn,row)
            if row['status']=='completed' or row.get('waiting') or row['stage']=='company_finance':continue
            attempt=journal.ident()
            fingerprint='automation:'+journal.digest([row['case_id'],row['input_sha'],request_id])
            conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
                (attempt,row['case_id'],request_id,row['input_sha'],'dispatch_pending',
                 journal.encoded({'store_id':row['store_id'],'fingerprint':fingerprint}),journal.now()))
        result=journal.finish(conn,key,'advance',signature,aid,current)
    from .automation_deterministic import run_available
    if not current.get('company_finance_only'):run_available(ws,model,period,aid)
    deliver(ws)
    return result


def _proposal(ws,actor,kind,period,store_id,input_sha,payload):
    pid=journal.ident();expires=int(time.time())+1800
    sha=journal.digest({'id':pid,'kind':kind,'period':period,'store_id':store_id,'input_sha':input_sha,'payload':payload})
    ws.conn.execute('INSERT INTO automation_proposal VALUES(?,?,?,?,?,?,?,?,?,?)',
        (pid,kind,period,store_id,input_sha,sha,journal.encoded(payload),actor['id'],expires,journal.now()))
    return {'proposal_id':pid,'proposal_sha':sha,'input_sha':input_sha,'kind':kind,
            'period':period,'store_id':store_id,'effect':payload,'expires_at':expires}


def decision_preview(ws,model,*,request_id,period,store_id,kind,values,reason,actor):
    human(actor);_request(request_id);journal.period(period)
    if not reason.strip():raise Conflict('请填写业务决定的依据')
    ids={s.id for s in model.stores}
    if kind!='overhead_total' and store_id not in ids:raise Conflict('请选择本月已登记店铺')
    if kind=='store_scope':
        if set(values)-{'included','valid_from','valid_until'} or not isinstance(values.get('included'),bool):raise Conflict('营业范围须明确包含或排除')
        start,end=values.get('valid_from',period),values.get('valid_until',period)
        journal.period(start);journal.period(end)
        if end<start:raise Conflict('营业有效月份的结束不能早于开始')
        values={**values,'valid_from':start,'valid_until':end}
    elif kind=='overhead_total':
        if store_id or set(values)!={'amount'}:raise Conflict('兼职总额属于公司月份范围')
        values={'amount':_money(values['amount'])}
    elif kind=='rule_grant':
        from datetime import datetime,timezone
        from .automation_rules import contracts
        known=contracts()
        expected={'rule_id','rule_sha','enabled','valid_from','valid_until'}
        if not expected<=set(values) or set(values)-expected-{'period_from','period_until'} or values['rule_id'] not in known or values['rule_sha']!=known[values['rule_id']][0] or not isinstance(values['enabled'],bool):
            raise Conflict('请确认已发布的确定性规则版本、范围和启用状态')
        RULE_CONTRACT=known[values['rule_id']][1]
        try:
            start=datetime.fromisoformat(values['valid_from']);end=datetime.fromisoformat(values['valid_until'])
            if start.tzinfo is None or end.tzinfo is None or end<=start:raise ValueError()
        except (ValueError,TypeError):raise Conflict('规则授权必须填写带时区且有先后顺序的有效期')
        values={**values,'valid_from':start.astimezone(timezone.utc).isoformat(),
                'valid_until':end.astimezone(timezone.utc).isoformat()}
        first,last=values.get('period_from',period),values.get('period_until',period)
        journal.period(first);journal.period(last)
        if last<first:raise Conflict('授权业务月范围无效')
        values.update(period_from=first,period_until=last)
    elif kind=='cost_line':
        allowed={'run_id','coverage_key','context_sha','amount','action','expected_line_revision'}
        if set(values)-allowed:raise Conflict('成本决定包含未知字段')
        from . import cost_lines
        source=next((r for r in cost_lines._source_rows(ws,int(values.get('run_id',0)),for_edit=True)
                     if r['coverage_key']==values.get('coverage_key')),None)
        if not source or source['order_count']!=1 or source['context_sha']!=values.get('context_sha'):
            raise Conflict('订单成本证据已变化或存在歧义')
        values={**values,'amount':_money(values['amount']) if values.get('action','save')=='save' else None}
    else:raise Conflict('不支持的业务决定类型')
    key='decision-preview:'+request_id;signature=journal.digest([key,period,store_id,kind,values,reason,actor['id']])
    with ws.transaction() as conn:
        old=journal.receipt(conn,key,signature)
        if old:return old
        sha=_decision_input(ws,model,kind,store_id,period,values)
        aid=journal.authorize(conn,key,actor,'decision.preview',sha,'',{'period':period,'store_id':store_id})
        payload={'kind':kind,'values':values,'reason':reason.strip()}
        first,last=_policy_range(kind,values,period)
        payload['effective_month_range']={'from':first,'until':last,'default_single_month':first==last==period}
        if kind=='rule_grant':payload['rule_contract']=RULE_CONTRACT
        result=_proposal(ws,actor,'decision',period,store_id,sha,payload)
        return journal.finish(conn,key,'decision.preview',signature,aid,result)


def _load_proposal(conn,pid,sha,input_sha,actor,kind):
    row=conn.execute('SELECT * FROM automation_proposal WHERE id=?',(pid,)).fetchone()
    if not row or row['kind']!=kind or row['proposal_sha']!=sha or row['input_sha']!=input_sha:
        raise Conflict('预览凭据不匹配，请重新预览')
    if row['actor']!=actor['id']:raise PermissionError('此预览属于另一位确认人')
    if row['expires_at']<int(time.time()):raise Conflict('预览已过期，请重新核对')
    return dict(row),json.loads(row['payload_json'])


def decision_apply(ws,model,*,request_id,proposal_id,proposal_sha,input_sha,actor):
    human(actor);_request(request_id)
    key='decision-apply:'+request_id;signature=journal.digest([key,proposal_id,proposal_sha,input_sha,actor['id']])
    with ws.transaction() as conn:
        old=journal.receipt(conn,key,signature)
        if old:return old
        proposal,payload=_load_proposal(conn,proposal_id,proposal_sha,input_sha,actor,'decision')
        period,store_id,kind=proposal['period'],proposal['store_id'],payload['kind']
        scope='' if kind in {'overhead_total','store_scope'} else store_id
        if _decision_input(ws,model,kind,store_id,period,payload['values'])!=input_sha:raise Conflict('预览后输入已变化，本次未应用，请重新预览')
        frozen=conn.execute("SELECT 1 FROM period WHERE period=? AND state='closed' LIMIT 1",(period,)).fetchone()
        if frozen and kind=='overhead_total':raise Conflict('本月已有冻结账，不能直接改变营业范围或公摊总额')
        if kind=='store_scope':
            first,last=_policy_range(kind,payload['values'],period)
            _frozen_scope_check(conn,store_id,first,last,payload['values']['included'])
        aid=journal.authorize(conn,key,actor,'decision.apply',input_sha,proposal_sha,{'period':period,'store_id':store_id})
        did=journal.ident()
        if kind=='cost_line':
            from .cost_lines import save
            value=payload['values']
            save(ws,store_id,period,int(value['run_id']),coverage_key=value['coverage_key'],
                context_sha=value['context_sha'],amount=value.get('amount'),reason=payload['reason'],
                action=value.get('action','save'),expected_line_revision=int(value.get('expected_line_revision',0)),by=actor['id'])
            applied=conn.execute('SELECT max(id) FROM cost_line_log WHERE store_id=? AND period=? AND coverage_key=?',
                (store_id,period,value['coverage_key'])).fetchone()[0]
            payload['values']={**value,'_line_log_id':applied}
        conn.execute('INSERT INTO automation_decision VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (did,key,kind,period,store_id,'apply','',journal.encoded(payload['values']),payload['reason'],input_sha,proposal_sha,actor['id'],journal.now()))
        if kind=='cost_line':
            case_id=journal.digest([store_id,period])[:32]
            checked=checker.check(ws,store_id,period,model=model)
            _case(conn,{'case_id':case_id,'store_id':store_id,'period':period,'status':'processing',
                'stage':'queued','input_sha':checked['input_sha'],'run_id':checked['run_id'],'issues':checked['issues']})
            conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
                (journal.ident(),case_id,request_id,checked['input_sha'],'dispatch_pending',
                 journal.encoded({'store_id':store_id,'fingerprint':'automation:'+journal.digest([did,input_sha])}),journal.now()))
        _audit_cases(ws,model,period,key,'decision_applied',{'decision_id':did,'kind':kind})
        result=journal.finish(conn,key,'decision.apply',signature,aid,{'decision_id':did,'status':'applied','period':period,'store_id':store_id})
    if kind=='cost_line':deliver(ws)
    return result


def decision_revoke(ws,model,decision_id,*,request_id,reason,input_sha,actor):
    from .service import _store_lock
    from .finance_guard import guard
    row=ws.conn.execute('SELECT store_id,period FROM automation_decision WHERE id=?',(decision_id,)).fetchone()
    if not row:raise Conflict('业务决定不存在')
    # Match publication's lock order before entering the writer transaction.
    with _store_lock(row['store_id']),guard(ws.root,row['store_id'],row['period']):
        return _decision_revoke(ws,model,decision_id,request_id=request_id,reason=reason,input_sha=input_sha,actor=actor)


def _decision_revoke(ws,model,decision_id,*,request_id,reason,input_sha,actor):
    human(actor);_request(request_id)
    if not reason.strip():raise Conflict('撤销决定必须填写原因')
    key='decision-revoke:'+request_id;signature=journal.digest([key,decision_id,reason,input_sha,actor['id']])
    with ws.transaction() as conn:
        old=journal.receipt(conn,key,signature)
        if old:return old
        row=conn.execute("SELECT * FROM automation_decision WHERE id=? AND action='apply'",(decision_id,)).fetchone()
        if not row:raise Conflict('业务决定不存在')
        scope='' if row['kind'] in {'overhead_total','store_scope'} else row['store_id']
        if _decision_input(ws,model,row['kind'],row['store_id'],row['period'],json.loads(row['values_json']))!=input_sha:raise Conflict('决定依据已变化，请刷新后再撤销')
        first,last=_policy_range(row['kind'],json.loads(row['values_json']),row['period'])
        company_policy=row['kind'] in {'store_scope','overhead_total'}
        if company_policy and conn.execute("SELECT 1 FROM period WHERE period BETWEEN ? AND ? AND state='closed' LIMIT 1",(first,last)).fetchone():
            raise Conflict('生效范围已有冻结账，不能直接撤销其决定；请另设未来生效范围')
        if row['kind']!='rule_grant' and not company_policy and conn.execute("SELECT 1 FROM period WHERE store_id=? AND period=? AND state='closed'",(row['store_id'],row['period'])).fetchone():
            raise Conflict('本店本月已冻结，不能直接撤销其凭据决定')
        if conn.execute("SELECT 1 FROM automation_decision WHERE action='revoke' AND ref_id=?",(decision_id,)).fetchone():raise Conflict('该决定已撤销')
        aid=journal.authorize(conn,key,actor,'decision.revoke',input_sha,'',{'decision_id':decision_id})
        from .automation_cost_actions import RULE as history_rule
        from .historical_cost_facts import RULE as source_facts_rule
        from .automation_deterministic import RULE as statement_rule
        affects_cost=row['kind'] in {'cost_line',history_rule}
        affects_statement=row['kind']==statement_rule
        from .automation_source_cost_v3 import RULE as physical_source_rule
        affects_source_facts=row['kind'] in {source_facts_rule,physical_source_rule}
        if affects_cost:
            value=json.loads(row['values_json'])
            if row['kind']==history_rule:value={**value,'coverage_key':value['coverage']['coverage_key'],'_line_log_id':value['line_log_id']}
            latest=conn.execute('SELECT * FROM cost_line_log WHERE store_id=? AND period=? AND coverage_key=? ORDER BY id DESC LIMIT 1',
                (row['store_id'],row['period'],value['coverage_key'])).fetchone()
            run=ws.latest_run(row['store_id'],row['period'])
            if not latest or latest['id']!=value.get('_line_log_id') or latest['action']!='save' or not run:
                raise Conflict('该订单存在后续成本决定，请先核对当前版本')
            # A withdrawal only removes this exact prior authority. It never
            # changes source rows or manufactures an alternative amount.
            conn.execute('INSERT INTO cost_line_log(store_id,period,coverage_key,context_sha,source_run_id,action,amount,reason,at,by) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (row['store_id'],row['period'],value['coverage_key'],latest['context_sha'],run['id'],'remove',None,reason.strip(),journal.now(),actor['id']))
            conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
                (journal.ident(),journal.digest([row['store_id'],row['period']])[:32],key,input_sha,'dispatch_pending',
                 journal.encoded({'store_id':row['store_id'],'fingerprint':'automation:'+journal.digest([decision_id,key])}),journal.now()))
        if affects_statement:
            from .statement_review import revoke
            value=json.loads(row['values_json']);run=ws.latest_run(row['store_id'],row['period'])
            if not run:raise Conflict('原始核算已不可用，请先核对')
            latest=conn.execute("SELECT id FROM config_log WHERE kind='statement-review' AND summary=? AND json_extract(after_json,'$.group_id')=? ORDER BY id DESC LIMIT 1",
                (row['store_id'],value['group_id'])).fetchone()
            if not latest or latest['id']!=value['audit_id']:raise Conflict('本组已有后续流水决定，请先核对当前版本')
            revoke(ws,row['store_id'],row['period'],run['id'],value['group_id'],reason,actor['id'])
            conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
                (journal.ident(),journal.digest([row['store_id'],row['period']])[:32],key,input_sha,'dispatch_pending',
                 journal.encoded({'store_id':row['store_id'],'fingerprint':'automation:'+journal.digest([decision_id,key])}),journal.now()))
        if affects_source_facts:
            conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
                (journal.ident(),journal.digest([row['store_id'],row['period']])[:32],key,input_sha,'dispatch_pending',
                 journal.encoded({'store_id':row['store_id'],'fingerprint':'automation:'+journal.digest([decision_id,key])}),journal.now()))
        conn.execute('INSERT INTO automation_decision VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (journal.ident(),key,row['kind'],row['period'],row['store_id'],'revoke',decision_id,'{}',reason.strip(),input_sha,'',actor['id'],journal.now()))
        _audit_cases(ws,model,row['period'],key,'decision_revoked',{'decision_id':decision_id})
        result=journal.finish(conn,key,'decision.revoke',signature,aid,{'decision_id':decision_id,'status':'revoked'})
    if affects_cost or affects_statement or affects_source_facts:deliver(ws)
    return result


def close_preview(ws,model,*,request_id,period,store_ids,actor):
    human(actor);_request(request_id);journal.period(period)
    known={s.id for s in checker.operating_stores(ws,model,period)}
    selected=sorted(set(store_ids or known))
    if set(selected)-known:raise Conflict('批次包含未登记营业店铺')
    if not selected:raise Conflict('请选择需要结账的店铺')
    key='close-preview:'+request_id;signature=journal.digest([key,period,selected,actor['id']])
    from .feed_freshness import close_boundaries,hold_close
    try:boundaries=close_boundaries(ws,period,selected,request_id,actor)
    except ValueError as exc:raise Conflict(str(exc)) from exc
    # Capture the physical source first. A concurrent financial recompute for
    # quote collection must not obsolete an in-flight source Worker's lease.
    # These durable technical tasks confer no financial/close authorization.
    from .source_recovery import ensure_close_refresh,verify_close_refresh,SourcePending
    from .automation_source_cost_v3 import ensure_quote_refresh,verify_quote_refresh
    from .automation_cost_actions import Pending
    try:
        source_refresh=ensure_close_refresh(ws,model,period=period,store_ids=selected,
            request_id=request_id,actor=actor)
    except SourcePending as exc:
        return {'status':'pending','request_id':request_id,'period':period,'store_ids':selected,
            'financial_applied':False,'proposal_id':None,'reason_codes':[exc.reason],
            'message':'已采用来源的当前依据存在待核事项，本次未生成结账预览'}
    if source_refresh['status']!='verified':
        return {'status':source_refresh['status'],'request_id':request_id,'period':period,
            'store_ids':selected,'source_refresh':source_refresh,'financial_applied':False,
            'proposal_id':None,'message':'正在按本批固定范围重新核查上游来源；完成后继续本次预览'}
    try:
        quote_refresh=ensure_quote_refresh(ws,model,period,selected,request_id,actor)
    except Pending as exc:
        return {'status':'pending','request_id':request_id,'period':period,'store_ids':selected,
            'source_refresh':source_refresh,'financial_applied':False,'proposal_id':None,
            'reason_codes':[exc.code],'message':'原日历史价依据待核，本次未生成结账预览'}
    if quote_refresh['status']!='verified':
        return {'status':quote_refresh['status'],'request_id':request_id,'period':period,
            'store_ids':selected,'source_refresh':source_refresh,'source_quote_refresh':quote_refresh,
            'financial_applied':False,'proposal_id':None,
            'message':'正在核对本批原日历史报价；完成后继续本次预览'}
    with hold_close(boundaries),ws.transaction() as conn:
        old=journal.receipt(conn,key,signature)
        if old:return old
        if source_refresh['required']:
            verify_close_refresh(ws,model,refresh_id=source_refresh['refresh_id'],
                generation_sha=source_refresh['generation_sha'])
        verify_quote_refresh(ws,model,period,selected,quote_refresh['refresh_id'],quote_refresh['quote_sha'])
        eligible=[];blocked=[]
        for sid in selected:
            state=ws.state(sid,period)
            if state and state.closed:continue
            result=checker.check(ws,sid,period,model=model,verify_artifacts=True)
            value={'store_id':sid,'run_id':result['run_id'],'input_sha':result['input_sha'],'issues':result['issues']}
            (eligible if result['allowed'] else blocked).append(value)
        sha=_scope_input(ws,model,period)
        company=checker.company_basis(ws,model,period)
        payload={'store_ids':selected,'eligible':eligible,'blocked':blocked,'overhead':company,'feed_boundaries':boundaries,
            'source_refresh':source_refresh,'source_quote_refresh':quote_refresh}
        aid=journal.authorize(conn,key,actor,'close.preview',sha,'',{'period':period,'store_ids':selected})
        result=_proposal(ws,actor,'close',period,'',sha,payload)
        result.update(eligible=eligible,blocked=blocked,overhead=company)
        return journal.finish(conn,key,'close.preview',signature,aid,result)


def close_confirm(ws,model,*,request_id,proposal_id,proposal_sha,input_sha,actor):
    human(actor);_request(request_id)
    key='close-confirm:'+request_id;signature=journal.digest([key,proposal_id,proposal_sha,input_sha,actor['id']])
    from .finance_guard import authority
    with authority(ws.root),ws.transaction() as conn:
        old=journal.receipt(conn,key,signature)
        if old:return old
        proposal,payload=_load_proposal(conn,proposal_id,proposal_sha,input_sha,actor,'close')
        period=proposal['period']
        from .feed_freshness import hold_close
        with hold_close(payload.get('feed_boundaries',{})):
            from .source_recovery import verify_close_refresh
            from .automation_source_cost_v3 import verify_quote_refresh
            source_refresh=payload.get('source_refresh') or {}
            quote_refresh=payload.get('source_quote_refresh') or {}
            if source_refresh.get('required'):
                verify_close_refresh(ws,model,refresh_id=source_refresh['refresh_id'],
                    generation_sha=source_refresh['generation_sha'])
            verify_quote_refresh(ws,model,period,payload['store_ids'],
                quote_refresh.get('refresh_id'),quote_refresh.get('quote_sha'))
            if _scope_input(ws,model,period)!=input_sha:raise Conflict('预览后输入已变化，本批次未结账，请重新预览')
            aid=journal.authorize(conn,key,actor,'close.confirm',input_sha,proposal_sha,{'period':period,'store_ids':payload['store_ids']})
            completed=[];unfinished=list(payload['blocked'])
            for target in payload['eligible']:
                sid=target['store_id']
                try:
                    current=checker.check(ws,sid,period,model=model,expected_run_id=target['run_id'])
                    if not current['allowed'] or current['input_sha']!=target['input_sha']:raise Conflict('本店预览后依据已变化')
                    state=ws.close_period(sid,period,by=actor['id'],note='月度批量结账（人工确认）',
                        expected_run_id=target['run_id'],model=model)
                    completed.append({'store_id':sid,'run_id':state.run_id})
                except WorkspaceError as exc:
                    unfinished.append({'store_id':sid,'issues':[checker.issue('close_not_completed',str(exc))]})
            result={'batch_id':proposal_id,'period':period,'status':'completed' if not unfinished else 'partial',
                    'completed':completed,'unfinished':unfinished}
            _audit_cases(ws,model,period,key,'close_verified',result)
            return journal.finish(conn,key,'close.confirm',signature,aid,result)
