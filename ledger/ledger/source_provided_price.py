"""Read-only price proofs for an original PROVIDED cost export.

A superseded container cannot prove shipment, but its same-SKU literal unit
price can remain original price evidence after actual members are proved.
This separate preview contract never creates V3 history quotes or writes facts.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, localcontext
import hashlib

from .jst_source_members import MemberPending, _Workbook, normalize_members
from .automation_source_cost_v3 import positive, exact_product
from .automation_cost_actions import Pending

SCHEMA = "provided_original_cost_price.v1"


def prove_original_prices(request, capture, blobs, *, pricing_mode):
    """Re-read original XML and source graph; return complete proof or pending.

    Callers obtain pricing_mode from the loaded platform contract. Outputs are
    preview evidence only: no consumer may pass them off as history intervals.
    Different physical members keep distinct price anchors, even for one SKU.
    """
    result = {"schema": SCHEMA, "status": "pending", "prices": [],
              "reasons": [], "financial_applied": False}
    try:
        if pricing_mode != "provided":
            raise MemberPending("provided_pricing_mode_required")
        proof = normalize_members(request, capture, blobs)
        if proof["status"] != "proved":
            result["reasons"] = ["physical_member_proof_incomplete", *proof["reasons"]]
            return result
        books = {}
        groups = defaultdict(list)
        prices = []
        physical = set()
        for member in proof["members"]:
            key = (member["internal_order_id"], member["internal_sub_order_id"])
            if key in physical:
                raise MemberPending("provided_physical_member_duplicate")
            physical.add(key)
            if member["bundle_component"] or member["source_sku"] != member["sku"]:
                raise MemberPending("provided_component_price_not_original_sku")
            values, anchors = set(), []
            for anchor in member["source_anchors"]:
                digest = anchor["sha256"]
                raw = blobs[digest]
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise MemberPending("source_blob_sha_changed")
                if digest not in books:
                    books[digest] = _Workbook(raw)
                book = books[digest]
                header = book.headers(anchor["sheet"], ("成本价",))
                if "成本价" not in header:
                    raise MemberPending("provided_original_price_column_missing")
                cell = book.cell(anchor["sheet"], anchor["row"], header["成本价"])
                if cell[1] != "n":
                    raise MemberPending("provided_original_price_literal_missing")
                value = positive(cell[0])
                values.add(value)
                anchors.append({**anchor, "unit_price_cell": cell[3],
                                "raw_unit_cost": cell[0]})
            if len(values) != 1:
                raise MemberPending("provided_original_prices_conflict")
            unit = next(iter(values))
            amount = exact_product(member["quantity"], str(unit))
            group = (member["source_order_id"], member["source_member_id"], member["source_sku"])
            groups[group].append((member, unit, amount))
            prices.append({"internal_order_id": key[0], "internal_sub_order_id": key[1],
                           "source_order_id": group[0], "source_member_id": group[1],
                           "sku": member["sku"], "company": member["company"],
                           "pricing_day": member["pricing_day"],
                           "quantity": member["quantity"], "unit_cost": str(unit),
                           "amount": str(amount), "original_price_anchors": anchors})
        with localcontext() as context:
            context.prec = 256
            for entries in groups.values():
                units = {unit for _, unit, _ in entries}
                logical = {positive(m["logical_quantity"]) for m, _, _ in entries}
                if len(units) != 1 or len(logical) != 1:
                    raise MemberPending("provided_original_group_conflict")
                if sum((positive(m["quantity"]) for m, _, _ in entries), Decimal(0)) != next(iter(logical)):
                    raise MemberPending("provided_original_quantity_not_conserved")
                if sum((amount for _, _, amount in entries), Decimal(0)) != next(iter(units)) * next(iter(logical)):
                    raise MemberPending("provided_original_amount_not_conserved")
        result.update(status="proved", prices=prices,
                      physical_member_count=len(prices), source_member_count=len(groups), source_proof=proof)
    except (MemberPending, Pending) as error:
        result["reasons"] = [getattr(error, "reason", str(error))]
    return result


def render_original_price_items(ingestion, baseline, store, period, target, request, capture, blobs, *, input_version):
    """Create explicit in-memory preview inputs for the unchanged engine.

    This has no Workspace, service, persisted grant, or production entry point.
    Raw original files remain unchanged; only the caller's isolated Ingestion
    may adopt the returned inputs with source_cost_posting.replace_inputs.
    """
    from datetime import date, datetime
    import json
    import polars as pl
    from . import source_cost_posting as posting
    from .engine.runtime import Ingested
    from .engine.types import Recognition, ANCHOR_SHA, ANCHOR_SHEET, ANCHOR_ROW
    from .model.schema import Template, ColumnBinding

    if not isinstance(input_version, str) or not input_version.strip() or len(input_version)>128:
        raise Pending("provided_explicit_input_version_required")
    platform=next((p for p in ingestion.model.platforms if p.id==store.platform),None)
    mode=platform.cost_pricing if platform else None
    if (request.platform!=store.platform
        or set(request.store_names)!=set((store.name,*store.aliases))):
        raise Pending("provided_source_store_scope_conflict")
    proof=prove_original_prices(request,capture,blobs,pricing_mode=mode)
    if proof["status"]!="proved":
        raise Pending("provided_source_pending:"+",".join(proof["reasons"]))
    if str(target.get("order_id"))!=request.platform_order_id or str(target.get("coverage_key"))!=request.platform_order_id:
        raise Pending("provided_native_target_conflict")
    # This adapter deliberately remains limited to the proven parent-grain
    # PROVIDED platform. Child-grain costs need their own native target proof.
    if store.platform!="alibaba1688":
        raise Pending("provided_parent_grain_contract_required")
    metric=posting.supported_metric(ingestion.model,store)
    members=proof["source_proof"]["members"]
    posting.guard_return_identity(ingestion,store,members)
    owned=posting.owned_members(ingestion,baseline,store,period,target,members,metric)
    by_physical={(p["internal_order_id"],p["internal_sub_order_id"]):p for p in proof["prices"]}
    groups={}; anchors=set(); physical=set()
    for member in members:
        key=posting.physical_key(member)
        if key in owned:continue
        price=by_physical[key]
        item,original=posting.original_row(ingestion,member,metric.source)
        row={k:v for k,v in original.items() if k not in posting.PRICE_FIELDS}
        stamp=row.get("order_date") or row.get("order_time")
        day=stamp.date() if isinstance(stamp,datetime) else stamp if isinstance(stamp,date) else None
        if day is None or day.isoformat()!=price["pricing_day"] or price["pricing_day"][:7]!=period:
            raise Pending("provided_original_book_date_unverified")
        evidence={"schema":SCHEMA,"input_version":input_version,"source_price":price,
                  "source_proof_sha256":hashlib.sha256(json.dumps(proof["source_proof"],sort_keys=True,ensure_ascii=False).encode()).hexdigest()}
        row.update(order_id=member["platform_order_id"],original_order_id=member["platform_order_id"],
            sub_order_id=member["platform_sub_order_id"] or member["internal_sub_order_id"],
            internal_order_id=member["internal_order_id"],internal_sub_order_id=member["internal_sub_order_id"],
            parent_internal_order_id=member["source_order_id"],sku=member["sku"],
            quantity=float(positive(price["quantity"])),unit_cost=float(positive(price["unit_cost"])),
            total_cost=float(Decimal(price["amount"])),cost_company_id=member["company"],
            order_state="Sent",cost_source="provided_original",cost_status="priced",cost_as_of=price["pricing_day"],
            source_pricing_day=price["pricing_day"],pricing_evidence=json.dumps(evidence,sort_keys=True,ensure_ascii=False),
            source_note="原件同SKU字面成本价及实际发货成员已核验；隔离输入版本 "+input_version,
            pricing_missing=False,pricing_suspect=False,pricing_identity_changed=False,pricing_source_suspect=False)
        group=(item.ref.sha256,str(row[ANCHOR_SHEET]))
        groups.setdefault(group,{"ref":item.ref,"rows":[]})["rows"].append(row)
        anchors.update((a["sha256"],a["sheet"],a["row"]) for a in member["source_anchors"])
        physical.add(key)
    result=[]
    for _,value in sorted(groups.items()):
        frame=pl.from_dicts(value["rows"],infer_schema_length=None,strict=False)
        template=Template(id="provided_original_source_preview_v1",name="原件成本价与实际成员隔离对照",source=metric.source,
            match_columns=("__provided_original_source_preview__",),time_slots={"order_date":"order_time"},
            bindings=tuple(ColumnBinding(role=role,columns=(role,)) for role in
                ("order_id","sub_order_id","original_order_id","internal_order_id","internal_sub_order_id",
                 "sku","quantity","unit_cost","order_time","order_state","order_type","store_name")))
        ref=value["ref"]
        result.append(Ingested(ref=ref,template=template,frame=frame,rows=frame.height,
            recognition=Recognition(ref=ref,signature=template.signature,header_count=len(frame.columns),
                                    source_id=metric.source,template_id=template.id),
            notes=["隔离新输入对照；原件、既有run与财务权限不变"]))
    return result,anchors,physical
