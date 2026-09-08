-- Order Console PostgreSQL only. Execute each statement outside a transaction.
-- The commission rollout observed sequential scans of ~2.9M order items for
-- o_id::text lookups and ~6M outbox rows for each health/revision request.
-- These indexes preserve existing API SQL and every business row. Before running,
-- check pg_indexes for these exact names and pg_index.indisvalid/indisready.
CREATE INDEX CONCURRENTLY ledger_feed_order_item_oid_text_idx
    ON public.order_item ((o_id::text), oi_id);
CREATE INDEX CONCURRENTLY ledger_feed_outbox_pending_created_idx
    ON public.integration_outbox (created_at) WHERE processed_at IS NULL;
