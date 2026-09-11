from pathlib import Path

import polars as pl
import pytest
from conftest import write_xlsx
from ledger.engine.runtime import ingest
from ledger.model.repository import ModelRepository
from ledger.model.schema import Store
from ledger.order_feed import OrderFeed


def test_identity_export_without_buyer_payment_is_still_order_spine(tmp_path):
    p = tmp_path / "订单明细-陈慨-淘宝拾梦小屋99-6月.xlsx"
    write_xlsx(p, [["子订单编号", "主订单编号", "商品标题", "订单状态", "退款状态",
        "退款金额", "订单创建时间", "商品ID", "物流单号"],
        ["5121910154426014049", "5121910154426014049", "商品", "交易成功", "没有申请退款",
          "无退款申请", "2026-06-29 11:46:21", "P1", "SF1"]], sheet="export")
    model = ModelRepository(Path(__file__).resolve().parents[2] / "models/cn-ecommerce").get().model
    ing = ingest([p], model, default_store="陈慨-淘宝拾梦小屋99")
    records = ing.frames_of("order_detail")
    assert len(records) == 1
    frame = records[0].frame
    assert frame["order_id"].item() == "5121910154426014049"
    assert "buyer_paid" not in frame.columns or frame["buyer_paid"].item() is None


@pytest.mark.parametrize("sales,amount", [(1, 6.95), (2, 14.04)])
@pytest.mark.parametrize("missing", [True, False])
def test_reshipment_never_dilutes_original_sales_receipt(sales, amount, missing):
    n = sales + 1
    orders = pl.DataFrame({"order_id": ["O"], "online_order_no": ["PLATFORM"],
        "order_time": ["2026-06-29 11:46:21"], "pay_time": ["2026-06-29 11:47:00"],
        "order_status_raw": ["Sent"], "paid_amount": [str(amount)],
        "refund_amount": ["0"], "tracking_no": ["SF1"]})
    items = pl.DataFrame({"order_id": ["O"] * n, "sub_order_id": [str(i) for i in range(n)],
        "online_order_no": ["PLATFORM"] * n, "outer_sku": [str(i) for i in range(sales)] + ["$Asr-1"],
        "merchant_sku": ["P"] * n, "product_name": ["商品"] * n,
        "paid_amount": pl.Series([None] * n if missing else [str(amount / sales)] * sales + ["100"], dtype=pl.Utf8),
        "refund_amount": ["0"] * n, "tracking_no": ["SF1"] * n})
    frame = object.__new__(OrderFeed)._order_frame(orders, items, pl.DataFrame(), pl.DataFrame(),
        Store(id="taobao_test", name="test", platform="taobao"), "test", {"$asr-1": "PLATFORM"})
    sale = frame.filter(pl.col("order_type") == "销售订单")
    assert sale["alloc_ratio"].sum() == pytest.approx(1)
    assert amount * sale["alloc_ratio"].sum() == pytest.approx(amount)
    assert frame.filter(pl.col("order_type") == "补发订单")["alloc_ratio"].item() == 0
