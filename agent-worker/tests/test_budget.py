import asyncio
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from ledger_agent.runtime import Config, ModelSpec, Runtime
from ledger_agent.state import BudgetExhausted, State, budget, load_price, worst_case

PRICE = {'version': 'unit', 'currency': 'CNY', 'input_per_million': '2', 'output_per_million': '8'}
JUNE = datetime(2030, 6, 30, 23, 59, tzinfo=ZoneInfo('Asia/Shanghai'))
JULY = datetime(2030, 7, 1, 0, 1, tzinfo=ZoneInfo('Asia/Shanghai'))


def test_cap_is_a_hard_2000_cny_ceiling():
    assert budget()['amount'] == Decimal('2000')
    assert budget({'amount': '500', 'currency': 'CNY'})['amount'] == Decimal('500')
    for value in ({'amount': '2000.01', 'currency': 'CNY'}, {'amount': '100', 'currency': 'USD'},
                  {'amount': '0', 'currency': 'CNY'}, {'amount': 'NaN', 'currency': 'CNY'}):
        with pytest.raises(ValueError):
            budget(value)


def test_unknown_or_partial_price_is_not_a_zero_charge(tmp_path):
    assert worst_case(None, 10, 10) is None
    assert worst_case({'currency': 'CNY', 'input_per_million': '1'}, 10, 10) is None
    assert worst_case({**PRICE, 'currency': 'USD'}, 10, 10) is None
    assert worst_case({**PRICE, 'input_per_million': '-1'}, 10, 10) is None
    state = State(tmp_path)
    with pytest.raises(BudgetExhausted) as error:
        state.reserve('case', 'fixture', None, 'sha', 10, 10)
    assert error.value.reason == 'model_price_unknown_under_cap'
    with pytest.raises(ValueError):
        (tmp_path / 'price.json').write_text('{"currency":"CNY"}')
        load_price(tmp_path / 'price.json')


def test_reservation_blocks_before_sending_and_counts_the_month(tmp_path):
    state = State(tmp_path)
    cap = {'amount': '1', 'currency': 'CNY'}
    each = worst_case(PRICE, 100000, 1200)
    first = state.reserve('case', 'fixture', PRICE, 'r1', 100000, 1200, cap, now=JUNE)
    assert first == each
    calls = 1
    while state.month_spend_for('2030-06') + each <= Decimal(1):
        state.reserve('case', 'fixture', PRICE, f'r{calls + 1}', 100000, 1200, cap, now=JUNE)
        calls += 1
    with pytest.raises(BudgetExhausted) as error:
        state.reserve('case', 'fixture', PRICE, 'over', 100000, 1200, cap, now=JUNE)
    assert error.value.reason == 'monthly_budget_exhausted'
    assert state.month_spend_for('2030-06') <= Decimal(1)
    # The rejected call left no reservation; a new Shanghai month starts again.
    state.reserve('case', 'fixture', PRICE, 'july', 100000, 1200, cap, now=JULY)
    assert state.month_spend_for('2030-07') == each


def test_known_usage_replaces_the_worst_case_reservation(tmp_path):
    state = State(tmp_path)
    reserved = state.reserve('case', 'fixture', PRICE, 'r1', 50000, 1200, now=JUNE)
    state.usage('case', 'fixture', SimpleNamespace(input_tokens=1000, output_tokens=100, cache_input_tokens=0), 'r1', PRICE)
    actual = (Decimal(1000) * 2 + Decimal(100) * 8) / Decimal(1000000)
    assert state.month_spend_for('2030-06') == actual < reserved
    # A failed call with unknown usage keeps its full worst case.
    unknown = state.reserve('case', 'fixture', PRICE, 'r2', 50000, 1200, now=JUNE)
    state.usage('case', 'fixture', None, 'r2', status='usage_unknown_failed')
    assert state.month_spend_for('2030-06') == actual + unknown


def test_two_runtimes_sharing_state_share_one_cap(tmp_path):
    business, ops = State(tmp_path), State(tmp_path)
    cap = {'amount': str(worst_case(PRICE, 10, 10) * 3 / 2), 'currency': 'CNY'}
    business.reserve('case', 'fixture', PRICE, 'b', 10, 10, cap, now=JUNE)
    with pytest.raises(BudgetExhausted):
        ops.reserve('repair', 'fixture', PRICE, 'o', 10, 10, cap, now=JUNE)


def test_runtime_without_price_never_calls_the_model(tmp_path):
    config = Config('http://not-called.invalid/v1', 'unit', (ModelSpec('fixture'),), evaluation=True)
    result = asyncio.run(Runtime(config, State(tmp_path)).run({'id': 'c', 'input_sha': 'a' * 64}, {}, lambda: {'verified': False}))
    assert result == {'state': 'waiting_configuration', 'model_used': False, 'reason': 'model_price_unknown_under_cap'}
    assert State(tmp_path).summary()['calls'] == 0
