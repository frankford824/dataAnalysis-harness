"""A merged ERP order whose platform orders were all refunded and returned has no goods cost.

Order Console attaches each after-sale to one representative product (sometimes a
line of a sibling platform order) while its quantity covers the whole platform order.
"""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from ledger.engine.runtime import Ingestion, run
from ledger.model.repository import ModelRepository
from ledger.order_feed import OrderFeed
from test_order_feed import FakeClient, _write

SHOP = "17265841"
P1, P2, P3 = "5127688873129011344", "5127631524117102545", "5127364658147011344"
LINES = [  # sub_order_id, platform order, sku, quantity, paid, unit_cost
    ("52015705", P2, "HBJ01100", "1", "18.88", "5.7249"),
    ("52015706", P3, "HQT22539", "4", "0.02", "0.0220"),
    ("52015707", P3, "HBJ01098", "2", "9.25", "0.7744"),
    ("52015708", P1, "HQT22539", "6", "5.92", "0.0220"),
    ("52015709", P1, "HZS08986", "1", "0.99", "0.8797"),
    ("52015710", P1, "HZS12652", "2", "1.97", "0.4842"),
]
# Production after-sales: one representative item per platform order. P1's item
# points at P2's line 52015705 but carries P1's total quantity 6+1+2.
AFTER = [  # after_sale_id, platform order, header sub, item sub, item sku, quantity
    ("1022217809", P2, "52015705", "52015705", "HBJ01100", "1"),
    ("1022217808", P1, "52015705", "52015705", "HBJ01100", "9"),
    ("1022217807", P3, "52015707", "52015707", "HBJ01098", "6"),
]
HEADER = {
    "order_id": "16046161", "order_store_id": SHOP,
    "online_order_no": f"{P1},{SHOP}:{P2},{SHOP}:{P3},{SHOP}:{P1}",
    "order_time": "2026-08-13 17:55:04", "pay_time": "2026-08-13 17:55:07",
    "order_status_raw": "Sent", "paid_amount": "55.63", "refund_amount": "37.03",
    "tracking_no": "SF1", "ship_time": "2026-08-14 11:24:07",
}


class MergedClient(FakeClient):
    def get(self, path: str, params=None):
        if path == "stores":
            return {"stores": [{"order_store_id": SHOP, "ledger_store_id": "alibaba1688_xingze",
                                "mapping_status": "confirmed"}]}
        if path == "changes":
            after = int((params or {}).get("after_seq", 0))
            if after >= 11:
                return {"to_seq": after, "has_more": False, "changes": []}
            return {"to_seq": 11, "has_more": False, "changes": [{
                "seq": 11, "revision": 11, "entity_type": "order", "entity_id": "16046161",
                "operation": "upsert", "order_store_id": SHOP, "order_id": "16046161",
                "entity_href": "/api/integration/ledger/v1/entities/order/16046161"}]}
        if path == "entities/order/16046161":
            return {"order": dict(HEADER), "items": []}
        return super().get(path, params)


def _feed(root: Path, after=AFTER, goods="卖家已收到退货", refund="退款成功", detail=None) -> dict:
    sub, parent, sku, qty, paid, unit = map(list, zip(*LINES))
    a_id, a_parent, a_sub, i_sub, i_sku, a_qty = map(list, zip(*after)) if after else ([],) * 6
    text = lambda values: pl.Series(values, dtype=pl.Utf8)
    if detail is None:
        items = pl.DataFrame({
            "after_sale_id": text(a_id), "order_id": text(["16046161"] * len(a_id)),
            "sub_order_id": text(i_sub), "sku_id": text(i_sku), "quantity": text(a_qty),
            "item_source": text(["inferred"] * len(a_id))})
    else:
        d_id, d_sub, d_sku, d_qty = map(list, zip(*detail))
        items = pl.DataFrame({
            "after_sale_id": text(d_id), "order_id": text(["16046161"] * len(d_id)),
            "sub_order_id": text(d_sub), "sku_id": text(d_sku), "quantity": text(d_qty),
            "item_source": text(["jst_detail"] * len(d_id))})
    objects = {
        "stores.parquet": _write(root, "stores.parquet", pl.DataFrame({
            "order_store_id": [SHOP], "ledger_store_id": ["alibaba1688_xingze"], "mapping_status": ["confirmed"]})),
        "orders.parquet": _write(root, "orders.parquet", pl.DataFrame({k: [v] for k, v in HEADER.items()})),
        "order_items.parquet": _write(root, "order_items.parquet", pl.DataFrame({
            "order_id": ["16046161"] * 6, "sub_order_id": sub, "order_store_id": [SHOP] * 6,
            "online_order_no": [f"{SHOP}:{p}" for p in parent], "outer_sku": parent,
            "sku_id": sku, "merchant_sku": sku, "product_name": ["剪纸"] * 6, "quantity": qty,
            "paid_amount": paid, "line_amount": paid, "refund_amount": ["0"] * 6, "tracking_no": ["SF1"] * 6})),
        "after_sales.parquet": _write(root, "after_sales.parquet", pl.DataFrame({
            "after_sale_id": text(a_id), "order_id": text(["16046161"] * len(a_id)),
            "online_order_no": text(a_parent), "sub_order_id": text(a_sub),
            "order_store_id": text([SHOP] * len(a_id)), "after_sale_type_raw": text(["普通退货"] * len(a_id)),
            "goods_status_raw": text([goods] * len(a_id)), "online_status_raw": text([refund] * len(a_id)),
            "refund_status_raw": text(["已确认"] * len(a_id)),
            "updated_at_source": text(["2026-08-24T14:09:57+08:00"] * len(a_id))})),
        "after_sale_items.parquet": _write(root, "after_sale_items.parquet", items),
        "order_costs.parquet": _write(root, "order_costs.parquet", pl.DataFrame({
            "order_id": ["16046161"] * 6, "sub_order_id": sub, "sku_id": sku, "quantity": qty,
            "unit_cost": unit, "cost_amount": [str(round(float(q) * float(u), 4)) for q, u in zip(qty, unit)],
            "cost_source": ["history"] * 6, "cost_status": ["priced"] * 6})),
        "order_relations.parquet": _write(root, "relations.parquet", pl.DataFrame(schema={
            name: pl.Utf8 for name in ("relation_type", "source_order_id", "target_order_id",
                                       "source_sub_order_id", "target_sub_order_id", "source", "observed_at")})),
        "controls.parquet": _write(root, "controls.parquet", pl.DataFrame({
            "order_store_id": [SHOP], "day": ["2026-08-13"], "item_count": [6]})),
    }
    manifest = {"snapshot_id": "s1", "schema_version": "ledger-feed.v1", "revision": 10,
                "through_seq": 10, "objects": objects}
    (root / "current").mkdir(parents=True)
    (root / "current" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def _goods_cost(tmp_path: Path, **kwargs) -> dict[str, float]:
    root = tmp_path / "feed"
    feed = OrderFeed(tmp_path / "workspace", client=MergedClient(_feed(root, **kwargs)), feed_root=root)
    feed.sync()
    model = ModelRepository(Path(__file__).resolve().parents[2] / "models" / "cn-ecommerce").get().model
    store = model.store("alibaba1688_xingze")
    ingestion = Ingestion(model=model)
    feed.append_to(ingestion, store)
    facts = run(ingestion, store.platform).facts.filter(pl.col("metric_id") == "goods_cost")
    assert facts.height == 6
    by_sku: dict[str, float] = {}
    for row in facts.iter_rows(named=True):
        by_sku[row["sku"]] = round(by_sku.get(row["sku"], 0.0) + float(row["amount"]), 4)
    return by_sku


def test_merged_order_fully_refunded_and_returned_has_zero_goods_cost(tmp_path):
    costs = _goods_cost(tmp_path)
    assert costs == dict.fromkeys(["HBJ01100", "HQT22539", "HBJ01098", "HZS08986", "HZS12652"], 0.0)


def test_representative_of_a_sibling_platform_order_does_not_zero_that_sibling(tmp_path):
    # Only P1 was returned. Its after-sale names P2's line; P2 and P3 keep their cost.
    costs = _goods_cost(tmp_path, after=[AFTER[1]])
    assert costs == {"HBJ01100": -5.7249, "HQT22539": -0.088, "HBJ01098": -1.5488,
                     "HZS08986": 0.0, "HZS12652": 0.0}


def test_partial_return_keeps_the_existing_per_line_rule(tmp_path):
    # 5 of P1's 9 units do not prove which lines came back: only the named line is excluded.
    costs = _goods_cost(tmp_path, after=[(*AFTER[1][:5], "5")])
    assert costs == {"HBJ01100": 0.0, "HQT22539": -0.22, "HBJ01098": -1.5488,
                     "HZS08986": -0.8797, "HZS12652": -0.9684}


def test_goods_page_lines_clear_exactly_the_returned_lines(tmp_path):
    # The goods page of P1's after-sale names its three lines; P2 and P3 keep their cost.
    detail = [("1022217808", "52015708", "HQT22539", "6"), ("1022217808", "52015709", "HZS08986", "1"),
              ("1022217808", "52015710", "HZS12652", "2")]
    costs = _goods_cost(tmp_path, after=[AFTER[1]], detail=detail)
    assert costs == {"HBJ01100": -5.7249, "HQT22539": -0.088, "HBJ01098": -1.5488,
                     "HZS08986": 0.0, "HZS12652": 0.0}


def test_goods_page_line_is_not_widened_to_the_whole_platform_order(tmp_path):
    # Its quantity equals P1's units, which widens a guessed line to all of P1.
    # A line read off the goods page stays the line it names.
    detail = [("1022217808", "52015709", "HZS08986", "9")]
    costs = _goods_cost(tmp_path, after=[AFTER[1]], detail=detail)
    assert costs == {"HBJ01100": -5.7249, "HQT22539": -0.22, "HBJ01098": -1.5488,
                     "HZS08986": 0.0, "HZS12652": -0.9684}


@pytest.mark.parametrize("goods,refund", [("买家已收到货", "退款成功"), ("卖家已收到退货", "退款关闭")])
def test_refund_without_returned_goods_keeps_cost(tmp_path, goods, refund):
    costs = _goods_cost(tmp_path, goods=goods, refund=refund)
    assert costs == {"HBJ01100": -5.7249, "HQT22539": -0.22, "HBJ01098": -1.5488,
                     "HZS08986": -0.8797, "HZS12652": -0.9684}
