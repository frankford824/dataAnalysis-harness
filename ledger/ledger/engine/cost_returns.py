"""Actual returned quantities reverse the original sale cost in the receipt month."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import polars as pl

from .link import normalize_key
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


def build(ingestion, platform):
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
    costs = defaultdict(list)
    for item in ingestion.frames_of("order_cost"):
        if not set(KEYS) <= set(item.frame.columns):
            continue
        eligible = item.frame.filter(policy_mask(item.frame, model))
        eligible = eligible.filter(compile_where(metric.where, eligible))
        for row in eligible.iter_rows(named=True):
            key = (*[normalize_key(row.get(k)) for k in KEYS], normalize_key(row.get("internal_sub_order_id")))
            quantity, unit = number(row.get("quantity")), number(row.get("unit_cost"))
            if all(key[:3]) and quantity is not None and quantity > 0 and unit is not None and unit >= 0:
                costs[key].append(row)
    if not costs:
        return None, []
    by_base = defaultdict(list)
    for key in costs:
        by_base[key[:3]].append(key)
    uploaded = set()
    for item in ingestion.frames_of("after_sales"):
        if item.template.id.startswith("order_console_"):
            continue
        for row in item.frame.select("after_sale_id", "internal_order_id").unique().iter_rows():
            uploaded.add(tuple(map(normalize_key, row)))
    events, errors, conflicted = {}, [], set()
    for item in ingestion.frames_of("after_sales"):
        if not set(KEYS) <= set(item.frame.columns):
            continue
        relevant = item.frame.filter(pl.col("internal_order_id").cast(pl.Utf8).is_in({k[0] for k in costs}))
        for row in relevant.iter_rows(named=True):
            if row.get("__preship_handled"):
                continue
            base_key = tuple(normalize_key(row.get(k)) for k in KEYS)
            options = by_base.get(base_key, [])
            item_id = normalize_key(row.get("internal_sub_order_id"))
            key = (*base_key, item_id) if item_id else options[0] if len(options) == 1 else None
            if not options or row.get("refund_status") != "退款成功":
                continue
            if row.get("goods_status") not in ("买家未收到货", "买家已退货", "卖家已收到退货"):
                continue
            event = normalize_key(row.get("after_sale_id"))
            if item.template.id.startswith("order_console_") and (event, base_key[0]) in uploaded:
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
            sold_dates = [day(c.get("order_time")) or day(c.get("order_date")) for c in costs[key]]
            if any(d is None or when < d for d in sold_dates):
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
    for identity, (when, requested, evidence) in sorted(events.items(), key=lambda pair: (pair[1][0], pair[0])):
        key = identity[1:]
        original = costs[key]
        sold_quantity = sum(number(c["quantity"]) for c in original)
        sold_amount = sum(number(c["quantity"]) * number(c["unit_cost"]) for c in original)
        quantity = min(requested, max(Decimal(0), sold_quantity - used[key]))
        if quantity == 0:
            continue
        used[key] += quantity
        amount = sold_amount * quantity / sold_quantity
        row = {k: evidence.get(k) for k in (ANCHOR_SHA, ANCHOR_FILE, ANCHOR_SHEET, ANCHOR_ROW)}
        row.update(internal_order_id=key[0], sub_order_id=key[1], sku=key[2],
                   order_id=original[0].get("order_id"), store_name=original[0].get("store_name"),
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
    return item, sorted(set(errors))
