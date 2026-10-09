"""Evidence-based order allocation. Missing monetary inputs are not zero.

Platform rows retain their identities. The order feed may only fill missing
amounts on an exact store/master/child match; it never adds duplicate children
to the allocation denominator.
"""
import polars as pl

ORIGIN='__spine_origin__'
NO_REFUND_MARKERS = ('无退款申请', '没有申请退款', '未退款', '无')
REASON_LABELS = {
    'missing_payment_basis':'子订单实付或退款金额有缺失，暂不能计算商品分配比例',
    'zero_net_payment':'订单明细中的实付扣退款后为 0，不能据此计算商品分配比例',
    'invalid_or_incomplete_ratio':'子订单比例有缺失、超出 0%—100%，或合计不是 100%',
    'ratio_missing':'同一主订单中，部分子订单没有填写分配比例',
    'ratio_invalid_value':'子订单分配比例不是有效数字，或超出 0%—100%',
    'ratio_total_not_one':'同一主订单的子订单分配比例合计不是 100%',
    'reship_freight_basis':'补发运费已计入店铺，尚不能确定对应的唯一补发商品',
    'reship_nonfreight_basis':'当前只找到补发订单，不能用补发单的收入比例分配这笔金额',
    'missing_or_duplicate_child':'子单编号缺失或重复',
    'cross_store_or_period':'关联跨店铺或账期',
}
REASON_ACTIONS = {
    'missing_payment_basis':'核对订单明细中同一主订单下所有子订单的买家实付金额、退款金额；空白不代表 0。',
    'zero_net_payment':'结合对账单核对收款、退款；另核对费用对应的子订单或商品。无需把已计入店铺的金额再录入一次。',
    'invalid_or_incomplete_ratio':'核对分配比例来源及同一主订单下的全部子订单；不要仅为通过校验而补成 100%。',
    'ratio_missing':'补齐同一主订单下全部子订单的分配比例，并核对合计是否为 100%。',
    'ratio_invalid_value':'核对原始分配比例的数值和百分比格式；比例必须在 0%—100% 之间。',
    'ratio_total_not_one':'核对是否缺少子订单、重复导入或比例填错；不要直接按剩余笔数平摊。',
    'reship_freight_basis':'在订单台按主订单号核对补发单、物流单号和商品；涉及多个商品时还需明确各商品承担金额。',
    'reship_nonfreight_basis':'核对原销售订单及对账流水的主子订单号，不要用零付款的补发单代替销售订单。',
    'missing_or_duplicate_child':'核对同一主订单下的子订单编号，补齐缺失编号并核实重复行。',
    'cross_store_or_period':'核对订单所属店铺和月份，不能跨店铺或跨月份混合分配。',
}


def refund_values(frame):
    if 'refund_amount' not in frame.columns:return frame
    text=pl.col('refund_amount').cast(pl.Utf8).str.strip_chars()
    absent=text.is_in(NO_REFUND_MARKERS).fill_null(False)
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
    ratio_missing=(~pl.col('__ratio_supplied')).any().over('link_key')
    ratio_invalid=((pl.col('__ratio_supplied') & ratio_value.is_null()) |
                   (~ratio_value.is_finite() | (ratio_value<0) | (ratio_value>1)).fill_null(False)).any().over('link_key')
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
    all_reship=(pl.col('order_type').eq('补发订单').fill_null(False).all().over('link_key')
                if 'order_type' in keyed.columns else pl.lit(False))
    # A uniquely identified replacement shipment's freight has one destination;
    # this is direct attribution, not an invented payment ratio or equal split.
    freight=metric.source=='freight' and metric.link is not None and metric.link.key=='tracking_no'
    direct_reship=pl.lit(False)
    evidence={'product_id','tracking_no','internal_order_id','sub_order_id','store','period','freight_attribution_evidence',ORIGIN}
    if freight and evidence<=set(keyed.columns):
        direct_reship=all_reship & (pl.len().over('link_key')==1) & (pl.col(ORIGIN)=='order_console')
        direct_reship &= (pl.col('freight_attribution_evidence')=='single_reship_item').fill_null(False)
        for col in ('product_id','tracking_no','internal_order_id','sub_order_id','store','period'):
            direct_reship &= pl.col(col).cast(pl.Utf8).str.strip_chars().is_not_null() & (pl.col(col).cast(pl.Utf8).str.strip_chars()!='')
    reason=(pl.when(context_bad).then(pl.lit('cross_store_or_period'))
        .when(duplicate).then(pl.lit('missing_or_duplicate_child'))
        .when(direct_reship).then(pl.lit(''))
        .when(all_reship).then(pl.lit('reship_freight_basis' if freight else 'reship_nonfreight_basis'))
        .when(declared & ratio_missing).then(pl.lit('ratio_missing'))
        .when(declared & ratio_invalid).then(pl.lit('ratio_invalid_value'))
        .when(declared & ~ratio_ok).then(pl.lit('ratio_total_not_one'))
        .when(~declared & ~amount_ok & ~single).then(pl.lit('missing_payment_basis'))
        .when(~declared & (denominator<=0) & ~single).then(pl.lit('zero_net_payment'))
        .otherwise(pl.lit('')))
    factor=pl.when(direct_reship).then(1.).when(declared).then(ratio_value/ratio_sum).when(single).then(1.).otherwise(net/denominator)
    return keyed.with_columns(reason.alias('__allocation_reason'),
        pl.when(reason=='').then(factor).otherwise(None).alias('__allocation_factor'))
