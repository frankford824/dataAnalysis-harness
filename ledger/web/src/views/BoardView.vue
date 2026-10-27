<script setup>
import {computed,onMounted,onUnmounted,ref,watch} from 'vue'
import {useRouter} from 'vue-router'
import {useMessage} from 'naive-ui'
import {api} from '../api'
import {useApp} from '../store'
import {money,percent,prettyPeriod,stamp} from '../format'
import {useLatest} from '../components/ui/useLatest'
import PageHead from '../components/PageHead.vue'
import CompanyFinanceCard from '../components/CompanyFinanceCard.vue'
import {onlyCompanyHistoryRemains} from '../companyFinanceView'
import {operationLevels,workflowLabels,comparisonCopy,sortedOperations} from '../operationsView'
import {clientRequestId,recordTelemetry} from '../telemetry'
import {policyStatusLabels,shiftedMonth,localDateTime,policyDecisionValues} from '../policyView'

const app=useApp(),router=useRouter(),message=useMessage()
const report=ref(null),workflow=ref(null),loading=ref(false),error=ref(''),workflowError=ref('')
const tab=ref('results'),level=ref('store'),managed=ref('all'),scope=ref({}),trail=ref([])
const expanded=ref(false),sortKey=ref('profit_after_labor'),sortDirection=ref('desc'),compare=ref('current')
const page=ref(1)
const request=useLatest(),workflowRequest=useLatest()
const period=computed(()=>app.period||app.periods[0]||'')
const params=computed(()=>({period:period.value,level:level.value,platform:app.platform,store_id:app.storeId,...scope.value,managed:managed.value,offset:(page.value-1)*50,limit:50,sort_key:sortKey.value,sort_direction:sortDirection.value,sort_basis:compare.value}))
const companyFinance=computed(()=>workflow.value?.decision_groups?.find(group=>group.kind==='company_finance')||null)
const monthAdvanceBlocked=computed(()=>onlyCompanyHistoryRemains(workflow.value))
const businessDecisionGroups=computed(()=>(workflow.value?.decision_groups||[]).filter(group=>group.kind!=='company_finance'))
const waitingGroups=computed(()=>workflow.value?.waiting_groups||[])
const waitingCount=computed(()=>workflow.value?.counts?.waiting??0)
const status=computed(()=>workflowError.value?'状态暂未更新':workflowLabels[workflow.value?.status]||'正在读取状态')
const rows=computed(()=>sortedOperations(report.value?.rows||[],sortKey.value,sortDirection.value))
const core=new Set(['net_income','goods_cost','gross_profit','promotion_cost','platform_cost','freight_cost','aftersales_cost','labor_cost','profit_after_labor'])
const columns=computed(()=>(report.value?.metrics||[]).filter(item=>expanded.value||core.has(item.key)))
const exportUrl=computed(()=>api.operationsExport({...params.value,expected_input_sha:report.value?.input_sha,comparison:compare.value}))
const busy=ref(false),preview=ref(null),modal=ref(false),decision=ref(null),decisionAmount=ref(''),decisionReason=ref(''),included=ref(true),selectedStore=ref(''),modalError=ref('')
const ruleContracts=ref(null),ruleError=ref(''),selectedRule=ref(''),scopeMode=ref('month'),businessFrom=ref(''),businessUntil=ref(''),executionFrom=ref(''),executionUntil=ref('')
const ruleOptions=computed(()=>(ruleContracts.value?.rules||[]).map(rule=>({label:rule.label,value:rule.id})))
const currentRule=computed(()=>(ruleContracts.value?.rules||[]).find(rule=>rule.id===selectedRule.value))
const storeOptions=computed(()=>(workflow.value?.stores||[]).map(row=>({label:row.store,value:row.store_id})))
const revocation=ref(null),revocationReason=ref(''),revocationSnapshot=ref(null),revocationError=ref('')
const actionId=clientRequestId
let timer,reportSerial=0
async function loadReport(quiet=false){
  if(!period.value)return
  const serial=++reportSerial
  if(!quiet)loading.value=true
  error.value=''
  try{const response=await request.run(signal=>api.operationsReport(params.value,{signal}));if(response)report.value=response.value}
  catch(reason){if(serial===reportSerial)error.value=reason.message}finally{if(serial===reportSerial)loading.value=false}
}
async function loadWorkflow(){
  if(!period.value)return
  try{const response=await workflowRequest.run(signal=>api.automationMonth(period.value,{signal}));if(response){workflow.value=response.value;workflowError.value=''}}
  catch(reason){workflowError.value=reason.message}
}
async function loadRules(){try{ruleContracts.value=await api.automationRuleContracts();ruleError.value=''}catch(reason){ruleError.value=reason.message}}
async function reload(){await Promise.all([loadReport(),loadWorkflow()])}
onMounted(async()=>{
  await app.loadNavigation().catch(()=>{});await Promise.all([reload(),loadRules()])
  timer=setInterval(()=>{if(!document.hidden&&!busy.value&&!modal.value&&!revocation.value){loadWorkflow();if(workflow.value?.status==='processing')loadReport(true)}},15000)
})
onUnmounted(()=>{clearInterval(timer);request.cancel();workflowRequest.cancel()})
watch(params,()=>loadReport(),{deep:true})
watch(period,()=>{workflow.value=null;report.value=null;modal.value=false;revocation.value=null;scope.value={};trail.value=[];page.value=1;loadWorkflow()})
watch([level,managed,scope,()=>app.storeId,()=>app.platform,sortKey,sortDirection,compare],()=>{page.value=1},{deep:true})
watch(()=>app.uiRefresh,reload)
function changeLevel(value){level.value=value;scope.value={};trail.value=[]}
function sort(key){if(sortKey.value===key)sortDirection.value=sortDirection.value==='desc'?'asc':'desc';else{sortKey.value=key;sortDirection.value='desc'}}
function reset(){app.pick({store:'',platform:''});scope.value={};trail.value=[];level.value='store'}
function openRow(row){
  if(level.value==='order'){openEvidence(row);return}
  const next={...scope.value}
  if(level.value==='team'){if(!row.team_id)return;next.team_id=row.team_id;level.value='person'}
  else if(level.value==='person'){if(!row.person_id)return;next.person_id=row.person_id;level.value='store'}
  else if(level.value==='store'){next.store_id=row.store_id;level.value='product'}
  else if(level.value==='product'){if(!row.product_id)return;next.store_id=row.store_id;next.product_id=row.product_id;level.value='order'}
  trail.value.push(row.name);scope.value=next
}
function periodDetail(id){if(!id)return;app.pick({store:id,period:period.value});router.push({name:'period',params:{id},query:{period:period.value}})}
function waitingStoreNames(group){return (group.store_ids||[]).map(id=>workflow.value?.stores?.find(store=>store.store_id===id)?.store||id).join('、')}
function noteWaitingSummary(){if(waitingCount.value)recordTelemetry({kind:'operation_complete',action:'waiting_summary_open',view:'board',period:period.value,store_id:app.storeId||'',duration_ms:0,outcome:'observed'})}
function openWaitingSummary(){tab.value='decisions';noteWaitingSummary()}
async function openWaitingDelivery(group){
  if(group.kind!=='source')return
  const storeId=group.store_ids?.length===1?group.store_ids[0]:'',wantedPeriod=period.value
  app.pick({store:storeId,platform:'',period:wantedPeriod});app.noted('deliver.section','work').value='work'
  try{
    await router.push({name:'deliver'})
    recordTelemetry({kind:'operation_complete',action:'waiting_deliver_open',view:'deliver',period:wantedPeriod,store_id:storeId,duration_ms:0,outcome:'observed'})
  }catch{message.error('资料入口暂未打开，请重试')}
}
async function advance(){
  if(busy.value)return
  busy.value=true
  try{workflow.value=await api.automationAdvance(period.value,{request_id:actionId()});message.success('已安排本月处理');await loadReport(true)}
  catch(reason){message.error(reason.message)}finally{busy.value=false}
}
function noteCompanyEvidence(){recordTelemetry({kind:'operation_complete',action:'view_open',view:'board',period:period.value,store_id:'',duration_ms:0,outcome:'observed'})}
function openDecision(group){
  if(group.kind==='company_finance')return
  if(group.kind==='close'){previewClose();return}
  if(!['store_scope','overhead_total','rule_grant'].includes(group.kind)){periodDetail(group.store_ids?.[0]);return}
  scopeMode.value='month';businessFrom.value=period.value;businessUntil.value=shiftedMonth(period.value);selectedRule.value=ruleContracts.value?.rules?.[0]?.id||''
  const now=new Date(),end=new Date(now);end.setFullYear(end.getFullYear()+1);executionFrom.value=localDateTime(now);executionUntil.value=localDateTime(end)
  decision.value=group;decisionAmount.value=workflow.value?.overhead?.total??'';decisionReason.value='';selectedStore.value=group.store_ids?.[0]||storeOptions.value[0]?.value||'';included.value=true;preview.value=null;modalError.value='';modal.value=true
}
async function previewDecision(){
  if(busy.value)return
  busy.value=true;modalError.value=''
  try{
    const kind=decision.value.kind
    const values=kind==='overhead_total'?{amount:String(decisionAmount.value)}:policyDecisionValues({kind,included:included.value,mode:scopeMode.value,period:period.value,from:businessFrom.value,until:businessUntil.value,rule:currentRule.value,executionFrom:executionFrom.value,executionUntil:executionUntil.value})
    preview.value=await api.automationDecisionPreview({request_id:actionId(),period:period.value,store_id:kind==='overhead_total'?'':selectedStore.value,kind,values,reason:decisionReason.value.trim()})
  }
  catch(reason){modalError.value=reason.message}finally{busy.value=false}
}
async function previewClose(){
  if(busy.value)return
  decision.value=null;preview.value=null;modalError.value='';modal.value=true;busy.value=true
  try{preview.value=await api.automationClosePreview({request_id:actionId(),period:period.value,store_ids:workflow.value?.close?.eligible_store_ids||[]})}
  catch(reason){modalError.value=reason.message}finally{busy.value=false}
}
async function applyPreview(){
  if(!preview.value||busy.value)return
  busy.value=true;modalError.value=''
  const body={request_id:actionId(),proposal_id:preview.value.proposal_id,proposal_sha:preview.value.proposal_sha,input_sha:preview.value.input_sha}
  try{
    const result=decision.value?await api.automationDecisionApply(body):await api.automationCloseConfirm(body)
    if(result.unfinished?.length)message.warning(`已完成 ${result.completed?.length||0} 家，${result.unfinished.length} 家继续保留待处理`)
    else message.success(decision.value?'决定已保存，系统继续核算':'本次结账已处理')
    modal.value=false;app.invalidate();await reload()
  }catch(reason){modalError.value=reason.message}finally{busy.value=false}
}
function openRevocation(policy){
  revocation.value=policy;revocationReason.value='';revocationSnapshot.value=null;revocationError.value=''
}
async function previewRevocation(){
  if(busy.value||!revocation.value||!revocationReason.value.trim())return
  busy.value=true;revocationError.value=''
  try{revocationSnapshot.value=await api.automationDecision(revocation.value.decision_id)}
  catch(reason){revocationError.value=reason.message}finally{busy.value=false}
}
async function confirmRevocation(){
  if(busy.value||!revocationSnapshot.value)return
  busy.value=true;revocationError.value=''
  try{
    await api.automationDecisionRevoke(revocation.value.decision_id,{request_id:actionId(),reason:revocationReason.value.trim(),input_sha:revocationSnapshot.value.input_sha})
    revocation.value=null;message.success('决定已撤销，历史记录保留');app.invalidate();await reload()
  }catch(reason){revocationError.value=reason.message;revocationSnapshot.value=null}finally{busy.value=false}
}
const evidence=ref(null),evidenceOpen=ref(false),evidenceBusy=ref(false),evidenceError=ref('')
async function openEvidence(row){
  if(!row.store_id)return
  evidenceOpen.value=true;evidence.value=null;evidenceError.value='';evidenceBusy.value=true
  try{evidence.value=await api.operationsEvidence({period:period.value,store_id:row.store_id,product_id:row.product_id,order_id:row.order_id,sub_order_id:row.sub_order_id,expected_input_sha:report.value.input_sha})}
  catch(reason){evidenceError.value=reason.message}finally{evidenceBusy.value=false}
}
const value=(key,amount)=>(report.value?.metrics||[]).find(item=>item.key===key)?.kind==='rate'?percent(amount):money(amount)
const compareText=item=>comparisonCopy(item,money)
const policyLabel=policy=>policy.kind==='store_scope'?'营业范围':ruleContracts.value?.rules?.find(rule=>rule.id===policy.values?.rule_id)?.label||'历史自动处理授权'
const policyRange=policy=>`${policy.effective_month_range?.from||policy.anchor_period} 至 ${policy.effective_month_range?.until||policy.anchor_period}`
const executionRange=policy=>`${stamp(policy.values?.valid_from)||'未留档'} 至 ${stamp(policy.values?.valid_until)||'未留档'}（当前设备时区）`
const previewRange=computed(()=>scopeMode.value==='month'?period.value:`${businessFrom.value} 至 ${businessUntil.value}`)
const metricChange=(key,item)=>comparisonCopy(item,(report.value?.metrics||[]).find(column=>column.key===key)?.kind==='rate'?amount=>`${(Number(amount)*100).toFixed(2)} 个百分点`:money)
</script>

<template>
  <PageHead title="月度工作台" :scope="[prettyPeriod(period),app.currentStore?.name||app.platformName].filter(Boolean)" hint="资料到齐后自动核算，只处理需要你决定的事项。">
    <template #actions><n-button :loading="loading" @click="reload">刷新</n-button><n-button type="primary" :loading="busy" :disabled="!period||monthAdvanceBlocked" @click="advance">继续处理本月</n-button></template>
  </PageHead>
  <p v-if="monthAdvanceBlocked" class="small muted month-alert" aria-label="本月处理入口说明">重算不能解除历史冻结差异，请公司财务核对已保存依据。</p>
  <n-alert v-if="error||workflowError" type="warning" class="month-alert">{{error||workflowError}}<n-button text @click="reload">重试</n-button></n-alert>
  <section class="month-progress" :class="workflowError?'unavailable':workflow?.status" aria-label="本月工作状态">
    <div><strong>{{status}}</strong><p>{{workflowError?'未取得最新处理状态，以下保留上次保存的结果。':waitingCount?`有 ${waitingCount} 个店月仍在等待资料或维护跟进，点击“其中等待”查看缺项与跟进角色。`:workflow?.status==='processing'?'系统正在收集、核算或复核，无需逐店重算。':workflow?.status==='completed'?'本月工作已完成，可以查看结果和依据。':workflow?.status==='needs_decision'?'以下事项需要决定，确认后系统继续推进。':'尚未取得后台工作状态，当前金额保留供查看。'}}</p></div>
    <div v-if="workflow" class="month-counts"><span>完成 <b>{{workflow.counts?.completed??0}}</b></span><span>处理中 <b>{{workflow.counts?.processing??0}}</b></span><button v-if="waitingCount" title="等待事项仍未完成" @click="openWaitingSummary">其中等待 <b>{{waitingCount}}</b></button><button @click="tab='decisions'">需决定 <b>{{workflow.counts?.needs_decision??0}}</b></button></div>
  </section>
  <div class="month-kpis" v-if="report">
    <div class="kpi"><span class="label">销售收入（到账）</span><b class="value">{{money(report.summary.net_income)}}</b><span class="foot">已扣销售退款</span></div>
    <div class="kpi"><span class="label">商品毛利</span><b class="value">{{money(report.summary.gross_profit)}}</b><span class="foot">毛利率 {{percent(report.summary.gross_margin)}}</span></div>
    <div class="kpi"><span class="label">利润额（扣兼职后）</span><b class="value" :class="{negative:Number(report.summary.profit_after_labor)<0}">{{money(report.summary.profit_after_labor)}}</b><span class="foot" :class="report.mom?.profit_after_labor?.direction">{{compareText(report.mom?.profit_after_labor)}}</span></div>
    <button class="kpi decision-kpi" @click="tab='decisions'"><span class="label">需要你的决定</span><b class="value">{{workflow?.decision_groups?.length??'—'}}</b><span class="foot">同类事项合并处理</span></button>
  </div>
  <n-spin :show="loading"><div class="card month-card"><n-tabs v-ledger-tabs v-model:value="tab" type="line" @update:value="value=>{if(value==='decisions')noteWaitingSummary()}">
    <n-tab-pane name="results" tab="经营分析">
      <div class="operations-controls">
        <label>查看维度<n-select :value="level" :options="operationLevels" @update:value="changeLevel" aria-label="经营分析维度"/></label>
        <label>经营范围<n-select v-model:value="managed" :options="[{label:'全部（互斥合计）',value:'all'},{label:'托管',value:'managed'},{label:'非托管',value:'standard'}]" aria-label="托管范围"/></label>
        <label>显示<n-select v-model:value="compare" :options="[{label:'本期数值',value:'current'},{label:'环比变化',value:'mom'},{label:'同比变化',value:'yoy'}]" aria-label="比较月份"/></label>
        <n-button @click="expanded=!expanded">{{expanded?'收起比率':'全部18项指标'}}</n-button><a :href="exportUrl" download class="export-link" aria-label="导出当前经营范围">导出当前范围</a>
      </div>
      <div v-if="trail.length" class="operations-trail"><n-button text @click="reset">全公司</n-button><span v-for="(name,index) in trail" :key="index"> / {{name}}</span></div>
      <p class="small muted operations-note">{{report?.is_final?'当前为已结账依据的完整经营结果。':'当前为阶段结果，缺少依据的金额显示“—”。'}} 点击名称继续查看人员、店铺、宝贝或订单；红色表示上升，绿色表示下降。</p>
      <n-alert v-if="report?.classification_pending_count&&managed!=='all'" type="warning" :bordered="false" class="month-alert">还有 {{report.classification_pending_count}} 项尚未确定托管分类，未计入当前分类小计。</n-alert>
      <div class="scroll operations-table" v-if="report?.rows?.length"><table>
        <thead><tr><th class="sticky-name">{{operationLevels.find(item=>item.value===level)?.label}}</th><th class="right">订单数</th><th v-for="column in columns" :key="column.key" class="right"><button class="column-sort" @click="sort(column.key)">{{column.label}}<span v-if="sortKey===column.key">{{sortDirection==='desc'?' ↓':' ↑'}}</span></button></th><th v-if="compare==='current'" class="right">环比利润额</th></tr></thead>
        <tbody><tr v-for="row in rows" :key="row.id" :class="{unclaimed:row.unclaimed}"><td class="sticky-name"><button class="row-drill" @click="openRow(row)">{{row.rank&&['person','team'].includes(level)&&sortKey==='profit_after_labor'?`${row.rank}. `:''}}{{row.name}}</button><small v-if="row.unclaimed">业绩待认领</small></td><td class="right num">{{row.order_count??'—'}}</td><td v-for="column in columns" :key="column.key" class="right num" :class="compare==='current'?'':row[compare]?.[column.key]?.direction">{{compare==='current'?value(column.key,row.metrics[column.key]):metricChange(column.key,row[compare]?.[column.key])}}</td><td v-if="compare==='current'" class="right num comparison" :class="row.mom?.profit_after_labor?.direction">{{compareText(row.mom?.profit_after_labor)}}</td></tr></tbody>
        <tfoot><tr><th>当前范围合计</th><td></td><td v-for="column in columns" :key="column.key" class="right num" :class="compare==='current'?'':report[compare]?.[column.key]?.direction">{{compare==='current'?value(column.key,report.summary[column.key]):metricChange(column.key,report[compare]?.[column.key])}}</td><td v-if="compare==='current'" class="right comparison" :class="report.mom?.profit_after_labor?.direction">{{compareText(report.mom?.profit_after_labor)}}</td></tr></tfoot>
      </table></div><n-empty v-else-if="!loading" description="当前范围尚无经营结果"/>
      <p v-if="Object.keys(report?.display_rounding||{}).length" class="small muted operations-note">当前范围全部记录按两位显示时有尾差：<span v-for="(item,key) in report.display_rounding" :key="key">{{item.label}} {{item.difference}} 元（逐项显示合计 {{item.displayed_rows_total}}，范围显示合计 {{item.displayed_scope_total}}）；</span>汇总与导出保留原始精度，以上仅作显示说明。</p>
      <div v-if="report?.total>50" class="operations-pages"><span>共 {{report.total}} 项，每页 50 项；导出包含当前范围全部记录。</span><n-pagination v-model:page="page" :item-count="report.total" :page-size="50" :disabled="loading" simple/></div>
      <details v-if="report?.reference_rows?.length" class="operations-details"><summary>抽点提成参考（{{report.reference_total??report.reference_rows.length}} 项，不累加到业绩）</summary><p v-if="report.reference_total>report.reference_rows.length">本页展示前 {{report.reference_rows.length}} 项，导出包含当前范围全部参考记录。</p><table><thead><tr><th>抽点人</th><th>店铺</th><th>宝贝ID</th><th>参考收入</th><th>已保存提成</th></tr></thead><tbody><tr v-for="(row,index) in report.reference_rows" :key="index"><td>{{row.person}}</td><td>{{row.store}}</td><td>{{row.product_id}}</td><td class="num">{{money(row.metrics.net_income)}}</td><td class="num">{{money(row.metrics.commission)}}</td></tr></tbody></table></details>
      <details v-if="report" class="operations-details"><summary>口径、来源与核算版本</summary><p>本页经营口径 {{report.version}}。提成使用已经保存的结果；组织分组按当前组织版本展示。兼职按未扣退款、含托管的已认领做货收入分配。</p><p v-if="report.labor_unallocated&&Number(report.labor_unallocated)!==0">还有兼职费用 {{money(report.labor_unallocated)}} 元未分配到个人，店铺费用仍保留。</p><ul><li v-for="issue in report.issues" :key="issue">{{issue}}</li></ul><p class="fingerprint">输入 {{report.input_sha}}<br/>范围 {{report.scope_sha}}</p><ul><li v-for="run in report.run_refs" :key="run.store_id">{{run.store_id}} · 核算 {{run.run_id??'未生成'}} · {{run.at||'时间未留档'}} <span v-if="run.stale">· 有新增资料</span></li></ul></details>
    </n-tab-pane>
    <n-tab-pane name="decisions" :tab="`需你决定（${workflow?.decision_groups?.length??0}）${waitingCount?` · 等待 ${waitingCount}`:''}`">
      <div v-if="waitingGroups.length" class="decision-list" aria-label="等待资料与维护摘要"><h3>等待资料或维护跟进</h3><article v-for="group in waitingGroups" :key="group.id" :data-waiting-kind="group.kind"><div><h3>{{group.missing_material}}</h3><p>{{group.summary}}</p><p>跟进角色：{{group.role_label||'角色未配置'}}<span v-if="group.assignee_status==='role_unconfigured'&&group.role_label">（角色未配置）</span> · 开始等待：{{stamp(group.since)||'未留档'}}</p><p>影响店铺：{{waitingStoreNames(group)}} · {{group.count}} 个店月</p><p>{{group.delivery_hint}}</p></div><n-button v-if="group.kind==='source'" @click="openWaitingDelivery(group)">去资料入口</n-button><span v-else-if="group.kind==='maintenance'">已交维护</span></article></div>
      <CompanyFinanceCard v-if="companyFinance" :group="companyFinance" :stores="workflow?.stores||[]" @open="noteCompanyEvidence"/>
      <div class="decision-list"><article v-for="(group,index) in businessDecisionGroups" :key="`${group.kind}:${index}`"><div><h3>{{group.title}}</h3><p>{{group.count}} 项 · {{group.store_ids?.length||0}} 家店</p></div><n-button @click="openDecision(group)">{{group.kind==='close'?'预览月结':['store_scope','overhead_total'].includes(group.kind)?'查看并决定':'查看需要的依据'}}</n-button></article></div>
      <n-empty v-if="workflow&&!workflow.decision_groups?.length&&!waitingGroups.length" :description="workflow.status==='processing'?'系统处理中，暂时没有需要你的决定':'暂无待决定事项'"/>
      <div class="decision-list"><article><div><h3>固定规则自动处理授权</h3><p>选择一店一规则及适用月份，一次确认后按有效范围执行。</p><p v-if="ruleContracts&&!ruleContracts.enabled">自动处理开关未启用，授权会保存，启用后才执行。</p></div><n-button :disabled="!ruleOptions.length" @click="openDecision({kind:'rule_grant',title:'授权固定规则自动处理'})">授权固定规则</n-button></article></div>
      <n-alert v-if="ruleError" type="warning" :bordered="false">{{ruleError}}<n-button text @click="loadRules">重试读取规则</n-button></n-alert>
      <details v-if="workflow?.policies?.length" class="operations-details" open><summary>本月适用的范围与授权历史（{{workflow.policies.length}}）</summary><ul><li v-for="policy in workflow.policies" :key="policy.decision_id" :data-policy-id="policy.decision_id"><strong>{{policyLabel(policy)}}</strong> · {{storeOptions.find(item=>item.value===policy.store_id)?.label||policy.store_id}} · {{policyStatusLabels[policy.status]||'状态待读取'}}<br/>业务月份：{{policyRange(policy)}}<span v-if="policy.kind==='store_scope'"> · {{policy.values?.included?'纳入处理':'排除处理'}}</span><br v-if="policy.kind==='rule_grant'"/><span v-if="policy.kind==='rule_grant'">执行有效期：{{executionRange(policy)}}</span><p>{{policy.reason}}</p><n-button v-if="policy.can_revoke" text size="small" :disabled="busy" @click="openRevocation(policy)">预览撤销</n-button><span v-else-if="policy.revoke_blocker">{{policy.revoke_blocker}}</span></li></ul></details>
      <div class="month-close"><div><strong>本月全公司结账</strong><p>{{workflow?.close?.eligible_store_ids?.length??0}} 家可进入预览，{{workflow?.close?.blocked_store_ids?.length??0}} 家保留待处理。</p></div><n-button type="primary" :disabled="!workflow?.close?.can_preview||busy" @click="previewClose">预览本月全公司结账</n-button></div>
      <details class="operations-details" v-if="workflow?.stores?.length"><summary>各店处理进度</summary><ul><li v-for="store in workflow.stores" :key="store.case_id||store.store_id"><button class="link" @click="periodDetail(store.store_id)">{{store.store}}</button> · {{workflowLabels[store.status]||store.status}} · {{store.message}} <n-button v-if="store.actions?.some(action=>action.kind==='store_scope')" text size="small" @click="openDecision({kind:'store_scope',title:'确认本月店铺范围',store_ids:[store.store_id]})">调整本月范围</n-button></li></ul></details>
    </n-tab-pane>
  </n-tabs></div></n-spin>
  <n-modal v-model:show="modal" preset="card" :title="decision?decision.title:'本月全公司结账预览'" style="max-width:640px;width:calc(100vw - 32px)"><n-spin :show="busy">
    <template v-if="decision&&!preview"><div class="policy-form">
      <label v-if="decision.kind!=='overhead_total'">适用店铺<n-select v-model:value="selectedStore" :options="storeOptions" aria-label="决定适用店铺"/></label>
      <n-checkbox v-if="decision.kind==='store_scope'" v-model:checked="included">纳入处理</n-checkbox>
      <template v-if="decision.kind==='rule_grant'"><label>固定业务规则<n-select v-model:value="selectedRule" :options="ruleOptions" aria-label="固定业务规则"/></label><p>{{currentRule?.description}}</p><n-alert v-if="ruleContracts&&!ruleContracts.enabled" type="info" :bordered="false">自动处理开关当前未启用。授权会保存，启用后才会按固定规则执行。</n-alert></template>
      <template v-if="decision.kind!=='overhead_total'"><label>适用业务月份<n-radio-group v-model:value="scopeMode" name="policy-month-scope" aria-label="适用业务月份"><n-space><n-radio value="month">仅本月</n-radio><n-radio value="range">持续生效（指定起止月）</n-radio></n-space></n-radio-group></label><div v-if="scopeMode==='range'" class="policy-dates"><label>业务开始月<input v-model="businessFrom" type="month" aria-label="业务开始月"/></label><label>业务结束月<input v-model="businessUntil" type="month" aria-label="业务结束月"/></label></div></template>
      <div v-if="decision.kind==='rule_grant'" class="policy-dates"><label>执行开始时间<input v-model="executionFrom" type="datetime-local" aria-label="执行开始时间"/></label><label>执行结束时间<input v-model="executionUntil" type="datetime-local" aria-label="执行结束时间"/></label></div>
      <n-input v-if="decision.kind==='overhead_total'" v-model:value="decisionAmount" inputmode="decimal" placeholder="本月兼职费用总额" aria-label="兼职费用总额" :input-props="{'aria-label':'兼职费用总额','inputmode':'decimal'}"/>
      <n-input v-model:value="decisionReason" type="textarea" placeholder="填写此次决定的依据" aria-label="决定依据" :input-props="{'aria-label':'决定依据'}"/>
      <n-button type="primary" :disabled="!decisionReason.trim()||busy||decision.kind!=='overhead_total'&&!selectedStore" @click="previewDecision">预览影响</n-button></div></template>
    <template v-if="preview"><p v-if="!decision">本次可确认 {{preview.eligible?.length??0}} 家；{{preview.blocked?.length??0}} 家继续保留，原已结账结果不变。</p><template v-else><p v-if="decision.kind==='overhead_total'">本月兼职总额确认 {{money(decisionAmount)}} 元，系统按本月公司收入分摊后继续核算。</p><template v-else><p>{{storeOptions.find(item=>item.value===selectedStore)?.label||selectedStore}} · 业务月份 {{previewRange}}</p><template v-if="decision.kind==='rule_grant'"><strong>{{currentRule?.label}}</strong><p>{{currentRule?.description}}</p><p>执行有效期：{{executionFrom.replace('T',' ')}} 至 {{executionUntil.replace('T',' ')}}（当前设备时区）</p><p v-if="!ruleContracts?.enabled" class="small muted">全局开关尚未启用；本次仅保存人工授权。</p></template><p v-else>将{{included?'纳入':'排除于'}}以上月份的处理范围。</p></template></template><ul v-if="preview.blocked?.length"><li v-for="(item,index) in preview.blocked" :key="index">{{typeof item==='string'?item:item.store||item.store_id}} {{item.message||item.reason||''}}</li></ul><p class="small muted">资料或结果变化时，本次预览将失效，需要重新预览。</p><n-button type="primary" :disabled="busy||(!decision&&!preview.eligible?.length)" @click="applyPreview">{{decision?'确认本次决定':'确认本次结账'}}</n-button></template>
    <n-alert v-if="modalError" type="error" style="margin-top:12px">{{modalError}}</n-alert>
  </n-spin></n-modal>
  <n-modal :show="!!revocation" @update:show="value=>{if(!value&&!busy)revocation=null}" preset="card" title="撤销范围或规则授权" style="max-width:640px;width:calc(100vw - 32px)"><n-spin :show="busy"><template v-if="revocation"><strong>{{policyLabel(revocation)}}</strong><p>{{storeOptions.find(item=>item.value===revocation.store_id)?.label||revocation.store_id}} · 业务月份 {{policyRange(revocation)}}</p><p v-if="revocation.kind==='rule_grant'">撤回此规则后续自动执行的权限。已完成处理和冻结金额继续保留，后台将再次核对当前依据。</p><p v-else>撤销本次营业范围或兼职总额决定后，后台重新判断未结月份。生效范围已有冻结账时不可撤销，请另设未来范围。</p><n-input v-model:value="revocationReason" type="textarea" :disabled="!!revocationSnapshot" placeholder="填写撤销依据" :input-props="{'aria-label':'撤销依据'}"/><n-button v-if="!revocationSnapshot" type="primary" :disabled="busy||!revocationReason.trim()" @click="previewRevocation" style="margin-top:16px">预览此次撤销</n-button><template v-else><p>已读取当前决定。确认撤销上述范围，理由：{{revocationReason}}</p><n-button type="primary" :disabled="busy" @click="confirmRevocation">确认此次撤销</n-button></template><n-alert v-if="revocationError" type="error" style="margin-top:12px">{{revocationError}}</n-alert></template></n-spin></n-modal>
  <n-drawer v-model:show="evidenceOpen" :width="920" style="max-width:100vw"><n-drawer-content title="订单核算与原始依据" closable><n-spin :show="evidenceBusy"><n-alert v-if="evidenceError" type="error">{{evidenceError}}</n-alert><template v-if="evidence"><p>核算 {{evidence.run_id}} · 来源事实 {{evidence.total}} 行</p><p v-if="!evidence.evidence_complete" class="small muted">本次旧核算的完整分摊证据未留档，以下仅展示已保存来源。</p><div class="scroll"><table><thead><tr><th>来源</th><th>工作表/行</th><th>科目</th><th class="right">入账贡献</th><th>说明</th></tr></thead><tbody><tr v-for="(row,index) in evidence.items" :key="index"><td>{{row.file_name}}</td><td>{{row.sheet}} / {{row.row_no}}</td><td>{{row.metric_id}}</td><td class="right num">{{money(row.contribution)}}</td><td>{{row.source_note}}</td></tr></tbody></table></div><p class="fingerprint">{{evidence.input_sha}}</p></template></n-spin></n-drawer-content></n-drawer>
</template>

<style scoped>
.month-alert{margin-bottom:16px}.month-progress{display:flex;justify-content:space-between;gap:24px;align-items:center;border:1px solid #e2e5eb;border-radius:8px;background:white;padding:18px 20px;margin-bottom:20px}.month-progress strong{font-size:18px;color:#35669a}.month-progress.needs_decision strong{color:#8b5914}.month-progress.completed strong{color:#17623f}.month-progress p{font-size:13px;color:#657184;margin:5px 0 0}.month-counts{display:flex;gap:20px;white-space:nowrap;font-size:13px}.month-counts b{margin-left:5px;font-size:18px}.month-counts button{border:0;background:none;cursor:pointer;color:#8b5914}.month-kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin-bottom:20px}.month-kpis .kpi{display:flex;flex-direction:column;gap:10px;text-align:left;padding:20px;background:white;border:1px solid #e2e5eb;border-radius:8px}.month-kpis .value{font-size:26px;font-variant-numeric:tabular-nums}.month-kpis .foot{font-size:12px;color:#657184}.decision-kpi{cursor:pointer}.month-card{margin-top:0}.operations-controls{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end;margin:12px 0 16px}.operations-controls label{font-size:12px;color:#657184;display:flex;flex-direction:column;gap:5px;min-width:150px}.export-link{margin-left:auto;padding:8px 0;color:#356bc6}.operations-note{margin:12px 0 18px}.operations-trail{font-size:13px}.operations-table{max-height:620px}.operations-table table{min-width:1200px;width:100%;border-collapse:separate;border-spacing:0}.operations-table th,.operations-table td{padding:13px 12px;white-space:nowrap}.operations-table th{font-size:12px;background:#f7f8fa;position:sticky;top:0;z-index:1}.sticky-name{position:sticky!important;left:0;z-index:2!important;background:#fff;min-width:150px;max-width:240px;white-space:normal!important}.row-drill{border:0;background:none;color:#356bc6;font:inherit;text-align:left;cursor:pointer;padding:0}.sticky-name small{display:block;font-size:11px;color:#8b5914;margin-top:4px}.column-sort{border:0;background:none;color:inherit;font:inherit;cursor:pointer}.operations-table tfoot{font-weight:600;background:#f8fafc}.comparison{font-size:12px}.up,.month-kpis .foot.up{color:#c04040}.down,.month-kpis .foot.down{color:#188052}.negative{color:#b74444}.unclaimed{background:#fffaf0}.operations-details{font-size:12px;color:#657184;margin:20px 0;overflow-wrap:anywhere}.operations-details summary{cursor:pointer;color:#356bc6}.operations-details p{margin:12px 0}.operations-details ul{padding-left:20px}.operations-details li{margin:8px 0}.operations-details table{width:100%;margin:14px 0}.fingerprint{font-size:11px;overflow-wrap:anywhere;margin-top:16px}.decision-list{display:flex;flex-direction:column;gap:12px;margin:16px 0}.decision-list article,.month-close{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:18px;border:1px solid #e2e5eb;border-radius:8px}.decision-list h3{font-size:15px}.decision-list p,.month-close p{font-size:12px;color:#657184;margin-top:6px}.month-close{margin-top:24px;background:#f8fafc}.policy-form{display:flex;flex-direction:column;gap:16px}.policy-form>label,.policy-dates label{display:flex;flex-direction:column;gap:8px;font-size:13px}.policy-dates{display:flex;gap:16px;flex-wrap:wrap}.policy-dates input{font:inherit;border:1px solid #dce1e8;border-radius:4px;padding:8px;max-width:100%;background:white}.policy-form p{font-size:13px;line-height:1.6;margin:0}.decision-effect{white-space:pre-wrap;font-size:12px;background:#f7f8fa;padding:14px;border-radius:6px;overflow-wrap:anywhere}
@media(max-width:760px){.month-kpis{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.month-kpis .kpi{padding:15px}.month-kpis .value{font-size:21px}.month-progress{align-items:flex-start;flex-direction:column;gap:15px}.month-counts{gap:15px}.operations-controls label{min-width:130px;flex:1}.export-link{margin-left:0}.decision-list article,.month-close{align-items:flex-start;flex-direction:column;gap:12px}.operations-table .sticky-name{min-width:130px;max-width:180px}.operations-controls :deep(button),.decision-list :deep(button){min-height:40px}}
</style>
