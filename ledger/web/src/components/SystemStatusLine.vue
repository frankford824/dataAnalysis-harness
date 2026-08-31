<script setup>
import { computed } from 'vue'

import { ago, count } from '../format'

const props = defineProps({
  feed: { type: Object, default: null },
  indexErrors: { type: Array, default: () => [] },
})

defineEmits(['details'])

const latest = computed(() => Number(
  props.feed?.source_latest_seq
  || props.feed?.health?.latest_seq
  || props.feed?.consumed_seq
  || 0,
))
const consumed = computed(() => Number(props.feed?.consumed_seq || 0))
const lag = computed(() => Math.max(0, latest.value - consumed.value))

const state = computed(() => {
  if (!props.feed) {
    return { tone: 'working', text: '正在确认订单与成本进度' }
  }
  if (!props.feed?.enabled) {
    return { tone: 'quiet', text: '订单与成本实时同步未启用' }
  }
  if (props.feed.last_error || props.feed.health?.healthy === false) {
    return { tone: 'warn', text: '订单与成本同步暂停 · 正在等待源端恢复' }
  }
  if (!props.feed.snapshot_id) {
    return { tone: 'working', text: '订单与成本正在建立首次同步' }
  }
  if (lag.value > 0) {
    return { tone: 'working', text: `订单与成本落后约 ${count(lag.value)} 条 · 正在追` }
  }
  return {
    tone: 'ok',
    text: `订单与成本已跟上 · 最近更新 ${ago(props.feed.last_success) || '刚刚'}`,
  }
})

const sentence = computed(() => {
  if (!props.indexErrors.length) return state.value.text
  return `${state.value.text} · 另有 ${props.indexErrors.length} 份原文件需要处理`
})
</script>

<template>
  <div class="system-line" :class="state.tone" role="status">
    <span class="system-line-dot" />
    <span>{{ sentence }}</span>
    <button class="link system-line-detail" type="button" @click="$emit('details')">
      查看系统状态
    </button>
  </div>
</template>

<style scoped>
.system-line {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 32px;
  color: var(--n7);
  font-size: var(--t-sm);
}
.system-line-dot {
  width: 8px;
  height: 8px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--n5);
  box-shadow: 0 0 0 3px var(--n2);
}
.system-line.ok .system-line-dot { background: var(--ok); box-shadow: 0 0 0 3px var(--ok-bg); }
.system-line.warn .system-line-dot { background: var(--warn); box-shadow: 0 0 0 3px var(--warn-bg); }
.system-line.working .system-line-dot {
  background: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-bg);
  animation: status-breathe 1.4s ease-in-out infinite;
}
.system-line-detail { margin-left: 6px; }
@keyframes status-breathe { 50% { opacity: .45; } }
</style>
