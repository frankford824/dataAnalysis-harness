"""Durable background work for catalog refresh, imports and relationship changes."""
from __future__ import annotations

import json
import threading
import time
import sqlite3
import logging
from contextlib import closing

from . import commission_catalog, commission_import, order_feed, service
from .commission_registry import Registry, json_text


class Manager:
    def __init__(self, workspace, model):
        self.workspace = workspace
        self.model = model
        self.stop_event = threading.Event()
        self.thread = None
        self.last_catalog = 0.0
        self.current_pending = None
        self.last_error = ""
        self.last_error_store = None

    def start(self):
        registry = Registry(self.workspace().root)
        with registry.transaction() as conn:
            conn.execute("UPDATE job SET status='queued' WHERE status='running'")
        self.thread = threading.Thread(target=self.run, name="commission-manager", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=15)

    def once(self):
        self.current_pending = None
        ws = self.workspace()
        registry = Registry(ws.root)
        with registry.transaction() as conn:
            row = conn.execute("SELECT * FROM job WHERE status='queued' ORDER BY at LIMIT 1").fetchone()
            if row:
                conn.execute("UPDATE job SET status='running' WHERE id=?", (row["id"],))
        if row:
            job = dict(row)
            payload = json.loads(job["payload"])
            try:
                if job["kind"] == "catalog":
                    result = commission_catalog.refresh(registry, self.model())
                    self.last_catalog = time.time()
                elif job["kind"] == "import":
                    path = registry.root / "uploads" / payload["sha"]
                    raw = path.read_bytes()
                    import hashlib
                    if hashlib.sha256(raw).hexdigest() != payload["sha"]:
                        raise ValueError("原文件校验失败")
                    result = commission_import.stage(registry, self.model(), raw, payload["filename"],
                                                     payload["effective_from"], job["actor"])
                elif job["kind"] == "activate":
                    result = commission_import.activate(registry, payload["batch_id"], job["actor"], payload["reason"])
                else:
                    raise ValueError("未知任务类型")
                with registry.transaction() as conn:
                    conn.execute("UPDATE job SET status='done',result=?,error='' WHERE id=?", (json_text(result), job["id"]))
            except Exception as exc:
                with registry.transaction() as conn:
                    conn.execute("UPDATE job SET status='failed',error=? WHERE id=?", (str(exc)[:2000], job["id"]))
            return
        claimed_at = int(time.time())
        with registry.transaction() as conn:
            pending = conn.execute("SELECT * FROM pending WHERE next_attempt<=? ORDER BY next_attempt,revision LIMIT 1",
                                   (claimed_at,)).fetchone()
            if pending:
                conn.execute("UPDATE pending SET next_attempt=? WHERE store_id=? AND revision=? AND source_seq=? AND source_fingerprint=?",
                             (claimed_at + 180, pending["store_id"], pending["revision"],
                              pending["source_seq"], pending["source_fingerprint"]))
        if pending:
            if order_feed.enabled():
                feed_path = ws.root / "order-feed.db"
                if not feed_path.exists():
                    return
                with closing(sqlite3.connect(f"file:{feed_path.as_posix()}?mode=ro", uri=True)) as source:
                    state = source.execute("SELECT consumed_seq,source_latest_seq,snapshot_id FROM feed_state").fetchone()
                # This job requires its captured source prefix, not a global
                # head which keeps moving as unrelated shops receive orders.
                if not state or not state[2] or state[0] < pending["source_seq"]:
                    return
            store_id, revision = pending["store_id"], pending["revision"]
            self.current_pending = (store_id, revision)
            if order_feed.enabled():
                with closing(sqlite3.connect(f"file:{(ws.root / 'order-feed.db').as_posix()}?mode=ro", uri=True)) as source:
                    mapped = source.execute("SELECT 1 FROM feed_store WHERE ledger_store_id=? AND mapping_status='confirmed' LIMIT 1", (store_id,)).fetchone()
                if not mapped:
                    # Missing mappings cannot be repaired by rereading all
                    # source files. Retain the durable work with a clear cause.
                    error = f"订单台没有 {self.model().store(store_id).name} 的已确认店铺映射；确认映射后自动重试"
                    with registry.transaction() as conn:
                        conn.execute("UPDATE pending SET next_attempt=?,error=? WHERE store_id=? AND revision=? AND source_seq=? AND source_fingerprint=?",
                                     (int(time.time())+300,error,store_id,revision,pending['source_seq'],pending['source_fingerprint']))
                    return
            ws.note_external_version(store_id, "__commission_rules__", f"commission:{revision}")
            note = "订单数据更新" if pending["source_seq"] else "提成设置更新"
            result = service.recompute(ws, self.model(), self.model().store(store_id), note=note)
            if result.failure:
                why = result.failure.get("why") or str(result.failure)
                if "没有任何数据" not in why and "没算出结果" not in why:
                    raise ValueError(why)
                # No order inputs yet is a normal state for a newly registered
                # listing/store. Its next source revision will enqueue it again.
            provisional = any(p.get("source_sync_pending") for p in result.periods)
            with registry.transaction() as conn:
                if provisional:
                    retry_at = int(time.time()) + 60
                    changed = conn.execute("UPDATE pending SET next_attempt=? WHERE store_id=? AND revision<=? AND source_seq<=? AND source_fingerprint=?",
                                           (retry_at, store_id, revision, pending["source_seq"], pending["source_fingerprint"]))
                    if not changed.rowcount:
                        conn.execute("UPDATE pending SET next_attempt=min(next_attempt,?) WHERE store_id=?",
                                     (retry_at, store_id))
                else:
                    removed = conn.execute("DELETE FROM pending WHERE store_id=? AND revision<=? AND source_seq<=? AND source_fingerprint=?",
                                          (store_id, revision, pending["source_seq"], pending["source_fingerprint"]))
                    if not removed.rowcount:
                        conn.execute("UPDATE pending SET next_attempt=min(next_attempt,?) WHERE store_id=?",
                                     (int(time.time()) + 10, store_id))
                conn.execute("INSERT INTO job VALUES(?,?,?,?,?,?,?,?)",
                             (__import__("uuid").uuid4().hex, "recompute", "system",
                              __import__("datetime").datetime.now().isoformat(), "done",
                              json_text({"store_id": store_id}), json_text({"store_id": store_id,
                              "periods": len(result.periods), "waiting_for_orders": bool(result.failure), "provisional": provisional}), ""))
            return
        if order_feed.enabled() and time.time() - self.last_catalog > 12 * 3600:
            self.last_catalog = time.time()
            registry.enqueue("catalog", {}, "system")

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.once()
                self.last_error = ""
                self.last_error_store = None
            except Exception as exc:
                # Pending rows remain durable. The status endpoint exposes the
                # failure, and the next attempt never changes frozen results.
                self.last_error = str(exc)[:2000]
                self.last_error_store = self.current_pending[0] if self.current_pending else None
                logging.getLogger(__name__).exception("Recompute failed for %s; durable work will retry", self.current_pending)
                try:
                    registry = Registry(self.workspace().root)
                    with registry.transaction() as conn:
                        if self.current_pending:
                            conn.execute("UPDATE pending SET next_attempt=?,error=? WHERE store_id=? AND revision<=?",
                                         (int(time.time()) + 120, str(exc)[:2000], *self.current_pending))
                        conn.execute("INSERT INTO job VALUES(?,?,?,?,?,?,?,?)",
                                     (__import__("uuid").uuid4().hex, "recompute", "system",
                                      __import__("datetime").datetime.now().isoformat(), "failed", "{}", "{}", str(exc)[:2000]))
                except Exception:
                    logging.getLogger(__name__).exception("Unable to persist recompute failure; worker will retry")
                self.stop_event.wait(2)
            finally:
                self.current_pending = None
            self.stop_event.wait(2)
