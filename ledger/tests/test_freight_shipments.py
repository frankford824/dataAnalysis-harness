from decimal import Decimal

import polars as pl

from ledger.freight_allocation import allocate
from ledger.freight_shipments import platform_bases,build_packages
from ledger.model.schema import Store,Platform
from test_commission import _model


MAIN='100000000'


def order(child,paid,quantity=None,**extra):
    return {'order_id':MAIN,'sub_order_id':child,'product_id':'product'+child,'buyer_paid':paid,
        'refund_amount':'0','quantity':quantity,'__sha__':'a'*64,'__file__':'订单明细.xlsx',
            '__sheet__':'export','__row__':int(child[-2:] or '0')+2,**extra}


def header(oid,track,paid,**extra):
    return {'order_id':oid,'order_store_id':'os1','online_order_no':MAIN,'platform_order_ids':[MAIN],
        'tracking_no':track,'ship_time':'2026-06-01 10:00:00','paid_amount':paid,'freight_amount':'0',
        'is_split':False,'logistics_company':'carrier','platform_order_identity_complete':True,**extra}


def item(oid,iid,child,qty,paid,**extra):
    return {'order_id':oid,'order_store_id':'os1','sub_order_id':iid,'outer_sku':child,'online_order_no':MAIN,
        'shop_item_id':'product'+child,'merchant_sku':None,'tracking_no':None,'quantity':qty,
        'item_pay_amount':paid,'refund_amount':'0','sku_id':'sku'+child,'sku_type':'normal','product_name':'商品'+child,**extra}


def packages(headers,items,orders,platform='taobao',mapping=None,relations=None,model=None):
    children,controls,native=platform_bases([('s1',0,pl.from_dicts(orders,infer_schema_length=None))])
    return build_packages(pl.from_dicts(headers,infer_schema_length=None),pl.from_dicts(items,infer_schema_length=None),
        pl.from_dicts(relations) if relations else pl.DataFrame(),
        mapping or [{'order_store_id':'os1','ledger_store_id':'s1','mapping_status':'confirmed'}],
        model or _model().model_copy(update={'stores':(Store(id='s1',name='测试店',platform=platform),),
            'platforms':(Platform(id=platform,name=platform),)}),children,controls,native)


def test_actual_shipments_exclude_unshipped_sibling_and_weight_partial_quantities():
    result=packages([header('e1','@W1','60',is_split=True),header('e2','W2','40',is_split=True),header('e3',None,'100')],
        [item('e1','i1','100000001','1','40'),item('e1','i2','100000002','1','20'),
         item('e2','i3','100000001','1','40'),item('e3','i4','100000003','1','100')],
        [order('100000001','80','2'),order('100000002','20','1'),order('100000003','100','1')])
    assert set(result)=={'W1','W2'}
    first=allocate('12',result['W1']['members'],issue=result['W1']['issue'])
    assert {a['child_id']:Decimal(a['amount']) for a in first['allocations']}=={'100000001':Decimal(8),'100000002':Decimal(4)}
    assert len(result['W1']['members'])==2
    assert allocate('6',result['W2']['members'])['status']=='allocated'


def test_missing_packet_member_identity_is_retained_instead_of_dropped():
    result=packages([header('e1','W1','100')],
        [item('e1','i1','100000001','1','80'),item('e1','i2','', '1','20')],
        [order('100000001','80','1')])
    assert len(result['W1']['members'])==2
    assert allocate('10',result['W1']['members'])['reason']=='identity_missing'


def test_verified_child_amount_fragments_work_when_purchase_quantity_is_absent():
    result=packages([header('e1','W1','100',is_split=True),header('e2','W2','100',is_split=True)],
        [item('e1','i1','100000001','1','40'),item('e1','i2','100000002','1','20'),item('e2','i3','100000001','1','40')],
        [order('100000001','80'),order('100000002','20')])
    proposed=allocate('12',result['W1']['members'])
    assert sorted(Decimal(a['amount']) for a in proposed['allocations'])==[Decimal(4),Decimal(8)]
    assert all(m['basis_source']=='platform_order_with_checked_erp_portion' for m in result['W1']['members'])


def test_unreconciled_child_payment_fragment_stays_pending_instead_of_using_whole_amount():
    result=packages([header('e1','W1','100',is_split=True),header('e2','W2','100',is_split=True)],
        [item('e1','i1','100000001','1','.01'),item('e1','i2','100000002','1','20'),item('e2','i3','100000001','1','.01')],
        [order('100000001','80'),order('100000002','20')])
    assert allocate('12',result['W1']['members'])['reason']=='quantity_basis'


def test_multiple_dispatch_candidates_are_not_resolved_by_taking_first():
    result=packages([header('e1','W1','80'),header('e2','W1','20',ship_time='2026-06-02 10:00:00')],
        [item('e1','i1','100000001','1','80'),item('e2','i2','100000002','1','20')],
        [order('100000001','80','1'),order('100000002','20','1')])
    assert result['W1']['issue']=='shipment_conflict'
    assert allocate('10',result['W1']['members'],issue=result['W1']['issue'])['status']=='pending'


def test_platform_child_conflict_does_not_borrow_another_parent_identity():
    result=packages([header('e1','W1','100')],[item('e1','i1','100000001','1','100')],
        [order('100000001','100','1'),order('100000001','100','1',order_id='200000000')])
    assert allocate('10',result['W1']['members'])['reason']=='identity_missing'


def test_merged_header_primary_value_cannot_broadcast_parent_to_an_unproven_child():
    result=packages([header('e1','W1','100',platform_order_ids=[MAIN,'200000000'])],
        [item('e1','i1','100000009','1','100')],[order('100000001','100','1')])
    assert result['W1']['members'][0]['identity_verified'] is False
    assert allocate('10',result['W1']['members'])['reason']=='identity_missing'


def test_platform_original_child_resolves_its_own_member_in_a_merged_header():
    result=packages([header('e1','W1','100',platform_order_ids=[MAIN,'200000000'])],
        [item('e1','i1','100000009','1','100')],[order('100000009','100','1',order_id='200000000')])
    assert result['W1']['members'][0]['order_id']=='200000000'
    assert allocate('10',result['W1']['members'])['status']=='allocated'


def test_native_platform_item_amounts_require_header_and_platform_controls():
    orders=[order('', '100',None,product_id='product100000001'),order('', '100',None,product_id='product100000002')]
    ready=packages([header('e1','W1','100')],[item('e1','i1','100000001','1','80'),item('e1','i2','100000002','1','20')],orders,platform='jd')
    proposed=allocate('10',ready['W1']['members'])
    assert sorted(Decimal(a['amount']) for a in proposed['allocations'])==[Decimal(2),Decimal(8)]
    unknown=packages([header('e1','W1','100')],[item('e1','i1','100000001','1','.01'),item('e1','i2','100000002','1','.01')],orders,platform='jd')
    assert allocate('10',unknown['W1']['members'])['status']=='pending'


def test_multiple_reship_items_use_proven_original_children_and_shipped_quantities():
    rel=[{'relation_type':'reship','source_order_id':'sale','target_order_id':'replacement',
          'source_sub_order_id':'s1','target_sub_order_id':'r1'},
         {'relation_type':'reship','source_order_id':'sale','target_order_id':'replacement',
          'source_sub_order_id':'s2','target_sub_order_id':'r2'}]
    rows=[item('sale','s1','100000001','2','80'),item('sale','s2','100000002','1','20'),
          item('replacement','r1','after_sale_A','1','0',shop_item_id='product100000001',sku_id='sku100000001'),
          item('replacement','r2','after_sale_B','1','0',shop_item_id='product100000002',sku_id='sku100000002')]
    result=packages([header('sale','OLD','100'),header('replacement','NEW','0')],rows,
        [order('100000001','80','2'),order('100000002','20','1')],relations=rel)
    proposal=allocate('12',result['NEW']['members'])
    assert {a['child_id']:Decimal(a['amount']) for a in proposal['allocations']}=={'100000001':Decimal(8),'100000002':Decimal(4)}


def test_native_amount_fallback_also_requires_platform_refund_total_to_match():
    orders=[order('', '100',None,product_id='product100000001',refund_amount='20'),
            order('', '100',None,product_id='product100000002',refund_amount='20')]
    unmatched=packages([header('e1','W1','100')],
        [item('e1','i1','100000001','1','80'),item('e1','i2','100000002','1','20')],orders,platform='jd')
    assert allocate('10',unmatched['W1']['members'])['reason']=='amount_basis'
    matched=packages([header('e1','W1','100')],
        [item('e1','i1','100000001','1','80'),item('e1','i2','100000002','1','20',refund_amount='20')],orders,platform='jd')
    assert sorted(Decimal(a['amount']) for a in allocate('10',matched['W1']['members'])['allocations'])==[Decimal(0),Decimal(10)]


def test_normal_and_replacement_pieces_of_same_child_keep_separate_weights():
    relations=[{'relation_type':'reship','source_order_id':'sale','target_order_id':'replacement',
                'source_sub_order_id':'s1','target_sub_order_id':'r1'}]
    result=packages([header('sale','SAME','100'),header('replacement','SAME','0')],
        [item('sale','s1','100000001','2','80'),item('sale','s2','100000002','1','20'),
         item('replacement','r1','after_sale_A','1','0',shop_item_id='product100000001',sku_id='sku100000001')],
        [order('100000001','80','2'),order('100000002','20','1')],relations=relations)
    packet=result['SAME']
    assert not packet['issue']
    assert len(packet['members'])==3
    proposal=allocate('14',packet['members'])
    assert sorted(Decimal(a['amount']) for a in proposal['allocations'])==[Decimal(2),Decimal(4),Decimal(8)]


def test_verified_replacement_quantity_can_exceed_original_purchase_quantity():
    relations=[{'relation_type':'reship','source_order_id':'sale','target_order_id':'replacement',
                'source_sub_order_id':source,'target_sub_order_id':target} for source,target in [('s1','r1'),('s2','r2')]]
    result=packages([header('sale','OLD','100'),header('replacement','NEW','0')],
        [item('sale','s1','100000001','2','80'),item('sale','s2','100000002','1','20'),
         item('replacement','r1','after_sale_A','3','0',shop_item_id='product100000001',sku_id='sku100000001'),
         item('replacement','r2','after_sale_B','1','0',shop_item_id='product100000002',sku_id='sku100000002')],
        [order('100000001','80','2'),order('100000002','20','1')],relations=relations)
    proposal=allocate('14',result['NEW']['members'])
    assert {a['child_id']:Decimal(a['amount']) for a in proposal['allocations']}=={'100000001':Decimal(12),'100000002':Decimal(2)}


def test_unknown_multi_item_reship_is_not_zero_income_equal_share():
    result=packages([header('new','W1','0',order_type='补发订单')],
        [item('new','n1','after_sale_A','1','0'),item('new','n2','after_sale_B','1','0')],
        [order('100000001','80','1')])
    assert allocate('12',result['W1']['members'])['reason']=='reship_basis'


def test_cross_shop_candidates_are_all_retained():
    model=_model(stores=(Store(id='s1',name='甲',platform='taobao'),Store(id='s2',name='乙',platform='taobao')))
    result=packages([header('one','W1','80'),header('two','W1','20',order_store_id='os2')],
        [item('one','i1','100000001','1','80'),item('two','i2','100000002','1','20',order_store_id='os2')],
        [order('100000001','80','1')],model=model,mapping=[
            {'order_store_id':'os1','ledger_store_id':'s1','mapping_status':'confirmed'},
            {'order_store_id':'os2','ledger_store_id':'s2','mapping_status':'confirmed'}])
    assert result['W1']['store_ids']==['s1','s2'] and result['W1']['issue']=='cross_store'
