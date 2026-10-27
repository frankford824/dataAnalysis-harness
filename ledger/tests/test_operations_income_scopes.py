"""Real 1688 engine evidence closes management grains without changing a commission."""
from copy import deepcopy
from decimal import Decimal
import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ledger import commission_engine, operations_api, operations_readmodel as operations, service
from ledger.automation_period_proof import prepare, save
from ledger.commission_registry import Registry
from test_operations_income import make_income_workspace


def owned_case(tmp_path, *, unknown_subject=False):
    receipts = [('A','订单收入调整' if unknown_subject else '订单收入',100),
        ('A','订单售后退款',-20),('B','订单收入',100)]
    case = make_income_workspace(tmp_path,receipts=receipts)
    ws,model,result = case['ws'],case['model'],case['result']
    registry=Registry(ws.root)
    for person in ({'id':'team','name':'合成团队'}, {'id':'a','name':'做货甲','parent_id':'team'}, {'id':'b','name':'做货乙','parent_id':'team'}):
        registry.person_save(person,'fixture','真实引擎经营归属测试输入')
    for pid,product,managed in [('a','P1',True),('b','P2',False)]:
        registry.save_store_member('s1',pid,'produce','','fixture','已确认做货归属',valid_from='2026-07-01')
        segment={'valid_from':'2026-07-01','valid_to':'','mode':'distribute','total_rate':'.05',
            'managed':managed,'managed_team_id':'team' if managed else '',
            'allocations':[{'person_id':pid,'rate':'.05','role':'运营','duty':'produce'}]}
        registry.save_scheme('s1',product,{'segments':[segment]},'fixture','合成原件已确认关系',publish=True)
    registry.save_policy({'base_node':'n_receipt','wages':'skip_preview'},'2026-07-01','fixture','本例固定既有提成政策')
    allocation=commission_engine.calculate(result,model,'s1','2026-07',registry,allow_partial_pricing=True)
    assert allocation is not None
    payload=deepcopy(case['payload']);payload['commission']=allocation[0]
    store=model.store('s1');sl=result.slice(store.name,'2026-07')
    proof=prepare(ws,model,store,case['ingestion'],sl,payload,allocation)
    run=ws.record('s1','2026-07',payload,[item.ref.sha256 for item in case['ingestion'].items],
        model_revision=hashlib.sha256(model.model_dump_json().encode()).hexdigest())
    commission_engine.persist(registry,run,allocation[1],allocation[2]);service._keep_facts(ws,run,sl);save(ws,run,proof)
    case.update(run_id=run,payload=payload,registry=registry)
    return case


def test_1688_store_product_person_and_managed_scopes_use_proven_gross_for_labor(tmp_path):
    case=owned_case(tmp_path);ws,model=case['ws'],case['model']
    try:
        frozen=deepcopy(ws.state('s1','2026-07').result)
        reports={level:operations.read_month(ws,model,'2026-07',level=level) for level in ('store','product','person','team')}
        for level,report in reports.items():
            assert report['summary']['receipts']=='200.00'
            assert report['summary']['refunds']=='20.00'
            assert report['summary']['net_income']=='180.00'
            assert report['summary']['refund_rate']=='0.10000000'
            assert report['summary']['labor_cost']=='10.00'
            assert sum(Decimal(row['metrics']['net_income']) for row in report['rows'])==180
            assert report['input_sha']==reports['store']['input_sha']
        people={row['person_id']:row for row in reports['person']['rows']}
        assert people['a']['metrics']['net_income']=='80.00'
        assert people['b']['metrics']['net_income']=='100.00'
        assert people['a']['metrics']['receipts']==people['b']['metrics']['receipts']=='100.00'
        assert people['a']['metrics']['labor_cost']==people['b']['metrics']['labor_cost']=='5.00'
        managed=operations.read_month(ws,model,'2026-07',level='person',managed='managed')
        standard=operations.read_month(ws,model,'2026-07',level='person',managed='standard')
        assert managed['summary']['net_income']=='80.00' and standard['summary']['net_income']=='100.00'
        assert managed['summary']['labor_cost']==standard['summary']['labor_cost']=='5.00'
        proof=reports['store']['income_evidence']['s1']
        assert proof['status']=='split_verified'
        assert proof['net_income']=='180.00' and proof['receipts']=='200.00' and proof['refunds']=='20.00'
        source=proof['proof']['net_source']
        assert source['result_sha']==operations.digest({key:source[key] for key in ('store_id','period','run_id','platform','statement_receipt')})
        assert 'by_spine' not in proof and 'detail_proof' not in proof
        app=FastAPI();operations_api.install(app,lambda:ws,lambda:model)
        with TestClient(app) as client:
            exported=client.get('/api/operations/export.csv',params={'period':'2026-07','level':'person','expected_input_sha':reports['store']['input_sha']})
            assert exported.status_code==200,exported.text
        assert ws.state('s1','2026-07').result==frozen
    finally:ws.close()


@pytest.mark.parametrize('mode',['missing_source','unknown_subject'])
def test_1688_unknown_gross_keeps_proven_net_and_saved_store_labor_without_fabricating_person_weights(tmp_path,mode):
    case=owned_case(tmp_path,unknown_subject=mode=='unknown_subject');ws,model=case['ws'],case['model']
    try:
        original=deepcopy(ws.state('s1','2026-07').result)
        if mode=='missing_source':ws.facts_path(case['run_id']).unlink()
        for level in ('store','product','person','team'):
            report=operations.read_month(ws,model,'2026-07',level=level)
            assert report['summary']['net_income']=='180.00'
            assert report['summary']['receipts'] is None and report['summary']['refunds'] is None
            assert report['summary']['refund_rate'] is None
            assert sum(Decimal(row['metrics']['net_income']) for row in report['rows'])==180
            assert report['income_evidence']['s1']['status']=='net_only'
            if level=='store':assert report['summary']['labor_cost']=='10.00'
            else:
                assert report['summary']['labor_cost'] is None
                assert all(row['metrics']['labor_cost'] is None for row in report['rows'])
            assert report['is_final'] is False
        people=operations.read_month(ws,model,'2026-07',level='person')
        assert {row['person_id']:row['metrics']['net_income'] for row in people['rows']}=={'a':'80.00','b':'100.00'}
        assert ws.state('s1','2026-07').result==original
    finally:ws.close()
