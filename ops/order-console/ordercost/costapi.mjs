import { advanceChanges } from './maintenance-scheduling.mjs';
// Cost-API worker: current-cost feed, history backfill, change log.
//
// Replaces the multi-day per-order scrape as the source of official cost.
// The scrape worker keeps running as an independent check; this process
// owns sku_cost_ext / sku_cost_period and writes order_item_cost with
// cost_source in (mirror, history). Frozen rows (closed ledger months)
// are never rewritten.
//
//   node costapi.mjs

import { writeSync } from 'node:fs';
import { pool } from './db.mjs';
import { applyPddHistory, quarantinePddReferences, queuePddPricing, fetchMissingPddHistory, discoverHistoryRequests } from './pdd-pricing-maintenance.mjs';
import { importDateContexts, backfillDateContexts } from './original-date-context.mjs';
import {
  BASE, HISTORY_BATCH, changesPage, history, skusPage,
} from './costclient.mjs';

const LOCK_KEY = 8100_2030;
const FEED_EVERY_MS = Number(process.env.COST_API_FEED_MS || 60 * 60 * 1000);
const CHANGES_EVERY_MS = Number(process.env.COST_API_CHANGES_MS || 24 * 60 * 60 * 1000);
const LOOP_MS = Number(process.env.COST_API_LOOP_MS || 30_000);
const HISTORY_MONTHS = (process.env.COST_API_MONTHS || '2026-06,2026-07,2026-08')
  .split(',').map((s) => s.trim()).filter(Boolean);
const HISTORY_GAP_MS = Number(process.env.COST_API_HISTORY_GAP_MS || 1500);
// The cost API rejects a sku_id over 100 UTF-8 bytes. One such SKU in a
// batch of 30 used to fail the entire request, and the worker reported
// that as a hard error -- which made /health degraded while /revision,
// looking only at watermarks, stayed green.
const SKU_ID_MAX_BYTES = 100;

const ts = () => new Date().toISOString().replace('T', ' ').slice(0, 19);
const log = (m) => writeSync(1, `${ts()} ${m}\n`);
const errLog = (m) => writeSync(2, `${ts()} ${m}\n`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function beat(phase, error) {
  await pool.query(`
    UPDATE costapi_state
       SET beat_at = now(), pid = $1, phase = $2, updated_at = now(),
           last_error = CASE WHEN $3::text IS NULL THEN NULL ELSE $3::text END,
           last_error_at = CASE WHEN $3::text IS NULL THEN last_error_at ELSE now() END
     WHERE id = 1`,
  [process.pid, phase, error ? String(error).slice(0, 500) : null]);
}

async function mark(col) {
  await pool.query(`UPDATE costapi_state SET ${col} = now(), updated_at = now() WHERE id = 1`);
}

async function getState(key) {
  const r = await pool.query('SELECT * FROM cost_api_state WHERE key = $1', [key]);
  return r.rows[0] || null;
}

async function putState(key, fields) {
  await pool.query(`
    INSERT INTO cost_api_state (key, watermark, snapshot_version, cursor, last_error, last_error_at, updated_at)
    VALUES ($1,$2,$3,$4,$5::text, CASE WHEN $5::text IS NULL THEN NULL ELSE now() END, now())
    ON CONFLICT (key) DO UPDATE SET
      watermark = COALESCE(EXCLUDED.watermark, cost_api_state.watermark),
      snapshot_version = COALESCE(EXCLUDED.snapshot_version, cost_api_state.snapshot_version),
      cursor = EXCLUDED.cursor,
      last_error = EXCLUDED.last_error,
      last_error_at = CASE WHEN EXCLUDED.last_error IS NULL
                           THEN cost_api_state.last_error_at ELSE now() END,
      updated_at = now()`,
  [key, fields.watermark ?? null, fields.snapshot_version ?? null,
   fields.cursor ?? null, fields.last_error ?? null]);
}

async function upsertExt(rows) {
  const clean = rows.filter((r) => r && r.sku_id);
  if (!clean.length) return 0;
  await pool.query(`
    INSERT INTO sku_cost_ext (sku_id, sku_type, cost_price, sale_price, modified_at, synced_at)
    SELECT t.sku_id, t.sku_type,
           NULLIF(t.cost_price, '')::numeric,
           NULLIF(t.sale_price, '')::numeric,
           NULLIF(t.modified_at, '')::timestamptz,
           now()
      FROM UNNEST($1::text[], $2::text[], $3::text[], $4::text[], $5::text[])
        AS t(sku_id, sku_type, cost_price, sale_price, modified_at)
    ON CONFLICT (sku_id) DO UPDATE SET
      sku_type = EXCLUDED.sku_type,
      cost_price = EXCLUDED.cost_price,
      sale_price = EXCLUDED.sale_price,
      modified_at = EXCLUDED.modified_at,
      synced_at = now()`,
  [
    clean.map((r) => r.sku_id),
    clean.map((r) => r.sku_type ?? null),
    clean.map((r) => r.cost_price == null ? null : String(r.cost_price)),
    clean.map((r) => r.sale_price == null ? null : String(r.sale_price)),
    clean.map((r) => r.modified_at == null ? null : String(r.modified_at)),
  ]);
  return clean.length;
}

/**
 * Walk /api/cost/skus to the end. First run is a full pull; later runs pass
 * the last watermark as updated_since. snapshot_version is stored so a
 * discontinuity is visible rather than silently accepted.
 */
async function feedSkus({ full = false } = {}) {
  const st = await getState('skus');
  let updatedSince = full ? null : (st?.watermark || null);
  let cursor = null;
  let pages = 0, rows = 0;
  let lastWm = null, lastSnap = st?.snapshot_version || null;

  for (;;) {
    const page = await skusPage({ updatedSince, cursor, limit: 1000 });
    const data = page.data || [];
    rows += await upsertExt(data);
    pages++;
    lastWm = page.watermark || lastWm;
    if (lastSnap && page.snapshot_version && page.snapshot_version !== lastSnap && pages === 1 && !full) {
      log(`skus snapshot_version moved (${lastSnap.slice(0, 8)} → ${page.snapshot_version.slice(0, 8)}); continuing`);
    }
    lastSnap = page.snapshot_version || lastSnap;
    if (!page.next_cursor) break;
    cursor = page.next_cursor;
    // After the first page of an incremental pull, drop updated_since so the
    // cursor is the only pagination key -- the docs say to send it as-is.
    updatedSince = null;
    if (pages % 20 === 0) {
      await beat(`feed ${pages}p/${rows}`);
      log(`skus feed: ${pages} pages, ${rows} upserts`);
    }
  }

  await putState('skus', { watermark: lastWm, snapshot_version: lastSnap, cursor: null });
  await mark('feed_at');
  log(`skus feed done: ${pages} pages, ${rows} upserts, watermark=${lastWm || '-'}`);
  return { pages, rows };
}

/**
 * Fill order_item_cost for every line we have a current SKU cost for and no
 * official cost yet. Existing scrape/history/mirror rows are left alone.
 */
async function applyMirror() {
  let total = 0;
  for (;;) {
    // The insert and the change rows it produces are one statement, so a
    // batch that lands without announcing itself is not possible. cost_as_of
    // is CURRENT_DATE and not the order date on purpose: a mirror row is
    // today's cost standing in for the real one, and saying so is what lets
    // the ledger rank it below a history-sourced figure.
    const r = await pool.query(`
      WITH ins AS (
        INSERT INTO order_item_cost
          (oi_id, o_id, sku_id, qty, cost_price, cost_amount,
           has_cost, cost_source, cost_status, cost_as_of, fetched_at)
        SELECT oi.oi_id, oi.o_id, oi.sku_id, oi.qty,
               CASE WHEN requires_order_day_history(o.shop_site) THEN NULL ELSE e.cost_price END,
               CASE WHEN requires_order_day_history(o.shop_site) THEN NULL ELSE ROUND((oi.qty * e.cost_price)::numeric, 2) END,
               NOT requires_order_day_history(o.shop_site), 'mirror',
               CASE WHEN requires_order_day_history(o.shop_site) THEN 'missing_price' ELSE 'priced' END, CURRENT_DATE, now()
          FROM order_item oi
          JOIN jst_order o ON o.o_id=oi.o_id
          JOIN sku_cost_ext e ON e.sku_id = oi.sku_id
         WHERE e.cost_price > 0
           AND NOT EXISTS (SELECT 1 FROM order_item_cost c WHERE c.oi_id = oi.oi_id)
         LIMIT 50000
        ON CONFLICT (oi_id) DO NOTHING
        RETURNING oi_id, o_id, sku_id
      ), ev AS (
        SELECT count(*)::int AS n,
               jsonb_agg(jsonb_build_object(
                 'entity_type', 'order_cost',
                 'entity_id', i.oi_id::text,
                 'order_id', i.o_id::text,
                 'parent_id', i.o_id::text,
                 'sub_order_id', i.oi_id::text,
                 'sku_id', i.sku_id,
                 'order_store_id', o.shop_id::text,
                 'affected_date_from', o.order_date::date)) AS rows
          FROM ins i JOIN jst_order o ON o.o_id = i.o_id
      )
      SELECT n, integration_emit_many(COALESCE(rows, '[]'::jsonb)) AS revision FROM ev`);
    const n = r.rows[0]?.n || 0;
    total += n;
    if (n) log(`mirror join: +${n} (total ${total})`);
    if (n < 50000) break;
    await beat(`mirror ${total}`);
  }
  log(`mirror join done: +${total} lines`);
  return total;
}

async function upsertPeriods(rows) {
  const clean = rows.filter((r) => r && r.sku_id && r.cost_price != null && r.begin_date);
  if (!clean.length) return 0;
  await pool.query(`
    INSERT INTO sku_cost_period
      (sku_id, wms_co_id, begin_date, end_date, cost_price, as_of, fetched_at)
    SELECT t.sku_id, t.wms_co_id,
           t.begin_date::date, NULLIF(t.end_date, '')::date,
           t.cost_price::numeric, NULLIF(t.as_of, '')::date, now()
      FROM UNNEST($1::text[], $2::text[], $3::text[], $4::text[], $5::text[], $6::text[])
        AS t(sku_id, wms_co_id, begin_date, end_date, cost_price, as_of)
    ON CONFLICT (sku_id, wms_co_id, begin_date) DO UPDATE SET
      end_date = EXCLUDED.end_date,
      cost_price = EXCLUDED.cost_price,
      as_of = EXCLUDED.as_of,
      fetched_at = now()`,
  [
    clean.map((r) => r.sku_id),
    clean.map((r) => r.wms_co_id || ''),
    clean.map((r) => String(r.begin_date)),
    clean.map((r) => r.end_date == null ? null : String(r.end_date)),
    clean.map((r) => String(r.cost_price)),
    clean.map((r) => r.as_of == null ? null : String(r.as_of)),
  ]);
  return clean.length;
}

function midOf(month) {
  // month is YYYY-MM; ask on the 15th so a typical half-month interval covers it.
  return `${month}-15`;
}

/**
 * SKUs sold in `month` that still have no history interval covering asOf.
 *
 * Ordered by the money that SKU carried that month, largest first, for the
 * same reason the scrape queue is: turnover is concentrated in a few thousand
 * SKUs, and a backfill that walks the catalogue alphabetically spends its
 * first hours on discontinued items that the API answers "not found" for
 * while the SKUs the books actually turn on wait at the back.
 */
async function uncoveredSkus(month, asOf, limit = 600) {
  const r = await pool.query(`
    SELECT oi.sku_id
      FROM order_item oi
      JOIN jst_order o ON o.o_id = oi.o_id
     WHERE o.order_date >= ($1 || '-01')::date
       AND o.order_date < (($1 || '-01')::date + INTERVAL '1 month')
       AND oi.sku_id IS NOT NULL AND oi.sku_id <> ''
       AND octet_length(oi.sku_id) <= 100
       AND oi.is_virtual IS NOT TRUE AND oi.is_suspect IS NOT TRUE
       AND NOT EXISTS (
         SELECT 1 FROM sku_cost_period p
          WHERE p.sku_id = oi.sku_id
            AND p.begin_date <= $2::date
            AND (p.end_date IS NULL OR p.end_date >= $2::date)
       )
       AND NOT EXISTS (
         SELECT 1 FROM sku_cost_history_miss m
          WHERE m.sku_id = oi.sku_id AND m.as_of = $2::date
       )
     GROUP BY oi.sku_id
     ORDER BY SUM(oi.amount) DESC NULLS LAST, COUNT(*) DESC
     LIMIT $3`, [month, asOf, limit]);
  return r.rows.map((x) => x.sku_id);
}

function skuFitsApi(id) {
  if (id == null || id === '') return false;
  const s = String(id);
  return s.length <= SKU_ID_MAX_BYTES && Buffer.byteLength(s, 'utf8') <= SKU_ID_MAX_BYTES;
}

async function markUnfetchable(skus, asOf) {
  if (!skus.length) return;
  await pool.query(`
    INSERT INTO sku_cost_history_miss (sku_id, as_of)
    SELECT x, $2::date FROM UNNEST($1::text[]) AS x
    ON CONFLICT (sku_id, as_of) DO UPDATE SET fetched_at = now()`,
  [skus, asOf]);
}

async function fetchHistoryBatch(skus, asOf) {
  const over = skus.filter((s) => !skuFitsApi(s));
  const fit = skus.filter((s) => skuFitsApi(s));
  if (over.length) {
    await markUnfetchable(over, asOf);
    log(`history skip ${over.length} sku(s) over ${SKU_ID_MAX_BYTES} bytes`);
  }
  if (!fit.length) return { ok: 0, missing: over.length, stored: 0 };
  try {
    const page = await history(fit, asOf);
    const n = await upsertPeriods(page.data || []);
    const missing = page.missing_sku_ids || [];
    if (missing.length) {
      await pool.query(`
        INSERT INTO sku_cost_history_miss (sku_id, as_of)
        SELECT x, $2::date FROM UNNEST($1::text[]) AS x
        ON CONFLICT (sku_id, as_of) DO UPDATE SET fetched_at = now()`,
      [missing, asOf]);
    }
    return { ok: (page.data || []).length, missing: missing.length + over.length, stored: n };
  } catch (e) {
    if (e.status === 400 && /sku_id must not exceed/i.test(String(e.message || e))) {
      if (fit.length > 1) {
        const mid = Math.ceil(fit.length / 2);
        const a = await fetchHistoryBatch(fit.slice(0, mid), asOf);
        const b = await fetchHistoryBatch(fit.slice(mid), asOf);
        return {
          ok: a.ok + b.ok,
          missing: a.missing + b.missing + over.length,
          stored: a.stored + b.stored,
        };
      }
      await markUnfetchable(fit, asOf);
      log(`history skip 1 sku the API still rejects for length: ${String(fit[0]).slice(0, 40)}`);
      return { ok: 0, missing: over.length + 1, stored: 0 };
    }
    if (e.status === 502 && fit.length > 5) {
      // Upstream batch limit is ~30; if it still 502s, split once.
      const mid = Math.ceil(fit.length / 2);
      const a = await fetchHistoryBatch(fit.slice(0, mid), asOf);
      const b = await fetchHistoryBatch(fit.slice(mid), asOf);
      return { ok: a.ok + b.ok, missing: a.missing + b.missing + over.length, stored: a.stored + b.stored };
    }
    throw e;
  }
}

/**
 * One slice of the history backfill. Bounded so the loop can beat and
 * applyMirror between slices instead of disappearing for hours.
 */
async function historySlice() {
  let did = 0;
  for (const month of HISTORY_MONTHS) {
    const asOf = midOf(month);
    const skus = await uncoveredSkus(month, asOf, HISTORY_BATCH * 8);
    if (!skus.length) continue;
    for (let i = 0; i < skus.length; i += HISTORY_BATCH) {
      const chunk = skus.slice(i, i + HISTORY_BATCH);
      try {
        const r = await fetchHistoryBatch(chunk, asOf);
        did += r.stored;
        log(`history ${month} as_of=${asOf}: +${r.stored} intervals, ${r.missing} missing`);
      } catch (e) {
        if (e.status === 502) {
          log(`history ${month} 502, backing off 15s`);
          await sleep(15_000);
          return did;
        }
        throw e;
      }
      await beat(`history ${month} +${did}`);
      if (HISTORY_GAP_MS) await sleep(HISTORY_GAP_MS);
      if (did >= HISTORY_BATCH * 8) return did;
    }
  }
  if (did) await mark('history_at');
  return did;
}

/**
 * The bulk backfill asks for cost as of the 15th, which returns one interval
 * per SKU. That covers the whole month only for SKUs whose price held steady.
 * A SKU repriced on the 20th comes back with an interval ending on the 19th,
 * and orders after that date would silently fall back to the current-cost
 * mirror. Find order dates left uncovered and ask again on those days.
 */
async function historyEdges() {
  const r = await pool.query(`
    SELECT o.order_date::date AS d, oi.sku_id, SUM(oi.amount) AS amt
      FROM order_item oi
      JOIN jst_order o ON o.o_id = oi.o_id
     WHERE o.order_date >= ($1 || '-01')::date
       AND o.order_date < (($2 || '-01')::date + INTERVAL '1 month')
       AND oi.sku_id IS NOT NULL AND oi.sku_id <> ''
       AND octet_length(oi.sku_id) <= 100
       AND oi.is_virtual IS NOT TRUE AND oi.is_suspect IS NOT TRUE
       AND EXISTS (SELECT 1 FROM sku_cost_period p WHERE p.sku_id = oi.sku_id)
       AND NOT EXISTS (
         SELECT 1 FROM sku_cost_period p
          WHERE p.sku_id = oi.sku_id
            AND p.begin_date <= o.order_date::date
            AND (p.end_date IS NULL OR p.end_date >= o.order_date::date)
       )
       AND NOT EXISTS (
         SELECT 1 FROM sku_cost_history_miss m
          WHERE m.sku_id = oi.sku_id AND m.as_of = o.order_date::date
       )
     GROUP BY 1, 2
     ORDER BY amt DESC NULLS LAST
     LIMIT $3`,
  [HISTORY_MONTHS[0], HISTORY_MONTHS[HISTORY_MONTHS.length - 1], HISTORY_BATCH * 8]);
  if (!r.rows.length) return 0;

  const byDay = new Map();
  for (const row of r.rows) {
    const d = row.d instanceof Date ? row.d.toISOString().slice(0, 10) : String(row.d).slice(0, 10);
    if (!byDay.has(d)) byDay.set(d, []);
    byDay.get(d).push(row.sku_id);
  }

  let did = 0;
  for (const [asOf, skus] of byDay) {
    for (let i = 0; i < skus.length; i += HISTORY_BATCH) {
      try {
        const res = await fetchHistoryBatch(skus.slice(i, i + HISTORY_BATCH), asOf);
        did += res.stored;
      } catch (e) {
        if (e.status === 502) { await sleep(15_000); return did; }
        throw e;
      }
      await beat(`edges ${asOf} +${did}`);
      if (HISTORY_GAP_MS) await sleep(HISTORY_GAP_MS);
    }
  }
  log(`history edges: +${did} intervals over ${byDay.size} days`);
  return did;
}

/**
 * Promote official cost to the history interval that covers the order date.
 * Frozen rows and already-matching history rows stay put.
 */
async function applyHistory() {
  const r = await pool.query(`
    WITH pick AS (
      SELECT DISTINCT ON (oi.oi_id)
             oi.oi_id, oi.qty, o.order_date::date AS d, p.cost_price
        FROM order_item oi
        JOIN jst_order o ON o.o_id = oi.o_id
        JOIN sku_cost_period p ON p.sku_id = oi.sku_id
         AND p.begin_date <= o.order_date::date
         AND (p.end_date IS NULL OR p.end_date >= o.order_date::date)
         AND p.cost_price > 0
       WHERE NOT requires_order_day_history(o.shop_site)
       ORDER BY oi.oi_id, p.begin_date DESC, p.wms_co_id
    ), upd AS (
      UPDATE order_item_cost c
         SET cost_price = pick.cost_price,
             cost_amount = ROUND((COALESCE(c.qty, pick.qty) * pick.cost_price)::numeric, 2),
             has_cost = true,
             cost_source = 'history',
             cost_status = 'priced',
             cost_as_of = pick.d,
             fetched_at = now()
        FROM pick
       WHERE c.oi_id = pick.oi_id
         AND c.frozen IS NOT TRUE
         AND (c.cost_source IS DISTINCT FROM 'history'
              OR c.cost_price IS DISTINCT FROM pick.cost_price)
      RETURNING c.oi_id, c.o_id, c.sku_id
    ), ev AS (
      SELECT count(*)::int AS n,
             jsonb_agg(jsonb_build_object(
               'entity_type', 'order_cost',
               'entity_id', u.oi_id::text,
               'order_id', u.o_id::text,
               'parent_id', u.o_id::text,
               'sub_order_id', u.oi_id::text,
               'sku_id', u.sku_id,
               'order_store_id', o.shop_id::text,
               'affected_date_from', o.order_date::date)) AS rows
        FROM upd u JOIN jst_order o ON o.o_id = u.o_id
    )
    SELECT n, integration_emit_many(COALESCE(rows, '[]'::jsonb)) AS revision FROM ev
  `);
  const n = r.rows[0]?.n || 0;
  log(`history apply: ${n} lines`);
  return n;
}

/**
 * Nail down closed ledger months so a later SKU reprice cannot rewrite margin
 * someone already signed off on.
 *
 * Only order-date history cost is worth freezing. Freezing a row still sitting
 * on the current-cost mirror would lock in the wrong number permanently, since
 * applyHistory skips frozen rows and could never correct it.
 */
async function freezeClosed() {
  const r = await pool.query(`
    UPDATE order_item_cost c
       SET frozen = true
      FROM order_item oi
      JOIN jst_order o ON o.o_id = oi.o_id
      JOIN ledger_shop_map m ON m.shop_name = o.shop_name
      JOIN ledger_period p ON p.store_id = m.store_id
       AND p.period = to_char(o.order_date, 'YYYY-MM')
       AND p.state = 'closed'
     WHERE c.oi_id = oi.oi_id
       AND c.frozen IS NOT TRUE
       AND c.has_cost
       AND c.cost_source = 'history'
  `);
  if (r.rowCount) log(`froze ${r.rowCount} history lines in closed ledger months`);

  const pend = await pool.query(`
    SELECT count(*)::int AS n
      FROM order_item_cost c
      JOIN order_item oi ON oi.oi_id = c.oi_id
      JOIN jst_order o ON o.o_id = oi.o_id
      JOIN ledger_shop_map m ON m.shop_name = o.shop_name
      JOIN ledger_period p ON p.store_id = m.store_id
       AND p.period = to_char(o.order_date, 'YYYY-MM')
       AND p.state = 'closed'
     WHERE c.frozen IS NOT TRUE`);
  if (pend.rows[0].n) log(`closed months awaiting history: ${pend.rows[0].n} lines`);
  return r.rowCount;
}

async function pullChanges() {
  const state = await getState('changes');
  const result = await advanceChanges({state, read: changesPage, refresh: fetchHistoryBatch,
    save: fields => putState('changes', fields), months: HISTORY_MONTHS, batchSize: HISTORY_BATCH,
    asOf: midOf, since: new Date(Date.now() - 3 * 86400_000).toISOString()});
  if (!result.more) await mark('changes_at');
  log(`changes: ${result.events} events, ${result.skus} skus; ${result.more ? 'checkpoint saved; more pending' : 'caught up'}`);
  return result.skus;
}

async function coverage() {
  const r = await pool.query(`
    SELECT count(*)::int AS lines,
           count(*) FILTER (WHERE c.oi_id IS NOT NULL AND c.has_cost)::int AS costed,
           count(*) FILTER (WHERE c.cost_source = 'history')::int AS history,
           count(*) FILTER (WHERE c.cost_source = 'mirror')::int AS mirror,
           count(*) FILTER (WHERE c.cost_source = 'scrape')::int AS scrape,
           count(*) FILTER (WHERE c.frozen)::int AS frozen
      FROM order_item oi
      LEFT JOIN order_item_cost c ON c.oi_id = oi.oi_id
     WHERE oi.is_virtual IS NOT TRUE AND oi.is_suspect IS NOT TRUE`);
  return r.rows[0];
}

async function oncePass({ forceFull = false, doChanges = false } = {}) {
  const st = await pool.query('SELECT feed_at, history_at, changes_at FROM costapi_state WHERE id = 1');
  const row = st.rows[0] || {};
  const age = (col) => (row[col] ? Date.now() - new Date(row[col]).getTime() : Infinity);
  await beat('original order dates');
  const contexts=await importDateContexts(pool);
  if(contexts.files||contexts.errors.length)log(`Original dates: ${contexts.files} files, ${contexts.items} items; errors ${JSON.stringify(contexts.errors)}`);
  await backfillDateContexts(pool);
  await beat('historical prices');
  await applyPddHistory(pool);
  await quarantinePddReferences(pool);

  const needFull = forceFull || !(await getState('skus'))?.watermark;
  if (needFull || age('feed_at') >= FEED_EVERY_MS) {
    await beat(needFull ? 'feed-full' : 'feed');
    await feedSkus({ full: needFull });
    await applyMirror();
  }

  await beat('history');
  // The bulk pass walks SKUs that have no interval at all; once a month is
  // exhausted it returns nothing and the edge pass takes over, closing the
  // day-level gaps left by mid-month reprices.
  const hist = await historySlice();
  if (!hist) await historyEdges();
  // Even with no new intervals, newly arrived orders may now match.
  await applyHistory();
  await beat('missing historical prices');
  const discovered = await discoverHistoryRequests(pool);
  log(`Historical requests: scanned ${discovered.scanned}, queued ${discovered.queued}, cursor ${discovered.cursor}`);
  const pdd = await fetchMissingPddHistory(pool, history, 150);
  if (pdd.checked) log(`Historical prices ${pdd.received}/${pdd.checked}`);
  await applyPddHistory(pool);
  await queuePddPricing(pool);

  if (doChanges || (await getState('changes'))?.cursor || age('changes_at') >= CHANGES_EVERY_MS) {
    await beat('changes');
    await pullChanges();
    await applyHistory();
    await applyPddHistory(pool);
    await quarantinePddReferences(pool);
    await queuePddPricing(pool);
  }

  await beat('freeze');
  await freezeClosed();

  const cov = await coverage();
  const pct = cov.lines ? (100 * cov.costed / cov.lines).toFixed(1) : '0';
  log(`coverage ${pct}%  costed=${cov.costed}/${cov.lines}  `
    + `history=${cov.history} mirror=${cov.mirror} scrape=${cov.scrape} frozen=${cov.frozen}`);
  return cov;
}

async function main() {
  const lock = await pool.query('SELECT pg_try_advisory_lock($1) AS ok', [LOCK_KEY]);
  if (!lock.rows[0].ok) {
    log('another costapi worker holds the lock; exiting');
    await pool.end();
    return;
  }

  log(`costapi up. pid=${process.pid} base=${BASE} historyBatch=${HISTORY_BATCH} months=${HISTORY_MONTHS.join(',')}`);
  await beat('starting');

  // First pass: full feed + join + a history slice, so coverage jumps now
  // rather than after the hourly timer.
  try {
    const hasWm = !!(await getState('skus'))?.watermark;
    await oncePass({ forceFull: !hasWm, doChanges: !hasWm });
  } catch (e) {
    errLog(`first pass failed: ${e.message || e}`);
    await beat('error', e.message || e);
  }

  for (;;) {
    try {
      await beat('idle');
      await sleep(LOOP_MS);
      await oncePass();
    } catch (e) {
      const msg = String(e.message || e);
      errLog(`loop failed: ${msg}`);
      await beat('error', msg);
      await sleep(15_000);
    }
  }
}

process.on('unhandledRejection', (e) => errLog(`unhandled rejection: ${e?.message || e}`));

main().catch(async (e) => {
  errLog(`fatal: ${e.message || e}`);
  await pool.end().catch(() => {});
  process.exit(1);
});
