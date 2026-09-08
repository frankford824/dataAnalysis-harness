"""推广日期与商品必须同时匹配，跨月导出不能借首个订单的月份入账。"""
from datetime import date

import polars as pl
import pytest

from conftest import MODELS
from ledger.engine.normalize import normalize
from ledger.engine.runtime import Ingested, Ingestion, run
from ledger.engine.types import FileRef, RawRow, RawTable, Recognition
from ledger.model.loader import load_model
from ledger.model.schema import ColumnBinding, Metric, Model, SourceContract, StatementNode, Store, Template


def ingested(template, headers, rows):
    ref = FileRef(template.id, template.id + ".xlsx", "sheet")
    frame, notes = normalize(RawTable(ref, headers, [RawRow(i + 2, tuple(r)) for i, r in enumerate(rows)]), template)
    return Ingested(ref=ref, frame=frame, template=template, rows=len(rows), notes=notes,
                    recognition=Recognition(ref=ref, signature=template.signature,
                                            header_count=len(headers), source_id=template.source,
                                            template_id=template.id))


def calculate(rows, *, live=False, dated=True, platform="douyin"):
    builtin = load_model(MODELS / "cn-ecommerce")
    model = Model(id="test", name="test", stores=(Store(id="s", name="shop", platform=platform),),
                  sources=(SourceContract(id="order_detail", name="订单", is_spine=True,
                                          owner_role="shop_owner", cadence="monthly"),
                           SourceContract(id="promotion", name="推广", owner_role="shop_owner", cadence="monthly", required_for_close=False)),
                  metrics=(builtin.metric("ad_cost"), Metric(id="order_amount", name="订单金额", source="order_detail",
                           value={"op": "sum", "of": ["buyer_paid"]}, time_basis="order_date")),
                  statement=(StatementNode(id="ad", name="推广", formula={"op": "add", "of": ["ad_cost"]}),))
    fields = ["order_id", "product_id", "store_name", "order_time", "buyer_paid"]
    template = Template(id=("order_console_" if live else "") + "orders", source="order_detail", name="orders",
                        match_columns=("order_id",), time_slots={"order_date": "order_time"},
                        bindings=tuple(ColumnBinding(role=r, columns=(r,)) for r in fields))
    orders = ingested(template, fields, [
        ["A", "P1", "shop", "2026-06-01", 1], ["B", "P1", "shop", "2026-07-01", 1],
        ["C", "P1", "shop", "2026-08-01", 1], ["D", "P2", "shop", "2026-08-01", 1],
    ])
    promo = ingested(builtin.template("promotion_" + platform + "_v1"),
                     ["商品ID", "整体消耗" if platform == "douyin" else "总花费(元)"] + (["日期"] if dated else []), rows)
    promo.frame = promo.frame.with_columns(pl.lit("shop").alias("__hint_store__"), pl.lit("2026-06").alias("__hint_period__"))
    return run(Ingestion(model=model, items=[orders, promo]), platform)


def test_declared_time_source_is_never_coerced_to_money_by_its_name():
    template = Template(id="t", name="t", source="promotion", match_columns=("日期",),
                        bindings=(ColumnBinding(role="spend_time", columns=("日期",)),
                                  ColumnBinding(role="spend", columns=("花费",))),
                        time_slots={"spend_date": "spend_time"})
    item = ingested(template, ["日期", "花费"], [["2026-08-18", "0.04"]])
    assert item.frame["spend_time"].to_list() == ["2026-08-18"]
    assert item.frame["spend_date"].to_list() == [date(2026, 8, 18)]
    assert item.frame["spend"].to_list() == [0.04]


@pytest.mark.parametrize("live", [False, True])
def test_multi_month_product_spend_is_projected_only_to_same_month(live):
    result = calculate([["P1", 10, "2026-06-02"], ["P1", 20, "2026-07-02"],
                        ["P1", 30, "2026-08-02"], ["P2", 40, "2026-06-02"]], live=live)
    assert dict(result.spine_facts.filter(pl.col("metric_id") == "ad_cost").group_by("period").agg(pl.col("amount").sum()).iter_rows()) == {
        "2026-06": -10, "2026-07": -20, "2026-08": -30,
    }
    orphan = result.facts.filter(pl.col("link_key") == "P2")
    assert orphan["period"].item() == "2026-06"
    assert orphan["amount"].item() == -40
    assert orphan["contribution"].item() == 0


@pytest.mark.parametrize("platform", ["douyin", "pdd"])
@pytest.mark.parametrize("bad_date", [None, "", "not-a-date"])
def test_invalid_dated_spend_blocks_instead_of_inheriting_order_month(platform, bad_date):
    result = calculate([["P1", 10, bad_date]], platform=platform)
    assert "promotion" in result.eval_errors
    assert "发生日期缺失或无效" in result.eval_errors["promotion"][0]
    assert result.spine_facts.filter(pl.col("metric_id") == "ad_cost").is_empty()
    june = result.slices[("shop", "2026-06")]
    assert not june.nodes["ad"].available
    assert june.nodes["ad"].value is None
    assert "发生日期缺失或无效" in june.completeness.reasons["promotion"]


@pytest.mark.parametrize("platform", ["douyin", "pdd"])
def test_optional_date_absent_summary_export_keeps_existing_policy(platform):
    result = calculate([["P1", 10]], dated=False, platform=platform)
    assert not result.eval_errors
    assert result.spine_facts.filter(pl.col("metric_id") == "ad_cost")["amount"].sum() == -10


def test_blank_optional_product_name_does_not_discard_invalid_date_detail():
    template = load_model(MODELS / "cn-ecommerce").template("promotion_pdd_v1")
    item = ingested(template, ["商品ID", "商品名称", "总花费(元)", "日期"], [["P1", None, 10, None]])
    assert item.frame.height == 1
    assert item.frame["spend"].item() == 10


def test_failed_optional_upload_cannot_be_hidden_by_another_contributing_file():
    from ledger.engine.runtime import _completeness
    model = load_model(MODELS / "cn-ecommerce")
    facts = pl.DataFrame({"source_id": ["promotion"], "period": ["2026-06"]})
    completeness = _completeness(model, Ingestion(model=model), facts, facts, facts,
                                 "shop", "2026-06", 1, {"promotion": ["日期缺失或无效"]})
    assert "promotion" in completeness.missing
    assert "promotion" not in completeness.arrived
