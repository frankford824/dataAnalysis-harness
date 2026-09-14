import { fairClaimSQL } from './maintenance-scheduling.mjs';
// Queue maintenance and cost persistence.
//
// The queue is derived, not authoritative: it is rebuilt from jst_order rather
// than written by the collector. That keeps the collector's write path exactly
// as it was -- no trigger, no extra statement in its transaction -- so a
// backlog here can never slow order ingestion down.

import { emitMany, ENTITY } from '../integration/outbox.mjs';
import { prepareHistoricalQuotes } from './historical-quotes.mjs';
import { isHistoricalPlatform } from './historical-pricing.mjs';

/** Money as an exact 2-decimal string. Never let a float reach the DB. */
function money(v) {
  if (v == null || v === '') return null;
  const n = typeof v === 'string' ? Number(v) : v;
  if (!Number.isFinite(n)) return null;
  return n.toFixed(2);
}

/** Costs and quantities keep 4 decimals; the ERP quotes 0.2766 and 19.2783. */
function num4(v) {
  if (v == null || v === '') return null;
  const n = typeof v === 'string' ? Number(v) : v;
  if (!Number.isFinite(n)) return null;
  return n.toFixed(4);
}

/**
 * Official unit cost. The ERP writes 0 for uncosted SKUs and a moving
 * average that can go negative after returns; neither is a price.
 * An explicit 0 on a gift line is the one true zero we keep.
 */
function officialPrice(raw, isGift) {
  if (raw == null) return null;
  const n = Number(raw);
  if (!Number.isFinite(n)) return null;
  if (n > 0) return raw;
  if (n === 0 && isGift) return raw;
  return null;
}

function unpricedReason(raw, isGift) {
  if (raw == null) return null;
  const n = Number(raw);
  if (n < 0) return 'negative_cost';
  if (n === 0 && !isGift) return 'zero_unpriced';
  return null;
}

const str = (v) => (v == null ? null : String(v));

/**
 * Add orders we have never costed.
 *
 * Bounded per call so the first run on a 700k-order table does not hold a
 * connection for minutes while the collector is trying to write.
 */
export async function enqueueNew(pool, limit = 50_000) {
  const r = await pool.query(`
    INSERT INTO order_cost_job (o_id, order_date, priority, order_hash)
    SELECT o.o_id, o.order_date::date, o.pay_amount, NULL
      FROM jst_order o
      LEFT JOIN order_cost_job j ON j.o_id = o.o_id
     WHERE j.o_id IS NULL
     ORDER BY o.order_date DESC NULLS LAST, o.pay_amount DESC NULLS LAST
     LIMIT $1
    ON CONFLICT (o_id) DO NOTHING`, [limit]);
  return r.rowCount;
}

/**
 * Re-queue orders whose content moved since we costed them.
 *
 * An order's fingerprint changes when its lines or their quantities change,
 * and a changed line means the cost we hold describes something that no longer
 * exists. Status-only transitions move the hash too, which is deliberate: cost
 * is settled at shipment, so an order that has just shipped is exactly the one
 * worth asking about again.
 */
export async function requeueChanged(pool, limit = 5_000) {
  const r = await pool.query(`
    UPDATE order_cost_job j
       SET state = 'pending', attempts = 0, next_try_at = now(), last_error = NULL
      FROM jst_order o
     WHERE o.o_id = j.o_id
       AND j.state IN ('done', 'empty')
       AND o.content_hash IS DISTINCT FROM j.order_hash
       AND j.o_id IN (
         SELECT j2.o_id FROM order_cost_job j2
           JOIN jst_order o2 ON o2.o_id = j2.o_id
          WHERE j2.state IN ('done', 'empty')
            AND o2.content_hash IS DISTINCT FROM j2.order_hash
          ORDER BY o2.updated_at DESC
          LIMIT $1
       )`, [limit]);
  return r.rowCount;
}

/**
 * Next batch of orders to cost.
 *
 * Two-tier ordering. Orders from the last couple of days come first regardless
 * of value: this table is read by a live console, and yesterday's margin being
 * hours stale is worse than last June's being a day later. Everything else is
 * drained largest-first, which matters because the money is concentrated --
 * the top 2,000 SKUs carry 77% of revenue, so a backfill ordered this way is
 * useful long before it is finished.
 */
export async function claim(pool, n) {
  const result = await pool.query(fairClaimSQL, [n]);
  return result.rows;
}

/**
 * Record one order's line costs from the detail page.
 *
 * The scrape is no longer the official cost. It always writes scrape_* as an
 * independent check. Official cost_price is filled only when the row is new
 * or still tagged scrape, and never when the row is frozen (a closed ledger
 * month) or already filled by history/mirror.
 *
 * No longer deletes the order's cost rows first: that would throw away a
 * history/mirror value the costapi worker just spent time getting.
 */
export async function saveCost(pool, oId, items, hash, { historyReader, originalCapturedAt } = {}) {
  let priced = items.filter((it) => it.oi_id != null);
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // The order's own date, read before the writes that depend on it.
    // cost_as_of dates the assertion, not the fetch: a June order costed in
    // August is still a statement about June, and stamping it with now() is
    // what makes a restatement indistinguishable from an original.
    const dayRow = await client.query(
      'SELECT COALESCE(pay_date, order_date)::date AS as_of, order_date::date AS day, shop_id, shop_site, content_hash'
      + '  FROM jst_order WHERE o_id = $1 FOR SHARE',
      [oId],
    );
    const asOf = dayRow.rows[0]?.as_of || null;
    const day = dayRow.rows[0]?.day || null;
    const storeId = dayRow.rows[0]?.shop_id != null ? String(dayRow.rows[0].shop_id) : null;
    const strictHistorical = isHistoricalPlatform(dayRow.rows[0]?.shop_site);
    if (strictHistorical) {
      if(dayRow.rows[0]?.content_hash!==hash)throw new Error('Order changed while fetching cost; retry from current order');
      priced = await prepareHistoricalQuotes(client, oId, priced, historyReader);
      const dates=new Map((await client.query('SELECT oi_id::text,pricing_day::text FROM order_item WHERE o_id=$1 FOR SHARE',[oId])).rows.map(r=>[r.oi_id,r.pricing_day]));
      if(priced.some(q=>q.cost_as_of!==dates.get(String(q.oi_id))))throw new Error('Original order date changed while pricing; retry');
      for(const quote of priced){
        if(quote.failure_reason!=='missing_historical_cost')continue;
        const manual=(await client.query(`SELECT cost_price::text,cost_amount::text,pricing_evidence FROM order_item_cost
          WHERE oi_id=$1::bigint AND sku_id=$2::text AND qty=$3::numeric AND cost_as_of=$4::date
            AND cost_source='manual' AND cost_status='priced' AND cost_price>=0
            AND cost_amount=round(cost_price*qty,2)
            AND pricing_evidence->>'order_date'=$4::text
            AND COALESCE(pricing_evidence->>'cost_company_id',pricing_evidence#>>'{components,0,cost_company_id}')=$5::text`,
          [quote.oi_id,quote.sku_id,quote.qty,quote.cost_as_of,quote.evidence?.cost_company_id])).rows[0];
        if(manual)Object.assign(quote,{cost_price:manual.cost_price,cost_amount:manual.cost_amount,cost_source:'manual',failure_reason:null,
          evidence:{...manual.pricing_evidence,unit_cost:manual.cost_price,rule:'manual_pricing',
            original_components:quote.evidence.original_components}});
      }
      for(const quote of priced)quote.evidence={...quote.evidence,source_order_hash:hash,
        original_captured_at:originalCapturedAt||new Date().toISOString()};
    }

    let withCost = 0;
    for (const it of priced) {
      const raw = num4(strictHistorical ? it.reference_cost_price : it.cost_price);
      const gift = it.is_gift === true;
      const cp = strictHistorical ? it.cost_price : officialPrice(raw, gift);
      const q = num4(it.qty);
      const scrapeAmt = strictHistorical ? it.reference_cost_amount : raw != null && q != null ? money(Number(raw) * Number(q)) : null;
      const amt = strictHistorical ? it.cost_amount : cp != null && q != null ? money(Number(cp) * Number(q)) : null;
      const has = cp != null && (strictHistorical || Number(cp) > 0);
      const reason = strictHistorical ? it.failure_reason : unpricedReason(raw, gift);
      if (has) withCost++;
      await client.query(`
        INSERT INTO order_item_cost
          (oi_id, o_id, sku_id, qty, cost_price, cost_amount, src_combine_sku_id,
           has_cost, is_gift, cost_source, scrape_cost_price, scrape_cost_amount,
           scrape_at, fetched_at, cost_status, cost_as_of, failure_reason, pricing_evidence, pricing_checked_at)
        -- Every parameter is cast at its first mention. A bare $5 inside
        -- CASE WHEN $5 IS NULL gives the planner nothing to infer from --
        -- IS NULL accepts anything -- so the whole statement fails to parse
        -- before a single row is touched.
        VALUES ($1,$2,$3,$4::numeric,$5::numeric,$6::numeric,$7,$8::boolean,$9::boolean,
                -- scrape_* keeps the raw page number, including 0 and
                -- negatives. Official cost_price is only a positive price
                -- (or an explicit gift 0). priced_source_chk still needs a
                -- source on every priced row; a raw scrape tags provenance
                -- even when official is missing.
                CASE WHEN $14::boolean THEN $15::text WHEN $11::numeric IS NOT NULL THEN 'scrape' ELSE NULL END,
                $11::numeric, $12::numeric, COALESCE($17::timestamptz,now()), now(),
                CASE WHEN $5::numeric IS NULL THEN 'missing_price' ELSE 'priced' END,
                CASE WHEN $14::boolean THEN $10::date WHEN $5::numeric IS NULL THEN NULL ELSE $10::date END,
                $13::text, $16::jsonb, CASE WHEN $14::boolean THEN now() END)
        ON CONFLICT (oi_id) DO UPDATE SET
          o_id = EXCLUDED.o_id,
          sku_id = CASE WHEN $14::boolean AND NOT order_item_cost.frozen THEN EXCLUDED.sku_id ELSE COALESCE(order_item_cost.sku_id, EXCLUDED.sku_id) END,
          qty = CASE WHEN $14::boolean AND NOT order_item_cost.frozen THEN EXCLUDED.qty ELSE COALESCE(order_item_cost.qty, EXCLUDED.qty) END,
          src_combine_sku_id = COALESCE(EXCLUDED.src_combine_sku_id, order_item_cost.src_combine_sku_id),
          is_gift = EXCLUDED.is_gift,
          scrape_cost_price = EXCLUDED.scrape_cost_price,
          scrape_cost_amount = EXCLUDED.scrape_cost_amount,
          scrape_at = EXCLUDED.scrape_at,
          -- Only a *valid* history/mirror price outranks scrape. A history
          -- row that was zeroed (zero_unpriced / NULL official) is not a
          -- price; keeping it would block a later positive scrape forever.
          -- Gift 0 is the one official zero we still protect.
          cost_price = CASE
            WHEN order_item_cost.frozen THEN order_item_cost.cost_price
            WHEN $14::boolean THEN EXCLUDED.cost_price
            WHEN order_item_cost.cost_source IN ('history', 'mirror')
             AND (order_item_cost.cost_price > 0
                  OR (order_item_cost.cost_price = 0 AND order_item_cost.is_gift))
              THEN order_item_cost.cost_price
            ELSE EXCLUDED.cost_price
          END,
          cost_amount = CASE
            WHEN order_item_cost.frozen THEN order_item_cost.cost_amount
            WHEN $14::boolean THEN EXCLUDED.cost_amount
            WHEN order_item_cost.cost_source IN ('history', 'mirror')
             AND (order_item_cost.cost_price > 0
                  OR (order_item_cost.cost_price = 0 AND order_item_cost.is_gift))
              THEN order_item_cost.cost_amount
            ELSE EXCLUDED.cost_amount
          END,
          has_cost = CASE
            WHEN order_item_cost.frozen THEN order_item_cost.has_cost
            WHEN $14::boolean THEN EXCLUDED.has_cost
            WHEN order_item_cost.cost_source IN ('history', 'mirror')
             AND (order_item_cost.cost_price > 0
                  OR (order_item_cost.cost_price = 0 AND order_item_cost.is_gift))
              THEN order_item_cost.has_cost
            ELSE EXCLUDED.has_cost
          END,
          cost_source = CASE
            WHEN order_item_cost.frozen THEN order_item_cost.cost_source
            WHEN $14::boolean THEN EXCLUDED.cost_source
            WHEN order_item_cost.cost_source IN ('history', 'mirror')
             AND (order_item_cost.cost_price > 0
                  OR (order_item_cost.cost_price = 0 AND order_item_cost.is_gift))
              THEN order_item_cost.cost_source
            WHEN EXCLUDED.scrape_cost_price IS NOT NULL THEN 'scrape'
            ELSE order_item_cost.cost_source
          END,
          fetched_at = CASE
            WHEN order_item_cost.frozen THEN order_item_cost.fetched_at
            WHEN $14::boolean THEN now()
            WHEN order_item_cost.cost_source IN ('history', 'mirror')
             AND (order_item_cost.cost_price > 0
                  OR (order_item_cost.cost_price = 0 AND order_item_cost.is_gift))
              THEN order_item_cost.fetched_at
            ELSE now()
          END,
          -- One row, one state. Derived from the price the row ends up
          -- holding, so it can never contradict it; a frozen or
          -- still-valid history-sourced row keeps the state of its price.
          cost_status = CASE
            WHEN order_item_cost.frozen THEN order_item_cost.cost_status
            WHEN $14::boolean THEN EXCLUDED.cost_status
            WHEN order_item_cost.cost_source IN ('history', 'mirror')
             AND (order_item_cost.cost_price > 0
                  OR (order_item_cost.cost_price = 0 AND order_item_cost.is_gift))
              THEN COALESCE(order_item_cost.cost_status, 'priced')
            WHEN EXCLUDED.cost_price IS NULL THEN 'missing_price'
            ELSE 'priced'
          END,
          cost_as_of = CASE
            WHEN order_item_cost.frozen THEN order_item_cost.cost_as_of
            WHEN $14::boolean THEN EXCLUDED.cost_as_of
            WHEN order_item_cost.cost_source IN ('history', 'mirror')
             AND (order_item_cost.cost_price > 0
                  OR (order_item_cost.cost_price = 0 AND order_item_cost.is_gift))
              THEN COALESCE(order_item_cost.cost_as_of, EXCLUDED.cost_as_of)
            ELSE EXCLUDED.cost_as_of
          END,
          failure_reason = CASE
            WHEN order_item_cost.frozen THEN order_item_cost.failure_reason
            WHEN $14::boolean THEN EXCLUDED.failure_reason
            WHEN order_item_cost.cost_source IN ('history', 'mirror')
             AND (order_item_cost.cost_price > 0
                  OR (order_item_cost.cost_price = 0 AND order_item_cost.is_gift))
              THEN order_item_cost.failure_reason
            ELSE EXCLUDED.failure_reason
          END,
          pricing_evidence = CASE WHEN order_item_cost.frozen THEN order_item_cost.pricing_evidence ELSE EXCLUDED.pricing_evidence END,
          pricing_checked_at = CASE WHEN order_item_cost.frozen THEN order_item_cost.pricing_checked_at ELSE EXCLUDED.pricing_checked_at END`,
        [
          it.oi_id, oId, str(it.sku_id), q, cp, amt,
          str(it.src_combine_sku_id), has, gift, strictHistorical ? it.cost_as_of : asOf,
          raw, scrapeAmt, reason, strictHistorical, strictHistorical ? it.cost_source : null,
          strictHistorical ? JSON.stringify(it.evidence) : null,
          originalCapturedAt || null,
        ]);
    }

    await client.query(`
      UPDATE order_cost_job
         SET state = $2, attempts = attempts + 1, last_error = NULL,
             done_at = now(), order_hash = $3
       WHERE o_id = $1`,
      [oId, withCost ? 'done' : 'empty', hash]);

    // One change per costed line, all under one revision. A per-order change
    // was not enough: the ledger prices by line, so "this order's cost moved"
    // leaves it re-reading every line to find out which one.
    try {
      await emitMany(client, priced.map((it) => ({
        entityType: ENTITY.orderCost,
        entityId: String(it.oi_id),
        orderId: String(oId),
        parentId: String(oId),
        subOrderId: String(it.oi_id),
        skuId: str(it.sku_id),
        orderStoreId: storeId,
        affectedDateFrom: day,
        payloadHash: hash,
      })));
    } catch (e) {
      if (!/integration_emit|does not exist/i.test(String(e.message || e))) throw e;
    }

    await client.query('COMMIT');
    return { lines: priced.length, withCost };
  } catch (e) {
    await client.query('ROLLBACK').catch(() => {});
    throw e;
  } finally {
    client.release();
  }
}

/** Park a job that could not be fetched, with capped exponential backoff. */
export async function failJob(pool, oId, reason) {
  await pool.query(`
    UPDATE order_cost_job
       SET state = 'failed', attempts = attempts + 1,
           last_error = $2,
           next_try_at = now() + (least(power(2, least(attempts + 1, 6)), 120) || ' minutes')::interval
     WHERE o_id = $1`,
    [oId, String(reason).slice(0, 400)]);
}

export async function progress(pool) {
  const r = await pool.query(`
    SELECT count(*) FILTER (WHERE state = 'pending') AS pending,
           count(*) FILTER (WHERE state = 'done')    AS done,
           count(*) FILTER (WHERE state = 'empty')   AS empty,
           count(*) FILTER (WHERE state = 'failed')  AS failed,
           count(*) AS total
      FROM order_cost_job`);
  return r.rows[0];
}

export async function beat(pool, phase, error) {
  await pool.query(`
    UPDATE ordercost_state
       SET beat_at = now(), pid = $1, phase = $2, updated_at = now(),
           last_error = COALESCE($3, last_error),
           last_error_at = CASE WHEN $3 IS NULL THEN last_error_at ELSE now() END
     WHERE id = 1`,
    [process.pid, phase, error ? String(error).slice(0, 500) : null]);
}

export async function addDone(pool, n) {
  if (n > 0) await pool.query('UPDATE ordercost_state SET done_total = done_total + $1 WHERE id = 1', [n]);
}

export async function modulePause(pool) {
  const r = await pool.query(`
    SELECT pause_reason, EXTRACT(EPOCH FROM (paused_until - now()))::int AS secs
      FROM ordercost_state WHERE id = 1 AND paused_until > now()`);
  return r.rows[0] || null;
}

export async function pauseModule(pool, secs, reason) {
  await pool.query(`
    UPDATE ordercost_state
       SET paused_until = now() + make_interval(secs => $1),
           pause_reason = $2, updated_at = now()
     WHERE id = 1`, [secs, String(reason).slice(0, 500)]);
}

/**
 * Is the collector keeping up?
 *
 * Order collection outranks costing: they share one ERP account, and this
 * worker is the heaviest of the three by request count. If the collector is
 * already struggling, the useful contribution here is to stop asking the ERP
 * for anything at all.
 */
export async function collectorHealthy(pool) {
  const r = await pool.query(
    `SELECT phase, EXTRACT(EPOCH FROM (now() - beat_at)) AS age
       FROM collect_heartbeat WHERE id = 1`);
  const age = Number(r.rows[0]?.age);
  if (Number.isFinite(age) && age >= 600) return false;
  const phase = r.rows[0]?.phase || '';
  if (phase === 'auth-blocked' || phase === 'auth-throttled' || phase === 'error') return false;
  return true;
}
