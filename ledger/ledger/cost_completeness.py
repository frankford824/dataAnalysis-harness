"""Economic cost completeness, separate from coverage-quality thresholds.

Known source contributions remain available as partial evidence. No missing
required target or unadmitted active cost component is rounded into zero.
"""
from __future__ import annotations
from decimal import Decimal

import polars as pl

from .engine import calculate
from .engine.types import Finding
from .manual_cost import observed

VERSION = 'cost_amount_completeness.v1'
KEY_METRICS = {'goods': {'goods_cost','goods_return_cost'}, 'dropship': {'dropship_cost'},
               'reshipment': {'reshipment_cost'}}
FINDING = 'required_cost_unresolved'


def _posted_keys(sl):
    """Only context-matched supplements actually counted in these facts count."""
    facts = sl.facts
    if not {'source_id','metric_id','link_key','counted','contribution'} <= set(facts.columns):
        return set()
    posted = facts.filter((pl.col('source_id') == '__manual_order_cost__')
                          & (pl.col('metric_id') == 'goods_cost') & pl.col('counted'))
    totals = dict(posted.group_by('link_key').agg(pl.col('contribution').sum()).iter_rows()) if posted.height else {}
    output = set()
    for review in (getattr(sl,'cost_line_posting',{}) or {}).get('reviews',[]):
        key = review.get('coverage_key')
        if key in totals and abs(Decimal(str(totals[key])) + Decimal(str(review['amount']))) <= Decimal('.0000005'):
            output.add(key)
    return output


def apply(sl, model, platform):
    previous = getattr(sl,'cost_completeness',{}) or {}
    active = {m.id for original in model.metrics if (m := original.for_platform(platform)) is not None}
    unavailable = set(sl.calculation_inputs.get('unavailable_metrics',[])) - set(previous.get('added_unavailable_metrics',[]))
    source_unavailable = set(unavailable)
    posted = _posted_keys(sl)
    coverage = getattr(sl,'coverage_gap_rows',pl.DataFrame())
    missing_keys = (set(coverage['coverage_key'].drop_nulls().to_list()) - posted
                    if 'coverage_key' in coverage.columns else set())
    quality = getattr(sl,'cost_coverage',{}) or {}
    quality_missing = int(quality.get('uncovered') or 0)
    if not quality.get('approved_cost_line_count'):
        quality_missing = max(0, quality_missing - len(posted))
    targets = {key: 0 for key in KEY_METRICS}
    if 'goods_cost' in active:
        targets['goods'] = max(len(missing_keys), quality_missing)
    gaps = getattr(sl,'pricing_gaps',pl.DataFrame())
    # A whole-order zero comes from the dedicated verified seller-evidence
    # adapter. A zero accessory sharing a child key cannot exempt its siblings.
    whole_zero = set()
    if {'metric_id','template_id','counted','contribution','link_key'} <= set(sl.facts.columns):
        whole_zero = set(sl.facts.filter((pl.col('metric_id') == 'goods_cost')
            & (pl.col('template_id') == 'order_console_brushing_zero_v1')
            & pl.col('counted') & (pl.col('contribution') == 0))['link_key'].drop_nulls().to_list())
    prices = {key: 0 for key in KEY_METRICS}
    pending_metrics = set()
    for gap in gaps.to_dicts():
        metric = gap.get('metric_id')
        if metric not in active:
            continue
        if metric == 'goods_cost' and gap.get('coverage_key') in posted | whole_zero:
            continue
        for key, metrics in KEY_METRICS.items():
            if metric in metrics:
                prices[key] += 1
                pending_metrics.add(metric)
    failures = {f.check_id for f in sl.audit.findings if not f.passed}
    if 'dropship_cost_evidence' in failures:
        targets['dropship'] = max(targets['dropship'],1)
    if 'dropship_scope_evidence' in failures:
        targets['goods'] = max(targets['goods'],1)
        targets['dropship'] = max(targets['dropship'],1)
    if 'cost_return_evidence' in failures:
        targets['goods'] = max(targets['goods'],1)
    complete = {key: not (targets[key] or prices[key] or (metrics & active & source_unavailable))
                for key, metrics in KEY_METRICS.items()}
    blocked = pending_metrics | (source_unavailable & set.union(*KEY_METRICS.values()))
    if missing_keys or quality_missing or 'dropship_scope_evidence' in failures:
        blocked.add('goods_cost')
    if targets['dropship']:blocked.add('dropship_cost')
    if 'cost_return_evidence' in failures:blocked.add('goods_return_cost')
    blocked &= active
    unavailable.update(blocked)
    sl.calculation_inputs = {**sl.calculation_inputs,'unavailable_metrics':sorted(unavailable)}
    totals = sl.calculation_inputs.get('metric_totals',{})
    nodes = calculate.evaluate_statement(model,totals,unavailable,
                                         set(sl.calculation_inputs.get('inapplicable_metrics',[])))
    sources = {m.source for m in model.metrics if m.id in blocked}
    for key,node in nodes.items():
        if not node.available and sources & set(node.missing_sources):
            node.value = None
            node.unavailable_reason = '必需成本金额尚未核齐；现有来源明细和已知部分保留，未知部分未按零计入'
        elif key in sl.nodes and not sl.nodes[key].available and not blocked:
            # Do not erase a distinct existing source/identity explanation.
            if not node.available and sl.nodes[key].unavailable_reason:
                node.unavailable_reason = sl.nodes[key].unavailable_reason
    sl.nodes = nodes
    known = observed({'calculation_inputs':sl.calculation_inputs})
    base = (getattr(sl,'cost_line_posting',{}) or {}).get('observed') or known
    sl.cost_completeness = {'schema':VERSION,'amount_complete':complete,
        'known_partial':dict(base),'known_booked':known,
        'unresolved_targets_count':targets,'unresolved_price_count':prices,
        'blocked_metrics':sorted(blocked),
        'added_unavailable_metrics':sorted(blocked-source_unavailable),
        'authority':'source_and_posted_lines' if posted else 'source_evidence'}
    sl.audit.findings = [f for f in sl.audit.findings if f.check_id != FINDING]
    if blocked:
        sl.audit.findings.append(Finding(FINDING,'必需成本金额未齐',passed=False,blocking=True,
            message='成本覆盖质量线不代表金额完整；未核齐的必需成本不能按零确认利润或月结',
            detail={'amount_complete':complete,'unresolved_targets_count':targets,'unresolved_price_count':prices}))
    return sl.cost_completeness
