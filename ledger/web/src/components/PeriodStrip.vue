<script setup>
/* 常用月份就近切换，全年进度在月份面板查看。 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { CalendarDays, ChevronLeft, ChevronRight } from '@lucide/vue'
import LedgerTabs from './ui/LedgerTabs.vue'

const props = defineProps({
  periods: { type: Array, default: () => [] },
  modelValue: { type: String, default: '' },
  compact: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue'])

const OTHER = '其他'
const picker=ref(false),small=ref(false)
let screen
function resize(){small.value=screen.matches}
onMounted(()=>{screen=window.matchMedia('(max-width:600px)');resize();screen.addEventListener('change',resize)})
onUnmounted(()=>screen?.removeEventListener('change',resize))

function yearOf(p) { return /^\d{4}-\d{2}$/.test(p || '') ? p.slice(0, 4) : OTHER }
function pretty(p) { const m = /^(\d{4})-(\d{2})$/.exec(p || ''); return m ? `${m[1]} 年 ${Number(m[2])} 月` : p || '' }

const list = computed(() =>
  [...(props.periods || [])].sort((a, b) => String(b.period).localeCompare(String(a.period))),
)
const index = computed(() => list.value.findIndex((p) => p.period === props.modelValue))
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

function go(p) { picker.value=false;if (p && p !== props.modelValue) emit('update:modelValue', p) }
function step(dir) { const next = list.value[index.value + dir]; if (next) go(next.period) }

function statusOf(p) {
  if (!p) return { mark: '', text: '' }
  if (p.state === 'closed' && p.stale) return { mark: 'evidence', text: '有新资料' }
  if (p.state === 'closed') return { mark: 'closed', text: '已结账' }
  if (p.can_close) return { mark: 'ready', text: '可确认' }
  return { mark: 'pending', text: '待补资料' }
}

const statusCounts = computed(() => {
  const c = { closed: 0, ready: 0, pending: 0, evidence: 0 }
  for (const item of list.value){if(item.state==='closed')c.closed++;const mark=statusOf(item).mark||'pending';if(mark!=='closed')c[mark]++}
  return c
})
const yearCounts = computed(() => {
  const m = new Map()
  for (const item of list.value) { const y = yearOf(item.period); m.set(y, (m.get(y) || 0) + 1) }
  return m
})
const nearby=computed(()=>{
  const size=small.value?3:6
  const start=Math.max(0,Math.min(index.value-Math.floor(size/2),list.value.length-size))
  return list.value.slice(start,start+size).reverse()
})
</script>

<template>
  <div v-if="list.length" class="month-strip" :class="{compact}">
    <div class="month-nearby"><button class="month-step" :disabled="index<0||index>=list.length-1" aria-label="上一个月" @click="step(1)"><ChevronLeft :size="15"/></button><button v-for="item in nearby" :key="item.period" class="month-shortcut" :class="{selected:item.period===modelValue}" :title="`${pretty(item.period)} · ${statusOf(item).text}`" :aria-pressed="item.period===modelValue" @click="go(item.period)"><i :class="statusOf(item).mark"/>{{item.period}}</button><button class="month-step" :disabled="index<=0" aria-label="下一个月" @click="step(-1)"><ChevronRight :size="15"/></button></div>
    <n-popover trigger="click" :show="picker" :show-arrow="false" raw placement="bottom-end" @update:show="picker=$event"><template #trigger><button class="month-calendar" :aria-expanded="picker"><CalendarDays :size="15"/><span>全部月份</span></button></template>
      <div class="month-picker"><LedgerTabs v-model="shownYear" :options="years.map(y=>({key:y,label:y,count:yearCounts.get(y)}))" label="选择年份"/><div class="ps-grid" :class="{free:shownYear===OTHER}"><button v-for="month in months" :key="month.key" class="ps-cell" :class="[month.status.mark,{on:month.period===modelValue,off:!month.has}]" :disabled="!month.has" @click="go(month.period)"><span class="ps-m">{{month.label}}<em v-if="shownYear!==OTHER">月</em></span><span v-if="month.has" class="ps-s">{{month.status.text}}</span></button></div><div class="ps-legend"><span class="closed">已结账 {{statusCounts.closed}}</span><span class="ready">可确认 {{statusCounts.ready}}</span><span class="pending">待补资料 {{statusCounts.pending}}</span><span class="evidence">有新资料 {{statusCounts.evidence}}</span></div></div>
    </n-popover>
  </div>
</template>

<style scoped>
.month-strip{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 0;margin-bottom:16px;border-bottom:1px solid #e5ebf3}.month-nearby{display:flex;align-items:center;gap:7px;min-width:0;overflow:auto}.month-shortcut,.month-step,.month-calendar{display:flex;align-items:center;justify-content:center;gap:6px;flex:none;border:1px solid #e0e6ef;background:#fff;border-radius:5px;color:#667992;min-height:34px;padding:6px 10px;font-size:12px;cursor:pointer}.month-shortcut.selected{color:#3468f0;background:#edf3ff;border-color:#b8cdf8}.month-shortcut i{width:5px;height:5px;border-radius:50%;background:#9daabb}.month-shortcut i.closed{background:#3a986b}.month-shortcut i.evidence,.month-shortcut i.pending{background:#d3a252}.month-shortcut i.ready{background:#3468f0}.month-step{padding:5px}.month-step:disabled{opacity:.4;cursor:default}.month-calendar{border-color:transparent;background:transparent;white-space:nowrap;color:#3468f0}.month-picker{width:min(490px,calc(100vw - 24px));background:#fff;border:1px solid #dfe6f0;border-radius:8px;padding:10px 12px;box-shadow:0 10px 32px #233b5e20}.month-picker .ps-grid{padding:12px 0}.month-picker .ps-legend{padding:10px 0 0;background:#fff}.month-picker :deep(.ledger-tabs){padding:0 4px}@media(max-width:600px){.month-strip{gap:4px}.month-step{display:none}.month-shortcut{padding:6px 8px;font-size:11px}.month-nearby{gap:5px}.month-calendar{padding:5px;font-size:11px;gap:4px}.month-picker .ps-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}

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
