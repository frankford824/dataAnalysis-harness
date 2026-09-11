"""Read-side ERP comparisons. No posting, price checks or checkpoint writes."""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any


ORDER_FIELDS = (
    'payable_amount', 'collected_amount', 'freight_amount', 'discount_amount',
    'after_sale_payable_amount', 'outer_pay_id', 'online_order_keys',
    'raw_so_id', 'merge_so_id', 'outer_so_id', 'pre_so_id',
    'is_paid', 'is_cod', 'is_refund', 'order_type',
)
ITEM_FIELDS = ('item_pay_amount', 'line_amount', 'outer_oi_id', 'i_id',
               'sku_type', 'item_labels', 'properties_value')


def keys(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple)):
        return set().union(*(keys(v) for v in value)) if value else set()
    raw = str(value).strip()
    if raw.startswith('['):
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, list):
                return keys(decoded)
        except ValueError:
            pass
    return {re.sub(r',[0-9]+$', '', part.strip()) for part in raw.split(':') if part.strip()}


def amount(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def flag(value: Any) -> bool | None:
    if value is None:
        return None
    return {'true': True, '1': True, 'false': False, '0': False}.get(str(value).lower())


def read_groups(orders: list[dict], items: list[dict], query: str) -> list[dict]:
    """Group connected online-order identities; payment IDs locate, never merge.

    Callers provide one confirmed store only. Each ERP header contributes once,
    regardless of how many component rows or matching aliases it carries.
    """
    by_id = {str(o['order_id']): o for o in orders}
    aliases = {oid: keys(o.get('online_order_keys')) | keys(o.get('online_order_no'))
               for oid, o in by_id.items()}
    lookup = {oid: set(aliases[oid]) | ({str(o['outer_pay_id']).strip()} if o.get('outer_pay_id') else set())
              for oid, o in by_id.items()}
    for item in items:
        oid = str(item.get('order_id'))
        if oid in lookup:
            lookup[oid] |= keys(item.get('outer_oi_id') or item.get('platform_sub_order_id') or item.get('outer_sku'))
    parent = {oid: oid for oid in by_id}

    def root(oid):
        while parent[oid] != oid:
            parent[oid] = parent[parent[oid]]
            oid = parent[oid]
        return oid

    owner = {}
    for oid, values in aliases.items():
        for key in values:
            if key in owner:
                parent[root(oid)] = root(owner[key])
            else:
                owner[key] = oid
    groups: dict[str, list[str]] = {}
    for oid in by_id:
        groups.setdefault(root(oid), []).append(oid)
    wanted = keys(query) | {query.strip()}
    result = []
    for ids in groups.values():
        if not any(wanted & lookup[oid] or query == oid for oid in ids):
            continue
        rows = [by_id[oid] for oid in ids]
        totals = {}
        for field in ('payable_amount', 'collected_amount', 'discount_amount', 'freight_amount'):
            values = [amount(row.get(field)) for row in rows]
            totals[field] = str(sum(values, Decimal(0))) if all(v is not None for v in values) else None
        suppressed = any(flag(row.get('is_paid')) is False or flag(row.get('is_cod')) is True
                         or amount(row.get('collected_amount')) == 0 for row in rows)
        result.append({
            'internal_order_ids': sorted(ids),
            'online_order_keys': sorted(set().union(*(aliases[oid] for oid in ids))),
            **totals,
            'receipt_check': 'not_applicable' if suppressed else 'requires_statement',
            'message': '含未付款或货到付款订单，不能据此判断漏收' if suppressed
                       else '请结合店铺订单明细和资金流水核对',
            'amount_basis': 'erp',
        })
    return result
