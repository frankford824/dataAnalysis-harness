"""Independent native-order -> original ERP sale -> physical-member proof.

This is a new contract, not an empty-cost-anchor exception in V3. Original
quantities missing in a native export remain absent; only explicit ERP raw
sales quantities or unmaterialized combination sales records may supply them.
No amounts, network calls, grants or financial writes occur in this module.
"""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, DecimalException, localcontext
from datetime import datetime
import hashlib
import xml.etree.ElementTree as ET
import zipfile

from .jst_source_members import (Anchor, Capture, Node, PARSER_SHA256, MAX_BLOBS,
    MemberPending, _need, _id, _number, _day, _company, _created_day, _platform,
    _Workbook, _related, _observed, parse_order)

SCHEMA="jst_native_source_members.v1"

@dataclass(frozen=True)
class NativeRequest:
    source_order_id: str
    platform_order_id: str
    platform_sub_order_id: str
    platform: str
    store_names: tuple[str, ...]
    shop_ids: tuple[str, ...]
    native_anchors: tuple[Anchor, ...]


def _native(request, blobs):
    contracts={"alibaba1688":("订单号",None,"订单创建时间",("数量",),()),
               "taobao":("主订单编号","子订单编号","订单创建时间",("购买数量","数量"),("商家编码","货号")),
               "douyin":("主订单编号","子订单编号","订单提交时间",("商品数量",),())}
    _need(request.platform in contracts,"native_platform_contract_pending")
    parent,child,stamp,quantities,skus=contracts[request.platform]
    records=[];books={};days=set()
    for a in request.native_anchors:
        _need(isinstance(a,Anchor) and isinstance(a.row,int) and not isinstance(a.row,bool)
              and 2<a.row<=1048576 and a.sha256 in blobs,"native_original_anchor_invalid")
        if a.sha256 not in books:books[a.sha256]=_Workbook(blobs[a.sha256])
        book=books[a.sha256];needed=(parent,stamp,*((child,) if child else ()))
        identity_roles=(*needed,*quantities,*skus,"Offer_ID","SKU_ID","单位")
        header=book.headers(a.sheet,identity_roles,("订单号",) if request.platform=="alibaba1688" else ())
        _need(all(k in header for k in needed),"native_header_contract_missing")
        _need(book.value(a.sheet,a.row,header[parent])==request.platform_order_id,"native_original_parent_conflict")
        if child:_need(book.value(a.sheet,a.row,header[child])==request.platform_sub_order_id,"native_original_child_conflict")
        day=book.day(a.sheet,a.row,header[stamp]);days.add(day)
        def optional_cell(column):
            sheet_rows,merges=book._sheet(a.sheet)
            number=book.column_number(column)
            in_merge=any(start<=a.row<=end and left<=number<=right for _,start,left,end,right in merges)
            if column not in sheet_rows.get(a.row,{}) and not in_merge:return None
            return book.cell(a.sheet,a.row,column)
        found=[];qty_refs=[]
        for name in quantities:
            if name not in header:continue
            cell=optional_cell(header[name])
            if cell is None or cell[0] in (None,""):continue
            _need(cell[1]=="n","native_quantity_literal_missing")
            found.append(_number(cell[0]));qty_refs.append({"column":name,"cell":cell[3],"raw":cell[0]})
        _need(len(set(found))<=1,"native_quantity_columns_conflict")
        sku_values={book.value(a.sheet,a.row,header[name]) for name in skus
                    if name in header and optional_cell(header[name]) is not None}
        sku_values.discard("");_need(len(sku_values)<=1,"native_sku_columns_conflict")
        product=book.value(a.sheet,a.row,header["Offer_ID"]) if "Offer_ID" in header and optional_cell(header["Offer_ID"]) is not None else ""
        variant=book.value(a.sheet,a.row,header["SKU_ID"]) if "SKU_ID" in header and optional_cell(header["SKU_ID"]) is not None else ""
        unit=book.value(a.sheet,a.row,header["单位"]) if "单位" in header and optional_cell(header["单位"]) is not None else ""
        if request.platform=="alibaba1688" and (product or variant):
            _need(bool(product) and bool(variant),"native_platform_variant_identity_incomplete")
        records.append({"sku":next(iter(sku_values)) if sku_values else None,
                        "platform_product_id":product,"platform_variant_id":variant,"sales_unit_label":unit,
                        "quantity":next(iter(found)) if found else None,
                        "anchor":{"sha256":a.sha256,"sheet":a.sheet,"row":a.row},"quantity_cells":qty_refs})
    _need(len(days)==1 and records,"native_original_day_conflict")
    if request.platform!="alibaba1688":
        # Native child IDs are one sales line. Repeated original exports must
        # agree; duplicate rows inside one export are not silently deduplicated.
        identities=[(r["anchor"]["sha256"],r["anchor"]["sheet"]) for r in records]
        _need(len(identities)==len(set(identities)),"native_child_original_rows_ambiguous")
        _need(len({(r["sku"],r["quantity"]) for r in records})==1,"native_original_exports_conflict")
        records=records[:1]
    return next(iter(days)),records


def _graph(request,capture,blobs,shops,original_day):
    source_id=_id(request.source_order_id);nodes={};refs={}
    for ref in capture.nodes:
        _need(isinstance(ref,Node) and ref.sha256 in blobs and ref.order_id not in nodes,"native_source_node_ambiguous")
        data,raw_proof=parse_order(blobs[ref.sha256],ref.order_id)
        _need(_id(data.get("shop_id")) in shops,"native_source_cross_shop")
        _need(data.get("type")=="普通订单","native_nonordinary_source_separate")
        _company(data)
        _need(all(_id(i.get("co_id"))==_id(data.get("co_id")) for i in data["items"]),"native_source_company_namespace_conflict")
        _need(data.get("status") in {"Sent","Split","Merged"},"native_source_lifecycle_unproved")
        nodes[ref.order_id]=data;refs[ref.order_id]=raw_proof
    _need(source_id in nodes,"native_original_sale_node_missing")
    source=nodes[source_id]
    _need(_day(source.get("order_date"))==original_day,"native_source_original_day_conflict")
    _need(request.platform_order_id in {_platform(v,shops) for v in str(source.get("so_id") or "").split(",")},"native_source_parent_conflict")
    related={}
    for ref in capture.relations:
        _need(isinstance(ref,Node) and ref.order_id in nodes and ref.order_id not in related and ref.sha256 in blobs,"native_source_relation_ambiguous")
        related[ref.order_id]=_related(blobs[ref.sha256]);_need(related[ref.order_id]<=set(nodes),"native_source_graph_incomplete")
    edges=defaultdict(set)
    for oid,node in nodes.items():
        target=node.get("link_o_id")
        if target in (None,"",0,Decimal(0)):continue
        target=_id(target);_need(target in nodes,"native_source_typed_target_missing")
        a,b=(oid,target) if node["status"]=="Merged" else (target,oid)
        _need(a!=b and (b in related.get(a,set()) or a in related.get(b,set())),"native_source_relation_not_observed")
        edges[a].add(b)
    visiting=set();visited=set()
    def walk(oid):
        _need(oid not in visiting,"native_source_graph_cycle")
        if oid in visited:return
        visiting.add(oid)
        for child in edges[oid]:walk(child)
        visiting.remove(oid);visited.add(oid)
    walk(source_id)
    _need(all(oid in related for oid in visited if nodes[oid]["status"] in {"Split","Merged"}),"native_source_graph_incomplete")
    terminals={oid:nodes[oid] for oid in sorted(visited) if nodes[oid]["status"]=="Sent"}
    _need(terminals,"native_no_actual_terminal_shipments")
    graph_refs=[{"kind":kind,"order_id":r.order_id,"sha256":r.sha256}
                for kind,collection in (("detail",capture.nodes),("relations",capture.relations)) for r in collection]
    return source,nodes,terminals,refs,graph_refs


def _normal(row,*,physical=False):
    _need(row.get("sku_type")=="normal" and row.get("is_gift") is False,"native_normal_nongift_basis_missing")
    for flag in ("is_virtual","is_suspect"):
        _need(flag not in row or row[flag] is False,"native_explicit_member_risk_unresolved")
    if physical:
        _need(row.get("item_status")=="Sent" and bool(row.get("created"))
              and row.get("is_combine_item") is not True and row.get("is_combine") is not True,"native_physical_lifecycle_unproved")



def _bundle_action_inventory(source,sku,quantity,entries,original_day,terminals):
    """Read the original order's explicit KIT*q -> A*a,B*b operation record.

    Quantities are original ERP inventory declarations, never inferred by
    dividing actual shipped pieces. Every competing declaration is retained;
    differing mappings for this same original sale remain a conflict.
    """
    plans=[]
    for position,action in enumerate(source.get("actions",[])):
        if not isinstance(action,dict) or action.get("name")!="拆开组合装商品":continue
        text=action.get("remark")
        if not isinstance(text,str) or text.count("->")!=1:continue
        left,right=text.split("->")
        if "*" not in left:continue
        logical,raw_qty=left.rsplit("*",1)
        if logical!=sku:continue
        q=_number(raw_qty)
        if q!=quantity:continue
        _need(_id(action.get("o_id"))==_id(source.get("o_id"))
              and _id(action.get("co_id"))==_id(source.get("co_id")),"native_bundle_action_owner_conflict")
        _need(_created_day(action.get("created"))>=original_day,"native_bundle_action_precedes_original")
        pieces={}
        for part in right.split(","):
            _need("*" in part,"native_bundle_action_mapping_invalid")
            component,raw_piece=part.rsplit("*",1)
            _need(bool(component) and component not in pieces,"native_bundle_action_component_ambiguous")
            pieces[component]=_number(raw_piece)
        _need(0<len(pieces)<=2000,"native_bundle_action_component_budget")
        plans.append((position,action,pieces))
    _need(plans,"native_original_bundle_action_missing")
    ship_times=[]
    for terminal in terminals.values():
        stamp=terminal.get("send_date")
        if stamp:
            _created_day(stamp);ship_times.append(datetime.fromisoformat(stamp.replace("Z","+00:00")))
        else:
            for action in terminal.get("actions",[]):
                if (isinstance(action,dict) and action.get("name")=="发货"
                    and str(action.get("o_id"))==str(terminal["o_id"])
                    and str(action.get("co_id"))==str(terminal["co_id"])):
                    _created_day(action.get("created"));ship_times.append(datetime.fromisoformat(action["created"].replace("Z","+00:00")))
    _need(ship_times,"native_bundle_actual_shipment_time_missing")
    first_ship=min(ship_times)
    _need(any(datetime.fromisoformat(a["created"].replace("Z","+00:00"))<=first_ship for _,a,_ in plans),"native_bundle_action_after_shipment")
    variants={tuple(sorted(p.items())) for _,_,p in plans}
    _need(len(variants)==1,"native_original_bundle_action_changed")
    with localcontext() as context:
        context.prec=256;expected=defaultdict(Decimal)
        for _,row in entries:expected[str(row["sku_id"])]+=_number(row.get("qty"))
    _need(dict(expected)==plans[0][2],"native_original_bundle_action_stock_mapping_conflict")
    return [{"pointer":"/actions/"+str(index),"created":action["created"],"raw_remark":action["remark"]} for index,action,_ in plans]


def normalize_native_members(request,capture,blobs):
    result={"schema":SCHEMA,"status":"pending","members":[],"logical_sales":[],"reasons":[],"financial_applied":False}
    try:
        _need(isinstance(request,NativeRequest) and isinstance(capture,Capture),"typed_native_source_request_required")
        _need(0<len(request.native_anchors)<=100 and 0<len(capture.nodes)<=12 and len(capture.relations)<=6,"native_source_budget")
        _need(capture.parser_sha256==PARSER_SHA256,"native_source_parser_identity_changed")
        _need(len(blobs)<=240 and all(isinstance(v,bytes) for v in blobs.values())
              and sum(map(len,blobs.values()))<=MAX_BLOBS,"native_source_blob_budget")
        _need(all(hashlib.sha256(raw).hexdigest()==digest for digest,raw in blobs.items()),"native_source_blob_sha_changed")
        shops={_id(v) for v in request.shop_ids};_need(shops and request.store_names,"native_source_shop_scope_missing")
        if request.platform!="alibaba1688":_need(bool(request.platform_sub_order_id),"native_child_identity_missing")
        original_day,native=_native(request,blobs)
        source,nodes,terminals,refs,graph_refs=_graph(request,capture,blobs,shops,original_day)
        native_pairs={(r["platform_product_id"],r["platform_variant_id"]) for r in native
                      if r["platform_product_id"] and r["platform_variant_id"]} if request.platform=="alibaba1688" else set()
        if native_pairs:_need(all(r["platform_product_id"] and r["platform_variant_id"] for r in native),"native_platform_variant_identity_partially_missing")
        selected=[]
        for index,row in enumerate(source["items"]):
            if _platform(row.get("oi_so_id"),shops)!=request.platform_order_id:continue
            if request.platform!="alibaba1688" and str(row.get("outer_oi_id") or "")!=request.platform_sub_order_id:continue
            if native_pairs:
                _need((str(row.get("shop_i_id") or ""),str(row.get("shop_sku_id") or "")) in native_pairs,"native_source_platform_variant_conflict")
            _normal(row)
            _need(bool(row.get("raw_so_id")) and bool(row.get("raw_sku_id")),"native_original_sale_binding_incomplete")
            selected.append((index,row))
        _need(selected,"native_original_sales_member_missing")
        grouped=defaultdict(list)
        for index,row in selected:
            group=(str(row["shop_i_id"]),str(row["shop_sku_id"])) if native_pairs else _id(row.get("oi_id"))
            grouped[group].append((index,row))
        logical=[]
        for group,entries in sorted(grouped.items()):
            iid="native-platform-variant:"+":".join(group) if native_pairs else group
            controls=[r for r in native if (r["platform_product_id"],r["platform_variant_id"])==group] if native_pairs else native
            rows=[r for _,r in entries]
            scalar=(len(rows)==1 and str(rows[0].get("sku_id"))==str(rows[0].get("raw_sku_id"))
                    and rows[0].get("src_combine_sku_id") in (None,"",rows[0].get("sku_id"))
                    and rows[0].get("is_combine_item") is not True and rows[0].get("is_combine") is not True)
            if scalar:
                row=rows[0];sku=str(row["sku_id"]);qty=_number(row.get("qty"))
                # Explicit raw_qty is ERP's original sale quantity, separate
                # from the fulfilled item's stock qty. Never back-solve it.
                native_control=(bool(native_pairs) and all(r["quantity"] is not None for r in controls)) or any(r["sku"]==sku and r["quantity"] is not None for r in native) or (
                    len(grouped)==1 and len(native)==1 and not native[0]["sku"] and native[0]["quantity"] is not None)
                if row.get("raw_qty") is not None:
                    _need(qty==_number(row["raw_qty"]),"native_scalar_sales_stock_unit_conversion_unproved")
                if native_control:
                    basis={"kind":"native_original_literal_sales_quantity","anchors":[r["anchor"] for r in controls],
                           "quantity_cells":[q for r in controls for q in r["quantity_cells"]]}
                else:
                    raw_qty=_number(row.get("raw_qty"))
                    _need(qty==raw_qty,"native_scalar_sales_stock_unit_conversion_unproved")
                    basis={"kind":"erp_original_raw_sales_quantity","pointer":"/items/"+str(entries[0][0])+"/raw_qty","raw":str(row["raw_qty"]),"source_sha256":refs[request.source_order_id]["sha256"]}
            else:
                skus={str(r.get("src_combine_sku_id") or "") for r in rows}
                _need(len(skus)==1 and "" not in skus and all(str(r.get("raw_sku_id"))==next(iter(skus)) for r in rows),"native_original_bundle_sales_mapping_missing")
                sku=next(iter(skus));quantities={_number(r.get("src_combine_sku_qty")) for r in rows}
                _need(len(quantities)==1,"native_bundle_original_sales_units_conflict")
                qty=next(iter(quantities));prototype=all(r.get("is_combine_item") is True and not r.get("created") for r in rows)
                if prototype:
                    basis={"kind":"erp_original_combination_sales_quantity","pointer":["/items/"+str(i)+"/src_combine_sku_qty" for i,_ in entries],"raw":str(qty),"source_sha256":refs[request.source_order_id]["sha256"]}
                else:
                    _need(bool(native_pairs) and controls and all(r["quantity"] is not None for r in controls),"native_original_bundle_sales_mapping_missing")
                    with localcontext() as context:
                        context.prec=256;native_quantity=sum((r["quantity"] for r in controls),Decimal(0))
                    _need(native_quantity==qty,"native_original_bundle_sales_units_conflict")
                    actions=_bundle_action_inventory(source,sku,qty,entries,original_day,terminals)
                    basis={"kind":"native_platform_variant_quantity_and_original_bundle_action","platform_product_id":group[0],"platform_variant_id":group[1],
                           "native_anchors":[r["anchor"] for r in controls],"raw_logical_quantity":str(qty),
                           "original_bundle_actions":actions,"source_sha256":refs[request.source_order_id]["sha256"]}
            logical.append({"id":iid,"sku":sku,"quantity":qty,"entries":entries,"scalar":scalar,"basis":basis,"platform_pair":group if native_pairs else None})
        # A native SKU and quantity are an independent control when supplied.
        # Missing cells stay explicitly missing and use the labelled ERP-only
        # sale basis above, never a fabricated native value or a quantity of 1.
        known_native={r["sku"] for r in native if r["sku"]}
        if native_pairs:
            _need({sale["platform_pair"] for sale in logical}==native_pairs,"native_original_platform_variants_missing")
            for sale in logical:
                controls=[r for r in native if (r["platform_product_id"],r["platform_variant_id"])==sale["platform_pair"]]
                _need(all(r["quantity"] is not None for r in controls),"native_sales_quantity_partially_missing")
                with localcontext() as context:
                    context.prec=256;total=sum((r["quantity"] for r in controls),Decimal(0))
                _need(total==sale["quantity"],"native_original_sales_quantity_conflict")
        elif known_native:
            _need(all(r["sku"] for r in native),"native_sales_sku_partially_missing")
            with localcontext() as context:
                context.prec=256
                native_totals=defaultdict(Decimal);source_totals=defaultdict(Decimal)
                for r in native:
                    _need(r["quantity"] is not None,"native_sales_quantity_partially_missing")
                    native_totals[r["sku"]]+=r["quantity"]
                for sale in logical:source_totals[sale["sku"]]+=sale["quantity"]
                _need(dict(native_totals)==dict(source_totals),"native_original_sales_sku_quantity_conflict")
        elif any(r["quantity"] is not None for r in native):
            _need(len(logical)==1 and len(native)==1 and native[0]["quantity"]==logical[0]["quantity"],"native_original_sales_quantity_conflict")
        used=set();members=[]
        for sale in logical:
            component_keys=set()
            for index,parent in sale["entries"]:
                fields=("oi_so_id","outer_oi_id","raw_so_id","outer_so_id","raw_sku_id","src_combine_sku_id","shop_i_id","shop_sku_id")
                binding={k:str(parent[k]) for k in fields if parent.get(k) not in (None,"")}
                key=str(parent["sku_id"]),tuple(sorted(binding.items()));_need(key not in component_keys,"native_original_component_ambiguous");component_keys.add(key)
                matches=[]
                for oid,target in terminals.items():
                    for position,row in enumerate(target["items"]):
                        if str(row.get("sku_id"))!=key[0] or any(str(row.get(k,""))!=v for k,v in binding.items()):continue
                        _normal(row,physical=True);_need(_created_day(row["created"])>=original_day,"native_physical_creation_precedes_original")
                        physical=(oid,_id(row.get("oi_id")));_need(physical not in used,"native_physical_member_reused")
                        _need(_company(target)==_company(source),"native_source_price_company_changed")
                        matches.append((physical,row,position))
                _need(matches and len({m[0] for m in matches})==len(matches),"native_physical_members_missing_or_ambiguous")
                with localcontext() as context:
                    context.prec=256;quantity=sum((_number(row["qty"]) for _,row,_ in matches),Decimal(0))
                _need(quantity==_number(parent["qty"]),"native_physical_stock_quantity_not_conserved")
                for physical,row,position in matches:
                    used.add(physical);members.append({"internal_order_id":physical[0],"internal_sub_order_id":physical[1],
                        "source_order_id":request.source_order_id,"source_member_id":sale["id"],"source_sku":sale["sku"],
                        "sku":key[0],"quantity":str(_number(row["qty"])),"logical_quantity":str(sale["quantity"]),
                        "quantity_basis":sale["basis"],"bundle_component":not sale["scalar"],
                        "platform_order_id":request.platform_order_id,"platform_sub_order_id":request.platform_sub_order_id,
                        "pricing_day":original_day,"company":_company(source),"shop_id":_id(source["shop_id"]),
                        "native_anchors":[r["anchor"] for r in native],"native_quantity_present":any(r["quantity"] is not None for r in native),
                        "source_component_member_id":_id(parent["oi_id"]),"source_sales_pointer":"/items/"+str(index),"physical_item_pointer":"/items/"+str(position),
                        "source_raw":refs[request.source_order_id],"physical_raw":refs[physical[0]],"graph_refs":graph_refs,
                        "observed_flags":{"source_member":_observed(parent,("sku_type","is_gift","is_virtual","is_suspect","created","item_status")),
                                          "physical_member":_observed(row,("sku_type","is_gift","is_virtual","is_suspect","created","item_status"))}})
            result["logical_sales"].append({"member_id":sale["id"],"sku":sale["sku"],"quantity":str(sale["quantity"]),"basis":sale["basis"],"native_quantity_present":any(r["quantity"] is not None for r in native)})
        result.update(status="proved",members=members)
    except MemberPending as exc:
        result["reasons"].append(exc.reason);result["members"]=[]
    except (KeyError,IndexError,TypeError,ValueError,ET.ParseError,zipfile.BadZipFile,OverflowError,DecimalException):
        result["reasons"].append("native_source_contract_invalid");result["members"]=[]
    return result
