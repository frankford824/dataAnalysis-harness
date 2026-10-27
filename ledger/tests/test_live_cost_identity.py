import polars as pl
import pytest
from ledger.order_feed import OrderFeed
from ledger.model.schema import Store
from test_order_feed import _fixture


@pytest.mark.parametrize('mode',['provided','required','historical'])
def test_retired_costs_never_post_under_any_platform_price_policy(tmp_path,mode):
    root=tmp_path/'feed';manifest=_fixture(root)
    table=lambda n:pl.read_parquet(root/manifest['objects'][n]['path'])
    costs=table('order_costs.parquet')
    orphan=costs.with_columns(pl.lit('99').alias('sub_order_id'),pl.lit('REMOVED').alias('sku_id'))
    foreign=costs.with_columns(pl.lit('another-order').alias('order_id'))
    source=pl.concat([costs,orphan,foreign])
    feed=OrderFeed(tmp_path/'ws',feed_root=root)
    result=feed._cost_frame(table('orders.parquet'),table('order_items.parquet'),source,
        table('order_relations.parquet'),Store(id='s',name='测试店',platform='alibaba1688'),'proof',
        require_pricing=mode=='required',require_history=mode=='historical')
    assert result['internal_sub_order_id'].to_list()==['11']
    assert result['sku'].to_list()==['SKU1']
    assert feed._retired_cost_rows==2
    assert source.height==3
