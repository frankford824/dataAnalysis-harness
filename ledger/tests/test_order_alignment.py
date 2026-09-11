from decimal import Decimal

import polars as pl

from ledger.order_alignment import keys, read_groups
from ledger.order_feed import OrderFeed, normalize_entity


def order(oid, key, **extra):
    return dict(order_id=oid, online_order_keys=key, payable_amount='10.00',
                collected_amount='10.00', freight_amount='0', discount_amount='1', **extra)


def test_aliases_and_transitive_siblings_count_headers_once():
    rows = [order('1', ['A,123', 'B']), order('2', ['B', 'C']), order('3', ['C'])]
    items = [{'order_id': '3', 'outer_oi_id': 'child'}] * 8
    result = read_groups(rows, items, 'child')[0]
    assert result['internal_order_ids'] == ['1', '2', '3']
    assert Decimal(result['payable_amount']) == 30
    assert result['online_order_keys'] == ['A', 'B', 'C']
    assert keys('A,123:B,456') == {'A', 'B'}


def test_payment_id_finds_separate_groups_without_merging_them():
    result = read_groups([order('1', ['A'], outer_pay_id='00123'),
                          order('2', ['B'], outer_pay_id='00123')], [], '00123')
    assert len(result) == 2
    assert all(len(g['internal_order_ids']) == 1 for g in result)


def test_unknown_is_not_zero_and_payment_flags_do_not_report_missing():
    a = order('1', ['A'], is_paid=False)
    b = order('2', ['A'])
    b['freight_amount'] = None
    result = read_groups([a, b], [], 'A')[0]
    assert result['freight_amount'] is None
    assert result['receipt_check'] == 'not_applicable'
    for fields in ({'is_cod': 'true'}, {'collected_amount': '0.00'}):
        row = order('1', ['A']) | fields
        assert read_groups([row], [], 'A')[0]['receipt_check'] == 'not_applicable'


def test_new_columns_survive_old_snapshot_without_changing_legacy_money():
    import json
    base = pl.DataFrame({'order_id': ['1'], 'paid_amount': ['9'], 'settled_amount': ['8']})
    raw = {'order_id': '1', 'order_date': '2026-09-11', 'pay_amount': '143.94', 'paid_amount': '143.94',
           'payable_amount': '143.94', 'discount_amount': '2.26',
           'freight_amount': '0', 'online_order_keys': ['3316418006045102157'], 'is_paid': False}
    delta = {'entity_type': 'order', 'entity_id': '1', 'operation': 'upsert',
             'payload_json': json.dumps({'order': raw})}
    result = OrderFeed._overlay(base, [delta], 'order', 'order_id', lambda p: [p['order']])
    assert result['discount_amount'].item() == '2.26'
    assert result['paid_amount'].item() == '143.94'
    assert keys(result['online_order_keys'].item()) == {'3316418006045102157'}
    assert normalize_entity('order', raw)['settled_amount'] == '143.94'


def test_component_catalog_gaps_do_not_change_group_amount_or_status():
    rows = [order('1', ['A'])]
    items = [{'order_id': '1', 'outer_oi_id': 'child', 'i_id': None,
              'sku_type': None, 'line_amount': '100'}] * 20
    result = read_groups(rows, items, 'child')[0]
    assert Decimal(result['payable_amount']) == 10
    assert result['receipt_check'] == 'requires_statement'


def test_reader_preserves_checkpoint_and_old_snapshot_semantics(tmp_path):
    from test_order_feed import _fixture, FakeClient
    from ledger.model.schema import Store
    manifest = _fixture(tmp_path / 'feed')
    manifest['engine_version'] = 'order-console-ledger-feed.v1.4'
    feed = OrderFeed(tmp_path / 'ws', feed_root=tmp_path / 'feed', client=FakeClient(manifest))
    feed.sync()
    before = feed.state()
    result = feed.alignment(Store(id='taobao_test', name='test', platform='taobao'), 'S1')
    assert result['available']
    assert result['groups'][0]['payable_amount'] == '21.00'  # latest consumed event
    assert result['groups'][0]['freight_amount'] is None
    assert feed.state() == before
    live = feed.alignment(Store(id='taobao_test', name='test', platform='taobao'), 'S1', live=True)
    assert live['live_refreshed'] is True
    assert feed.state() == before


def test_missing_item_payment_stays_unknown_in_read_frame(tmp_path):
    from test_order_feed import _fixture
    from ledger.model.schema import Store
    root = tmp_path / 'feed'
    manifest = _fixture(root)
    orders = pl.read_parquet(root / manifest['objects']['orders.parquet']['path'])
    items = pl.read_parquet(root / manifest['objects']['order_items.parquet']['path']).with_columns(
        pl.lit(None, dtype=pl.Utf8).alias('paid_amount'))
    frame = object.__new__(OrderFeed)._order_frame(orders, items, pl.DataFrame(), pl.DataFrame(),
        Store(id='taobao_test', name='test', platform='taobao'), 'probe')
    assert frame['buyer_paid'].item() is None
