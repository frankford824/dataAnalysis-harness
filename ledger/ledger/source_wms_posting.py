"""Explicit in-memory WMS cost preview inputs, with no production authority.

Original ERP observations remain in evidence. WMS document identities never
replace ERP order-item IDs. PROVIDED prices belong to the original sale SKU;
when its BOM is fully proved, a KIT total is booked once, never as invented
component unit prices. Historical prices instead value actual stock members.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json

import polars as pl
from .automation_cost_actions import Pending
from .automation_source_cost_v3 import exact_product, positive
from .engine.runtime import Ingested
from .engine.types import Recognition, ANCHOR_SHA, ANCHOR_SHEET, ANCHOR_ROW
from .full_history_intervals import FullHistoryIntervals, SCHEMA as HISTORY_SCHEMA
from .jst_source_members import _Workbook
from .model.schema import Template, ColumnBinding
from .order_lifecycle import SUPERSEDED_COST_NOTE
from .source_cost_posting import original_row, guard_return_identity, supported_metric, PRICE_FIELDS
from .source_wms_members import normalize_wms_members

TEMPLATE = "wms_source_preview_v1"
SCHEMA = "wms_cost_preview_input.v1"


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value):
    return hashlib.sha256(_encoded(value).encode()).hexdigest()


def _original_unit(member, blobs):
    values, refs = set(), []
    for anchor in member["source_anchors"]:
        raw = blobs[anchor["sha256"]]
        if hashlib.sha256(raw).hexdigest() != anchor["sha256"]:
            raise Pending("wms_original_price_bytes_changed")
        book = _Workbook(raw)
        headers = book.headers(anchor["sheet"], ("成本价",))
        if "成本价" not in headers:
            raise Pending("wms_original_price_column_missing")
        value, kind, formula, address = book.cell(anchor["sheet"], anchor["row"], headers["成本价"])
        if kind != "n" or formula:
            raise Pending("wms_original_price_literal_missing")
        values.add(positive(value))
        refs.append({**anchor, "price_cell": address, "raw_unit_cost": value})
    if len(values) != 1:
        raise Pending("wms_original_price_conflict")
    return str(next(iter(values))), refs


def _existing_authority(baseline, store, period, target):
    sl = baseline.slices.get((store.name, period))
    if sl is None:
        raise Pending("wms_preview_baseline_missing")
    relevant = sl.facts.filter((pl.col("metric_id") == "goods_cost") & pl.col("counted")
                              & (pl.col("link_key") == str(target["coverage_key"])))
    if relevant.height:
        notes = relevant["source_note"].to_list() if "source_note" in relevant.columns else [""] * relevant.height
        if any(SUPERSEDED_COST_NOTE not in str(note or "") for note in notes):
            raise Pending("wms_existing_cost_authority_preserved")
    return sl


def render_wms_preview_items(ingestion, baseline, store, period, target, request, capture, blobs,
                             *, warehouse, history_by_sku=None, input_version):
    """Return ``(items, original_anchors, stock_identities, proof_with_prices)``.

    This renderer creates no run, Workspace mutation, grant, payout or close.
    A future production caller must separately enforce its authority, frozen
    state, whole-period manual inputs and current source CAS. Known target cost
    authority, including zero/existing partial costs, is retained here.
    """
    if not isinstance(input_version, str) or not input_version.strip() or len(input_version) > 128:
        raise Pending("wms_preview_input_version_required")
    if request.platform != store.platform or set(request.store_names) != set((store.name, *store.aliases)):
        raise Pending("wms_preview_store_contract_conflict")
    if str(target.get("order_id")) != request.platform_order_id:
        raise Pending("wms_preview_native_parent_conflict")
    child = str(target.get("sub_order_id") or "")
    if store.platform in {"taobao", "douyin"} and (not child or str(target.get("coverage_key")) != child):
        raise Pending("wms_preview_native_child_required")
    if store.platform == "alibaba1688" and str(target.get("coverage_key")) != request.platform_order_id:
        raise Pending("wms_preview_parent_grain_required")
    proof = normalize_wms_members(request, capture, blobs, period=period, warehouse=warehouse)
    if proof["status"] != "proved":
        raise Pending("wms_source_pending:" + ",".join(proof["reasons"]))
    members = [m for m in proof["members"] if m["platform_order_id"] == request.platform_order_id
               and (not child or m["platform_sub_order_id"] == child)]
    if not members:
        raise Pending("wms_preview_members_missing")
    _existing_authority(baseline, store, period, target)
    metric = supported_metric(ingestion.model, store)
    guard_return_identity(ingestion, store, members)
    for member in members:
        stock = member.get("warehouse_evidence")
        if stock and stock.get("document_order_type") != "普通订单":
            raise Pending("wms_goods_requires_ordinary_document_type")
    identities = {tuple(m["physical_identity"]) for m in members}
    if len(identities) != len(members):
        raise Pending("wms_preview_physical_identity_reused")
    mode = next((p.cost_pricing for p in ingestion.model.platforms if p.id == store.platform), None)
    prices, financial = [], []
    if mode == "provided":
        groups = defaultdict(list)
        for member in members:
            groups[(member["source_order_id"], member["source_member_id"], member["source_sku"])].append(member)
        for group, stock_members in sorted(groups.items()):
            member = stock_members[0]
            quantities = {m["logical_quantity"] for m in stock_members}
            if len(quantities) != 1:
                raise Pending("wms_provided_sale_quantity_conflict")
            unit, refs = _original_unit(member, blobs)
            if any(m["bundle_component"] for m in stock_members) and any(not m.get("original_bundle_actions") for m in stock_members):
                raise Pending("wms_provided_original_bundle_inventory_missing")
            price = {"schema": "wms-provided-original-sale-unit.v1", "sale_group": list(group),
                     "sku": member["source_sku"], "quantity": member["logical_quantity"], "unit_cost": unit,
                     "price_anchors": refs, "book_order_date": member["pricing_day"],
                     "price_effective_period_proved": False, "component_unit_prices_proved": False}
            prices.append(price)
            financial.append((member, stock_members, member["source_sku"], member["logical_quantity"], unit,
                              "provided_original_sale_group", price))
    elif mode in {"historical", "required"}:
        for member in members:
            reader = (history_by_sku or {}).get(member["sku"])
            if type(reader) is not FullHistoryIntervals:
                raise Pending("wms_typed_full_history_capture_required")
            quote = reader.quote(member["sku"], member["company"], member["pricing_day"])
            if quote.get("schema") != HISTORY_SCHEMA:
                raise Pending("wms_full_history_source_contract_required")
            prices.append(quote)
            financial.append((member, [member], member["sku"], member["quantity"], quote["unit_cost"],
                              "historical_stock_member", quote))
    else:
        raise Pending("wms_preview_platform_price_contract_unsupported")
    groups, anchors, erp_stock_keys = {}, set(), set()
    for member, stock_members, sku, quantity, unit, grain, price in financial:
        original_item, original = original_row(ingestion, member, metric.source)
        stamp = original.get("order_date") or original.get("order_time")
        stamp = stamp.date() if isinstance(stamp, datetime) else stamp if isinstance(stamp, date) else None
        if stamp is None or stamp.isoformat() != member["pricing_day"] or stamp.strftime("%Y-%m") != period:
            raise Pending("wms_original_book_date_unverified")
        amount = exact_product(quantity, unit)
        row = {key: value for key, value in original.items() if key not in PRICE_FIELDS}
        warehouse_only = all(m.get("warehouse_evidence") for m in stock_members)
        derived_state = "Confirmed" if warehouse_only else "proved_fulfillment"
        observations = [{"source_order_id": m["source_order_id"], "erp_order_id": m["internal_order_id"],
                         "erp_item_id": m["internal_sub_order_id"], "physical_identity": m["physical_identity"],
                         "observed_flags": m.get("observed_flags", {}), "warehouse_evidence": m.get("warehouse_evidence")}
                        for m in stock_members]
        evidence = {"schema": SCHEMA, "input_version": input_version, "financial_grain": grain,
                    "order_date": member["pricing_day"], "cost_company_id": member["company"],
                    "source_proof_sha256": _digest(proof), "price": price,
                    "observations": observations, "original_erp_state": original.get("order_state"),
                    "derived_state_kind": "proved_fulfillment_input", "returns_evidence": "not_queried",
                    "financial_authority": None, "exact_amount": str(amount)}
        row.update(order_id=request.platform_order_id, original_order_id=request.platform_order_id,
                   sub_order_id=child or member["source_member_id"],
                   internal_order_id=member["source_order_id"] if mode == "provided" else member["internal_order_id"],
                   internal_sub_order_id=member["source_member_id"] if mode == "provided" else member["internal_sub_order_id"],
                   sku=sku, quantity=float(positive(quantity)), unit_cost=float(positive(unit)), total_cost=float(amount),
                   cost_company_id=member["company"], order_state=derived_state,
                   source_original_erp_state=original.get("order_state"), source_wms_financial_grain=grain,
                   cost_source="provided_original" if mode == "provided" else "history", cost_status="priced",
                   cost_as_of=None if mode == "provided" else member["pricing_day"],
                   source_pricing_day=None if mode == "provided" else member["pricing_day"],
                   pricing_evidence=_encoded(evidence),
                   source_note="WMS及原销售单位独立对照；隔离输入版本 " + input_version)
        key = (original_item.ref.sha256, original_item.ref.sheet)
        groups.setdefault(key, {"ref": original_item.ref, "rows": []})["rows"].append(row)
        for m in stock_members:
            anchors.update((a["sha256"], a["sheet"], int(a["row"])) for a in m["source_anchors"])
            erp_stock_keys.add((m["internal_order_id"], m["internal_sub_order_id"], m["sku"]))
    items = []
    template = Template(id=TEMPLATE, name="WMS原始履约与成本独立试算", source=metric.source,
        match_columns=("__wms_source_preview__",), time_slots={"order_date": "order_time"},
        bindings=tuple(ColumnBinding(role=role, columns=(role,)) for role in
            ("order_id", "sub_order_id", "original_order_id", "internal_order_id", "internal_sub_order_id",
             "sku", "quantity", "unit_cost", "order_time", "order_state", "order_type", "store_name")))
    for _, value in sorted(groups.items()):
        frame = pl.from_dicts(value["rows"], infer_schema_length=None, strict=False)
        ref = value["ref"]
        items.append(Ingested(ref=ref, frame=frame, rows=frame.height, template=template,
            recognition=Recognition(ref=ref, signature=template.signature, header_count=len(frame.columns),
                                    source_id=metric.source, template_id=template.id)))
    return items, anchors, identities, {**proof, "prices": prices,
        "erp_stock_removal_keys": sorted(erp_stock_keys), "input_version": input_version,
        "financial_grains": sorted({entry[5] for entry in financial}), "financial_authority": None}


def replace_wms_preview_inputs(ingestion, items, anchors, erp_stock_keys):
    """Replace proved inputs in memory; never confuse WMS IDs with ERP IDs."""
    from dataclasses import replace
    output = []
    stock = set(map(tuple, erp_stock_keys))
    for item in ingestion.items:
        if not item.template or item.template.source != "order_cost" or item.frame is None:
            output.append(item)
            continue
        keep = []
        for row in item.frame.to_dicts():
            anchor = (str(row[ANCHOR_SHA]), str(row[ANCHOR_SHEET]), int(row[ANCHOR_ROW]))
            erp = (str(row.get("internal_order_id")), str(row.get("internal_sub_order_id")), str(row.get("sku")))
            keep.append(anchor not in anchors and erp not in stock)
        frame = item.frame.filter(pl.Series(keep, dtype=pl.Boolean))
        output.append(replace(item, frame=frame, rows=frame.height))
    ingestion.items = [*output, *items]
