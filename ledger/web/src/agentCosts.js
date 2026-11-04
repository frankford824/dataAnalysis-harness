/* 整个系统的模型费用：台账助手、Agent Worker、台账识别加在一起。
 *
 * 数据主要从助手面板的实时事件里来（连上时的 hello，之后每轮回答完、总额变了时
 * 的 costs）。事件流断了才退回每分钟问一次 /api/agent/costs。这里单独用 fetch，
 * 费用接口出错不该把助手标成离线。
 */

import { reactive } from 'vue'

const POLL_EVERY = 60000
// 刚断开的那一下多半是 EventSource 在重连，等一会儿再开始问。
const GRACE = 3000

export const costs = reactive({ summary: null, live: false })

let timer = null

export function setCosts(summary) {
  if (summary && typeof summary === 'object') costs.summary = summary
}

async function poll() {
  timer = null
  if (costs.live) return
  try {
    const res = await fetch('/api/agent/costs')
    if (res.ok) setCosts(await res.json())
  } catch {
    // 问不到就保留上一次的数，下一分钟再问。
  }
  if (!costs.live && !timer) timer = setTimeout(poll, POLL_EVERY)
}

export function setCostsLive(on) {
  costs.live = on
  if (on) {
    clearTimeout(timer)
    timer = null
  } else if (!timer) {
    timer = setTimeout(poll, costs.summary ? POLL_EVERY : GRACE)
  }
}

/** ¥ 金额，两位小数；大于 0 但不到一分钱的写成「<¥0.01」，免得看起来像没花钱。 */
export function yuan(value) {
  if (value == null || value === '' || Number.isNaN(Number(value))) return '—'
  const n = Number(value)
  if (n > 0 && n < 0.01) return '<¥0.01'
  return `¥${n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

/** 本月花费占预算的程度：'' / 'warn'（≥70%）/ 'bad'（≥90%）。 */
export function costLevel(summary) {
  const budget = Number(summary?.budget_cny) || 0
  if (!budget) return ''
  const ratio = (Number(summary?.month_cny) || 0) / budget
  return ratio >= 0.9 ? 'bad' : ratio >= 0.7 ? 'warn' : ''
}
