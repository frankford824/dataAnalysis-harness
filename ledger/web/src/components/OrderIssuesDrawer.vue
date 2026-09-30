<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { api } from '../api'
import { useLatest } from './ui/useLatest'

const props = defineProps({ runId: { type: Number, required: true }, issueId: { type: String, required: true }, title: String })
const emit = defineEmits(['close'])
const data = ref(null), loading = ref(false), error = ref(''), query = ref(''), search = ref(''), page = ref(1)
const copied = ref(''), copyError = ref(''), copyText = ref('')
const request = useLatest()
let serial = 0
const size = 50
const download = computed(() => `/api/runs/${props.runId}/order-issues/${encodeURIComponent(props.issueId)}/export.csv?${new URLSearchParams({ q: search.value })}`)
async function load() {
  const current = ++serial
  loading.value = true; error.value = ''; data.value = null; copied.value = ''; copyError.value = ''; copyText.value = ''
  try {
    const response = await request.run(signal => api.orderIssues(props.runId, props.issueId,
      { q: search.value, offset: (page.value - 1) * size, limit: size }, { signal }))
    if (response) data.value = response.value
  } catch (e) { if (current === serial) error.value = e.message }
  finally { if (current === serial) loading.value = false }
}
function submit() { search.value = query.value.trim(); if (page.value === 1) load(); else page.value = 1 }
watch(page, load)
watch(() => [props.runId, props.issueId], () => { query.value = ''; search.value = ''; if (page.value === 1) load(); else page.value = 1 }, { immediate: true })
onUnmounted(() => { ++serial; request.cancel() })
async function copyOrders(ids, label) {
  copied.value = ''; copyError.value = ''; copyText.value = ids.filter(Boolean).join('\n')
  if (!copyText.value) return
  try {
    if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable')
    await navigator.clipboard.writeText(copyText.value)
  } catch {
    const input = document.createElement('textarea')
    input.value = copyText.value; input.style.position = 'fixed'; input.style.opacity = '0'
    document.body.appendChild(input); input.select()
    const success = document.execCommand('copy'); input.remove()
    if (!success) { copyError.value = '自动复制未成功，可从下方文本框手动复制订单号。'; return }
  }
  copied.value = label
}
</script>

<template>
  <n-drawer :show="true" :width="'min(1100px, 96vw)'" @update:show="value => { if (!value) emit('close') }">
    <n-drawer-content :title="`${title || data?.title || '待处理订单'} · 订单明细`" closable>
      <n-alert type="info" :bordered="false" style="margin-bottom: var(--s3)">
        <template v-if="issueId === 'dropship_scope_evidence'">核对备注具体指向哪个商品编码、代发多少件；同一订单的不同商品分别列出。</template>
        <template v-else>核对代发表中该订单的付款记录和订单号；缺口商品的聚水潭成本已计零，代发支出需有对应依据。</template>
      </n-alert>
      <n-form @submit.prevent="submit">
        <div class="row controls">
          <n-input v-model:value="query" clearable aria-label="搜索待处理订单" placeholder="平台订单号、聚水潭订单号或商品编码" style="flex: 1; min-width: 220px" />
          <n-button :loading="loading" @click="submit">查询订单</n-button>
        </div>
      </n-form>
      <n-alert v-if="error" type="error" style="margin-top: var(--s3)">{{ error }}</n-alert>
      <n-spin :show="loading">
        <template v-if="data">
          <p class="small summary">{{ data.order_count }} 笔订单 · {{ data.total }} 条商品明细<template v-if="search">（筛选后；原检查 {{ data.expected_order_count }} 笔）</template></p>
          <p v-if="data.copyable_order_count !== data.order_count" class="xs muted">原检查按原始单号记录计数；拆开合并单号、去重后，可复制 {{ data.copyable_order_count }} 个平台订单号。</p>
          <p class="xs muted">平台订单号和聚水潭订单号均保留完整文本。旧留档没有保存的备注或数量显示“未留档”，请按订单号核对原资料。</p>
          <div class="row controls" style="margin: var(--s3) 0">
            <n-button size="small" :disabled="!data.order_ids?.length" @click="copyOrders(data.order_ids, '已复制筛选结果全部订单号')">复制{{ search ? '筛选结果' : '全部' }}订单号</n-button>
            <a :href="download" download>导出{{ search ? '筛选结果' : '全部' }}商品明细 CSV</a>
          </div>
          <p v-if="copied" class="small" role="status">{{ copied }}</p>
          <template v-if="copyError"><n-alert type="warning">{{ copyError }}</n-alert><n-input :value="copyText" type="textarea" aria-label="可手动复制的订单号" /></template>
          <n-alert v-if="data.missing_order_rows" type="warning">{{ data.missing_order_rows }} 条原记录缺少平台订单号，请按聚水潭订单号及来源位置核对。</n-alert>
          <div class="scroll" style="margin-top: var(--s3)">
            <n-table size="small" :bordered="false" class="issue-table">
              <thead><tr><th>平台订单号</th><th>聚水潭订单号</th><th>商品编码 / 数量</th><th>备注与待核对原因</th><th>来源位置</th></tr></thead>
              <tbody>
                <tr v-for="(row, i) in data.items" :key="i">
                  <td>
                    <div v-for="id in row.platform_order_ids" :key="id" class="identifier">{{ id }}</div>
                    <span v-if="!row.platform_order_ids?.length">{{ row.order_id || '原记录缺少订单号' }}</span>
                    <button v-if="row.platform_order_ids?.length" class="link xs" @click="copyOrders(row.platform_order_ids, '已复制该订单号')">复制订单号</button>
                    <div v-if="row.order_id && row.platform_order_ids?.join(',') !== row.order_id" class="xs muted" style="max-width: 240px; overflow-wrap: anywhere">原记录：{{ row.order_id }}</div>
                  </td>
                  <td class="identifier">{{ row.internal_order_id || '未留档' }}</td>
                  <td><span class="identifier">{{ row.sku || '未留档' }}</span><div class="xs muted">原记录数量：{{ row.quantity ?? '未留档' }}</div></td>
                  <td><div>订单备注：{{ row.order_remark || '未留档' }}</div><div class="xs muted">{{ row.reason }}</div></td>
                  <td class="xs">{{ row.file_name }} · {{ row.sheet }} · 第 {{ row.row_no || '未留档' }} 行</td>
                </tr>
                <tr v-if="!data.items.length"><td colspan="5" class="muted">当前筛选没有订单明细。</td></tr>
              </tbody>
            </n-table>
          </div>
          <n-pagination v-if="data.total > size" v-model:page="page" :item-count="data.total" :page-size="size" :disabled="loading" style="margin-top: var(--s3)" />
        </template>
      </n-spin>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped>
.controls { flex-wrap: wrap; gap: var(--s2); }
.summary { margin-top: var(--s3); font-weight: 600; }
.issue-table { min-width: 1000px; }
.identifier { white-space: nowrap; font-variant-numeric: tabular-nums; }
.issue-table td { vertical-align: top; }
</style>
