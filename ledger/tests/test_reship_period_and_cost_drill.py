"""原单月份与完整成本下钻；不能把漏显示的组件重复补成一笔成本。"""
from datetime import datetime

import polars as pl
import pytest

from conftest import MODELS
from ledger.engine.runtime import Ingested, Ingestion, run
from ledger.engine.types import ANCHOR_FILE, ANCHOR_ROW, ANCHOR_SHA, ANCHOR_SHEET, FileRef, Recognition
from ledger.model.loader import load_model
from ledger.model.schema import ColumnBinding, Model, SourceContract, StatementNode, Store, Template
from ledger.view import drill


def item(source, rows, roles):
    frame = pl.DataFrame(rows).with_columns(
        pl.lit(source).alias(ANCHOR_FILE), pl.lit(source).alias(ANCHOR_SHA),
        pl.lit("sheet").alias(ANCHOR_SHEET), pl.int_range(2, len(rows) + 2, dtype=pl.Int64).alias(ANCHOR_ROW),
        pl.col("order_time").alias("order_date"),
    )
    template = Template(id="order_console_" + source, name=source, source=source,
                        match_columns=(source,), time_slots={"order_date": "order_time"},
                        bindings=tuple(ColumnBinding(role=r, columns=(r,)) for r in roles))
    ref = FileRef(source, source, "sheet")
    return Ingested(ref=ref, template=template, frame=frame, rows=len(rows),
                    recognition=Recognition(ref=ref, signature=template.signature, header_count=len(roles),
                                            source_id=source, template_id=template.id))


def minimal(original_month=None, reship_month=6, *, direct=False):
    builtin = load_model(MODELS / "cn-ecommerce")
    metric = builtin.metric("reshipment_cost")
    if direct:
        metric = metric.model_copy(update={"link": None, "allocate": None})
    model = Model(id="test", name="test", stores=(Store(id="s", name="shop", platform="pdd"),),
                  sources=(SourceContract(id="order_detail", name="订单", is_spine=True,
                                          owner_role="shop_owner", cadence="monthly"),
                           SourceContract(id="order_cost", name="成本", owner_role="shop_owner", cadence="monthly")),
                  metrics=(metric,), statement=(StatementNode(id="cost", name="成本", formula={"op": "add", "of": [metric.id]}),))
    orders = [dict(order_id="sale-A", sub_order_id="reship-A", order_type="补发订单", store_name="shop",
                   order_time=datetime(2026, reship_month, 10))]
    if original_month:
        orders.insert(0, dict(order_id="sale-A", sub_order_id="sale-child", order_type="销售订单",
                              store_name="shop", order_time=datetime(2026, original_month, 1)))
    cost = [dict(order_id="sale-A", original_order_id="sale-A", internal_order_id="internal-reship",
                 sub_order_id="reship-A", sku="component", order_type="补发订单", order_state="Sent",
                 store_name="shop", order_time=datetime(2026, reship_month, 10), quantity=2.0, unit_cost=5.0)]
    return run(Ingestion(model=model, items=[
        item("order_detail", orders, orders[0].keys()), item("order_cost", cost, cost[0].keys()),
    ]), "pdd")


@pytest.mark.parametrize("original,reship", [(5, 6), (6, 7)])
def test_reship_cost_belongs_to_original_sale_month(original, reship):
    result = minimal(original, reship)
    assert result.spine_facts["period"].to_list() == [f"2026-{original:02d}"]
    assert result.spine_facts["amount"].sum() == -10
    assert result.facts["contribution"].sum() == -10


def test_only_reship_does_not_fall_back_to_raw_amount_when_projection_is_empty():
    result = minimal()
    assert result.spine_facts.is_empty()
    assert result.facts["amount"].sum() == -10  # 原始成本证据仍在。
    assert result.facts["contribution"].sum() == 0
    assert result.slices[("shop", "2026-06")].nodes["cost"].value == 0


def test_declared_period_metric_still_posts_without_an_order_link():
    result = minimal(direct=True)
    assert result.spine_facts["amount"].sum() == -10
    assert result.facts["contribution"].sum() == -10


@pytest.mark.parametrize("from_file", [False, True])
def test_cost_drill_resolves_internal_components_without_changing_amounts(tmp_path, from_file):
    model = load_model(MODELS / "cn-ecommerce")
    frame = pl.DataFrame({
        "metric_id": ["goods_cost"] * 3, "link_key": ["platform-child", "internal-component", "other-child"],
        "order_id": ["platform-parent", "platform-parent", "other-parent"],
        "internal_order_id": ["erp-A", "erp-A", "erp-B"], "sku": ["base", "component", "other"],
        "amount": [-25.872, -66.528, -7.0], "contribution": [-25.872, -66.528, -7.0],
        "counted": [True] * 3, "linked": [True] * 3, "major": pl.Series([None]*3, dtype=pl.Utf8),
        "subject": pl.Series([None]*3, dtype=pl.Utf8), "minor": pl.Series([None]*3, dtype=pl.Utf8),
        "classify_via": [""]*3, "file_name": ["source.xlsx"]*3, "file_sha": ["sha"]*3,
        "sheet": ["sheet"]*3, "row_no": [2, 3, 4],
    })
    source = frame
    if from_file:
        source = tmp_path / "facts.parquet"
        frame.write_parquet(source)
    for query in ["platform-parent", "platform-child", "internal-component"]:
        d = drill(source, model, "g_goods", q=query)
        assert d["selection"]["amount"] == -92.40
        assert len(d["sample"]) == 2
        assert d["total"] == -99.40
    assert drill(source, model, "g_goods", q="component")["selection"]["amount"] == -66.53


def test_pdd_settlement_cannot_take_a_pure_reship_as_a_sale():
    from ledger.engine.link import Spine, link
    model = load_model(MODELS / "cn-ecommerce")
    spine = Spine(pl.DataFrame({"order_id": ["old-sale"], "order_type": ["补发订单"],
                                "store": ["shop"], "period": ["2026-06"]}))
    for mid in ["trade_receipt_pdd", "software_fee_pdd", "trade_compensation_pdd"]:
        linked, _ = link(pl.DataFrame({"base_order_id": ["old-sale"]}), model.metric(mid), spine)
        assert not linked["__linked__"][0]
