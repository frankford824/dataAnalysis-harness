<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { NPopover, NInput, NCheckbox, NButton } from 'naive-ui'
import { ChevronDown, Search, Store, Users, X } from '@lucide/vue'
defineOptions({inheritAttrs:false})
const props=defineProps({modelValue:{type:Array,default:()=>[]},options:{type:Array,default:()=>[]},placeholder:{type:String,default:'全部'},label:{type:String,default:'选项'},disabled:Boolean,showChips:{type:Boolean,default:true}})
const emit=defineEmits(['update:modelValue'])
const opened=ref(false),search=ref(''),draft=ref([]),showAll=ref(false)
const noun=computed(()=>props.label.replace(/^[家位]/,''))
const filtered=computed(()=>props.options.filter(o=>String(o.label).toLocaleLowerCase().includes(search.value.trim().toLocaleLowerCase())))
const selected=computed(()=>props.modelValue.map(value=>({value,label:props.options.find(o=>o.value===value)?.label||'未找到的选项'})))
const chips=computed(()=>showAll.value?selected.value:selected.value.slice(0,6))
const groups=computed(()=>{
  const result=new Map()
  for(const option of filtered.value){const group=option.group||'';if(!result.has(group))result.set(group,[]);result.get(group).push(option)}
  return [...result].map(([label,options])=>({label,options}))
})
const allChecked=computed(()=>{const available=filtered.value.filter(o=>!o.disabled);return !!available.length&&available.every(o=>draft.value.includes(o.value))})
function commit(){if(JSON.stringify([...props.modelValue].sort())!==JSON.stringify([...draft.value].sort()))emit('update:modelValue',[...draft.value])}
function open(value){
  if(value){draft.value=[...props.modelValue];search.value='';opened.value=true}
  else {opened.value=false;nextTick(commit)}
}
function toggle(value,checked){const next=new Set(draft.value);checked?next.add(value):next.delete(value);draft.value=[...next]}
function selectVisible(checked){const next=new Set(draft.value);for(const o of filtered.value)if(!o.disabled){checked?next.add(o.value):next.delete(o.value)}draft.value=[...next]}
function remove(value){emit('update:modelValue',props.modelValue.filter(v=>v!==value))}
watch(()=>props.modelValue,values=>{draft.value=[...values]})
</script>
<template>
  <div class="workflow-select">
    <NPopover trigger="click" placement="bottom-start" :show="opened" :show-arrow="false" raw @update:show="open">
      <template #trigger><button type="button" class="workflow-filter-button" :class="{chosen:modelValue.length}" :disabled="disabled" :aria-expanded="opened" v-bind="$attrs"><component :is="noun.includes('店铺')?Store:Users" :size="15"/><span>{{noun}}</span><strong>{{modelValue.length || '全部'}}</strong><ChevronDown :size="14"/></button></template>
      <div class="workflow-filter-panel" @keydown.esc.stop.prevent="draft=[...modelValue];opened=false">
        <NInput v-model:value="search" :placeholder="`搜索${noun}`" :aria-label="`搜索${noun}选项`" clearable autofocus><template #prefix><Search :size="15"/></template></NInput>
        <div class="workflow-filter-tools"><NCheckbox :checked="allChecked" :indeterminate="!allChecked&&filtered.some(o=>draft.includes(o.value))" @update:checked="selectVisible">{{search?'全选搜索结果':'全选'}}</NCheckbox><NButton text size="small" @click="draft=[]">清空</NButton></div>
        <div class="workflow-filter-options">
          <section v-for="group in groups" :key="group.label"><h4 v-if="group.label">{{group.label}}</h4><label v-for="option in group.options" :key="option.value" class="workflow-filter-option"><NCheckbox :checked="draft.includes(option.value)" :disabled="option.disabled" @update:checked="toggle(option.value,$event)">{{option.label}}</NCheckbox></label></section>
          <p v-if="!filtered.length" class="workflow-filter-empty">没有找到{{noun}}</p>
        </div>
        <footer><span>已选 {{draft.length}} {{label}}</span><NButton type="primary" size="small" @click="open(false)">完成</NButton></footer>
      </div>
    </NPopover>
    <div v-if="showChips&&selected.length" class="workflow-chips"><button v-for="item in chips" :key="item.value" class="workflow-chip" :title="item.label" :aria-label="`移除${noun}${item.label}`" @click="remove(item.value)"><span>{{item.label}}</span><X :size="12"/></button><button v-if="selected.length>6" class="workflow-expand" @click="showAll=!showAll">{{showAll?'收起':`展开其余 ${selected.length-6} 项`}}</button></div>
  </div>
</template>
<style scoped>
.workflow-select{min-width:0}.workflow-filter-button{height:36px;display:flex;align-items:center;gap:8px;border:1px solid #dce4ef;background:#fff;border-radius:6px;padding:0 12px;color:#506078;font:inherit;font-size:13px;cursor:pointer}.workflow-filter-button strong{font-weight:550;color:#203148}.workflow-filter-button.chosen{border-color:#bdd0fb;background:#f6f9ff}.workflow-filter-button.chosen strong{color:#3468f0}.workflow-filter-button:disabled{opacity:.5;cursor:default}.workflow-filter-panel{width:min(400px,calc(100vw - 28px));border:1px solid #e0e7f0;border-radius:9px;padding:14px;background:white;box-shadow:0 10px 35px #13243e20}.workflow-filter-tools{display:flex;justify-content:space-between;align-items:center;padding:12px 0;border-bottom:1px solid #edf1f6}.workflow-filter-options{max-height:330px;overflow:auto;overscroll-behavior:contain}.workflow-filter-options h4{margin:12px 0 5px;color:#8a98aa;font-size:11px;font-weight:500}.workflow-filter-option{display:block;padding:9px 6px;border-radius:5px}.workflow-filter-option:hover{background:#f4f7fc}.workflow-filter-option :deep(.n-checkbox){width:100%;align-items:flex-start}.workflow-filter-option :deep(.n-checkbox__label){white-space:normal;line-height:1.6}.workflow-filter-empty{padding:25px;color:#8290a4;text-align:center;font-size:13px}.workflow-filter-panel footer{display:flex;align-items:center;justify-content:space-between;padding-top:12px;border-top:1px solid #edf1f6;color:#8390a3;font-size:12px}.workflow-chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}.workflow-chip{display:inline-flex;align-items:center;gap:6px;max-width:265px;border:0;border-radius:4px;background:#edf2f8;padding:4px 7px;color:#53647c;font-size:12px;cursor:pointer}.workflow-chip span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.workflow-chip svg{flex:none}.workflow-expand{border:0;background:none;color:#3468f0;cursor:pointer;font-size:12px}
</style>
