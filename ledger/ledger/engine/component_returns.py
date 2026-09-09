"""Convert complete ERP component returns to the priced bundle's unit, without guessing."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
import polars as pl
from .link import normalize_key
from .types import ANCHOR_ROW


def number(value):
    try:
        result=Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def normalize(ingestion, cost, components=None):
    columns={"internal_order_id","internal_sub_order_id","sub_order_id","sku","quantity","store_name"}
    if ingestion.model is None or cost is None or not columns <= set(cost.columns):
        return
    enabled={n for s in ingestion.model.stores if s.cost_return_posting=="transaction" for n in (s.name,*s.aliases)}
    specs={}
    for row in cost.filter(pl.col("store_name").is_in(enabled)).iter_rows(named=True):
        key=(normalize_key(row['internal_order_id']),normalize_key(row['internal_sub_order_id']))
        q=number(row['quantity'])
        if all(key) and q is not None and q>0:
            specs[key]=(normalize_key(row['sub_order_id']),normalize_key(row['sku']),q)
    if not specs:
        return
    parts=defaultdict(lambda:defaultdict(set))
    required={"internal_order_id","internal_sub_order_id","sub_order_id","sku","quantity"}
    for item in ingestion.frames_of('order_cost'):
        if not required <= set(item.frame.columns):
            continue
        for row in item.frame.select(sorted(required)).unique().iter_rows(named=True):
            key=(normalize_key(row['internal_order_id']),normalize_key(row['internal_sub_order_id']))
            if key not in specs or normalize_key(row['sub_order_id'])!=specs[key][0]:
                continue
            parts[key][normalize_key(row['sku'])].add(number(row['quantity']))
    parts={key:values for key,values in parts.items() if len(values)>1}
    evidence={}
    if components is not None:
        verified=defaultdict(lambda:defaultdict(set))
        for row in components.iter_rows(named=True):
            key=(normalize_key(row['internal_order_id']),normalize_key(row['internal_sub_order_id']))
            if key in specs and normalize_key(row['sub_order_id'])==specs[key][0]:
                verified[key][normalize_key(row['sku'])].add(number(row['quantity']))
                evidence[key]=row['component_evidence']
        parts.update(verified)
    if not parts:
        return
    for after in ingestion.frames_of('after_sales'):
        frame=after.frame
        if not {"after_sale_id","internal_order_id","internal_sub_order_id","sku"} <= set(frame.columns):
            continue
        groups=defaultdict(list)
        for row in frame.with_row_index('__component_row').filter(pl.col('internal_order_id').cast(pl.Utf8).is_in({k[0] for k in parts})).iter_rows(named=True):
            key=(normalize_key(row['internal_order_id']),normalize_key(row['internal_sub_order_id']))
            if key in parts and normalize_key(row['sku'])==specs[key][1]:
                groups[(normalize_key(row['after_sale_id']),*key)].append(row)
        if not groups:
            continue
        quantities=frame['quantity'].to_list() if 'quantity' in frame.columns else [None]*frame.height
        returned=frame['returned_quantity'].to_list() if 'returned_quantity' in frame.columns else [None]*frame.height
        raw_quantities=quantities.copy();raw_returned=returned.copy()
        request_pending=[False]*frame.height;return_pending=[False]*frame.height;notes=[None]*frame.height
        for (_,internal,item),rows in groups.items():
            key=(internal,item);expected=parts[key]
            seen={normalize_key(row.get('original_sku') or row['sku']) for row in rows}
            complete=seen==set(expected) and all(len(q)==1 and None not in q and next(iter(q))>0 for q in expected.values())
            def converted(field):
                statuses = all(row.get('refund_status') == '退款成功' and row.get('goods_status') in
                               (('买家未收到货',) if field == 'quantity' else ('买家未收到货','买家已退货','卖家已收到退货')) for row in rows)
                if not complete or not statuses:
                    return None
                values=set()
                for row in rows:
                    q=number(row.get(field));sku=normalize_key(row.get('original_sku') or row['sku'])
                    if q is None or q<0:
                        return None
                    values.add(q*specs[key][2]/next(iter(expected[sku])))
                return next(iter(values)) if len(values)==1 else None
            q,back=converted('quantity'),converted('returned_quantity')
            anchors='、'.join(str(row.get(ANCHOR_ROW,'')) for row in rows)
            for row in rows:
                i=row['__component_row'];quantities[i]=float(q) if q is not None else None;returned[i]=float(back) if back is not None else None
                request_pending[i]=q is None;return_pending[i]=back is None
                notes[i]=f"组合商品数量由完整组件清单换算；售后原表行：{anchors}" + ("；"+evidence[key] if key in evidence else "")
        after.frame=frame.with_columns(pl.Series('quantity',quantities,dtype=pl.Float64),
            pl.Series('returned_quantity',returned,dtype=pl.Float64),
            pl.Series('component_original_quantity',raw_quantities,dtype=pl.Float64),
            pl.Series('component_original_returned_quantity',raw_returned,dtype=pl.Float64),
            pl.Series('__component_request_pending',request_pending),pl.Series('__component_return_pending',return_pending),
            pl.Series('component_note',notes,dtype=pl.Utf8))
