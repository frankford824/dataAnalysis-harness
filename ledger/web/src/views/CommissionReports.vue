<script setup>
import { computed, h, onDeactivated, ref, watch } from 'vue'
import { NButton, NTag } from 'naive-ui'
import LedgerTabs from '../components/ui/LedgerTabs.vue'
import LedgerTable from '../components/ui/LedgerTable.vue'
import CommissionDetailDrawer from '../components/CommissionDetailDrawer.vue'
import ProfitCompositionDrawer from '../components/ProfitCompositionDrawer.vue'
import { useCommission } from '../commissionStore'
import { useCommissionQuery } from '../components/useCommissionQuery'
import { commissionRequest } from '../components/commissionRequest'
import { sameSettlementScope, settlementDifference as compareSettlement } from '../components/commissionSettlement'
import { reportRowActions } from '../commissionRowActions'
const state = useCommission()
const page = ref(1), downloading = ref(false), downloadError = ref(''), detail = ref(null)
const settlements = ref([]), settlementLoading = ref(false), settlementOpen = ref(false)
const settlementNote = ref(''), settling = ref(false), settlementError = ref('')
const payoutOpen = ref(false), payoutLoading = ref(false), payoutSaving = ref(false)
const payoutError = ref(''), payoutContext = ref(null), payoutPeople = ref([])
const payoutReason = ref(''), payoutNoPeople = ref(false)
const payoutNotice = ref('')
const payoutTargetsOpen = ref(false), payoutTargets = ref([]), payoutTargetTitle = ref('')
const profit = ref(null)
let payoutRequest = 0
const kinds = [{key:'store_people',label:'店铺与分配人'},{key:'people',label:'按人员'},{key:'stores',label:'按店铺'},{key:'breakdown',label:'按月明细'},{key:'coverage',label:'月份进度'}]
const scope = computed(() => ({start:state.start,end:state.end,...state.scope}))
const monthError = computed(() => state.start && state.end && state.start > state.end ? '结束月份不能早于开始月份' : '')
const queryKey = computed(() => JSON.stringify({...scope.value,view:state.reportView,offset:(page.value-1)*50}))
const {data:report,error,loading,stale,load} = useCommissionQuery('reports', () => queryKey.value,
  signal => commissionRequest('/reports/query',{signal,body:{...scope.value,view:state.reportView,offset:(page.value-1)*50,limit:50}}),
  () => !!state.start && !!state.end && !monthError.value && !downloading.value)
const rows = computed(() => report.value?.view === state.reportView ? report.value.items : [])
const money = value => value == null ? '—' : Number(value).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})
const locked = computed(() => stale.value || loading.value || !!monthError.value)
const warnings = computed(() => (report.value?.missing_periods || 0) + (report.value?.trial_periods || 0))
const assignmentGaps = computed(() => report.value?.assignment_gaps || [])
const canSettle = computed(() => !!report.value && report.value.total != null && !locked.value && !warnings.value)
const validMoney = value => /^-?\d+(?:\.\d{1,2})?$/.test(String(value ?? '').trim())
const payoutReady = computed(() => !!payoutContext.value && !!payoutReason.value.trim()
  && !payoutLoading.value && !payoutSaving.value && (payoutPeople.value.length
    ? payoutPeople.value.every(person => validMoney(person.amount)) : payoutNoPeople.value))
const payoutTotal = computed(() => payoutPeople.value.length && payoutPeople.value.every(p => validMoney(p.amount))
  ? money(payoutPeople.value.reduce((sum, person) => sum + Number(person.amount), 0)) : '—')
const labels = {store_people:'店铺人员构成',people:'人员汇总',stores:'店铺汇总',breakdown:'按月明细',coverage:'月份进度'}
function status(value='') { return value.replaceAll('未计算提成','未出金额').replaceAll('未计算','未出金额').replaceAll('试算','待核对').replaceAll('历史口径','历史提成').replaceAll('已计算','待结账').replaceAll('无对应提成记录','暂无提成').replaceAll('合计待核对','金额待核对') }
function explanation(row) {
  if(!row.has_result)return '本月还没有提成金额'
  if(row.unassigned_orders>0)return `${row.unassigned_orders} 笔订单尚未分配人员`
  if(row.notes?.includes('工资'))return '工资尚未确认'
  if(row.notes?.includes('合计'))return '人员合计与店铺金额不一致'
  if(row.notes?.includes('原结账'))return '结账时仍有金额待核对'
  if(row.notes || row.status?.includes('试算'))return '收入或成本仍需核对'
  return row.status==='已结账'?'本月已结账':''
}
const columns = computed(() => ({
  store_people:[['store','店铺'],['person','分配人'],['period','月份'],['sales','销售额'],['gross','毛利额'],['profit_after_labor','利润额'],
    ['labor_cost','兼职额'],['amount','提成额'],['status','状态']],
  people:[['person','人员'],['employee_no','工号'],['amount','提成金额'],['stores','店铺'],['periods','月份'],['status','状态']],
  stores:[['store','店铺'],['amount','提成金额'],['labor_cost','兼职分摊'],['configured_people','提成设置人数'],['people','已出金额人数'],['periods','已有金额'],['missing','未出金额'],['status','状态']],
  breakdown:[['person','人员'],['store','店铺'],['period','月份'],['amount','提成金额'],['status','状态']],
  coverage:[['store','店铺'],['period','月份'],['selected_amount','提成金额'],['status','状态'],['explanation','待办']],
}[state.reportView]))
function cell(row,key) {
  if(['amount','selected_amount','labor_cost','sales','gross','profit_after_labor','base','store_amount'].includes(key))return money(row[key])
  if(key==='status')return status(row.status)
  if(key==='explanation')return explanation(row)
  if(key==='stores')return `${row[key]} 家`
  if(key==='configured_people')return `${row[key] ?? 0} 人`
  if(key==='people')return row.periods ? `${row[key]} 人` : '未出金额'
  if(key==='periods'||key==='missing')return `${row[key]} 个月`
  return row[key] || '—'
}
function monthsAgo(month,delta) {const [year,part]=month.split('-').map(Number);const date=new Date(year,part-1+delta,1);return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}`}
function shortcut(which) {
  const month = new Date().toLocaleDateString('sv-SE',{timeZone:'Asia/Shanghai'}).slice(0,7)
  state.end=which==='last'?monthsAgo(month,-1):month
  state.start=which==='three'?monthsAgo(state.end,-2):state.end
}
function drill(row) {
  if(locked.value||row.amount==null)return
  const selection={...report.value.selection}
  let runIds=report.value.run_ids
  if(state.reportView==='people')selection.person_ids=[row.person_id]
  else if(state.reportView==='store_people'){
    selection.store_ids=[row.store_id]
    selection.person_ids=[row.person_id]
    runIds=[row.finance_run]
  }
  else{
    selection.store_ids=[row.store_id]
    runIds=(report.value.run_scopes||[]).filter(run=>run.store_id===row.store_id).map(run=>run.run_id)
  }
  detail.value={kind:state.reportView,name:row.person||row.store,expected:row.amount,selection,run_ids:runIds}
}
async function openPayout(row) {
  if(locked.value || !row.finance_run)return
  const ticket=++payoutRequest
  payoutOpen.value=true;payoutLoading.value=true;payoutError.value='';payoutContext.value=null
  payoutReason.value='';payoutNoPeople.value=false;payoutPeople.value=[]
  try {
    const params=new URLSearchParams({store_id:row.store_id,period:row.period,run_id:String(row.finance_run)})
    const context=await commissionRequest(`/payout-confirmations/context?${params}`)
    if(!payoutOpen.value || ticket!==payoutRequest)return
    payoutContext.value=context
    const saved=new Map((context.latest?.payouts||[]).map(person=>[person.person_id,person.amount]))
    payoutPeople.value=context.people.map(person=>({
      ...person,amount:saved.has(person.person_id)?Number(saved.get(person.person_id)).toFixed(2):
        person.suggested==null?'':Number(person.suggested).toFixed(2),
    }))
  } catch(e) { if(ticket===payoutRequest)payoutError.value=e.message }
  finally { if(ticket===payoutRequest)payoutLoading.value=false }
}
function targetsFor(row) {
  if(!report.value)return []
  let targets=[]
  if(['store_people','breakdown','coverage'].includes(state.reportView) && row.finance_run){
    targets=[{store_id:row.store_id,store:row.store,period:row.period,
      run_id:row.finance_run,status:row.status,
      amount:row.amount ?? row.selected_amount ?? row.store_amount}]
  }else if(state.reportView==='stores'){
    targets=(report.value.confirmation_scopes||[]).filter(item=>item.store_id===row.store_id)
  }else if(state.reportView==='people'){
    targets=(report.value.person_confirmation_scopes||[]).filter(item=>item.person_id===row.person_id)
  }
  const unique=new Map()
  for(const target of targets)if(target.run_id)unique.set(`${target.store_id}:${target.period}:${target.run_id}`,target)
  return [...unique.values()].sort((a,b)=>b.period.localeCompare(a.period)||a.store.localeCompare(b.store))
}
function choosePayout(row) {
  if(locked.value)return
  const targets=targetsFor(row)
  if(!targets.length){payoutError.value='当前行没有可确认的店铺月份核算记录';return}
  if(targets.length===1){openPayout(targets[0]);return}
  payoutTargets.value=targets
  payoutTargetTitle.value=row.person||row.store||'提成'
  payoutTargetsOpen.value=true
}
function pickPayout(target){payoutTargetsOpen.value=false;openPayout(target)}
function laborFor(row) {
  return rows.value.find(item => item.kind==='store' && item.store_id===row.store_id && item.period===row.period)?.labor_cost
    ?? row.labor_cost
}
function canOpenProfit(row) {
  return !!(row.person_id && row.finance_run && row.store_id && row.period && row.kind!=='store')
}
function openProfit(row) {
  if (locked.value || !canOpenProfit(row)) return
  profit.value = {
    store_id: row.store_id, store: row.store, period: row.period,
    person_id: row.person_id, person: row.person, run_id: row.finance_run,
    labor_cost: laborFor(row),
  }
}
function onProfitSaved(saved) {
  load()
  if (!payoutContext.value || payoutContext.value.run_id !== saved.run_id
      || payoutContext.value.store_id !== saved.store_id
      || payoutContext.value.period !== saved.period) return
  payoutPeople.value = payoutPeople.value.map(person =>
    person.person_id === saved.person_id || person.person === saved.person
      ? {...person, included_profit: saved.included_profit,
         excluded_count: (saved.excluded_product_ids || []).length}
      : person)
}
function openProfitFromPayout(person) {
  const context = payoutContext.value
  if (!context) return
  profit.value = {
    store_id: context.store_id, store: context.store, period: context.period,
    person_id: person.person_id, person: person.person, run_id: context.run_id,
    labor_cost: laborFor({store_id: context.store_id, period: context.period}),
  }
}
async function savePayout() {
  if(!payoutReady.value)return
  payoutSaving.value=true;payoutError.value=''
  try {
    const context=payoutContext.value
    await commissionRequest('/payout-confirmations',{body:{
      store_id:context.store_id,period:context.period,run_id:context.run_id,
      source_sha:context.source_sha,expected_confirmation_id:context.latest?.id||'',
      payouts:payoutPeople.value.map(({person_id,amount})=>({person_id,amount})),
      no_payout:payoutNoPeople.value,reason:payoutReason.value.trim(),
    }})
    payoutOpen.value=false
    payoutNotice.value=`${context.store} ${context.period} 的提成已确认。当前范围若还有其他店铺待确认，可先筛选这家店结算。`
    load()
  } catch(e) { payoutError.value=e.message }
  finally { payoutSaving.value=false }
}
watch(() => JSON.stringify(scope.value), () => {page.value=1;downloadError.value='';detail.value=null;profit.value=null;payoutNotice.value=''})
watch(() => state.reportView, () => {page.value=1;downloadError.value='';detail.value=null;profit.value=null})
onDeactivated(()=>{detail.value=null;profit.value=null})
watch(report, value => { if(value)state.reportPeople=value.available_people || [] })
async function download() {
  if(!report.value || locked.value || downloading.value)return
  downloading.value=true;downloadError.value=''
  try {
    const current=report.value, kind=state.reportView
    const response=await fetch(`/api/commission-v2/export/reports/${kind}`,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({...current.selection,run_ids:current.run_ids,fingerprint:current.fingerprint,presentation:true})})
    if(!response.ok){const failure=await response.json().catch(()=>({}));throw new Error(response.status===409?'金额有更新，请刷新后再导出':typeof failure.detail==='string'?failure.detail:'导出失败，请重试')}
    const url=URL.createObjectURL(await response.blob()),link=document.createElement('a')
    link.href=url;link.download=`提成-${labels[kind]}-${current.selection.start}至${current.selection.end}.csv`
    document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000)
  }catch(e){downloadError.value=e.message}finally{downloading.value=false;if(stale.value)load()}
}

const matchingSettlements = computed(() => settlements.value.filter(item =>
  sameSettlementScope(item.selection,scope.value)))
const latestSettlement = computed(() => matchingSettlements.value[0] || null)
const settlementDifference = computed(() => compareSettlement(report.value?.total,latestSettlement.value?.total))
const displayTime = value => value ? new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}) : '—'
async function loadSettlements() {
  if(!state.start||!state.end)return
  settlementLoading.value=true;settlementError.value=''
  try{
    const query=new URLSearchParams({start:state.start,end:state.end,limit:'50'})
    const response=await fetch(`/api/commission-v2/settlements?${query}`)
    const body=await response.json().catch(()=>({}))
    if(!response.ok)throw new Error(typeof body.detail==='string'?body.detail:'结算记录加载失败')
    settlements.value=body.settlements||[]
  }catch(e){settlementError.value=e.message}
  finally{settlementLoading.value=false}
}
watch(() => `${state.start}:${state.end}`, loadSettlements, {immediate:true})
function openSettlement() { if(canSettle.value){settlementNote.value='';settlementError.value='';settlementOpen.value=true} }
async function confirmSettlement() {
  if(!canSettle.value||!settlementNote.value.trim()||settling.value)return
  settling.value=true;settlementError.value=''
  try{
    const current=report.value
    const saved=await commissionRequest('/settlements',{body:{...current.selection,run_ids:current.run_ids,
      fingerprint:current.fingerprint,note:settlementNote.value.trim()}})
    settlementOpen.value=false
    await loadSettlements()
    if(saved.duplicate)settlementError.value='这个金额版本已经确认过结算，未重复生成记录。'
  }catch(e){settlementError.value=e.message}
  finally{settling.value=false}
}
async function viewSettlement(item){
  settlementLoading.value=true;settlementError.value=''
  try{
    const response=await fetch(`/api/commission-v2/settlements/${item.id}`),body=await response.json().catch(()=>({}))
    if(!response.ok)throw new Error(typeof body.detail==='string'?body.detail:'结算明细加载失败')
    detail.value={kind:'stores',name:`结算记录 ${displayTime(item.at)}`,expected:item.total,
      selection:item.selection,run_ids:item.run_ids,report:body.report,settlement_id:item.id}
  }catch(e){settlementError.value=e.message}
  finally{settlementLoading.value=false}
}

const rowKey=row=>[row.kind||'',row.person_id||'',row.store_id,row.period].filter(Boolean).join(':')
const tableColumns=computed(()=>{
  const composition=state.reportView==='store_people'
  const list=columns.value.map(([key,title],index)=>({title,key,
    width:composition?(key==='store'?180:key==='person'?92:key==='period'?86:key==='status'?92:112):
      ['amount','selected_amount','labor_cost','sales','gross','profit_after_labor','base','store_amount'].includes(key)?145:key==='employee_no'?90:key==='period'?100:index===0?undefined:key==='store'?240:125,
    minWidth:index===0?180:undefined,mobileWidth:['amount','selected_amount','labor_cost','sales','gross','profit_after_labor','base','store_amount'].includes(key)?115:key==='period'?84:index===0?135:undefined,
    mobile:index===0||['amount','selected_amount','sales','gross','profit_after_labor','labor_cost','period','person'].includes(key),align:['amount','selected_amount','labor_cost','sales','gross','profit_after_labor','base','store_amount'].includes(key)?'right':'left',
    render:row=>key==='status'?h(NTag,{bordered:false,size:'small',type:row.status?.includes('试算')?'warning':'default'},()=>status(row.status)):
      h('div',{class:['amount','selected_amount','labor_cost','sales','gross','profit_after_labor','base','store_amount'].includes(key)?['table-money',row[key]<0?'negative':'']:undefined,
               title:state.reportView==='store_people'&&row.kind==='person'&&['sales','gross'].includes(key)?'共享订单按提成点数占比分拆，可与其他成员相加':
                 state.reportView==='store_people'&&key==='profit_after_labor'?row.kind==='person'?'共享订单利润按提成点数拆分；兼职和未归属净亏损按成员参与销售额分摊，可与其他成员相加':'店铺经营账利润减本店兼职额':
                 state.reportView==='store_people'&&row.kind==='person'&&key==='labor_cost'?'兼职额按店铺分摊':undefined},
        index===0?[h('span',{class:row.kind==='store'?'store-total-name':''},cell(row,key)),h('div',{class:'table-secondary table-mobile-only'},status(row.status))]:
          key==='person'&&row.kind==='store'?h('strong','店铺合计'):
          key==='amount'&&row.kind==='store'?h('strong',money(row.store_amount)):cell(row,key))
  }))
  if(['people','stores','store_people','breakdown','coverage'].includes(state.reportView))list.push({
    title:'操作',key:'action',width:292,minWidth:220,mobileWidth:176,mobile:true,fixed:'right',
    render:row=>renderRowActions(row),
  })
  return list
})
function renderRowActions(row) {
  const actions = reportRowActions(row, {
    profit: canOpenProfit(row),
    payout: targetsFor(row).length > 0,
    detail: row.amount != null,
  })
  if (!actions.length) return h('span', {class:'report-row-actions-empty'}, '—')
  const clicks = {
    profit: () => openProfit(row),
    payout: () => choosePayout(row),
    detail: () => drill(row),
  }
  return h('div', {class:'report-row-actions', role:'group', 'aria-label':'行操作'},
    actions.flatMap((action, index) => [
      ...(index ? [h('span', {class:'report-row-action-split', 'aria-hidden':'true'})] : []),
      h('button', {
        type:'button',
        class:['report-row-action', `is-${action.kind}`],
        disabled: locked.value,
        onClick: clicks[action.key],
      }, action.label),
    ]))
}
</script>
<template>
  <div class="commission-content report-content">
    <div class="report-months"><span class="month-label">月份</span><input v-model="state.start" type="month" aria-label="开始月份"/><span class="date-separator">至</span><input v-model="state.end" type="month" aria-label="结束月份"/><div class="month-shortcuts"><button class="text-button" @click="shortcut('this')">本月</button><button class="text-button" @click="shortcut('last')">上月</button><button class="text-button" @click="shortcut('three')">最近三个月</button></div></div>
    <div v-if="monthError" class="commission-error" role="alert">{{ monthError }}</div>
    <div v-if="error || downloadError || settlementError" class="commission-error" role="alert">{{ error || downloadError || settlementError }}<button class="text-button" @click="downloadError='';settlementError='';load();loadSettlements()">重试</button></div>
    <div class="report-overview" :class="{'commission-stale':stale}"><div class="report-total"><span>{{warnings ? "已出金额合计" : "提成合计"}}</span><strong><small v-if="report?.total!=null">¥</small>{{ money(report?.total) }}</strong></div><div class="report-count"><strong>{{ report?.configured_people_count ?? '—' }}</strong><span>位人员已设置</span></div><div class="report-count"><strong>{{ report?.people_count ?? '—' }}</strong><span>位人员已有金额</span></div><div class="report-count"><strong>{{ report?.store_count ?? '—' }}</strong><span>家店铺</span></div><button v-if="warnings" class="report-attention" @click="state.reportView='coverage'"><span class="attention-dot"/>{{ state.reportView==='store_people'&&state.personIds.length ? (report?.missing_periods ? `全公司另有 ${report.missing_periods} 个店铺月份未出金额` : '全公司金额待核对') : (report?.missing_periods ? `${report.missing_periods} 个月份未出金额` : '金额待核对') }} <span>查看</span></button></div>
    <n-alert v-if="warnings" type="warning" style="margin-bottom:16px"><template v-if="state.reportView==='store_people'&&state.personIds.length">当前个人产出可查看；全公司仍有店铺月份未出金额或待核对，提成合计还不是最终应发金额。</template><template v-else>还有店铺月份未出金额或待核对，当前合计不是最终应发金额。</template></n-alert>
    <n-alert v-if="payoutNotice" type="success" :bordered="false" style="margin-bottom:16px">{{ payoutNotice }}</n-alert>
    <n-alert v-if="assignmentGaps.length" type="warning" style="margin-bottom:16px">
      <template v-for="gap in assignmentGaps.slice(0,3)" :key="`${gap.store_id}:${gap.period}`">
        <span>{{ gap.store }} {{ gap.period }}：{{ gap.orders }} 笔订单尚未分配人，未分配利润基数 ¥{{ money(gap.base) }}。表内提成额只算已分配订单，是试算，不是最终应发金额。</span>
        <router-link :to="{name:'period',params:{id:gap.store_id},query:{period:gap.period}}" class="text-button">到店铺人工确认提成 →</router-link>
      </template>
      <span v-if="assignmentGaps.length>3">另有 {{ assignmentGaps.length-3 }} 个店铺月份未分配完整，可在月份进度中查看。</span>
    </n-alert>
    <n-alert v-else-if="latestSettlement" :type="settlementDifference ? 'warning' : 'success'" style="margin-bottom:16px">
      本范围最近一次结算为 {{ displayTime(latestSettlement.at) }}，金额 ¥{{ money(latestSettlement.total) }}。
      <template v-if="settlementDifference">当前金额比该次结算{{settlementDifference>0?'增加':'减少'}} ¥{{money(Math.abs(settlementDifference))}}，原结算记录未改变。</template>
      <template v-else>当前金额与该次结算一致。</template>
    </n-alert>
    <p v-if="state.reportView==='stores'" style="color:#64748b;margin:0 0 12px">提成设置人数按所选月份的有效设置统计；已出金额人数只统计已有结算金额的人员。</p>
    <div class="report-tabs-row"><LedgerTabs v-model="state.reportView" :options="kinds" label="汇总方式" @update:model-value="detail=null" /><div class="report-actions"><n-button :disabled="!canSettle" @click="openSettlement">确认员工结算</n-button><n-button type="primary" :disabled="!report || locked" :loading="downloading" @click="download">导出表格</n-button></div></div>
    <p v-if="state.reportView==='store_people'" class="report-grain-note"><template v-if="state.personIds.length">当前仅显示所选人员；请清空人员筛选后再核对店铺合计。 </template>店铺销售额、毛利额和利润额是真实总额；个人三项金额均按有效提成点数拆分，可相加核对。兼职与未归属净亏损按成员销售额分摊，未归属净利润留在店铺；仅一位分配人时整店金额归本人。人员涉及的链接总点数唯一时，提成＝人员利润额×该点数；点数不一致时保留订单明细计算。兼职额仍只在店铺行显示。人员行可打开利润构成，勾掉不进阶梯的商品。已配置本店身份时，销售额/毛利额/利润额仅归属做货人员，抽点人员只计提成不计产出。</p>

    <div v-if="loading" class="commission-loading-line"/>
    <LedgerTable :rows="rows" :columns="tableColumns" :row-key="rowKey" :loading="loading" :max-height="440" empty="没有找到提成记录，可调整店铺、人员或月份" />
    <div class="commission-paging"><span class="row-count">共 {{report?.count || 0}} {{state.reportView==='people'?'人':state.reportView==='stores'?'家店铺':'条'}}</span><n-button size="small" :disabled="page<=1||locked" @click="page--">上一页</n-button><span>{{page}} / {{Math.max(1,Math.ceil((report?.count||0)/50))}}</span><n-button size="small" :disabled="page*50>=(report?.count||0)||locked" @click="page++">下一页</n-button></div>
    <section v-if="settlementLoading || matchingSettlements.length" class="settlement-history">
      <div class="spread"><div><h3>员工结算记录</h3><p>记录确认时所见金额；后续到账只显示差额，不改旧记录。</p></div></div>
      <n-spin :show="settlementLoading"><n-table v-if="matchingSettlements.length" size="small" :bordered="false"><thead><tr><th>确认时间</th><th>说明</th><th class="right">结算金额</th><th>操作</th></tr></thead><tbody><tr v-for="item in matchingSettlements" :key="item.id"><td>{{displayTime(item.at)}}</td><td>{{item.note}}</td><td class="right num">¥{{money(item.total)}}</td><td><n-button text type="primary" @click="viewSettlement(item)">查看当时明细</n-button></td></tr></tbody></n-table></n-spin>
    </section>
    <CommissionDetailDrawer :target="detail" @close="detail=null" />
    <ProfitCompositionDrawer :target="profit" @close="profit=null" @saved="onProfitSaved" />
    <n-modal v-model:show="payoutTargetsOpen" preset="card" :title="`${payoutTargetTitle}：选择要确认的店铺月份`" style="width:min(620px,calc(100vw - 32px))">
      <p class="settlement-help">汇总行可能包含多个店铺月份。人工确认按店铺月份保存，请选择一项。</p>
      <div class="payout-targets">
        <button v-for="target in payoutTargets" :key="`${target.store_id}:${target.period}:${target.run_id}`" type="button" @click="pickPayout(target)">
          <span><strong>{{ target.store }}</strong><small>{{ target.period }} · {{ status(target.status) }}</small></span>
          <span class="num">¥{{ money(target.amount) }}</span>
        </button>
      </div>
      <template #footer><div class="settlement-footer"><n-button @click="payoutTargetsOpen=false">取消</n-button></div></template>
    </n-modal>
    <n-modal v-model:show="payoutOpen" preset="card" title="人工确认提成" style="width:min(520px,calc(100vw - 32px))">
      <n-spin :show="payoutLoading">
        <template v-if="payoutContext">
          <p class="settlement-help">{{ payoutContext.store }} · {{ payoutContext.period }}。填本期最终提成，已含兼职费用；自动试算和原始订单会保留，店铺经营账不会因此结账。</p>
          <n-alert v-if="payoutContext.unassigned_orders" type="info" :bordered="false" style="margin-bottom:12px">还有 {{ payoutContext.unassigned_orders }} 笔订单没有提成归属，可由人工直接确认最终金额。</n-alert>
          <div v-if="payoutPeople.length" class="payout-people">
            <label v-for="person in payoutPeople" :key="person.person_id">
              <span>{{ person.person }}<small>系统试算 {{ money(person.suggested) }}</small>
                <small v-if="person.included_profit!=null">计入阶梯 ¥{{ money(person.included_profit) }}<template v-if="person.excluded_count"> · 已剔除 {{ person.excluded_count }} 个商品</template></small>
                <button type="button" class="report-inline-action" @click="openProfitFromPayout(person)">查看利润构成</button>
              </span>
              <n-input v-model:value="person.amount" inputmode="decimal" :aria-label="`${person.person}确认提成`" placeholder="确认金额" />
            </label>
            <p class="payout-sum">确认合计 <strong>¥{{ payoutTotal }}</strong></p>
          </div>
          <n-checkbox v-else v-model:checked="payoutNoPeople">确认本期无需发放提成</n-checkbox>
          <n-input v-model:value="payoutReason" type="textarea" :rows="2" maxlength="500" show-count placeholder="填写确认依据，例如：已与运营核对本期提成" style="margin-top:12px" />
          <details v-if="payoutContext.history?.length" class="payout-history">
            <summary>查看之前确认的提成（{{ payoutContext.history.length }}）</summary>
            <div v-for="item in payoutContext.history" :key="item.id">
              <span>{{ displayTime(item.at) }} · ¥{{ money(item.confirmed_total) }}<small v-if="item.finance_run !== payoutContext.run_id">后来有新核算</small></span>
              <small>{{ item.reason }}</small>
              <small>{{ item.payouts.map(person => `${person.person} ¥${money(person.amount)}`).join(' · ') }}</small>
            </div>
          </details>
        </template>
        <n-alert v-if="payoutError" type="error" :bordered="false" style="margin-top:12px">{{ payoutError }}</n-alert>
      </n-spin>
      <template #footer><div class="settlement-footer"><n-button @click="payoutOpen=false">取消</n-button><n-button type="primary" :loading="payoutSaving" :disabled="!payoutReady" @click="savePayout">保存确认金额</n-button></div></template>
    </n-modal>
    <n-modal v-model:show="settlementOpen" preset="card" title="确认员工结算" style="width:min(520px,calc(100vw - 32px))">
      <p class="settlement-help">确认后保存当前计算记录和金额。以后补到账单时，旧记录保持不变，页面会显示差额。</p>
      <n-alert v-if="warnings" type="warning">还有未出金额或待核对账期，暂不能结算。</n-alert>
      <n-input v-model:value="settlementNote" type="textarea" :rows="3" maxlength="2000" show-count placeholder="填写结算说明，例如：已于8月15日与员工核对并发放" />
      <template #footer><div class="settlement-footer"><n-button @click="settlementOpen=false">取消</n-button><n-button type="primary" :loading="settling" :disabled="!settlementNote.trim()||!canSettle" @click="confirmSettlement">确认并保存</n-button></div></template>
    </n-modal>
  </div>
</template>
<style scoped>
.mobile-context{display:none}
.report-tabs button{border-radius:0;box-shadow:none}.month-label{white-space:nowrap}

.report-content{padding-top:0}.report-months{display:flex;align-items:center;gap:12px;min-height:76px;border-bottom:1px solid #e9edf2;flex-wrap:wrap;padding:14px 0}.month-label{font-size:13px;margin-right:4px;color:#566176}.report-months input{height:35px;width:145px;max-width:100%;border:1px solid #dce2eb;border-radius:5px;padding:0 10px;background:#fff;font-size:13px;color:#30415c}.date-separator{font-size:13px;color:#8a94a3}.month-shortcuts{display:flex;gap:18px;margin-left:12px}.report-overview{display:flex;align-items:center;gap:0;padding:26px 0 27px;min-height:129px;border-bottom:1px solid #e9edf2}.report-total{padding-right:42px;min-width:240px}.report-total>span{font-size:13px;color:#67748a}.report-total strong{display:block;margin-top:7px;font-size:33px;line-height:1.3;font-weight:650;letter-spacing:-.7px;font-variant-numeric:tabular-nums}.report-total small{font-size:25px;margin-right:3px}.report-count{border-left:1px solid #e9edf2;padding:8px 32px;display:flex;align-items:baseline;gap:9px;white-space:nowrap}.report-count strong{font-size:28px;font-weight:600}.report-count span{color:#6e7b90;font-size:13px}.report-attention{display:flex;gap:8px;align-items:center;margin-left:auto;background:transparent;border:0;padding:0;color:#b88734;font-size:12px;cursor:pointer;text-align:left}.report-attention>span:last-child{color:#3468f0;margin-left:3px}.attention-dot{width:6px;height:6px;background:#d9a13d;border-radius:50%;flex:none}.report-tabs-row{display:flex;align-items:center;justify-content:space-between;gap:16px;min-height:76px}.report-tabs{display:flex;gap:28px;align-self:stretch;min-width:0;overflow:auto}.report-tabs button{border:0;border-bottom:2px solid transparent;background:transparent;color:#768397;font-size:13px;white-space:nowrap;padding:17px 0 13px;cursor:pointer}.report-tabs button.active{color:#3468f0;border-color:#3468f0;font-weight:550}.report-table th:first-child{width:22%}.report-table td:first-child{color:#30415b;font-weight:500}.report-state{font-size:12px;color:#8490a0}.report-state.review{color:#b18741}.report-empty-action{display:block;margin:9px auto 0}.report-back{padding:0 0 13px}.report-table{min-width:780px}
.report-actions{display:flex;gap:10px}.settlement-history{border-top:1px solid #e9edf2;margin-top:22px;padding-top:22px}.settlement-history h3{font-size:15px;margin:0}.settlement-history p,.settlement-help{font-size:12px;color:#718097;margin:5px 0 14px}.settlement-footer{display:flex;justify-content:flex-end;gap:10px}
.report-grain-note{font-size:12px;color:#718097;line-height:1.7;margin:0 0 14px}.report-row-actions{display:inline-flex;align-items:center;gap:0;max-width:100%;padding:3px;background:#f4f6f9;border:1px solid #e3e8f0;border-radius:9px;white-space:nowrap}.report-row-action{appearance:none;border:0;background:transparent;height:26px;padding:0 10px;border-radius:6px;font:inherit;font-size:12px;line-height:26px;color:#5a6578;cursor:pointer}.report-row-action:hover:not(:disabled){background:#fff;color:#1f5eff}.report-row-action.is-main{background:#fff;color:#1f5eff;font-weight:600;box-shadow:0 0 0 1px #d7e2ff}.report-row-action.is-main:hover:not(:disabled){background:#f4f7ff}.report-row-action:disabled{opacity:.4;cursor:default}.report-row-action-split{width:1px;height:14px;background:#d9dee8;flex:none}.report-row-actions-empty{color:#9aa3b2}.payout-people{display:grid;gap:12px;margin-top:14px}.payout-people label{display:grid;grid-template-columns:minmax(0,1fr) 145px;align-items:center;gap:10px;font-size:13px}.payout-people small{display:block;color:#8490a0;font-size:11px;margin-top:3px}.report-inline-action{display:inline-flex;margin-top:6px;border:0;background:#eef2f7;color:#3d4a5c;border-radius:999px;padding:2px 9px;font:inherit;font-size:11px;cursor:pointer}.report-inline-action:hover{background:#e4ebff;color:#1f5eff}.payout-sum{display:flex;justify-content:space-between;margin:2px 0 0;border-top:1px solid #e9edf2;padding-top:12px;font-size:13px}.payout-sum strong{font-size:17px;font-variant-numeric:tabular-nums}
.payout-history{margin-top:14px;border-top:1px solid #e9edf2;padding-top:10px;font-size:12px;color:#536176}.payout-history summary{cursor:pointer;color:#3468f0}.payout-history>div{margin-top:10px;display:grid;gap:3px}.payout-history small{color:#8490a0;font-size:11px;margin-left:4px}
.payout-targets{display:grid;gap:8px}.payout-targets button{border:1px solid #e3e8f0;background:#fff;border-radius:7px;padding:11px 13px;display:flex;justify-content:space-between;align-items:center;text-align:left;cursor:pointer;color:#334155}.payout-targets button:hover{border-color:#91adf8;background:#f7f9ff}.payout-targets strong{display:block;font-size:13px}.payout-targets small{display:block;color:#7b8798;font-size:11px;margin-top:4px}.payout-targets .num{font-weight:600;font-size:14px}
@media(max-width:1180px){.report-total{min-width:200px;padding-right:24px}.report-count{padding:8px 20px}.report-overview{flex-wrap:wrap;row-gap:18px}.report-attention{margin-left:0;flex-basis:100%}.report-tabs{gap:22px}}
@media(max-width:600px){.report-months{gap:8px}.report-months input{width:calc((100% - 63px)/2);min-width:0;padding:0 5px}.month-shortcuts{margin-left:40px;margin-top:6px}.report-overview{padding:22px 0;gap:18px}.report-total{flex-basis:100%;padding:0}.report-total strong{font-size:31px}.report-count{padding:0 20px 0 0;border:0}.report-count strong{font-size:21px}.report-tabs-row{flex-wrap:wrap;padding:12px 0 16px;gap:12px}.report-tabs{gap:22px;width:100%;min-height:42px}.report-tabs button{padding:10px 0}.report-attention{font-size:12px}}
@media(max-width:600px){.report-months{display:grid;grid-template-columns:28px minmax(0,1fr) 12px minmax(0,1fr);gap:6px}.report-months input{width:100%;min-width:0;font-size:12px}.month-shortcuts{grid-column:2/-1;margin:8px 0 0;gap:20px}.report-table{min-width:0}.report-table th:first-child{width:auto}.report-table td,.report-table th{padding:12px 9px;font-size:12px}.report-table .amount{width:96px;font-size:14px}.report-table .sticky-action{width:70px}.report-table [data-field=employee_no],.report-table [data-field=stores],.report-table [data-field=people],.report-table [data-field=periods],.report-table [data-field=missing],.report-table [data-field=status],.report-table [data-field=explanation]{display:none}.report-table [data-field=store]:not(:first-child){display:none}.mobile-context{display:block;margin-top:5px;font-size:10px;color:#8a94a3;font-weight:400}.report-table [data-field=period]{width:66px;font-size:11px}.report-table .text-button{font-size:11px}}
</style>
