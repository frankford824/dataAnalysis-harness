"""Remove confirmed pre-shipment cancelled quantities from their exact split cost line."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import polars as pl

from .link import normalize_key
from .rules import norm_expr
from .types import ANCHOR_FILE, ANCHOR_ROW, ANCHOR_SHA, ANCHOR_SHEET

LOCAL = timezone(timedelta(hours=8))


def timestamp(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or '').strip()
        if ':' not in text:
            return None  # A date alone cannot establish before/after shipping.
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
            for fmt in ('%Y/%m/%d %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if parsed is None:
                return None
    return parsed.replace(tzinfo=LOCAL) if parsed.tzinfo is None else parsed.astimezone(LOCAL)


def decimal(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def apply(cost, afters):
    frame = cost.frame
    required = {'internal_order_id','order_id','sub_order_id','sku','ship_time','quantity','unit_cost'}
    if frame is None or not required <= set(frame.columns):
        return []
    selected = required | {"order_type", "parent_internal_order_id", "source_note", "total_cost", ANCHOR_ROW}
    rows = frame.select([c for c in frame.columns if c in selected]).to_dicts()
    index = defaultdict(list)
    for i, row in enumerate(rows):
        if row.get('order_type') == '补发订单':
            continue
        key = tuple(normalize_key(row.get(k)) for k in ('order_id','sub_order_id','sku'))
        qty, unit = decimal(row['quantity']), decimal(row['unit_cost'])
        if all(key) and timestamp(row['ship_time']) and qty is not None and qty > 0 and unit is not None and unit >= 0:
            index[key].append(i)
    if not index:
        return []
    mains = {key[0] for key in index}
    sources, uploaded, feed_times = [], set(), {}
    for source in afters:
        data = source.frame
        if data is None or not {'after_sale_id','order_id','internal_order_id','sub_order_id','sku','quantity','refund_status','goods_status'} <= set(data.columns):
            continue
        data = data.with_row_index('__preship_row').filter(norm_expr(pl.col('order_id').cast(pl.Utf8)).is_in(mains))
        records = data.to_dicts()
        is_feed = source.template.id.startswith('order_console_')
        for row in records:
            event = (normalize_key(row['after_sale_id']), normalize_key(row['order_id']))
            if not is_feed:
                uploaded.add(event)
            elif row.get('refund_status') == '退款成功':
                when = timestamp(row.get('refund_confirm_time'))
                if when:
                    feed_times[event] = max(when, feed_times.get(event, when))
        sources.append((source, records, is_feed))
    events, conflicts = {}, set()
    for source, records, is_feed in sources:
        for row in records:
            if row.get('__preship_handled') or row['refund_status'] != '退款成功' or row['goods_status'] != '买家未收到货':
                continue
            event = (normalize_key(row['after_sale_id']), normalize_key(row['order_id']))
            if not event[0] or (is_feed and event in uploaded):
                continue
            when = timestamp(row.get('refund_confirm_time')) or feed_times.get(event)
            quantity = decimal(row['quantity'])
            key = tuple(normalize_key(row.get(k)) for k in ('order_id','sub_order_id','sku'))
            internal = normalize_key(row['internal_order_id'])
            if not when or quantity is None or quantity <= 0 or not all((*key, internal)):
                continue
            candidates = [i for i in index.get(key, [])
                          if internal in (normalize_key(rows[i]['internal_order_id']), normalize_key(rows[i].get('parent_internal_order_id')))
                          and when < timestamp(rows[i]['ship_time'])]
            if len(candidates) != 1:
                continue  # Do not spread a cancellation over unrelated/reused order lines.
            identity = (*event, internal, key[1], key[2])
            if identity in conflicts:
                continue
            old = events.get(identity)
            if old and (old['when'] != when or old['quantity'] != quantity or old['cost_row'] != candidates[0]):
                conflicts.add(identity);events.pop(identity, None)
                continue
            if old:
                old['sources'].append((source, row['__preship_row']))
            else:
                events[identity] = {'when':when,'quantity':quantity,'cost_row':candidates[0],
                                    'evidence':row,'sources':[(source,row['__preship_row'])]}
    quantities = [decimal(row['quantity']) for row in rows]
    notes = [row.get('source_note') for row in rows]
    handled = defaultdict(set)
    same_internal = defaultdict(set)
    adjustments = []
    for identity, event in sorted(events.items(), key=lambda x:(x[1]['when'],x[0])):
        i = event['cost_row'];row = rows[i]
        old = quantities[i];cancelled = min(old, event['quantity'])
        quantities[i] -= cancelled
        for source, n in event['sources']:
            handled[id(source)].add(n)
            if identity[2] == normalize_key(row['internal_order_id']):
                same_internal[id(source)].add(n)
        if cancelled == 0:
            continue
        unit = decimal(row['unit_cost']);evidence = event['evidence']
        adjustments.append({'order_id':identity[1], 'after_sale_id':identity[0],
                            'after_internal_order_id':identity[2], 'internal_order_id':row['internal_order_id'],
                            'sub_order_id':row['sub_order_id'],'sku':row['sku'],
                            'cancelled_quantity':str(cancelled),'remaining_quantity':str(quantities[i]),
                            'removed_cost':str(unit*cancelled), 'refund_confirm_time':event['when'].isoformat(),
                            'ship_time':str(row['ship_time']),
                            'after_file':evidence.get(ANCHOR_FILE),'after_row':evidence.get(ANCHOR_ROW),
                            'after_sha':evidence.get(ANCHOR_SHA),'cost_row':row.get(ANCHOR_ROW)})
        detail = f"发货前退款，售后单{identity[0]}：不计成本数量{cancelled}，保留数量{quantities[i]}"
        notes[i] = (notes[i]+'；' if notes[i] else '')+detail
    if adjustments:
        cost.frame = frame.with_columns(pl.Series('quantity',[float(q) if q is not None else None for q in quantities],dtype=pl.Float64),
                                        pl.Series('source_note',notes,dtype=pl.Utf8))
        if 'total_cost' in frame.columns:
            cost.frame = cost.frame.with_columns(pl.Series('total_cost',[float(quantities[i]*decimal(row['unit_cost'])) if quantities[i] != decimal(row['quantity']) else row['total_cost'] for i,row in enumerate(rows)],dtype=pl.Float64))
        cost.notes.append(f"按退款确认时间和拆单商品对应关系，剔除{len(adjustments)}行发货前退款成本")
    for source, _, _ in sources:
        marked = handled.get(id(source))
        if marked:
            previous = source.frame['__preship_handled'].to_list() if '__preship_handled' in source.frame.columns else [False]*source.frame.height
            old_skip = source.frame['__preship_skip_exclusion'].to_list() if '__preship_skip_exclusion' in source.frame.columns else [False]*source.frame.height
            source.frame = source.frame.with_columns(
                pl.Series('__preship_handled',[bool(v) or i in marked for i,v in enumerate(previous)]),
                pl.Series('__preship_skip_exclusion',[bool(v) or i in same_internal[id(source)] for i,v in enumerate(old_skip)]))
    cost.preship_adjustments = adjustments
    return adjustments
