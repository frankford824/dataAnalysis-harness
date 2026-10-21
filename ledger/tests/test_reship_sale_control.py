import json
import polars as pl
import pytest
from ledger.order_feed import OrderFeed
from ledger.model.schema import Store


@pytest.mark.parametrize('original_qty,original_paid,original_shop,proof_ok,expected',[
    (32,'2100.04','10',True,False),
    (31,'2100.04','10',True,True),
    (32,'10.00','10',True,True),
    (32,'2100.04','20',True,True),
    (32,'2100.04','10',False,True),
])
def test_small_reship_collection_does_not_imply_wrong_cost_when_original_sale_is_proved(
    tmp_path,original_qty,original_paid,original_shop,proof_ok,expected):
    master='5127692905860061844'
    orders=pl.DataFrame({'order_id':['1','2'],'online_order_no':[master,master],
        'order_store_id':[original_shop,'10'],'order_type':['普通订单','补发订单'],
        'order_time':['2026-08-25 11:51:24','2026-08-30 10:41:03'],
        'order_status_raw':['Sent','Sent'],'tracking_no':['a','b'],
        'paid_amount':[original_paid,'0.04'],'content_hash':['a'*64,'b'*64]})
    items=pl.DataFrame({'order_id':['1','2'],'sub_order_id':['11','21'],'sku_id':['SKU','SKU'],
        'quantity':[str(original_qty),'32'],'unit_price':['0.0013','0.0013'],'line_amount':['0.04','0.04'],
        'online_order_no':[original_shop+':'+master,'10:'+master],
        'outer_sku':[master,'$Asr-1'],'tracking_no':['a','b'],'is_suspect':[False,False]})
    proof={'version':'reship-cost-input.v1','order_id':'2','sub_order_id':'21','sku_id':'SKU','quantity':'32.0000',
        'source':'jst.order_detail','source_order_hash':'b'*64,'captured_at':'2026-10-20T00:00:00Z',
        'identity_status':'verified','quantity_status':'verified','sale_price_status':'normal','failure_reason':None,
        'components':[{'sub_order_id':'21','sku_id':'SKU','quantity':'32','src_combine_sku_id':None,'src_combine_sku_qty':None}]}
    if not proof_ok:proof['source_order_hash']='c'*64
    costs=pl.DataFrame({'order_id':['2'],'sub_order_id':['21'],'sku_id':['SKU'],'quantity':['32'],
        'unit_cost':['6.2213'],'cost_amount':['199.08'],'cost_source':['history'],'cost_status':['priced'],
        'cost_as_of':['2026-08-25'],'pricing_evidence':[json.dumps({'order_date':'2026-08-25','cost_input':proof})]})
    relations=pl.DataFrame({'relation_type':['reship'],'source_order_id':['1'],'target_order_id':['2']})
    result=OrderFeed(tmp_path/'ws')._cost_frame(orders,items,costs,relations,Store(id='s',name='shop',platform='taobao'),'f',require_history=True)
    result=result.filter(pl.col('internal_order_id')=='2')
    assert result['pricing_suspect'].item() is expected
    assert result['quantity'].item()==32
    assert result['unit_cost'].item()==6.2213
    assert result['total_cost'].item()==199.08
    if not expected:assert '原销售单 1' in result['source_note'].item()
