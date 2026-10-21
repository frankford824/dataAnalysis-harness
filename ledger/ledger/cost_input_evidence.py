"""Narrow admission of verified quantity evidence for sale-price placeholders.

This does not certify a price or its date. Normal pricing, identity, company,
source and implausible-amount checks still apply after this one legacy flag.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import re

import polars as pl


def quantity(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
        if not parsed.is_finite() or parsed <= 0 or parsed != parsed.quantize(Decimal('.0001')):
            return None
        return parsed
    except (ValueError, TypeError, InvalidOperation):
        return None


def verified_quantity_input(row, *, require_placeholder=False):
    try:
        evidence = json.loads(row.get('pricing_evidence') or '{}')
        proof = evidence.get('cost_input')
        if not isinstance(proof, dict) or proof.get('version') != 'reship-cost-input.v1':
            return False
        if (proof.get('source') != 'jst.order_detail' or proof.get('identity_status') != 'verified'
                or proof.get('quantity_status') != 'verified' or proof.get('sale_price_status') not in {'normal','placeholder'}
                or proof.get('failure_reason') is not None or row.get('__identity_changed')):
            return False
        if require_placeholder and proof.get('sale_price_status')!='placeholder':
            return False
        sha = proof.get('source_order_hash')
        if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha) or sha != row.get('__current_order_hash'):
            return False
        captured = datetime.fromisoformat(proof['captured_at'].replace('Z', '+00:00'))
        if captured.tzinfo is None:
            return False
        for field in ('order_id', 'sub_order_id', 'sku_id'):
            if not isinstance(proof.get(field), str) or not proof[field] or proof[field] != str(row.get(field) or ''):
                return False
        if proof['sku_id'] != row.get('__current_item_sku'):
            return False
        qty = quantity(proof.get('quantity'))
        if qty is None or qty != quantity(row.get('quantity')) or qty != quantity(row.get('__current_item_quantity')):
            return False
        price = Decimal(str(row.get('__current_sale_price')))
        if not price.is_finite() or price<0:
            return False
        if (proof['sale_price_status']=='placeholder') != (price>=9999):
            return False
        parts = proof.get('components')
        if not isinstance(parts, list) or not parts or len(parts) > 10000:
            return False
        seen = set()
        for part in parts:
            if not isinstance(part, dict) or part.get('sub_order_id') != proof['sub_order_id']:
                return False
            sku = part.get('sku_id')
            component_qty = quantity(part.get('quantity'))
            if not isinstance(sku, str) or not sku or sku in seen or component_qty is None:
                return False
            seen.add(sku)
            if sku == proof['sku_id']:
                if len(parts) != 1 or component_qty != qty:
                    return False
            elif part.get('src_combine_sku_id') != proof['sku_id'] or quantity(part.get('src_combine_sku_qty')) != qty:
                return False
        return True
    except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation):
        return False


def verified_placeholder(row):
    return verified_quantity_input(row,require_placeholder=True)


def placeholder_admission(frame):
    """Inspect only flagged rows carrying the new protocol, not every cost row."""
    if 'pricing_evidence' not in frame.columns:
        return pl.lit(False)
    indexed = frame.with_row_index('__proof_index')
    candidates = indexed.filter(pl.col('is_suspect') &
        pl.col('pricing_evidence').cast(pl.String).str.contains('reship-cost-input.v1', literal=True).fill_null(False))
    accepted = [r['__proof_index'] for r in candidates.iter_rows(named=True) if verified_placeholder(r)]
    return pl.int_range(0, pl.len()).is_in(accepted)
