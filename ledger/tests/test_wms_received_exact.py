"""Only the calibrated received-exact wire contract may certify empty scope."""
from copy import deepcopy
import hashlib,json
import pytest
from ledger.wms_received_exact import ReceivedExactDocuments,ReceivedSourceError,PATH


def document(order=900,sale=800,shop=77,owner=0):
    return {"o_id":order,"as_id":sale,"shop_id":shop,"owner_co_id":owner,"io_id":700,
            "status":"Confirmed","io_date":"2026-08-01 12:00:00","modified":"2026-08-01 12:00:00",
            "items":[{"ioi_id":701,"sku_id":"A","qty":"2","unit":"piece","combine_sku_id":"KIT","per_unit":None}]}


def capture(ids,docs=None,*,null=False,auth="a"*64):
    data={"datas":None if null else (docs or []),"page_index":0 if null else 1,"page_size":0 if null else len(docs or []),
          "has_next":False,"data_count":0 if null else len(docs or []),"page_count":0 if null or not docs else 1}
    raw=json.dumps({"code":0,"data":data}).encode();env={"http_status":200,"raw_sha256":hashlib.sha256(raw).hexdigest(),
        "captured_at":1793500000.0,"merchant_authorization_sha256":auth,"shared_auth_context":True,
        "provider_public_identity_sha256":"p"*64,
        "request":{"method":"POST","path":PATH,"body":{"o_ids":list(map(str,ids)),"page_index":1,"page_size":50}}}
    return raw,env


def fixture():
    control=capture([900],[document()]);empty=capture([100,101],null=True)
    headers=[{"o_id":"900","as_id":"800","shop_id":"77","owner_co_id":"0","co_id":"10","r_qty":"1"}]
    expected={str(x):{"company_id":"10","shop_id":"77"} for x in (100,101)}
    return [empty],[control],headers,expected


def rewrite(pair,change):
    raw,env=deepcopy(pair);data=json.loads(raw);change(data,env);raw=json.dumps(data).encode();env["raw_sha256"]=hashlib.sha256(raw).hexdigest();return raw,env


def test_positive_same_merchant_control_calibrates_exact_null_zero_wire_shape_only():
    reader=ReceivedExactDocuments(*fixture());o=reader.for_order("100")
    assert reader.calibration["positive_documents"]==1
    assert reader.calibration["controls"][0]["sales_and_stock_units_numerically_equal"] is False
    assert reader.calibration["proves_sales_stock_unit_equivalence"] is False
    assert o["complete_exact_received_scope"] is True and o["no_registered_received_documents_in_scope"] is True
    assert o["wire_shape"]=="calibrated_exact_zero_null_page"
    assert o["proves_no_returns"] is False and o["covers_all_sales_refunds"] is False
    assert o["financial_ready"] is False and o["financial_authority"] is None
    assert reader.for_order("102")["status"]=="pending"


def test_same_application_different_merchant_authorization_cannot_calibrate_absence():
    c=fixture();c[0][0][1]["merchant_authorization_sha256"]="b"*64
    with pytest.raises(ReceivedSourceError,match="merchant_mismatch"):ReceivedExactDocuments(*c)


@pytest.mark.parametrize("field",["merchant_authorization_sha256","shared_auth_context"])
def test_old_public_application_hash_alone_is_not_retroactive_merchant_proof(field):
    c=fixture();c[0][0][1].pop(field)
    with pytest.raises(ReceivedSourceError,match="merchant_authorization_unverified"):ReceivedExactDocuments(*c)


@pytest.mark.parametrize("field,value",[("page_index",1),("page_size",50),("data_count",1),("page_count",1),("has_next",True)])
def test_other_null_shapes_are_not_silently_empty(field,value):
    c=fixture();c[0][0]=rewrite(c[0][0],lambda body,env:body['data'].update({field:value}))
    with pytest.raises(ReceivedSourceError):ReceivedExactDocuments(*c)


def test_nonempty_actual_n_page_keeps_real_inventory_ids_and_units():
    c=fixture();c[0][0]=capture([100,101],[document(order=100,sale=801)])
    reader=ReceivedExactDocuments(*c);o=reader.for_order("100")
    assert o["wire_shape"]=="calibrated_actual_count_single_page" and not o["no_registered_received_documents_in_scope"]
    line=o["received_documents"][0]["stock_lines"][0]
    assert line["return_ioi_id"]=="701" and line["is_erp_oi_id"] is False
    assert line["stock_quantity"]=="2" and line["unit"]=="piece"


def test_truncation_or_more_pages_is_pending_not_declared_scope_complete():
    c=fixture();c[0][0]=capture([100,101],[document(order=100)])
    c[0][0]=rewrite(c[0][0],lambda body,env:body['data'].update(data_count=2,page_count=2,has_next=True))
    with pytest.raises(ReceivedSourceError,match="incomplete_or_truncated"):ReceivedExactDocuments(*c)


@pytest.mark.parametrize("change",[
    lambda body,env:env['request']['body'].update(shop_id=77),
    lambda body,env:env['request']['body'].update(owner_co_ids='0'),
    lambda body,env:env['request']['body'].update(modified_begin='2026-05-01'),
    lambda body,env:env['request'].update(path='/open/aftersale/upload'),
])
def test_narrowing_filters_and_write_paths_are_not_the_calibrated_exact_query(change):
    c=fixture();c[0][0]=rewrite(c[0][0],change)
    with pytest.raises(ReceivedSourceError):ReceivedExactDocuments(*c)


def test_missing_target_ids_or_expanded_targets_are_not_a_full_company_month_claim():
    c=fixture();c[0][0]=capture([100],null=True)
    with pytest.raises(ReceivedSourceError,match="scope_missing_or_expanded"):ReceivedExactDocuments(*c)


def test_wrong_positive_original_as_id_cannot_certify_an_empty_negative():
    c=fixture();c[2][0]['as_id']='801'
    with pytest.raises(ReceivedSourceError,match="positive_original_identity_conflict"):ReceivedExactDocuments(*c)


def test_header_requested_qty_is_not_substituted_for_a_missing_real_received_quantity():
    c=fixture();c[2][0]['r_qty']=None
    with pytest.raises(ReceivedSourceError):ReceivedExactDocuments(*c)


def test_changed_source_bytes_fail_before_empty_interpretation():
    c=fixture();raw,env=c[0][0];c[0][0]=(raw+b'changed',env)
    with pytest.raises(ReceivedSourceError,match="capture_sha_invalid"):ReceivedExactDocuments(*c)
