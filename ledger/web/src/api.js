/* 后端调用。
 *
 * 出错时一定要把后端那句话原样带出来。FastAPI 的 detail 里写的是「这张表里没有
 * 数据行」「结不了：还缺三张表」这类人能照着做事的话；替换成「请求失败」等于把
 * 唯一有用的信息扔了。
 */

import {notifyAuthentication} from './telemetry.js'
import {asked, cacheState, cached, checked, dataChanged, isStale, markAllStale, needsCheck, served, store} from './apiCache.js'
export {startTelemetry as startClientTelemetry,recordTelemetry} from './telemetry.js'

// 手动刷新期间发出的读请求带 X-Ledger-Fresh，让后端跳过缓存；平时的加载和后台自动刷新不带。
let freshDepth = 0
export async function withFresh(work) {
  freshDepth++
  try { return await work() } finally { freshDepth-- }
}
export function freshInit(init = {}) {
  if (!freshDepth || (init.method && init.method.toUpperCase() !== 'GET')) return init
  return { ...init, headers: { ...(init.headers || {}), 'X-Ledger-Fresh': '1' } }
}

// 轮询进度要的是服务端此刻的数，不走本地副本。
let liveDepth = 0
export function withLive(work) {
  liveDepth++
  try { return work() } finally { liveDepth-- }
}

// 进度、任务、登录状态这类读要么在轮询，要么跟人有关，不留副本；导出和对话也不留。
const UNCACHED = /^\/api\/(agent\/|upload\/|recompute\/|fees\/jobs|commission-v2\/|automation\/questions|freight-review\/shipments(\?|$))|\/pricing-status|\/export|\.csv(\?|$)/
// 这些 POST 只是试算，不改数据，不该让副本过期。
const PROBES = /[/-]preview(\?|$)|\/onboard\/try$|\/fees\/suggest$|\/commission\/plan$/
// 写完的头几秒，读先等服务端一会儿，免得人刚改完看见的还是改之前的数。
const WRITE_GRACE = 3000
const WRITE_WAIT = 1500
let writtenUntil = 0
const writeListeners = new Set()
const inflight = new Map()

export { cacheState, onDataChanged, newView as newReadView } from './apiCache.js'

export function onWrite(fn) {
  writeListeners.add(fn)
  return () => writeListeners.delete(fn)
}

/** 服务端的数可能变了：副本全部算过期，写完的头几秒读先等服务端。 */
export function expireReads() {
  markAllStale()
  writtenUntil = Date.now() + WRITE_GRACE
}

function wrote() {
  expireReads()
  for (const fn of writeListeners) fn()
}

async function request(path, init, background = false) {
  let res
  try {
    res = await fetch(path, init)
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    // 网络层失败没有 detail 可取。这套系统跑在内网，多半是服务没起来。
    throw new Error('连不上服务。它可能没在跑，或者这台机器不在内网里。')
  }
  const text = await res.text()
  let body = null, json = !!text
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    body = null
    json = false
  }
  if (!res.ok) {
    const detail=typeof body?.detail==='string'?body.detail:null
    if (!background) notifyAuthentication(res.status,detail)
    throw Object.assign(new Error(detail || (res.status>=500?'服务暂时未能完成请求，请稍后重试':res.status===404?'没有找到对应数据，请刷新后重试':res.status===422?'请检查填写内容':`请求失败（${res.status}）`)),{status:res.status})
  }
  return { body, text, json }
}

function abortable(job, signal) {
  if (!signal) return job
  const aborted = () => new DOMException('请求已取消', 'AbortError')
  if (signal.aborted) return Promise.reject(aborted())
  return new Promise((resolve, reject) => {
    const abort = () => reject(aborted())
    signal.addEventListener('abort', abort, { once: true })
    job.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort))
  })
}

// 同一个地址同时只发一次；先走的调用方取消了也让它跑完，结果留作副本。
function fetchShared(path, headers, background = false) {
  const key = headers?.['X-Ledger-Fresh'] ? `fresh ${path}` : path
  const running = inflight.get(key)
  if (running) return { job: running, joined: true }
  const at = Date.now()
  asked(path, at)
  if (background) cacheState.revalidating++
  const job = request(path, headers ? { headers } : {}, background)
    .then((res) => ({ ...res, changed: res.json && store(path, res.text, at) }))
    .finally(() => {
      checked(path)
      inflight.delete(key)
      if (background) cacheState.revalidating--
    })
  inflight.set(key, job)
  return { job, joined: false }
}

function revalidate(path) {
  const { job } = fetchShared(path, null, true)
  job.then((res) => { if (res.changed) dataChanged(path) }, () => {})
  return job
}

async function read(path, init) {
  const hit = freshDepth || liveDepth ? null : await cached(path)
  if (hit) {
    if (Date.now() < writtenUntil && isStale(path)) {
      const latest = await Promise.race([
        revalidate(path).catch(() => null),
        new Promise((resolve) => setTimeout(resolve, WRITE_WAIT)),
      ])
      if (latest?.json) { served(path); return JSON.parse(latest.text) }
    } else if (needsCheck(path)) {
      revalidate(path).catch(() => {})
    }
    served(path)
    return JSON.parse(hit.text)
  }
  const { job, joined } = fetchShared(path, freshInit(init).headers)
  const res = await abortable(job, init.signal)
  served(path)
  return joined && res.json ? JSON.parse(res.text) : res.body
}

async function call(path, init = {}) {
  const method = (init.method || 'GET').toUpperCase()
  if (method === 'GET' && init.body == null && path.startsWith('/api/') && !UNCACHED.test(path)) return read(path, init)
  const { body } = await request(path, freshInit(init))
  if (method !== 'GET' && !PROBES.test(path)) wrote()
  return body
}

function query(params) {
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null && v !== '') q.set(k, v)
  }
  const s = q.toString()
  return s ? `?${s}` : ''
}

export const api = {
  operatorStatus: () => call('/api/commission-v2/status'),
  operatorLogin: (name,password) => call('/api/commission-v2/session',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({name,password})}),
  operatorLogout: () => call('/api/commission-v2/session',{method:'DELETE'}),
  operationsReport: (params, options={}) => call(`/api/operations/month${query(params)}`,options),
  operationsEvidence: (params, options={}) => call(`/api/operations/evidence${query(params)}`,options),
  operationsExport: params => `/api/operations/export.csv${query(params)}`,
  automationRuleContracts: () => call('/api/automation/rule-contracts'),
  automationMonth: (period, options={}) => call(`/api/automation/months/${encodeURIComponent(period)}`,options),
  automationAdvance: (period, body) => call(`/api/automation/months/${encodeURIComponent(period)}/advance`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)}),
  automationDecision: id => call(`/api/automation/decisions/${encodeURIComponent(id)}`),
  automationDecisionRevoke: (id,body) => call(`/api/automation/decisions/${encodeURIComponent(id)}/revoke`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)}),
  sentinelStatus: () => call('/api/operations/sentinel/status'),
  sentinelNotifications: (limit=30) => call(`/api/operations/sentinel/notifications?limit=${limit}`),
  sentinelEvents: (limit=50) => call(`/api/operations/sentinel/events?limit=${limit}`),
  automationDecisionPreview: body => call('/api/automation/decisions/preview',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)}),
  automationDecisionApply: body => call('/api/automation/decisions/apply',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)}),
  automationClosePreview: body => call('/api/automation/close-batches/preview',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)}),
  automationCloseConfirm: body => call('/api/automation/close-batches/confirm',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)}),
  agentAuthority: (period, options={}) => call(`/api/automation/agent-authority${query({period})}`,options),
  agentAuthorityPreview: body => call('/api/automation/agent-authority/preview',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)}),
  agentAuthorityApply: body => call('/api/automation/agent-authority/apply',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)}),
  agentAuthorityRevoke: body => call('/api/automation/agent-authority/revoke',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)}),
  agentQuestions: (storeId, period, options={}) => call(`/api/automation/questions${query({store_id:storeId,period})}`,options),
  agentAsk: body => call('/api/automation/questions',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)}),
  agentQuestionAcknowledge: id => call(`/api/automation/questions/${encodeURIComponent(id)}/acknowledge`,{method:'POST'}),
  agentInbox: (period, options={}) => call(`/api/automation/inbox${query({period})}`,options),
  upgradeConfirm: body => call('/api/automation/upgrade/confirm',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)}),
  freightShipmentLatest: (period, options={}) => call(`/api/freight-review/shipments${query({period})}`, options),
  freightShipmentStart: period => call('/api/freight-review/shipments', {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({period})}),
  freightShipmentResult: (id,params,options={}) => call(`/api/freight-review/shipments/${id}${query(params)}`,options),
  freightShipmentDetail: (id,charge,params,options={}) => call(`/api/freight-review/shipments/${id}/charges/${charge}${query(params)}`,options),
  freightShipmentExport: async (id,params) => {
    const response=await fetch(`/api/freight-review/shipments/${id}/export.csv${query(params)}`)
    if(!response.ok){const error=await response.json().catch(()=>({}));throw new Error(typeof error.detail==='string'?error.detail:'导出暂未完成，请稍后重试')}
    return response.blob()
  },
  freightReview: (params, options={}) => call(`/api/freight-review${query(params)}`, options),
  freightReviewExport: async params => {
    const response=await fetch(`/api/freight-review/export.csv${query(params)}`)
    if(!response.ok){const error=await response.json().catch(()=>({}));throw new Error(typeof error.detail==='string'?error.detail:'导出暂未完成，请稍后重试')}
    return response.blob()
  },
  statementReview: (storeId,period,runId,offset=0) => call(`/api/stores/${encodeURIComponent(storeId)}/periods/${encodeURIComponent(period)}/statement-review${query({run_id:runId,offset})}`),
  confirmStatementReview: (storeId,period,body) => call(`/api/stores/${encodeURIComponent(storeId)}/periods/${encodeURIComponent(period)}/statement-review`,{
    method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body),
  }),
  revokeStatementReview: (storeId,period,body) => call(`/api/stores/${encodeURIComponent(storeId)}/periods/${encodeURIComponent(period)}/statement-review/revoke`,{
    method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body),
  }),
  pricingStatus: (id, period, init) => call(`/api/stores/${encodeURIComponent(id)}/pricing-status${query({period})}`, init),
  pricingGaps: (runId, params, options = {}) => call(`/api/runs/${runId}/pricing-gaps${query(params)}`, options),
  orderIssues: (runId, issueId, params, options = {}) => call(`/api/runs/${runId}/order-issues/${encodeURIComponent(issueId)}${query(params)}`, options),
  coverageGaps: (runId, params, options = {}) => call(`/api/runs/${runId}/coverage-gaps${query(params)}`, options),
  saveCostLine: (storeId, period, body) =>
    call(`/api/stores/${encodeURIComponent(storeId)}/periods/${encodeURIComponent(period)}/cost-lines`, {
      method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(body),
    }),
  previewCostBatch: (storeId, period, runId, file, reason) => {
    const form = new FormData(); form.append('file', file)
    return call(`/api/stores/${encodeURIComponent(storeId)}/periods/${encodeURIComponent(period)}/cost-lines/batch-preview${query({run_id:runId,default_reason:reason})}`, {
      method:'POST',body:form,
    })
  },
  applyCostBatch: (storeId, period, runId, file, reason, preview) => {
    const form = new FormData(); form.append('file', file)
    return call(`/api/stores/${encodeURIComponent(storeId)}/periods/${encodeURIComponent(period)}/cost-lines/batch-apply${query({run_id:runId,default_reason:reason,expected_file_sha:preview.file_sha,expected_line_revision:preview.line_revision})}`, {
      method:'POST',body:form,
    })
  },
  navigation: () => call('/api/navigation'),
  bootstrap: () => call('/api/bootstrap'),

  overview: (params, options = {}) => call(`/api/overview${query(params)}`, options),
  trend: (params, options = {}) => call(`/api/trend${query(params)}`, options),
  gaps: (params, options = {}) => call(`/api/gaps${query(params)}`, options),
  store: (id, options = {}) => call(`/api/stores/${encodeURIComponent(id)}`, options),
  period: (id, period, options = {}) =>
    call(`/api/stores/${encodeURIComponent(id)}/periods/${encodeURIComponent(period)}`, options),
  recompute: (id) =>
    call(`/api/stores/${encodeURIComponent(id)}/recompute`, { method: 'POST' }),
  manualCostPreview: (id, period, runId, costs, lineRevision = 0) =>
    call(`/api/stores/${encodeURIComponent(id)}/periods/${encodeURIComponent(period)}/manual-cost-preview`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({run_id: runId, costs, line_revision: lineRevision}),
    }),
  close: (id, period, note = '', ignoredBlockers = [], runId = null, decision = null) =>
    call(`/api/stores/${encodeURIComponent(id)}/periods/${encodeURIComponent(period)}/close`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ note, ignored_blockers: ignoredBlockers, run_id: runId,
        ...(decision || {}) }),
    }),
  reopen: (id, period, note) =>
    call(`/api/stores/${encodeURIComponent(id)}/periods/${encodeURIComponent(period)}/reopen`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ note }),
    }),

  /** 交表。
   *
   * 这里用 XHR 而不是 fetch，只为一件事：`upload.onprogress`。传一份两百兆的
   * 订单明细要几十秒，fetch 给不出已经传了多少，界面就只能转圈——而转圈证明不了
   * 它还活着。`token` 是这次交表的号，服务端拿它报解析到哪一步了。
   */
  upload: (files, { token = '', onSent } = {}) =>
    new Promise((resolve, reject) => {
      const form = new FormData()
      // 文件名要原样带上：引擎靠它认店铺和账期。
      for (const f of files) form.append('files', f, f.name)
      const xhr = new XMLHttpRequest()
      xhr.open('POST', `/api/upload${query({ token })}`)
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onSent) onSent(e.loaded, e.total)
      }
      xhr.onload = () => {
        let body = null
        try {
          body = xhr.responseText ? JSON.parse(xhr.responseText) : null
        } catch {
          body = null
        }
        if (xhr.status >= 200 && xhr.status < 300) { wrote(); resolve(body) }
        else reject(new Error(body?.detail || xhr.responseText || `请求失败（${xhr.status}）`))
      }
      xhr.onerror = () =>
        reject(new Error('连不上服务。它可能没在跑，或者这台机器不在内网里。'))
      xhr.send(form)
    }),
  uploadProgress: (token) => call(`/api/upload/progress/${encodeURIComponent(token)}`),
  recomputeProgress: () => call('/api/recompute/progress'),
  dropFile: (storeId, name) =>
    call(`/api/stores/${encodeURIComponent(storeId)}/files${query({ name })}`, {
      method: 'DELETE',
    }),

  search: (params, options = {}) => call(`/api/search${query(params)}`, options),
  indexStatus: () => call('/api/index/status'),
  indexFiles: () => call('/api/index/files'),
  indexErrors: () => call('/api/index/errors'),
  indexStorage: () => call('/api/index/storage'),
  indexPreview: (params, options = {}) => call(`/api/index/preview${query(params)}`, options),
  orderFeedStatus: () => call('/api/order-feed/status'),
  drill: (runId, nodeId, params, options = {}) =>
    call(`/api/runs/${runId}/drill/${encodeURIComponent(nodeId)}${query(params)}`, options),

  stores: () => call('/api/stores'),
  patchStore: (id, patch) =>
    call(`/api/stores/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(patch),
    }),
  addStore: (store) =>
    call('/api/stores', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(store),
    }),

  commission: (period) => call(`/api/commission${query({ period })}`),
  commissionConfig: (storeId) => call(`/api/commission/config${query({ store_id: storeId })}`),
  commissionProducts: (params) => call(`/api/commission/products${query(params)}`),
  commissionPlan: (plan, apply) =>
    call(`/api/commission/plan${query({ apply: apply ? 'true' : '' })}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(plan),
    }),
  uploadCommission: (file, recomputeStores) => {
    const form = new FormData()
    form.append('file', file, file.name)
    return call(
      `/api/commission/config${query({ recompute_stores: recomputeStores ? 'true' : '' })}`,
      { method: 'POST', body: form },
    )
  },

  roles: (source, options = {}) => call(`/api/roles${query({ source })}`, options),
  draft: (sha, params, options = {}) => call(`/api/onboard/${sha}${query(params)}`, options),
  assist: (sha, params, options = {}) => call(`/api/onboard/${sha}/assist${query(params)}`, options),
  onboardTry: (commit) =>
    call('/api/onboard/try', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(commit),
    }),
  onboard: (commit) =>
    call('/api/onboard', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(commit),
    }),

  fees: (params, options = {}) => call(`/api/fees${query(params)}`, options),
  feesPreview: (body) =>
    call('/api/fees/preview', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }),
  feesApply: (body) =>
    call('/api/fees', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }),
  feeJobs: (options = {}) => call('/api/fees/jobs', options),
  feeJobRetry: (id) => call(`/api/fees/jobs/${encodeURIComponent(id)}/retry`, {method:'POST'}),
  feesSuggest: (body) =>
    call('/api/fees/suggest', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }),
}
