from __future__ import annotations

import hashlib
import json
from pathlib import Path

import polars as pl

from ledger.engine.runtime import Ingested, Ingestion, _project_scoped_live, run
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


def _fixture(root: Path) -> dict:
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
            "order_id": ["1"], "sub_order_id": ["11"], "online_order_no": ["ON1"],
            "sku_id": ["SKU1"], "merchant_sku": ["P1"], "outer_sku": ["S1"],
            "product_name": ["商品"],
            "quantity": ["2"], "paid_amount": ["20.00"], "refund_amount": ["0.00"],
            "tracking_no": ["SF1"],
        })),
        "after_sales.parquet": _write(root, "after_sales.parquet", pl.DataFrame({
            "after_sale_id": ["A1"], "order_id": ["1"], "online_order_no": ["ON1"],
            "sub_order_id": ["11"], "order_store_id": ["10"],
            "goods_status_raw": ["买家未收到货"], "online_status_raw": ["退款成功"],
            "refund_status_raw": ["退款成功"],
        })),
        "after_sale_items.parquet": _write(root, "after_sale_items.parquet", pl.DataFrame({
            "after_sale_id": ["A1"], "order_id": ["1"], "sub_order_id": ["11"],
            "sku_id": ["SKU1"], "quantity": ["2"],
        })),
        "order_costs.parquet": _write(root, "order_costs.parquet", pl.DataFrame({
            "order_id": ["1"], "sub_order_id": ["11"], "sku_id": ["SKU1"],
            "quantity": ["2"], "unit_cost": ["3.50"], "cost_amount": ["7.00"],
            "cost_source": ["history"], "cost_status": ["priced"],
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

    def get(self, path: str, params=None):
        if path == "revision":
            return {"schema_version": "ledger-feed.v1", "revision": 11, "healthy": True}
        if path == "health":
            return {"healthy": True, "quality_risks": []}
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
    assert cost is not None and cost.row(0, named=True)["unit_cost"] == 3.5
    assert cost.row(0, named=True)["order_type"] == "销售订单"
    assert after is not None and after.row(0, named=True)["goods_status"] == "买家未收到货"


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
