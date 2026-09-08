from __future__ import annotations

import csv
import io

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledger.commission_api import install
from ledger.commission_registry import Registry
from ledger.workspace import Workspace
from test_commission import _model


def setup(tmp_path):
    ws = Workspace(tmp_path)
    registry = Registry(tmp_path)
    registry.operator_add("tester", "test-password-strong", admin=True)
    app = FastAPI()
    install(app, lambda: ws, _model)
    client = TestClient(app)
    return ws, registry, client


def test_writes_auth_audit_and_conflict(tmp_path):
    _, registry, c = setup(tmp_path)
    payload = {"person": {"name": "甲"}, "reason": "登记", "expected_revision": 0}
    assert c.post("/api/commission-v2/people", json=payload).status_code == 401
    assert c.post("/api/commission-v2/session", json={"name": "tester", "password": "wrong"}).status_code == 400
    login = c.post("/api/commission-v2/session", json={"name": "tester", "password": "test-password-strong"})
    assert login.status_code == 200
    assert "HttpOnly" in login.headers["set-cookie"]
    person = c.post("/api/commission-v2/people", json=payload).json()
    assert person["name"] == "甲"
    assert c.post("/api/commission-v2/people", json=payload, headers={"Origin": "https://example.invalid"}).status_code == 403
    stale = {**payload, "person": person, "expected_revision": 0}
    assert c.post("/api/commission-v2/people", json=stale).status_code == 409
    history = c.get("/api/commission-v2/history").json()["events"]
    assert history[-1]["actor"] == "tester"
    assert "password_hash" not in str(history)
    export = c.get("/api/commission-v2/export/history")
    rows = list(csv.DictReader(io.StringIO(export.text.lstrip('\ufeff'))))
    assert len(rows) == len(history)
    assert c.post("/api/commission-v2/password", json={"old_password": "test-password-strong", "new_password": "changed-password-strong"}).status_code == 200
    assert c.post("/api/commission-v2/people", json=payload).status_code == 401


def test_bulk_validation_is_atomic_and_closed_period_remains_frozen(tmp_path):
    ws, registry, c = setup(tmp_path)
    p = registry.person_save({"name": "甲"}, "tester", "建立")
    c.post("/api/commission-v2/session", json={"name": "tester", "password": "test-password-strong"})
    body = {"segments": [{"valid_from": "2026-06-01", "mode": "distribute", "total_rate": ".05",
                           "allocations": [{"person_id": p["id"], "role": "运营", "rate": ".05"}]}]}
    bad = c.post("/api/commission-v2/bulk", json={"reason": "批量测试", "schemes": [
        {"store_id": "s1", "product_id": "123456789001", "body": body, "publish": True},
        {"store_id": "unknown", "product_id": "123456789002", "body": body, "publish": True}]})
    assert bad.status_code == 400
    assert not registry.active("s1")[1]
    old = {"can_close": True, "findings": [], "missing_sources": [],
           "commission": {"total": 12.34, "base_total": 246.8, "people": [{"person": "旧人员", "amount": 12.34}]}}
    run_id = ws.record("s1", "2026-06", old, [], evidence_ready=True)
    ws.close_period("s1", "2026-06", by="tester", note="原月结")
    frozen = ws.state("s1", "2026-06")
    assert frozen.run_id == run_id
    res = c.post("/api/commission-v2/schemes", json={"store_id": "s1", "product_id": "123456789001", "body": body,
                                                   "reason": "回溯修改", "publish": True})
    assert res.status_code == 200
    assert ws.state("s1", "2026-06").run_id == run_id
    assert c.get("/api/commission-v2/payout?store_id=s1&period=2026-06").json()["total"] == 12.34
