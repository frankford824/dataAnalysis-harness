<script setup>
import { NTabs, NTab } from 'naive-ui'
import { nextTick,ref } from 'vue'
const props=defineProps({modelValue:{type:[String,Number],default:''},options:{type:Array,required:true},appearance:{type:String,default:'line'},label:{type:String,default:'切换内容'}})
const emit=defineEmits(['update:modelValue'])
const host=ref(null)
function keydown(event,index){
  const available=props.options.filter(item=>!item.disabled)
  if(!available.length || props.options[index]?.disabled)return
  const current=available.findIndex(item=>item.key===props.options[index].key)
  let next=current
  if(event.key==='ArrowRight')next=(current+1)%available.length
  else if(event.key==='ArrowLeft')next=(current-1+available.length)%available.length
  else if(event.key==='Home')next=0
  else if(event.key==='End')next=available.length-1
  else if(!['Enter',' '].includes(event.key))return
  event.preventDefault();emit('update:modelValue',available[next].key)
  nextTick(()=>host.value?.$el.querySelectorAll('[role=tab]')[props.options.indexOf(available[next])]?.focus())
}
</script>
<template><NTabs ref="host" class="ledger-tabs" role="tablist" :class="{'ledger-tabs-segment':appearance==='segment'}" :value="modelValue" :type="appearance==='segment'?'segment':'line'" size="medium" :aria-label="label" @update:value="$emit('update:modelValue',$event)"><NTab v-for="(item,index) in options" :key="item.key" :name="item.key" :disabled="item.disabled" role="tab" :aria-selected="modelValue===item.key" :aria-disabled="!!item.disabled" :tabindex="modelValue===item.key?0:-1" @keydown="keydown($event,index)">{{item.label}}<span v-if="item.count!=null" class="ledger-tab-count">{{item.count}}</span></NTab></NTabs></template>
<style scoped>.ledger-tabs{min-width:0}.ledger-tabs:not(.ledger-tabs-segment){flex:1}.ledger-tabs-segment{width:auto;max-width:100%}.ledger-tab-count{margin-left:7px;color:#8491a5;font-size:12px;font-variant-numeric:tabular-nums}.ledger-tabs :deep(.n-tabs-tab){font-weight:500}.ledger-tabs :deep(.n-tabs-tab__label){gap:4px}.ledger-tabs-segment :deep(.n-tabs-rail){gap:3px;padding:3px;background:#f1f4f8;border-radius:7px}.ledger-tabs-segment :deep(.n-tabs-capsule){border-radius:5px;box-shadow:0 1px 3px #172b4d12}.ledger-tabs-segment :deep(.n-tabs-tab){padding:7px 12px!important;white-space:nowrap;font-size:13px}</style>
