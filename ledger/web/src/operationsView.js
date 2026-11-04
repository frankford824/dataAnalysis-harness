export const operationLevels = [{value:'store',label:'店铺'},{value:'team',label:'团队'},{value:'person',label:'个人'},{value:'product',label:'宝贝'},{value:'order',label:'订单'}]
export const workflowLabels = {completed:'完成',processing:'处理中',needs_decision:'需决定'}
export const coreMetrics = new Set(['net_income','goods_cost','gross_profit','promotion_cost','platform_cost','freight_cost','aftersales_cost','labor_cost','profit_after_labor'])
const CHANGE_METRICS=['net_income','refund_rate','goods_rate','gross_profit','gross_margin','promotion_rate','freight_rate','profit','profit_margin','labor_rate','profit_after_labor','after_labor_margin']
// 对应《经营分析看板数据汇总》的看板编号；metrics 为空表示 18 项全列。
export const operationBoards = [
  {id:'store',label:'1 各店铺',level:'store',compare:'current',metrics:null},
  {id:'mom',label:'2-1 店铺环比',level:'store',compare:'mom',metrics:CHANGE_METRICS},
  {id:'yoy',label:'2-2 店铺同比',level:'store',compare:'yoy',metrics:CHANGE_METRICS},
  {id:'person',label:'3 人员（做货/抽点）',level:'person',compare:'current',managedSplit:true,
    metrics:['net_income','goods_cost','gross_profit','promotion_cost','platform_cost','freight_cost','aftersales_cost','profit','labor_cost','profit_after_labor','after_labor_margin','gross_margin']},
  {id:'rank',label:'4 排名',level:'person',compare:'current',ranking:true,metrics:['net_income','gross_profit','profit','profit_after_labor']},
]
export function boardColumns(metrics=[],keys=null){
  if(!keys)return metrics.filter(item=>!item.component_of)
  const byKey=new Map(metrics.map(item=>[item.key,item]))
  return keys.map(key=>byKey.get(key)).filter(Boolean)
}
export function componentsOf(metrics=[],key){return metrics.filter(item=>item.component_of===key)}
export function identityColumns(level){return {platform:level==='store',team:level==='person',commission:['person','team'].includes(level)}}
export function managedPart(row,key){
  if(!row?.managed_metrics)return {known:false,value:null}
  return {known:true,value:row.managed_metrics[key]??null}
}
export function comparisonCopy(value, format) {
  if (!value || value.state==='unavailable') return '缺少可比数据'
  const delta=Number(value.delta),text=`${delta>0?'+':''}${format(value.delta)}`
  if(value.state==='zero_base') return `${text} · 上期为零`
  if(value.state==='negative_base') return `${text} · 上期为负`
  return `${text} · ${Number(value.rate)>0?'+':''}${(Number(value.rate)*100).toFixed(1)}%`
}
export function sortedOperations(rows,key,direction='desc') {
  return [...rows].sort((a,b)=>{
    if(a.unclaimed!==b.unclaimed)return a.unclaimed?1:-1
    const left=a.metrics?.[key],right=b.metrics?.[key]
    if(left==null || right==null)return left==null?(right==null?String(a.id).localeCompare(String(b.id)):1):-1
    return (Number(left)-Number(right))*(direction==='desc'?-1:1)||String(a.id).localeCompare(String(b.id))
  })
}
