// These are backend issues, not a second client-side close calculation.
export function finalCloseIssues(snapshot){
  const engine=new Set((snapshot?.findings||[]).filter(item=>!item.passed).map(item=>item.id))
  return (snapshot?.close_verification?.issues||[]).filter(item=>item.code!=='already_closed'&&!engine.has(item.code))
}
export function needsMonthWorkspace(issues,period){
  return /^\d{4}-(0[1-9]|1[0-2])$/.test(period||'')&&issues.some(item=>/^(company_|overhead_)/.test(item.code))
}
