<script setup>
import { computed, h, onMounted, onUnmounted, ref, watch } from 'vue'
import { NDrawer, NDrawerContent, NInput, NButton, NDataTable, NPagination, NAlert } from 'naive-ui'
import { api } from '../api'
import { useLatest } from './ui/useLatest'
import { costCoverageView } from '../costCoverageView'

const props = defineProps({ runId: { type: Number, required: true }, count: { type: Number, required: true }, coverageRows: {type:Number,default:0}, coverage: { type: Object, default: () => ({}) }, posting: {type:Object,default:()=>({})}, observed: {type:Object,default:()=>({})}, lineSummary: {type:Object,default:()=>({})}, manualDecision: {type:Object,default:null}, closed: Boolean, storeId: String, period: String })
const overview = ref(false)
const postedCount = computed(() => props.closed && props.manualDecision?.line_count != null
  ? props.manualDecision.line_count
  : props.lineSummary?.posted_line_count ?? props.posting?.count)
const summary = computed(() => costCoverageView(props.coverage, props.coverageRows, props.count, props.closed,
  postedCount.value))
function openGaps() { overview.value=false; show.value=true }
function requestManual() { overview.value=false; show.value=false; emit('request-manual') }
const emit = defineEmits(['show-quality','request-manual','line-saved','request-recompute'])
const percentage = value => value == null ? '—' : `${(Number(value) * 100).toFixed(1)}%`
const thresholdMet = computed(() => !!props.coverage?.passed)
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
defineExpose({ open: () => { if (props.count || props.coverageRows) openGaps(); else overview.value=true } })
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
const coveragePage=ref(1), coverageData=ref({total:null,matching:null,reviewed_count:0,supplement_total:0,items:[]})
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
watch(()=>props.runId,()=>{editor.value=null;batchFile.value=null;batchPreview.value=null;batchError.value='';if(show.value){coveragePage.value=1;loadCoverage()}})
const editor=ref(null), editorAmount=ref(''), editorReason=ref(''), editorAction=ref('save')
const savingLine=ref(false), saveError=ref('')
const batchFile=ref(null), batchReason=ref(''), batchPreview=ref(null)
const batchBusy=ref(false), batchError=ref(''), batchResult=ref('')
const batchInput=ref(null)
watch(batchReason,()=>{batchPreview.value=null})
function chooseBatch(event){batchFile.value=event.target.files?.[0]||null;batchPreview.value=null;batchError.value='';batchResult.value=''}
async function previewBatch(){
  if(props.closed||!batchFile.value||!props.storeId||!props.period)return
  batchBusy.value=true;batchError.value='';batchResult.value=''
  try{batchPreview.value=await api.previewCostBatch(props.storeId,props.period,props.runId,batchFile.value,batchReason.value)}
  catch(error){batchPreview.value=null;batchError.value=error.message}
  finally{batchBusy.value=false}
}
async function applyBatch(){
  if(props.closed||!batchFile.value||!batchPreview.value?.valid||batchPreview.value?.issue_count||batchBusy.value)return
  batchBusy.value=true;batchError.value=''
  try{
    const saved=await api.applyCostBatch(props.storeId,props.period,props.runId,batchFile.value,batchReason.value,batchPreview.value)
    batchResult.value=`已批量保存 ${saved.saved} 笔、${Number(saved.amount_total).toFixed(2)} 元，已安排自动核算更新看板`
    batchPreview.value=null;batchFile.value=null
    if(batchInput.value)batchInput.value.value=''
    await loadCoverage();emit('line-saved')
  }catch(error){batchError.value=error.message}
  finally{batchBusy.value=false}
}
const validCents=value=>/^-?\d+(?:\.\d{1,2})?$/.test(String(value||'').trim())
function editLine(row){
  if(props.closed || !row.editable)return
  editor.value=row;editorAmount.value=row.manual_amount==null?'':Number(row.manual_amount).toFixed(2)
  editorReason.value=row.manual_reason||'';editorAction.value='save';saveError.value=''
}
async function saveLine(){
  if(props.closed||!editor.value||!editorReason.value.trim()||(editorAction.value==='save'&&!validCents(editorAmount.value)))return false
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
  {title:'核对说明',key:'brushing_review',minWidth:280,render:row=>row.coverage_reason||row.brushing_review||'商品成本未覆盖，需核对来源成本'},
  {title:'人工补录总成本',key:'manual_amount',width:140,render:row=>row.manual_amount==null?'—':`${Number(row.manual_amount).toFixed(2)} 元`},
  {title:'操作',key:'action',width:140,render:row=>props.closed?'已结账 · 只读':row.partial_cost?'请核对缺失商品':row.editable?h(NButton,{size:'small',type:'primary',text:true,onClick:()=>editLine(row)},()=>row.manual_amount==null?'添加金额':'修改金额'):'订单归属不唯一'},
]
</script>

<template>
  <section class="cost-summary" aria-label="成本覆盖摘要">
    <div class="cost-summary-heading"><strong>{{ summary.title }}</strong><span v-if="closed" class="cost-scope">已结账 · 只读</span><span v-else-if="lineSummary?.line_posting_pending" class="cost-scope">补录待计入</span></div>
    <div class="cost-summary-main">
      <div class="cost-stat"><span>补录后覆盖率</span><b :class="{attention:!summary.afterKnown}">{{ summary.afterPercentage }}</b></div>
      <div class="cost-stat"><span>补录后剩余缺口</span><b :class="{attention:!summary.afterKnown || summary.afterGaps > 0}">{{ summary.afterGapLabel }}</b></div>
      <div class="cost-stat cost-stat-secondary"><span>原始覆盖率</span><b>{{ summary.percentage }}</b></div>
      <div class="cost-stat cost-stat-secondary"><span>原始成本缺口</span><b>{{ summary.gapLabel }}</b></div>
      <n-button class="cost-detail-button" size="small" @click="overview=true">成本详情</n-button>
    </div>
    <p>{{ summary.description }}</p>
    <p v-if="lineSummary?.line_posting_pending">最新补录待核算；补录后指标仍按本次已计入的记录统计。</p>
  </section>
  <n-drawer v-model:show="overview" :width="600" :z-index="3950" style="max-width:100vw">
    <n-drawer-content title="成本覆盖与核算详情" closable>
      <p class="pricing-help">{{ period }} · {{ summary.title }}。覆盖率说明订单是否有成本依据，不代表全部资料已齐或已满足结账条件。</p>
      <p v-if="closed && manualDecision" class="pricing-help">已确认金额：商品成本 {{ Number(manualDecision.confirmed?.goods || 0).toFixed(2) }} 元 · 代发 {{ Number(manualDecision.confirmed?.dropship || 0).toFixed(2) }} 元 · 补发 {{ Number(manualDecision.confirmed?.reshipment || 0).toFixed(2) }} 元。确认依据：{{ manualDecision.reason }}</p>
      <dl class="cost-details">
        <div><dt>补录后覆盖率</dt><dd>{{ summary.afterPercentage }}</dd></div>
        <div><dt>补录后已覆盖 / 应覆盖订单</dt><dd>{{ summary.afterKnown ? `${integer(summary.afterCovered)} / ${integer(coverage.expected)} 笔` : '尚未形成完整统计' }}</dd></div>
        <div><dt>{{ closed ? '结账时已计入人工补录' : '本次核算已计入人工补录' }}</dt><dd>{{ summary.afterKnown ? `${integer(summary.postedCount)} 笔` : '未统计' }}</dd></div>
        <div><dt>补录后剩余缺口</dt><dd>{{ summary.afterGapLabel }}</dd></div>
        <div><dt>原始已覆盖 / 应覆盖订单</dt><dd>{{ summary.known ? `${integer(coverage.covered)} / ${integer(coverage.expected)} 笔` : '尚未形成统计范围' }}</dd></div>
        <div><dt>原始覆盖率</dt><dd>{{ summary.percentage }}</dd></div>
        <div><dt>{{ closed ? '结账时原始缺口' : '原始成本缺口' }}</dt><dd>{{ summary.gapLabel }}</dd></div>
        <div><dt>来源成本待核对</dt><dd>{{ integer(count) }} 条</dd></div>
      </dl>
      <p class="pricing-help">补录后覆盖率＝（原始已覆盖＋本次核算已计入人工补录的缺口订单）÷应覆盖订单。尚未计入的补录不参与统计；整期人工确认金额不会直接视为订单全部覆盖。</p>
      <p v-if="!summary.afterKnown" class="pricing-help">当前核算未提供可核对的补录覆盖笔数，暂不推算补录后覆盖率。</p>
      <div class="pricing-notice" :class="{passed:!summary.attention}">
    <div><strong>{{ manualDecision ? '本期成本已由人工确认' : summary.title }}</strong><p>{{ summary.description }}</p><p v-if="manualDecision">人工确认金额与原因已和结账记录一起保存，原始缺口仍保留供回查。</p><p v-else-if="summary.known && !closed">原始成本覆盖自动放行参考 {{ percentage(coverage.threshold) }}；{{ thresholdMet ? '原始覆盖率已达到参考值，但仍需核对其他结账条件。' : `原始覆盖还差 ${integer(needed)} 笔达到参考值。` }}</p>
      <p v-if="observed && Object.hasOwn(observed,'goods') && !manualDecision" class="pricing-help">来源表成本：商品 {{ Number(observed.goods).toLocaleString('zh-CN',{minimumFractionDigits:2}) }} 元 · 代发 {{ Number(observed.dropship).toLocaleString('zh-CN',{minimumFractionDigits:2}) }} 元 · 补发 {{ Number(observed.reshipment).toLocaleString('zh-CN',{minimumFractionDigits:2}) }} 元<template v-if="lineSummary?.line_count"> · 已人工补录 {{ integer(lineSummary.line_count) }} 笔、{{ Number(lineSummary.line_total).toLocaleString('zh-CN',{minimumFractionDigits:2}) }} 元；本次看板已计入补录 {{ Number(lineSummary.posted_line_total || 0).toLocaleString('zh-CN',{minimumFractionDigits:2}) }} 元</template><template v-if="lineSummary?.line_posting_pending">；最新补录待自动核算，当前看板尚未更新。</template></p>
      <p v-if="coverage.brushing_zero_covered" class="pricing-help">其中 {{ integer(coverage.brushing_zero_covered) }} 笔已按“蓝色旗帜＋by”刷单规则确认商品成本为 0，不需要补成本，也不在缺口清单中。</p>
      <p v-if="progress" aria-live="polite">{{ progress.message }}</p>
      <div v-if="['running','queued'].includes(progress?.state)" class="pricing-work-bar" role="progressbar" :aria-valuenow="progress.percent ?? 0" aria-valuemin="0" aria-valuemax="100" aria-label="本店后台核算进度"><span :style="{width:`${progress.percent ?? 0}%`}" /></div>
      <p v-if="calculatedAt" class="pricing-help">最近核算（北京时间）：{{ calculatedAt }}。上方数量属于已保存的核算结果。</p>
      <p v-if="progressError" role="status">{{ progressError }}</p>
    </div>
      </div>
      <p v-if="closed" class="pricing-help">仅回查结账时的依据，不需要在此重复补录。若要更正历史账，请按反结账流程处理。</p>
      <template #footer><div class="pricing-actions"><n-button v-if="count || coverageRows || summary.gaps" @click="openGaps">{{ closed ? '回查原始缺口与来源' : summary.gaps ? '查看缺口与补录记录' : '核对来源成本记录' }}</n-button><n-button v-if="!closed && !manualDecision" @click="requestManual">人工确认成本</n-button><n-button @click="overview=false">关闭</n-button></div></template>
    </n-drawer-content>
  </n-drawer>
  <n-drawer v-model:show="show" :width="920" :z-index="3950" style="max-width: 100vw">
    <n-drawer-content :title="closed ? '结账时原始成本缺口（只读）' : '订单成本缺口与补录记录'" closable>
      <p class="pricing-help">{{ closed ? '以下为结账时保留的原始缺口，仅供回查，不代表需要再次补录。' : '列出尚无成本及部分商品成本未计全的订单。部分成本已计入的订单须先核对缺失商品，不能在此重复补录整单成本；已补录记录单独保留。' }}</p>
      <div class="coverage-summary"><span>原始缺口 <b>{{ coverageBusy ? '加载中' : integer(coverageData.total ?? summary.gaps) }}</b> 笔</span><span>已补录 <b>{{ coverageBusy ? '—' : integer(coverageData.reviewed_count) }}</b> 笔</span><span>人工金额 <b>{{ coverageBusy ? '—' : Number(coverageData.supplement_total || 0).toLocaleString('zh-CN',{minimumFractionDigits:2}) }}</b> 元</span></div>
      <form class="pricing-search" @submit.prevent="submit">
        <n-input v-model:value="query" clearable placeholder="搜索订单号、子订单号或商品链接" aria-label="搜索未覆盖成本明细" />
        <n-button attr-type="submit" :loading="busy">搜索</n-button>
        <a v-if="count" :href="download" download>导出来源异常行</a>
        <a :href="`/api/runs/${runId}/coverage-gaps.xlsx`" download>导出全部订单缺口（Excel）</a>
      </form>
      <div v-if="!closed && summary.gaps" class="pricing-batch">
        <strong>批量补录成本</strong>
        <p>下载 Excel 模板，仅对“可直接补录”的行填写“人工补录总成本”；部分商品未计全的行需先核对来源明细。订单号和核对列已固定为文本。旧 CSV 仍可上传；可填写统一依据，逐行依据优先。</p>
        <div class="pricing-batch-actions">
          <input ref="batchInput" type="file" accept=".xlsx,.csv" aria-label="上传批量成本Excel或CSV" @change="chooseBatch" />
          <n-input v-model:value="batchReason" aria-label="批量成本统一依据" placeholder="统一确认依据（可选）" style="max-width:260px" />
          <n-button size="small" :loading="batchBusy" :disabled="!batchFile || batchBusy" @click="previewBatch">预览批量金额</n-button>
        </div>
        <p v-if="batchPreview" class="pricing-help">可保存 {{ integer(batchPreview.valid) }} 笔、{{ Number(batchPreview.amount_total).toFixed(2) }} 元；{{ integer(batchPreview.skipped) }} 行未填写或未变化。<template v-if="batchPreview.issue_count">还有 {{ integer(batchPreview.issue_count) }} 行需修改。</template></p>
        <n-alert v-if="batchPreview?.issue_count" type="warning" :bordered="false"><p v-for="issue in batchPreview.issues" :key="issue">{{ issue }}</p></n-alert>
        <n-alert v-if="batchError" type="error" :bordered="false">{{ batchError }}</n-alert>
        <n-alert v-if="batchResult" type="success" :bordered="false">{{ batchResult }}</n-alert>
        <n-button v-if="batchResult" size="small" text type="primary" @click="emit('request-manual')">确认本月成本与提成并结账 →</n-button>
        <n-button v-if="batchPreview?.valid" size="small" type="primary" :loading="batchBusy" :disabled="batchBusy || !!batchPreview.issue_count" @click="applyBatch">确认批量补录</n-button>
      </div>
      <n-alert v-if="coverageError" type="warning" style="margin-bottom:12px">{{ coverageError }} <n-button size="small" @click="emit('request-recompute')">重算后显示全部缺口</n-button></n-alert>
      <n-data-table class="pricing-desktop" :columns="coverageColumns" :data="coverageData.items" :loading="coverageBusy" :scroll-x="1110" :max-height="480" size="small" />
      <div class="pricing-mobile" :aria-busy="coverageBusy">
        <p v-if="coverageBusy">正在加载订单缺口…</p>
        <article v-for="row in coverageData.items" :key="row.coverage_key">
          <strong>{{ row.order_id || '订单号未提供' }}</strong><span>{{ row.order_date || '日期未提供' }}</span>
          <p>子订单 {{ row.sub_order_id || '—' }} · 商品链接 {{ row.product_ids || '—' }} · 数量 {{ row.quantities || '未提供' }}</p>
          <p>人工总成本 {{ row.manual_amount == null ? '尚未补录' : `${Number(row.manual_amount).toFixed(2)} 元` }}</p>
          <p v-if="row.coverage_reason" class="pricing-reason">{{ row.coverage_reason }}</p>
          <p v-if="row.brushing_review" class="pricing-reason">{{ row.brushing_review }}</p>
          <span v-if="closed" class="pricing-reason">已结账 · 只读</span><span v-else-if="row.partial_cost" class="pricing-reason">请核对缺失商品</span><n-button v-else-if="row.editable" size="small" @click="editLine(row)">{{ row.manual_amount == null ? '添加金额' : '修改金额' }}</n-button><span v-else class="pricing-reason">此键对应多个订单，需先确认归属</span>
        </article>
      </div>
      <div class="pricing-pages"><span>{{ coverageBusy ? '正在加载订单记录…' : `共 ${coverageData.matching ?? coverageData.total ?? summary.gaps} 笔` }}</span><n-pagination v-model:page="coveragePage" :item-count="coverageData.matching || 0" :page-size="50" :disabled="coverageBusy" simple /></div>
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
  <n-modal :show="!!editor" :z-index="3960" preset="dialog" title="人工补录订单总成本" positive-text="保存金额" negative-text="取消" :positive-button-props="{disabled: !editorReason.trim() || (editorAction==='save' && !validCents(editorAmount))}" @update:show="!$event && (editor=null)" @positive-click="saveLine">
    <p class="small muted">平台订单 {{ editor?.order_id }} · 子订单 {{ editor?.sub_order_id }} · {{ editor?.order_date || `本店${period}账期，原下单日未提供` }}。金额按本子订单所有商品合计填写；没有原下单日时请在依据中说明月份归属。</p>
    <n-input v-if="editorAction==='save'" v-model:value="editorAmount" inputmode="decimal" aria-label="人工总成本金额" placeholder="输入本子订单总成本" />
    <n-alert v-else type="warning" :bordered="false">将撤销此前的人工补录金额。旧记录仍保留。</n-alert>
    <n-input v-model:value="editorReason" type="textarea" :rows="3" maxlength="500" show-count placeholder="填写来源和确认依据" style="margin-top:12px" />
    <n-button v-if="editor?.manual_amount != null" size="small" text style="margin-top:10px" @click="editorAction=editorAction==='remove'?'save':'remove'">{{ editorAction==='remove'?'继续修改金额':'撤销此前补录' }}</n-button>
    <n-alert v-if="saveError" type="error" style="margin-top:12px">{{ saveError }}</n-alert>
  </n-modal>
</template>

<style scoped>
.cost-summary{padding:16px 0 18px;margin-bottom:16px;border-bottom:1px solid var(--line,#e2e5eb)}
.cost-summary-heading{display:flex;align-items:center;gap:12px}.cost-summary-heading strong{font-size:14px}.cost-scope{font-size:12px;color:#657184}
.cost-summary-main{display:flex;flex-wrap:wrap;gap:20px 32px;align-items:center;margin-top:12px}.cost-stat{display:flex;flex-direction:column;gap:4px}.cost-stat>span{font-size:12px;color:#657184}.cost-stat b{font-size:24px;line-height:1.3;font-variant-numeric:tabular-nums;color:#17623f}.cost-stat b.attention{color:#8a4a0b}.cost-stat-secondary b{font-size:20px;color:#526477}.cost-detail-button{margin-left:auto}.cost-summary>p{margin:10px 0 0;font-size:12px;line-height:1.7;color:#657184}
.cost-details{margin:20px 0}.cost-details>div{display:flex;justify-content:space-between;gap:16px;padding:12px 0;border-bottom:1px solid #e2e5eb}.cost-details dt{color:#657184}.cost-details dd{margin:0;text-align:right;font-variant-numeric:tabular-nums}
.pricing-batch{border:1px solid #dce6f2;background:#f8fbff;border-radius:8px;padding:14px;margin:12px 0 16px}.pricing-batch>strong{font-size:14px}.pricing-batch>p{font-size:12px;color:#62728b;margin:8px 0}.pricing-batch-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.pricing-batch-actions input[type=file]{max-width:280px;font-size:12px}.pricing-batch .n-alert{margin:10px 0}.pricing-batch .n-alert p{margin:3px 0}
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
:deep(.coverage-secondary) { display:block;color:#7a879b;font-size:11px;margin-top:4px;white-space:normal;word-break:break-all; }
.source-gap-details {border-top:1px solid #e8edf4;margin-top:17px;padding-top:14px;}
.source-gap-details summary {cursor:pointer;color:#3c67c5;font-size:13px;margin-bottom:12px;}
.pricing-search a { white-space: nowrap; }
.pricing-pages { display: flex; justify-content: space-between; align-items: center; margin-top: 16px; color: #657184; }
.pricing-mobile { display: none; }
@media (max-width: 600px) {
  .cost-summary-main{gap:16px}.cost-stat{flex:1 1 calc(50% - 16px);min-width:0}.cost-detail-button{margin-left:0}.cost-summary :deep(button),.pricing-actions :deep(button){min-height:44px}.pricing-search{flex-wrap:wrap}.pricing-search :deep(.n-input){flex-basis:100%}.pricing-pages{gap:8px;flex-wrap:wrap}
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
