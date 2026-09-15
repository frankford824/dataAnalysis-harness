from datetime import datetime
import json
import pytest
from conftest import MODELS
from ledger.engine.runtime import Ingestion,run
from ledger.view import slice_dict
from ledger.model.loader import load_model
from ledger.model.schema import Check,Model,Platform,Store,SourceContract,StatementNode
from test_reship_period_and_cost_drill import item


@pytest.mark.parametrize('platform',['taobao','douyin'])
@pytest.mark.parametrize('source,day,evidence_day,available',[
    ('history','2026-06-05','2026-06-05',True),
    ('register','2026-06-05','2026-06-05',True),
    ('register_first','2026-06-05','2026-06-05',True),
    ('register_first','2026-06-05','2026-07-02',False),
    ('register_first','2026-07-02','2026-06-05',False),
    ('history','2026-07-02','2026-06-05',False),
    ('mirror','2026-06-05','2026-06-05',False),
])
def test_original_platform_day_controls_cost_evidence(platform,source,day,evidence_day,available):
    full=load_model(MODELS/'cn-ecommerce')
    assert next(p for p in full.platforms if p.id==platform).cost_pricing=='historical'
    metric=full.metric('goods_cost').for_platform(platform).model_copy(update={'by_platform':()})
    model=Model(id='history-test',name='history',platforms=(Platform(id=platform,name=platform,cost_pricing='historical'),),
        stores=(Store(id='s',name='shop',platform=platform),),
        sources=(SourceContract(id='order_detail',name='订单',is_spine=True,owner_role='shop_owner',cadence='monthly'),
                 SourceContract(id='order_cost',name='成本',owner_role='shop_owner',cadence='monthly')),
        metrics=(metric,),statement=(StatementNode(id='profit',name='利润',formula={'op':'add','of':['goods_cost']}),))
    order=dict(order_id='3306862164152025489',sub_order_id='CHILD',store_name='shop',order_time=datetime(2026,6,5),order_type='销售订单')
    cost={**order,'original_order_id':order['order_id'],'internal_order_id':'I','sku':'SKU','quantity':2.,'unit_cost':5.,
          'order_time':datetime(2026,7,2),'order_state':'Sent','cost_source':source,'cost_status':'priced','cost_as_of':day,
          'pricing_evidence':json.dumps({'order_date':evidence_day})}
    result=run(Ingestion(model=model,items=[item('order_detail',[order],order),item('order_cost',[cost],cost)]),platform)
    sl=result.slices[('shop','2026-06')]
    assert sl.nodes['profit'].value==(-10 if available else None)
    assert result.pricing_gaps.height==(0 if available else 1)
    if not available:assert result.pricing_gaps['order_date'].item()=='2026-06-05'


def test_small_uncovered_cost_share_is_visible_without_blocking_profit_or_close():
    full=load_model(MODELS/'cn-ecommerce')
    metric=full.metric('goods_cost').for_platform('taobao').model_copy(update={'by_platform':()})
    model=Model(id='coverage-test',name='coverage',platforms=(Platform(id='taobao',name='淘宝',cost_pricing='historical'),),
        stores=(Store(id='s',name='shop',platform='taobao'),),
        sources=(SourceContract(id='order_detail',name='订单',is_spine=True,owner_role='shop_owner',cadence='monthly'),
                 SourceContract(id='order_cost',name='成本',owner_role='shop_owner',cadence='monthly')),
        metrics=(metric,),statement=(StatementNode(id='profit',name='利润',commission_base=True,formula={'op':'add','of':['goods_cost']}),),
        checks=(Check(id='chk_goods_coverage',name='商品成本覆盖率',kind='spine_coverage',metric='goods_cost',threshold=.95,blocking=True),))
    orders=[dict(order_id=f'O{i}',sub_order_id=f'S{i}',store_name='shop',order_time=datetime(2026,6,5),
                 order_type='销售订单',order_state='Sent') for i in range(20)]
    costs=[{**order,'original_order_id':order['order_id'],'internal_order_id':f'I{i}','sku':f'SKU{i}',
            'quantity':1.,'unit_cost':5. if i<19 else None,'cost_source':'history' if i<19 else None,
            'cost_status':'priced' if i<19 else 'missing_price','cost_as_of':'2026-06-05',
            'pricing_evidence':json.dumps({'order_date':'2026-06-05'})} for i,order in enumerate(orders)]
    result=run(Ingestion(model=model,items=[item('order_detail',orders,orders[0]),item('order_cost',costs,costs[0])]),'taobao')
    sl=result.slices[('shop','2026-06')]
    assert sl.pricing_gaps.height==1
    assert sl.cost_coverage=={'coverage':.95,'threshold':.95,'passed':True,'covered':19,'expected':20,'uncovered':1}
    assert sl.nodes['profit'].value==-95
    assert sl.can_close
    assert slice_dict(sl,model.store('s'),model)['cost_coverage']['passed'] is True
