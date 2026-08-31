"""Apply ready NAS catalog entries to the deterministic Ledger workspace."""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import time
from typing import Callable

from . import service
from .model.config import add_store, update_store
from .model.loader import ModelError, load_model
from .model.schema import Model, Store
from .workspace import SHARED_STORE_ID, Workspace


APPLY_SCHEMA = """
create table if not exists ledger_apply (
  path text primary key,
  sha256 text not null,
  store_id text not null,
  name text not null,
  state text not null,
  applied_at text not null default '',
  error text not null default '',
  removed_at text not null default ''
);
create index if not exists ledger_apply_sha on ledger_apply(sha256);
"""

_DEFAULT_MODEL = Path(__file__).resolve().parents[2] / "models" / "cn-ecommerce"
_STORE_FOLDER = re.compile(r"^(.+) \[([^\]]+)\]$")
_DATE_TAIL = re.compile(
    r"(?:[_-](?:\d{8}|\d{4}-\d{2}-\d{2})(?:[ T_]\d{2}[_.:-]\d{2}[_.:-]\d{2})?(?:[_-]\d+)*)+$"
)
_SEQ_TAIL = re.compile(r"(?:[_-]\d+)+$")
_GENERIC_TAILS = (
    "保单明细", "导出明细", "数据明细", "资金流水", "明细", "导出", "报表", "数据", "流水",
)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _record(connection: sqlite3.Connection, row: sqlite3.Row, state: str, error: str = "") -> None:
    connection.execute(
        "insert into ledger_apply(path,sha256,store_id,name,state,applied_at,error) values(?,?,?,?,?,?,?) "
        "on conflict(path) do update set sha256=excluded.sha256,store_id=excluded.store_id,"
        "name=excluded.name,state=excluded.state,applied_at=excluded.applied_at,error=excluded.error,removed_at=''",
        (row["path"], row["sha256"], row["store_id"], Path(row["path"]).name,
         state, _now(), error),
    )


def _archive(path: Path, sha: str, root: Path) -> None:
    target = root / "90_历史版本" / sha[:2] / sha / "payload"
    if target.exists():
        if _hash(target) != sha:
            raise ValueError(f"历史库中 {sha} 的内容不匹配")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name("payload.part")
    shutil.copy2(path, temporary)
    if _hash(temporary) != sha:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"归档后哈希不匹配：{path}")
    temporary.replace(target)


def _under(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def _accept_uploaded(path: Path, root: Path, sha: str) -> Path:
    upload = root / "00_上传区"
    try:
        relative = path.relative_to(upload)
    except ValueError:
        return path
    target = root / "10_已接收" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if _hash(target) != sha:
            raise FileExistsError(f"已接收目录存在同名不同内容：{target}")
        path.unlink()
        return target
    path.replace(target)
    return target


def _relative_to_areas(path: Path, root: Path, *areas: str) -> Path | None:
    for area in areas:
        try:
            return path.relative_to(root / area)
        except ValueError:
            continue
    return None


def _quarantine(path: Path, root: Path, reason: str) -> Path:
    if _under(path, root / "20_需修正"):
        path.with_suffix(path.suffix + ".reason.txt").write_text(reason + "\n", encoding="utf-8")
        return path
    relative = _relative_to_areas(path, root, "00_上传区", "10_已接收")
    if relative is None:
        relative = Path(path.name)
    target = root / "20_需修正" / relative
    if target.exists():
        target = target.with_name(f"{target.stem}__conflict{target.suffix}")
    target.parent.mkdir(parents=True, exist_ok=True)
    path.replace(target)
    target.with_suffix(target.suffix + ".reason.txt").write_text(reason + "\n", encoding="utf-8")
    return target


def _restore_quarantined(path: Path, root: Path, sha: str, catalog_path: str) -> Path:
    original = Path(catalog_path)
    relative = _relative_to_areas(original, root, "00_上传区", "10_已接收", "20_需修正")
    if relative is None:
        relative = _relative_to_areas(path, root, "20_需修正")
    if relative is None:
        return path
    name = relative.name.replace("__conflict", "")
    target = root / "10_已接收" / relative.with_name(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if _hash(target) != sha:
            raise FileExistsError(f"已接收目录存在同名不同内容：{target}")
        if path != target:
            path.unlink(missing_ok=True)
    else:
        path.replace(target)
    reason = path.with_suffix(path.suffix + ".reason.txt")
    reason.unlink(missing_ok=True)
    target.with_suffix(target.suffix + ".reason.txt").unlink(missing_ok=True)
    return target


def _live_path(catalog_path: str, nas_root: Path, sha: str) -> Path | None:
    original = Path(catalog_path)
    candidates = [original]
    relative = _relative_to_areas(original, nas_root, "00_上传区", "10_已接收")
    if relative is not None:
        quarantine = nas_root / "20_需修正" / relative
        candidates.append(quarantine)
        candidates.append(quarantine.with_name(f"{relative.stem}__conflict{relative.suffix}"))
    if _under(original, nas_root / "20_需修正"):
        candidates.append(original)
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate if candidate.is_absolute() else nas_root / candidate
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        if _hash(resolved) == sha:
            return resolved
    return None


def _relocate_catalog(connection: sqlite3.Connection, old_path: str, new_path: str, sha: str) -> None:
    if old_path == new_path:
        return
    existing = connection.execute(
        "select path from file_catalog where path=?", (new_path,),
    ).fetchone()
    if existing is None:
        connection.execute(
            "update file_catalog set path=?, missing_scans=0, missing_since=null "
            "where path=? and sha256=?",
            (new_path, old_path, sha),
        )
    else:
        connection.execute(
            "update file_catalog set missing_scans=0, missing_since=null where path=?",
            (new_path,),
        )
    connection.execute(
        "update ledger_apply set path=? where path=? and sha256=?",
        (new_path, old_path, sha),
    )


@dataclass
class OwnerDecision:
    ok: bool
    reason: str = ""
    audit: str = ""
    quarantine: bool = True
    model: Model | None = None


def _folder_name_from_path(path: str) -> str:
    for part in Path(path).parts:
        match = _STORE_FOLDER.fullmatch(part)
        if match:
            return match.group(1).strip()
    return ""


def _source_labels(model: Model) -> tuple[str, ...]:
    labels = [source.name for source in model.sources]
    for source in model.sources:
        labels.extend(source.filename_hints)
    return tuple(sorted({label for label in labels if label}, key=len, reverse=True))


def _extract_store_name(filename: str, source_labels: tuple[str, ...] = ()) -> str:
    """从文件名里抽出店铺名：去掉扩展名、数据源、日期和序号。"""
    stem = Path(filename).stem.replace("__conflict", "")
    stem = _DATE_TAIL.sub("", stem)
    stem = _SEQ_TAIL.sub("", stem)
    labels = tuple(source_labels) or ()
    changed = True
    while changed and stem:
        changed = False
        for label in labels:
            for sep in ("-", "_", " "):
                prefix, suffix = label + sep, sep + label
                if stem.startswith(prefix):
                    stem = stem[len(prefix):]
                    changed = True
                    break
                if stem.endswith(suffix):
                    stem = stem[:-len(suffix)]
                    changed = True
                    break
            if changed:
                break
            if stem == label:
                return ""
    changed = True
    while changed and stem:
        changed = False
        for tail in _GENERIC_TAILS:
            for sep in ("-", "_"):
                suffix = sep + tail
                if stem.endswith(suffix):
                    stem = stem[:-len(suffix)]
                    changed = True
                    break
            if changed:
                break
    return stem.strip("-_ ").strip()


def _platform_mismatch(model: Model, row: sqlite3.Row, store_id: str) -> str:
    expected_platform = model.store(store_id).platform
    platform_names = {platform.name: platform.id for platform in model.platforms}
    directory_platform = platform_names.get(row["platform"], row["platform"])
    if directory_platform != expected_platform:
        return f"目录平台 {row['platform']} 与店铺档案平台 {expected_platform} 冲突"
    return ""


def _foreign_store(model: Model, filename: str, store_id: str) -> str:
    for store in model.stores:
        if store.id == store_id:
            continue
        for alias in (store.name, *store.aliases):
            if alias and alias in filename:
                return f"文件名包含其他店铺 {store.id} 的名字「{alias}」"
    return ""


def _alias_conflicts(model: Model, store_id: str, candidate: str) -> str:
    for store in model.stores:
        if store.id == store_id:
            continue
        names = (store.name, *store.aliases)
        if candidate in names:
            return f"别名「{candidate}」已被 {store.id} 使用"
    return ""


def _infer_platform(store_id: str, directory_platform: str, model: Model) -> str:
    names = {platform.name: platform.id for platform in model.platforms}
    if directory_platform in names:
        return names[directory_platform]
    known = {platform.id for platform in model.platforms}
    if directory_platform in known:
        return directory_platform
    for platform in sorted(model.platforms, key=lambda item: len(item.id), reverse=True):
        if store_id.startswith(f"{platform.id}_"):
            return platform.id
    return ""


def _feed_has_store(feed_db: Path, store_id: str) -> bool:
    if not feed_db.is_file():
        return False
    try:
        connection = sqlite3.connect(feed_db, timeout=5)
        try:
            row = connection.execute(
                "select 1 from feed_store where ledger_store_id=? limit 1", (store_id,),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    return row is not None


def _try_auto_alias(
    model_dir: Path, model: Model, store_id: str, filename: str,
) -> tuple[Model, str, str]:
    """store_of 认不出时，尝试从文件名学一个别名。返回 (model, audit, error)。"""
    store = model.store(store_id)
    candidate = _extract_store_name(filename, _source_labels(model))
    if not candidate or len(candidate) < 3 or candidate.isdigit():
        return model, "文件名无法提取别名，按目录店铺接收", ""
    if candidate == store.name or candidate in store.aliases:
        return model, "", ""
    conflict = _alias_conflicts(model, store_id, candidate)
    if conflict:
        return model, "", conflict
    try:
        update_store(model_dir, store_id, {"aliases": [*store.aliases, candidate]})
    except ModelError as exc:
        return model, "", str(exc)
    return load_model(model_dir), f"自动学习别名：{candidate}", ""


def _try_auto_register(
    model_dir: Path, model: Model, store_id: str, row: sqlite3.Row, feed_db: Path,
) -> tuple[Model, str, str]:
    """Model 里没有这个 store_id 时，在订单台确认后自动登记。"""
    if not _feed_has_store(feed_db, store_id):
        return model, "", f"store_id {store_id} 不在订单台映射中，需人工登记"
    folder_name = _folder_name_from_path(row["path"])
    if not folder_name:
        return model, "", f"无法从目录提取店名：{row['path']}"
    if any(store.name == folder_name for store in model.stores):
        return model, "", f"已经有一家店叫 {folder_name} 了。同名会让文件认不清归谁。"
    platform_id = _infer_platform(store_id, row["platform"], model)
    if not platform_id:
        return model, "", f"无法推断平台：{store_id} / {row['platform']}"
    try:
        add_store(model_dir, Store(
            id=store_id,
            name=folder_name,
            platform=platform_id,
            note=f"NAS自动登记。目录名「{folder_name}」。",
        ))
    except ModelError as exc:
        return model, "", str(exc)
    return load_model(model_dir), f"自动登记店铺：{store_id}「{folder_name}」", ""


def _decide_owner(
    model: Model, row: sqlite3.Row, *, model_dir: Path, feed_db: Path,
) -> OwnerDecision:
    expected = row["store_id"]
    name = Path(row["path"]).name
    if expected == SHARED_STORE_ID:
        return OwnerDecision(ok=True, model=model)

    registered = any(store.id == expected for store in model.stores)
    matched = model.store_of(name)

    if matched is not None:
        if matched.id != expected:
            return OwnerDecision(
                ok=False, reason=f"目录店铺 {expected} 与文件名店铺 {matched.id} 冲突",
            )
        reason = _platform_mismatch(model, row, expected)
        if reason:
            return OwnerDecision(ok=False, reason=reason)
        return OwnerDecision(ok=True, model=model)

    if registered:
        reason = _platform_mismatch(model, row, expected)
        if reason:
            return OwnerDecision(ok=False, reason=reason)
        foreign = _foreign_store(model, name, expected)
        if foreign:
            return OwnerDecision(ok=False, reason=foreign)
        model, audit, error = _try_auto_alias(model_dir, model, expected, name)
        if error:
            return OwnerDecision(ok=False, reason=error, model=model)
        return OwnerDecision(ok=True, model=model, audit=audit)

    model, audit, error = _try_auto_register(model_dir, model, expected, row, feed_db)
    if error:
        return OwnerDecision(ok=False, reason=error, quarantine=False, model=model)
    if model.store_of(name) is None:
        model, alias_audit, alias_error = _try_auto_alias(model_dir, model, expected, name)
        if alias_error:
            return OwnerDecision(ok=False, reason=alias_error, model=model)
        if alias_audit:
            audit = f"{audit}；{alias_audit}" if audit else alias_audit
    reason = _platform_mismatch(model, row, expected)
    if reason:
        return OwnerDecision(ok=False, reason=reason, model=model)
    return OwnerDecision(ok=True, model=model, audit=audit)


def _validate_owner(model: Model, row: sqlite3.Row) -> str:
    """兼容旧调用：只做交叉验证，不写模型。"""
    expected = row["store_id"]
    name = Path(row["path"]).name
    if expected == SHARED_STORE_ID:
        return ""
    store = model.store_of(name)
    if store is None:
        return f"文件名无法识别店铺；目录登记为 {expected}"
    if store.id != expected:
        return f"目录店铺 {expected} 与文件名店铺 {store.id} 冲突"
    return _platform_mismatch(model, row, expected)


def _pending_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    fresh = connection.execute(
        "select f.* from file_catalog f left join ledger_apply a on a.path=f.path and a.sha256=f.sha256 "
        "where f.state in ('ready','finance_only') and f.sha256<>'' and a.path is null "
        "order by f.indexed_at,f.path"
    ).fetchall()
    quarantined = connection.execute(
        "select f.* from file_catalog f join ledger_apply a on a.path=f.path and a.sha256=f.sha256 "
        "where a.state='quarantined' and f.sha256<>'' order by f.indexed_at,f.path"
    ).fetchall()
    seen = {row["path"] for row in fresh}
    return list(fresh) + [row for row in quarantined if row["path"] not in seen]


def reconcile_ready(
    ws: Workspace,
    model: Model,
    catalog: Path,
    nas_root: Path,
    model_dir: Path | None = None,
) -> dict:
    if not nas_root.is_dir():
        return {"applied": 0, "search_only": 0, "errors": ["NAS 根目录不可达"]}
    model_dir = Path(model_dir) if model_dir is not None else _DEFAULT_MODEL
    feed_db = ws.root / "order-feed.db"
    connection = sqlite3.connect(catalog, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.executescript(APPLY_SCHEMA)
    rows = _pending_rows(connection)
    applicable: list[sqlite3.Row] = []
    live_paths: dict[str, Path] = {}
    errors: list[str] = []
    audits: list[str] = []
    search_only = 0
    for row in rows:
        live = _live_path(row["path"], nas_root, row["sha256"])
        if live is None:
            continue
        if row["authority"] == "search_only":
            _record(connection, row, "search_only")
            search_only += 1
            continue
        decision = _decide_owner(model, row, model_dir=model_dir, feed_db=feed_db)
        if decision.model is not None:
            model = decision.model
        if not decision.ok:
            if decision.quarantine:
                try:
                    _quarantine(live, nas_root, decision.reason)
                finally:
                    _record(connection, row, "quarantined", decision.reason)
                errors.append(f"{live.name}：{decision.reason}")
            else:
                errors.append(f"{live.name}：{decision.reason}")
            continue
        if decision.audit:
            audits.append(f"{live.name}：{decision.audit}")
        live_paths[row["path"]] = live
        applicable.append(row)
    connection.commit()

    if applicable:
        source_ids = {source.name: source.id for source in model.sources}
        assigned = []
        assigned_rows = []
        for row in applicable:
            source_id = source_ids.get(row["source"])
            if source_id is None:
                reason = f"目录数据源未登记：{row['source']}"
                _record(connection, row, "error", reason)
                errors.append(f"{Path(row['path']).name}：{reason}")
                continue
            live = live_paths[row["path"]]
            assigned.append((live.name, live, row["store_id"], source_id))
            assigned_rows.append(row)
        result = service.intake_assigned(
            ws, model, assigned,
            by="NAS自动接收",
        )
        kept = {(item.store_id, item.name) for item in result.kept}
        rejected = {item.file: item.why for item in result.rejected}
        for row in assigned_rows:
            live = live_paths[row["path"]]
            name = live.name
            key = (row["store_id"], name)
            audit = next((item.split("：", 1)[1] for item in audits if item.startswith(f"{name}：")), "")
            if key not in kept:
                reason = rejected.get(name, "财务引擎没有接收该文件")
                _record(connection, row, "error", reason)
                errors.append(f"{name}：{reason}")
                continue
            try:
                _archive(live, row["sha256"], nas_root)
                if _under(live, nas_root / "20_需修正"):
                    accepted = _restore_quarantined(live, nas_root, row["sha256"], row["path"])
                else:
                    accepted = _accept_uploaded(live, nas_root, row["sha256"])
                _record(connection, row, "applied", audit)
                if str(accepted) != row["path"]:
                    _relocate_catalog(connection, row["path"], str(accepted), row["sha256"])
            except Exception as exc:  # workspace is already consistent; leave a retryable audit
                _record(connection, row, "error", str(exc))
                errors.append(f"{name}：{exc}")
        connection.commit()
    applied = connection.execute("select count(*) from ledger_apply where state='applied'").fetchone()[0]
    connection.close()
    return {"applied": applied, "search_only": search_only, "errors": errors, "audits": audits}


def reconcile_missing(ws: Workspace, model: Model, catalog: Path) -> dict:
    connection = sqlite3.connect(catalog, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.executescript(APPLY_SCHEMA)
    meta = connection.execute(
        "select root_reachable,last_completed from scan_meta where id=1"
    ).fetchone()
    if not meta or not meta["root_reachable"] or not meta["last_completed"]:
        connection.close()
        return {"removed": 0, "errors": []}
    cutoff = int(time()) - 600
    rows = connection.execute(
        "select f.path,f.sha256,f.missing_scans,f.missing_since,a.store_id,a.name "
        "from file_catalog f join ledger_apply a on a.path=f.path and a.sha256=f.sha256 "
        "where a.state='applied' and a.removed_at='' and f.missing_scans>=3 and f.missing_since<=? "
        "and not exists(select 1 from file_catalog live where live.sha256=f.sha256 and live.state='ready' "
        "and live.missing_scans=0 and live.path<>f.path)",
        (cutoff,),
    ).fetchall()
    touched: set[str] = set()
    shared = False
    errors: list[str] = []
    for row in rows:
        try:
            ws.forget(row["store_id"], row["name"])
            if row["store_id"] == SHARED_STORE_ID:
                shared = True
            else:
                touched.add(row["store_id"])
            connection.execute(
                "update ledger_apply set state='removed',removed_at=? where path=?",
                (_now(), row["path"]),
            )
        except Exception as exc:
            errors.append(f"{row['name']}：{exc}")
    if shared:
        active = {store.id for store in model.active_stores()}
        touched.update(store_id for store_id in ws.store_ids() if store_id in active)
    for store_id in sorted(touched):
        service.recompute(ws, model, model.store(store_id))
    connection.commit()
    connection.close()
    return {"removed": len(rows), "errors": errors}


class NasIngestWorker:
    def __init__(
        self,
        workspace_fn: Callable[[], Workspace],
        model_fn: Callable[[], Model],
        catalog: Path,
        root: Path,
        model_dir: Path | None = None,
    ) -> None:
        self.workspace_fn = workspace_fn
        self.model_fn = model_fn
        self.catalog = catalog
        self.root = root
        self.model_dir = Path(model_dir) if model_dir is not None else _DEFAULT_MODEL
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="ledger-nas-ingest", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                if self.catalog.is_file() and self.root.is_dir():
                    ws, model = self.workspace_fn(), self.model_fn()
                    reconcile_ready(ws, model, self.catalog, self.root, model_dir=self.model_dir)
                    reconcile_missing(ws, model, self.catalog)
            except Exception:
                # The catalog retains per-file errors. A transient SMB/SQLite failure must not kill
                # the worker or be reinterpreted as deletion.
                pass
            self.stop_event.wait(30)
