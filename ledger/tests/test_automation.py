"""Temporary-DB business contracts; no production endpoints or source writes."""
import json
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledger import automation_actions as actions,automation_checker as checker
from ledger.automation_api import install
from ledger.commission_registry import Registry
from ledger.model.schema import Store,Overhead
from ledger.workspace import Workspace,WorkspaceError
from test_commission import _model

HUMAN={'id':'finance','kind':'human','authenticated':True}
PERIOD='2030-06'


@pytest.fixture
def sample(tmp_path):
    ws=Workspace(tmp_path/'workspace')
    model=_model(stores=(Store(id='s1',name='甲',platform='taobao'),Store(id='s2',name='乙',platform='taobao')))
    model=model.model_copy(update={'statement':tuple(n.model_copy(update={'headline':'revenue'}) if n.id=='n_receipt' else n for n in model.statement),
                                  'overheads':(Overhead(period=PERIOD,amount=100),)})
    checker.bind_model(ws,model)
    for sid,value in [('s1',100),('s2',300)]:record(ws,sid,value)
    yield ws,model
    ws.close()


def record(ws,sid,value,**extra):
    return ws.record(sid,PERIOD,{'can_close':True,'statement':[
        {'id':'n_receipt','value':value,'available':True},{'id':'net_profit','name':'利润','value':50,'available':True}],
        'findings':[],'missing_sources':[],'commission':{'amount_complete':True},**extra},[])


def policy(ws,model,request,kind,values,store_id='s2',period=PERIOD):
    p=actions.decision_preview(ws,model,request_id=request+'-p',period=period,store_id=store_id,
        kind=kind,values=values,reason='核对已确认营业起止与规则范围',actor=HUMAN)
    return actions.decision_apply(ws,model,request_id=request+'-a',proposal_id=p['proposal_id'],
        proposal_sha=p['proposal_sha'],input_sha=p['input_sha'],actor=HUMAN)


def test_scope_defaults_to_one_month_and_explicit_range_resolves_latest_then_revocation(sample):
    from ledger import automation_store as journal
    ws,model=sample
    policy(ws,model,'single','store_scope',{'included':False})
    assert ('s2','store_scope') not in journal.active_decisions(ws.conn,'2030-07')
    policy(ws,model,'range','store_scope',{'included':False,'valid_from':'2030-06','valid_until':'2030-09'})
    assert journal.active_decisions(ws.conn,'2030-08')[('s2','store_scope')]['values']['included'] is False
    july=policy(ws,model,'july','store_scope',{'included':True},period='2030-07')
    assert journal.active_decisions(ws.conn,'2030-07')[('s2','store_scope')]['values']['included'] is True
    row=ws.conn.execute('SELECT * FROM automation_decision WHERE id=?',(july['decision_id'],)).fetchone()
    sha=actions._decision_input(ws,model,'store_scope','s2','2030-07',json.loads(row['values_json']))
    actions.decision_revoke(ws,model,july['decision_id'],request_id='revoke-july',reason='重新核对七月没有营业，撤回临时纳入决定',input_sha=sha,actor=HUMAN)
    assert journal.active_decisions(ws.conn,'2030-07')[('s2','store_scope')]['values']['included'] is False
    policies=actions.month(ws,model,'2030-07')['policies']
    assert next(p for p in policies if p['decision_id']==july['decision_id'])['status']=='revoked'
    assert next(p for p in policies if p['status']=='active')['values']['included'] is False


def test_scope_range_cannot_rewrite_any_frozen_month(sample):
    ws,model=sample
    ws.close_period('s1',PERIOD,model=model)
    with pytest.raises(WorkspaceError,match='冻结'):
        policy(ws,model,'backdate','store_scope',{'included':False,'valid_from':PERIOD,'valid_until':'2030-09'})
    assert checker.company_basis(ws,model,PERIOD)['basis_total']=='400'


def test_rule_grant_business_month_range_is_independent_from_execution_validity(sample):
    from datetime import datetime,timedelta,timezone
    from ledger.automation_deterministic import approved_rule,RULE,RULE_SHA
    ws,model=sample;stamp=datetime.now(timezone.utc)
    value={'rule_id':RULE,'rule_sha':RULE_SHA,'enabled':True,
        'valid_from':(stamp-timedelta(days=1)).isoformat(),'valid_until':(stamp+timedelta(days=1)).isoformat(),
        'period_from':'2030-06','period_until':'2030-09'}
    applied=policy(ws,model,'rule','rule_grant',value)
    assert approved_rule(ws,'s2','2030-08')['id']==applied['decision_id']
    with pytest.raises(PermissionError):approved_rule(ws,'s2','2030-10')
    policy(ws,model,'expired','rule_grant',{**value,'valid_from':(stamp-timedelta(days=3)).isoformat(),
        'valid_until':(stamp-timedelta(days=2)).isoformat()})
    with pytest.raises(PermissionError,match='有效期'):approved_rule(ws,'s2','2030-08')
    policy_status=actions.month(ws,model,'2030-08')['policies']
    assert policy_status[0]['status']=='expired' and policy_status[0]['can_revoke']
    assert policy_status[1]['status']=='superseded'


def test_rule_metadata_exposes_business_copy_and_fixed_published_contracts(sample):
    from ledger.automation_rules import contracts
    ws,model=sample;app=FastAPI();install(app,lambda:ws,lambda:model)
    result=TestClient(app).get('/api/automation/rule-contracts')
    assert result.status_code==200 and result.json()['requires_human_grant']
    for rule in result.json()['rules']:
        assert rule['label'] and rule['description']
        assert rule['contract_sha']==contracts()[rule['id']][0]==rule['rule_sha']
        assert rule['allowed_scope']['default_business_months']==1
    assert not ws.conn.execute('SELECT 1 FROM automation_decision').fetchone()


def test_outward_close_view_uses_company_checker_without_mutating_stored_engine_result(sample):
    ws,model=sample
    original=json.loads(ws.latest_run('s1',PERIOD)['result'])
    assert checker.close_view(ws,model,'s1',PERIOD,original)['can_close']
    record(ws,'s2',None)
    shown=checker.close_view(ws,model,'s1',PERIOD,original)
    assert shown['engine_can_close'] is True and shown['can_close'] is False
    assert 'company_revenue_unknown' in {i['code'] for i in shown['close_verification']['issues']}
    assert original==json.loads(ws.latest_run('s1',PERIOD)['result'])


def test_outward_cached_payload_cannot_certify_a_different_new_run(sample):
    ws,model=sample;old=ws.latest_run('s1',PERIOD)
    cached={**json.loads(old['result']),'run_id':old['id']}
    record(ws,'s1',101)
    shown=checker.close_view(ws,model,'s1',PERIOD,cached)
    assert not shown['can_close']
    assert 'stale_run' in {i['code'] for i in shown['close_verification']['issues']}


def preview(ws,model,request='preview'):
    return actions.close_preview(ws,model,request_id=request,period=PERIOD,store_ids=['s1','s2'],actor=HUMAN)


def confirm(ws,model,p,request='confirm'):
    return actions.close_confirm(ws,model,request_id=request,proposal_id=p['proposal_id'],
        proposal_sha=p['proposal_sha'],input_sha=p['input_sha'],actor=HUMAN)


def test_month_includes_operating_stores_without_any_data(sample):
    ws,model=sample
    model=model.model_copy(update={'stores':(*model.stores,Store(id='new',name='新店',platform='taobao'))})
    value=actions.month(ws,model,PERIOD)
    assert len(value['stores'])==3
    new=next(s for s in value['stores'] if s['store_id']=='new')
    assert new['status']=='processing' and new['run_id'] is None
    assert value['overhead']['missing_store_ids']==['new']
    assert not value['close']['eligible_store_ids']


def test_unknown_company_income_cannot_be_zeroed_at_single_close(sample):
    ws,model=sample
    record(ws,'s2',None)
    with pytest.raises(WorkspaceError,match='收入未确定'):
        ws.close_period('s1',PERIOD,model=model)
    assert ws.state('s1',PERIOD).state=='open'


def test_monthly_overhead_missing_differs_from_explicit_zero(sample):
    ws,model=sample
    missing=model.model_copy(update={'overheads':()})
    assert checker.company_basis(ws,missing,PERIOD)['shares']=={}
    zero=model.model_copy(update={'overheads':(Overhead(period=PERIOD,amount=0),)})
    assert checker.company_basis(ws,zero,PERIOD)['shares']=={'s1':'0.00','s2':'0.00'}


def test_batch_close_freezes_company_basis_and_preserves_cent_total(sample):
    ws,model=sample;p=preview(ws,model)
    result=confirm(ws,model,p)
    assert result['status']=='completed' and len(result['completed'])==2
    assert ws.state('s1',PERIOD).labor_cut=='25.00'
    assert ws.state('s2',PERIOD).labor_cut=='75.00'
    assert sum(Decimal(r[0]) for r in ws.conn.execute('SELECT amount FROM run_labor'))==100
    assert ws.conn.execute('SELECT count(*) FROM automation_overhead_close').fetchone()[0]==1
    assert ws.conn.execute('SELECT count(*) FROM automation_authorization').fetchone()[0]==2
    assert ws.conn.execute('SELECT count(*) FROM automation_receipt').fetchone()[0]==2


def test_confirm_replay_is_idempotent_and_different_payload_is_rejected(sample):
    ws,model=sample;p=preview(ws,model)
    first=confirm(ws,model,p)
    assert confirm(ws,model,p)==first
    assert ws.conn.execute('SELECT count(*) FROM run_labor').fetchone()[0]==2
    with pytest.raises(ValueError,match='同一请求'):
        actions.close_confirm(ws,model,request_id='confirm',proposal_id='different',proposal_sha=p['proposal_sha'],input_sha=p['input_sha'],actor=HUMAN)


def test_preview_change_rejects_whole_confirmation_without_partial_writes(sample):
    ws,model=sample;p=preview(ws,model)
    record(ws,'s2',301)
    before=ws.conn.execute('SELECT count(*) FROM automation_authorization').fetchone()[0]
    with pytest.raises(actions.Conflict,match='预览后'):
        confirm(ws,model,p)
    assert all(ws.state(s,PERIOD).state=='open' for s in ['s1','s2'])
    assert ws.conn.execute('SELECT count(*) FROM automation_authorization').fetchone()[0]==before
    assert not ws.conn.execute('SELECT 1 FROM automation_overhead_close').fetchone()


@pytest.mark.parametrize('code',['source_sync_pending','evidence_archive','promotion_integrity','promotion_scope_evidence','allocation_basis','shared_store_identity'])
def test_single_ignored_blockers_cannot_bypass_hard_checks(sample,code):
    ws,model=sample
    rid=record(ws,'s1',100,can_close=False,findings=[{'id':code,'blocking':True,'passed':False,'message':'硬阻断'}])
    with pytest.raises(WorkspaceError):
        ws.close_period('s1',PERIOD,model=model,expected_run_id=rid,note='人工例外',ignored_blockers=(code,))


def test_scope_decision_does_not_make_missing_revenue_zero(sample):
    ws,model=sample
    model=model.model_copy(update={'stores':(*model.stores,Store(id='new',name='未营业',platform='taobao'))})
    p=actions.decision_preview(ws,model,request_id='p',period=PERIOD,store_id='new',kind='store_scope',values={'included':False},reason='本月未营业，已核对营业范围',actor=HUMAN)
    result=actions.decision_apply(ws,model,request_id='a',proposal_id=p['proposal_id'],proposal_sha=p['proposal_sha'],input_sha=p['input_sha'],actor=HUMAN)
    value=actions.month(ws,model,PERIOD)
    assert result['status']=='applied'
    assert next(r for r in value['stores'] if r['store_id']=='new')['stage']=='excluded'
    assert 'new' not in [r['store_id'] for r in value['overhead']['basis']]


def test_decisions_receipts_are_immutable(sample):
    ws,model=sample
    p=actions.decision_preview(ws,model,request_id='p',period=PERIOD,store_id='',kind='overhead_total',values={'amount':'0.00'},reason='本月确认无兼职支出',actor=HUMAN)
    actions.decision_apply(ws,model,request_id='a',proposal_id=p['proposal_id'],proposal_sha=p['proposal_sha'],input_sha=p['input_sha'],actor=HUMAN)
    assert checker.company_basis(ws,model,PERIOD)['shares']['s1']=='0.00'
    with pytest.raises(Exception,match='immutable'):
        with ws.conn:ws.conn.execute('DELETE FROM automation_decision')


def test_frozen_company_denominator_cannot_be_repriced_by_later_income(sample):
    ws,model=sample
    ws.close_period('s1',PERIOD,model=model)
    record(ws,'s2',600)
    assert any(i['code']=='overhead_frozen_input_changed' for i in checker.check(ws,'s2',PERIOD,model=model)['issues'])
    assert ws.state('s1',PERIOD).labor_cut=='25.00'


def test_service_identity_and_unconfigured_auth_cannot_confirm(sample):
    ws,model=sample
    with pytest.raises(PermissionError):actions.close_preview(ws,model,request_id='x',period=PERIOD,store_ids=['s1'],actor={'id':'worker','kind':'service'})
    app=FastAPI();install(app,lambda:ws,lambda:model)
    client=TestClient(app)
    assert client.get('/api/automation/months/'+PERIOD).status_code==200
    assert client.post('/api/automation/close-batches/preview',json={'request_id':'x','period':PERIOD}).status_code==403


def test_batch_does_not_accept_ignored_blockers(sample):
    ws,model=sample
    app=FastAPI();install(app,lambda:ws,lambda:model,lambda request:HUMAN)
    client=TestClient(app)
    result=client.post('/api/automation/close-batches/preview',json={'request_id':'x','period':PERIOD,'ignored_blockers':['anything']})
    assert result.status_code==422


def test_advance_durable_delivery_reuses_existing_queue_and_deduplicates(sample):
    ws,model=sample
    first=actions.advance(ws,model,PERIOD,'advance',HUMAN)
    second=actions.advance(ws,model,PERIOD,'advance',HUMAN)
    assert second==first
    registry=Registry(ws.root)
    with registry.connect() as conn:
        assert conn.execute('SELECT count(*) FROM pending').fetchone()[0]==2
        assert [r[0] for r in conn.execute('SELECT files_revision FROM pending')]==[1,1]
    assert ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='queued'").fetchone()[0]==2
    assert actions.deliver(ws)==0


def test_unbound_workspace_cannot_skip_company_scope(sample,tmp_path):
    ws=Workspace(tmp_path/'unbound')
    record(ws,'s1',100)
    with pytest.raises(WorkspaceError,match='营业范围'):
        ws.close_period('s1',PERIOD)


def test_unrelated_month_does_not_invalidate_preview(sample):
    ws,model=sample;p=preview(ws,model)
    ws.record('s1','2030-07',{'can_close':True,'statement':[]},[])
    assert confirm(ws,model,p)['status']=='completed'


@pytest.mark.parametrize('state',['provisional','archive_failed'])
def test_other_store_visible_revenue_is_not_confirmed_until_its_inputs_are_complete(sample,state):
    ws,model=sample
    rid=record(ws,'s2',300,source_sync_pending=state=='provisional')
    if state=='archive_failed':ws.mark_evidence(rid,ready=False,error='disk failure')
    basis=checker.company_basis(ws,model,PERIOD)
    assert basis['missing_store_ids']==['s2'] and not basis['shares']
    with pytest.raises(WorkspaceError):ws.close_period('s1',PERIOD,model=model)


def test_other_month_overhead_configuration_does_not_invalidate_this_month(sample):
    ws,model=sample;p=preview(ws,model)
    changed=model.model_copy(update={'overheads':(*model.overheads,Overhead(period='2030-07',amount=500))})
    assert confirm(ws,changed,p)['status']=='completed'


def test_historical_frozen_sync_flag_is_not_current_income_backlog(sample):
    ws,model=sample
    rid=record(ws,'s1',100,source_sync_pending=True)
    # Existing historical finality is a fixture; current close cannot create it.
    with ws.conn:ws.conn.execute("UPDATE period SET state='closed',run_id=? WHERE store_id='s1' AND period=?",(rid,PERIOD))
    assert checker.company_basis(ws,model,PERIOD)['missing_store_ids']==[]
