"""Read the public Order Console catalog contract; never access orderdb."""
from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict

import polars as pl

from datetime import datetime, timezone, timedelta

from .commission_registry import Registry, RegistryError, json_text, now, local_time, member_active_at
from .order_feed import Client

SEARCH_SPLIT = re.compile(r'[,，、;；|/\n\r]+')


def parse_search_tokens(search: str) -> list[str]:
    """Split pasted IDs on comma /顿号 / semicolon / newline; keep a single name intact."""
    raw = (search or '').strip()
    if not raw:
        return []
    tokens = [part.strip() for part in SEARCH_SPLIT.split(raw) if part.strip()]
    if len(tokens) == 1 and re.search(r'\s', tokens[0]):
        pieces = [part for part in re.split(r'\s+', tokens[0]) if part]
        if len(pieces) > 1 and all(re.fullmatch(r'\d{6,}', part) for part in pieces):
            return pieces
    return tokens


def product_search_sql(tokens: list[str], id_expr: str, name_expr: str) -> tuple[str, list[str]]:
    if not tokens:
        return '1', []
    if len(tokens) == 1:
        token = tokens[0]
        if re.fullmatch(r'\d{9,20}', token):
            return f'{id_expr}=?', [token]
        return f'(instr({id_expr},?)>0 OR instr({name_expr},?)>0)', [token, token]
    ids = [token for token in tokens if re.fullmatch(r'\d{6,}', token)]
    rest = [token for token in tokens if token not in ids]
    parts, params = [], []
    if ids:
        parts.append(f'{id_expr} IN ({",".join("?" for _ in ids)})')
        params.extend(ids)
    for token in rest:
        parts.append(f'(instr({id_expr},?)>0 OR instr({name_expr},?)>0)')
        params.extend([token, token])
    return f'({" OR ".join(parts)})', params


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


def _store_condition(stores):
    if not stores:
        return '1', []
    return 'store_id IN (' + ','.join('?' for _ in stores) + ')', list(stores)


_settings_lock = threading.Lock()
_page_cache: OrderedDict[tuple, dict] = OrderedDict()
_count_cache: OrderedDict[tuple, int] = OrderedDict()
_PAGE_CACHE_LIMIT = 48
_COUNT_CACHE_LIMIT = 24


def _settings_scope(registry: Registry, *, store_id="", search="", state="", at=None,
                    person_id="", store_ids=None, person_ids=None):
    moment = at or datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None).isoformat(timespec='seconds')
    with registry.connect() as conn:
        catalog_generation = conn.execute('SELECT generation FROM catalog_read_meta WHERE id=1').fetchone()[0]
        boundary = conn.execute('SELECT max(t) FROM (SELECT max(valid_from) t FROM scheme_read_segment WHERE valid_from<=? UNION ALL SELECT max(valid_to) t FROM scheme_read_segment WHERE valid_to<=?)', (moment,moment)).fetchone()[0]
    return (str(registry.root.resolve()), registry.revision(), store_id, search, state,
            at or boundary or '', person_id, tuple(sorted(store_ids or [])), tuple(sorted(person_ids or [])), catalog_generation)


def cached_count(registry: Registry, *, store_id="", search="", state="", at=None,
                 person_id="", store_ids=None, person_ids=None) -> int:
    key = _settings_scope(registry, store_id=store_id, search=search, state=state, at=at,
                          person_id=person_id, store_ids=store_ids, person_ids=person_ids)
    with _settings_lock:
        hit = _count_cache.get(key)
        if hit is not None:
            _count_cache.move_to_end(key)
            return hit
    total = count_settings(registry, store_id=store_id, search=search, state=state, at=at,
                           person_id=person_id, store_ids=store_ids, person_ids=person_ids)
    with _settings_lock:
        _count_cache[key] = total
        while len(_count_cache) > _COUNT_CACHE_LIMIT:
            _count_cache.popitem(last=False)
    return total


def count_settings(registry: Registry, *, store_id="", search="", state="", at=None, person_id="", store_ids=None, person_ids=None) -> int:
    """Count without decoding every scheme JSON when the filter does not need it."""
    stores = sorted(set(store_ids or ([store_id] if store_id else [])))
    persons = sorted(set(person_ids or ([person_id] if person_id else [])))
    tokens = parse_search_tokens(search)
    if persons and not state and not tokens:
        from .commission_read_index import person_keys
        moment = at or datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None).isoformat(timespec='seconds')
        sql, params = person_keys(persons, moment)
        scope, scope_params = _store_condition(stores)
        with registry.connect() as conn:
            return conn.execute(f'SELECT count(*) FROM ({sql}) WHERE {scope}', [*params,*scope_params]).fetchone()[0]
    if state or persons:
        return next(iter_settings(registry, store_id=store_id, search=search, state=state, at=at,
                                  person_id=person_id, store_ids=store_ids, person_ids=person_ids,
                                  _count_only=True))['total']
    store_sql, store_params = _store_condition(stores)
    catalog_name = ("coalesce(nullif((SELECT s.product_name FROM scheme s "
                    "WHERE s.store_id=c.store_id AND s.product_id=c.product_id),''),c.product_name)")
    search_c, search_c_params = product_search_sql(tokens, 'c.product_id', catalog_name)
    search_s, search_s_params = product_search_sql(tokens, 's.product_id', "coalesce(s.product_name,'')")
    sql = f"""SELECT (
      SELECT count(*) FROM catalog c WHERE {store_sql} AND {search_c}
    ) + (
      SELECT count(*) FROM scheme s WHERE {store_sql}
        AND NOT EXISTS (SELECT 1 FROM catalog c WHERE c.store_id=s.store_id AND c.product_id=s.product_id)
        AND {search_s}
    ) AS total"""
    with registry.connect(thread_affine=False) as conn:
        row = conn.execute(sql, [*store_params, *search_c_params, *store_params, *search_s_params]).fetchone()
    return int(row['total'] if row else 0)


def iter_settings(registry: Registry, *, store_id="", search="", state="", after="", limit=-1, at=None, person_id="", store_ids=None, person_ids=None, _count_only=False):
    from datetime import datetime, timezone, timedelta
    moment = at or datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None).isoformat(timespec="seconds")
    # Include historical bindings even when the live catalogue no longer lists the item.
    stores = sorted(set(store_ids or ([store_id] if store_id else [])))
    persons = sorted(set(person_ids or ([person_id] if person_id else [])))
    tokens = parse_search_tokens(search)
    after_parts = None
    if after:
        after_parts = after.split('\x1f', 1)
        if len(after_parts) != 2: raise RegistryError('请重新打开商品列表')
    def scope_sql(alias):
        parts, values = [], []
        if stores:
            parts.append(f'{alias}.store_id IN ({",".join("?" for _ in stores)})')
            values.extend(stores)
        if after_parts:
            parts.append(f'({alias}.store_id,{alias}.product_id)>(?,?)')
            values.extend(after_parts)
        if persons:
            from .commission_read_index import person_keys
            sql, params = person_keys(persons, moment)
            parts.append(f'({alias}.store_id,{alias}.product_id) IN ({sql})')
            values.extend(params)
        return ' AND '.join(parts) or '1', values
    catalog_cond, catalog_params = scope_sql('c')
    scheme_cond, scheme_params = scope_sql('s')
    catalog_name = ("coalesce(nullif((SELECT z.product_name FROM scheme z "
                    "WHERE z.store_id=c.store_id AND z.product_id=c.product_id),''),c.product_name)")
    search_c, search_c_params = product_search_sql(tokens, 'c.product_id', catalog_name)
    search_s, search_s_params = product_search_sql(tokens, 's.product_id', "coalesce(s.product_name,'')")
    row_sql, row_params = product_search_sql(tokens, 'product_id', 'product_name')
    # Key-only scan first. Payload/JSON is joined only for the page (or the
    # state/person filter set), not the whole 100k-row catalogue.
    candidate_limit = limit if not (state or _count_only) else -1
    person_condition = '1'
    sql = f"""WITH keys AS (
      SELECT store_id,product_id FROM (
        SELECT c.store_id,c.product_id FROM catalog c WHERE {catalog_cond} AND {search_c}
        UNION
        SELECT s.store_id,s.product_id FROM scheme s WHERE {scheme_cond}
          AND NOT EXISTS (SELECT 1 FROM catalog x WHERE x.store_id=s.store_id AND x.product_id=s.product_id)
          AND {search_s}
      ) ORDER BY store_id,product_id LIMIT ?
    ), candidates AS (
      SELECT k.store_id,k.product_id,coalesce(c.product_name,s.product_name,'') product_name,
             coalesce(c.payload,'{{}}') payload
      FROM keys k
      LEFT JOIN catalog c ON c.store_id=k.store_id AND c.product_id=k.product_id
      LEFT JOIN scheme s ON s.store_id=k.store_id AND s.product_id=k.product_id
    ), rows AS (
      SELECT c.store_id,c.product_id,coalesce(nullif(s.product_name,''),c.product_name) product_name,c.payload,
        s.id scheme_id,s.revision,v.body,j.value setting,
        CASE WHEN j.mode='distribute' THEN 'enabled'
             WHEN j.mode='exclude' THEN 'disabled'
             WHEN j.value IS NOT NULL OR v.body IS NULL THEN 'pending'
             WHEN EXISTS (SELECT 1 FROM scheme_read_segment f WHERE f.scheme_id=s.id AND f.valid_from>?) THEN 'scheduled'
             ELSE 'expired' END state
      FROM candidates c LEFT JOIN scheme s ON s.store_id=c.store_id AND s.product_id=c.product_id
      LEFT JOIN scheme_version v ON v.id=s.active_version
      LEFT JOIN scheme_read_segment j ON j.scheme_id=s.id
        AND j.valid_from<=? AND (j.valid_to='' OR j.valid_to>?)
    ) SELECT * FROM rows WHERE {row_sql}
      AND (?='' OR state=?)
      AND {person_condition}
      ORDER BY store_id,product_id LIMIT ?"""
    if _count_only:
        sql = sql.replace("SELECT * FROM rows WHERE", "SELECT count(*) AS total FROM rows WHERE")
    with registry.connect(thread_affine=False) as conn:
        conn.execute("BEGIN")
        people = {r['id']:r['name'] for r in conn.execute('SELECT id,name FROM person')}
        cursor = conn.execute(sql, [*catalog_params,*search_c_params,*scheme_params,*search_s_params,
                              candidate_limit,moment,moment,moment,
                              *row_params,state,state,limit])
        for record in cursor:
            if _count_only:
                yield {"total": record["total"]}
                return
            row = dict(record)
            body = json.loads(row.pop('body') or '{}')
            row['listed'] = json.loads(row.pop('payload') or '{}').get('listed')
            row['setting'] = json.loads(row['setting']) if row['setting'] else None
            if row['state']=='scheduled':
                row['setting'] = next(s for s in body['segments'] if s['valid_from']>moment)
            grouped = {}
            from decimal import Decimal
            for a in (row['setting'] or {}).get('allocations', []):
                pid = a['person_id']
                person = grouped.setdefault(pid, {'person_id':pid,'name':people.get(pid,pid),'rate':Decimal(0),'allocation_duty':'','source':''})
                person['rate'] += Decimal(a['rate'])
                if a.get('duty'):
                    person['allocation_duty'] = a['duty']
                if a.get('source') == 'hierarchy':
                    person['source'] = 'hierarchy'
            row['people'] = [{**a,'rate':str(a['rate'])} for a in grouped.values()]
            row['product_name'] = row['product_name'] or body.get('product_name','')
            yield row


def settings(registry: Registry, *, store_id="", search="", state="", after="", limit=60, at=None, person_id="", store_ids=None, person_ids=None, include_total=True) -> dict:
    page_key = (*_settings_scope(registry, store_id=store_id, search=search, state=state, at=at,
                                 person_id=person_id, store_ids=store_ids, person_ids=person_ids),
                after, limit, include_total)
    with _settings_lock:
        hit = _page_cache.get(page_key)
        if hit is not None:
            _page_cache.move_to_end(page_key)
            return hit
    rows = list(iter_settings(registry, store_id=store_id, search=search, state=state, after=after, limit=limit+1, at=at, person_id=person_id, store_ids=store_ids, person_ids=person_ids))
    more = len(rows)>limit
    rows = rows[:limit]
    total = (cached_count(registry, store_id=store_id, search=search, state=state, at=at,
                          person_id=person_id, store_ids=store_ids, person_ids=person_ids)
             if include_total else len(rows) + (1 if more else 0))
    segments = {}
    for r in registry.store_members_for_stores({row['store_id'] for row in rows}):
        segments.setdefault((r['store_id'], r['person_id']), []).append(r)
    now_stamp = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None).isoformat(timespec='seconds')
    for row in rows:
        lookup_at = (row.get('setting') or {}).get('valid_from') or now_stamp
        try:
            lookup_at = local_time(lookup_at)
        except Exception:
            lookup_at = now_stamp
        for person in row.get('people') or []:
            alloc_duty = person.get('allocation_duty') or ''
            if alloc_duty in ('produce', 'cut'):
                person['duty'] = alloc_duty
            else:
                current = next((seg for seg in segments.get((row['store_id'], person['person_id']), [])
                                if member_active_at(seg, lookup_at)), None)
                person['duty'] = current['duty'] if current else None
    result = {'total':total,'total_pages':(total+limit-1)//limit,'page_size':limit,'rows':rows,'has_more':more,
              'next_after':rows[-1]['store_id']+'\x1f'+rows[-1]['product_id'] if rows and more else ''}
    with _settings_lock:
        _page_cache[page_key] = result
        while len(_page_cache) > _PAGE_CACHE_LIMIT:
            _page_cache.popitem(last=False)
    return result
