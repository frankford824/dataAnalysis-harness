// Checkpoint only after every SKU in this page has been processed. Replaying a
// failed page is safe (history upserts); skipping it would lose price changes.
export async function advanceChanges({state, read, refresh, save, months, batchSize, asOf, since}) {
  const cursor = state?.cursor || null;
  const page = await read({cursor, since: cursor ? null : since, limit: 200});
  if (!Array.isArray(page?.data)) throw new Error('Cost changes page has no data array');
  if (cursor && page.next_cursor === cursor) throw new Error('Cost changes cursor did not advance');
  const skus = [...new Set(page.data.map(row => row.sku_id).filter(Boolean))];
  for (const month of months) {
    for (let start = 0; start < skus.length; start += batchSize) {
      await refresh(skus.slice(start, start + batchSize), asOf(month));
    }
  }
  await save({cursor: page.next_cursor || null,
    watermark: page.watermark == null ? state?.watermark : String(page.watermark),
    snapshot_version: page.snapshot_version || state?.snapshot_version || null});
  return {events: page.data.length, skus: skus.length, more: Boolean(page.next_cursor)};
}

// Half the batch is reserved for the oldest due work. High-priority orders
// continue to progress without permanently starving last month's small orders.
export const fairClaimSQL = `
 WITH oldest AS MATERIALIZED (
   SELECT j.o_id FROM order_cost_job j JOIN jst_order o ON o.o_id=j.o_id
    WHERE j.state IN ('pending','failed') AND j.next_try_at<=now() AND o.o_id_en IS NOT NULL
    ORDER BY j.next_try_at,j.order_date,j.o_id LIMIT GREATEST(1,$1::int/2)
 ), priority AS MATERIALIZED (
   SELECT j.o_id FROM order_cost_job j JOIN jst_order o ON o.o_id=j.o_id
    WHERE j.state='pending' AND j.next_try_at<=now() AND o.o_id_en IS NOT NULL
      AND NOT EXISTS(SELECT 1 FROM oldest a WHERE a.o_id=j.o_id)
    ORDER BY j.priority DESC NULLS LAST
    LIMIT ($1::int-GREATEST(1,$1::int/2))
 )
 SELECT j.o_id,o.o_id_en,o.content_hash,o.shop_site FROM (
   SELECT o_id,0 AS lane FROM oldest UNION ALL SELECT o_id,1 AS lane FROM priority
 ) picked JOIN order_cost_job j USING(o_id) JOIN jst_order o USING(o_id)
 ORDER BY picked.lane,j.next_try_at,j.o_id`;
