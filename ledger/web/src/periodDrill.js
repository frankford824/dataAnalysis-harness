// An unavailable amount is not necessarily a cost-pricing problem.
// Receipt, promotion and identity-review rows must retain their own evidence.
export function periodDrillTarget(row) {
  return ['n_goods', 'g_goods'].includes(row?.id)
    && row?.unavailable_reason === '已算现有成本，支持人工确认金额'
    ? 'pricing' : 'facts'
}
