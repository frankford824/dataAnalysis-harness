"""Carry explicit ERP order flags across the live-cost overlay."""
from collections import defaultdict

import polars as pl

from .engine.link import normalize_key
from .model.schema import ColumnBinding


def collect(ingestion, store):
    if store.platform != "pdd":
        return {}, 0
    choices = defaultdict(set)
    for item in ingestion.frames_of("order_cost"):
        frame = item.frame
        if frame is None or not {"internal_order_id", "order_flag"} <= set(frame.columns):
            continue
        if "store_name" in frame.columns:
            frame = frame.filter(pl.col("store_name").is_in([store.name, *store.aliases]))
        for internal, flag in frame.select("internal_order_id", "order_flag").unique().iter_rows():
            internal, flag = normalize_key(internal), normalize_key(flag)
            if internal and flag:
                choices[internal].add(flag)
    flags = {key: next(iter(values)) for key, values in choices.items() if len(values) == 1}
    conflicts = sum(len(values) > 1 for values in choices.values())
    return flags, conflicts


def apply(ingestion, store, cost, orders):
    if store.platform != "pdd":
        return
    flags, conflicts = collect(ingestion, store)
    if conflicts:
        cost.notes.append(f"有 {conflicts} 张内部订单的旗帜记录不一致，未据此排除成本")
    if "order_flag" not in cost.frame.columns:
        cost.frame = cost.frame.with_columns(pl.col("internal_order_id").cast(pl.Utf8).replace_strict(
            flags, default=None, return_dtype=pl.Utf8).alias("order_flag"))
    blue = cost.frame.filter(pl.col("order_flag") == "蓝色旗帜")
    if blue.height:
        cost.notes.append(f"蓝旗订单商品成本不计，共 {blue.height} 条商品记录；原数量和单价保留")
    # A merged group can contain genuine items alongside blue-flag items.
    # Only a wholly excluded group is exempt from the cost coverage check.
    if "order_flag" not in orders.frame.columns:
        coverage = cost.frame.group_by("order_id").agg(
            (pl.col("order_flag") == "蓝色旗帜").fill_null(False).all().alias("__all_blue"))
        orders.frame = orders.frame.join(coverage, on="order_id", how="left").with_columns(
            pl.when(pl.col("__all_blue").fill_null(False)).then(pl.lit("蓝色旗帜"))
            .otherwise(pl.lit(None, dtype=pl.Utf8)).alias("order_flag")
        ).drop("__all_blue")
    for item in (cost, orders):
        template = getattr(item, "template", None)
        if template is not None and not any(binding.role == "order_flag" for binding in template.bindings):
            item.template = template.model_copy(update={"bindings": template.bindings + (
                ColumnBinding(role="order_flag", columns=("order_flag",), required=False),)})
