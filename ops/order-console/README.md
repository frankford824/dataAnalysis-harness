# Order Console pricing pipeline overlay

These modules were captured from the deployed `D:/order/ordercost` runtime
(release `871f084`, ledger-feed.v1.4) because that runtime has no Git checkout.
They are now versioned here for reviewable, hash-guarded pricing maintenance.
Unchanged imports continue to come from the installed Order Console runtime;
this directory is not a standalone application or a replacement for the collector.

Apply `ordercost/historical-requests.sql` and `ordercost/pricing-queue-indexes.sql`
before copying the four runtime modules (all `.mjs` except the test file).
The index must be valid; an interrupted concurrent build is not success.
Back up every destination and verify its expected before hash. Copy
`maintenance-scheduling.mjs` first, then the maintenance/store/costapi modules.
Restart only the order-cost and cost-API workers using the existing supervisor.
Keep existing collector, account, browser pacing, and frozen-cost rules intact.

Changes:

- Oldest due orders get half of each detail batch; existing value priority gets
  the other half. Pending and failed retry dates are respected. One new partial
  index serves oldest-first reads; the existing priority index is reused.
- Historical requests are deduplicated by SKU, cost company and original day.
  Discovery scans a bounded range of item IDs and commits its cursor with its
  inserts. It wraps to revisit changed dates, SKUs and previously priced items.
  Existing probe outcomes remain the retry evidence; unprobed requests go first.
- Price-change pages are bounded. The next cursor is persisted only after that
  page's history refreshes succeed. An incomplete cursor resumes next loop,
  rather than waiting for the daily timer or replaying the first 51,000 events.

No current/reference price is promoted to historical evidence. Missing prices
remain missing; order-date/company checks and closed-cost protection remain in
the original pricing functions. Queue records contain no payable amount.

Local scheduling checks:

    node --test ops/order-console/ordercost/maintenance-scheduling.test.mjs

Release evidence belongs under the server's `D:/ledger/qa/<release>` directory:
before/after hashes, SQL validation, closed-period hashes, worker IDs/heartbeats,
saved run IDs, pricing-gap counts and API/browser readbacks.
