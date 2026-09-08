<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import { useApp } from '../store'
import PageHead from '../components/PageHead.vue'
import CommissionBatchDialog from '../components/CommissionBatchDialog.vue'
import CommissionPeople from '../components/CommissionPeople.vue'

const app = useApp()
const message = useMessage()
const rows = ref([])
const people = ref([])
const search = ref('')
const state = ref('')
const after = ref('')
const pages = ref([])
const next = ref('')
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const showEditor = ref(false)
const selected = ref(null)
const personFilter = ref('')
const checked = ref({})
const allScope = ref(null)
const batchDialog = ref(null)
const peopleDialog = ref(null)
const fileInput = ref(null)
const keyOf = row => row.store_id + ':' + row.product_id
const chosen = computed(() => Object.values(checked.value))
const storeOptions = computed(() => [{value:'',label:'全部店铺'},...app.stores.map(s=>({value:s.id,label:s.name}))])
function switchStore(id) { app.pick({store:id,platform:app.stores.find(s=>s.id===id)?.platform || ''}) }
function rowSelected(row){return allScope.value?!((allScope.value.excluded||[]).includes(keyOf(row))):!!checked.value[keyOf(row)]}
function toggleRow(row, value) {
  if(allScope.value){const excluded=new Set(allScope.value.excluded||[]);if(value)excluded.delete(keyOf(row));else excluded.add(keyOf(row));allScope.value={...allScope.value,excluded:[...excluded]};return}
  const next={...checked.value};if(value)next[keyOf(row)]={store_id:row.store_id,product_id:row.product_id,product_name:row.product_name,revision:row.revision||0};else delete next[keyOf(row)];checked.value=next
}
function togglePage(value) { for(const row of rows.value.filter(r=>!r.store_id.startsWith('unmapped:')))toggleRow(row,value) }
function clearSelection(){checked.value={};allScope.value=null}
function selectAll(){checked.value={};allScope.value={store_id:app.storeId || '',search:search.value,state:state.value,person_id:personFilter.value}}
function bulk(){batchDialog.value.open(allScope.value?{scope:{...allScope.value}}:{targets:chosen.value})}
async function saved(){clearSelection();await Promise.all([load(),loadPeople()])}
function showAssignments(id){clearSelection();personFilter.value=id;state.value='';search.value='';switchStore('')}
function importFile(event){const file=event.target.files?.[0];event.target.value='';batchDialog.value.importFile(file)}
const form = ref({ allocations: [] })
let serial = 0, searchTimer
const states = { enabled:'提成中', disabled:'不提成', pending:'未设置', scheduled:'待生效', expired:'已到期' }
const now = () => new Date().toLocaleString('sv-SE', { timeZone:'Asia/Shanghai' }).replace(' ', 'T')
const storeName = id => app.stores.find(s => s.id === id)?.name || '店铺待确认'
const rateText = rate => `${Number((Number(rate) * 100).toFixed(6))}%`
const personOptions = computed(() => people.value.map(p => ({ value:p.id, label:p.name + (p.employee_no ? `（${p.employee_no}）` : '') })))
const total = computed(() => form.value.allocations.reduce((sum, p) => sum + (Number(p.percent) || 0), 0))
const params = computed(() => new URLSearchParams({ store_id:app.storeId || '', search:search.value, state:state.value, person_id:personFilter.value }).toString())
async function call(path, options = {}) {
  const r = await fetch(`/api/commission-v2${path}`, { ...options, headers:{'Content-Type':'application/json'} })
  const body = await r.json()
  if (!r.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '未能保存，请检查填写内容')
  return body
}
async function loadPeople() { people.value = (await call('/people')).people }
async function load() {
  const request = ++serial
  loading.value = true; error.value = ''
  try {
    const data = await call(`/settings?${params.value}&after=${encodeURIComponent(after.value)}`)
    if (request === serial) { rows.value = data.rows; next.value = data.next_after }
  } catch(e) { if (request === serial) error.value = e.message }
  finally { if (request === serial) loading.value = false }
}
function reset() { rows.value=[];next.value='';allScope.value=null; after.value = ''; pages.value = []; load() }
function nextPage() { pages.value.push(after.value); after.value = next.value; load() }
function previousPage() { after.value = pages.value.pop() || ''; load() }
watch([() => app.storeId, state, personFilter], reset)
watch(search, () => { clearTimeout(searchTimer); searchTimer = setTimeout(reset, 250) })
onMounted(async () => { await load(); try { await loadPeople() } catch(e) { error.value = e.message } })
onUnmounted(() => { clearTimeout(searchTimer); serial++ })
async function edit(row = {}) {
  busy.value = true
  try {
    selected.value = row.scheme_id ? await call(`/schemes/${row.scheme_id}`) : null
    const current = row.setting || {}
    form.value = { store_id:row.store_id || app.storeId || '', product_id:row.product_id || '',
      product_name:row.product_name || '', mode:current.mode || 'distribute',
      valid_from:row.state === 'scheduled' ? current.valid_from : now(), valid_to:current.valid_to || '',
      allocations:(row.people || []).map(p => ({ person:p.person_id, percent:Number((Number(p.rate)*100).toFixed(8)) })) }
    if (!form.value.allocations.length) form.value.allocations.push({person:null, percent:null})
    showEditor.value = true
  } catch(e) { message.error(e.message) }
  finally { busy.value = false }
}
async function save() {
  busy.value = true
  try {
    if (!form.value.store_id || !form.value.product_id.trim()) throw new Error('请填写店铺和宝贝ID')
    if (form.value.mode === 'distribute' && (!form.value.allocations.length || form.value.allocations.some(p => !p.person || p.percent == null || !Number.isFinite(Number(p.percent)) || Number(p.percent) <= 0))) throw new Error('请为每位人员填写大于0的提成比例；不提成请选择“不提成”')
    if (form.value.mode === 'distribute' && total.value > 100) throw new Error('提成比例合计不能超过100%')
    const allocations = form.value.mode === 'distribute' ? form.value.allocations.map(p => ({
      ...(people.value.some(x => x.id === p.person) ? {person_id:p.person} : {name:p.person}),
      rate:(Number(p.percent)/100).toFixed(8)
    })) : []
    await call('/settings', {method:'POST', body:JSON.stringify({...form.value, allocations, expected_revision:selected.value?.revision || 0})})
    showEditor.value = false; message.success('已保存')
    await Promise.all([load(), loadPeople()])
  } catch(e) { message.error(e.message, {duration:5000}) }
  finally { busy.value = false }
}
function historicalPeople(segment) {
  return (segment.allocations || []).map(a => `${people.value.find(p => p.id === a.person_id)?.name || '原登记人员'} ${rateText(a.rate)}`).join('、')
}
</script>

<template>
  <div class="commission-page" @dragover.prevent @drop.stop.prevent="batchDialog.importFile($event.dataTransfer.files?.[0])">
    <PageHead title="提成设置" hint="按店铺和宝贝，设置所属人员及提成比例。" />
    <div class="filters">
      <label>店铺<n-select :value="app.storeId || ''" :options="storeOptions" filterable @update:value="switchStore" aria-label="切换店铺" /></label>
      <label>人员<n-select v-model:value="personFilter" :options="[{value:'',label:'全部人员'},...personOptions]" filterable aria-label="筛选人员" /></label>
    </div>
    <div class="tools">
      <input v-model="search" class="search" placeholder="搜索宝贝名称或ID" aria-label="搜索宝贝" />
      <select v-model="state" aria-label="筛选状态"><option value="">全部状态</option><option v-for="(label,key) in states" :key="key" :value="key">{{ label }}</option></select>
      <n-button type="primary" :disabled="busy" @click="edit()">新增设置</n-button>
      <n-button @click="batchDialog.open({kind:'new',store_id:app.storeId})">批量新增</n-button>
      <n-button @click="peopleDialog.open()">人员名单</n-button>
      <n-button @click="fileInput.click()">Excel导入</n-button>
      <input ref="fileInput" type="file" accept=".xlsx" class="file-input" aria-label="导入Excel文件" @change="importFile" />
      <a href="/static/commission-template.xlsx" download="提成设置导入模板.xlsx">下载模板</a>
      <a :href="`/api/commission-v2/export/settings?${params}`">导出设置</a>
      <router-link to="/commission/reports">金额汇总 / 导出</router-link>
      <n-button text :disabled="loading" @click="load">刷新</n-button>
    </div>
    <div class="selection" v-if="rows.length || chosen.length || allScope">
      <span v-if="allScope">已选择筛选内全部可设置商品（{{ app.storeId?storeName(app.storeId):'全部店铺' }}），排除{{ allScope.excluded?.length || 0 }}个</span>
      <span v-else>已选{{ chosen.length }}个宝贝 · {{ new Set(chosen.map(r=>r.store_id)).size }}家店铺</span>
      <n-button text type="primary" v-if="!allScope && rows.length" @click="selectAll">选择全部可设置商品</n-button>
      <n-button :disabled="!chosen.length && !allScope" type="primary" @click="bulk">批量设置人员 / 比例</n-button>
      <n-button text :disabled="!chosen.length && !allScope" @click="clearSelection">清空选择</n-button>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div class="table-wrap" :aria-busy="loading">
      <table>
        <thead><tr><th class="check"><input type="checkbox" aria-label="选择本页商品" :disabled="loading || busy" :checked="!!rows.length && rows.filter(r=>!r.store_id.startsWith('unmapped:')).every(rowSelected)" @change="togglePage($event.target.checked)" /></th><th class="store">店铺</th><th class="product">商品宝贝</th><th class="state">状态</th><th class="person">所属人员</th><th class="rate">提成比率</th><th class="action"></th></tr></thead>
        <tbody><tr v-for="row in rows" :key="row.store_id+':'+row.product_id">
          <td class="check"><input type="checkbox" :disabled="loading || busy || row.store_id.startsWith('unmapped:')" :aria-label="`选择宝贝 ${row.product_id}`" :checked="rowSelected(row)" @change="toggleRow(row,$event.target.checked)"/></td><td>{{ storeName(row.store_id) }}</td>
          <td><div>{{ row.product_name || '名称待补充' }}</div><small>{{ row.product_id === '*' ? '店铺默认' : row.product_id }}</small></td>
          <td><span class="status" :class="row.state">{{ states[row.state] }}</span><small v-if="row.listed != null">{{ row.listed ? '商品在售' : '商品未在售' }}</small></td>
          <td><div v-for="p in row.people" :key="p.person_id" class="person-line">{{ p.name }}</div><span v-if="!row.people.length" class="muted">—</span></td>
          <td class="rate"><div v-for="p in row.people" :key="p.person_id" class="person-line">{{ rateText(p.rate) }}</div><span v-if="!row.people.length" class="muted">—</span></td>
          <td><n-button size="small" :disabled="loading || busy || row.store_id.startsWith('unmapped:')" @click="edit(row)">修改</n-button></td>
        </tr><tr v-if="!rows.length"><td colspan="7" class="empty">{{ loading ? '正在加载…' : '没有找到商品，可更换店铺或搜索条件。' }}</td></tr></tbody>
      </table>
    </div>
    <div class="paging"><n-button :disabled="!pages.length || loading" @click="previousPage">上一页</n-button><span>第{{ pages.length+1 }}页</span><n-button :disabled="!next || loading" @click="nextPage">下一页</n-button></div>

    <CommissionBatchDialog ref="batchDialog" :stores="app.stores" :people="people" @saved="saved" />
    <CommissionPeople ref="peopleDialog" @changed="loadPeople();load()" @assignments="showAssignments" />
    <n-modal v-model:show="showEditor" preset="card" title="设置提成" class="commission-editor" style="width:min(640px,94vw)">
      <div class="fields">
        <label>店铺<select v-model="form.store_id" :disabled="!!selected" aria-label="设置店铺"><option value="">选择店铺</option><option v-for="s in app.stores" :key="s.id" :value="s.id">{{ s.name }}</option></select></label>
        <label>宝贝ID<input v-model="form.product_id" :disabled="!!selected" aria-label="宝贝ID" /></label>
      </div>
      <label>商品名称<input v-model="form.product_name" aria-label="商品名称" /></label>
      <label>状态<select v-model="form.mode" aria-label="提成状态"><option value="distribute">提成中</option><option value="exclude">不提成</option><option value="hold">暂不设置</option></select></label>
      <template v-if="form.mode === 'distribute'">
        <div class="allocation-head"><span>所属人员</span><span>提成比率</span></div>
        <div v-for="(p,i) in form.allocations" :key="i" class="allocation-row">
          <n-select v-model:value="p.person" :options="personOptions" filterable tag placeholder="选择或输入姓名" :aria-label="`所属人员${i+1}`" />
          <label class="percentage"><input v-model="p.percent" type="number" min="0" max="100" step="0.01" :aria-label="`提成比率${i+1}`" /><span>%</span></label>
          <n-button text @click="form.allocations.splice(i,1)">移除</n-button>
        </div>
        <div class="allocation-footer"><n-button text type="primary" @click="form.allocations.push({person:null,percent:null})">＋ 添加人员</n-button><span>合计 {{ Number(total.toFixed(6)) }}%</span></div>
      </template>
      <details class="dates"><summary>生效时间 <span>{{ form.valid_from?.replace('T',' ') }}起{{ form.valid_to ? '，至'+form.valid_to.replace('T',' ') : '' }}</span></summary><div class="fields"><label>开始时间<input v-model="form.valid_from" type="datetime-local" step="1" aria-label="开始时间" /></label><label>结束时间（可留空）<input v-model="form.valid_to" type="datetime-local" step="1" aria-label="结束时间" /></label></div><small>北京时间；此前设置会保留。</small></details>
      <details v-if="selected?.versions?.length" class="history"><summary>查看修改记录</summary><div v-for="v in selected.versions" :key="v.id" class="history-item"><small>{{ new Date(v.recorded_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}) }}</small><p v-for="s in v.body.segments" :key="s.valid_from">{{ s.valid_from.replace('T',' ') }}起：{{ s.mode==='distribute' ? historicalPeople(s) : s.mode==='exclude' ? '不提成' : '暂不设置' }}{{ s.valid_to ? '（至'+s.valid_to.replace('T',' ')+ '）' : '' }}</p></div></details>
      <div class="footer"><n-button :disabled="busy" @click="showEditor=false">取消</n-button><n-button type="primary" :loading="busy" @click="save">保存</n-button></div>
    </n-modal>
  </div>
</template>

<style scoped>
.commission-page{max-width:1400px;margin:auto;padding-bottom:32px}.filters{display:flex;gap:16px;flex-wrap:wrap;margin-top:20px}.filters label{font-size:13px;color:#657080;display:block;min-width:240px;flex:1;max-width:420px}.filters :deep(.n-select){margin-top:7px}.selection{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:14px 0;font-size:13px;color:#536071}.file-input{display:none}.check{width:34px;padding-left:10px;padding-right:10px}.tools{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:22px 0 16px}.tools a{color:#2563eb;font-size:14px}.search{width:320px}.tools input,.tools select,.commission-editor input,.commission-editor select{border:1px solid #dce0e6;border-radius:7px;padding:8px 10px;background:white;font-size:14px;color:#263244;box-sizing:border-box;min-width:0}.table-wrap{overflow:auto;background:white;border:1px solid #e5e7eb;border-radius:10px}table{width:100%;min-width:740px;border-collapse:collapse;table-layout:fixed}th,td{padding:15px 18px;text-align:left;vertical-align:top;border-bottom:1px solid #eef0f3;font-size:14px;line-height:1.6;overflow-wrap:anywhere}th{background:#f8fafc;font-weight:500;color:#6b7280}.store{width:19%}.product{width:auto}.state{width:13%}.person{width:13%}.rate{width:11%;font-variant-numeric:tabular-nums;white-space:nowrap}.action{width:72px}th:last-child,td:last-child{position:sticky;right:0;background:white;padding-left:10px;padding-right:10px}th:last-child{background:#f8fafc}small{display:block;font-size:12px;color:#8a919d;margin-top:4px}.person-line{min-height:24px}.muted{color:#9ca3af}.status{font-size:12px;display:inline-block;padding:1px 8px;border-radius:4px;background:#f3f4f6;color:#687385}.status.enabled{background:#ecf7f1;color:#278056}.status.scheduled{background:#eef3ff;color:#416bb0}.empty{text-align:center;padding:45px;color:#89919d}.paging{display:flex;justify-content:flex-end;gap:14px;align-items:center;margin-top:16px;font-size:13px;color:#6b7280}.error{color:#b42318}.commission-editor label{display:block;margin:12px 0 6px;font-size:13px;color:#4b5563}.commission-editor label>input,.commission-editor label>select{display:block;width:100%;margin-top:6px}.fields{display:grid;grid-template-columns:1fr 1fr;gap:14px}.allocation-head{display:grid;grid-template-columns:1fr 125px 42px;gap:12px;margin-top:24px;color:#6b7280;font-size:13px}.allocation-row{display:grid;grid-template-columns:1fr 125px 42px;gap:12px;align-items:center;margin:10px 0}.allocation-row .percentage{display:flex;align-items:center;gap:6px;margin:0}.percentage input{width:98px!important;margin:0!important}.allocation-footer{display:flex;justify-content:space-between;align-items:center;font-size:13px;color:#6b7280}.dates,.history{border-top:1px solid #eef0f3;padding-top:16px;margin-top:20px;font-size:13px}.dates summary,.history summary{cursor:pointer;color:#677183}.dates summary span{font-size:12px;margin-left:8px;color:#8a919d}.history-item{border-bottom:1px solid #eef0f3;padding:8px 0}.history-item p{margin:5px 0;line-height:1.6}.history{max-height:260px;overflow:auto}.footer{display:flex;justify-content:flex-end;gap:10px;margin-top:25px}@media(max-width:640px){.search{width:100%}.tools{gap:10px}.fields{grid-template-columns:1fr;gap:0}.allocation-head,.allocation-row{grid-template-columns:1fr 96px 32px;gap:7px}.percentage input{width:70px!important}.dates summary span{display:block;margin:6px 0}}
</style>
