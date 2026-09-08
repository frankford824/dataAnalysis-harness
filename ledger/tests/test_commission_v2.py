from __future__ import annotations

import json
import sqlite3

import pytest
import polars as pl

from ledger.commission_engine import calculate, persist
from ledger.commission_registry import Registry, RegistryError, RevisionConflict
from test_commission import _model, _run, _rule
from ledger.model.schema import Metric, StatementNode, ValueExpr


def segment(start, person, end="", rate="0.05"):
    return {"valid_from": start, "valid_to": end, "mode": "distribute", "total_rate": rate,
            "allocations": [{"person_id": person, "role": "运营", "rate": rate}]}


def registry(tmp_path):
    r = Registry(tmp_path)
    a = r.person_save({"name": "甲"}, "tester", "登记人员")
    b = r.person_save({"name": "乙"}, "tester", "登记人员")
    return r, a["id"], b["id"]


def test_boundaries_expiry_and_legacy_before_cutover(tmp_path):
    r, a, b = registry(tmp_path)
    r.save_scheme("s1", "p1", {"segments": [segment("2026-05-10", a, "2026-05-20"),
                                            segment("2026-05-20", b, "2026-05-25")]},
                  "tester", "交接", publish=True)
    model = _model([_rule("2026-01-01", "", "历史店长", .1, .1)])
    orders = _run([("old", "p1", "2026-05-09", 100), ("a", "p1", "2026-05-10", 100),
                   ("b", "p1", "2026-05-20", 100), ("expired", "p1", "2026-05-25", 100)])
    summary, details, meta = calculate(orders, model, "s1", "2026-05", r)
    assert summary["total"] == 20
    assert summary["unassigned_base"] == 100
    assert details.filter(details["sub_order_id"] == "expired")["status"].item() == "expired"
    persist(r, 1, details, meta)
    with r.connect() as conn:
        saved = conn.execute("SELECT * FROM calculation").fetchone()
        assert json.loads(saved["summary_json"])["total"] == 20


def test_same_name_separate_identity_and_exact_cent_allocation(tmp_path):
    r, a, _ = registry(tmp_path)
    second = r.person_save({"name": "甲", "employee_no": "different"}, "tester", "同名不同人")
    body = {"segments": [{"valid_from": "2026-05-01", "total_rate": ".1", "allocations": [
        {"person_id": a, "role": "组长", "rate": ".05"},
        {"person_id": second["id"], "role": "运营", "rate": ".05"}]}]}
    r.save_scheme("s1", "p1", body, "tester", "分点", publish=True)
    summary, details, _ = calculate(_run([("a", "p1", "2026-05-02", .1)]), _model(), "s1", "2026-05", r)
    assert len(summary["people"]) == 2
    assert summary["total"] == .02
    assert sum(p["amount"] for p in summary["people"]) == summary["total"]


def test_history_draft_publish_and_optimistic_concurrency(tmp_path):
    r, a, b = registry(tmp_path)
    first = r.save_scheme("s1", "p1", {"segments": [segment("2026-05-01", a)]}, "tester", "初版", publish=True)
    second = r.save_scheme("s1", "p1", {"segments": [segment("2026-05-01", b)]}, "tester", "调整草稿", expected=1)
    assert r.active("s1")[1][0]["id"] == first["version_id"]
    with pytest.raises(RevisionConflict):
        r.publish(first["id"], 1, "tester", "过期页面")
    r.publish(first["id"], second["revision"], "tester", "确认调整")
    assert len(r.scheme(first["id"])["versions"]) == 3
    with pytest.raises(sqlite3.IntegrityError), r.transaction() as conn:
        conn.execute("DELETE FROM scheme_version WHERE id=?", (first["version_id"],))
    with pytest.raises(sqlite3.IntegrityError), r.transaction() as conn:
        conn.execute("DELETE FROM event")


def test_overlaps_invalid_rates_and_explicit_zero(tmp_path):
    r, a, _ = registry(tmp_path)
    with pytest.raises(RegistryError, match="重叠"):
        r.save_scheme("s1", "p1", {"segments": [segment("2026-05-01", a), segment("2026-05-02", a)]}, "t", "r")
    with pytest.raises(RegistryError):
        r.save_scheme("s1", "p1", {"segments": [segment("2026-05-01", a, rate="NaN")]}, "t", "r")
    r.save_scheme("s1", "p1", {"segments": [{"valid_from": "2026-05-01", "mode": "exclude",
                                            "total_rate": "0", "allocations": []}]}, "t", "不提成", publish=True)
    summary, details, _ = calculate(_run([("a", "p1", "2026-05-02", 100)]),
                                    _model([_rule("2026-01-01", "", "店长", .1, .1)]), "s1", "2026-05", r)
    assert summary["total"] == 0
    assert summary["unassigned_base"] == 0
    assert details["status"].item() == "exclude"


def test_one_person_multiple_roles_is_rounded_once_per_order(tmp_path):
    r, a, _ = registry(tmp_path)
    r.save_scheme("s1", "p1", {"segments": [{"valid_from": "2026-05-01", "total_rate": ".1", "allocations": [
        {"person_id": a, "role": "组长", "rate": ".05"},
        {"person_id": a, "role": "运营", "rate": ".05"}]}]}, "tester", "两角色同一人", publish=True)
    summary, details, _ = calculate(_run([("a", "p1", "2026-05-02", .1)]), _model(), "s1", "2026-05", r)
    assert summary["total"] == .01
    assert len(summary["people"]) == 1
    assert details.height == 1


def test_profit_policy_is_month_effective_and_wage_preview_is_explicit(tmp_path):
    r, a, _ = registry(tmp_path)
    r.save_scheme("s1", "p1", {"segments": [{**segment("2026-05-01", a), "amount_hold": "wage_pending"}]},
                  "tester", "登记关系", publish=True)
    model = _model()
    model = model.model_copy(update={"metrics": (*model.metrics, Metric(id="fees", name="费用", source="cost", value=ValueExpr(op="sum", of=["amount"]))),
                                    "statement": (*model.statement, StatementNode(id="profit", name="利润", commission_base=True,
                                                                                 formula={"op":"add","of":["gross","fees"]}))})
    r.save_policy({"base_node":"profit","on_loss":"inherit","wages":"skip_preview"}, "2026-06-01", "tester", "六月利润口径")
    run = _run([("a", "p1", "2026-06-02", 100.0)], period="2026-06")
    goods = run.spine_facts.with_columns(pl.lit("goods").alias("metric_id"), pl.lit(-30.0).alias("amount"))
    fees = run.spine_facts.with_columns(pl.lit("fees").alias("metric_id"), pl.lit(-20.0).alias("amount"))
    run.spine_facts = pl.concat([run.spine_facts, goods, fees])
    result, details, meta = calculate(run, model, "s1", "2026-06", r)
    assert result["base_node"] == "profit"
    assert result["base_total"] == 50
    assert result["total"] == 2.5
    assert result["wage_preview_orders"] == 1
    assert result["amount_complete"] is False
    assert r.policy("2026-05", "s1") is None


def test_global_policy_with_no_relationship_still_records_unassigned(tmp_path):
    r = Registry(tmp_path)
    r.save_policy({"base_node":"gross","wages":"pending"}, "2026-05-01", "tester", "统一口径")
    summary, details, _ = calculate(_run([("a","p1","2026-05-02",100)]), _model(), "s1", "2026-05", r)
    assert summary["unassigned_base"] == 100
    assert details["amount"].item() is None
