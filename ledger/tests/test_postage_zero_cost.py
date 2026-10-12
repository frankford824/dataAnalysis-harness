"""HQT00365 is a virtual postage charge, never a second goods cost."""
import csv
import io
from datetime import datetime

import polars as pl
import pytest

from ledger.engine.cost_policy import is_freight_surcharge, FREIGHT_SURCHARGE_NOTE
from ledger.fee_display import describe
from ledger.view import fees_csv
from test_cost_exclusivity import calculate


@pytest.mark.parametrize('unit',[None,0.,1.,18.5,-1.,9999.])
@pytest.mark.parametrize('kind',['销售订单','补发订单'])
def test_postage_cost_is_explicit_zero_even_when_price_is_missing(unit,kind):
    r=calculate(sku='HQT00365',kind=kind,unit=unit)
    assert r.pricing_gaps.is_empty()
    assert r.facts.height==1
    assert r.facts['contribution'].sum()==0
    assert r.facts['counted'].all()
    assert FREIGHT_SURCHARGE_NOTE in r.facts['source_note'].item()
    row=next(csv.DictReader(io.StringIO(fees_csv(r.facts,r.model))))
    assert row['核算状态']=='邮费补差价：商品成本为0'
    assert '无需补录' in row['核算说明']


def test_exact_code_only_not_name_prefix_or_identity_conflict():
    frame=pl.DataFrame({'sku':['HQT00365',' hqt00365 ','HQT003650','XHQT00365','HQT00365-A',None],
                        'product_name':['邮费补差价']*6})
    assert frame.select(is_freight_surcharge(frame)).to_series().to_list()==[True,True,False,False,False,False]
    conflicted=frame.with_columns(pl.lit(True).alias('pricing_identity_changed'))
    assert not conflicted.select(is_freight_surcharge(conflicted)).to_series().any()
    assert calculate(sku='HQT00365-A',unit=10.).facts['contribution'].sum()==-10.


@pytest.mark.parametrize('review',[False,True])
@pytest.mark.parametrize('missing',[False,True])
def test_feed_keeps_virtual_postage_as_a_zero_rule_candidate(tmp_path,review,missing):
    from test_order_feed import _fixture
    from ledger.order_feed import OrderFeed
    from ledger.model.schema import Store
    root=tmp_path/'feed';manifest=_fixture(root,second_unnamed=True)
    def table(name):return pl.read_parquet(root/manifest['objects'][name]['path'])
    items=table('order_items.parquet').with_columns(pl.Series('sku_id',['HQT00365','SKU2']),pl.lit(True).alias('is_virtual'))
    costs=table('order_costs.parquet').with_columns(pl.Series('sku_id',['HQT00365','SKU2']))
    if missing:costs=costs.filter(pl.col('sku_id')!='HQT00365')
    before=costs.clone()
    feed=OrderFeed(tmp_path/'ws',feed_root=root)
    frame=feed._cost_frame(table('orders.parquet'),items,costs,table('order_relations.parquet'),
        Store(id='taobao_test',name='shop',platform='taobao'),'fixture',require_history=review)
    assert frame.filter(pl.col('sku')=='HQT00365').height==1
    assert frame.filter(pl.col('sku')=='SKU2')['unit_cost'].item()==1.
    assert costs.equals(before)
    if review:assert not frame.filter(pl.col('sku')=='HQT00365')['pricing_missing'].item()


def test_zero_label_does_not_certify_missing_or_nonzero_postings():
    row={'metric_id':'goods_cost','counted':True,'contribution':0,'source_note':FREIGHT_SURCHARGE_NOTE}
    assert describe(row)['accounting_status']=='邮费补差价：商品成本为0'
    assert describe({**row,'contribution':None})['accounting_status'].startswith('待核对')
    assert describe({**row,'contribution':-10})['accounting_status']=='本行已计入'
    assert describe(row,graded=False)['accounting_status'].startswith('待核对')


def test_mixed_order_keeps_other_goods_cost_and_actual_freight():
    from conftest import MODELS
    from ledger.engine.runtime import Ingestion,run
    from ledger.model.loader import load_model
    from ledger.model.schema import Model,Store,SourceContract,StatementNode
    from test_reship_period_and_cost_drill import item
    full=load_model(MODELS/'cn-ecommerce')
    metrics=tuple(full.metric(n).for_platform('douyin') for n in ['goods_cost','freight_cost'])
    model=Model(id='postage',name='postage',stores=(Store(id='s',name='shop',platform='douyin'),),platforms=full.platforms,
        sources=tuple(SourceContract(id=s,name=s,is_spine=s=='order_detail',owner_role='shop_owner',cadence='monthly') for s in ['order_detail','order_cost','freight']),
        metrics=metrics,statement=(StatementNode(id='value',name='成本',formula={'op':'add','of':['goods_cost','freight_cost']}),))
    orders=[dict(order_id='MAIN',sub_order_id='SUB',product_id='P',store_name='shop',order_type='销售订单',
        order_time=datetime(2026,7,1),tracking_no='T',buyer_paid=10.,refund_amount=0.)]
    costs=[dict(order_id='MAIN',original_order_id='MAIN',sub_order_id='SUB',internal_order_id='I',sku=sku,
        store_name='shop',order_type='销售订单',order_state='Sent',order_time=datetime(2026,7,1),
        quantity=1.,unit_cost=unit,cost_source='history',cost_status='priced',cost_as_of='2026-07-01',
        pricing_evidence='{"order_date":"2026-07-01"}') for sku,unit in [('HQT00365',8.),('OTHER',10.)]]
    freight=[dict(order_id='MAIN',sub_order_id='SUB',store_name='shop',tracking_no='T',order_time=datetime(2026,7,1),ship_date=datetime(2026,7,1),total_cost=8.)]
    ing=Ingestion(model=model,items=[item(s,rows,rows[0].keys()) for s,rows in [('order_detail',orders),('order_cost',costs),('freight',freight)]])
    result=run(ing,'douyin')
    assert result.facts.filter(pl.col('sku')=='HQT00365')['contribution'].sum()==0
    assert result.facts.filter(pl.col('sku')=='OTHER')['contribution'].sum()==-10
    fee=result.facts.filter(pl.col('metric_id')=='freight_cost')
    assert fee['contribution'].sum()==-8, fee.select('link_key','linked','amount','period','source_note').to_dicts()
