"""A superseded ERP container is evidence, not another fulfilment."""
import polars as pl

SUPERSEDED_PARENT_STATES = frozenset({'Split', 'Merged', '被拆分', '被合并'})
SUPERSEDED_COST_NOTE = '原单已被拆分或合并，仅保留追溯，不重复计成本；实际发货单另行核算'


def superseded_parent(frame, column='order_state'):
    if column not in frame.columns:
        return pl.lit(False)
    return pl.col(column).cast(pl.String).str.strip_chars().is_in(sorted(SUPERSEDED_PARENT_STATES)).fill_null(False)


def superseded_cost_fact(frame):
    if not {'metric_id', 'source_note'} <= set(frame.columns):
        return pl.lit(False)
    return (pl.col('metric_id').is_in(['goods_cost', 'reshipment_cost'])
        & pl.col('source_note').str.contains(SUPERSEDED_COST_NOTE, literal=True).fill_null(False))
