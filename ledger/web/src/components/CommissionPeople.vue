<script setup>
import { ref, computed, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useMessage } from 'naive-ui'
import { useCommission } from '../commissionStore'
import { DUTY_OPTIONS, dutyLabel, loadStoreMembers, saveStoreMembers } from '../storeMembers'
const emit=defineEmits(['changed','assignments'])
const shared=useCommission()
const { storeOptions }=storeToRefs(shared)
const message=useMessage(),shown=ref(false),busy=ref(false),rows=ref([]),name=ref(''),employee=ref(''),error=ref('')
const tab=ref('people')

async function call(path,options={}) {const r=await fetch('/api/commission-v2'+path,{...options,headers:{'Content-Type':'application/json'}});const b=await r.json();if(!r.ok)throw new Error(typeof b.detail==='string'?b.detail:'保存失败');return b}
async function load(){rows.value=(await call('/people/summary')).people}
async function open(nextTab='people'){shown.value=true;tab.value=nextTab==='identity'?'identity':'people';error.value='';busy.value=true;try{await load();if(tab.value==='identity'&&!memberStoreIds.value.length&&shared.storeIds.length){memberStoreIds.value=[...shared.storeIds];await loadMembers()}}catch(e){error.value=e.message}finally{busy.value=false}}
async function save(person=null){busy.value=true;error.value='';try{await call('/people',{method:'POST',body:JSON.stringify({person:person||{name:name.value,employee_no:employee.value},expected_revision:person?.revision||0,reason:person?'修改人员姓名或工号':'新增提成人员'})});name.value='';employee.value='';await load();emit('changed');message.success('人员名单已保存')}catch(e){error.value=e.message}finally{busy.value=false}}
function assignments(person){shown.value=false;emit('assignments',person.id)}

// --- 本店身份 ---
const memberStoreIds=ref([])
const memberAt=ref('')
const memberFrom=ref('')
const memberTo=ref('')
const memberLoading=ref(false)
const memberError=ref('')
const members=ref([])
const memberSaving=ref({})
const leaderOptions=computed(()=>members.value.map(m=>({label:m.person_name,value:m.person_id})))
const multiStore=computed(()=>memberStoreIds.value.length>1)
const dutyTimes=computed(()=>({valid_from:memberFrom.value||memberAt.value||'',valid_to:memberTo.value||''}))
function segmentText(row){
  if(!row?.valid_from)return ''
  const end=row.valid_to?row.valid_to.replace('T',' '):'持续'
  return `${row.valid_from.replace('T',' ')} 起，至 ${end}`
}

function mergeMembers(raw){
  const byPerson=new Map()
  for(const m of raw){
    const current=byPerson.get(m.person_id)||{person_id:m.person_id,person_name:m.person_name,duties:new Set(),leaders:new Set(),confirmed:false,segments:[]}
    current.duties.add(m.duty||m.suggested_duty||'produce')
    if(m.leader_id)current.leaders.add(m.leader_id)
    current.confirmed=current.confirmed||!!m.confirmed
    current.person_name=current.person_name||m.person_name
    current.segments=[...(current.segments||[]),...(m.segments||[])]
    byPerson.set(m.person_id,current)
  }
  return [...byPerson.values()].map(m=>({
    person_id:m.person_id,person_name:m.person_name,
    _duty:m.duties.size===1?[...m.duties][0]:'produce',
    _leader_id:m.leaders.size===1?[...m.leaders][0]:null,
    confirmed:m.confirmed,
    mixed:m.duties.size>1,
    segments:m.segments,
  }))
}

async function loadMembers(){
  if(!memberStoreIds.value.length)return
  memberLoading.value=true;memberError.value=''
  try{
    members.value=mergeMembers(await loadStoreMembers(memberStoreIds.value, memberAt.value||memberFrom.value||''))
  }catch(e){memberError.value=e.message;members.value=[]}
  finally{memberLoading.value=false}
}

watch([memberStoreIds,memberAt],()=>{members.value=[];loadMembers()},{deep:true})

async function saveMember(m){
  const key=m.person_id;memberSaving.value={...memberSaving.value,[key]:true}
  try{
    const result=await saveStoreMembers(memberStoreIds.value,[{person_id:m.person_id,duty:m._duty,leader_id:m._leader_id||''}],'设置本店身份',dutyTimes.value)
    message.success(multiStore.value?`${m.person_name} 已写入 ${result.stores} 家店`:`${m.person_name} 身份已保存`)
    await loadMembers()
  }catch(e){message.error(e.message,{duration:4000})}
  finally{const next={...memberSaving.value};delete next[key];memberSaving.value=next}
}

const batchSaving=ref(false)
function setAllDuty(duty){members.value.forEach(m=>{m._duty=duty;m.mixed=false})}
async function saveAll(){
  if(!members.value.length||batchSaving.value)return
  batchSaving.value=true
  try{
    const result=await saveStoreMembers(memberStoreIds.value,members.value.map(m=>({person_id:m.person_id,duty:m._duty,leader_id:m._leader_id||''})),'批量设置本店身份',dutyTimes.value)
    message.success(`已保存 ${members.value.length} 人 × ${result.stores} 家店`)
    await loadMembers();emit('changed')
  }catch(e){message.error(e.message,{duration:4000})}
  finally{batchSaving.value=false}
}

defineExpose({open,shown})
</script>
<template><n-modal v-model:show="shown" preset="card" title="人员名单与分配" style="width:min(860px,95vw)">
  <div class="tab-bar">
    <button :class="['tab-item',{active:tab==='people'}]" @click="tab='people'">人员名单</button>
    <button :class="['tab-item',{active:tab==='identity'}]" @click="tab='identity'">店铺默认身份</button>
  </div>

  <!-- 人员名单 -->
  <template v-if="tab==='people'">
    <p class="hint">统一维护名单；点"查看分配"可查看该人员在各店铺的商品和比例。</p>
    <div class="add"><input v-model="name" placeholder="姓名" aria-label="新增人员姓名"/><input v-model="employee" placeholder="工号（可空）" aria-label="新增人员工号"/><n-button type="primary" :disabled="!name.trim()" :loading="busy" @click="save()">添加人员</n-button></div>
    <p v-if="error" class="error">{{ error }}</p>
    <div class="table-wrap"><table><thead><tr><th>姓名</th><th>工号</th><th>当前分配</th><th></th></tr></thead><tbody><tr v-for="p in rows" :key="p.id"><td><input v-model="p.name" :aria-label="`人员姓名 ${p.id}`"/></td><td><input v-model="p.employee_no" :aria-label="`人员工号 ${p.id}`"/></td><td>{{ p.stores }}家店铺 · {{ p.products }}个宝贝</td><td><n-button size="small" :disabled="busy" @click="save(p)">保存</n-button> <n-button size="small" :disabled="busy" @click="assignments(p)">查看分配</n-button></td></tr></tbody></table></div>
  </template>

  <!-- 本店身份 -->
  <template v-if="tab==='identity'">
    <p class="hint">这里设置的是<b>店铺默认身份</b>——当某个商品没有单独标注身份时，自动采用这里的设置。如需给不同商品设不同身份，请在商品设置里逐条修改或批量操作。做货 = 归属销售/毛利/利润，抽点 = 只计提成。</p>
    <div class="identity-toolbar">
      <n-select v-model:value="memberStoreIds" :options="storeOptions" multiple filterable clearable placeholder="选择店铺（可多选）" style="min-width:240px;flex:1" aria-label="选择店铺" />
      <label class="identity-date">查看时点<input v-model="memberAt" type="datetime-local" step="1" aria-label="查看身份时点" /></label>
      <label class="identity-date">生效时间<input v-model="memberFrom" type="datetime-local" step="1" aria-label="身份生效时间" /></label>
      <label class="identity-date">结束（可空）<input v-model="memberTo" type="datetime-local" step="1" aria-label="身份结束时间" /></label>
      <template v-if="members.length">
        <n-button size="small" @click="setAllDuty('produce')" :disabled="batchSaving">全部设做货</n-button>
        <n-button size="small" @click="setAllDuty('cut')" :disabled="batchSaving">全部设抽点</n-button>
        <n-button size="small" type="primary" :loading="batchSaving" :disabled="memberLoading||!members.length" @click="saveAll">一键保存到所选店铺</n-button>
      </template>
    </div>
    <p v-if="multiStore" class="hint">同时修改 {{ memberStoreIds.length }} 家店铺的默认身份。各店原身份不一致的人员暂按做货显示，保存后统一。</p>
    <p v-if="memberError" class="error">{{ memberError }} <button class="text-button" @click="loadMembers">重试</button></p>
    <n-spin :show="memberLoading">
      <div v-if="!memberStoreIds.length" class="identity-empty">请先选择店铺</div>
      <div v-else-if="!memberLoading && !members.length && !memberError" class="identity-empty">所选店铺暂无人员</div>
      <div v-else class="table-wrap">
        <table>
          <thead><tr><th>姓名</th><th>身份</th><th>所属组长</th><th>时间节点</th><th></th></tr></thead>
          <tbody>
            <tr v-for="m in members" :key="m.person_id">
              <td>{{ m.person_name }}<n-tag v-if="m.mixed" size="small" type="warning" :bordered="false" style="margin-left:6px">各店不一致</n-tag></td>
              <td><n-select v-model:value="m._duty" :options="DUTY_OPTIONS" size="small" style="width:100px" :aria-label="`身份 ${m.person_name}`" /></td>
              <td><n-select v-model:value="m._leader_id" :options="leaderOptions.filter(o=>o.value!==m.person_id)" size="small" style="width:140px" clearable placeholder="无" filterable :aria-label="`组长 ${m.person_name}`" /></td>
              <td class="segments"><div v-for="(seg,i) in m.segments" :key="i">{{ dutyLabel(seg.duty) }} · {{ segmentText(seg) }}</div><span v-if="!m.segments?.length">尚未分段</span></td>
              <td><n-button size="small" type="primary" :loading="!!memberSaving[m.person_id]" :disabled="memberLoading" @click="saveMember(m)">保存</n-button></td>
            </tr>
          </tbody>
        </table>
      </div>
    </n-spin>
  </template>
</n-modal></template>
<style scoped>
.tab-bar{display:flex;gap:0;border-bottom:1px solid #e8edf4;margin-bottom:16px}.tab-item{padding:8px 18px;font-size:14px;border:none;background:none;cursor:pointer;color:#7b8490;border-bottom:2px solid transparent;transition:all .15s}.tab-item.active{color:#18a058;border-bottom-color:#18a058;font-weight:500}.tab-item:hover{color:#333}
.identity-toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 0 16px}.identity-date{display:flex;flex-direction:column;gap:4px;font-size:12px;color:#6b7280}.identity-date input{width:190px}.segments{font-size:12px;color:#667085;line-height:1.6}.identity-empty{text-align:center;color:#8a919d;padding:40px 0;font-size:14px}
.add{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}input{box-sizing:border-box;border:1px solid #dce0e6;border-radius:6px;padding:8px;font-size:14px;max-width:100%;width:160px}.table-wrap{max-height:60vh;overflow:auto}table{width:100%;border-collapse:collapse;min-width:620px}td,th{padding:10px;text-align:left;border-bottom:1px solid #eef0f3;font-size:13px}th{background:#f8fafc;position:sticky;top:0}td:last-child{white-space:nowrap}.hint{font-size:13px;color:#7b8490}.error{color:#b42318}.text-button{background:none;border:none;color:#18a058;cursor:pointer;font-size:13px;text-decoration:underline}
</style>
