// Backend contracts fix the rule and price logic; these helpers only form explicit ranges.
export const policyStatusLabels={active:'已生效',expired:'已到期',not_started:'尚未生效',disabled:'已停用',version_changed:'规则已更新，需重新授权',invalid:'授权资料需复核',superseded:'已有后续决定',revoked:'已撤销'}
export function shiftedMonth(period,months=11){
  if(!/^\d{4}-\d{2}$/.test(period||''))return ''
  const [year,month]=period.split('-').map(Number),date=new Date(Date.UTC(year,month-1+months,1))
  return `${date.getUTCFullYear()}-${String(date.getUTCMonth()+1).padStart(2,'0')}`
}
export function localDateTime(date=new Date()){
  return new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,16)
}
export function policyDecisionValues({kind,included,mode,period,from,until,rule,executionFrom,executionUntil}){
  const range=mode==='month'?{valid_from:period,valid_until:period}:{valid_from:from,valid_until:until}
  if(!/^\d{4}-\d{2}$/.test(range.valid_from||'')||!/^\d{4}-\d{2}$/.test(range.valid_until||'')||range.valid_from>range.valid_until)throw Error('请填写有效的业务起止月份')
  if(kind==='store_scope')return {included,...range}
  if(!rule?.contract_sha)throw Error('请先选择后台提供的固定规则')
  const start=new Date(executionFrom),end=new Date(executionUntil)
  if(!Number.isFinite(+start)||!Number.isFinite(+end)||end<=start)throw Error('请填写有效的执行起止时间')
  return {rule_id:rule.id,rule_sha:rule.contract_sha,enabled:true,period_from:range.valid_from,period_until:range.valid_until,valid_from:start.toISOString(),valid_until:end.toISOString()}
}
