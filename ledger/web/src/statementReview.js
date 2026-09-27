export function canConfirmStatement(group,chosen,reason,ack,reviewer,busy=false){
  return !busy && !!group?.can_confirm && group.members?.some(r=>r.anchor===chosen && r.can_select!==false)
    && !!ack && (reviewer||'').trim().length>0 && (reason||'').trim().length>=8
}
export async function recomputeReviewedStore(storeId,recompute){
  const result=await recompute(storeId)
  if(result?.failure)throw new Error(result.failure.why||'核对依据已保存，但重算未完成；请重试重算，不要重复保存核对')
  return result
}
