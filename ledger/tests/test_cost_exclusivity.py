from datetime import datetime
import json
import polars as pl
import pytest
from conftest import MODELS
from ledger.model.loader import load_model
from ledger.model.schema import Model,Store,SourceContract,StatementNode
from ledger.engine.runtime import Ingestion,run
from test_reship_period_and_cost_drill import item


def calculate(*,sku='SKU',kind='销售订单',copies=1,goods=None,status='退款成功',unit=10.,item_state='Sent',remark=''):
    full=load_model(MODELS/'cn-ecommerce')
    metrics=tuple(full.metric(n).for_platform('douyin') for n in ['goods_cost','reshipment_cost'])
    model=Model(id='cost-policy',name='cost',stores=(Store(id='s',name='shop',platform='douyin'),),
        platforms=full.platforms,
        sources=tuple(SourceContract(id=s,name=s,is_spine=s=='order_detail',owner_role='shop_owner',cadence='monthly') for s in ['order_detail','order_cost','after_sales']),
        metrics=metrics,statement=(StatementNode(id='value',name='成本',formula={'op':'add','of':['goods_cost','reshipment_cost']}),))
    orders=[dict(order_id='MAIN',sub_order_id='SUB',order_type='销售订单',store_name='shop',order_time=datetime(2026,6,1),tracking_no='T') for _ in range(copies)]
    costs=[dict(order_id='MAIN',original_order_id='MAIN',sub_order_id='SUB',internal_order_id='1',sku=sku,order_remark=remark,order_type=kind,order_state=item_state,store_name='shop',order_time=datetime(2026,6,1),quantity=1.,unit_cost=unit,cost_source='history',cost_status='priced' if unit is not None else 'missing_price',cost_as_of='2026-06-01',pricing_evidence=json.dumps({'order_date':'2026-06-01'}))]
    items=[item('order_detail',orders,orders[0].keys()),item('order_cost',costs,costs[0].keys())]
    if goods:
        after=[dict(order_id='MAIN',sub_order_id='SUB',internal_order_id='1',sku=sku,refund_status=status,goods_status=goods,order_time=datetime(2026,7,1))]
        items.append(item('after_sales',after,after[0].keys()))
    return run(Ingestion(model=model,items=items),'douyin')


def test_duplicate_sub_order_rows_do_not_multiply_cost():
    for copies in [1,2,4]:
        result=calculate(copies=copies)
        assert result.facts['contribution'].sum()==pytest.approx(-10)
        assert result.spine_facts['amount'].sum()==pytest.approx(-10)


@pytest.mark.parametrize('sku',['DF','dfA','skuDf12','sku-dF','abcDFend','代发','周代发','dF.'])
@pytest.mark.parametrize('kind',['销售订单','补发订单'])
def test_df_items_are_explicit_zero_without_historical_price(sku,kind):
    result=calculate(sku=sku,kind=kind,unit=None)
    assert result.pricing_gaps.is_empty()
    assert result.facts.height==1
    assert result.facts['contribution'].sum()==0
    assert '代发商品' in result.facts['source_note'][0]


def test_reship_only_posts_to_reship_cost():
    r=calculate(kind='补发订单')
    assert r.facts['metric_id'].to_list()==['reshipment_cost']
    assert r.facts['contribution'].sum()==-10


@pytest.mark.parametrize('goods,status,expected',[
    ('买家未收到货','退款成功',0),('买家已退货','退款成功',0),('卖家已收到退货','退款成功',0),
    ('卖家已收到退货','退款关闭',-10),('买家已收到货','退款成功',-10)])
def test_five_after_sale_combinations_apply_to_original_cost(goods,status,expected):
    r=calculate(goods=goods,status=status)
    assert r.facts['contribution'].sum()==expected
    assert not r.facts.filter(pl.col('period')=='2026-07').height


def test_blank_online_child_keeps_real_internal_cost_identity(tmp_path):
    from test_order_feed import _fixture,_write,FakeClient
    from ledger.order_feed import OrderFeed
    from ledger.model.schema import Store
    from ledger.engine.runtime import Ingestion
    root=tmp_path/'feed';manifest=_fixture(root,second_unnamed=True)
    path=root/'objects/order_items.parquet'
    rows=pl.read_parquet(path).with_columns(pl.Series('outer_sku',['S1','']))
    manifest['objects']['order_items.parquet']=_write(root,'order_items.parquet',rows)
    feed=OrderFeed(tmp_path/'ws',client=FakeClient(manifest),feed_root=root);feed.sync()
    ing=Ingestion(model=None)
    feed.append_to(ing,Store(id='taobao_test',name='淘宝测试店',platform='taobao'))
    cost=ing.frames_of('order_cost')[0].frame
    assert cost.filter(pl.col('sku')=='SKU2')['sub_order_id'].item()=='12'
    assert cost.filter(pl.col('sku')=='SKU2')['original_order_id'].item()=='ON1'


def test_cancelled_item_in_sent_order_is_not_charged(tmp_path):
    from test_order_feed import _fixture,_write,FakeClient
    from ledger.order_feed import OrderFeed
    from ledger.model.schema import Store
    from ledger.engine.runtime import Ingestion
    root=tmp_path/'feed';manifest=_fixture(root,second_unnamed=True)
    data=pl.read_parquet(root/'objects/order_items.parquet').with_columns(pl.Series('item_status_raw',['Cancelled','Normal']))
    manifest['objects']['order_items.parquet']=_write(root,'order_items.parquet',data)
    feed=OrderFeed(tmp_path/'ws',client=FakeClient(manifest),feed_root=root);feed.sync()
    ing=Ingestion(model=None);feed.append_to(ing,Store(id='taobao_test',name='淘宝测试店',platform='taobao'))
    cost=ing.frames_of('order_cost')[0].frame
    assert cost.filter(pl.col('sku')=='SKU1')['order_state'].item()=='Cancelled'
    assert cost.filter(pl.col('sku')=='SKU2')['order_state'].item()=='Sent'


def test_item_status_delta_survives_an_older_snapshot_schema():
    from ledger.order_feed import OrderFeed
    base=pl.DataFrame({'sub_order_id':['1'],'order_id':['10']})
    delta={'entity_type':'order_item','entity_id':'1','operation':'upsert','order_id':'10','payload_json':json.dumps({'sub_order_id':'1','item_status_raw':'Cancelled'})}
    result=OrderFeed._overlay(base,[delta],'order_item','sub_order_id',lambda row:[row])
    assert result['item_status_raw'].to_list()==['Cancelled']


def test_unpriced_cost_review_uses_original_order_not_internal_item():
    result=calculate(unit=None)
    assert result.pricing_gaps['order_id'].to_list()==['MAIN']


def test_cost_export_keeps_original_order_and_product_identity():
    import csv,io
    from ledger.view import fees_csv
    result=calculate()
    rows=list(csv.DictReader(io.StringIO(fees_csv(result.facts,result.model))))
    assert rows[0]['订单号']=='SUB'
    assert rows[0]['原订单号']=='MAIN'
    assert rows[0]['商品编码']=='SKU'
    assert rows[0]['聚水潭订单号']=='1'


@pytest.mark.parametrize('sid',['douyin_mt9sbkne','douyin_luckywish'])
def test_confirmed_after_sale_costs_are_deducted_from_original_month(sid):
    assert load_model(MODELS/'cn-ecommerce').store(sid).cost_return_posting=='order'


def test_confirmed_refund_is_known_zero_cost_not_a_missing_price():
    r=calculate(goods='买家未收到货',unit=None)
    assert r.pricing_gaps.is_empty()
    assert r.facts.height==1
    assert r.facts['contribution'].item()==0
    assert r.facts['counted'].item()
    assert '售后已确认' in r.facts['source_note'].item()


def test_reship_after_sale_zeroes_only_its_own_cost():
    r=calculate(kind='补发订单',goods='买家未收到货',unit=None)
    assert r.pricing_gaps.is_empty()
    assert r.facts['metric_id'].to_list()==['reshipment_cost']
    assert r.facts['contribution'].item()==0


def test_cancelled_product_is_known_zero_without_a_price():
    r=calculate(item_state='Cancelled',unit=None)
    assert r.pricing_gaps.is_empty()
    assert r.facts.height==1 and r.facts['counted'].item()
    assert r.facts['contribution'].item()==0
    assert '商品已取消' in r.facts['source_note'].item()


@pytest.mark.parametrize('sku,remark,expected', [
    ('HZS12994','HZS12994 HZS12995代发采购钱',True),
    ('HZS12995','HZS12994 HZS12995代发采购钱',True),
    ('HZS08539','HZS08539采购徐代发',True),
    ('HQT22862','HQT22862代发采购钱',True),
    ('HZS08185','HZS08185采购徐代发',True),
    ('HZS08185','HZS08539采购徐代发',False),
    ('HZS08185','HZS08185仓库发，HZS08539代发',False),
    ('HZS08185','HZS08185不代发',False),
    ('HZS08185','HZS08185取消代发',False),
    ('HZS08185','HZS08185部分代发',False),
    ('HZS08185','HZS08185 2个代发',False),
    ('HZS0818','HZS08185采购徐代发',False),
    ('QT','采购徐代发',False),
])
def test_remark_only_exempts_explicit_product(sku,remark,expected):
    from ledger.engine.cost_policy import remark_dropship,prepare_dropship,is_dropship
    assert remark_dropship(sku,remark) is expected
    frame=prepare_dropship(pl.DataFrame({'sku':[sku],'order_remark':[remark]}))
    assert frame.select(is_dropship(frame)).item() is expected


def test_supplier_expense_cannot_be_borrowed_from_another_order_or_month():
    from ledger.engine.cost_policy import missing_supplier_costs
    facts=pl.DataFrame([
        dict(metric_id='goods_cost',order_id='A',sku='代发',source_note='代发商品：聚水潭成本计 0',counted=True,contribution=0.,store='s',period='2026-06'),
        dict(metric_id='dropship_cost',order_id='B',sku='',source_note='',counted=True,contribution=-20.,store='s',period='2026-06'),
        dict(metric_id='dropship_cost',order_id='A',sku='',source_note='',counted=True,contribution=-20.,store='s',period='2026-07'),
    ])
    assert missing_supplier_costs(facts).height==1
    matching=facts.tail(1).with_columns(pl.lit('2026-06').alias('period'))
    assert missing_supplier_costs(pl.concat([facts,matching])).is_empty()


def test_partial_cost_export_says_amounts_are_incomplete():
    import csv,io
    from ledger.view import fees_csv
    result=calculate()
    rows=list(csv.DictReader(io.StringIO(fees_csv(result.facts,result.model,review_status='成本未核齐（3条待核价），本表金额不完整'))))
    assert rows[0]['核对状态']=='成本未核齐（3条待核价），本表金额不完整'


def test_feed_remark_reaches_cost_line(tmp_path):
    from test_order_feed import _fixture,_write,FakeClient
    from ledger.order_feed import OrderFeed
    root=tmp_path/'feed';manifest=_fixture(root)
    path=root/'objects/orders.parquet'
    rows=pl.read_parquet(path).with_columns(pl.lit('SKU1采购徐代发').alias('order_remark'))
    manifest['objects']['orders.parquet']=_write(root,'orders.parquet',rows)
    client=FakeClient(manifest);original=client.get
    def get(path,params=None):
        response=original(path,params)
        if path in {'entities/order/1','orders/1'}:
            response['order']['order_remark']='SKU1采购徐代发'
        return response
    client.get=get
    feed=OrderFeed(tmp_path/'ws',client=client,feed_root=root);feed.sync()
    ing=Ingestion(model=None);feed.append_to(ing,Store(id='taobao_test',name='淘宝测试店',platform='taobao'))
    cost=ing.frames_of('order_cost')[0].frame
    assert cost['order_remark'][0]=='SKU1采购徐代发'


def test_order_remark_delta_survives_old_snapshot_schema():
    from ledger.order_feed import OrderFeed
    base=pl.DataFrame({'order_id':['1']})
    delta={'entity_type':'order','entity_id':'1','operation':'upsert','order_id':'1','payload_json':json.dumps({'order_id':'1','order_remark':'HZS08539采购徐代发'})}
    result=OrderFeed._overlay(base,[delta],'order','order_id',lambda row:[row])
    assert result['order_remark'].item()=='HZS08539采购徐代发'


def test_remark_exemption_precedes_price_requirement_and_preserves_other_goods():
    exempt=calculate(sku='HZS08539',remark='HZS08539采购徐代发',unit=None)
    assert exempt.pricing_gaps.is_empty()
    assert exempt.facts['contribution'].item()==0
    regular=calculate(sku='HQT22862',remark='HZS08539采购徐代发',unit=12)
    assert regular.facts['contribution'].item()==-12


def test_mixed_fulfilment_in_one_clause_does_not_clear_warehouse_goods():
    from ledger.engine.cost_policy import remark_dropship
    assert not remark_dropship('HZS08539','HZS08539仓库发 HQT22862代发')


def test_unscoped_dropship_remark_requests_review_without_guessing_zero():
    result=calculate(sku='QT',remark='采购徐代发',unit=10)
    assert result.facts['contribution'].item()==-10
    assert '代发范围待确认' in result.facts['source_note'].item()


def test_explicit_non_dropship_remark_does_not_require_scope_review():
    result=calculate(sku='SKU',remark='SKU取消代发',unit=10)
    assert result.facts['contribution'].item()==-10
    assert '代发范围待确认' not in (result.facts['source_note'].item() or '')


def test_export_retains_hosting_residual_in_its_actual_posted_months():
    from ledger.engine.runtime import _mark_counted
    from ledger.engine.normalize import STORE_WIDE_PRODUCT
    metric=load_model(MODELS/'cn-ecommerce').metric('ad_cost').for_platform('pdd')
    source=pl.DataFrame({'metric_id':['ad_cost'],'store':['s'],'period':['(未知账期)'],
        'link_key':[STORE_WIDE_PRODUCT],'amount':[-100.],'major':[None]})
    projected=pl.DataFrame({'metric_id':['ad_cost']*2,'store':['s']*2,'period':['2026-06','2026-07'],
        'link_key':[STORE_WIDE_PRODUCT]*2,'factor':[.4,.6]})
    result=_mark_counted(source,projected,[metric]).sort('period')
    assert result['period'].to_list()==['2026-06','2026-07']
    assert result['contribution'].to_list()==[-40.,-60.]
    assert result['counted'].to_list()==[True,True]


def test_split_internal_orders_do_not_flag_other_goods_when_remark_has_a_match():
    from ledger.engine.cost_policy import prepare_dropship,is_dropship
    frame=pl.DataFrame({'sku':['HZS05153','HQT00053'],'order_remark':['HZS05153采购徐代发']*2,
        'original_order_id':['MAIN']*2,'internal_order_id':['1','2'],'store_name':['s']*2})
    out=prepare_dropship(frame)
    assert out.select(is_dropship(out)).to_series().to_list()==[True,False]
    assert out['__dropship_ambiguous'].to_list()==[False,False]
