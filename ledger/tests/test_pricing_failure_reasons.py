import pytest
import polars as pl

from test_pdd_historical_pricing import scenario


@pytest.mark.parametrize('reason,label',[
    ('conflicting_historical_cost','同一日期存在多个历史成本价，请核对价格依据'),
    ('conflicting_register_cost','成本登记记录相互冲突，尚未采用任何一个价格'),
    ('unverified_historical_carry','现有价格记录无法证明原订单日期的成本，需补充历史依据'),
    ('unverified_prior_register_cost','现有价格记录无法证明原订单日期的成本，需补充历史依据'),
    ('unverified_register_effective_date','现有价格记录无法证明原订单日期的成本，需补充历史依据'),
    ('unverified_original_quantity','尚未取得可核对的原始数量，不能按默认数量计成本'),
    ('original_quantity_conflict','原始数量与当前订单数量不一致，请核对后再计价'),
    ('item_identity_mismatch','成本证据与当前订单商品不一致，等待重新核实'),
    ('nonpositive_register_cost','登记成本为零或负数，尚不能作为有效成本'),
    ('unresolved_item_risk','订单异常尚未查清，暂不计入成本'),
    ('component_unit_rounding_conflict','套餐组件合计与单价乘数量的结果不一致，请核对金额'),
])
def test_specific_cost_evidence_failure_is_not_hidden_by_legacy_suspect(reason,label):
    _,result=scenario(unit_cost=None,cost_status='missing_price',failure_reason=reason,
                      pricing_suspect=True,pricing_source_suspect=True)
    assert result.pricing_gaps['reason'].to_list()==[label]
    assert result.facts.filter(pl.col('counted')).is_empty()


def test_identity_mismatch_remains_stronger_than_a_price_conflict():
    _,result=scenario(unit_cost=None,cost_status='missing_price',failure_reason='conflicting_historical_cost',
                      pricing_suspect=True,pricing_identity_changed=True)
    assert result.pricing_gaps['reason'].to_list()==['订单商品与成本明细不一致，等待更新']


def test_verified_upstream_day_without_original_sale_never_posts_to_reship_month():
    import json
    from datetime import datetime
    from conftest import MODELS
    from ledger.engine.runtime import Ingestion,run
    from ledger.model.loader import load_model
    from ledger.model.schema import Model,Platform,Store,SourceContract,StatementNode
    from test_reship_period_and_cost_drill import item
    metric=load_model(MODELS/'cn-ecommerce').metric('reshipment_cost').for_platform('taobao').model_copy(update={'by_platform':()})
    model=Model(id='reship-day',name='reship-day',platforms=(Platform(id='taobao',name='淘宝',cost_pricing='historical'),),
        stores=(Store(id='s',name='shop',platform='taobao'),),
        sources=(SourceContract(id='order_detail',name='订单',is_spine=True,owner_role='shop_owner',cadence='monthly'),
                 SourceContract(id='order_cost',name='成本',owner_role='shop_owner',cadence='monthly')),
        metrics=(metric,),statement=(StatementNode(id='cost',name='成本',formula={'op':'add','of':['reshipment_cost']}),))
    order=dict(order_id='5117236707584255215',sub_order_id='$Asr-1022122218',order_type='补发订单',
               store_name='shop',order_time=datetime(2026,7,3))
    cost={**order,'original_order_id':order['order_id'],'internal_order_id':'15619925','sku':'HSC31230',
          'quantity':1.,'unit_cost':3.85,'order_state':'Sent','cost_status':'priced','cost_source':'history',
          'cost_as_of':'2026-05-27','pricing_evidence':json.dumps({'order_date':'2026-05-27','cost_input':{'version':'reship-cost-input.v1'}})}
    result=run(Ingestion(model=model,items=[item('order_detail',[order],order),item('order_cost',[cost],cost)]),'taobao')
    assert result.spine_facts.is_empty()
    assert result.pricing_gaps.height==1
    assert result.pricing_gaps['reason'].item()=='订单台已提供原单日期 2026-05-27，但台账未匹配原销售订单明细；请补齐原单资料，不能计入补发月份'
    assert result.pricing_gaps['order_date'].item() is None
