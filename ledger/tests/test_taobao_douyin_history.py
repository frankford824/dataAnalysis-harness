from datetime import datetime
import pytest
from conftest import MODELS
from ledger.engine.runtime import Ingestion,run
from ledger.model.loader import load_model
from ledger.model.schema import Model,Platform,Store,SourceContract,StatementNode
from test_reship_period_and_cost_drill import item


@pytest.mark.parametrize('platform',['taobao','douyin'])
@pytest.mark.parametrize('source,day,available',[('history','2026-06-05',True),('history','2026-07-02',False),('mirror','2026-06-05',False)])
def test_original_platform_day_controls_cost_evidence(platform,source,day,available):
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
          'order_time':datetime(2026,7,2),'order_state':'Sent','cost_source':source,'cost_status':'priced','cost_as_of':day}
    result=run(Ingestion(model=model,items=[item('order_detail',[order],order),item('order_cost',[cost],cost)]),platform)
    sl=result.slices[('shop','2026-06')]
    assert sl.nodes['profit'].value==(-10 if available else None)
    assert result.pricing_gaps.height==(0 if available else 1)
    if not available:assert result.pricing_gaps['order_date'].item()=='2026-06-05'
