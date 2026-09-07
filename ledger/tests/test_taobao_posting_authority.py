"""平台明细与内部拆补单混用曾把已关联收入变成零或三分之一。"""

from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest

from ledger.commission import _order_base
from ledger.engine.calculate import evaluate_metric
from ledger.engine.classify import classify
from ledger.engine.link import Spine, link
from ledger.engine.project import project, project_transactions
from ledger.engine.runtime import _mark_counted, _project_scoped_live
from ledger.engine.types import ANCHOR_FILE, ANCHOR_ROW, ANCHOR_SHA, ANCHOR_SHEET
from ledger.model.loader import load_model
from ledger.model.schema import Allocation, LinkRule, Metric, Predicate, Template, ValueExpr
from conftest import MODELS


def metric(**extra):
    return Metric(**(dict(
        id="receipt", name="收款", source="settlement", value=ValueExpr(op="sum", of=["amount"]),
        time_basis="settle_date", allocate=Allocation(mode="ratio", by="alloc_ratio"),
        link=LinkRule(key="order_id", to="order.order_id", prefer_exported_orders=True,
                      spine_where=(Predicate(field="order_type", op="not_in", value=["补发订单"], include_null=True),)),
    ) | extra))


def facts(rows):
    return pl.DataFrame([dict(metric_id="receipt", source_id="settlement", store="s", period="2026-06",
                              link_key="A", amount=20.0, **r) for r in rows])


def order(origin="order_detail_file", **extra):
    return dict(order_id="A", store="s", period="2026-06", __spine_origin__=origin,
                order_type=None, buyer_paid=20.0, refund_amount=0.0, alloc_ratio=None) | extra


@pytest.mark.parametrize("remaining_share", [0.0, 1 / 3, 0.5])
def test_internal_rows_cannot_zero_or_dilute_exported_order(remaining_share):
    spine = Spine(pl.DataFrame([
        order(sub_order_id="platform-paid"), order(sub_order_id="platform-gift", buyer_paid=0.0),
        order("order_console", sub_order_id="internal-id", buyer_paid=0.0, alloc_ratio=remaining_share),
    ]))
    m = metric()
    source = facts([{}])
    projected = _project_scoped_live(source, m, spine)
    assert projected.facts["amount"].sum() == pytest.approx(20)
    assert projected.facts["spine_row"].to_list() == [0]
    marked = _mark_counted(source, projected.facts, [m])
    assert marked["contribution"].sum() == pytest.approx(20)
    linked, _ = link(pl.DataFrame({"order_id": ["A"]}), m, spine)
    assert linked["__linked__"].to_list() == [True]
    assert spine.frame.height == 3  # 成本与售后仍可读取完整证据。


def test_authority_is_store_scoped_and_keeps_new_sales():
    spine = Spine(pl.DataFrame([
        order(), order("order_console", store="other", alloc_ratio=1.0),
        order("order_console", order_id="B", alloc_ratio=1.0),
    ]))
    assert spine.eligible(metric().link).frame.height == 3


def test_reship_does_not_receive_income_but_keeps_cost_and_stable_row_identity():
    spine = Spine(pl.DataFrame([
        order("order_console", order_type="补发订单", alloc_ratio=1.0),
        order(order_id="B"),
    ]))
    assert project(facts([{}]), metric(), spine).facts.is_empty()
    cost = metric(link=LinkRule(key="order_id", to="order.order_id"))
    assert project(facts([{}]), cost, spine).facts["amount"].sum() == 20
    source = facts([{}]).with_columns(pl.lit("B").alias("link_key"))
    assert project(source, metric(), spine).facts["spine_row"].to_list() == [1]


def test_preference_is_applied_before_month_slicing():
    spine = Spine(pl.DataFrame([order(), order("order_console", period="2026-07", alloc_ratio=1.0)]))
    source = facts([{}]).with_columns(pl.lit("2026-07").alias("period"))
    assert _project_scoped_live(source, metric(), spine).facts.is_empty()


def test_refunds_follow_transaction_month_and_post_unmatched_keys_once():
    m = metric(posting_basis="transaction")
    source = pl.DataFrame({
        "metric_id": ["receipt"] * 5, "source_id": ["settlement"] * 5, "store": ["s"] * 5,
        "period": ["2026-06", "2026-07", "2026-06", "2026-06", "2026-06"],
        "link_key": ["A", "A", "missing", None, None], "amount": [-10.0, -20.0, -3.0, -2.0, -1.0],
    })
    spine = Spine(pl.DataFrame([order(period="2026-05")]))
    p = project_transactions(source, m, spine)
    assert p.facts.filter(pl.col("period") == "2026-06")["amount"].sum() == -16
    assert p.facts.filter(pl.col("period") == "2026-07")["amount"].sum() == -20
    marked = _mark_counted(source, p.facts, [m])
    assert marked["counted"].to_list() == [True] * 5
    assert marked["contribution"].to_list() == source["amount"].to_list()


def test_refund_with_no_order_spine_still_has_source_evidence():
    m = metric(posting_basis="transaction")
    p = project_transactions(facts([{}]), m, Spine.empty())
    assert p.facts["amount"].sum() == 20
    assert p.facts["spine_row"].null_count() == 1


def test_refund_does_not_inherit_order_date_or_guess_missing_transaction_date():
    frame = pl.DataFrame({
        "amount": [-10.0, -5.0], "settle_date": [datetime(2026, 7, 2), None],
        "__spine_period__": ["2026-06"] * 2, "__spine_store__": ["s"] * 2,
        "__link_key__": ["A"] * 2, "__linked__": [True] * 2,
        ANCHOR_SHA: ["sha"] * 2, ANCHOR_FILE: ["source.csv"] * 2,
        ANCHOR_SHEET: [""] * 2, ANCHOR_ROW: [2, 3],
    })
    template = Template(id="t", source="settlement", name="t", match_columns=("amount",))
    out, _ = evaluate_metric(frame, metric(posting_basis="transaction"), template, period_hint="2026-06")
    assert out["period"].to_list() == ["2026-07", "(未知账期)"]


def test_commission_keeps_cross_period_refund_in_its_posting_month():
    spine = pl.DataFrame({"store": ["s"], "period": ["2026-06"], "product_id": ["P"],
                          "order_time": [datetime(2026, 6, 1)], "sub_order_id": ["child"]})
    postings = pl.DataFrame({"metric_id": ["receipt", "refund"], "store": ["s", "s"],
                             "period": ["2026-06", "2026-07"], "spine_row": pl.Series([0, 0], dtype=pl.UInt32),
                             "amount": [100.0, -20.0]})
    result = SimpleNamespace(spine=spine, spine_facts=postings)
    assert _order_base(result, ("receipt", "refund"), ["s"], "2026-06")["base"].sum() == 100
    july = _order_base(result, ("receipt", "refund"), ["s"], "2026-07")
    assert july["base"].sum() == -20
    assert july["product_id"].to_list() == ["P"]


def test_app_discount_is_classified_in_all_taobao_wallet_templates():
    model = load_model(MODELS / "cn-ecommerce")
    for template in model.templates:
        if template.id not in {"taobao_settlement_alipay_v1", "taobao_settlement_wechat_v1", "taobao_settlement_wechat_v2"}:
            continue
        frame = pl.DataFrame({"subject": [""], "remark": ["天猫APP专享折扣服务费(KY_ITEM)(1234567890123456789)扣款"],
                              "biz_type": ["扣款"], "amount": [-0.88]})
        out, _ = classify(frame, model, "taobao", "amount", template)
        assert out["__major__"].to_list() == ["marketing_fee"]
