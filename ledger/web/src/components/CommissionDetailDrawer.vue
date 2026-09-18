<script setup>
import { computed, h, ref, watch } from 'vue'
import { commissionRequest } from './commissionRequest'
import { useLatest } from './ui/useLatest'
import LedgerTable from './ui/LedgerTable.vue'
const props=defineProps({target:{type:Object,default:null}})
const emit=defineEmits(['close'])
const request=useLatest(),current=ref(null),data=ref(null),loading=ref(false),error=ref(''),page=ref(1),downloading=ref(false)
let serial=0
const money=value=>value==null?'—':Number(value).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})
const mismatch=computed(()=>data.value && current.value?.expected!=null && data.value.total!==current.value.expected)
const columns=computed(()=>[
  ...(['stores', 'teams'].includes(current.value?.kind)?[{title:'人员',key:'person',minWidth:120,mobileWidth:100}]:[]),
  {title:'店铺',key:'store',minWidth:180,mobileWidth:130},
  {title:'月份',key:'period',width:90,mobileWidth:75},
  ...(current.value?.kind==='store_people'?[{title:'本人参与基数',key:'base',width:110,mobileWidth:95,align:'right',render:row=>money(row.base)}]:[]),
  {title:'系统应发',key:'trial_amount',width:105,mobileWidth:90,align:'right',render:row=>h('span',{style:'color:#64748b'},money(row.trial_amount))},
  {title:'参考提成金额',key:'amount',width:115,mobileWidth:95,align:'right',render:row=>money(row.amount)},
  {title:'已核定实发',key:'confirmed_amount',width:115,mobileWidth:95,align:'right',render:row=>money(row.confirmed_amount)},
  {title:'核定状态',key:'confirmation_state',width:95,render:row=>({pending:'待核定',partial:'部分核定',confirmed:'已核定实发'}[row.confirmation_state]||'历史记录')},
  {title:'调整差额',key:'diff_amount',width:100,mobileWidth:85,align:'right',render:row=>{
    if(!row.confirmed_count)return '—'
    if(!row.diff_amount || Math.abs(row.diff_amount)<0.001)return h('span',{style:'color:#94a3b8'},'0.00')
    const pos = row.diff_amount > 0
    return h('span',{style:{color:pos?'#16a34a':'#d97706',fontWeight:600}},`${pos?'+':''}${money(row.diff_amount)}`)
  }}
])
async function load(){
  if(!props.target)return
  const attempt=++serial;loading.value=true;error.value=''
  try{
    if(current.value.report){
      const rows=current.value.report.rows||[],offset=(page.value-1)*50
      data.value={...current.value.report,items:rows.slice(offset,offset+50),count:rows.length}
    }else{
      const result=await request.run(signal=>commissionRequest('/reports/query',{signal,body:{...current.value.selection,run_ids:current.value.run_ids,view:'breakdown',offset:(page.value-1)*50,limit:50}}))
      if(result)data.value=result.value
    }
  }catch(e){if(attempt===serial)error.value=e.message}
  finally{if(attempt===serial)loading.value=false}
}
watch(()=>props.target,value=>{
  serial++;request.cancel();loading.value=false
  if(value){current.value=value;data.value=null;if(page.value===1)load();else page.value=1}
})
watch(page,load)
async function download(){
  if(!data.value||mismatch.value||loading.value||downloading.value)return
  const target=current.value, snapshot=data.value
  downloading.value=true;error.value=''
  try{
    const response=target.settlement_id
      ? await fetch(`/api/commission-v2/export/settlements/${target.settlement_id}`)
      : await fetch('/api/commission-v2/export/reports/breakdown',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...snapshot.selection,run_ids:snapshot.run_ids,fingerprint:snapshot.fingerprint,presentation:true})})
    if(!response.ok){const body=await response.json().catch(()=>({}));throw new Error(typeof body.detail==='string'?body.detail:'导出失败，请重试')}
    const url=URL.createObjectURL(await response.blob()),link=document.createElement('a')
    link.href=url;link.download=`提成明细-${target.name}-${target.selection.start}至${target.selection.end}.csv`
    document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000)
  }catch(e){error.value=e.message}finally{downloading.value=false}
}
</script>
<template>
  <n-drawer :show="!!target" :width="'min(560px,100vw)'" @update:show="!$event&&emit('close')"><n-drawer-content :title="`${current?.name || ''}的提成`" closable :native-scrollbar="false">
    <p class="detail-scope">{{current?.selection.start}} 至 {{current?.selection.end}}</p>
    <div class="detail-total"><span>所选月份合计</span><strong>¥{{money(data?.total)}}</strong></div>
    <n-alert v-if="error" type="error" :bordered="false">{{error}} <n-button text @click="load">重试</n-button></n-alert>
    <n-alert v-if="mismatch" type="warning" :bordered="false">明细与列表金额不一致，请关闭后刷新列表。</n-alert>
    <LedgerTable :rows="data?.items||[]" :columns="columns" :row-key="row=>[row.person_id,row.store_id,row.period].join(':')" :loading="loading" :max-height="560" empty="没有对应的提成明细"/>
    <div class="commission-paging"><span class="row-count">共 {{data?.count||0}} 条</span><n-button size="small" :disabled="page<=1||loading" @click="page--">上一页</n-button><span>{{page}}</span><n-button size="small" :disabled="page*50>=(data?.count||0)||loading" @click="page++">下一页</n-button></div>
    <template #footer><n-space><n-button :disabled="!data||loading||mismatch" :loading="downloading" @click="download">导出明细</n-button><n-button type="primary" @click="emit('close')">关闭</n-button></n-space></template>
  </n-drawer-content></n-drawer>
</template>
<style scoped>.detail-scope{font-size:12px;color:#8390a3;margin-bottom:18px}.detail-total{padding:18px 20px;background:#f4f7fc;border-radius:7px;margin-bottom:20px}.detail-total span{color:#718097;font-size:12px}.detail-total strong{display:block;font-size:28px;font-weight:600;color:#3468f0;font-variant-numeric:tabular-nums;margin-top:4px}.n-alert{margin-bottom:14px}</style>
