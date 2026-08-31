<script setup>
/* iOS 风格滑动选择器：年份 | 月份 两列联动。
 *
 * 年份列滚动后更新月份列的数据状态；月份列滚动到有数据的月份时触发导航。
 * 视觉上用渐变遮罩 + 中央高亮带模拟 iOS UIPickerView 的观感。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'

const ITEM_H = 40
const VISIBLE = 5
const PAD = Math.floor(VISIBLE / 2) * ITEM_H

const props = defineProps({
  periods: { type: Array, default: () => [] },
  modelValue: { type: String, default: '' },
  compact: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue'])

const OTHER = '其他'

function yearOf(p) { return /^\d{4}-\d{2}$/.test(p || '') ? p.slice(0, 4) : OTHER }
function monthIdx(p) { const m = /^\d{4}-(\d{2})$/.exec(p || ''); return m ? Number(m[1]) - 1 : 0 }
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
function syncYear() {
  const y = yearOf(props.modelValue)
  if (years.value.includes(y)) shownYear.value = y
  else if (!years.value.includes(shownYear.value)) shownYear.value = years.value[0] || ''
}
watch(() => [props.modelValue, years.value], syncYear, { immediate: true })

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

const yearCol = ref(null)
const monthCol = ref(null)
let ytimer = null
let mtimer = null
let suppress = false

function scrollCol(el, idx, smooth = true) {
  if (!el) return
  el.scrollTo({ top: Math.max(0, idx) * ITEM_H, behavior: smooth ? 'smooth' : 'instant' })
}

function onYearScroll() {
  if (suppress) return
  clearTimeout(ytimer)
  ytimer = setTimeout(() => {
    if (!yearCol.value) return
    const idx = Math.round(yearCol.value.scrollTop / ITEM_H)
    const yr = years.value[Math.max(0, Math.min(years.value.length - 1, idx))]
    if (yr && yr !== shownYear.value) {
      shownYear.value = yr
      nextTick(() => {
        const mi = Math.round((monthCol.value?.scrollTop || 0) / ITEM_H)
        const m = months.value[Math.max(0, Math.min(months.value.length - 1, mi))]
        if (m?.has) go(m.period)
      })
    }
  }, 80)
}

function onMonthScroll() {
  if (suppress) return
  clearTimeout(mtimer)
  mtimer = setTimeout(() => {
    if (!monthCol.value) return
    const idx = Math.round(monthCol.value.scrollTop / ITEM_H)
    const m = months.value[Math.max(0, Math.min(months.value.length - 1, idx))]
    if (m?.has) go(m.period)
  }, 80)
}

function scrollToSelection(smooth = true) {
  suppress = true
  const yi = years.value.indexOf(shownYear.value)
  if (yi >= 0) scrollCol(yearCol.value, yi, smooth)
  scrollCol(monthCol.value, monthIdx(props.modelValue), smooth)
  setTimeout(() => (suppress = false), smooth ? 400 : 60)
}

watch(() => props.modelValue, (v, o) => {
  if (!v || v === o) return
  syncYear()
  nextTick(() => scrollToSelection(true))
})

onMounted(() => nextTick(() => scrollToSelection(false)))
onUnmounted(() => { clearTimeout(ytimer); clearTimeout(mtimer) })
</script>

<template>
  <div v-if="list.length" class="periods" :class="{ compact }">
    <div class="head">
      <div class="pager">
        <button type="button" :disabled="index <= 0" title="较新的一个月" @click="step(-1)">‹</button>
        <button type="button" :disabled="index < 0 || index >= list.length - 1" title="更早的一个月" @click="step(1)">›</button>
      </div>
      <div class="when">{{ pretty(modelValue) }}</div>
      <div v-if="status.text" class="state" :class="status.mark"><i />{{ status.text }}</div>
    </div>

    <div class="picker" :style="{ height: `${ITEM_H * VISIBLE}px` }">
      <div class="picker-band" :style="{ top: `${PAD}px`, height: `${ITEM_H}px` }" />
      <div class="picker-fade top" :style="{ height: `${PAD}px` }" />
      <div class="picker-fade btm" :style="{ bottom: 0, height: `${PAD}px` }" />

      <div ref="yearCol" class="picker-col year-col" @scroll.passive="onYearScroll">
        <div class="picker-pad" :style="{ height: `${PAD}px` }" />
        <div v-for="y in years" :key="y" class="picker-cell" :style="{ height: `${ITEM_H}px` }">
          <span class="cell-main">{{ y }}</span>
          <span class="cell-sub">{{ yearCounts.get(y) }}期</span>
        </div>
        <div class="picker-pad" :style="{ height: `${PAD}px` }" />
      </div>

      <div class="picker-sep" />

      <div ref="monthCol" class="picker-col month-col" @scroll.passive="onMonthScroll">
        <div class="picker-pad" :style="{ height: `${PAD}px` }" />
        <div v-for="m in months" :key="m.key" class="picker-cell" :class="{ off: !m.has }" :style="{ height: `${ITEM_H}px` }">
          <span class="cell-main">{{ m.label }}<em v-if="shownYear !== OTHER">月</em></span>
          <span v-if="m.has" class="cell-status" :class="m.status.mark">{{ m.status.text }}</span>
        </div>
        <div class="picker-pad" :style="{ height: `${PAD}px` }" />
      </div>
    </div>

    <div class="legend">
      <span class="closed"><i />已结账 {{ statusCounts.closed }}</span>
      <span class="ready"><i />可确认 {{ statusCounts.ready }}</span>
      <span class="pending"><i />待补证据 {{ statusCounts.pending }}</span>
      <span class="evidence"><i />有新证据 {{ statusCounts.evidence }}</span>
    </div>
  </div>
</template>

<style scoped>
.periods {
  margin-bottom: var(--s5);
  padding: var(--s3) 0 var(--s4);
  border-bottom: 1px solid var(--n2);
}
.periods.compact { margin-top: var(--s4); }

.head {
  display: flex;
  align-items: center;
  gap: var(--s3);
  margin-bottom: var(--s4);
}
.pager { display: flex; gap: var(--s1); }
.pager button {
  width: 30px; height: 30px; padding: 0;
  border: 1px solid var(--n3); border-radius: var(--r-sm);
  background: var(--n0); color: var(--n6);
  font-size: 18px; line-height: 1; cursor: pointer;
  transition: color .15s, border-color .15s;
}
.pager button:hover:not(:disabled) { color: var(--n9); border-color: var(--n5); }
.pager button:disabled { opacity: .35; cursor: default; }

.when {
  font-family: var(--num); font-size: var(--t-xl);
  font-weight: 620; letter-spacing: -.01em; line-height: 1.2;
}
.state {
  display: flex; align-items: center; gap: 6px;
  font-size: var(--t-sm); color: var(--n6);
  padding: 3px 10px; border-radius: 999px; background: var(--n2);
}
.state i { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.state.closed { color: var(--ok); background: var(--ok-bg); }
.state.ready { color: var(--accent); background: var(--accent-bg); }
.state.pending { color: var(--warn); background: var(--warn-bg); }
.state.evidence { color: var(--ok); background: var(--ok-bg); }

/* ---- iOS 风格滚轮选择器 ---- */
.picker {
  position: relative;
  display: flex;
  max-width: 400px;
  border: 1px solid var(--n3);
  border-radius: var(--r-lg);
  background: var(--n0);
  overflow: hidden;
  user-select: none;
  -webkit-user-select: none;
}

.picker-band {
  position: absolute; left: 0; right: 0;
  background: rgba(0, 0, 0, .04);
  border-top: 1px solid var(--n3);
  border-bottom: 1px solid var(--n3);
  pointer-events: none; z-index: 1;
}

.picker-fade {
  position: absolute; left: 0; right: 0;
  pointer-events: none; z-index: 2;
}
.picker-fade.top { top: 0; background: linear-gradient(to bottom, var(--n0) 10%, transparent); }
.picker-fade.btm { background: linear-gradient(to top, var(--n0) 10%, transparent); }

.picker-sep {
  width: 1px; flex-shrink: 0;
  background: var(--n3);
  z-index: 3;
}

.picker-col {
  flex: 1; min-width: 0;
  overflow-y: auto;
  scroll-snap-type: y mandatory;
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: none;
  -ms-overflow-style: none;
}
.picker-col::-webkit-scrollbar { display: none; }

.picker-pad { flex-shrink: 0; }

.picker-cell {
  display: flex; align-items: center; justify-content: center; gap: 8px;
  scroll-snap-align: center;
  flex-shrink: 0;
  font-size: var(--t-sm);
  color: var(--n8);
  transition: color .15s;
}
.picker-cell.off { color: var(--n4); }

.year-col { max-width: 140px; }
.month-col { flex: 1; }

.cell-main {
  font-family: var(--num); font-weight: 600;
  font-size: var(--t-base);
}
.cell-main em {
  font-style: normal; font-family: var(--font);
  font-size: var(--t-xs); font-weight: 400;
  margin-left: 1px; color: var(--n5);
}
.picker-cell.off .cell-main em { color: var(--n3); }

.cell-sub {
  font-size: 11px; font-weight: 400; color: var(--n5);
}

.cell-status {
  font-size: 11px; font-weight: 500;
  padding: 1px 6px; border-radius: 999px;
}
.cell-status.closed { color: var(--ok); background: var(--ok-bg); }
.cell-status.ready { color: var(--accent); background: var(--accent-bg); }
.cell-status.pending { color: var(--warn); background: var(--warn-bg); }
.cell-status.evidence { color: var(--ok); background: var(--ok-bg); }

/* 图例 */
.legend {
  display: flex; flex-wrap: wrap; gap: var(--s3);
  margin-top: var(--s3);
  color: var(--n6); font-size: var(--t-xs);
}
.legend span { display: inline-flex; align-items: center; gap: 5px; }
.legend i { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.legend .closed { color: var(--ok); }
.legend .ready { color: var(--accent); }
.legend .pending { color: var(--warn); }
.legend .evidence { color: var(--ok); }

@media (max-width: 640px) {
  .head { flex-wrap: wrap; }
  .picker { max-width: none; }
}
</style>
