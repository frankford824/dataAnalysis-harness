<script setup>
import { computed, h, ref, watch } from 'vue'
import { NButton } from 'naive-ui'
import { commissionRequest } from './commissionRequest'
import { useLatest } from './ui/useLatest'
import LedgerTable from './ui/LedgerTable.vue'
import { excludedProductIds, liveProfitTotals, matchesProduct, sameIds } from '../commissionProfit'
const props = defineProps({target:{type:Object,default:null}})
const emit = defineEmits(['close', 'saved'])
const request = useLatest()
const data = ref(null), loading = ref(false), saving = ref(false), error = ref('')
const search = ref(''), note = ref(''), included = ref([]), opened = ref('')
let serial = 0
const money = value => value == null ? '—' : Number(value).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})
const products = computed(() => data.value?.products || [])
const visible = computed(() => products.value.filter(row => matchesProduct(row, search.value)))
const totals = computed(() => liveProfitTotals(products.value, included.value))
const savedExcluded = computed(() => data.value?.saved?.excluded_product_ids || [])
const dirty = computed(() => !sameIds(excludedProductIds(products.value, included.value), savedExcluded.value))
const canSave = computed(() => !!data.value && dirty.value && !!note.value.trim() && !loading.value && !saving.value)
function rateText(row) {
  if (row.rate_mixed) return '不一致'
  if (row.rate == null) return '—'
  return `${(Number(row.rate) * 100).toLocaleString('zh-CN', {maximumFractionDigits:4})}%`
}
function mergeChecked(keys) {
  const shown = new Set(visible.value.map(row => row.product_id))
  included.value = [...new Set([
    ...included.value.filter(id => !shown.has(id)),
    ...keys,
  ])]
}
function includeVisible() { mergeChecked(visible.value.map(row => row.product_id)) }
function excludeVisible() { mergeChecked([]) }
function openOrders(row) { opened.value = opened.value === row.product_id ? '' : row.product_id }
const openedOrders = computed(() => products.value.find(row => row.product_id === opened.value)?.lines || [])
const columns = computed(() => [
  {type:'selection', width:36, mobileWidth:32},
  {title:'商品', key:'product', minWidth:220, mobileWidth:150, render:row => h('button', {
    type:'button', class:'profit-product', onClick:() => openOrders(row),
  }, [h('strong', row.product_name || '未填写名称'), h('small', row.product_id || '无宝贝ID')])},
  {title:'订单', key:'orders', width:72, mobileWidth:56, align:'right', render:row => h('button', {
    type:'button', class:'profit-orders', onClick:() => openOrders(row),
  }, `${row.orders} 笔`)},
  {title:'销售额', key:'sales', width:112, mobile:false, align:'right', render:row => money(row.sales)},
  {title:'毛利', key:'gross', width:112, mobile:false, align:'right', render:row => money(row.gross)},
  {title:'本人创造利润', key:'profit', width:128, mobileWidth:110, align:'right',
    render:row => h('span', {class:['table-money', row.profit < 0 ? 'negative' : '']}, money(row.profit))},
  {title:'点数', key:'rate', width:80, mobile:false, render:row => rateText(row)},
])
async function load() {
  if (!props.target) return
  const attempt = ++serial
  loading.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({
      store_id: props.target.store_id, period: props.target.period,
      person_id: props.target.person_id, run_id: String(props.target.run_id),
    })
    const result = await request.run(signal => commissionRequest(`/profit-composition?${params}`, {signal}))
    if (!result || attempt !== serial) return
    data.value = result.value
    const cut = new Set(result.value.excluded_product_ids || [])
    included.value = (result.value.products || []).filter(row => !cut.has(row.product_id)).map(row => row.product_id)
    note.value = result.value.saved?.note || ''
    opened.value = ''
  } catch (e) {
    if (attempt === serial) error.value = e.message
  } finally {
    if (attempt === serial) loading.value = false
  }
}
watch(() => props.target, value => {
  serial++
  request.cancel()
  loading.value = false
  data.value = null
  included.value = []
  search.value = ''
  note.value = ''
  opened.value = ''
  error.value = ''
  if (value) load()
})
async function save() {
  if (!canSave.value) return
  saving.value = true
  error.value = ''
  try {
    const saved = await commissionRequest('/profit-exclusions', {body:{
      store_id: data.value.store_id, period: data.value.period,
      person_id: data.value.person_id, run_id: data.value.run_id,
      source_sha: data.value.source_sha,
      excluded_product_ids: excludedProductIds(products.value, included.value),
      note: note.value.trim(),
    }})
    data.value = saved
    const cut = new Set(saved.excluded_product_ids || [])
    included.value = (saved.products || []).filter(row => !cut.has(row.product_id)).map(row => row.product_id)
    emit('saved', saved)
  } catch (e) {
    error.value = e.message
  } finally {
    saving.value = false
  }
}
</script>
<template>
  <n-drawer :show="!!target" :width="'min(880px,100vw)'" @update:show="!$event && emit('close')">
    <n-drawer-content :title="`${target?.person || ''}的利润构成`" closable :native-scrollbar="false">
      <p class="profit-scope">{{ target?.store }} · {{ target?.period }}。勾掉不进阶梯的商品，上面合计马上变。本月已算提成不会改。</p>
      <n-spin :show="loading">
        <div class="profit-kpis">
          <div><span>全部商品利润</span><strong>¥{{ money(totals.all) }}</strong></div>
          <div class="cut"><span>已剔除 {{ totals.excludedCount }} 个</span><strong>¥{{ money(totals.excluded) }}</strong></div>
          <div class="keep"><span>计入阶梯</span><strong>¥{{ money(totals.included) }}</strong></div>
          <div><span>当前提成试算</span><strong>¥{{ money(data?.commission_trial) }}</strong></div>
        </div>
        <n-alert v-if="error" type="error" :bordered="false">{{ error }} <n-button text @click="load">重试</n-button></n-alert>
        <div class="profit-toolbar">
          <input v-model="search" class="commission-search" type="search" placeholder="搜索商品名称或宝贝ID" aria-label="搜索商品">
          <button type="button" class="text-button" :disabled="!visible.length" @click="includeVisible">全选计入</button>
          <button type="button" class="text-button" :disabled="!visible.length" @click="excludeVisible">全选剔除</button>
          <span class="profit-hint">勾选计入阶梯</span>
        </div>
        <p v-if="search.trim()" class="profit-filter">正在看 {{ visible.length }} / {{ products.length }} 个商品，顶栏合计仍是全部。</p>
        <LedgerTable :rows="visible" :columns="columns" :row-key="row => row.product_id" :checked-keys="included"
          :loading="loading" :max-height="420" empty="这个人在本店本月没有已分配的商品利润"
          @update:checked-keys="mergeChecked" />
        <div v-if="opened" class="profit-orders-panel">
          <div class="spread"><strong>{{ products.find(row => row.product_id === opened)?.product_name || '商品' }} 的订单</strong>
            <button type="button" class="text-button" @click="opened=''">收起</button></div>
          <div v-for="line in openedOrders" :key="line.order_id" class="profit-order">
            <span>{{ line.order_id }}</span>
            <span class="num">利润 ¥{{ money(line.profit) }}</span>
          </div>
          <p v-if="!openedOrders.length" class="muted">没有可展开的订单。</p>
        </div>
        <p v-if="target?.labor_cost != null" class="profit-labor">本店本月兼职 ¥{{ money(target.labor_cost) }}，只作对照，不进阶梯加减。</p>
        <p class="profit-foot">计入阶梯利润 <strong>¥{{ money(totals.included) }}</strong>。请按公司规则套在这个数上；系统不自动改本月已算提成。</p>
        <n-input v-model:value="note" type="textarea" :rows="2" maxlength="500" show-count
          placeholder="写明为什么剔除这些商品，例如：样品链接不计入阶梯" />
      </n-spin>
      <template #footer>
        <n-space>
          <n-button @click="emit('close')">关闭</n-button>
          <n-button type="primary" :loading="saving" :disabled="!canSave" @click="save">保存剔除</n-button>
        </n-space>
      </template>
    </n-drawer-content>
  </n-drawer>
</template>
<style scoped>
.profit-scope{font-size:12px;color:#8390a3;margin:0 0 16px;line-height:1.6}
.profit-kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:16px}
.profit-kpis>div{background:#f4f7fc;border-radius:8px;padding:12px 14px}
.profit-kpis span{display:block;font-size:12px;color:#718097}
.profit-kpis strong{display:block;margin-top:6px;font-size:20px;font-weight:650;letter-spacing:-.4px;font-variant-numeric:tabular-nums}
.profit-kpis .cut{background:#fff4f2}
.profit-kpis .cut strong{color:#b45248}
.profit-kpis .keep{background:#eef4ff}
.profit-kpis .keep strong{color:#3468f0}
.profit-toolbar{display:flex;align-items:center;gap:14px;margin:0 0 10px}
.profit-toolbar .commission-search{min-width:0;flex:1;max-width:360px}
.profit-hint{margin-left:auto;font-size:12px;color:#8a94a3}
.profit-filter{font-size:12px;color:#7a8595;margin:0 0 10px}
.profit-orders-panel{margin:12px 0 4px;padding:12px 14px;background:#f7f9fc;border-radius:8px}
.profit-orders-panel strong{font-size:13px}
.profit-order{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-bottom:1px solid #e9edf2;font-size:12px}
.profit-order:last-child{border-bottom:0}
.profit-labor,.profit-foot{font-size:12px;color:#5b6a80;line-height:1.7;margin:14px 0 10px}
.profit-foot strong{font-size:15px;color:#1e2c40;font-variant-numeric:tabular-nums}
.n-alert{margin-bottom:12px}
:deep(.profit-product),:deep(.profit-orders){border:0;background:transparent;padding:0;text-align:left;cursor:pointer;color:inherit;font:inherit}
:deep(.profit-product){display:grid;gap:3px}
:deep(.profit-product) strong{font-weight:550;color:#233247}
:deep(.profit-product) small{color:#8390a2;font-size:12px}
:deep(.profit-orders){color:#3468f0}
@media(max-width:720px){
  .profit-kpis{grid-template-columns:repeat(2,minmax(0,1fr))}
  .profit-kpis strong{font-size:18px}
  .profit-toolbar{flex-wrap:wrap}
}
</style>
