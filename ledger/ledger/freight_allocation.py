"""Deterministic shipment-level proposals. No ledger or commission writes."""
from __future__ import annotations

from decimal import Decimal, localcontext, ROUND_HALF_UP

from .money import decimal_amount

RULE_VERSION = 'shipment-erp-group-child-equal.v6'
REASONS = {
    'no_shipment': '未找到实际发货记录',
    'non_shipping_items': '有发货记录，明细被标记为非实物，需核对商品属性',
    'identity_missing': '商品或订单身份待核对',
    'no_valid_members': '忽略空子单后，没有可分摊的有效商品身份',
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
METHODS = {'direct': '单商品直接归属', 'amount': '按本包商品金额', 'erp_goods_amount': '按本包聚水潭商品金额', 'declared_ratio': '按原表分配率',
           'zero_equal': '零净额商品均分', 'original_reversal': '按原分摊冲回',
           'direct_credit': '单商品运费冲减', 'erp_goods_credit': '按本包聚水潭商品金额冲减',
           'zero_identity_equal': '全零按子单或商品身份等分'}


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
    erp_basis=all(m.get('basis_source')=='erp_goods_amount' and m.get('basis_unit','amount')=='amount' for m in members)
    if amount < 0 and original_factors is not None:
        # A later refund must not silently acquire today's unrelated weights.
        if not original_factors or set(original_factors) != {m['target_key'] for m in members}:
            return _pending(amount, 'refund_basis')
        factors = [number(original_factors[m['target_key']]) for m in members]
        with localcontext() as context:
            context.prec = 50
            valid = all(v is not None and v >= 0 for v in factors) and sum(factors) == 1
        if not valid:
            return _pending(amount, 'refund_basis')
        method = 'original_reversal'
    elif amount < 0 and not erp_basis:
        return _pending(amount, 'refund_basis')
    elif method == 'direct':
        factors = [Decimal(1)]
        if amount < 0:method='direct_credit'
    else:
        if relative and (any(m.get('basis_unit')!='ratio' for m in members)
                         or len({m.get('order_id') for m in members})!=1):
            return _pending(amount, 'cross_order_basis')
        if any(v is None for v in weights):
            reason = ('reship_basis' if any(m.get('is_reship') for m in members)
                      else 'quantity_basis' if any(m.get('amount_verified') and not m.get('whole_child') and w is None
                                                   for m,w in zip(members,weights))
                      else 'amount_basis')
            return _pending(amount, reason)
        total = sum(weights)
        if total == 0:
            if allow_zero_equal and erp_basis:
                units={}
                for i,m in enumerate(members):
                    key=(m['store_id'],m.get('order_id'),m.get('child_id')) if m.get('child_id') else (m['store_id'],m.get('order_id'),m.get('identity_kind'),m.get('product_id') or m['target_key'])
                    units.setdefault(key,[]).append(i)
                with localcontext() as context:
                    context.prec=50
                    weights=[Decimal(0)]*len(members)
                    for indices in units.values():
                        for i in indices:weights[i]=Decimal(1)/len(indices)
                    factors=[w/len(units) for w in weights]
                method='zero_identity_equal'
            elif (not allow_zero_equal or any(m.get('is_reship') for m in members)
                    or len({m.get('order_id') for m in members}) != 1
                    or any(not m.get('order_id') for m in members)):
                return _pending(amount, 'zero_basis')
            else:
                factors = [Decimal(1) / len(members)] * len(members)
                method = 'zero_equal'
        else:
            with localcontext() as context:
                context.prec = 40
                factors = [w / total for w in weights]
            if relative: method='declared_ratio'
            elif erp_basis:method='erp_goods_credit' if amount<0 else 'erp_goods_amount'
    # Fixed tie break: input order never decides who receives the residue.
    recipient = min(range(len(members)), key=lambda i: (-factors[i], members[i]['target_key']))
    quantum = Decimal(1).scaleb(min(-6, amount.as_tuple().exponent))
    with localcontext() as context:
        context.prec = 50
        # Persist factors that sum exactly to one, including repeating thirds,
        # so a later credit can use the saved allocation without a false gap.
        factors[recipient] += Decimal(1) - sum(factors)
        amounts = [(amount * factor).quantize(quantum, rounding=ROUND_HALF_UP) for factor in factors]
        amounts[recipient] += amount - sum(amounts)
    assert sum(amounts) == amount
    allocations = [{'target_key': m['target_key'], 'store_id': m['store_id'],
        'order_id': m.get('order_id') or '', 'child_id': m.get('child_id') or '',
        'product_id': m.get('product_id') or '', 'identity_kind': m.get('identity_kind') or 'platform_child',
        'basis_amount': '0' if method=='zero_identity_equal' else None if relative else text_amount(weights[i]), 'basis_weight':text_amount(weights[i]),
        'factor': text_amount(factors[i]),
        'amount': text_amount(amounts[i])} for i, m in enumerate(members)]
    return {'status': 'allocated', 'reason': '', 'reason_label': '', 'method': method,
            'method_label': METHODS[method], 'basis_unit':'identity' if method=='zero_identity_equal' else 'ratio' if relative else 'amount',
            'basis_total': str(len(units)) if method=='zero_identity_equal' else text_amount(sum(weights)) if not relative and all(w is not None for w in weights) else None,
            'amount': text_amount(amount), 'allocations': allocations}


def expand_group(result,charges):
    """Split an allocated bill/waybill total back to auditable source fee rows."""
    if result['status']!='allocated':return {c['charge_id']:_pending(number(c['amount']),result['reason']) for c in charges}
    rows={};template=result['allocations']
    recipient=min(range(len(template)),key=lambda i:(-Decimal(template[i]['factor']),template[i]['target_key']))
    quantum=Decimal(1).scaleb(min(-6,*(Decimal(c['amount']).as_tuple().exponent for c in charges)))
    with localcontext() as ctx:
        ctx.prec=50
        for c in charges:
            amount=Decimal(c['amount']);parts=[(amount*Decimal(a['factor'])).quantize(quantum,rounding=ROUND_HALF_UP) for a in template]
            parts[recipient]+=amount-sum(parts)
            method=result['method']
            if amount<0:method={'direct':'direct_credit','erp_goods_amount':'erp_goods_credit'}.get(method,method)
            elif method in {'original_reversal','direct_credit','erp_goods_credit'}:method='direct' if len(template)==1 else 'erp_goods_amount'
            rows[c['charge_id']]={**result,'amount':text_amount(amount),'method':method,'method_label':METHODS[method],
                'allocations':[{**a,'amount':text_amount(parts[i])} for i,a in enumerate(template)]}
        # The largest source fee absorbs only micro rounding differences between
        # allocating the sum once and expanding the source rows. Each row still
        # sums exactly to its own fee, and each target to the group allocation.
        anchor=min(charges,key=lambda c:(-abs(Decimal(c['amount'])),c['charge_id']))['charge_id']
        for i,a in enumerate(template):
            residue=Decimal(a['amount'])-sum(Decimal(r['allocations'][i]['amount']) for r in rows.values())
            rows[anchor]['allocations'][i]['amount']=text_amount(Decimal(rows[anchor]['allocations'][i]['amount'])+residue)
        assert all(sum(Decimal(a['amount']) for a in r['allocations'])==Decimal(r['amount']) for r in rows.values())
    return rows
