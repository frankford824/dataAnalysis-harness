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
REJECTION_OBSERVATION_FIELDS = {'run_present','state','outcome','source_state',
    'checker_allowed','reason_codes','failure'}
REJECTION_CATEGORIES = {'incomplete_data','safety_boundary','scope_coverage'}
NON_BUSINESS_REASONS = {'code_source_drift','unverified_code_change',
    'unverified_model_change','unverified_input_change','input_capture_unverified',
    'manual_input_unverified','evidence_archive','stale_run','publication_scope_unverified'}
NON_BUSINESS_FAILURES = {'maintenance','code_source_drift','publication_scope_unverified',
    'publication_scope_changed','storage','storage_failure','internal_error'}
SOURCE_FAILURE_KINDS = {'source_unavailable','source_missing','missing_sources',
    'source_incomplete','source_invalid','period_source_missing','no_period_data',
    'empty_inputs','no_sources','input_unavailable'}


class ReplayError(RuntimeError):
    pass


class ReplayServiceFailure(ReplayError):
    """The service explicitly returned failure; never an arbitrary exception."""
    def __init__(self, store_id, failure):
        if not isinstance(failure,dict) or not failure:
            raise ReplayError('服务失败缺少受信返回对象')
        self.store_id=store_id
        self.failure=dict(failure)
        super().__init__('服务重算失败：'+json.dumps(failure,ensure_ascii=False))


class ReplayActionRejection(ReplayError):
    """A real business gate rejected the requested operation."""
    def __init__(self, operation, store_id, period, reason_codes):
        self.operation=operation;self.store_id=store_id;self.period=period
        self.reason_codes=sorted(set(reason_codes))
        super().__init__('离线业务入口拒绝：'+operation+'：'+','.join(self.reason_codes))


def _partial_differences(expected, actual, path=''):
    """Compare only explicitly evidenced fields; an absent field is not None.

    Empty maps remain exact assertions (e.g. no confirmed people), rather than
    silently matching every nonempty actual map.
    """
    _finite(expected);_finite(actual)
    if isinstance(expected,dict) and expected:
        if not isinstance(actual,dict):return differences(expected,actual,path)
        result=[]
        for key,value in expected.items():
            child=path+'.'+str(key) if path else str(key)
            if key not in actual:
                result.append({'path':child,'expected':value,'actual':None,'kind':'missing_field'})
            else:result.extend(_partial_differences(value,actual[key],child))
        return result
    return differences(expected,actual,path)


def _leaf_paths(value,prefix=''):
    if isinstance(value,dict) and value:
        return {path for key,child in value.items() for path in _leaf_paths(child,prefix+'.'+str(key) if prefix else str(key))}
    return {prefix}


def _validate_rejection_financial(section, *, unknown):
    if not isinstance(section,dict) or set(section)-REQUIRED_SCOPE_FIELDS:
        raise ReplayError('拒绝期望的财务字段必须明确且属于服务合同')
    for key,value in section.items():
        if key=='statement':
            if not isinstance(value,dict) or not value:raise ReplayError('拒绝期望不能用空statement跳过未知语义')
            for node in value.values():
                if not isinstance(node,dict) or set(node)!={'value','available'}:
                    raise ReplayError('拒绝期望的金额节点必须同时声明value和available')
                if unknown:
                    if node['value'] is not None or node['available'] is not False:
                        raise ReplayError('未知金额必须明确None/available=False，不能补零或放入部分金额')
                elif (node['available'] is not True or isinstance(node['value'],bool)
                        or not isinstance(node['value'],(int,float,Decimal))):
                    raise ReplayError('已证金额必须是有限数值且available=True')
        elif unknown:
            def only_none(v):
                return bool(v) and all(only_none(c) for c in v.values()) if isinstance(v,dict) else v is None
            if not only_none(value):raise ReplayError('未知财务字段必须显式为None')
        else:
            if value is None:raise ReplayError('None应列入unknown而不是proved')
            if key in {'commission_total','confirmed_total','cost_line_total'}:
                if isinstance(value,bool) or not isinstance(value,(int,float,Decimal)):
                    raise ReplayError('已证金额字段必须为有限数值，不能用布尔值或字符串')
            if key=='allocation_pending_count' and (type(value) is not int or value<0):
                raise ReplayError('已证计数必须为非负整数')
            if key in {'commission_complete','can_close','source_sync_pending'} and type(value) is not bool:
                raise ReplayError('已证完成/来源标志必须为布尔值')
    _finite(section)


def _posted_cost_line_total(ws,store_id,period,run_id,payload):
    """Known empty local supplements are zero; missing external costs are not.

    cost_line_log is the sole append-only entry for these extra amounts. A
    completed run with no such records, or an explicitly captured revision
    with no active save, has a proven empty contribution set. Existing saves
    with no posted-result/context evidence remain unknown, never summed as if
    they had already entered this run.
    """
    if run_id is None:return None
    run=ws.conn.execute('SELECT id,evidence_ready FROM run WHERE id=? AND store_id=? AND period=?',
        (run_id,store_id,period)).fetchone()
    if not run or not run['evidence_ready']:return None
    posting=payload.get('cost_line_posting')
    if isinstance(posting,dict) and posting.get('total') is not None:return posting['total']
    last=ws.conn.execute('SELECT coalesce(max(id),0) FROM cost_line_log WHERE store_id=? AND period=?',
        (store_id,period)).fetchone()[0]
    proof=ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(run_id,)).fetchone()
    validation=ws.conn.execute('SELECT manifest_json FROM automation_run_validation WHERE run_id=? ORDER BY rowid DESC LIMIT 1',
        (run_id,)).fetchone()
    captured=json.loads((validation or proof)['manifest_json']) if validation or proof else {}
    revision=captured.get('manual_revision')
    if not last:return 0 if revision in (None,0) else None
    if type(revision) is not int or not 0<=revision<=last:return None
    active=ws.conn.execute('''SELECT 1 FROM cost_line_log l
        WHERE l.store_id=? AND l.period=? AND l.id<=? AND l.action='save'
        AND l.id=(SELECT max(x.id) FROM cost_line_log x WHERE x.store_id=l.store_id
            AND x.period=l.period AND x.coverage_key=l.coverage_key AND x.id<=?) LIMIT 1''',
        (store_id,period,revision,revision)).fetchone()
    return None if active else 0


def _ordered_people(people):
    """Validate the whole hierarchy before creating any temporary person."""
    if not isinstance(people,list) or any(not isinstance(person,dict) for person in people):
        raise ReplayError('固定人员输入必须是完整列表')
    ids=[person.get('id') for person in people]
    if any(not isinstance(pid,str) or not pid.strip() or pid!=pid.strip() for pid in ids) or len(set(ids))!=len(ids):
        raise ReplayError('固定人员身份缺失或重复')
    known=set(ids)
    if any(str(person.get('parent_id') or '').strip() not in known|{''} for person in people):
        raise ReplayError('固定人员包含未知上级')
    ordered=[];pending=list(people);created=set()
    while pending:
        ready=[person for person in pending if str(person.get('parent_id') or '').strip() in created|{''}]
        if not ready:raise ReplayError('固定人员上级关系存在循环')
        ordered.extend(ready);created.update(person['id'] for person in ready)
        pending=[person for person in pending if person['id'] not in created]
    return ordered


def _seed_fingerprint(seed):
    return hashlib.sha256(json.dumps(seed,ensure_ascii=False,sort_keys=True,
        separators=(',',':'),allow_nan=False).encode()).hexdigest()


def registry_seed_from_snapshot(reference, *, store_ids=None):
    """Adapt hash-bound current configuration facts, never historical approval.

    Source bytes stay in the parent evidence store. The isolated oracle child
    receives the fixed seed and its provenance, not access to that parent path.
    """
    from .commission_registry import local_time,validate_timeline,RegistryError
    if (not isinstance(reference,dict) or set(reference)!={'path','sha256'}
            or not isinstance(reference['path'],str) or not Path(reference['path']).is_absolute()
            or not re.fullmatch('[0-9a-f]{64}',str(reference['sha256']))):
        raise ReplayError('配置快照必须绑定绝对路径和SHA')
    path=Path(reference['path'])
    if path.is_symlink() or not path.is_file() or path.stat().st_size>256*1024*1024:
        raise ReplayError('配置快照必须是有界固定原文件')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=reference['sha256']:raise ReplayError('配置快照SHA已变化')
    snapshot=json.loads(raw)
    _finite(snapshot)
    if (snapshot.get('schema')!=1 or snapshot.get('kind')!='readonly_registry_configuration_snapshot'
            or snapshot.get('read_only_sqlite') is not True or snapshot.get('transaction')!='BEGIN read snapshot'
            or not snapshot.get('meta') or snapshot['meta']!=snapshot.get('meta_after')):
        raise ReplayError('配置快照缺少一致的只读事务身份')
    for table in ('people','members','org_stores','schemes','scheme_versions','policies'):
        if not isinstance(snapshot.get(table),list) or any(not isinstance(row,dict) for row in snapshot[table]):
            raise ReplayError('配置快照缺少完整配置表')
    captured=local_time(snapshot['captured_at'])
    captured_stores=snapshot.get('stores')
    if (not isinstance(captured_stores,list) or not captured_stores
            or any(not isinstance(sid,str) or not sid for sid in captured_stores)
            or len(set(captured_stores))!=len(captured_stores)):
        raise ReplayError('配置快照店铺范围无效')
    selected=sorted(captured_stores if store_ids is None else store_ids)
    if not selected or len(set(selected))!=len(selected) or set(selected)-set(captured_stores):
        raise ReplayError('配置种子不能超出或静默清空采集店铺范围')
    people=_ordered_people(snapshot['people']);person_ids={person['id'] for person in people}
    versions={row['id']:row for row in snapshot['scheme_versions']}
    if len(versions)!=len(snapshot['scheme_versions']):raise ReplayError('配置方案版本身份重复')
    seed={'people':people,'members':[],'schemes':[],'policies':[],'org_stores':[]}
    version_refs=[];seen=set()
    try:
        for scheme in snapshot['schemes']:
            if scheme['store_id'] not in captured_stores:raise ReplayError('配置方案超出原采集店铺范围')
            if scheme['store_id'] not in selected:continue
            key=(scheme['store_id'],scheme['product_id'])
            if key in seen:raise ReplayError('配置方案店铺商品身份重复')
            seen.add(key)
            if not scheme.get('active_version'):continue
            version=versions.get(scheme['active_version'])
            if (not version or version.get('scheme_id')!=scheme['id']
                    or type(version.get('revision')) is not int or version['revision']<1
                    or type(scheme.get('revision')) is not int or version['revision']>scheme['revision']
                    or local_time(version['recorded_at'])>captured):
                raise ReplayError('配置active版本身份或记录日期无效')
            body=json.loads(version['body']) if isinstance(version['body'],str) else version['body']
            if (not isinstance(body,dict) or body.get('store_id',scheme['store_id'])!=scheme['store_id']
                    or body.get('product_id',scheme['product_id'])!=scheme['product_id']):
                raise ReplayError('配置active版本body身份不一致')
            _finite(body);validate_timeline(body,person_ids)
            seed['schemes'].append({'store_id':scheme['store_id'],'product_id':scheme['product_id'],'body':body})
            version_refs.append({'scheme_id':scheme['id'],'active_version':version['id'],
                'source_revision':version['revision'],'recorded_at':version['recorded_at'],
                'body_sha256':_seed_fingerprint(body)})
        intervals={}
        for row in snapshot['members']:
            if row['store_id'] not in captured_stores:raise ReplayError('配置成员超出原采集店铺范围')
            if row['store_id'] not in selected:continue
            if row['person_id'] not in person_ids or row.get('leader_id','') not in person_ids|{''}:
                raise ReplayError('配置成员引用未知人员')
            start=local_time(row['valid_from']);end=local_time(row.get('valid_to',''),optional=True)
            if end and end<=start:raise ReplayError('配置成员有效期无效')
            intervals.setdefault((row['store_id'],row['person_id']),[]).append((start,end))
            seed['members'].append(dict(row))
        for rows in intervals.values():
            rows.sort()
            if any(not a[1] or a[1]>b[0] for a,b in zip(rows,rows[1:])):
                raise ReplayError('配置成员有效期重叠')
        seed['members'].sort(key=lambda row:(row['store_id'],row['person_id'],local_time(row['valid_from'])))
        for row in snapshot['org_stores']:
            if row['person_id'] not in person_ids:raise ReplayError('配置组织归属引用未知人员')
            if row['store_id'] in selected:seed['org_stores'].append(dict(row))
        for row in snapshot['policies']:
            if row['store_id'] and row['store_id'] not in selected:continue
            effective=local_time(row['effective_from'])
            if effective[8:]!='01T00:00:00' or local_time(row['recorded_at'])>captured:
                raise ReplayError('配置政策版本日期无效')
            body=json.loads(row['body']) if isinstance(row['body'],str) else row['body']
            if not isinstance(body,dict):raise ReplayError('配置政策body无效')
            _finite(body)
            seed['policies'].append({'body':body,'effective_from':row['effective_from'],'store_id':row['store_id']})
    except (RegistryError,KeyError,TypeError,ValueError) as error:
        if isinstance(error,ReplayError):raise
        raise ReplayError('配置版本body或有效期验证失败') from error
    source={'kind':'current_configuration_facts_not_historical_truth_or_authorization',
        'reference':dict(reference),'captured_at':snapshot['captured_at'],'meta':snapshot['meta'],
        'captured_store_ids':captured_stores,'selected_store_ids':selected,'active_versions':version_refs,
        'historical_truth_established':False,'new_month_authorized':False}
    seed['source_snapshot']=source
    source['seed_fingerprint']=_seed_fingerprint(seed)
    return seed


def _validate_seed_source(seed):
    if 'source_snapshot' not in seed:return
    source=seed['source_snapshot']
    if (not isinstance(source,dict) or not re.fullmatch('[0-9a-f]{64}',str(source.get('seed_fingerprint','')))
            or source.get('historical_truth_established') is not False or source.get('new_month_authorized') is not False):
        raise ReplayError('固定配置种子缺少来源身份')
    expected=source['seed_fingerprint']
    payload={**seed,'source_snapshot':{key:value for key,value in source.items() if key!='seed_fingerprint'}}
    if _seed_fingerprint(payload)!=expected:raise ReplayError('固定配置来源种子身份已变化')


def _blocked(*_args, **_kwargs):
    raise ReplayError('服务整链回放禁止网络；必须提供固定来源')


def _finite(value):
    if (isinstance(value,float) and not math.isfinite(value)) or (isinstance(value,Decimal) and not value.is_finite()):
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
    def __init__(self, bundle, registry_seed, *, publication_scopes=None):
        self.bundle = Path(bundle).resolve()
        self.registry_seed = registry_seed
        self.trace = []
        self.workspace_decisions = None
        self._workspace_reference = None
        self.publication_scopes = publication_scopes
        self.publication_scope = None

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
            if self.publication_scopes is not None:
                from .publication_scope import PublicationScope,install_isolated,remove_isolated,bind
                if not isinstance(self.publication_scopes,(dict,list,tuple,set)) or not self.publication_scopes:
                    raise ReplayError('离线发布范围必须明确绑定店铺和账期')
                scopes=[]
                for key in self.publication_scopes:
                    if not isinstance(key,str) or key.count('/')!=1:raise ReplayError('离线发布范围缺少明确店月')
                    scopes.append(tuple(key.split('/')))
                self.publication_scope=PublicationScope.for_isolated(self.root,scopes,
                    approval_reference='offline-fixed-input:'+self.fingerprint)
                self.publication_scope.verify(self.root,self.model)
                install_isolated(self.publication_scope)
                self.stack.callback(remove_isolated,self.publication_scope)
                self.stack.enter_context(bind(self.publication_scope,self.root))
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
        _validate_seed_source(self.registry_seed)
        people=_ordered_people(self.registry_seed['people'])
        for person in people:
            self.registry.person_save(person,'replay:seed','固定人员输入')
        assignments={}
        for row in self.registry_seed.get('org_stores',[]):
            assignments.setdefault(row['person_id'],[]).append(row['store_id'])
        for person_id,store_ids in assignments.items():
            self.registry.save_org_stores(person_id,store_ids,'replay:seed','固定组织归属')
        for row in self.registry_seed['members']:
            self.registry.save_store_member(row['store_id'],row['person_id'],row['duty'],row.get('leader_id',''),
                'replay:seed','固定店铺身份',valid_from=row.get('valid_from','1970-01-01'),valid_to=row.get('valid_to',''))
        for row in self.registry_seed['schemes']:
            self.registry.save_scheme(row['store_id'],row['product_id'],row['body'],
                'replay:seed','固定版本关系',publish=True)
        for row in self.registry_seed.get('policies',[]):
            self.registry.save_policy(row['body'],row['effective_from'],'replay:seed','固定计提政策',
                store_id=row.get('store_id',''))

    def recompute(self,store_id):
        from . import service
        result = service.recompute(self.ws,self.model,self.model.store(store_id),note='离线服务整链回放',
            publication_scope=self.publication_scope)
        if result.failure:
            raise ReplayServiceFailure(store_id,result.failure)
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
        if self.publication_scope is None:raise ReplayError('离线Manager缺少明确publication_scopes，不能默认全部账期')
        manager=Manager(lambda:self.ws,lambda:self.model,publication_scope=self.publication_scope)
        stores=sorted({sid for sid,_ in self.publication_scope.scopes})
        for _ in range(max_passes):
            with self.registry.connect() as db:
                remaining=db.execute('SELECT count(*) FROM pending WHERE store_id IN (%s)' % ','.join('?' for _ in stores),stores).fetchone()[0]
            if not remaining:
                self.trace.append({'operation':'commission_manager.once','remaining_pending':0})
                return
            manager.once()
        raise ReplayError('固定输入的待核算队列未清空，不能把排队当作服务回放完成')

    def confirm(self,store_id,period,payouts,reason):
        from . import commission_confirm
        state = self.ws.state(store_id,period)
        if state is None:
            raise ReplayActionRejection('confirm',store_id,period,['no_result'])
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
            codes=[i['code'] for row in preview['blocked'] for i in row['issues']]
            raise ReplayActionRejection('close_approved',store_id,period,codes or ['not_eligible'])
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

    def _declared_scope_keys(self):
        scope=getattr(self,'publication_scope',None)
        if scope is not None:
            scope.verify(self.root,self.model)
            return sorted(sid+'/'+period for sid,period in scope.scopes)
        return sorted(state.store_id+'/'+state.period for state in self.ws.overview())

    def expect_failure(self,operation,arguments,expected):
        """Observe a specifically expected domain refusal, never arbitrary errors.

        Inputs and source identity errors remain ReplayError/ValueError and are
        deliberately not caught. A failure to get the requested refusal fails.
        """
        from .workspace import WorkspaceError
        from .commission_registry import RegistryError
        if operation not in {'recompute','supplement','confirm','replace_scheme','drain_pending','close_approved'}:
            raise ReplayError('拒绝观测不支持此步骤')
        if (not isinstance(arguments,dict) or not isinstance(expected,dict)
            or not {'exception_type','kind','code'}<=set(expected)
            or set(expected)-{'exception_type','kind','code','reason_codes','message'}):
            raise ReplayError('预期失败必须明确异常类型、kind与code')
        declared=self._declared_scope_keys()
        if getattr(self,'publication_scope',None) is None or not declared:
            raise ReplayError('拒绝观测需要明确批准店月范围')
        sid=arguments.get('store_id');period=arguments.get('period')
        targets=[key for key in declared if (sid is None or key.split('/')[0]==sid)
                 and (period is None or key.split('/')[1]==period)]
        if not targets:raise ReplayError('拒绝步骤不在批准范围内')
        try:
            getattr(self,operation)(**arguments)
        except (ReplayServiceFailure,ReplayActionRejection,WorkspaceError,RegistryError,PermissionError) as error:
            actual={'exception_type':type(error).__name__,'kind':None,'code':None}
            if isinstance(error,ReplayServiceFailure):
                actual.update(kind=error.failure.get('kind'),code=error.failure.get('code'))
            elif isinstance(error,ReplayActionRejection):
                actual.update(kind='checker_rejected',code='business_action_rejected',reason_codes=error.reason_codes)
            else:
                actual.update(kind='domain_rejected',code=getattr(error,'code',None),message=str(error))
                if not isinstance(expected.get('message'),str) or not expected['message']:
                    raise ReplayError('没有稳定错误码的领域拒绝必须明确预期message，不能按异常父类吞错') from error
            changes=differences(expected,actual)
            if changes:raise ReplayError('实际失败类型或原因与明确预期不符：'+json.dumps(changes,ensure_ascii=False)) from error
            self.trace.append({'operation':'expected_failure','requested_operation':operation,
                'scope_keys':targets,'failure':actual})
            return actual
        raise ReplayError('预期的失败或拒绝没有发生，不能把成功执行算成负向通过')

    def _failure_observations(self,declared):
        failures={};attempted=set()
        for step in self.trace:
            if step.get('operation')=='expected_failure':
                for key in step['scope_keys']:
                    if key in declared:failures[key]=step['failure'];attempted.add(key)
            elif step.get('operation')=='service.recompute':
                for key in declared:
                    if key.split('/')[0]==step['store_id']:
                        failures.pop(key,None);attempted.add(key)
            elif step.get('operation') in {'payout.confirm','close.confirm','cost_lines.save'}:
                key=step['store_id']+'/'+step['period'];failures.pop(key,None)
        return failures,attempted

    def snapshot(self):
        from .storage_integrity import verified
        from .commission_confirm import _last
        from . import automation_checker
        if self._workspace_reference:
            reference,fixture=self._workspace_reference
            load_workspace_decisions(reference,fixture=fixture)
        scopes = {}; proofs = {}; observations={}
        states={state.store_id+'/'+state.period:state for state in self.ws.overview()}
        declared=self._declared_scope_keys()
        failures,attempted=self._failure_observations(set(declared))
        companies={period:automation_checker.company_basis(self.ws,self.model,period)
                   for period in sorted({key.split('/')[1] for key in declared})}
        for key in declared:
            store_id,period=key.split('/');state=states.get(key);run_id=state.run_id if state else None
            checked=automation_checker.check(self.ws,store_id,period,model=self.model,
                expected_run_id=run_id,verify_artifacts=True,company_context=companies[period])
            if state is None and checked['run_id'] is not None:
                raise ReplayError('存在孤立run但无对应账期，不能伪装成无资料')
            failure=failures.get(key);payload=state.result or {} if state else {}
            if failure:outcome='failed' if failure['exception_type']=='ReplayServiceFailure' else 'rejected'
            elif not run_id:outcome='no_run' if key in attempted else 'not_started'
            elif state.closed:outcome='closed'
            else:outcome='ready' if checked['allowed'] else 'pending'
            if failure:source_state='failed' if failure['exception_type']=='ReplayServiceFailure' else 'rejected'
            elif not run_id:source_state='no_published_result' if key in attempted else 'not_attempted'
            elif payload.get('source_sync_pending') is True:source_state='sync_pending'
            elif payload.get('missing_sources'):source_state='missing_sources'
            elif payload.get('source_sync_pending') is False:source_state='captured'
            else:source_state='unknown'
            company=companies[period];missing=sorted(company.get('missing_store_ids') or [])
            income_complete=bool(company.get('basis')) and not missing
            observations[key]={'run_present':bool(run_id),'state':state.state if state else None,
                'outcome':outcome,'source_state':source_state,'checker_allowed':bool(checked['allowed']),
                'reason_codes':sorted({p['code'] for p in checked['issues']}),'failure':failure,
                'company':{'total':Decimal(str(company['total'])) if company.get('total') is not None else None,
                    'basis_total':Decimal(str(company['basis_total'])) if income_complete and company.get('basis_total') is not None else None,
                    'known_income_subtotal':Decimal(str(company['basis_total'])) if company.get('basis_total') is not None else None,
                    'income_complete':income_complete,'missing_store_ids':missing}}
            if not run_id:continue
            if not verified(self.ws.facts_path(state.run_id)):
                raise ReplayError('服务事实留档缺失或未通过校验')
            commission = payload.get('commission') or {}
            with self.registry.connect() as db:
                confirmation = _last(db,state.store_id,state.period,state.run_id)
                calculation = db.execute('SELECT path,sha FROM calculation WHERE finance_run=?',(state.run_id,)).fetchone()
            if commission.get('calculation_id'):
                import hashlib
                if not calculation or hashlib.sha256((self.registry.root/'calculations'/Path(calculation['path']).name).read_bytes()).hexdigest()!=calculation['sha']:
                    raise ReplayError('服务提成留档缺失或校验失败')
            scopes[key] = {'state':state.state,
                'statement':{r['id']:{'value':r.get('value'),'available':r.get('available')}
                    for r in payload.get('statement') or []},
                'commission_total':commission.get('total'),
                'commission_complete':commission.get('amount_complete'),
                'can_close':bool(checked['allowed'] and not state.closed),
                'source_sync_pending':payload.get('source_sync_pending'),
                'commission_people':{p['person_id']:{'base':p.get('base'),'amount':p.get('amount')}
                    for p in commission['people']} if isinstance(commission.get('people'),list) else None,
                'confirmed_total':float(confirmation['confirmed_total']) if confirmation else None,
                'confirmed_people':{p['person_id']:p['amount'] for p in confirmation['payouts']} if confirmation else {},
                'cost_line_total':_posted_cost_line_total(self.ws,state.store_id,state.period,state.run_id,payload),
                'allocation_pending_count':payload.get('allocation_pending_count')}
            row = self.ws.conn.execute('SELECT input_fingerprint,model_revision FROM run WHERE id=?',(state.run_id,)).fetchone()
            proofs[key] = {'run_id':state.run_id,'input_fingerprint':row['input_fingerprint'],
                'model_revision':row['model_revision'],'calculation_sha':calculation['sha'] if calculation else None,
                'engine_can_close':bool(payload.get('can_close')),
                'close_issue_codes':sorted(p['code'] for p in checked['issues'])}
        _finite(scopes);_finite(observations)
        return {'scopes':scopes,'proofs':proofs,'observations':observations,'declared_scopes':declared,
                'registry_revision':self.registry.revision(),
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

    def check_rejection(self,expected):
        """Assert explicitly incomplete/refused scopes without positive credit."""
        if (not isinstance(expected,dict) or expected.get('schema_version')!=1
            or expected.get('category') not in REJECTION_CATEGORIES
            or not isinstance(expected.get('scopes'),dict) or not expected['scopes']):
            raise ReplayError('拒绝期望缺少版本、明确分类和批准范围')
        if getattr(self,'publication_scope',None) is None:
            raise ReplayError('负向验收不能从现有run猜批准范围')
        declared=self._declared_scope_keys()
        if set(expected['scopes'])!=set(declared):
            raise ReplayError('拒绝期望必须覆盖全部批准scope，包含没有run的店月')
        category=expected['category'];actual=self.snapshot();changes=[];asserted=[];source_rejected=0
        for key,contract in expected['scopes'].items():
            if not isinstance(contract,dict) or set(contract)!={'observation','proved','unknown'}:
                raise ReplayError('每个拒绝scope必须分别声明observation/proved/unknown')
            observation=contract['observation']
            if (not isinstance(observation,dict) or not REJECTION_OBSERVATION_FIELDS<=set(observation)
                or set(observation)-REJECTION_OBSERVATION_FIELDS-{'company'}):
                raise ReplayError('拒绝观测必须明确run、状态、来源状态、Checker与失败类型')
            codes=observation['reason_codes']
            if (not isinstance(codes,list) or not codes or any(not isinstance(c,str) or not c for c in codes)
                or codes!=sorted(set(codes))):raise ReplayError('预期Checker原因码须为非空且排序去重的明确集合')
            if not isinstance(observation['run_present'],bool) or not isinstance(observation['checker_allowed'],bool):
                raise ReplayError('拒绝观测的run与Checker标志必须是布尔值')
            if (observation['outcome'] not in {'not_started','no_run','pending','failed','rejected','ready','closed'}
                or observation['source_state'] not in {'not_attempted','no_published_result','sync_pending','missing_sources','captured','unknown','failed','rejected'}
                or observation['state'] not in {None,'open','closed'}):
                raise ReplayError('拒绝观测的状态必须使用明确合同枚举')
            failure=observation['failure']
            if failure is not None and (not isinstance(failure,dict) or not {'exception_type','kind','code'}<=set(failure)):
                raise ReplayError('拒绝观测的失败必须明确受信类型/kind/code，或显式None')
            for name,unknown in [('proved',False),('unknown',True)]:
                _validate_rejection_financial(contract[name],unknown=unknown)
            if contract['proved'] and contract['unknown'] and _leaf_paths(contract['proved'])&_leaf_paths(contract['unknown']):
                raise ReplayError('同一财务字段不能既声称已证又声称未知')
            got=actual['observations'][key]
            if category=='scope_coverage':
                if contract['proved'] or contract['unknown'] or observation['outcome']!='not_started':
                    raise ReplayError('纯清单基线不能冒充已执行资料拒绝或金额验证')
            elif category=='incomplete_data':
                if (observation['checker_allowed'] or observation['outcome'] in {'ready','closed','not_started'}
                    or observation['state']=='closed' or NON_BUSINESS_REASONS&set(codes)):
                    raise ReplayError('技术/身份故障、未开始或已完成不能冒充资料不足负例')
                failure=observation['failure']
                if failure:
                    if (failure.get('exception_type') not in {'ReplayServiceFailure','ReplayActionRejection'}
                        or failure.get('kind') in NON_BUSINESS_FAILURES or failure.get('kind') is None
                        or (failure.get('exception_type')=='ReplayServiceFailure' and failure.get('kind') not in SOURCE_FAILURE_KINDS)):
                        raise ReplayError('缺少明确业务来源失败类型；请求/权限/技术故障不能计入资料拒绝覆盖')
                if got['outcome'] in {'pending','no_run','failed','rejected'} and not got['checker_allowed']:
                    source_rejected+=1
            elif observation['failure'] is None and not NON_BUSINESS_REASONS&set(codes):
                raise ReplayError('安全边界案例必须明确真实拒绝或身份完整性阻断')
            if not observation['run_present'] and (contract['proved'] or contract['unknown']):
                raise ReplayError('没有run时金融结果缺席；不能伪造带金额字段的结果')
            changes.extend(_partial_differences(observation,got,'observations.'+key))
            if observation['run_present']:
                if key not in actual['scopes']:
                    changes.append({'path':'scopes.'+key,'expected':'financial_result','actual':None,'kind':'missing_field'})
                else:
                    for name in ('proved','unknown'):
                        if contract[name]:
                            changes.extend(_partial_differences(contract[name],actual['scopes'][key],'scopes.'+key))
                            asserted.extend(key+'/'+p for p in _leaf_paths(contract[name]))
        return {'status':'failed' if changes else 'passed','assertion_kind':'rejection',
            'category':category,'differences':changes,'checked_scopes':declared,
            'asserted_financial_fields':sorted(asserted),'source_rejection_scope_count':source_rejected if not changes else 0,
            'positive_scope_count':0,'ready_scope_count':0,'closed_scope_count':0,
            'eligible_for_financial_release':False,'actual':actual}


def execute(case):
    if case.get('schema_version')!=1 or not case.get('case_id'):
        raise ReplayError('服务回放案例合同无效')
    if case.get('kind') not in {'service_fixture','historical_golden','service_rejection_fixture','source_rejection'}:
        raise ReplayError('必须区分服务夹具与真实历史黄金语料')
    if case['kind'] in {'historical_golden','source_rejection'} and not case.get('approval_reference'):
        raise ReplayError('真实历史黄金语料缺批准依据')
    if not case.get('expected',{}).get('scopes'):
        raise ReplayError('真实语料或期望缺失，回放失败；不会自动录制基线')
    with Session(case['bundle'],case.get('registry'),publication_scopes=case['expected']['scopes']) as session:
        reference=case.get('workspace_decisions')
        if reference is not None:
            if not isinstance(reference,dict):raise ReplayError('工作区决定引用必须为对象')
            if case['kind'] in {'historical_golden','source_rejection'} and (case.get('source_evidence') or {}).get('workspace_decisions_sha256')!=reference.get('sha256'):
                raise ReplayError('历史案例的工作区决定缺少source_evidence SHA绑定')
            session.restore_workspace_decisions(reference,fixture=case['kind'] in {'service_fixture','service_rejection_fixture'})
        for step in case.get('steps',[]):
            arguments = {k:v for k,v in step.items() if k!='operation'}
            operation = step.get('operation')
            if operation not in {'recompute','supplement','confirm','replace_scheme','drain_pending','close_approved','expect_failure'}:
                raise ReplayError('不支持的离线回放步骤：'+str(operation))
            if operation=='expect_failure' and case['kind'] not in {'service_rejection_fixture','source_rejection'}:
                raise ReplayError('只有显式负向案例可以声明expected failure')
            if case['kind']=='source_rejection' and operation not in {'recompute','drain_pending','expect_failure'}:
                raise ReplayError('真实拒绝轨道不执行成功的金额确认、补录或结账步骤')
            nested_close=(operation=='expect_failure' and arguments.get('operation')=='close_approved')
            close_arguments=arguments.get('arguments',{}) if nested_close else arguments
            if nested_close and (not case.get('approval_reference') or close_arguments.get('approval_reference')!=case['approval_reference']):
                raise ReplayError('预期拒绝中的月结批准也必须与案例完全一致')
            if operation=='close_approved' and (not case.get('approval_reference')
                or arguments.get('approval_reference')!=case['approval_reference']):
                raise ReplayError('月结步骤的批准引用必须与批准案例完全一致')
            getattr(session,operation)(**arguments)
        report = (session.check_rejection(case['expected']) if case['kind'] in {'service_rejection_fixture','source_rejection'}
                  else session.check(case['expected']))
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
