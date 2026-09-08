<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useApp } from '../store'
import PageHead from '../components/PageHead.vue'

const app = useApp()
const start = ref(app.period || app.periods[0] || new Date().toLocaleDateString('sv-SE', { timeZone: 'Asia/Shanghai' }).slice(0, 7))
const end = ref(start.value)
const stores = ref(app.storeId ? [app.storeId] : [])
const people = ref([])
const roster = ref([])
const report = ref(null)
const busy = ref(false)
const downloading = ref(false)
const error = ref('')
const kind = ref('people')
const page = ref(1)
const kinds = [
  { value: 'people', label: '按人员汇总' }, { value: 'stores', label: '按店铺汇总' },
  { value: 'breakdown', label: '人员 / 店铺 / 账期明细' }, { value: 'coverage', label: '计算情况' },
]
const storeOptions = computed(() => app.stores.map(s => ({ value: s.id, label: s.name })))
const personOptions = computed(() => {
  const all = new Map(roster.value.map(p => [p.id, { value: p.id, label: p.name + (p.employee_no ? `（${p.employee_no}）` : '') }]))
  for (const p of report.value?.available_people || []) if (!all.has(p.id)) all.set(p.id, { value: p.id, label: p.name })
  return [...all.values()]
})
const selection = computed(() => ({ start: start.value, end: end.value, store_ids: [...stores.value].sort(), person_ids: [...people.value].sort() }))
const stale = computed(() => !!report.value && JSON.stringify(selection.value) !== JSON.stringify(report.value.selection))
const tableRows = computed(() => report.value?.[kind.value === 'breakdown' ? 'rows' : kind.value] || [])
const visibleRows = computed(() => tableRows.value.slice((page.value - 1) * 50, page.value * 50))
const columns = computed(() => ({
  people: [['person', '人员'], ['employee_no', '工号'], ['amount', '提成金额'], ['stores', '店铺数'], ['periods', '账期数'], ['status', '计算状态']],
  stores: [['store', '店铺'], ['amount', '提成金额'], ['people', '人员数'], ['periods', '已计算账期数'], ['missing', '未计算账期数'], ['status', '计算状态']],
  breakdown: [['person', '人员'], ['store', '店铺'], ['period', '账期'], ['amount', '提成金额'], ['base', '本人参与基数'], ['base_name', '基数名称'], ['status', '计算状态'], ['finance_run', '计算记录']],
  coverage: [['store', '店铺'], ['period', '账期'], ['selected_amount', '筛选范围提成金额'], ['status', '计算状态'], ['unassigned_orders', '未分配订单数'], ['notes', '说明'], ['finance_run', '计算记录']],
}[kind.value]))
const moneyKeys = new Set(['amount', 'base', 'selected_amount'])
const money = value => value == null ? '—' : Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
async function checked(response) {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(typeof body.detail === 'string' ? body.detail : '查询失败，请检查账期和筛选条件')
  }
  return response
}
async function query() {
  busy.value = true; error.value = ''
  try {
    const response = await checked(await fetch('/api/commission-v2/reports/query', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(selection.value),
    }))
    report.value = await response.json(); page.value = 1
  } catch (e) { error.value = e.message; report.value = null }
  finally { busy.value = false }
}
async function download() {
  if (!report.value || stale.value || busy.value || downloading.value) return
  downloading.value = true; error.value = ''
  try {
    const response = await checked(await fetch(`/api/commission-v2/export/reports/${kind.value}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...report.value.selection, run_ids: report.value.run_ids, fingerprint: report.value.fingerprint }),
    }))
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url; link.download = `提成-${kinds.find(k => k.value === kind.value).label.replaceAll('/', '-')}-${report.value.selection.start}至${report.value.selection.end}.csv`
    document.body.appendChild(link); link.click(); link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (e) { error.value = e.message }
  finally { downloading.value = false }
}
watch(kind, () => { page.value = 1 })
onMounted(async () => {
  try { roster.value = await checked(await fetch('/api/commission-v2/people')).then(r => r.json()).then(r => r.people) }
  catch (e) { error.value = e.message }
  await query()
})
</script>

<template>
  <div class="report-page">
    <PageHead title="提成金额汇总" hint="选择账期、人员和店铺，查看已保存的提成金额并导出。" />
    <router-link to="/commission" class="back">返回提成设置</router-link>
    <div class="filters">
      <label>开始账期<input v-model="start" type="month" aria-label="开始账期" :disabled="busy || downloading" /></label>
      <label>结束账期<input v-model="end" type="month" aria-label="结束账期" :disabled="busy || downloading" /></label>
      <label class="multi">店铺<n-select v-model:value="stores" multiple filterable clearable :options="storeOptions" placeholder="全部店铺，可多选" aria-label="汇总店铺" :disabled="busy || downloading" /></label>
      <label class="multi">人员<n-select v-model:value="people" multiple filterable clearable :options="personOptions" placeholder="全部人员，可多选" aria-label="汇总人员" :disabled="busy || downloading" /></label>
      <n-button type="primary" :loading="busy" :disabled="downloading" @click="query">查询汇总</n-button>
    </div>
    <p class="hint">人员和店铺留空表示全部；同时选择时，汇总所选人员在所选店铺的提成。已结账使用结账快照，未结账使用最近一次计算。</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="stale" role="status" class="notice">筛选条件已修改，请点击“查询汇总”更新结果后再导出。</p>
    <template v-if="report">
      <div class="summary"><div><span>筛选范围提成合计（元）</span><strong>{{ money(report.total) }}</strong></div><p>{{ report.selection.start }} 至 {{ report.selection.end }}<br/>{{ report.people.length }} 位人员 · {{ report.stores.length }} 家店铺</p></div>
      <p v-if="report.missing_periods || report.trial_periods" class="notice">{{ report.missing_periods }} 个店铺账期未计算，{{ report.trial_periods }} 个店铺账期含试算标记。合计仅包含已有金额，请结合计算情况核对。</p>
      <p class="hint">此处为计算结果，不代表已发放金额；“—”表示没有对应金额记录。下载包含当前筛选下全部行，CSV 可用 Excel / WPS 打开。</p>
      <div class="toolbar"><n-select v-model:value="kind" :options="kinds" aria-label="汇总方式" :disabled="downloading" /><n-button :disabled="stale || busy" :loading="downloading" @click="download">导出当前汇总 CSV</n-button></div>
      <div class="table-wrap"><table><thead><tr><th v-for="[key,label] in columns" :key="key">{{ label }}</th></tr></thead><tbody><tr v-for="(row,i) in visibleRows" :key="i"><td v-for="[key] in columns" :key="key" :class="{amount:moneyKeys.has(key)}">{{ moneyKeys.has(key) ? money(row[key]) : (row[key] ?? '—') }}</td></tr><tr v-if="!tableRows.length"><td :colspan="columns.length" class="empty">没有对应提成记录。可切换“计算情况”查看哪些账期尚未计算。</td></tr></tbody></table></div>
      <div class="paging"><span>共 {{ tableRows.length }} 行</span><n-button :disabled="page <= 1" @click="page--">上一页</n-button><span>{{ page }} / {{ Math.max(1, Math.ceil(tableRows.length/50)) }}</span><n-button :disabled="page * 50 >= tableRows.length" @click="page++">下一页</n-button></div>
    </template>
  </div>
</template>

<style scoped>
.report-page{max-width:1400px;margin:auto;padding-bottom:32px}.back{display:inline-block;margin:12px 0;color:#2563eb;font-size:14px}.filters{display:flex;align-items:flex-end;gap:14px;flex-wrap:wrap;margin-top:12px}.filters label{display:flex;flex-direction:column;gap:8px;font-size:13px;color:#657080}.filters .multi{flex:1;min-width:220px}.filters input{height:34px;border:1px solid #dce0e6;border-radius:6px;padding:0 9px;color:#263244;background:white}.hint{font-size:13px;color:#657080;line-height:1.8}.notice{padding:12px 16px;background:#fff8e8;color:#8a5d13;border-radius:7px;font-size:14px}.error{color:#b42318}.summary{display:flex;gap:30px;align-items:center;margin:24px 0 16px;padding:20px 24px;background:white;border:1px solid #e5e7eb;border-radius:10px}.summary span,.summary p{font-size:13px;color:#657080;line-height:1.8}.summary strong{display:block;font-size:30px;color:#22334b;font-variant-numeric:tabular-nums}.toolbar{display:flex;gap:12px;align-items:center;margin:20px 0 14px;flex-wrap:wrap}.toolbar .n-select{width:240px}.table-wrap{overflow:auto;background:white;border:1px solid #e5e7eb;border-radius:10px}table{width:100%;min-width:740px;border-collapse:collapse}th,td{padding:14px 18px;text-align:left;border-bottom:1px solid #eef0f3;font-size:14px;line-height:1.6;vertical-align:top}th{background:#f8fafc;font-weight:500;color:#6b7280}.amount{font-variant-numeric:tabular-nums;white-space:nowrap}.empty{text-align:center;padding:40px;color:#89919d}.paging{display:flex;gap:14px;align-items:center;justify-content:flex-end;margin-top:16px;font-size:13px;color:#657080}@media(max-width:640px){.filters .multi{min-width:100%}.summary{gap:15px;padding:16px}.summary strong{font-size:25px}.paging{gap:8px}.filters label{flex:1}}
</style>
