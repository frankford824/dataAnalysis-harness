<script setup>
import { computed } from 'vue'
import { RefreshCw } from '@lucide/vue'
import { useApp } from '../store'

const app = useApp()
const clock = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
const stamp = computed(() => clock.format(new Date(app.lastUpdated)))
const spinning = scope => app.refreshing && app.refreshScope === scope
</script>

<template>
  <div class="refresh-control" role="group" aria-label="页面数据">
    <span class="refresh-stamp" aria-live="polite">数据更新于 {{ stamp }}</span>
    <button type="button" class="refresh-btn" :disabled="app.refreshing" @click="app.refresh('page')">
      <RefreshCw :size="13" :class="{ spinning: spinning('page') }" aria-hidden="true" />刷新本页
    </button>
    <button type="button" class="refresh-btn" :disabled="app.refreshing" title="连同店铺列表、后台核算进度一起刷新" @click="app.refresh('all')">
      <RefreshCw :size="13" :class="{ spinning: spinning('all') }" aria-hidden="true" />全部刷新
    </button>
  </div>
</template>

<style scoped>
.refresh-control { display: flex; align-items: center; gap: 4px; min-width: 0; color: #8a94a6; font-size: 12px; }
.refresh-stamp { margin-right: 6px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.refresh-btn {
  display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px; border: 0; border-radius: 6px;
  background: transparent; color: #4a5568; font: inherit; cursor: pointer; white-space: nowrap;
}
.refresh-btn:hover:not(:disabled) { background: #f1f4f8; color: #1f2a3d; }
.refresh-btn:focus-visible { outline: 2px solid #4c7cf0; outline-offset: 1px; }
.refresh-btn:disabled { cursor: default; color: #a0a8b6; }
.spinning { animation: refresh-spin .9s linear infinite; }
@keyframes refresh-spin { to { transform: rotate(360deg); } }
@media (max-width: 760px) { .refresh-stamp { display: none; } }
@media (prefers-reduced-motion: reduce) { .spinning { animation-duration: 2.4s; } }
</style>
