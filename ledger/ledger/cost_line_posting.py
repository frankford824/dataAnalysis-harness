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
            reviews.append({'coverage_key': item['coverage_key'], 'context_sha': item['context_sha'],
                            'revision': saved['id'], 'amount': float(amount)})
            rows.append({'metric_id': 'goods_cost', 'source_id': SOURCE, 'template_id': SOURCE,
                'store': name, 'period': period, 'grain': 'order', 'link_key': item['coverage_key'],
                'order_id': item['order_id'], 'linked': True, 'counted': True,
                'count_without_order': False, 'amount': float(-amount), 'contribution': float(-amount),
                'major': metric.major, 'subject': '人工补录商品成本', 'minor': '人工补录商品成本',
                'file_sha': None, 'file_name': '人工补录订单成本', 'sheet': '补录记录（行号为记录编号）',
                'row_no': saved['id'], 'record_type': '人工补录商品成本',
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
        totals = dict(sl.calculation_inputs['metric_totals'])
        totals['goods_cost'] = float(Decimal(str(totals.get('goods_cost', 0)))-total)
        sl.calculation_inputs = {**sl.calculation_inputs, 'metric_totals': totals}
        nodes = calculate.evaluate_statement(result.model, totals,
            set(sl.calculation_inputs['unavailable_metrics']), set(sl.calculation_inputs['inapplicable_metrics']))
        # Preserve completeness and pending explanations; a partial supplement
        # is not an authorization to certify other missing cost evidence.
        for key, old in sl.nodes.items():
            if not old.available:
                nodes[key] = replace(old, value=None if old.value is None else nodes[key].value)
        sl.nodes = nodes
