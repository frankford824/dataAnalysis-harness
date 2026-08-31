<script setup>
/* 一家店的账期切换。
 *
 * 用年份横向标签 + 月份网格的方式展现。年份用标签切换，月份按日历排列成 4×3
 * 网格。每个月的状态用颜色和文字双重表达。
 */
import { computed, ref, watch } from 'vue'

const props = defineProps({
  periods: { type: Array, default: () => [] },
  modelValue: { type: String, default: '' },
  compact: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue'])

const OTHER = '其他'

function yearOf(period) {
  return /^\d{4}-\d{2}$/.test(period || '') ? period.slice(0, 4) : OTHER
}

function pretty(period) {
  const m = /^(\d{4})-(\d{2})$/.exec(period || '')
  return m ? `${m[1]} 年 ${Number(m[2])} 月` : period || ''
}

const list = computed(() =>
  [...(props.periods || [])].sort((a, b) => String(b.period).localeCompare(String(a.period))),
)

const index = computed(() => list.value.findIndex((p) => p.period === props.modelValue))
const current = computed(() => list.value[index.value] || null)
const byPeriod = computed(() => new Map(list.value.map((p) => [p.period, p])))

const years = computed(() => {
  const out = []
  for (const p of list.value) {
    const y = yearOf(p.period)
    if (!out.includes(y)) out.push(y)
  }
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
      .map((p) => ({
        key: p.period, label: p.period || '未知', period: p.period, has: true,
        status: statusOf(p),
      }))
  }
  return Array.from({ length: 12 }, (_, i) => {
    const period = `${shownYear.value}-${String(i + 1).padStart(2, '0')}`
    const item = byPeriod.value.get(period)
    return {
      key: period, label: `${i + 1}`, period, has: !!item,
      status: statusOf(item),
    }
  })
})

function go(p) {
  if (p && p !== props.modelValue) emit('update:modelValue', p)
}

function step(dir) {
  const next = list.value[index.value + dir]
  if (next) go(next.period)
}

function pickYear(year) {
  shownYear.value = year
}

function statusOf(p) {
  if (!p) return { mark: '', text: '' }
  if (p.state === 'closed' && p.stale) return { mark: 'evidence', text: '有新证据' }
  if (p.state === 'closed') return { mark: 'closed', text: '已结账' }
  if (p.can_close) return { mark: 'ready', text: '可确认' }
  return { mark: 'pending', text: '待补证据' }
}

const status = computed(() => statusOf(current.value))
const statusCounts = computed(() => {
  const counts = { closed: 0, ready: 0, pending: 0, evidence: 0 }
  for (const item of list.value) counts[statusOf(item).mark || 'pending'] += 1
  return counts
})
const yearCounts = computed(() => {
  const counts = new Map()
  for (const item of list.value) {
    const year = yearOf(item.period)
    counts.set(year, (counts.get(year) || 0) + 1)
  }
  return counts
})
</script>

<template>
  <div v-if="list.length" class="periods" :class="{ compact }">
    <div class="head">
      <div class="pager">
        <button
          type="button"
          :disabled="index <= 0"
          title="较新的一个月"
          aria-label="较新的一个月"
          @click="step(-1)"
        >
          ‹
        </button>
        <button
          type="button"
          :disabled="index < 0 || index >= list.length - 1"
          title="更早的一个月"
          aria-label="更早的一个月"
          @click="step(1)"
        >
          ›
        </button>
      </div>
      <div class="when">{{ pretty(modelValue) }}</div>
      <div v-if="status.text" class="state" :class="status.mark">
        <i />{{ status.text }}
      </div>
    </div>

    <div class="strip-card">
      <div class="year-tabs" role="tablist" aria-label="选择年份">
        <button
          v-for="y in years"
          :key="y"
          type="button"
          role="tab"
          :class="{ on: y === shownYear }"
          :aria-selected="y === shownYear"
          @click="pickYear(y)"
        >
          {{ y }}<small>{{ yearCounts.get(y) }}期</small>
        </button>
      </div>

      <div class="month-grid" :class="{ free: shownYear === OTHER }" role="listbox" aria-label="选择月份">
        <button
          v-for="m in months"
          :key="m.key"
          type="button"
          class="m-cell"
          :class="[m.status?.mark, { on: m.period === modelValue, off: !m.has }]"
          :disabled="!m.has"
          :aria-selected="m.period === modelValue"
          :title="m.has ? `${pretty(m.period)} · ${statusOf(byPeriod.get(m.period)).text}` : '这个月还没算过'"
          @click="go(m.period)"
        >
          <span class="m-num">{{ m.label }}<em v-if="shownYear !== OTHER">月</em></span>
          <span class="m-status">{{ m.has ? statusOf(byPeriod.get(m.period)).text : '' }}</span>
        </button>
      </div>

      <div class="legend">
        <span class="closed"><i />已结账 {{ statusCounts.closed }}</span>
        <span class="ready"><i />可确认 {{ statusCounts.ready }}</span>
        <span class="pending"><i />待补证据 {{ statusCounts.pending }}</span>
        <span class="evidence"><i />有新证据 {{ statusCounts.evidence }}</span>
      </div>
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

/* 顶部导航行 */
.head {
  display: flex;
  align-items: center;
  gap: var(--s3);
  margin-bottom: var(--s4);
}

.pager { display: flex; gap: var(--s1); }
.pager button {
  width: 30px;
  height: 30px;
  padding: 0;
  border: 1px solid var(--n3);
  border-radius: var(--r-sm);
  background: var(--n0);
  color: var(--n6);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  transition: color .15s, border-color .15s;
}
.pager button:hover:not(:disabled) { color: var(--n9); border-color: var(--n5); }
.pager button:disabled { opacity: .35; cursor: default; }

.when {
  font-family: var(--num);
  font-size: var(--t-xl);
  font-weight: 620;
  letter-spacing: -.01em;
  line-height: 1.2;
}

.state {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--t-sm);
  color: var(--n6);
  padding: 3px 10px;
  border-radius: 999px;
  background: var(--n2);
}
.state i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
}
.state.closed { color: var(--ok); background: var(--ok-bg); }
.state.ready { color: var(--accent); background: var(--accent-bg); }
.state.pending { color: var(--warn); background: var(--warn-bg); }
.state.evidence { color: var(--ok); background: var(--ok-bg); }

/* 主容器 */
.strip-card {
  border: 1px solid var(--n3);
  border-radius: var(--r-lg);
  background: var(--n0);
  overflow: hidden;
}

/* 年份标签行 */
.year-tabs {
  display: flex;
  gap: 0;
  padding: 0 var(--s3);
  border-bottom: 1px solid var(--n3);
  background: var(--n1);
  overflow-x: auto;
  scrollbar-width: none;
}
.year-tabs::-webkit-scrollbar { display: none; }
.year-tabs button {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  padding: 10px 16px;
  border: none;
  border-bottom: 2px solid transparent;
  background: none;
  font: 500 var(--t-sm)/1.4 var(--num);
  color: var(--n5);
  white-space: nowrap;
  cursor: pointer;
  transition: color .12s, border-color .12s;
  margin-bottom: -1px;
}
.year-tabs button:hover { color: var(--n8); }
.year-tabs button.on {
  color: var(--n9);
  font-weight: 640;
  border-bottom-color: var(--n9);
}
.year-tabs button small {
  font-family: var(--font);
  font-size: 10px;
  font-weight: 400;
  color: var(--n4);
}
.year-tabs button.on small { color: var(--n6); }

/* 月份网格 */
.month-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  padding: 16px 20px;
}
.month-grid.free { grid-template-columns: repeat(2, minmax(0, 1fr)); }

.m-cell {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 3px;
  min-height: 56px;
  padding: 10px 14px;
  border: 1px solid var(--n3);
  border-radius: var(--r-md);
  background: var(--n0);
  text-align: left;
  cursor: pointer;
  transition: all .12s;
}
.m-cell:hover:not(:disabled):not(.on) {
  border-color: var(--n5);
  background: var(--n1);
}

.m-num {
  font-family: var(--num);
  font-size: var(--t-base);
  font-weight: 640;
  line-height: 1.2;
  color: var(--n8);
}
.m-num em {
  margin-left: 1px;
  font-family: var(--font);
  font-size: var(--t-xs);
  font-style: normal;
  font-weight: 400;
  color: var(--n5);
}
.m-status {
  font-size: 11px;
  font-weight: 500;
  line-height: 1;
  color: var(--n5);
}

/* 状态色 */
.m-cell.closed { border-color: #c8e6d4; background: var(--ok-bg); }
.m-cell.closed .m-num { color: var(--ok); }
.m-cell.closed .m-status { color: var(--ok); }

.m-cell.ready { border-color: #c2d4f7; background: var(--accent-bg); }
.m-cell.ready .m-num { color: var(--accent); }
.m-cell.ready .m-status { color: var(--accent); }

.m-cell.pending { border-color: #f0d8a8; background: var(--warn-bg); }
.m-cell.pending .m-num { color: var(--warn); }
.m-cell.pending .m-status { color: var(--warn); }

.m-cell.evidence { border-color: #c8e6d4; background: var(--ok-bg); }
.m-cell.evidence .m-num { color: var(--ok); }
.m-cell.evidence .m-status { color: var(--ok); }

/* 选中态：深色边框 + 底色，保留状态色文字 */
.m-cell.on {
  border-color: var(--n8);
  background: var(--n8);
  box-shadow: 0 1px 4px rgba(0,0,0,.15);
}
.m-cell.on .m-num,
.m-cell.on .m-status,
.m-cell.on .m-num em { color: var(--n0); }

/* 空月份 */
.m-cell.off {
  border-color: var(--n2);
  background: var(--n1);
  cursor: default;
}
.m-cell.off .m-num { color: var(--n4); }
.m-cell.off .m-num em { color: var(--n3); }

/* 图例 */
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: var(--s3);
  padding: 10px 20px 12px;
  border-top: 1px solid var(--n3);
  background: var(--n1);
  color: var(--n6);
  font-size: var(--t-xs);
}
.legend span { display: inline-flex; align-items: center; gap: 5px; }
.legend i { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.legend .closed { color: var(--ok); }
.legend .ready { color: var(--accent); }
.legend .pending { color: var(--warn); }
.legend .evidence { color: var(--ok); }

@media (max-width: 640px) {
  .head { flex-wrap: wrap; }
  .month-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; padding: 12px; }
  .month-grid.free { grid-template-columns: minmax(0, 1fr); }
  .m-cell { min-height: 48px; padding: 8px 10px; }
  .legend { padding: 8px 12px; gap: var(--s2); }
}
</style>
