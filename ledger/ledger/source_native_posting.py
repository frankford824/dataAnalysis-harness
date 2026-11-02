"""Isolated source-native/full-history inputs for the original cost engine.

This pure adapter installs no production caller or authorization. Source-native
proof and genuine all-history quotes keep distinct schemas from original-cost
anchors and the V3 dated-HTTP quote contract.
"""
from __future__ import annotations
from datetime import date
import hashlib
import json

import polars as pl
from .automation_cost_actions import Pending
from .automation_source_cost_v3 import positive,exact_product
from .source_native_members import normalize_native_members
from .full_history_intervals import FullHistoryIntervals


def render_native_history_items(ingestion,baseline,store,period,target,request,capture,blobs,history_sources,*,input_version):
    from .source_cost_posting import guard_return_identity,supported_metric,physical_key
    from .engine.runtime import Ingested
    from .engine.types import FileRef,Recognition
    from .model.schema import Template,ColumnBinding
    if not isinstance(input_version,str) or not input_version.strip() or len(input_version)>128:
        raise Pending("native_preview_input_version_required")
    if request.platform!=store.platform or set(request.store_names)!=set((store.name,*store.aliases)):
        raise Pending("native_preview_store_scope_conflict")
    role_key=request.platform_sub_order_id if request.platform in {"taobao","douyin"} else request.platform_order_id
    if str(target.get("order_id"))!=request.platform_order_id or str(target.get("coverage_key"))!=role_key:
        raise Pending("native_preview_target_identity_conflict")
    proof=normalize_native_members(request,capture,blobs)
    if proof["status"]!="proved":raise Pending("native_source_pending:"+",".join(proof["reasons"]))
    members=proof["members"];metric=supported_metric(ingestion.model,store)
    expected_role="order.sub_order_id" if request.platform in {"taobao","douyin"} else "order.order_id"
    if metric.link.to!=expected_role:raise Pending("native_cost_metric_grain_unsupported")
    guard_return_identity(ingestion,store,members)
    financial=baseline.slices.get((store.name,period))
    if financial is None:raise Pending("native_current_financial_slice_missing")
    required={"metric_id","counted","link_key","contribution"}
    if not required<=set(financial.facts.columns):raise Pending("native_current_financial_provenance_missing")
    existing=financial.facts.filter((pl.col("metric_id")=="goods_cost")&pl.col("counted")
        &(pl.col("link_key")==role_key)&(pl.col("contribution")!=0))
    if existing.height:raise Pending("native_existing_cost_authority_preserved")
    quotes={};groups={};physical=set()
    for member in members:
        if member["pricing_day"][:7]!=period:raise Pending("native_original_month_conflict")
        key=(member["company"],member["sku"],member["pricing_day"])
        if key not in quotes:
            source=history_sources.get(key)
            if not isinstance(source,tuple) or len(source)!=2:raise Pending("native_full_history_source_missing")
            # Reparse raw/envelope, preserving actual GetWay=all identity; no
            # synthetic as_of response, artifact refs or financial grant.
            quotes[key]=FullHistoryIntervals(source[0],source[1]).quote(key[1],key[0],key[2])
        quote=quotes[key];amount=exact_product(member["quantity"],quote["unit_cost"])
        oid,iid=physical_key(member)
        if (oid,iid) in physical:raise Pending("native_physical_member_reused")
        physical.add((oid,iid))
        physical_sha=member["physical_raw"]["sha256"]
        pointer=member["physical_item_pointer"];position=int(pointer.rsplit("/",1)[1])
        evidence={"schema":"native_source_history_preview.v1","input_version":input_version,
                  "order_date":member["pricing_day"],"cost_company_id":member["company"],
                  "source_rule":"native_source_history_preview.v1","source_member_proof":member,
                  "quote":quote,"raw_quantity":member["quantity"],"raw_unit_cost":quote["unit_cost"],
                  "source_scope":"native_proved_physical_member"}
        row={"order_id":member["platform_order_id"],"sub_order_id":member["platform_sub_order_id"] or iid,
             "original_order_id":member["platform_order_id"],"internal_order_id":oid,"internal_sub_order_id":iid,
             "parent_internal_order_id":member["source_order_id"],"sku":member["sku"],
             "quantity":float(positive(member["quantity"])),"unit_cost":float(positive(quote["unit_cost"])),"total_cost":float(amount),
             "order_time":member["pricing_day"],"order_date":date.fromisoformat(member["pricing_day"]),
             "order_type":"普通订单","order_state":"Sent","store_name":store.name,
             "__hint_store__":store.name,"__hint_period__":period,"__sign__":1.0,"__parent_first__":True,
             "__sha__":physical_sha,"__sheet__":"/items","__row__":position+1,"__file__":"JST-"+physical_sha+".html",
             "cost_company_id":member["company"],"cost_source":"history","cost_status":"priced","cost_as_of":member["pricing_day"],
             "source_pricing_day":member["pricing_day"],"pricing_evidence":json.dumps(evidence,sort_keys=True,ensure_ascii=False),
             "pricing_missing":False,"pricing_suspect":False,"pricing_identity_changed":False,"pricing_source_suspect":False,
             "source_note":"原生原件身份及原销售数量与实际成员已核验；全历史原日有效价；隔离输入版本 "+input_version,
             "__source_v3_native_child_present":bool(member["platform_sub_order_id"])}
        groups.setdefault(physical_sha,[]).append(row)
    template=Template(id="native_source_history_preview_v1",name="原生原件与上游实际成员隔离对照",source="order_cost",
        match_columns=("__native_source_history_preview__",),time_slots={"order_date":"order_time"},
        bindings=tuple(ColumnBinding(role=role,columns=(role,)) for role in
            ("order_id","sub_order_id","original_order_id","internal_order_id","internal_sub_order_id", "sku","quantity","unit_cost",
             "order_time","order_state","order_type","store_name")))
    items=[]
    for digest,rows in sorted(groups.items()):
        frame=pl.from_dicts(rows,infer_schema_length=None,strict=False);ref=FileRef(digest,"JST-"+digest+".html","/items")
        items.append(Ingested(ref=ref,template=template,frame=frame,rows=frame.height,
            recognition=Recognition(ref=ref,signature=template.signature,header_count=len(frame.columns),source_id="order_cost",template_id=template.id),
            notes=["native-only独立契约；数量不补1、不伪造成本原件或V3授权"]))
    return items,set(),physical
