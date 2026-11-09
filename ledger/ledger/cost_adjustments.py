"""人工成本调整：按订单商品行或商品规则指定商品成本。

调整只认订单号、子订单号、商品编码这些业务身份，不绑定某一次计算结果，所以每次
重算都会重新套用；记录只追加，撤销也是追加一条。已结账月份的结果不会被重算，
调整也就碰不到它们。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import polars as pl

from .workspace import WorkspaceError

ALL_STORES = '*'


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def _text(value) -> str:
    return str(value if value is not None else '').strip()


def _amount(value, label: str) -> str | None:
    if value is None or _text(value) == '':
        return None
    try:
        number = Decimal(_text(value))
    except InvalidOperation as exc:
        raise WorkspaceError(f'{label}不是有效金额：{value}') from exc
    if not number.is_finite() or number < 0:
        raise WorkspaceError(f'{label}必须是不小于 0 的金额')
    return str(number)


def _validate(ws, item: dict, store_ids: set[str]) -> dict:
    kind = _text(item.get('kind')) or ('rule' if not _text(item.get('order_id')) else 'line')
    store_id = _text(item.get('store_id'))
    reason = _text(item.get('reason'))
    if not reason or len(reason) > 500:
        raise WorkspaceError('每条成本调整都要写不超过 500 字的依据')
    unit, total = _amount(item.get('unit_cost'), '单价'), _amount(item.get('total_cost'), '总成本')
    if (unit is None) == (total is None):
        raise WorkspaceError('单价和总成本只填一个')
    spec = {key: _text(item.get(key)) for key in ('order_id', 'sub_order_id', 'sku', 'with_sku') if _text(item.get(key))}
    period = _text(item.get('period'))
    if kind == 'line':
        if store_id not in store_ids:
            raise WorkspaceError(f'没有这家店：{store_id or "未填"}')
        if not spec.get('order_id'):
            raise WorkspaceError('按商品行调整必须给订单号')
        if spec.get('with_sku'):
            raise WorkspaceError('按商品行调整不支持“同单商品”条件，请改用商品规则')
    elif kind == 'rule':
        if store_id != ALL_STORES and store_id not in store_ids:
            raise WorkspaceError(f'没有这家店：{store_id or "未填"}（全部店铺填 *）')
        if not spec.get('sku') or spec.get('order_id') or spec.get('sub_order_id'):
            raise WorkspaceError('商品规则按商品编码匹配，只能填商品编码和可选的同单商品编码')
        if total is not None:
            raise WorkspaceError('商品规则只能指定单价')
    else:
        raise WorkspaceError('调整类型只能是 line（按商品行）或 rule（商品规则）')
    if period:
        if len(period) != 7 or period[4] != '-':
            raise WorkspaceError(f'月份格式应为 2026-08：{period}')
        stores = sorted(store_ids) if store_id == ALL_STORES else [store_id]
        closed = ws.conn.execute(
            f"SELECT store_id FROM period WHERE period=? AND state='closed' AND store_id IN ({','.join('?' * len(stores))})",
            (period, *stores)).fetchall()
        if closed and store_id != ALL_STORES:
            raise WorkspaceError(f'{store_id} {period} 已结账，不能再调整成本')
    elif kind == 'line':
        raise WorkspaceError('按商品行调整要给出所属月份')
    return {'store_id': store_id, 'period': period, 'kind': kind, 'spec': spec,
            'unit_cost': unit, 'total_cost': total, 'reason': reason}


def add(ws, items: list[dict], store_ids: set[str], by: str = '人工操作') -> dict:
    if not items:
        raise WorkspaceError('没有要保存的成本调整')
    rows = [_validate(ws, item, store_ids) for item in items]
    saved = []
    with ws.transaction() as conn:
        for row in rows:
            cursor = conn.execute(
                'INSERT INTO cost_adjustment(store_id,period,kind,spec_json,unit_cost,total_cost,target_id,reason,at,by) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)',
                (row['store_id'], row['period'], row['kind'], json.dumps(row['spec'], ensure_ascii=False, sort_keys=True),
                 row['unit_cost'], row['total_cost'], None, row['reason'], _now(), by))
            saved.append(cursor.lastrowid)
    stores = set(store_ids) if any(r['store_id'] == ALL_STORES for r in rows) else {r['store_id'] for r in rows}
    return {'saved': saved, 'stores': sorted(stores), 'revision': revision(ws)}


def revoke(ws, ids: list[int], reason: str, store_ids: set[str], by: str = '人工操作') -> dict:
    reason = _text(reason)
    if not ids or not reason or len(reason) > 500:
        raise WorkspaceError('撤销要给出调整编号和不超过 500 字的依据')
    current = {row['id']: row for row in _rows(ws.conn)}
    missing = [i for i in ids if i not in current]
    if missing:
        raise WorkspaceError(f'这些调整不存在或已撤销：{missing}')
    with ws.transaction() as conn:
        for i in ids:
            conn.execute(
                'INSERT INTO cost_adjustment(store_id,period,kind,spec_json,unit_cost,total_cost,target_id,reason,at,by) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)',
                (current[i]['store_id'], current[i]['period'], 'revoke', '{}', None, None, i, reason, _now(), by))
    stores = set(store_ids) if any(current[i]['store_id'] == ALL_STORES for i in ids) else {current[i]['store_id'] for i in ids}
    return {'revoked': ids, 'stores': sorted(stores), 'revision': revision(ws)}


def revision(ws) -> int:
    return int(ws.conn.execute('SELECT coalesce(max(id),0) FROM cost_adjustment').fetchone()[0])


def _rows(conn, store_id: str | None = None) -> list[dict]:
    rows = [dict(r) for r in conn.execute('SELECT * FROM cost_adjustment ORDER BY id')]
    revoked = {r['target_id'] for r in rows if r['kind'] == 'revoke'}
    active = []
    for r in rows:
        if r['kind'] == 'revoke' or r['id'] in revoked:
            continue
        if store_id is not None and r['store_id'] not in (store_id, ALL_STORES):
            continue
        r['spec'] = json.loads(r['spec_json'] or '{}')
        active.append(r)
    return active


def listing(ws, store_id: str | None = None) -> list[dict]:
    return [{k: v for k, v in r.items() if k != 'spec_json'} for r in _rows(ws.conn, store_id)]


def _active(root, store_id: str) -> list[dict]:
    path = Path(root) / 'workspace.db'
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cost_adjustment'").fetchone():
            return []
        return _rows(conn, store_id)


def _key(name: str) -> pl.Expr:
    return pl.col(name).cast(pl.Utf8).str.strip_chars().str.to_uppercase()


def apply(root, store, cost) -> list[str]:
    """把有效的人工调整套到成本行上，返回给明细看的说明。"""
    frame = cost.frame
    if frame is None or frame.is_empty() or not {'order_id', 'sku', 'quantity', 'unit_cost'} <= set(frame.columns):
        return []
    entries = _active(root, store.id)
    if not entries:
        return []
    columns = set(frame.columns)
    orders = [c for c in ('order_id', 'original_order_id') if c in columns]
    unit = pl.col('unit_cost').cast(pl.Float64, strict=False)
    total = (pl.col('total_cost') if 'total_cost' in columns else pl.lit(None)).cast(pl.Float64, strict=False)
    label_of = pl.lit(None, dtype=pl.Utf8)
    exempt = (pl.col('__cost_exempt_reason') if '__cost_exempt_reason' in columns else pl.lit(None)).cast(pl.Utf8)
    manual = (pl.col('__manual_cost') if '__manual_cost' in columns else pl.lit(False)).cast(pl.Boolean).fill_null(False)
    quantity = pl.col('quantity').cast(pl.Float64, strict=False)
    notes, skipped = [], []
    applied = 0
    for entry in entries:
        spec = entry['spec']
        if entry['kind'] == 'line':
            order = spec['order_id'].upper()
            mask = pl.any_horizontal([_key(c) == order for c in orders])
            if spec.get('sub_order_id') and 'sub_order_id' in columns:
                mask &= _key('sub_order_id') == spec['sub_order_id'].upper()
            if spec.get('sku'):
                mask &= _key('sku') == spec['sku'].upper()
        else:
            mask = _key('sku') == spec['sku'].upper()
            if spec.get('with_sku'):
                order_key = orders[0]
                with_orders = frame.filter(_key('sku') == spec['with_sku'].upper()).get_column(order_key).cast(pl.Utf8).unique()
                mask &= pl.col(order_key).cast(pl.Utf8).is_in(with_orders.implode())
        mask = mask.fill_null(False)
        hits = frame.select(mask.sum()).item()
        if not hits:
            if entry['kind'] == 'line':
                skipped.append(entry['id'])
            continue
        if entry['total_cost'] is not None and hits != 1:
            notes.append(f"人工调整 #{entry['id']} 给的是总成本，但对上了 {hits} 行商品，没有套用；请补子订单号或商品编码")
            continue
        if entry['total_cost'] is not None:
            target_total = pl.lit(float(entry['total_cost']))
            target_unit = pl.when(quantity > 0).then(target_total / quantity).otherwise(pl.lit(0.0))
        else:
            target_unit = pl.lit(float(entry['unit_cost']))
            target_total = target_unit * quantity.fill_null(0.0)
        zero = float(entry['total_cost'] if entry['total_cost'] is not None else entry['unit_cost']) == 0
        label = (f"人工调整 #{entry['id']}：" + ('成本计 0' if zero else
                 f"总成本改为 {entry['total_cost']}" if entry['total_cost'] is not None else f"单价改为 {entry['unit_cost']}")
                 + f"（{entry['reason']}，{entry['by']} {entry['at'][:10]}）")
        updates = [
            pl.when(mask).then(target_unit).otherwise(unit).alias('unit_cost'),
            pl.when(mask).then(pl.lit(label)).otherwise(label_of).alias('__manual_label'),
            pl.when(mask).then(pl.lit(label if zero else None, dtype=pl.Utf8)).otherwise(exempt).alias('__cost_exempt_reason'),
            (manual | mask).alias('__manual_cost'),
        ]
        if 'total_cost' in columns:
            updates.append(pl.when(mask).then(target_total).otherwise(total).alias('total_cost'))
        for flag in ('pricing_missing', 'pricing_suspect', 'pricing_source_suspect'):
            if flag in columns:
                updates.append(pl.when(mask).then(pl.lit(False)).otherwise(pl.col(flag).cast(pl.Boolean, strict=False)).alias(flag))
        if 'cost_status' in columns:
            updates.append(pl.when(mask).then(pl.lit('priced')).otherwise(pl.col('cost_status')).alias('cost_status'))
        frame = frame.with_columns(updates)
        columns |= {'__cost_exempt_reason', '__manual_cost', '__manual_label'}
        unit = pl.col('unit_cost').cast(pl.Float64, strict=False)
        total = (pl.col('total_cost') if 'total_cost' in columns else pl.lit(None)).cast(pl.Float64, strict=False)
        label_of, exempt = pl.col('__manual_label').cast(pl.Utf8), pl.col('__cost_exempt_reason').cast(pl.Utf8)
        manual = pl.col('__manual_cost').cast(pl.Boolean).fill_null(False)
        applied += hits
    if '__manual_label' in frame.columns:
        base = (pl.col('source_note') if 'source_note' in frame.columns else pl.lit(None)).cast(pl.Utf8)
        base = pl.when(base.str.strip_chars() == '').then(None).otherwise(base)
        priced = pl.col('__manual_label').is_not_null() & pl.col('__cost_exempt_reason').is_null()
        frame = frame.with_columns(
            pl.when(priced).then(pl.concat_str([base, pl.col('__manual_label')], separator='；', ignore_nulls=True))
            .when(pl.col('__manual_label').is_not_null()).then(base)
            .otherwise(pl.col('source_note') if 'source_note' in frame.columns else pl.lit(None, dtype=pl.Utf8))
            .alias('source_note')).drop('__manual_label')
    cost.frame = frame
    if applied:
        notes.append(f"按人工成本调整改了 {applied:,} 行商品成本，明细里写着每一行的调整依据")
    if skipped:
        notes.append(f"人工调整 {'、'.join('#' + str(i) for i in skipped)} 在本店当前订单里没有对上商品行")
    return notes
