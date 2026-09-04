from __future__ import annotations

import hashlib
import json
from pathlib import Path

import polars as pl
import pytest
from conftest import write_xlsx

from ledger.engine.runtime import Ingested, Ingestion, _project_scoped_live, _slice_keys, ingest, run
from ledger.engine.link import Spine
from ledger.engine.link import SPINE_PERIOD, SPINE_STORE
from ledger.engine.types import FileRef, Recognition
from ledger.model.repository import ModelRepository
from ledger.model.schema import Store
from ledger.order_feed import OrderFeed, OrderFeedError, OrderFeedNotFound
from ledger.workspace import Workspace


def _write(root: Path, name: str, frame: pl.DataFrame) -> dict:
    data = root / "objects" / name
    data.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(data)
    raw = data.read_bytes()
    return {
        "path": f"objects/{name}",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "rows": frame.height,
        "bytes": len(raw),
    }


def _fixture(root: Path, after_sku: str | None = "SKU1", second_unnamed: bool = False) -> dict:
    """One order, one item, one after-sale.

    ``second_unnamed`` adds a second item/cost/after-sale on the same order whose
    after-sale row carries no 商品编码 — the shape the console exports for multi-item orders.
    """
    n = 2 if second_unnamed else 1
    sub = ["11", "12"][:n]
    outer = ["S1", "S2"][:n]
    sku = ["SKU1", "SKU2"][:n]
    objects = {
        "stores.parquet": _write(root, "stores.parquet", pl.DataFrame({
            "order_store_id": ["10"], "ledger_store_id": ["taobao_test"],
            "mapping_status": ["confirmed"],
        })),
        "orders.parquet": _write(root, "orders.parquet", pl.DataFrame({
            "order_id": ["1"], "online_order_no": ["ON1"], "order_store_id": ["10"],
            "order_time": ["2026-06-02 10:00:00"], "pay_time": ["2026-06-02 10:01:00"],
            "order_status_raw": ["Sent"], "paid_amount": ["20.00"],
            "refund_amount": ["0.00"], "tracking_no": ["SF1"],
        })),
        "order_items.parquet": _write(root, "order_items.parquet", pl.DataFrame({
            "order_id": ["1"] * n, "sub_order_id": sub, "online_order_no": ["ON1"] * n,
            "sku_id": sku, "merchant_sku": ["P1", "P2"][:n], "outer_sku": outer,
            "product_name": ["商品"] * n,
            "quantity": ["2"] * n, "paid_amount": ["20.00", "10.00"][:n], "refund_amount": ["0.00"] * n,
            "tracking_no": ["SF1"] * n,
        })),
        "after_sales.parquet": _write(root, "after_sales.parquet", pl.DataFrame({
            "after_sale_id": ["A1", "A2"][:n], "order_id": ["1"] * n, "online_order_no": ["ON1"] * n,
            "sub_order_id": sub, "order_store_id": ["10"] * n,
            "goods_status_raw": ["买家未收到货"] * n, "online_status_raw": ["退款成功"] * n,
            "refund_status_raw": ["退款成功"] * n,
        })),
        "after_sale_items.parquet": _write(root, "after_sale_items.parquet", pl.DataFrame({
            "after_sale_id": ["A1", "A2"][:n], "order_id": ["1"] * n,
            "sub_order_id": pl.Series(["11", None][:n], dtype=pl.Utf8),
            "sku_id": pl.Series([after_sku, None][:n], dtype=pl.Utf8), "quantity": ["2"] * n,
        })),
        "order_costs.parquet": _write(root, "order_costs.parquet", pl.DataFrame({
            "order_id": ["1"] * n, "sub_order_id": sub, "sku_id": sku,
            "quantity": ["2"] * n, "unit_cost": ["3.50", "1.00"][:n], "cost_amount": ["7.00", "2.00"][:n],
            "cost_source": ["history"] * n, "cost_status": ["priced"] * n,
        })),
        "order_relations.parquet": _write(root, "relations.parquet", pl.DataFrame({
            "relation_type": pl.Series([], dtype=pl.Utf8),
            "source_order_id": pl.Series([], dtype=pl.Utf8),
            "target_order_id": pl.Series([], dtype=pl.Utf8),
            "source_sub_order_id": pl.Series([], dtype=pl.Utf8),
            "target_sub_order_id": pl.Series([], dtype=pl.Utf8),
            "source": pl.Series([], dtype=pl.Utf8),
            "observed_at": pl.Series([], dtype=pl.Utf8),
        })),
        "controls.parquet": _write(root, "controls.parquet", pl.DataFrame({
            "order_store_id": ["10"], "day": ["2026-06-02"], "item_count": [1],
        })),
    }
    manifest = {
        "snapshot_id": "s1", "schema_version": "ledger-feed.v1", "revision": 10,
        "through_seq": 10, "objects": objects,
    }
    (root / "current").mkdir(parents=True)
    (root / "current" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


class FakeClient:
    def __init__(self, manifest: dict):
        self.manifest = manifest
        self.calls: list[str] = []
        self.etag = '"rev-11"'

    def revision(self, etag: str = ""):
        self.calls.append("revision")
        if etag == self.etag:
            return None, etag
        return {
            "schema_version": "ledger-feed.v1", "revision": 11,
            "latest_seq": 11, "healthy": True,
        }, self.etag

    def get(self, path: str, params=None):
        self.calls.append(path)
        if path == "revision":
            return {
                "schema_version": "ledger-feed.v1", "revision": 11,
                "latest_seq": 11, "healthy": True,
            }
        if path == "health":
            return {
                "healthy": True, "quality_risks": [],
                "last_successful_snapshot": self.manifest["snapshot_id"],
            }
        if path == "snapshot":
            return self.manifest
        if path == "stores":
            return {"stores": [{
                "order_store_id": "10", "ledger_store_id": "taobao_test",
                "mapping_status": "confirmed",
            }]}
        if path == "changes":
            after = int((params or {}).get("after_seq", 0))
            if after >= 11:
                return {"to_seq": after, "has_more": False, "changes": []}
            return {"to_seq": 11, "has_more": False, "changes": [{
                "seq": 11, "revision": 11, "entity_type": "order", "entity_id": "1",
                "operation": "upsert", "order_store_id": "10", "order_id": "1",
                "entity_href": "/api/integration/ledger/v1/entities/order/1",
            }]}
        if path in {"entities/order/1", "orders/1"}:
            return {"order": {
                "order_id": "1", "online_order_no": "ON1", "order_store_id": "10",
                "order_time": "2026-06-02 10:00:00", "pay_time": "2026-06-02 10:01:00",
                "order_status_raw": "Sent", "paid_amount": "21.00", "refund_amount": "0.00",
                "tracking_no": "SF1",
            }, "items": []}
        raise AssertionError(path)

    def get_href(self, href: str):
        marker = "/api/integration/ledger/v1/"
        return self.get(href.split(marker, 1)[-1] if marker in href else href.lstrip("/"))


def test_snapshot_and_delta_become_normalized_engine_sources(tmp_path):
    feed_root = tmp_path / "feed"
    manifest = _fixture(feed_root)
    feed = OrderFeed(tmp_path / "workspace", client=FakeClient(manifest), feed_root=feed_root)

    result = feed.sync()
    assert result.snapshot_changed
    assert result.caught_up
    assert result.consumed_seq == 11
    assert result.affected_stores == {"taobao_test"}

    ingestion = Ingestion(model=None)  # type: ignore[arg-type]
    store = Store(id="taobao_test", name="淘宝测试店", platform="taobao")
    feed.append_to(ingestion, store)

    assert {item.recognition.source_id for item in ingestion.items} == {
        "order_detail", "order_cost", "after_sales",
    }
    order = ingestion.frames_of("order_detail")[0].frame
    cost = ingestion.frames_of("order_cost")[0].frame
    after = ingestion.frames_of("after_sales")[0].frame
    assert order is not None and order.row(0, named=True)["order_id"] == "ON1"
    assert order.row(0, named=True)["sub_order_id"] == "S1"
    assert order.row(0, named=True)["product_id"] == "P1"
    assert order.get_column("order_date").null_count() == 0
    assert order.row(0, named=True)["refund_status"] == "退款成功"
    assert order.row(0, named=True)["alloc_ratio"] == 1.0
    assert cost is not None and cost.row(0, named=True)["unit_cost"] == 3.5
    assert cost.row(0, named=True)["order_type"] == "销售订单"
    assert after is not None and after.row(0, named=True)["goods_status"] == "买家未收到货"
    # 售后行和成本行用同一把钥匙：平台子订单号 + 商品编码，规则才对得上。
    assert after.row(0, named=True)["sub_order_id"] == cost.row(0, named=True)["sub_order_id"] == "S1"
    assert after.row(0, named=True)["sku"] == cost.row(0, named=True)["sku"] == "SKU1"


AFTER_SALES_HEADER = [
    "售后单号", "内部订单号", "店铺名称", "线上订单号", "状态", "线上状态",
    "货物状态", "商品编码", "线上子订单编号", "申请数量",
]


def _uploaded_after_sales(tmp_path: Path, model, store: Store, *rows: tuple[str, str]) -> Ingestion:
    """A JST 售后单 export naming exact products (子订单号, 商品编码) of the feed's cost rows."""
    rows = rows or (("S1", "SKU1"),)
    path = write_xlsx(
        tmp_path / "售后单_样本.xlsx",
        [AFTER_SALES_HEADER, *[
            [f"AS{i}", "1", store.name, "ON1", "已确认", "退款成功", "买家未收到货", sku, sub, 2]
            for i, (sub, sku) in enumerate(rows, 1)
        ]],
    )
    return ingest([path], model, [store.name])


def _model_and_store(feed: OrderFeed):
    model = ModelRepository(
        Path(__file__).resolve().parents[2] / "models" / "cn-ecommerce"
    ).get().model
    store = model.store("taobao_msy387nx")
    with feed._connect() as conn:  # noqa: SLF001 - fixture remaps one synthetic shop
        conn.execute(
            "update feed_store set ledger_store_id=? where order_store_id='10'", (store.id,)
        )
    return model, store


def test_feed_after_sales_without_sku_keeps_the_uploaded_export_in_force(tmp_path):
    root = tmp_path / "feed"
    manifest = _fixture(root, after_sku=None)
    feed = OrderFeed(tmp_path / "workspace", client=FakeClient(manifest), feed_root=root)
    feed.sync()
    model, store = _model_and_store(feed)
    ingestion = _uploaded_after_sales(tmp_path, model, store)
    feed.append_to(ingestion, store)

    labels = [item.ref.label() for item in ingestion.frames_of("after_sales")]
    assert any("售后单_样本" in label for label in labels)
    live = next(item for item in ingestion.frames_of("after_sales") if "订单台" in item.ref.label())
    assert any("有 0 行带商品编码" in note and "售后单_样本" in note for note in live.notes)

    result = run(ingestion, store.platform)
    goods = result.facts.filter(pl.col("metric_id") == "goods_cost")
    assert goods.is_empty(), "买家未收到货 + 退款成功 的商品成本必须归零"
    assert any("逐商品排除 1 行" in note for note in result.notes)


def test_partially_named_feed_after_sales_and_the_upload_work_together(tmp_path):
    """The console names the product only on single-item orders; the upload covers the rest."""
    root = tmp_path / "feed"
    manifest = _fixture(root, second_unnamed=True)
    feed = OrderFeed(tmp_path / "workspace", client=FakeClient(manifest), feed_root=root)
    feed.sync()
    model, store = _model_and_store(feed)
    # The upload knows about the second product only; the feed knows about the first.
    ingestion = _uploaded_after_sales(tmp_path, model, store, ("S2", "SKU2"))
    feed.append_to(ingestion, store)

    assert len(ingestion.frames_of("after_sales")) == 2
    live = next(item for item in ingestion.frames_of("after_sales") if "订单台" in item.ref.label())
    assert any("2 行有 1 行带商品编码" in note for note in live.notes)

    result = run(ingestion, store.platform)
    assert result.facts.filter(pl.col("metric_id") == "goods_cost").is_empty()
    assert any("逐商品排除 2 行" in note for note in result.notes)


def test_a_fully_named_feed_does_not_double_count_the_same_after_sale(tmp_path):
    root = tmp_path / "feed"
    manifest = _fixture(root, second_unnamed=True)
    feed = OrderFeed(tmp_path / "workspace", client=FakeClient(manifest), feed_root=root)
    feed.sync()
    model, store = _model_and_store(feed)
    # Upload and feed both name the first product; the upload alone names the second.
    ingestion = _uploaded_after_sales(tmp_path, model, store, ("S1", "SKU1"), ("S2", "SKU2"))
    feed.append_to(ingestion, store)
    result = run(ingestion, store.platform)
    assert result.facts.filter(pl.col("metric_id") == "goods_cost").is_empty()
    assert any("逐商品排除 2 行" in note for note in result.notes)


def test_feed_after_sales_naming_the_product_zeroes_cost_without_any_upload(tmp_path):
    root = tmp_path / "feed"
    manifest = _fixture(root)
    feed = OrderFeed(tmp_path / "workspace", client=FakeClient(manifest), feed_root=root)
    feed.sync()
    model, store = _model_and_store(feed)
    ingestion = Ingestion(model=model)
    feed.append_to(ingestion, store)

    labels = [item.ref.label() for item in ingestion.frames_of("after_sales")]
    assert labels == ["订单台实时售后 · 订单台"]
    result = run(ingestion, store.platform)
    assert result.facts.filter(pl.col("metric_id") == "goods_cost").is_empty()
    assert any("逐商品排除 1 行" in note for note in result.notes)


def test_the_upload_stays_next_to_a_fully_named_feed(tmp_path):
    """One feed after-sale names one product; a whole-order refund on a multi-item
    order is only fully described by the per-product 聚水潭 export."""
    root = tmp_path / "feed"
    manifest = _fixture(root)
    feed = OrderFeed(tmp_path / "workspace", client=FakeClient(manifest), feed_root=root)
    feed.sync()
    model, store = _model_and_store(feed)
    ingestion = _uploaded_after_sales(tmp_path, model, store)
    feed.append_to(ingestion, store)
    labels = sorted(item.ref.label() for item in ingestion.frames_of("after_sales"))
    assert len(labels) == 2 and any("售后单_样本" in label for label in labels)
    live = next(item for item in ingestion.frames_of("after_sales") if "订单台" in item.ref.label())
    assert any("1 行有 1 行带商品编码" in note for note in live.notes)


def test_non_positive_unit_costs_are_unpriced_unless_the_item_is_a_gift(tmp_path):
    """ERP writes 0 for 'no price' and moving averages go negative after returns;
    neither is a cost of goods sold. A declared gift at 0 is a real price."""
    root = tmp_path / "feed"
    manifest = _fixture(root)
    objects = manifest["objects"]
    objects["order_items.parquet"] = _write(root, "order_items.parquet", pl.DataFrame({
        "order_id": ["1"] * 4, "sub_order_id": ["11", "12", "13", "14"], "online_order_no": ["ON1"] * 4,
        "sku_id": ["SKU1", "SKU2", "SKU3", "GIFT"], "merchant_sku": ["P1", "P2", "P3", "P4"],
        "outer_sku": ["S1", "S2", "S3", "S4"], "product_name": ["商品"] * 4,
        "quantity": ["1"] * 4, "paid_amount": ["20.00", "10.00", "5.00", "0.00"], "refund_amount": ["0.00"] * 4,
        "tracking_no": ["SF1"] * 4, "is_gift": [False, False, False, True],
    }))
    objects["order_costs.parquet"] = _write(root, "order_costs.parquet", pl.DataFrame({
        "order_id": ["1"] * 4, "sub_order_id": ["11", "12", "13", "14"], "sku_id": ["SKU1", "SKU2", "SKU3", "GIFT"],
        "quantity": ["1"] * 4, "unit_cost": ["3.50", "-1.27", "0", "0"], "cost_amount": ["3.50", "-1.27", "0", "0"],
        "cost_source": ["history", "scrape", "mirror", "history"], "cost_status": ["priced"] * 4,
    }))
    (root / "current" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    feed = OrderFeed(tmp_path / "workspace", client=FakeClient(manifest), feed_root=root)
    feed.sync()
    ingestion = Ingestion(model=None)  # type: ignore[arg-type]
    feed.append_to(ingestion, Store(id="taobao_test", name="淘宝测试店", platform="taobao"))
    cost = ingestion.frames_of("order_cost")[0]
    assert sorted(cost.frame.get_column("sku").to_list()) == ["GIFT", "SKU1"]
    assert cost.frame.get_column("unit_cost").min() >= 0
    assert any("2 行成本价 ≤ 0" in note for note in cost.notes)


def test_a_line_costing_many_times_its_price_is_a_data_error_not_a_cost(tmp_path):
    """A 0.21-yuan custom accessory shipped 2043 times on a 0.03-yuan order is a
    quantity glitch; its 30,000-yuan cost must not reach the statement. A one-cent
    loss leader costing 1 yuan is real and stays, and so does a 300-piece line whose
    JST line amount is ~0 because the whole order's money sits on a sibling line."""
    root = tmp_path / "feed"
    manifest = _fixture(root)
    objects = manifest["objects"]
    objects["orders.parquet"] = _write(root, "orders.parquet", pl.DataFrame({
        "order_id": ["1", "2"], "online_order_no": ["ON1", "ON2"], "order_store_id": ["10", "10"],
        "order_time": ["2026-06-02 10:00:00"] * 2, "pay_time": ["2026-06-02 10:01:00"] * 2,
        "order_status_raw": ["Sent"] * 2, "paid_amount": ["0.03", "1459.50"],
        "refund_amount": ["0.00"] * 2, "tracking_no": ["SF1", "SF2"],
    }))
    objects["order_items.parquet"] = _write(root, "order_items.parquet", pl.DataFrame({
        "order_id": ["1", "1", "1", "2", "2"], "sub_order_id": ["11", "12", "13", "21", "22"],
        "online_order_no": ["ON1"] * 3 + ["ON2"] * 2,
        "sku_id": ["SKU1", "GLITCH", "LEADER", "BULK", "MAIN"], "merchant_sku": ["P1", "P2", "P3", "P4", "P5"],
        "outer_sku": ["S1", "S2", "S3", "S4", "S5"], "product_name": ["商品", "定制配件", "引流品", "圆盘水晶夹", "主商品"],
        "quantity": ["1", "2043", "2", "300", "1"], "unit_price": ["20.00", "0.21", "0.01", "0.0006", "1459.32"],
        "line_amount": ["20.00", "429.03", "0.02", "0.18", "1459.32"],
        "paid_amount": ["20.00", None, "0.02", None, None], "refund_amount": ["0.00"] * 5,
        "tracking_no": ["SF1"] * 3 + ["SF2"] * 2, "is_gift": [False] * 5,
    }))
    objects["order_costs.parquet"] = _write(root, "order_costs.parquet", pl.DataFrame({
        "order_id": ["1", "1", "1", "2"], "sub_order_id": ["11", "12", "13", "21"],
        "sku_id": ["SKU1", "GLITCH", "LEADER", "BULK"],
        "quantity": ["1", "2043", "2", "300"], "unit_cost": ["3.50", "15.00", "1.00", "1.0945"],
        "cost_amount": ["3.50", "30645.00", "2.00", "328.35"],
        "cost_source": ["history", "mirror", "history", "scrape"], "cost_status": ["priced"] * 4,
    }))
    (root / "current" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    feed = OrderFeed(tmp_path / "workspace", client=FakeClient(manifest), feed_root=root)
    feed.sync()
    ingestion = Ingestion(model=None)  # type: ignore[arg-type]
    feed.append_to(ingestion, Store(id="taobao_test", name="淘宝测试店", platform="taobao"))
    cost = ingestion.frames_of("order_cost")[0]
    assert sorted(cost.frame.get_column("sku").to_list()) == ["BULK", "LEADER", "SKU1"]
    assert cost.frame.get_column("total_cost").sum() == pytest.approx(333.85)
    assert any("1 行一行成本超过售价 5 倍（合计 30,645.00 元）" in note for note in cost.notes)


def test_a_cost_delta_without_order_id_in_its_body_still_lands_on_the_order(tmp_path):
    """/cost-history rows carry sub_order_id but no order_id. The parent key sits on
    the change event; if it is not copied onto the row, the overlay removes the old
    row and appends an orphan, and the order silently loses its cost."""

    class CostDeltaClient(FakeClient):
        def get(self, path: str, params=None):
            if path == "changes":
                after = int((params or {}).get("after_seq", 0))
                if after >= 12:
                    return {"to_seq": after, "has_more": False, "changes": []}
                return {"to_seq": 12, "has_more": False, "changes": [{
                    "seq": 12, "revision": 12, "entity_type": "order_cost", "entity_id": "11",
                    "operation": "upsert", "order_store_id": "10", "order_id": "1", "sub_order_id": "11",
                    "sku_id": "SKU1",
                }]}
            if path.endswith("orders/1/cost-history"):
                return {"costs": [{
                    "sub_order_id": "11", "sku_id": "SKU1", "quantity": "1", "unit_cost": "4.25",
                    "cost_amount": "4.25", "cost_source": "scrape", "cost_status": "priced",
                }]}
            return super().get(path, params)

    root = tmp_path / "feed"
    manifest = _fixture(root)
    feed = OrderFeed(tmp_path / "workspace", client=CostDeltaClient(manifest), feed_root=root)
    feed.sync()
    ingestion = Ingestion(model=None)  # type: ignore[arg-type]
    feed.append_to(ingestion, Store(id="taobao_test", name="淘宝测试店", platform="taobao"))
    cost = ingestion.frames_of("order_cost")[0].frame
    row = cost.filter(pl.col("sku") == "SKU1")
    assert row.height == 1
    assert row.row(0, named=True)["internal_order_id"] == "1"
    assert row.row(0, named=True)["order_id"] == "ON1"
    assert row.row(0, named=True)["unit_cost"] == pytest.approx(4.25)


def test_rows_the_console_marks_suspect_carry_no_cost(tmp_path):
    """is_suspect means the console kept the source row but distrusts its amount
    and quantity. Cost is price x quantity, so it goes too - even when the ratio
    guard alone would have let the row through."""
    root = tmp_path / "feed"
    manifest = _fixture(root)
    objects = manifest["objects"]
    objects["order_items.parquet"] = _write(root, "order_items.parquet", pl.DataFrame({
        "order_id": ["1"] * 2, "sub_order_id": ["11", "12"], "online_order_no": ["ON1"] * 2,
        "sku_id": ["SKU1", "SUS"], "merchant_sku": ["P1", "P2"], "outer_sku": ["S1", "S2"],
        "product_name": ["商品", "配件"], "quantity": ["1", "2043"], "unit_price": ["20.00", "9999.00"],
        "line_amount": ["20.00", "20437957.00"], "paid_amount": ["20.00", None], "refund_amount": ["0.00"] * 2,
        "tracking_no": ["SF1"] * 2, "is_gift": [False, False], "is_suspect": [False, True],
    }))
    objects["order_costs.parquet"] = _write(root, "order_costs.parquet", pl.DataFrame({
        "order_id": ["1"] * 2, "sub_order_id": ["11", "12"], "sku_id": ["SKU1", "SUS"],
        "quantity": ["1", "2043"], "unit_cost": ["3.50", "0.50"], "cost_amount": ["3.50", "1021.50"],
        "cost_source": ["history", "mirror"], "cost_status": ["priced"] * 2,
    }))
    (root / "current" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    feed = OrderFeed(tmp_path / "workspace", client=FakeClient(manifest), feed_root=root)
    feed.sync()
    ingestion = Ingestion(model=None)  # type: ignore[arg-type]
    feed.append_to(ingestion, Store(id="taobao_test", name="淘宝测试店", platform="taobao"))
    cost = ingestion.frames_of("order_cost")[0]
    assert cost.frame.get_column("sku").to_list() == ["SKU1"]
    assert any("is_suspect 的商品行 1 行" in note for note in cost.notes)
    assert not any("成本超过售价" in note for note in cost.notes)


def test_unmapped_store_is_a_feed_error_not_a_crash(tmp_path):
    root = tmp_path / "feed"
    manifest = _fixture(root)
    feed = OrderFeed(tmp_path / "workspace", client=FakeClient(manifest), feed_root=root)
    feed.sync()
    with pytest.raises(OrderFeedError, match="已确认店铺映射"):
        feed.append_to(
            Ingestion(model=None),  # type: ignore[arg-type]
            Store(id="pdd_nobody", name="没映射的店", platform="pdd"),
        )


def test_feed_without_sku_and_without_upload_says_so(tmp_path):
    root = tmp_path / "feed"
    manifest = _fixture(root, after_sku=None)
    feed = OrderFeed(tmp_path / "workspace", client=FakeClient(manifest), feed_root=root)
    feed.sync()
    ingestion = Ingestion(model=None)  # type: ignore[arg-type]
    feed.append_to(ingestion, Store(id="taobao_test", name="淘宝测试店", platform="taobao"))
    live = ingestion.frames_of("after_sales")[0]
    assert any("没有上传聚水潭售后单" in note for note in live.notes)
    assert any("有 0 行带商品编码" in note for note in live.notes)


def test_disk_current_snapshot_wins_over_stale_health_announce(tmp_path):
    """8001 health can still name the old snapshot after current/ and GET snapshot
    have moved. Ledger must follow the disk pointer, not wait for health."""
    root = tmp_path / "feed"
    old = _fixture(root)
    feed = OrderFeed(tmp_path / "workspace", client=FakeClient(old), feed_root=root)
    feed.sync()
    assert feed.state()["snapshot_id"] == "s1"

    newer = {**old, "snapshot_id": "s2", "revision": 12}
    (root / "current" / "manifest.json").write_text(json.dumps(newer), encoding="utf-8")

    class StaleHealth(FakeClient):
        def revision(self, etag: str = ""):
            self.calls.append("revision")
            if etag == '"rev-11"':
                return {
                    "schema_version": "ledger-feed.v1", "revision": 12,
                    "latest_seq": 12, "healthy": True,
                }, '"rev-12"'
            return super().revision(etag)

        def get(self, path, params=None):
            if path == "health":
                self.calls.append(path)
                return {
                    "healthy": True, "quality_risks": [],
                    "last_successful_snapshot": "s1",
                }
            if path == "changes":
                self.calls.append(path)
                after = int((params or {}).get("after_seq", 0))
                return {"to_seq": after, "has_more": False, "changes": []}
            return super().get(path, params)

    feed.client = StaleHealth(newer)
    result = feed.sync()
    assert result.snapshot_changed
    assert result.snapshot_id == "s2"
    assert feed.state()["snapshot_id"] == "s2"


def test_caught_up_304_hot_path_calls_only_revision(tmp_path):
    root = tmp_path / "feed"
    client = FakeClient(_fixture(root))
    feed = OrderFeed(tmp_path / "workspace", client=client, feed_root=root)
    feed.sync()
    client.calls.clear()

    result = feed.sync()

    assert result.caught_up
    assert client.calls == ["revision"]


def test_304_store_fallback_refreshes_only_stores(tmp_path):
    root = tmp_path / "feed"
    client = FakeClient(_fixture(root))
    feed = OrderFeed(tmp_path / "workspace", client=client, feed_root=root)
    feed.sync()
    with feed._connect() as conn:  # noqa: SLF001 - force the periodic fallback due
        conn.execute("update feed_state set stores_refreshed_at=0 where id=1")
    client.calls.clear()

    feed.sync()

    assert client.calls == ["revision", "stores"]


def test_304_continues_local_backlog_without_health_or_stores(tmp_path):
    root = tmp_path / "feed"
    client = FakeClient(_fixture(root))
    feed = OrderFeed(tmp_path / "workspace", client=client, feed_root=root)
    feed.sync()
    with feed._connect() as conn:  # noqa: SLF001 - emulate a bounded prior replay
        conn.execute(
            "update feed_state set consumed_seq=10,source_latest_seq=11 where id=1"
        )
    client.calls.clear()

    result = feed.sync()

    assert result.consumed_seq == 11
    assert "health" not in client.calls
    assert "snapshot" not in client.calls
    assert "stores" not in client.calls
    assert client.calls[:2] == ["revision", "changes"]


def test_store_change_refreshes_mapping_without_polling_stores_every_round(tmp_path):
    root = tmp_path / "feed"

    class StoreChangeClient(FakeClient):
        def revision(self, etag: str = ""):
            self.calls.append("revision")
            if etag == '"rev-11"':
                return {
                    "schema_version": "ledger-feed.v1", "revision": 12,
                    "latest_seq": 12, "healthy": True,
                }, '"rev-12"'
            return super().revision(etag)

        def get(self, path, params=None):
            if path == "changes" and int((params or {}).get("after_seq", 0)) == 11:
                self.calls.append(path)
                return {"to_seq": 12, "has_more": False, "changes": [{
                    "seq": 12, "revision": 12, "entity_type": "store",
                    "entity_id": "10", "operation": "upsert", "order_store_id": "10",
                    "entity_href": "/api/integration/ledger/v1/entities/store/10",
                }]}
            if path == "entities/store/10":
                self.calls.append(path)
                return {"store": {
                    "order_store_id": "10", "ledger_store_id": "taobao_changed",
                    "mapping_status": "confirmed",
                }}
            return super().get(path, params)

    client = StoreChangeClient(_fixture(root))
    feed = OrderFeed(tmp_path / "workspace", client=client, feed_root=root)
    feed.sync()
    client.calls.clear()

    result = feed.sync()

    assert result.consumed_seq == 12
    assert client.calls.count("stores") == 1
    assert client.calls[:3] == ["revision", "health", "changes"]


def test_noisy_store_last_seen_event_does_not_pull_full_registry(tmp_path):
    root = tmp_path / "feed"

    class NoisyStoreClient(FakeClient):
        def revision(self, etag: str = ""):
            self.calls.append("revision")
            if etag == '"rev-11"':
                return {
                    "schema_version": "ledger-feed.v1", "revision": 12,
                    "latest_seq": 12, "healthy": True,
                }, '"rev-12"'
            return super().revision(etag)

        def get(self, path, params=None):
            if path == "changes" and int((params or {}).get("after_seq", 0)) == 11:
                self.calls.append(path)
                return {"to_seq": 12, "has_more": False, "changes": [{
                    "seq": 12, "revision": 12, "entity_type": "store",
                    "entity_id": "10", "operation": "upsert", "order_store_id": "10",
                    "entity_href": "/api/integration/ledger/v1/entities/store/10",
                }]}
            if path == "entities/store/10":
                self.calls.append(path)
                return {"store": {
                    "order_store_id": "10", "ledger_store_id": "taobao_test",
                    "mapping_status": "confirmed", "last_seen_at": "later",
                }}
            return super().get(path, params)

    client = NoisyStoreClient(_fixture(root))
    feed = OrderFeed(tmp_path / "workspace", client=client, feed_root=root)
    feed.sync()
    client.calls.clear()

    feed.sync()

    assert "stores" not in client.calls


def test_304_backlog_never_bypasses_cached_unhealthy_state(tmp_path):
    root = tmp_path / "feed"
    client = FakeClient(_fixture(root))
    feed = OrderFeed(tmp_path / "workspace", client=client, feed_root=root)
    feed.sync()
    with feed._connect() as conn:  # noqa: SLF001 - emulate a degraded prior probe
        conn.execute(
            "update feed_state set consumed_seq=10,source_latest_seq=11,health_json=? where id=1",
            (json.dumps({"healthy": False, "degraded": ["cost_api_worker"]}),),
        )
    client.calls.clear()

    try:
        feed.sync()
    except OrderFeedError as exc:
        assert "cost_api_worker" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("cached unhealthy state was bypassed")
    assert client.calls == ["revision"]


def test_snapshot_without_through_seq_is_rejected(tmp_path):
    root = tmp_path / "feed"
    manifest = _fixture(root)
    manifest.pop("through_seq")
    feed = OrderFeed(tmp_path / "workspace", client=FakeClient(manifest), feed_root=root)
    try:
        feed.sync()
    except OrderFeedError as exc:
        assert "through_seq" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("missing through_seq was accepted")


def test_external_revision_marks_a_closed_period_stale_idempotently(tmp_path):
    ws = Workspace(tmp_path / "workspace")
    ws.record("taobao_test", "2026-06", {"can_close": True}, ["raw"])
    ws.close_period("taobao_test", "2026-06", by="tester")
    assert ws.state("taobao_test", "2026-06").stale is False

    assert ws.note_external_version(
        "taobao_test", "__order_console__", "order-feed:s1:11",
    )
    assert ws.state("taobao_test", "2026-06").stale is True
    assert not ws.note_external_version(
        "taobao_test", "__order_console__", "order-feed:s1:11",
    )


def test_replayed_upsert_whose_current_entity_is_gone_becomes_tombstone(tmp_path):
    root = tmp_path / "feed"
    manifest = _fixture(root)

    class MissingClient(FakeClient):
        def get(self, path, params=None):
            if path in {"entities/order/1", "orders/1"}:
                raise OrderFeedNotFound("gone")
            return super().get(path, params)

    feed = OrderFeed(tmp_path / "workspace", client=MissingClient(manifest), feed_root=root)
    result = feed.sync()
    assert result.consumed_seq == 11
    with feed._connect() as conn:  # noqa: SLF001 - verifies durable replay semantics
        row = conn.execute(
            "select operation,payload_json from feed_entity where entity_type='order' and entity_id='1'"
        ).fetchone()
    assert tuple(row) == ("delete", None)


def test_feed_order_time_builds_a_real_month_slice(tmp_path):
    root = tmp_path / "feed"
    manifest = _fixture(root)
    feed = OrderFeed(tmp_path / "workspace", client=FakeClient(manifest), feed_root=root)
    feed.sync()
    model = ModelRepository(
        Path(__file__).resolve().parents[2] / "models" / "cn-ecommerce"
    ).get().model
    store = model.store("taobao_msy387nx")
    with feed._connect() as conn:  # noqa: SLF001 - fixture remaps one synthetic shop
        conn.execute(
            "update feed_store set ledger_store_id=? where order_store_id='10'", (store.id,)
        )
    ingestion = Ingestion(model=model)
    feed.append_to(ingestion, store)
    result = run(ingestion, store.platform)
    assert result.spine.get_column(SPINE_PERIOD).to_list() == ["2026-06"]
    assert result.spine.get_column(SPINE_STORE).to_list() == [store.name]


def test_existing_certified_spine_is_enriched_not_duplicated(tmp_path):
    root = tmp_path / "feed"
    manifest = _fixture(root)
    feed = OrderFeed(tmp_path / "workspace", client=FakeClient(manifest), feed_root=root)
    feed.sync()
    ref = FileRef("manual", "人工订单.xlsx", "订单")
    template = feed._order_template()  # noqa: SLF001 - compatible normalized fixture
    manual = Ingested(
        ref=ref,
        recognition=Recognition(
            ref=ref, signature="manual", header_count=3,
            template_id="manual_order", source_id="order_detail",
        ),
        rows=1,
        frame=pl.DataFrame({
            "order_id": ["ON1"], "sub_order_id": ["S1"],
            "refund_status": ["没有申请退款"], "tracking_no": [None],
        }),
        template=template,
    )
    ingestion = Ingestion(model=None, items=[manual])  # type: ignore[arg-type]
    feed.append_to(
        ingestion, Store(id="taobao_test", name="淘宝测试店", platform="taobao")
    )
    existing, live_only = ingestion.frames_of("order_detail")
    assert existing.frame.row(0, named=True)["refund_status"] == "退款成功"
    assert existing.frame.row(0, named=True)["tracking_no"] == "SF1"
    assert live_only.frame.is_empty()


def test_live_product_projection_never_spreads_money_across_months():
    model = ModelRepository(
        Path(__file__).resolve().parents[2] / "models" / "cn-ecommerce"
    ).get().model
    metric = model.metric("ad_cost")
    facts = pl.DataFrame({
        "metric_id": ["ad_cost", "ad_cost"],
        "link_key": ["P1", "P1"],
        "amount": [-100.0, -200.0],
        "store": ["淘宝店", "淘宝店"],
        "period": ["2026-06", "2026-07"],
    })
    spine = Spine(pl.DataFrame({
        "product_id": ["P1", "P1"],
        SPINE_STORE: ["淘宝店", "淘宝店"],
        SPINE_PERIOD: ["2026-06", "2026-07"],
    }))
    projected = _project_scoped_live(facts, metric, spine).facts
    totals = {
        period: amount for period, amount in
        projected.group_by("period").agg(pl.col("amount").sum()).iter_rows()
    }
    assert totals == {"2026-06": -100.0, "2026-07": -200.0}


def test_live_order_projection_never_halves_a_later_month_receipt():
    """同一主订单跨两个月各有一行时，7月收款不能一半分回6月。

    生产实例 6927467973411634881 的 71.37 元曾因此只进 35.685 元。
    """
    model = ModelRepository(
        Path(__file__).resolve().parents[2] / "models" / "cn-ecommerce"
    ).get().model
    metric = model.metric("trade_receipt_douyin")
    facts = pl.DataFrame({
        "metric_id": ["trade_receipt_douyin"],
        "major": ["trade_receipt"],
        "link_key": ["6927467973411634881"],
        "amount": [71.37],
        "store": ["抖音浅花涧节日装饰"],
        "period": ["2026-07"],
    })
    spine = Spine(pl.DataFrame({
        "order_id": ["6927467973411634881", "6927467973411634881"],
        SPINE_STORE: ["抖音浅花涧节日装饰", "抖音浅花涧节日装饰"],
        SPINE_PERIOD: ["2026-06", "2026-07"],
    }))
    projected = _project_scoped_live(facts, metric, spine).facts
    assert projected.select("period", "amount").to_dicts() == [
        {"period": "2026-07", "amount": 71.37},
    ]


def test_reship_target_cannot_receive_sales_money_but_still_receives_reship_cost(tmp_path):
    """补发单复用原线上订单号时，只承接补发成本，不能冒充原销售订单。"""
    from ledger.engine.link import LINKED, link
    from ledger.engine.runtime import _spine_frame

    feed = OrderFeed(tmp_path / "workspace")
    orders = pl.DataFrame({
        "order_id": ["15545141"], "online_order_no": ["6926682603205983993"],
        "order_time": ["2026-06-25 09:25:37"], "pay_time": ["2026-06-25 09:25:37"],
        "order_status_raw": ["Sent"], "paid_amount": ["0.00"],
        "refund_amount": ["0.00"], "tracking_no": ["321211088703818"],
    })
    items = pl.DataFrame({
        "order_id": ["15545141"], "sub_order_id": ["50017304"],
        "online_order_no": ["6926682603205983993"], "sku_id": ["HSC34648"],
        "merchant_sku": ["3816860347843871377"], "outer_sku": ["$Asr-1022109179"],
        "product_name": ["商品"], "paid_amount": [None], "refund_amount": ["0.00"],
        "tracking_no": ["321211088703818"],
    })
    after = pl.DataFrame(schema={"order_id": pl.Utf8, "online_status_raw": pl.Utf8})
    relations = pl.DataFrame({
        "relation_type": ["reship"], "source_order_id": ["15188247"],
        "target_order_id": ["15545141"],
    })
    store = Store(id="douyin_test", name="抖音测试店", platform="douyin")
    order_frame = feed._order_frame(  # noqa: SLF001 - verify the normalized contract
        orders, items, after, relations, store, "fingerprint",
    )
    assert order_frame.get_column("order_type").to_list() == ["补发订单"]

    spine = Spine(_spine_frame(order_frame, feed._order_template()))  # noqa: SLF001
    model = ModelRepository(
        Path(__file__).resolve().parents[2] / "models" / "cn-ecommerce"
    ).get().model
    receipt = model.metric("trade_receipt_douyin")
    receipt_rows, _ = link(
        pl.DataFrame({"base_order_id": ["6926682603205983993"]}), receipt, spine,
    )
    assert receipt_rows.get_column(LINKED).to_list() == [False]

    reshipment = model.metric("reshipment_cost")
    cost_rows, _ = link(
        pl.DataFrame({"original_order_id": ["6926682603205983993"]}), reshipment, spine,
    )
    assert cost_rows.get_column(LINKED).to_list() == [True]


def test_a_real_sale_wins_when_a_reship_reuses_the_same_online_and_suborder(tmp_path):
    """原销售单仍在6月范围内时，不能因为随后补发而把真实货款一起排除。"""
    feed = OrderFeed(tmp_path / "workspace")
    orders = pl.DataFrame({
        "order_id": ["source", "target"], "online_order_no": ["ON1", "ON1"],
        "order_time": ["2026-06-20 09:00:00", "2026-06-25 09:00:00"],
        "pay_time": ["2026-06-20 09:01:00", "2026-06-25 09:01:00"],
        "order_status_raw": ["Sent", "Sent"], "paid_amount": ["20.00", "0.00"],
        "refund_amount": ["0.00", "0.00"], "tracking_no": ["SF1", "SF2"],
    })
    items = pl.DataFrame({
        "order_id": ["source", "target"], "sub_order_id": ["1", "2"],
        "online_order_no": ["ON1", "ON1"], "sku_id": ["A", "A"],
        "merchant_sku": ["P1", "P1"], "outer_sku": ["SAME", "SAME"],
        "product_name": ["商品", "商品"], "paid_amount": ["20.00", None],
        "refund_amount": ["0.00", "0.00"], "tracking_no": ["SF1", "SF2"],
    })
    after = pl.DataFrame(schema={"order_id": pl.Utf8, "online_status_raw": pl.Utf8})
    relations = pl.DataFrame({
        "relation_type": ["reship"], "source_order_id": ["source"],
        "target_order_id": ["target"],
    })
    store = Store(id="douyin_test", name="抖音测试店", platform="douyin")
    frame = feed._order_frame(  # noqa: SLF001
        orders, items, after, relations, store, "fingerprint",
    )
    assert frame.height == 1
    assert frame.get_column("order_type").item() == "销售订单"


def test_live_rows_without_accounting_date_never_create_a_null_period():
    facts = pl.DataFrame({
        "store": ["淘宝店", "淘宝店", "(未知店铺)"],
        "period": [None, "2026-06", "2026-06"],
    })
    assert _slice_keys(facts) == [("淘宝店", "2026-06")]
