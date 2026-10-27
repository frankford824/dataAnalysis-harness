export const operationLevels = [{value:'store',label:'店铺'},{value:'team',label:'团队'},{value:'person',label:'个人'},{value:'product',label:'宝贝'},{value:'order',label:'订单'}]
export const workflowLabels = {completed:'完成',processing:'处理中',needs_decision:'需决定'}
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
