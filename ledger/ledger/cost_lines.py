"""Audited per-order human cost amounts for uncovered order keys."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import csv
import io

import polars as pl

from .manual_cost import _cents
from .storage_integrity import verified
from .workspace import WorkspaceError


def _source_rows(ws, run_id: int) -> list[dict]:
    path = ws.coverage_gaps_path(run_id)
    if not path.exists() or not verified(path):
        raise WorkspaceError("这次计算尚无完整的未覆盖订单明细，请先重算本店")
    return pl.read_parquet(path).to_dicts()


def revision(ws, store_id: str, period: str) -> int:
    row = ws.conn.execute(
        'SELECT coalesce(max(id),0) FROM cost_line_log WHERE store_id=? AND period=?',
        (store_id, period),
    ).fetchone()
    return int(row[0])


def _latest(ws, store_id: str, period: str, run_id: int) -> dict[str, dict]:
    rows = ws.conn.execute('''
        SELECT l.* FROM cost_line_log l JOIN (
          SELECT coverage_key,max(id) id FROM cost_line_log
          WHERE store_id=? AND period=? AND source_run_id<=? GROUP BY coverage_key
        ) current ON current.id=l.id
        WHERE l.store_id=? AND l.period=?
    ''', (store_id, period, run_id, store_id, period)).fetchall()
    return {row['coverage_key']: dict(row) for row in rows}


def current(ws, run_id: int, store_id: str, period: str) -> dict:
    """Merge saved decisions only while their exact source order context is still missing."""
    items = _source_rows(ws, run_id)
    latest = _latest(ws, store_id, period, run_id)
    frozen = ws.conn.execute('SELECT decision_json FROM manual_finance WHERE run_id=?',
                             (run_id,)).fetchone()
    if frozen:
        decision = json.loads(frozen['decision_json'])
        latest = {row['coverage_key']: {
            'id': row['revision'], 'context_sha': row['context_sha'],
            'action': 'save', 'amount': str(row['amount']),
            'reason': row['reason'], 'by': '结账时的人工确认', 'at': '',
        } for row in decision.get('line_reviews') or []}
    total = Decimal(0)
    reviewed = []
    for item in items:
        item['editable'] = item['order_count'] == 1
        saved = latest.get(item['coverage_key'])
        item['line_revision'] = saved['id'] if saved else 0
        item['manual_amount'] = None
        item['manual_reason'] = ''
        item['stale_decision'] = bool(saved and saved['context_sha'] != item['context_sha'])
        if (saved and not item['stale_decision'] and item['order_count'] == 1
                and saved['action'] == 'save'):
            amount = _cents(saved['amount'])
            item['manual_amount'] = float(amount)
            item['manual_reason'] = saved['reason']
            item['manual_by'] = saved['by']
            item['manual_at'] = saved['at']
            total += amount
            reviewed.append({
                'coverage_key': item['coverage_key'], 'context_sha': item['context_sha'],
                'order_id': item['order_id'], 'amount': float(amount),
                'reason': saved['reason'], 'revision': saved['id'],
            })
    return {'items': items, 'total': len(items), 'reviewed_count': len(reviewed),
            'supplement_total': float(total),
            'line_revision': decision.get('line_revision', 0) if frozen else revision(ws, store_id, period),
            'reviewed': reviewed}


def page(ws, run_id: int, store_id: str, period: str, *, q: str = '',
         offset: int = 0, limit: int = 50) -> dict:
    result = current(ws, run_id, store_id, period)
    needle = q.strip().casefold()
    visible = [row for row in result['items']
               if not needle or any(needle in str(row.get(key) or '').casefold()
                                    for key in ('order_id','sub_order_id','coverage_key','product_ids'))]
    return {key: value for key, value in result.items() if key not in ('items','reviewed')} | {
        'matching': len(visible), 'items': visible[max(0,offset):max(0,offset)+max(1,min(limit,200))],
        'offset': max(0,offset),
    }


def export_csv(ws, run_id: int, store_id: str, period: str) -> str:
    from .view import _excel_identifier_cell
    fields = [('order_id','平台订单号'),('sub_order_id','子订单号'),
              ('product_ids','商品链接'),('quantities','数量'),('order_date','下单日期'),
              ('manual_amount','人工补录总成本'),('manual_reason','确认依据'),
              ('editable','可直接补录')]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([label for _, label in fields])
    for row in current(ws, run_id, store_id, period)['items']:
        writer.writerow([_excel_identifier_cell(row.get(key)) if key.endswith('order_id')
                         else row.get(key) if row.get(key) is not None else ''
                         for key, _ in fields])
    return output.getvalue()


def save(ws, store_id: str, period: str, run_id: int, *, coverage_key: str,
         context_sha: str, amount, reason: str, action: str = 'save',
         expected_line_revision: int = 0, by: str = '人工操作') -> dict:
    if action not in {'save','remove'} or not reason.strip() or len(reason.strip()) > 500:
        raise WorkspaceError("补录或撤销成本必须填写不超过 500 字的依据")
    if action == 'save':
        amount = _cents(amount)
    else:
        amount = None
    source = next((row for row in _source_rows(ws, run_id)
                   if row['coverage_key'] == coverage_key), None)
    if source is None or source['order_count'] != 1 or source['context_sha'] != context_sha:
        raise WorkspaceError("订单对应关系已变化或存在歧义，请刷新明细后再修改")
    conn = ws.conn
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        active = conn.execute('SELECT id FROM run WHERE store_id=? AND period=? ORDER BY id DESC LIMIT 1',
                              (store_id, period)).fetchone()
        state = conn.execute('SELECT state FROM period WHERE store_id=? AND period=?',
                             (store_id, period)).fetchone()
        if not active or active['id'] != run_id or not state or state['state'] != 'open':
            raise WorkspaceError("计算结果或账期状态已更新，请刷新后重试")
        previous = conn.execute('SELECT id FROM cost_line_log WHERE store_id=? AND period=? '
                                'AND coverage_key=? ORDER BY id DESC LIMIT 1',
                                (store_id, period, coverage_key)).fetchone()
        if (previous['id'] if previous else 0) != expected_line_revision:
            raise WorkspaceError("这笔人工成本已被修改，请刷新后再确认")
        if action == 'remove' and not previous:
            raise WorkspaceError("这笔订单尚无人工补录金额")
        conn.execute('''INSERT INTO cost_line_log(
            store_id,period,coverage_key,context_sha,source_run_id,action,amount,reason,at,by
        ) VALUES(?,?,?,?,?,?,?,?,?,?)''',
          (store_id, period, coverage_key, context_sha, run_id, action,
           str(amount) if amount is not None else None, reason.strip(),
           datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'), by))
    return page(ws, run_id, store_id, period)
