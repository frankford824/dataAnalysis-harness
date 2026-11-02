"""Warehouse facts have independent authority, not fabricated ERP flags."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import hashlib,json
import pytest
from ledger.source_wms_members import normalize_wms_members
from ledger.jst_source_members import normalize_members
from ledger.wms_confirmed_lines import WmsDocuments
from test_jst_source_members import fixture,rebuild
from test_wms_confirmed_lines import document,full


def sample(*,item_status="SentCancelled",prototype=False,qty="1.25",owner=0,co=10):
    f=fixture();source=deepcopy(f[3]);target=deepcopy(f[4]);target["items"][0]["item_status"]=item_status
    if prototype:
        for row in [source["items"][0],target["items"][0]]:row.update(co_id=None,is_combine_item=True,created=None)
    f=rebuild(f,source=source,target=target)
    d=document(original=201,sku="A",qty=qty,owner=owner,co=co);d["o_id"]=200;d["order_type"]="普通订单";d["created"]=d["modified"]=d["io_date"]="2026-05-15 14:00:00"
    line=d["items"][0];line["is_gift"]=False;line["raw_so_id"]="202600001";line["outer_oi_id"]="C1";line["inout_item_flds"]["created"]=line["inout_item_flds"]["modified"]="2026-05-15 13:00:00"
    captures=full([d])
    for _,env in captures:env["request"]["body"]["o_ids"]=[200]
    return f,captures


def prove(f,captures,period="2026-05"):
    return normalize_wms_members(*f[:3],period=period,warehouse=WmsDocuments(captures))


def test_erp_cancelled_after_shipment_is_preserved_and_wms_confirmation_is_separate():
    f,captures=sample();before={k:hashlib.sha256(v).hexdigest() for k,v in f[2].items()}
    assert normalize_members(*f[:3])["status"]=="pending"
    result=prove(f,captures)
    assert result["status"]=="proved",result["reasons"]
    member,=result["members"]
    assert member["observed_flags"]["physical_member"]["item_status"]=={"present":True,"value":"SentCancelled"}
    assert member["warehouse_evidence"]["document_status"]=="Confirmed"
    assert member["physical_basis"]=="current_confirmed_inventory_document"
    assert member["internal_sub_order_id"]=="201" and member["warehouse_evidence"]["ioi_id"]=="900"
    assert member["physical_identity"]==["wms_out_item","10","200","900","A"]
    assert result["financial_ready"] is False and result["proves_no_returns"] is False
    assert result["proves_exact_dispatch_time"] is False
    assert before=={k:hashlib.sha256(v).hexdigest() for k,v in f[2].items()}


def test_logical_prototype_co_and_created_remain_missing_but_real_stock_is_independent():
    f,captures=sample(prototype=True)
    assert normalize_members(*f[:3])["status"]=="pending"
    result=prove(f,captures)
    assert result["status"]=="proved",result["reasons"]
    member,=result["members"]
    assert member["observed_flags"]["physical_member"]["co_id"]=={"present":True,"value":None}
    assert member["observed_flags"]["physical_member"]["created"]=={"present":True,"value":None}
    assert member["observed_flags"]["physical_member"]["is_combine_item"]["value"] is True
    assert member["warehouse_evidence"]["co_id"]=="10"
    assert member["warehouse_evidence"]["created"]=="2026-05-15 13:00:00"


def test_source_logical_quantity_must_equal_all_actual_stock_lines():
    f,captures=sample(qty="2")
    result=prove(f,captures)
    assert result["status"]=="pending" and "physical_quantity_not_conserved" in result["reasons"]


def test_same_sale_line_multiple_actual_stock_batches_keep_distinct_wms_ids():
    f,captures=sample(qty="0.50")
    first=json.loads(captures[0][0])
    docs=[]
    for raw,env in captures:
        body=json.loads(raw)
        if body["data"]["datas"]:
            second=deepcopy(body["data"]["datas"][0]);second["io_id"]=201;second["items"][0]["ioi_id"]=901;second["items"][0]["qty"]="0.75";second["items"][0]["inout_item_flds"]["io_id"]=201
            body["data"]["datas"].append(second);body["data"]["data_count"]=2
        raw=json.dumps(body).encode();env["raw_sha256"]=hashlib.sha256(raw).hexdigest();docs.append((raw,env))
    result=prove(f,docs)
    assert result["status"]=="proved",result["reasons"]
    assert len(result["members"])==2
    assert sum(Decimal(m["quantity"]) for m in result["members"])==Decimal("1.25")
    assert len({tuple(m["physical_identity"]) for m in result["members"]})==2


def test_missing_warehouse_scopes_do_not_turn_an_unknown_erp_member_into_complete():
    f,captures=sample()
    assert prove(f,captures[:-1])["status"]=="pending"


def test_no_cost_original_anchor_is_not_smuggled_into_the_old_original_contract():
    f,captures=sample();request=replace(f[0],cost_anchors=())
    result=normalize_wms_members(request,f[1],f[2],period="2026-05",warehouse=WmsDocuments(captures))
    assert result["status"]=="pending" and result["reasons"]==["source_anchor_budget"]


def test_confirmed_stock_has_no_authority_to_move_original_ledger_month():
    f,captures=sample()
    assert prove(f,captures,period="2026-09")["status"]=="pending"


def test_other_stock_company_cannot_be_substituted_for_the_original_erp_namespace():
    f,captures=sample(owner=10,co=88)
    result=prove(f,captures)
    assert result["status"]=="pending" and "wms_erp_company_namespace_conflict" in result["reasons"]


def test_warehouse_gift_conflict_cannot_fall_back_to_an_erp_sent_flag():
    f,captures=sample(item_status="Sent");changed=[]
    for raw,env in captures:
        body=json.loads(raw)
        for doc in body["data"]["datas"]:doc["items"][0]["is_gift"]=True
        raw=json.dumps(body).encode();env["raw_sha256"]=hashlib.sha256(raw).hexdigest();changed.append((raw,env))
    result=prove(f,changed)
    assert result["status"]=="pending" and "wms_nongift_basis_missing" in result["reasons"]


def test_nonconfirmed_warehouse_counterevidence_is_not_hidden_by_stale_erp_sent():
    f,captures=sample(item_status="Sent");doc=None
    for raw,env in captures:
        body=json.loads(raw)
        if body["data"]["datas"]:doc=body["data"]["datas"][0];break
    assert doc is not None
    doc["status"]="Cancelled";changed=[]
    for raw,env in captures:
        body=json.loads(raw);records=[doc] if env["request"]["body"]["status"]=="Cancelled" else []
        body["data"]["datas"]=records;body["data"]["data_count"]=len(records);body["data"]["page_count"]=1 if records else 0
        raw=json.dumps(body).encode();env["raw_sha256"]=hashlib.sha256(raw).hexdigest();changed.append((raw,env))
    result=prove(f,changed)
    assert result["status"]=="pending" and "wms_nonconfirmed_line_observation" in result["reasons"]
    assert result["proves_no_returns"] is False


@pytest.mark.parametrize("kind",["补发订单","换货订单",None])
def test_unknown_or_separate_warehouse_order_type_cannot_inherit_erp_ordinary_goods(kind):
    f,captures=sample(item_status="Sent");changed=[]
    for raw,env in captures:
        body=json.loads(raw)
        for doc in body["data"]["datas"]:
            if kind is None:doc.pop("order_type",None)
            else:doc["order_type"]=kind
        raw=json.dumps(body).encode();env["raw_sha256"]=hashlib.sha256(raw).hexdigest();changed.append((raw,env))
    result=prove(f,changed)
    assert result["status"]=="pending" and "wms_goods_source_order_type_not_ordinary" in result["reasons"]
