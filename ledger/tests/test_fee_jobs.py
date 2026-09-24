import json
import shutil
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from conftest import MODELS
from ledger import fee_jobs, service
from ledger.commission_registry import Registry
from ledger.model.loader import load_model
from ledger.model.schema import FeeRule
from ledger.workspace import Workspace


@pytest.fixture
def setup(tmp_path):
    root=tmp_path/'model';shutil.copytree(MODELS/'cn-ecommerce',root)
    ws=Workspace(tmp_path/'workspace')
    ws.store_ids=lambda:['taobao_msy387nx','pdd_huanglishi','douyin_luckywish']
    return ws,root,lambda:load_model(root)


def submit(setup,platform='douyin',**options):
    ws,root,model=setup;rules=[*model().fee_rules,FeeRule(platform=platform,value='测试专用费项',major='software_fee')]
    return fee_jobs.submit(ws,root,rules,expected=fee_jobs.revision(model().fee_rules),request_id=str(uuid.uuid4()),**options)


def job_row(ws,jid):
    with Registry(ws.root).connect() as c:return dict(c.execute('SELECT * FROM job WHERE id=?',(jid,)).fetchone())


def test_save_returns_durable_job_without_running_engine(setup,monkeypatch):
    monkeypatch.setattr(service,'recompute',lambda *a,**k:pytest.fail('request must not calculate'))
    job=submit(setup)
    assert job['status']=='queued' and job['saved']
    assert [s['store_id'] for s in job['stores']]==['douyin_luckywish']
    assert job['total']==1
    assert setup[2]().fee_rules[-1].at


def test_wildcard_and_reorder_affect_effective_platform_sequence():
    a=FeeRule(platform='douyin',value='a',exclude=True);b=FeeRule(platform='pdd',value='b',exclude=True);c=FeeRule(value='c',exclude=True)
    assert fee_jobs.affected([a,b],[b,a],['douyin','pdd','taobao'])==set()
    assert fee_jobs.affected([a,c,b],[c,a,b],['douyin','pdd','taobao'])=={'douyin'}
    assert fee_jobs.affected([c],[],['douyin','pdd'])=={'douyin','pdd'}
    assert not fee_jobs.affected([a],[a.model_copy(update={'note':'note','at':'later'})],['douyin'])


def test_idempotent_retry_does_not_resave_or_enqueue(setup):
    ws,root,model=setup;rules=[*model().fee_rules,FeeRule(platform='pdd',value='x',major='software_fee')]
    kwargs=dict(expected=fee_jobs.revision(model().fee_rules),request_id=str(uuid.uuid4()))
    first=fee_jobs.submit(ws,root,rules,**kwargs)
    second=fee_jobs.submit(ws,root,rules,**kwargs)
    assert second['id']==first['id']
    assert ws.conn.execute("select count(*) from config_log where kind='fee-rules'").fetchone()[0]==1
    with pytest.raises(fee_jobs.Conflict):fee_jobs.submit(ws,root,[],**kwargs)


def test_stale_editor_cannot_overwrite_or_queue(setup):
    ws,root,model=setup;before=(root/'fee-rules.csv').read_bytes()
    with pytest.raises(fee_jobs.Conflict):fee_jobs.submit(ws,root,[],expected='stale',request_id=str(uuid.uuid4()))
    assert (root/'fee-rules.csv').read_bytes()==before
    assert fee_jobs.status(ws) is None


def test_old_request_replay_cannot_claim_newer_rules_are_its_own(setup):
    from ledger.model.config import replace_fee_rules
    ws,root,model=setup;original=model().fee_rules
    proposed=[*original,FeeRule(platform='pdd',value='x',major='software_fee')]
    kwargs=dict(expected=fee_jobs.revision(original),request_id=str(uuid.uuid4()))
    fee_jobs.submit(ws,root,proposed,**kwargs)
    replace_fee_rules(root,list(original))
    with pytest.raises(fee_jobs.Conflict,match='当前规则已更新'):
        fee_jobs.submit(ws,root,proposed,**kwargs)
    assert model().fee_rules==original


def test_concurrent_rule_batch_is_rejected_before_write(setup):
    first=submit(setup)
    with pytest.raises(fee_jobs.Conflict,match='上一批'):submit(setup,'pdd')
    assert fee_jobs.status(setup[0])['id']==first['id']


def test_worker_counts_failures_and_retries_only_failed_stores(setup,monkeypatch):
    ws,root,model=setup;job=submit(setup,'*');called=[]
    def calculate(ws,m,s,**kw):
        called.append(s.id);kw['report']('归类核算',0,0)
        return SimpleNamespace(failure={'why':'可见失败'} if s.platform=='pdd' else None,
                               periods=[{'run_id':1,'can_close':False}])
    monkeypatch.setattr(service,'recompute',calculate)
    for _ in range(3):fee_jobs.step(ws,model,job_row(ws,job['id']))
    result=fee_jobs.status(ws)
    assert result['status']=='failed' and result['completed']==3 and result['failed']==1
    assert result['percent']==100 and any(s.get('error')=='可见失败' for s in result['stores'])
    fee_jobs.retry(ws,model(),job['id'])
    monkeypatch.setattr(service,'recompute',lambda *a,**k:SimpleNamespace(failure=None,periods=[]))
    fee_jobs.step(ws,model,job_row(ws,job['id']))
    assert fee_jobs.status(ws)['status']=='done'
    assert len(json.loads(job_row(ws,job['id'])['result'])['attempts'])==1


def test_rule_change_before_execution_is_not_silently_used(setup,monkeypatch):
    ws,root,model=setup;job=submit(setup)
    different=model().model_copy(update={'fee_rules':()})
    monkeypatch.setattr(service,'recompute',lambda *a,**k:pytest.fail('stale task must stop'))
    fee_jobs.step(ws,lambda:different,job_row(ws,job['id']))
    assert fee_jobs.status(ws)['status']=='failed'


def test_adopt_saved_legacy_request_without_rewriting_rules(setup):
    from ledger.model.config import replace_fee_rules
    ws,root,model=setup;before=model().fee_rules
    after=(*before,FeeRule(platform='douyin',value='legacy',major='software_fee'))
    replace_fee_rules(root,list(after))
    ws.log_config('fee-rules','old request',before=fee_jobs.rows(before),after=fee_jobs.rows(after))
    cid=ws.conn.execute('select max(id) from config_log').fetchone()[0]
    contents=(root/'fee-rules.csv').read_bytes()
    job=fee_jobs.adopt_legacy(ws,model(),cid)
    assert job['total']==1 and job['platforms']==['douyin']
    assert fee_jobs.adopt_legacy(ws,model(),cid)['id']==job['id']
    assert (root/'fee-rules.csv').read_bytes()==contents


def test_recover_prepared_committed_save_without_rewriting_model(setup,monkeypatch):
    ws,root,model=setup
    original=fee_jobs._audit
    monkeypatch.setattr(fee_jobs,'_audit',lambda *a:(_ for _ in ()).throw(RuntimeError('crash after model save')))
    with pytest.raises(RuntimeError):submit(setup)
    job=fee_jobs.status(ws);assert job['status']=='preparing'
    saved=(root/'fee-rules.csv').read_bytes()
    monkeypatch.setattr(fee_jobs,'_audit',original)
    fee_jobs.recover(ws,model)
    assert fee_jobs.status(ws)['status']=='queued'
    assert (root/'fee-rules.csv').read_bytes()==saved
    fee_jobs.recover(ws,model)
    assert ws.conn.execute("select count(*) from config_log where kind='fee-rules'").fetchone()[0]==1


def test_api_fast_response_status_and_conflict(setup,monkeypatch):
    import ledger.api as api
    ws,root,model=setup
    monkeypatch.setattr(api,'DEFAULT_MODEL',root)
    monkeypatch.setattr(api,'workspace',lambda:ws)
    monkeypatch.setattr(api,'_model',model)
    monkeypatch.setattr(api,'_invalidate_model',lambda:None)
    monkeypatch.setattr(service,'recompute',lambda *a,**k:pytest.fail('request must return before calculation'))
    client=TestClient(api.app)
    body=dict(rules=[*[r.model_dump() for r in model().fee_rules],FeeRule(value='z',platform='douyin',major='software_fee').model_dump()],
              expected_fee_revision=fee_jobs.revision(model().fee_rules),request_id=str(uuid.uuid4()))
    response=client.post('/api/fees',json=body)
    assert response.status_code==200,response.text
    assert response.json()['job']['status']=='queued'
    assert client.get('/api/fees/jobs').json()['job']['id']==response.json()['job']['id']
    assert client.post('/api/fees',json={**body,'request_id':str(uuid.uuid4())}).status_code==409


def test_restart_keeps_finished_stores_and_resumes_interrupted_one(setup,monkeypatch):
    ws,root,model=setup;job=submit(setup,'*');registry=Registry(ws.root)
    payload=json.loads(job_row(ws,job['id'])['payload']);first,second,*_=payload['stores']
    state={'stores':{first:{'state':'done','percent':100,'phase':'已完成'},second:{'state':'running','percent':55,'phase':'归类核算'}}}
    with registry.transaction() as c:c.execute("UPDATE job SET status='running',result=? WHERE id=?",(json.dumps(state),job['id']))
    fee_jobs.restart(registry)
    recovered=fee_jobs.status(ws)
    assert recovered['succeeded']==1 and recovered['status']=='queued'
    assert next(s for s in recovered['stores'] if s['store_id']==second)['state']=='queued'
    calls=[]
    def calculate(ws,m,s,**kw):calls.append(s.id);return SimpleNamespace(failure=None,periods=[])
    monkeypatch.setattr(service,'recompute',calculate)
    fee_jobs.step(ws,model,job_row(ws,job['id']))
    assert calls==[second]


def test_fee_worker_yields_to_existing_source_queue(setup,monkeypatch):
    from ledger.commission_manager import Manager
    ws,root,model=setup;job=submit(setup,'*');registry=Registry(ws.root)
    monkeypatch.setenv('LEDGER_ORDER_FEED_ENABLED','0')
    registry.enqueue_source({'pdd_huanglishi'},'order-feed:snapshot:10')
    calls=[]
    def calculate(ws,m,s,**kw):
        calls.append(kw.get('note'));return SimpleNamespace(failure=None,periods=[])
    monkeypatch.setattr(service,'recompute',calculate)
    worker=Manager(lambda:ws,model)
    worker.once();worker.once();worker.once()
    assert calls==['费项规则更新','订单数据更新','费项规则更新']


def test_invalid_rules_leave_original_model_and_fail_visible_job(setup):
    ws,root,model=setup;before=(root/'fee-rules.csv').read_bytes()
    with pytest.raises(Exception):
        fee_jobs.submit(ws,root,[FeeRule(value='invalid',major='missing_major')],expected=fee_jobs.revision(model().fee_rules),request_id=str(uuid.uuid4()))
    assert (root/'fee-rules.csv').read_bytes()==before
    assert fee_jobs.status(ws)['status']=='failed'
