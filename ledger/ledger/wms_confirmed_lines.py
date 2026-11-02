"""Pure, bounded WMS inventory observations; no ERP status or financial grant.

ERP order/item IDs and warehouse document/line IDs remain separate. Shipment
and cancellation dates, returns and original sales quantities require their
own evidence. An io_date or generic modified timestamp establishes none of
those conclusions by itself.
"""
from __future__ import annotations

import base64
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re

SCHEMA = "ledger-wms-confirmed-lines.v1"
STATUSES = frozenset({"WaitConfirm", "Confirmed", "Delete", "Cancelled", "OuterConfirming", "Archive"})
PATH = "/open/orders/out/simple/query"


class WmsSourceError(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _need(ok, reason):
    if not ok:
        raise WmsSourceError(reason)


def _id(value):
    _need(not isinstance(value, (bool, float)), "wms_identity_literal_invalid")
    value = str(value or "")
    _need(bool(re.fullmatch(r"[1-9][0-9]{0,23}", value)), "wms_identity_literal_invalid")
    return value


def _number(value, *, positive=False):
    _need(not isinstance(value, (bool, float)) and value is not None, "wms_quantity_literal_invalid")
    try:
        out = Decimal(str(value))
    except InvalidOperation:
        raise WmsSourceError("wms_quantity_literal_invalid") from None
    _need(out.is_finite() and len(out.as_tuple().digits) <= 100
          and abs(out.as_tuple().exponent) <= 100 and (not positive or out > 0), "wms_quantity_literal_invalid")
    return str(out)


def _meta_literal(value):
    if value is None:
        return None
    _need(not isinstance(value, bool) and isinstance(value, (str, int, Decimal)), "wms_meta_literal_invalid")
    return str(value)


def _pairs(values):
    result = {}
    for k, v in values:
        _need(k not in result, "wms_duplicate_json_key")
        result[k] = v
    return result


def _json(raw):
    try:
        return json.loads(raw, parse_int=Decimal, parse_float=Decimal, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(WmsSourceError("wms_nonfinite_json")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, WmsSourceError):
            raise
        raise WmsSourceError("wms_source_json_invalid") from None


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()


def _extra(row, lower, upper):
    fields = [row[k] for k in (lower, upper) if k in row]
    _need(fields and all(isinstance(x, dict) for x in fields), "wms_extra_fields_missing")
    _need(len({_encoded(x) for x in fields}) == 1, "wms_extra_field_alias_conflict")
    return fields[0]


class WmsDocuments:
    """Validate captured read-only queries and retain all current/archived scopes.

    SHA validation proves bytes, not source-provider authority. The caller
    separately binds trusted collection and original native sales evidence.
    """

    def __init__(self, captures):
        _need(isinstance(captures, (list, tuple)) and 0 < len(captures) <= 192, "wms_capture_budget")
        self._documents = {}
        self._conflicts = set()
        self._pages = defaultdict(dict)
        self._scopes = {}
        total_bytes = 0
        for raw, envelope in captures:
            envelope = deepcopy(envelope)
            _need(isinstance(raw, bytes) and len(raw) <= 16 * 1024 * 1024, "wms_response_budget")
            total_bytes += len(raw)
            _need(total_bytes <= 128 * 1024 * 1024, "wms_total_response_budget")
            digest = hashlib.sha256(raw).hexdigest()
            _need(isinstance(envelope, dict) and envelope.get("http_status") == 200
                  and envelope.get("raw_sha256") == digest, "wms_capture_identity_invalid")
            if "raw_b64" in envelope:
                text = envelope["raw_b64"]
                _need(isinstance(text, str) and len(text) <= 4 * ((16 * 1024 * 1024 + 2) // 3), "wms_capture_encoding_invalid")
                try:
                    _need(base64.b64decode(text, validate=True) == raw, "wms_capture_bytes_changed")
                except (ValueError, TypeError):
                    raise WmsSourceError("wms_capture_encoding_invalid") from None
            captured = envelope.get("captured_at")
            _need(not isinstance(captured, bool) and isinstance(captured, (float, int))
                  and math.isfinite(captured) and captured > 0, "wms_capture_time_invalid")
            request = envelope.get("request")
            _need(isinstance(request, dict) and request.get("method") == "POST"
                  and request.get("path") == PATH, "wms_readonly_query_path_invalid")
            body = request.get("body")
            allowed = {"shop_id", "o_ids", "status", "archive", "page_index", "page_size", "is_get_total",
                       "is_get_cbfinance", "is_get_cblogistic", "inout_flds", "inout_item_flds"}
            _need(isinstance(body, dict) and set(body) <= allowed, "wms_query_contract_invalid")
            shop = _id(body.get("shop_id"))
            ids = body.get("o_ids")
            _need(isinstance(ids, list) and 0 < len(ids) <= 50, "wms_query_identity_budget")
            ids = tuple(_id(x) for x in ids)
            _need(len(ids) == len(set(ids)), "wms_query_duplicate_identity")
            status, archive = body.get("status"), body.get("archive")
            _need(isinstance(status, str) and status in STATUSES and type(archive) is bool, "wms_query_status_scope_invalid")
            page, size = body.get("page_index"), body.get("page_size")
            _need(type(page) is int and 1 <= page <= 4 and type(size) is int and 1 <= size <= 50,
                  "wms_query_page_budget")
            _need(body.get("is_get_total") is True and body.get("is_get_cbfinance") is False
                  and body.get("is_get_cblogistic") is False, "wms_query_expansion_invalid")
            extras_allowed = {"inout_flds": {"owner_co_id", "is_split", "is_merge"},
                              "inout_item_flds": {"co_id", "io_id", "created", "modified", "qty_type", "oi_id",
                                                  "oi_id_qty", "combine_sku_qty", "per_unit", "sales_unit"}}
            for field, choices in extras_allowed.items():
                if field in body:
                    requested_fields = body[field]
                    _need(isinstance(requested_fields, list) and len(requested_fields) <= 16
                          and all(isinstance(value, str) and value in choices for value in requested_fields)
                          and len(set(requested_fields)) == len(requested_fields), "wms_query_extra_fields_invalid")
            scope = (shop, tuple(sorted(ids)), status, archive)
            self._scopes[scope] = (shop, ids, status, archive)
            root = _json(raw)
            _need(isinstance(root, dict) and not isinstance(root.get("code"), bool)
                  and root.get("code") in (0, "0") and isinstance(root.get("data"), dict), "wms_source_contract_invalid")
            data = root["data"]
            for field in ("page_index", "page_size", "data_count", "page_count"):
                value = Decimal(_number(data.get(field)))
                _need(value >= 0 and value == value.to_integral_value(), "wms_pagination_integer_invalid")
            _need(type(data.get("has_next")) is bool and data.get("page_index") == page
                  and data.get("page_size") == size and isinstance(data.get("datas"), list), "wms_pagination_contract_invalid")
            docs = data["datas"]
            _need(len(docs) <= size and data.get("data_count") is not None and data.get("page_count") is not None,
                  "wms_pagination_count_invalid")
            count, count_pages = int(data["data_count"]), int(data["page_count"])
            _need(count >= len(docs) and count_pages == (count + size - 1) // size,
                  "wms_pagination_count_invalid")
            page_fingerprint = hashlib.sha256(_encoded({"docs": docs, "has_next": data["has_next"]})).hexdigest()
            old = self._pages[scope].get(page)
            _need(old is None or old == (data["has_next"], page_fingerprint, count, count_pages, len(docs)), "wms_same_page_conflict")
            self._pages[scope][page] = (data["has_next"], page_fingerprint, count, count_pages, len(docs))
            for position, doc in enumerate(docs):
                _need(isinstance(doc, dict) and _id(doc.get("o_id")) in ids
                      and _id(doc.get("shop_id")) == shop, "wms_response_scope_conflict")
                _need(doc.get("status") == status, "wms_response_status_scope_conflict")
                co, io = _id(doc.get("co_id")), _id(doc.get("io_id"))
                extra = _extra(doc, "inout_flds", "InoutFlds")
                own = doc.get("owner_co_id")
                if "owner_co_id" in extra:
                    _need(own is None or str(own) == str(extra["owner_co_id"]), "wms_document_owner_conflict")
                    own = extra["owner_co_id"]
                owner = co if own is None or str(own) in ("", "0") else _id(own)
                _need(isinstance(doc.get("items"), list), "wms_document_items_missing")
                lines, seen = [], set()
                for item_index, item in enumerate(doc["items"]):
                    _need(isinstance(item, dict), "wms_line_contract_invalid")
                    line = _extra(item, "inout_item_flds", "InoutItemFlds")
                    oi, ioi = _id(item.get("oi_id")), _id(item.get("ioi_id"))
                    sku = item.get("sku_id")
                    _need(isinstance(sku, str) and sku and len(sku) <= 256, "wms_line_sku_invalid")
                    _need(_id(line.get("co_id")) == co and _id(line.get("io_id")) == io
                          and _id(line.get("oi_id")) == oi, "wms_line_nested_identity_conflict")
                    identity = (co, io, ioi, sku)
                    _need((ioi, sku) not in seen, "wms_document_duplicate_line")
                    seen.add((ioi, sku))
                    qty = _number(item.get("qty"), positive=status == "Confirmed")
                    for name in ("combine_sku_qty", "per_unit"):
                        if name in line and name in item and item[name] is not None and line[name] is not None:
                            _need(_number(item[name]) == _number(line[name]), "wms_line_nested_unit_conflict")
                    optional = {name: (_number(line[name]) if line.get(name) is not None else None)
                                for name in ("oi_id_qty", "combine_sku_qty", "per_unit")}
                    source_ref = {"source_response_sha": digest, "source_pointer": f"/data/datas/{position}/items/{item_index}",
                                  "source_request": {"path": PATH, "body": body}, "captured_at": envelope.get("captured_at")}
                    lines.append({"physical_identity": list(identity), "co_id": co, "company_id": owner,
                        "shop_id": shop, "o_id": _id(doc["o_id"]), "oi_id": oi, "io_id": io, "ioi_id": ioi,
                        "sku_id": sku, "qty": qty, "stock_quantity": qty, "document_status": status,
                        "outer_oi_id": str(item.get("outer_oi_id") or ""), "raw_so_id": item.get("raw_so_id"),
                        "combine_sku_id": item.get("combine_sku_id"), "created": line.get("created"),
                        "modified": line.get("modified"), "doc_created": doc.get("created"), "doc_modified": doc.get("modified"),
                        "io_date": doc.get("io_date"), "qty_type": _meta_literal(line.get("qty_type")), "sales_unit": line.get("sales_unit"),
                        "is_gift": item.get("is_gift"), "unit": item.get("unit"), "i_id": _meta_literal(item.get("i_id")),
                        "document_order_type": doc.get("order_type"),
                        "source_is_gift_present": "is_gift" in item,
                        "source_optional_field_presence": {name: name in line for name in
                            ("created", "modified", "oi_id_qty", "combine_sku_qty", "per_unit", "sales_unit")},
                        **optional, "source_refs": [source_ref], "financial_authority": None})
                normalized = {"co_id": co, "company_id": owner, "o_id": _id(doc["o_id"]), "io_id": io,
                              "shop_id": shop, "status": status, "document_order_type": doc.get("order_type"),
                              "items": sorted(lines, key=lambda x: tuple(x["physical_identity"]))}
                semantic = _encoded({**normalized, "items": [{k: v for k, v in x.items() if k != "source_refs"} for x in normalized["items"]]})
                key = (co, io)
                existing = self._documents.get(key)
                if existing is not None:
                    if existing[0] != semantic:
                        self._conflicts.add(key)
                    else:
                        for old_line, new_line in zip(existing[1]["items"], normalized["items"]):
                            old_line["source_refs"].extend(new_line["source_refs"])
                else:
                    self._documents[key] = (semantic, normalized)

    def _complete(self, order, shop):
        seen = set()
        for scope, pages in self._pages.items():
            source_shop, ids, status, archive = self._scopes[scope]
            if source_shop != shop or order not in ids:
                continue
            numbers = sorted(pages)
            complete = numbers and numbers == list(range(1, numbers[-1] + 1))
            complete = complete and all(pages[n][0] is True for n in numbers[:-1]) and pages[numbers[-1]][0] is False
            if complete:
                counts = {pages[n][2] for n in numbers}
                declared_pages = {pages[n][3] for n in numbers}
                complete = len(counts) == len(declared_pages) == 1
                complete = complete and sum(pages[n][4] for n in numbers) == pages[numbers[0]][2]
                complete = complete and (numbers[-1] == pages[numbers[0]][3] or pages[numbers[0]][2] == 0)
            if complete:
                seen.add((status, archive))
        return seen == {(status, archive) for status in STATUSES for archive in (False, True)}

    def documents_for_order(self, o_id):
        """All observations, including unconfirmed/cancelled; never money facts."""
        order = _id(o_id)
        return [deepcopy({**doc, "observation_conflict": key in self._conflicts,
                          "complete_query_scopes": self._complete(order, doc["shop_id"])})
                for key, (_, doc) in self._documents.items() if doc["o_id"] == order]

    def match(self, o_id, oi_id, sku, shop_id, company_id):
        order, original, shop, owner = map(_id, (o_id, oi_id, shop_id, company_id))
        result = {"schema": SCHEMA, "status": "pending", "lines": [], "reasons": [],
                  "financial_authority": None, "proves_no_returns": False, "proves_exact_dispatch_time": False}
        if not self._complete(order, shop):
            result["reasons"].append("wms_query_scopes_incomplete")
            return result
        for key, (_, doc) in self._documents.items():
            if doc["o_id"] != order or doc["shop_id"] != shop:
                continue
            if key in self._conflicts:
                result["reasons"].append("wms_document_version_conflict")
                return result
            if doc["status"] != "Confirmed" or doc["company_id"] != owner:
                continue
            result["lines"].extend(deepcopy(line) for line in doc["items"] if line["oi_id"] == original and line["sku_id"] == sku)
        if result["lines"]:
            result["status"] = "proved"
        else:
            result["reasons"].append("wms_exact_confirmed_line_not_proved")
        return result
