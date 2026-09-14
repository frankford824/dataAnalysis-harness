-- Derived lookup requests, not financial values. Existing price probes retain
-- their original outcome and timestamps. Source orders/costs are not changed.
CREATE TABLE IF NOT EXISTS cost_history_request (
  sku_id text NOT NULL,
  company text NOT NULL,
  price_day date NOT NULL,
  discovered_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(sku_id, company, price_day)
);
