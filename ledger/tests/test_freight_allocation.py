from decimal import Decimal

import pytest

from ledger.freight_allocation import allocate


def member(key, amount=None, **kw):
    return {'target_key': key, 'store_id': 'shop', 'order_id': 'master', 'child_id': key,
            'identity_verified': True, 'amount_verified': amount is not None,
            'basis_amount': amount, 'whole_child': True, **kw}


def amounts(result):
    return {a['target_key']: Decimal(a['amount']) for a in result['allocations']}


def test_split_shipment_only_allocates_to_actual_members_and_quantity_portions():
    first = allocate('12', [member('A', '80', whole_child=False, ordered_quantity='2',
        shipped_quantity='1', quantity_verified=True), member('B', '20')])
    second = allocate('6', [member('A', '80', whole_child=False)])
    assert amounts(first) == {'A': Decimal(8), 'B': Decimal(4)}
    assert amounts(second) == {'A': Decimal(6)}
    assert sum(amounts(first).values()) + sum(amounts(second).values()) == 18
    assert 'C' not in amounts(first)


def test_merged_orders_use_amounts_not_incomparable_parent_percentages():
    result = allocate('58', [member('A', '80', order_id='order100'), member('B', '500', order_id='order1000')])
    assert amounts(result) == {'A': Decimal(8), 'B': Decimal(50)}


def test_verified_relative_ratios_are_usable_only_inside_one_parent():
    members=[member('A','.8',basis_unit='ratio'),member('B','.2',basis_unit='ratio')]
    ready=allocate('10',members)
    assert amounts(ready)=={'A':Decimal(8),'B':Decimal(2)}
    assert ready['method']=='declared_ratio' and ready['basis_total'] is None
    assert all(a['basis_amount'] is None for a in ready['allocations'])
    members[1]['order_id']='other'
    assert allocate('10',members)['reason']=='cross_order_basis'


def test_proven_shipment_amount_is_usable_without_guessing_piece_quantity():
    result = allocate('12', [member('A', '80', whole_child=False, portion_amount='40'), member('B', '20')])
    assert amounts(result) == {'A': Decimal(8), 'B': Decimal(4)}


@pytest.mark.parametrize('extra', [{}, {'quantity_verified': True, 'ordered_quantity':'0','shipped_quantity':'1'},
    {'quantity_verified': True, 'ordered_quantity':'1','shipped_quantity':'2'},
    {'quantity_verified': False, 'ordered_quantity':'2','shipped_quantity':'1'}])
def test_unknown_or_invalid_partial_quantities_never_take_the_full_child_amount(extra):
    result = allocate('12', [member('A','80',whole_child=False,**extra),member('B','20')])
    assert result['reason']=='quantity_basis' and not result['allocations'] and result['amount']=='12'


def test_explicit_zero_is_valid_but_missing_amount_is_not_zero():
    zero = allocate('10',[member('gift','0'),member('sale','80')])
    assert amounts(zero)=={'gift':Decimal(0),'sale':Decimal(10)}
    missing=allocate('10',[member('unknown'),member('sale','80')])
    assert missing['reason']=='amount_basis' and missing['amount']=='10'


def test_zero_net_exception_is_only_for_complete_same_order_sales_members():
    same=allocate('0.01',[member(k,'0') for k in ('C','A','B')],allow_zero_equal=True)
    assert same['method']=='zero_equal' and sum(amounts(same).values())==Decimal('.01')
    assert amounts(same)['A']==Decimal('.003334')
    assert allocate('10',[member('A','0'),member('B','0',order_id='other')])['reason']=='zero_basis'
    assert allocate('10',[member('A','0'),member('B','0',is_reship=True)])['reason']=='zero_basis'


def test_reshipment_uses_original_amount_evidence_and_never_its_zero_new_income():
    ready=allocate('12',[member('A','40',is_reship=True),member('B','20',is_reship=True)])
    assert amounts(ready)=={'A':Decimal(8),'B':Decimal(4)}
    unknown=allocate('12',[member('A',is_reship=True),member('B',is_reship=True)])
    assert unknown['reason']=='reship_basis'


def test_direct_target_needs_no_payment_ratio_but_still_needs_identity():
    assert amounts(allocate('10',[member('only')]))=={'only':Decimal(10)}
    assert allocate('10',[member('only',identity_verified=False)])['reason']=='identity_missing'


def test_charge_cannot_be_fanned_out_across_shops_or_duplicate_targets():
    assert allocate('10',[member('A','80'),member('B','20',store_id='other')])['reason']=='cross_store'
    assert allocate('10',[member('A','80'),member('A','80')])['reason']=='duplicate_member'


def test_refund_keeps_original_factors_instead_of_current_weights():
    members=[member('A','20'),member('B','80')]
    assert allocate('-10',members)['reason']=='refund_basis'
    result=allocate('-10',members,original_factors={'A':'.8','B':'.2'})
    assert amounts(result)=={'A':Decimal(-8),'B':Decimal(-2)}
    assert result['method']=='original_reversal'
    assert allocate('-10',[member('only')])['reason']=='refund_basis'
    assert amounts(allocate('-10',[member('only')],original_factors={'only':1}))=={'only':Decimal(-10)}


def test_subcent_charge_and_rounding_residue_are_conserved_deterministically():
    left=allocate('0.123',[member(k,'1') for k in ('C','B','A')])
    right=allocate('0.123', [member(k,'1') for k in ('A','B','C')])
    assert amounts(left)==amounts(right) and sum(amounts(left).values())==Decimal('.123')


@pytest.mark.parametrize('value',[None,'bad','NaN','Infinity'])
def test_invalid_charge_is_unknown_and_is_not_changed_to_zero(value):
    result=allocate(value,[member('A')]);assert result['reason']=='invalid_amount' and result['amount'] is None


def test_repeating_factors_can_be_reused_to_reverse_a_charge_exactly():
    from decimal import localcontext
    members=[member(k,'1') for k in ('C','B','A')]
    original=allocate('.01',members)
    factors={a['target_key']:a['factor'] for a in original['allocations']}
    with localcontext() as context:
        context.prec=50
        assert sum(Decimal(v) for v in factors.values())==1
    credit=allocate('-.01',members,original_factors=factors)
    assert credit['status']=='allocated'
    assert all(amounts(original)[key]+amounts(credit)[key]==0 for key in factors)


def test_erp_parcel_credit_does_not_require_a_positive_bill_in_the_same_month():
    single=allocate('-.5',[member('only',basis_source='erp_goods_amount')])
    assert single['method']=='direct_credit' and amounts(single)=={'only':Decimal('-.5')}
    members=[member('A','70',basis_source='erp_goods_amount'),member('B','30',basis_source='erp_goods_amount')]
    credit=allocate('-10',members)
    assert credit['method']=='erp_goods_credit' and amounts(credit)=={'A':Decimal(-7),'B':Decimal(-3)}
    # A recorded original allocation still takes priority over current weights.
    assert amounts(allocate('-10',members,original_factors={'A':'.2','B':'.8'}))=={'A':Decimal(-2),'B':Decimal(-8)}


def test_missing_original_never_bypasses_parcel_identity_money_or_source_checks():
    members=[member('A','70',basis_source='erp_goods_amount'),member('B',basis_source='erp_goods_amount')]
    assert allocate('-10',members)['reason']=='amount_basis'
    assert allocate('-10',members,issue='duplicate_charge')['reason']=='duplicate_charge'
    members[1]['identity_verified']=False
    assert allocate('-10',members)['reason']=='identity_missing'
    zero=[member(k,'0',basis_source='erp_goods_amount') for k in ('A','B')]
    assert allocate('-10',zero)['reason']=='zero_basis'


@pytest.mark.parametrize('fee',['10','-0.5','0.0000001'])
def test_zero_erp_amounts_equal_distinct_children_then_split_their_product_targets(fee):
    rows=[member('A1','0',child_id='A',product_id='p1',basis_source='erp_goods_amount'),
          member('A2','0',child_id='A',product_id='p2',basis_source='erp_goods_amount'),
          member('B','0',order_id='another',child_id='B',product_id='p3',basis_source='erp_goods_amount')]
    result=allocate(fee,rows,allow_zero_equal=True)
    assert result['method']=='zero_identity_equal' and result['basis_total']=='2' and result['basis_unit']=='identity'
    assert sum(amounts(result).values())==Decimal(fee)
    assert [Decimal(a['factor']) for a in result['allocations']]==[Decimal('.25'),Decimal('.25'),Decimal('.5')]


def test_zero_native_and_reship_identities_are_equal_without_fabricating_child_ids():
    rows=[member('A','0',child_id='',product_id='p1',identity_kind='native_item',basis_source='erp_goods_amount'),
          member('B','0',child_id='',product_id='p2',identity_kind='erp_reship_item',is_reship=True,basis_source='erp_goods_amount')]
    result=allocate('3',rows,allow_zero_equal=True)
    assert amounts(result)=={'A':Decimal('1.5'),'B':Decimal('1.5')}
    assert all(a['child_id']=='' for a in result['allocations'])
    rows[1]['amount_verified']=False
    assert allocate('3',rows,allow_zero_equal=True)['status']=='pending'


def test_aggregate_then_expand_preserves_every_fee_and_the_aggregate_target_totals():
    from ledger.freight_allocation import expand_group
    rows=[member(k,'0',basis_source='erp_goods_amount') for k in ['C','A','B']]
    charges=[{'charge_id':str(i),'amount':'.01'} for i in range(3)]
    aggregate=allocate('.03',rows,allow_zero_equal=True);parts=expand_group(aggregate,charges)
    assert all(sum(amounts(r).values())==Decimal('.01') for r in parts.values())
    for target in amounts(aggregate):assert sum(amounts(r)[target] for r in parts.values())==amounts(aggregate)[target]
    assert parts==expand_group(aggregate,list(reversed(charges)))
