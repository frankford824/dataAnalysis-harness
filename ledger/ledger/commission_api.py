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
import threading
import time
import uuid
from decimal import Decimal
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse

import polars as pl
from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from . import commission_catalog, commission_batch, commission_confirm, commission_profit, commission_reports
from .commission_registry import Registry, RegistryError, RevisionConflict, json_text, local_time, now
from .money import money_float


class Change(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    expected_revision: int = 0


class SchemeChange(Change):
    store_id: str
    product_id: str
    body: dict
    publish: bool = False


class PayoutConfirmationChange(BaseModel):
    store_id: str
    period: str
    run_id: int = Field(gt=0)
    source_sha: str = Field(min_length=64, max_length=64)
    expected_confirmation_id: str = ""
    payouts: list[dict] = Field(default_factory=list, max_length=100)
    no_payout: bool = False
    reason: str = Field(min_length=1, max_length=500)


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


class ReportSelection(BaseModel):
    start: str
    end: str
    store_ids: list[str] = Field(default_factory=list, max_length=2000)
    person_ids: list[str] = Field(default_factory=list, max_length=2000)
    run_ids: list[int] | None = Field(default=None, max_length=240000)
    fingerprint: str = ""
    view: str = ""
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)
    presentation: bool = False


class ProfitExclusionChange(BaseModel):
    store_id: str
    period: str
    person_id: str
    run_id: int = Field(gt=0)
    source_sha: str = Field(min_length=64, max_length=64)
    excluded_product_ids: list[str] = Field(default_factory=list, max_length=5000)
    note: str = Field(min_length=1, max_length=500)


class StoreMemberChange(BaseModel):
    store_id: str
    person_id: str
    duty: str = 'produce'
    leader_id: str = ''
    valid_from: str = ''
    valid_to: str = ''
    reason: str = Field(min_length=1, max_length=500)


class StoreMemberItem(BaseModel):
    person_id: str
    duty: str = 'produce'
    leader_id: str = ''
    valid_from: str = ''
    valid_to: str = ''


class StoreMemberBatchChange(BaseModel):
    store_id: str = ''
    store_ids: list[str] = Field(default_factory=list, max_length=200)
    members: list[StoreMemberItem] = Field(min_length=1, max_length=200)
    valid_from: str = ''
    valid_to: str = ''
    reason: str = Field(min_length=1, max_length=500)


class OrgMoveChange(BaseModel):
    person_ids: list[str] = Field(min_length=1, max_length=200)
    parent_id: str = ''
    reason: str = Field(min_length=1, max_length=500)


class OrgStoresChange(BaseModel):
    person_id: str
    store_ids: list[str] = Field(default_factory=list, max_length=2000)
    reason: str = Field(min_length=1, max_length=500)


class OrgInferApply(BaseModel):
    items: list[dict] = Field(default_factory=list, max_length=500)
    reason: str = Field(default="一键按店铺理顺架构", min_length=1, max_length=500)


class FillHierarchyChange(BaseModel):
    store_id: str = ''
    product_id: str = ''
    store_ids: list[str] = Field(default_factory=list, max_length=200)
    product_ids: list[str] = Field(default_factory=list, max_length=500)
    allocations: list[dict] = Field(default_factory=list)
    valid_from: str = ''
    apply: bool = False
    replace: bool = False
    reason: str = Field(default='按组织补上级抽成', min_length=1, max_length=500)


class SettlementCreate(BaseModel):
    start: str
    end: str
    store_ids: list[str] = Field(default_factory=list, max_length=2000)
    person_ids: list[str] = Field(default_factory=list, max_length=2000)
    run_ids: list[int] = Field(min_length=1, max_length=240000)
    fingerprint: str = Field(min_length=1, max_length=128)
    note: str = Field(min_length=1, max_length=2000)


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
                    if value[:1] in "=+@-\t\r\n" or column.endswith("_id") or column in {"product_id", "宝贝ID", "人员ID", "工号"}:
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


def install(app, workspace, model, model_root: Path | None = None):
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

    def _store_member_rows(registry, store_id, at=''):
        from .commission_registry import member_active_at
        stamp = local_time(at) if at else ''
        segments = {}
        for row in registry.store_members(store_id):
            segments.setdefault(row['person_id'], []).append(row)
        saved = {}
        for pid, rows in segments.items():
            current = next((row for row in rows if member_active_at(row, stamp)), rows[-1] if not stamp else None)
            if current:
                saved[pid] = current
        with registry.connect() as conn:
            rows = conn.execute("""
                SELECT json_extract(a.value,'$.person_id') pid,
                       json_extract(a.value,'$.role') role,
                       count(*) n
                FROM scheme s JOIN scheme_version v ON v.id=s.active_version
                JOIN json_each(v.body,'$.segments') seg
                JOIN json_each(seg.value,'$.allocations') a
                WHERE s.store_id=?
                GROUP BY pid, role ORDER BY n DESC""", (store_id,)).fetchall()
        role_counts = {}
        for r in rows:
            pid = r['pid']
            role_counts.setdefault(pid, []).append({'role': r['role'], 'count': r['n']})
        suggestions = {}
        for pid, roles in role_counts.items():
            top_role = roles[0]['role'] if roles else ''
            suggestions[pid] = 'cut' if top_role in ('组长', '高级组长') else 'produce'
        roster = {p['id']: p for p in registry.people()}
        members = []
        seen = set()
        for pid in list(saved) + [pid for pid in role_counts if pid not in saved]:
            if pid in seen:
                continue
            seen.add(pid)
            s = saved.get(pid)
            members.append({
                'store_id': store_id,
                'person_id': pid,
                'person_name': roster.get(pid, {}).get('name', ''),
                'duty': s['duty'] if s else suggestions.get(pid, 'produce'),
                'leader_id': s.get('leader_id', '') if s else '',
                'leader_name': roster.get(s.get('leader_id', '') if s else '', {}).get('name', ''),
                'revision': s['revision'] if s else 0,
                'confirmed': s is not None,
                'suggested_duty': suggestions.get(pid, 'produce'),
                'valid_from': s.get('valid_from', '') if s else '',
                'valid_to': s.get('valid_to', '') if s else '',
                'segments': [
                    {'valid_from': row['valid_from'], 'valid_to': row['valid_to'],
                     'duty': row['duty'], 'leader_id': row.get('leader_id', '')}
                    for row in segments.get(pid, [])
                ],
            })
        return members

    @router.get("/store-members")
    def store_members(store_id: str = '', store_ids: list[str] = Query(default=[]), at: str = ''):
        ids = [sid for sid in ([store_id] if store_id else []) + list(store_ids) if sid]
        if not ids:
            raise RegistryError("请选择店铺")
        registry = reg()
        members = []
        for sid in dict.fromkeys(ids):
            members.extend(_store_member_rows(registry, sid, at))
        return {"members": members}

    @router.post("/store-members")
    def save_store_member(change: StoreMemberChange, request: Request):
        return reg().save_store_member(
            change.store_id, change.person_id, change.duty,
            change.leader_id, actor(request)["id"], change.reason,
            valid_from=change.valid_from, valid_to=change.valid_to)

    @router.post("/store-members/batch")
    def save_store_members_batch(change: StoreMemberBatchChange, request: Request):
        store_ids = [sid for sid in ([change.store_id] if change.store_id else []) + list(change.store_ids) if sid]
        if not store_ids:
            raise RegistryError("请选择店铺")
        registry = reg()
        acting = actor(request)["id"]
        saved = []
        for sid in dict.fromkeys(store_ids):
            for item in change.members:
                saved.append(registry.save_store_member(
                    sid, item.person_id, item.duty,
                    item.leader_id, acting, change.reason,
                    valid_from=item.valid_from or change.valid_from,
                    valid_to=item.valid_to or change.valid_to))
        return {"saved": saved, "count": len(saved), "stores": len(set(store_ids))}

    @router.get("/org/tree")
    def org_tree():
        names = {s.id: s.name for s in model().stores}
        return reg().org_tree(names)

    @router.post("/org/person")
    def org_person_save(change: PersonChange, request: Request):
        return reg().person_save(change.person, actor(request)["id"], change.reason, change.expected_revision)

    @router.post("/org/move")
    def org_move(change: OrgMoveChange, request: Request):
        return {"moved": reg().org_move(change.person_ids, change.parent_id, actor(request)["id"], change.reason)}

    @router.get("/org/stores")
    def org_stores(person_id: str = "", include_descendants: bool = False):
        names = {s.id: s.name for s in model().stores}
        rows = reg().org_stores(person_id, include_descendants=include_descendants)
        stores = [{"person_id": row["person_id"], "store_id": row["store_id"],
                   "store_name": names.get(row["store_id"], row["store_id"])} for row in rows]
        return {"stores": stores}

    @router.post("/org/stores")
    def save_org_stores(change: OrgStoresChange, request: Request):
        known = {s.id for s in model().stores}
        unknown = [sid for sid in change.store_ids if sid not in known]
        if unknown:
            raise RegistryError("有尚未登记的店铺，无法分配")
        return reg().save_org_stores(change.person_id, change.store_ids, actor(request)["id"], change.reason)

    @router.get("/org/infer")
    def org_infer_preview():
        names = {s.id: s.name for s in model().stores}
        return reg().infer_org_hierarchy(names)

    @router.post("/org/infer/apply")
    def org_infer_apply(payload: OrgInferApply, request: Request):
        applied = 0
        act = actor(request)["id"]
        # Group by target parent_id for efficient org_move calls
        by_parent: dict[str, list[str]] = {}
        for item in payload.items:
            pid = item.get("person_id")
            parent_id = item.get("parent_id")
            if pid and parent_id:
                by_parent.setdefault(parent_id, []).append(pid)
        for parent_id, pids in by_parent.items():
            res = reg().org_move(pids, parent_id, act, payload.reason)
            applied += len(res)
        return {"applied": applied}

    @router.post("/org/fill-hierarchy")
    def fill_hierarchy(change: FillHierarchyChange, request: Request):
        if change.allocations and not change.store_id and not change.store_ids:
            merged = reg().hierarchy_allocations(change.allocations)
            added = [line for line in merged if line.get("source") == "hierarchy"
                     and line["person_id"] not in {a.get("person_id") for a in change.allocations}]
            return {"allocations": merged, "added": added, "count": 1, "applied": False}
        store_ids = [sid for sid in ([change.store_id] if change.store_id else []) + list(change.store_ids) if sid]
        product_ids = [pid for pid in ([change.product_id] if change.product_id else []) + list(change.product_ids) if pid]
        if not change.valid_from:
            raise RegistryError("请填写生效时间")
        return reg().fill_hierarchy(
            store_ids=store_ids, product_ids=product_ids, valid_from=change.valid_from,
            actor=actor(request)["id"], reason=change.reason,
            apply=change.apply, replace=change.replace)

    @router.post("/catalog/refresh")
    def refresh_catalog(request: Request):
        return reg().enqueue("catalog", {}, actor(request)["id"])

    @router.get("/settings")
    async def settings(store_id: str = "", search: str = "", state: str = "", after: str = "",
                 limit: int = Query(60, ge=1, le=500), person_id: str = "",
                 store_ids: list[str] = Query(default=[]), person_ids: list[str] = Query(default=[]),
                 include_total: bool = True):
        return await run_in_threadpool(
            commission_catalog.settings, reg(), store_id=store_id, search=search, state=state,
            after=after, limit=limit, person_id=person_id, store_ids=store_ids,
            person_ids=person_ids, include_total=include_total)

    @router.post("/settings/preview")
    async def settings_preview(request: Request):
        acting = actor(request)
        return await run_in_threadpool(commission_batch.preview, reg(), model(), await request.json(), acting["id"])

    @router.post("/settings/apply/{batch_id}")
    def settings_apply(batch_id: str, request: Request):
        return commission_batch.apply(reg(), model(), batch_id, actor(request)["id"])

    @router.post("/settings/import-preview")
    async def settings_import(request: Request, file: UploadFile = File(...)):
        acting = actor(request)
        raw = await file.read(20*1024*1024+1)
        parsed = await run_in_threadpool(commission_batch.parse_excel, raw, Path(file.filename or '提成.xlsx').name, model())
        if parsed.get('errors'):
            return parsed
        registry = reg()
        original = registry.root / 'batch-inputs' / (parsed['source']['sha256']+'.xlsx')
        original.parent.mkdir(parents=True, exist_ok=True)
        if not original.exists(): original.write_bytes(raw)
        return await run_in_threadpool(commission_batch.preview, registry, model(), parsed, acting['id'])

    @router.get("/people/summary")
    def people_summary():
        from datetime import datetime, timezone, timedelta
        moment = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None).isoformat(timespec='seconds')
        with reg().connect() as conn:
            counts = {r['person_id']:dict(r) for r in conn.execute("""
                SELECT json_extract(a.value,'$.person_id') person_id,
                       count(DISTINCT s.id) products,count(DISTINCT s.store_id) stores
                FROM scheme s JOIN scheme_version v ON v.id=s.active_version
                JOIN json_each(v.body,'$.segments') t JOIN json_each(t.value,'$.allocations') a
                WHERE json_extract(t.value,'$.valid_from')<=?
                  AND (coalesce(json_extract(t.value,'$.valid_to'),'')='' OR json_extract(t.value,'$.valid_to')>?)
                GROUP BY person_id""", (moment,moment))}
            people = [{**dict(r), 'products':counts.get(r['id'],{}).get('products',0),
                       'stores':counts.get(r['id'],{}).get('stores',0)} for r in conn.execute('SELECT * FROM person ORDER BY archived,name,id')]
        return {'people':people}

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

    @router.get('/unassigned')
    def unassigned(store_id: str, period: str):
        m = model()
        if store_id not in {store.id for store in m.stores}:
            raise RegistryError('请选择已登记店铺')
        commission_reports.months(period, period)
        state = workspace().state(store_id, period)
        if not state or not state.result:
            raise RegistryError('本店本月尚无核算记录')
        c = state.result.get('commission') or {}
        links = [product for product in c.get('products') or []
                 if product.get('unassigned') and
                 re.fullmatch(r'\d{9,20}', str(product.get('product_id') or ''))]
        ids = [product['product_id'] for product in links]
        schemes = {}
        if ids:
            with reg().connect() as conn:
                for offset in range(0, len(ids), 500):
                    chunk = ids[offset:offset + 500]
                    for row in conn.execute('SELECT id,product_id,revision FROM scheme '
                                            'WHERE store_id=? AND product_id IN ('
                                            + ','.join('?' for _ in chunk) + ')',
                                            (store_id, *chunk)):
                        schemes[row['product_id']] = dict(row)
        ranked = sorted(links, key=lambda product: -float(product.get('base') or 0))
        without_id = sum(int(product.get('sub_orders') or 0)
                         for product in c.get('products') or []
                         if product.get('unassigned') and not
                         re.fullmatch(r'\d{9,20}', str(product.get('product_id') or '')))
        return {'store_id': store_id, 'store': m.store(store_id).name,
                'period': period, 'run_id': state.run_id,
                'orders': c.get('unassigned_orders') or 0,
                'base': c.get('unassigned_base') or 0,
                'link_count': len(links), 'without_product': without_id,
                'links': [{'store_id': store_id,
                           'product_id': product['product_id'],
                           'product_name': product.get('product_name') or '',
                           'base': product.get('base') or 0,
                           'sub_orders': product.get('sub_orders') or 0,
                           'scheme_id': schemes.get(product['product_id'], {}).get('id'),
                           'revision': schemes.get(product['product_id'], {}).get('revision', 0)}
                          for product in ranked]}

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

    report_cache = {}
    report_cache_lock = threading.Lock()

    def report_watermark():
        row = workspace().conn.execute("SELECT ifnull(max(id),0) FROM run").fetchone()
        return int(row[0])

    def report_needs_product_rates(selection: ReportSelection, view: str | None = None):
        effective = selection.view if view is None else view
        return effective in ('', 'store_people')

    def report_cache_key(selection: ReportSelection, need_product_rates: bool):
        return (selection.start, selection.end, tuple(selection.store_ids or []),
                tuple(selection.person_ids or []), tuple(selection.run_ids or []),
                report_watermark(), need_product_rates)

    def clear_report_cache():
        with report_cache_lock:
            report_cache.clear()

    def report_result(selection: ReportSelection, *, view: str | None = None):
        need_product_rates = report_needs_product_rates(selection, view)
        key = report_cache_key(selection, need_product_rates)
        now_ts = time.time()
        with report_cache_lock:
            hit = report_cache.get(key)
            if hit and now_ts - hit[0] < 45 and (not selection.fingerprint or selection.fingerprint == hit[2]):
                report, fingerprint = hit[1], hit[2]
                if selection.fingerprint and selection.fingerprint != fingerprint:
                    raise RevisionConflict("计算状态或人员信息已变化，请重新查询后导出")
                return {**report, "fingerprint": fingerprint}
        report = commission_reports.build(workspace(), reg(), model(), selection.start, selection.end,
                                          selection.store_ids, selection.person_ids, selection.run_ids,
                                          model_root=model_root, need_product_rates=need_product_rates)
        fingerprint = hashlib.sha256(json_text(report).encode()).hexdigest()
        if selection.fingerprint and selection.fingerprint != fingerprint:
            raise RevisionConflict("计算状态或人员信息已变化，请重新查询后导出")
        with report_cache_lock:
            report_cache[key] = (now_ts, report, fingerprint)
            if len(report_cache) > 8:
                oldest = min(report_cache, key=lambda item: report_cache[item][0])
                report_cache.pop(oldest, None)
        return {**report, "fingerprint": fingerprint}

    def report_payload(selection: ReportSelection):
        report = report_result(selection)
        if not selection.view:
            return report
        if selection.view not in commission_reports.COLUMNS:
            raise RegistryError("请选择查看方式")
        rows = report['rows' if selection.view == 'breakdown' else selection.view]
        visible_stores = (len({row['store_id'] for row in rows if row['kind'] == 'store'})
                          if selection.view == 'store_people' else len(report['stores']))
        return {k:v for k,v in report.items() if k not in {'people','stores','rows','coverage','teams'}} | {
            'items': rows[selection.offset:selection.offset+selection.limit], 'count': len(rows),
            'people_count':len(report['people']), 'store_count':visible_stores,
            'view':selection.view, 'offset':selection.offset,
            'run_scopes':[{'run_id':row['finance_run'],'store_id':row['store_id'],'period':row['period']} for row in report['coverage'] if row['finance_run'] is not None],
            'confirmation_scopes':[{
                'run_id':row['finance_run'],'store_id':row['store_id'],
                'store':row['store'],'period':row['period'],'status':row['status'],
                'has_result':row['has_result'],'amount':money_float(row['selected_amount'])
                if row['selected_amount'] is not None else None,
                'unassigned_orders':row.get('unassigned_orders') or 0,
            } for row in report['coverage'] if row['finance_run'] is not None],
            'person_confirmation_scopes':[{
                'person_id':row['person_id'],'person':row['person'],
                'run_id':row['finance_run'],'store_id':row['store_id'],
                'store':row['store'],'period':row['period'],'status':row['status'],
                'amount':row['amount'],
            } for row in report['rows'] if row.get('finance_run') is not None],
            'assignment_gaps':[{'store_id':row['store_id'],'store':row['store'],'period':row['period'],
                                'orders':row['unassigned_orders'],'base':row.get('unassigned_base')}
                               for row in report['coverage'] if row.get('unassigned_orders')
                               and '试算' in row['status']],
        }

    @router.post("/reports/query")
    async def report_query(selection: ReportSelection):
        return await run_in_threadpool(report_payload, selection)

    def profit_scope(store_id, period, person_id, run_id):
        if store_id not in {store.id for store in model().stores}:
            raise RegistryError('请选择已登记店铺')
        commission_reports.months(period, period)
        if not person_id:
            raise RegistryError('请选择人员')
        if run_id < 1:
            raise RegistryError('请选择本次核算')
        return model().store(store_id).name

    @router.get('/profit-composition')
    def profit_composition(store_id: str, period: str, person_id: str, run_id: int):
        store_name = profit_scope(store_id, period, person_id, run_id)
        duties = commission_reports.store_member_duties(reg(), store_id, period)
        return commission_profit.compose(reg(), store_id, period, person_id, run_id,
                                         store_name=store_name, duties=duties)

    @router.post('/profit-exclusions')
    def profit_exclusion_save(change: ProfitExclusionChange, request: Request):
        store_name = profit_scope(change.store_id, change.period, change.person_id, change.run_id)
        return commission_profit.save(
            reg(), store_id=change.store_id, period=change.period,
            person_id=change.person_id, run_id=change.run_id,
            source_sha=change.source_sha,
            excluded_product_ids=change.excluded_product_ids,
            note=change.note, actor=actor(request)['id'], store_name=store_name)

    @router.get('/payout-confirmations/context')
    def payout_confirmation_context(store_id: str, period: str, run_id: int):
        return commission_confirm.context(workspace(), reg(), model(),
                                          store_id, period, expected_run=run_id)

    @router.post('/payout-confirmations')
    def payout_confirmation_save(change: PayoutConfirmationChange, request: Request):
        result = commission_confirm.confirm(
            workspace(), reg(), model(), store_id=change.store_id,
            period=change.period, run_id=change.run_id,
            source_sha=change.source_sha,
            expected_confirmation_id=change.expected_confirmation_id,
            payouts=change.payouts, no_payout=change.no_payout,
            reason=change.reason, actor=actor(request)['id'])
        clear_report_cache()
        return result

    @router.post("/export/reports/{kind}")
    def report_export(kind: str, selection: ReportSelection):
        if kind not in commission_reports.COLUMNS:
            raise RegistryError("请选择导出类型")
        report = report_result(selection, view=kind)
        if selection.presentation:
            columns, rows = commission_reports.business_export(report, kind)
            return csv_response(f"commission-{kind}-{selection.start}-{selection.end}.csv", columns, rows)
        return csv_response(f"commission-{kind}-{selection.start}-{selection.end}.csv",
                            commission_reports.COLUMNS[kind], commission_reports.export_rows(report, kind))

    def settlement_row(row):
        result = dict(row)
        result["selection"] = json.loads(result.pop("selection_json"))
        result["run_ids"] = json.loads(result.pop("run_ids_json"))
        snapshot = json.loads(result.pop("report_json"))
        result["total"] = money_float(Decimal(result["total"]))
        result["people_count"] = len(snapshot.get("people") or [])
        result["store_count"] = len(snapshot.get("stores") or [])
        result["missing_periods"] = snapshot.get("missing_periods", 0)
        result["trial_periods"] = snapshot.get("trial_periods", 0)
        return result

    @router.get("/settlements")
    def settlements(start: str = "", end: str = "", limit: int = Query(30, ge=1, le=200)):
        with reg().connect() as conn:
            rows = [settlement_row(r) for r in conn.execute(
                "SELECT * FROM settlement WHERE (?='' OR end_period>=?) AND (?='' OR start_period<=?) "
                "ORDER BY at DESC LIMIT ?", (start, start, end, end, limit))]
        return {"settlements": rows}

    @router.get("/settlements/{settlement_id}")
    def settlement_detail(settlement_id: str):
        with reg().connect() as conn:
            row = conn.execute("SELECT * FROM settlement WHERE id=?", (settlement_id,)).fetchone()
        if not row:
            raise HTTPException(404, "没有这次结算记录")
        saved = settlement_row(row)
        selection = saved["selection"]
        try:
            current = commission_reports.build(workspace(), reg(), model(), selection["start"], selection["end"],
                                               selection.get("store_ids"), selection.get("person_ids"),
                                               model_root=model_root)
            current_total = current.get("total")
            saved["current_total"] = current_total
            saved["difference"] = (None if current_total is None else
                                   money_float(Decimal(str(current_total)) - Decimal(str(saved["total"]))))
            saved["current_error"] = ""
        except RegistryError as exc:
            # Current personnel or store configuration can disappear years later. That must
            # never make the immutable settlement snapshot itself unreadable.
            saved["current_total"] = None
            saved["difference"] = None
            saved["current_error"] = str(exc)
        saved["report"] = json.loads(dict(row)["report_json"])
        return saved

    @router.get("/export/settlements/{settlement_id}")
    def settlement_export(settlement_id: str):
        with reg().connect() as conn:
            row = conn.execute("SELECT report_json,start_period,end_period FROM settlement WHERE id=?",
                               (settlement_id,)).fetchone()
        if not row:
            raise HTTPException(404, "没有这次结算记录")
        report = json.loads(row["report_json"])
        columns, rows = commission_reports.business_export(report, "breakdown")
        return csv_response(f"commission-settlement-{row['start_period']}-{row['end_period']}.csv", columns, rows)

    @router.post("/settlements")
    def settle_commission(change: SettlementCreate, request: Request):
        who = actor(request)
        with reg().connect() as conn:
            existing = conn.execute("SELECT * FROM settlement WHERE fingerprint=?", (change.fingerprint,)).fetchone()
        if existing:
            return {**settlement_row(existing), "duplicate": True}
        selection = ReportSelection(start=change.start, end=change.end, store_ids=change.store_ids,
                                    person_ids=change.person_ids, run_ids=change.run_ids,
                                    fingerprint=change.fingerprint)
        report = report_result(selection)
        if report.get("total") is None:
            raise RegistryError("当前范围还没有可结算的提成金额")
        if report.get("missing_periods") or report.get("trial_periods"):
            raise RegistryError("仍有未出金额或待核对账期，不能确认员工结算")
        if any(not row.get("has_result") or row.get("status") in {"试算", "待核价"}
               for row in report.get("coverage", [])):
            raise RegistryError("仍有未完成的店铺账期，不能确认员工结算")
        stored_selection = {"start": change.start, "end": change.end,
                            "store_ids": sorted(set(change.store_ids)),
                            "person_ids": sorted(set(change.person_ids))}
        settlement_id = str(uuid.uuid4())
        at = now()
        snapshot = {k: v for k, v in report.items() if k != "fingerprint"}
        record = {"id": settlement_id, "at": at, "actor": who["id"], "note": change.note.strip(),
                  "start_period": change.start, "end_period": change.end,
                  "selection_json": json_text(stored_selection), "run_ids_json": json_text(change.run_ids),
                  "fingerprint": change.fingerprint, "total": str(report["total"]),
                  "report_json": json_text(snapshot)}
        with reg().transaction() as conn:
            existing = conn.execute("SELECT * FROM settlement WHERE fingerprint=?", (change.fingerprint,)).fetchone()
            if existing:
                return {**settlement_row(existing), "duplicate": True}
            conn.execute("INSERT INTO settlement VALUES(:id,:at,:actor,:note,:start_period,:end_period,"
                         ":selection_json,:run_ids_json,:fingerprint,:total,:report_json)", record)
            Registry.audit(conn, who["id"], "commission.settle", settlement_id, change.note.strip(), None,
                           {"selection": stored_selection, "run_ids": change.run_ids,
                            "fingerprint": change.fingerprint, "total": report["total"]})
        return settlement_row(record)

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
    def export_settings(store_id: str = "", search: str = "", state: str = "", person_id: str = "",
                        store_ids: list[str] = Query(default=[]), person_ids: list[str] = Query(default=[])):
        names = {s.id:s.name for s in model().stores}
        labels = {"enabled":"提成中","disabled":"不提成","pending":"未设置","scheduled":"待生效","expired":"已到期"}
        def rows():
            for row in commission_catalog.iter_settings(reg(), store_id=store_id, search=search, state=state, person_id=person_id, store_ids=store_ids, person_ids=person_ids):
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
                           "pricing_pending_count": c.get("pricing_pending_count", 0),
                           "amount_complete": c.get("amount_complete", False), "wage_pending_orders": c.get("wage_pending_orders", 0),
                           "registry_revision": c.get("registry_revision")})
            for p in c.get("people", []):
                pid = p.get("person_id") or "legacy:" + hashlib.sha256(p["person"].encode()).hexdigest()[:24]
                entry = people.setdefault(pid, {"person_id": pid, "person": p["person"], "amount": Decimal(0), "stores": 0})
                entry["amount"] += Decimal(str(p["amount"]))
                entry["stores"] += 1
        return {"stores": stores, "people": sorted(people.values(), key=lambda x: -x["amount"]),
                "total": (None if any(s["pricing_pending_count"] for s in stores)
                          else money_float(sum(p["amount"] for p in people.values()))),
                "calculated_total": money_float(sum(p["amount"] for p in people.values())),
                "amount_complete": all(s["amount_complete"] for s in stores),
                "note": "以下合计为已算部分；尚有未分配或工资口径待确认时不代表最终应结算额。已结账月份固定原版本。"}

    app.include_router(router)
    return actor
