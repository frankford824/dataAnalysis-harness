// These are display metrics, not additional charges or payout inputs.
export const AFTER_LABOR_LABEL = '利润额（扣兼职）'

export function reportMetricText(row, key, money) {
  if (key === 'managed_sales' && row.managed_sales_pending) return '待核对'
  if (key === 'labor_cost' && row.labor_pending) return '待分摊'
  return money(row[key])
}

export function storeLaborFor(row, rows) {
  if (row.store_labor_cost != null) return row.store_labor_cost
  const store = rows.find(item => item.kind === 'store' && item.store_id === row.store_id && item.period === row.period)
  if (store) return store.labor_cost
  // A personal allocation must never be described as the whole shop's cost
  // in the profit drawer, including when pagination hides the store header.
  return row.kind === 'store' ? row.labor_cost : undefined
}
