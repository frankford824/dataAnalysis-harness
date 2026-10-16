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
