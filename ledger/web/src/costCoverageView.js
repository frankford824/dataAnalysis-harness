// Presentation only: never infer complete coverage from a missing denominator.
export function costCoverageView(coverage, coverageRows = 0, pending = 0, closed = false, postedCount = null) {
  const known = Number(coverage?.expected) > 0 && Number.isFinite(coverage?.coverage)
  const gaps = known ? Math.max(Number(coverage?.uncovered || 0), Number(coverageRows || 0)) : Number(coverageRows || 0)
  // Posting counts are captured with this run, after matching unique gap keys.
  // Never use the latest saved line_count: those edits may await recomputation.
  const { expected, covered, uncovered } = coverage || {}
  const afterKnown = known && [expected, covered, uncovered, postedCount].every(Number.isSafeInteger)
    && covered >= 0 && uncovered >= 0 && covered + uncovered === expected
    && postedCount >= 0 && postedCount <= uncovered
  const afterCovered = afterKnown ? covered + postedCount : null
  const afterGaps = afterKnown ? uncovered - postedCount : null
  return {
    known, gaps,
    afterKnown, afterCovered, afterGaps,
    postedCount: afterKnown ? postedCount : null,
    afterPercentage: afterKnown ? `${(afterCovered / expected * 100).toFixed(1)}%` : '未统计',
    afterGapLabel: afterKnown ? `${afterGaps.toLocaleString('zh-CN')} 笔` : '待确认',
    percentage: known ? `${(coverage.coverage * 100).toFixed(1)}%` : '未统计',
    gapLabel: known || gaps ? `${gaps.toLocaleString('zh-CN')} 笔` : '待确认',
    title: closed ? '结账时成本覆盖' : '商品成本覆盖',
    description: closed ? '结账时保存的结果；后续资料不会自动改写历史账。'
      : !known ? '暂无可用于覆盖率统计的订单范围，请查看详情核对资料。'
      : afterKnown && gaps ? `原始缺口 ${gaps.toLocaleString('zh-CN')} 笔；本次核算已计入人工补录 ${postedCount.toLocaleString('zh-CN')} 笔，补录后剩余 ${afterGaps.toLocaleString('zh-CN')} 笔。`
      : gaps ? `仍有 ${gaps.toLocaleString('zh-CN')} 笔原始成本缺口；补录与计入情况请查看详情。`
      : pending ? '商品成本覆盖无缺口，另有来源成本记录待核对。'
      : '应核对订单均已覆盖，无需补录商品成本。',
    attention: !known || gaps > 0 || Number(pending) > 0,
  }
}
