"""Low coverage shows the missing order keys, including ones without any cost source row."""
from pathlib import Path

import polars as pl

from ledger.engine.runtime import _goods_coverage_rows
from ledger.engine.types import LinkReport
from ledger.model.repository import ModelRepository


def test_missing_orders_are_itemized_and_ambiguous_keys_cannot_be_edited():
    model = ModelRepository(Path(__file__).resolve().parents[2] / 'models' / 'cn-ecommerce').get().model
    spine = pl.DataFrame({
        'store':['谷本文-luckyglow旗舰店'] * 4,
        'period':['2026-06'] * 4,
        'order_id':['O1','O2','O3','O4'],
        'sub_order_id':['S1','S2','S3','S3'],
        'product_id':['P1','P2','P3','P4'],
        'order_date':['2026-06-01','2026-06-02','2026-06-03','2026-06-03'],
        'tracking_no':['SF1','SF2','SF3','SF4'],
        'order_state':['已发货'] * 4,
        'order_type':['销售订单'] * 4,
    })
    report = LinkReport('goods_cost','sub_order_id','order',spine_keys=3,
                        covered_keys={'S1'})
    items = _goods_coverage_rows(model, spine, 'taobao', {'goods_cost':report}).to_dicts()
    assert [item['coverage_key'] for item in items] == ['S2','S3']
    assert items[0]['order_id'] == 'O2' and items[0]['editable']
    assert items[1]['order_count'] == 2 and not items[1]['editable']
    assert all(len(item['context_sha']) == 64 for item in items)
