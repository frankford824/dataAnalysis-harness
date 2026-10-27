// Backend classifies the remaining work; the UI never infers financial readiness.
export function onlyCompanyHistoryRemains(workflow){
  return workflow?.company_finance_only===true&&workflow?.can_advance===false&&workflow?.advance_block_reason==='company_finance_decision'
}
