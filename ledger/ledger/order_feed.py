"""Read-only bridge from Order Console into Ledger's deterministic engine.

Order Console owns operational facts.  Ledger owns accounting rules and close.
The bridge therefore never writes Order Console and never mutates a closed run:
it verifies an immutable snapshot, replays the transactional outbox after the
snapshot's ``through_seq``, and exposes normalized engine frames for open runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import polars as pl

from .engine.runtime import Ingested, Ingestion
from .engine.types import ANCHOR_FILE, ANCHOR_ROW, ANCHOR_SHA, ANCHOR_SHEET, FileRef, Recognition
from .model.schema import ColumnBinding, Model, Store, Template

SCHEMA_VERSION = "ledger-feed.v1"
REPLACED_SOURCES = frozenset({"order_cost", "after_sales"})
_DB_SCHEMA = """
pragma journal_mode=wal;
pragma synchronous=full;
create table if not exists feed_state (
  id integer primary key check(id=1),
  schema_version text not null default '',
  snapshot_id text not null default '',
  snapshot_revision integer not null default 0,
  snapshot_through_seq integer not null default 0,
  consumed_seq integer not null default 0,
  source_revision integer not null default 0,
  source_latest_seq integer not null default 0,
  revision_etag text not null default '',
  stores_refreshed_at integer not null default 0,
  manifest_json text not null default '',
  health_json text not null default '',
  last_success text not null default '',
  last_error text not null default ''
);
insert or ignore into feed_state(id) values(1);
create table if not exists feed_store (
  order_store_id text primary key,
  ledger_store_id text not null,
  mapping_status text not null,
  payload_json text not null
);
create table if not exists feed_entity (
  entity_type text not null,
  entity_id text not null,
  seq integer not null,
  revision integer not null,
  operation text not null,
  order_store_id text,
  order_id text,
  sub_order_id text,
  sku_id text,
  payload_json text,
  primary key(entity_type,entity_id)
);
create index if not exists feed_entity_store on feed_entity(order_store_id,entity_type);
"""

_STATE_COLUMNS = {
    "source_latest_seq": "integer not null default 0",
    "revision_etag": "text not null default ''",
    "stores_refreshed_at": "integer not null default 0",
}


class OrderFeedError(RuntimeError):
    """The feed cannot be consumed without weakening accounting evidence."""


class OrderFeedNotFound(OrderFeedError):
    """A pointer names an entity that is no longer present in current state."""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def enabled() -> bool:
    return os.environ.get("LEDGER_ORDER_FEED_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _root() -> Path:
    return Path(os.environ.get("LEDGER_ORDER_FEED_ROOT", r"D:\order\exchange\ledger-feed"))


class Client:
    def __init__(self, base_url: str | None = None, timeout: float = 30.0):
        self.base_url = (base_url or os.environ.get(
            "LEDGER_ORDER_FEED_URL", "http://127.0.0.1:8001/api/integration/ledger/v1",
        )).rstrip("/")
        self.timeout = timeout
        self.token = os.environ.get("LEDGER_ORDER_FEED_TOKEN", "")

    def get(self, path: str, params: dict[str, object] | None = None) -> dict[str, Any]:
        url = self.base_url + "/" + path.lstrip("/")
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            url += "?" + query
        return self._get_url(url)

    def get_href(self, href: str) -> dict[str, Any]:
        if not href.startswith("/"):
            return self.get(href)
        origin = self.base_url.split("/api/integration/", 1)[0]
        return self._get_url(origin + href)

    def revision(self, etag: str = "") -> tuple[dict[str, Any] | None, str]:
        """Cheap hot-path probe.  ``None`` means the source returned HTTP 304."""
        url = self.base_url + "/revision"
        headers = self._headers()
        if etag:
            headers["If-None-Match"] = etag
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=self.timeout,
            ) as response:
                return (
                    json.loads(response.read().decode("utf-8")),
                    str(response.headers.get("ETag") or etag),
                )
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return None, etag
            raise OrderFeedError(f"订单台 revision 不可用：HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise OrderFeedError(f"订单台 revision 不可用：{exc}") from exc

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-Integration-Token"] = self.token
        return headers

    def _get_url(self, url: str) -> dict[str, Any]:
        headers = self._headers()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise OrderFeedNotFound(f"订单台实体已不存在：{url}") from exc
            raise OrderFeedError(f"订单台接口不可用：HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise OrderFeedError(f"订单台接口不可用：{exc}") from exc


@dataclass(slots=True)
class SyncResult:
    snapshot_id: str = ""
    consumed_seq: int = 0
    source_revision: int = 0
    snapshot_changed: bool = False
    changes: int = 0
    caught_up: bool = False
    affected_stores: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


class OrderFeed:
    def __init__(
        self,
        workspace_root: Path,
        *,
        client: Client | None = None,
        feed_root: Path | None = None,
    ):
        self.workspace_root = Path(workspace_root)
        self.feed_root = Path(feed_root) if feed_root is not None else _root()
        self.client = client or Client()
        self.db_path = self.workspace_root / "order-feed.db"
        self._guard = threading.RLock()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_DB_SCHEMA)
            have = {row["name"] for row in conn.execute("pragma table_info(feed_state)")}
            for name, declaration in _STATE_COLUMNS.items():
                if name not in have:
                    conn.execute(f"alter table feed_state add column {name} {declaration}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma busy_timeout=30000")
        return conn

    def state(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("select * from feed_state where id=1").fetchone()
        return dict(row) if row else {}

    def record_error(self, message: str) -> None:
        with self._connect() as conn:
            conn.execute("update feed_state set last_error=? where id=1", (message[:2000],))

    def status(self) -> dict[str, Any]:
        state = self.state()
        for key in ("health_json", "manifest_json"):
            raw = state.pop(key, "")
            state[key[:-5] if key.endswith("_json") else key] = json.loads(raw) if raw else {}
        state["enabled"] = True
        return state

    def sync(self, *, max_pages: int = 20, limit: int = 500) -> SyncResult:
        """Advance only after a page and every referenced entity are durable locally."""
        with self._guard:
            try:
                return self._sync(max_pages=max_pages, limit=limit)
            except Exception as exc:
                with self._connect() as conn:
                    conn.execute("update feed_state set last_error=? where id=1", (str(exc),))
                raise

    def _sync(self, *, max_pages: int, limit: int) -> SyncResult:
        old = self.state()
        revision, revision_etag = self.client.revision(str(old.get("revision_etag") or ""))
        source_revision = int(old.get("source_revision") or 0)
        source_latest_seq = int(old.get("source_latest_seq") or 0)
        consumed_seq = int(old.get("consumed_seq") or 0)
        health = json.loads(old.get("health_json") or "{}")
        changed = False
        stores: dict[str, Any] | None = None

        if revision is not None:
            if revision.get("schema_version") != SCHEMA_VERSION:
                raise OrderFeedError(f"订单台合同版本不是 {SCHEMA_VERSION}")
            source_revision = int(revision.get("revision") or 0)
            source_latest_seq = int(revision.get("latest_seq") or 0)
            advanced = (
                source_revision > int(old.get("source_revision") or 0)
                or source_latest_seq > consumed_seq
            )
            self._record_revision_probe(source_revision, source_latest_seq, revision_etag)
        else:
            # A previous bounded replay may still have local backlog even though the
            # source ETag has not moved.  Continue from the durable checkpoint.
            advanced = consumed_seq < source_latest_seq

        if not advanced:
            if self._stores_due(old):
                self._refresh_stores(self.client.get("stores"))
            return SyncResult(
                snapshot_id=str(old.get("snapshot_id") or ""),
                consumed_seq=consumed_seq,
                source_revision=source_revision,
                caught_up=True,
            )

        if revision is not None:
            health = self.client.get("health")
            if not revision.get("healthy") or not health.get("healthy"):
                self._refresh_health(
                    health, source_revision, source_latest_seq, revision_etag,
                )
                raise OrderFeedError(
                    "订单台数据源未就绪：" + "、".join(health.get("degraded") or [])
                )
            announced_snapshot = str(health.get("last_successful_snapshot") or "")
            changed = not old.get("snapshot_id") or (
                bool(announced_snapshot) and announced_snapshot != old.get("snapshot_id")
            )
            if changed:
                manifest = self.client.get("snapshot")
                self._validate_manifest(manifest, full=True)
                stores = self.client.get("stores")
                self._install_snapshot(
                    manifest, stores, health,
                    source_revision=source_revision,
                    source_latest_seq=source_latest_seq,
                    revision_etag=revision_etag,
                )
            else:
                self._refresh_health(
                    health, source_revision, source_latest_seq, revision_etag,
                )
        elif health and not health.get("healthy"):
            raise OrderFeedError(
                "订单台数据源未就绪：" + "、".join(health.get("degraded") or [])
            )

        current = self.state()
        manifest = json.loads(current.get("manifest_json") or "{}")
        if not manifest:
            raise OrderFeedError("订单台快照尚未安装")
        if not changed and self._stores_due(current):
            self._refresh_stores(self.client.get("stores"))
            current = self.state()

        result = SyncResult(
            snapshot_id=str(manifest["snapshot_id"]),
            consumed_seq=int(current.get("consumed_seq") or 0),
            source_revision=source_revision,
            snapshot_changed=changed,
            warnings=[str(r.get("detail") or r.get("id")) for r in health.get("quality_risks") or []],
        )
        if changed:
            assert stores is not None
            result.affected_stores.update(
                str(s["ledger_store_id"])
                for s in stores.get("stores") or []
                if s.get("mapping_status") == "confirmed" and s.get("ledger_store_id")
            )
        for _ in range(max_pages):
            page = self.client.get("changes", {"after_seq": result.consumed_seq, "limit": limit})
            changes = page.get("changes") or []
            if not changes:
                result.caught_up = True
                break
            workers = max(1, int(os.environ.get("LEDGER_ORDER_FEED_FETCHERS", "8")))
            hrefs = {self._fetch_href(change) for change in changes if change.get("operation") != "delete"}
            fetched: dict[str, dict[str, Any] | None] = {}

            def fetch_href(href: str):
                try:
                    return href, self.client.get_href(href)
                except OrderFeedNotFound:
                    return href, None

            with ThreadPoolExecutor(max_workers=workers) as pool:
                for href, payload in pool.map(fetch_href, sorted(hrefs)):
                    fetched[href] = payload
            entities = [self._entity_from_fetch(change, fetched) for change in changes]
            if self._store_changes_require_refresh(entities) or self._stores_due(self.state()):
                self._refresh_stores(self.client.get("stores"))
            affected = self._commit_page(
                page, entities, health, source_revision, source_latest_seq, revision_etag,
            )
            result.affected_stores.update(affected)
            result.changes += len(changes)
            result.consumed_seq = int(page["to_seq"])
            if not page.get("has_more"):
                result.caught_up = True
                break
        return result

    @staticmethod
    def _fetch_href(change: dict[str, Any]) -> str:
        kind = str(change.get("entity_type") or "")
        if kind in {"order", "order_item"} and change.get("order_href"):
            return str(change["order_href"])
        if kind == "order_cost" and change.get("order_id"):
            return f"/api/orders/{urllib.parse.quote(str(change['order_id']), safe='')}/cost-history"
        href = str(change.get("entity_href") or "")
        if not href:
            raise OrderFeedError(
                f"增量 {change.get('seq')} {change.get('entity_type')} 没有 entity_href"
            )
        return href

    def _entity_from_fetch(
        self, change: dict[str, Any], fetched: dict[str, dict[str, Any] | None],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if change.get("operation") == "delete":
            return change, None
        href = self._fetch_href(change)
        got = fetched.get(href)
        if got is None:
            return {**change, "operation": "delete"}, None
        kind = str(change.get("entity_type") or "")
        if kind == "order":
            record = got.get("order")
            return (change, {"order": record}) if record else ({**change, "operation": "delete"}, None)
        if kind == "order_item":
            wanted = str(change.get("sub_order_id") or change.get("entity_id") or "")
            record = next(
                (row for row in got.get("items") or [] if str(row.get("sub_order_id") or "") == wanted),
                None,
            )
            return (change, {"order_item": record}) if record else ({**change, "operation": "delete"}, None)
        if kind == "order_cost":
            wanted = str(change.get("sub_order_id") or change.get("entity_id") or "")
            record = next(
                (row for row in got.get("costs") or [] if str(row.get("sub_order_id") or "") == wanted),
                None,
            )
            return (change, {"cost": record}) if record else ({**change, "operation": "delete"}, None)
        return change, got

    def _store_changes_require_refresh(
        self, entities: list[tuple[dict[str, Any], dict[str, Any] | None]],
    ) -> bool:
        """Ignore noisy last-seen store events; refresh on identity/mapping changes.

        Order Console currently emits one store pointer alongside ordinary order upserts.
        Pulling the 17-second registry for those is equivalent to polling it every page.
        The entity payload is enough to prove whether any mapping-significant field moved.
        """
        store_events = [pair for pair in entities if pair[0].get("entity_type") == "store"]
        if not store_events:
            return False
        with self._connect() as conn:
            current = {
                str(row["order_store_id"]): json.loads(row["payload_json"])
                for row in conn.execute(
                    "select order_store_id,payload_json from feed_store"
                )
            }
        identity_fields = (
            "ledger_store_id", "mapping_status", "jst_shop_id", "shop_name",
            "platform_code", "aliases", "active",
        )
        for change, payload in store_events:
            order_store_id = str(change.get("order_store_id") or change.get("entity_id") or "")
            if change.get("operation") == "delete" or order_store_id not in current:
                return True
            record = (payload or {}).get("store") or {}
            existing = current[order_store_id]
            if any(
                field in record and record.get(field) != existing.get(field)
                for field in identity_fields
            ):
                return True
        return False

    def _validate_manifest(self, manifest: dict[str, Any], *, full: bool = True) -> None:
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise OrderFeedError("快照合同版本不匹配")
        through = manifest.get("through_seq")
        if not isinstance(through, int) or through < 0:
            raise OrderFeedError("快照缺少原子 through_seq")
        required = {
            "stores.parquet", "orders.parquet", "order_items.parquet", "after_sales.parquet",
            "after_sale_items.parquet", "order_costs.parquet", "order_relations.parquet",
            "controls.parquet",
        }
        objects = manifest.get("objects") or {}
        missing = required - set(objects)
        if missing:
            raise OrderFeedError("快照缺对象：" + "、".join(sorted(missing)))
        root = self.feed_root.resolve()
        for name, meta in objects.items():
            path = (root / str(meta["path"])).resolve()
            if path != root and root not in path.parents:
                raise OrderFeedError(f"快照路径越界：{name}")
            if not path.is_file():
                raise OrderFeedError(f"快照对象不存在：{name}")
            if path.stat().st_size != int(meta["bytes"]):
                raise OrderFeedError(f"快照对象大小不符：{name}")
            if not full:
                continue
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != meta["sha256"]:
                raise OrderFeedError(f"快照对象SHA不符：{name}")
            rows = int(pl.scan_parquet(path).select(pl.len()).collect().item())
            if rows != int(meta["rows"]):
                raise OrderFeedError(f"快照对象行数不符：{name}")

    def _install_snapshot(
        self,
        manifest: dict[str, Any],
        stores: dict[str, Any],
        health: dict[str, Any],
        *,
        source_revision: int,
        source_latest_seq: int,
        revision_etag: str,
    ) -> None:
        confirmed = [s for s in stores.get("stores") or [] if s.get("mapping_status") == "confirmed"]
        if len(confirmed) != len(stores.get("stores") or []):
            raise OrderFeedError("订单台仍有未确认店铺映射")
        with self._connect() as conn:
            conn.execute("begin immediate")
            conn.execute("delete from feed_entity")
            conn.execute("delete from feed_store")
            conn.executemany(
                "insert into feed_store(order_store_id,ledger_store_id,mapping_status,payload_json) values(?,?,?,?)",
                [
                    (str(s["order_store_id"]), str(s["ledger_store_id"]), str(s["mapping_status"]),
                     json.dumps(s, ensure_ascii=False, separators=(",", ":")))
                    for s in confirmed
                ],
            )
            conn.execute(
                "update feed_state set schema_version=?,snapshot_id=?,snapshot_revision=?,"
                "snapshot_through_seq=?,consumed_seq=?,source_revision=?,source_latest_seq=?,"
                "revision_etag=?,stores_refreshed_at=?,manifest_json=?,health_json=?,"
                "last_success=?,last_error='' where id=1",
                (
                    SCHEMA_VERSION, str(manifest["snapshot_id"]), int(manifest["revision"]),
                    int(manifest["through_seq"]), int(manifest["through_seq"]),
                    source_revision, source_latest_seq, revision_etag, int(time.time()),
                    json.dumps(manifest, ensure_ascii=False),
                    json.dumps(health, ensure_ascii=False), _now(),
                ),
            )

    @staticmethod
    def _store_refresh_seconds() -> int:
        configured = int(os.environ.get("LEDGER_ORDER_FEED_STORE_REFRESH_SECONDS", "600"))
        return min(900, max(300, configured))

    def _stores_due(self, state: dict[str, Any]) -> bool:
        return time.time() - int(state.get("stores_refreshed_at") or 0) >= self._store_refresh_seconds()

    def _record_revision_probe(
        self, source_revision: int, source_latest_seq: int, revision_etag: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "update feed_state set source_revision=?,source_latest_seq=?,revision_etag=? where id=1",
                (source_revision, source_latest_seq, revision_etag),
            )

    def _refresh_health(
        self,
        health: dict[str, Any],
        source_revision: int,
        source_latest_seq: int,
        revision_etag: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "update feed_state set source_revision=?,source_latest_seq=?,revision_etag=?,"
                "health_json=?,last_success=?,last_error='' where id=1",
                (
                    source_revision, source_latest_seq, revision_etag,
                    json.dumps(health, ensure_ascii=False), _now(),
                ),
            )

    def _refresh_stores(self, stores: dict[str, Any]) -> None:
        confirmed = [s for s in stores.get("stores") or [] if s.get("mapping_status") == "confirmed"]
        if len(confirmed) != len(stores.get("stores") or []):
            raise OrderFeedError("订单台仍有未确认店铺映射")
        with self._connect() as conn:
            conn.execute("begin immediate")
            conn.execute("delete from feed_store")
            conn.executemany(
                "insert into feed_store(order_store_id,ledger_store_id,mapping_status,payload_json) values(?,?,?,?)",
                [
                    (str(s["order_store_id"]), str(s["ledger_store_id"]), str(s["mapping_status"]),
                     json.dumps(s, ensure_ascii=False, separators=(",", ":")))
                    for s in confirmed
                ],
            )
            conn.execute(
                "update feed_state set stores_refreshed_at=?,last_success=?,last_error='' where id=1",
                (int(time.time()), _now()),
            )

    def _commit_page(
        self,
        page: dict[str, Any],
        entities: list[tuple[dict[str, Any], dict[str, Any] | None]],
        health: dict[str, Any],
        source_revision: int,
        source_latest_seq: int,
        revision_etag: str,
    ) -> set[str]:
        affected: set[str] = set()
        with self._connect() as conn:
            conn.execute("begin immediate")
            mapping = {
                str(r["order_store_id"]): str(r["ledger_store_id"])
                for r in conn.execute("select order_store_id,ledger_store_id from feed_store")
            }
            for change, payload in entities:
                order_store_id = str(change.get("order_store_id") or "")
                if order_store_id in mapping:
                    affected.add(mapping[order_store_id])
                conn.execute(
                    "insert into feed_entity(entity_type,entity_id,seq,revision,operation,order_store_id,"
                    "order_id,sub_order_id,sku_id,payload_json) values(?,?,?,?,?,?,?,?,?,?) "
                    "on conflict(entity_type,entity_id) do update set seq=excluded.seq,revision=excluded.revision,"
                    "operation=excluded.operation,order_store_id=excluded.order_store_id,order_id=excluded.order_id,"
                    "sub_order_id=excluded.sub_order_id,sku_id=excluded.sku_id,payload_json=excluded.payload_json",
                    (
                        str(change["entity_type"]), str(change["entity_id"]), int(change["seq"]),
                        int(change["revision"]), str(change["operation"]), order_store_id or None,
                        str(change.get("order_id") or "") or None,
                        str(change.get("sub_order_id") or "") or None,
                        str(change.get("sku_id") or "") or None,
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if payload else None,
                    ),
                )
            conn.execute(
                "update feed_state set consumed_seq=?,source_revision=?,source_latest_seq=?,revision_etag=?,"
                "health_json=?,last_success=?,last_error='' where id=1",
                (
                    int(page["to_seq"]), source_revision, source_latest_seq, revision_etag,
                    json.dumps(health, ensure_ascii=False), _now(),
                ),
            )
        return affected

    def fingerprint(self) -> str:
        state = self.state()
        return f"order-feed:{state.get('snapshot_id','')}:{int(state.get('consumed_seq') or 0)}"

    def append_to(self, ingestion: Ingestion, store: Store) -> None:
        """Overlay live facts while preserving human-certified platform identities.

        Existing order exports keep their platform product/sub-order identifiers: those are
        what settlement and promotion files link to.  Order Console enriches their mutable
        status fields and contributes orders absent from the export.  Cost and after-sale
        sources are replaced outright because the feed is the more current operational fact.
        """
        state = self.state()
        if not state.get("snapshot_id") or not state.get("manifest_json"):
            raise OrderFeedError("订单台快照尚未同步")
        frames = self._frames(store, json.loads(state["manifest_json"]))
        feed_order = next(item for item in frames if item.recognition.source_id == "order_detail")
        self._enrich_existing_orders(ingestion, feed_order.frame)
        assert feed_order.frame is not None
        has_suborders = any(
            item.frame is not None and "sub_order_id" in item.frame.columns
            for item in ingestion.frames_of("order_detail")
        )
        key = "sub_order_id" if has_suborders else "order_id"
        existing_keys: set[str] = set()
        for item in ingestion.frames_of("order_detail"):
            if item.frame is not None and key in item.frame.columns:
                existing_keys.update(
                    str(value) for value in item.frame.get_column(key).drop_nulls().cast(pl.Utf8).to_list()
                )
        if existing_keys and key in feed_order.frame.columns:
            feed_order.frame = feed_order.frame.filter(
                ~pl.col(key).cast(pl.Utf8).is_in(existing_keys)
            )
            feed_order.rows = feed_order.frame.height
        ingestion.items = [
            item for item in ingestion.items
            if item.recognition.source_id not in REPLACED_SOURCES
        ]
        ingestion.items.extend(frames)
        for item in ingestion.known:
            assert item.frame is not None
            item.frame = item.frame.with_columns(pl.lit(True).alias("__live_period_scope__"))

    @staticmethod
    def _enrich_existing_orders(ingestion: Ingestion, feed: pl.DataFrame | None) -> None:
        if feed is None or feed.is_empty():
            return
        candidates = {
            "refund_status": "text", "tracking_no": "text", "order_state": "text",
            "order_time": "time", "pay_time": "time", "order_date": "time", "pay_date": "time",
        }
        for item in ingestion.frames_of("order_detail"):
            if item.frame is None or item.template is None:
                continue
            key = "sub_order_id" if "sub_order_id" in item.frame.columns else "order_id"
            if key not in item.frame.columns or key not in feed.columns:
                continue
            columns = [name for name in candidates if name in feed.columns]
            updates = feed.select(key, *columns).unique(subset=[key], maintain_order=True).rename(
                {name: f"__feed_{name}" for name in columns}
            )
            frame = item.frame.join(updates, on=key, how="left")
            expressions: list[pl.Expr] = []
            for name in columns:
                incoming = pl.col(f"__feed_{name}")
                if name in frame.columns:
                    # Mutable status comes from Order Console; timestamps preserve the
                    # certified export and use the feed only to fill a gap.
                    value = (
                        pl.coalesce(incoming, pl.col(name))
                        if name in {"refund_status", "tracking_no", "order_state"}
                        else pl.coalesce(pl.col(name), incoming)
                    )
                else:
                    value = incoming
                expressions.append(value.alias(name))
            item.frame = frame.with_columns(expressions).drop(
                [f"__feed_{name}" for name in columns]
            )
            roles = {binding.role for binding in item.template.bindings}
            extra = tuple(
                ColumnBinding(role=name, columns=(name,), required=False, kind=kind)
                for name, kind in candidates.items()
                if name in item.frame.columns and name not in roles
            )
            if extra:
                item.template = item.template.model_copy(
                    update={"bindings": item.template.bindings + extra}
                )

    def _frames(self, store: Store, manifest: dict[str, Any]) -> list[Ingested]:
        with self._connect() as conn:
            order_store_ids = [
                str(r[0]) for r in conn.execute(
                    "select order_store_id from feed_store where ledger_store_id=? and mapping_status='confirmed'",
                    (store.id,),
                )
            ]
            deltas = [dict(r) for r in conn.execute(
                "select * from feed_entity where order_store_id in (%s)"
                % ",".join("?" for _ in order_store_ids), order_store_ids,
            )] if order_store_ids else []
        if not order_store_ids:
            return []
        objects = manifest["objects"]
        path = lambda name: self.feed_root / objects[name]["path"]

        orders = pl.scan_parquet(path("orders.parquet")).filter(
            pl.col("order_store_id").cast(pl.Utf8).is_in(order_store_ids)
        ).collect()
        orders = self._overlay(orders, deltas, "order", "order_id", lambda p: [p.get("order") or {}])
        order_ids = orders.get_column("order_id").cast(pl.Utf8).unique().to_list()
        items = pl.scan_parquet(path("order_items.parquet")).filter(
            pl.col("order_id").cast(pl.Utf8).is_in(order_ids)
        ).collect()
        items = self._overlay(items, deltas, "order_item", "sub_order_id", lambda p: [p.get("order_item") or {}])
        costs = pl.scan_parquet(path("order_costs.parquet")).filter(
            pl.col("order_id").cast(pl.Utf8).is_in(order_ids)
        ).collect()
        costs = self._overlay(costs, deltas, "order_cost", "sub_order_id", lambda p: [p.get("cost") or p])
        relations = pl.scan_parquet(path("order_relations.parquet")).filter(
            pl.col("source_order_id").cast(pl.Utf8).is_in(order_ids)
            | pl.col("target_order_id").cast(pl.Utf8).is_in(order_ids)
        ).collect()
        relation_records: list[dict[str, Any]] = []
        for delta in deltas:
            if delta["entity_type"] != "order_relation" or delta["operation"] == "delete":
                continue
            if delta["payload_json"]:
                payload = json.loads(delta["payload_json"])
                record = payload.get("order_relation") or payload
                if record:
                    relation_records.append(record)
        if relation_records:
            relations = self._append_records(relations, relation_records).unique(maintain_order=True)
        after = pl.scan_parquet(path("after_sales.parquet")).filter(
            pl.col("order_store_id").cast(pl.Utf8).is_in(order_store_ids)
        ).collect()
        after = self._overlay(after, deltas, "after_sale", "after_sale_id", lambda p: [p.get("after_sale") or {}])
        after_ids = after.get_column("after_sale_id").cast(pl.Utf8).unique().to_list()
        after_items = pl.scan_parquet(path("after_sale_items.parquet")).filter(
            pl.col("after_sale_id").cast(pl.Utf8).is_in(after_ids)
        ).collect()
        # Child rows deliberately ride the parent after_sale event.
        parent_deltas = [d for d in deltas if d["entity_type"] == "after_sale"]
        replaced = [d["entity_id"] for d in parent_deltas]
        if replaced:
            after_items = after_items.filter(~pl.col("after_sale_id").cast(pl.Utf8).is_in(replaced))
        child_records: list[dict[str, Any]] = []
        for delta in parent_deltas:
            if delta["operation"] == "delete" or not delta["payload_json"]:
                continue
            payload = json.loads(delta["payload_json"])
            child_records.extend(payload.get("items") or [])
        after_items = self._append_records(after_items, child_records)

        fingerprint = self.fingerprint()
        order_frame = self._order_frame(orders, items, after, store, fingerprint)
        cost_frame = self._cost_frame(orders, items, costs, relations, store, fingerprint)
        after_frame = self._after_frame(after, after_items, store, fingerprint)
        return [
            self._item("order_detail", "order_console_order_v1", "订单台实时订单", order_frame,
                       self._order_template(), fingerprint),
            self._item("order_cost", "order_console_cost_v1", "订单台日期时点成本", cost_frame,
                       self._cost_template(), fingerprint),
            self._item("after_sales", "order_console_after_sale_v1", "订单台实时售后", after_frame,
                       self._after_template(), fingerprint),
        ]

    @staticmethod
    def _overlay(
        base: pl.DataFrame,
        deltas: list[dict[str, Any]],
        entity_type: str,
        key: str,
        extract: Callable[[dict[str, Any]], list[dict[str, Any]]],
    ) -> pl.DataFrame:
        selected = [d for d in deltas if d["entity_type"] == entity_type]
        ids = [str(d["entity_id"]) for d in selected]
        if ids and key in base.columns:
            base = base.filter(~pl.col(key).cast(pl.Utf8).is_in(ids))
        records: list[dict[str, Any]] = []
        for delta in selected:
            if delta["operation"] == "delete" or not delta["payload_json"]:
                continue
            records.extend(extract(json.loads(delta["payload_json"])))
        return OrderFeed._append_records(base, records)

    @staticmethod
    def _append_records(base: pl.DataFrame, records: list[dict[str, Any]]) -> pl.DataFrame:
        records = [r for r in records if r]
        if not records:
            return base
        incoming = pl.from_dicts(records, infer_schema_length=None)
        for name, dtype in base.schema.items():
            if name not in incoming.columns:
                incoming = incoming.with_columns(pl.lit(None, dtype=dtype).alias(name))
            else:
                incoming = incoming.with_columns(pl.col(name).cast(dtype, strict=False))
        return pl.concat([base, incoming.select(base.columns)], how="vertical_relaxed")

    @staticmethod
    def _dt(name: str) -> pl.Expr:
        return pl.col(name).cast(pl.Utf8).str.to_datetime(strict=False)

    @staticmethod
    def _anchors(frame: pl.DataFrame, fingerprint: str, label: str) -> pl.DataFrame:
        return frame.with_row_index(ANCHOR_ROW, offset=1).with_columns(
            pl.lit(hashlib.sha256(fingerprint.encode()).hexdigest()).alias(ANCHOR_SHA),
            pl.lit(label).alias(ANCHOR_FILE),
            pl.lit("订单台").alias(ANCHOR_SHEET),
        )

    def _order_frame(
        self, orders: pl.DataFrame, items: pl.DataFrame, after: pl.DataFrame,
        store: Store, fingerprint: str,
    ) -> pl.DataFrame:
        refund = after.group_by("order_id").agg(
            pl.col("online_status_raw").drop_nulls().last().alias("refund_status")
        ) if not after.is_empty() else pl.DataFrame(schema={"order_id": pl.Utf8, "refund_status": pl.Utf8})
        frame = items.join(orders, on="order_id", how="inner", suffix="_order").join(
            refund, on="order_id", how="left",
        ).select(
            pl.col("online_order_no_order").fill_null(pl.col("online_order_no")).cast(pl.Utf8).alias("order_id"),
            pl.col("outer_sku").fill_null(pl.col("sub_order_id")).cast(pl.Utf8).alias("sub_order_id"),
            pl.col("merchant_sku").fill_null(pl.col("sku_id")).cast(pl.Utf8).alias("product_id"),
            pl.col("product_name").cast(pl.Utf8),
            pl.col("paid_amount").cast(pl.Float64, strict=False).alias("buyer_paid"),
            pl.col("refund_amount").cast(pl.Float64, strict=False),
            pl.col("refund_status").cast(pl.Utf8).fill_null("没有申请退款"),
            pl.col("tracking_no").fill_null(pl.col("tracking_no_order")).cast(pl.Utf8),
            pl.col("order_status_raw").cast(pl.Utf8).alias("order_state"),
            self._dt("order_time").alias("order_time"),
            self._dt("pay_time").alias("pay_time"),
        ).group_by("order_id", "sub_order_id", maintain_order=True).agg(
            pl.col("product_id").drop_nulls().first(),
            pl.col("product_name").drop_nulls().first(),
            pl.lit(1.0).alias("quantity"),
            pl.col("buyer_paid").sum(),
            pl.col("refund_amount").sum(),
            pl.col("refund_status").drop_nulls().last(),
            pl.col("tracking_no").drop_nulls().first(),
            pl.col("order_state").drop_nulls().first(),
            pl.col("order_time").drop_nulls().first(),
            pl.col("pay_time").drop_nulls().first(),
        ).with_columns(
            pl.lit(store.name).alias("store_name"),
            pl.col("order_time").alias("order_date"),
            pl.col("pay_time").alias("pay_date"),
        )
        return self._anchors(frame, fingerprint, "订单台实时订单")

    def _cost_frame(
        self, orders: pl.DataFrame, items: pl.DataFrame, costs: pl.DataFrame,
        relations: pl.DataFrame,
        store: Store, fingerprint: str,
    ) -> pl.DataFrame:
        certified = costs.filter(
            (pl.col("cost_status") == "priced")
            & pl.col("cost_source").is_in(["history", "mirror", "scrape", "unknown_evidence"])
        )
        reships = (
            relations.filter(pl.col("relation_type") == "reship")
            .get_column("target_order_id").cast(pl.Utf8).drop_nulls().unique().to_list()
            if not relations.is_empty() and {"relation_type", "target_order_id"} <= set(relations.columns)
            else []
        )
        frame = certified.join(
            orders.select("order_id", "online_order_no", "order_time", "order_status_raw", "tracking_no"),
            on="order_id", how="left",
        ).join(
            items.select("order_id", "sub_order_id", "outer_sku", "tracking_no").rename({"tracking_no": "item_tracking_no"}),
            on=["order_id", "sub_order_id"], how="left",
        ).select(
            pl.col("order_id").cast(pl.Utf8).alias("internal_order_id"),
            pl.col("online_order_no").cast(pl.Utf8).alias("order_id"),
            pl.col("online_order_no").cast(pl.Utf8).alias("original_order_id"),
            pl.col("outer_sku").fill_null(pl.col("sub_order_id")).cast(pl.Utf8).alias("sub_order_id"),
            pl.col("sku_id").cast(pl.Utf8).alias("sku"),
            pl.col("quantity").cast(pl.Float64, strict=False),
            pl.col("unit_cost").cast(pl.Float64, strict=False),
            pl.col("cost_amount").cast(pl.Float64, strict=False).alias("total_cost"),
            pl.col("item_tracking_no").fill_null(pl.col("tracking_no")).cast(pl.Utf8).alias("tracking_no"),
            pl.col("order_status_raw").cast(pl.Utf8).alias("order_state"),
            self._dt("order_time").alias("order_time"),
            pl.when(pl.col("order_id").cast(pl.Utf8).is_in(reships))
            .then(pl.lit("补发订单")).otherwise(pl.lit("销售订单")).alias("order_type"),
            pl.lit(store.name).alias("store_name"),
        ).with_columns(pl.col("order_time").alias("order_date"))
        return self._anchors(frame, fingerprint, "订单台日期时点成本")

    def _after_frame(
        self, after: pl.DataFrame, items: pl.DataFrame, store: Store, fingerprint: str,
    ) -> pl.DataFrame:
        frame = after.join(items, on="after_sale_id", how="left", suffix="_item").select(
            pl.col("after_sale_id").cast(pl.Utf8),
            pl.col("order_id").cast(pl.Utf8).alias("internal_order_id"),
            pl.col("online_order_no").cast(pl.Utf8).alias("order_id"),
            pl.coalesce(pl.col("sub_order_id_item"), pl.col("sub_order_id")).cast(pl.Utf8).alias("sub_order_id"),
            pl.col("sku_id").cast(pl.Utf8).alias("sku"),
            pl.col("goods_status_raw").cast(pl.Utf8).alias("goods_status"),
            pl.col("online_status_raw").cast(pl.Utf8).alias("refund_status"),
            pl.col("refund_status_raw").cast(pl.Utf8).alias("after_sale_status"),
            pl.col("quantity").cast(pl.Float64, strict=False),
            pl.lit(store.name).alias("store_name"),
        )
        return self._anchors(frame, fingerprint, "订单台实时售后")

    @staticmethod
    def _template(template_id: str, source: str, roles: list[tuple[str, str]]) -> Template:
        return Template(
            id=template_id, source=source, name=template_id, match_columns=(roles[0][0],),
            bindings=tuple(ColumnBinding(role=role, columns=(role,), kind=kind) for role, kind in roles),
            time_slots={
                slot: role for slot, role in (("order_date", "order_time"), ("pay_date", "pay_time"))
                if any(candidate == role for candidate, _ in roles)
            },
        )

    @classmethod
    def _order_template(cls) -> Template:
        return cls._template("order_console_order_v1", "order_detail", [
            ("order_id", "text"), ("sub_order_id", "text"), ("product_id", "text"),
            ("product_name", "text"), ("quantity", "number"), ("buyer_paid", "number"),
            ("refund_amount", "number"), ("refund_status", "text"), ("tracking_no", "text"),
            ("order_state", "text"), ("order_time", "time"), ("pay_time", "time"),
            ("store_name", "text"),
        ])

    @classmethod
    def _cost_template(cls) -> Template:
        return cls._template("order_console_cost_v1", "order_cost", [
            ("internal_order_id", "text"), ("order_id", "text"), ("original_order_id", "text"),
            ("sub_order_id", "text"), ("sku", "text"), ("quantity", "number"),
            ("unit_cost", "number"), ("total_cost", "number"), ("tracking_no", "text"),
            ("order_state", "text"), ("order_time", "time"), ("order_type", "text"),
            ("store_name", "text"),
        ])

    @classmethod
    def _after_template(cls) -> Template:
        return cls._template("order_console_after_sale_v1", "after_sales", [
            ("after_sale_id", "text"), ("internal_order_id", "text"), ("order_id", "text"),
            ("sub_order_id", "text"), ("sku", "text"), ("goods_status", "text"),
            ("refund_status", "text"), ("after_sale_status", "text"), ("quantity", "number"),
            ("store_name", "text"),
        ])

    @staticmethod
    def _item(
        source_id: str, template_id: str, name: str, frame: pl.DataFrame,
        template: Template, fingerprint: str,
    ) -> Ingested:
        ref = FileRef(hashlib.sha256(fingerprint.encode()).hexdigest(), name, "订单台")
        return Ingested(
            ref=ref,
            recognition=Recognition(
                ref=ref, signature=template.signature, header_count=len(frame.columns),
                template_id=template_id, source_id=source_id, reason="订单台 ledger-feed.v1",
            ),
            rows=frame.height, frame=frame, template=template,
            notes=[f"订单台证据 {fingerprint}"],
        )


class Worker:
    def __init__(
        self,
        feed: OrderFeed,
        on_stores: Callable[[set[str], str], None] | None = None,
        interval: float | None = None,
    ):
        self.feed = feed
        self.on_stores = on_stores
        self.interval = interval or float(os.environ.get("LEDGER_ORDER_FEED_POLL_SECONDS", "10"))
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.pending_stores: set[str] = set()

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="ledger-order-feed", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=15)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                result = self.feed.sync()
                self.pending_stores.update(result.affected_stores)
                if result.caught_up and self.pending_stores and self.on_stores:
                    self.on_stores(set(self.pending_stores), self.feed.fingerprint())
                    self.pending_stores.clear()
            except Exception as exc:
                # status() carries the exact failure; a transient source outage must not kill Ledger.
                self.feed.record_error(str(exc))
            self.stop_event.wait(self.interval)


__all__ = [
    "Client", "OrderFeed", "OrderFeedError", "OrderFeedNotFound",
    "SyncResult", "Worker", "enabled",
]
