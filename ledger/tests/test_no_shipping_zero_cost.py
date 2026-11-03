"""The explicitly confirmed HQT01773 rule is separate from missing history."""
import polars as pl
import pytest

from ledger.engine.cost_policy import is_no_shipping, NO_SHIPPING_NOTE
from ledger.fee_display import describe
from test_cost_exclusivity import calculate


@pytest.mark.parametrize('kind', ['销售订单', '补发订单'])
@pytest.mark.parametrize('unit', [None, 0., .0001, 5., -1.])
def test_confirmed_no_shipping_item_has_zero_cost_without_history(kind, unit):
    result = calculate(sku='HQT01773', kind=kind, unit=unit)
    assert result.pricing_gaps.is_empty()
    assert result.facts.height == 1
    assert result.facts['counted'].all()
    assert result.facts['contribution'].sum() == 0
    assert NO_SHIPPING_NOTE in result.facts['source_note'].item()
    assert describe(result.facts.row(0, named=True))['accounting_status'] == '无需发货：商品成本为0'


def test_exception_is_exact_sku_and_never_rounds_other_small_prices():
    frame = pl.DataFrame({'sku': ['HQT01773', ' hqt01773 ', 'HQT017730', 'HQT01773-A', 'OTHER', None]})
    assert frame.select(is_no_shipping(frame)).to_series().to_list() == [True, True, False, False, False, False]
    changed = frame.with_columns(pl.lit(True).alias('pricing_identity_changed'))
    assert not changed.select(is_no_shipping(changed)).to_series().any()
    assert calculate(sku='OTHER', unit=.0001).facts['contribution'].sum() == pytest.approx(-.0001)
    assert not calculate(sku='OTHER', unit=None).pricing_gaps.is_empty()


@pytest.mark.parametrize('review', [False, True])
@pytest.mark.parametrize('missing', [False, True])
def test_feed_keeps_no_shipping_candidate_and_preserves_source_costs(tmp_path, review, missing):
    from test_order_feed import _fixture
    from ledger.order_feed import OrderFeed
    from ledger.model.schema import Store
    root = tmp_path / 'feed'
    manifest = _fixture(root, second_unnamed=True)
    def table(name): return pl.read_parquet(root / manifest['objects'][name]['path'])
    items = table('order_items.parquet').with_columns(pl.Series('sku_id', ['HQT01773', 'SKU2']), pl.lit(True).alias('is_virtual'))
    costs = table('order_costs.parquet').with_columns(pl.Series('sku_id', ['HQT01773', 'SKU2']))
    if missing: costs = costs.filter(pl.col('sku_id') != 'HQT01773')
    before = costs.clone()
    frame = OrderFeed(tmp_path / 'ws', feed_root=root)._cost_frame(
        table('orders.parquet'), items, costs, table('order_relations.parquet'),
        Store(id='taobao_test', name='shop', platform='taobao'), 'fixture', require_history=review)
    assert frame.filter(pl.col('sku') == 'HQT01773').height == 1
    assert frame.filter(pl.col('sku') == 'SKU2')['unit_cost'].item() == 1.
    assert costs.equals(before)
    if review: assert not frame.filter(pl.col('sku') == 'HQT01773')['pricing_missing'].item()


def test_display_never_certifies_nonzero_or_unrecorded_amount_as_zero():
    row = {'metric_id': 'goods_cost', 'counted': True, 'contribution': 0., 'source_note': NO_SHIPPING_NOTE}
    assert describe(row)['accounting_status'] == '无需发货：商品成本为0'
    assert describe({**row, 'contribution': None})['accounting_status'].startswith('待核对')
    assert describe({**row, 'contribution': -1.})['accounting_status'] == '本行已计入'
