"""Carry explicit ERP order flags across the live-cost overlay."""
from collections import defaultdict

import polars as pl

from .engine.link import normalize_key
from .engine.rules import norm_expr
from .model.schema import ColumnBinding


def collect(ingestion, store):
    choices = defaultdict(set)
    for item in ingestion.frames_of("order_cost"):
        frame = item.frame
        if frame is None or not {"internal_order_id", "order_flag"} <= set(frame.columns):
            continue
        owners=[pl.col(c).cast(pl.Utf8).str.strip_chars().replace('',None)
                for c in ('store_name','__hint_store__') if c in frame.columns]
        if not owners:
            continue  # An internal order number alone does not prove ownership.
        frame=frame.filter(pl.coalesce(owners).is_in([store.name,*store.aliases]))
        for internal, flag in frame.select("internal_order_id", "order_flag").unique().iter_rows():
            internal, flag = normalize_key(internal), normalize_key(flag)
            if internal and flag:
                choices[internal].add(flag)
    flags = {key: next(iter(values)) for key, values in choices.items() if len(values) == 1}
    conflicts = sum(len(values) > 1 for values in choices.values())
    return flags, conflicts


def fill_missing(frame, key, flags):
    """Fill absent/blank flags, never overwrite a live flag or mixed-order evidence."""
    live=(pl.col('order_flag').cast(pl.Utf8).str.strip_chars().replace('',None)
          if 'order_flag' in frame.columns else pl.lit(None,dtype=pl.Utf8))
    frame=frame.with_columns(live.alias('order_flag'))
    if key not in frame.columns or not flags or frame.is_empty():
        return frame
    identity=norm_expr(pl.col(key).cast(pl.Utf8))
    proposed=identity.replace_strict(flags,default=None,return_dtype=pl.Utf8)
    conflict=((pl.col('order_flag').is_not_null()) & proposed.is_not_null()
              & (pl.col('order_flag')!=proposed)).fill_null(False).any().over(identity)
    fill=pl.col('order_flag').is_null() & proposed.is_not_null() & ~conflict
    prior=pl.col('__flag_from_upload').fill_null(False) if '__flag_from_upload' in frame.columns else pl.lit(False)
    return frame.with_columns(pl.when(fill).then(proposed).otherwise(pl.col('order_flag')).alias('order_flag'),
                              (prior|fill).alias('__flag_from_upload'))


def apply(ingestion, store, cost, orders):
    flags, conflicts = collect(ingestion, store)
    if conflicts:
        cost.notes.append(f"有 {conflicts} 张内部订单的旗帜记录不一致，未据此排除成本")
    cost.frame=fill_missing(cost.frame,'internal_order_id',flags)
    if '__flag_from_upload' in cost.frame.columns:
        amount=int(cost.frame['__flag_from_upload'].sum() or 0)
        if amount:cost.notes.append(f'订单台旗帜缺失：按同店且无冲突的内部订单原表证据补全 {amount} 行，仍须同时满足卖家备注 by 规则才不计成本')
    # A merged group can contain items with different flags.  Only propagate a
    # flag when every line agrees; the cost rule combines it with seller remarks.
    coverage = cost.frame.group_by("order_id").agg(
        (pl.col("order_flag") == "蓝色旗帜").fill_null(False).all().alias("__all_blue"))
    all_blue={str(key):'蓝色旗帜' for key in coverage.filter(pl.col('__all_blue'))['order_id'].drop_nulls().to_list()}
    orders.frame=fill_missing(orders.frame,'order_id',all_blue)
    for item in (cost, orders):
        template = getattr(item, "template", None)
        if template is not None and not any(binding.role == "order_flag" for binding in template.bindings):
            item.template = template.model_copy(update={"bindings": template.bindings + (
                ColumnBinding(role="order_flag", columns=("order_flag",), required=False),)})
