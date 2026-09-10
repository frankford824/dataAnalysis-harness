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
@pytest.mark.parametrize('kind',['master','child','orderless','unknown','reship_only','reference'])
def test_refunds_post_by_original_order_without_later_month_fallback(platform,metric_id,kind):
    metric=load_model(MODELS/'cn-ecommerce').metric(metric_id).model_copy(update={'major':None})
    model=Model(id='refund-month',name='refund',stores=(Store(id='s',name='shop',platform=platform),),
        sources=(SourceContract(id='order_detail',name='订单',is_spine=True,owner_role='shop_owner',cadence='monthly'),
                 SourceContract(id='settlement',name='流水',owner_role='shop_owner',cadence='monthly'), SourceContract(id='refund_order_dates',name='原订单日期',owner_role='shop_owner',cadence='monthly')),
        metrics=(metric,),statement=(StatementNode(id='value',name='退款',formula={'op':'add','of':[metric_id]}),))
    master='6927041886243815238';child='6927041886243880774'
    orders=[dict(order_id=master,sub_order_id=child,order_type='补发订单' if kind=='reship_only' else '普通订单',
        store_name='shop',order_time=datetime(2026,5,20),buyer_paid=100.,alloc_ratio=.3)]
    key={'master':master,'child':child,'orderless':'','unknown':'9999999999999999999','reship_only':master,'reference':'5111111111111111111'}[kind]
    rows=[dict(base_order_id=key,store_name='shop',order_time=datetime(2026,6,10),settle_date=datetime(2026,6,10),income=-12.,outgo=0.)]
    items=[item('order_detail',orders,orders[0].keys()),item('settlement',rows,rows[0].keys())]
    if kind=='reference':
        dates=[dict(order_id=key,store_name='shop',order_time=datetime(2026,5,4),internal_order_id='123')]
        items.append(item('refund_order_dates',dates,dates[0].keys()))
    result=run(Ingestion(model=model,items=items),platform)
    assert result.facts['amount'].sum()==-12
    if kind in ('unknown','reship_only'):
        assert result.facts['contribution'].sum()==0
        assert result.spine_facts.is_empty()
    else:
        expected='2026-06' if kind=='orderless' else '2026-05'
        assert result.facts['period'].to_list()==[expected]
        assert result.facts['contribution'].sum()==pytest.approx(-12)
        assert result.spine_facts['amount'].sum()==pytest.approx(-12)


def test_date_references_are_store_scoped_and_conflicts_do_not_supply_dates():
    from ledger.engine.refund_dates import lookup,apply
    dates=[dict(order_id='A',store_name=store,order_time=datetime(2026,month,4),internal_order_id='123') for store,month in [('shop',4),('shop',5),('other',3)]]
    refs=lookup([item('refund_order_dates',dates,dates[0].keys())],{'shop':'shop','other':'other'})
    frame=pl.DataFrame({'__link_key__':['A','A','A'],'store_name':['shop','other','other'],'__spine_period__':[None,None,'2026-06']})
    out=apply(frame,refs,{'shop':'shop','other':'other'})
    assert out['__spine_period__'].to_list()==[None,'2026-03','2026-06']
    assert '123' in out['source_note'][1]


def test_reference_csv_preserves_long_order_id_and_does_not_become_sales(tmp_path):
    from ledger.engine.runtime import ingest
    from ledger.engine.refund_dates import lookup
    m=load_model(MODELS/'cn-ecommerce')
    name=m.store('taobao_mt9s7n9x').name
    path=tmp_path/'退款原订单日期.csv'
    path.write_text('退款原订单号,原订单下单时间,核对店铺名称,核对聚水潭订单号\n2701813838116075077,2026-04-06 12:56:02,'+name+',14484669\n',encoding='utf-8-sig')
    ing=ingest([path],m,default_store=name)
    assert len(ing.items)==1
    assert ing.items[0].template.source=='refund_order_dates'
    refs=lookup(ing.items,{name:name})
    assert (name,'2701813838116075077') in refs, ing.items[0].frame.to_dicts()
    assert refs[name,'2701813838116075077'][0]=='2026-04'


def test_unknown_input_table_cannot_supply_refund_dates():
    from types import SimpleNamespace
    from ledger.engine.refund_dates import lookup
    assert lookup([SimpleNamespace(template=None)],{})=={}
