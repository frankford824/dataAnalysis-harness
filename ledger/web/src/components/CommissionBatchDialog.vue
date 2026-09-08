<script setup>
import { computed, ref } from 'vue'
import { useMessage } from 'naive-ui'
const props = defineProps({stores:Array, people:Array})
const emit = defineEmits(['saved'])
const message = useMessage()
const shown=ref(false), busy=ref(false), kind=ref('selected'), operation=ref('replace')
const selection=ref({}), shops=ref([]), text=ref(''), plan=ref(null), errors=ref([]), failure=ref(''), page=ref(0)
const form=ref({})
const stamp=()=>new Date().toLocaleString('sv-SE',{timeZone:'Asia/Shanghai'}).replace(' ','T')
const rate=x=>`${Number((Number(x)*100).toFixed(6))}%`
const names=a=>(a||[]).map(p=>`${p.name} ${rate(p.rate)}`).join('、') || '未分配'
const personOptions=computed(()=>props.people.map(p=>({value:p.id,label:p.name+(p.employee_no?`（${p.employee_no}）`:'')})))
const storeOptions=computed(()=>props.stores.map(s=>({value:s.id,label:s.name})))
const visibleRows=computed(()=>plan.value?.rows.slice(page.value*50,(page.value+1)*50)||[])
const title=computed(()=>plan.value?'确认本次提成设置':kind.value==='import'?'Excel导入':kind.value==='new'?'批量新增':'批量设置')
async function call(path, options={}) {
  const response=await fetch('/api/commission-v2'+path,{...options,headers:options.body instanceof FormData?{}:{'Content-Type':'application/json'}})
  const body=await response.json()
  if(!response.ok)throw new Error(typeof body.detail==='string'?body.detail:'未能处理，请检查填写内容')
  return body
}
function open(options={}) {
  selection.value=options;kind.value=options.kind||'selected';shops.value=options.store_id?[options.store_id]:[]
  text.value='';plan.value=null;errors.value=[];failure.value='';page.value=0;operation.value=kind.value==='selected'?'merge':'replace'
  form.value={mode:'distribute',valid_from:stamp(),valid_to:'',allocations:[{person:null,percent:null}]};shown.value=true
}
function prepared() {
  if(form.value.mode!=='distribute')return []
  if(!form.value.allocations.length||form.value.allocations.some(p=>!p.person))throw new Error('请先选择人员')
  return form.value.allocations.map(p=>{
    if(operation.value!=='remove'&&(p.percent==null||!Number.isFinite(Number(p.percent))||Number(p.percent)<=0))throw new Error('请填写大于0的提成比例')
    return {...(props.people.some(x=>x.id===p.person)?{person_id:p.person}:{name:p.person}),rate:operation.value==='remove'?'0':(Number(p.percent)/100).toFixed(8)}
  })
}
async function preview() {
  busy.value=true;failure.value=''
  try {
    const template={...form.value,allocations:prepared()}
    let request={template,operation:form.value.mode==='distribute'?operation.value:'replace'}
    if(kind.value==='new') {
      if(!shops.value.length||!text.value.trim())throw new Error('请选择店铺并填写宝贝ID')
      const products=text.value.trim().split(/\r?\n/).filter(x=>x.trim()).map(line=>{const [id,...rest]=line.trim().split(/\t|,/);return {product_id:id.trim(),product_name:rest.join(' ').trim()}})
      request={changes:shops.value.flatMap(store_id=>products.map(p=>({...template,...p,store_id}))),operation:'replace'}
    } else if(selection.value.scope)request.scope=selection.value.scope
    else request.targets=selection.value.targets
    plan.value=await call('/settings/preview',{method:'POST',body:JSON.stringify(request)});page.value=0
  } catch(e){failure.value=e.message} finally {busy.value=false}
}
async function importFile(file) {
  if(!file)return
  open({kind:'import'});busy.value=true
  try { const data=new FormData();data.append('file',file);const result=await call('/settings/import-preview',{method:'POST',body:data});if(result.errors)errors.value=result.errors;else plan.value=result }
  catch(e){failure.value=e.message}finally{busy.value=false}
}
async function apply() {
  busy.value=true;failure.value=''
  try {const r=await call(`/settings/apply/${plan.value.id}`,{method:'POST'});shown.value=false;message.success(`已保存${r.count}条设置，涉及${r.stores}家店铺`);emit('saved')}
  catch(e){failure.value=e.message}finally{busy.value=false}
}
defineExpose({open,importFile,shown})
</script>
<template>
<n-modal v-model:show="shown" preset="card" :title="title" class="batch-dialog" :mask-closable="!busy" style="width:min(1080px,95vw)">
  <p v-if="busy">{{ plan ? '正在保存，请稍候…' : '正在核对店铺、人员、比例和日期…' }}</p>
  <p v-if="failure" class="error" role="alert">{{ failure }}</p>
  <template v-if="!plan && kind!=='import'">
    <template v-if="kind==='new'"><label>店铺（可多选）</label><n-select v-model:value="shops" :options="storeOptions" multiple filterable placeholder="选择要新增设置的店铺" aria-label="批量新增店铺"/><label>宝贝ID（每行一个，可粘贴Excel两列：ID、商品名称）</label><textarea v-model="text" rows="6" aria-label="批量宝贝ID" placeholder="123456789001&#10;123456789002"/><p class="hint">以上宝贝会分别添加到每一家所选店铺，下一步可核对完整清单。</p></template>
    <p v-else class="hint">{{ selection.scope ? '处理当前筛选条件下的全部商品' : `已选${selection.targets?.length||0}个商品，涉及${new Set((selection.targets||[]).map(x=>x.store_id)).size}家店铺` }}</p>
    <div class="fields"><label>状态<select v-model="form.mode" aria-label="批量提成状态"><option value="distribute">提成中</option><option value="exclude">不提成</option><option value="hold">暂不设置</option></select></label><label v-if="kind!=='new' && form.mode==='distribute'">处理方式<select v-model="operation" aria-label="批量处理方式"><option value="replace">统一替换全部人员和比例</option><option value="merge">只调整指定人员，保留其他人员</option><option value="remove">移除指定人员，保留其他人员</option></select></label></div>
    <template v-if="form.mode==='distribute'"><div v-for="(p,i) in form.allocations" :key="i" class="allocation"><n-select v-model:value="p.person" :options="personOptions" filterable tag placeholder="选择或输入姓名" :aria-label="`批量人员${i+1}`"/><label v-if="operation!=='remove'"><input v-model="p.percent" type="number" min="0" max="100" step="0.01" :aria-label="`批量比例${i+1}`"/> %</label><n-button text @click="form.allocations.splice(i,1)">移除</n-button></div><n-button text type="primary" @click="form.allocations.push({person:null,percent:null})">＋ 添加人员</n-button></template>
    <div class="fields"><label>生效时间<input v-model="form.valid_from" type="datetime-local" step="1" aria-label="批量生效时间"/></label><label>结束时间（可留空）<input v-model="form.valid_to" type="datetime-local" step="1" aria-label="批量结束时间"/></label></div>
    <p class="hint">按北京时间生效，此前的人员及比例保留。{{ operation==='merge' ? '只调整指定人员，其他分配保持原样。' : operation==='remove' ? '只移除指定人员，其他分配保持原样。' : '新时间段内按本次完整名单统一设置。' }}</p>
  </template>
  <template v-if="errors.length"><p>有{{ errors.length }}处需要修改，本次未保存任何设置。</p><div class="table-wrap"><table><thead><tr><th>位置</th><th>需要修改</th></tr></thead><tbody><tr v-for="(e,i) in errors" :key="i"><td>{{ e.row }}</td><td>{{ e.error }}</td></tr></tbody></table></div></template>
  <template v-if="plan"><p class="summary">共{{ plan.count }}条设置 · {{ plan.stores }}家店铺 · 新增{{ plan.new_count }}条 · 修改{{ plan.count-plan.new_count }}条</p><p class="hint">请核对下列店铺、宝贝、人员和比例，点击确认后统一保存。</p><div class="table-wrap"><table><thead><tr><th>店铺 / 宝贝</th><th>原人员与比例</th><th>保存后的人员与比例</th><th>生效时间</th></tr></thead><tbody><tr v-for="(r,i) in visibleRows" :key="i"><td>{{ r.store }}<br/>{{ r.product_name || '名称待补充' }}<small>{{ r.product_id }}{{ r.catalog_missing?' · 目录暂未收录':'' }}</small></td><td>{{ names(r.before) }}</td><td>{{ r.mode==='distribute'?names(r.after):r.mode==='exclude'?'不提成':'暂不设置' }}</td><td>{{ r.valid_from.replace('T',' ') }}<small v-if="r.valid_to">至{{ r.valid_to.replace('T',' ') }}</small></td></tr></tbody></table></div><div class="paging"><n-button :disabled="page===0" @click="page--">上一页</n-button><span>{{ page+1 }} / {{ Math.ceil(plan.count/50) }}</span><n-button :disabled="(page+1)*50>=plan.count" @click="page++">下一页</n-button></div></template>
  <div class="footer"><n-button :disabled="busy" @click="shown=false">取消</n-button><n-button v-if="plan && kind!=='import'" :disabled="busy" @click="plan=null">返回修改</n-button><n-button v-if="!plan && kind!=='import'" type="primary" :loading="busy" @click="preview">预览修改</n-button><n-button v-if="plan" type="primary" :loading="busy" @click="apply">确认保存{{ plan.count }}条设置</n-button></div>
</n-modal>
</template>
<style scoped>
label{display:block;font-size:13px;color:#536071;margin:14px 0 6px}input,select,textarea{box-sizing:border-box;border:1px solid #dce0e6;border-radius:6px;padding:8px;background:white;font-size:14px;max-width:100%}label>select,label>input,textarea{display:block;width:100%;margin-top:6px}.fields{display:grid;grid-template-columns:1fr 1fr;gap:16px}.allocation{display:grid;grid-template-columns:minmax(150px,1fr) 130px 44px;gap:14px;align-items:center;margin:12px 0}.allocation label{display:flex;align-items:center;margin:0;gap:6px}.allocation input{width:105px;margin:0}.hint,small{color:#7b8490;font-size:12px;line-height:1.7}small{display:block}.error{color:#b42318}.summary{font-weight:600}.table-wrap{max-height:52vh;overflow:auto;border:1px solid #e5e7eb;border-radius:8px}table{border-collapse:collapse;width:100%;min-width:640px}td,th{padding:12px;text-align:left;border-bottom:1px solid #eef0f3;vertical-align:top;font-size:13px;overflow-wrap:anywhere}th{background:#f8fafc;position:sticky;top:0}.footer,.paging{display:flex;justify-content:flex-end;align-items:center;gap:12px;margin-top:18px}.paging{font-size:12px;color:#7b8490}@media(max-width:640px){.fields{grid-template-columns:1fr;gap:0}.allocation{grid-template-columns:minmax(110px,1fr) 92px 32px;gap:8px}.allocation input{width:70px}}
</style>
