// Presentation only: never infer complete coverage from a missing denominator.
export function costCoverageView(coverage, coverageRows = 0, pending = 0, closed = false) {
  const known = Number(coverage?.expected) > 0 && Number.isFinite(coverage?.coverage)
  const gaps = known ? Math.max(Number(coverage?.uncovered || 0), Number(coverageRows || 0)) : Number(coverageRows || 0)
  return {
    known, gaps,
    percentage: known ? `${(coverage.coverage * 100).toFixed(1)}%` : '未统计',
    gapLabel: known || gaps ? `${gaps.toLocaleString('zh-CN')} 笔` : '待确认',
    title: closed ? '结账时成本覆盖' : '商品成本覆盖',
    description: closed ? '结账时保存的结果；后续资料不会自动改写历史账。'
      : !known ? '暂无可用于覆盖率统计的订单范围，请查看详情核对资料。'
      : gaps ? `仍有 ${gaps.toLocaleString('zh-CN')} 笔原始成本缺口；补录与计入情况请查看详情。`
      : pending ? '商品成本覆盖无缺口，另有来源成本记录待核对。'
      : '应核对订单均已覆盖，无需补录商品成本。',
    attention: !known || gaps > 0 || Number(pending) > 0,
  }
}
