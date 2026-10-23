from decimal import Decimal

import polars as pl
import pytest

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
        'line_amount':paid,'item_pay_amount':paid,'refund_amount':'0','sku_id':'sku'+child,'sku_type':'normal','product_name':'商品'+child,**extra}


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


def test_superseded_parent_with_reused_tracking_is_not_another_shipment_or_quantity():
    result=packages([header('old','@W1','100',order_status_raw='Split',ship_time='2026-06-01 09:59:00'),
        header('e1','W1','60',is_split=True,order_status_raw='Sent'),
        header('e2','W2','40',is_split=True,order_status_raw='Sent')],
        [item('old','o1','100000001','2','80'),item('old','o2','100000002','1','20'),
         item('e1','i1','100000001','1','40'),item('e1','i2','100000002','1','20'),
         item('e2','i3','100000001','1','40')],
        [order('100000001','80','2'),order('100000002','20','1')])
    packet=result['W1']
    assert packet['issue']=='' and packet['internal_order_ids']==['e1']
    proposal=allocate('12',packet['members'])
    assert {a['child_id']:Decimal(a['amount']) for a in proposal['allocations']}=={'100000001':Decimal(8),'100000002':Decimal(4)}


def test_verified_child_amount_fragments_work_when_purchase_quantity_is_absent():
    result=packages([header('e1','W1','100',is_split=True),header('e2','W2','100',is_split=True)],
        [item('e1','i1','100000001','1','40'),item('e1','i2','100000002','1','20'),item('e2','i3','100000001','1','40')],
        [order('100000001','80'),order('100000002','20')])
    proposed=allocate('12',result['W1']['members'])
    assert sorted(Decimal(a['amount']) for a in proposed['allocations'])==[Decimal(4),Decimal(8)]
    assert all(m['basis_source']=='erp_goods_amount' for m in result['W1']['members'])


def test_erp_line_totals_need_not_match_platform_payment_or_header():
    result=packages([header('e1','W1','100',is_split=True),header('e2','W2','100',is_split=True)],
        [item('e1','i1','100000001','1','.01'),item('e1','i2','100000002','1','20'),item('e2','i3','100000001','1','.01')],
        [order('100000001','80'),order('100000002','20')])
    proposal=allocate('12',result['W1']['members'])
    assert proposal['method']=='erp_goods_amount'
    assert proposal['basis_total']=='20.01'
    assert sum(Decimal(a['amount']) for a in proposal['allocations'])==12


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


def test_native_platform_items_use_the_same_erp_goods_amount_rule():
    orders=[order('', '100',None,product_id='product100000001'),order('', '100',None,product_id='product100000002')]
    ready=packages([header('e1','W1','100')],[item('e1','i1','100000001','1','80'),item('e1','i2','100000002','1','20')],orders,platform='jd')
    proposed=allocate('10',ready['W1']['members'])
    assert sorted(Decimal(a['amount']) for a in proposed['allocations'])==[Decimal(2),Decimal(8)]
    unknown=packages([header('e1','W1','100')],[item('e1','i1','100000001','1','.01'),item('e1','i2','100000002','1','.01')],orders,platform='jd')
    assert [Decimal(a['amount']) for a in allocate('10',unknown['W1']['members'])['allocations']]==[Decimal(5),Decimal(5)]


def test_multiple_reship_items_use_current_shipment_goods_amounts_with_original_identity():
    rel=[{'relation_type':'reship','source_order_id':'sale','target_order_id':'replacement',
          'source_sub_order_id':'s1','target_sub_order_id':'r1'},
         {'relation_type':'reship','source_order_id':'sale','target_order_id':'replacement',
          'source_sub_order_id':'s2','target_sub_order_id':'r2'}]
    rows=[item('sale','s1','100000001','2','80'),item('sale','s2','100000002','1','20'),
          item('replacement','r1','after_sale_A','1','0',line_amount='40',shop_item_id='product100000001',sku_id='sku100000001'),
          item('replacement','r2','after_sale_B','1','0',line_amount='20',shop_item_id='product100000002',sku_id='sku100000002')]
    result=packages([header('sale','OLD','100'),header('replacement','NEW','0')],rows,
        [order('100000001','80','2'),order('100000002','20','1')],relations=rel)
    proposal=allocate('12',result['NEW']['members'])
    assert {a['child_id']:Decimal(a['amount']) for a in proposal['allocations']}=={'100000001':Decimal(8),'100000002':Decimal(4)}


def test_goods_amount_weights_do_not_subtract_or_require_refunds():
    orders=[order('', '100',None,product_id='product100000001',refund_amount='20'),
            order('', '100',None,product_id='product100000002',refund_amount='20')]
    unmatched=packages([header('e1','W1','100')],
        [item('e1','i1','100000001','1','80'),item('e1','i2','100000002','1','20')],orders,platform='jd')
    assert sorted(Decimal(a['amount']) for a in allocate('10',unmatched['W1']['members'])['allocations'])==[Decimal(2),Decimal(8)]
    matched=packages([header('e1','W1','100')],
        [item('e1','i1','100000001','1','80'),item('e1','i2','100000002','1','20',refund_amount='20')],orders,platform='jd')
    assert sorted(Decimal(a['amount']) for a in allocate('10',matched['W1']['members'])['allocations'])==[Decimal(2),Decimal(8)]


def test_normal_and_replacement_pieces_of_same_child_keep_separate_weights():
    relations=[{'relation_type':'reship','source_order_id':'sale','target_order_id':'replacement',
                'source_sub_order_id':'s1','target_sub_order_id':'r1'}]
    result=packages([header('sale','SAME','100'),header('replacement','SAME','0')],
        [item('sale','s1','100000001','2','80'),item('sale','s2','100000002','1','20'),
         item('replacement','r1','after_sale_A','1','0',line_amount='40',shop_item_id='product100000001',sku_id='sku100000001')],
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
         item('replacement','r1','after_sale_A','3','0',line_amount='120',shop_item_id='product100000001',sku_id='sku100000001'),
         item('replacement','r2','after_sale_B','1','0',line_amount='20',shop_item_id='product100000002',sku_id='sku100000002')],
        [order('100000001','80','2'),order('100000002','20','1')],relations=relations)
    proposal=allocate('14',result['NEW']['members'])
    assert {a['child_id']:Decimal(a['amount']) for a in proposal['allocations']}=={'100000001':Decimal(12),'100000002':Decimal(2)}


def test_unknown_multi_item_reship_is_not_zero_income_equal_share():
    result=packages([header('new','W1','0',order_type='补发订单')],
        [item('new','n1','after_sale_A','1','0'),item('new','n2','after_sale_B','1','0')],
        [order('100000001','80','1')])
    assert allocate('12',result['W1']['members'])['reason']=='zero_basis'


def test_cross_shop_candidates_are_all_retained():
    model=_model(stores=(Store(id='s1',name='甲',platform='taobao'),Store(id='s2',name='乙',platform='taobao')))
    result=packages([header('one','W1','80'),header('two','W1','20',order_store_id='os2')],
        [item('one','i1','100000001','1','80'),item('two','i2','100000002','1','20',order_store_id='os2')],
        [order('100000001','80','1')],model=model,mapping=[
            {'order_store_id':'os1','ledger_store_id':'s1','mapping_status':'confirmed'},
            {'order_store_id':'os2','ledger_store_id':'s2','mapping_status':'confirmed'}])
    assert result['W1']['store_ids']==['s1','s2'] and result['W1']['issue']=='cross_store'


def test_qianhuajian_glue_and_paper_use_erp_line_totals_without_platform_refunds():
    # Real failure: 1 platform purchase expands into glue x3 + paper x1.
    # ERP goods amounts differ from the platform export; neither difference
    # nor the missing refund column is a reason to discard these weights.
    main='6953955743890543720';other='6953955743890609256';product='3818528803085484358'
    rows=[item('15542855','50013143',main,'3','.04',sku_id='HQT00840',product_name='点胶'),
          item('15542855','50013144',main,'1','12.76',sku_id='HSC34610',product_name='相纸'),
          item('15542855','50013145',other,'3','.07',sku_id='HQT00840',product_name='点胶'),
          item('15542855','50013146',other,'1','23.73',sku_id='HSC34537',product_name='定制相纸')]
    for row in rows:row.update(shop_item_id=product,item_pay_amount=None,refund_amount=None)
    originals=[order(main,'11.41','1',order_id=main,product_id=product),
               order(other,'21.19','1',order_id=main,product_id=product)]
    for row in originals:row.pop('refund_amount')
    packet=packages([header('15542855','YT0050441008139','36.60',platform_order_ids=[main])],
        rows,originals,platform='douyin')['YT0050441008139']
    proposal=allocate('1.55',packet['members'],issue=packet['issue'])
    assert proposal['method']=='erp_goods_amount' and Decimal(proposal['basis_total'])==Decimal('36.60')
    assert {a['child_id']:Decimal(a['basis_amount']) for a in proposal['allocations']}=={main:Decimal('12.80'),other:Decimal('23.80')}
    assert {a['child_id']:Decimal(a['amount']) for a in proposal['allocations']}=={main:Decimal('.542077'),other:Decimal('1.007923')}
    assert all(len(m['erp_items'])==2 for m in packet['members'])
    assert '相纸' in packet['members'][0]['product_name']


def test_line_amount_is_not_payment_unit_price_or_quantity_times_line_total():
    packet=packages([header('e','W','999')],
        [item('e','i1','100000001','3','80',line_amount='7',unit_price='123',refund_amount='70'),
         item('e','i2','100000002','9','20',line_amount='3',unit_price='456')],
        [order('100000001','800','1'),order('100000002','200','1')])['W']
    proposal=allocate('10',packet['members'])
    assert [Decimal(a['amount']) for a in proposal['allocations']]==[Decimal(7),Decimal(3)]


@pytest.mark.parametrize('value',[None,'','bad','NaN','Infinity','-1'])
def test_invalid_erp_goods_amount_is_not_replaced_by_payment_or_platform_basis(value):
    packet=packages([header('e','W','100')],
        [item('e','i1','100000001','1','80',line_amount=value),item('e','i2','100000002','1','20')],
        [order('100000001','80','1'),order('100000002','20','1')])['W']
    assert allocate('10',packet['members'])['reason']=='amount_basis'


def test_explicit_zero_goods_amount_and_all_zero_package_are_distinct():
    rows=[item('e','i1','100000001','1','0'),item('e','i2','100000002','1','20')]
    originals=[order('100000001','0','1'),order('100000002','20','1')]
    packet=packages([header('e','W','20')],rows,originals)['W']
    assert [Decimal(a['amount']) for a in allocate('10',packet['members'])['allocations']]==[Decimal(0),Decimal(10)]
    rows[1]['line_amount']='0'
    packet=packages([header('e','W','20')],rows,originals)['W']
    assert allocate('10',packet['members'],allow_zero_equal=packet['allow_zero_equal'])['reason']=='zero_basis'


def test_erp_duplicate_lines_do_not_inflate_weights_and_conflicts_block():
    rows=[item('e','i1','100000001','1','80'),item('e','i2','100000002','1','20')]
    originals=[order('100000001','80','1'),order('100000002','20','1')]
    packet=packages([header('e','W','100')],[*rows,dict(rows[0])],originals)['W']
    assert allocate('10',packet['members'])['basis_total']=='100'
    packet=packages([header('e','W','100')],[*rows,{**rows[0],'line_amount':'81'}],originals)['W']
    assert allocate('10',packet['members'])['reason']=='identity_missing'


@pytest.mark.parametrize('name',['定制海报/定制专拍/下单备注文案/70*24cm方形','定制海报/定制专拍/100*24方形','定制KT板专拍'])
def test_dispatched_custom_goods_survive_legacy_virtual_label(name):
    packet=packages([header('e','W','8.33',order_status_raw='Sent')],
        [item('e','i1','100000001','1','8.33',is_virtual=True,product_name=name)],
        [order('100000001','8.33','1')])['W']
    assert packet['issue']=='' and len(packet['members'])==1
    proposal=allocate('1.55',packet['members'],issue=packet['issue'])
    assert proposal['status']=='allocated' and proposal['allocations'][0]['amount']=='1.550000'


@pytest.mark.parametrize('extra',[
    {'product_name':'邮费/补差价专拍'}, {'product_name':'运费专拍'},
    {'product_name':'电子服务'}, {'sku_type':'virtual'}, {'sku_id':''},
])
def test_fee_or_unproven_virtual_items_do_not_become_physical_from_a_tracking_number(extra):
    packet=packages([header('e','W','8.33',order_status_raw='Sent')],
        [item('e','i1','100000001','1','8.33',is_virtual=True,
              **{'product_name':'定制海报/定制专拍',**extra})],
        [order('100000001','8.33','1')])['W']
    assert packet['members']==[]
    assert allocate('1.55',packet['members'],issue=packet['issue'])['reason']=='non_shipping_items'


def test_custom_purchase_wording_does_not_replace_missing_dispatch_evidence():
    packet=packages([header('e','W','8.33',ship_time=None,order_status_raw='WaitConfirm')],
        [item('e','i1','100000001','1','8.33',is_virtual=True,product_name='定制海报/定制专拍')],
        [order('100000001','8.33','1')])['W']
    assert packet['issue']=='non_shipping_items'


def test_excluded_fee_does_not_change_real_package_members_or_dispatch_time():
    packet=packages([header('e','W','20',order_status_raw='Sent'),
                     header('fee','W','8',ship_time='2026-06-02 10:00:00',order_status_raw='Sent')],
        [item('e','i1','100000001','1','20'),
         item('fee','i2','100000002','1','8',is_virtual=True,product_name='邮费专拍')],
        [order('100000001','20','1'),order('100000002','8','1')])['W']
    assert packet['issue']=='' and packet['internal_order_ids']==['e']
    assert len(packet['members'])==1 and packet['members'][0]['child_id']=='100000001'


@pytest.mark.parametrize('kind',['virtual','no_deliver'])
def test_explicit_nonshipping_type_is_not_overridden_by_a_false_legacy_flag(kind):
    packet=packages([header('e','W','8.33',order_status_raw='Sent')],
        [item('e','i1','100000001','1','8.33',is_virtual=False,sku_type=kind,product_name='定制专拍')],
        [order('100000001','8.33','1')])['W']
    assert packet['issue']=='non_shipping_items' and not packet['members']


def test_parcel_preserves_each_erp_identity_even_when_ledger_store_is_unmapped():
    result=packages([header('one','W1','10',jst_shop_id='19027942',platform_code='得物'),
        header('two','W1','10',order_store_id='unknown',jst_shop_id='12876922',platform_code='其它平台')],
        [item('one','i1','100000001','1','10'),item('two','i2','100000002','1','10',order_store_id='unknown')],[order('100000001','10')])
    assert result['W1']['erp_shop_keys']==['12876922|其它平台','19027942|得物']
    assert result['W1']['issue']=='cross_store'
    incomplete=packages([header('one','W1','10',jst_shop_id='19027942',platform_code='得物'),
        header('two','W1','10',order_store_id='unknown')],
        [item('one','i1','100000001','1','10'),item('two','i2','100000002','1','10',order_store_id='unknown')],[order('100000001','10')])
    assert incomplete['W1']['erp_shop_keys']==['','19027942|得物']
