"""Native-only source contract: quantities are evidence, never defaults."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import pytest
from ledger.source_native_members import NativeRequest,normalize_native_members
from ledger.jst_source_members import Anchor
from test_jst_source_members import fixture,rebuild,xlsx,sha


def sample(*,platform="taobao",native_qty=None,bundle=False,raw_qty="1.25",formula=None):
    f=fixture(bundle=bundle)
    source=deepcopy(f[3]);target=deepcopy(f[4])
    if bundle:
        for r in [*source["items"],*target["items"]]:r["raw_sku_id"]="KIT"
    elif raw_qty is not None:source["items"][0]["raw_qty"]=raw_qty
    if platform=="alibaba1688":headers=["订单号","订单创建时间"];values=["202600001","2026-05-15"]
    else:headers=["主订单编号","子订单编号","订单创建时间"];values=["202600001","C1","2026-05-15"]
    if native_qty is not None:headers.append("数量");values.append(native_qty)
    native=xlsx(headers,values,formula=formula)
    f=rebuild(f,source=source,target=target,native=native)
    old=f[0];request=NativeRequest(old.source_order_id,old.platform_order_id,"" if platform=="alibaba1688" else "C1",platform,old.store_names,old.shop_ids,old.native_anchors)
    return request,f[1],f[2],f


def prove(f):return normalize_native_members(*f[:3])


def test_missing_native_quantity_uses_explicit_original_erp_sales_quantity():
    result=prove(sample())
    assert result["status"]=="proved",result["reasons"]
    m,=result["members"]
    assert m["quantity"]=="1.25" and m["logical_quantity"]=="1.25"
    assert m["quantity_basis"]["kind"]=="erp_original_raw_sales_quantity"
    assert m["quantity_basis"]["pointer"]=="/items/0/raw_qty"
    assert m["native_quantity_present"] is False
    assert "source_anchors" not in m and result["schema"]=="jst_native_source_members.v1"
    assert result["financial_applied"] is False


def test_missing_both_native_and_original_erp_sale_quantity_never_defaults_to_one():
    result=prove(sample(raw_qty=None))
    assert result["status"]=="pending" and result["members"]==[]
    assert "quantity_literal_missing" in result["reasons"]


def test_empty_native_quantity_is_explicitly_missing_not_zero():
    result=prove(sample(native_qty=""))
    assert result["status"]=="proved" and result["members"][0]["native_quantity_present"] is False


def test_original_native_quantity_can_supply_independent_sale_units():
    result=prove(sample(platform="alibaba1688",native_qty=Decimal("1.25"),raw_qty=None))
    assert result["status"]=="proved",result["reasons"]
    assert result["members"][0]["quantity_basis"]["kind"]=="native_original_literal_sales_quantity"
    assert result["members"][0]["native_quantity_present"] is True


@pytest.mark.parametrize("quantity",[Decimal("2"),Decimal("0"),Decimal("-1"),"1.25"])
def test_native_quantity_conflicts_and_nonliteral_values_fail_closed(quantity):
    result=prove(sample(native_qty=quantity))
    assert result["status"]=="pending" and result["members"]==[]


def test_native_formula_cache_is_not_original_sales_quantity():
    result=prove(sample(native_qty=Decimal("1.25"),formula=(3,"1+0.25")))
    assert result["status"]=="pending" and result["reasons"]==["source_cell_missing_or_formula"]


def test_fulfilled_stock_quantity_cannot_replace_different_raw_sales_quantity():
    result=prove(sample(raw_qty="2"))
    assert result["status"]=="pending" and result["reasons"]==["native_scalar_sales_stock_unit_conversion_unproved"]


def test_explicit_unmaterialized_original_bundle_has_a_separate_sales_unit():
    result=prove(sample(bundle=True))
    assert result["status"]=="proved",result["reasons"]
    assert {m["quantity"] for m in result["members"]}=={"2","3"}
    assert {m["logical_quantity"] for m in result["members"]}=={"1.25"}
    assert all(m["quantity_basis"]["kind"]=="erp_original_combination_sales_quantity" for m in result["members"])


def test_current_component_rows_without_original_sales_mapping_are_not_a_bundle_proof():
    f=sample(bundle=True);source=deepcopy(f[3][3])
    for row in source["items"]:row["is_combine_item"]=False;row["created"]="2026-05-15 12:00:00"
    changed=rebuild(f[3],source=source)
    result=normalize_native_members(f[0],changed[1],changed[2])
    assert result["status"]=="pending" and result["reasons"]==["native_original_bundle_sales_mapping_missing"]


@pytest.mark.parametrize("change",[{"item_status":"SentCancelled"},{"item_status":None},{"is_gift":None},{"is_virtual":None},{"created":"2020-01-01 10:00:00"}])
def test_header_sent_never_broadcasts_lifecycle_or_nongift_evidence(change):
    f=sample();target=deepcopy(f[3][4]);target["items"][0].update(change);changed=rebuild(f[3],target=target)
    result=normalize_native_members(f[0],changed[1],changed[2])
    assert result["status"]=="pending" and result["members"]==[]


def test_typed_native_identity_and_confirmed_shop_are_required():
    f=sample();result=normalize_native_members(replace(f[0],platform_sub_order_id="OTHER"),f[1],f[2])
    assert result["status"]=="pending" and result["reasons"]==["native_original_child_conflict"]
    result=normalize_native_members(replace(f[0],shop_ids=("88",)),f[1],f[2])
    assert result["status"]=="pending" and result["reasons"]==["native_source_cross_shop"]


def test_changed_original_bytes_are_rejected_before_proof():
    f=sample();f[2][f[0].native_anchors[0].sha256]+=b"changed"
    result=prove(f)
    assert result["status"]=="pending" and result["reasons"]==["native_source_blob_sha_changed"]


def materialized_ali_bundle(*,remark="KIT*1.25->A*2,B*3",variant="888",product="777",when="2026-05-15 11:00:00"):
    f=fixture(bundle=True);source=deepcopy(f[3]);target=deepcopy(f[4])
    for position,row in enumerate(source["items"]):
        row.update(raw_sku_id="KIT",shop_i_id="777",shop_sku_id="888",
                   oi_id=str(101+position),is_combine_item=False,created="2026-05-15 10:00:00")
    for row in target["items"]:row.update(raw_sku_id="KIT",shop_i_id="777",shop_sku_id="888")
    source["actions"]=[{"name":"拆开组合装商品","o_id":"100","co_id":"10","created":when,"remark":remark}]
    target["send_date"]="2026-05-15 14:00:00"
    native=xlsx(["订单号","订单创建时间","数量","SKU_ID","Offer_ID","单位","货号"],
        ["202600001","2026-05-15",Decimal("1.25"),variant,product,"套","not-a-company-SKU"])
    changed=rebuild(f,source=source,target=target,native=native)
    req=changed[0];request=NativeRequest(req.source_order_id,req.platform_order_id,"","alibaba1688",req.store_names,req.shop_ids,req.native_anchors)
    return request,changed[1],changed[2],changed


def test_native_variant_and_original_bundle_action_prove_units_without_merchant_sku_alias():
    result=prove(materialized_ali_bundle())
    assert result["status"]=="proved",result["reasons"]
    assert len(result["members"])==2
    assert {m["source_component_member_id"] for m in result["members"]}=={"101","102"}
    assert all(m["quantity_basis"]["kind"]=="native_platform_variant_quantity_and_original_bundle_action" for m in result["members"])
    assert {m["quantity"] for m in result["members"]}=={"2","3"}
    assert {m["logical_quantity"] for m in result["members"]}=={"1.25"}


@pytest.mark.parametrize("changes",[{"remark":"KIT*1.25->A*1,B*3"},{"remark":"KIT*1.25"},
    {"variant":"999"},{"product":"999"},{"when":"2026-05-15 15:00:00"}])
def test_variant_bom_or_timeline_conflicts_never_reconstruct_a_bundle(changes):
    result=prove(materialized_ali_bundle(**changes))
    assert result["status"]=="pending" and result["members"]==[]


def test_competing_original_bom_events_are_not_resolved_by_selecting_a_convenient_one():
    f=materialized_ali_bundle();source=deepcopy(f[3][3]);second=deepcopy(source["actions"][0]);second["remark"]="KIT*1.25->A*9,B*3";source["actions"].append(second)
    changed=rebuild(f[3],source=source)
    result=normalize_native_members(f[0],changed[1],changed[2])
    assert result["status"]=="pending" and result["reasons"]==["native_original_bundle_action_changed"]


def test_native_and_erp_quantity_low_digit_difference_survives_28_digit_context():
    erp="123456789012345678901234567890.000000000000000000000000000002"
    original=Decimal("123456789012345678901234567890.000000000000000000000000000001")
    f=fixture(qty=erp);source=deepcopy(f[3]);source["items"][0]["raw_qty"]=erp
    native=xlsx(["主订单编号","子订单编号","订单创建时间","货号","数量"],
        ["202600001","C1","2026-05-15","A",original])
    changed=rebuild(f,source=source,native=native)
    old=changed[0];request=NativeRequest(old.source_order_id,old.platform_order_id,"C1","taobao",old.store_names,old.shop_ids,old.native_anchors)
    result=normalize_native_members(request,changed[1],changed[2])
    assert result["status"]=="pending" and result["members"]==[]
    assert result["reasons"]==["native_original_sales_sku_quantity_conflict"]


def test_two_optional_quantity_headers_cannot_choose_a_convenient_column():
    f=sample();native=xlsx(["主订单编号","子订单编号","订单创建时间","数量","数量"],
        ["202600001","C1","2026-05-15",Decimal("1.25"),Decimal("2")])
    changed=rebuild(f[3],native=native);request=replace(f[0],native_anchors=changed[0].native_anchors)
    result=normalize_native_members(request,changed[1],changed[2])
    assert result["status"]=="pending" and result["members"]==[]
    assert result["reasons"]==["source_header_role_ambiguous"]
