"""Full source histories retain their identity across dated selections."""
import base64
from copy import deepcopy
import hashlib
import json

import pytest

from ledger.full_history_intervals import FullHistoryIntervals, SCHEMA
from ledger.upstream_history import HistoryConflict, HistoryUnavailable


def period(price="1.2500", begin="开始", end="", sku="A"):
    return {"sku_id": sku, "cost_price": price, "begin_date": begin, "end_date": end}


def source(rows=None, *, companies=None, skus=None, code=0):
    body = {"code": code, "data": {"sku_history_cost_price_maps": companies or {"10": rows or [period()]}}}
    raw = json.dumps(body, ensure_ascii=False).encode()
    envelope = {"http_status": 200, "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "raw_b64": base64.b64encode(raw).decode(), "captured_at": 1793500000.5,
                "request": {"sku_ids": skus or ["A"], "get_way": "all", "is_use_item_sku_cost_price": False}}
    return raw, envelope


def test_one_actual_full_response_selects_multiple_days_without_faking_as_of():
    raw, env = source([period(begin="开始", end="2026-05-15"), period("2.5000", "2026-05-16")])
    reader = FullHistoryIntervals(raw, env)
    early = reader.quote("A", "10", "2026-05-01")
    boundary = reader.quote("A", "10", "2026-05-15")
    late = reader.quote("A", "10", "2026-05-16")
    assert early["unit_cost"] == boundary["unit_cost"] == "1.2500"
    assert late["unit_cost"] == "2.5000"
    assert early["source_response_sha"] == late["source_response_sha"] == env["raw_sha256"]
    assert early["schema"] == SCHEMA != "ledger-history-quote.v1"
    assert "as_of" not in early["source_request"]
    assert early["source_request"] == env["request"]
    assert early["quote_sha"] == reader.quote("A", "10", "2026-05-01")["quote_sha"]
    assert early["quote_sha"] != late["quote_sha"]
    assert early["financial_authority"] is None and early["financial_applied"] is False
    assert "artifact_refs" not in early


def test_only_the_literal_opening_marker_is_unbounded():
    raw, env = source()
    q = FullHistoryIntervals(raw, env).quote("A", "10", "1900-01-01")
    assert q["intervals"][0]["begin_date"] is None
    assert q["intervals"][0]["begin_date_raw"] == "开始"
    assert q["intervals"][0]["begin_unbounded"] is True
    raw, env = source([period(begin="")])
    with pytest.raises(HistoryUnavailable, match="boundary_unknown"):
        FullHistoryIntervals(raw, env).quote("A", "10", "2026-05-01")


def test_current_or_other_company_prices_do_not_fill_a_missing_historical_quote():
    raw, env = source(companies={"10": [period(begin="2026-06-01")], "20": [period("99")]})
    reader = FullHistoryIntervals(raw, env)
    with pytest.raises(HistoryUnavailable, match="not_covering_original_day"):
        reader.quote("A", "10", "2026-05-01")
    with pytest.raises(HistoryUnavailable, match="not_covering_original_day"):
        reader.quote("A", "30", "2026-05-01")


def test_different_overlapping_prices_remain_a_conflict():
    raw, env = source([period(), period("2", "2026-05-01")])
    with pytest.raises(HistoryConflict, match="overlapping_conflict"):
        FullHistoryIntervals(raw, env).quote("A", "10", "2026-05-05")


def test_equal_overlapping_intervals_keep_both_source_pointers():
    raw, env = source([period("1.25"), period("1.2500", "2026-05-01")])
    q = FullHistoryIntervals(raw, env).quote("A", "10", "2026-05-05")
    assert len(q["intervals"]) == 2
    assert len({x["source_pointer"] for x in q["intervals"]}) == 2


@pytest.mark.parametrize("price", ["0", "-1", "NaN", "Infinity", "1e1000000000", None, True])
def test_no_zero_negative_nonfinite_or_unbounded_decimal_price_is_admitted(price):
    raw, env = source([period(price)])
    with pytest.raises(HistoryUnavailable):
        FullHistoryIntervals(raw, env).quote("A", "10", "2026-05-05")


@pytest.mark.parametrize("begin,end", [("2026-06-01", "2026-05-01"), ("bad", ""), ("开始", "2026-02-30")])
def test_invalid_interval_boundaries_are_not_inferred(begin, end):
    raw, env = source([period(begin=begin, end=end)])
    with pytest.raises((HistoryConflict, HistoryUnavailable)):
        FullHistoryIntervals(raw, env).quote("A", "10", "2026-05-05")


@pytest.mark.parametrize("change", [
    {"get_way": "latest"}, {"is_use_item_sku_cost_price": True},
    {"wms_co_ids": ["10"]}, {"as_of": "2026-05-01"}, {"sku_ids": ["A", "A"]},
])
def test_narrowed_or_synthetic_source_requests_are_rejected(change):
    raw, env = source()
    env["request"].update(change)
    with pytest.raises(HistoryUnavailable):
        FullHistoryIntervals(raw, env)


@pytest.mark.parametrize("change", [{"http_status": 403}, {"raw_sha256": "0" * 64},
                                     {"raw_b64": "not-base64"}, {"captured_at": float("nan")}])
def test_capture_tampering_and_failed_source_requests_cannot_quote(change):
    raw, env = source()
    env.update(change)
    with pytest.raises(HistoryUnavailable):
        FullHistoryIntervals(raw, env)


def test_source_error_unrequested_sku_duplicate_keys_and_nonfinite_json_fail_closed():
    for kwargs in [{"code": False}, {"code": 1}, {"rows": [period(sku="OTHER")]}]:
        raw, env = source(**kwargs)
        with pytest.raises(HistoryUnavailable):
            FullHistoryIntervals(raw, env)
    raw, env = source()
    for changed in [raw.replace(b'"code": 0', b'"code": 0, "code": 1'),
                    raw.replace(b'"1.2500"', b'NaN')]:
        modified = deepcopy(env)
        modified["raw_sha256"] = hashlib.sha256(changed).hexdigest()
        modified["raw_b64"] = base64.b64encode(changed).decode()
        with pytest.raises(HistoryUnavailable):
            FullHistoryIntervals(changed, modified)


def test_raw_numeric_price_lexeme_keeps_more_than_binary_float_precision():
    raw, env = source()
    raw = raw.replace(b'"1.2500"', b'0.10000000000000001')
    env.update(raw_sha256=hashlib.sha256(raw).hexdigest(), raw_b64=base64.b64encode(raw).decode())
    assert FullHistoryIntervals(raw, env).quote("A", "10", "2026-05-01")["unit_cost"] == "0.10000000000000001"


def test_callers_cannot_mutate_a_later_quotes_source_request():
    raw, env = source()
    reader = FullHistoryIntervals(raw, env)
    env["request"]["sku_ids"].append("OTHER")
    first = reader.quote("A", "10", "2026-05-01")
    first["source_request"]["sku_ids"].append("OTHER")
    assert reader.quote("A", "10", "2026-05-02")["source_request"]["sku_ids"] == ["A"]
