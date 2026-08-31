<script setup>
/* 一家店的账期切换：年份标签 + 6×2 月份网格。
 *
 * 所有月份一目了然，不用滚动。每个月用颜色和文字同时标出状态，
 * 选中态用深色突出。没算过的月份保留位置但淡化。
 */
import { computed, ref, watch } from 'vue'

const props = defineProps({
  periods: { type: Array, default: () => [] },
  modelValue: { type: String, default: '' },
  compact: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue'])

const OTHER = '其他'

function yearOf(p) { return /^\d{4}-\d{2}$/.test(p || '') ? p.slice(0, 4) : OTHER }
function pretty(p) { const m = /^(\d{4})-(\d{2})$/.exec(p || ''); return m ? `${m[1]} 年 ${Number(m[2])} 月` : p || '' }

const list = computed(() =>
  [...(props.periods || [])].sort((a, b) => String(b.period).localeCompare(String(a.period))),
)
const index = computed(() => list.value.findIndex((p) => p.period === props.modelValue))
const current = computed(() => list.value[index.value] || null)
const byPeriod = computed(() => new Map(list.value.map((p) => [p.period, p])))

const years = computed(() => {
  const out = []
  for (const p of list.value) { const y = yearOf(p.period); if (!out.includes(y)) out.push(y) }
  return out
})

const shownYear = ref('')
watch(
  () => [props.modelValue, years.value],
  () => {
    const y = yearOf(props.modelValue)
    if (years.value.includes(y)) shownYear.value = y
    else if (!years.value.includes(shownYear.value)) shownYear.value = years.value[0] || ''
  },
  { immediate: true },
)

const months = computed(() => {
  if (shownYear.value === OTHER) {
    return list.value
      .filter((p) => yearOf(p.period) === OTHER)
      .map((p) => ({ key: p.period, label: p.period || '未知', period: p.period, has: true, status: statusOf(p) }))
  }
  return Array.from({ length: 12 }, (_, i) => {
    const period = `${shownYear.value}-${String(i + 1).padStart(2, '0')}`
    const item = byPeriod.value.get(period)
    return { key: period, label: `${i + 1}`, period, has: !!item, status: statusOf(item) }
  })
})

function go(p) { if (p && p !== props.modelValue) emit('update:modelValue', p) }
function step(dir) { const next = list.value[index.value + dir]; if (next) go(next.period) }

function statusOf(p) {
  if (!p) return { mark: '', text: '' }
  if (p.state === 'closed' && p.stale) return { mark: 'evidence', text: '有新证据' }
  if (p.state === 'closed') return { mark: 'closed', text: '已结账' }
  if (p.can_close) return { mark: 'ready', text: '可确认' }
  return { mark: 'pending', text: '待补证据' }
}

const status = computed(() => statusOf(current.value))
const statusCounts = computed(() => {
  const c = { closed: 0, ready: 0, pending: 0, evidence: 0 }
  for (const item of list.value) c[statusOf(item).mark || 'pending'] += 1
  return c
})
const yearCounts = computed(() => {
  const m = new Map()
  for (const item of list.value) { const y = yearOf(item.period); m.set(y, (m.get(y) || 0) + 1) }
  return m
})
</script>

<template>
  <div v-if="list.length" class="ps" :class="{ compact }">
    <div class="ps-head">
      <div class="ps-pager">
        <button type="button" :disabled="index <= 0" title="较新的一个月" @click="step(-1)">‹</button>
        <button type="button" :disabled="index < 0 || index >= list.length - 1" title="更早的一个月" @click="step(1)">›</button>
      </div>
      <div class="ps-when">{{ pretty(modelValue) }}</div>
      <div v-if="status.text" class="ps-badge" :class="status.mark"><i />{{ status.text }}</div>
    </div>

    <div class="ps-card">
      <div class="ps-years" role="tablist">
        <button
          v-for="y in years" :key="y" type="button" role="tab"
          :class="{ on: y === shownYear }" :aria-selected="y === shownYear"
          @click="shownYear = y"
        >{{ y }}<small>{{ yearCounts.get(y) }}期</small></button>
      </div>

      <div class="ps-grid" :class="{ free: shownYear === OTHER }">
        <button
          v-for="m in months" :key="m.key" type="button"
          class="ps-cell" :class="[m.status?.mark, { on: m.period === modelValue, off: !m.has }]"
          :disabled="!m.has" :title="m.has ? `${pretty(m.period)} · ${m.status.text}` : '这个月还没算过'"
          @click="go(m.period)"
        >
          <span class="ps-m">{{ m.label }}<em v-if="shownYear !== OTHER">月</em></span>
          <span v-if="m.has" class="ps-s">{{ m.status.text }}</span>
        </button>
      </div>

      <div class="ps-legend">
        <span class="closed"><i />已结账 {{ statusCounts.closed }}</span>
        <span class="ready"><i />可确认 {{ statusCounts.ready }}</span>
        <span class="pending"><i />待补证据 {{ statusCounts.pending }}</span>
        <span class="evidence"><i />有新证据 {{ statusCounts.evidence }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ps { margin-bottom: var(--s5); padding: var(--s3) 0 var(--s4); border-bottom: 1px solid var(--n2); }
.ps.compact { margin-top: var(--s4); }

.ps-head { display: flex; align-items: center; gap: var(--s3); margin-bottom: var(--s4); }
.ps-pager { display: flex; gap: var(--s1); }
.ps-pager button {
  width: 30px; height: 30px; padding: 0;
  border: 1px solid var(--n3); border-radius: var(--r-sm);
  background: var(--n0); color: var(--n6);
  font-size: 18px; line-height: 1; cursor: pointer;
  transition: color .15s, border-color .15s;
}
.ps-pager button:hover:not(:disabled) { color: var(--n9); border-color: var(--n5); }
.ps-pager button:disabled { opacity: .35; cursor: default; }
.ps-when { font-family: var(--num); font-size: var(--t-xl); font-weight: 620; letter-spacing: -.01em; }
.ps-badge {
  display: flex; align-items: center; gap: 6px;
  font-size: var(--t-sm); padding: 3px 10px;
  border-radius: 999px; color: var(--n6); background: var(--n2);
}
.ps-badge i { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.ps-badge.closed { color: var(--ok); background: var(--ok-bg); }
.ps-badge.ready { color: var(--accent); background: var(--accent-bg); }
.ps-badge.pending { color: var(--warn); background: var(--warn-bg); }
.ps-badge.evidence { color: var(--ok); background: var(--ok-bg); }

.ps-card { border: 1px solid var(--n3); border-radius: var(--r-lg); background: var(--n0); overflow: hidden; }

/* 年份标签 */
.ps-years {
  display: flex; gap: 0; padding: 0 var(--s3);
  border-bottom: 1px solid var(--n3); background: var(--n1);
  overflow-x: auto; scrollbar-width: none;
}
.ps-years::-webkit-scrollbar { display: none; }
.ps-years button {
  display: inline-flex; align-items: baseline; gap: 6px;
  padding: 10px 16px; border: none;
  border-bottom: 2px solid transparent;
  background: none; font: 500 var(--t-sm)/1.4 var(--num);
  color: var(--n5); white-space: nowrap; cursor: pointer;
  transition: color .12s, border-color .12s; margin-bottom: -1px;
}
.ps-years button:hover { color: var(--n8); }
.ps-years button.on { color: var(--n9); font-weight: 640; border-bottom-color: var(--n9); }
.ps-years button small { font-family: var(--font); font-size: 10px; font-weight: 400; color: var(--n4); }
.ps-years button.on small { color: var(--n6); }

/* 6×2 月份网格 */
.ps-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 6px; padding: 14px 16px; }
.ps-grid.free { grid-template-columns: repeat(3, minmax(0, 1fr)); }

.ps-cell {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 2px; min-height: 52px; padding: 8px 4px;
  border: 1px solid var(--n3); border-radius: var(--r-md);
  background: var(--n0); text-align: center; cursor: pointer;
  transition: all .12s;
}
.ps-cell:hover:not(:disabled):not(.on) { border-color: var(--n5); background: var(--n1); }

.ps-m { font-family: var(--num); font-size: var(--t-base); font-weight: 640; line-height: 1.2; color: var(--n8); }
.ps-m em { font-style: normal; font-family: var(--font); font-size: 11px; font-weight: 400; margin-left: 1px; color: var(--n5); }
.ps-s { font-size: 10px; font-weight: 500; line-height: 1; white-space: nowrap; }

/* 状态色 */
.ps-cell.closed { border-color: #c8e6d4; background: var(--ok-bg); }
.ps-cell.closed .ps-m { color: var(--ok); }
.ps-cell.closed .ps-s { color: var(--ok); }

.ps-cell.ready { border-color: #c2d4f7; background: var(--accent-bg); }
.ps-cell.ready .ps-m { color: var(--accent); }
.ps-cell.ready .ps-s { color: var(--accent); }

.ps-cell.pending { border-color: #f0d8a8; background: var(--warn-bg); }
.ps-cell.pending .ps-m { color: var(--warn); }
.ps-cell.pending .ps-s { color: var(--warn); }

.ps-cell.evidence { border-color: #c8e6d4; background: var(--ok-bg); }
.ps-cell.evidence .ps-m { color: var(--ok); }
.ps-cell.evidence .ps-s { color: var(--ok); }

/* 选中态 */
.ps-cell.on { border-color: var(--n8); background: var(--n8); box-shadow: 0 1px 4px rgba(0,0,0,.15); }
.ps-cell.on .ps-m, .ps-cell.on .ps-s, .ps-cell.on .ps-m em { color: var(--n0); }

/* 空月份 */
.ps-cell.off { border-color: var(--n2); background: var(--n1); cursor: default; }
.ps-cell.off .ps-m { color: var(--n4); }
.ps-cell.off .ps-m em { color: var(--n3); }

/* 图例 */
.ps-legend {
  display: flex; flex-wrap: wrap; gap: var(--s3);
  padding: 10px 16px; border-top: 1px solid var(--n3);
  background: var(--n1); color: var(--n6); font-size: var(--t-xs);
}
.ps-legend span { display: inline-flex; align-items: center; gap: 5px; }
.ps-legend i { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.ps-legend .closed { color: var(--ok); }
.ps-legend .ready { color: var(--accent); }
.ps-legend .pending { color: var(--warn); }
.ps-legend .evidence { color: var(--ok); }

@media (max-width: 720px) {
  .ps-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
}
@media (max-width: 480px) {
  .ps-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 4px; padding: 10px 12px; }
  .ps-cell { min-height: 44px; padding: 6px 2px; }
  .ps-legend { padding: 8px 12px; gap: var(--s2); }
}
</style>
