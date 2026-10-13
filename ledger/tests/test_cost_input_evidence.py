from copy import deepcopy
import json

import polars as pl
import pytest

from ledger.cost_input_evidence import verified_placeholder


def proof():
    return {'version':'reship-cost-input.v1','order_id':'1','sub_order_id':'11','sku_id':'SKU1','quantity':'2.0000',
        'source':'jst.order_detail','source_order_hash':'a'*64,'captured_at':'2026-10-12T12:00:00Z',
        'identity_status':'verified','quantity_status':'verified','sale_price_status':'placeholder','failure_reason':None,
        'components':[{'sub_order_id':'11','sku_id':'SKU1','quantity':'2','src_combine_sku_id':None,'src_combine_sku_qty':None}]}


def row(p=None):
    return {'order_id':'1','sub_order_id':'11','sku_id':'SKU1','quantity':'2',
        '__current_item_sku':'SKU1','__current_item_quantity':'2','__current_sale_price':'99999',
        '__current_order_hash':'a'*64,'pricing_evidence':json.dumps({'cost_input':proof() if p is None else p})}


def test_current_source_bound_placeholder_quantity_is_accepted():
    assert verified_placeholder(row())


@pytest.mark.parametrize('field,value',[
    ('version','reship-cost-input.v0'),('order_id','2'),('sub_order_id','12'),('sku_id','SKU2'),
    ('quantity','1'),('quantity',0),('quantity',True),('quantity','NaN'),('quantity','-2'),
    ('identity_status','unverified'),('quantity_status','unverified'),('sale_price_status','normal'),
    ('failure_reason','original_quantity_conflict'),('source','unknown'),('source_order_hash','b'*64),
    ('captured_at','2026-10-12'),('captured_at','invalid'),('components',[]),
])
def test_incomplete_or_conflicting_proof_does_not_clear_legacy_guard(field,value):
    p=proof();p[field]=value
    assert not verified_placeholder(row(p))


@pytest.mark.parametrize('field,value',[
    ('__current_item_sku','changed'),('__current_item_quantity','3'),('__current_order_hash',None),
    ('__current_sale_price','20'),('__identity_changed',True),('pricing_evidence','broken json'),
])
def test_current_item_and_source_must_still_match(field,value):
    r=row();r[field]=value
    assert not verified_placeholder(r)


def test_bundle_is_one_parent_with_distinct_components_not_two_parent_copies():
    p=proof();p['components']=[{'sub_order_id':'11','sku_id':s,'quantity':'2',
        'src_combine_sku_id':'SKU1','src_combine_sku_qty':'2'} for s in ('C1','C2')]
    assert verified_placeholder(row(p))
    duplicate=deepcopy(p);duplicate['components'].append(duplicate['components'][0])
    assert not verified_placeholder(row(duplicate))
    p['components'][1]['src_combine_sku_qty']='1'
    assert not verified_placeholder(row(p))


@pytest.mark.parametrize('valid',[True,False])
@pytest.mark.parametrize('historical',[True,False])
@pytest.mark.parametrize('high_cost',[True,False])
def test_feed_only_waives_placeholder_with_current_positive_evidence(tmp_path,valid,historical,high_cost):
    from test_order_feed import _fixture,_write,FakeClient
    from ledger.order_feed import OrderFeed
    from ledger.engine.runtime import Ingestion
    from ledger.model.schema import Store
    from ledger.model.loader import load_model
    from conftest import MODELS
    root=tmp_path/'feed';manifest=_fixture(root);objects=manifest['objects']
    orders=pl.read_parquet(root/objects['orders.parquet']['path']).with_columns(pl.lit('a'*64).alias('content_hash'))
    items=pl.read_parquet(root/objects['order_items.parquet']['path']).with_columns(
        pl.lit(True).alias('is_suspect'),pl.lit('99999').alias('unit_price'))
    p=proof()
    if not valid:p['source_order_hash']='b'*64
    costs=pl.read_parquet(root/objects['order_costs.parquet']['path']).with_columns(
        pl.lit(json.dumps({'order_date':'2026-06-02','cost_input':p})).alias('pricing_evidence'),
        pl.lit('2026-06-02').alias('cost_as_of'))
    if high_cost:
        costs=costs.with_columns(pl.lit('500.00').alias('unit_cost'),pl.lit('1000.00').alias('cost_amount'))
    for name,f in [('orders.parquet',orders),('order_items.parquet',items),('order_costs.parquet',costs)]:
        objects[name]=_write(root,name,f)
    (root/'current'/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    client=FakeClient(manifest);original=client.get
    def get(path,params=None):
        result=original(path,params)
        if path in {'entities/order/1','orders/1'}:
            result['order']['content_hash']='a'*64
            result['order']['paid_amount']='20.00'
        return result
    client.get=get
    feed=OrderFeed(tmp_path/'ws',client=client,feed_root=root);feed.sync()
    ingestion=Ingestion(model=load_model(MODELS/'cn-ecommerce') if historical else None)
    feed.append_to(ingestion,Store(id='taobao_test',name='淘宝测试店',platform='taobao'))
    frame=ingestion.frames_of('order_cost')[0].frame
    if historical:
        assert frame.height==1 and frame['pricing_source_suspect'].item() is (not valid)
        assert frame['pricing_suspect'].item() is (not valid or high_cost)
    else:
        assert frame.height==int(valid and not high_cost)
    if valid and not high_cost:assert frame['unit_cost'].item()==3.5
