"""Source query boundaries cannot silently erase earlier uploaded evidence."""
from __future__ import annotations

from copy import deepcopy
from datetime import date

import polars as pl
import pytest

from ledger.engine.runtime import Ingested, Ingestion
from ledger.engine.types import FileRef, Recognition
from ledger.order_feed import OrderFeed, OrderFeedError, producer_cost_scope
from ledger.model.schema import Store
from test_order_feed import FakeClient, _fixture


STORE = Store(id="taobao_test", name="淘宝测试店", platform="taobao")


def declaration(manifest, *, start="2026-06-01", stop=None):
    return {
        "schema_version": "producer_query_scope.v1",
        "snapshot_id": manifest["snapshot_id"], "producer_sha256": "a" * 64,
        "objects": {name: {"basis": "header.order_date", "from_inclusive": start,
                           "to_exclusive": stop, "complete": True,
                           "sha256": manifest["objects"][name]["sha256"]}
                    for name in ("orders.parquet", "order_items.parquet", "order_costs.parquet")},
    }


def setup_feed(tmp_path, *, declared=True):
    root = tmp_path / "feed"
    manifest = _fixture(root)
    if declared:
        manifest["producer_query_scope"] = declaration(manifest)
    feed = OrderFeed(tmp_path / "ws", client=FakeClient(manifest), feed_root=root)
    feed.sync()
    return feed, manifest


def source_item(feed, **overrides):
    row = {
        "internal_order_id": "old-parent", "internal_sub_order_id": "old-item",
        "order_id": "OLD-ONLINE", "original_order_id": "OLD-ONLINE",
        "sub_order_id": "OLD-CHILD", "sku": "SKU-OLD", "quantity": 2.0,
        "unit_cost": 1.234567, "total_cost": 2.469134,
        "order_time": "2026-05-31 23:59:59", "store_name": STORE.name,
        "order_state": "已发货", "order_type": "销售订单", "order_flag": None,
        "__anchor_row__": 82, "__anchor_sha__": "source-original-sha",
    }
    row.update(overrides)
    ref = FileRef("source-original-sha", "unchanged-original.xlsx", "商品")
    template = feed._cost_template().model_copy(update={"id": "jushuitan_cost_v1"})
    frame = pl.DataFrame([row])
    return Ingested(ref, Recognition(ref, "source", 12, template.id, "order_cost"),
                    frame.height, frame, template)


def retained(ingestion):
    return [item for item in ingestion.frames_of("order_cost")
            if item.template.id == "jushuitan_cost_v1"]


def test_append_preserves_exact_original_outside_explicit_window(tmp_path):
    feed, _ = setup_feed(tmp_path)
    original = source_item(feed)
    before = original.frame.clone()
    ingestion = Ingestion(model=None, items=[original])
    feed.append_to(ingestion, STORE)
    kept, = retained(ingestion)
    assert kept.frame.drop("__live_period_scope__").equals(before)
    assert kept.ref is original.ref and kept.template is original.template
    assert kept.frame["unit_cost"].item() == 1.234567
    assert kept.frame["__anchor_row__"].item() == 82
    assert original.frame.equals(before)
    # Retry is an overlay, not a second contribution of the same source row.
    feed.append_to(ingestion, STORE)
    assert len(retained(ingestion)) == 1
    assert retained(ingestion)[0].frame.equals(before.with_columns(pl.lit(True).alias("__live_period_scope__")))


@pytest.mark.parametrize("time,keep", [
    ("2026-05-31 23:59:59", True), ("2026-06-01 00:00:00", False),
    ("2026-06-30 23:59:59", False), ("2026-07-01 00:00:00", True),
])
def test_half_open_window_bounds_are_not_observed_min_max(tmp_path, time, keep):
    feed, manifest = setup_feed(tmp_path)
    manifest["producer_query_scope"] = declaration(manifest, stop="2026-07-01")
    # The observed snapshot has only June 2; that cannot shorten its query scope.
    manifest["date_range"] = {"from": "2026-06-02", "to": "2026-06-02"}
    feed._frames(STORE, manifest)
    output = feed._costs_outside_source_scope(Ingestion(model=None, items=[source_item(feed, order_time=time)]), STORE, manifest)
    assert bool(output) is keep


@pytest.mark.parametrize("field,value", [
    ("internal_order_id", "1"), ("internal_sub_order_id", "11"),
    ("order_id", "ON1"), ("original_order_id", "ON1"), ("sub_order_id", "S1"),
])
def test_captured_identity_owns_old_row_even_outside_window(tmp_path, field, value):
    feed, _ = setup_feed(tmp_path)
    changes = {field: value}
    if field not in ("internal_order_id", "internal_sub_order_id"):
        changes.update(internal_order_id=None, internal_sub_order_id=None)
    original = source_item(feed, **changes)
    ingestion = Ingestion(model=None, items=[original])
    feed.append_to(ingestion, STORE)
    assert not retained(ingestion)


def test_later_physical_reship_with_same_platform_ids_does_not_erase_original_sale(tmp_path):
    feed, _ = setup_feed(tmp_path)
    original = source_item(feed, order_id="ON1", original_order_id="ON1", sub_order_id="S1")
    ingestion = Ingestion(model=None, items=[original])
    feed.append_to(ingestion, STORE)
    kept, = retained(ingestion)
    assert kept.frame["internal_order_id"].item() == "old-parent"
    assert kept.frame["internal_sub_order_id"].item() == "old-item"
    assert kept.frame["unit_cost"].item() == 1.234567


def test_missing_current_price_cannot_resurrect_old_price(tmp_path):
    feed, manifest = setup_feed(tmp_path)
    path = feed.feed_root / manifest["objects"]["order_costs.parquet"]["path"]
    frame = pl.read_parquet(path).with_columns(pl.lit(None, dtype=pl.String).alias("unit_cost"))
    frame.write_parquet(path)  # Synthetic source fixture only.
    ingestion = Ingestion(model=None, items=[source_item(feed, internal_order_id="1")])
    feed.append_to(ingestion, STORE)
    assert not retained(ingestion)
    assert all(item.ref.sha256 != "source-original-sha" for item in ingestion.frames_of("order_cost"))


@pytest.mark.parametrize("kind,entity", [("order", "old-parent"), ("order_item", "old-item"), ("order_cost", "old-item")])
def test_tombstone_prevents_older_uploaded_row_from_returning(tmp_path, kind, entity):
    feed, _ = setup_feed(tmp_path)
    with feed._connect() as conn:
        conn.execute("INSERT INTO feed_entity(entity_type,entity_id,seq,revision,operation,order_store_id,order_id,sub_order_id) VALUES(?,?,12,12,'delete','10',?,?)",
                     (kind, entity, "old-parent", "old-item"))
    ingestion = Ingestion(model=None, items=[source_item(feed)])
    feed.append_to(ingestion, STORE)
    assert not retained(ingestion)


@pytest.mark.parametrize("kind", ["order_item", "order_cost"])
def test_one_deleted_old_member_does_not_delete_its_unrepresented_siblings(tmp_path, kind):
    feed, _ = setup_feed(tmp_path)
    with feed._connect() as conn:
        conn.execute("INSERT INTO feed_entity(entity_type,entity_id,seq,revision,operation,order_store_id,order_id,sub_order_id) VALUES(?,'old-other-item',12,12,'delete','10','old-parent','old-other-item')", (kind,))
    ingestion = Ingestion(model=None, items=[source_item(feed)])
    feed.append_to(ingestion, STORE)
    kept, = retained(ingestion)
    assert kept.frame["internal_sub_order_id"].item() == "old-item"


@pytest.mark.parametrize("kind,entity", [("order", "old-parent"), ("order_item", "old-item"), ("order_cost", "old-item")])
@pytest.mark.parametrize("shop", [None, ""])
def test_unscoped_delete_intersects_only_this_shops_exact_source_ids(tmp_path, kind, entity, shop):
    feed, _ = setup_feed(tmp_path)
    with feed._connect() as conn:
        conn.execute("INSERT INTO feed_entity(entity_type,entity_id,seq,revision,operation,order_store_id) VALUES(?,?,12,12,'delete',?)", (kind, entity, shop))
    ingestion = Ingestion(model=None, items=[source_item(feed)])
    feed.append_to(ingestion, STORE)
    assert not retained(ingestion)


@pytest.mark.parametrize("kind,entity", [("order", "old-item"), ("order_cost", "old-parent"), ("order_item", "unrelated")])
def test_unscoped_delete_does_not_cross_namespaces_or_unrelated_entities(tmp_path, kind, entity):
    feed, _ = setup_feed(tmp_path)
    with feed._connect() as conn:
        conn.execute("INSERT INTO feed_entity(entity_type,entity_id,seq,revision,operation) VALUES(?,?,12,12,'delete')", (kind, entity))
    ingestion = Ingestion(model=None, items=[source_item(feed)])
    feed.append_to(ingestion, STORE)
    assert len(retained(ingestion)) == 1


def test_other_shops_source_row_does_not_bind_an_unscoped_delete(tmp_path):
    feed, _ = setup_feed(tmp_path)
    with feed._connect() as conn:
        conn.execute("INSERT INTO feed_entity(entity_type,entity_id,seq,revision,operation) VALUES('order','other-shop-parent',12,12,'delete')")
    mine = source_item(feed)
    other = source_item(feed, internal_order_id="other-shop-parent", store_name="另一家店")
    ingestion = Ingestion(model=None, items=[mine, other])
    feed.append_to(ingestion, STORE)
    kept, = retained(ingestion)
    assert kept.frame["internal_order_id"].to_list() == ["old-parent"]


def test_conflicting_unscoped_tombstone_cannot_broadcast_a_second_identity(tmp_path):
    feed, _ = setup_feed(tmp_path)
    with feed._connect() as conn:
        conn.execute("INSERT INTO feed_entity(entity_type,entity_id,seq,revision,operation,order_id) VALUES('order','old-parent',12,12,'delete','unrelated-parent')")
    with pytest.raises(OrderFeedError, match="source_scope_unverified"):
        feed.append_to(Ingestion(model=None, items=[source_item(feed)]), STORE)


def test_relation_superseded_parent_is_not_an_absent_legacy_order(tmp_path):
    feed, _ = setup_feed(tmp_path)
    with feed._connect() as conn:
        import json
        payload = {"order_relation": {"relation_type": "split", "source_order_id": "old-parent", "target_order_id": "1"}}
        conn.execute("INSERT INTO feed_entity(entity_type,entity_id,seq,revision,operation,order_store_id,payload_json) VALUES('order_relation','r',12,12,'upsert','10',?)", (json.dumps(payload),))
    ingestion = Ingestion(model=None, items=[source_item(feed)])
    feed.append_to(ingestion, STORE)
    assert not retained(ingestion)


def test_reship_relation_does_not_supersede_its_original_sale_cost(tmp_path):
    import json
    feed, _ = setup_feed(tmp_path)
    with feed._connect() as conn:
        payload = {"order_relation": {"relation_type": "reship", "source_order_id": "old-parent", "target_order_id": "1"}}
        conn.execute("INSERT INTO feed_entity(entity_type,entity_id,seq,revision,operation,order_store_id,order_id,payload_json) VALUES('order_relation','r',12,12,'upsert','10','old-parent',?)", (json.dumps(payload),))
    ingestion = Ingestion(model=None, items=[source_item(feed)])
    feed.append_to(ingestion, STORE)
    kept, = retained(ingestion)
    assert kept.frame["internal_order_id"].item() == "old-parent"


def test_unknown_scope_fails_before_mutating_original_ingestion(tmp_path):
    feed, _ = setup_feed(tmp_path, declared=False)
    original = source_item(feed)
    before = original.frame.clone()
    ingestion = Ingestion(model=None, items=[original])
    with pytest.raises(OrderFeedError, match="source_scope_unverified"):
        feed.append_to(ingestion, STORE)
    assert ingestion.items == [original] and original.frame.equals(before)


def test_old_manifest_can_still_replace_an_explicitly_captured_entity(tmp_path):
    feed, _ = setup_feed(tmp_path, declared=False)
    ingestion = Ingestion(model=None, items=[source_item(feed, internal_order_id="1")])
    feed.append_to(ingestion, STORE)
    assert not retained(ingestion)


@pytest.mark.parametrize("day", [None, "", "2026-02-30", "May 31, 2026"])
def test_unverifiable_legacy_day_does_not_become_zero(tmp_path, day):
    feed, _ = setup_feed(tmp_path)
    with pytest.raises(OrderFeedError, match="source_scope_unverified"):
        feed.append_to(Ingestion(model=None, items=[source_item(feed, order_time=day)]), STORE)


@pytest.mark.parametrize("field,value", [
    ("basis", "observed.order_time"), ("complete", False), ("complete", 1),
    ("sha256", "a" * 64), ("from_inclusive", "20260601"),
    ("to_exclusive", "2026-06-01"), ("from_inclusive", "2026-02-30"),
])
def test_invalid_object_contract_is_rejected(tmp_path, field, value):
    _, manifest = setup_feed(tmp_path)
    manifest = deepcopy(manifest)
    manifest["producer_query_scope"]["objects"]["order_costs.parquet"][field] = value
    with pytest.raises(OrderFeedError, match="source_scope_unverified"):
        producer_cost_scope(manifest)


def test_observed_range_alone_never_proves_query_completeness():
    assert producer_cost_scope({"date_range": {"from": "2026-06-01", "to": "2026-10-27"}}) is None
    m = {"snapshot_id": "synthetic", "objects": {name: {"sha256": "b" * 64}
         for name in ("orders.parquet", "order_items.parquet", "order_costs.parquet")}}
    m["producer_query_scope"] = declaration(m)
    assert producer_cost_scope(m) == (date(2026, 6, 1), None)


@pytest.mark.parametrize("field,value", [("snapshot_id", "other"), ("producer_sha256", "unknown"), ("objects", {})])
def test_snapshot_and_producer_identity_are_required(tmp_path, field, value):
    _, manifest = setup_feed(tmp_path)
    manifest["producer_query_scope"][field] = value
    with pytest.raises(OrderFeedError, match="source_scope_unverified"):
        producer_cost_scope(manifest)


def test_scope_declaration_participates_in_input_identity(tmp_path):
    import json
    feed, manifest = setup_feed(tmp_path)
    before = feed.fingerprint()
    manifest["producer_query_scope"] = declaration(manifest, start="2026-05-01")
    with feed._connect() as conn:
        conn.execute("UPDATE feed_state SET manifest_json=? WHERE id=1", (json.dumps(manifest),))
    assert feed.fingerprint() != before


def test_different_object_predicates_are_not_a_complete_cost_window(tmp_path):
    _, manifest = setup_feed(tmp_path)
    manifest["producer_query_scope"]["objects"]["order_items.parquet"]["from_inclusive"] = "2026-05-01"
    with pytest.raises(OrderFeedError, match="source_scope_unverified"):
        producer_cost_scope(manifest)
