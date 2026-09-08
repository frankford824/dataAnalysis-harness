"""Lossless import of the company's historical workbook into reviewable versions.

Column names are matched exactly. In particular, 运营组长 must never match
高级运营组长. The original workbook and every row, including rejected rows, stay
in the import batch. Only unambiguous complete groups can be published.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from python_calamine import CalamineWorkbook

from .commission_registry import Registry, RegistryError, json_text, local_time, now, rate

ROLE_COLUMNS = (
    ("高级组长", "高级运营组长姓名（手动填写）", "高级运营组长点数（手动填写）"),
    ("组长", "运营组长姓名（手动填写）", "运营组长点数（手动填写）"),
    ("运营1", "运营1姓名（手动填写）", "运营1点数（手动填写）"),
    ("运营2", "运营2姓名（手动填写）", "运营2点数（手动填写）"),
    ("运营3", "运营3姓名（手动填写）", "运营3点数（手动填写）"),
)
PLACEHOLDERS = {"无人", "赠品", "补差价", "QT", "运费", "邮费", "打气筒", "配件", "好评返现", "/"}


def _header(value):
    return re.sub(r"\s+", "", str(value or "")).replace("(", "（").replace(")", "）")


def _text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def read_workbook(raw: bytes) -> tuple[str, list[dict], list[dict]]:
    if len(raw) > 40 * 1024 * 1024:
        raise RegistryError("导入文件超过40MB，请拆分后导入")
    import zipfile
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if sum(z.file_size for z in archive.infolist()) > 512 * 1024 * 1024:
            raise RegistryError("工作簿解压后超过512MB")
    wb = CalamineWorkbook.from_filelike(io.BytesIO(raw))
    sheets = []
    chosen = None
    rows = []
    for name in wb.sheet_names:
        sheet = wb.get_sheet_by_name(name)
        table = sheet.to_python()
        nonempty = sum(any(c is not None and c != "" for c in row) for row in table)
        sheets.append({"sheet": name, "nonempty_rows": nonempty})
        if not table:
            continue
        headers = [_header(v) for v in table[0]]
        if _header("宝贝编码（ID）（手动填写）") not in headers or _header(ROLE_COLUMNS[0][1]) not in headers:
            continue
        if chosen is not None:
            raise RegistryError("存在多张主提成表，请先明确主表")
        chosen = name
        by_header = {h: i for i, h in enumerate(headers) if h}
        if len(by_header) != sum(bool(h) for h in headers):
            raise RegistryError("主表有重复表头，不能猜测列位置")
        needed = [n for _, person, point in ROLE_COLUMNS for n in (person, point)]
        if any(_header(h) not in by_header for h in needed):
            raise RegistryError("主表缺少完整的5组人员和点数列")
        def cell(row, name):
            i = by_header.get(_header(name))
            return row[i] if i is not None and i < len(row) else None
        # Calamine may trim the leading empty A column; sheet.start records the
        # real row offset and exact header matching is independent of that trim.
        start_row = sheet.start[0] if sheet.start else 0
        for rn, row in enumerate(table[1:], start=start_row + 2):
            pid_value = cell(row, "宝贝编码（ID）（手动填写）")
            if pid_value is None or pid_value == "":
                continue
            shares = [{"role": role, "person": _text(cell(row, person)),
                       "rate_raw": _text(cell(row, point))} for role, person, point in ROLE_COLUMNS]
            rows.append({"row_no": rn, "sheet": name,
                         "product_id": _text(pid_value),
                         "numeric_id_unsafe": isinstance(pid_value, (float, int)) and abs(pid_value) >= 2 ** 53,
                         "source_store": _text(cell(row, "归属店铺（下拉选择）")),
                         "source_shop_no": _text(cell(row, "店铺编号")),
                         "source_date": _text(cell(row, "新增/变更日期（格式年月日）")),
                         "total_rate_raw": _text(cell(row, "小计点数（不填写）")),
                         "kind": _text(cell(row, "类型（不填写）")),
                         "note": _text(cell(row, "备注（运营专用）")),
                         "bp_note": _text(cell(row, "备注（BP专用无需填写）")),
                         "allocations": shares})
    if chosen is None:
        raise RegistryError("没有识别到含5组人员点数的主提成表")
    return chosen, rows, sheets


def stage(registry: Registry, model, raw: bytes, filename: str, effective_from: str, actor: str):
    cutover = local_time(effective_from)
    sha = hashlib.sha256(raw).hexdigest()
    with registry.connect() as conn:
        existing = conn.execute("SELECT * FROM import_batch WHERE sha=? AND effective_from=?", (sha, cutover)).fetchone()
        if existing:
            return {**dict(existing), "summary": json.loads(existing["summary"]), "reused": True}
    sheet, rows, sheets = read_workbook(raw)
    names = defaultdict(set)
    for s in model.stores:
        for name in (s.name, *s.aliases):
            names[name].add(s.id)
    with registry.connect() as conn:
        catalog = {(r["store_id"], r["product_id"]): dict(r) for r in conn.execute("SELECT * FROM catalog")}
        existing_schemes = {(r["store_id"], r["product_id"]): dict(r) for r in conn.execute("SELECT * FROM scheme")}
    id_stores = defaultdict(set)
    for sid, pid in catalog:
        if not sid.startswith("unmapped:"):
            id_stores[pid].add(sid)
    grouped = defaultdict(list)
    for row in rows:
        issues = []
        pid = row["product_id"]
        candidates = names.get(row["source_store"], set())
        sid = next(iter(candidates)) if len(candidates) == 1 else ""
        if not re.fullmatch(r"\d{9,20}", pid) or row["numeric_id_unsafe"]:
            issues.append("平台宝贝ID无效或数值精度不足")
        if not sid:
            inferred = id_stores.get(pid, set())
            if len(inferred) == 1 and row["source_store"] in {"", "无店铺"}:
                sid = next(iter(inferred))
                issues.append("店铺由目录推断，需确认")
            else:
                issues.append("店铺归属待确认")
        elif id_stores.get(pid) and sid not in id_stores[pid]:
            issues.append("原表店铺与目录不一致")
        total = "0"
        allocations = []
        try:
            total = rate(row["total_rate_raw"])
            for line in row["allocations"]:
                person = line["person"]
                point = rate(line["rate_raw"] or "0")
                if Decimal(point) and not person:
                    issues.append("有点数但缺少人员")
                if person and not line["rate_raw"]:
                    issues.append("人员点数为空")
                if person and Decimal(point):
                    if person in PLACEHOLDERS or "离职" in person or "小组" in person:
                        issues.append("占位标签不能作为提成人员")
                    if re.search(r"[0-9]$", person):
                        issues.append("姓名带方案后缀，需核对人员身份")
                    allocations.append({"person": person, "rate": point, "role": line["role"]})
            if sum((Decimal(a["rate"]) for a in allocations), Decimal(0)) != Decimal(total):
                issues.append("个人点数之和不等于总点数")
        except RegistryError as exc:
            issues.append(str(exc))
        row.update(store_id=sid, total_rate=total, resolved_allocations=allocations,
                   product_name=catalog.get((sid, pid), {}).get("product_name", ""), issues=issues,
                   mode="exclude" if Decimal(total) == 0 else "distribute")
        row["amount_hold"] = "wage_pending" if "工资" in row["bp_note"] and Decimal(total) > 0 else ""
        existing = existing_schemes.get((sid, pid))
        row["existing_revision"] = existing["revision"] if existing else 0
        if existing and existing["draft_version"]:
            row["issues"].append("该商品已有未发布草稿，需先比较版本")
        grouped[(sid, pid)].append(row)
    for (sid, pid), group in grouped.items():
        if not sid:
            continue
        signatures = {json_text([r["resolved_allocations"], r["total_rate"], r["mode"]]) for r in group}
        if len(signatures) > 1:
            for row in group:
                row["issues"].append("同店同宝贝的原始方案冲突")
    batch_id = str(uuid.uuid4())
    folder = registry.root / "imports"
    folder.mkdir(exist_ok=True)
    original = folder / (sha + ".xlsx")
    if not original.exists():
        original.write_bytes(raw)
    counts = Counter("review" if r["issues"] else "ready" for r in rows)
    summary = {"rows": len(rows), **dict(counts), "sheet": sheet, "sheets": sheets,
               "people": len({a["person"] for r in rows for a in r["resolved_allocations"]}),
               "groups": len(grouped), "cutover": cutover}
    with registry.transaction() as conn:
        conn.execute("INSERT INTO import_batch VALUES(?,?,?,?,?,?,?,?)",
                     (batch_id, sha, Path(filename).name, now(), actor, cutover, "staged", json_text(summary)))
        conn.executemany("INSERT INTO import_row VALUES(?,?,?,?,?,?,?)", [
            (batch_id, r["row_no"], r["store_id"], r["product_id"], "review" if r["issues"] else "ready",
             json_text(r["issues"]), json_text(r)) for r in rows])
        registry.audit(conn, actor, "import.stage", batch_id, "原始提成方案登记", None,
                       {"sha": sha, "filename": Path(filename).name, "summary": summary})
    return {"id": batch_id, "sha": sha, "status": "staged", "summary": summary}


def activate(registry: Registry, batch_id: str, actor: str, reason: str) -> dict:
    with registry.transaction() as conn:
        batch = conn.execute("SELECT * FROM import_batch WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise RegistryError("没有这批导入记录")
        if batch["status"] == "activated":
            return {"id": batch_id, "reused": True, "summary": json.loads(batch["summary"])}
        rows = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM import_row WHERE batch_id=? AND status='ready' ORDER BY row_no", (batch_id,))]
        grouped = defaultdict(list)
        for row in rows:
            grouped[(row["store_id"], row["product_id"])].append(row)
        count = 0
        conflicts = 0
        for (sid, pid), group in grouped.items():
            existing = conn.execute("SELECT * FROM scheme WHERE store_id=? AND product_id=?", (sid, pid)).fetchone()
            expected = group[0].get("existing_revision", 0)
            if (existing["revision"] if existing else 0) != expected:
                conflicts += len(group)
                conn.executemany("UPDATE import_row SET status='review',issues=? WHERE batch_id=? AND row_no=?",
                                 [(json_text(["已经存在关系，需比较版本后调整"]), batch_id, r["row_no"]) for r in group])
                continue
            row = group[0]
            allocations = []
            for line in row["resolved_allocations"]:
                person_id = "legacy:" + hashlib.sha256(line["person"].encode()).hexdigest()[:24]
                if not conn.execute("SELECT 1 FROM person WHERE id=?", (person_id,)).fetchone():
                    person = {"id": person_id, "name": line["person"], "note": "历史原表姓名登记；外部账号身份未自动合并"}
                    conn.execute("INSERT INTO person(id,name,note) VALUES(?,?,?)", (person_id, person["name"], person["note"]))
                    registry.audit(conn, actor, "person.import", person_id, reason, None, person)
                allocations.append({"person_id": person_id, "role": line["role"], "rate": line["rate"]})
            body = {"product_name": row["product_name"], "source": {"batch_id": batch_id, "sha": batch["sha"],
                    "sheet": row["sheet"], "rows": [r["row_no"] for r in group]},
                    "segments": [{"valid_from": batch["effective_from"], "valid_to": "", "mode": row["mode"],
                                  "amount_hold": row.get("amount_hold", ""),
                                  "total_rate": row["total_rate"], "allocations": allocations}]}
            if existing and existing["active_version"]:
                old = json.loads(conn.execute("SELECT body FROM scheme_version WHERE id=?", (existing["active_version"],)).fetchone()[0])
                earlier = [{**s, "valid_to": min(s["valid_to"] or batch["effective_from"], batch["effective_from"])}
                           for s in old["segments"] if s["valid_from"] < batch["effective_from"]]
                body["segments"] = earlier + body["segments"]
            registry.save_scheme(sid, pid, body, actor, reason, expected=expected, publish=True, action="import", conn=conn)
            count += 1
        summary = {**json.loads(batch["summary"]), "activated_groups": count, "existing_relation_rows": conflicts}
        conn.execute("UPDATE import_batch SET status='activated',summary=? WHERE id=?", (json_text(summary), batch_id))
        registry.audit(conn, actor, "import.activate", batch_id, reason, dict(batch), summary)
        return {"id": batch_id, "summary": summary}
