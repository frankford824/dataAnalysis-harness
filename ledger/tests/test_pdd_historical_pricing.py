from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest

from conftest import MODELS
from ledger import commission, commission_engine, pricing_gaps, service, view
from ledger.engine.runtime import Ingestion, run
from ledger.model.loader import load_model
from ledger.model.schema import Model, Platform, SourceContract, StatementNode, Store
from ledger.workspace import Workspace
from test_reship_period_and_cost_drill import item


def scenario(**overrides):
    metric = load_model(MODELS / 'cn-ecommerce').metric('reshipment_cost').for_platform('pdd')
    metric = metric.model_copy(update={'by_platform': ()})
    model = Model(id='test', name='test', platforms=(Platform(id='pdd', name='拼多多', cost_pricing='historical'),),
        stores=(Store(id='s', name='shop', platform='pdd'),),
        sources=(SourceContract(id='order_detail', name='订单', is_spine=True, owner_role='shop_owner', cadence='monthly'),
                 SourceContract(id='order_cost', name='成本', owner_role='shop_owner', cadence='monthly')),
        metrics=(metric,), statement=(StatementNode(id='cost', name='成本', formula={'op': 'add', 'of': [metric.id]}),
            StatementNode(id='profit', name='利润', commission_base=True, formula={'op': 'add', 'of': ['cost']})))
    row = dict(order_id='260524-163771850381198', original_order_id='260524-163771850381198',
        base_order_id='260524-163771850381198', internal_order_id='123', sub_order_id='CHILD',
        sku='SKU', order_type='补发订单', order_state='Sent', store_name='shop',
        order_time=datetime(2026, 6, 10), quantity=2., unit_cost=5., cost_source='history',
        cost_status='priced', cost_as_of='2026-05-24', order_flag=None)
    row.update(overrides)
    orders = [{k: row[k] for k in ('order_id', 'sub_order_id', 'order_type', 'store_name', 'order_time')}]
    result = run(Ingestion(model=model, items=[item('order_detail', orders, orders[0]), item('order_cost', [row], row)]), 'pdd')
    return model, result


@pytest.mark.parametrize('changes', [
    {'cost_source': 'mirror'}, {'cost_as_of': '2026-06-10'},
    {'unit_cost': None, 'cost_status': 'missing_price'}, {'unit_cost': float('nan')},
    {'quantity': -1}, {'pricing_suspect': True},
])
def test_missing_history_never_becomes_zero_profit_or_commission(changes):
    model, result = scenario(**changes)
    assert result.pricing_gaps.height == 1
    sl = result.slices[('shop', '2026-05')]
    assert sl.facts.is_empty()
    assert sl.nodes['profit'].value is None
    assert not sl.nodes['profit'].available
    assert not sl.can_close
    assert 'order_cost' in sl.completeness.arrived
    assert 'order_cost' not in sl.completeness.missing
    payload = view.slice_dict(sl, model.store('s'), model)
    assert payload['pricing_pending_count'] == 1
    assert payload['statement'][-1]['unavailable_reason']
    with pytest.raises(ValueError, match='待核价'):
        commission_engine.calculate(result, model, 's', '2026-05', None)
    with pytest.raises(commission.CommissionError, match='待核价'):
        commission.compute(result, model, 's', '2026-05')
    assert service._commission(result, model, model.store('s'), '2026-05')['total'] is None


def test_verified_history_is_booked_in_original_month():
    _, result = scenario()
    assert result.pricing_gaps.is_empty()
    assert result.facts['contribution'].sum() == -10
    assert result.slices[('shop', '2026-05')].nodes['profit'].value == -10


def test_blue_flag_needs_no_price_and_no_pending_review():
    _, result = scenario(order_flag='蓝色旗帜', unit_cost=None, cost_status='missing_price')
    assert result.pricing_gaps.is_empty()
    assert result.facts.is_empty()


def test_pending_rows_are_saved_even_without_monetary_facts(tmp_path):
    model, result = scenario(unit_cost=None, cost_status='missing_price', reference_unit_cost=float('nan'))
    sl = result.slices[('shop', '2026-05')]
    ws = Workspace(tmp_path)
    rid = ws.record('s', '2026-05', view.slice_dict(sl, model.store('s'), model), [])
    service._keep_facts(ws, rid, sl)
    path = ws.pricing_gaps_path(rid)
    data = pricing_gaps.page(path)
    assert data['total'] == 1
    assert data['items'][0]['reference_unit_cost'] is None
    assert pricing_gaps.page(path, q='not-found')['total'] == 0
    assert pricing_gaps.page(path, offset=100)['items'] == []
    assert '260524-163771850381198' in pricing_gaps.csv(path)
    assert 'nan' not in pricing_gaps.csv(path)
    assert not ws.state('s', '2026-05').result['can_close']


def test_fee_export_keeps_actual_zero_without_other_metrics_copies():
    model = load_model(MODELS / 'cn-ecommerce')
    base = {'link_key': '260601-610889920124065', 'major': 'trade_refund', 'subject': '退款',
            'amount': 0., 'contribution': 0., 'counted': True, 'linked': True}
    # Pick the declared major rather than assuming a label in the current model.
    base['major'] = model.metric('trade_refund_pdd').major
    facts = pl.DataFrame([{**base, 'metric_id': 'trade_refund_pdd'},
                          {**base, 'metric_id': 'trade_receipt_pdd', 'counted': False}])
    csv = view.fees_csv(facts, model)
    assert csv.count('260601-610889920124065') == 1
