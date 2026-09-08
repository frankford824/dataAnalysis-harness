<script setup>
import { ref } from 'vue'
import { useMessage } from 'naive-ui'
const emit=defineEmits(['changed','assignments'])
const message=useMessage(),shown=ref(false),busy=ref(false),rows=ref([]),name=ref(''),employee=ref(''),error=ref('')
async function call(path,options={}) {const r=await fetch('/api/commission-v2'+path,{...options,headers:{'Content-Type':'application/json'}});const b=await r.json();if(!r.ok)throw new Error(typeof b.detail==='string'?b.detail:'保存失败');return b}
async function load(){rows.value=(await call('/people/summary')).people}
async function open(){shown.value=true;error.value='';busy.value=true;try{await load()}catch(e){error.value=e.message}finally{busy.value=false}}
async function save(person=null){busy.value=true;error.value='';try{await call('/people',{method:'POST',body:JSON.stringify({person:person||{name:name.value,employee_no:employee.value},expected_revision:person?.revision||0,reason:person?'修改人员姓名或工号':'新增提成人员'})});name.value='';employee.value='';await load();emit('changed');message.success('人员名单已保存')}catch(e){error.value=e.message}finally{busy.value=false}}
function assignments(person){shown.value=false;emit('assignments',person.id)}
defineExpose({open,shown})
</script>
<template><n-modal v-model:show="shown" preset="card" title="人员名单与分配" style="width:min(860px,95vw)"><p class="hint">统一维护名单；点“查看分配”可查看该人员在各店铺的商品和比例。</p><div class="add"><input v-model="name" placeholder="姓名" aria-label="新增人员姓名"/><input v-model="employee" placeholder="工号（可空）" aria-label="新增人员工号"/><n-button type="primary" :disabled="!name.trim()" :loading="busy" @click="save()">添加人员</n-button></div><p v-if="error" class="error">{{ error }}</p><div class="table-wrap"><table><thead><tr><th>姓名</th><th>工号</th><th>当前分配</th><th></th></tr></thead><tbody><tr v-for="p in rows" :key="p.id"><td><input v-model="p.name" :aria-label="`人员姓名 ${p.id}`"/></td><td><input v-model="p.employee_no" :aria-label="`人员工号 ${p.id}`"/></td><td>{{ p.stores }}家店铺 · {{ p.products }}个宝贝</td><td><n-button size="small" :disabled="busy" @click="save(p)">保存</n-button> <n-button size="small" :disabled="busy" @click="assignments(p)">查看分配</n-button></td></tr></tbody></table></div></n-modal></template>
<style scoped>
.add{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}input{box-sizing:border-box;border:1px solid #dce0e6;border-radius:6px;padding:8px;font-size:14px;max-width:100%;width:160px}.table-wrap{max-height:60vh;overflow:auto}table{width:100%;border-collapse:collapse;min-width:620px}td,th{padding:10px;text-align:left;border-bottom:1px solid #eef0f3;font-size:13px}th{background:#f8fafc;position:sticky;top:0}td:last-child{white-space:nowrap}.hint{font-size:13px;color:#7b8490}.error{color:#b42318}
</style>
