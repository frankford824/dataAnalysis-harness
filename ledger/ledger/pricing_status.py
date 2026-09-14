"""Read-only explanation of saved cost results versus current background work."""
from pathlib import Path
import sqlite3
from contextlib import closing


def read_feed(root):
    path = Path(root) / "order-feed.db"
    if not path.exists():
        return None
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT snapshot_id,consumed_seq,source_latest_seq,last_success,last_error FROM feed_state WHERE id=1").fetchone()
        return dict(row) if row else None


def status(ws, store_id, period, manager=None, activity=None):
    result = ws.conn.execute("SELECT id,at FROM run WHERE store_id=? AND period=? ORDER BY id DESC LIMIT 1", (store_id, period)).fetchone()
    frozen = ws.conn.execute("SELECT state,run_id FROM period WHERE store_id=? AND period=?", (store_id, period)).fetchone()
    if frozen and frozen["state"] == "closed" and frozen["run_id"]:
        saved = ws.conn.execute("SELECT id,at FROM run WHERE id=?", (frozen["run_id"],)).fetchone()
        return {"run_id": saved["id"], "calculated_at": saved["at"], "state": "closed",
                "message": "已结账，当前展示冻结结果；后续核价不会改写这份历史账。"}
    out = {"run_id": result["id"] if result else None, "calculated_at": result["at"] if result else None,
           "state": "review", "message": "已完成最近一次核算。仍缺少的历史成本需继续核实。"}
    pending = None
    path = ws.root / "commission" / "registry.db"
    if path.exists():
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT next_attempt,error FROM pending WHERE store_id=?", (store_id,)).fetchone()
            pending = dict(row) if row else None
    feed = read_feed(ws.root)
    running = manager is not None and manager.current_pending and manager.current_pending[0] == store_id
    dead = manager is not None and (manager.thread is None or not manager.thread.is_alive())
    if activity:
        state = activity["state"]
        out.update(state=state, message=("正在核算：" if state == "running" else "本店已排队：") + activity["phase"] + "。完成后自动更新。")
    elif dead and pending:
        out.update(state="error", message="自动核算服务未运行，旧结果尚未更新。", error=manager.last_error)
    elif running:
        out.update(state="running", message="正在核算本店数据，完成后自动更新。")
    elif pending and manager is not None and manager.last_error and getattr(manager, "last_error_store", None) in (None, store_id):
        out.update(state="error", message="自动核算遇到错误，服务会重试；当前显示已保存的结果。", error=manager.last_error)
    elif pending and pending["error"]:
        out.update(state="error", message="上次自动核算失败，正在等待重试。", error=pending["error"])
    elif feed and feed["last_error"]:
        out.update(state="error", message="订单数据同步异常，当前显示已保存的结果。", error=feed["last_error"])
    elif feed and feed["consumed_seq"] < feed["source_latest_seq"]:
        out.update(state="syncing", message="订单数据仍在同步，当前为已同步数据的试算，暂不可结账。")
    elif pending:
        out.update(state="queued", message="新数据已收到，本店正在等待自动核算。")
    return out
