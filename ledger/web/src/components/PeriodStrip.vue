<script setup>
/* 一家店的账期切换。
 *
 * 摆法照日历来：翻页键在最左，后面是当前月份；选择区分成「年份」和「月份」两级。
 * 年份放在可滚动的窄栏里，一次只展开一年的十二个月，避免几十个账期继续横向平铺。
 * 月份仍按一到十二排——倒序是列表逻辑，不是日历逻辑。
 *
 * 没算过的月份留在原位、点不动。这样一年里哪几个月还没账，扫一眼就知道；
 * 只列算过的那几个月，缺口就看不见了。
 *
 * 每个月都显示状态，但用绿/蓝/红/橙四种固定语义，并在下方给图例。当前月份再用
 * 深色选中态强调，避免「正在看哪个月」和「这个月是否已结」混成同一件事。
 */
import { computed, ref, watch } from 'vue'

const props = defineProps({
  periods: { type: Array, default: () => [] },
  modelValue: { type: String, default: '' },
  compact: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue'])

//: 认不出年月的账期（「(未知账期)」）归到这一档，不能让它挤进某一年的十二格里。
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
  if (p.state === 'closed') return { mark: 'closed', text: '已结账' }
  if (p.can_close === false) return { mark: 'bad', text: '结不了' }
  if (p.can_close) return { mark: 'ready', text: '可结账' }
  return { mark: 'idle', text: '未结账' }
}

const status = computed(() => statusOf(current.value))
const statusCounts = computed(() => {
  const counts = { closed: 0, ready: 0, bad: 0, idle: 0 }
  for (const item of list.value) counts[statusOf(item).mark || 'idle'] += 1
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

    <div class="period-levels">
      <div class="year-level">
        <div class="level-title">年份</div>
        <div class="years" role="listbox" aria-label="选择年份">
          <button
            v-for="y in years"
            :key="y"
            type="button"
            :class="{ on: y === shownYear }"
            :aria-selected="y === shownYear"
            @click="pickYear(y)"
          >
            <span>{{ y }}</span><small>{{ yearCounts.get(y) }} 期</small>
          </button>
        </div>
      </div>
      <div class="month-level">
        <div class="level-title month-title">
          <span>{{ shownYear === OTHER ? '其他账期' : `${shownYear} 年` }}</span>
          <small>{{ shownYear === OTHER ? '按原始名称选择' : '选择月份' }}</small>
        </div>
        <div class="months" :class="{ free: shownYear === OTHER }" role="listbox" aria-label="选择月份">
          <button
            v-for="m in months"
            :key="m.key"
            type="button"
            :class="[m.status?.mark, { on: m.period === modelValue, off: !m.has }]"
            :disabled="!m.has"
            :aria-selected="m.period === modelValue"
            :title="m.has ? `${pretty(m.period)} · ${statusOf(byPeriod.get(m.period)).text}` : '这个月还没算过'"
            @click="go(m.period)"
          >
            <span class="month-number">{{ m.label }}<em v-if="shownYear !== OTHER">月</em></span>
            <small>{{ m.has ? statusOf(byPeriod.get(m.period)).text : '暂无' }}</small>
            <i v-if="m.has" class="month-state" />
          </button>
        </div>
      </div>
    </div>
    <div class="legend">
      <span class="closed"><i />已结账 {{ statusCounts.closed }}</span>
      <span class="ready"><i />可确认 {{ statusCounts.ready }}</span>
      <span class="bad"><i />待完善 {{ statusCounts.bad }}</span>
      <span class="idle"><i />未结账 {{ statusCounts.idle }}</span>
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
  margin-bottom: var(--s3);
}

.pager { display: flex; gap: var(--s1); }
.pager button {
  width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid var(--n3);
  border-radius: var(--r-sm);
  background: var(--n0);
  color: var(--n6);
  font-size: 17px;
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
  padding: 3px 9px;
  border-radius: 999px;
  background: var(--n2);
}
.state i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}
.state.closed { color: var(--ok); background: var(--ok-bg); }
.state.ready { color: var(--accent); background: var(--accent-bg); }
.state.bad { color: var(--bad); background: var(--bad-bg); }
.state.idle { color: var(--n5); }

.period-levels {
  display: grid;
  grid-template-columns: 112px minmax(0, 1fr);
  min-height: 178px;
  overflow: hidden;
  border: 1px solid var(--n3);
  border-radius: var(--r-md);
  background: var(--n0);
}
.year-level {
  min-width: 0;
  padding: var(--s2);
  border-right: 1px solid var(--n3);
  background: var(--n1);
}
.month-level { min-width: 0; padding: var(--s2) var(--s3) var(--s3); }
.level-title {
  padding: 4px 7px 7px;
  color: var(--n5);
  font-size: var(--t-xs);
  font-weight: 560;
  letter-spacing: .04em;
}
.month-title { display: flex; align-items: baseline; justify-content: space-between; }
.month-title span { color: var(--n8); font-family: var(--num); font-size: var(--t-sm); }
.month-title small { color: var(--n5); font-weight: 400; letter-spacing: 0; }

.years {
  display: grid;
  gap: 3px;
  max-height: 140px;
  overflow-y: auto;
  scrollbar-width: thin;
}
.months { display: grid; grid-template-columns: repeat(4, minmax(76px, 1fr)); gap: 6px; }
.months.free { grid-template-columns: repeat(2, minmax(110px, 1fr)); }

.years button,
.months button {
  border: 1px solid transparent;
  background: transparent;
  color: var(--n6);
  font: inherit;
  line-height: 1;
  cursor: pointer;
  border-radius: var(--r-sm);
  transition: color .12s, background .12s;
}
.years button {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  font-family: var(--num);
  font-size: var(--t-xs);
  padding: 7px 8px;
  text-align: left;
}
.years button small { color: var(--n5); font-family: var(--font); font-size: 10px; }
.months button {
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: center;
  min-width: 0;
  min-height: 42px;
  padding: 6px 9px;
  border-color: var(--n3);
  text-align: left;
  position: relative;
}
.months.free button {
  min-height: 36px;
}
.years button:hover,
.months button:hover:not(:disabled) { color: var(--n9); border-color: var(--n5); background: var(--n1); }

.years button.on { color: var(--n0); background: var(--n8); font-weight: 560; }
.years button.on small { color: var(--n3); }
.months button.on { color: var(--n0); border-color: var(--n8); background: var(--n8); font-weight: 560; }
.months button.closed { color: var(--ok); background: var(--ok-bg); }
.months button.ready { color: var(--accent); background: var(--accent-bg); }
.months button.bad { color: var(--bad); background: var(--bad-bg); }
.months button.idle { color: var(--warn); background: var(--warn-bg); }
.months button.on { color: var(--n0); background: var(--n8); }
.month-number { font-family: var(--num); font-size: var(--t-sm); font-weight: 600; }
.month-number em { margin-left: 2px; font-family: var(--font); font-size: 10px; font-style: normal; font-weight: 400; }
.months button small { color: currentColor; font-size: 10px; opacity: .75; }
.month-state {
  position: absolute;
  right: 5px;
  top: 5px;
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: currentColor;
}
/* 没算过的月份：淡到不像能点，但位置还占着——缺哪个月是要看见的。 */
.months button.off { color: var(--n4); cursor: default; }

.legend {
  display: flex;
  flex-wrap: wrap;
  gap: var(--s3);
  margin-top: var(--s2);
  padding-left: 124px;
  color: var(--n6);
  font-size: var(--t-xs);
}
.legend span { display: inline-flex; align-items: center; gap: 5px; }
.legend i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.legend .closed { color: var(--ok); }
.legend .ready { color: var(--accent); }
.legend .bad { color: var(--bad); }
.legend .idle { color: var(--warn); }

@media (max-width: 640px) {
  .head { flex-wrap: wrap; }
  .period-levels { grid-template-columns: minmax(0, 1fr); }
  .year-level { border-right: 0; border-bottom: 1px solid var(--n3); }
  .years { display: flex; overflow-x: auto; overflow-y: hidden; max-height: none; }
  .years button { min-width: 92px; gap: var(--s2); }
  .months { grid-template-columns: repeat(3, minmax(72px, 1fr)); }
  .months.free { grid-template-columns: minmax(0, 1fr); }
  .legend { padding-left: 0; gap: var(--s2); }
}
</style>
