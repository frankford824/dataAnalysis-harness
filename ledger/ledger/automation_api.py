"""Thin HTTP adapter for deterministic month operations."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from . import automation_actions as actions
from .workspace import WorkspaceError


class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid')


class RequestId(Strict):
    request_id: str=Field(min_length=1,max_length=128)


class DecisionPreview(RequestId):
    period: str
    store_id: str=''
    kind: str
    values: dict
    reason: str=Field(min_length=1,max_length=1000)


class Confirmation(RequestId):
    proposal_id: str
    proposal_sha: str
    input_sha: str


class Revoke(RequestId):
    reason: str=Field(min_length=1,max_length=1000)
    input_sha: str


class ClosePreview(RequestId):
    period: str
    store_ids: list[str]=Field(default_factory=list,max_length=500)


class AgentAuthorityPreview(RequestId):
    period_from: str
    period_until: str=''
    valid_days: int=Field(default=365,ge=1,le=366)
    standing: bool=False
    reason: str=Field(min_length=1,max_length=1000)


class DisputeRow(Strict):
    metric_id: str=Field(default='',max_length=200)
    file_name: str=Field(default='',max_length=300)
    sheet: str=Field(default='',max_length=200)
    row_no: int|None=None
    accounting_status: str=Field(default='',max_length=200)


class DisputeSubject(Strict):
    node_id: str=Field(min_length=1,max_length=128)
    row: DisputeRow|None=None


class UpgradeConfirmation(RequestId):
    period: str
    input_sha: str=Field(min_length=64,max_length=64)


class AgentAuthorityRevoke(RequestId):
    period: str
    reason: str=Field(min_length=1,max_length=1000)


class Question(RequestId):
    store_id: str=Field(min_length=1,max_length=128)
    period: str
    question: str=Field(min_length=1,max_length=500)
    subject: DisputeSubject|None=None


class ServiceAction(RequestId):
    rule_id:str
    case_id:str
    store_id:str
    period:str
    run_id:int=Field(gt=0)
    coverage_key:str
    context_sha:str
    expected_line_revision:int=Field(ge=0)
    input_sha:str


class SourceFactGap(Strict):
    coverage_key:str
    context_sha:str
    expected_line_revision:int=Field(ge=0)


class SourceFacts(RequestId):
    rule_id:str
    case_id:str
    store_id:str
    period:str
    run_id:int=Field(gt=0)
    input_sha:str
    items:list[SourceFactGap]=Field(min_length=1,max_length=100)


def install(app,workspace,model,actor=None,model_root=None):
    router=APIRouter(prefix='/api/automation',tags=['automation'])

    def who(request):
        if actor is None:raise HTTPException(403,'未配置已认证的人工确认身份')
        value=actor(request)
        if not value or not value.get('id') or not value.get('authenticated'):
            raise HTTPException(403,'请先登录再确认业务操作')
        identity={**value,'kind':value.get('kind','')}
        if identity['kind']!='human':raise HTTPException(403,'Agent不能代替人工确认财务决定')
        return identity

    def call(operation,write=True):
        from contextlib import nullcontext
        from .model.transaction import model_lock
        root=model_root() if callable(model_root) else model_root
        try:
            with model_lock(root) if write and root is not None else nullcontext():
                return operation()
        except PermissionError as exc:raise HTTPException(403,str(exc)) from exc
        except WorkspaceError as exc:raise HTTPException(409,str(exc)) from exc
        except ValueError as exc:raise HTTPException(400,str(exc)) from exc

    @router.get('/months/{period}')
    def read_month(period:str):return call(lambda:actions.month(workspace(),model(),period),write=False)

    @router.get('/rules')
    @router.get('/rule-contracts')
    def rules():
        from .automation_deterministic import enabled
        from .automation_rules import metadata
        return {'enabled':enabled(),'requires_human_grant':True,
                'rules':metadata()}

    @router.get('/agent-authority')
    def agent_authority_status(period:str):
        from . import agent_authority
        return call(lambda:agent_authority.status(workspace(),model(),period),write=False)

    @router.post('/agent-authority/preview')
    def agent_authority_preview(body:AgentAuthorityPreview,request:Request):
        from . import agent_authority
        return call(lambda:agent_authority.preview(workspace(),model(),**body.model_dump(),actor=who(request)))

    @router.post('/agent-authority/apply')
    def agent_authority_apply(body:Confirmation,request:Request):
        from . import agent_authority
        return call(lambda:agent_authority.apply(workspace(),model(),**body.model_dump(),actor=who(request)))

    @router.post('/agent-authority/revoke')
    def agent_authority_revoke(body:AgentAuthorityRevoke,request:Request):
        from . import agent_authority
        return call(lambda:agent_authority.revoke(workspace(),model(),**body.model_dump(),actor=who(request)))

    @router.get('/questions')
    def questions(store_id:str,period:str):
        from . import agent_questions
        return call(lambda:agent_questions.listing(workspace(),store_id,period),write=False)

    @router.post('/questions')
    def ask(body:Question,request:Request):
        from . import agent_questions
        return call(lambda:agent_questions.ask(workspace(),model(),**body.model_dump(),actor=who(request)),write=False)

    @router.post('/questions/{question_id}/acknowledge')
    def acknowledge(question_id:str,request:Request):
        from . import agent_questions
        return call(lambda:agent_questions.acknowledge(workspace(),question_id,actor=who(request)),write=False)

    @router.get('/inbox')
    def inbox(period:str):
        from . import agent_questions,agent_upgrade
        return call(lambda:{'period':period,'upgrade':agent_upgrade.check(workspace(),model(),period),
            'disputes':agent_questions.open_disputes(workspace(),model(),period)},write=False)

    @router.post('/upgrade/confirm')
    def upgrade_confirm(body:UpgradeConfirmation,request:Request):
        from . import agent_upgrade
        return call(lambda:agent_upgrade.confirm(workspace(),model(),**body.model_dump(),actor=who(request)))

    @router.post('/service-actions')
    def service_action(body:ServiceAction,request:Request):
        from .automation_cost_actions import apply
        identity=actor(request) if actor else None
        return call(lambda:apply(workspace(),model(),body.model_dump(),identity))

    @router.post('/service-physical-facts')
    def physical_facts(body:ServiceAction,request:Request):
        from .automation_source_cost_v3 import apply
        identity=actor(request) if actor else None
        return call(lambda:apply(workspace(),model(),body.model_dump(),identity))

    @router.post('/service-facts')
    def service_facts(body:SourceFacts,request:Request):
        from .historical_cost_facts import apply
        identity=actor(request) if actor else None
        return call(lambda:apply(workspace(),model(),body.model_dump(),identity))

    @router.post('/months/{period}/advance')
    def advance(period:str,body:RequestId,request:Request):
        return call(lambda:actions.advance(workspace(),model(),period,body.request_id,who(request)))

    @router.post('/decisions/preview')
    def preview(body:DecisionPreview,request:Request):
        return call(lambda:actions.decision_preview(workspace(),model(),**body.model_dump(),actor=who(request)))

    @router.post('/decisions/apply')
    def apply(body:Confirmation,request:Request):
        return call(lambda:actions.decision_apply(workspace(),model(),**body.model_dump(),actor=who(request)))

    @router.post('/decisions/{decision_id}/revoke')
    def revoke(decision_id:str,body:Revoke,request:Request):
        return call(lambda:actions.decision_revoke(workspace(),model(),decision_id,**body.model_dump(),actor=who(request)))

    @router.get('/decisions/{decision_id}')
    def decision(decision_id:str):
        import json
        ws=workspace();row=ws.conn.execute('SELECT * FROM automation_decision WHERE id=?',(decision_id,)).fetchone()
        if not row:raise HTTPException(404,'业务决定不存在')
        values=json.loads(row['values_json'])
        return {'decision_id':decision_id,'kind':row['kind'],'period':row['period'],'store_id':row['store_id'],
            'values':values,'reason':row['reason'],'action':row['action'],
            'input_sha':actions._decision_input(ws,model(),row['kind'],row['store_id'],row['period'],values)}

    @router.post('/close-batches/preview')
    def close_preview(body:ClosePreview,request:Request):
        return call(lambda:actions.close_preview(workspace(),model(),**body.model_dump(),actor=who(request)))

    @router.post('/close-batches/confirm')
    def close_confirm(body:Confirmation,request:Request):
        return call(lambda:actions.close_confirm(workspace(),model(),**body.model_dump(),actor=who(request)))

    app.include_router(router)
