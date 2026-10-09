<script setup>
import {NAlert} from 'naive-ui'
defineProps({snapshot:{type:Object,required:true}})
</script>
<template>
  <section v-if="snapshot.allocation_correction || snapshot.allocation_pending_count || snapshot.has_allocation_evidence" class="allocation-notice">
    <NAlert v-if="snapshot.allocation_correction" type="info" :bordered="false">
      原核算 {{snapshot.allocation_correction.from_run}} 已追加分配更正记录，本次核实 {{snapshot.allocation_correction.verified_orders}} 个主订单。
      原结账损益和已核定实发不变，仅更正有依据的历史收入及同口径平台费分配。
      <span v-if="snapshot.allocation_correction.pending_orders?.length">另有 {{snapshot.allocation_correction.pending_orders.length}} 个主订单仍待补充依据，原分配保留待复核，不代表全店个人金额已重新确认。</span>
    </NAlert>
    <NAlert v-if="snapshot.allocation_pending_count" type="warning" :bordered="false">
      有 {{snapshot.allocation_pending_count}} 个金额项目已计入店铺核算，尚未分配到具体商品，不代表漏记，也不代表这些订单没有收入。
      请下载下方明细，按“待核对原因”和“核对方式”处理；这些金额不要重复录入。
    </NAlert>
    <a v-if="snapshot.has_allocation_evidence && snapshot.run_id" :href="`/api/runs/${snapshot.run_id}/allocation.csv`" download>下载分配明细（订单号、金额、未分配原因及核对方式）</a>
  </section>
</template>
<style scoped>.allocation-notice{display:grid;gap:8px;margin:12px 0}.allocation-notice a{font-size:13px;color:#3560d6;text-decoration:underline}</style>
