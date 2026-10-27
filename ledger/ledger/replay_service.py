"""Offline replay through the real workspace, registry and recompute service.

No record/update-baseline mode exists. A caller supplies a complete expected result;
missing historical materials or expectations fail rather than becoming a new baseline.
"""
from contextlib import ExitStack
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import socket
import tempfile
import threading
from unittest.mock import patch

from . import freight_experiments_inputs as frozen
from .freight_trial_inputs import InputError

_offline_lock = threading.RLock()
REQUIRED_SCOPE_FIELDS = {'state','statement','commission_total','commission_people',
    'confirmed_total','confirmed_people','cost_line_total','allocation_pending_count',
    'commission_complete','can_close','source_sync_pending'}


class ReplayError(RuntimeError):
    pass


def _blocked(*_args, **_kwargs):
    raise ReplayError('服务整链回放禁止网络；必须提供固定来源')


def _finite(value):
    if isinstance(value,float) and not math.isfinite(value):
        raise ReplayError('回放结果含非有限数字')
    if isinstance(value,dict):
        for item in value.values():
            _finite(item)
    if isinstance(value,(list,tuple)):
        for item in value:
            _finite(item)


def differences(expected, actual, path=''):
    """Strict shape, identity, boolean and missing-value comparison; money at 6dp."""
    _finite(expected); _finite(actual)
    if isinstance(expected,dict) and isinstance(actual,dict):
        output = []
        for key in sorted(set(expected)|set(actual)):
            child = path+'.'+key if path else key
            if key not in expected or key not in actual:
                output.append({'path':child,'expected':expected.get(key),'actual':actual.get(key),'kind':'missing_field'})
            else:
                output.extend(differences(expected[key],actual[key],child))
        return output
    if isinstance(expected,bool) or isinstance(actual,bool):
        equal = type(expected) is type(actual) and expected==actual
    elif isinstance(expected,(int,float,Decimal)) and isinstance(actual,(int,float,Decimal)):
        # Counts are exact. Ratios are not money and keep their higher precision.
        tolerance = Decimal(0) if path.endswith('_count') else Decimal('.0000000005') if 'margin' in path or 'ratio' in path else Decimal('.0000005')
        equal = abs(Decimal(str(expected))-Decimal(str(actual)))<=tolerance
    else:
        equal = type(expected) is type(actual) and expected==actual
    return [] if equal else [{'path':path,'expected':expected,'actual':actual,'kind':'changed'}]


def load_workspace_decisions(reference, *, fixture=False):
    """Read one approved, hash-bound scope/overhead export, never infer values."""
    if (not isinstance(reference,dict) or set(reference)!={'path','sha256','approval_reference'}
        or not isinstance(reference.get('path'),str) or not Path(reference['path']).is_absolute()
        or not re.fullmatch('[0-9a-f]{64}',str(reference.get('sha256','')))
        or not isinstance(reference.get('approval_reference'),str) or not reference['approval_reference'].strip()):
        raise ReplayError('离线工作区决定必须绑定绝对材料路径、SHA与批准依据')
    path=Path(reference['path'])
    if path.is_symlink() or path.stat().st_size>2*1024*1024:
        raise ReplayError('工作区决定材料不是有界的固定文件')
    raw=path.read_bytes();sha=hashlib.sha256(raw).hexdigest()
    if sha!=reference['sha256']:raise ReplayError('工作区决定材料SHA与批准版本不符')
    body=json.loads(raw)
    expected_kind='service_fixture_workspace_decisions' if fixture else 'approved_workspace_decisions'
    if (not isinstance(body,dict) or body.get('schema_version')!=1 or body.get('kind')!=expected_kind
        or body.get('approval_reference')!=reference['approval_reference']
        or not isinstance(body.get('decisions'),list) or not body['decisions']):
        raise ReplayError('缺少明确批准的工作区决定；夹具不能冒充历史批准材料')
    for row in body['decisions']:
        if (not isinstance(row,dict) or set(row)!={'period','store_id','kind','values','reason'}
            or row['kind'] not in {'store_scope','overhead_total'} or not isinstance(row['values'],dict)
            or not isinstance(row['period'],str) or not isinstance(row['store_id'],str)
            or not isinstance(row['reason'],str) or not row['reason'].strip()):
            raise ReplayError('只允许恢复已有人工营业范围/公摊总额决定，不含授权、结账或实发')
    return body,sha


def _offline_actor(ws,registry):
    root=Path(ws.root).resolve();temporary=Path(tempfile.gettempdir()).resolve()
    if (root.parent!=temporary or not root.name.startswith(('ledger-service-replay-','ledger-service-scope-'))
        or Path(registry.root).resolve().parent!=root):
        raise ReplayError('人工决定仅允许隔离临时回放，不能写实际账号或业务库')
    actor=getattr(registry,'_offline_approved_actor',None)
    if actor is None:
        # An actual temporary session supplies the normal human actor adapter.
        # It is not a production login or a new authority outside this clone.
        identity='replay-approved-human';password=secrets.token_urlsafe(32)
        registry.operator_add(identity,password,by='replay:approved-source')
        actor={**registry.actor(registry.login(identity,password)),'kind':'human','authenticated':True}
        registry._offline_approved_actor=actor
    return actor


def restore_workspace_decisions(ws,registry,model,reference,*,fixture=False):
    """Replay archived human approval using real rules, in a disposable DB only."""
    from . import automation_actions as actions
    body,sha=load_workspace_decisions(reference,fixture=fixture)
    actor=_offline_actor(ws,registry)
    applied=[]
    with _offline_lock,ExitStack() as stack:
        stack.enter_context(patch('socket.create_connection',_blocked))
        stack.enter_context(patch.object(socket.socket,'connect',_blocked))
        stack.enter_context(patch.object(socket.socket,'connect_ex',_blocked))
        stack.enter_context(patch('urllib.request.urlopen',_blocked))
        with ws.transaction():
            for index,row in enumerate(body['decisions']):
                key='replay-approved:'+sha+':'+str(index)
                preview=actions.decision_preview(ws,model,request_id=key+':preview',actor=actor,**row)
                result=actions.decision_apply(ws,model,request_id=key+':apply',proposal_id=preview['proposal_id'],
                    proposal_sha=preview['proposal_sha'],input_sha=preview['input_sha'],actor=actor)
                saved=ws.conn.execute('SELECT values_json FROM automation_decision WHERE id=?',(result['decision_id'],)).fetchone()
                applied.append({**row,'values':json.loads(saved['values_json'])})
            load_workspace_decisions(reference,fixture=fixture)
    return {'source_sha256':sha,'approval_reference':body['approval_reference'],
            'kind':body['kind'],'decisions':applied,'offline_only':True}


class Session:
    """An isolated, temporary service instance over a verified input bundle."""
    def __init__(self, bundle, registry_seed):
        self.bundle = Path(bundle).resolve()
        self.registry_seed = registry_seed
        self.trace = []
        self.workspace_decisions = None
        self._workspace_reference = None

    def restore_workspace_decisions(self,reference,*,fixture=False):
        if self.workspace_decisions is not None:raise ReplayError('一轮回放只允许一份批准工作区决定，不能混用范围版本')
        result=restore_workspace_decisions(self.ws,self.registry,self.model,reference,fixture=fixture)
        self.workspace_decisions=result
        self._workspace_reference=(dict(reference),fixture)
        self.trace.append({'operation':'workspace_decisions.restore','source_sha256':result['source_sha256'],
                           'approval_reference':result['approval_reference'],'decision_count':len(result['decisions'])})
        return result

    def __enter__(self):
        from .workspace import Workspace
        from .commission_registry import Registry
        from .model.schema import Model
        if not isinstance(self.registry_seed,dict) or not {'people','schemes','members'}<=set(self.registry_seed):
            raise ReplayError('回放缺少固定人员、提成关系或人员身份输入')
        self.stack = ExitStack()
        try:
            self.stack.enter_context(_offline_lock)
            self.inputs,self.fingerprint = frozen.verify(self.bundle)
            self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='ledger-service-replay-')))
            self.stack.enter_context(patch.dict('os.environ',{
                'LEDGER_ORDER_FEED_ENABLED':'1','LEDGER_ORDER_FEED_ROOT':str(self.bundle/'feed'),
                'LEDGER_ORDER_FEED_URL':'http://offline.invalid', 'LEDGER_COMMISSION_AUTH_MODE':'open'}))
            self.stack.enter_context(patch('socket.create_connection',_blocked))
            self.stack.enter_context(patch.object(socket.socket,'connect',_blocked))
            self.stack.enter_context(patch.object(socket.socket,'connect_ex',_blocked))
            self.stack.enter_context(patch('urllib.request.urlopen',_blocked))
            self.model = Model.model_validate_json((self.bundle/'model.json').read_text(encoding='utf-8'))
            from .freight_trial_inputs import digest
            if digest(json.loads(self.model.model_dump_json())) != self.inputs['model_hash']:
                raise ReplayError('服务回放模型与输入版本不符')
            self.ws = Workspace(self.root)
            self.stack.callback(self.ws.close)
            for slot in self.inputs['slots']:
                source = self.bundle/'files'/slot['sha'][:2]/slot['sha']
                self.ws.keep(slot['name'],source,slot['store_id'],by='replay:input')
            self.feed = frozen.materialize_feed(self.bundle,self.root)
            self.registry = Registry(self.root)
            self._seed()
            return self
        except Exception:
            self.stack.close()
            raise

    def __exit__(self,*args):
        return self.stack.__exit__(*args)

    def _seed(self):
        for person in self.registry_seed['people']:
            self.registry.person_save(person,'replay:seed','固定人员输入')
        for row in self.registry_seed['members']:
            self.registry.save_store_member(row['store_id'],row['person_id'],row['duty'],row.get('leader_id',''),
                'replay:seed','固定店铺身份',valid_from=row.get('valid_from','1970-01-01'))
        for row in self.registry_seed['schemes']:
            self.registry.save_scheme(row['store_id'],row['product_id'],row['body'],
                'replay:seed','固定版本关系',publish=True)
        for row in self.registry_seed.get('policies',[]):
            self.registry.save_policy(row['body'],row['effective_from'],'replay:seed','固定计提政策',
                store_id=row.get('store_id',''))

    def recompute(self,store_id):
        from . import service
        result = service.recompute(self.ws,self.model,self.model.store(store_id),note='离线服务整链回放')
        if result.failure:
            raise ReplayError('服务重算失败：'+json.dumps(result.failure,ensure_ascii=False))
        self.trace.append({'operation':'service.recompute','store_id':store_id,
            'runs':{s.period:s.run_id for s in self.ws.periods_of_store(store_id)}})
        return result

    def supplement(self,store_id,period,*,amount,reason,coverage_key='',order_id=''):
        from . import cost_lines
        state = self.ws.state(store_id,period)
        if state is None:
            raise ReplayError('补录前缺少真实服务核算记录')
        rows = cost_lines.current(self.ws,state.run_id,store_id,period)['items']
        selected = [r for r in rows if (coverage_key and r['coverage_key']==coverage_key)
                    or (not coverage_key and order_id and r['order_id']==order_id)]
        if len(selected)!=1:
            raise ReplayError('补录必须绑定唯一的当前缺口和上下文')
        row = selected[0]
        result = cost_lines.save(self.ws,store_id,period,state.run_id,coverage_key=row['coverage_key'],
            context_sha=row['context_sha'],amount=amount,reason=reason,by='replay:decision',
            expected_line_revision=row['line_revision'])
        self.registry.enqueue_cost_lines(store_id,period,result['line_revision'])
        self.trace.append({'operation':'cost_lines.save','store_id':store_id,'period':period,
            'coverage_key':row['coverage_key'],'context_sha':row['context_sha']})
        return result

    def drain_pending(self,max_passes=100):
        """Use the real manager to acknowledge durable work; never delete pending by hand."""
        from .commission_manager import Manager
        manager=Manager(lambda:self.ws,lambda:self.model)
        for _ in range(max_passes):
            with self.registry.connect() as db:
                remaining=db.execute('SELECT count(*) FROM pending').fetchone()[0]
            if not remaining:
                self.trace.append({'operation':'commission_manager.once','remaining_pending':0})
                return
            manager.once()
        raise ReplayError('固定输入的待核算队列未清空，不能把排队当作服务回放完成')

    def confirm(self,store_id,period,payouts,reason):
        from . import commission_confirm
        state = self.ws.state(store_id,period)
        if state is None:
            raise ReplayError('核定前缺少真实服务核算记录')
        context = commission_confirm.context(self.ws,self.registry,self.model,store_id,period,expected_run=state.run_id)
        result = commission_confirm.confirm(self.ws,self.registry,self.model,store_id=store_id,period=period,
            run_id=state.run_id,source_sha=context['source_sha'],
            expected_confirmation_id=context['latest']['id'] if context['latest'] else '',
            payouts=payouts,no_payout=not context['people'],reason=reason,actor='replay:decision')
        self.trace.append({'operation':'payout.confirm','store_id':store_id,'period':period,'run_id':state.run_id})
        return result

    def close_approved(self,store_id,period,*,approval_reference,reason):
        """An explicit offline human close via the same preview/confirm gate."""
        from . import automation_actions as actions
        if not isinstance(approval_reference,str) or not approval_reference.strip() or not isinstance(reason,str) or not reason.strip():
            raise ReplayError('离线月结必须有明确批准依据和理由')
        actor=_offline_actor(self.ws,self.registry)
        key='replay-close:'+hashlib.sha256(json.dumps([store_id,period,approval_reference,reason],ensure_ascii=False).encode()).hexdigest()
        preview=actions.close_preview(self.ws,self.model,request_id=key+':preview',period=period,store_ids=[store_id],actor=actor)
        self.trace.append({'operation':'close.preview','store_id':store_id,'period':period,
            'approval_reference':approval_reference,'input_sha':preview['input_sha'],
            'blocked':[{'store_id':row['store_id'],'issue_codes':[i['code'] for i in row['issues']]} for row in preview['blocked']]})
        if preview['blocked'] or len(preview['eligible'])!=1:
            raise ReplayError('离线月结未通过原有Checker，不能忽略成本、公司条件或队列阻断')
        result=actions.close_confirm(self.ws,self.model,request_id=key+':confirm',proposal_id=preview['proposal_id'],
            proposal_sha=preview['proposal_sha'],input_sha=preview['input_sha'],actor=actor)
        state=self.ws.state(store_id,period)
        if result['status']!='completed' or not state or not state.closed:
            raise ReplayError('原有月结确认未完成，不能将预览或请求当作结账成功')
        self.trace.append({'operation':'close.confirm','store_id':store_id,'period':period,'run_id':state.run_id,
            'approval_reference':approval_reference,'input_sha':preview['input_sha'],'reason':reason})
        return result

    def replace_scheme(self,store_id,product_id,body,reason):
        with self.registry.connect() as db:
            row = db.execute('SELECT revision FROM scheme WHERE store_id=? AND product_id=?',(store_id,product_id)).fetchone()
        return self.registry.save_scheme(store_id,product_id,body,'replay:decision',reason,
            expected=row['revision'] if row else 0,publish=True)

    def snapshot(self):
        from .storage_integrity import verified
        from .commission_confirm import _last
        from . import automation_checker
        if self._workspace_reference:
            reference,fixture=self._workspace_reference
            load_workspace_decisions(reference,fixture=fixture)
        scopes = {}; proofs = {}
        for state in self.ws.overview():
            if not verified(self.ws.facts_path(state.run_id)):
                raise ReplayError('服务事实留档缺失或未通过校验')
            payload = state.result or {}; commission = payload.get('commission') or {}
            checked=automation_checker.check(self.ws,state.store_id,state.period,model=self.model,
                expected_run_id=state.run_id,verify_artifacts=True)
            with self.registry.connect() as db:
                confirmation = _last(db,state.store_id,state.period,state.run_id)
                calculation = db.execute('SELECT path,sha FROM calculation WHERE finance_run=?',(state.run_id,)).fetchone()
            if commission.get('calculation_id'):
                import hashlib
                if not calculation or hashlib.sha256((self.registry.root/'calculations'/Path(calculation['path']).name).read_bytes()).hexdigest()!=calculation['sha']:
                    raise ReplayError('服务提成留档缺失或校验失败')
            key = state.store_id+'/'+state.period
            scopes[key] = {'state':state.state,
                'statement':{r['id']:{'value':r.get('value'),'available':bool(r.get('available',True))}
                    for r in payload.get('statement') or []},
                'commission_total':commission.get('total'),
                'commission_complete':bool(commission.get('amount_complete')),
                'can_close':bool(checked['allowed'] and not state.closed),
                'source_sync_pending':bool(payload.get('source_sync_pending')),
                'commission_people':{p['person_id']:{'base':p.get('base'),'amount':p.get('amount')}
                    for p in commission.get('people') or []},
                'confirmed_total':float(confirmation['confirmed_total']) if confirmation else None,
                'confirmed_people':{p['person_id']:p['amount'] for p in confirmation['payouts']} if confirmation else {},
                'cost_line_total':(payload.get('cost_line_posting') or {}).get('total',0),
                'allocation_pending_count':int(payload.get('allocation_pending_count') or 0)}
            row = self.ws.conn.execute('SELECT input_fingerprint,model_revision FROM run WHERE id=?',(state.run_id,)).fetchone()
            proofs[key] = {'run_id':state.run_id,'input_fingerprint':row['input_fingerprint'],
                'model_revision':row['model_revision'],'calculation_sha':calculation['sha'] if calculation else None,
                'engine_can_close':bool(payload.get('can_close')),
                'close_issue_codes':sorted(p['code'] for p in checked['issues'])}
        _finite(scopes)
        return {'scopes':scopes,'proofs':proofs,'registry_revision':self.registry.revision(),
                'input_bundle_fingerprint':self.fingerprint,'trace':list(self.trace),
                'workspace_decisions':self.workspace_decisions}

    def check(self,expected):
        if not isinstance(expected,dict) or not expected.get('scopes'):
            raise ReplayError('缺少批准的服务整链期望；禁止自动记录当前结果为基线')
        for key,row in expected['scopes'].items():
            if set(row)!=REQUIRED_SCOPE_FIELDS or not {'n_receipt','gross_profit','net_profit'}<=set(row.get('statement') or {}):
                raise ReplayError('店月期望缺少必需金额、确认或人工决定字段：'+key)
            for metric in ('n_receipt','gross_profit','net_profit'):
                node = row['statement'][metric]
                if node.get('available') is not True or isinstance(node.get('value'),bool) or not isinstance(node.get('value'),(int,float,Decimal)):
                    raise ReplayError('店月黄金期望缺少可验证的必需金额：'+key+'/'+metric)
        actual = self.snapshot()
        changes = differences({'scopes':expected['scopes']},{'scopes':actual['scopes']})
        return {'status':'failed' if changes else 'passed','differences':changes,
                'checked_scopes':sorted(actual['scopes']),'actual':actual}


def execute(case):
    if case.get('schema_version')!=1 or not case.get('case_id'):
        raise ReplayError('服务回放案例合同无效')
    if case.get('kind') not in {'service_fixture','historical_golden'}:
        raise ReplayError('必须区分服务夹具与真实历史黄金语料')
    if case['kind']=='historical_golden' and not case.get('approval_reference'):
        raise ReplayError('真实历史黄金语料缺批准依据')
    if not case.get('expected',{}).get('scopes'):
        raise ReplayError('真实语料或期望缺失，回放失败；不会自动录制基线')
    with Session(case['bundle'],case.get('registry')) as session:
        reference=case.get('workspace_decisions')
        if reference is not None:
            if not isinstance(reference,dict):raise ReplayError('工作区决定引用必须为对象')
            if case['kind']=='historical_golden' and (case.get('source_evidence') or {}).get('workspace_decisions_sha256')!=reference.get('sha256'):
                raise ReplayError('历史案例的工作区决定缺少source_evidence SHA绑定')
            session.restore_workspace_decisions(reference,fixture=case['kind']=='service_fixture')
        for step in case.get('steps',[]):
            arguments = {k:v for k,v in step.items() if k!='operation'}
            operation = step.get('operation')
            if operation not in {'recompute','supplement','confirm','replace_scheme','drain_pending','close_approved'}:
                raise ReplayError('不支持的离线回放步骤：'+str(operation))
            if operation=='close_approved' and (not case.get('approval_reference')
                or arguments.get('approval_reference')!=case['approval_reference']):
                raise ReplayError('月结步骤的批准引用必须与批准案例完全一致')
            getattr(session,operation)(**arguments)
        report = session.check(case['expected'])
        return {'case_id':case['case_id'],'kind':case['kind'],**report}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='离线服务整链回放；不提供记录或覆盖基线功能')
    parser.add_argument('case')
    args = parser.parse_args(argv)
    try:
        path = Path(args.case).resolve()
        case = json.loads(path.read_text(encoding='utf-8'))
        if not Path(case.get('bundle','')).is_absolute():
            case['bundle'] = str(path.parent/case.get('bundle',''))
        if isinstance(case.get('workspace_decisions'),dict) and not Path(case['workspace_decisions'].get('path','')).is_absolute():
            case['workspace_decisions']['path']=str((path.parent/case['workspace_decisions'].get('path','')).resolve())
        result = execute(case)
    except (ReplayError,InputError,OSError,ValueError,KeyError) as exc:
        print(json.dumps({'status':'failed','error':str(exc)},ensure_ascii=False))
        return 2
    print(json.dumps(result,ensure_ascii=False,default=str))
    return 0 if result['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
