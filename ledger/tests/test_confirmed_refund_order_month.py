"""Confirmed refund policy: original sale month, with traceable exceptions."""
from datetime import datetime
import polars as pl
import pytest
from conftest import MODELS
from ledger.engine.runtime import Ingestion, run
from ledger.model.loader import load_model
from ledger.model.schema import Model, SourceContract, StatementNode, Store
from test_reship_period_and_cost_drill import item

@pytest.mark.parametrize('platform,metric_id',[('taobao','trade_refund'),('douyin','trade_refund_douyin')])
@pytest.mark.parametrize('kind',['master','child','orderless','unknown','reship_only'])
def test_refunds_post_by_original_order_without_later_month_fallback(platform,metric_id,kind):
    metric=load_model(MODELS/'cn-ecommerce').metric(metric_id).model_copy(update={'major':None})
    model=Model(id='refund-month',name='refund',stores=(Store(id='s',name='shop',platform=platform),),
        sources=(SourceContract(id='order_detail',name='订单',is_spine=True,owner_role='shop_owner',cadence='monthly'),
                 SourceContract(id='settlement',name='流水',owner_role='shop_owner',cadence='monthly')),
        metrics=(metric,),statement=(StatementNode(id='value',name='退款',formula={'op':'add','of':[metric_id]}),))
    master='6927041886243815238';child='6927041886243880774'
    orders=[dict(order_id=master,sub_order_id=child,order_type='补发订单' if kind=='reship_only' else '普通订单',
        store_name='shop',order_time=datetime(2026,5,20),buyer_paid=100.,alloc_ratio=.3)]
    key={'master':master,'child':child,'orderless':'','unknown':'9999999999999999999','reship_only':master}[kind]
    rows=[dict(base_order_id=key,store_name='shop',order_time=datetime(2026,6,10),settle_date=datetime(2026,6,10),income=-12.,outgo=0.)]
    result=run(Ingestion(model=model,items=[item('order_detail',orders,orders[0].keys()),item('settlement',rows,rows[0].keys())]),platform)
    assert result.facts['amount'].sum()==-12
    if kind in ('unknown','reship_only'):
        assert result.facts['contribution'].sum()==0
        assert result.spine_facts.is_empty()
    else:
        expected='2026-06' if kind=='orderless' else '2026-05'
        assert result.facts['period'].to_list()==[expected]
        assert result.facts['contribution'].sum()==pytest.approx(-12)
        assert result.spine_facts['amount'].sum()==pytest.approx(-12)
