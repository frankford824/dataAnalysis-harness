-- Apply outside a transaction. The existing idx_ocj_due serves priority work; this adds oldest-first reads.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ocj_oldest_due
ON order_cost_job(next_try_at, order_date, o_id)
WHERE state IN ('pending', 'failed');
