// These are display metrics, not additional charges or payout inputs.
export const AFTER_LABOR_LABEL = '综合利润（含托管，已扣兼职）'
export const CREATOR_PROFIT_LABEL = '非托管创造利润（未扣兼职）'
export const LABOR_LABEL = '其中：已扣兼职（含托管）'
export const COMMISSION_BASE_LABEL = '提成计算基数（按计提规则）'

// Keep scope visible in every header, including horizontally scrolled/mobile tables.
export const REPORT_METRIC_HEADERS = {
  creator_cost: ['已入账做货成本', '仅非托管 · 缺口未计入'],
  creator_gross: ['创造毛利', '仅非托管'],
  creator_profit: ['非托管创造利润', '未扣兼职'],
  managed_sales: ['托管销售额', '利润已含在综合利润中'],
  profit_after_labor: ['综合利润', '含托管 · 已扣兼职'],
  labor_cost: ['其中：已扣兼职', '含托管分摊 · 勿重复扣'],
}

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
