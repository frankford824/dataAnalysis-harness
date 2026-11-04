"""Existing host-agent protocol over case attempts, with fenced service leases.

Claims do not create another financial scheduler. The ledger pending queue
remains the only accounting executor. Worker output is never close authority.
"""
from __future__ import annotations

import json
import time
from typing import Literal

from fastapi import APIRouter,HTTPException,Request,Response,Header
from pydantic import BaseModel,ConfigDict,Field,field_validator

from . import automation_store as journal,automation_checker as checker


class Lease(BaseModel):
    job_id:str
    lease_token:int


class Configuration(BaseModel):
    model_config=ConfigDict(extra='forbid')
    runtime_sha:str=Field(pattern=r'^[0-9a-f]{64}$')
    source_sha:str=Field(pattern=r'^[0-9a-f]{64}$')


class Heartbeat(BaseModel):
    agent_id:str|None=None
    status:str='online'
    leases:list[Lease]=Field(default_factory=list,max_length=100)
    configuration_sha:str|None=Field(default=None,pattern=r'^[0-9a-f]{64}$')
    configuration:Configuration|None=None


class Claim(BaseModel):
    agent_id:str|None=None
    capabilities:list[str]=Field(default_factory=list)


class Progress(BaseModel):
    sequence:int=Field(ge=0)
    stage:str
    message:str=''
    detail:dict=Field(default_factory=dict)


class Terminal(BaseModel):
    model_config=ConfigDict(extra='allow')
    lease_token:int
    input_sha:str
    result:dict=Field(default_factory=dict)
    message:str=''
    error_code:str='worker_failure'
    retryable:bool=False
    tool_outcomes:list['ToolOutcome']=Field(default_factory=list,max_length=16)


class LocalReference(BaseModel):
    model_config=ConfigDict(extra='forbid')
    path:str=Field(min_length=1,max_length=2048)
    sha256:str=Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator('path')
    @classmethod
    def local_path(cls,value):
        if '\x00' in value or '://' in value:raise ValueError('只接受Worker本机证据引用')
        return value


class ToolOutcome(BaseModel):
    model_config=ConfigDict(extra='forbid')
    tool_id:Literal['collect_case_evidence','request_member_seller_evidence','poll_member_seller_evidence','apply_approved_history_costs','worker_runtime']
    source_kind:Literal['order','history_cost','seller_member','statement','freight','system']
    reason_code:Literal['source_unavailable','login_required','original_evidence_missing','sku_contract_unknown','technical_failure']
    artifact_refs:list[LocalReference]=Field(min_length=1,max_length=32)


Terminal.model_rebuild()


class QuestionAnswer(BaseModel):
    model_config=ConfigDict(extra='forbid')
    lease_token:int
    text:str=Field(min_length=1,max_length=4000)


class QuestionFailure(BaseModel):
    model_config=ConfigDict(extra='forbid')
    lease_token:int
    reason:str=Field(max_length=64)
    retryable:bool=False


def install(app,workspace,model,actor,*,lease_seconds=180,clock=time.time):
    router=APIRouter(tags=['automation-worker'])

    def service(request,agent_id=None):
        value=actor(request) if actor else None
        if (not value or not value.get('id') or value.get('kind')!='service'
            or not value.get('authenticated') or 'business' not in value.get('scopes',[])):
            raise HTTPException(403,'需要已授权的business Worker服务身份')
        if agent_id and agent_id!=value['id']:raise HTTPException(403,'执行器编号与服务令牌不一致')
        return value

    def owned(ws,job_id,identity,token,input_sha=None):
        attempt=ws.conn.execute('SELECT * FROM automation_attempt WHERE id=?',(job_id,)).fetchone()
        case=ws.conn.execute('SELECT * FROM automation_case WHERE id=?',(attempt['case_id'],)).fetchone() if attempt else None
        if (not attempt or not case or case['worker_attempt_id']!=job_id or case['lease_owner']!=identity['id']
            or case['lease_token']!=token or case['lease_until']<=int(clock())
            or attempt['input_sha']!=case['input_sha'] or (input_sha is not None and case['input_sha']!=input_sha)):
            raise HTTPException(409,'执行租约或输入版本已失效，旧Worker不能确认当前案件')
        from .automation_handoff import context_sha
        metadata=json.loads(attempt['result_json'])
        current_configuration=ws.conn.execute('SELECT configuration_sha FROM automation_worker WHERE id=?',(identity['id'],)).fetchone()
        if metadata.get('worker_configuration_sha','')!=(current_configuration[0] if current_configuration else ''):
            raise HTTPException(409,'Worker配置已变化，旧执行不能更新当前案件')
        if metadata.get('source_context_sha')!=context_sha(ws,model(),case['store_id'],case['period'],case['input_sha']):
            raise HTTPException(409,'来源或规则版本已变化，旧Worker不能更新案件')
        return dict(case),dict(attempt)

    def service_auth(ws,key,identity,action,input_sha,scope):
        aid=journal.ident()
        ws.conn.execute('INSERT INTO automation_authorization VALUES(?,?,?,?,?,?,?,?,?)',
            (aid,key,identity['id'],'service',action,input_sha,'',journal.encoded(scope),journal.now()))
        return aid

    def prior(conn,key,signature):
        try:return journal.receipt(conn,key,signature)
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc

    @router.post('/api/v1/agents/heartbeat')
    def heartbeat(body:Heartbeat,request:Request):
        identity=service(request,body.agent_id);ws=workspace();renewed=[];rejected=[]
        if body.configuration and journal.digest(body.configuration.model_dump())!=body.configuration_sha:
            raise HTTPException(422,'Worker配置指纹不匹配')
        with ws.transaction() as conn:
            previous=conn.execute('SELECT configuration_sha FROM automation_worker WHERE id=?',(identity['id'],)).fetchone()
            changed=bool(body.configuration_sha and (not previous or previous[0]!=body.configuration_sha))
            conn.execute('INSERT INTO automation_worker(id,scopes_json,status,heartbeat_at,configuration_sha,configuration_json) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET scopes_json=excluded.scopes_json,status=excluded.status,heartbeat_at=excluded.heartbeat_at,configuration_sha=CASE WHEN excluded.configuration_sha<>\'\' THEN excluded.configuration_sha ELSE configuration_sha END,configuration_json=CASE WHEN excluded.configuration_json<>\'{}\' THEN excluded.configuration_json ELSE configuration_json END',
                (identity['id'],journal.encoded(identity.get('scopes',[])),body.status,int(clock()),body.configuration_sha or '',journal.encoded(body.configuration.model_dump()) if body.configuration else '{}'))
            if changed:
                conn.execute("UPDATE automation_attempt SET state='interrupted' WHERE state='worker_running' AND id IN (SELECT worker_attempt_id FROM automation_case WHERE lease_owner=?)",(identity['id'],))
                conn.execute("UPDATE automation_case SET lease_owner='',lease_until=0,lease_token=lease_token+1 WHERE lease_owner=?",(identity['id'],))
                from .source_recovery import configuration_changed
                configuration_changed(ws,identity['id'],body.configuration_sha,model=model(),stamp=int(clock()))
            from .source_recovery import retry_due
            retry_due(ws,model(),identity['id'],int(clock()))
            for lease in body.leases:
                try:
                    case,_=owned(ws,lease.job_id,identity,lease.lease_token)
                    conn.execute('UPDATE automation_case SET lease_until=? WHERE id=? AND lease_owner=? AND lease_token=?',
                        (int(clock())+lease_seconds,case['id'],identity['id'],lease.lease_token));renewed.append(lease.job_id)
                except HTTPException:rejected.append(lease.job_id)
        return {'agent_id':identity['id'],'status':body.status,'renewed':renewed,'rejected':rejected,'server_time':int(clock())}

    @router.post('/api/v1/agent-jobs/claim')
    def claim(body:Claim,request:Request):
        identity=service(request,body.agent_id);ws=workspace()
        if body.capabilities and 'recompute' not in body.capabilities:return Response(status_code=204)
        source_capable='source_recovery' in body.capabilities
        from .automation_handoff import refresh_waiting
        refresh_waiting(ws,model())
        if source_capable:
            from .source_recovery import ensure_date_demands
            ensure_date_demands(ws,model())
        with ws.transaction() as conn:
            for _ in range(32):
                row=conn.execute("SELECT * FROM automation_case c WHERE status='processing' AND stage IN ('gathering','queued','retry_wait') AND lease_until<=? AND (? OR NOT EXISTS(SELECT 1 FROM automation_attempt a WHERE a.case_id=c.id AND a.input_sha=c.input_sha AND a.state='source_queued')) AND NOT EXISTS(SELECT 1 FROM period p WHERE p.store_id=c.store_id AND p.period=c.period AND p.state='closed') ORDER BY updated_at,id LIMIT 1",(int(clock()),int(source_capable))).fetchone()
                if not row:return Response(status_code=204)
                from .automation_actions import _month as month,_case
                current=next(r for r in month(ws,model(),row['period'])['stores'] if r['store_id']==row['store_id'])
                _case(conn,current)
                row=conn.execute('SELECT * FROM automation_case WHERE id=?',(row['id'],)).fetchone()
                if row['status']=='processing' and row['stage'] in {'gathering','queued','retry_wait'}:break
            else:return Response(status_code=204)
            job_id=journal.ident();token=row['lease_token']+1
            changed=conn.execute('UPDATE automation_case SET lease_owner=?,lease_token=?,lease_until=?,worker_attempt_id=? WHERE id=? AND lease_token=? AND lease_until<=?',
                (identity['id'],token,int(clock())+lease_seconds,job_id,row['id'],row['lease_token'],int(clock())))
            if not changed.rowcount:return Response(status_code=204)
            if row['worker_attempt_id']:
                conn.execute("UPDATE automation_attempt SET state='interrupted' WHERE id=? AND state='worker_running'",(row['worker_attempt_id'],))
            from .automation_handoff import context_sha
            source_context=context_sha(ws,model(),row['store_id'],row['period'],row['input_sha'])
            worker_configuration=conn.execute('SELECT configuration_sha FROM automation_worker WHERE id=?',(identity['id'],)).fetchone()
            metadata={'owner':identity['id'],'lease_token':token,'case_id':row['id'],'source_context_sha':source_context,
                'worker_configuration_sha':worker_configuration[0] if worker_configuration else ''}
            from . import source_recovery
            source_task=source_recovery.queued(ws,row['id'],row['input_sha']) if source_capable else None
            if source_task:
                metadata['source_task_id']=source_task['id']
            conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
                (job_id,row['id'],job_id,row['input_sha'],'worker_running',journal.encoded(metadata),journal.now()))
            aid=service_auth(ws,'worker-claim:'+job_id,identity,'worker.claim',row['input_sha'],metadata)
            case={'id':row['id'],'store_id':row['store_id'],'period':row['period'],'input_sha':row['input_sha'],
                'reason_codes':[i['code'] for i in json.loads(row['issues_json'])],'run_id':row['run_id'],'source_context_sha':source_context}
            result={'id':job_id,'kind':'recompute','idempotency_key':journal.digest([row['id'],row['input_sha'],source_context,'business-evidence.v1']),
                'payload':{'automation_case':case,'lease_token':token,'scope':'business'}}
            if source_task:
                source=json.loads(source_task['result_json'])
                result['idempotency_key']=journal.digest([source_task['id'],source['plan_sha256'],token])
                result['payload'].update(capability='source_recovery',source_task={
                    'task_id':source_task['id'],'plan_sha256':source['plan_sha256']})
            journal.finish(conn,'worker-claim:'+job_id,'worker.claim',journal.digest(result),aid,result)
            return result

    @router.post('/api/v1/agent-questions/claim')
    def claim_question(body:Claim,request:Request):
        from . import agent_questions
        identity=service(request,body.agent_id)
        result=agent_questions.claim(workspace(),model(),identity,lease_seconds=lease_seconds,clock=clock)
        return result if result else Response(status_code=204)

    @router.post('/api/v1/agent-questions/{question_id}/answer')
    def answer_question(question_id:str,body:QuestionAnswer,request:Request):
        from . import agent_questions
        identity=service(request)
        try:return agent_questions.answer(workspace(),model(),question_id,identity,lease_token=body.lease_token,text=body.text,clock=clock)
        except agent_questions.QuestionError as exc:raise HTTPException(409,str(exc)) from exc

    @router.post('/api/v1/agent-questions/{question_id}/fail')
    def fail_question(question_id:str,body:QuestionFailure,request:Request):
        from . import agent_questions
        identity=service(request)
        try:return agent_questions.fail(workspace(),question_id,identity,lease_token=body.lease_token,
            reason=body.reason,retryable=body.retryable,clock=clock)
        except agent_questions.QuestionError as exc:raise HTTPException(409,str(exc)) from exc

    @router.post('/api/v1/agent-jobs/{job_id}/progress')
    def progress(job_id:str,body:Progress,request:Request,idempotency_key:str|None=Header(default=None,alias='Idempotency-Key')):
        identity=service(request);ws=workspace();token=body.detail.get('lease_token')
        if not isinstance(token,int):raise HTTPException(409,'缺少任务租约令牌')
        key=idempotency_key or f'{job_id}:progress:{body.sequence}'
        signature=journal.digest([job_id,body.model_dump(),identity['id']])
        with ws.transaction() as conn:
            old=prior(conn,key,signature)
            if old:return old
            case,attempt=owned(ws,job_id,identity,token)
            conn.execute('UPDATE automation_case SET lease_until=?,updated_at=? WHERE id=?',
                (int(clock())+lease_seconds,journal.now(),case['id']))
            state=json.loads(attempt['result_json']);state.update(stage=body.stage,message=body.message)
            conn.execute('UPDATE automation_attempt SET result_json=? WHERE id=?',(journal.encoded(state),job_id))
            aid=service_auth(ws,key,identity,'worker.progress',case['input_sha'],{'job_id':job_id,'lease_token':token})
            return journal.finish(conn,key,'worker.progress',signature,aid,{'ok':True,'job_id':job_id,'lease_token':token})

    def terminal(job_id,body,request,idempotency_key,success):
        identity=service(request);ws=workspace();action='complete' if success else 'fail'
        key=idempotency_key or f'{job_id}:{action}'
        signature=journal.digest([job_id,body.model_dump(),identity['id'],action])
        with ws.transaction() as conn:
            old=prior(conn,key,signature)
            if old:return old
            case,attempt=owned(ws,job_id,identity,body.lease_token,body.input_sha)
            if json.loads(attempt['result_json']).get('source_task_id') and (success or not body.tool_outcomes):
                raise HTTPException(409,'取证任务只能提交固定原始凭据回执，不能确认业务完成')
            frozen=ws.state(case['store_id'],case['period'])
            if frozen and frozen.closed:raise HTTPException(409,'本期已由人工冻结，旧取证任务不得更新终态')
            checked=checker.check(ws,case['store_id'],case['period'],model=model(),verify_artifacts=True)
            if checked['input_sha']!=body.input_sha:
                raise HTTPException(409,'当前核算输入已变化，旧任务不能更新当前案件')
            from . import source_recovery
            source_pending=source_recovery.queued(ws,case['id'],case['input_sha'])
            source_ready=source_recovery.ready(ws,case['id'],case['input_sha'])
            if body.tool_outcomes and not source_pending:
                if (body.result or body.message or body.error_code not in {'worker_failure','TrustedSourceHandoff'}
                    or set(body.model_extra or {})-{'job_id','worker_idempotency_key'}):
                    raise HTTPException(422,'来源交接只接受固定工具分类与本机证据引用，不接受金额或自由诊断文字')
                if (body.model_extra or {}).get('job_id',job_id)!=job_id:raise HTTPException(409,'交接任务编号不匹配')
                if success:raise HTTPException(409,'等待来源不能作为完成凭据')
                if checked['allowed']:raise HTTPException(409,'当前核算已就绪，不能用无关来源失败覆盖其状态')
                from . import automation_handoff as handoff
                try:waiting=handoff.record(ws,model(),case,job_id,identity['id'],[item.model_dump() for item in body.tool_outcomes])
                except ValueError as exc:raise HTTPException(422,str(exc)) from exc
                stage='waiting_source' if any(item['kind']=='source' for item in waiting['items']) else 'waiting_maintenance'
                result={'job_id':job_id,'case_id':case['id'],'state':stage,'waiting':waiting,
                    'verification':{'allowed':False,'issues':checked['issues'],'input_sha':checked['input_sha']}}
                conn.execute('UPDATE automation_attempt SET state=?,result_json=? WHERE id=?',(stage,journal.encoded(result),job_id))
                conn.execute("UPDATE automation_case SET status='processing',stage=?,lease_owner='',lease_until=0,updated_at=? WHERE id=? AND lease_token=?",
                    (stage,journal.now(),case['id'],body.lease_token))
                aid=service_auth(ws,key,identity,'worker.source_handoff',body.input_sha,{'job_id':job_id,'lease_token':body.lease_token})
                return journal.finish(conn,key,'worker.source_handoff',signature,aid,{'ok':True,'job':result})
            if success and not checked['allowed']:
                raise HTTPException(409,{'message':'Worker回复不能代替独立业务复核','issues':checked['issues'],'input_sha':checked['input_sha']})
            state='completed' if success else 'source_queued' if source_pending else 'waiting_source_adoption' if source_ready else 'retry_wait' if body.retryable else 'failed'
            # The accepted envelope contains references and verification, never
            # LLM text as a proof that money or source completeness is correct.
            result={'job_id':job_id,'case_id':case['id'],'state':state,'verification':{
                'allowed':checked['allowed'],'issues':checked['issues'],'input_sha':checked['input_sha']},
                'references':body.result.get('artifact_refs',[]),'error_code':None if success else body.error_code}
            conn.execute('UPDATE automation_attempt SET state=?,result_json=? WHERE id=?',(state,journal.encoded(result),job_id))
            delay=min(3600,30*2**min(body.lease_token,7)) if body.retryable and not success and not (source_pending or source_ready) else 0
            conn.execute("UPDATE automation_case SET status=?,stage=?,lease_owner='',lease_until=?,input_sha=?,issues_json=?,updated_at=? WHERE id=? AND lease_token=?",
                ('needs_decision' if success else 'processing','ready_for_close' if success else 'gathering' if source_pending else 'waiting_source_adoption' if source_ready else 'retry_wait' if body.retryable else 'maintenance',
                 int(clock())+delay if delay else 0,checked['input_sha'],journal.encoded(checked['issues']),journal.now(),case['id'],body.lease_token))
            aid=service_auth(ws,key,identity,'worker.'+action,body.input_sha,{'job_id':job_id,'lease_token':body.lease_token})
            return journal.finish(conn,key,'worker.'+action,signature,aid,{'ok':True,'job':result})

    @router.post('/api/v1/agent-jobs/{job_id}/complete')
    def complete(job_id:str,body:Terminal,request:Request,idempotency_key:str|None=Header(default=None,alias='Idempotency-Key')):
        return terminal(job_id,body,request,idempotency_key,True)

    @router.post('/api/v1/agent-jobs/{job_id}/fail')
    def fail(job_id:str,body:Terminal,request:Request,idempotency_key:str|None=Header(default=None,alias='Idempotency-Key')):
        return terminal(job_id,body,request,idempotency_key,False)

    @router.get('/api/automation/workers/status')
    def status():
        ws=workspace();rows=ws.conn.execute('SELECT * FROM automation_worker ORDER BY id').fetchall()
        workers=[{'id':r['id'],'status':r['status'],'scopes':json.loads(r['scopes_json']),
            'heartbeat_age_seconds':max(0,int(clock())-r['heartbeat_at']),
            'alive':int(clock())-r['heartbeat_at']<lease_seconds} for r in rows]
        stages={r['stage']:r['count'] for r in ws.conn.execute("SELECT stage,count(*) count FROM automation_case WHERE status='processing' GROUP BY stage")}
        return {'worker_alive':any(w['alive'] for w in workers),
            'worker_healthy':any(w['alive'] and w['status'] in {'online','busy'} for w in workers),
            'processing_by_stage':stages,'workers':workers}

    from .source_recovery import install_routes
    install_routes(router,workspace,model,service,owned,service_auth,prior,clock)
    app.include_router(router)
