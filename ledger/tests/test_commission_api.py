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


def test_writes_auth_audit_and_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_COMMISSION_AUTH_MODE", "password")
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


def test_large_csv_export_survives_sequential_worker_thread_switches(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import ledger.commission_api as api
    ws, registry, _ = setup(tmp_path)
    with registry.transaction() as conn:
        for i in range(3000):
            Registry.audit(conn, "tester", "probe", str(i), "x" * 80, {}, {"row": i})
    app = FastAPI()
    routers = []
    monkeypatch.setattr(app, "include_router", routers.append)
    install(app, lambda: ws, _model)
    # Starlette calls next(iterator) in a thread pool. Exercise an actual switch
    # after the cursor has opened, including closing it on another worker.
    monkeypatch.setattr(api, "StreamingResponse", lambda iterator, **kwargs: iterator)
    route = next(r for r in routers[0].routes if r.path == "/api/commission-v2/export/history")
    stream = route.endpoint()
    chunks = []
    with ThreadPoolExecutor(1) as first, ThreadPoolExecutor(1) as second:
        pools = [first, second]
        for i in range(100):
            chunk = pools[i % 2].submit(next, stream, None).result()
            if chunk is None:
                break
            chunks.append(chunk)
        else:
            raise AssertionError("export did not finish")
    rows = list(csv.DictReader(io.StringIO("".join(chunks).lstrip("\ufeff"))))
    assert len(rows) == 3001  # operator creation plus all events
    assert rows[-1]["entity_id"] == "'2999"
    assert len(chunks) < 40  # bounded chunks, not one thread hop per CSV row


def test_simple_settings_without_login_and_atomic_person_creation(tmp_path, monkeypatch):
    monkeypatch.delenv("LEDGER_COMMISSION_AUTH_MODE", raising=False)
    _, registry, client = setup(tmp_path)
    change = {"store_id":"s1","product_id":"123456789001","product_name":"商品甲",
              "valid_from":"2000-06-01", "allocations":[{"name":"直接填写的人员","rate":"0.05"}]}
    response = client.post("/api/commission-v2/settings", json=change)
    assert response.status_code == 200
    assert client.get("/api/commission-v2/status").json()["auth_mode"] == "open"
    with registry.transaction() as conn:
        conn.execute("INSERT INTO catalog VALUES(?,?,?,?,?,?)", ('s1','123456789001','源端标题','10','{}','2000-06-01'))
    rows = client.get("/api/commission-v2/settings?state=enabled&search=商品甲").json()["rows"]
    assert len(rows) == 1 and rows[0]["people"][0]["name"] == "直接填写的人员"
    assert rows[0]["people"][0]["rate"] == "0.05"
    assert rows[0]["product_name"] == "商品甲"  # still visible without a catalog entry
    export = client.get("/api/commission-v2/export/settings").text
    exported = list(csv.DictReader(io.StringIO(export.lstrip('\ufeff'))))
    assert exported[0]["宝贝ID"] == "'123456789001"
    assert exported[0]["提成比例"] == "5.00%"
    assert client.post("/api/commission-v2/settings", json=change, headers={"Origin":"https://example.invalid"}).status_code == 403
    bad = {**change,"product_id":"123456789002","allocations":[{"name":"不能残留的人员","rate":"2"}]}
    assert client.post("/api/commission-v2/settings", json=bad).status_code == 400
    assert not any(p['name']=="不能残留的人员" for p in registry.people())
    assert client.post("/api/commission-v2/settings", json=change).status_code == 409


def test_simple_edit_keeps_prior_dates_future_settings_and_role_parts(tmp_path):
    _, registry, _ = setup(tmp_path)
    p = registry.person_save({"name":"甲"}, "tester", "登记")
    original = {"segments":[
        {"valid_from":"2026-06-01","valid_to":"2026-10-01","mode":"distribute","total_rate":".05",
         "allocations":[{"person_id":p['id'],"role":"组长","rate":".02"},{"person_id":p['id'],"role":"运营","rate":".03"}]},
        {"valid_from":"2026-10-01","mode":"exclude","total_rate":"0","allocations":[]}]}
    registry.save_scheme('s1','123456789001',original,'tester','原设置',publish=True)
    updated = registry.save_setting({"store_id":"s1","product_id":"123456789001","valid_from":"2026-09-01",
                                     "expected_revision":1,"allocations":[{"person_id":p['id'],"rate":".05"}]}, 'local:commission')
    parts = updated['body']['segments']
    assert [(p['valid_from'],p['valid_to']) for p in parts] == [('2026-06-01T00:00:00','2026-09-01T00:00:00'),('2026-09-01T00:00:00','2026-10-01T00:00:00'),('2026-10-01T00:00:00','')]
    assert len(parts[1]['allocations']) == 2
    from ledger.commission_catalog import settings
    row = settings(registry, at='2026-09-10T00:00:00')['rows'][0]
    assert len(row['people']) == 1 and row['people'][0]['rate'] == '0.05'
    assert settings(registry, state='disabled', at='2026-10-10T00:00:00')['rows'][0]['setting']['mode'] == 'exclude'
