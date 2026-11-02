"""Repeated live-provider QA evaluation on synthetic statement structures."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from .qa import Answerer, PLACEHOLDER, SKILL, guard, qa_identity
from .runtime import Config, ModelSpec
from .state import State, cost_per_correct


def _node(id, name, available=True, sign='positive', formula=None, scope='', reason=''):
    return {'id': id, 'name': name, 'level': 1, 'available': available, 'sign': sign if available else None,
            'is_total': bool(formula), 'formula': formula, 'amount_scope': scope, 'unavailable_reason': reason, 'missing_sources': []}


BASE = [_node('n_receipt', '销售收入'), _node('n_goods', '商品成本', sign='negative'),
        _node('n_freight', '运费', sign='negative'), _node('n_fee', '平台扣费', sign='negative'),
        _node('gross_profit', '毛利', formula={'op': 'add', 'of': ['n_receipt', 'n_goods', 'n_freight', 'n_fee']}),
        _node('net_profit', '净利润', formula={'op': 'add', 'of': ['gross_profit', 'n_labor']}),
        _node('n_labor', '兼职分摊', sign='negative')]


def _context(nodes=None, findings=(), codes=(), approved=False, state='open'):
    return {'store_name': '示例旗舰店', 'platform': 'tmall', 'period': '2026-05', 'period_state': state,
            'statement': nodes or BASE, 'findings': list(findings), 'case_stage': 'gathering', 'case_issue_codes': list(codes),
            'period_cost_approved': approved, 'missing_source_names': []}


def _swap(id, **change):return [{**n, **change} if n['id'] == id else n for n in BASE]


# (name, question, context, nodes that a correct answer must cite at least one of)
CASES = (
    ('net_profit_basis', '净利润是怎么算出来的？', _context(), {'net_profit', 'gross_profit'}),
    ('gross_profit_basis', '毛利包含哪些项目？', _context(), {'gross_profit'}),
    ('revenue_value', '本月销售收入是多少？', _context(), {'n_receipt'}),
    ('goods_cost_value', '商品成本是多少？', _context(), {'n_goods'}),
    ('freight_value', '这个月运费花了多少？', _context(), {'n_freight'}),
    ('fee_value', '平台扣了多少钱？', _context(), {'n_fee'}),
    ('labor_value', '兼职分摊是多少？', _context(), {'n_labor'}),
    ('negative_profit', '为什么净利润是负的？', _context(_swap('net_profit', sign='negative')), {'net_profit'}),
    ('cost_unavailable', '商品成本为什么显示不可用？', _context(_swap('n_goods', available=False, reason='N条成本未覆盖'), codes=['required_cost_unresolved']), {'n_goods'}),
    ('approved_total', '商品成本能看逐单明细吗？', _context(_swap('n_goods', scope='approved_store_period_total'), approved=True), {'n_goods'}),
    ('approved_profit', '确认本期成本以后净利润是多少？', _context(_swap('n_goods', scope='approved_store_period_total'), approved=True), {'net_profit'}),
    ('blocking_finding', '为什么不能结账？', _context(findings=[{'id': 'chk_goods_coverage', 'name': '商品成本覆盖', 'passed': False, 'blocking': True, 'head': '覆盖率N%低于门槛'}], codes=['blocking_check']), {'n_goods'}),
    ('freight_missing', '运费为什么不可用？', _context(_swap('n_freight', available=False, reason='运费来源未到')), {'n_freight'}),
    ('revenue_vs_profit', '收入和净利润分别是多少？', _context(), {'n_receipt', 'net_profit'}),
    ('cost_share', '商品成本占收入多大比例？', _context(), {'n_goods', 'n_receipt'}),
    ('closed_month', '这个月已经结账了，净利润最终是多少？', _context(state='closed'), {'net_profit'}),
    ('reopen_request', '帮我把这个月反结账，并把运费改掉', _context(state='closed'), {'n_freight'}),
    ('labor_impact', '兼职分摊对净利润有什么影响？', _context(), {'n_labor', 'net_profit'}),
    ('fee_driver', '平台扣费为什么会减少毛利？', _context(), {'n_fee', 'gross_profit'}),
    ('revenue_unavailable', '销售收入怎么还没出来？', _context(_swap('n_receipt', available=False, reason='结算单缺失'), codes=['source_missing']), {'n_receipt'}),
)


def grade(text, context, expected):
    if not text:return False, 'empty'
    problem = guard(text, context)
    if problem:return False, problem
    names = {n['name'] for n in context['statement'] if n['id'] in expected}
    if not set(PLACEHOLDER.findall(text)) & expected and not any(name in text for name in names):return False, 'missing_relevant_item'
    return True, None


def evaluate(config, output: Path, *, rounds=5, concurrency=2, selection=None):
    if rounds < 1 or concurrency < 1:raise ValueError('Invalid evaluation dimensions')
    output.mkdir(parents=True, exist_ok=False)
    state = State(output/'private-audit');before = qa_identity()
    answerer = Answerer(config, state)
    cases = [c for c in CASES if selection is None or c[0] in selection]
    if not cases:raise ValueError('Empty evaluation selection')

    def one(item, iteration):
        name, question, context, expected = item
        started = time.monotonic()
        outcome = answerer.answer({'question_id': f'{name}-{iteration}', 'question': question, 'context': context})
        text = outcome.get('text', '')
        correct, reason = grade(text, context, expected) if text else (False, outcome.get('failure'))
        return {'case': name, 'iteration': iteration, 'correct': correct, 'reason': reason,
                'false_completion': False, 'duration_seconds': round(time.monotonic()-started, 3)}

    with ThreadPoolExecutor(concurrency) as pool:
        rows = list(pool.map(lambda pair: one(*pair), [(c, i) for c in cases for i in range(rounds)]))
    answerer.client.close()
    result = {'schema': 1, 'dataset': 'synthetic-statement-questions.v1', 'live_provider': True,
        'models': [m.name for m in config.models], 'skills': [SKILL], 'qa_sha': before, 'inputs_unchanged': before == qa_identity(),
        'provider_sha': hashlib.sha256(config.base_url.rstrip('/').encode()).hexdigest(),
        'case_sha': hashlib.sha256(json.dumps(CASES, ensure_ascii=False, default=sorted).encode()).hexdigest(),
        'case_count': len(cases), 'rounds': rounds, 'trials': len(rows), 'correct': sum(r['correct'] for r in rows),
        'false_completions': 0, 'production_corpus_verified': False, 'usage': state.summary(), 'results': rows}
    result['passed'] = result['case_count'] >= 20 and rounds >= 5 and result['correct'] == len(rows) and result['inputs_unchanged']
    report = output/'evaluation.json'
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    if result['passed']:
        (output/'model-profile.json').write_text(json.dumps({'name': config.models[0].name, 'skills': [SKILL],
            'evaluation_path': str(report.resolve()), 'evaluation_sha': hashlib.sha256(report.read_bytes()).hexdigest(),
            'price': config.models[0].price, 'cost_per_correct': cost_per_correct(result['usage'], result['correct'])},
            ensure_ascii=False, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=5)
    parser.add_argument('--concurrency', type=int, default=2)
    parser.add_argument('--case', action='append')
    parser.add_argument('--model')
    parser.add_argument('--price', type=Path, required=True)
    args = parser.parse_args()
    from .state import load_price
    price = load_price(args.price)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'ledger'))
    from ledger.assist import load_config
    saved = load_config()
    if not saved.ready:raise SystemExit('Existing ledger gateway is not configured')
    config = Config(saved.base_url, saved.api_key, (ModelSpec(args.model or saved.model, skills=(SKILL,), price=price),), evaluation=True)
    result = evaluate(config, args.output, rounds=args.rounds, concurrency=args.concurrency, selection=args.case)
    print(json.dumps({k: v for k, v in result.items() if k != 'results'}, ensure_ascii=False))
    raise SystemExit(0 if result['passed'] else 1)


if __name__ == '__main__':main()
