-- Run outside a transaction on the order-console PostgreSQL database.
-- Existing oi_id primary keys remain unchanged. The expression index gives
-- the text-based v1 after-sale join the same unique-key information.
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS order_item_oi_id_text_idx
    ON order_item ((oi_id::text));
ANALYZE order_item;
-- Verify indisvalid=true and indisunique=true before accepting the migration.
SELECT indisvalid, indisunique FROM pg_index
 WHERE indexrelid='order_item_oi_id_text_idx'::regclass;

-- The order-level refund join also casts the primary key to text.
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS jst_order_o_id_text_idx
    ON jst_order ((o_id::text));
ANALYZE jst_order;
SELECT indisvalid, indisunique FROM pg_index
 WHERE indexrelid='jst_order_o_id_text_idx'::regclass;
