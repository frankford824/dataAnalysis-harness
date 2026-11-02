"""Literal original prices retain PROVIDED policy without reviving containers."""
from decimal import Decimal
from copy import deepcopy
import pytest
from ledger.source_provided_price import prove_original_prices
from test_jst_source_members import HEADERS, fixture, rebuild, xlsx, sha, Request, Anchor


def sample(price=Decimal("3.335"), *, bundle=False, formula=None):
    f = fixture(bundle=bundle)
    cost = xlsx([*HEADERS, "成本价"],
        ["100", "101", "202600001", "C1", "KIT" if bundle else "A",
         Decimal("1.25"), "2026-05-15", "测试店", "普通订单", price], formula=formula)
    return rebuild(f, cost=cost)


def prove(f, mode="provided"):
    return prove_original_prices(*f[:3], pricing_mode=mode)


def test_original_literal_price_and_exact_fulfilled_quantity_are_preserved():
    result = prove(sample())
    assert result["status"] == "proved", result["reasons"]
    assert result["financial_applied"] is False
    price, = result["prices"]
    assert (price["unit_cost"], price["quantity"], price["amount"]) == ("3.335", "1.25", "4.16875")
    assert price["original_price_anchors"][0]["unit_price_cell"] == "J3"
    assert "intervals" not in price and "quote_sha" not in price


@pytest.mark.parametrize("mode", ["historical", "required", "", None])
def test_provided_evidence_does_not_relax_other_pricing_policies(mode):
    result = prove(sample(), mode)
    assert result["status"] == "pending" and result["prices"] == []
    assert result["reasons"] == ["provided_pricing_mode_required"]


@pytest.mark.parametrize("price", ["3.335", Decimal("0"), Decimal("-1"), Decimal("1e999")])
def test_text_zero_negative_and_unrepresentable_original_price_stay_pending(price):
    result = prove(sample(price))
    assert result["status"] == "pending" and result["prices"] == []


def test_formula_cache_is_not_independent_original_price_evidence():
    result = prove(sample(formula=(9, "1+2")))
    assert result["status"] == "pending"
    assert result["reasons"] == ["source_cell_missing_or_formula"]


def test_parent_kit_unit_price_never_becomes_a_component_unit_price():
    result = prove(sample(bundle=True))
    assert result["status"] == "pending" and result["prices"] == []
    assert result["reasons"] == ["provided_component_price_not_original_sku"]


def test_cancelled_physical_member_does_not_become_provided_cost():
    f = sample()
    target = deepcopy(f[4]); target["items"][0]["item_status"] = "Cancelled"
    result = prove(rebuild(f, target=target))
    assert result["status"] == "pending" and result["prices"] == []
    assert "physical_member_proof_incomplete" in result["reasons"]


def test_original_bytes_are_rehashed_before_any_price_is_used():
    f = sample(); f[2][f[0].cost_anchors[0].sha256] += b"changed"
    result = prove(f)
    assert result["status"] == "pending" and result["prices"] == []
    assert "source_blob_sha_changed" in result["reasons"]


def test_two_actual_shipments_share_unit_price_without_repeating_parent_total():
    f = sample()
    target = deepcopy(f[4])
    a = deepcopy(target["items"][0]); a["qty"] = "0.50"
    b = deepcopy(a); b["oi_id"] = "202"; b["qty"] = "0.75"
    target["items"] = [a, b]
    result = prove(rebuild(f, target=target))
    assert result["status"] == "proved", result["reasons"]
    assert len(result["prices"]) == 2
    assert sum(Decimal(p["amount"]) for p in result["prices"]) == Decimal("4.16875")
    assert {p["unit_cost"] for p in result["prices"]} == {"3.335"}


def test_conflicting_duplicate_exports_are_not_chosen_by_file_order():
    f = sample()
    other = sample(Decimal("4.335"))
    req, cap, blobs = f[:3]
    digest = other[0].cost_anchors[0].sha256
    blobs[digest] = other[2][digest]
    request = Request(req.source_order_id, req.platform_order_id, req.platform,
        req.store_names, req.shop_ids,
        (*req.cost_anchors, Anchor(digest, "证据", 3)), req.native_anchors)
    result = prove_original_prices(request, cap, blobs, pricing_mode="provided")
    assert result["status"] == "pending" and result["prices"] == []
    assert result["reasons"] == ["provided_original_prices_conflict"]


def engine_case(*,exclude=False):
    import polars as pl
    from ledger.engine.runtime import run
    from test_source_cost_posting import sample as cost_case
    ing,_,store,target,_,_=cost_case(exclude=exclude)
    raw=sample()
    ing.model=ing.model.model_copy(update={"platforms":tuple(p.model_copy(update={"cost_pricing":"provided"}) for p in ing.model.platforms)})
    original=ing.items[1]
    original.frame=original.frame.with_columns(pl.lit("A").alias("sku"),
        pl.lit(1.25).alias("quantity"),pl.lit(3.335).alias("unit_cost"),
        pl.lit(raw[0].cost_anchors[0].sha256).alias("__sha__"))
    original.ref=type(original.ref)(raw[0].cost_anchors[0].sha256,"original.xlsx","证据")
    ing.source_recovery_originals=(original,)
    return ing,run(ing,store.platform),store,target,raw


def test_explicit_preview_inputs_are_actually_consumed_by_original_engine():
    from ledger.source_provided_price import render_original_price_items
    from ledger.source_cost_posting import replace_inputs
    from ledger.engine.runtime import run
    from test_source_cost_posting import cost_sum
    ing,baseline,store,target,f=engine_case()
    assert cost_sum(baseline)==0
    made,anchors,physical=render_original_price_items(ing,baseline,store,"2026-05",target,*f[:3],input_version="isolated-input-v1")
    assert len(made)==1 and physical=={("200","201")}
    assert made[0].frame["cost_source"].to_list()==["provided_original"]
    assert made[0].template.id=="provided_original_source_preview_v1"
    assert "history_quote" not in made[0].frame["pricing_evidence"].item()
    replace_inputs(ing,made,anchors,physical)
    result=run(ing,store.platform)
    assert cost_sum(result)==-4.16875
    assert result.slices[(store.name,"2026-05")].nodes["net"].value==15.83  # unchanged statement cent rounding
    assert cost_sum(baseline)==0
    assert ing.source_recovery_originals[0].frame["order_state"].to_list()==["Split"]


def test_original_financial_filter_still_applies_to_proved_provided_sources():
    from ledger.source_provided_price import render_original_price_items
    from ledger.source_cost_posting import replace_inputs
    from ledger.engine.runtime import run
    from test_source_cost_posting import cost_sum
    ing,baseline,store,target,f=engine_case(exclude=True)
    made,anchors,physical=render_original_price_items(ing,baseline,store,"2026-05",target,*f[:3],input_version="isolated-input-v1")
    replace_inputs(ing,made,anchors,physical)
    assert cost_sum(run(ing,store.platform))==0


def test_preview_requires_distinct_input_identity():
    from ledger.source_provided_price import render_original_price_items
    from ledger.automation_cost_actions import Pending
    ing,baseline,store,target,f=engine_case()
    with pytest.raises(Pending,match="provided_explicit_input_version_required"):
        render_original_price_items(ing,baseline,store,"2026-05",target,*f[:3],input_version="")


def test_proved_source_cannot_be_applied_to_another_store_scope():
    from dataclasses import replace
    from ledger.source_provided_price import render_original_price_items
    from ledger.automation_cost_actions import Pending
    ing,baseline,store,target,f=engine_case()
    request=replace(f[0],store_names=("其他店",))
    with pytest.raises(Pending,match="provided_source_store_scope_conflict"):
        render_original_price_items(ing,baseline,store,"2026-05",target,request,*f[1:3],input_version="isolated-input-v1")


def test_source_original_month_cannot_be_applied_as_another_month():
    from ledger.source_provided_price import render_original_price_items
    from ledger.automation_cost_actions import Pending
    ing,baseline,store,target,f=engine_case()
    # Give the later month an otherwise present financial slice: the explicit
    # original-day guard still rejects moving the source cost into that month.
    baseline.slices[(store.name,"2026-09")]=baseline.slices[(store.name,"2026-05")]
    with pytest.raises(Pending,match="provided_original_book_date_unverified"):
        render_original_price_items(ing,baseline,store,"2026-09",target,*f[:3],input_version="isolated-input-v1")
