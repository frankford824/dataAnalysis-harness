"""Evidence-bound exact actions. No model chooses an order, amount or date."""
from __future__ import annotations

import json
import os
from datetime import datetime,timezone

from . import automation_store as journal
from . import statement_review
from .workspace import WorkspaceError

RULE='statement_unique_candidate.v1'
IDENTITY='service:deterministic-ledger'
RULE_CONTRACT={'id':RULE,'money':'preserve exact original amount, time and subject',
    'scope':'one store and one accounting month; complete immutable source group',
    'selection':'exactly one candidate with proven original-order linkage',
    'authority':'no close, reopening, payout or policy approval'}
RULE_SHA=journal.digest(RULE_CONTRACT)


def enabled():
    return os.environ.get('LEDGER_DETERMINISTIC_AUTOMATION_ENABLED','').lower() in {'1','true','yes','on'}


def approved_rule(ws,store_id,period,grant_id=None,*,rule_id=RULE,rule_sha=RULE_SHA):
    from . import publication_scope
    try:
        publication=(publication_scope.current() or publication_scope.load_automatic(ws.root))
        publication.require(store_id,period,workspace_root=ws.root)
    except publication_scope.PublicationScopeError as exc:
        raise PermissionError('自动发布范围尚未验证或不含本店账期：'+str(exc)) from exc
    from . import agent_upgrade
    if agent_upgrade.blocking(ws,None,period):raise PermissionError('新版本核算结果有变化，等待人工确认后才自动处理')
    grant=journal.active_decisions(ws.conn,period).get((store_id,'rule_grant:'+rule_id))
    if not grant or (grant_id and grant['id']!=grant_id):raise PermissionError('缺少当前已批准的店月规则授权')
    value=grant['values'];stamp=datetime.now(timezone.utc)
    if (not value.get('enabled') or value.get('rule_id')!=rule_id or value.get('rule_sha')!=rule_sha
        or not datetime.fromisoformat(value['valid_from'])<=stamp<datetime.fromisoformat(value['valid_until'])):
        raise PermissionError('规则灰度范围、版本或有效期不匹配')
    authorization=ws.conn.execute("SELECT 1 FROM automation_authorization WHERE request_id=? AND identity_kind='human' AND action='decision.apply'",
        (grant['request_id'],)).fetchone()
    if not authorization:raise PermissionError('规则授权没有已认证人工批准依据')
    return grant


def preview_statement(ws,model,store_id,period,run_id,group_id):
    from .automation_actions import _scope_input
    page=statement_review.context(ws,store_id,period,run_id,model,limit=50)
    offset=0;group=None
    while True:
        group=next((g for g in page['groups'] if g['group_id']==group_id),None)
        if group or offset+50>=page['total']:break
        offset+=50;page=statement_review.context(ws,store_id,period,run_id,model,offset=offset,limit=50)
    if not group or not group['can_confirm']:raise WorkspaceError('流水完整性尚未验证，不能自动关联')
    choices=[m for m in group['members'] if m.get('can_select')]
    if len(choices)!=1:raise WorkspaceError('必须且只能有一个已验证原订单候选；多候选交业务决定')
    value={'rule':RULE,'store_id':store_id,'period':period,'run_id':run_id,
        'group_id':group_id,'chosen':choices[0]['anchor'],'evidence':group,
        'input_sha':_scope_input(ws,model,period,store_id)}
    return {**value,'proposal_sha':journal.digest(value)}


def apply_statement(ws,model,plan,parent_authorization):
    from .version import assert_code_integrity
    assert_code_integrity()
    from .automation_actions import _scope_input
    from .finance_guard import guard
    from .service import _store_lock
    sid,period=plan['store_id'],plan['period']
    if not enabled():raise PermissionError('确定性规则自动化尚未启用')
    key='deterministic-statement:'+journal.digest([sid,period,plan['group_id'],RULE])
    signature=journal.digest([key,plan['input_sha'],plan['proposal_sha']])
    with _store_lock(sid),guard(ws.root,sid,period),ws.transaction() as conn:
        assert_code_integrity()
        withdrawn=conn.execute("SELECT 1 FROM automation_decision r JOIN automation_decision d ON d.id=r.ref_id WHERE r.action='revoke' AND d.kind=? AND d.period=? AND d.store_id=? AND json_extract(d.values_json,'$.group_id')=?",
            (RULE,period,sid,plan['group_id'])).fetchone()
        if withdrawn:raise PermissionError('本组自动决定已被人工撤回，等待业务核对')
        old=journal.receipt(conn,key,signature)
        if old:return old
        grant=approved_rule(ws,sid,period,parent_authorization)
        fresh=preview_statement(ws,model,sid,period,plan['run_id'],plan['group_id'])
        if fresh['input_sha']!=plan['input_sha'] or fresh['proposal_sha']!=plan['proposal_sha']:
            raise WorkspaceError('流水处理预览后依据已变化，本次未应用')
        aid=journal.ident()
        conn.execute('INSERT INTO automation_authorization VALUES(?,?,?,?,?,?,?,?,?)',
            (aid,key,IDENTITY,'delegated_service',RULE,plan['input_sha'],plan['proposal_sha'],
             journal.encoded({'approved_rule_grant':grant['id'],'rule_sha':RULE_SHA,'store_id':sid,'period':period}),journal.now()))
        result=statement_review.confirm(ws,model,sid,period,plan['run_id'],plan['group_id'],plan['chosen'],
            '确定性自动关联：同一流水金额时间科目互证，且仅一个原订单候选有完整关联证据',True,IDENTITY)
        did=journal.ident()
        conn.execute('INSERT INTO automation_decision VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (did,key,RULE,period,sid,'apply','',journal.encoded({'audit_id':result['audit_id'],
                'group_id':plan['group_id'],'chosen':plan['chosen'],'evidence':plan['evidence']}),
             '固定规则唯一候选，不编辑原始金额与订单号',plan['input_sha'],plan['proposal_sha'],IDENTITY,journal.now()))
        case_id=journal.digest([sid,period])[:32]
        conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
            (journal.ident(),case_id,key,plan['input_sha'],'dispatch_pending',
             journal.encoded({'store_id':sid,'fingerprint':'automation:'+journal.digest([did,plan['input_sha']])}),journal.now()))
        return journal.finish(conn,key,RULE,signature,aid,{'status':'applied','decision_id':did,
            'store_id':sid,'period':period,'source_run_id':plan['run_id'],'audit_id':result['audit_id'],
            'recompute_required':True,'rule':RULE})


def run_available(ws,model,period,parent_authorization=None,store_ids=None):
    """Run from source arrival/Manager as well as explicit advance, using grants."""
    if not enabled():return []
    from . import agent_authority,agent_upgrade
    if agent_upgrade.blocking(ws,model,period):return []
    agent_authority.extend(ws,model,period)
    scopes=journal.active_decisions(ws.conn,period)
    candidates=[sid for (sid,kind),grant in scopes.items() if kind=='rule_grant:'+RULE]
    if store_ids is not None:candidates=[sid for sid in candidates if sid in store_ids]
    results=[]
    for sid in candidates:
        if len(results)>=20:break
        try:grant=approved_rule(ws,sid,period)
        except (PermissionError,ValueError):continue
        row=ws.latest_run(sid,period);state=ws.state(sid,period)
        if not row or (state and state.closed):continue
        try:page=statement_review.context(ws,sid,period,row['id'],model,limit=50)
        except WorkspaceError:continue
        for group in page['groups']:
            if len(results)>=20:break
            if not group['can_confirm'] or sum(bool(m.get('can_select')) for m in group['members'])!=1:continue
            try:
                plan=preview_statement(ws,model,sid,period,row['id'],group['group_id'])
                results.append(apply_statement(ws,model,plan,grant['id']))
            except (WorkspaceError,ValueError,PermissionError):
                # Nothing is acknowledged as solved. The original durable work
                # and raw evidence remain for subsequent compute/investigation.
                continue
    return results
