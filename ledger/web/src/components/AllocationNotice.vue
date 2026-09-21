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
      有 {{snapshot.allocation_pending_count}} 项主订单金额缺少有效分配依据。金额保留在店铺，但不按笔数均摊到商品或个人。请核对完整子单实付、退款或原始分配率。
    </NAlert>
    <a v-if="snapshot.has_allocation_evidence && snapshot.run_id" :href="`/api/runs/${snapshot.run_id}/allocation.csv`" download>下载本次分配依据（原金额、比例、分摊结果）</a>
  </section>
</template>
<style scoped>.allocation-notice{display:grid;gap:8px;margin:12px 0}.allocation-notice a{font-size:13px;color:#3560d6;text-decoration:underline}</style>
