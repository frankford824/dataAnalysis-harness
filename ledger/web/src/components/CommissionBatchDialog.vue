<script setup>
import { computed, ref } from 'vue'
import { useMessage } from 'naive-ui'
import { DUTY_OPTIONS, dutyLabel } from '../storeMembers'
const props = defineProps({stores:Array, people:Array})
const batchDutyOptions=DUTY_OPTIONS.map(option=>({value:option.value,label:dutyLabel(option.value)}))
const emit = defineEmits(['saved'])
const message = useMessage()
const shown=ref(false), busy=ref(false), kind=ref('selected'), operation=ref('replace')
const selection=ref({}), shops=ref([]), text=ref(''), plan=ref(null), errors=ref([]), failure=ref(''), page=ref(0)
const form=ref({})
const classification=ref('keep'), managedTeam=ref(null), classificationOnly=ref(false)
const teamOptions=computed(()=>(props.people||[]).filter(p=>!p.archived&&!p.parent_id).map(p=>({value:p.id,label:p.alias||p.name})))
const stamp=()=>new Date().toLocaleString('sv-SE',{timeZone:'Asia/Shanghai'}).replace(' ','T')
const rate=x=>`${Number((Number(x)*100).toFixed(6))}%`
const names=a=>(a||[]).map(p=>`${p.name}${p.duty?`（${dutyLabel(p.duty)}）`:''}${p.source==='hierarchy'?' · 上级抽成':''} ${rate(p.rate)}`).join('、') || '未分配'
const personOptions=computed(()=>{
  const people=props.people||[], index=new Map(people.map(p=>[p.id,p])), groups=new Map()
  for(const person of people.filter(p=>!p.archived)){
    let root=person;const visited=new Set([root.id])
    while(root.parent_id&&index.has(root.parent_id)&&!visited.has(root.parent_id)){root=index.get(root.parent_id);visited.add(root.id)}
    if(!groups.has(root.id))groups.set(root.id,{type:'group',key:root.id,label:root.alias||root.name,children:[]})
    groups.get(root.id).children.push({value:person.id,label:person.name+(person.employee_no?`（${person.employee_no}）`:'')})
  }
  return [...groups.values()]
})
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
  classification.value='keep';managedTeam.value=null;classificationOnly.value=false
  selection.value=options;kind.value=options.kind||'selected';shops.value=options.store_id?[options.store_id]:[]
  text.value='';plan.value=null;errors.value=[];failure.value='';page.value=0;operation.value=kind.value==='selected'?'merge':'replace'
  form.value={mode:'distribute',valid_from:options.valid_from||stamp(),valid_to:'',allocations:[{person:null,percent:null,duty:'produce'}]};shown.value=true
}
function prepared() {
  if(form.value.mode!=='distribute')return []
  if(!form.value.allocations.length||form.value.allocations.some(p=>!p.person))throw new Error('请先选择人员')
  if(new Set(form.value.allocations.map(p=>p.person)).size!==form.value.allocations.length)throw new Error('同一人员只能填写一行，请合并后再预览')
  return form.value.allocations.map(p=>{
    if(!props.people.some(x=>x.id===p.person&&!x.archived))throw new Error('请从组织架构中选择有效人员；新增人员请先在组织架构登记')
    if(operation.value!=='remove'&&(p.percent==null||!Number.isFinite(Number(p.percent))||Number(p.percent)<=0))throw new Error('请填写大于0的提成比例')
    return {person_id:p.person,rate:operation.value==='remove'?'0':(Number(p.percent)/100).toFixed(8),duty:p.duty||'produce',...(p.source==='hierarchy'?{source:'hierarchy'}:{})}
  })
}
async function fillHierarchy(){
  busy.value=true;failure.value=''
  try{
    const result=await call('/org/fill-hierarchy',{method:'POST',body:JSON.stringify({allocations:prepared(),reason:'批量按组织补上级抽成'})})
    const ids=new Set(form.value.allocations.map(p=>p.person))
    for(const line of result.added||[])if(!ids.has(line.person_id)){
      form.value.allocations.push({person:line.person_id,percent:Number(line.rate)*100,duty:'cut',source:'hierarchy'});ids.add(line.person_id)
    }
    message.success(result.added?.length?`已补入 ${result.added.length} 位上级，请核对后预览`:'没有需要补入的上级抽成')
  }catch(e){failure.value=e.message}finally{busy.value=false}
}
async function preview() {
  busy.value=true;failure.value=''
  try {
    if(classificationOnly.value&&classification.value==='keep')throw new Error('请选择设为托管商品或设为非托管商品')
    if(classification.value==='managed'&&!teamOptions.value.some(t=>t.value===managedTeam.value))throw new Error('请选择有效的托管团队')
    const template={...form.value,allocations:classificationOnly.value?[]:prepared(),
      ...(classification.value==='keep'?{}:{managed:classification.value==='managed',managed_team_id:classification.value==='managed'?managedTeam.value:''})}
    let request={template,operation:classificationOnly.value?'classification':form.value.mode==='distribute'?operation.value:'replace'}
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
    try {
      const r=await call(`/settings/apply/${plan.value.id}`,{method:'POST'})
      shown.value=false;message.success(`已保存${r.count}条设置，涉及${r.stores}家店铺`);emit('saved')
    }
  catch(e){failure.value=e.message}finally{busy.value=false}
}
defineExpose({open,importFile,shown})
</script>
<template>
<n-modal v-model:show="shown" preset="card" :title="title" class="commission-modal batch-dialog" :mask-closable="!busy" :closable="!busy" :close-on-esc="!busy" style="width:min(960px,calc(100vw - 32px))">
  <p v-if="busy">{{ plan ? '正在保存，请稍候…' : '正在核对店铺、人员、比例和日期…' }}</p>
  <p v-if="failure" class="error" role="alert">{{ failure }}</p>
  <template v-if="!plan && kind!=='import'">
    <template v-if="kind==='new'"><label>店铺（可多选）</label><n-select v-model:value="shops" :options="storeOptions" multiple filterable placeholder="选择要新增设置的店铺" aria-label="批量新增店铺"/><label>宝贝ID（每行一个，可粘贴Excel两列：ID、商品名称）</label><textarea v-model="text" rows="6" aria-label="批量宝贝ID" placeholder="123456789001&#10;123456789002"/><p class="hint">以上宝贝会分别添加到每一家所选店铺，下一步可核对完整清单。</p></template>
    <p v-else class="hint">{{ selection.scope ? '处理当前筛选条件下的全部商品' : `已选${selection.targets?.length||0}个商品，涉及${new Set((selection.targets||[]).map(x=>x.store_id)).size}家店铺` }}</p>
    <section class="classification-settings">
      <n-checkbox v-if="kind!=='new'" v-model:checked="classificationOnly">仅修改托管分类，保留提成状态、人员、身份和比例</n-checkbox>
      <div class="fields"><label>商品托管分类<select v-model="classification" aria-label="批量托管分类"><option value="keep">保持原设置（新商品默认非托管）</option><option value="managed">设为托管商品</option><option value="unmanaged">设为非托管商品</option></select></label>
        <label v-if="classification==='managed'">托管团队<n-select v-model:value="managedTeam" :options="teamOptions" filterable placeholder="选择托管团队" aria-label="批量托管团队"/></label></div>
      <p class="hint">托管商品不计个人销售额，仅计入指定团队；毛利、利润和提成不变。按下方生效时间记录归属，保存前请逐项核对预览。</p>
    </section>
    <div v-if="!classificationOnly" class="fields"><label>状态<select v-model="form.mode" aria-label="批量提成状态"><option value="distribute">提成中</option><option value="exclude">不提成</option><option value="hold">暂不设置</option></select></label><label v-if="kind!=='new' && form.mode==='distribute'">处理方式<select v-model="operation" aria-label="批量处理方式"><option value="replace">统一替换全部人员和比例</option><option value="merge">只调整指定人员，保留其他人员</option><option value="remove">移除指定人员，保留其他人员</option></select></label></div>
    <template v-if="!classificationOnly && form.mode==='distribute'">
      <div class="allocation-heading"><strong>商品提成人员</strong><span>从组织架构选择，按商品设置身份与点数</span></div>
      <div v-for="(p,i) in form.allocations" :key="i" class="allocation">
        <label>人员<n-select v-model:value="p.person" :options="personOptions" filterable placeholder="按团队选择人员" :aria-label="`批量人员${i+1}`"/><small v-if="p.source==='hierarchy'">来自上级抽成</small></label>
        <label>商品身份<n-select v-model:value="p.duty" :options="batchDutyOptions" :aria-label="`身份${i+1}`"/></label>
        <label v-if="operation!=='remove'">提成点数<div class="rate-input"><input v-model="p.percent" type="number" min="0" max="100" step="0.01" :aria-label="`批量比例${i+1}`"/><span>%</span></div></label>
        <n-button text @click="form.allocations.splice(i,1)">移除</n-button>
      </div>
      <div class="allocation-actions"><n-button secondary @click="form.allocations.push({person:null,percent:null,duty:'produce'})">添加人员</n-button><n-button v-if="operation!=='remove'" secondary :loading="busy" @click="fillHierarchy">按组织补上级抽成</n-button><router-link :to="{name:'commission-org'}" @click="shown=false">维护组织人员 →</router-link></div>
      <p class="hint">本次只保存商品规则，不修改组织上下级、负责店铺或店铺默认身份。上级抽成需点击补入并核对后保存。做货归属产出；抽点只计提成。</p>
    </template>
    <div class="fields"><label>生效时间<input v-model="form.valid_from" type="datetime-local" step="1" aria-label="批量生效时间"/></label><label>结束时间（可留空）<input v-model="form.valid_to" type="datetime-local" step="1" aria-label="批量结束时间"/></label></div>
    <p v-if="!classificationOnly" class="hint">按北京时间生效，此前的人员及比例保留。{{ operation==='merge' ? '只调整指定人员，其他分配和已有后续安排保持原样。' : operation==='remove' ? '只移除指定人员，其他分配和已有后续安排保持原样。' : form.valid_to ? '指定时间段内按本次完整名单统一设置。' : '从生效时间起按本次完整名单持续生效，并覆盖已有后续安排。' }}</p>
    <p v-else class="hint">按北京时间生效，仅修改预览区间内的托管分类；不改人员、身份、比例及提成状态。填写结束时间后恢复原设置，已有后续安排保留。</p>
  </template>
  <template v-if="errors.length"><p>有{{ errors.length }}处需要修改，本次未保存任何设置。</p><div class="table-wrap"><table><thead><tr><th>位置</th><th>需要修改</th></tr></thead><tbody><tr v-for="(e,i) in errors" :key="i"><td>{{ e.row }}</td><td>{{ e.error }}</td></tr></tbody></table></div></template>
  <p class="hint">托管分类选择“保持原设置”时，不改变各商品的分类和团队。仅修改分类时，已有后续设置保持原样；以预览中的生效区间为准。</p>
  <template v-if="plan"><p class="summary">共{{ plan.count }}条设置 · {{ plan.stores }}家店铺 · 新增{{ plan.new_count }}条 · 修改{{ plan.count-plan.new_count }}条</p><n-alert v-if="plan.future_overwritten_count" type="warning" :bordered="false" style="margin-bottom:12px">本次统一替换将覆盖 {{ plan.future_overwritten_count }} 个已有后续时间段；历史记录仍保留。</n-alert><p class="hint">请核对下列店铺、宝贝、人员和比例，点击确认后统一保存。</p><div class="table-wrap"><table><thead><tr><th>店铺 / 宝贝</th><th>原人员与比例</th><th>保存后的人员与比例</th><th>生效时间</th></tr></thead><tbody><tr v-for="(r,i) in visibleRows" :key="i"><td>{{ r.store }}<br/>{{ r.product_name || '名称待补充' }}<small>{{ r.product_id }}{{ r.catalog_missing?' · 目录暂未收录':'' }}</small><small>{{r.managed ? '托管商品 · '+r.managed_team : '非托管商品'}}</small></td><td>{{ names(r.before) }}</td><td>{{ r.mode==='distribute'?names(r.after):r.mode==='exclude'?'不提成':'暂不设置' }}</td><td>{{ r.valid_from.replace('T',' ') }}<small v-if="r.valid_to">至{{ r.valid_to.replace('T',' ') }}</small><small v-if="r.future_overwritten" class="overwritten">覆盖 {{r.future_overwritten}} 个后续时间段</small></td></tr></tbody></table></div><div class="paging"><n-button :disabled="page===0" @click="page--">上一页</n-button><span>{{ page+1 }} / {{ Math.ceil(plan.count/50) }}</span><n-button :disabled="(page+1)*50>=plan.count" @click="page++">下一页</n-button></div></template>
  <template #footer><div class="footer"><n-button :disabled="busy" @click="shown=false">取消</n-button><n-button v-if="plan && kind!=='import'" :disabled="busy" @click="plan=null">返回修改</n-button><n-button v-if="!plan && kind!=='import'" type="primary" :loading="busy" @click="preview">预览修改</n-button><n-button v-if="plan" type="primary" :loading="busy" @click="apply">确认保存{{ plan.count }}条设置</n-button></div></template>
</n-modal>
</template>
<style scoped>
.classification-settings{padding:12px 16px;margin-top:12px;border:1px solid #dfe7f1;border-radius:10px;background:#f8faff}.classification-settings .n-select{margin-top:6px}
.allocation-heading{display:flex;gap:12px;align-items:baseline;margin-top:20px}.allocation-heading span{font-size:12px;color:#718097}.allocation-actions{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.allocation-actions a{font-size:12px;color:#3468f0}.allocation.allocation{grid-template-columns:minmax(180px,1fr) 180px 120px 44px;gap:12px;padding:12px;background:#f8fafc;border:1px solid #e5eaf1;border-radius:10px}.allocation.allocation label{display:block;margin:0;font-size:12px}.allocation .n-select{margin-top:6px}.rate-input{display:flex;align-items:center;gap:6px;margin-top:6px}.rate-input input{min-width:0;width:100%}.footer.footer{margin:0}.allocation .n-button{align-self:end;margin-bottom:8px}@media(max-width:640px){.allocation.allocation{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}.allocation-heading{display:block}.allocation.allocation label:first-child{grid-column:1/-1}.allocation.allocation .n-button{grid-column:2;justify-self:end}}
label{display:block;font-size:13px;color:#536071;margin:14px 0 6px}input,select,textarea{box-sizing:border-box;border:1px solid #dce0e6;border-radius:6px;padding:8px;background:white;font-size:14px;max-width:100%}label>select,label>input,textarea{display:block;width:100%;margin-top:6px}.fields{display:grid;grid-template-columns:1fr 1fr;gap:16px}.allocation{display:grid;grid-template-columns:minmax(150px,1fr) 100px 130px 44px;gap:14px;align-items:center;margin:12px 0}.allocation label{display:flex;align-items:center;margin:0;gap:6px}.allocation input{width:105px;margin:0}.hint,small{color:#7b8490;font-size:12px;line-height:1.7}small{display:block}.overwritten{color:#a15c00}.error{color:#b42318}.summary{font-weight:600}.table-wrap{max-height:52vh;overflow:auto;border:1px solid #e5e7eb;border-radius:8px}table{border-collapse:collapse;width:100%;min-width:640px}td,th{padding:12px;text-align:left;border-bottom:1px solid #eef0f3;vertical-align:top;font-size:13px;overflow-wrap:anywhere}th{background:#f8fafc;position:sticky;top:0}.footer,.paging{display:flex;justify-content:flex-end;align-items:center;gap:12px;margin-top:18px}.paging{font-size:12px;color:#7b8490}@media(max-width:640px){.fields{grid-template-columns:1fr;gap:0}.allocation{grid-template-columns:minmax(110px,1fr) 84px 92px 32px;gap:8px}.allocation input{width:70px;margin:0}}
</style>
