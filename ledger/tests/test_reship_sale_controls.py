"""Original-sale references are plausibility controls, never replacement prices."""
from copy import deepcopy
import json

import polars as pl
import pytest

from ledger.reship_sale_control import original_sale_controls
from ledger.order_feed import OrderFeed
from ledger.model.schema import Store
from test_cost_input_evidence import proof as quantity_proof

MASTER='1234567890123456789'


def inputs():
    proof=quantity_proof();proof.update(order_id='2',sale_price_status='normal')
    candidate={'order_id':'2','sub_order_id':'11','sku_id':'SKU1','quantity':'2',
        '__current_item_sku':'SKU1','__current_item_quantity':'2','__current_sale_price':'0.01',
        '__current_order_hash':'a'*64,'pricing_evidence':json.dumps({'cost_input':proof,'order_date':'2026-06-02'})}
    orders=[{'order_id':identifier,'online_order_no':master,'order_store_id':'10','order_type':kind,
        'order_status_raw':'Sent','paid_amount':paid,'order_time':'2026-06-02 10:00:00',
        'pay_time':'2026-06-02 10:01:00','tracking_no':'W'+identifier,'content_hash':'a'*64}
        for identifier,master,kind,paid in [('1',MASTER,'普通订单','500'),('2','BF2','补发订单','0.01')]]
    items=[{'order_id':identifier,'sub_order_id':sub,'online_order_no':master,'sku_id':sku,'quantity':qty,
        'paid_amount':paid,'line_amount':paid,'unit_price':price,'is_suspect':False,'is_gift':False,
        'merchant_sku':'product','shop_item_id':'product','outer_sku':'child-'+sub,'tracking_no':'W'+identifier}
        for identifier,sub,master,sku,qty,paid,price in [
            ('1','old',MASTER,'SKU1','2','1','0.5'),('1','other',MASTER,'SKU2','10','499','49.9'),
            ('2','11','10:'+MASTER,'SKU1','2','0.01','0.01')]]
    relations=[{'relation_type':'reship','source_order_id':'1','target_order_id':'2'}]
    return candidate,orders,items,relations


def controls(candidate,orders,items,relations):
    return original_sale_controls(pl.from_dicts([candidate]),pl.from_dicts(orders),pl.from_dicts(items),pl.from_dicts(relations))


def test_multi_sku_original_total_is_only_a_plausibility_ceiling():
    candidate,orders,items,relations=inputs()
    row=controls(candidate,orders,items,relations).row(0,named=True)
    assert row['__original_sale_control']==500 and row['__original_sale_control_id']=='1'
    # SKU1's sold line is 1. This whole-order ceiling must never be called its
    # unit price, its income, its original cost or its pricing-day evidence.
    assert not {'unit_cost','quantity','original_order_date','income'} & set(row)


@pytest.mark.parametrize('change',[
    'hash','sku','quantity','cross_store','many_originals','old_suspect','old_quantity','old_status','old_type','member','negative_paid',
])
def test_unproved_or_conflicting_original_sale_does_not_waive_the_ratio_guard(change):
    candidate,orders,items,relations=inputs()
    if change=='hash':candidate['__current_order_hash']='b'*64
    elif change=='sku':candidate['__current_item_sku']='SKU-other'
    elif change=='quantity':candidate['__current_item_quantity']='3'
    elif change=='cross_store':orders[0]['order_store_id']='20'
    elif change=='many_originals':relations.append({'relation_type':'reship','source_order_id':'3','target_order_id':'2'})
    elif change=='old_suspect':items[0]['is_suspect']=True
    elif change=='old_quantity':items[0]['quantity']='1'
    elif change=='old_status':orders[0]['order_status_raw']='Cancelled'
    elif change=='old_type':orders[0]['order_type']='补发订单'
    elif change=='member':items[-1]['online_order_no']='other-member'
    elif change=='negative_paid':orders[0]['paid_amount']='-1'
    assert controls(candidate,orders,items,relations).is_empty()


@pytest.mark.parametrize('valid',[True,False])
def test_cost_frame_keeps_dated_cost_and_quantity_instead_of_original_header_money(tmp_path,valid):
    candidate,orders,items,relations=inputs()
    if not valid:candidate['__current_order_hash']='b'*64
    orders[1]['content_hash']=candidate['__current_order_hash']
    cost={'order_id':'2','sub_order_id':'11','sku_id':'SKU1','quantity':'2','unit_cost':'100','cost_amount':'200',
        'cost_source':'history','cost_status':'priced','cost_as_of':'2026-06-02','pricing_evidence':candidate['pricing_evidence']}
    feed=OrderFeed(tmp_path/'ws')
    result=feed._cost_frame(pl.from_dicts(orders),pl.from_dicts(items),pl.from_dicts([cost]),pl.from_dicts(relations),
        Store(id='s',name='synthetic',platform='taobao'),'synthetic',require_pricing=True)
    row=result.row(0,named=True)
    assert row['quantity']==2 and row['unit_cost']==100 and row['total_cost']==200
    assert row['pricing_suspect'] is (not valid)
    assert ('数量量级按原销售单' in str(row.get('source_note'))) is valid
