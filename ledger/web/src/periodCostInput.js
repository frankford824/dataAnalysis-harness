import {clientRequestId} from './telemetry.js'
export const periodCostKeys=['goods','dropship','reshipment']
const identity=value=>typeof value==='string'&&/^[A-Za-z0-9:_-]{1,128}$/.test(value)
const sha=value=>typeof value==='string'&&/^[a-f0-9]{64}$/.test(value)
export function costMoney(value){
  if(typeof value!=='string'||value.length>32||!/^[-]?\d+(?:\.\d{1,2})?$/.test(value.trim()))throw Error('请分别填写有效的三项成本金额，最多两位小数')
  const text=value.trim(),negative=text.startsWith('-'),[whole,fraction='']=text.replace(/^-/,'').split('.')
  let cents=BigInt(whole)*100n+BigInt(fraction.padEnd(2,'0'))
  if(cents>99999999999999n)throw Error('成本金额超过允许范围')
  if(negative)cents=-cents
  const absolute=cents<0n?-cents:cents
  return (cents<0n?'-':'')+String(absolute/100n)+'.'+String(absolute%100n).padStart(2,'0')
}
export function periodCostRequest(storeId,period,runId,costs,reason){
  if(typeof storeId!=='string'||!storeId||storeId.length>128||!/^\d{4}-(0[1-9]|1[0-2])$/.test(period)||!Number.isSafeInteger(runId)||runId<=0)throw Error('请先读取本店本期最新核算依据')
  if(!costs||Object.keys(costs).sort().join(',')!==[...periodCostKeys].sort().join(','))throw Error('必须分别填写全部三项成本')
  if(typeof reason!=='string'||!reason.trim()||reason.trim().length>500)throw Error('请填写不超过500字的成本批准依据')
  return {request_id:clientRequestId(),store_id:storeId,period,kind:'period_cost_input',
    values:{run_id:runId,costs:Object.fromEntries(periodCostKeys.map(key=>[key,costMoney(costs[key])]))},reason:reason.trim()}
}
export function verifyPeriodCostProposal(p,request,now=Date.now()/1000){
  const e=p?.effect,v=e?.values
  if(!identity(p?.proposal_id)||!sha(p.proposal_sha)||!sha(p.input_sha)||p.kind!=='decision'||
    p.store_id!==request.store_id||p.period!==request.period||e?.kind!=='period_cost_input'||
    e.reason!==request.reason||v?.run_id!==request.values.run_id||!Number.isFinite(p.expires_at)||p.expires_at<=now||
    !v.costs||Object.keys(v.costs).sort().join(',')!==[...periodCostKeys].sort().join(',')||
    periodCostKeys.some(key=>costMoney(v.costs[key])!==request.values.costs[key]))throw Error('成本预览的范围、金额或有效期不一致，请重新预览')
  return {request_id:clientRequestId(),proposal_id:p.proposal_id,proposal_sha:p.proposal_sha,input_sha:p.input_sha}
}
export function verifyPeriodCostReceipt(r,request){
  if(r?.status!=='applied'||r.store_id!==request.store_id||r.period!==request.period||
    !identity(r.cost_input_id)||!identity(r.decision_id)||!identity(r.receipt_id)||r.payouts_confirmed!==false||r.period_closed!==false)throw Error('尚未收到完整成本保存回执；可按原请求重试核对')
  return r
}
// A definite 4xx rejection wrote nothing. Lost responses, 5xx and malformed
// receipts may have committed, so only the same request may be resent.
export function outcomeUnknown(error){
  return !(error?.status>=400&&error.status<500)
}
export function periodCostRevocation(detail,scope,reason){
  if(detail?.decision_id!==scope.decision_id||detail.kind!=='period_cost_input'||detail.action!=='apply'||
    detail.store_id!==scope.store_id||detail.period!==scope.period||
    detail.values?.cost_input_id!==scope.cost_input_id||!sha(detail.input_sha))throw Error('成本批准范围已变化，请刷新后核对')
  if(typeof reason!=='string'||!reason.trim()||reason.trim().length>500)throw Error('请填写不超过500字的撤销依据')
  return {request_id:clientRequestId(),reason:reason.trim(),input_sha:detail.input_sha}
}
