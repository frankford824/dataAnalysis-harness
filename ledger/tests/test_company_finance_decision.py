"""Frozen company basis needs a finance decision, never another source retry."""
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ledger import automation_actions as actions,automation_checker as checker,automation_store as journal
from ledger.worker_control_api import install as install_worker
from ledger.workspace import WorkspaceError
from ledger.model.schema import Store
from test_automation import sample,record,PERIOD


def financial_snapshot(ws):
    return journal.digest({
        'closed':[dict(r) for r in ws.conn.execute("SELECT p.store_id,p.period,p.run_id,r.result FROM period p JOIN run r ON r.id=p.run_id WHERE p.state='closed'")],
        'labor':[dict(r) for r in ws.conn.execute('SELECT * FROM run_labor')],
        'company':[dict(r) for r in ws.conn.execute('SELECT * FROM automation_overhead_close')]})


def company_conflict(sample):
    ws,model=sample
    ws.close_period('s1',PERIOD,model=model)
    record(ws,'s2',600)
    return ws,model


def test_known_frozen_change_is_one_finance_card_and_cannot_be_claimed(sample):
    ws,model=company_conflict(sample);before=financial_snapshot(ws)
    value=actions.month(ws,model,PERIOD)
    assert value['status']=='needs_decision' and value['counts']['company_finance_decisions']==1
    groups=[g for g in value['decision_groups'] if g['kind']=='company_finance']
    assert len(groups)==1 and groups[0]['role_label']=='公司财务负责人'
    assert groups[0]['assignee_status']=='role_unconfigured' and groups[0]['actions']==[]
    assert groups[0]['frozen']['total']==groups[0]['current']['total']
    assert groups[0]['total_evidence_status']=='same'
    assert groups[0]['changes']==[{'store_id':'s2','frozen_revenue':'300','current_revenue':'600','kind':'revenue_changed'}]
    rows={r['store_id']:r for r in value['stores']}
    assert rows['s1']['status']=='completed' and rows['s1']['stage']=='closed'
    assert rows['s2']['stage']=='company_finance' and rows['s2']['actions']==[] and rows['s2']['waiting'] is None
    assert value['company_finance_only'] and not value['can_advance']
    assert value['advance_block_reason']=='company_finance_decision'
    actions.sync_month(ws,model,PERIOD)
    # Simulate an earlier queued source case. The actual claim rechecks it and
    # must transition it to the finance responsibility without executing it.
    with ws.transaction() as db:db.execute("UPDATE automation_case SET status='processing',stage='gathering' WHERE store_id='s2'")
    actor={'id':'fixture-service','kind':'service','authenticated':True,'scopes':['business'],'capabilities':['agent_control']}
    app=FastAPI();install_worker(app,lambda:ws,lambda:model,actor=lambda request:actor)
    response=TestClient(app).post('/api/v1/agent-jobs/claim',json={'agent_id':actor['id'],'capabilities':['recompute']})
    assert response.status_code==204
    assert financial_snapshot(ws)==before


def test_company_finance_card_does_not_block_genuine_other_store_evidence(sample):
    ws,model=company_conflict(sample)
    mixed=model.model_copy(update={'stores':(*model.stores,Store(id='s3',name='丙',platform='taobao'))})
    checker.bind_model(ws,mixed)
    value=actions.month(ws,mixed,PERIOD)
    rows={r['store_id']:r for r in value['stores']}
    assert any(g['kind']=='company_finance' for g in value['decision_groups'])
    assert rows['s2']['stage']=='company_finance' and rows['s2']['actions']==[]
    assert rows['s3']['stage']!='company_finance'
    assert any(a['kind']=='advance' for a in rows['s3']['actions'])
    assert value['can_advance'] and not value['company_finance_only']
    assert value['advance_block_reason'] is None


def test_equal_numeric_revenue_keeps_original_company_identity(sample):
    ws,model=sample;ws.close_period('s1',PERIOD,model=model)
    before=checker.company_basis(ws,model,PERIOD);frozen=financial_snapshot(ws)
    record(ws,'s2',300.0)
    after=checker.company_basis(ws,model,PERIOD)
    assert after['input_sha']==before['input_sha'] and after['finance_decision'] is None
    assert after['shares']==before['shares'] and financial_snapshot(ws)==frozen


def test_preexisting_nonzero_legacy_shares_are_not_assumed_a_uniform_baseline(sample):
    ws,model=sample
    run=ws.latest_run('s1',PERIOD)
    # Explicit preexisting legacy database fixture, not a current close bypass.
    with ws.transaction() as db:
        db.execute("UPDATE period SET state='closed',run_id=? WHERE store_id='s1' AND period=?",(run['id'],PERIOD))
        db.execute('INSERT INTO run_labor(run_id,store_id,period,amount,at,by) VALUES(?,?,?,?,?,?)',
            (run['id'],'s1',PERIOD,'25.00','legacy-source','legacy-source'))
    before=financial_snapshot(ws);value=actions.month(ws,model,PERIOD)
    group=next(g for g in value['decision_groups'] if g['kind']=='company_finance')
    assert group['reason_code']=='overhead_legacy_frozen_basis'
    assert group['frozen'] is None and group['changes']==[]
    assert group['comparison_status']=='legacy_basis_unverified'
    assert len(group['closed_allocations'])==1
    assert {k:group['closed_allocations'][0][k] for k in ('store_id','run_id','amount')}=={'store_id':'s1','run_id':run['id'],'amount':'25.00'}
    assert value['status']=='needs_decision'
    with pytest.raises(WorkspaceError):ws.close_period('s2',PERIOD,model=model)
    assert financial_snapshot(ws)==before
