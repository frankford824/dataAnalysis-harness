"""Person-level profit composition for ladder worksheets.

Store commission stays untouched. This only reads an archived per-order file
and records which products a human took out of the ladder basis.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal
from pathlib import Path

import polars as pl

from .commission_engine import allocated_outputs
from .commission_registry import RegistryError, RevisionConflict, json_text, now
from .money import decimal_amount, money_float

_WANTED = (
    'status', 'person_id', 'person', 'product_id', 'product_name',
    'share', 'total_rate', 'original_base', 'amount',
    'participation_sales', 'participation_gross', 'participation_profit',
    'spine_row', 'order_id',
)


def display_person_id(store_id, person_id, person, roster_ids):
    if not person_id or (str(person_id).startswith('legacy:') and person_id not in roster_ids):
        return 'legacy:' + hashlib.sha256((store_id + '\0' + (person or '')).encode()).hexdigest()
    return person_id


def _calculation(registry, store_id, period, run_id):
    with registry.connect() as conn:
        row = conn.execute(
            'SELECT * FROM calculation WHERE store_id=? AND period=? AND finance_run=?'
            ' ORDER BY at DESC,id DESC LIMIT 1',
            (store_id, period, run_id)).fetchone()
    if not row:
        raise RegistryError('这次核算没有逐单提成明细')
    path = registry.root / 'calculations' / Path(row['path']).name
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha']:
        raise RegistryError('提成明细文件缺失或校验不符')
    return dict(row), path


def _person_rows(details, store_id, person_id, roster_ids):
    if 'person_id' not in details.columns or details.is_empty():
        return details.head(0)
    if person_id in set(details['person_id'].drop_nulls().to_list()):
        return details.filter(pl.col('person_id') == person_id)
    names = details.select(
        [name for name in ('person_id', 'person') if name in details.columns]).unique()
    matched = [
        row['person_id'] for row in names.iter_rows(named=True)
        if display_person_id(store_id, row['person_id'], row.get('person') or '', roster_ids)
        == person_id
    ]
    if len(matched) == 1:
        return details.filter(pl.col('person_id') == matched[0])
    return details.head(0)


def _allocated_parts(assigned):
    fields = {name: name for name in (
        'participation_sales', 'participation_gross', 'participation_profit')
        if name in assigned.columns}
    if 'participation_profit' not in fields and 'original_base' in assigned.columns:
        fields['participation_profit'] = 'original_base'
    if not fields or assigned.is_empty():
        return assigned.head(0)
    ready = assigned.with_columns(
        pl.col('share').cast(pl.Decimal(16, 8)).alias('__share'),
        pl.col('total_rate').cast(pl.Decimal(16, 8), strict=False).alias('__rate'),
        *[pl.col(source).cast(pl.Decimal(28, 10), strict=False).alias('__' + name)
          for name, source in fields.items()],
    ).filter(pl.col('__rate').is_not_null() & (pl.col('__rate') > 0))
    return ready.with_columns(*[
        (pl.col('__' + name) * pl.col('__share') / pl.col('__rate')).alias(name)
        for name in fields
    ])


def _money_or_none(value):
    return None if value is None else money_float(value)


def _rate_or_none(value):
    """Keep commission shares like 1.5%; do not round them as money cents."""
    if value is None:
        return None
    return float(decimal_amount(value).quantize(Decimal('0.00000001')))


def _unique_rates(values):
    seen, out = set(), []
    for value in values or []:
        rate = _rate_or_none(value)
        if rate is None or rate <= 0 or rate in seen:
            continue
        seen.add(rate)
        out.append(rate)
    out.sort()
    return out


def _product_rows(parts):
    if parts.is_empty() or 'product_id' not in parts.columns:
        return []
    aggs = [
        pl.col('product_name').drop_nulls().first().alias('product_name')
        if 'product_name' in parts.columns else pl.lit('').alias('product_name'),
        pl.col('spine_row').n_unique().alias('orders')
        if 'spine_row' in parts.columns else pl.len().alias('orders'),
        pl.col('participation_sales').sum().alias('sales')
        if 'participation_sales' in parts.columns else pl.lit(None).alias('sales'),
        pl.col('participation_gross').sum().alias('gross')
        if 'participation_gross' in parts.columns else pl.lit(None).alias('gross'),
        pl.col('participation_profit').sum().alias('profit')
        if 'participation_profit' in parts.columns else pl.lit(None).alias('profit'),
        pl.col('share').cast(pl.Float64).unique().alias('rates'),
    ]
    grouped = parts.group_by('product_id').agg(aggs)
    orders = _order_rows(parts)
    products = []
    for row in grouped.iter_rows(named=True):
        rates = _unique_rates(row.get('rates'))
        products.append({
            'product_id': row['product_id'] or '',
            'product_name': row.get('product_name') or '',
            'orders': int(row['orders'] or 0),
            'sales': _money_or_none(row.get('sales')),
            'gross': _money_or_none(row.get('gross')),
            'profit': _money_or_none(row.get('profit')),
            'rate': rates[0] if len(rates) == 1 else None,
            'rates': rates,
            'rate_mixed': len(rates) > 1,
            'lines': orders.get(row['product_id'] or '', []),
        })
    products.sort(key=lambda row: (-(row['profit'] or 0), row['product_id']))
    return products


def _order_rows(parts):
    if parts.is_empty():
        return {}
    keys = ['product_id']
    if 'spine_row' in parts.columns:
        keys.append('spine_row')
    elif 'order_id' in parts.columns:
        keys.append('order_id')
    aggs = []
    if 'order_id' in parts.columns:
        aggs.append(pl.col('order_id').drop_nulls().first().alias('order_id'))
    if 'participation_profit' in parts.columns:
        aggs.append(pl.col('participation_profit').sum().alias('profit'))
    if 'amount' in parts.columns:
        aggs.append(pl.col('amount').sum().alias('amount'))
    grouped = parts.group_by(keys).agg(aggs or [pl.len().alias('n')])
    out = {}
    for row in grouped.iter_rows(named=True):
        pid = row.get('product_id') or ''
        out.setdefault(pid, []).append({
            'order_id': str(row.get('order_id') or row.get('spine_row') or ''),
            'profit': _money_or_none(row.get('profit')),
            'amount': _money_or_none(row.get('amount')),
        })
    for rows in out.values():
        rows.sort(key=lambda item: (-(item['profit'] or 0), item['order_id']))
    return out


def _sum_profit(products, pred):
    return money_float(sum((
        Decimal(str(row['profit'])) for row in products
        if row['profit'] is not None and pred(row)
    ), Decimal(0)))


def compose(registry, store_id, period, person_id, run_id, *, store_name=''):
    """Group one person's allocated output by product for a single store month."""
    if not person_id:
        raise RegistryError('请选择人员')
    meta, path = _calculation(registry, store_id, period, run_id)
    available = set(pl.scan_parquet(path).collect_schema().names())
    details = pl.read_parquet(path, columns=[name for name in _WANTED if name in available])
    roster = {row['id']: row for row in registry.people()}
    person_details = _person_rows(details, store_id, person_id, set(roster))
    assigned = (person_details.filter(pl.col('status') == 'distribute')
                if 'status' in person_details.columns and not person_details.is_empty()
                else person_details.head(0))
    parts = _allocated_parts(assigned)
    products = _product_rows(parts)
    person_name = next((row.get('person') for row in assigned.iter_rows(named=True)
                        if row.get('person')), '') or roster.get(person_id, {}).get('name') or person_id
    split = allocated_outputs(assigned)
    engine_pid = assigned['person_id'][0] if not assigned.is_empty() else person_id
    trial = None
    if not assigned.is_empty() and 'amount' in assigned.columns:
        trial = money_float(assigned['amount'].sum())
    excluded = _latest(registry, store_id, period, person_id, run_id)
    known = {row['product_id'] for row in products}
    excluded_ids = [pid for pid in (excluded or {}).get('excluded_product_ids') or [] if pid in known]
    cut = set(excluded_ids)
    return {
        'store_id': store_id, 'store': store_name or store_id, 'period': period,
        'person_id': person_id, 'person': person_name, 'run_id': run_id,
        'calculation_id': meta['id'], 'source_sha': meta['sha'],
        'commission_trial': trial,
        'allocated_profit': None if split is None or engine_pid not in split
        else split[engine_pid].get('profit'),
        'products': products,
        'excluded_product_ids': excluded_ids,
        'included_profit': _sum_profit(products, lambda row: row['product_id'] not in cut) if products else None,
        'excluded_profit': _sum_profit(products, lambda row: row['product_id'] in cut) if products else None,
        'saved': excluded,
    }


def _latest(registry, store_id, period, person_id, run_id):
    with registry.connect() as conn:
        row = conn.execute(
            'SELECT * FROM profit_exclusion WHERE store_id=? AND period=? AND person_id=?'
            ' AND finance_run=? ORDER BY at DESC,id DESC LIMIT 1',
            (store_id, period, person_id, run_id)).fetchone()
    if not row:
        return None
    return {'id': row['id'], 'at': row['at'], 'actor': row['actor'],
            'note': row['note'], 'included_profit': money_float(row['included_profit']),
            'excluded_product_ids': json.loads(row['excluded_json'])}


def latest_for_run(registry, store_id, period, run_id):
    with registry.connect() as conn:
        rows = conn.execute(
            'SELECT * FROM profit_exclusion WHERE store_id=? AND period=? AND finance_run=?'
            ' ORDER BY at DESC,id DESC', (store_id, period, run_id)).fetchall()
    out = {}
    for row in rows:
        if row['person_id'] in out:
            continue
        out[row['person_id']] = {
            'included_profit': money_float(row['included_profit']),
            'excluded_count': len(json.loads(row['excluded_json'])),
        }
    return out


def save(registry, *, store_id, period, person_id, run_id, source_sha,
         excluded_product_ids, note, actor, store_name=''):
    if not note.strip():
        raise RegistryError('请写明剔除这些商品的原因')
    current = compose(registry, store_id, period, person_id, run_id, store_name=store_name)
    if source_sha != current['source_sha']:
        raise RevisionConflict('逐单明细已更新，请刷新后再保存')
    wanted = {row['product_id'] for row in current['products']}
    excluded, seen = [], set()
    for pid in excluded_product_ids or []:
        pid = str(pid or '')
        if pid in seen:
            continue
        if pid not in wanted:
            raise RegistryError('只能剔除本次明细里出现的商品')
        seen.add(pid)
        excluded.append(pid)
    included = _sum_profit(current['products'], lambda row: row['product_id'] not in seen)
    record = {
        'id': str(uuid.uuid4()), 'store_id': store_id, 'period': period,
        'person_id': person_id, 'finance_run': run_id, 'at': now(),
        'actor': actor, 'note': note.strip(), 'source_sha': source_sha,
        'excluded_json': json_text(excluded),
        'included_profit': str(included),
    }
    with registry.transaction() as conn:
        previous = conn.execute(
            'SELECT * FROM profit_exclusion WHERE store_id=? AND period=? AND person_id=?'
            ' AND finance_run=? ORDER BY at DESC,id DESC LIMIT 1',
            (store_id, period, person_id, run_id)).fetchone()
        conn.execute('INSERT INTO profit_exclusion VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                     tuple(record[key] for key in (
                         'id', 'store_id', 'period', 'person_id', 'finance_run',
                         'at', 'actor', 'note', 'source_sha', 'excluded_json',
                         'included_profit')))
        registry.audit(conn, actor, 'profit.exclude', record['id'], note.strip(),
                       dict(previous) if previous else None, record)
    current['excluded_product_ids'] = excluded
    current['included_profit'] = included
    current['excluded_profit'] = _sum_profit(current['products'], lambda row: row['product_id'] in seen)
    current['saved'] = {'id': record['id'], 'at': record['at'], 'actor': actor,
                        'note': record['note'], 'included_profit': included,
                        'excluded_product_ids': excluded}
    return current
