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
