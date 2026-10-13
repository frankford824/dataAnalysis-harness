<script setup>
import { computed } from 'vue'
import { NButton, NDrawer, NDrawerContent } from 'naive-ui'
const props=defineProps({snapshot:{type:Object,required:true},blockers:{type:Array,default:()=>[]},show:Boolean})
const emit=defineEmits(['update:show'])
const closed=computed(()=>props.snapshot.state==='closed')
const needsReview=computed(()=>props.blockers.length || props.snapshot.file_errors?.length || props.snapshot.deduplication?.some(d=>d.status==='conflict'))
const title=computed(()=>closed.value?'本月已结账':needsReview.value?'本月有待核对事项':props.snapshot.can_close?'本月已满足结账条件':'本月核算状态')
const hint=computed(()=>closed.value?(props.snapshot.stale?'有新资料，当前金额仍为结账时结果。':'当前展示结账时保存的金额。'):props.blockers.length?`${props.blockers.length} 项结账条件待处理${props.snapshot.file_errors?.length?` · ${props.snapshot.file_errors.length} 张源表未计入`:''}。查看详情逐项核对。`:props.snapshot.file_errors?.length?`${props.snapshot.file_errors.length} 张源表尚未进入核算。`:props.snapshot.can_close?'仍可查看来源、核对记录与分配明细。':'查看资料接收、核算与金额分配说明。')
</script>
<template>
  <section class="period-review-summary" aria-label="账期核对摘要">
    <div><strong :class="{attention:needsReview && !closed}">{{ title }}</strong><p>{{ hint }}</p></div>
    <n-button @click="emit('update:show',true)">账期核对详情</n-button>
  </section>
  <n-drawer :show="show" :width="720" :z-index="3950" style="max-width:100vw" @update:show="emit('update:show',$event)">
    <n-drawer-content title="账期核对与结账记录" closable><slot /></n-drawer-content>
  </n-drawer>
</template>
<style scoped>
.period-review-summary{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:16px 20px;background:white;border:1px solid #e2e5eb;border-radius:8px;margin-bottom:20px}
.period-review-summary strong{font-size:15px;color:#17623f}.period-review-summary strong.attention{color:#9e3d13}.period-review-summary p{font-size:13px;color:#657184;margin:4px 0 0;line-height:1.7}
@media(max-width:600px){.period-review-summary{align-items:flex-start;flex-direction:column;gap:12px}.period-review-summary :deep(button){min-height:44px}}
</style>
