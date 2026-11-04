<script setup>
import { computed } from 'vue'
import { RefreshCw } from '@lucide/vue'
import { cacheState } from '../api'
import { useApp } from '../store'

const app = useApp()
const clock = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
const day = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: 'numeric', day: 'numeric' })
// 先显示的是上次留下的副本时，这里写的是那份数取回的时间，不是打开页面的时间。
const stamp = computed(() => {
  const at = new Date(cacheState.shownAt)
  return day.format(at) === day.format(new Date()) ? clock.format(at) : `${day.format(at)} ${clock.format(at)}`
})
const checking = computed(() => cacheState.revalidating > 0 && !app.refreshing)
const spinning = scope => app.refreshing && app.refreshScope === scope
</script>

<template>
  <div class="refresh-control" role="group" aria-label="页面数据">
    <span class="refresh-stamp" aria-live="polite" :title="checking ? '正在后台核对最新数据，有变化会自动换上' : '页面上的数是这个时间取到的'">
      数据更新于 {{ stamp }}<i class="refresh-dot" :class="{ on: checking }" aria-hidden="true" />
    </span>
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
.refresh-stamp { display: inline-flex; align-items: center; gap: 5px; margin-right: 6px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.refresh-dot { width: 5px; height: 5px; border-radius: 50%; background: #4c7cf0; opacity: 0; transition: opacity .3s ease; }
.refresh-dot.on { opacity: .8; animation: refresh-pulse 1.4s ease-in-out infinite; }
@keyframes refresh-pulse { 50% { opacity: .3; } }
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
@media (prefers-reduced-motion: reduce) { .spinning { animation-duration: 2.4s; } .refresh-dot.on { animation: none; } }
</style>
