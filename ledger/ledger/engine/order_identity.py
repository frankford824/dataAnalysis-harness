"""Canonical order targets, without deleting financial source rows."""
import polars as pl


def canonical_exports(frame):
    """Collapse only fully identical, identified platform child-order snapshots.

    No master-only exports, ERP shipment rows, or conflicting snapshots are
    discarded. All normalized fields must agree, not merely the child number.
    """
    keys=('store','period','order_id','sub_order_id','product_id')
    if not {*keys,'__spine_origin__'}<=set(frame.columns) or frame.is_empty():
        return frame,0
    valid=pl.col('__spine_origin__').eq('order_detail_file').fill_null(False)
    for key in keys:
        valid &= pl.col(key).is_not_null() & (pl.col(key).cast(pl.String).str.strip_chars()!='')
    indexed=frame.with_row_index('__canonical_position')
    candidates=indexed.filter(valid)
    kept=candidates.unique(subset=frame.columns,keep='first',maintain_order=True)
    removed=candidates.height-kept.height
    if not removed:return frame,0
    result=pl.concat([indexed.filter(~valid.fill_null(False)),kept],how='vertical_relaxed')
    return result.sort('__canonical_position').drop('__canonical_position'),removed


def prepare_direct(keyed):
    """A direct source amount has one target, never one full copy per match."""
    conflict=pl.lit(False)
    for name in ('store','period'):
        if name in keyed.columns:
            conflict |= pl.col(name).n_unique().over('link_key')!=1
    reason=(pl.when(conflict).then(pl.lit('cross_store_or_period'))
            .when(pl.len().over('link_key')>1).then(pl.lit('direct_order_identity_conflict'))
            .otherwise(pl.lit('')))
    return keyed.with_columns(reason.alias('__allocation_reason'),
        pl.when(reason=='').then(1.).otherwise(None).alias('__allocation_factor'),
        pl.when(reason=='').then(pl.lit('direct_single_target')).otherwise(None).alias('allocation_method'),
        pl.when(reason=='').then(1).otherwise(None).alias('allocation_child_count'))
