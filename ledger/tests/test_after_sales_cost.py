"""聚水潭售后单只决定对应商品的成本是否归零。"""

from __future__ import annotations

import io

import pytest
from conftest import MODELS, write_xlsx

import ledger.service as service
from ledger.engine.runtime import ingest, run
from ledger.model.loader import load_model
from ledger.workspace import SHARED_STORE_ID, Workspace


ORDER_HEADER = [
    "子订单编号", "主订单编号", "买家实付金额", "退款金额", "商品ID", "物流单号",
    "订单创建时间", "订单付款时间", "退款状态",
]
COST_HEADER = [
    "内部订单号", "线上订单号", "店铺名称", "下单时间", "状态", "订单类型",
    "线上子订单编号", "原始线上订单号", "商品编码", "商品名称", "数量", "成本价", "总成本",
]
AFTER_HEADER = [
    "售后单号", "内部订单号", "店铺名称", "线上订单号", "状态", "线上状态",
    "货物状态", "商品编码", "线上子订单编号", "申请数量",
]


@pytest.fixture(scope="module")
def model():
    return load_model(MODELS / "cn-ecommerce")


def _cost_row(internal: str, order: str, sub: str, sku: str, cost: float):
    return [
        internal, order, "淘宝喜必顺", "2026-05-08 10:00:00", "已发货", "普通订单",
        sub, order, sku, sku, 1, cost, cost,
    ]


def _after_row(
    index: int, internal: str, order: str, sub: str, sku: str,
    goods_status: str, online_status: str,
):
    return [
        f"AS{index}", internal, "淘宝喜必顺", order, "已确认", online_status,
        goods_status, sku, sub, 1,
    ]


def test_only_the_three_confirmed_combinations_zero_the_exact_product(tmp_path, model):
    records = [
        ("I1", "O1", "S1", "SKU-A", 1.0, "买家未收到货", "退款成功"),
        ("I2", "O2", "S2", "SKU-B", 2.0, "买家已退货", "退款成功"),
        ("I3", "O3", "S3", "SKU-C", 3.0, "卖家已收到退货", "退款成功"),
        ("I4", "O4", "S4", "SKU-D", 4.0, "卖家已收到退货", "退款关闭"),
        ("I5", "O5", "S5", "SKU-E", 5.0, "买家已收到货", "退款成功"),
        ("I6", "O6", "S6", "SKU-F", 6.0, "", "退款成功"),
        ("I7", "O7", "S7", "SKU-G", 7.0, "卖家已收到退款", "退款关闭"),
    ]
    # 同一个内部单、子订单下另一个商品没有售后，必须继续算，证明不是整单排除。
    extra = ("I1", "O1", "S1", "SKU-KEEP", 8.0)

    order_rows = []
    seen_subs = set()
    for _internal, order, sub, sku, *_rest in records:
        if sub in seen_subs:
            continue
        seen_subs.add(sub)
        order_rows.append([sub, order, 100, 0, sku, f"L{sub}", "2026-05-08", "2026-05-08", ""])

    order_file = write_xlsx(
        tmp_path / "订单明细-淘宝喜必顺.xlsx",
        [["说明"], ORDER_HEADER, *order_rows],
    )
    cost_file = write_xlsx(
        tmp_path / "聚水潭成本-淘宝喜必顺.xlsx",
        [["说明"], COST_HEADER, *[_cost_row(*row[:5]) for row in records], _cost_row(*extra)],
    )
    after_file = write_xlsx(
        tmp_path / "售后单_样本.xlsx",
        [AFTER_HEADER, *[
            _after_row(index, internal, order, sub, sku, goods, online)
            for index, (internal, order, sub, sku, _cost, goods, online) in enumerate(records, 1)
        ]],
    )

    result = run(
        ingest([order_file, cost_file, after_file], model, ["淘宝喜必顺"]),
        "taobao",
    )
    goods = result.facts.filter(result.facts["metric_id"] == "goods_cost")
    assert goods.height == 5
    assert goods["amount"].sum() == pytest.approx(-(4 + 5 + 6 + 7 + 8))
    assert any("逐商品排除 3 行" in note for note in result.notes)


def test_a_generic_after_sales_file_is_stored_once_and_shared(tmp_path, model, monkeypatch):
    ws = Workspace(tmp_path / "space")
    store_id = "taobao_xibishun"
    ws.keep("订单明细-淘宝喜必顺.xlsx", io.BytesIO(b"store"), store_id)
    calls = []

    def recomputed(_ws, _model, store, **_kwargs):
        calls.append(store.id)
        return service.Recomputed(store_id=store.id)

    monkeypatch.setattr(service, "recompute", recomputed)
    result = service.intake(
        ws, model, [("售后单_20260828.xlsx", io.BytesIO(b"shared"))],
    )
    assert result.rejected == []
    assert result.kept[0].store_id == SHARED_STORE_ID
    assert calls == [store_id]
    assert {path.name for path in ws.active_files(store_id)} == {
        "订单明细-淘宝喜必顺.xlsx", "售后单_20260828.xlsx",
    }
    shared = next(row for row in ws.submissions(store_id) if row["shared"])
    assert shared["store_id"] == SHARED_STORE_ID

    service.intake(
        ws, model, [("售后单_20260829.xlsx", io.BytesIO(b"new snapshot"))],
    )
    shared_rows = [row for row in ws.submissions(store_id) if row["shared"]]
    assert [row["name"] for row in shared_rows] == ["售后单_20260829.xlsx"]
    assert ws.conn.execute(
        "select count(*) from version where store_id=?", (SHARED_STORE_ID,)
    ).fetchone()[0] == 2
    ws.close()
