<script setup>
import { computed, h, onMounted, onUnmounted, ref, watch } from 'vue'
import { NDrawer, NDrawerContent, NInput, NButton, NDataTable, NPagination, NAlert } from 'naive-ui'
import { api } from '../api'
import { useLatest } from './ui/useLatest'

const props = defineProps({ runId: { type: Number, required: true }, count: { type: Number, required: true }, coverageRows: {type:Number,default:0}, coverage: { type: Object, default: () => ({}) }, observed: {type:Object,default:()=>({})}, lineSummary: {type:Object,default:()=>({})}, manualDecision: {type:Object,default:null}, storeId: String, period: String })
const emit = defineEmits(['show-quality','request-manual','line-saved','request-recompute'])
const percentage = value => value == null ? '—' : `${(Number(value) * 100).toFixed(1)}%`
const thresholdMet = computed(() => !!props.coverage?.passed)
const coverageTitle = computed(() => props.coverage?.expected
  ? `商品成本覆盖 ${percentage(props.coverage.coverage)} · 自动放行参考 ${percentage(props.coverage.threshold)}`
  : `${props.count} 条商品成本未覆盖`)
const needed = computed(() => Math.max(0, Math.ceil((props.coverage?.expected || 0) * (props.coverage?.threshold || 0)) - (props.coverage?.covered || 0)))
const uncoveredOrders = computed(() => props.coverage?.uncovered ?? props.count)
const reasonName = value => ({
  '下单日历史成本待核实':'成本尚未确认',
  '缺少已核实的下单日历史成本':'没有可用成本',
  '原订单日期待核对':'缺少下单日期',
  '取价日期与下单日不一致':'成本日期不一致',
  '成本与订单金额差异较大，需核对':'成本金额异常',
  '原订单商品信息待核对':'商品信息不一致',
}[value] || value)
const progress = ref(null), progressError = ref(''), progressRequest = useLatest()
const calculatedAt = computed(() => progress.value?.calculated_at
  ? new Date(progress.value.calculated_at).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false}) : '')
let progressTimer
async function loadProgress() {
  if (!props.storeId || !props.period) return
  try {
    const result = await progressRequest.run(signal => api.pricingStatus(props.storeId, props.period, {signal}))
    if (result) { progress.value = result.value; progressError.value = '' }
  } catch (error) { progressError.value = '暂时无法读取后台进度，请稍后刷新。' }
}
onMounted(() => { loadProgress(); progressTimer = setInterval(() => { if(document.visibilityState === 'visible')loadProgress() }, 20000) })
onUnmounted(() => { clearInterval(progressTimer); progressRequest.cancel() })
watch(() => props.runId, loadProgress)
const show = ref(false), query = ref(''), search = ref(''), page = ref(1)
const data = ref({ total: 0, reference_count: 0, reason_counts: [], items: [] }), busy = ref(false), error = ref('')
const request = useLatest()
defineExpose({ open: () => { if (props.count || props.coverageRows) show.value = true; else emit('show-quality') } })
let serial = 0
const columns = [
  { title: '平台订单号', key: 'order_id', width: 230 },
  { title: '商品编码', key: 'sku', width: 210 },
  { title: '下单日期', key: 'order_date', width: 115 },
  { title: '数量', key: 'quantity', width: 80 },
  { title: '原因', key: 'reason', minWidth: 180, render: row => reasonName(row.reason) },
  { title: '聚水潭订单号', key: 'internal_order_id', width: 130 },
]
const download = computed(() => `/api/runs/${props.runId}/pricing-gaps.csv?${new URLSearchParams({ q: search.value })}`)
const integer = value => Number(value || 0).toLocaleString('zh-CN')
async function load(preserve=false) {
  const current = ++serial
  busy.value = true; error.value = ''
  if(!preserve)data.value = { ...data.value, items: [] }
  try {
    const result = await request.run(signal => api.pricingGaps(props.runId,
      { q: search.value, offset: (page.value - 1) * 50, limit: 50 }, { signal }))
    if (result) data.value = result.value
  } catch (e) { if (current === serial) error.value = e.message }
  finally { if (current === serial) busy.value = false }
}
function submit() { search.value = query.value.trim(); page.value = 1; coveragePage.value=1; if(props.count)load(); loadCoverage() }
watch(page, () => load())
watch(show, value => { if (value) { if(props.count)load(); loadCoverage() } else { request.cancel(); ++serial; busy.value = false; coverageRequest.cancel(); editor.value=null } })
watch(() => props.runId, () => { if(show.value)load(true) })
const coveragePage=ref(1), coverageData=ref({total:0,matching:0,reviewed_count:0,supplement_total:0,items:[]})
const coverageBusy=ref(false), coverageError=ref(''), coverageRequest=useLatest()
async function loadCoverage() {
  coverageBusy.value=true;coverageError.value=''
  try {
    const response=await coverageRequest.run(signal=>api.coverageGaps(props.runId,
      {q:search.value,offset:(coveragePage.value-1)*50,limit:50},{signal}))
    if(response)coverageData.value=response.value
  } catch(error){coverageError.value=error.message}
  finally{coverageBusy.value=false}
}
watch(coveragePage,loadCoverage)
watch(()=>props.runId,()=>{editor.value=null;if(show.value){coveragePage.value=1;loadCoverage()}})
const editor=ref(null), editorAmount=ref(''), editorReason=ref(''), editorAction=ref('save')
const savingLine=ref(false), saveError=ref('')
const validCents=value=>/^-?\d+(?:\.\d{1,2})?$/.test(String(value||'').trim())
function editLine(row){
  if(!row.editable)return
  editor.value=row;editorAmount.value=row.manual_amount==null?'':Number(row.manual_amount).toFixed(2)
  editorReason.value=row.manual_reason||'';editorAction.value='save';saveError.value=''
}
async function saveLine(){
  if(!editor.value||!editorReason.value.trim()||(editorAction.value==='save'&&!validCents(editorAmount.value)))return false
  savingLine.value=true;saveError.value=''
  try{
    await api.saveCostLine(props.storeId,props.period,{
      run_id:props.runId,coverage_key:editor.value.coverage_key,context_sha:editor.value.context_sha,
      amount:editorAction.value==='save'?editorAmount.value:null,action:editorAction.value,
      reason:editorReason.value.trim(),expected_line_revision:editor.value.line_revision,
    })
    editor.value=null;await loadCoverage();emit('line-saved');return true
  }catch(error){saveError.value=error.message;return false}
  finally{savingLine.value=false}
}
const coverageColumns=[
  {title:'平台订单 / 子订单',key:'order_id',minWidth:230,render:row=>h('div',[h('strong',row.order_id||'订单号未提供'),h('small',{class:'coverage-secondary'},row.sub_order_id||'—')])},
  {title:'商品链接 / 数量',key:'product_ids',minWidth:195,render:row=>h('div',[h('span',row.product_ids||'—'),h('small',{class:'coverage-secondary'},`数量 ${row.quantities||'未提供'}`)])},
  {title:'下单日',key:'order_date',width:112},
  {title:'人工补录总成本',key:'manual_amount',width:140,render:row=>row.manual_amount==null?'—':`${Number(row.manual_amount).toFixed(2)} 元`},
  {title:'操作',key:'action',width:120,render:row=>row.editable?h(NButton,{size:'small',type:'primary',text:true,onClick:()=>editLine(row)},()=>row.manual_amount==null?'添加金额':'修改金额'):'订单归属不唯一'},
]
</script>

<template>
  <div class="pricing-notice" :class="{passed:thresholdMet || manualDecision}">
    <div><strong>{{ manualDecision ? '本期成本已由人工确认' : coverageTitle }}</strong><p v-if="manualDecision">原始覆盖率 {{ percentage(coverage.coverage) }}，人工确认金额与原因已和结账记录一起冻结。</p><p v-else-if="thresholdMet">还有 {{ integer(uncoveredOrders) }} 笔订单未覆盖商品成本，当前只计入已识别金额。</p><p v-else>还有 {{ integer(uncoveredOrders) }} 笔订单未覆盖；系统已完成现有资料的计算，人工可确认或修改金额后结账。自动放行还差 {{ integer(needed) }} 笔覆盖。</p>
      <p v-if="observed && Object.hasOwn(observed,'goods') && !manualDecision" class="pricing-help">现有资料识别：商品成本 {{ Number(observed.goods).toLocaleString('zh-CN',{minimumFractionDigits:2}) }} 元 · 代发 {{ Number(observed.dropship).toLocaleString('zh-CN',{minimumFractionDigits:2}) }} 元 · 补发 {{ Number(observed.reshipment).toLocaleString('zh-CN',{minimumFractionDigits:2}) }} 元<template v-if="lineSummary?.line_count"> · 已人工补录 {{ integer(lineSummary.line_count) }} 笔、{{ Number(lineSummary.line_total).toLocaleString('zh-CN',{minimumFractionDigits:2}) }} 元（预览时另计）</template></p>
      <p v-if="progress" aria-live="polite">{{ progress.message }}</p>
      <div v-if="['running','queued'].includes(progress?.state)" class="pricing-work-bar" role="progressbar" :aria-valuenow="progress.percent ?? 0" aria-valuemin="0" aria-valuemax="100" aria-label="本店后台核算进度"><span :style="{width:`${progress.percent ?? 0}%`}" /></div>
      <p v-if="calculatedAt" class="pricing-help">最近核算（北京时间）：{{ calculatedAt }}。上方数量属于已保存的核算结果。</p>
      <p v-if="progressError" role="status">{{ progressError }}</p>
    </div>
    <div class="pricing-actions"><n-button v-if="count || coverageRows" size="small" @click="show = true">查看全部 {{ integer(coverageRows || uncoveredOrders) }} 笔缺口</n-button><n-button v-else size="small" @click="emit('show-quality')">查看成本覆盖</n-button><n-button v-if="!manualDecision" size="small" type="primary" @click="emit('request-manual')">人工确认成本</n-button></div>
  </div>
  <n-drawer v-model:show="show" :width="920" style="max-width: 100vw">
    <n-drawer-content title="未覆盖订单成本" closable>
      <p class="pricing-help">按本店本月应有商品成本的订单键列出全部缺口。可给唯一对应的子订单补录总成本金额；原始文件与人工修改历史都保留。</p>
      <div class="coverage-summary"><span>缺口 <b>{{ integer(coverageData.total || coverageRows || uncoveredOrders) }}</b> 笔</span><span>已补录 <b>{{ integer(coverageData.reviewed_count) }}</b> 笔</span><span>人工金额 <b>{{ Number(coverageData.supplement_total || 0).toLocaleString('zh-CN',{minimumFractionDigits:2}) }}</b> 元</span></div>
      <form class="pricing-search" @submit.prevent="submit">
        <n-input v-model:value="query" clearable placeholder="搜索订单号、子订单号或商品链接" aria-label="搜索未覆盖成本明细" />
        <n-button attr-type="submit" :loading="busy">搜索</n-button>
        <a v-if="count" :href="download" download>导出来源异常行</a>
        <a :href="`/api/runs/${runId}/coverage-gaps.csv`" download>导出全部订单缺口</a>
      </form>
      <n-alert v-if="coverageError" type="warning" style="margin-bottom:12px">{{ coverageError }} <n-button size="small" @click="emit('request-recompute')">重算后显示全部缺口</n-button></n-alert>
      <n-data-table class="pricing-desktop" :columns="coverageColumns" :data="coverageData.items" :loading="coverageBusy" :scroll-x="870" :max-height="480" size="small" />
      <div class="pricing-mobile" :aria-busy="coverageBusy">
        <p v-if="coverageBusy">正在加载订单缺口…</p>
        <article v-for="row in coverageData.items" :key="row.coverage_key">
          <strong>{{ row.order_id || '订单号未提供' }}</strong><span>{{ row.order_date || '日期未提供' }}</span>
          <p>子订单 {{ row.sub_order_id || '—' }} · 商品链接 {{ row.product_ids || '—' }} · 数量 {{ row.quantities || '未提供' }}</p>
          <p>人工总成本 {{ row.manual_amount == null ? '尚未补录' : `${Number(row.manual_amount).toFixed(2)} 元` }}</p>
          <n-button v-if="row.editable" size="small" @click="editLine(row)">{{ row.manual_amount == null ? '添加金额' : '修改金额' }}</n-button><span v-else class="pricing-reason">此键对应多个订单或缺下单日，不能直接认定金额</span>
        </article>
      </div>
      <div class="pricing-pages"><span>共 {{ coverageData.matching ?? coverageData.total ?? coverageRows }} 笔</span><n-pagination v-model:page="coveragePage" :item-count="coverageData.matching" :page-size="50" :disabled="coverageBusy" simple /></div>
      <details v-if="count" class="source-gap-details"><summary>查看 {{ integer(count) }} 条来源缺价记录与参考单价</summary>
        <p class="pricing-help">这是聚水潭等来源行的逐条异常；覆盖缺口以完整订单清单为准，来源行不会重复补录金额。</p>
        <div v-if="data.reason_counts?.length" class="pricing-summary" aria-label="未覆盖原因汇总"><span v-for="item in data.reason_counts" :key="item.reason"><b>{{ integer(item.count) }}</b>{{ reasonName(item.reason) }}</span></div>
      <n-alert v-if="error" type="error" style="margin-bottom: 16px">{{ error }} <n-button size="small" @click="load()">重试</n-button></n-alert>
      <n-data-table class="pricing-desktop" :columns="columns" :data="data.items" :loading="busy" :scroll-x="995" :max-height="560" size="small" />
      <div class="pricing-mobile" :aria-busy="busy">
        <p v-if="busy">正在加载…</p>
        <p v-else-if="!data.items.length && !error">没有找到对应记录</p>
        <article v-for="(row, index) in data.items" :key="index">
          <strong>{{ row.sku || '未提供商品编码' }}</strong><span>数量 {{ row.quantity ?? '未提供' }}</span>
          <p>下单日期 {{ row.order_date || '未提供' }}</p>
          <p>平台订单 {{ row.order_id || '未提供' }}</p>
          <p>聚水潭订单 {{ row.internal_order_id || '—' }}</p>
          <p class="pricing-reason">{{ reasonName(row.reason) }}</p>
        </article>
      </div>
      <div class="pricing-pages"><span>共 {{ data.total }} 条</span><n-pagination v-model:page="page" :item-count="data.total" :page-size="50" :disabled="busy" simple /></div>
      </details>
    </n-drawer-content>
  </n-drawer>
  <n-modal :show="!!editor" preset="dialog" title="人工补录订单总成本" positive-text="保存金额" negative-text="取消" :positive-button-props="{disabled: !editorReason.trim() || (editorAction==='save' && !validCents(editorAmount))}" @update:show="!$event && (editor=null)" @positive-click="saveLine">
    <p class="small muted">平台订单 {{ editor?.order_id }} · 子订单 {{ editor?.sub_order_id }} · {{ editor?.order_date }}。金额按本子订单所有商品合计填写。</p>
    <n-input v-if="editorAction==='save'" v-model:value="editorAmount" inputmode="decimal" aria-label="人工总成本金额" placeholder="输入本子订单总成本" />
    <n-alert v-else type="warning" :bordered="false">将撤销此前的人工补录金额。旧记录仍保留。</n-alert>
    <n-input v-model:value="editorReason" type="textarea" :rows="3" maxlength="500" show-count placeholder="填写来源和确认依据" style="margin-top:12px" />
    <n-button v-if="editor?.manual_amount != null" size="small" text style="margin-top:10px" @click="editorAction=editorAction==='remove'?'save':'remove'">{{ editorAction==='remove'?'继续修改金额':'撤销此前补录' }}</n-button>
    <n-alert v-if="saveError" type="error" style="margin-top:12px">{{ saveError }}</n-alert>
  </n-modal>
</template>

<style scoped>
.pricing-notice { display: flex; align-items: center; gap: 16px; justify-content: space-between; padding: 16px; margin-bottom: 16px; background: #fff8e8; border: 1px solid #f0dcb0; border-radius: 8px; color: #715020; }
.pricing-notice.passed { background:#edf8f2;border-color:#bee2ce;color:#17623f; }
.pricing-actions { display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end; }
.pricing-work-bar { height:5px; max-width:320px; margin-top:8px; overflow:hidden; border-radius:999px; background:#ebeff6; }
.pricing-work-bar span { display:block;height:100%;border-radius:inherit;background:#4783f4;transition:width .2s ease; }
.pricing-notice p { margin: 4px 0 0; font-size: 13px; }
.pricing-help { color: #657184; margin: 0 0 16px; }
.pricing-search { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }
.pricing-summary { display:flex;flex-wrap:wrap;gap:8px;margin:-4px 0 14px; }
.pricing-summary span { display:inline-flex;gap:5px;align-items:center;padding:6px 9px;border-radius:6px;background:#f5f7fa;color:#59667a;font-size:12px; }
.pricing-summary b { color:#26364d;font-variant-numeric:tabular-nums; }
.coverage-summary { display:flex;gap:9px;flex-wrap:wrap;margin-bottom:13px; }
.coverage-summary span {padding:7px 10px;border:1px solid #e4eaf3;border-radius:7px;background:#f7f9fc;color:#657184;font-size:12px; }
.coverage-summary b {font-variant-numeric:tabular-nums;color:#273b59;font-size:13px;}
.coverage-secondary { display:block;color:#7a879b;font-size:11px;margin-top:4px;white-space:normal;word-break:break-all; }
.source-gap-details {border-top:1px solid #e8edf4;margin-top:17px;padding-top:14px;}
.source-gap-details summary {cursor:pointer;color:#3c67c5;font-size:13px;margin-bottom:12px;}
.pricing-search a { white-space: nowrap; }
.pricing-pages { display: flex; justify-content: space-between; align-items: center; margin-top: 16px; color: #657184; }
.pricing-mobile { display: none; }
@media (max-width: 600px) {
  .pricing-notice { align-items: flex-start; flex-direction: column; }
  .pricing-desktop { display: none; }
  .pricing-mobile { display: block; }
  .pricing-mobile article { border: 1px solid #e1e6ed; border-radius: 6px; padding: 12px; margin-top: 10px; overflow-wrap: anywhere; }
  .pricing-mobile strong { font-size: 14px; }
  .pricing-mobile article > span { float: right; color: #657184; }
  .pricing-mobile article p { margin: 8px 0 0; font-size: 12px; }
  .pricing-mobile .pricing-reason { color: #8a621f; background: #fff8e8; padding: 8px; border-radius: 4px; }
}
</style>
