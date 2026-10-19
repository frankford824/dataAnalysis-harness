from datetime import datetime

import polars as pl
import pytest

from conftest import MODELS
from ledger.engine.runtime import Ingestion, run
from ledger.model.loader import load_model
from ledger.model.schema import Check, Model, SourceContract, StatementNode, Store
from ledger.order_lifecycle import SUPERSEDED_COST_NOTE
from test_reship_period_and_cost_drill import item


def evaluate(costs, *, platform='taobao', reship=False):
    original=load_model(MODELS/'cn-ecommerce')
    mid='reshipment_cost' if reship else 'goods_cost'
    model=Model(id='test',name='test',stores=(Store(id='s',name='shop',platform=platform),),
        sources=(SourceContract(id='order_detail',name='订单',is_spine=True,owner_role='shop_owner',cadence='monthly'),
                 SourceContract(id='order_cost',name='成本',owner_role='shop_owner',cadence='monthly')),
        metrics=(original.metric(mid),),
        checks=(Check(id='coverage',name='成本覆盖',kind='spine_coverage',metric=mid,threshold=.95),),
        statement=(StatementNode(id='cost',name='成本',formula={'op':'add','of':[mid]}),))
    orders=[dict(order_id='parent',sub_order_id='child',order_type='销售订单',order_state='Sent',
        tracking_no='W',refund_status='没有申请退款',store_name='shop',order_time=datetime(2026,6,1))]
    rows=[dict(order_id='parent',original_order_id='parent',sub_order_id='child',sku='SAME-SKU',
        order_type='补发订单' if reship else '销售订单',store_name='shop',order_time=datetime(2026,6,1),**row) for row in costs]
    source=item('order_cost',rows,rows[0].keys())
    return run(Ingestion(model=model,items=[item('order_detail',orders,orders[0].keys()),source]),platform),source


@pytest.mark.parametrize('platform',['taobao','pdd','douyin','jd','alibaba1688'])
@pytest.mark.parametrize('state',['Split','被拆分','Merged','被合并'])
@pytest.mark.parametrize('reship',[False,True])
def test_superseded_original_is_trace_only_and_live_split_shipments_keep_quantities(platform,state,reship):
    result,source=evaluate([
        dict(internal_order_id='old',order_state=state,quantity=3.0,unit_cost=2.0),
        dict(internal_order_id='sent-A',order_state='Sent',is_split=True,quantity=2.0,unit_cost=2.0),
        dict(internal_order_id='sent-B',order_state='Sent',is_split=True,quantity=1.0,unit_cost=2.0)],
        platform=platform,reship=reship)
    assert not result.eval_errors
    assert source.frame['quantity'].to_list()==[3,2,1]
    assert result.facts.filter(pl.col('internal_order_id')=='old')['contribution'].sum()==0
    assert SUPERSEDED_COST_NOTE in result.facts.filter(pl.col('internal_order_id')=='old')['source_note'].item()
    assert result.facts.filter(pl.col('internal_order_id')=='sent-A')['contribution'].sum()==pytest.approx(-4)
    assert result.facts.filter(pl.col('internal_order_id')=='sent-B')['contribution'].sum()==pytest.approx(-2)
    assert result.spine_facts['amount'].sum()==pytest.approx(-6)


def test_trace_only_parent_cannot_cover_missing_real_shipment_cost():
    result,_=evaluate([dict(internal_order_id='old',order_state='Split',quantity=1.0,unit_cost=7.0)])
    scope=result.slices[('shop','2026-06')]
    assert scope.cost_coverage['expected']==1
    assert scope.cost_coverage['covered']==0
    assert scope.cost_coverage['uncovered']==1
    assert scope.cost_coverage['passed'] is False


def test_two_real_rows_of_the_same_sku_are_not_deduplicated():
    result,_=evaluate([
        dict(internal_order_id='real',order_state='Sent',quantity=1.0,unit_cost=.7676,is_gift=True,is_merge=True),
        dict(internal_order_id='real',order_state='Sent',quantity=1.0,unit_cost=.7676,is_gift=False,is_merge=True)])
    assert result.facts.height==2
    assert result.facts['contribution'].sum()==pytest.approx(-1.5352)


def test_display_and_export_explain_trace_only_parent_without_relabeling_old_booked_money():
    import csv,io
    from decimal import Decimal
    from ledger.fee_display import describe,BOOKED_AMOUNT
    from ledger.view import fees_csv
    result,_=evaluate([
        dict(internal_order_id='old',order_state='Split',quantity=1.0,unit_cost=2.0),
        dict(internal_order_id='live',order_state='Sent',quantity=1.0,unit_cost=2.0)])
    original=result.facts.filter(pl.col('internal_order_id')=='old').to_dicts()[0]
    assert describe(original)['accounting_status']=='拆分/合并原单不计成本'
    assert describe({**original,'contribution':-2})['accounting_status']=='本行已计入'
    rows=list(csv.DictReader(io.StringIO(fees_csv(result.facts,load_model(MODELS/'cn-ecommerce')).lstrip('\ufeff'))))
    assert len(rows)==2
    assert any(row['核算状态']=='拆分/合并原单不计成本' and Decimal(row[BOOKED_AMOUNT])==0 for row in rows)
    assert sum(Decimal(row[BOOKED_AMOUNT]) for row in rows)==-2
