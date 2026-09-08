<script setup>
import { computed, h, ref, watch, onDeactivated } from 'vue'
import { storeToRefs } from 'pinia'
import { onBeforeRouteLeave } from 'vue-router'
import { useMessage, useDialog, NButton, NTag } from 'naive-ui'
import { ChevronDown } from '@lucide/vue'
import LedgerTabs from '../components/ui/LedgerTabs.vue'
import LedgerTable from '../components/ui/LedgerTable.vue'
import { useApp } from '../store'
import { useCommission } from '../commissionStore'
import { useCommissionQuery } from '../components/useCommissionQuery'
import { commissionRequest } from '../components/commissionRequest'
import CommissionBatchDialog from '../components/CommissionBatchDialog.vue'
import CommissionPeople from '../components/CommissionPeople.vue'

const app = useApp()
const shared = useCommission()
const { people, settingsSearch:search, settingsState:state } = storeToRefs(shared)
const message = useMessage()
const dialog=useDialog()
const editorLoading=ref(false),editorError=ref(''),editorOriginal=ref(''),editorRow=ref(null)
let editorController,editorSerial=0
const after = ref('')
const pages = ref([])
const busy = ref(false)
const showEditor = ref(false)
const selected = ref(null)
const checked = ref({})
const allScope = ref(null)
const batchDialog = ref(null)
const peopleDialog = ref(null)
const fileInput = ref(null)
const keyOf = row => row.store_id + ':' + row.product_id
const chosen = computed(() => Object.values(checked.value))
function rowSelected(row){return allScope.value?!((allScope.value.excluded||[]).includes(keyOf(row))):!!checked.value[keyOf(row)]}
function toggleRow(row, value) {
  if(allScope.value){const excluded=new Set(allScope.value.excluded||[]);if(value)excluded.delete(keyOf(row));else excluded.add(keyOf(row));allScope.value={...allScope.value,excluded:[...excluded]};return}
  const next={...checked.value};if(value)next[keyOf(row)]={store_id:row.store_id,product_id:row.product_id,product_name:row.product_name,revision:row.revision||0};else delete next[keyOf(row)];checked.value=next
}
function togglePage(value) { for(const row of rows.value.filter(r=>!r.store_id.startsWith('unmapped:')))toggleRow(row,value) }
function clearSelection(){checked.value={};allScope.value=null}
function selectAll(){checked.value={};allScope.value={...shared.scope,search:search.value,state:state.value}}
function bulk(){batchDialog.value.open(allScope.value?{scope:{...allScope.value}}:{targets:chosen.value})}
async function saved(){clearSelection();await shared.changed();load()}
function showAssignments(id){clearSelection();shared.personIds=[id];shared.storeIds=[];state.value='';search.value=''}
function importFile(event){const file=event.target.files?.[0];event.target.value='';batchDialog.value.importFile(file)}
const form = ref({ allocations: [] })
const states = { enabled:'提成中', disabled:'不提成', pending:'未设置', scheduled:'待生效', expired:'已到期' }
const now = () => new Date().toLocaleString('sv-SE', { timeZone:'Asia/Shanghai' }).replace(' ', 'T')
const storeName = id => app.stores.find(s => s.id === id)?.name || '店铺待确认'
const rateText = rate => `${Number((Number(rate) * 100).toFixed(6))}%`
const personOptions = computed(() => people.value.map(p => ({ value:p.id, label:p.name + (p.employee_no ? `（${p.employee_no}）` : '') })))
const total = computed(() => form.value.allocations.reduce((sum, p) => sum + (Number(p.percent) || 0), 0))
const params = computed(() => {
  const query = new URLSearchParams({ search:search.value, state:state.value })
  for(const id of shared.storeIds) query.append('store_ids',id)
  for(const id of shared.personIds) query.append('person_ids',id)
  return query.toString()
})
const {data,error,loading,stale,load} = useCommissionQuery('settings', () => `${params.value}&after=${encodeURIComponent(after.value)}`,
  signal => commissionRequest(`/settings?${params.value}&after=${encodeURIComponent(after.value)}`,{signal}),
  () => !busy.value && !showEditor.value && !batchDialog.value?.shown && !peopleDialog.value?.shown)
const rows = computed(() => data.value?.rows || [])
const next = computed(() => data.value?.next_after || '')
const locked = computed(() => loading.value || stale.value || busy.value)
const menuOptions = [{label:'批量新增',key:'new'},{label:'人员名单',key:'people'},{type:'divider',key:'line'},
  {label:'下载模板',key:'template'},{label:'导出设置',key:'export'}]
const otherStates = [{label:'待生效',key:'scheduled'},{label:'已到期',key:'expired'}]
function menu(key) {
  if(key==='new')batchDialog.value.open({kind:'new',store_id:shared.storeIds.length===1?shared.storeIds[0]:''})
  if(key==='people')peopleDialog.value.open()
  if(key==='import')fileInput.value.click()
  if(key==='template'||key==='export'){
    const a=document.createElement('a');a.href=key==='template'?'/static/commission-template.xlsx':`/api/commission-v2/export/settings?${params.value}`
    document.body.appendChild(a);a.click();a.remove()
  }
}
async function call(path, options = {}) {
  const r = await fetch(`/api/commission-v2${path}`, { ...options, headers:{'Content-Type':'application/json'} })
  const body = await r.json()
  if (!r.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '未能保存，请检查填写内容')
  return body
}
function reset() { allScope.value=null; after.value = ''; pages.value = [] }
function nextPage() { pages.value.push(after.value); after.value = next.value }
function previousPage() { after.value = pages.value.pop() || '' }
watch(params, reset)
async function edit(row = {}) {
  editorController?.abort();editorController=new AbortController()
  const ticket=++editorSerial;editorRow.value=row;editorError.value='';editorLoading.value=true;busy.value=true;showEditor.value=true
  form.value={store_id:row.store_id||'',product_id:row.product_id||'',product_name:row.product_name||'',allocations:[]}
  try {
    const fetched=row.scheme_id?await call(`/schemes/${row.scheme_id}`,{signal:editorController.signal}):null
    if(ticket!==editorSerial)return
    selected.value=fetched
    const version=selected.value?.versions?.find(v=>v.id===selected.value.active_version)
    const segments=version?.body?.segments||[]
    const stamp=now()
    const current=segments.find(p=>p.valid_from<=stamp&&(!p.valid_to||stamp<p.valid_to))||segments.find(p=>p.valid_from>stamp)||row.setting||{}
    const grouped=new Map()
    for(const p of current.allocations||[])grouped.set(p.person_id,(grouped.get(p.person_id)||0)+Number(p.rate))
    const allocations=selected.value?[...grouped].map(([person,rate])=>({person,percent:Number((rate*100).toFixed(8))})):(row.people||[]).map(p=>({person:p.person_id,percent:Number((Number(p.rate)*100).toFixed(8))}))
    form.value={store_id:row.store_id||(shared.storeIds.length===1?shared.storeIds[0]:''),product_id:row.product_id||'',product_name:selected.value?.product_name||row.product_name||'',
      mode:current.mode||'distribute',valid_from:current.valid_from>stamp?current.valid_from:stamp,valid_to:current.valid_to>stamp?current.valid_to:'',allocations}
    if(!form.value.allocations.length)form.value.allocations.push({person:null,percent:null})
    editorOriginal.value=JSON.stringify(form.value)
  }catch(e){if(ticket===editorSerial&&e.name!=='AbortError')editorError.value=e.message}
  finally{if(ticket===editorSerial){busy.value=false;editorLoading.value=false}}
}
function canDiscard(){
  if(!showEditor.value||editorLoading.value||editorError.value||editorOriginal.value===JSON.stringify(form.value))return Promise.resolve(true)
  return new Promise(resolve=>dialog.warning({title:'修改尚未保存',content:'关闭后，本次修改将丢失。',positiveText:'关闭',negativeText:'继续编辑',onPositiveClick:()=>resolve(true),onNegativeClick:()=>resolve(false),onClose:()=>resolve(false),onMaskClick:()=>resolve(false)}))
}
async function closeEditor(show){
  if(show||busy.value&&!editorLoading.value)return
  if(await canDiscard()){editorSerial++;editorController?.abort();showEditor.value=false;busy.value=false;editorLoading.value=false}
}
onBeforeRouteLeave(async()=>{if(busy.value&&!editorLoading.value)return false;return canDiscard()})
onDeactivated(()=>{editorSerial++;editorController?.abort();showEditor.value=false;editorLoading.value=false;busy.value=false})
async function save() {
  if(busy.value||editorLoading.value||editorError.value)return
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
    await shared.changed();load()
  } catch(e) { message.error(e.message, {duration:5000}) }
  finally { busy.value = false }
}
function historicalPeople(segment) {
  return (segment.allocations || []).map(a => `${people.value.find(p => p.id === a.person_id)?.name || '原登记人员'} ${rateText(a.rate)}`).join('、')
}

const visibleChecked=computed(()=>rows.value.filter(rowSelected).map(keyOf))
function checkTableRows(keys){const selected=new Set(keys);for(const row of rows.value)if(!row.store_id.startsWith('unmapped:'))toggleRow(row,selected.has(keyOf(row)))}
const tableColumns=computed(()=>[
  {type:'selection',width:42,mobileWidth:32,disabled:row=>locked.value||row.store_id.startsWith('unmapped:')},
  {title:'商品',key:'product',minWidth:230,mobileWidth:140,render:row=>h('div',[
    h('div',{class:'table-product'},row.product_name||'未填写商品名称'),h('div',{class:'table-secondary'},row.product_id==='*'?'店铺通用':row.product_id),h('div',{class:'table-secondary table-mobile-only'},`${storeName(row.store_id)} · ${states[row.state]}`)])},
  {title:'店铺',key:'store',width:210,mobile:false,render:row=>storeName(row.store_id)},
  {title:'所属人员 / 比例',key:'people',width:195,mobileWidth:105,render:row=>row.people.length?row.people.map(p=>h('div',{class:'table-assignee'},[h('span',p.name),h('strong',rateText(p.rate))])):h('span',{class:'table-secondary'},'未分配')},
  {title:'状态',key:'state',width:100,mobile:false,render:row=>h(NTag,{size:'small',bordered:false,type:row.state==='enabled'?'success':row.state==='pending'?'warning':'default'},()=>states[row.state])},
  {title:'操作',key:'action',width:74,mobileWidth:56,fixed:'right',render:row=>h(NButton,{text:true,type:'primary',size:'small',disabled:locked.value||row.store_id.startsWith('unmapped:'),onClick:()=>edit(row)},()=> '修改')},
])
defineExpose({edit,menu,busy})
</script>

<template>
  <div class="commission-content" @dragover.prevent @drop.stop.prevent="batchDialog?.importFile($event.dataTransfer.files?.[0])">
    <div class="commission-toolbar">
      <LedgerTabs v-model="state" appearance="segment" label="商品状态" :options="[{key:'',label:'全部'},{key:'enabled',label:'提成中'},{key:'pending',label:'未设置'},{key:'disabled',label:'不提成'},{key:'scheduled',label:'待生效'},{key:'expired',label:'已到期'}]" />
      <span class="spacer" />
      <n-dropdown trigger="click" :options="menuOptions" @select="menu"><n-button :disabled="busy || !shared.ready">更多操作 <ChevronDown :size="14" style="margin-left:6px" aria-hidden="true"/></n-button></n-dropdown>
      <input ref="fileInput" type="file" accept=".xlsx" class="file-input" aria-label="导入表格" @change="importFile" />
    </div>
    <div v-if="chosen.length || allScope" class="commission-selection">
      <span>{{ allScope ? '已选全部符合条件的商品' : `已选 ${chosen.length} 件商品` }}</span><span v-if="allScope?.excluded?.length" class="selection-note">已排除 {{ allScope.excluded.length }} 件</span>
      <n-button size="small" type="primary" :disabled="locked" @click="bulk">批量修改</n-button>
      <button v-if="!allScope && rows.length" class="text-button" :disabled="locked" @click="selectAll">选择全部符合条件的商品</button>
      <button class="text-button" @click="clearSelection">取消选择</button>
    </div>
    <div v-if="error" class="commission-error" role="alert">{{ error }}<button class="text-button" @click="load">重试</button></div>
    <div v-if="loading" class="commission-loading-line" />
    <LedgerTable :rows="rows" :columns="tableColumns" :row-key="keyOf" :loading="loading" :checked-keys="visibleChecked" :max-height="520" empty="没有找到商品，可调整筛选条件" @update:checked-keys="checkTableRows" />
    <div class="commission-paging"><span class="row-count">本页 {{ rows.length }} 件商品</span><n-button size="small" :disabled="!pages.length || locked" @click="previousPage">上一页</n-button><span>{{ pages.length+1 }}</span><n-button size="small" :disabled="!next || locked" @click="nextPage">下一页</n-button></div>

    <CommissionBatchDialog ref="batchDialog" :stores="app.stores" :people="people" @saved="saved" />
    <CommissionPeople ref="peopleDialog" @changed="shared.changed();load()" @assignments="showAssignments" />
    <n-drawer :show="showEditor" :width="'min(540px,100vw)'" :mask-closable="!busy || editorLoading" :close-on-esc="!busy || editorLoading" @update:show="closeEditor"><n-drawer-content :title="editorRow?.scheme_id?'修改提成':'新增设置'" closable class="commission-editor">
      <div v-if="editorRow?.scheme_id" class="commission-edit-context"><strong>{{form.product_name || '未填写商品名称'}}</strong><p>{{storeName(form.store_id)}} · {{form.product_id}}</p></div>
      <n-alert v-if="editorError" type="error" :bordered="false">{{editorError}} <n-button text @click="edit(editorRow)">重试</n-button></n-alert>
      <n-skeleton v-if="editorLoading" text :repeat="5" />
      <template v-else-if="!editorError">
      <div v-if="!selected" class="fields">
        <label>店铺<select v-model="form.store_id" :disabled="!!selected" aria-label="设置店铺"><option value="">选择店铺</option><option v-for="s in app.stores" :key="s.id" :value="s.id">{{ s.name }}</option></select></label>
        <label>宝贝ID<input v-model="form.product_id" :disabled="!!selected" aria-label="宝贝ID" /></label>
      </div>
      <label v-if="!selected">商品名称<input v-model="form.product_name" aria-label="商品名称" /></label>
      <details v-else class="editor-product-details"><summary>商品信息</summary><label>商品名称<input v-model="form.product_name" aria-label="商品名称"/></label></details>
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
      </template><template #footer><n-space justify="end"><n-button :disabled="busy&&!editorLoading" @click="closeEditor(false)">取消</n-button><n-button type="primary" :disabled="editorLoading||!!editorError" :loading="busy&&!editorLoading" @click="save">保存</n-button></n-space></template>
    </n-drawer-content></n-drawer>
  </div>
</template>

<style scoped>
.commission-edit-context{padding:0 0 18px;border-bottom:1px solid #e8edf4;margin-bottom:18px}.commission-edit-context strong{font-size:15px;line-height:1.7}.commission-edit-context p{font-size:12px;color:#8290a3;margin-top:7px}.editor-product-details{font-size:12px;color:#7e8b9c;margin-bottom:16px}.editor-product-details summary{cursor:pointer}

.setting-person{display:flex;justify-content:space-between;gap:16px;line-height:1.85}.setting-person strong{font-weight:500;font-variant-numeric:tabular-nums}.empty-reset{display:block;margin:8px auto 0}.file-input{display:none}
.commission-editor input,.commission-editor select{border:1px solid #dce2eb;border-radius:6px;padding:8px 10px;background:white;font-size:14px;color:#263244;box-sizing:border-box;min-width:0}.commission-editor label{display:block;margin:12px 0 6px;font-size:13px;color:#536071}.commission-editor label>input,.commission-editor label>select{display:block;width:100%;margin-top:6px}.commission-editor small{display:block;font-size:12px;color:#8a919d;margin-top:4px}.fields{display:grid;grid-template-columns:1fr 1fr;gap:14px}.allocation-head{display:grid;grid-template-columns:1fr 125px 42px;gap:12px;margin-top:24px;color:#6b7280;font-size:13px}.allocation-row{display:grid;grid-template-columns:1fr 125px 42px;gap:12px;align-items:center;margin:10px 0}.allocation-row .percentage{display:flex;align-items:center;gap:6px;margin:0}.percentage input{width:98px!important;margin:0!important}.allocation-footer{display:flex;justify-content:space-between;align-items:center;font-size:13px;color:#6b7280}.dates,.history{border-top:1px solid #eef0f3;padding-top:16px;margin-top:20px;font-size:13px}.dates summary,.history summary{cursor:pointer;color:#677183}.dates summary span{font-size:12px;margin-left:8px;color:#8a919d}.history-item{border-bottom:1px solid #eef0f3;padding:8px 0}.history-item p{margin:5px 0;line-height:1.6}.history{max-height:260px;overflow:auto}.footer{display:flex;justify-content:flex-end;gap:10px;margin-top:25px}
@media(max-width:640px){.fields{grid-template-columns:1fr;gap:0}.allocation-head,.allocation-row{grid-template-columns:1fr 96px 32px;gap:7px}.percentage input{width:70px!important}.dates summary span{display:block;margin:6px 0}}
</style>
