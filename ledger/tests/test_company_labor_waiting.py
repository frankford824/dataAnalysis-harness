"""Company labor source waiting is independent of each store's evidence work."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledger import automation_actions as actions, automation_checker as checker
from ledger.model.schema import Store
from ledger.worker_control_api import install
from ledger.workspace import Workspace, WorkspaceError
from conftest import fixture_publication_scope
from test_automation import HUMAN, PERIOD, record, policy
from test_commission import _model
from test_worker_control_api import claim
from test_automation_handoff import observation, submit


@pytest.fixture
def labor_waiting(tmp_path):
    ws=Workspace(tmp_path/'workspace')
    model=_model(stores=tuple(Store(id=f's{i}',name=f'店{i}',platform='taobao') for i in range(1,53)))
    model=model.model_copy(update={'statement':tuple(n.model_copy(update={'headline':'revenue'})
        if n.id=='n_receipt' else n for n in model.statement), 'overheads':()})
    checker.bind_model(ws,model)
    fixture_publication_scope(ws,model,[PERIOD])
    yield ws,model
    ws.close()


def complete_stores(ws,model):
    for store in model.stores: record(ws,store.id,100)


def control(ws,model):
    app=FastAPI();identity={'id':'worker','kind':'service','authenticated':True,'scopes':['business']}
    install(app,lambda:ws,lambda:model,lambda request:identity,clock=lambda:1000)
    return TestClient(app)


def test_missing_company_labor_is_one_source_wait_not_52_decisions(labor_waiting,monkeypatch):
    ws,model=labor_waiting;complete_stores(ws,model)
    monkeypatch.setenv('LEDGER_DETERMINISTIC_AUTOMATION_ENABLED','0')
    before=[tuple(r) for r in ws.conn.execute('SELECT id,result FROM run ORDER BY id')]
    result=actions.month(ws,model,PERIOD)
    assert result['overhead']['total'] is None and result['overhead']['shares']=={}
    assert result['status']=='processing' and result['counts']['needs_decision']==0
    assert result['company_waiting']['evidence_type']=='monthly_labor_confirmation'
    assert result['company_waiting']['role']=='finance' and result['company_waiting']['count']==1
    assert len(result['waiting_groups'])==1 and result['waiting_groups'][0]['count']==1
    assert not result['decision_groups'] and not result['close']['eligible_store_ids']
    assert all(r['stage']=='waiting_company' and r['waiting'] and not r['verification']['allowed'] for r in result['stores'])
    with pytest.raises(WorkspaceError,match='兼职'): ws.close_period('s1',PERIOD,model=model)
    assert before==[tuple(r) for r in ws.conn.execute('SELECT id,result FROM run ORDER BY id')]


def test_missing_labor_does_not_stop_missing_order_or_date_evidence_claim(labor_waiting,monkeypatch):
    ws,model=labor_waiting
    monkeypatch.setenv('LEDGER_DETERMINISTIC_AUTOMATION_ENABLED','0')
    actions.advance(ws,model,PERIOD,'continue-local',HUMAN)
    result=actions.month(ws,model,PERIOD)
    assert result['company_waiting'] and all(r['status']=='processing' and r['stage']=='gathering' for r in result['stores'])
    job=claim(control(ws,model))
    assert job['payload']['automation_case']['run_id'] is None
    assert 'overhead_total_unknown' in job['payload']['automation_case']['reason_codes']
    assert not ws.conn.execute("SELECT 1 FROM period WHERE state='closed'").fetchone()


def test_approved_cost_evidence_continues_with_labor_unknown(labor_waiting,monkeypatch):
    from datetime import datetime,timedelta,timezone
    from ledger.automation_cost_actions import RULE,RULE_SHA
    ws,model=labor_waiting;complete_stores(ws,model)
    record(ws,'s1',100,cost_review={'requires_human':True},pricing_pending_count=1,
        commission={'amount_complete':False})
    stamp=datetime.now(timezone.utc)
    policy(ws,model,'approved-cost','rule_grant',{'rule_id':RULE,'rule_sha':RULE_SHA,'enabled':True,
        'valid_from':(stamp-timedelta(days=1)).isoformat(),'valid_until':(stamp+timedelta(days=1)).isoformat()},store_id='s1')
    monkeypatch.setenv('LEDGER_DETERMINISTIC_AUTOMATION_ENABLED','1')
    actions.sync_month(ws,model,PERIOD)
    result=actions.month(ws,model,PERIOD);row=next(r for r in result['stores'] if r['store_id']=='s1')
    assert row['status']=='processing' and row['stage']=='gathering' and row['waiting'] is None
    assert claim(control(ws,model))['payload']['automation_case']['store_id']=='s1'
    assert not row['verification']['allowed'] and result['overhead']['total'] is None


def test_other_source_wait_remains_separate_and_does_not_become_finance_decision(labor_waiting):
    ws,model=labor_waiting
    actions.advance(ws,model,PERIOD,'get-originals',HUMAN);client=control(ws,model);job=claim(client)
    response=submit(client,job,[observation(ws)])
    assert response.status_code==200,response.text
    result=actions.month(ws,model,PERIOD)
    row=next(r for r in result['stores'] if r['case_id']==job['payload']['automation_case']['id'])
    assert row['status']=='processing' and row['stage']=='waiting_source'
    assert len(result['waiting_groups'])==2 and result['company_waiting']['count']==1
    assert not result['decision_groups'] and result['counts']['needs_decision']==0
    assert not row['verification']['allowed']


def test_valid_candidate_is_one_company_decision_without_publishing_total(labor_waiting):
    ws,model=labor_waiting;complete_stores(ws,model)
    preview=actions.decision_preview(ws,model,request_id='candidate-preview',period=PERIOD,store_id='',
        kind='overhead_total',values={'amount':'12.00'},reason='本期已收到核定资料，集中预览',actor=HUMAN)
    result=actions.month(ws,model,PERIOD)
    assert result['company_waiting'] is None and result['status']=='needs_decision'
    assert result['counts']['needs_decision']==0 and result['counts']['company_source_decisions']==1
    assert len(result['decision_groups'])==1 and result['decision_groups'][0]['kind']=='overhead_total'
    assert result['decision_groups'][0]['count']==1 and result['decision_groups'][0]['proposal_id']==preview['proposal_id']
    assert result['overhead']['total'] is None and not result['close']['can_preview']
    actions.decision_apply(ws,model,request_id='candidate-approval',proposal_id=preview['proposal_id'],
        proposal_sha=preview['proposal_sha'],input_sha=preview['input_sha'],actor=HUMAN)
    after=actions.month(ws,model,PERIOD)
    assert after['company_waiting'] is None and after['overhead']['total']=='12.00'
    assert len(after['close']['eligible_store_ids'])==52
    assert after['counts']['company_source_decisions']==0


@pytest.mark.parametrize('invalid',['expired','changed_input','changed_sha'])
def test_stale_or_unverified_candidate_stays_source_wait(labor_waiting,invalid):
    ws,model=labor_waiting;complete_stores(ws,model)
    preview=actions.decision_preview(ws,model,request_id='draft',period=PERIOD,store_id='',kind='overhead_total',
        values={'amount':'12.00'},reason='候选金额需要按当前输入批准',actor=HUMAN)
    if invalid=='expired':ws.conn.execute('UPDATE automation_proposal SET expires_at=0')
    elif invalid=='changed_input':record(ws,'s1',101)
    else:ws.conn.execute("UPDATE automation_proposal SET proposal_sha=?",('f'*64,))
    result=actions.month(ws,model,PERIOD)
    assert result['company_waiting'] and not result['decision_groups']
    assert result['overhead']['total'] is None


def test_real_local_business_decision_is_not_hidden_by_company_wait(labor_waiting,monkeypatch):
    ws,model=labor_waiting;complete_stores(ws,model)
    monkeypatch.setenv('LEDGER_DETERMINISTIC_AUTOMATION_ENABLED','0')
    record(ws,'s1',100,commission={'amount_complete':False})
    result=actions.month(ws,model,PERIOD);row=next(r for r in result['stores'] if r['store_id']=='s1')
    assert row['status']=='needs_decision' and row['stage']=='business_decision'
    assert result['company_waiting'] and len(result['decision_groups'])==1
    assert result['decision_groups'][0]['kind']=='business_decision' and result['decision_groups'][0]['store_ids']==['s1']


def test_company_wait_does_not_authorize_outside_publication_scope(labor_waiting):
    from ledger import publication_scope
    ws,model=labor_waiting
    approved=publication_scope.PublicationScope.for_isolated(ws.root,[('s1',PERIOD)],approval_reference='only-s1-approved')
    publication_scope.install_isolated(approved,replace=True)
    with publication_scope.bind(approved,ws.root):actions.sync_month(ws,model,PERIOD)
    rows=ws.conn.execute('SELECT store_id FROM automation_case').fetchall()
    assert rows and {r[0] for r in rows}=={'s1'}
    result=actions.month(ws,model,PERIOD)
    assert result['company_waiting'] and result['overhead']['total'] is None
    assert not result['close']['eligible_store_ids']
