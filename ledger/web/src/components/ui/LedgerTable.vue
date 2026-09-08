<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { NDataTable, NEmpty } from 'naive-ui'
const props=defineProps({columns:{type:Array,required:true},rows:{type:Array,default:()=>[]},rowKey:{type:Function,required:true},loading:Boolean,checkedKeys:{type:Array,default:undefined},empty:{type:String,default:'没有找到记录'},maxHeight:{type:[Number,String],default:520}})
defineEmits(['update:checkedKeys'])
const host=ref(null),width=ref(1200),availableHeight=ref(520)
let observer
function measure(){const rect=host.value?.getBoundingClientRect();if(!rect?.width)return;width.value=rect.width;availableHeight.value=Math.max(180,window.innerHeight-Math.max(0,rect.top)-100)}
onMounted(()=>{observer=new ResizeObserver(measure);observer.observe(host.value);window.addEventListener('resize',measure);measure()})
onUnmounted(()=>{observer?.disconnect();window.removeEventListener('resize',measure)})
watch(()=>[props.rows,props.loading],()=>nextTick(measure))
const height=computed(()=>Math.min(Number(props.maxHeight)||520,availableHeight.value))
const visibleColumns=computed(()=>props.columns.filter(c=>width.value>=640||c.mobile!==false).map(c=>({...c,width:width.value<640?(c.mobileWidth??c.width):c.width,minWidth:width.value<640?undefined:c.minWidth})))
const scrollWidth=computed(()=>visibleColumns.value.reduce((n,c)=>n+(Number(c.width)||Number(c.minWidth)||120),0))
</script>
<template><div ref="host" class="ledger-data-table"><NDataTable :columns="visibleColumns" :data="rows" :row-key="rowKey" :loading="loading" :checked-row-keys="checkedKeys" :max-height="height" :scroll-x="scrollWidth" :bordered="false" :single-line="true" :striped="false" size="medium" @update:checked-row-keys="$emit('update:checkedKeys',$event)"><template #empty><NEmpty :description="empty" size="small"/></template></NDataTable></div></template>
<style scoped>.ledger-data-table{container-type:inline-size;border:1px solid #e6ebf2;border-radius:8px;overflow:hidden;min-width:0;background:white}.ledger-data-table :deep(.n-data-table-th){font-size:12px;font-weight:500;white-space:nowrap}.ledger-data-table :deep(.n-data-table-td){font-size:13px;line-height:1.7}.ledger-data-table :deep(.n-empty){padding:28px 0}.ledger-data-table :deep(.table-product){font-weight:550;color:#233247;white-space:normal;overflow-wrap:anywhere}.ledger-data-table :deep(.table-secondary){font-size:12px;color:#8390a2;margin-top:4px;overflow-wrap:anywhere}.ledger-data-table :deep(.table-money){font-variant-numeric:tabular-nums;font-size:14px;font-weight:550}.ledger-data-table :deep(.table-money.negative){color:#b45248}.ledger-data-table :deep(.table-assignee){display:flex;justify-content:space-between;gap:16px}.ledger-data-table :deep(.table-assignee strong){font-weight:500;font-variant-numeric:tabular-nums}.ledger-data-table :deep(.table-mobile-only){display:none}@container(max-width:640px){.ledger-data-table :deep(.table-mobile-only){display:block}}
</style>
