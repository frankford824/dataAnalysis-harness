from datetime import datetime

import polars as pl
import pytest

from conftest import MODELS, write_xlsx
from ledger.engine.runtime import Ingestion, ingest, run, _mark_counted, _project_scoped_live
from ledger.engine.normalize import STORE_WIDE_PRODUCT
from ledger.model.loader import load_model
from ledger.model.schema import Model, SourceContract, StatementNode, Store
from ledger.service import unknown_tables
from test_reship_period_and_cost_drill import item
from test_promotion_control_months import sample


@pytest.mark.parametrize("hint", ["shop-a", "shop-b"])
def test_shared_bad_shop_never_falls_back_to_each_current_shop(hint):
    metric = load_model(MODELS / "cn-ecommerce").metric("dropship_cost").for_platform("pdd")
    model = Model(id="shared", name="shared", stores=(
        Store(id="a", name="shop-a", platform="pdd"), Store(id="b", name="shop-b", platform="pdd")),
        sources=(SourceContract(id="order_detail", name="orders", is_spine=True, owner_role="shop_owner", cadence="monthly"),
                 SourceContract(id="dropship", name="shared", company_wide=True, owner_role="shop_owner", cadence="monthly")),
        metrics=(metric,), statement=(StatementNode(id="cost", name="cost", formula={"op": "add", "of": [metric.id]}),))
    orders = [dict(order_id="260701-123456789012345", sub_order_id="sub", product_id="p", store_name="shop-a", order_time=datetime(2026, 7, 1))]
    rows = [dict(order_id="260702-123456789012345", store_name=name, total_cost=amount, order_time=datetime(2026, 7, 2))
            for name, amount in [("shop-a", 10.), ("2026-07-07", 20.), ("", 30.)]]
    shared = item("dropship", rows, rows[0].keys())
    shared.frame = shared.frame.with_columns(pl.lit(hint).alias("__hint_store__"))
    result = run(Ingestion(model=model, items=[item("order_detail", orders, orders[0].keys()), shared]), "pdd")
    assert result.spine_facts["amount"].sum() == -10
    assert result.spine_facts["store"].unique().to_list() == ["shop-a"]
    pending = result.facts.filter(pl.col("store") == "(未知店铺)")
    assert pending.height == 2
    assert pending["contribution"].sum() == 0
    assert not pending["counted"].any()
    sl = result.slice("shop-a", "2026-07")
    assert any(f.check_id == "shared_store_identity" and f.blocking for f in sl.audit.findings)
    assert sl.facts.height == 3  # unresolved source evidence remains downloadable


@pytest.mark.parametrize("declared", [-100., 0., 100.])
def test_overlapping_promotion_pools_reconcile_with_source_evidence(declared):
    metric, spine, source = sample()
    rows = source.to_dicts()
    rows[1]["file_name"] = "推广-20260601至20260731.xlsx"
    rows[1]["period"] = "(未知账期)"
    rows[1]["amount"] = declared
    source = pl.from_dicts(rows)
    projected = _project_scoped_live(source, metric, spine).facts
    marked = _mark_counted(source, projected, [metric])
    for period in ("2026-06", "2026-07"):
        expected = projected.filter(pl.col("period") == period)["amount"].sum()
        assert marked.filter(pl.col("period") == period)["contribution"].sum() == pytest.approx(expected)
    old = marked.filter(pl.col("file_name") == "推广-20260601至20260731.xlsx")
    assert old.height == 2


def test_undated_control_cannot_spread_over_unrelated_months():
    metric, spine, source = sample()
    rows = source.to_dicts()
    rows[1].update(file_name="推广-old.xlsx", period="(未知账期)")
    source = pl.from_dicts(rows)
    projected = _project_scoped_live(source, metric, spine).facts
    marked = _mark_counted(source, projected, [metric])
    old = marked.filter(pl.col("file_name") == "推广-old.xlsx")
    assert not old["counted"].any()
    assert old["contribution"].sum() == 0


def test_empty_unknown_sheet_does_not_block_but_nonempty_unknown_still_does(tmp_path):
    model = load_model(MODELS / "cn-ecommerce")
    empty = write_xlsx(tmp_path / "空表.xlsx", [["new header"]])
    nonempty = write_xlsx(tmp_path / "业务表.xlsx", [["new header"], ["unrecognized business row"]])
    received = ingest([empty, nonempty], model)
    assert [r["file"] for r in unknown_tables(received, model.stores[0])] == [nonempty.name]


def test_insurance_new_header_is_known_even_when_empty(tmp_path):
    path = write_xlsx(tmp_path / "保费.xlsx", [["投保单号", "订单编号", "下单时间", "动账时间", "动账流水号", "保险名称", "支付保费", "备注"]])
    got = ingest([path], load_model(MODELS / "cn-ecommerce"))
    assert got.items[0].ok
    assert got.items[0].recognition.template_id == "insurance_douyin_v2"


def test_proven_supplier_column_swap_recovers_owner_without_changing_source(tmp_path):
    model = load_model(MODELS / "cn-ecommerce")
    name = model.store("pdd_xibishun").name
    path = write_xlsx(tmp_path / "代发-全店铺-7+8月.xlsx", [
        ["下单日期", "店铺", "付款日期", "订单号", "订单金额", "代购成本"],
        [datetime(2026, 7, 2), datetime(2026, 7, 7), name, "260702-123456789012345", 19, 19],
        [datetime(2026, 7, 2), "not a date", name, "260702-123456789012346", 20, 20],
        [datetime(2026, 7, 2), datetime(2026, 7, 7), "unknown shop", "260702-123456789012347", 21, 21],
    ])
    original = path.read_bytes()
    received = ingest([path], model)
    frame = received.items[0].frame
    assert frame["store_name"].to_list()[:2] == [name, "not a date"]
    assert frame["source_note"][0].startswith("代发表列互换校验：")
    assert frame["source_note"].drop_nulls().len() == 1
    assert path.read_bytes() == original


JD_HEADERS = ["商家ID", "订单编号", "业务单据编号", "下单时间", "完成时间", "商品编号", "商品名称", "商品单价", "商品数量", "结算时间", "商户订单号", "资金动账备注", "费用名称", "金额", "结算主体"]


def test_jd_two_level_header_recognized_without_inventing_settled_status(tmp_path):
    model = load_model(MODELS / "cn-ecommerce")
    path = write_xlsx(tmp_path / "对账-京东皇莉诗-2607月.xlsx", [
        ["商家信息", *(["单据基础信息"] * 8), *(["结算信息"] * 6)], JD_HEADERS,
        ["merchant", "123456789", "bill-1", "2026-07-01", "2026-07-02", "p", "product", 10, 1, "2026-07-03", "", "", "货款", 10, "merchant"],
    ], sheet="费用明细")
    got = ingest([path], model)
    assert got.items[0].ok
    assert got.items[0].recognition.template_id == "jd_settlement_v12"
    assert got.items[0].frame["income"].sum() == 10
    # The new format still needs actual settled-status evidence; dates alone
    # must never silently turn tentative money into certified income.
    result = run(got, "jd")
    assert result.eval_errors.get("settlement")
    assert any("bill_status" in error for error in result.eval_errors["settlement"])


def test_pdd_export_without_unused_buyer_column_is_readable(tmp_path):
    model = load_model(MODELS / "cn-ecommerce")
    path = write_xlsx(tmp_path / "订单明细（国风）.xlsx", [
        ["订单号", "商品id", "商家实收金额(元)", "快递单号", "订单成交时间"],
        ["260702-123456789012345", "p", 19., "track", "2026-07-02"],
    ])
    received = ingest([path], model)
    assert received.items[0].ok
    assert received.items[0].frame["buyer_paid"].to_list() == [19.]


def test_amount_mismatch_is_rejected_before_publishing():
    from ledger.engine.runtime import _assert_reconciled
    from ledger.engine.calculate import CalculateError
    key = {"store": ["shop"], "period": ["2026-07"], "metric_id": ["ad_cost"]}
    with pytest.raises(CalculateError, match="已阻止发布"):
        _assert_reconciled(pl.DataFrame({**key, "contribution": [-9690.63]}),
                           pl.DataFrame({**key, "amount": [-8400.56]}))


def test_payment_side_paste_is_ignored_only_with_exact_source_evidence():
    from ledger.engine.layout import payment_reference
    from ledger.engine.types import RawTable, RawRow, FileRef
    columns = ["序号", "网店名称", "下单日期", "收款人账号", "收款人姓名", "付款金额", "报销人", "报销日期", "摘要", "订单号"]
    row = (1, "shop", "2026-07-01", "account", "buyer", 3, "operator", "2026-07-02", "refund", "order")
    source = RawTable(FileRef("sha", "小额打款.xlsx", "正式明细"), columns, [RawRow(2, row)])
    table = RawTable(FileRef("sha", "小额打款.xlsx", "Sheet1"), ["网店名称"], [RawRow(2, ("shop", None, None, None, *row))])
    assert payment_reference(table, [source]) == 1
    changed = list(row)
    changed[5] = 4
    table.rows = [RawRow(2, ("shop", None, None, None, *changed))]
    assert payment_reference(table, [source]) is None
    assert payment_reference(table, []) is None


def test_cached_sheet_period_is_not_lost(tmp_path):
    model = load_model(MODELS / "cn-ecommerce")
    path = write_xlsx(tmp_path / "订单明细.xlsx", [
        ["订单号", "商品id", "商家实收金额(元)", "快递单号", "订单成交时间"],
        ["260702-123456789012345", "p", 19., "track", "2026-07-02"],
    ], sheet="20260701至20260731")
    cache = tmp_path / "cache"
    first = ingest([path], model, cache_root=cache)
    second = ingest([path], model, cache_root=cache)
    assert first.known[0].frame["__hint_period__"].to_list() == ["2026-07"]
    assert first.known[0].frame.equals(second.known[0].frame)
