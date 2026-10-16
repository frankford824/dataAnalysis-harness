"""Deterministic shipment-level proposals. No ledger or commission writes."""
from __future__ import annotations

from decimal import Decimal, localcontext, ROUND_HALF_UP

from .money import decimal_amount

RULE_VERSION = 'shipment-amount.v1'
REASONS = {
    'no_shipment': '未找到实际发货记录',
    'identity_missing': '商品或订单身份待核对',
    'duplicate_member': '同一商品的发货明细存在冲突',
    'cross_store': '运单涉及多个店铺，归属待核对',
    'shipment_conflict': '同运单存在多次发货候选',
    'carrier_conflict': '同运单存在不同承运商',
    'quantity_basis': '拆包份额依据不足',
    'amount_basis': '商品分摊金额依据不足',
    'cross_order_basis': '跨主单合包缺少可比较的原单金额基数',
    'reship_basis': '多商品补发的原单金额或补发范围待核对',
    'zero_basis': '本包商品金额全为零，分摊方法待核对',
    'refund_basis': '费用冲减的原分摊依据待核对',
    'source_overlap': '不同账单存在重叠收费候选',
    'duplicate_charge': '同一账单存在重复收费候选',
    'source_conflict': '收费来源或金额存在冲突',
    'invalid_amount': '运费金额无效',
}
METHODS = {'direct': '单商品直接归属', 'amount': '按本包商品金额', 'declared_ratio': '按原表分配率',
           'zero_equal': '零净额商品均分', 'original_reversal': '按原分摊冲回'}


def number(value):
    if value in (None, ''):
        return None
    try:
        return decimal_amount(value)
    except (ValueError, ArithmeticError):
        return None


def text_amount(value):
    return None if value is None else format(value, 'f')


def _pending(amount, reason):
    return {'status': 'pending', 'reason': reason, 'reason_label': REASONS[reason],
            'method': '', 'basis_total': None, 'amount': text_amount(amount), 'allocations': []}


def _weight(member):
    """Only the adapter may certify amount/quantity provenance."""
    if not member.get('amount_verified'):
        return None
    portion = number(member.get('portion_amount'))
    if portion is not None:
        return portion if portion >= 0 else None
    amount = number(member.get('basis_amount'))
    if amount is None or amount < 0:
        return None
    if member.get('whole_child'):
        return amount
    ordered, shipped = number(member.get('ordered_quantity')), number(member.get('shipped_quantity'))
    if not member.get('quantity_verified') or ordered is None or shipped is None or ordered <= 0 or shipped <= 0 or (shipped > ordered and not member.get('is_reship')):
        return None
    return amount * shipped / ordered


def allocate(amount, members, *, issue='', allow_zero_equal=False, original_factors=None):
    """Allocate one charge inside its proven shipment membership.

    Amount strings retain at least the ledger's six-decimal intermediate
    precision. Display rounding is not used as a calculation input.
    """
    amount = number(amount)
    if amount is None:
        return _pending(None, 'invalid_amount')
    if issue:
        return _pending(amount, issue)
    if not members:
        return _pending(amount, 'no_shipment')
    if any(not m.get('identity_verified') or not m.get('target_key') or not m.get('store_id') for m in members):
        return _pending(amount, 'identity_missing')
    if len({m['store_id'] for m in members}) != 1:
        return _pending(amount, 'cross_store')
    if len({m['target_key'] for m in members}) != len(members):
        return _pending(amount, 'duplicate_member')
    weights = [_weight(m) for m in members]
    relative = any(m.get('basis_unit')=='ratio' for m in members)
    method = 'direct' if len(members) == 1 else 'amount'
    if amount < 0:
        # A later refund must not silently acquire today's unrelated weights.
        if not original_factors or set(original_factors) != {m['target_key'] for m in members}:
            return _pending(amount, 'refund_basis')
        factors = [number(original_factors[m['target_key']]) for m in members]
        if any(v is None or v < 0 for v in factors) or sum(factors) != 1:
            return _pending(amount, 'refund_basis')
        method = 'original_reversal'
    elif method == 'direct':
        factors = [Decimal(1)]
    else:
        if relative and (any(m.get('basis_unit')!='ratio' for m in members)
                         or len({m.get('order_id') for m in members})!=1):
            return _pending(amount, 'cross_order_basis')
        if any(v is None for v in weights):
            reason = ('reship_basis' if any(m.get('is_reship') for m in members)
                      else 'quantity_basis' if any(m.get('amount_verified') and not m.get('whole_child') for m in members)
                      else 'amount_basis')
            return _pending(amount, reason)
        total = sum(weights)
        if total == 0:
            if (not allow_zero_equal or any(m.get('is_reship') for m in members)
                    or len({m.get('order_id') for m in members}) != 1
                    or any(not m.get('order_id') for m in members)):
                return _pending(amount, 'zero_basis')
            factors = [Decimal(1) / len(members)] * len(members)
            method = 'zero_equal'
        else:
            with localcontext() as context:
                context.prec = 40
                factors = [w / total for w in weights]
            if relative: method='declared_ratio'
    quantum = Decimal(1).scaleb(min(-6, amount.as_tuple().exponent))
    amounts = [(amount * factor).quantize(quantum, rounding=ROUND_HALF_UP) for factor in factors]
    # Fixed tie break: input order never decides who receives the residue.
    recipient = min(range(len(members)), key=lambda i: (-factors[i], members[i]['target_key']))
    amounts[recipient] += amount - sum(amounts)
    assert sum(amounts) == amount
    allocations = [{'target_key': m['target_key'], 'store_id': m['store_id'],
        'order_id': m.get('order_id') or '', 'child_id': m.get('child_id') or '',
        'product_id': m.get('product_id') or '', 'identity_kind': m.get('identity_kind') or 'platform_child',
        'basis_amount': None if relative else text_amount(weights[i]), 'basis_weight':text_amount(weights[i]),
        'factor': text_amount(factors[i]),
        'amount': text_amount(amounts[i])} for i, m in enumerate(members)]
    return {'status': 'allocated', 'reason': '', 'reason_label': '', 'method': method,
            'method_label': METHODS[method], 'basis_unit':'ratio' if relative else 'amount',
            'basis_total': text_amount(sum(weights)) if not relative and all(w is not None for w in weights) else None,
            'amount': text_amount(amount), 'allocations': allocations}
