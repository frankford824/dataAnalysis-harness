// Fast path for existing SKU history. Original bundle expansion remains on the
// paced detail queue; unavailable historical prices remain visibly unpriced.
export async function applyPddHistory(db, limit=20000) {
  const result=await db.query(`
    WITH picked AS (
      SELECT oi.oi_id,oi.o_id,oi.sku_id,oi.qty,oi.is_gift,o.shop_id,
             d.price_day,p.begin_date,p.end_date,d.company,
             CASE WHEN oi.order_flag='蓝色旗帜' THEN 0 ELSE p.cost_price END AS price,
             CASE WHEN oi.order_flag='蓝色旗帜' THEN 'blue_flag' ELSE 'history' END AS source
        FROM order_item oi JOIN jst_order o ON o.o_id=oi.o_id
        LEFT JOIN order_item_cost c ON c.oi_id=oi.oi_id
        CROSS JOIN LATERAL (SELECT oi.pricing_day AS price_day,oi.cost_company_id AS company) d
        LEFT JOIN LATERAL (
          SELECT h.cost_price,h.begin_date,h.end_date FROM sku_cost_period h
           WHERE h.sku_id=oi.sku_id AND h.wms_co_id=d.company AND h.begin_date<=d.price_day
             AND (h.end_date IS NULL OR h.end_date>=d.price_day)
           ORDER BY h.begin_date DESC LIMIT 1
        ) p ON true
       WHERE requires_order_day_history(o.shop_site) AND NOT COALESCE(c.frozen,false)
         AND NOT COALESCE(oi.is_suspect,false) AND oi.qty>0 AND d.price_day IS NOT NULL
         AND (oi.order_flag='蓝色旗帜' OR p.cost_price>0)
         AND (c.pricing_evidence IS NULL OR c.cost_as_of IS DISTINCT FROM d.price_day
           OR COALESCE(c.pricing_evidence->>'cost_company_id',c.pricing_evidence#>>'{components,0,cost_company_id}') IS DISTINCT FROM d.company
           OR c.o_id IS DISTINCT FROM oi.o_id OR c.sku_id IS DISTINCT FROM oi.sku_id
           OR c.qty IS DISTINCT FROM oi.qty
           OR c.cost_price IS DISTINCT FROM CASE WHEN oi.order_flag='蓝色旗帜' THEN 0 ELSE p.cost_price END
           OR c.cost_source IS DISTINCT FROM CASE WHEN oi.order_flag='蓝色旗帜' THEN 'blue_flag' ELSE 'history' END)
       LIMIT $1::int
    ), written AS (
      INSERT INTO order_item_cost(oi_id,o_id,sku_id,qty,is_gift,cost_price,cost_amount,has_cost,
        cost_source,cost_status,cost_as_of,fetched_at,pricing_checked_at,pricing_evidence,failure_reason)
      SELECT oi_id,o_id,sku_id,qty,is_gift,price,round(price*qty,2),true,source,'priced',price_day,now(),now(),
        jsonb_build_object('order_date',price_day,'cost_company_id',company,'sku_id',sku_id,
          'begin_date',begin_date,'end_date',end_date,'unit_cost',price,'rule',source),NULL FROM picked
      ON CONFLICT(oi_id) DO UPDATE SET o_id=EXCLUDED.o_id,sku_id=EXCLUDED.sku_id,qty=EXCLUDED.qty,is_gift=EXCLUDED.is_gift,
        cost_price=EXCLUDED.cost_price,cost_amount=EXCLUDED.cost_amount,has_cost=true,
        cost_source=EXCLUDED.cost_source,cost_status='priced',cost_as_of=EXCLUDED.cost_as_of,
        fetched_at=now(),pricing_checked_at=now(),pricing_evidence=jsonb_strip_nulls(jsonb_build_object(
          'original_components',order_item_cost.pricing_evidence->'original_components',
          'original_captured_at',order_item_cost.pricing_evidence->'original_captured_at',
          'source_order_hash',order_item_cost.pricing_evidence->'source_order_hash'))||EXCLUDED.pricing_evidence,failure_reason=NULL
      WHERE NOT order_item_cost.frozen RETURNING oi_id,o_id,sku_id,cost_as_of
    ), events AS (
      SELECT count(*)::int AS n,jsonb_agg(jsonb_build_object('entity_type','order_cost','entity_id',w.oi_id::text,
        'order_id',w.o_id::text,'parent_id',w.o_id::text,'sub_order_id',w.oi_id::text,'sku_id',w.sku_id,
        'order_store_id',o.shop_id::text,'affected_date_from',w.cost_as_of)) AS rows
      FROM written w JOIN jst_order o ON o.o_id=w.o_id
    ) SELECT n,integration_emit_many(COALESCE(rows,'[]'::jsonb)) AS revision FROM events`,[limit]);
  return result.rows[0]?.n||0;
}

export async function quarantinePddReferences(db, limit=20000) {
  const result=await db.query(`
    WITH picked AS (
      SELECT c.oi_id,oi.pricing_day AS price_day
        FROM order_item_cost c JOIN order_item oi ON oi.oi_id=c.oi_id JOIN jst_order o ON o.o_id=oi.o_id
       WHERE requires_order_day_history(o.shop_site) AND NOT c.frozen
         AND (c.pricing_evidence IS NULL OR c.cost_source NOT IN ('history','component_history','blue_flag','manual')
              OR c.cost_source IS NULL
              OR c.cost_as_of IS DISTINCT FROM oi.pricing_day
              OR c.qty IS DISTINCT FROM oi.qty OR c.sku_id IS DISTINCT FROM oi.sku_id
              OR COALESCE(c.pricing_evidence->>'cost_company_id',c.pricing_evidence#>>'{components,0,cost_company_id}') IS DISTINCT FROM oi.cost_company_id
              OR (c.cost_source='blue_flag' AND oi.order_flag IS DISTINCT FROM '蓝色旗帜')
              OR (c.cost_source='history' AND COALESCE((
                SELECT p.cost_price FROM sku_cost_period p WHERE p.sku_id=oi.sku_id AND p.wms_co_id=oi.cost_company_id
                  AND p.begin_date<=oi.pricing_day AND (p.end_date IS NULL OR p.end_date>=oi.pricing_day)
                ORDER BY p.begin_date DESC LIMIT 1),0)<=0))
         AND c.cost_price IS NOT NULL LIMIT $1::int
    ), written AS (
      UPDATE order_item_cost c SET cost_price=NULL,cost_amount=NULL,has_cost=false,
        cost_status='missing_price',cost_as_of=p.price_day,failure_reason='missing_historical_cost',
        pricing_checked_at=now(),pricing_evidence=jsonb_strip_nulls(jsonb_build_object(
          'original_components',c.pricing_evidence->'original_components','original_captured_at',c.pricing_evidence->'original_captured_at',
          'source_order_hash',c.pricing_evidence->'source_order_hash'))||jsonb_build_object('order_date',p.price_day,
          'reference_unit_cost',c.cost_price,'reference_source',c.cost_source,'reference_as_of',c.cost_as_of)
      FROM picked p WHERE c.oi_id=p.oi_id AND NOT c.frozen RETURNING c.oi_id,c.o_id,c.sku_id,c.cost_as_of
    ), events AS (
      SELECT count(*)::int AS n,jsonb_agg(jsonb_build_object('entity_type','order_cost','entity_id',w.oi_id::text,
        'order_id',w.o_id::text,'parent_id',w.o_id::text,'sub_order_id',w.oi_id::text,'sku_id',w.sku_id,
        'order_store_id',o.shop_id::text,'affected_date_from',w.cost_as_of)) AS rows
      FROM written w JOIN jst_order o ON o.o_id=w.o_id
    ) SELECT n,integration_emit_many(COALESCE(rows,'[]'::jsonb)) AS revision FROM events`,[limit]);
  return result.rows[0]?.n||0;
}

export async function queuePddPricing(db, limit=5000) {
  const result=await db.query(`
    UPDATE order_cost_job j SET state='pending',attempts=0,next_try_at=now(),last_error=NULL
     WHERE j.o_id IN (
       SELECT DISTINCT j2.o_id FROM order_cost_job j2 JOIN jst_order o ON o.o_id=j2.o_id
        JOIN order_item_cost c ON c.o_id=o.o_id
        JOIN order_item oi ON oi.oi_id=c.oi_id AND oi.o_id=c.o_id
       WHERE requires_order_day_history(o.shop_site) AND j2.state IN ('done','empty') AND NOT c.frozen
         AND (j2.state<>'empty' OR j2.done_at IS NULL OR j2.done_at<now()-interval '24 hours')
         AND c.cost_status='missing_price' AND
           (c.pricing_checked_at IS NULL OR c.pricing_evidence ? 'reference_unit_cost'
            OR c.pricing_checked_at<now()-interval '24 hours')
       ORDER BY j2.o_id DESC LIMIT $1::int
     )`,[limit]);
  return result.rowCount;
}

export async function fetchMissingPddHistory(db, historyReader, limit=300) {
  const rows=(await db.query(`
    SELECT d.price_day::text,d.company,d.sku_id FROM cost_history_request d
      LEFT JOIN cost_price_probe probe ON probe.sku_id=d.sku_id AND probe.cost_company_id=d.company AND probe.price_day=d.price_day
     WHERE (probe.checked_at IS NULL OR probe.checked_at<=now()-interval '24 hours')
       AND COALESCE((SELECT p.cost_price FROM sku_cost_period p WHERE p.sku_id=d.sku_id AND p.wms_co_id=d.company
         AND p.begin_date<=d.price_day AND (p.end_date IS NULL OR p.end_date>=d.price_day)
         ORDER BY p.begin_date DESC LIMIT 1),0)<=0
     ORDER BY probe.checked_at NULLS FIRST,d.discovered_at,d.price_day,d.company,d.sku_id LIMIT $1::int
`,[limit])).rows;
  const batches=new Map();
  for(const row of rows){const key=row.price_day+'|'+row.company;if(!batches.has(key))batches.set(key,{...row,ids:[]});batches.get(key).ids.push(row.sku_id);}
  let received=0;
  for(const batch of batches.values()) for(let offset=0;offset<batch.ids.length;offset+=30){
    const ids=batch.ids.slice(offset,offset+30),page=await historyReader(ids,batch.price_day,batch.company);
    if(!Array.isArray(page?.data))throw new Error('Historical cost API returned no data array');
    if(page.data.some(r=>!ids.includes(r.sku_id)||String(r.wms_co_id)!==String(batch.company)))
      throw new Error('Historical cost API returned a different cost company or SKU');
    for(const p of page.data){
      if(!ids.includes(p.sku_id)||String(p.wms_co_id)!==batch.company)continue;
      await db.query(`INSERT INTO sku_cost_period(sku_id,wms_co_id,begin_date,end_date,cost_price,as_of,fetched_at)
        VALUES($1::text,$2::text,$3::date,$4::date,$5::numeric,$6::date,now())
        ON CONFLICT(sku_id,wms_co_id,begin_date) DO UPDATE SET end_date=EXCLUDED.end_date,
          cost_price=EXCLUDED.cost_price,as_of=EXCLUDED.as_of,fetched_at=now()`,
        [p.sku_id,String(p.wms_co_id),p.begin_date,p.end_date||null,p.cost_price,batch.price_day]);received++;
    }
    for(const sku of ids)await db.query(`INSERT INTO cost_price_probe(sku_id,cost_company_id,price_day,outcome)
      VALUES($1::text,$2::text,$3::date,$4::text) ON CONFLICT(sku_id,cost_company_id,price_day)
      DO UPDATE SET checked_at=now(),outcome=EXCLUDED.outcome`,
      [sku,batch.company,batch.price_day,page.data.some(p=>p.sku_id===sku&&Number(p.cost_price)>0)?'found':'missing']);
  }
  return {checked:rows.length,received};
}

// Each batch and its discovery checkpoint commit atomically. A completed sweep
// wraps so changed original dates/SKUs and newly unpriced old items are revisited.
export async function discoverHistoryRequests(db, limit=50000) {
  const result=await db.query(`
    WITH batch AS MATERIALIZED (
      SELECT oi_id,o_id,sku_id,pricing_day,cost_company_id,order_flag,is_suspect FROM order_item
       WHERE oi_id>COALESCE((SELECT watermark::bigint FROM cost_api_state WHERE key='historical_requests_scan'),0)
       ORDER BY oi_id LIMIT $1::int
    ), inserted AS (
      INSERT INTO cost_history_request(sku_id,company,price_day)
      SELECT DISTINCT b.sku_id,b.cost_company_id,b.pricing_day FROM batch b
       JOIN jst_order o ON o.o_id=b.o_id LEFT JOIN order_item_cost c ON c.oi_id=b.oi_id
       WHERE requires_order_day_history(o.shop_site) AND b.pricing_day IS NOT NULL AND b.cost_company_id IS NOT NULL
         AND NOT COALESCE(c.frozen,false) AND NOT COALESCE(b.is_suspect,false)
         AND b.order_flag IS DISTINCT FROM '蓝色旗帜' AND octet_length(b.sku_id)<=100
         AND (c.oi_id IS NULL OR c.cost_status IS DISTINCT FROM 'priced'
          OR c.cost_source NOT IN ('history','component_history','blue_flag')
          OR c.pricing_evidence IS NULL OR c.cost_as_of IS DISTINCT FROM b.pricing_day)
      ON CONFLICT DO NOTHING RETURNING 1
    ), checkpoint AS (
      INSERT INTO cost_api_state(key,watermark,updated_at)
      SELECT 'historical_requests_scan',COALESCE(max(oi_id),0)::text,now() FROM batch
      ON CONFLICT(key) DO UPDATE SET watermark=EXCLUDED.watermark,updated_at=EXCLUDED.updated_at RETURNING watermark
    ) SELECT (SELECT count(*)::int FROM batch) scanned,(SELECT count(*)::int FROM inserted) queued,
             (SELECT watermark FROM checkpoint) cursor`,[limit]);
  return result.rows[0];
}
