"""Actual returned quantities reverse the original sale cost in the receipt month."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import polars as pl

from .link import normalize_key
from .rules import norm_expr
from .predicate import compile_where
from .types import ANCHOR_FILE, ANCHOR_ROW, ANCHOR_SHA, ANCHOR_SHEET, FileRef, Recognition
from ..model.schema import ColumnBinding, Template

SOURCE = "cost_return"
KEYS = ("internal_order_id", "sub_order_id", "sku")


def policy_mask(frame, model):
    names = [name for store in model.stores if store.cost_return_posting == "transaction"
             for name in (store.name, *store.aliases)]
    if not names or "store_name" not in frame.columns:
        return pl.lit(False)
    return pl.col("store_name").is_in(names).fill_null(False)


def number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).strip().replace("/", "-")).date()
    except ValueError:
        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(str(value).strip(), fmt).date()
            except ValueError:
                continue
        return None


def build(ingestion, platform, *, spine=None):
    """Only source-backed receipts; cap quantities across duplicate/repeated returns.

    The uploaded ERP export has per-product actual receipts. An Order Console
    event naming only one representative item cannot replace those product rows.
    """
    from .runtime import Ingested
    model = ingestion.model
    if not any(s.cost_return_posting == "transaction" for s in model.stores):
        return None, []
    if not any(m.id == "goods_return_cost" for m in model.metrics):
        raise ValueError("按退货月份记账缺少退货成本冲回指标")
    metric = model.metric("goods_cost").for_platform(platform)
    if metric is None:
        return None, []
    # Join identity includes the canonical ledger store. Identical ERP or
    # platform numbers in two stores never share a price, receipt or cap.
    stores=[store for store in model.stores if platform=='*' or store.platform==platform]
    from .ownership import owner_context
    names={store.id:store.name for store in stores}
    enabled={store.id for store in stores if store.cost_return_posting=='transaction'}
    contexts={id(item):owner_context(item.frame,item.ref,model,platform) for item in ingestion.known}
    after_items=[item for item in ingestion.frames_of('after_sales') if set(KEYS)<=set(item.frame.columns)]
    demanded={normalize_key(v) for item in after_items for v in item.frame['internal_order_id'].drop_nulls().to_list()}
    if not demanded:return None,[]
    costs = defaultdict(list);unknown_cost_keys=set()
    require_history=any(p.id==platform and p.cost_pricing=='historical' for p in model.platforms)
    for item in ingestion.frames_of("order_cost"):
        if not set(KEYS) <= set(item.frame.columns):
            continue
        wanted=item.frame.filter(norm_expr(pl.col('internal_order_id').cast(pl.String)).is_in(demanded))
        owner_expr=contexts[id(item)].expression(wanted)
        wanted=wanted.with_columns(owner_expr.alias('__return_store_id__'))
        for row in wanted.filter(pl.col('__return_store_id__').is_null()).select(KEYS).iter_rows(named=True):
            key=tuple(normalize_key(row.get(k)) for k in KEYS)
            if all(key):unknown_cost_keys.add(key)
        eligible = wanted.filter(pl.col('__return_store_id__').is_in(enabled))
        from .cost_policy import is_brushing, is_dropship, is_policy_zero_sku
        # This internal template is restored only after its frozen run's
        # nonzero counted goods-cost/member archive has been independently
        # proved. New sale-admission rules cannot rewrite that old basis.
        # Actual return evidence, quantity caps and receipt dates below remain
        # the same original return engine.
        if item.template.id!='jst_source_cost_frozen_v3':
            eligible = eligible.filter(~is_dropship(eligible))
            eligible = eligible.filter(~is_brushing(eligible))
            eligible = eligible.filter(~is_policy_zero_sku(eligible))
            eligible = eligible.filter(compile_where(metric.where, eligible))
        if require_history:
            from .link import link
            from .calculate import historical_price_evidence
            if spine is not None:
                from .link import Spine,SPINE_STORE
                parts=[]
                for (sid,),part in eligible.partition_by('__return_store_id__',as_dict=True).items():
                    scoped=spine.frame.filter(pl.col(SPINE_STORE)==names[sid]) if SPINE_STORE in spine.frame.columns else spine.frame.clear()
                    linked,_=link(part,metric,Spine(scoped),item.template);parts.append(linked)
                if parts:eligible=pl.concat(parts,how='diagonal_relaxed')
            if item.template.id=='jst_source_cost_frozen_v3':
                # Frozen member quantity/price and its original native day
                # have already been proved against nonzero counted evidence.
                # Reinterpreting today's quote/source policy would rewrite the
                # old basis (including older valid feed quotes with {} JSON).
                valid=pl.lit(True)
                original_day=pl.col('__frozen_original_day__').str.strptime(pl.Date,'%Y-%m-%d',strict=False)
            else:
                valid,original_day=historical_price_evidence(eligible)
            eligible=eligible.with_columns(valid.alias('__return_price_verified'),original_day.alias('__return_order_date'))
        for row in eligible.iter_rows(named=True):
            key = (*[normalize_key(row.get(k)) for k in KEYS], normalize_key(row.get("internal_sub_order_id")),row['__return_store_id__'])
            quantity, unit = number(row.get("quantity")), number(row.get("unit_cost"))
            if all(key[:3]) and quantity is not None and quantity > 0:
                if not require_history:row['__return_price_verified']=unit is not None and unit>=0
                costs[key].append(row)
    if not costs and not unknown_cost_keys:
        return None, []
    by_base = defaultdict(list);by_raw_owners=defaultdict(set)
    for key in costs:
        by_base[(*key[:3],key[4])].append(key)
        by_raw_owners[key[:3]].add(key[4])
    uploaded = set()
    for item in ingestion.frames_of("after_sales"):
        if item.template.id.startswith("order_console_"):
            continue
        if not {'after_sale_id','internal_order_id'}<=set(item.frame.columns):continue
        for row in item.frame.to_dicts():
            sid=contexts[id(item)].owner(row)
            if sid:uploaded.add((sid,normalize_key(row['after_sale_id']),normalize_key(row['internal_order_id'])))
    events, errors, conflicted = {}, [], set()
    for item in ingestion.frames_of("after_sales"):
        if not set(KEYS) <= set(item.frame.columns):
            continue
        relevant = item.frame.filter(norm_expr(pl.col("internal_order_id").cast(pl.Utf8)).is_in({k[0] for k in costs}|{k[0] for k in unknown_cost_keys}))
        for row in relevant.iter_rows(named=True):
            if row.get("__preship_handled"):
                continue
            base_key = tuple(normalize_key(row.get(k)) for k in KEYS)
            if row.get("refund_status") != "退款成功" or row.get("goods_status") not in ("买家未收到货", "买家已退货", "卖家已收到退货"):
                continue
            if number(row.get("returned_quantity"))==0:continue
            event = normalize_key(row.get("after_sale_id"))
            sid=contexts[id(item)].owner(row)
            candidate_owners=by_raw_owners.get(base_key,set())
            if sid is None:
                if candidate_owners or base_key in unknown_cost_keys:
                    errors.append(f"售后单{event or '未填单号'}店铺归属缺失、歧义或未配置，未按相同订单号套用其他店成本")
                continue
            if sid not in enabled:continue
            if base_key in unknown_cost_keys:
                errors.append(f"售后单{event or '未填单号'}原成本店铺归属尚未核清，未采用编号相同的未归属成本")
                continue
            options = by_base.get((*base_key,sid), [])
            if not options:
                if candidate_owners:
                    errors.append(f"售后单{event or '未填单号'}与原成本店铺归属冲突，未跨店冲回成本")
                continue
            item_id = normalize_key(row.get("internal_sub_order_id"))
            key = (*base_key, item_id,sid) if item_id else options[0] if len(options) == 1 else None
            if item.template.id.startswith("order_console_") and (sid,event,base_key[0]) in uploaded:
                continue
            if key not in costs:
                errors.append(f"售后单{event} 商品{base_key[2]}缺少唯一的内部商品行对应关系")
                continue
            if row.get("__component_return_pending"):
                errors.append(f"售后单{event} 组合商品{key[2]}的组件清单不完整或实退数量不一致")
                continue
            quantity = number(row.get("returned_quantity"))
            if quantity == 0:
                continue
            when = day(row.get("settle_date")) or day(row.get("return_time")) or day(row.get("received_time"))
            if not event or quantity is None or quantity < 0 or when is None:
                errors.append(f"售后单{event or '未填单号'} 商品{row.get('original_sku') or key[2]}缺少有效实退数量或进仓日期")
                continue
            sold_dates = [day(c.get('__return_order_date')) if require_history else day(c.get("order_time")) or day(c.get("order_date")) for c in costs[key]]
            verified=all(c.get('__return_price_verified') for c in costs[key])
            if any((d is None and verified) or (d is not None and when<d) for d in sold_dates):
                errors.append(f"售后单{event}的进仓日期早于销售日期或销售日期缺失")
                continue
            identity = (event, *key)
            if identity in conflicted:
                continue
            previous = events.get(identity)
            if previous and (previous[0] != when or previous[1] != quantity):
                errors.append(f"售后单{event} 商品{key[2]}的实退数量或进仓日期不一致")
                conflicted.add(identity)
                events.pop(identity, None)
                continue
            events[identity] = (when, quantity, row)
    used = defaultdict(lambda: Decimal(0))
    generated = []
    pending_prices=[]
    for identity, (when, requested, evidence) in sorted(events.items(), key=lambda pair: (pair[1][0], pair[0])):
        key = identity[1:]
        original = costs[key]
        sold_quantity = sum(number(c["quantity"]) for c in original)
        quantity = min(requested, max(Decimal(0), sold_quantity - used[key]))
        if quantity == 0:
            continue
        used[key] += quantity
        if not all(c.get('__return_price_verified') for c in original):
            first=original[0];dates={day(c.get('__return_order_date')) if require_history else day(c.get('order_time')) or day(c.get('order_date')) for c in original}
            sale_day=next(iter(dates)) if len(dates)==1 else None
            pending_prices.append({'store':names[key[4]],
                'period':when.strftime('%Y-%m'),'metric_id':'goods_return_cost','order_id':first.get('original_order_id') or first.get('order_id'),
                'order_date':sale_day.isoformat() if sale_day else None,'internal_order_id':key[0],'sku':key[2],
                'quantity':float(quantity),'reference_unit_cost':float(number(first.get('unit_cost'))) if number(first.get('unit_cost')) is not None else None,
                'reason':'原销售成本尚未核实，退货成本暂不能冲回','file_sha':evidence.get(ANCHOR_SHA),'file_name':evidence.get(ANCHOR_FILE),
                'sheet':evidence.get(ANCHOR_SHEET),'row_no':evidence.get(ANCHOR_ROW)})
            continue
        sold_amount = sum(number(c["quantity"]) * number(c["unit_cost"]) for c in original)
        amount = sold_amount * quantity / sold_quantity
        row = {k: evidence.get(k) for k in (ANCHOR_SHA, ANCHOR_FILE, ANCHOR_SHEET, ANCHOR_ROW)}
        row.update(internal_order_id=key[0], sub_order_id=key[1], sku=key[2],
                   order_id=original[0].get("order_id"), store_name=names[key[4]],
                   after_sale_id=identity[0], returned_quantity=float(quantity),
                   return_time=when, settle_date=when, total_cost=float(amount),
                   source_note=f"退货日期：{when}；本次冲回数量：{quantity}；原成本单价：{sold_amount / sold_quantity}；售后单：{identity[0]}；原订单：{original[0].get('order_id')}" + (f"；原表实退数量：{requested}，累计冲回以原销售数量为限" if requested != quantity else "") + ("；"+evidence["component_note"] if evidence.get("component_note") else ""))
        generated.append(row)
    schema = {ANCHOR_SHA:pl.Utf8, ANCHOR_FILE:pl.Utf8, ANCHOR_SHEET:pl.Utf8, ANCHOR_ROW:pl.Int64,
              "internal_order_id":pl.Utf8, "sub_order_id":pl.Utf8, "sku":pl.Utf8, "order_id":pl.Utf8,
              "store_name":pl.Utf8, "after_sale_id":pl.Utf8, "returned_quantity":pl.Float64,
              "return_time":pl.Date, "settle_date":pl.Date, "total_cost":pl.Float64, "source_note":pl.Utf8}
    frame = pl.DataFrame(generated, schema=schema)
    template = Template(id="derived_cost_return_v1", name="退货成本冲回", source=SOURCE,
                        match_columns=("after_sale_id",), time_slots={"settle_date":"return_time"},
                        bindings=tuple(ColumnBinding(role=k, columns=(k,), required=False,
                                      kind="time" if k=="return_time" else "number" if k in ("total_cost","returned_quantity") else "text")
                                       for k in schema if k not in (ANCHOR_SHA,ANCHOR_FILE,ANCHOR_SHEET,ANCHOR_ROW,"settle_date")))
    ref = FileRef("derived-cost-return", "退货成本冲回", "售后明细")
    item = Ingested(ref=ref, frame=frame, template=template, rows=frame.height,
                    recognition=Recognition(ref=ref, signature=template.signature, header_count=len(schema),
                                            source_id=SOURCE, template_id=template.id))
    item.pricing_gaps=pending_prices
    return item, sorted(set(errors))
