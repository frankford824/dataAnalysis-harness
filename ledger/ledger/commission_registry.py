"""Versioned commission relationships. Operational facts remain owned by Order Console.

Every relationship revision and its audit event are committed together. A revision
contains a complete timeline for one (ledger store, platform listing); publishing
never destroys the previously published timeline or a financial close.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """

CREATE TABLE IF NOT EXISTS setting_batch (
 id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL,
 applied_at TEXT, result TEXT NOT NULL DEFAULT '{}'
);
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL);
INSERT OR IGNORE INTO meta VALUES(1,0);
CREATE TABLE IF NOT EXISTS person (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, employee_no TEXT NOT NULL DEFAULT '',
 external_user_id TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 1, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS scheme (
 id TEXT PRIMARY KEY, store_id TEXT NOT NULL, product_id TEXT NOT NULL,
 product_name TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 0,
 active_version TEXT, draft_version TEXT, UNIQUE(store_id,product_id)
);
CREATE TABLE IF NOT EXISTS scheme_version (
 id TEXT PRIMARY KEY, scheme_id TEXT NOT NULL REFERENCES scheme(id), revision INTEGER NOT NULL,
 recorded_at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
 reason TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(scheme_id,revision)
);
CREATE TRIGGER IF NOT EXISTS version_no_update BEFORE UPDATE ON scheme_version
 BEGIN SELECT RAISE(ABORT,'relationship history is immutable'); END;
CREATE TRIGGER IF NOT EXISTS version_no_delete BEFORE DELETE ON scheme_version
 BEGIN SELECT RAISE(ABORT,'relationship history is immutable'); END;
CREATE TABLE IF NOT EXISTS event (
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL,
 action TEXT NOT NULL, entity_id TEXT NOT NULL, reason TEXT NOT NULL,
 before_json TEXT NOT NULL, after_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS event_no_update BEFORE UPDATE ON event
 BEGIN SELECT RAISE(ABORT,'commission audit is immutable'); END;
CREATE TRIGGER IF NOT EXISTS event_no_delete BEFORE DELETE ON event
 BEGIN SELECT RAISE(ABORT,'commission audit is immutable'); END;
CREATE TABLE IF NOT EXISTS catalog (
 store_id TEXT NOT NULL, product_id TEXT NOT NULL, product_name TEXT NOT NULL,
 order_store_id TEXT NOT NULL, payload TEXT NOT NULL, refreshed_at TEXT NOT NULL,
 PRIMARY KEY(store_id,product_id)
);
CREATE TABLE IF NOT EXISTS external_person (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS import_batch (
 id TEXT PRIMARY KEY, sha TEXT NOT NULL, filename TEXT NOT NULL, at TEXT NOT NULL,
 actor TEXT NOT NULL, effective_from TEXT NOT NULL, status TEXT NOT NULL,
 summary TEXT NOT NULL, UNIQUE(sha,effective_from)
);
CREATE TABLE IF NOT EXISTS import_row (
 batch_id TEXT NOT NULL REFERENCES import_batch(id), row_no INTEGER NOT NULL,
 store_id TEXT NOT NULL, product_id TEXT NOT NULL, status TEXT NOT NULL,
 issues TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(batch_id,row_no)
);
CREATE TABLE IF NOT EXISTS calculation (
 id TEXT PRIMARY KEY, finance_run INTEGER NOT NULL, store_id TEXT NOT NULL,
 period TEXT NOT NULL, at TEXT NOT NULL, registry_revision INTEGER NOT NULL,
 path TEXT NOT NULL, sha TEXT NOT NULL, model_json TEXT NOT NULL,
 rules_json TEXT NOT NULL, summary_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS calculation_scope ON calculation(store_id,period,finance_run);
CREATE INDEX IF NOT EXISTS calculation_recent ON calculation(finance_run DESC);
CREATE TRIGGER IF NOT EXISTS calculation_no_update BEFORE UPDATE ON calculation
 BEGIN SELECT RAISE(ABORT,'commission calculation history is immutable'); END;
CREATE TRIGGER IF NOT EXISTS calculation_no_delete BEFORE DELETE ON calculation
 BEGIN SELECT RAISE(ABORT,'commission calculation history is immutable'); END;
CREATE TABLE IF NOT EXISTS settlement (
 id TEXT PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, note TEXT NOT NULL,
 start_period TEXT NOT NULL, end_period TEXT NOT NULL,
 selection_json TEXT NOT NULL, run_ids_json TEXT NOT NULL,
 fingerprint TEXT NOT NULL UNIQUE, total TEXT NOT NULL, report_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS settlement_recent ON settlement(at DESC);
CREATE TRIGGER IF NOT EXISTS settlement_no_update BEFORE UPDATE ON settlement
 BEGIN SELECT RAISE(ABORT,'commission settlement history is immutable'); END;
CREATE TRIGGER IF NOT EXISTS settlement_no_delete BEFORE DELETE ON settlement
 BEGIN SELECT RAISE(ABORT,'commission settlement history is immutable'); END;
CREATE TABLE IF NOT EXISTS payout_confirmation (
 id TEXT PRIMARY KEY, store_id TEXT NOT NULL, period TEXT NOT NULL,
 finance_run INTEGER NOT NULL, at TEXT NOT NULL, actor TEXT NOT NULL,
 reason TEXT NOT NULL, source_sha TEXT NOT NULL,
 payouts_json TEXT NOT NULL, trial_json TEXT NOT NULL,
 confirmed_total TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS payout_confirmation_scope
 ON payout_confirmation(store_id,period,finance_run,at DESC);
CREATE INDEX IF NOT EXISTS payout_confirmation_run
 ON payout_confirmation(finance_run,at DESC);
CREATE TRIGGER IF NOT EXISTS payout_confirmation_no_update BEFORE UPDATE ON payout_confirmation
 BEGIN SELECT RAISE(ABORT,'confirmed payout history is immutable'); END;
CREATE TRIGGER IF NOT EXISTS payout_confirmation_no_delete BEFORE DELETE ON payout_confirmation
 BEGIN SELECT RAISE(ABORT,'confirmed payout history is immutable'); END;
CREATE TABLE IF NOT EXISTS profit_exclusion (
 id TEXT PRIMARY KEY, store_id TEXT NOT NULL, period TEXT NOT NULL,
 person_id TEXT NOT NULL, finance_run INTEGER NOT NULL, at TEXT NOT NULL,
 actor TEXT NOT NULL, note TEXT NOT NULL, source_sha TEXT NOT NULL,
 excluded_json TEXT NOT NULL, included_profit TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS profit_exclusion_scope
 ON profit_exclusion(store_id,period,person_id,finance_run,at DESC);
CREATE TRIGGER IF NOT EXISTS profit_exclusion_no_update BEFORE UPDATE ON profit_exclusion
 BEGIN SELECT RAISE(ABORT,'profit exclusion history is immutable'); END;
CREATE TRIGGER IF NOT EXISTS profit_exclusion_no_delete BEFORE DELETE ON profit_exclusion
 BEGIN SELECT RAISE(ABORT,'profit exclusion history is immutable'); END;
CREATE TABLE IF NOT EXISTS store_member (
 id TEXT PRIMARY KEY, store_id TEXT NOT NULL, person_id TEXT NOT NULL,
 valid_from TEXT NOT NULL, valid_to TEXT NOT NULL DEFAULT '',
 duty TEXT NOT NULL DEFAULT 'produce',
 leader_id TEXT NOT NULL DEFAULT '',
 revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS operator (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, salt TEXT NOT NULL, password_hash TEXT NOT NULL,
 admin INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS session (digest TEXT PRIMARY KEY, operator_id TEXT NOT NULL,
 expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS pending (store_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
 next_attempt INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
 source_seq INTEGER NOT NULL DEFAULT 0, source_fingerprint TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS job (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, actor TEXT NOT NULL, at TEXT NOT NULL,
 status TEXT NOT NULL, payload TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS policy_version (
 id INTEGER PRIMARY KEY AUTOINCREMENT, store_id TEXT NOT NULL DEFAULT '',
 effective_from TEXT NOT NULL, recorded_at TEXT NOT NULL, actor TEXT NOT NULL,
 reason TEXT NOT NULL, body TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS policy_no_update BEFORE UPDATE ON policy_version
 BEGIN SELECT RAISE(ABORT,'commission policy history is immutable'); END;
CREATE TRIGGER IF NOT EXISTS policy_no_delete BEFORE DELETE ON policy_version
 BEGIN SELECT RAISE(ABORT,'commission policy history is immutable'); END;
"""
from .snapshot_store import SCHEMA as EVIDENCE_SCHEMA
SCHEMA += EVIDENCE_SCHEMA

_initialized: set[Path] = set()
_init_lock = threading.RLock()


class RegistryError(ValueError):
    pass


class RevisionConflict(RegistryError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def local_time(value: str, *, optional: bool = False) -> str:
    """Business times are Beijing wall times, stored in lexically sortable form."""
    if not value and optional:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo:
            parsed = parsed.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
        return parsed.isoformat(timespec="seconds")
    except (ValueError, TypeError) as exc:
        raise RegistryError("日期格式应为 YYYY-MM-DD 或 YYYY-MM-DDTHH:mm:ss（北京时间）") from exc


def rate(value: Any) -> str:
    try:
        raw = str(value).strip()
        number = Decimal(raw[:-1]) / 100 if raw.endswith("%") else Decimal(raw)
        if not number.is_finite() or number < 0 or number > 1:
            raise ValueError()
        if number != number.quantize(Decimal("0.00000001")):
            raise ValueError()
        return format(number, "f")
    except (ValueError, InvalidOperation) as exc:
        raise RegistryError("点数请填0到100%的比例，最多保留8位小数") from exc


def validate_timeline(body: dict, person_ids: set[str]) -> dict:
    segments = []
    for source in body.get("segments", []):
        item = dict(source)
        item["valid_from"] = local_time(str(item.get("valid_from") or ""))
        item["valid_to"] = local_time(str(item.get("valid_to") or ""), optional=True)
        if item["valid_to"] and item["valid_to"] <= item["valid_from"]:
            raise RegistryError("失效时间必须晚于生效时间")
        item["mode"] = item.get("mode", "distribute")
        if item["mode"] not in {"distribute", "exclude", "hold"}:
            raise RegistryError("方案应为分配、明确不提成或待确认")
        item["amount_hold"] = item.get("amount_hold", "")
        if item["amount_hold"] not in {"", "wage_pending"}:
            raise RegistryError("未知的金额待确认状态")
        item["total_rate"] = rate(item.get("total_rate", "0"))
        allocations = []
        seen = set()
        for source_line in item.get("allocations", []):
            line = {"person_id": str(source_line.get("person_id") or ""),
                    "role": str(source_line.get("role") or "运营").strip(),
                    "rate": rate(source_line.get("rate", "0"))}
            if line["person_id"] not in person_ids:
                raise RegistryError("方案中有未登记的人员")
            key = (line["person_id"], line["role"])
            if key in seen:
                raise RegistryError("同一人员的同一角色不能重复分点")
            seen.add(key)
            allocations.append(line)
        total = sum((Decimal(a["rate"]) for a in allocations), Decimal(0))
        if item["mode"] == "distribute":
            if not allocations or total != Decimal(item["total_rate"]):
                raise RegistryError("个人点数之和必须等于总点数，且必须指定人员")
        elif allocations or Decimal(item["total_rate"]) != 0:
            raise RegistryError("不提成和待确认状态不能包含人员分点")
        item["allocations"] = allocations
        segments.append(item)
    if not segments:
        raise RegistryError("至少登记一个有效时间区间")
    segments.sort(key=lambda r: r["valid_from"])
    for previous, following in zip(segments, segments[1:]):
        if not previous["valid_to"] or previous["valid_to"] > following["valid_from"]:
            raise RegistryError("同一店铺商品的有效区间不能重叠")
    return {**body, "segments": segments}


STORE_MEMBER_EPOCH = "1970-01-01T00:00:00"


def _migrate_store_member(conn):
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    create = """
CREATE TABLE IF NOT EXISTS store_member (
 id TEXT PRIMARY KEY, store_id TEXT NOT NULL, person_id TEXT NOT NULL,
 valid_from TEXT NOT NULL, valid_to TEXT NOT NULL DEFAULT '',
 duty TEXT NOT NULL DEFAULT 'produce',
 leader_id TEXT NOT NULL DEFAULT '',
 revision INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS store_member_scope ON store_member(store_id, person_id, valid_from);
"""
    if "store_member" not in tables:
        conn.executescript(create)
    else:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(store_member)")}
        if "valid_from" not in cols or "id" not in cols:
            rows = [dict(r) for r in conn.execute("SELECT * FROM store_member")]
            conn.execute("ALTER TABLE store_member RENAME TO store_member_legacy")
            conn.executescript(create)
            for row in rows:
                conn.execute(
                    "INSERT INTO store_member(id,store_id,person_id,valid_from,valid_to,duty,leader_id,revision) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), row["store_id"], row["person_id"], STORE_MEMBER_EPOCH, "",
                     row["duty"], row.get("leader_id") or "", row.get("revision") or 1))
            conn.execute("DROP TABLE store_member_legacy")
    conn.execute("CREATE INDEX IF NOT EXISTS store_member_scope ON store_member(store_id, person_id, valid_from)")


def member_active_at(row, at: str) -> bool:
    if not at:
        return not row.get("valid_to")
    return row["valid_from"] <= at and (not row.get("valid_to") or at < row["valid_to"])


class Registry:
    def __init__(self, root: str | Path):
        self.root = Path(root) / "commission"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "registry.db"
        key = self.path.resolve()
        with _init_lock:
            if key not in _initialized or not self.path.exists():
                with self.connect() as conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.executescript(SCHEMA)
                with self.transaction() as conn:
                    fields = {r["name"] for r in conn.execute("PRAGMA table_info(pending)")}
                    for column, definition in [("next_attempt", "INTEGER NOT NULL DEFAULT 0"), ("error", "TEXT NOT NULL DEFAULT ''"),
                                               ("source_seq", "INTEGER NOT NULL DEFAULT 0"), ("source_fingerprint", "TEXT NOT NULL DEFAULT ''")]:
                        if column not in fields:
                            conn.execute(f"ALTER TABLE pending ADD COLUMN {column} {definition}")
                    _migrate_store_member(conn)
                _initialized.add(key)

    @contextmanager
    def connect(self, *, thread_affine: bool = True) -> Iterator[sqlite3.Connection]:
        # Streaming HTTP iterators resume sequentially on different worker threads.
        # Only those private read connections opt out of thread affinity.
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=thread_affine)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def calculation_evidence(self, calculation_id: str) -> dict | None:
        from .snapshot_store import resolve
        with self.connect() as conn:
            row=conn.execute('SELECT * FROM calculation WHERE id=?',(calculation_id,)).fetchone()
            if row is None:return None
            result=dict(row)
            for column in ('model_json','rules_json'):
                result[column]=resolve(conn,result[column])
            return result

    @contextmanager
    def transaction(self):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    @staticmethod
    def audit(conn, actor: str, action: str, entity_id: str, reason: str, before, after):
        if not actor.strip() or not reason.strip():
            raise RegistryError("操作人和变更原因不能为空")
        conn.execute("INSERT INTO event(at,actor,action,entity_id,reason,before_json,after_json) "
                     "VALUES(?,?,?,?,?,?,?)", (now(), actor, action, entity_id, reason,
                                                json_text(before), json_text(after)))
        conn.execute("UPDATE meta SET revision=revision+1 WHERE id=1")

    def revision(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT revision FROM meta WHERE id=1").fetchone()[0])

    def people(self) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM person ORDER BY archived,name,id")]

    def person_save(self, data: dict, actor: str, reason: str, expected: int | None = None) -> dict:
        pid = str(data.get("id") or uuid.uuid4())
        name = str(data.get("name") or "").strip()
        if not name or len(name) > 100:
            raise RegistryError("请填写有效人员姓名")
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM person WHERE id=?", (pid,)).fetchone()
            before = dict(row) if row else None
            if before and expected != before["revision"]:
                raise RevisionConflict("人员已被修改，请刷新后再保存")
            result = {"id": pid, "name": name, "employee_no": str(data.get("employee_no") or ""),
                      "external_user_id": str(data.get("external_user_id") or ""),
                      "archived": int(bool(data.get("archived"))),
                      "revision": (before["revision"] if before else 0) + 1,
                      "note": str(data.get("note") or "")}
            conn.execute("INSERT INTO person VALUES(:id,:name,:employee_no,:external_user_id,:archived,:revision,:note) "
                         "ON CONFLICT(id) DO UPDATE SET name=excluded.name,employee_no=excluded.employee_no,"
                         "external_user_id=excluded.external_user_id,archived=excluded.archived,"
                         "revision=excluded.revision,note=excluded.note", result)
            self.audit(conn, actor, "person.save", pid, reason, before, result)
        return result

    def store_members(self, store_id: str) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM store_member WHERE store_id=? ORDER BY person_id,valid_from", (store_id,))]

    def store_members_for_stores(self, store_ids: list[str]) -> list[dict]:
        ids = [sid for sid in store_ids if sid]
        if not ids:
            return []
        with self.connect() as conn:
            marks = ','.join('?' for _ in ids)
            return [dict(r) for r in conn.execute(
                f"SELECT * FROM store_member WHERE store_id IN ({marks}) "
                "ORDER BY store_id,person_id,valid_from", ids)]

    def store_members_at(self, store_id: str, at: str) -> list[dict]:
        stamp = local_time(at) if at else ""
        latest = {}
        for row in self.store_members(store_id):
            if member_active_at(row, stamp):
                latest[row["person_id"]] = row
        return list(latest.values())

    def save_store_member(self, store_id: str, person_id: str, duty: str,
                          leader_id: str, actor: str, reason: str, *,
                          valid_from: str = "", valid_to: str = "") -> dict:
        if duty not in ('produce', 'cut'):
            raise RegistryError("身份应为做货或抽点")
        start = local_time(valid_from) if valid_from else local_time(
            datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None).isoformat(timespec="seconds"))
        end = local_time(valid_to, optional=True) if valid_to else ""
        if end and end <= start:
            raise RegistryError("身份失效时间必须晚于生效时间")
        with self.transaction() as conn:
            if not conn.execute("SELECT 1 FROM person WHERE id=?", (person_id,)).fetchone():
                raise RegistryError("人员不存在")
            if leader_id and not conn.execute("SELECT 1 FROM person WHERE id=?", (leader_id,)).fetchone():
                raise RegistryError("所属组长不存在")
            existing = [dict(r) for r in conn.execute(
                "SELECT * FROM store_member WHERE store_id=? AND person_id=? ORDER BY valid_from",
                (store_id, person_id))]
            if not end:
                later = [seg["valid_from"] for seg in existing if seg["valid_from"] > start]
                if later:
                    end = min(later)
            for seg in existing:
                if end and seg["valid_from"] >= end:
                    continue
                if seg["valid_from"] >= start:
                    conn.execute("DELETE FROM store_member WHERE id=?", (seg["id"],))
                elif not seg["valid_to"] or seg["valid_to"] > start:
                    conn.execute("UPDATE store_member SET valid_to=? WHERE id=?", (start, seg["id"]))
            revision = (max((seg["revision"] for seg in existing), default=0)) + 1
            result = {"id": str(uuid.uuid4()), "store_id": store_id, "person_id": person_id,
                      "valid_from": start, "valid_to": end, "duty": duty,
                      "leader_id": leader_id or "", "revision": revision}
            conn.execute(
                "INSERT INTO store_member(id,store_id,person_id,valid_from,valid_to,duty,leader_id,revision) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (result["id"], store_id, person_id, start, end, duty, leader_id or "", revision))
            self.audit(conn, actor, "store_member.save", f"{store_id}/{person_id}",
                       reason, existing, result)
            return result

    def scheme(self, sid: str) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM scheme WHERE id=?", (sid,)).fetchone()
            if not row:
                raise RegistryError("没有这条提成关系")
            result = dict(row)
            result["versions"] = [dict(r) for r in conn.execute(
                "SELECT * FROM scheme_version WHERE scheme_id=? ORDER BY revision DESC", (sid,))]
            for version in result["versions"]:
                version["body"] = json.loads(version["body"])
            return result

    def save_scheme(self, store_id: str, product_id: str, body: dict, actor: str, reason: str,
                    *, expected: int = 0, publish: bool = False, action: str = "save",
                    conn=None) -> dict:
        if conn is None:
            with self.transaction() as transaction:
                return self.save_scheme(store_id, product_id, body, actor, reason,
                                        expected=expected, publish=publish, action=action, conn=transaction)
        if not store_id or not product_id:
            raise RegistryError("请指定店铺和平台宝贝ID；店铺默认规则请填写 *")
        body = validate_timeline(body, {r[0] for r in conn.execute("SELECT id FROM person")})
        row = conn.execute("SELECT * FROM scheme WHERE store_id=? AND product_id=?",
                           (store_id, product_id)).fetchone()
        before = dict(row) if row else None
        revision = before["revision"] if before else 0
        if expected != revision:
            raise RevisionConflict("关系已被其他操作修改，请刷新并比较版本后重试")
        sid = before["id"] if before else str(uuid.uuid4())
        if not before:
            conn.execute("INSERT INTO scheme(id,store_id,product_id) VALUES(?,?,?)", (sid, store_id, product_id))
        elif before["active_version"]:
            previous = conn.execute("SELECT body FROM scheme_version WHERE id=?", (before["active_version"],)).fetchone()
            before["active_body"] = json.loads(previous[0])
        version = str(uuid.uuid4())
        body = {**body, "store_id": store_id, "product_id": product_id}
        revision += 1
        operation = "publish" if publish and action == "save" else action
        conn.execute("INSERT INTO scheme_version VALUES(?,?,?,?,?,?,?,?)",
                     (version, sid, revision, now(), actor, operation,
                      reason, json_text(body)))
        field = "active_version" if publish else "draft_version"
        conn.execute(f"UPDATE scheme SET revision=?,product_name=?,{field}=? WHERE id=?",
                     (revision, body.get("product_name", ""), version, sid))
        if publish:
            conn.execute("UPDATE scheme SET draft_version=NULL WHERE id=?", (sid,))
        result = {"id": sid, "version_id": version, "revision": revision, "published": publish,
                  "store_id": store_id, "product_id": product_id, "body": body}
        self.audit(conn, actor, "scheme." + operation, sid, reason, before, result)
        if publish:
            rev = int(conn.execute("SELECT revision FROM meta WHERE id=1").fetchone()[0])
            conn.execute("INSERT INTO pending(store_id,revision) VALUES(?,?) ON CONFLICT(store_id) "
                         "DO UPDATE SET revision=excluded.revision,next_attempt=0,error=''",
                         (store_id, rev))
        return result

    def save_setting(self, data: dict, actor: str, *, conn=None) -> dict:
        """One-screen editing; retain earlier dates and already scheduled changes."""
        if conn is None:
            with self.transaction() as transaction:
                return self.save_setting(data, actor, conn=transaction)
        start = local_time(data["valid_from"])
        end = local_time(data.get("valid_to", ""), optional=True)
        mode = data.get("mode", "distribute")
        row = conn.execute("SELECT * FROM scheme WHERE store_id=? AND product_id=?",
                           (data["store_id"], data["product_id"])).fetchone()
        if (row["revision"] if row else 0) != data.get("expected_revision", 0):
            raise RevisionConflict("这条设置已被修改，请重新打开后保存")
        if row and row["draft_version"]:
            raise RegistryError("该商品还有未生效的修改，需要先核对，避免覆盖")
        previous = conn.execute("SELECT body FROM scheme_version WHERE id=?", (row["active_version"],)).fetchone() if row else None
        body = json.loads(previous[0]) if previous else {}
        old = body.get("segments", [])
        # A bulk "replace" with no end date means one uniform rule from the
        # chosen time onward. Keeping an older scheduled segment would silently
        # undo part of that batch later. Single-item edits and merge/remove still
        # preserve scheduled changes unless the caller explicitly requests this.
        replace_future = bool(data.get("replace_future"))
        future = "" if replace_future else min((x["valid_from"] for x in old if x["valid_from"] > start), default="")
        if future and end and end > future:
            raise RegistryError("结束时间不能越过已有的后续设置：" + future.replace("T", " "))
        end = end or future
        current = next((x for x in old if x["valid_from"] <= start and (not x.get("valid_to") or start < x["valid_to"])), {})
        allocations = []
        seen = set()
        for item in data.get("allocations", []) if mode == "distribute" else []:
            pid = str(item.get("person_id") or "")
            if not pid:
                name = str(item.get("name") or "").strip()
                if not name or len(name) > 100:
                    raise RegistryError("请填写人员姓名")
                matches = conn.execute("SELECT id FROM person WHERE name=?", (name,)).fetchall()
                if len(matches) > 1:
                    raise RegistryError("存在同名人员，请从列表选择具体人员")
                if matches:
                    pid = matches[0][0]
                else:
                    pid = str(uuid.uuid4())
                    person = {"id":pid,"name":name,"employee_no":"","external_user_id":"","archived":0,"revision":1,"note":""}
                    conn.execute("INSERT INTO person VALUES(:id,:name,:employee_no,:external_user_id,:archived,:revision,:note)", person)
                    self.audit(conn, actor, "person.save", pid, "设置提成人员", None, person)
            if pid in seen:
                raise RegistryError("同一人员只需填写一次，比例填合计值")
            seen.add(pid)
            share = rate(item.get("rate", "0"))
            parts = [a for a in current.get("allocations", []) if a["person_id"] == pid]
            if parts and sum(Decimal(a["rate"]) for a in parts) == Decimal(share):
                allocations.extend(parts)
            else:
                allocations.append({"person_id":pid,"role":parts[0]["role"] if len(parts)==1 else "提成","rate":share})
        segment = {"valid_from":start,"valid_to":end,"mode":mode,"allocations":allocations,
                   "total_rate":str(sum((Decimal(a["rate"]) for a in allocations), Decimal(0))),
                   "amount_hold":current.get("amount_hold", "")}
        segments = []
        for x in old:
            if x["valid_from"] < start:
                segments.append({**x,"valid_to":min(x.get("valid_to") or start, start)})
            elif end and x["valid_from"] >= end:
                segments.append(x)
        segments.append(segment)
        return self.save_scheme(data["store_id"], data["product_id"],
                                {**body,"product_name":data.get("product_name", ""),"segments":segments, **({"source":data["source"]} if data.get("source") else {})},
                                actor, data.get("reason", "调整提成设置"), expected=data.get("expected_revision",0), publish=True, conn=conn)

    def publish(self, sid: str, expected: int, actor: str, reason: str) -> dict:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM scheme WHERE id=?", (sid,)).fetchone()
            if not row or not row["draft_version"]:
                raise RegistryError("没有待启用版本")
            version = conn.execute("SELECT body FROM scheme_version WHERE id=?", (row["draft_version"],)).fetchone()
            return self.save_scheme(row["store_id"], row["product_id"], json.loads(version[0]),
                                    actor, reason, expected=expected, publish=True, conn=conn)

    def terminate(self, sid: str, at: str, expected: int, actor: str, reason: str,
                  *, mode: str = "hold") -> dict:
        item = self.scheme(sid)
        active = next((v for v in item["versions"] if v["id"] == item["active_version"]), None)
        if not active:
            raise RegistryError("该关系尚未启用")
        moment = local_time(at)
        segments = []
        for segment in active["body"]["segments"]:
            if segment["valid_from"] < moment:
                segments.append({**segment, "valid_to": min(segment["valid_to"] or moment, moment)})
        segments.append({"valid_from": moment, "valid_to": "", "mode": mode,
                         "allocations": [], "total_rate": "0"})
        return self.save_scheme(item["store_id"], item["product_id"],
                                {**active["body"], "segments": segments}, actor, reason,
                                expected=expected, publish=True, action="terminate")

    def active(self, store_id: str, period: str | None = None):
        with self.connect() as conn:
            conn.execute("BEGIN")
            rev = int(conn.execute("SELECT revision FROM meta WHERE id=1").fetchone()[0])
            rows = [dict(r) for r in conn.execute(
                "SELECT v.*,s.product_id FROM scheme s JOIN scheme_version v ON v.id=s.active_version "
                "WHERE s.store_id=?", (store_id,))]
            for row in rows:
                row["body"] = json.loads(row["body"])
            people = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM person")}
            if period is not None:
                policy_row = conn.execute("SELECT * FROM policy_version WHERE store_id IN ('',?) AND effective_from<=? "
                                          "ORDER BY (store_id<>'') DESC,effective_from DESC,id DESC LIMIT 1",
                                          (store_id, period + "-01T00:00:00")).fetchone()
                policy = {**dict(policy_row), "body": json.loads(policy_row["body"])} if policy_row else None
                return rev, rows, people, policy
            return rev, rows, people

    def history(self, entity_id: str = "", after: int = 0, limit: int = 200) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM event WHERE id>? AND (?='' OR entity_id=?) ORDER BY id LIMIT ?",
                (after, entity_id, entity_id, limit))]

    def operator_add(self, name: str, password: str, *, admin: bool = False, by: str = "system:bootstrap") -> None:
        if len(password) < 12 or not name.strip():
            raise RegistryError("账号不能为空，密码至少12位")
        salt = secrets.token_hex(16)
        hashed = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM operator WHERE id=?", (name,)).fetchone():
                raise RegistryError("该操作账号已存在")
            conn.execute("INSERT INTO operator(id,name,salt,password_hash,admin) VALUES(?,?,?,?,?)",
                         (name, name, salt, hashed, int(admin)))
            self.audit(conn, by, "operator.create", name, "创建提成操作账号", None,
                       {"id": name, "name": name, "admin": admin})

    def change_password(self, operator_id: str, old_password: str, new_password: str) -> None:
        if len(new_password) < 12:
            raise RegistryError("新密码至少12位")
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM operator WHERE id=? AND disabled=0", (operator_id,)).fetchone()
            if not row:
                raise RegistryError("账号不存在")
            old_hash = hashlib.scrypt(old_password.encode(), salt=bytes.fromhex(row["salt"]), n=16384, r=8, p=1).hex()
            if not hmac.compare_digest(old_hash, row["password_hash"]):
                raise RegistryError("原密码不正确")
            salt = secrets.token_hex(16)
            new_hash = hashlib.scrypt(new_password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
            conn.execute("UPDATE operator SET salt=?,password_hash=? WHERE id=?", (salt, new_hash, operator_id))
            conn.execute("DELETE FROM session WHERE operator_id=?", (operator_id,))
            self.audit(conn, operator_id, "operator.password", operator_id, "修改密码并注销全部会话", None, {"changed": True})

    def login(self, name: str, password: str) -> str:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM operator WHERE id=? AND disabled=0", (name,)).fetchone()
            salt = row["salt"] if row else "00" * 16
            actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
            if not row or not hmac.compare_digest(actual, row["password_hash"]):
                raise RegistryError("账号或密码不正确")
            token = secrets.token_urlsafe(32)
            conn.execute("DELETE FROM session WHERE expires<?", (int(time.time()),))
            conn.execute("INSERT INTO session VALUES(?,?,?)",
                         (hashlib.sha256(token.encode()).hexdigest(), name, int(time.time()) + 8 * 3600))
            return token

    def actor(self, token: str) -> dict | None:
        if not token:
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT o.id,o.name,o.admin FROM session s JOIN operator o ON o.id=s.operator_id "
                "WHERE s.digest=? AND s.expires>? AND o.disabled=0",
                (hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone()
            return dict(row) if row else None

    def auth_mode(self) -> str:
        return os.environ.get("LEDGER_COMMISSION_AUTH_MODE", "open")

    def enqueue(self, kind: str, payload: dict, actor: str) -> dict:
        job_id = str(uuid.uuid4())
        with self.transaction() as conn:
            conn.execute("INSERT INTO job(id,kind,actor,at,status,payload) VALUES(?,?,?,?,?,?)",
                         (job_id, kind, actor, now(), "queued", json_text(payload)))
        return {"job_id": job_id, "status": "queued"}

    def enqueue_source(self, stores: set[str], fingerprint: str) -> None:
        match = re.fullmatch(r"order-feed:[^:]+:(\d+)(?::components:[0-9a-f]{64})?", fingerprint)
        if match is None:
            raise RegistryError("订单来源版本格式不正确")
        seq = int(match.group(1))
        with self.transaction() as conn:
            revision = conn.execute("SELECT revision FROM meta WHERE id=1").fetchone()[0]
            conn.executemany(
                "INSERT INTO pending(store_id,revision,source_seq,source_fingerprint) VALUES(?,?,?,?) "
                "ON CONFLICT(store_id) DO UPDATE SET revision=max(revision,excluded.revision),"
                "source_seq=max(source_seq,excluded.source_seq),source_fingerprint=excluded.source_fingerprint,"
                # Preserve an active worker lease/backoff. A new event makes an
                # idle row due now, but must not let one noisy store jump ahead
                # again while its current recomputation is still running.
                "next_attempt=max(next_attempt,?),error=''",
                [(sid, revision, seq, fingerprint, int(time.time())) for sid in stores],
            )

    def policy(self, period: str, store_id: str = "") -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM policy_version WHERE store_id IN ('',?) AND effective_from<=? "
                               "ORDER BY (store_id<>'') DESC,effective_from DESC,id DESC LIMIT 1",
                               (store_id, period + "-01T00:00:00")).fetchone()
            return {**dict(row), "body": json.loads(row["body"])} if row else None

    def save_policy(self, body: dict, effective_from: str, actor: str, reason: str,
                    *, store_id: str = "", stores: list[str] | None = None,
                    expected_revision: int | None = None) -> dict:
        start = local_time(effective_from)
        if start[8:] != "01T00:00:00":
            raise RegistryError("计算口径按账期设置，生效日应为当月1日00:00；人员分点可按具体时刻调整")
        if body.get("on_loss", "inherit") not in {"inherit", "deduct", "skip"}:
            raise RegistryError("未知亏损处理方式")
        if body.get("wages", "pending") not in {"pending", "skip_preview"}:
            raise RegistryError("未知工资处理方式")
        with self.transaction() as conn:
            previous = conn.execute("SELECT * FROM policy_version WHERE store_id=? ORDER BY id DESC LIMIT 1", (store_id,)).fetchone()
            if expected_revision is not None and expected_revision != (previous["id"] if previous else 0):
                raise RevisionConflict("计算口径已被修改，请刷新比较后再保存")
            cursor = conn.execute("INSERT INTO policy_version(store_id,effective_from,recorded_at,actor,reason,body) VALUES(?,?,?,?,?,?)",
                                  (store_id, start, now(), actor, reason, json_text(body)))
            result = {"id": cursor.lastrowid, "store_id": store_id, "effective_from": start, "body": body}
            self.audit(conn, actor, "policy.publish", str(cursor.lastrowid), reason, dict(previous) if previous else None, result)
            revision = conn.execute("SELECT revision FROM meta WHERE id=1").fetchone()[0]
            touched = [store_id] if store_id else (stores or [r[0] for r in conn.execute("SELECT DISTINCT store_id FROM scheme")])
            conn.executemany("INSERT INTO pending(store_id,revision) VALUES(?,?) ON CONFLICT(store_id) "
                             "DO UPDATE SET revision=excluded.revision,next_attempt=0,error=''", [(sid, revision) for sid in touched])
        return result
