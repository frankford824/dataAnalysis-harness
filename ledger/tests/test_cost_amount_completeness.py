"""A quality percentage never supplies the price of an uncovered required item."""
from copy import deepcopy
from datetime import datetime
import json

import polars as pl
import pytest

from ledger import replay_service,automation_checker,automation_actions,manual_cost
from ledger.engine.runtime import Ingestion,run
from ledger.model.loader import load_model
from ledger.model.schema import Check,Model,Platform,SourceContract,StatementNode,Store
from ledger.view import slice_dict
from ledger.workspace import WorkspaceError
from conftest import MODELS
from test_reship_period_and_cost_drill import item
from test_replay_service import bundle,seed,all_expected,fixture_session


def one_gap(*,missing_row=False,zero_sku=False):
    full=load_model(MODELS/'cn-ecommerce');metric=full.metric('goods_cost').for_platform('taobao').model_copy(update={'by_platform':()})
    model=Model(id='economic-completeness-fixture',name='synthetic cost completeness',
        platforms=(Platform(id='taobao',name='TB',cost_pricing='historical'),),
        stores=(Store(id='s',name='shop',platform='taobao'),),
        sources=(SourceContract(id='order_detail',name='orders',is_spine=True,owner_role='shop_owner',cadence='monthly'),SourceContract(id='order_cost',name='costs',owner_role='warehouse',cadence='monthly')),
        metrics=(metric,),statement=(StatementNode(id='profit',name='profit',commission_base=True,formula={'op':'add','of':['goods_cost']}),),
        checks=(Check(id='coverage',name='coverage',kind='spine_coverage',metric='goods_cost',threshold=.95,blocking=True),))
    orders=[dict(order_id=f'O{i}',sub_order_id=f'S{i}',store_name='shop',order_time=datetime(2026,6,5),order_type='销售订单',order_state='Sent',tracking_no=f'W{i}') for i in range(20)]
    costs=[{**o,'original_order_id':o['order_id'],'internal_order_id':f'I{i}','sku':'HQT01773' if zero_sku and i==19 else f'SKU{i}',
        'quantity':1.,'unit_cost':5. if i<19 else None,'cost_source':'history' if i<19 else None,'cost_status':'priced' if i<19 else 'missing_price','cost_as_of':'2026-06-05','pricing_evidence':json.dumps({'order_date':'2026-06-05'})} for i,o in enumerate(orders)]
    if missing_row:costs=costs[:-1]
    result=run(Ingestion(model=model,items=[item('order_detail',orders,orders[0]),item('order_cost',costs,costs[0])]),'taobao')
    return model,result.slice('shop','2026-06')


@pytest.mark.parametrize('missing_row',[False,True])
def test_required_target_missing_with_or_without_a_price_gap_keeps_known_facts(missing_row):
    model,sl=one_gap(missing_row=missing_row)
    assert sl.cost_coverage['passed'] is True and sl.cost_coverage['threshold']==.95
    assert sl.facts.filter(pl.col('metric_id')=='goods_cost')['contribution'].sum()==-95
    assert sl.nodes['profit'].value is None and not sl.nodes['profit'].available and not sl.can_close
    review=slice_dict(sl,model.store('s'),model)['cost_review']
    assert review['amount_complete']=={'goods':False,'dropship':True,'reshipment':True}
    assert review['known_partial']['goods']==95 and review['observed']['goods']==95
    assert review['unresolved_targets_count']['goods']==1
    assert review['requires_human'] is True


def test_confirmed_no_shipping_zero_cost_is_complete_without_inventing_a_price():
    model,sl=one_gap(zero_sku=True)
    assert sl.pricing_gaps.is_empty()
    assert sl.nodes['profit'].value==-95 and sl.nodes['profit'].available
    assert slice_dict(sl,model.store('s'),model)['cost_review']['amount_complete']['goods'] is True


def test_actual_service_95_percent_required_gap_cannot_preview_or_close(bundle):
    path,_missing=bundle
    with fixture_session(path,seed()) as session:
        session.recompute('s1');session.recompute('s2');session.drain_pending()
        ws,model=session.ws,session.model;state=ws.state('s1','2026-06');body=state.result
        assert body['cost_coverage']['passed'] is True and body['cost_coverage']['coverage']==.95
        assert body['pricing_pending_count']==1 and body['cost_review']['observed']['goods']==38
        assert body['cost_review']['amount_complete']['goods'] is False
        assert body['commission']['amount_complete'] is False
        assert all(p['amount_complete'] is False for p in body['commission']['people'])
        assert next(r for r in body['statement'] if r['id']=='gross_profit')['value'] is None
        checked=automation_checker.check(ws,'s1','2026-06',model=model,verify_artifacts=True)
        assert not checked['allowed'] and 'required_cost_unresolved' in {r['code'] for r in checked['issues']}
        actor=replay_service._offline_actor(ws,session.registry)
        preview=automation_actions.close_preview(ws,model,request_id='unknown-cost-preview',period='2026-06',store_ids=['s1'],actor=actor)
        assert not preview['eligible'] and len(preview['blocked'])==1
        with pytest.raises(WorkspaceError):ws.close_period('s1','2026-06',expected_run_id=state.run_id,model=model)
        assert not ws.state('s1','2026-06').closed


def test_exact_posted_supplement_restores_full_amount_once_and_preserves_source_base(bundle):
    path,missing=bundle
    with fixture_session(path,seed()) as session:
        session.recompute('s1');session.recompute('s2');session.drain_pending()
        session.supplement('s1','2026-06',amount='2.00',reason='explicit synthetic invoice amount',order_id=missing['s1/2026-06'])
        session.drain_pending()
        first=session.ws.state('s1','2026-06');body=first.result;review=body['cost_review']
        assert body['cost_line_posting']['total']==2 and body['cost_line_posting']['count']==1
        assert review['observed']['goods']==38 and review['known_booked']['goods']==40
        assert review['amount_complete']['goods'] is True and review['requires_human'] is False
        assert next(r for r in body['statement'] if r['id']=='net_profit')['value']==160
        assert 'required_cost_unresolved' not in {r['code'] for r in automation_checker.check(session.ws,'s1','2026-06',model=session.model)['issues']}
        session.recompute('s1')
        after=session.ws.state('s1','2026-06').result
        assert after['cost_line_posting']['total']==2 and after['cost_review']['known_booked']['goods']==40
        assert next(r for r in after['statement'] if r['id']=='net_profit')['value']==160


def test_approved_full_cost_total_is_independent_authority_and_payout_only_is_not(bundle):
    path,_missing=bundle
    with fixture_session(path,seed()) as session:
        session.recompute('s1');session.recompute('s2');session.drain_pending()
        ws,model=session.ws,session.model;state=ws.state('s1','2026-06');body=state.result
        payouts=[{'person_id':'p1','amount':'8.00'}]
        paid,decision=manual_cost.payout_only(body,state.run_id,payouts,False,'independent synthetic payout confirmation',labor_cut='0.00')
        assert paid['commission']['people'][0]['amount_complete'] is True
        assert paid['cost_review']['amount_complete']['goods'] is False
        assert not automation_checker.check(ws,'s1','2026-06',model=model,manual_result=paid)['allowed']
        with pytest.raises(WorkspaceError):ws.close_period('s1','2026-06',expected_run_id=state.run_id,note='payout only',manual_result=paid,manual_decision=decision,model=model)
        confirmed,decision=manual_cost.certified(model,body,state.run_id,{'goods':'40.00','dropship':'0.00','reshipment':'0.00'},payouts,False,'independent audited synthetic monthly cost',labor_cut='0.00')
        assert confirmed['cost_review']['amount_complete']=={'goods':True,'dropship':True,'reshipment':True}
        assert confirmed['cost_review']['authority']=='approved_manual_total'
        assert confirmed['cost_review']['observed']['goods']==38
        assert next(r for r in confirmed['statement'] if r['id']=='net_profit')['value']==160
        checked=automation_checker.check(ws,'s1','2026-06',model=model,manual_result=confirmed)
        assert checked['allowed'],checked['issues']
        closed=ws.close_period('s1','2026-06',expected_run_id=state.run_id,note='approved synthetic full total',manual_result=confirmed,manual_decision={**decision,'line_revision':0},labor_cut='0.00',model=model)
        assert closed.closed


def test_income_based_person_amount_can_be_known_while_required_cost_still_blocks_close(bundle):
    path,_missing=bundle
    fixed=seed();fixed['policies'][0]['body']['base_node']='n_receipt'
    with fixture_session(path,fixed) as session:
        session.recompute('s1');session.recompute('s2');session.drain_pending()
        body=session.ws.state('s1','2026-06').result
        assert body['cost_review']['amount_complete']['goods'] is False
        assert body['commission']['amount_complete'] is True
        assert body['commission']['total']==10
        assert all(p['amount_complete'] is True for p in body['commission']['people'])
        assert next(n for n in body['statement'] if n['id']=='net_profit')['value'] is None
        assert not automation_checker.check(session.ws,'s1','2026-06',model=session.model)['allowed']
