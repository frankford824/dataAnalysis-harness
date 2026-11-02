"""Captured warehouse lines never replace ERP identities or sales units."""
from copy import deepcopy
import hashlib
import json
from decimal import Decimal

import pytest

from ledger.wms_confirmed_lines import WmsDocuments, WmsSourceError, STATUSES, PATH


def document(*, io=200, ioi=900, original=300, sku="A", qty="2", status="Confirmed", co=10, owner=0):
    return {"o_id": 100, "io_id": io, "co_id": co, "owner_co_id": owner, "shop_id": 77,
            "status": status, "io_date": "2026-05-01 00:00:00", "created": "2026-05-01 01:00:00",
            "modified": "2026-05-02 02:00:00", "inout_flds": {"owner_co_id": owner},
            "items": [{"oi_id": original, "ioi_id": ioi, "sku_id": sku, "qty": qty,
                       "outer_oi_id": "CHILD", "raw_so_id": "PARENT", "combine_sku_id": "KIT",
                       "combine_sku_qty": "3", "per_unit": None,
                       "inout_item_flds": {"co_id": co, "io_id": io, "oi_id": original,
                           "oi_id_qty": "3", "combine_sku_qty": "3", "per_unit": None,
                           "sales_unit": "set", "qty_type": 0, "created": "2026-05-01 01:00:00",
                           "modified": "2026-05-02 02:00:00"}}]}


def capture(status, archive, docs=(), *, page=1, has_next=False):
    body = {"shop_id": 77, "o_ids": [100], "status": status, "archive": archive,
            "page_index": page, "page_size": 50, "is_get_total": True,
            "is_get_cbfinance": False, "is_get_cblogistic": False,
            "inout_flds": ["owner_co_id"], "inout_item_flds": ["co_id", "io_id", "oi_id", "oi_id_qty"]}
    root = {"code": 0, "data": {"datas": list(docs), "page_index": page, "page_size": 50,
                              "has_next": has_next, "data_count": len(docs), "page_count": 1 if docs else 0}}
    raw = json.dumps(root).encode()
    return raw, {"http_status": 200, "raw_sha256": hashlib.sha256(raw).hexdigest(),
                 "captured_at": 1793500000.0, "request": {"method": "POST", "path": PATH,
                                                       "scope_id": status + str(archive), "body": body}}


def full(docs=None):
    docs = [document()] if docs is None else docs
    return [capture(status, archive, docs if status == "Confirmed" else [])
            for archive in (False, True) for status in sorted(STATUSES)]


def confirmed_index(docs, archive=False):
    return next(i for i, (_, env) in enumerate(docs)
                if env["request"]["body"]["status"] == "Confirmed"
                and env["request"]["body"]["archive"] is archive)


def replace_response(pair, fn):
    raw, env = deepcopy(pair)
    body = json.loads(raw)
    fn(body)
    raw = json.dumps(body).encode()
    env["raw_sha256"] = hashlib.sha256(raw).hexdigest()
    return raw, env


def test_erp_and_warehouse_identifiers_and_units_are_separate():
    source = WmsDocuments(full())
    result = source.match("100", "300", "A", "77", "10")
    assert result["status"] == "proved" and len(result["lines"]) == 1
    line = result["lines"][0]
    assert line["physical_identity"] == ["10", "200", "900", "A"]
    assert line["oi_id"] == "300" and line["ioi_id"] == "900"
    assert line["stock_quantity"] == "2" and line["oi_id_qty"] == "3"
    assert line["combine_sku_qty"] == "3" and line["per_unit"] is None
    assert line["document_status"] == "Confirmed" and "Sent" not in line.values()
    assert line["financial_authority"] is None
    assert result["proves_no_returns"] is False and result["proves_exact_dispatch_time"] is False
    assert len(line["source_refs"]) == 2  # same actual document in current/archived scopes


def test_multiple_actual_batches_are_all_returned_once():
    result = WmsDocuments(full([document(), document(io=201, ioi=901, qty="5")])).match("100", "300", "A", "77", "10")
    assert result["status"] == "proved" and len(result["lines"]) == 2
    assert sum(Decimal(x["stock_quantity"]) for x in result["lines"]) == Decimal(7)


def test_incomplete_or_empty_scopes_never_prove_zero_or_a_complete_match():
    result = WmsDocuments(full()[:-1]).match("100", "300", "A", "77", "10")
    assert result["status"] == "pending" and result["reasons"] == ["wms_query_scopes_incomplete"]
    result = WmsDocuments(full([])).match("100", "300", "A", "77", "10")
    assert result["status"] == "pending" and result["lines"] == []


def test_a_missing_erp_item_id_is_not_replaced_by_the_warehouse_line_id():
    docs = full()
    idx = confirmed_index(docs)
    docs[idx] = replace_response(docs[idx], lambda body: body["data"]["datas"][0]["items"][0].pop("oi_id"))
    with pytest.raises(WmsSourceError, match="identity_literal_invalid"):
        WmsDocuments(docs)


@pytest.mark.parametrize("field,value", [("co_id", 11), ("io_id", 201), ("oi_id", 301), ("combine_sku_qty", "4")])
def test_nested_namespace_or_unit_conflicts_are_rejected(field, value):
    docs = full()
    idx = confirmed_index(docs)
    docs[idx] = replace_response(docs[idx], lambda body: body["data"]["datas"][0]["items"][0]["inout_item_flds"].update({field: value}))
    with pytest.raises(WmsSourceError):
        WmsDocuments(docs)


def test_stock_company_and_price_owner_are_not_the_warehouse_default():
    source = WmsDocuments(full([document(co=88, owner=10)]))
    row = source.match("100", "300", "A", "77", "10")["lines"][0]
    assert row["co_id"] == "88" and row["company_id"] == "10"
    assert source.match("100", "300", "A", "77", "88")["status"] == "pending"


@pytest.mark.parametrize("change", [lambda d: d.update(o_id=101), lambda d: d.update(shop_id=78),
                                     lambda d: d.update(status="Cancelled")])
def test_response_cannot_escape_the_original_query_scope(change):
    docs = full()
    idx = confirmed_index(docs)
    docs[idx] = replace_response(docs[idx], lambda body: change(body["data"]["datas"][0]))
    with pytest.raises(WmsSourceError):
        WmsDocuments(docs)


@pytest.mark.parametrize("change", [lambda e: e.update(http_status=403), lambda e: e.update(raw_sha256="0" * 64),
    lambda e: e["request"].update(path="/open/orders/out/upload"),
    lambda e: e["request"]["body"].update(is_get_cbfinance=True),
    lambda e: e["request"]["body"].update(force_update=True),
    lambda e: e["request"]["body"].update(o_ids=[100, 100])])
def test_source_errors_writes_scope_widening_and_duplicate_requests_fail_closed(change):
    docs = full()
    raw, env = docs[1]
    env = deepcopy(env);change(env);docs[1] = raw, env
    with pytest.raises(WmsSourceError):
        WmsDocuments(docs)


@pytest.mark.parametrize("qty", ["0", "-1", "NaN", "1e1000000000", None, True])
def test_confirmed_inventory_quantities_need_literal_positive_evidence(qty):
    with pytest.raises(WmsSourceError):
        WmsDocuments(full([document(qty=qty)]))


def test_current_and_archived_document_versions_cannot_choose_the_favourable_one():
    docs = full()
    archive_confirmed = next(i for i, (_, e) in enumerate(docs) if e["request"]["body"]["archive"] and e["request"]["body"]["status"] == "Confirmed")
    docs[archive_confirmed] = replace_response(docs[archive_confirmed], lambda b: b["data"]["datas"][0]["items"][0].update(qty="9"))
    source = WmsDocuments(docs)
    assert source.match("100", "300", "A", "77", "10")["reasons"] == ["wms_document_version_conflict"]
    assert source.documents_for_order("100")[0]["observation_conflict"] is True


def test_missing_pages_do_not_prove_the_end_of_query_scope():
    docs = full()
    idx = next(i for i, (_, e) in enumerate(docs) if e["request"]["body"]["status"] == "Confirmed" and not e["request"]["body"]["archive"])
    docs[idx] = capture("Confirmed", False, [document()], has_next=True)
    docs.append(capture("Confirmed", False, [], page=3, has_next=False))
    assert WmsDocuments(docs).match("100", "300", "A", "77", "10")["reasons"] == ["wms_query_scopes_incomplete"]


def test_sku_is_not_casefolded_and_callers_cannot_mutate_future_proofs():
    source = WmsDocuments(full())
    assert source.match("100", "300", "a", "77", "10")["status"] == "pending"
    line = source.match("100", "300", "A", "77", "10")["lines"][0]
    line["qty"] = "999"
    line["source_refs"][0]["source_request"]["body"]["o_ids"] = [101]
    later = source.match("100", "300", "A", "77", "10")["lines"][0]
    assert later["qty"] == "2" and later["source_refs"][0]["source_request"]["body"]["o_ids"] == [100]


@pytest.mark.parametrize("field,value", [("page_index", True), ("page_count", "0.5"), ("data_count", -1)])
def test_pagination_counts_need_exact_nonnegative_integers(field, value):
    docs = full()
    idx = confirmed_index(docs)
    docs[idx] = replace_response(docs[idx], lambda b: b["data"].update({field: value}))
    with pytest.raises(WmsSourceError):
        WmsDocuments(docs)


def test_a_false_end_marker_cannot_hide_declared_remaining_documents():
    docs = full()
    idx = confirmed_index(docs)
    docs[idx] = replace_response(docs[idx], lambda b: b["data"].update(data_count=100, page_count=2))
    source = WmsDocuments(docs)
    assert source.match("100", "300", "A", "77", "10")["reasons"] == ["wms_query_scopes_incomplete"]


def test_failed_capture_times_and_duplicate_source_json_are_rejected():
    docs = full()
    raw, env = docs[0]
    env = deepcopy(env);env["captured_at"] = float("nan");docs[0] = raw, env
    with pytest.raises(WmsSourceError, match="capture_time_invalid"):
        WmsDocuments(docs)
    docs = full();raw, env = docs[0];raw = raw.replace(b'"code": 0', b'"code": 0, "code": 1')
    env = deepcopy(env);env["raw_sha256"] = hashlib.sha256(raw).hexdigest();docs[0] = raw, env
    with pytest.raises(WmsSourceError, match="duplicate_json_key"):
        WmsDocuments(docs)



def test_query_field_expansion_cannot_smuggle_a_noncontract_field():
    docs = full()
    raw, env = docs[0];env = deepcopy(env)
    env["request"]["body"]["inout_item_flds"] = ["co_id", "unapproved_expression"]
    docs[0] = raw, env
    with pytest.raises(WmsSourceError, match="query_extra_fields_invalid"):
        WmsDocuments(docs)



@pytest.mark.parametrize("order_type", ["普通订单", "补发订单", "换货订单", None])
def test_warehouse_document_business_type_is_preserved_without_defaulting_to_ordinary(order_type):
    doc = document()
    if order_type is not None:
        doc["order_type"] = order_type
    source = WmsDocuments(full([doc]))
    observed = source.documents_for_order("100")[0]
    line = source.match("100", "300", "A", "77", "10")["lines"][0]
    assert observed["document_order_type"] == line["document_order_type"] == order_type


def test_literal_metadata_is_json_serializable_without_fabricating_missing_defaults():
    doc = document()
    doc["items"][0]["i_id"] = 1234567890123456789
    source = WmsDocuments(full([doc]))
    proof = source.match("100", "300", "A", "77", "10")
    assert proof["lines"][0]["qty_type"] == "0"
    assert proof["lines"][0]["i_id"] == "1234567890123456789"
    assert json.loads(json.dumps(proof))["lines"][0]["i_id"] == "1234567890123456789"
    doc["items"][0]["inout_item_flds"].pop("qty_type")
    doc["items"][0].pop("i_id")
    proof = WmsDocuments(full([doc])).match("100", "300", "A", "77", "10")
    assert proof["lines"][0]["qty_type"] is None and proof["lines"][0]["i_id"] is None
