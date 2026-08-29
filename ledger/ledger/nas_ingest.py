"""Apply ready NAS catalog entries to the deterministic Ledger workspace."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from time import sleep, time
from typing import Callable

from . import service
from .model.schema import Model
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


def _quarantine(path: Path, root: Path, reason: str) -> None:
    relative = path.name
    for area in (root / "00_上传区", root / "10_已接收"):
        try:
            relative = path.relative_to(area)
            break
        except ValueError:
            continue
    target = root / "20_需修正" / relative
    if target.exists():
        target = target.with_name(f"{target.stem}__conflict{target.suffix}")
    target.parent.mkdir(parents=True, exist_ok=True)
    path.replace(target)
    target.with_suffix(target.suffix + ".reason.txt").write_text(reason + "\n", encoding="utf-8")


def _validate_owner(model: Model, row: sqlite3.Row) -> str:
    expected = row["store_id"]
    name = Path(row["path"]).name
    if expected == SHARED_STORE_ID:
        return ""
    store = model.store_of(name)
    if store is None:
        return f"文件名无法识别店铺；目录登记为 {expected}"
    if store.id != expected:
        return f"目录店铺 {expected} 与文件名店铺 {store.id} 冲突"
    expected_platform = model.store(expected).platform
    platform_names = {platform.name: platform.id for platform in model.platforms}
    directory_platform = platform_names.get(row["platform"], row["platform"])
    if directory_platform != expected_platform:
        return f"目录平台 {row['platform']} 与店铺档案平台 {expected_platform} 冲突"
    return ""


def reconcile_ready(ws: Workspace, model: Model, catalog: Path, nas_root: Path) -> dict:
    if not nas_root.is_dir():
        return {"applied": 0, "search_only": 0, "errors": ["NAS 根目录不可达"]}
    connection = sqlite3.connect(catalog, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.executescript(APPLY_SCHEMA)
    rows = connection.execute(
        "select f.* from file_catalog f left join ledger_apply a on a.path=f.path and a.sha256=f.sha256 "
        "where f.state='ready' and f.sha256<>'' and a.path is null order by f.indexed_at,f.path"
    ).fetchall()
    applicable: list[sqlite3.Row] = []
    errors: list[str] = []
    search_only = 0
    for row in rows:
        path = Path(row["path"])
        if not path.is_file():
            continue
        if row["authority"] == "search_only":
            _record(connection, row, "search_only")
            search_only += 1
            continue
        reason = _validate_owner(model, row)
        if reason:
            try:
                _quarantine(path, nas_root, reason)
            finally:
                _record(connection, row, "quarantined", reason)
            errors.append(f"{path.name}：{reason}")
            continue
        applicable.append(row)
    connection.commit()

    if applicable:
        result = service.intake(
            ws, model, [(Path(row["path"]).name, Path(row["path"])) for row in applicable],
            by="NAS自动接收",
        )
        kept = {(item.store_id, item.name) for item in result.kept}
        rejected = {item.file: item.why for item in result.rejected}
        for row in applicable:
            path = Path(row["path"])
            name = path.name
            key = (row["store_id"], name)
            if key not in kept:
                reason = rejected.get(name, "财务引擎没有接收该文件")
                _record(connection, row, "error", reason)
                errors.append(f"{name}：{reason}")
                continue
            try:
                _archive(path, row["sha256"], nas_root)
                _accept_uploaded(path, nas_root, row["sha256"])
                _record(connection, row, "applied")
            except Exception as exc:  # workspace is already consistent; leave a retryable audit
                _record(connection, row, "error", str(exc))
                errors.append(f"{name}：{exc}")
        connection.commit()
    applied = connection.execute("select count(*) from ledger_apply where state='applied'").fetchone()[0]
    connection.close()
    return {"applied": applied, "search_only": search_only, "errors": errors}


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
    ) -> None:
        self.workspace_fn = workspace_fn
        self.model_fn = model_fn
        self.catalog = catalog
        self.root = root
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
                    reconcile_ready(ws, model, self.catalog, self.root)
                    reconcile_missing(ws, model, self.catalog)
            except Exception:
                # The catalog retains per-file errors. A transient SMB/SQLite failure must not kill
                # the worker or be reinterpreted as deletion.
                pass
            self.stop_event.wait(30)
