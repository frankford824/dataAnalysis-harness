<script setup>
import { computed, h, ref } from 'vue'
import { NSelect, NTag, NPopover, NButton } from 'naive-ui'
import { X, ListChecks } from '@lucide/vue'
defineOptions({ inheritAttrs:false })
const props=defineProps({modelValue:{type:Array,default:()=>[]},options:{type:Array,default:()=>[]},placeholder:{type:String,default:'全部'},label:{type:String,default:'选项'},disabled:Boolean})
const emit=defineEmits(['update:modelValue'])
const expanded=ref(false)
const noun=computed(()=>props.label.replace(/^[家位]/,''))
const selected=computed(()=>props.modelValue.map(value=>({value,label:props.options.find(o=>o.value===value)?.label || '未找到的选项'})))
function remove(value){emit('update:modelValue',props.modelValue.filter(v=>v!==value))}
function tag({option,handleClose}) {
  return h(NTag,{size:'small',closable:true,bordered:false,onClose:e=>{e.stopPropagation();handleClose()},style:{maxWidth:'min(230px, 100%)'}},
    {default:()=>h('span',{class:'filter-tag-name',title:String(option.label)},String(option.label))})
}
</script>
<template>
  <div class="ledger-multi-select">
    <NSelect :value="modelValue" :options="options" :placeholder="placeholder" :disabled="disabled" multiple filterable clearable :max-tag-count="1" :render-tag="tag" :consistent-menu-width="false" :menu-props="{style:{maxWidth:'min(520px,92vw)'}}" v-bind="$attrs" @update:value="emit('update:modelValue',$event)" />
    <div v-if="selected.length" class="filter-selection-summary">
      <span>已选 {{selected.length}} {{label}}</span>
      <NPopover trigger="click" placement="bottom-start" :show="expanded" @update:show="expanded=$event">
        <template #trigger><button type="button" class="selection-detail-button" :aria-label="`查看已选${noun}`" :aria-expanded="expanded" @click.stop.prevent><ListChecks :size="14"/>查看已选</button></template>
        <div class="selection-popover"><div class="selection-popover-heading"><strong>已选{{noun}}（{{selected.length}}）</strong><NButton text size="small" :disabled="disabled" @click="emit('update:modelValue',[]);expanded=false">清空</NButton></div><ul><li v-for="item in selected" :key="item.value"><span>{{item.label}}</span><button type="button" :disabled="disabled" :aria-label="`移除${item.label}`" @click="remove(item.value)"><X :size="15"/></button></li></ul></div>
      </NPopover>
    </div>
  </div>
</template>
<style scoped>
.ledger-multi-select{min-width:0;width:100%}.ledger-multi-select :deep(.filter-tag-name){display:block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.ledger-multi-select :deep(.n-tag__content){min-width:0}.filter-selection-summary{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:center;margin-top:7px;font-size:12px;color:#758198}.selection-detail-button{white-space:nowrap;display:inline-flex;align-items:center;gap:5px;border:0;background:transparent;color:#3468f0;cursor:pointer;padding:0;font:inherit}.selection-popover{width:min(380px,80vw)}.selection-popover-heading{display:flex;align-items:center;justify-content:space-between;gap:20px;font-size:13px;padding:3px 0 10px;border-bottom:1px solid #e8edf4}.selection-popover ul{list-style:none;padding:0;margin:0;max-height:280px;overflow:auto}.selection-popover li{display:flex;align-items:center;justify-content:space-between;gap:15px;padding:10px 0;border-bottom:1px solid #f0f3f7;font-size:13px}.selection-popover li span{overflow-wrap:anywhere}.selection-popover li button{display:flex;flex:none;padding:5px;border:0;background:transparent;color:#7c899d;cursor:pointer;border-radius:4px}.selection-popover li button:hover{background:#f0f4fa;color:#b42318}
</style>
