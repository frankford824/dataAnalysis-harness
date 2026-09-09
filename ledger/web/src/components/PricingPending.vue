<script setup>
import { computed, ref, watch } from 'vue'
import { NDrawer, NDrawerContent, NInput, NButton, NDataTable, NPagination, NAlert } from 'naive-ui'
import { api } from '../api'
import { useLatest } from './ui/useLatest'

const props = defineProps({ runId: { type: Number, required: true }, count: { type: Number, required: true } })
const show = ref(false), query = ref(''), search = ref(''), page = ref(1)
const data = ref({ total: 0, items: [] }), busy = ref(false), error = ref('')
const request = useLatest()
defineExpose({ open: () => { show.value = true } })
let serial = 0
const columns = [
  { title: '平台订单号', key: 'order_id', width: 230 },
  { title: '商品编码', key: 'sku', width: 210 },
  { title: '数量', key: 'quantity', width: 80 },
  { title: '待核对事项', key: 'reason', minWidth: 230 },
  { title: '聚水潭订单号', key: 'internal_order_id', width: 130 },
]
const download = computed(() => `/api/runs/${props.runId}/pricing-gaps.csv?${new URLSearchParams({ q: search.value })}`)
async function load() {
  const current = ++serial
  busy.value = true; error.value = ''; data.value = { ...data.value, items: [] }
  try {
    const result = await request.run(signal => api.pricingGaps(props.runId,
      { q: search.value, offset: (page.value - 1) * 50, limit: 50 }, { signal }))
    if (result) data.value = result.value
  } catch (e) { if (current === serial) error.value = e.message }
  finally { if (current === serial) busy.value = false }
}
function submit() { search.value = query.value.trim(); page.value = 1; load() }
watch(page, load)
watch(show, value => { if (value) load(); else { request.cancel(); ++serial; busy.value = false } })
watch(() => props.runId, () => { show.value = false; query.value = ''; search.value = ''; page.value = 1 })
</script>

<template>
  <div class="pricing-notice">
    <div><strong>{{ count }} 条商品成本待核价</strong><p>缺少下单日的历史成本，相关利润和提成暂不能确认。</p></div>
    <n-button size="small" @click="show = true">查看待核价明细</n-button>
  </div>
  <n-drawer v-model:show="show" :width="920" style="max-width: 100vw">
    <n-drawer-content title="待核价明细" closable>
      <p class="pricing-help">请按下单当天的成本核对。导出中的参考单价尚未计入成本。</p>
      <form class="pricing-search" @submit.prevent="submit">
        <n-input v-model:value="query" clearable placeholder="搜索订单号、商品编码" aria-label="搜索待核价明细" />
        <n-button attr-type="submit" :loading="busy">搜索</n-button>
        <a :href="download" download>导出明细</a>
      </form>
      <n-alert v-if="error" type="error" style="margin-bottom: 16px">{{ error }} <n-button size="small" @click="load">重试</n-button></n-alert>
      <n-data-table class="pricing-desktop" :columns="columns" :data="data.items" :loading="busy" :scroll-x="880" :max-height="560" size="small" />
      <div class="pricing-mobile" :aria-busy="busy">
        <p v-if="busy">正在加载…</p>
        <p v-else-if="!data.items.length && !error">没有找到对应记录</p>
        <article v-for="(row, index) in data.items" :key="index">
          <strong>{{ row.sku || '商品编码待核对' }}</strong><span>数量 {{ row.quantity ?? '待核对' }}</span>
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
