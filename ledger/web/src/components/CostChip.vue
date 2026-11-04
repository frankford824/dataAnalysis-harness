<script setup>
import { computed } from 'vue'
import { Coins } from '@lucide/vue'
import { costLevel, costs, yuan } from '../agentCosts'

const props = defineProps({ compact: Boolean, placement: { type: String, default: 'bottom-start' } })

const summary = computed(() => costs.summary)
const level = computed(() => costLevel(summary.value))
const budget = computed(() => Number(summary.value?.budget_cny) || 0)
const ratio = computed(() => (budget.value ? Math.min(1, (Number(summary.value?.month_cny) || 0) / budget.value) : 0))
const label = computed(() => {
  const s = summary.value
  if (!s) return props.compact ? '' : '模型费用 读取中…'
  return props.compact ? `本月 ${yuan(s.month_cny)}` : `模型费用 本月 ${yuan(s.month_cny)} · 今日 ${yuan(s.today_cny)}`
})
const models = computed(() => [...(summary.value?.models || [])].sort((a, b) => (Number(b.cost) || 0) - (Number(a.cost) || 0)))

const count = n => (n == null ? '—' : Number(n).toLocaleString('zh-CN'))
// 缓存命中率 0~1；没有数据时是 null。
const hasRate = v => v != null && v !== '' && !Number.isNaN(Number(v))
const rate = v => (hasRate(v) ? `${Math.round(Number(v) * 100)}%` : '—')
const showCache = computed(() => hasRate(summary.value?.cache_rate))
const sourceCache = computed(() => (summary.value?.sources || []).some(s => hasRate(s.cache_rate)))
// token 数以「万」为单位，不到 0.1 万的写成 <0.1。
function wan(n) {
  const v = Number(n) || 0
  if (!v) return '0'
  if (v < 1000) return '<0.1'
  return (v / 10000).toLocaleString('zh-CN', { maximumFractionDigits: 1 })
}
const clockFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
const updated = computed(() => {
  const raw = summary.value?.updated_at
  if (raw == null || raw === '') return ''
  const d = typeof raw === 'number'
    ? new Date(raw < 1e12 ? raw * 1000 : raw)
    : new Date(/^\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d(\.\d+)?)?$/.test(raw) ? `${raw.replace(' ', 'T')}+08:00` : raw)
  return Number.isNaN(d.getTime()) ? '' : clockFormat.format(d)
})
</script>

<template>
  <n-popover v-if="label" trigger="click" :placement="placement" :show-arrow="false" :z-index="4300" style="padding: 0">
    <template #trigger>
      <button type="button" class="cost-chip" :class="[level, { compact }]" :title="compact ? '本月模型费用，点开看明细' : '点开看各部分和各模型的费用'">
        <Coins :size="compact ? 12 : 13" aria-hidden="true" /><span class="num">{{ label }}</span>
      </button>
    </template>
    <div v-if="summary" class="cost-pop" role="dialog" aria-label="模型费用明细">
      <header class="cost-head">
        <strong>模型费用</strong>
        <span class="muted">{{ summary.month || '' }}</span>
      </header>
      <div class="cost-budget">
        <div class="cost-budget-line">
          <span>本月 <b class="num" :class="level">{{ yuan(summary.month_cny) }}</b></span>
          <span v-if="budget" class="muted num">预算 {{ yuan(budget) }} · {{ ratio > 0 && ratio < 0.01 ? '<1' : Math.round(ratio * 100) }}%</span>
        </div>
        <div v-if="budget" class="cost-bar" :class="level" role="progressbar" :aria-valuenow="Math.round(ratio * 100)" aria-valuemin="0" aria-valuemax="100" aria-label="本月费用占预算">
          <span :style="{ width: `${ratio * 100}%` }" />
        </div>
        <div class="cost-stats">
          <span>今日 <b class="num">{{ yuan(summary.today_cny) }}</b></span>
          <span>最近一小时 <b class="num">{{ yuan(summary.last_hour_cny) }}</b></span>
        </div>
        <p v-if="showCache" class="cost-cache" title="重复用到的内容按缓存价计费，命中越高越省">
          缓存命中 <b class="num">{{ rate(summary.cache_rate) }}</b><template v-if="Number(summary.cache_saved_cny) > 0"> · 本月省 <b class="num">{{ yuan(summary.cache_saved_cny) }}</b></template>
        </p>
      </div>

      <section v-if="summary.sources?.length">
        <h4>按来源</h4>
        <table class="cost-table">
          <thead><tr><th>来源</th><th>本月</th><th>今日</th><th>调用次数</th><th v-if="sourceCache">缓存命中</th></tr></thead>
          <tbody>
            <tr v-for="s in summary.sources" :key="s.key">
              <td>{{ s.label }}</td>
              <td class="num">{{ yuan(s.month_cny) }}</td>
              <td class="num">{{ yuan(s.today_cny) }}</td>
              <td class="num">{{ count(s.calls) }}</td>
              <td v-if="sourceCache" class="num">{{ rate(s.cache_rate) }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section v-if="models.length">
        <h4>按模型</h4>
        <table class="cost-table">
          <thead><tr><th>模型</th><th>调用</th><th>输入 / 输出（万 tokens）</th><th>费用</th></tr></thead>
          <tbody>
            <tr v-for="m in models" :key="m.model">
              <td class="cost-model" :title="m.model">{{ m.model }}</td>
              <td class="num">{{ count(m.calls) }}</td>
              <td class="num">{{ wan(m.tokens_in) }} / {{ wan(m.tokens_out) }}</td>
              <td class="num">{{ yuan(m.cost) }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <footer class="cost-foot">
        按<a v-if="summary.price_source" :href="summary.price_source" target="_blank" rel="noopener noreferrer">阿里云百炼官方价目表</a><template v-else>阿里云百炼官方价目表</template>计算（含缓存命中折扣）<template v-if="updated"> · 更新于 {{ updated }}</template>
      </footer>
    </div>
  </n-popover>
</template>

<style scoped>
.cost-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 26px;
  padding: 0 10px;
  border: 1px solid var(--n3);
  border-radius: 999px;
  background: var(--n1);
  color: var(--n8);
  font: 560 12px/1 var(--sans, inherit);
  white-space: nowrap;
  cursor: pointer;
}
.cost-chip:hover { border-color: var(--n4); background: var(--n0); }
.cost-chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.cost-chip svg { color: var(--n6); flex: none; }
.cost-chip.compact { height: 22px; padding: 0 8px; font-size: 11px; font-weight: 500; }
.cost-chip.warn { border-color: #f3d19e; background: var(--warn-bg); color: var(--warn); }
.cost-chip.bad { border-color: #f2b8b5; background: var(--bad-bg); color: var(--bad); }
.cost-chip.warn svg, .cost-chip.bad svg { color: currentColor; }

.cost-pop { width: 420px; max-width: calc(100vw - 32px); padding: 14px 16px 12px; font-size: 12.5px; color: var(--n8); }
.cost-head { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 10px; }
.cost-head strong { font-size: 14px; }
.muted { color: var(--n5); font-size: 11.5px; }
.cost-budget { padding-bottom: 12px; border-bottom: 1px solid var(--n2); }
.cost-budget-line { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.cost-budget-line b { font-size: 18px; font-weight: 640; }
b.warn { color: var(--warn); }
b.bad { color: var(--bad); }
.cost-bar { height: 6px; margin: 8px 0 10px; border-radius: 3px; background: var(--n2); overflow: hidden; }
.cost-bar span { display: block; height: 100%; border-radius: 3px; background: var(--accent); }
.cost-bar.warn span { background: var(--warn); }
.cost-bar.bad span { background: var(--bad); }
.cost-stats { display: flex; gap: 20px; color: var(--n6); }
.cost-stats b { color: var(--n8); font-weight: 600; margin-left: 4px; }
.cost-cache { margin: 8px 0 0; color: var(--n6); }
.cost-cache b { color: var(--ok, var(--n8)); font-weight: 600; }
section h4 { margin: 12px 0 6px; font-size: 11.5px; font-weight: 560; letter-spacing: .06em; color: var(--n5); }
.cost-table { width: 100%; border-collapse: collapse; }
.cost-table th, .cost-table td { padding: 5px 4px; border-bottom: 1px solid var(--n2); text-align: right; white-space: nowrap; }
.cost-table th { font-weight: 500; font-size: 11px; color: var(--n5); }
.cost-table th:first-child, .cost-table td:first-child { text-align: left; }
.cost-model { max-width: 150px; overflow: hidden; text-overflow: ellipsis; }
.num { font-variant-numeric: tabular-nums; }
.cost-foot { margin-top: 10px; font-size: 11px; color: var(--n5); line-height: 1.6; }
.cost-foot a { color: var(--accent); text-decoration: none; }
.cost-foot a:hover { text-decoration: underline; }
</style>
