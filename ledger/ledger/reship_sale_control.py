"""Use a proved original sale only as a replacement-quantity plausibility check."""
from decimal import Decimal, InvalidOperation
import re
import polars as pl
from .cost_input_evidence import verified_quantity_input, quantity


SCHEMA={'order_id':pl.String,'sub_order_id':pl.String,'__original_sale_control':pl.Float64,'__original_sale_control_id':pl.String}


def original_sale_controls(candidates, orders, items, relations):
    if candidates.is_empty() or not {'relation_type','source_order_id','target_order_id'}<=set(relations.columns):
        return pl.DataFrame(schema=SCHEMA)
    targets=set(candidates['order_id'].cast(pl.String).to_list())
    links={}
    for r in relations.filter(pl.col('relation_type')=='reship').select('source_order_id','target_order_id').iter_rows(named=True):
        source,target=str(r['source_order_id'] or ''),str(r['target_order_id'] or '')
        if target in targets and source and source!=target:links.setdefault(target,set()).add(source)
    roots={t:next(iter(v)) for t,v in links.items() if len(v)==1}
    ids=targets|set(roots.values())
    headers={str(r['order_id']):r for r in orders.filter(pl.col('order_id').cast(pl.String).is_in(ids)).iter_rows(named=True)}
    lines=items.filter(pl.col('order_id').cast(pl.String).is_in(ids)).to_dicts()
    current={(str(r['order_id']),str(r['sub_order_id'])):r for r in lines}
    out=[]
    for r in candidates.iter_rows(named=True):
        target=str(r['order_id']);source=roots.get(target);old=headers.get(source);new=headers.get(target)
        if not old or not new or not verified_quantity_input(r):continue
        if old.get('order_type') not in {'普通订单','销售订单'} or old.get('order_status_raw')!='Sent':continue
        shop=str(old.get('order_store_id') or '')
        if not shop or shop!=str(new.get('order_store_id') or ''):continue
        master=str(old.get('online_order_no') or '').strip()
        if not re.fullmatch(r'\d{19}',master):continue
        item=current.get((target,str(r['sub_order_id'])),{})
        if str(item.get('online_order_no') or '') not in {master,shop+':'+master}:continue
        original=[v for v in lines if str(v['order_id'])==source and v.get('sku_id')==r['sku_id']]
        quantities=[quantity(v.get('quantity')) for v in original]
        if not quantities or any(v is None for v in quantities) or any(v.get('is_suspect') for v in original):continue
        if sum(quantities)<quantity(r['quantity']):continue
        try:amount=Decimal(str(old.get('paid_amount')))
        except (InvalidOperation,ValueError):continue
        if not amount.is_finite() or amount<=0:continue
        out.append({'order_id':target,'sub_order_id':str(r['sub_order_id']),
                    '__original_sale_control':float(amount),'__original_sale_control_id':source})
    return pl.from_dicts(out,schema=SCHEMA) if out else pl.DataFrame(schema=SCHEMA)
