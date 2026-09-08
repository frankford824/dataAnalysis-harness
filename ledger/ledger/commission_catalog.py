"""Read the public Order Console catalog contract; never access orderdb."""
from __future__ import annotations

import json
import polars as pl

from .commission_registry import Registry, RegistryError, json_text, now
from .order_feed import Client


def refresh(registry: Registry, model, client=None) -> dict:
    client = client or Client()
    source_stores = client.get("stores")
    stores = source_stores.get("stores")
    if not isinstance(stores, list) or not stores:
        raise RegistryError("订单侧未返回店铺目录，保留现有目录")
    known = {s.id for s in model.stores}
    mapping = {str(s["order_store_id"]): str(s["ledger_store_id"]) for s in stores
               if s.get("mapping_status") == "confirmed" and s.get("ledger_store_id") in known}
    users = client.get("users", {"include_disabled": "true", "include_removed": "true"})
    cursor = ""
    seen = set()
    rows = []
    refreshed_at = None
    for _ in range(2000):
        page = client.get("shop-listings", {"order": "key", "limit": 1000, "after_key": cursor})
        stamp = page.get("refreshed_at")
        if refreshed_at is not None and stamp != refreshed_at:
            raise RegistryError("目录在翻页期间更新，请重新同步；原目录未改变")
        refreshed_at = stamp
        for item in page.get("listings", []):
            oid = str(item.get("order_store_id") or item.get("shop_id") or "")
            pid = str(item.get("shop_item_id") or "")
            if not oid or not pid or (oid, pid) in seen:
                raise RegistryError("目录存在空键或重复店铺商品键，未保存")
            seen.add((oid, pid))
            rows.append((mapping.get(oid, "unmapped:" + oid), pid, item.get("listing_name") or "",
                         oid, json_text(item), stamp or now()))
        if not page.get("has_more"):
            break
        following = page.get("next_after_key")
        if not following or following == cursor:
            raise RegistryError("目录分页缺少有效游标，未保存不完整数据")
        cursor = following
    else:
        raise RegistryError("目录超过分页上限，未保存不完整数据")
    if not rows:
        raise RegistryError("商品目录为空，保留现有目录")
    with registry.transaction() as conn:
        keys = {(r[0], r[1]) for r in rows}
        observed = [tuple(r) for r in conn.execute(
            "SELECT * FROM catalog WHERE payload LIKE '%\"origin\":\"order_observation\"%'")
                    if (r["store_id"], r["product_id"]) not in keys]
        conn.execute("DELETE FROM catalog")
        conn.executemany("INSERT INTO catalog VALUES(?,?,?,?,?,?)", rows + observed)
        conn.execute("DELETE FROM external_person")
        conn.executemany("INSERT INTO external_person VALUES(?,?)", [
            (str(u["user_id"]), json_text({k: u.get(k) for k in
             ["user_id", "user_name", "enabled", "removed_at", "dept_names", "owned_shop_ids", "operated_shop_ids"]}))
            for u in users.get("users", [])
        ])
    return {"products": len(rows), "refreshed_at": refreshed_at,
            "recent_order_products": len(observed),
            "unmapped_products": sum(r[0].startswith("unmapped:") for r in rows),
            "users": len(users.get("users", []))}


def observe(registry: Registry, store, spine) -> int:
    """New ordered listings are visible immediately, even before the daily catalog rebuild."""
    if not isinstance(spine, pl.DataFrame) or spine.is_empty() or not {"store", "product_id"} <= set(spine.columns):
        return 0
    rows = spine.filter(pl.col("store").is_in([store.id, store.name, *store.aliases]))
    if "product_name" not in rows.columns:
        rows = rows.with_columns(pl.lit("", dtype=pl.Utf8).alias("product_name"))
    rows = rows.select(pl.col("product_id").cast(pl.Utf8), pl.col("product_name").cast(pl.Utf8))
    rows = rows.filter(pl.col("product_id").str.contains(r"^\d{9,20}$")).unique(subset=["product_id"], keep="first")
    stamp = now()
    with registry.transaction() as conn:
        known = {r[0] for r in conn.execute("SELECT product_id FROM catalog WHERE store_id=?", (store.id,))}
        fresh = [r for r in rows.iter_rows(named=True) if r["product_id"] not in known]
        conn.executemany("INSERT OR IGNORE INTO catalog VALUES(?,?,?,?,?,?)", [
            (store.id, r["product_id"], r["product_name"] or "", "", json_text({"origin": "order_observation",
             "listed": None, "observed_at": stamp}), stamp) for r in fresh])
    return len(fresh)


def products(registry: Registry, *, store_id="", search="", missing=False, after="", limit=100) -> dict:
    clauses = ["c.product_id>?", "(?='' OR c.store_id=?)",
               "(?='' OR c.product_id LIKE ? OR c.product_name LIKE ?)"]
    args = [after, store_id, store_id, search, "%" + search + "%", "%" + search + "%"]
    if missing:
        clauses.append("s.active_version IS NULL")
    # Cursor for a single store; all-store pages use a compound key to avoid
    # losing the same product ID appearing in several stores.
    clauses[0] = "(c.store_id || char(31) || c.product_id)>?"
    with registry.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT c.*,s.id AS scheme_id,s.revision,s.active_version,s.draft_version FROM catalog c "
            "LEFT JOIN scheme s ON s.store_id=c.store_id AND s.product_id=c.product_id WHERE "
            + " AND ".join(clauses) + " ORDER BY c.store_id,c.product_id LIMIT ?", (*args, limit + 1))]
    has_more = len(rows) > limit
    rows = rows[:limit]
    for row in rows:
        row["payload"] = json.loads(row["payload"])
    return {"products": rows, "has_more": has_more,
            "next_after": rows[-1]["store_id"] + "\x1f" + rows[-1]["product_id"] if rows and has_more else ""}
