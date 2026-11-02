"""Select dated quotes from retained, complete OpenWeb history responses.

This is a pure evidence adapter. It performs no I/O, retains the actual all-
history request, and confers no financial grant. In particular its output is
not an HTTP response to an ``as_of`` query and cannot use that source schema.
"""
from __future__ import annotations

import base64
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re

from .upstream_history import HistoryConflict, HistoryUnavailable

SCHEMA = "ledger-openweb-full-history-quote.v1"
MAX_RAW_BYTES = 16 * 1024 * 1024
MAX_ROWS = 200_000


def _fail(reason, conflict=False):
    raise (HistoryConflict if conflict else HistoryUnavailable)(reason)


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            _fail("full_history_duplicate_json_key")
        result[key] = value
    return result


def _bad_constant(_):
    _fail("full_history_nonfinite_json")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


class FullHistoryIntervals:
    """Validate one captured request, then reuse its real intervals by day.

    The caller must establish the capture provider's authority separately.
    A SHA proves byte identity, not who supplied it. No environment switches,
    file paths or production action consumers are installed by this class.
    """

    def __init__(self, raw: bytes, envelope: dict):
        if not isinstance(raw, bytes) or len(raw) > MAX_RAW_BYTES:
            _fail("full_history_response_budget")
        if not isinstance(envelope, dict) or envelope.get("http_status") != 200:
            _fail("full_history_capture_http_invalid")
        if envelope.get("raw_sha256") != _sha(raw):
            _fail("full_history_capture_sha_changed")
        if "raw_b64" in envelope:
            if not isinstance(envelope["raw_b64"], str) or len(envelope["raw_b64"]) > 4 * ((MAX_RAW_BYTES + 2) // 3):
                _fail("full_history_capture_encoding_invalid")
            try:
                retained = base64.b64decode(envelope["raw_b64"], validate=True)
            except (ValueError, TypeError):
                _fail("full_history_capture_encoding_invalid")
            if retained != raw:
                _fail("full_history_capture_bytes_changed")
        request = envelope.get("request")
        if (not isinstance(request, dict)
                or set(request) != {"sku_ids", "get_way", "is_use_item_sku_cost_price"}
                or request.get("get_way") != "all"
                or request.get("is_use_item_sku_cost_price") is not False):
            _fail("full_history_request_scope_invalid")
        skus = request.get("sku_ids")
        if (not isinstance(skus, list) or not 0 < len(skus) <= 10
                or any(not isinstance(sku, str) or not sku or len(sku) > 256
                       or sku != sku.strip() or any(c in sku for c in ",\r\n\x00") for sku in skus)
                or len(set(skus)) != len(skus)):
            _fail("full_history_request_identity_invalid")
        captured = envelope.get("captured_at")
        if (isinstance(captured, bool) or not isinstance(captured, (float, int))
                or not math.isfinite(captured) or captured <= 0):
            _fail("full_history_capture_time_invalid")
        try:
            body = json.loads(raw, parse_int=Decimal, parse_float=Decimal,
                              object_pairs_hook=_pairs, parse_constant=_bad_constant)
        except (ValueError, UnicodeError, RecursionError):
            _fail("full_history_source_json_invalid")
        if (not isinstance(body, dict) or isinstance(body.get("code"), bool)
                or body.get("code") not in (0, "0") or not isinstance(body.get("data"), dict)):
            _fail("full_history_source_contract_invalid")
        maps = body["data"].get("sku_history_cost_price_maps")
        if not isinstance(maps, dict):
            _fail("full_history_source_map_invalid")
        rows, count = defaultdict(list), 0
        for company, periods in maps.items():
            if not re.fullmatch(r"[1-9][0-9]{0,18}", company) or not isinstance(periods, list):
                _fail("full_history_company_contract_invalid")
            for index, row in enumerate(periods):
                count += 1
                if count > MAX_ROWS:
                    _fail("full_history_period_budget")
                if not isinstance(row, dict) or row.get("sku_id") not in skus:
                    _fail("full_history_unrequested_sku")
                rows[(company, row["sku_id"])].append((index, {key: row.get(key)
                    for key in ("begin_date", "end_date", "cost_price")}))
        self._rows = dict(rows)
        self._skus = tuple(skus)
        self._captured_at = captured
        self.raw_sha256 = _sha(raw)
        self.raw_bytes = len(raw)
        self.period_count = count

    def quote(self, sku: str, company: str, pricing_day: str) -> dict:
        """A derived selection with its original all-history source identity."""
        if (not isinstance(sku, str) or sku not in self._skus
                or not isinstance(company, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", company)
                or _day(pricing_day) is None):
            _fail("full_history_query_identity_invalid")
        intervals = []
        for index, row in self._rows.get((company, sku), ()):
            begin_raw, end_raw = row.get("begin_date"), row.get("end_date")
            unbounded = begin_raw == "开始"
            begin = "0001-01-01" if unbounded else _day(begin_raw)
            end = None if end_raw in (None, "") else _day(end_raw)
            if begin is None or (end_raw not in (None, "") and end is None):
                _fail("full_history_interval_boundary_unknown")
            if end is not None and end < begin:
                _fail("full_history_interval_reversed", conflict=True)
            if pricing_day < begin or (end is not None and pricing_day > end):
                continue
            try:
                price = Decimal(str(row.get("cost_price")))
            except (InvalidOperation, ValueError):
                _fail("full_history_price_invalid")
            if (not price.is_finite() or price <= 0 or len(price.as_tuple().digits) > 100
                    or abs(price.as_tuple().exponent) > 100 or abs(price.adjusted()) > 100):
                _fail("full_history_price_requires_independent_zero_or_invalid_basis")
            intervals.append({"begin_date": None if unbounded else begin,
                              "begin_unbounded": unbounded, "begin_date_raw": begin_raw,
                              "end_date": end, "unit_cost": str(price),
                              "source_pointer": f"/data/sku_history_cost_price_maps/{company}/{index}"})
        if not intervals:
            _fail("full_history_price_not_covering_original_day")
        prices = {Decimal(row["unit_cost"]) for row in intervals}
        if len(prices) != 1:
            _fail("full_history_price_overlapping_conflict", conflict=True)
        quote = {"schema": SCHEMA, "sku": sku, "company": company,
                 "pricing_day": pricing_day, "unit_cost": str(next(iter(prices))),
                 "intervals": intervals, "source_response_sha": self.raw_sha256,
                 "source_response_bytes": self.raw_bytes,
                 "source_request": {"sku_ids": list(self._skus), "get_way": "all",
                                    "is_use_item_sku_cost_price": False},
                 "captured_at": self._captured_at, "financial_applied": False,
                 "financial_authority": None}
        encoded = json.dumps(quote, ensure_ascii=False, sort_keys=True, allow_nan=False,
                             separators=(",", ":")).encode()
        return {**quote, "quote_sha": _sha(encoded)}
