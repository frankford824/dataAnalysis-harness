"""Commission workspace API. Reads are public on the existing intranet surface;
writes require an identified commission operator and an audit reason.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import re
from decimal import Decimal
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse

import polars as pl
from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import commission_catalog
from .commission_registry import Registry, RegistryError, RevisionConflict, json_text, local_time
from .money import money_float


class Change(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    expected_revision: int = 0


class SchemeChange(Change):
    store_id: str
    product_id: str
    body: dict
    publish: bool = False


class SettingChange(BaseModel):
    store_id: str
    product_id: str
    product_name: str = ""
    mode: str = "distribute"
    allocations: list[dict] = Field(default_factory=list)
    valid_from: str
    valid_to: str = ""
    expected_revision: int = 0


class PersonChange(Change):
    person: dict


class Terminate(Change):
    at: str
    mode: str = "hold"


class Restore(Change):
    version_id: str


class BulkChange(Change):
    schemes: list[dict] = Field(min_length=1, max_length=500)


class PolicyChange(Change):
    effective_from: str
    store_id: str = ""
    base_node: str
    on_loss: str = "inherit"
    wages: str = "pending"


def csv_response(filename, columns, rows):
    def stream():
        yield "\ufeff"
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        for row in rows:
            values = []
            for column in columns:
                value = row.get(column, "")
                if isinstance(value, (dict, list)):
                    value = json_text(value)
                if isinstance(value, str):
                    # Prevent formulas; preserve long identifiers as text in Excel.
                    if value[:1] in "=+@-" or column.endswith("_id") or column in {"product_id", "宝贝ID"}:
                        value = "'" + value if value else ""
                values.append(value)
            writer.writerow(values)
            if buffer.tell() >= 65536:
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate()
        if buffer.tell():
            yield buffer.getvalue()
    return StreamingResponse(stream(), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def install(app, workspace, model):
    router = APIRouter(prefix="/api/commission-v2")

    def reg():
        return Registry(workspace().root)

    def validate_product(store_id, product_id):
        if product_id == "*" or re.fullmatch(r"\d{9,20}", product_id):
            return
        with reg().connect() as conn:
            known = conn.execute("SELECT 1 FROM catalog WHERE store_id=? AND product_id=?", (store_id, product_id)).fetchone()
        if not known:
            raise RegistryError("请输入有效的平台宝贝ID，不能使用ERP编码或人名")

    def actor(request: Request):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "跨站写入已拒绝")
        registry = reg()
        if registry.auth_mode() == "open":
            return {"id": "local:commission", "name": "本机操作", "admin": False}
        if registry.auth_mode() == "declared":
            name = request.headers.get("x-commission-actor", "").strip()
            if not name or len(name) > 100:
                raise HTTPException(401, "请先登记操作人姓名")
            return {"id": "declared:" + name, "name": name, "admin": False}
        found = registry.actor(request.cookies.get("commission_session", ""))
        if not found:
            raise HTTPException(401, "请先登录提成管理账号")
        return found

    @app.exception_handler(RegistryError)
    async def registry_error(_request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409 if isinstance(exc, RevisionConflict) else 400,
                            content={"detail": str(exc)})

    @router.get("/status")
    def status(request: Request):
        registry = reg()
        with registry.connect() as conn:
            counts = {table: int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
                      for table in ["person", "scheme", "catalog", "pending", "operator"]}
            counts["active"] = int(conn.execute("SELECT count(*) FROM scheme WHERE active_version IS NOT NULL").fetchone()[0])
            jobs = [dict(r) for r in conn.execute("SELECT id,kind,at,status,result,error FROM job ORDER BY at DESC LIMIT 10")]
            imports = [dict(r) for r in conn.execute("SELECT * FROM import_batch ORDER BY at DESC LIMIT 20")]
            refreshed = conn.execute("SELECT max(refreshed_at) FROM catalog WHERE payload NOT LIKE '%\"origin\":\"order_observation\"%'").fetchone()[0]
            pending = [dict(r) for r in conn.execute("SELECT * FROM pending ORDER BY revision")]
        for item in imports:
            item["summary"] = json.loads(item["summary"])
        for item in jobs:
            item["result"] = json.loads(item["result"])
        feed = None
        feed_path = workspace().root / "order-feed.db"
        if feed_path.exists():
            with closing(sqlite3.connect(f"file:{feed_path.as_posix()}?mode=ro", uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT snapshot_id,consumed_seq,source_latest_seq,last_success,last_error FROM feed_state").fetchone()
                if row:
                    feed = dict(row)
                    feed["caught_up"] = row["consumed_seq"] >= row["source_latest_seq"]
                    feed["unmapped_stores"] = conn.execute("SELECT count(*) FROM feed_store WHERE mapping_status<>'confirmed'").fetchone()[0]
        return {"auth_mode": registry.auth_mode(), "actor": registry.actor(request.cookies.get("commission_session", "")),
                "counts": counts, "jobs": jobs, "imports": imports, "pending": pending, "catalog_refreshed_at": refreshed,
                "stores": [{"id": s.id, "name": s.name} for s in model().stores], "revision": registry.revision(), "feed": feed}

    @router.post("/session")
    async def login(request: Request, response: Response):
        body = await request.json()
        token = reg().login(str(body.get("name") or ""), str(body.get("password") or ""))
        response.set_cookie("commission_session", token, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", max_age=8 * 3600, path="/api")
        return {"actor": reg().actor(token)}

    @router.delete("/session")
    def logout(request: Request, response: Response):
        token = request.cookies.get("commission_session", "")
        with reg().transaction() as conn:
            conn.execute("DELETE FROM session WHERE digest=?", (hashlib.sha256(token.encode()).hexdigest(),))
        response.delete_cookie("commission_session", path="/api")
        return {"ok": True}

    @router.post("/operators")
    async def add_operator(request: Request):
        who = actor(request)
        if not who.get("admin"):
            raise HTTPException(403, "只有管理员可以创建操作账号")
        body = await request.json()
        reg().operator_add(str(body.get("name") or ""), str(body.get("password") or ""), admin=bool(body.get("admin")), by=who["id"])
        return {"ok": True}

    @router.post("/password")
    async def change_password(request: Request, response: Response):
        who = actor(request)
        body = await request.json()
        reg().change_password(who["id"], str(body.get("old_password") or ""), str(body.get("new_password") or ""))
        response.delete_cookie("commission_session", path="/api")
        return {"ok": True}

    @router.get("/people")
    def people():
        registry = reg()
        with registry.connect() as conn:
            external = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM external_person")]
        return {"people": registry.people(), "external_users": external}

    @router.get("/policies")
    def policies():
        with reg().connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM policy_version ORDER BY id DESC LIMIT 200")]
            revisions = {r[0]: r[1] for r in conn.execute("SELECT store_id,max(id) FROM policy_version GROUP BY store_id")}
        for row in rows:
            row["body"] = json.loads(row["body"])
        return {"policies": rows, "revisions": revisions, "bases": [{"id": n.id, "name": n.name} for n in model().commission_bases()]}

    @router.post("/policies")
    def policy_save(change: PolicyChange, request: Request):
        m = model()
        if change.base_node not in {n.id for n in m.commission_bases()}:
            raise RegistryError("所选节点不能用于提成基数")
        if change.store_id and change.store_id not in {s.id for s in m.stores}:
            raise RegistryError("该店铺尚未登记")
        body = {"base_node": change.base_node, "on_loss": change.on_loss, "wages": change.wages}
        if change.on_loss == "inherit":
            body["loss_by_store"] = {s.id: s.commission_on_loss for s in m.stores}
        return reg().save_policy(body,
                                  change.effective_from, actor(request)["id"], change.reason,
                                  store_id=change.store_id, stores=[s.id for s in m.active_stores()], expected_revision=change.expected_revision)

    @router.post("/people")
    def person_save(change: PersonChange, request: Request):
        return reg().person_save(change.person, actor(request)["id"], change.reason, change.expected_revision)

    @router.post("/catalog/refresh")
    def refresh_catalog(request: Request):
        return reg().enqueue("catalog", {}, actor(request)["id"])

    @router.get("/settings")
    def settings(store_id: str = "", search: str = "", state: str = "", after: str = "",
                 limit: int = Query(60, ge=1, le=500)):
        return commission_catalog.settings(reg(), store_id=store_id, search=search, state=state, after=after, limit=limit)

    @router.post("/settings")
    def setting_save(change: SettingChange, request: Request):
        if change.store_id not in {s.id for s in model().stores}:
            raise RegistryError("请选择已登记的店铺")
        validate_product(change.store_id, change.product_id.strip())
        return reg().save_setting({**change.model_dump(), "product_id":change.product_id.strip()}, actor(request)["id"])

    @router.get("/products")
    def products(store_id: str = "", search: str = "", missing: bool = False, after: str = "",
                 limit: int = Query(100, ge=1, le=500)):
        return commission_catalog.products(reg(), store_id=store_id, search=search, missing=missing, after=after, limit=limit)

    @router.get("/schemes")
    def schemes(store_id: str = "", search: str = "", after: str = "", limit: int = Query(100, ge=1, le=500)):
        with reg().connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT s.*,v.body FROM scheme s LEFT JOIN scheme_version v ON v.id=coalesce(s.draft_version,s.active_version) "
                "WHERE s.id>? AND (?='' OR s.store_id=?) AND (?='' OR s.product_id LIKE ? OR s.product_name LIKE ?) "
                "ORDER BY s.id LIMIT ?", (after, store_id, store_id, search, "%" + search + "%", "%" + search + "%", limit + 1))]
        more = len(rows) > limit
        rows = rows[:limit]
        for row in rows:
            row["body"] = json.loads(row["body"] or "{}")
        return {"schemes": rows, "has_more": more, "next_after": rows[-1]["id"] if more else ""}

    @router.get("/schemes/{scheme_id}")
    def scheme_get(scheme_id: str):
        return reg().scheme(scheme_id)

    @router.post("/schemes")
    def scheme_save(change: SchemeChange, request: Request):
        if change.store_id not in {s.id for s in model().stores}:
            raise RegistryError("店铺尚未在台账登记")
        validate_product(change.store_id, change.product_id.strip())
        return reg().save_scheme(change.store_id, change.product_id.strip(), change.body,
                                 actor(request)["id"], change.reason,
                                 expected=change.expected_revision, publish=change.publish)

    @router.post("/schemes/{scheme_id}/publish")
    def scheme_publish(scheme_id: str, change: Change, request: Request):
        return reg().publish(scheme_id, change.expected_revision, actor(request)["id"], change.reason)

    @router.post("/schemes/{scheme_id}/terminate")
    def scheme_terminate(scheme_id: str, change: Terminate, request: Request):
        if change.mode not in {"hold", "exclude"}:
            raise RegistryError("终止后请选择待确认或明确不提成")
        return reg().terminate(scheme_id, change.at, change.expected_revision,
                               actor(request)["id"], change.reason, mode=change.mode)

    @router.post("/schemes/{scheme_id}/restore")
    def scheme_restore(scheme_id: str, change: Restore, request: Request):
        registry = reg()
        item = registry.scheme(scheme_id)
        version = next((v for v in item["versions"] if v["id"] == change.version_id), None)
        if version is None:
            raise RegistryError("没有这版历史关系")
        return registry.save_scheme(item["store_id"], item["product_id"], version["body"],
                                    actor(request)["id"], change.reason, expected=change.expected_revision, action="restore")

    @router.post("/bulk")
    def bulk_save(change: BulkChange, request: Request):
        registry = reg()
        who = actor(request)["id"]
        stores = {s.id for s in model().stores}
        with registry.transaction() as conn:
            result = []
            for item in change.schemes:
                if item.get("store_id") not in stores:
                    raise RegistryError("批量修改中有未登记店铺")
                validate_product(item["store_id"], item["product_id"])
                result.append(registry.save_scheme(item["store_id"], item["product_id"], item["body"],
                                                   who, change.reason, expected=item.get("expected_revision", 0),
                                                   publish=bool(item.get("publish")), action="bulk", conn=conn))
        return {"count": len(result), "schemes": result}

    @router.get("/history")
    def history(entity_id: str = "", after: int = 0, limit: int = Query(100, ge=1, le=1000)):
        rows = reg().history(entity_id, after, limit)
        for row in rows:
            row["before"] = json.loads(row.pop("before_json"))
            row["after"] = json.loads(row.pop("after_json"))
        return {"events": rows, "next_after": rows[-1]["id"] if rows else after}

    @router.post("/imports")
    async def import_file(request: Request, file: UploadFile = File(...), effective_from: str = "2026-06-01"):
        who = actor(request)["id"]
        raw = await file.read(40 * 1024 * 1024 + 1)
        if len(raw) > 40 * 1024 * 1024:
            raise RegistryError("导入文件超过40MB")
        registry = reg()
        folder = registry.root / "uploads"
        folder.mkdir(exist_ok=True)
        sha = hashlib.sha256(raw).hexdigest()
        (folder / sha).write_bytes(raw)
        return registry.enqueue("import", {"sha": sha, "filename": Path(file.filename or "方案.xlsx").name,
                                           "effective_from": local_time(effective_from)}, who)

    @router.get("/imports/{batch_id}")
    def import_get(batch_id: str, status: str = "", after: int = 0, limit: int = Query(100, ge=1, le=500)):
        with reg().connect() as conn:
            batch = conn.execute("SELECT * FROM import_batch WHERE id=?", (batch_id,)).fetchone()
            if not batch:
                raise HTTPException(404, "没有这批记录")
            rows = [dict(r) for r in conn.execute("SELECT * FROM import_row WHERE batch_id=? AND row_no>? "
                                                 "AND (?='' OR status=?) ORDER BY row_no LIMIT ?",
                                                 (batch_id, after, status, status, limit + 1))]
        more = len(rows) > limit
        rows = rows[:limit]
        for row in rows:
            row["issues"] = json.loads(row["issues"])
            row["payload"] = json.loads(row["payload"])
        return {**dict(batch), "summary": json.loads(batch["summary"]), "rows": rows,
                "has_more": more, "next_after": rows[-1]["row_no"] if more else 0}

    @router.post("/imports/{batch_id}/activate")
    def import_activate(batch_id: str, change: Change, request: Request):
        return reg().enqueue("activate", {"batch_id": batch_id, "reason": change.reason}, actor(request)["id"])

    @router.post("/imports/{batch_id}/resolve/{row_no}")
    def resolve_import(batch_id: str, row_no: int, change: SchemeChange, request: Request):
        registry = reg()
        who = actor(request)["id"]
        if change.store_id not in {s.id for s in model().stores}:
            raise RegistryError("请先选择已登记的台账店铺")
        validate_product(change.store_id, change.product_id.strip())
        with registry.transaction() as conn:
            row = conn.execute("SELECT * FROM import_row WHERE batch_id=? AND row_no=?", (batch_id, row_no)).fetchone()
            batch = conn.execute("SELECT * FROM import_batch WHERE id=?", (batch_id,)).fetchone()
            if not row or not batch:
                raise RegistryError("没有这条原始记录")
            original = json.loads(row["payload"])
            body = {**change.body, "source": {"batch_id": batch_id, "sha": batch["sha"],
                                              "sheet": original["sheet"], "rows": [row_no]}}
            result = registry.save_scheme(change.store_id, change.product_id, body, who, change.reason,
                                           expected=change.expected_revision, publish=change.publish,
                                           action="resolve", conn=conn)
            if change.publish:
                conn.execute("UPDATE import_row SET status='resolved',issues='[]',store_id=?,product_id=? "
                             "WHERE batch_id=? AND row_no=?", (change.store_id, change.product_id, batch_id, row_no))
                counts = {r[0]: r[1] for r in conn.execute("SELECT status,count(*) FROM import_row WHERE batch_id=? GROUP BY status", (batch_id,))}
                summary = {**json.loads(batch["summary"]), "remaining_review": counts.get("review", 0),
                           "resolved": counts.get("resolved", 0)}
                conn.execute("UPDATE import_batch SET summary=? WHERE id=?", (json_text(summary), batch_id))
            registry.audit(conn, who, "import.resolve", batch_id, change.reason, dict(row), result)
        return result

    @router.get("/calculations")
    def calculations(store_id: str = "", period: str = "", after: int = 0, limit: int = Query(50, ge=1, le=500)):
        with reg().connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id,finance_run,store_id,period,at,registry_revision,summary_json FROM calculation "
                "WHERE (?='' OR store_id=?) AND (?='' OR period=?) AND (?=0 OR finance_run<?) "
                "ORDER BY finance_run DESC LIMIT ?", (store_id, store_id, period, period, after, after, limit))]
        for row in rows:
            summary = json.loads(row.pop("summary_json"))
            row.update(total=summary["total"], base_total=summary["base_total"], unassigned_orders=summary.get("unassigned_orders", 0),
                       amount_complete=summary.get("amount_complete", False), notes=summary.get("notes", []),
                       wage_preview_orders=summary.get("wage_preview_orders", 0))
            state = workspace().state(row["store_id"], row["period"])
            row["shown"] = bool(state and state.run_id == row["finance_run"])
            row["closed"] = bool(state and state.state == "closed")
        return {"calculations": rows}

    def detail_frame(calculation_id):
        registry = reg()
        with registry.connect() as conn:
            row = conn.execute("SELECT * FROM calculation WHERE id=?", (calculation_id,)).fetchone()
        if not row:
            raise HTTPException(404, "没有这次计算")
        path = registry.root / "calculations" / Path(row["path"]).name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha"]:
            raise HTTPException(409, "提成明细文件缺失或校验不符")
        return pl.scan_parquet(path), dict(row)

    @router.get("/details/{calculation_id}")
    def details(calculation_id: str, search: str = "", offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        frame, meta = detail_frame(calculation_id)
        if search:
            frame = frame.filter(pl.any_horizontal(pl.col(c).cast(pl.Utf8).fill_null("").str.contains(search, literal=True)
                                                    for c in ["order_id", "sub_order_id", "product_id", "person"]))
        data = frame.slice(offset, limit + 1).collect()
        return {"calculation": {k: meta[k] for k in ["id", "finance_run", "store_id", "period", "at", "registry_revision"]},
                "rows": data.head(limit).to_dicts(), "has_more": data.height > limit}

    @router.get("/export/details/{calculation_id}")
    def export_details(calculation_id: str):
        frame, _ = detail_frame(calculation_id)
        data = frame.collect()
        return csv_response("commission-details.csv", data.columns, data.iter_rows(named=True))

    @router.get("/export/settings")
    def export_settings(store_id: str = "", search: str = "", state: str = ""):
        names = {s.id:s.name for s in model().stores}
        labels = {"enabled":"提成中","disabled":"不提成","pending":"未设置","scheduled":"待生效","expired":"已到期"}
        def rows():
            for row in commission_catalog.iter_settings(reg(), store_id=store_id, search=search, state=state):
                current = row["setting"] or {}
                for person in row["people"] or [{}]:
                    yield {"店铺":names.get(row["store_id"], row["store_id"]),"宝贝ID":row["product_id"],
                           "商品名称":row["product_name"],"状态":labels[row["state"]],"人员":person.get("name", ""),
                           "提成比例":format(Decimal(person["rate"])*100,'f')+'%' if person else "",
                           "生效时间":current.get("valid_from", ""),"结束时间":current.get("valid_to", "")}
        return csv_response("commission-settings.csv", ["店铺","宝贝ID","商品名称","状态","人员","提成比例","生效时间","结束时间"], rows())

    @router.get("/export/history")
    def export_history():
        def rows():
            with reg().connect(thread_affine=False) as conn:
                for row in conn.execute("SELECT * FROM event ORDER BY id"):
                    yield dict(row)
        return csv_response("commission-history.csv", ["id", "at", "actor", "action", "entity_id", "reason", "before_json", "after_json"], rows())

    @router.get("/export/schemes")
    def export_schemes(store_id: str = "", at: str = "", all_versions: bool = False):
        moment = local_time(at) if at else ""
        def rows():
            with reg().connect(thread_affine=False) as conn:
                people = {r["id"]: r["name"] for r in conn.execute("SELECT * FROM person")}
                cursor = conn.execute("SELECT s.store_id,s.product_id,v.* FROM scheme s JOIN scheme_version v ON v.scheme_id=s.id "
                                      "WHERE (?='' OR s.store_id=?) AND (? OR v.id=s.active_version) ORDER BY s.store_id,s.product_id,v.revision",
                                      (store_id, store_id, all_versions))
                for row in cursor:
                    body = json.loads(row["body"])
                    for segment in body["segments"]:
                        if moment and not (segment["valid_from"] <= moment and (not segment["valid_to"] or moment < segment["valid_to"])):
                            continue
                        for line in segment.get("allocations") or [{}]:
                            yield {**dict(row), **segment, **line, "person": people.get(line.get("person_id"), ""),
                                   "product_name": body.get("product_name", ""), "source": body.get("source", {})}
        return csv_response("commission-relationships.csv", ["store_id", "product_id", "product_name", "scheme_id", "id", "revision",
                             "recorded_at", "actor", "action", "reason", "valid_from", "valid_to", "mode", "person_id", "person", "role", "rate", "total_rate", "source"], rows())

    @router.get("/export/import/{batch_id}")
    def export_import(batch_id: str):
        def rows():
            with reg().connect(thread_affine=False) as conn:
                for row in conn.execute("SELECT * FROM import_row WHERE batch_id=? ORDER BY row_no", (batch_id,)):
                    payload = json.loads(row["payload"])
                    yield {**dict(row), **payload}
        return csv_response("commission-import-review.csv", ["row_no", "sheet", "store_id", "source_store", "source_shop_no", "product_id",
                             "product_name", "source_date", "total_rate", "mode", "status", "issues", "allocations", "resolved_allocations", "note", "bp_note"], rows())

    @router.get("/payout")
    def payout(store_id: str = "", period: str = ""):
        states = workspace().periods_of_store(store_id) if store_id else workspace().overview()
        states = [s for s in states if not period or s.period == period]
        people = {}
        stores = []
        for state in states:
            c = (state.result or {}).get("commission") or {}
            stores.append({"store_id": state.store_id, "period": state.period, "state": state.state,
                           "finance_run": state.run_id, "total": c.get("total"), "base_total": c.get("base_total"),
                           "base_name": c.get("base_name", ""), "on_loss": c.get("on_loss", "deduct"),
                           "engine": c.get("engine", "legacy"), "calculation_id": c.get("calculation_id"),
                           "notes": c.get("notes", []), "unassigned_orders": c.get("unassigned_orders"),
                           "amount_complete": c.get("amount_complete", False), "wage_pending_orders": c.get("wage_pending_orders", 0),
                           "registry_revision": c.get("registry_revision")})
            for p in c.get("people", []):
                pid = p.get("person_id") or "legacy:" + hashlib.sha256(p["person"].encode()).hexdigest()[:24]
                entry = people.setdefault(pid, {"person_id": pid, "person": p["person"], "amount": Decimal(0), "stores": 0})
                entry["amount"] += Decimal(str(p["amount"]))
                entry["stores"] += 1
        return {"stores": stores, "people": sorted(people.values(), key=lambda x: -x["amount"]),
                "total": money_float(sum(p["amount"] for p in people.values())),
                "amount_complete": all(s["amount_complete"] for s in stores),
                "note": "以下合计为已算部分；尚有未分配或工资口径待确认时不代表最终应结算额。已结账月份固定原版本。"}

    app.include_router(router)
    return actor
