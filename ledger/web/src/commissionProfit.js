function cents(value) {
  return value == null ? 0 : Math.round(Number(value) * 100)
}

export function liveProfitTotals(products = [], includedIds = []) {
  const keep = new Set(includedIds)
  let all = 0, included = 0, excludedCount = 0
  for (const row of products) {
    const amount = cents(row.profit)
    all += amount
    if (keep.has(row.product_id)) included += amount
    else excludedCount += 1
  }
  return {
    all: all / 100,
    included: included / 100,
    excluded: (all - included) / 100,
    excludedCount,
    includedCount: products.length - excludedCount,
  }
}

export function matchesProduct(row, term = '') {
  const needle = term.trim().toLowerCase()
  if (!needle) return true
  return [row.product_name, row.product_id].some(value =>
    String(value || '').toLowerCase().includes(needle))
}

export function excludedProductIds(products = [], includedIds = []) {
  const keep = new Set(includedIds)
  return products.map(row => row.product_id).filter(id => !keep.has(id))
}

export function sameIds(left = [], right = []) {
  if (left.length !== right.length) return false
  const other = new Set(right)
  return left.every(id => other.has(id))
}

export const PROFIT_BEFORE_LABOR = '本人创造利润（未扣兼职）'
export const PROFIT_BASIS_NOTE = '订单经营利润按本人提成份额拆到商品；未扣店级兼职；负数为该商品分到本人的亏损'

function csvCell(value) {
  if (value == null || value === '') return ''
  const text = String(value)
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

function percentLabel(rate) {
  return `${(Number(rate) * 100).toLocaleString('zh-CN', {maximumFractionDigits: 4})}%`
}

export function rateLabel(row) {
  const rates = (row?.rates || []).length ? row.rates
    : row?.rate == null ? [] : [row.rate]
  if (rates.length > 1 || row?.rate_mixed) {
    return rates.length ? rates.map(percentLabel).join(' / ') : '不一致'
  }
  if (!rates.length) return ''
  return percentLabel(rates[0])
}

export function profitCompositionExportRows(data, includedIds = []) {
  const keep = new Set(includedIds)
  return (data?.products || []).map(row => ({
    人员: data.person || '',
    店铺: data.store || '',
    月份: data.period || '',
    商品: row.product_name || '',
    宝贝ID: row.product_id || '',
    订单数: row.orders ?? '',
    销售额: row.sales,
    成本: row.sales != null && row.gross != null
      ? Math.round((Number(row.sales) - Number(row.gross)) * 100) / 100 : null,
    毛利: row.gross,
    [PROFIT_BEFORE_LABOR]: row.profit,
    本人点数: rateLabel(row),
    是否计入阶梯: keep.has(row.product_id) ? '计入' : '剔除',
    利润口径: PROFIT_BASIS_NOTE,
  }))
}

export function profitCompositionCsv(data, includedIds = []) {
  const headers = ['人员', '店铺', '月份', '商品', '宝贝ID', '订单数', '销售额', '成本', '毛利',
    PROFIT_BEFORE_LABOR, '本人点数', '是否计入阶梯', '利润口径']
  const rows = profitCompositionExportRows(data, includedIds)
  const lines = [headers.map(csvCell).join(','),
    ...rows.map(row => headers.map(key => csvCell(row[key])).join(','))]
  return `\uFEFF${lines.join('\r\n')}`
}
