"""Calibrated exact-order-ID stock receipts, with a vendor-specific empty page.

No generic WMS/refund parser is relaxed. This reader proves only returned
received-stock documents in the captured scope, never all sales refunds or a
financial zero. Calibration and negatives require the same merchant token
commitment captured in one trusted read-only context; app identity alone is
insufficient. Capture authority itself remains the trusted caller's duty.
"""
from __future__ import annotations
import base64,hashlib,json,math,re
from copy import deepcopy
from decimal import Decimal,InvalidOperation,localcontext

PATH="/open/aftersale/received/query"
SCHEMA="ledger-received-exact-order-observation.v1"

class ReceivedSourceError(ValueError):
    def __init__(self,reason):super().__init__(reason);self.reason=reason

def _need(ok,reason):
    if not ok:raise ReceivedSourceError(reason)

def _id(value):
    _need(not isinstance(value,(bool,float)),"received_identity_literal_invalid")
    text=str(value or "");_need(bool(re.fullmatch(r"[1-9][0-9]{0,23}",text)),"received_identity_literal_invalid");return text

def _number(value,positive=False):
    _need(value is not None and not isinstance(value,(bool,float)),"received_number_literal_invalid")
    try:n=Decimal(str(value))
    except (InvalidOperation,ValueError):raise ReceivedSourceError("received_number_invalid") from None
    _need(n.is_finite() and len(n.as_tuple().digits)<=100 and abs(n.as_tuple().exponent)<=100 and abs(n.adjusted())<=100
          and (not positive or n>0),"received_number_invalid")
    return n

def _integer(value):
    n=_number(value);_need(n>=0 and n==n.to_integral_value(),"received_paging_integer_invalid");return int(n)

def _pairs(pairs):
    out={}
    for k,v in pairs:_need(k not in out,"received_duplicate_json_key");out[k]=v
    return out

def _json(raw):
    try:return json.loads(raw,parse_int=Decimal,parse_float=Decimal,object_pairs_hook=_pairs,
                          parse_constant=lambda _:(_ for _ in ()).throw(ReceivedSourceError("received_nonfinite_json")))
    except (ValueError,UnicodeError,RecursionError) as e:
        if isinstance(e,ReceivedSourceError):raise
        raise ReceivedSourceError("received_json_invalid") from None


def _read(raw,envelope):
    _need(isinstance(raw,bytes) and len(raw)<=1048576,"received_response_budget")
    digest=hashlib.sha256(raw).hexdigest()
    _need(isinstance(envelope,dict) and envelope.get("http_status")==200
          and envelope.get("raw_sha256")==digest,"received_capture_sha_invalid")
    auth=envelope.get("merchant_authorization_sha256")
    _need(isinstance(auth,str) and re.fullmatch(r"[0-9a-f]{64}",auth)
          and envelope.get("shared_auth_context") is True,"received_merchant_authorization_unverified")
    captured=envelope.get("captured_at")
    _need(not isinstance(captured,bool) and isinstance(captured,(int,float)) and math.isfinite(captured) and captured>0,"received_capture_time_invalid")
    if "raw_b64" in envelope:
        text=envelope["raw_b64"];_need(isinstance(text,str) and len(text)<=4*((1048576+2)//3),"received_capture_encoding_invalid")
        try:_need(base64.b64decode(text,validate=True)==raw,"received_capture_bytes_changed")
        except (ValueError,TypeError):raise ReceivedSourceError("received_capture_encoding_invalid") from None
    req=envelope.get("request");_need(isinstance(req,dict) and req.get("method")=="POST" and req.get("path")==PATH,"received_fixed_query_path_required")
    body=req.get("body")
    # This calibrated path is exact o_ids only, with no narrowing shop, owner,
    # warehouse, date or status filter. A different query needs another proof.
    _need(isinstance(body,dict) and set(body)=={"o_ids","page_index","page_size"},"received_exact_unfiltered_query_required")
    ids=body.get("o_ids");_need(isinstance(ids,list) and 0<len(ids)<=20 and all(isinstance(x,str) for x in ids),"received_exact_id_budget_or_type")
    ids=tuple(_id(x) for x in ids);_need(len(ids)==len(set(ids)),"received_duplicate_query_id")
    _need(type(body.get("page_index")) is int and body["page_index"]==1 and type(body.get("page_size")) is int
          and 0<body["page_size"]<=50,"received_query_page_contract")
    root=_json(raw);_need(isinstance(root,dict) and not isinstance(root.get("code"),bool)
          and root.get("code") in (0,"0") and isinstance(root.get("data"),dict),"received_upstream_source_error")
    data=root["data"];_need(type(data.get("has_next")) is bool,"received_has_next_literal_required")
    page,size,total,pages=(_integer(data.get(k)) for k in ("page_index","page_size","data_count","page_count"))
    docs=data.get("datas")
    if docs is None:
        _need(page==size==total==pages==0 and data["has_next"] is False,"received_null_is_not_exact_empty_shape")
        docs=[];shape="calibrated_exact_zero_null_page"
    else:
        _need(isinstance(docs,list),"received_document_array_required")
        n=len(docs)
        if n==0:
            _need(page==1 and size==body["page_size"] and total==pages==0 and data["has_next"] is False,"received_empty_page_contract_invalid")
            shape="documented_capacity_empty_page"
        else:
            _need(page==1 and total==n and pages==1 and data["has_next"] is False,"received_scope_incomplete_or_truncated")
            _need(size==n or (size==body["page_size"] and n<=size),"received_single_page_size_invalid")
            shape="calibrated_actual_count_single_page" if size==n else "documented_capacity_single_page"
    output=[];seen=set()
    for pos,doc in enumerate(docs):
        _need(isinstance(doc,dict) and _id(doc.get("o_id")) in ids,"received_returned_order_outside_exact_scope")
        io=_id(doc.get("io_id"));order=_id(doc["o_id"]);sale=_id(doc.get("as_id"));shop=_id(doc.get("shop_id"))
        _need(io not in seen,"received_duplicate_document");seen.add(io)
        _need(doc.get("status") in {"Confirmed","Cancelled"},"received_unknown_document_status")
        owner=doc.get("owner_co_id");_need(owner is not None and not isinstance(owner,(bool,float)),"received_owner_scope_missing")
        owner=str(owner);_need(owner=="0" or re.fullmatch(r"[1-9][0-9]{0,23}",owner),"received_owner_scope_invalid")
        _need(isinstance(doc.get("items"),list),"received_stock_lines_missing");lines=[];line_ids=set()
        for index,line in enumerate(doc["items"]):
            _need(isinstance(line,dict),"received_line_invalid");line_id=_id(line.get("ioi_id"));_need(line_id not in line_ids,"received_duplicate_stock_line");line_ids.add(line_id)
            sku=line.get("sku_id");_need(isinstance(sku,str) and bool(sku) and len(sku)<=256,"received_stock_sku_invalid")
            quantity=_number(line.get("qty"),positive=doc["status"]=="Confirmed")
            lines.append({"io_id":io,"return_ioi_id":line_id,"sku_id":sku,"stock_quantity":str(quantity),"unit":line.get("unit"),
                          "combine_sku_id":line.get("combine_sku_id"),"per_unit":str(line["per_unit"]) if line.get("per_unit") is not None else None,
                          "source_pointer":f"/data/datas/{pos}/items/{index}","is_erp_oi_id":False})
        output.append({"o_id":order,"as_id":sale,"shop_id":shop,"owner_co_id":owner,"io_id":io,"status":doc["status"],
            "io_date":doc.get("io_date"),"modified":doc.get("modified"),"stock_lines":lines,
            "source_ref":{"raw_sha256":digest,"source_pointer":f"/data/datas/{pos}","source_request":deepcopy(req),"captured_at":captured}})
    return {"merchant_authorization_sha256":auth,"ids":ids,"shape":shape,"docs":output,"raw_sha256":digest,"envelope":envelope}


class ReceivedExactDocuments:
    """Calibrate source identity/existence, then observe an exact finite scope.

    headers are trusted original UI/PG header observations, not synthesized
    refund item quantities. Their sales-unit r_qty and WMS stock pieces remain
    separate, even where their numerical values happen to agree.
    """
    def __init__(self,captures,calibration_captures,calibration_headers,expected_orders):
        _need(isinstance(captures,(list,tuple)) and 0<len(captures)<=20,"received_capture_count_budget")
        _need(isinstance(calibration_captures,(list,tuple)) and 0<len(calibration_captures)<=5,"received_calibration_capture_budget")
        all_raw=[raw for raw,_ in [*captures,*calibration_captures]];_need(sum(map(len,all_raw))<=8388608,"received_total_bytes_budget")
        controls=[_read(*pair) for pair in calibration_captures];scopes=[_read(*pair) for pair in captures]
        auth={v["merchant_authorization_sha256"] for v in [*controls,*scopes]};_need(len(auth)==1,"received_calibration_merchant_mismatch")
        headers={(_id(h["o_id"]),_id(h["as_id"])):h for h in calibration_headers}
        _need(headers,"received_original_positive_headers_required");control_companies=set();control_checks=[];positive_count=0
        for control in controls:
            _need(control["docs"],"received_positive_calibration_cannot_be_empty")
            for doc in control["docs"]:
                key=(doc["o_id"],doc["as_id"]);header=headers.get(key);_need(header is not None,"received_positive_original_identity_conflict")
                _need(doc["status"]=="Confirmed" and doc["stock_lines"],"received_positive_stock_receipt_unproved")
                _need(doc["shop_id"]==_id(header["shop_id"]) and doc["owner_co_id"]==str(header["owner_co_id"]),"received_positive_original_scope_conflict")
                positive=_number(header.get("r_qty"),positive=True);company=_id(header["co_id"]);control_companies.add(company)
                with localcontext() as context:
                    context.prec=256;stock=sum((_number(line["stock_quantity"],positive=True) for line in doc["stock_lines"]),Decimal(0))
                control_checks.append({"o_id":doc["o_id"],"as_id":doc["as_id"],"stock_lines":len(doc["stock_lines"]),
                    "original_header_received_quantity_positive":positive>0,"stock_quantity_positive":stock>0,
                    "sales_and_stock_units_numerically_equal":positive==stock,"does_not_prove_unit_conversion":True,
                    "company_scope_original_header":company,"source_ref":doc["source_ref"]})
                positive_count+=1
        _need(positive_count>0,"received_positive_calibration_required")
        expected={_id(k):v for k,v in expected_orders.items()};_need(expected,"received_expected_original_scope_required")
        observed={oid for scope in scopes for oid in scope["ids"]};_need(observed==set(expected),"received_target_scope_missing_or_expanded")
        self._observations={};self.calibration={"schema":SCHEMA,"positive_documents":positive_count,"controls":control_checks,
            "merchant_authorization_sha256":next(iter(auth)),"proves_sales_stock_unit_equivalence":False,"financial_authority":None}
        for scope in scopes:
            for oid in scope["ids"]:
                original=expected[oid];_need(_id(original["company_id"]) in control_companies,"received_target_company_uncalibrated")
                docs=[d for d in scope["docs"] if d["o_id"]==oid]
                for doc in docs:
                    _need(doc["shop_id"]==_id(original["shop_id"]),"received_target_original_shop_conflict")
                    owner=_id(original["company_id"]) if doc["owner_co_id"]=="0" else doc["owner_co_id"]
                    _need(owner==_id(original["company_id"]),"received_target_original_owner_conflict")
                observed_value={"schema":SCHEMA,"status":"observed","o_id":oid,"complete_exact_received_scope":True,
                    "received_documents":docs,"no_registered_received_documents_in_scope":not docs,
                    "wire_shape":scope["shape"],"scope_raw_sha256":scope["raw_sha256"],
                    "source_request":deepcopy(scope["envelope"]["request"]),"merchant_authorization_sha256":next(iter(auth)),
                    "covers_actual_received_inventory_only":True,"covers_all_sales_refunds":False,
                    "proves_no_returns":False,"proves_no_future_returns":False,"financial_authority":None,"financial_ready":False}
                old=self._observations.get(oid)
                if old:
                    _need(old["received_documents"]==observed_value["received_documents"],"received_observation_version_conflict")
                self._observations[oid]=observed_value

    def for_order(self,o_id):
        oid=_id(o_id)
        if oid not in self._observations:
            return {"schema":SCHEMA,"status":"pending","reason":"received_order_not_covered","financial_ready":False,"financial_authority":None}
        return deepcopy(self._observations[oid])
