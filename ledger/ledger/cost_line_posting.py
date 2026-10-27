"""Book saved, context-matched order cost supplements in open-period runs.

Raw files and cost decisions remain immutable. A later source cost removes its
key from the raw coverage gap list, so its human supplement retires naturally.
"""
from decimal import Decimal
from dataclasses import replace

import polars as pl

from . import cost_lines, manual_cost
from .engine import calculate
from .engine.link import Spine
from .engine.project import project
from .money import money_float
from .goods_cost_scope import sales_only, VERSION

SOURCE = '__manual_order_cost__'


def apply(ws, result, store):
    if not ws.conn.execute('SELECT 1 FROM cost_line_log WHERE store_id=? LIMIT 1', (store.id,)).fetchone():
        return
    raw_metric = next((m.for_platform(store.platform) for m in result.model.metrics if m.id == 'goods_cost'), None)
    if raw_metric is None or raw_metric.link is None:
        return
    metric = raw_metric.model_copy(update={'source': SOURCE})
    for (name, period), sl in result.slices.items():
        if name != store.name:
            continue
        state = ws.conn.execute('SELECT state FROM period WHERE store_id=? AND period=?', (store.id, period)).fetchone()
        if state and state['state'] == 'closed':
            continue
        captured_revision = cost_lines.revision(ws, store.id, period)
        latest = cost_lines._latest(ws, store.id, period, 2**63-1, max_revision=captured_revision)
        reviews = []
        rows = []
        for item in sl.coverage_gap_rows.to_dicts():
            if item.get('partial_cost', False):
                continue
            saved = latest.get(item['coverage_key'])
            if not saved or saved['action'] != 'save' or saved['context_sha'] != item['context_sha'] or item['order_count'] != 1:
                continue
            if item.get('goods_scope_version') != VERSION:
                raise ValueError('补录成本缺少已排除补发订单的清单依据，已停止核算')
            amount = manual_cost._cents(saved['amount'])
            automatic=str(saved['by']).startswith('service:')
            reviews.append({'coverage_key': item['coverage_key'], 'context_sha': item['context_sha'],
                            'revision': saved['id'], 'amount': float(amount)})
            rows.append({'metric_id': 'goods_cost', 'source_id': SOURCE, 'template_id': SOURCE,
                'store': name, 'period': period, 'grain': 'order', 'link_key': item['coverage_key'],
                'order_id': item['order_id'], 'linked': True, 'counted': True,
                'count_without_order': False, 'amount': float(-amount), 'contribution': float(-amount),
                'major': metric.major, 'subject': '人工补录商品成本', 'minor': '人工补录商品成本',
                'file_sha': None, 'file_name': '已验证历史凭据成本' if automatic else '人工补录订单成本', 'sheet': '补录记录（行号为记录编号）',
                'row_no': saved['id'], 'record_type': '凭据核实商品成本' if automatic else '人工补录商品成本',
                'source_note': f"已计入本店本月商品成本；补录版本 {saved['id']}；依据：{saved['reason']}"})
        if not rows:
            if latest:
                sl.cost_line_posting = {'line_revision': captured_revision,
                    'count': 0, 'total': 0, 'observed': manual_cost.observed({'calculation_inputs': sl.calculation_inputs}), 'reviews': []}
            continue
        if getattr(sl, 'cost_line_posting', None):
            raise ValueError('人工补录成本不能在同一次核算中重复计入')
        facts = pl.DataFrame(rows, infer_schema_length=None)
        spine = sales_only(Spine(result.spine).indexed().frame).filter((pl.col('store') == name) & (pl.col('period') == period))
        projected = project(facts, metric, Spine(spine)).facts
        actual = dict(projected.group_by('link_key').agg(pl.col('amount').sum()).iter_rows())
        for row in rows:
            if row['link_key'] not in actual or abs(Decimal(str(actual[row['link_key']]))-Decimal(str(row['amount']))) > Decimal('.00001'):
                raise ValueError('人工补录成本与订单分摊金额不一致，已停止核算')
        base = manual_cost.observed({'calculation_inputs': sl.calculation_inputs})
        total = sum((Decimal(str(r['amount'])) for r in reviews), Decimal(0))
        sl.cost_line_posting = {'line_revision': captured_revision,
                               'count': len(reviews), 'total': money_float(total),
                               'observed': base, 'reviews': reviews}
        sl.facts = pl.concat([sl.facts, facts], how='diagonal_relaxed')
        result.facts = pl.concat([result.facts, facts], how='diagonal_relaxed')
        result.spine_facts = pl.concat([result.spine_facts, projected], how='diagonal_relaxed')
        from .operations_readmodel import capture_projection
        operations=capture_projection(projected,spine)
        old_operations=getattr(sl,'operations_evidence',pl.DataFrame())
        sl.operations_evidence=pl.concat([old_operations,operations],how='diagonal_relaxed')
        approved={r['coverage_key'] for r in reviews}
        report=getattr(sl,'link_reports',{}).get('goods_cost')
        if report is not None:
            sl.link_reports={**sl.link_reports,'goods_cost':replace(report,covered_keys=report.covered_keys|approved)}
        raw_coverage=dict(getattr(sl,'cost_coverage',{}) or {})
        if raw_coverage and raw_coverage.get('expected'):
            expected=raw_coverage['expected']
            covered=min(expected,raw_coverage.get('covered',expected-raw_coverage.get('uncovered',expected))+len(approved))
            sl.cost_coverage={**raw_coverage,'upstream_coverage':raw_coverage.get('coverage'),
                'approved_cost_line_count':len(approved),'covered':covered,'uncovered':expected-covered,
                'coverage':covered/expected,'passed':covered/expected>=raw_coverage.get('threshold',1)}
        raw_pending=getattr(sl,'pricing_gaps',pl.DataFrame())
        if not raw_pending.is_empty() and {'metric_id','coverage_key'}<=set(raw_pending.columns):
            sl.raw_pricing_gaps=raw_pending
            sl.pricing_gaps=raw_pending.filter(~((pl.col('metric_id')=='goods_cost')&pl.col('coverage_key').is_in(approved)))
            global_pending=getattr(result,'pricing_gaps',pl.DataFrame())
            if not global_pending.is_empty() and {'metric_id','coverage_key','store','period'}<=set(global_pending.columns):
                if not hasattr(result,'raw_pricing_gaps'):result.raw_pricing_gaps=global_pending.clone()
                result.pricing_gaps=global_pending.filter(~(
                    (pl.col('metric_id')=='goods_cost')&pl.col('coverage_key').is_in(approved)
                    &pl.col('store').is_in([store.name,store.id,*store.aliases])&(pl.col('period')==period)))
        totals = dict(sl.calculation_inputs['metric_totals'])
        totals['goods_cost'] = float(Decimal(str(totals.get('goods_cost', 0)))-total)
        unavailable=set(sl.calculation_inputs['unavailable_metrics'])
        fully_covered=bool(raw_coverage and sl.cost_coverage.get('uncovered')==0)
        if fully_covered:
            unavailable.discard('goods_cost')
            completeness=getattr(sl,'completeness',None)
            if completeness is not None and raw_metric.source in completeness.missing:
                completeness.missing.remove(raw_metric.source)
                if raw_metric.source not in completeness.arrived:completeness.arrived.append(raw_metric.source)
                completeness.reasons.pop(raw_metric.source,None)
        sl.calculation_inputs = {**sl.calculation_inputs, 'metric_totals': totals,'unavailable_metrics':sorted(unavailable)}
        nodes = calculate.evaluate_statement(result.model, totals,
            set(sl.calculation_inputs['unavailable_metrics']), set(sl.calculation_inputs['inapplicable_metrics']))
        # Preserve completeness and pending explanations; a partial supplement
        # is not an authorization to certify other missing cost evidence.
        for key, old in sl.nodes.items():
            if not old.available and not (fully_covered and set(old.missing_sources)<= {raw_metric.source}):
                nodes[key] = replace(old, value=None if old.value is None else nodes[key].value)
        sl.nodes = nodes
        if hasattr(sl,'audit'):
            from .engine.audit import audit
            revised=audit(result.model,sl.facts,sl.link_reports,sl.classify_report,sl.completeness,sl.nodes)
            recomputed={f.check_id:f for f in revised.findings}
            # Reevaluate only the checks this operation actually supplies. All
            # unrelated identity, freight, refund and source gates are preserved.
            changed={c.id for c in result.model.checks if
                (c.kind=='spine_coverage' and c.metric=='goods_cost') or c.kind=='completeness'}
            sl.audit.findings=[recomputed.get(f.check_id,f) if f.check_id in changed else
                replace(f,passed=True,message='未覆盖商品成本已按逐单核对决定入账，原缺价证据保留')
                if f.check_id=='historical_cost_evidence' and sl.cost_coverage.get('passed') else f
                for f in sl.audit.findings]
