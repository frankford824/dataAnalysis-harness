export function expenseText(value) {
  return value == null || !Number.isFinite(Number(value)) ? '尚未形成'
    : `¥${Number(value).toLocaleString('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:2})}`
}

export function changeText(value) {
  if (value == null) return '暂不能比较'
  return `${Number(value)>0?'+':Number(value)<0?'−':''}${expenseText(Math.abs(Number(value)))}`
}

export function reviewConclusion(data) {
  if (!data?.summary?.store_count) return {title:'本月暂无核算记录', hint:'先在“数据与店铺”查看本月资料。'}
  const ids = new Set((data.groups || []).map(g=>g.id))
  if (ids.has('syncing')) return {title:'部分试算资料不完整', hint: data.summary.changed_store_count
    ? `可先复核 ${data.summary.changed_store_count} 家试算变化；资料未齐的店铺暂不比较。`
    : '先保留账内金额，有完整的新试算后再判断差额。'}
  if (ids.has('evidence')) return {title:'先核对金额依据', hint:'依据不齐的金额显示待核对，不按零元处理。'}
  if (ids.has('changes')) return {title:'试算有变化，原账未更正', hint:'按问题查看影响范围；需要查证时再展开原始记录。'}
  if (ids.has('no_trial')) return {title:'等待可对照的新结果', hint:'尚无后续完整试算的店铺，暂不能判断变化。'}
  if (ids.has('allocation')) return {title:'费用已计入，商品分摊待核对', hint:'先核对分摊依据，不要重复补扣店铺费用。'}
  return {title:'暂未发现试算差异', hint:'这不等于原始账单已逐笔核实；需要时可查看账内依据。'}
}

export function trialState(row) {
  return {ready:'可对照', same:'同一份试算', syncing:'试算资料未齐',
    evidence_missing:'金额依据待核对', no_trial:'尚无后续试算'}[row.trial_status] || '状态待核对'
}

export function taskStores(data, group) {
  return (data?.stores || []).filter(row=>!group || row.issues?.includes(group))
}

export function freightEvidence(row) {
  const records=[]
  if(row.shown_run_id)records.push({run_id:row.shown_run_id,label:'账内记录',at:row.shown_at,
    hint:row.closed?'结账时保留的记录，后续试算不会自动改写。':'当前未结账试算，仅用于核对。'})
  if(row.trial_run_id && row.trial_run_id!==row.shown_run_id)records.push({run_id:row.trial_run_id,label:'后续试算',at:row.trial_at,
    hint:row.trial_amount==null?'本次试算尚不完整，只可回查来源，不能确认差额。':'后续完整试算，尚未更正原结账和提成。'})
  return {store:row.store,store_id:row.store_id,period:row.period,platform:row.platform,expense:true,records}
}
