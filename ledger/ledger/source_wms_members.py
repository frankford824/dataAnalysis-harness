"""Independent WMS-backed physical sources; original ERP flags stay untouched.

This separate contract retains the original native/cost XML controls and graph
bindings. Expanded logical recipes are not physical observations. Only a true
WMS line can replace missing physical evidence, with its own complete scopes,
company/IDs/quantity and raw provenance. No money or financial writes occur.
"""
from __future__ import annotations
from collections import defaultdict
from decimal import Decimal,DecimalException,localcontext
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Mapping
from .jst_source_members import (Request,Capture,Anchor,Node,MemberPending,PARSER_SHA256,MAX_BLOBS,
    _need,_hash,_id,_number,_day,_company,_platform,_created_day,_observed,_Workbook,_related,parse_order)
from .wms_confirmed_lines import WmsDocuments

SCHEMA="jst_wms_source_members.v1"


def _physical_observations(item,target,oid,index,warehouse,original_day):
    _need(item.get("sku_type")=="normal" and item.get("is_gift") is not True
          and item.get("is_virtual") is not True and item.get("is_suspect") is not True,"physical_member_risk_unresolved")
    original_iid=_id(item.get("oi_id"));sku=str(item.get("sku_id") or "")
    found=warehouse.match(oid,original_iid,sku,_id(target.get("shop_id")),_company(target))
    if found["status"]=="proved":
        output=[]
        for line in found["lines"]:
            _need(line.get("document_order_type")=="普通订单","wms_goods_source_order_type_not_ordinary")
            _need(line.get("co_id")==_id(target.get("co_id")),"wms_erp_company_namespace_conflict")
            _need(line.get("is_gift") is False and line.get("source_is_gift_present") is True,"wms_nongift_basis_missing")
            _need(_created_day(line.get("created"))>=original_day,"wms_line_creation_precedes_original")
            # Dates retain their documented meanings. They are not silently
            # renamed to dispatch/cancellation timestamps.
            output.append({"identity":["wms_out_item",*line["physical_identity"]],
                "order_id":oid,"erp_member_id":original_iid,"quantity":str(_number(line["stock_quantity"])),
                "erp_item":item,"basis":"current_confirmed_inventory_document",
                "warehouse_evidence":line})
        return output
    _need("wms_document_version_conflict" not in found["reasons"],"wms_document_version_conflict")
    for document in warehouse.documents_for_order(oid):
        if not document["complete_query_scopes"]:continue
        same=[line for line in document["items"] if line["oi_id"]==original_iid]
        exact=[line for line in same if line["sku_id"]==sku]
        if exact and document["status"]!="Confirmed":
            raise MemberPending("wms_nonconfirmed_line_observation")
        if same:
            _need(any(line["sku_id"]==sku for line in same),"wms_original_member_sku_conflict")
            _need(all(line["company_id"]==_company(target) for line in same if line["sku_id"]==sku),"wms_original_member_company_conflict")
    _need(item.get("item_status")=="Sent" and item.get("is_gift") is False
          and item.get("is_combine_item") is not True and item.get("is_combine") is not True
          and bool(item.get("created")) and item.get("co_id") is not None,"physical_member_lifecycle_unproved")
    _need(_created_day(item["created"])>=original_day,"physical_member_creation_precedes_original")
    return [{"identity":["erp_order_item",oid,original_iid,sku],"order_id":oid,"erp_member_id":original_iid,
             "quantity":str(_number(item.get("qty"))),"erp_item":item,"basis":"explicit_erp_sent_member"}]


def normalize_wms_members(request: Request, capture: Capture, blobs: Mapping[str, bytes], *, period: str, warehouse: WmsDocuments) -> dict:
    """Original sale units plus independently captured inventory observations.

    ERP flags remain observations. Warehouse confirmation may prove a physical
    stock line even when its ERP sale line was subsequently cancelled; this
    does not prove no return, an exact dispatch time or financial authority.
    """
    result = {"schema": "jst_wms_source_members.v1", "status": "pending", "members": [], "rows": [], "reasons": [],
              "returns_evidence":"not_queried","proves_no_returns":False,"proves_exact_dispatch_time":False,
              "financial_authority":None,"financial_ready":False}
    try:
        _need(isinstance(request, Request) and isinstance(capture, Capture), "typed_source_request_required")
        _need(isinstance(warehouse,WmsDocuments),"typed_wms_documents_required")
        _need(isinstance(period,str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}",period),"wms_ledger_period_required")
        _need(0 < len(request.cost_anchors) <= 100 and 0 < len(request.native_anchors) <= 100, "source_anchor_budget")
        _need(0 < len(capture.nodes) <= 12 and len(capture.relations) <= 6, "source_graph_budget")
        _need(capture.parser_sha256 == PARSER_SHA256, "source_parser_identity_changed")
        _need(len(blobs) <= 240 and all(isinstance(v, bytes) for v in blobs.values())
              and sum(len(v) for v in blobs.values()) <= MAX_BLOBS, "source_blob_budget")
        for digest, raw in blobs.items():
            _need(_hash(raw) == digest, "source_blob_sha_changed")
        shops = {_id(v) for v in request.shop_ids}
        _need(shops and request.store_names, "source_shop_scope_missing")
        source_id, books = _id(request.source_order_id), {}
        def book(a):
            _need(isinstance(a, Anchor) and isinstance(a.row, int) and not isinstance(a.row, bool)
                  and 2 < a.row <= 1048576 and a.sha256 in blobs, "source_anchor_invalid")
            if a.sha256 not in books:
                books[a.sha256] = _Workbook(blobs[a.sha256])
            return books[a.sha256]
        native_days, native_children = set(), set()
        times = {"alibaba1688": "订单创建时间", "taobao": "订单创建时间", "douyin": "订单提交时间"}
        _need(request.platform in times, "native_platform_contract_pending")
        for a in request.native_anchors:
            b = book(a)
            parent = "订单号" if request.platform == "alibaba1688" else "主订单编号"
            roles = {parent, times[request.platform]}
            if request.platform != "alibaba1688":
                roles.add("子订单编号")
            # The approved Ali native template explicitly names occurrence=0:
            # its second 订单号 is a manually filled merged-cell copy.
            h = b.headers(a.sheet, roles, ("订单号",) if request.platform == "alibaba1688" else ())
            _need(parent in h and times[request.platform] in h, "native_header_contract_missing")
            _need(b.value(a.sheet, a.row, h[parent]) == request.platform_order_id, "native_parent_conflict")
            native_days.add(b.day(a.sheet, a.row, h[times[request.platform]]))
            if request.platform != "alibaba1688":
                _need("子订单编号" in h, "native_child_contract_missing")
                child = b.value(a.sheet, a.row, h["子订单编号"])
                _need(bool(child), "native_child_identity_missing")
                native_children.add(child)
        _need(len(native_days) == 1, "native_original_days_conflict")
        original_day, logical = next(iter(native_days)), {}
        _need(original_day[:7]==period,"wms_original_month_conflict")
        fields = {"oid": "内部订单号", "iid": "子订单编号", "parent": "原始线上订单号", "child": "线上子订单编号",
                  "sku": "商品编码", "qty": "数量", "day": "下单时间", "shop": "店铺名称", "type": "订单类型"}
        for a in request.cost_anchors:
            b = book(a)
            h = b.headers(a.sheet, fields.values())
            _need(all(v in h for v in fields.values()), "cost_header_contract_missing")
            row = {k: b.value(a.sheet, a.row, h[v]) for k, v in fields.items()}
            _need(row["type"] == "普通订单", "reship_original_source_separate")
            _need(row["oid"] == source_id and row["parent"] == request.platform_order_id and row["shop"] in request.store_names, "original_cost_identity_conflict")
            _need(request.platform == "alibaba1688" or row["child"] in native_children, "native_child_conflict")
            _need(b.day(a.sheet, a.row, h["下单时间"]) == original_day, "original_cost_native_day_conflict")
            c = b.cell(a.sheet, a.row, h["数量"])
            _need(c[1] == "n", "quantity_literal_missing")
            qty, key = _number(c[0]), (_id(row["iid"]), row["sku"])
            if key in logical:
                _need(logical[key]["quantity"] == qty and logical[key]["child"] == row["child"], "duplicate_original_sources_conflict")
            else:
                logical[key] = {"quantity": qty, "child": row["child"], "anchors": []}
            logical[key]["anchors"].append({"sha256": a.sha256, "sheet": a.sheet, "row": a.row, "quantity_cell": c[3]})
        nodes, refs = {}, {}
        for ref in capture.nodes:
            _need(isinstance(ref, Node) and ref.sha256 in blobs and ref.order_id not in nodes, "source_node_ambiguous")
            data, proof = parse_order(blobs[ref.sha256], ref.order_id)
            _need(_id(data.get("shop_id")) in shops, "source_cross_shop")
            _need(data.get("type") == "普通订单", "non_normal_order_source_separate")
            _company(data)
            for item in data["items"]:
                prototype=(item.get("is_combine_item") is True and not item.get("created"))
                if item.get("co_id") is None and prototype:
                    # A logical expanded definition has no physical company
                    # assertion. Never fill one from the header; actual stock
                    # identity/owner must come from the separate WMS source.
                    continue
                _need(_id(item.get("co_id")) == _id(data.get("co_id")), "member_company_namespace_conflict")
            nodes[ref.order_id], refs[ref.order_id] = data, proof
        _need(source_id in nodes, "original_source_node_missing")
        source = nodes[source_id]
        _need(_day(source.get("order_date")) == original_day, "source_original_day_conflict")
        _need(request.platform_order_id in {_platform(v, shops) for v in str(source.get("so_id") or "").split(",")}, "source_original_parent_conflict")
        related = {}
        for ref in capture.relations:
            _need(ref.order_id in nodes and ref.order_id not in related and ref.sha256 in blobs, "relation_source_ambiguous")
            related[ref.order_id] = _related(blobs[ref.sha256])
            _need(related[ref.order_id] <= set(nodes), "source_graph_incomplete")
        edges = defaultdict(set)
        for oid, node in nodes.items():
            status = str(node.get("status") or "")
            _need(status in {"Sent", "Split", "Merged"}, "source_lifecycle_pending")
            link = node.get("link_o_id")
            if link not in (None, "", 0, Decimal(0)):
                target = _id(link)
                _need(target in nodes, "typed_relation_target_missing")
                a, b = (oid, target) if status == "Merged" else (target, oid)
                _need(a != b and (b in related.get(a, set()) or a in related.get(b, set())), "typed_relation_not_observed")
                edges[a].add(b)
        visiting, visited = set(), set()
        def visit(oid):
            _need(oid not in visiting, "source_graph_cycle")
            if oid in visited:
                return
            visiting.add(oid)
            for child in sorted(edges[oid]):
                visit(child)
            visiting.remove(oid)
            visited.add(oid)
        visit(source_id)
        _need(all(oid in related for oid in visited if nodes[oid]["status"] in {"Split", "Merged"}), "source_graph_incomplete")
        terminals = {oid: nodes[oid] for oid in sorted(visited) if nodes[oid]["status"] == "Sent"}
        _need(terminals, "no_proved_terminal_shipments")
        graph_refs = [{"kind": kind, "order_id": n.order_id, "sha256": n.sha256}
                      for kind, collection in (("detail", capture.nodes), ("relations", capture.relations))
                      for n in sorted(collection, key=lambda v: v.order_id)]
        used = set()
        for (iid, sku), row in sorted(logical.items()):
            record = {"source_member_id": iid, "source_sku": sku, "logical_quantity": str(row["quantity"]), "status": "pending", "members": []}
            try:
                parents = [i for i in source["items"] if _id(i.get("oi_id")) == iid]
                _need(parents, "original_member_missing")
                scalar = len(parents) == 1 and str(parents[0].get("sku_id")) == sku
                if scalar:
                    _need(_number(parents[0].get("qty")) == row["quantity"], "original_member_quantity_changed")
                else:
                    _need(all(i.get("is_combine_item") is True and not i.get("created") and str(i.get("src_combine_sku_id")) == sku
                              and _number(i.get("src_combine_sku_qty")) == row["quantity"] for i in parents), "bundle_original_inventory_unproved")
                original_bundle_actions=[]
                if not scalar:
                    from .source_native_members import _bundle_action_inventory
                    entries=[(index,item) for index,item in enumerate(source["items"]) if item in parents]
                    original_bundle_actions=_bundle_action_inventory(source,sku,row["quantity"],entries,original_day,terminals)
                component_keys, members = set(), []
                for parent in parents:
                    _need(parent.get("sku_type") == "normal" and parent.get("is_gift") is not True and parent.get("is_virtual") is not True, "non_normal_member_separate")
                    _need(_platform(parent.get("oi_so_id"), shops) == request.platform_order_id and str(parent.get("outer_oi_id") or "") == row["child"], "original_member_platform_conflict")
                    keys = ("oi_so_id", "outer_oi_id", "raw_so_id", "outer_so_id", "raw_sku_id", "src_combine_sku_id")
                    binding = {k: str(parent[k]) for k in keys if parent.get(k) not in (None, "")}
                    _need(binding.get("oi_so_id") and binding.get("raw_so_id"), "original_member_identity_insufficient")
                    key = str(parent.get("sku_id")), tuple(sorted(binding.items()))
                    _need(key not in component_keys, "original_component_identity_ambiguous")
                    component_keys.add(key)
                    equivalent = [i for i in source["items"] if str(i.get("sku_id")) == key[0] and all(str(i.get(k, "")) == v for k, v in binding.items())]
                    _need(len(equivalent) == 1, "original_component_identity_ambiguous")
                    candidates = []
                    for oid, target in terminals.items():
                        for index, item in enumerate(target["items"]):
                            if str(item.get("sku_id")) != key[0] or any(str(item.get(k, "")) != v for k, v in binding.items()):
                                continue
                            _need(_company(target)==_company(source),"price_company_changed")
                            for observed in _physical_observations(item,target,oid,index,warehouse,original_day):
                                physical=tuple(observed["identity"])
                                _need(physical not in used,"physical_member_reused")
                                candidates.append((physical,observed,index))
                    _need(candidates and len({v[0] for v in candidates}) == len(candidates), "physical_member_missing_or_ambiguous")
                    with localcontext() as ctx:
                        ctx.prec = 256
                        total = sum((_number(v[1]["quantity"]) for v in candidates), Decimal(0))
                    _need(total == _number(parent.get("qty")), "physical_quantity_not_conserved")
                    for physical, observed, index in candidates:
                        item=observed["erp_item"]
                        stock=observed.get("warehouse_evidence")
                        if stock:
                            if stock.get("raw_so_id") not in (None,""):
                                _need(_platform(stock["raw_so_id"],shops)==request.platform_order_id,"wms_original_parent_conflict")
                            if request.platform in {"taobao","douyin"} and stock.get("outer_oi_id"):
                                _need(stock["outer_oi_id"]==row["child"],"wms_original_native_child_conflict")
                        members.append({"internal_order_id": observed["order_id"], "internal_sub_order_id": observed["erp_member_id"], "source_order_id": source_id,
                            "physical_identity":list(physical),"physical_basis":observed["basis"],"warehouse_evidence":observed.get("warehouse_evidence"),
                            "original_bundle_actions":original_bundle_actions,
                            "source_member_id": iid, "source_sku": sku, "sku": key[0], "quantity": observed["quantity"],
                            "pricing_day": original_day, "company": _company(source), "logical_quantity": str(row["quantity"]),
                            "bundle_component": not scalar, "platform_order_id": request.platform_order_id,
                            "platform_sub_order_id": row["child"], "source_anchors": row["anchors"], "shop_id": _id(source.get("shop_id")),
                            "native_anchors": [{"sha256": a.sha256, "sheet": a.sheet, "row": a.row} for a in request.native_anchors],
                            "graph_refs": graph_refs, "source_raw": refs[source_id], "physical_raw": refs[observed["order_id"]],
                            "observed_flags": {
                                "source_member": _observed(parent, ("co_id", "is_suspect", "sku_type", "is_gift", "is_virtual", "is_combine_item", "is_combine", "item_status", "created")),
                                "physical_member": _observed(item, ("co_id", "is_suspect", "sku_type", "is_gift", "is_virtual", "is_combine_item", "is_combine", "item_status", "created")),
                                "source_header": _observed(source, ("type", "status", "is_split", "is_merge", "order_date")),
                                "physical_header": _observed(nodes[observed["order_id"]], ("type", "status", "is_split", "is_merge", "order_date"))},
                            "physical_item_pointer": "/items/" + str(index)})
                _need(len({tuple(m["physical_identity"]) for m in members})==len(members),"physical_member_reused")
                used.update(tuple(m["physical_identity"]) for m in members)
                record.update(status="proved", members=members)
                result["members"].extend(members)
            except MemberPending as exc:
                record["reason"] = exc.reason
                result["reasons"].append(exc.reason)
            result["rows"].append(record)
        result["status"] = "proved" if all(r["status"] == "proved" for r in result["rows"]) else "pending"
        result.update(gross_outflow_proved=result["status"]=="proved",returns_evidence="not_queried",
                      proves_no_returns=False,proves_exact_dispatch_time=False,financial_authority=None,financial_ready=False)
    except MemberPending as exc:
        result["reasons"].append(exc.reason)
        result["members"] = []
    except (KeyError, IndexError, TypeError, ValueError, ET.ParseError, zipfile.BadZipFile, OverflowError, DecimalException):
        result["reasons"].append("source_contract_invalid")
        result["members"] = []
    return result
