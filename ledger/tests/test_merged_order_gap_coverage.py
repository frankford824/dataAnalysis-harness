"""A merged ERP header must not hide a fully costed order-level (1688/JD) order in the gap list."""
from datetime import date, datetime

import polars as pl
import pytest

from conftest import MODELS
from test_order_feed import _fixture, _write
from ledger.engine.runtime import Ingested, Ingestion, run
from ledger.engine.types import FileRef, Recognition
from ledger.model.loader import load_model
from ledger.model.schema import Check, ColumnBinding, Model, StatementNode, Store, Template
from ledger.order_feed import OrderFeed

ORDER = '5127692077145007220'
OTHER = '5127692077145007221'
STORE = Store(id='alibaba1688_test', name='合成1688店', platform='alibaba1688')
JD = Store(id='jd_test', name='合成京东店', platform='jd')


def calculate(tmp_path, *, header, member, item_shop='10', uploaded=(ORDER,), store=STORE):
    root = tmp_path / 'feed'
    manifest = _fixture(root)
    def read(name):
        return pl.read_parquet(root / manifest['objects'][name]['path'])
    changed = {
        'orders.parquet': read('orders.parquet').with_columns(pl.lit(header).alias('online_order_no')),
        'order_items.parquet': read('order_items.parquet').with_columns(
            pl.lit(member).alias('online_order_no'), pl.lit(item_shop).alias('order_store_id'),
            pl.lit('2026-05-04').alias('original_order_date')),
        'after_sales.parquet': read('after_sales.parquet').clear(),
        'after_sale_items.parquet': read('after_sale_items.parquet').clear(),
    }
    for name, frame in changed.items():
        manifest['objects'][name] = _write(root, name, frame)
    builtin = load_model(MODELS / 'cn-ecommerce')
    model = Model(id='merged-gap', name='合单成本覆盖', platforms=builtin.platforms, stores=(store,),
        sources=tuple(builtin.source(name).model_copy(update={'provides': ()})
                      for name in ('order_detail', 'order_cost', 'after_sales')),
        metrics=(builtin.metric('goods_cost'),),
        checks=(Check(id='coverage', name='商品成本覆盖', kind='spine_coverage', metric='goods_cost', threshold=.95),),
        statement=(StatementNode(id='cost', name='商品成本', formula={'op': 'add', 'of': ['goods_cost']}),))
    export = pl.DataFrame({
        'order_id': list(uploaded), 'product_id': ['894665106214'] * len(uploaded),
        'quantity': [20.] * len(uploaded), 'store_name': [store.name] * len(uploaded),
        'order_type': ['销售订单'] * len(uploaded), 'tracking_no': ['SF1'] * len(uploaded), 'order_state': ['完成'] * len(uploaded),
        'buyer_paid': [20.] * len(uploaded), 'order_date': [datetime(2026, 6, 2)] * len(uploaded),
        'order_time': [datetime(2026, 6, 2, 9)] * len(uploaded)})
    template = Template(id='synthetic_1688_export', source='order_detail', match_columns=('order_id',),
        bindings=tuple(ColumnBinding(role=c, columns=(c,)) for c in export.columns),
        time_slots={'order_date': 'order_time'})
    ref = FileRef('synthetic-1688-export', '订单明细.xlsx', 'sheet')
    upload = Ingested(ref=ref, template=template, frame=export, rows=export.height,
        recognition=Recognition(ref=ref, signature=template.signature, header_count=len(export.columns),
                                source_id='order_detail', template_id=template.id))
    ingestion = Ingestion(model=model, items=[upload])
    feed = OrderFeed(tmp_path / 'ws', feed_root=root)
    feed._install_snapshot(manifest, {'stores': [{'order_store_id': '10', 'ledger_store_id': store.id,
                                                  'mapping_status': 'confirmed'}]},
                           {'healthy': True}, source_revision=10, source_latest_seq=10, revision_etag='synthetic')
    feed.append_to(ingestion, store)
    return ingestion, run(ingestion, store.platform).slices[(store.name, '2026-06')]


def test_counted_cost_of_merged_1688_order_covers_the_uploaded_order(tmp_path):
    ingestion, sl = calculate(tmp_path, header=f'{ORDER},10:{ORDER}', member=f'10:{ORDER}')
    cost = ingestion.frames_of('order_cost')[0].frame
    assert cost['order_id'].to_list() == cost['original_order_id'].to_list() == [ORDER]
    # The console line is the uploaded order, not an extra order keyed by the header list.
    assert ingestion.frames_of('order_detail')[-1].frame.is_empty()
    assert sl.coverage_gap_rows.is_empty()
    assert sl.cost_coverage['expected'] == sl.cost_coverage['covered'] == 1
    goods = sl.facts.filter(pl.col('metric_id') == 'goods_cost')
    assert goods['link_key'].to_list() == [ORDER]
    assert goods['contribution'].sum() == pytest.approx(-7)


def test_member_date_promotion_stays_limited_to_taobao_and_douyin(tmp_path):
    root = tmp_path / 'feed'
    manifest = _fixture(root)
    def read(name):
        return pl.read_parquet(root / manifest['objects'][name]['path'])
    orders = read('orders.parquet').with_columns(pl.lit(f'{ORDER},10:{ORDER}').alias('online_order_no'))
    items = read('order_items.parquet').with_columns(pl.lit(f'10:{ORDER}').alias('online_order_no'),
        pl.lit('10').alias('order_store_id'), pl.lit('2026-05-04').alias('original_order_date'))
    frame = OrderFeed(tmp_path / 'ws')._order_frame(orders, items, read('after_sales.parquet').clear(),
                                                    read('order_relations.parquet'), STORE, 'synthetic')
    assert frame['order_id'].to_list() == [ORDER]
    assert frame['order_date'][0].date() == date(2026, 6, 2)


@pytest.mark.parametrize('member,item_shop', [(f'20:{ORDER}', '10'), (f'10:{ORDER}', '20'),
                                              (f'{ORDER},10:{ORDER}', '10')])
def test_unproved_member_keeps_the_merged_header_and_the_gap(tmp_path, member, item_shop):
    _, sl = calculate(tmp_path, header=f'{ORDER},10:{ORDER}', member=member, item_shop=item_shop)
    assert sl.coverage_gap_rows['coverage_key'].to_list() == [ORDER]


def test_a_truly_uncovered_order_of_the_same_upload_is_still_listed(tmp_path):
    _, sl = calculate(tmp_path, header=f'{ORDER},10:{ORDER}', member=f'10:{ORDER}', uploaded=(ORDER, OTHER))
    assert sl.coverage_gap_rows['coverage_key'].to_list() == [OTHER]
    assert sl.cost_coverage['expected'] == 2 and sl.cost_coverage['covered'] == 1


def test_two_platform_orders_merged_by_the_erp_keep_their_own_member(tmp_path):
    _, sl = calculate(tmp_path, header=f'{OTHER},10:{ORDER}', member=f'10:{ORDER}', uploaded=(ORDER, OTHER))
    assert sl.coverage_gap_rows['coverage_key'].to_list() == [OTHER]
    assert sl.facts.filter(pl.col('metric_id') == 'goods_cost')['link_key'].to_list() == [ORDER]


JD_ORDER = '3412345678901234'
JD_OTHER = '3412345678901235'


@pytest.mark.parametrize('order', [JD_ORDER, '341234567890'])
def test_counted_cost_of_merged_jd_order_covers_the_uploaded_order(tmp_path, order):
    ingestion, sl = calculate(tmp_path, header=f'{order},10:{order},10:{order}', member=f'10:{order}',
                              uploaded=(order,), store=JD)
    cost = ingestion.frames_of('order_cost')[0].frame
    assert cost['order_id'].to_list() == cost['original_order_id'].to_list() == [order]
    assert ingestion.frames_of('order_detail')[-1].frame.is_empty()
    assert sl.coverage_gap_rows.is_empty()
    assert sl.cost_coverage['expected'] == sl.cost_coverage['covered'] == 1
    assert sl.facts.filter(pl.col('metric_id') == 'goods_cost')['contribution'].sum() == pytest.approx(-7)


@pytest.mark.parametrize('member,item_shop', [(f'20:{JD_ORDER}', '10'), (f'10:{JD_ORDER}', '20'),
                                              ('10:3412345678901', '10')])
def test_unproved_jd_member_keeps_the_merged_header_and_the_gap(tmp_path, member, item_shop):
    _, sl = calculate(tmp_path, header=f'{JD_ORDER},10:{JD_ORDER},10:3412345678901', member=member,
                      item_shop=item_shop, uploaded=(JD_ORDER,), store=JD)
    assert sl.coverage_gap_rows['coverage_key'].to_list() == [JD_ORDER]


def test_two_jd_orders_merged_by_the_erp_keep_their_own_member(tmp_path):
    _, sl = calculate(tmp_path, header=f'{JD_OTHER},10:{JD_ORDER}', member=f'10:{JD_ORDER}',
                      uploaded=(JD_ORDER, JD_OTHER), store=JD)
    assert sl.coverage_gap_rows['coverage_key'].to_list() == [JD_OTHER]
    assert sl.facts.filter(pl.col('metric_id') == 'goods_cost')['link_key'].to_list() == [JD_ORDER]


def test_merged_jd_feed_order_is_not_refilled_from_the_uploaded_jst_cost(tmp_path):
    root = tmp_path / 'feed'
    manifest = _fixture(root)
    def read(name):
        return pl.read_parquet(root / manifest['objects'][name]['path'])
    orders = read('orders.parquet').with_columns(pl.lit(f'{JD_ORDER},10:{JD_ORDER}').alias('online_order_no'))
    items = read('order_items.parquet').with_columns(pl.lit(f'10:{JD_ORDER}').alias('online_order_no'),
                                                    pl.lit('10').alias('order_store_id'))
    feed = OrderFeed(tmp_path / 'ws')
    relations = read('order_relations.parquet')
    feed_order = feed._order_frame(orders, items, read('after_sales.parquet').clear(), relations, JD, 'synthetic')
    feed_cost = feed._cost_frame(orders, items, read('order_costs.parquet'), relations, JD, 'synthetic')
    builtin = load_model(MODELS / 'cn-ecommerce')
    def item(ref, template_id, frame):
        template = builtin.template(template_id)
        return Ingested(ref, Recognition(ref, 'sheet', 10, template_id, template.source), frame.height, frame, template)
    upload = item(FileRef('order-file', '京东订单明细.xlsx', 'Sheet1'), 'jd_order_detail_v1',
                  pl.DataFrame({'order_id': [JD_ORDER], 'store_name': [JD.name]}))
    jst = item(FileRef('cost-file', '聚水潭成本.xlsx', 'Sheet1'), 'jushuitan_cost_v1',
               pl.DataFrame({'original_order_id': [JD_ORDER], 'store_name': [JD.name], 'unit_cost': [3.5],
                             'quantity': [2], 'sku': ['SKU1']}))
    # The feed already owns this platform order's cost; a second copy would double it.
    assert OrderFeed._jd_export_cost_for_absent_orders(Ingestion(builtin, [upload, jst]), JD, feed_order, feed_cost) == []
