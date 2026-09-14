<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { NDrawer, NDrawerContent, NInput, NButton, NDataTable, NPagination, NAlert } from 'naive-ui'
import { api } from '../api'
import { useLatest } from './ui/useLatest'

const props = defineProps({ runId: { type: Number, required: true }, count: { type: Number, required: true }, storeId: String, period: String })
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
defineExpose({ open: () => { show.value = true } })
let serial = 0
const columns = [
  { title: '平台订单号', key: 'order_id', width: 230 },
  { title: '商品编码', key: 'sku', width: 210 },
  { title: '下单日期', key: 'order_date', width: 115 },
  { title: '数量', key: 'quantity', width: 80 },
  { title: '待核对事项', key: 'reason', minWidth: 230 },
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
function submit() { search.value = query.value.trim(); page.value = 1; load() }
watch(page, () => load())
watch(show, value => { if (value) load(); else { request.cancel(); ++serial; busy.value = false } })
watch(() => props.runId, () => { if(show.value)load(true) })
</script>

<template>
  <div class="pricing-notice">
    <div><strong>{{ count }} 条商品成本待核价</strong><p>还有商品成本未核实，相关利润和提成暂不能确认。</p>
      <p v-if="progress" aria-live="polite">{{ progress.message }}</p>
      <p v-if="calculatedAt" class="pricing-help">最近核算（北京时间）：{{ calculatedAt }}。上方数量属于已保存的核算结果。</p>
      <p v-if="progress?.error" class="pricing-help">后台原因：{{ progress.error }}</p>
      <p v-if="progressError" role="status">{{ progressError }}</p>
    </div>
    <n-button size="small" @click="show = true">查看待核价明细</n-button>
  </div>
  <n-drawer v-model:show="show" :width="920" style="max-width: 100vw">
    <n-drawer-content title="待核价明细" closable>
      <p class="pricing-help">请按下单日期核实清单中的价格。已有历史价格的记录随同步更新；缺少历史价格的记录需要另行核价，参考单价不直接计入成本。</p>
      <div v-if="data.reason_counts?.length" class="pricing-summary" aria-label="待核价原因汇总">
        <span v-for="item in data.reason_counts" :key="item.reason"><b>{{ integer(item.count) }}</b>{{ item.reason }}</span>
      </div>
      <p v-if="data.reference_count" class="pricing-help">其中 {{ integer(data.reference_count) }} 条带有参考单价；参考价只用于核对，未取得下单日历史证据前不会计入利润。</p>
      <form class="pricing-search" @submit.prevent="submit">
        <n-input v-model:value="query" clearable placeholder="搜索订单号、商品编码" aria-label="搜索待核价明细" />
        <n-button attr-type="submit" :loading="busy">搜索</n-button>
        <a :href="download" download>导出明细</a>
      </form>
      <n-alert v-if="error" type="error" style="margin-bottom: 16px">{{ error }} <n-button size="small" @click="load()">重试</n-button></n-alert>
      <n-data-table class="pricing-desktop" :columns="columns" :data="data.items" :loading="busy" :scroll-x="995" :max-height="560" size="small" />
      <div class="pricing-mobile" :aria-busy="busy">
        <p v-if="busy">正在加载…</p>
        <p v-else-if="!data.items.length && !error">没有找到对应记录</p>
        <article v-for="(row, index) in data.items" :key="index">
          <strong>{{ row.sku || '商品编码待核对' }}</strong><span>数量 {{ row.quantity ?? '待核对' }}</span>
          <p>下单日期 {{ row.order_date || '待核对' }}</p>
          <p>平台订单 {{ row.order_id || '待核对' }}</p>
          <p>聚水潭订单 {{ row.internal_order_id || '—' }}</p>
          <p class="pricing-reason">{{ row.reason }}</p>
        </article>
      </div>
      <div class="pricing-pages"><span>共 {{ data.total }} 条</span><n-pagination v-model:page="page" :item-count="data.total" :page-size="50" :disabled="busy" simple /></div>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped>
.pricing-notice { display: flex; align-items: center; gap: 16px; justify-content: space-between; padding: 16px; margin-bottom: 16px; background: #fff8e8; border: 1px solid #f0dcb0; border-radius: 8px; color: #715020; }
.pricing-notice p { margin: 4px 0 0; font-size: 13px; }
.pricing-help { color: #657184; margin: 0 0 16px; }
.pricing-search { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }
.pricing-summary { display:flex;flex-wrap:wrap;gap:8px;margin:-4px 0 14px; }
.pricing-summary span { display:inline-flex;gap:5px;align-items:center;padding:6px 9px;border-radius:6px;background:#f5f7fa;color:#59667a;font-size:12px; }
.pricing-summary b { color:#26364d;font-variant-numeric:tabular-nums; }
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
