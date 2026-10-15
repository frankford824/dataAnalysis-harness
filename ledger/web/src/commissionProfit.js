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

export const PROFIT_BEFORE_LABOR = '非托管创造利润（未扣兼职）'
export const PROFIT_BASIS_NOTE = '非托管商品销售额、成本、毛利及商品利润按明确生效的做货身份归属：单人做货归全额，多人仅按做货人员点数分摊，抽点不参与；依据不足时待确认。原核算分摊利润仍用于阶梯剔除和提成参考，不因创造归属变化而重算；托管商品另列'
export const COST_STATUS_SCOPE_NOTE = '原核算成本状态保留结账时或本次核算的缺口；当前成本状态仅按所列试算核对同一主单、子单的已入账成本。后续试算已补齐，不代表原结账成本、利润和提成已调整；当前仍缺成本，表示该试算仍保留缺口；证据不完整或尚无后续试算时，显示当前状态暂无法核实。'

export function costStatusLabel(row) {
  return row.cost_status || '成本覆盖留档不可用'
}

export function costStatusNote(row) {
  if (row.cost_status_note) return row.cost_status_note
  if (!row.cost_pending) return ''
  return row.cost_status === '成本口径待核对'
    ? '冻结补录与原提成成本口径待核对；成本空值不代表零，毛利与利润保留原明细已入账金额，不代表冻结补录已反映到原明细'
    : '成本待核实，成本空值不代表零；毛利与利润只反映已入账成本，尚未扣除成本缺口；原提成与核定金额未改动'
}

function costGapIds(row, status, field) {
  return [...new Set((row.cost_gap_details || []).filter(gap => gap.current_status === status)
    .map(gap => gap[field]).filter(Boolean))].sort().join(';')
}

function csvCell(value, header = '') {
  if (value == null || value === '') return ''
  let text = String(value)
  if (((header === '宝贝ID' || header.endsWith('单号')) && /^\d{15,}$/.test(text)) || /^[=+@\t\r\n]/.test(text)) text = "'" + text
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
    核算记录: data.run_id || '',
    核算档案校验值: data.calculation_sha || '',
    做货归属规则指纹: data.creator_rule_revision || '',
    商品: row.product_name || '',
    宝贝ID: row.product_id || '',
    订单数: row.orders ?? '',
    成本核实状态: costStatusLabel(row),
    原核算成本状态: row.cost_snapshot_status || costStatusLabel(row),
    当前成本状态: row.cost_current_status || '当前状态暂无法核实',
    当前核对试算: data.cost_comparison?.run_id || '',
    当前核对试算时间: data.cost_comparison?.at || '',
    成本状态说明: costStatusNote(row),
    原核算缺口主单号: (row.cost_pending_order_ids || []).join(';'),
    原核算缺口子单号: (row.cost_pending_sub_order_ids || []).join(';'),
    当前仍缺成本子单号: costGapIds(row, '当前仍缺成本', 'sub_order_id'),
    当前成本口径待核对子单号: costGapIds(row, '当前成本口径待核对', 'sub_order_id'),
    后续试算已补齐子单号: costGapIds(row, '后续试算已补齐', 'sub_order_id'),
    当前状态待核实子单号: costGapIds(row, '当前状态暂无法核实', 'sub_order_id'),
    '商品销售收入（全额）': row.product_sales,
    本人销售额: row.sales,
    销售归属状态: row.sales_pending ? '待确认身份' : '已解析',
    创造业绩归属状态: row.managed ? '托管商品另列' : row.creator_pending ? '待确认身份或商品金额' : '已解析',
    销售身份规则版本: (row.sales_rule_versions || []).join(';'),
    做货成本: row.cost_pending ? null : row.creator_cost,
    已入账做货成本: row.booked_creator_cost ?? row.creator_cost,
    做货毛利: row.creator_gross,
    [PROFIT_BEFORE_LABOR]: row.creator_profit,
    原核算分摊成本: row.cost_pending ? null : row.cost,
    原核算分摊毛利: row.gross,
    '原核算阶梯利润（未扣兼职）': row.profit,
    本人点数: rateLabel(row),
    是否计入阶梯: keep.has(row.product_id) ? '计入' : '剔除',
    利润口径: PROFIT_BASIS_NOTE + (row.cost_pending ? '；' + COST_STATUS_SCOPE_NOTE + costStatusNote(row) : '') + (data.allocation_correction ? `；历史分配更正自核算 ${data.allocation_correction.from_run}，已核实 ${data.allocation_correction.verified_orders} 个主单，另有 ${data.allocation_correction.pending_orders?.length || 0} 个主单保留原分配待复核；已核定实发未改变` : ''),
  }))
}

export function profitCompositionCsv(data, includedIds = []) {
  const headers = ['人员', '店铺', '月份', '核算记录', '核算档案校验值', '做货归属规则指纹', '商品', '宝贝ID', '订单数',
    '成本核实状态', '原核算成本状态', '当前成本状态', '当前核对试算', '当前核对试算时间', '成本状态说明',
    '原核算缺口主单号', '原核算缺口子单号', '当前仍缺成本子单号', '当前成本口径待核对子单号', '后续试算已补齐子单号', '当前状态待核实子单号',
    '商品销售收入（全额）', '本人销售额', '销售归属状态', '创造业绩归属状态', '销售身份规则版本',
    '做货成本', '已入账做货成本', '做货毛利', PROFIT_BEFORE_LABOR,
    '原核算分摊成本', '原核算分摊毛利', '原核算阶梯利润（未扣兼职）',
    '本人点数', '是否计入阶梯', '利润口径']
  const rows = profitCompositionExportRows(data, includedIds)
  const lines = [headers.map(label => csvCell(label)).join(','),
    ...rows.map(row => headers.map(key => csvCell(row[key], key)).join(','))]
  return `\uFEFF${lines.join('\r\n')}`
}
