"""Evidence-preserving monthly store allocation for unmatched product spend."""
import polars as pl
from ..money import money_float,sum_amounts
from .link import target_role
from .project import claims,_store_wide_spine_facts
from .rules import norm_expr

ANCHORS=['metric_id','store','period','file_sha','sheet','row_no','link_key']


def allocate(source,metric,spine,projection,has_control=False):
    rule=metric.allocate
    if not (rule and rule.unmatched=='store_wide' and metric.source=='promotion'
            and metric.link and metric.link.grain=='product') or has_control:
        return projection
    targets=spine.eligible(metric.link).frame
    role=target_role(metric.link.to)
    if targets.is_empty() or role not in targets.columns or 'source_period' not in source.columns:
        return projection
    candidates=source.filter(claims(metric) & pl.col('source_period').is_not_null()
        & (pl.col('source_period')==pl.col('period')) & pl.col('amount').is_finite())
    if candidates.is_empty():return projection
    periods=candidates['period'].unique().to_list()
    owners=candidates['store'].unique().to_list()
    if len(periods)!=1 or len(owners)!=1 or owners[0] in [None,'','(未知店铺)']:
        return projection
    targets=targets.filter((pl.col('store')==owners[0]) & (pl.col('period')==periods[0]))
    known=targets.select(norm_expr(pl.col(role).cast(pl.String)).alias('__promotion_key')).unique()
    candidates=candidates.with_columns(norm_expr(pl.col('link_key')).alias('__promotion_key')).join(known,on='__promotion_key',how='anti').drop('__promotion_key')
    if candidates.is_empty():return projection
    amount=float(sum_amounts(candidates['amount'].to_list(),cents=False))
    # One pool per store-month, not a product x order Cartesian expansion.
    pooled=_store_wide_spine_facts(targets,metric,amount,1.,tuple(periods))
    if pooled.is_empty():return projection
    pooled=pooled.with_columns(pl.lit(1./pooled.height).alias('factor'),
        pl.lit('unmatched:'+periods[0]).alias('__control_pool'))
    proof=f"{owners[0]} · {periods[0]}：未匹配当月商品ID的推广费按 {pooled.height} 条有效订单明细全店均摊，优先原始订单明细；不跨店、不跨月"
    projection.store_wide_evidence=candidates.select(ANCHORS).unique().with_columns(pl.lit(proof).alias('__allocation_basis'))
    projection.facts=pl.concat([projection.facts,pooled],how='diagonal_relaxed')
    projection.orphan_amount=money_float(projection.orphan_amount-amount)
    projection.orphan_keys=max(0,projection.orphan_keys-candidates['link_key'].n_unique())
    projection.notes=[n for n in projection.notes if not (n.startswith(metric.name+'：源表里有 ') and '这部分没进利润' in n)]
    if projection.orphan_keys:
        projection.notes.append(f'{metric.name}：仍有 {projection.orphan_keys} 个键未计入，缺少明确账期或分摊依据')
    projection.notes.append(proof+f'；本次全店分摊 {money_float(amount):,.2f} 元')
    return projection


def annotate(facts,projections):
    frames=[p.store_wide_evidence for p in projections.values() if not p.store_wide_evidence.is_empty()]
    if not frames:return facts
    evidence=pl.concat(frames,how='vertical_relaxed').unique(subset=ANCHORS)
    joined=facts.join(evidence,on=ANCHORS,how='left',nulls_equal=True,maintain_order='left')
    matched=pl.col('__allocation_basis').is_not_null()
    return joined.with_columns(
        pl.when(matched).then(True).otherwise(pl.col('counted')).alias('counted'),
        pl.when(matched).then(pl.col('amount')).otherwise(pl.col('contribution')).alias('contribution'),
        pl.when(matched).then(pl.lit('store_wide')).otherwise(pl.col('booking_status')).alias('booking_status'),
        pl.when(matched).then(pl.col('__allocation_basis')).otherwise(pl.col('allocation_control')).alias('allocation_control'),
        pl.when(matched).then(pl.concat_str([pl.col('source_note'),pl.col('__allocation_basis')],separator='；',ignore_nulls=True))
          .otherwise(pl.col('source_note')).alias('source_note'),
    ).drop('__allocation_basis')
