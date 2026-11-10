// A recognised subtotal is not permission to prefill an unknown period total.
export const COST_KEYS = ['goods', 'dropship', 'reshipment']

function amount(value) {
  if (value == null || !['string', 'number'].includes(typeof value)) return null
  if (typeof value === 'string' && !/^-?\d+(?:\.\d+)?$/.test(value.trim())) return null
  const number = Number(value)
  return Number.isFinite(number) && Math.abs(number) <= 999999999999.99 ? number : null
}

export function needsManualCostInput(review) {
  return review?.requires_human === true && review?.line_posting_pending !== true
}

export function initialCostInputs(review) {
  return Object.fromEntries(COST_KEYS.map(key => {
    const value = amount(review?.known_partial?.[key] ?? review?.observed?.[key])
    return [key, value === null ? '' : value.toFixed(2)]
  }))
}

export function sourceCostText(review, key) {
  const value = amount(review?.known_partial?.[key] ?? review?.observed?.[key])
  if (value === null) return '总额待补齐'
  const shown = value.toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' 元'
  if (review?.amount_complete?.[key] === true) return shown
  return value === 0 ? '总额待补齐' : `已识别部分 ${shown}（总额待补齐）`
}
