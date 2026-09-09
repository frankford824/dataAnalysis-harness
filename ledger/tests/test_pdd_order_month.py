from datetime import datetime

import polars as pl
import pytest

from conftest import MODELS
from ledger.engine.runtime import Ingestion, run
from ledger.model.loader import load_model
from ledger.model.schema import Model, SourceContract, StatementNode, Store
from test_reship_period_and_cost_drill import item


def calculate(order_id, happened=6, metric_id='reshipment_cost', outgo=-10.0):
    metric=load_model(MODELS/'cn-ecommerce').metric(metric_id).for_platform('pdd')
    if metric_id=='trade_refund_pdd':
        metric=metric.model_copy(update={'major':None})
    source=metric.source
    model=Model(id='pdd-test',name='pdd',stores=(Store(id='s',name='shop',platform='pdd'),),
        sources=(SourceContract(id='order_detail',name='订单',is_spine=True,owner_role='shop_owner',cadence='monthly'),
                 SourceContract(id=source,name='明细',owner_role='shop_owner',cadence='monthly')),
        metrics=(metric,),statement=(StatementNode(id='value',name='金额',formula={'op':'add','of':[metric.id]}),))
    orders=[dict(order_id=order_id,sub_order_id='REPLACEMENT',order_type='补发订单',store_name='shop',order_time=datetime(2026,happened,10))]
    rows=[dict(order_id=order_id,original_order_id=order_id,base_order_id=order_id,internal_order_id='I',
        sub_order_id='REPLACEMENT',sku='SKU',order_type='补发订单',order_state='Sent',store_name='shop',
        order_time=datetime(2026,happened,10),settle_date=datetime(2026,happened,20),quantity=2.0,unit_cost=5.0,income=0.0,outgo=outgo)]
    return run(Ingestion(model=model,items=[item('order_detail',orders,orders[0].keys()),item(source,rows,rows[0].keys())]),'pdd')


@pytest.mark.parametrize('metric',['reshipment_cost','trade_refund_pdd'])
@pytest.mark.parametrize('oid,happened,period',[('260524-163771850381198',6,'2026-05'),('260601-163771850381198',7,'2026-06')])
def test_order_month_wins_over_later_replacement_or_refund_date(metric,oid,happened,period):
    result=calculate(oid,happened,metric)
    assert result.spine_facts['period'].to_list()==[period]
    assert result.spine_facts['amount'].sum()==-10
    assert result.facts['contribution'].sum()==-10
    assert period in result.facts['source_note'].item()


def test_unknown_original_date_never_falls_back_to_refund_date():
    for key in ['unknown','260232-163771850381198']:
        result=calculate(key,6,'trade_refund_pdd')
        assert result.facts['amount'].sum()==-10
        assert result.spine_facts.is_empty()
        assert result.facts['contribution'].sum()==0


def test_pdd_policy_does_not_change_other_platform_refund_months():
    model=load_model(MODELS/'cn-ecommerce')
    for name in ['trade_receipt_pdd','trade_refund_pdd','software_fee_pdd','trade_compensation_pdd','goods_cost','reshipment_cost','freight_cost']:
        assert model.metric(name).for_platform('pdd').posting_basis=='order_number'
    assert model.metric('trade_refund').posting_basis=='transaction'
    assert model.metric('trade_refund_douyin').posting_basis=='order'


def test_actual_zero_refund_remains_traceable_but_missing_amount_does_not_become_zero():
    result=calculate('260601-610889920124065',6,'trade_refund_pdd',0.0)
    assert result.facts.height==1
    assert result.facts['contribution'].sum()==0
    assert result.facts['counted'].all()
    assert result.facts['period'].to_list()==['2026-06']
    missing=calculate('260601-610889920124065',6,'trade_refund_pdd',None)
    assert missing.facts.is_empty()
