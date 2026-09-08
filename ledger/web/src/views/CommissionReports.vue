<script setup>
import { computed, ref, watch } from 'vue'
import { useCommission } from '../commissionStore'
import { useCommissionQuery } from '../components/useCommissionQuery'
import { commissionRequest } from '../components/commissionRequest'
const state = useCommission()
const page = ref(1), downloading = ref(false), downloadError = ref(''), previous = ref(null)
const kinds = [{key:'people',label:'按人员'},{key:'stores',label:'按店铺'},{key:'breakdown',label:'按月明细'},{key:'coverage',label:'月份进度'}]
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
const labels = {people:'人员汇总',stores:'店铺汇总',breakdown:'按月明细',coverage:'月份进度'}
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
  people:[['person','人员'],['employee_no','工号'],['amount','提成金额'],['stores','店铺'],['periods','月份'],['status','状态']],
  stores:[['store','店铺'],['amount','提成金额'],['people','人员'],['periods','已有金额'],['missing','未出金额'],['status','状态']],
  breakdown:[['person','人员'],['store','店铺'],['period','月份'],['amount','提成金额'],['status','状态']],
  coverage:[['store','店铺'],['period','月份'],['selected_amount','提成金额'],['status','状态'],['explanation','待办']],
}[state.reportView]))
function cell(row,key) {
  if(key==='amount'||key==='selected_amount')return money(row[key])
  if(key==='status')return status(row.status)
  if(key==='explanation')return explanation(row)
  if(key==='stores')return `${row[key]} 家`
  if(key==='people')return `${row[key]} 人`
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
  previous.value={...state.scope,view:state.reportView}
  if(state.reportView==='people')state.personIds=[row.person_id]
  else state.storeIds=[row.store_id]
  state.reportView='breakdown'
}
function back() {
  state.storeIds=previous.value.store_ids;state.personIds=previous.value.person_ids;state.reportView=previous.value.view
  previous.value=null
}
watch(() => JSON.stringify(scope.value), () => {page.value=1;downloadError.value=''})
watch(() => state.reportView, () => {page.value=1;downloadError.value=''})
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
</script>
<template>
  <div class="commission-content report-content">
    <div class="report-months"><span class="month-label">月份</span><input v-model="state.start" type="month" aria-label="开始月份"/><span class="date-separator">至</span><input v-model="state.end" type="month" aria-label="结束月份"/><div class="month-shortcuts"><button class="text-button" @click="shortcut('this')">本月</button><button class="text-button" @click="shortcut('last')">上月</button><button class="text-button" @click="shortcut('three')">最近三个月</button></div></div>
    <div v-if="monthError" class="commission-error" role="alert">{{ monthError }}</div>
    <div v-if="error || downloadError" class="commission-error" role="alert">{{ error || downloadError }}<button class="text-button" @click="downloadError='';load()">重试</button></div>
    <div class="report-overview" :class="{'commission-stale':stale}"><div class="report-total"><span>提成合计</span><strong><small v-if="report?.total!=null">¥</small>{{ money(report?.total) }}</strong></div><div class="report-count"><strong>{{ report?.people_count ?? '—' }}</strong><span>位人员</span></div><div class="report-count"><strong>{{ report?.store_count ?? '—' }}</strong><span>家店铺</span></div><button v-if="warnings" class="report-attention" @click="state.reportView='coverage'"><span class="attention-dot"/>{{ report?.missing_periods ? `${report.missing_periods} 个月份未出金额` : '金额待核对' }} <span>查看</span></button></div>
    <div class="report-tabs-row"><div class="report-tabs" role="tablist" aria-label="汇总方式"><button v-for="item in kinds" :key="item.key" role="tab" :aria-selected="state.reportView===item.key" :class="{active:state.reportView===item.key}" @click="state.reportView=item.key;previous=null">{{ item.label }}</button></div><n-button type="primary" :disabled="!report || locked" :loading="downloading" @click="download">导出表格</n-button></div>
    <div v-if="previous && state.reportView==='breakdown'" class="report-back"><button class="text-button" @click="back">‹ 返回{{ previous.view==='people'?'人员':'店铺' }}汇总</button></div>
    <div v-if="loading" class="commission-loading-line"/>
    <div class="commission-table-wrap" :aria-busy="loading" :class="{'commission-stale':stale}"><table class="commission-table report-table"><thead><tr><th v-for="[key,label] in columns" :key="key" :data-field="key" :class="{amount:key==='amount'||key==='selected_amount'}">{{ label }}</th><th v-if="['people','stores'].includes(state.reportView)" class="sticky-action">操作</th></tr></thead><tbody>
      <tr v-for="(row,index) in rows" :key="index"><td v-for="[key] in columns" :key="key" :data-field="key" :class="{amount:key==='amount'||key==='selected_amount',negative:(key==='amount'||key==='selected_amount')&&row[key]<0}"><span v-if="key==='status'" class="report-state" :class="{review:row.status?.includes('试算')||row.status?.includes('待核对')}">{{ cell(row,key) }}</span><template v-else>{{ cell(row,key) }}<span v-if="key===columns[0][0]" class="mobile-context">{{ status(row.status) }}<template v-if="state.reportView==='breakdown'"> · {{ row.store }}</template></span></template></td><td v-if="['people','stores'].includes(state.reportView)" class="sticky-action"><button class="text-button" :disabled="locked" @click="drill(row)">查看明细</button></td></tr>
      <template v-if="!rows.length && loading"><tr v-for="i in 4" :key="i" class="commission-skeleton"><td v-for="j in columns.length" :key="j"><span/></td></tr></template>
      <tr v-else-if="!rows.length"><td :colspan="columns.length+1" class="empty">{{state.reportView==='coverage'?'这些月份还没有记录':'没有找到提成记录'}}<button v-if="state.reportView!=='coverage'" class="text-button report-empty-action" @click="state.reportView='coverage'">查看月份进度</button></td></tr>
    </tbody></table></div>
    <div class="commission-paging"><span class="row-count">共 {{report?.count || 0}} {{state.reportView==='people'?'人':state.reportView==='stores'?'家店铺':'条'}}</span><n-button size="small" :disabled="page<=1||locked" @click="page--">上一页</n-button><span>{{page}} / {{Math.max(1,Math.ceil((report?.count||0)/50))}}</span><n-button size="small" :disabled="page*50>=(report?.count||0)||locked" @click="page++">下一页</n-button></div>
  </div>
</template>
<style scoped>
.mobile-context{display:none}
.report-tabs button{border-radius:0;box-shadow:none}.month-label{white-space:nowrap}

.report-content{padding-top:0}.report-months{display:flex;align-items:center;gap:12px;min-height:76px;border-bottom:1px solid #e9edf2;flex-wrap:wrap;padding:14px 0}.month-label{font-size:13px;margin-right:4px;color:#566176}.report-months input{height:35px;width:145px;max-width:100%;border:1px solid #dce2eb;border-radius:5px;padding:0 10px;background:#fff;font-size:13px;color:#30415c}.date-separator{font-size:13px;color:#8a94a3}.month-shortcuts{display:flex;gap:18px;margin-left:12px}.report-overview{display:flex;align-items:center;gap:0;padding:26px 0 27px;min-height:129px;border-bottom:1px solid #e9edf2}.report-total{padding-right:42px;min-width:240px}.report-total>span{font-size:13px;color:#67748a}.report-total strong{display:block;margin-top:7px;font-size:33px;line-height:1.3;font-weight:650;letter-spacing:-.7px;font-variant-numeric:tabular-nums}.report-total small{font-size:25px;margin-right:3px}.report-count{border-left:1px solid #e9edf2;padding:8px 32px;display:flex;align-items:baseline;gap:9px;white-space:nowrap}.report-count strong{font-size:28px;font-weight:600}.report-count span{color:#6e7b90;font-size:13px}.report-attention{display:flex;gap:8px;align-items:center;margin-left:auto;background:transparent;border:0;padding:0;color:#b88734;font-size:12px;cursor:pointer;text-align:left}.report-attention>span:last-child{color:#3468f0;margin-left:3px}.attention-dot{width:6px;height:6px;background:#d9a13d;border-radius:50%;flex:none}.report-tabs-row{display:flex;align-items:center;justify-content:space-between;gap:16px;min-height:76px}.report-tabs{display:flex;gap:28px;align-self:stretch;min-width:0;overflow:auto}.report-tabs button{border:0;border-bottom:2px solid transparent;background:transparent;color:#768397;font-size:13px;white-space:nowrap;padding:17px 0 13px;cursor:pointer}.report-tabs button.active{color:#3468f0;border-color:#3468f0;font-weight:550}.report-table th:first-child{width:22%}.report-table td:first-child{color:#30415b;font-weight:500}.report-state{font-size:12px;color:#8490a0}.report-state.review{color:#b18741}.report-empty-action{display:block;margin:9px auto 0}.report-back{padding:0 0 13px}.report-table{min-width:780px}
@media(max-width:1180px){.report-total{min-width:200px;padding-right:24px}.report-count{padding:8px 20px}.report-overview{flex-wrap:wrap;row-gap:18px}.report-attention{margin-left:0;flex-basis:100%}.report-tabs{gap:22px}}
@media(max-width:600px){.report-months{gap:8px}.report-months input{width:calc((100% - 63px)/2);min-width:0;padding:0 5px}.month-shortcuts{margin-left:40px;margin-top:6px}.report-overview{padding:22px 0;gap:18px}.report-total{flex-basis:100%;padding:0}.report-total strong{font-size:31px}.report-count{padding:0 20px 0 0;border:0}.report-count strong{font-size:21px}.report-tabs-row{flex-wrap:wrap;padding:12px 0 16px;gap:12px}.report-tabs{gap:22px;width:100%;min-height:42px}.report-tabs button{padding:10px 0}.report-attention{font-size:12px}}
@media(max-width:600px){.report-months{display:grid;grid-template-columns:28px minmax(0,1fr) 12px minmax(0,1fr);gap:6px}.report-months input{width:100%;min-width:0;font-size:12px}.month-shortcuts{grid-column:2/-1;margin:8px 0 0;gap:20px}.report-table{min-width:0}.report-table th:first-child{width:auto}.report-table td,.report-table th{padding:12px 9px;font-size:12px}.report-table .amount{width:96px;font-size:14px}.report-table .sticky-action{width:70px}.report-table [data-field=employee_no],.report-table [data-field=stores],.report-table [data-field=people],.report-table [data-field=periods],.report-table [data-field=missing],.report-table [data-field=status],.report-table [data-field=explanation]{display:none}.report-table [data-field=store]:not(:first-child){display:none}.mobile-context{display:block;margin-top:5px;font-size:10px;color:#8a94a3;font-weight:400}.report-table [data-field=period]{width:66px;font-size:11px}.report-table .text-button{font-size:11px}}
</style>
