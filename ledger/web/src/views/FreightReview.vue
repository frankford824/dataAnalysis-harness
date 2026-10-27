<script setup>
import { computed, defineAsyncComponent, h, onMounted, onUnmounted, ref, watch } from 'vue'
import { NButton } from 'naive-ui'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import { useApp } from '../store'
import { useLatest } from '../components/ui/useLatest'
import PageHead from '../components/PageHead.vue'
import LedgerTable from '../components/ui/LedgerTable.vue'
import DrillDrawer from '../components/DrillDrawer.vue'
import { changeText, expenseText, freightEvidence, reviewConclusion, taskStores, trialState } from '../freightReview'

const app=useApp(),route=useRoute(),router=useRouter()
const FreightShipmentTrial=defineAsyncComponent(()=>import('../components/FreightShipmentTrial.vue'))
const view=computed(()=>route.query.view==='records'?'records':'shipments')
function showView(value){evidence.value=null;router.replace({query:{...route.query,view:value}})}
const data=ref(null),loading=ref(false),error=ref(''),exporting=ref(false),ready=ref(false)
const group=ref(''),page=ref(1),evidence=ref(null),selectedRun=ref(null),newResult=ref(false)
const pollError=ref(false)
const request=useLatest(),pollRequest=useLatest()
let loadSerial=0
const params=computed(()=>({period:app.period,store_id:app.storeId,platform:app.platform}))
const scopeKey=value=>JSON.stringify(value)
const summary=computed(()=>data.value?.summary || {})
const conclusion=computed(()=>reviewConclusion(data.value))
const groups=computed(()=>data.value?.groups || [])
const selectedGroup=computed(()=>groups.value.find(g=>g.id===group.value))
const rows=computed(()=>taskStores(data.value,group.value))
const shownRows=computed(()=>rows.value.slice((page.value-1)*20,page.value*20))
const scope=computed(()=>[data.value?.scope?.period || app.period || '选择月份',app.storeId?(app.stores.find(s=>s.id===app.storeId)?.name || '所选店铺'):'全部登记店铺'])

function openEvidence(row){evidence.value=freightEvidence(row);selectedRun.value=evidence.value.records[0]?.run_id || null}
function pickGroup(id){group.value=id;page.value=1}
async function load(clear=false){
  const ticket=++loadSerial
  const wanted={...params.value},key=scopeKey(wanted)
  if(!/^\d{4}-(0[1-9]|1[0-2])$/.test(wanted.period || '')){
    request.cancel();pollRequest.cancel();data.value=null;evidence.value=null
    loading.value=false;error.value='请选择一个月份查看运费复核';return
  }
  pollRequest.cancel();loading.value=true;error.value=''
  if(clear){data.value=null;evidence.value=null;group.value='';page.value=1}
  try{
    const result=await request.run(signal=>api.freightReview(wanted,{signal}))
    if(!result || key!==scopeKey(params.value))return
    data.value=result.value;newResult.value=false;pollError.value=false
    if(!result.value.groups.some(g=>g.id===group.value))group.value=result.value.groups[0]?.id || ''
    page.value=1
  }catch(e){if(ticket===loadSerial)error.value=e.message}finally{if(ticket===loadSerial)loading.value=false}
}
async function checkUpdates(){
  if(document.hidden || loading.value || !data.value)return
  const wanted={...params.value},key=scopeKey(wanted)
  try{
    const result=await pollRequest.run(signal=>api.freightReview(wanted,{signal}))
    if(result && key===scopeKey(params.value)){newResult.value=result.value.fingerprint!==data.value.fingerprint;pollError.value=false}
  }catch{if(key===scopeKey(params.value))pollError.value=true}
}
async function download(){
  if(!data.value || exporting.value)return
  exporting.value=true;error.value=''
  try{
    const blob=await api.freightReviewExport({...data.value.scope,group:group.value,fingerprint:data.value.fingerprint})
    const url=URL.createObjectURL(blob),link=document.createElement('a')
    link.href=url;link.download=`运费复核-${data.value.scope.period}-${selectedGroup.value?.title || '全部店铺'}.csv`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000)
  }catch(e){error.value=e.message;if(e.message.includes('更新'))newResult.value=true}finally{exporting.value=false}
}
function routeScope(){
  const selected={}
  if(/^\d{4}-(0[1-9]|1[0-2])$/.test(route.query.period || ''))selected.period=route.query.period
  if(Object.hasOwn(route.query,'store_id'))selected.store=String(route.query.store_id || '')
  if(Object.hasOwn(route.query,'platform'))selected.platform=String(route.query.platform || '')
  app.pick(selected)
}
function syncRoute(){
  if(Object.entries(params.value).some(([key,value])=>String(route.query[key] || '')!==value)){
    router.replace({query:{...route.query,...params.value}})
  }
}
let timer
onMounted(async()=>{
  try{await app.loadNavigation();routeScope();ready.value=true;syncRoute();if(view.value==='records')await load(true)}catch(e){error.value=e.message}
  timer=setInterval(()=>{if(view.value==='records')checkUpdates()},30000)
})
onUnmounted(()=>{clearInterval(timer);request.cancel();pollRequest.cancel()})
watch(()=>scopeKey(params.value),()=>{if(ready.value){newResult.value=false;syncRoute();if(view.value==='records')load(true);else{request.cancel();pollRequest.cancel();data.value=null;evidence.value=null}}})
watch(view,value=>{if(ready.value && value==='records')load(true)})
watch(()=>route.query,()=>{if(ready.value)routeScope()})
watch(()=>app.uiRefresh,()=>{if(ready.value && view.value==='records')load()})

const columns=computed(()=>[
  {title:'店铺',key:'store',minWidth:190,mobileWidth:150,render:r=>h('div',{class:'freight-store'},[
    h('strong',r.store),h('small',r.closed?'已结账 · 原金额保留':'未结账试算'),
    r.trial_reason?h('small',{class:'freight-row-warning'},r.trial_reason):null])},
  {title:'账内已计入',key:'shown_amount',width:140,mobileWidth:125,align:'right',render:r=>expenseText(r.shown_amount)},
  {title:'后续试算',key:'trial_amount',width:140,mobileWidth:125,align:'right',render:r=>h('div',[expenseText(r.trial_amount),h('small',{class:'freight-secondary'},trialState(r))])},
  {title:'试算差额',key:'change',width:145,mobileWidth:130,align:'right',render:r=>h('span',{class:r.change?'freight-row-warning':''},changeText(r.change))},
  {title:'核对说明',key:'reason',minWidth:220,mobile:false,render:r=>r.allocation_reasons?.length?r.allocation_reasons.join('；'):r.shown_reason || r.trial_reason || (r.change?'差额尚未核实，原账未更正':'未发现试算变化')},
  {title:'依据',key:'action',width:105,mobileWidth:95,render:r=>h(NButton,{size:'small',text:true,type:'primary',disabled:!r.shown_run_id&&!r.trial_run_id,onClick:()=>openEvidence(r)},()=> '对照记录')},
])
</script>

<template>
  <main class="freight-review-page">
    <PageHead title="运费复核" :scope="scope" :hint="view==='records'?'对照已有核算记录；原始运费账单尚未逐笔核实。':'按实际发货商品试算运费，原账与提成保留。'">
      <template v-if="view==='records'" #actions><n-button :loading="loading" @click="load()">更新结果</n-button></template>
    </PageHead>
    <div class="freight-views" role="group" aria-label="查看运费记录"><n-button :type="view==='shipments'?'primary':'default'" :aria-pressed="view==='shipments'" @click="showView('shipments')">包裹分摊试算</n-button><n-button :type="view==='records'?'primary':'default'" :aria-pressed="view==='records'" @click="showView('records')">账内记录对照</n-button></div>
    <n-alert v-if="view==='records' && error" type="error" :bordered="false" role="alert">{{error}} <n-button text @click="load()">重试</n-button></n-alert>
    <div v-if="view==='records' && newResult" class="freight-update" role="status"><span>有新核算结果，当前仍显示上次读取的记录。</span><n-button size="small" @click="load()">更新结果</n-button></div>
    <p v-else-if="view==='records' && pollError" class="freight-money-note" role="status">暂时无法检查新数据，当前仍是上次读取的记录。可点击“更新结果”重试。</p>
    <n-spin v-if="view==='records'" :show="loading">
      <section class="freight-conclusion" aria-label="本月核对结论">
        <div><h2>{{conclusion.title}}</h2><p>{{conclusion.hint}}</p></div>
        <span v-if="summary.store_count" class="freight-scope-count">{{summary.store_count}} 家有核算记录 · {{summary.closed_count}} 家已结账</span>
      </section>
      <section v-if="data?.stores?.length" class="freight-metrics" aria-label="运费金额对照">
        <div><span>账内已计入</span><strong>{{expenseText(summary.shown_amount)}}</strong><small>{{summary.shown_amount==null?`${summary.shown_available_count}/${summary.store_count} 家有完整金额`:'已结账取原记录，未结账取当前试算'}}</small></div>
        <div><span>后续试算运费</span><strong>{{expenseText(summary.trial_amount)}}</strong><small>{{summary.trial_amount==null?`${summary.trial_available_count}/${summary.store_count} 家可对照；其余暂不比较`:'未结账店铺可与账内同为一份试算'}}</small></div>
        <div class="freight-change"><span>试算变化</span><strong>{{changeText(summary.change)}}</strong><small>尚未核实，不是应补扣金额</small></div>
      </section>
      <p v-if="data?.stores?.length" class="freight-money-note">费用按正数展示，退费冲减可为负数。原结账和提成不因查看或导出而改变。</p>
      <section v-if="data?.stores?.length" class="freight-work" aria-label="按问题查看">
        <div class="freight-section-title"><h2>核对事项</h2><span>按问题归组，不需要逐单打开。 <n-button text size="small" @click="pickGroup('')">全部店铺</n-button></span></div>
        <div v-if="groups.length" class="freight-task-list">
          <button v-for="task in groups" :key="task.id" type="button" :aria-label="task.title" :aria-pressed="group===task.id" :class="{selected:group===task.id}" @click="pickGroup(task.id)">
            <span class="freight-task-owner">{{task.owner}}</span><strong>{{task.title}}</strong><span>{{task.store_count}} 家店</span>
          </button>
        </div>
        <div v-else class="freight-no-issues">当前记录没有以上事项；这不代表原始账单已逐笔核实。</div>
        <section class="freight-affected" aria-label="问题影响范围">
          <div class="freight-section-title"><div><h3>{{selectedGroup?.title || '全部店铺'}}</h3><p>{{selectedGroup?.hint || '仅在需要查证时打开依据，无需逐单确认。'}}</p></div><n-button size="small" :loading="exporting" :disabled="!rows.length || loading" @click="download">{{group?'导出本组':'导出全部'}}</n-button></div>
          <p v-if="group==='changes'" class="freight-group-change">本组试算净变化 <strong>{{changeText(selectedGroup?.net_change)}}</strong><span>费用增加 {{summary.increased_store_count}} 家 · 减少 {{summary.decreased_store_count}} 家；尚未核实</span></p>
          <p class="freight-swipe-note">左右滑动查看完整金额与差额。</p>
          <LedgerTable :rows="shownRows" :columns="columns" :row-key="r=>r.store_id" :loading="loading" :max-height="520" empty="本组暂无记录"/>
          <div v-if="rows.length>20" class="freight-paging"><span>共 {{rows.length}} 家店，每页 20 家</span><n-button size="small" :disabled="page<=1" @click="page--">上一页</n-button><span>{{page}} / {{Math.ceil(rows.length/20)}}</span><n-button size="small" :disabled="page*20>=rows.length" @click="page++">下一页</n-button></div>
        </section>
        <details class="freight-scope-note"><summary>金额与核对范围</summary><p>{{data.notice}} 未在单店计入的公司公共表记录，不能跨店重复相加为漏扣金额。</p><p>费用是否真实、属于哪家店、应计入哪个月，需要账单与发货依据共同确认。当前页面不执行补扣或更正。</p></details>
      </section>
      <section v-else-if="!loading" class="freight-empty"><p>该范围内暂无运费核算记录。</p><n-button @click="router.push({name:'deliver'})">查看数据与店铺</n-button></section>
    </n-spin>
    <FreightShipmentTrial v-if="ready && view==='shipments'" :period="app.period" :store-id="app.storeId" :platform="app.platform" :refresh-token="app.uiRefresh"/>
    <DrillDrawer v-if="view==='records' && evidence && selectedRun" :run-id="selectedRun" node="n_freight" title="运费依据" :store-id="evidence.store_id" :period="evidence.period" :platform="evidence.platform" :review-context="evidence" :live-index="true" @select-record="selectedRun=$event" @close="evidence=null"/>
  </main>
</template>

<style scoped>
.freight-views{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0 22px}
.freight-swipe-note{display:none}@media(max-width:600px){.freight-swipe-note{display:block;font-size:var(--sk-type-caption);color:var(--n6);margin:0 0 10px}}
.freight-group-change{font-size:var(--sk-type-body);color:var(--n6);margin:0 0 16px;line-height:1.8}.freight-group-change strong{color:var(--sk-color-warning);font-variant-numeric:tabular-nums;font-weight:var(--sk-weight-medium);margin-left:6px}.freight-group-change span{display:block;font-size:var(--sk-type-caption)}
.freight-review-page{max-width:1280px;margin:0 auto;padding:28px}.freight-conclusion{display:flex;align-items:center;justify-content:space-between;gap:20px;margin:22px 0 18px}.freight-conclusion h2{font-size:var(--sk-type-heading);font-weight:var(--sk-weight-medium);margin:0 0 7px}.freight-conclusion p,.freight-section-title p{color:var(--n6);font-size:var(--sk-type-body);line-height:1.7;margin:0}.freight-scope-count{font-size:var(--sk-type-caption);color:var(--n6);white-space:nowrap}.freight-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.freight-metrics>div{background:var(--n0);border:1px solid var(--sk-color-line);border-radius:8px;padding:18px 20px;min-width:0}.freight-metrics span{display:block;font-size:var(--sk-type-body);color:var(--sk-color-muted)}.freight-metrics strong{display:block;font-weight:var(--sk-weight-medium);font-size:var(--sk-type-title);font-variant-numeric:tabular-nums;letter-spacing:-.5px;margin:10px 0;color:var(--n9);overflow-wrap:anywhere}.freight-metrics small{display:block;color:var(--n6);font-size:var(--sk-type-caption);line-height:1.6}.freight-metrics .freight-change{background:var(--warn-bg);border-color:var(--sk-color-action)}.freight-change strong{color:var(--sk-color-warning)}.freight-money-note{font-size:var(--sk-type-caption);color:var(--n6);margin:12px 0 26px;line-height:1.7}.freight-section-title{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:14px}.freight-section-title h2{font-size:var(--sk-type-section);margin:0;font-weight:var(--sk-weight-medium)}.freight-section-title h3{font-size:var(--sk-type-body);font-weight:var(--sk-weight-medium);margin:0 0 5px}.freight-section-title>span{font-size:var(--sk-type-caption);color:var(--n6)}.freight-task-list{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:22px}.freight-task-list>button{border:1px solid var(--n3);border-radius:7px;padding:13px 16px;background:var(--n0);display:grid;gap:4px;text-align:left;min-width:160px;color:var(--sk-color-ink);font:inherit;cursor:pointer}.freight-task-list>button.selected{border-color:var(--accent);background:var(--accent-bg)}.freight-task-list strong{font-size:var(--sk-type-body);font-weight:var(--sk-weight-medium)}.freight-task-list>button>span:last-child{font-size:var(--sk-type-caption);color:var(--n6)}.freight-task-owner{font-size:var(--sk-type-caption);color:var(--n6)}.freight-affected{padding:20px;background:var(--n0);border:1px solid var(--n3);border-radius:8px}.freight-affected>.freight-section-title{align-items:flex-start}.freight-affected:deep(.freight-store strong){font-weight:var(--sk-weight-medium);color:var(--n9);display:block;white-space:normal;overflow-wrap:anywhere}.freight-affected:deep(.freight-store small),.freight-affected :deep(.freight-secondary){display:block;color:var(--n6);font-size:var(--sk-type-caption);line-height:1.6;margin-top:4px;white-space:normal}.freight-affected:deep(.freight-row-warning){color:var(--sk-color-warning)}.freight-no-issues,.freight-empty{padding:24px;color:var(--n6);background:var(--n0);border:1px solid var(--n3);border-radius:8px;margin-bottom:20px}.freight-empty p{margin:0 0 12px}.freight-scope-note{margin:18px 0 0;color:var(--n6);font-size:var(--sk-type-caption);line-height:1.8}.freight-scope-note summary{cursor:pointer;color:var(--accent)}.freight-scope-note p{margin:8px 0 0}.freight-paging{display:flex;gap:12px;align-items:center;justify-content:flex-end;margin-top:16px;font-size:var(--sk-type-caption);color:var(--n6);flex-wrap:wrap}.freight-update{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 16px;margin:16px 0;background:var(--accent-bg);border-radius:6px;color:var(--sk-color-action);font-size:var(--sk-type-body)}.n-alert{margin:16px 0}
@media(max-width:900px){.freight-review-page{padding:20px}.freight-metrics{gap:10px}.freight-metrics>div{padding:15px}.freight-metrics strong{font-size:var(--sk-type-heading)}}
@media(max-width:600px){.freight-review-page{padding:16px}.freight-conclusion{align-items:flex-start;flex-direction:column;gap:8px}.freight-metrics{grid-template-columns:1fr 1fr}.freight-metrics>div:last-child{grid-column:1/-1}.freight-metrics strong{font-size:var(--sk-type-heading)}.freight-section-title{align-items:flex-start;flex-direction:column;gap:8px}.freight-affected{padding:14px}.freight-task-list{display:grid;grid-template-columns:1fr 1fr;gap:8px}.freight-task-list>button{min-width:0;padding:12px}.freight-update{align-items:flex-start;flex-direction:column}.freight-money-note{margin-bottom:22px}}
</style>
