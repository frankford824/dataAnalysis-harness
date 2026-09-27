"""Evidence-bound, append-only manual selection of a conflicting source row.

Never accepts an edited amount or arbitrary order ID. Decisions apply only to
an unchanged complete event group. Raw files and closed-period pointers stay
untouched. A changed upload or new conflicting copy invalidates the group hash.
"""
import hashlib
import json
import math
import re
from datetime import datetime,timezone
import polars as pl
from .workspace import WorkspaceError, SHARED_STORE_ID
from .storage_integrity import verified, digest
from .finance_guard import guard
from .version import engine_version


def anchor(row):
    return f"{row['file_sha']}:{row.get('sheet') or ''}:{row['row_no']}"


def event_key(group):
    members=group.get('members') or []
    return (group.get('namespace'),group.get('leg','event'),members[0].get('event_id') if members else None)


def active_shas(ws,store_id):
    rows=ws.conn.execute('select name,sha from slot where store_id in (?,?) order by case when store_id=? then 0 else 1 end,name',
                         (store_id,SHARED_STORE_ID,store_id)).fetchall()
    by_name={}
    for row in rows:by_name.setdefault(row['name'],row['sha'])
    return set(by_name.values())


def _eligibility(group,local_anchors,store,period):
    members=group['members']
    if group.get('truncated') or len(members)<2:return '需补充可互证的完整原始流水，不能直接忽略'
    events={r['event_id'] for r in members}
    if len(events)!=1 or any(not x or re.search(r'(?i)e[+-]?\d+$',x) for x in events):
        return '流水号精度不足，需平台原始账单互证，不能猜补编号'
    if not all(r['valid'] and r['time'] and all(v is not None and math.isfinite(v) for v in [r['income'],r['outgo']]) for r in members):
        return '时间或金额不完整，不能核定'
    try:
        for member in members:datetime.fromisoformat(member['time'])
    except (ValueError,TypeError):return '原始流水发生时间无效，请重新导出原始账单'
    if len({(r['time'],r['income'],r['outgo'],r['subject']) for r in members})!=1:
        return '同一流水的金额、时间或科目不一致，请重新导出原始账单；不能人工选择金额'
    if not all(anchor(r) in local_anchors for r in members):return '候选记录涉及其他或未知账期，不能只在本月确认'
    if any({(s.get('store'),s.get('period')) for s in r.get('scopes',[])} != {(store,period)} for r in members):
        return '候选原始行涉及其他店铺、跨账期或归属未知，需要先核对归属，不能仅在本期选择'
    return ''


def context(ws,store_id,period,run_id,model,offset=0,limit=20):
    row=ws.conn.execute('select * from run where id=? and store_id=? and period=?',(run_id,store_id,period)).fetchone()
    if not row:raise WorkspaceError('核算记录不属于本店本月')
    path=ws.facts_path(run_id)
    if not row['evidence_ready'] or not path.exists() or not verified(path):raise WorkspaceError('核对证据留档不完整，禁止确认')
    payload=json.loads(row['result']);groups={}
    for d in payload.get('deduplication',[]):
        if d.get('status')=='conflict':
            for g in d.get('groups',[]):groups[g['group_id']]=g
    frame=pl.read_parquet(path,columns=['file_sha','sheet','row_no','linked'])
    anchors={anchor(r) for r in frame.unique().to_dicts()}
    linked={anchor(r) for g in groups.values() for r in g['members']
            if any(s.get('linked') for s in r.get('scopes',[]))}
    latest=ws.latest_run(store_id,period);state=ws.state(store_id,period)
    frozen=bool(state and state.state=='closed')
    stale=not latest or latest['id']!=run_id
    stale=stale or row['engine']!=engine_version()
    stale=stale or row['model_revision']!=hashlib.sha256(model.model_dump_json().encode()).hexdigest()
    stale=stale or not active_shas(ws,store_id).issubset(set(json.loads(row['shas'])))
    decisions=[json.loads(r[0]) for r in ws.conn.execute("select after_json from config_log where kind='statement-review' and summary=? order by id desc",(store_id,))]
    last={}
    for d in decisions:last.setdefault(d['group_id'],d)
    done={key for key,d in last.items() if d.get('action','confirm')=='confirm'}
    items=[]
    for g in sorted(groups.values(),key=lambda g:g['group_id']):
        reason=('已结账账期仅供查看，不得直接修改' if frozen else '资料或计算版本已更新，请先刷新重算' if stale else
                '本组已保存核对，等待重算；请勿重复提交' if g['group_id'] in done else _eligibility(g,anchors,payload.get('store'),period))
        if not reason:
            for decision in last.values():
                old=decision.get('evidence') or {}
                if (decision.get('action','confirm')!='revoke' and event_key(old)==event_key(g)
                    and decision['period']!=period):
                    previous=ws.state(store_id,decision['period'])
                    if previous and previous.state=='closed':
                        reason='同一流水曾用于其他已结账账期，请先进行审计更正，不能直接跨期重新确认'
                        break
        if not reason and not any(anchor(r) in linked for r in g['members']):reason='没有已匹配原订单的候选，请先补齐订单明细或平台关联依据'
        items.append({**g,'can_confirm':not reason,'reason':reason,
                      'members':[{**r,'anchor':anchor(r),'can_select':anchor(r) in linked} for r in g['members']]})
    return dict(run_id=run_id,total=len(items),groups=items[max(0,offset):max(0,offset)+min(max(limit,1),50)],
        history=[{k:d.get(k) for k in ['group_id','run_id','period','reason','actor','at','action']}
                 for d in last.values() if d['period']==period],read_only=frozen or stale,
        needs_recompute=bool(any(d.get('status')=='conflict' for d in payload.get('deduplication',[])) and not groups))


def confirm(ws,model,store_id,period,run_id,group_id,chosen,reason,acknowledged,actor):
    from .service import _store_lock
    if not acknowledged or not actor.strip() or len(reason.strip())<8:raise WorkspaceError('请核对平台原始流水、填写核对人及至少8字依据；不能用忽略代替核对')
    with _store_lock(store_id),guard(ws.root,store_id,period):
        page=context(ws,store_id,period,run_id,model,limit=50)
        # Query requested group from the immutable payload rather than trusting
        # a browser-supplied candidate list or dropping groups beyond page one.
        offset=0;found=None
        while True:
            found=next((g for g in page['groups'] if g['group_id']==group_id),None)
            if found or offset+50>=page['total']:break
            offset+=50;page=context(ws,store_id,period,run_id,model,offset=offset,limit=50)
        if not found:raise WorkspaceError('核对组已变化或不存在，请刷新')
        if not found['can_confirm']:raise WorkspaceError(found['reason'])
        selected=next((r for r in found['members'] if r['anchor']==chosen),None)
        if not selected:raise WorkspaceError('只能选择本组已留档的原始记录，不能输入任意订单或金额')
        if not selected['can_select']:raise WorkspaceError('该候选尚未匹配原订单，不能将未知关联直接确认为原订单')
        for sha in {r['file_sha'] for r in found['members']}:
            if not re.fullmatch('[0-9a-f]{64}',sha):raise WorkspaceError('原始账单标识无效，禁止确认')
            path=ws.path_of(sha)
            if not path.exists() or digest(path)!=sha:raise WorkspaceError('原始账单留档缺失或校验失败，禁止确认')
        decision=dict(store_id=store_id,period=period,run_id=run_id,group_id=group_id,chosen=chosen,
            reason=reason.strip(),actor=actor,at=datetime.now(timezone.utc).isoformat(),evidence=found)
        # Strict transactional audit; unlike best-effort config logging, failure
        # aborts this decision and does not claim it was saved.
        with ws.conn:
            ws.conn.execute('BEGIN IMMEDIATE')
            fresh=ws.latest_run(store_id,period)
            if not fresh or fresh['id']!=run_id or not active_shas(ws,store_id).issubset(set(json.loads(fresh['shas']))):
                raise WorkspaceError('提交时原始资料或计算已更新，请刷新核对')
            cursor=ws.conn.execute('insert into config_log(at,by,kind,summary,before_json,after_json) values(?,?,?,?,?,?)',
                (decision['at'],actor,'statement-review',store_id,json.dumps(found,ensure_ascii=False),json.dumps(decision,ensure_ascii=False)))
        return dict(saved=True,audit_id=cursor.lastrowid,recompute_required=True)


def revoke(ws,store_id,period,run_id,group_id,reason,actor):
    from .service import _store_lock
    if len(reason.strip())<8 or not actor.strip():raise WorkspaceError('撤销需填写核对人及至少8字原因')
    with _store_lock(store_id),guard(ws.root,store_id,period),ws.conn:
        ws.conn.execute('BEGIN IMMEDIATE')
        state=ws.state(store_id,period);latest=ws.latest_run(store_id,period)
        if state and state.state=='closed':raise WorkspaceError('已结账账期不允许直接撤销核对')
        if not latest or latest['id']!=run_id:raise WorkspaceError('计算已更新，请刷新')
        prior=None
        for row in ws.conn.execute("select after_json from config_log where kind='statement-review' and summary=? order by id desc",(store_id,)):
            d=json.loads(row[0])
            if d['group_id']==group_id:prior=d;break
        if not prior or prior['period']!=period or prior.get('action')=='revoke':raise WorkspaceError('没有可撤销的本月核对记录')
        decision={**prior,'action':'revoke','reason':reason.strip(),'actor':actor,
                  'at':datetime.now(timezone.utc).isoformat(),'run_id':run_id}
        cursor=ws.conn.execute('insert into config_log(at,by,kind,summary,before_json,after_json) values(?,?,?,?,?,?)',
            (decision['at'],actor,'statement-review',store_id,json.dumps(prior,ensure_ascii=False),json.dumps(decision,ensure_ascii=False)))
        return dict(saved=True,audit_id=cursor.lastrowid,recompute_required=True)


def apply(ingestion,ws,store_id):
    decisions={}
    for row in ws.conn.execute("select after_json from config_log where kind='statement-review' and summary=? order by id",(store_id,)):
        decision=json.loads(row[0]);decisions[decision['group_id']]=decision
    decisions={k:d for k,d in decisions.items() if d.get('action','confirm')=='confirm'}
    ingestion.statement_decisions=decisions


def bind_scopes(facts,metrics):
    """Bind a review group to its *actual* posting scopes after order linking.

    A raw statement can stay unchanged while a new order export changes its
    month. Such a group must not silently inherit an old manual decision.
    """
    if facts.is_empty() or 'statement_evidence' not in facts.columns:return facts
    pending=facts.filter(pl.col('statement_evidence').is_not_null())
    if pending.is_empty():return facts
    from .engine.project import claims
    claim=pl.lit(False)
    for metric in metrics:claim=claim|claims(metric)
    owned=pending.filter(claim.fill_null(False)|pl.col('major').is_null())
    scopes={}
    for row in owned.select('file_sha','sheet','row_no','store','period',
            (pl.col('linked') & (pl.col('grain')=='order')).alias('matched')).unique().to_dicts():
        scopes.setdefault(anchor(row),set()).add((row['store'],row['period'],bool(row['matched'])))
    mapping={}
    for proof in pending['statement_evidence'].unique().to_list():
        group=json.loads(proof)
        group['base_group_id']=group.get('base_group_id',group['group_id'])
        for member in group['members']:
            member['scopes']=[dict(store=s,period=p,linked=matched) for s,p,matched in sorted(scopes.get(anchor(member),set()))]
        signature=[group['base_group_id'],[(anchor(r),r['scopes']) for r in group['members']]]
        group['group_id']=hashlib.sha256(json.dumps(signature,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        mapping[proof]=json.dumps(group,ensure_ascii=False)
    return facts.with_columns(pl.col('statement_evidence').replace_strict(mapping,default=pl.col('statement_evidence'),return_dtype=pl.String))


def apply_to_facts(facts,decisions):
    if not decisions or facts.is_empty() or 'statement_evidence' not in facts.columns:return facts
    chosen={};notes={}
    for proof in facts['statement_evidence'].drop_nulls().unique().to_list():
        group=json.loads(proof);decision=decisions.get(group['group_id'])
        if not decision:continue
        chosen[proof]=decision['chosen']
        notes[proof]=f"流水关联已人工核对：{decision['reason']}；核对人 {decision['actor']}；时间 {decision['at']}；原始候选记录均保留审计"
    if not chosen:return facts
    selected=pl.col('statement_evidence').replace_strict(chosen,default=None,return_dtype=pl.String)
    key=pl.concat_str(pl.col('file_sha'),pl.lit(':'),pl.col('sheet').fill_null(''),pl.lit(':'),pl.col('row_no').cast(pl.String))
    kept=facts.filter(selected.is_null()|(key==selected))
    return kept.with_columns(
        pl.when(selected.is_not_null()).then(pl.col('statement_evidence').replace_strict(notes,default=None,return_dtype=pl.String))
          .otherwise(pl.col('source_note')).alias('source_note'),
        pl.when(selected.is_not_null()).then(pl.lit(None,dtype=pl.String)).otherwise(pl.col('statement_evidence')).alias('statement_evidence'))
