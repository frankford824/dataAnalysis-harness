"""Supplement refund dates from archived, store-scoped original order evidence."""
from __future__ import annotations
from datetime import datetime
import polars as pl
from .types import ANCHOR_FILE, ANCHOR_ROW
from .link import LINK_KEY


def lookup(items, store_names):
    found = {}
    for item in items:
        if item.template is None or item.template.source != 'refund_order_dates':
            continue
        for row in item.frame.iter_rows(named=True):
            store = store_names.get(str(row.get('store_name') or ''))
            key = str(row.get('order_id') or '').strip()
            day = row.get('order_time')
            if isinstance(day, str):
                try:
                    day = datetime.fromisoformat(day.strip())
                except ValueError:
                    day = None
            if not store or not key or not isinstance(day, datetime):
                continue
            if day.tzinfo:
                from zoneinfo import ZoneInfo
                day = day.astimezone(ZoneInfo('Asia/Shanghai'))
            proof = f"原订单日期依据：{row.get(ANCHOR_FILE) or item.ref.label()} 第{row.get(ANCHOR_ROW)}行；聚水潭订单：{row.get('internal_order_id') or ''}"
            found.setdefault((store,key), {}).setdefault(day.strftime('%Y-%m-%d'), proof)
    return {key: (next(iter(days))[:7], next(iter(days.values()))) for key,days in found.items() if len(days)==1}


def apply(frame, references, store_names, store_hint=''):
    if not references or LINK_KEY not in frame.columns:
        return frame
    def resolve(row):
        if row.get('__spine_period__'):
            return None
        store = row.get('__spine_store__') or store_names.get(str(row.get('store_name') or '')) or store_names.get(str(row.get('__hint_store__') or store_hint))
        return references.get((store,str(row.get(LINK_KEY) or '').strip()))
    matches=[resolve(row) for row in frame.iter_rows(named=True)]
    periods=pl.Series('__refund_reference_period',[r[0] if r else None for r in matches],dtype=pl.Utf8)
    notes=pl.Series('__refund_reference_note',[r[1] if r else None for r in matches],dtype=pl.Utf8)
    frame=frame.with_columns(periods,notes)
    old=pl.col('__spine_period__') if '__spine_period__' in frame.columns else pl.lit(None,dtype=pl.Utf8)
    note=pl.col('source_note') if 'source_note' in frame.columns else pl.lit(None,dtype=pl.Utf8)
    return frame.with_columns(pl.coalesce(old,pl.col('__refund_reference_period')).alias('__spine_period__'),
        pl.concat_str([note,pl.col('__refund_reference_note')],separator='；',ignore_nulls=True).alias('source_note')).drop('__refund_reference_period','__refund_reference_note')
