export function trialState(job){
  return {queued:'等待试算',running:'正在试算',ready:'试算已生成',failed:'试算未完成',interrupted:'试算已中断'}[job?.state] || '尚未生成试算'
}
export function canStart(job){return !['queued','running'].includes(job?.state)}
export function percentage(value){return value==null?'—':`${(Number(value)*100).toLocaleString('zh-CN',{maximumFractionDigits:2,minimumFractionDigits:2})}%`}
export function memberRows(detail){
  const charge=detail?.charge
  const allocations=new Map((charge?.allocations || []).map(a=>[a.target_key,a]))
  return (charge?.packet?.members || []).map(m=>({...m,allocation:allocations.get(m.target_key) || null}))
}
export function quantityLabel(member){
  if(member.basis_source==='erp_goods_amount')return `ERP发货明细 ${member.erp_items?.length || 0} 条`
  if(member.quantity_verified){
    const text=v=>Number(v).toLocaleString('zh-CN',{maximumFractionDigits:6})
    return `本次 ${text(member.shipped_quantity)} / 原数量 ${text(member.ordered_quantity)}`
  }
  return member.whole_child?'整单商品范围':'数量未用于分摊'
}
export function basisSourceLabel(member){
  return {erp_goods_amount:'聚水潭商品金额（本包实际发货明细合计）',platform_order:'平台订单原表',
    platform_order_with_checked_erp_portion:'本次发货金额片段与原子单合计核对',
    erp_checked_against_platform:'订单台金额与平台原表合计核对'}[member.basis_source] || '金额依据尚未核定'
}
export function identityLabel(member){
  return member.identity_kind==='erp_reship_item'?'ERP补发商品行':member.identity_kind==='native_item'?'平台商品行':'平台子订单'
}
import {expenseText} from './freightReview.js'

export function amountText(value){
  const n=Number(value)
  if(value!=null && Number.isFinite(n) && n!==0 && Math.abs(n)<.01)return `${n<0?'−':''}<¥0.01`
  return expenseText(value)
}
