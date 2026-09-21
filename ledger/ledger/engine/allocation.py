"""Evidence-based order allocation. Missing monetary inputs are not zero.

Platform rows retain their identities. The order feed may only fill missing
amounts on an exact store/master/child match; it never adds duplicate children
to the allocation denominator.
"""
import polars as pl

ORIGIN='__spine_origin__'


def refund_values(frame):
    if 'refund_amount' not in frame.columns:return frame
    text=pl.col('refund_amount').cast(pl.Utf8).str.strip_chars()
    absent=text.is_in(['无退款申请','没有申请退款','未退款','无']).fill_null(False)
    if 'refund_status' in frame.columns:
        absent |= pl.col('refund_amount').is_null() & pl.col('refund_status').is_in(['没有申请退款','无退款申请','未退款']).fill_null(False)
    return frame.with_columns(pl.when(absent).then(pl.lit(0.)).otherwise(pl.col('refund_amount').cast(pl.Float64,strict=False)).alias('refund_amount'))


def enrich(frame):
    frame=refund_values(frame)
    keys=[c for c in ('store','order_id','sub_order_id') if c in frame.columns]
    if len(keys)!=3 or ORIGIN not in frame.columns or 'buyer_paid' not in frame.columns:
        return frame
    source=frame.filter(pl.col(ORIGIN)=='order_console')
    if 'order_type' in source.columns:
        source=source.filter((pl.col('order_type')!='补发订单').fill_null(True))
    if source.is_empty():return frame
    if 'refund_amount' not in frame.columns:
        return frame
    source=source.with_columns(*[pl.col(c).cast(pl.Float64,strict=False).alias(c) for c in ('buyer_paid','refund_amount')])
    proof=source.group_by(keys).agg(
        pl.col('buyer_paid').first().alias('__feed_paid'),pl.col('refund_amount').first().alias('__feed_refund'),
        ((pl.col('buyer_paid').is_not_null() & pl.col('buyer_paid').is_finite() & (pl.col('buyer_paid')>=0)).all()
         & (pl.col('buyer_paid').n_unique()==1)
         & (pl.col('refund_amount').is_not_null() & pl.col('refund_amount').is_finite() & (pl.col('refund_amount')>=0)).all()
         & (pl.col('refund_amount').n_unique()==1)).alias('__feed_valid'),
        *([pl.col('product_id').drop_nulls().unique().alias('__feed_products')] if 'product_id' in source.columns else []))
    result=frame.join(proof,on=keys,how='left',maintain_order='left')
    usable=(pl.col(ORIGIN)=='order_detail_file') & pl.col('__feed_valid').fill_null(False)
    if '__feed_products' in result.columns:
        usable &= pl.col('__feed_products').list.contains(pl.col('product_id')).fill_null(False)
    missing=pl.col('buyer_paid').cast(pl.Float64,strict=False).is_null()
    supplement=usable & missing
    return result.with_columns(
        pl.when(supplement).then(pl.col('__feed_paid')).otherwise(pl.col('buyer_paid').cast(pl.Float64,strict=False)).alias('buyer_paid'),
        pl.when(supplement & pl.col('refund_amount').cast(pl.Float64,strict=False).is_null()).then(pl.col('__feed_refund'))
          .otherwise(pl.col('refund_amount').cast(pl.Float64,strict=False)).alias('refund_amount'),
        pl.when(supplement).then(pl.lit('exact_order_feed_match')).otherwise(
            pl.col('allocation_basis_source').fill_null('original_source') if 'allocation_basis_source' in result.columns else pl.lit('original_source')).alias('allocation_basis_source')
    ).drop([c for c in result.columns if c.startswith('__feed_')])


def enrich_order_table(frame,feed,model):
    """Run before the feed drops already-uploaded child identities."""
    from .rules import norm_expr
    required={'order_id','sub_order_id','product_id'}
    if not required<=set(frame.columns) or not (required|{'store_name','buyer_paid','refund_amount'})<=set(feed.columns):
        return frame
    names={name:s.name for s in model.stores for name in (s.id,s.name,*s.aliases)} if model else {}
    def project(data,origin):
        store=pl.coalesce([pl.col(c).cast(pl.Utf8) for c in ('store_name','__hint_store__') if c in data.columns]) if any(c in data.columns for c in ('store_name','__hint_store__')) else pl.lit(None,dtype=pl.Utf8)
        return data.select(store.replace_strict(names,default=store).alias('store'),
            *[norm_expr(pl.col(c).cast(pl.Utf8)).alias(c) for c in ('order_id','sub_order_id','product_id')],
            *[(pl.col(c).cast(pl.Utf8) if c in data.columns else pl.lit(None,dtype=pl.Utf8)).alias(c) for c in ('buyer_paid','refund_amount','refund_status','order_type')],
            pl.lit(origin).alias(ORIGIN))
    combined=pl.concat([project(frame,'order_detail_file'),project(feed,'order_console')],how='vertical_relaxed')
    filled=enrich(combined).filter(pl.col(ORIGIN)=='order_detail_file')
    if filled.height!=frame.height:raise ValueError('订单补充金额改变了原始明细行数')
    return frame.with_columns(*[filled[c] for c in ('buyer_paid','refund_amount','allocation_basis_source')])


def prepare(keyed, metric):
    """Attach a valid factor or an explicit pending reason per order group."""
    ratio=metric.allocate.by
    keyed=refund_values(keyed)
    for name in (ratio,'buyer_paid','refund_amount'):
        if name not in keyed.columns:keyed=keyed.with_columns(pl.lit(None,dtype=pl.Float64).alias(name))
    keyed=keyed.with_columns((pl.col(ratio).is_not_null() & (pl.col(ratio).cast(pl.Utf8).str.strip_chars()!='')).alias('__ratio_supplied'))
    keyed=keyed.with_columns(*[pl.col(c).cast(pl.Float64,strict=False).alias(c) for c in dict.fromkeys((ratio,'buyer_paid','refund_amount'))])
    ratio_value=pl.col(ratio)
    declared=pl.col('__ratio_supplied').any().over('link_key')
    ratio_ok=(ratio_value.is_not_null() & ratio_value.is_finite() & (ratio_value>=0) & (ratio_value<=1)).all().over('link_key')
    ratio_sum=ratio_value.sum().over('link_key')
    ratio_ok &= (ratio_sum-1).abs()<=0.000001
    paid=pl.col('buyer_paid');refund=pl.col('refund_amount')
    amount_ok=(paid.is_not_null() & paid.is_finite() & (paid>=0) & refund.is_not_null() & refund.is_finite() & (refund>=0)).all().over('link_key')
    net=pl.max_horizontal(paid-refund,pl.lit(0.))
    denominator=net.sum().over('link_key')
    duplicate=pl.lit(False)
    if 'sub_order_id' in keyed.columns:
        duplicate=pl.col('sub_order_id').is_null().any().over('link_key') | (pl.col('sub_order_id').n_unique().over('link_key')!=pl.len().over('link_key'))
    single=pl.lit(False)
    if {'sub_order_id',ORIGIN}<=set(keyed.columns):
        # Taobao's certified single-child representation uses the same master
        # and child identifier. This is a unique destination, not an even split.
        single=(pl.len().over('link_key')==1) & (pl.col('sub_order_id')==pl.col('link_key')) & (pl.col(ORIGIN)=='order_detail_file')
        single &= ~((paid.is_not_null() & (~paid.is_finite() | (paid<0))) | (refund.is_not_null() & (~refund.is_finite() | (refund<0))))
    context_bad=pl.lit(False)
    for col in ('store','period'):
        if col in keyed.columns:context_bad |= pl.col(col).n_unique().over('link_key')!=1
    reason=(pl.when(context_bad).then(pl.lit('cross_store_or_period'))
        .when(duplicate).then(pl.lit('missing_or_duplicate_child'))
        .when(declared & ~ratio_ok).then(pl.lit('invalid_or_incomplete_ratio'))
        .when(~declared & ~amount_ok & ~single).then(pl.lit('missing_payment_basis'))
        .when(~declared & (denominator<=0) & ~single).then(pl.lit('zero_net_payment'))
        .otherwise(pl.lit('')))
    factor=pl.when(declared).then(ratio_value/ratio_sum).when(single).then(1.).otherwise(net/denominator)
    return keyed.with_columns(reason.alias('__allocation_reason'),
        pl.when(reason=='').then(factor).otherwise(None).alias('__allocation_factor'))
