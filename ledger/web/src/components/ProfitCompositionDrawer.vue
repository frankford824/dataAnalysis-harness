<script setup>
import { computed, h, ref, watch } from 'vue'
import { NButton } from 'naive-ui'
import { commissionRequest } from './commissionRequest'
import { useLatest } from './ui/useLatest'
import LedgerTable from './ui/LedgerTable.vue'
import { excludedProductIds, liveProfitTotals, matchesProduct, profitCompositionCsv, rateLabel, sameIds } from '../commissionProfit'
const props = defineProps({target:{type:Object,default:null}})
const emit = defineEmits(['close', 'saved'])
const request = useLatest()
const orderRequest = useLatest(), orderLines=ref([]), ordersLoading=ref(false), ordersError=ref('')
const data = ref(null), loading = ref(false), saving = ref(false), error = ref('')
const search = ref(''), note = ref(''), included = ref([]), opened = ref(''), exporting = ref(false)
let serial = 0
let orderSerial = 0
const money = value => value == null ? '—' : Number(value).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})
const products = computed(() => data.value?.products || [])
const visible = computed(() => products.value.filter(row => matchesProduct(row, search.value)))
const productPage=ref(1)
const pageProducts=computed(()=>visible.value.slice((productPage.value-1)*50,productPage.value*50))
watch(search,()=>{productPage.value=1})
const totals = computed(() => liveProfitTotals(products.value, included.value))
const creatorTotal = computed(() => {
  const ordinary = products.value.filter(row => !row.managed)
  if (!ordinary.length || ordinary.some(row => row.creator_pending || row.creator_profit == null)) return null
  return ordinary.reduce((sum, row) => sum + Math.round(Number(row.creator_profit) * 100), 0) / 100
})
const savedExcluded = computed(() => data.value?.saved?.excluded_product_ids || [])
const dirty = computed(() => !sameIds(excludedProductIds(products.value, included.value), savedExcluded.value))
const canSave = computed(() => !!data.value && dirty.value && !!note.value.trim() && !loading.value && !saving.value)
function rateText(row) {
  return rateLabel(row) || '—'
}
function mergeChecked(keys) {
  const shown = new Set(pageProducts.value.map(row => row.product_id))
  included.value = [...new Set([
    ...included.value.filter(id => !shown.has(id)),
    ...keys,
  ])]
}
function includeVisible() { included.value=[...new Set([...included.value,...visible.value.map(row=>row.product_id)])] }
function excludeVisible() { const ids=new Set(visible.value.map(row=>row.product_id));included.value=included.value.filter(id=>!ids.has(id)) }
async function openOrders(row) {
  const ticket=++orderSerial
  orderRequest.cancel();orderLines.value=[];ordersError.value='';ordersLoading.value=false
  opened.value = opened.value === row.product_id ? '' : row.product_id
  if(!opened.value)return
  if(row.lines?.length){orderLines.value=row.lines;return}
  ordersLoading.value=true
  try{
    const context=data.value, product=opened.value
    const params=new URLSearchParams({store_id:context.store_id,period:context.period,person_id:context.person_id,run_id:String(context.run_id),product_id:product,include_orders:'true'})
    const result=await orderRequest.run(signal=>commissionRequest(`/profit-composition?${params}`,{signal}))
    if(result&&opened.value===product)orderLines.value=result.value.products.find(p=>p.product_id===product)?.lines||[]
  }catch(e){if(ticket===orderSerial)ordersError.value=e.message}finally{if(ticket===orderSerial)ordersLoading.value=false}
}
const openedOrders = computed(() => orderLines.value)
const columns = computed(() => [
  {type:'selection', width:36, mobileWidth:32},
  {title:'商品', key:'product', minWidth:220, mobileWidth:150, render:row => h('button', {
    type:'button', class:'profit-product', onClick:() => openOrders(row),
  }, [h('strong', row.product_name || '未填写名称'), h('small', row.product_id || '无宝贝ID'), row.managed ? h('small', '含托管销售额 · 不计个人销售额') : null])},
  {title:'订单', key:'orders', width:72, mobileWidth:56, align:'right', render:row => h('button', {
    type:'button', class:'profit-orders', onClick:() => openOrders(row),
  }, `${row.orders} 笔`)},
  {title:() => h('div', {class:'profit-col-title'}, [h('span', '商品销售收入'), h('small', '全额，对看板')]),
    key:'product_sales', width:124, mobile:false, align:'right',
    render:row => h('span', {title:'该宝贝本月订单的财务销售收入全额，未按人头拆，用来对利润看板'}, money(row.product_sales))},
  {title:() => h('div', {class:'profit-col-title'}, [h('span', '本人销售额'), h('small', '按做货归属')]),
    key:'sales', width:112, mobile:false, align:'right',
    render:row => h('span', {title:'仅使用明确生效的商品身份或归档身份；缺少依据时不猜测'}, row.sales_pending?'归属待确认':money(row.sales))},
  {title:() => h('div', {class:'profit-col-title'}, [h('span', '做货成本'), h('small', '非托管身份归属')]),
    key:'creator_cost', width:112, mobile:false, align:'right', render:row => row.creator_pending?'归属待确认':money(row.creator_cost)},
  {title:() => h('div', {class:'profit-col-title'}, [h('span', '做货毛利'), h('small', '非托管身份归属')]),
    key:'creator_gross', width:112, mobile:false, align:'right', render:row => row.creator_pending?'归属待确认':money(row.creator_gross)},
  {title:() => h('div', {class:'profit-col-title'}, [h('span', '做货创造利润'), h('small', '未扣店级兼职')]),
    key:'creator_profit', width:132, mobileWidth:118, align:'right',
    render:row => h('span', {class:['table-money', row.creator_profit < 0 ? 'negative' : ''],
      title:'非托管商品经营利润按做货身份归属；提成沿用原核算点数'},
      row.creator_pending?'归属待确认':money(row.creator_profit))},
  {title:() => h('div', {class:'profit-col-title'}, [h('span', '旧阶梯利润'), h('small', '原核算，供剔除对照')]),
    key:'profit', width:112, mobile:false, align:'right',render:row=>money(row.profit)},
  {title:() => h('div', {class:'profit-col-title'}, [h('span', '本人点数'), h('small', '该商品上此人份额')]),
    key:'rate', width:96, mobile:false, render:row => h('span', {
      title: row.rate_mixed ? '本月这个商品上此人登记过不同点数' : '提成设置里此人在这个宝贝上的份额，不是链接总点数',
    }, rateText(row))},
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
      include_orders: 'false',
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
  productPage.value=1
  note.value = ''
  opened.value = ''
  orderSerial++;orderRequest.cancel();orderLines.value=[];ordersError.value='';ordersLoading.value=false
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
function exportTable() {
  if (!data.value || exporting.value) return
  exporting.value = true
  try {
    const csv = profitCompositionCsv(data.value, included.value)
    const blob = new Blob([csv], {type: 'text/csv;charset=utf-8'})
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    const who = data.value.person || '人员'
    const store = data.value.store || data.value.store_id || '店铺'
    link.href = url
    link.download = `利润构成-${who}-${store}-${data.value.period || ''}.csv`
    document.body.appendChild(link)
    link.click()
    link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } finally {
    exporting.value = false
  }
}
</script>
<template>
  <n-drawer :show="!!target" :width="'min(1080px,100vw)'" @update:show="!$event && emit('close')">
    <n-drawer-content :title="`${target?.person || ''}的利润构成`" closable :native-scrollbar="false">
      <p class="profit-scope">{{ target?.store }} · {{ target?.period }}。做货创造业绩按商品身份查看；勾选计入阶梯仍沿用原核算利润，本月已算提成不会改。</p>
      <n-alert type="info" :bordered="false" class="profit-basis">
        <b>做货创造业绩</b>：非托管商品的销售、成本、毛利和商品利润归做货人；抽点人员保留提成，不分走创造业绩。身份不足时显示“归属待确认”。
        <b>旧阶梯利润与提成</b>保留原核算口径，供剔除和已结账金额对照；勾选商品不会改变已算提成。
        <b>托管商品</b>的销售额归指定团队，个人原毛利、利润、提成仍在旧核算口径中查看。
        <details class="profit-basis-more"><summary>查看归属与点数说明</summary>商品销售收入是全额财务收入。单人做货归全额；多位做货人仅在做货人员间按点数分摊。缺信息订单只有人员、身份和比例完全一致且覆盖整月时才采用唯一归属。负数表示做货亏损，尚未扣店级兼职；本人点数是该商品上的提成份额。</details>
      </n-alert>
      <n-alert v-if="data?.allocation_correction" type="info" :bordered="false">本次为历史分配更正，原核算 {{data.allocation_correction.from_run}} 已保留。全店已核实 {{data.allocation_correction.verified_orders}} 个主单，另有 {{data.allocation_correction.pending_orders?.length || 0}} 个主单仍保留历史分配待复核；原已核定实发未改变。</n-alert>
      <n-alert v-if="data?.sales_pending_products?.length" type="warning" :bordered="false">本店本月有 {{data.sales_pending_products.length}} 个商品的销售归属待确认，请在提成设置中明确生效身份后刷新；当前不计为已确认个人业绩。</n-alert>
      <n-alert v-if="data?.creator_pending_products?.length" type="warning" :bordered="false">本店本月有 {{data.creator_pending_products.length}} 个非托管商品的做货创造业绩缺少完整身份或金额依据；相关创造成本、毛利和利润保持待确认。</n-alert>
      <n-spin :show="loading">
        <div class="profit-kpis">
          <div><span>做货创造利润<small>非托管商品，未扣兼职</small></span><strong>{{creatorTotal==null?'—':`¥${money(creatorTotal)}`}}</strong></div>
          <div><span>旧阶梯利润<small>原核算口径，未扣兼职</small></span><strong>¥{{ money(totals.all) }}</strong></div>
          <div class="cut"><span>已剔除 {{ totals.excludedCount }} 个<small>未扣兼职</small></span><strong>¥{{ money(totals.excluded) }}</strong></div>
          <div class="keep"><span>计入阶梯<small>未扣兼职</small></span><strong>¥{{ money(totals.included) }}</strong></div>
          <div><span>当前提成试算<small>订单试算，未扣兼职</small></span><strong>¥{{ money(data?.commission_trial) }}</strong></div>
        </div>
        <n-alert v-if="error" type="error" :bordered="false">{{ error }} <n-button text @click="load">重试</n-button></n-alert>
        <div class="profit-toolbar">
          <input v-model="search" class="commission-search" type="search" placeholder="搜索商品名称或宝贝ID" aria-label="搜索商品">
          <button type="button" class="text-button" :disabled="!visible.length" @click="includeVisible">全选计入</button>
          <button type="button" class="text-button" :disabled="!visible.length" @click="excludeVisible">全选剔除</button>
          <n-button size="small" :disabled="!products.length" :loading="exporting" @click="exportTable">导出表格</n-button>
          <span class="profit-hint">勾选计入阶梯</span>
        </div>
        <p v-if="search.trim()" class="profit-filter">正在看 {{ visible.length }} / {{ products.length }} 个商品，顶栏合计仍是全部。</p>
        <LedgerTable :rows="pageProducts" :columns="columns" :row-key="row => row.product_id" :checked-keys="included"
          :loading="loading" :max-height="420" empty="这个人在本店本月没有已分配的商品利润"
          @update:checked-keys="mergeChecked" />
        <div v-if="visible.length>50" class="commission-paging"><span>共 {{visible.length}} 个商品，每页 50 个；上方合计仍为全部</span><n-button :disabled="productPage<=1" @click="productPage--">上一页</n-button><span>{{productPage}} / {{Math.ceil(visible.length/50)}}</span><n-button :disabled="productPage*50>=visible.length" @click="productPage++">下一页</n-button></div>
        <div v-if="opened" class="profit-orders-panel">
          <div class="spread"><strong>{{ products.find(row => row.product_id === opened)?.product_name || '商品' }} 的订单</strong>
            <button type="button" class="text-button" @click="opened=''">收起</button></div>
          <div v-for="line in openedOrders" :key="line.order_id" class="profit-order">
            <span>{{ line.order_id }}</span>
            <span class="num">做货创造利润 ¥{{ money(line.creator_profit) }} · 旧阶梯利润 ¥{{ money(line.profit) }}</span>
          </div>
          <p v-if="ordersLoading" class="muted">正在加载订单…</p>
          <p v-else-if="ordersError" role="alert">{{ ordersError }}</p>
          <p v-else-if="!openedOrders.length" class="muted">没有可展开的订单。</p>
        </div>
        <p v-if="target?.labor_cost != null" class="profit-labor">本店本月兼职 ¥{{ money(target.labor_cost) }} 是店级分摊，只作对照：<b>没有从上面任何一行利润里扣除</b>，也不进阶梯加减。</p>
        <p class="profit-foot">原核算计入阶梯的未扣兼职利润 <strong>¥{{ money(totals.included) }}</strong>。请按公司规则套在这个数上；系统不自动改本月已算提成。</p>
        <n-input v-model:value="note" type="textarea" :rows="2" maxlength="500" show-count
          placeholder="写明为什么剔除这些商品，例如：样品链接不计入阶梯" />
      </n-spin>
      <template #footer>
        <n-space>
          <n-button :disabled="!products.length" :loading="exporting" @click="exportTable">导出表格</n-button>
          <n-button @click="emit('close')">关闭</n-button>
          <n-button type="primary" :loading="saving" :disabled="!canSave" @click="save">保存剔除</n-button>
        </n-space>
      </template>
    </n-drawer-content>
  </n-drawer>
</template>
<style scoped>
.profit-scope{font-size:12px;color:#8390a3;margin:0 0 12px;line-height:1.6}
.profit-basis{margin:0 0 14px}
.profit-basis :deep(.n-alert__content){font-size:12px;line-height:1.7;color:#3d4a5c}
.profit-kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-bottom:16px}
.profit-basis-more{margin-top:6px}.profit-basis-more summary{cursor:pointer;color:#3560d6}
.profit-kpis>div{background:#f4f7fc;border-radius:8px;padding:12px 14px}
.profit-kpis span{display:block;font-size:12px;color:#718097}
.profit-kpis small{display:block;margin-top:2px;font-size:11px;color:#8a94a3}
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
:deep(.profit-col-title){display:grid;gap:2px;line-height:1.2}
:deep(.profit-col-title) small{color:#8a94a3;font-weight:400;font-size:11px}
@media(max-width:940px){.profit-kpis{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:720px){
  .profit-kpis{grid-template-columns:repeat(2,minmax(0,1fr))}
  .profit-kpis strong{font-size:18px}
  .profit-toolbar{flex-wrap:wrap}
}
</style>
