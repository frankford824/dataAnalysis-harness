/* 台账助手的后端调用。
 *
 * 对话接口是 POST 回来一条 text/event-stream，EventSource 只能发 GET，所以这里用
 * fetch 拿到流之后自己按 SSE 的规矩拆行。data 一律是 JSON。
 */

const CJK = /[\u3400-\u9fff]/
const NETWORK = /network|fetch|load failed|connection|socket|timed? ?out|ECONN|aborted/i

/** 给人看的错误说明。浏览器抛出来的 TypeError「network error」「Failed to fetch」
 *  之类一律说成「网络中断」；其他不是中文的说明换成 fallback。 */
export function errorText(error, fallback = '这一轮没有完成，请稍后再试') {
  if (error?.network) return '网络中断'
  const message = typeof error === 'string' ? error : error?.message || ''
  if (error instanceof TypeError || (!CJK.test(message) && NETWORK.test(message))) return '网络中断'
  return CJK.test(message) ? message : fallback
}

// 网络断开或服务端 5xx 时通知面板，在线状态立刻变灰。
let onFailure = null
export const onAgentFailure = fn => { onFailure = fn }
const fail = error => {
  if (error?.network || error?.status >= 500) onFailure?.(error)
  return error
}
const networkError = () => Object.assign(new Error('网络中断'), { network: true })

async function readError(res) {
  const text = await res.text().catch(() => '')
  let detail = null
  try {
    const body = text ? JSON.parse(text) : null
    detail = typeof body?.detail === 'string' ? body.detail : typeof body?.message === 'string' ? body.message : null
  } catch {
    detail = null
  }
  const fallback = res.status >= 500 ? '助手暂时没有响应，请稍后再试' : res.status === 404 ? '没有找到这段对话' : `请求失败（${res.status}）`
  return Object.assign(new Error(detail && CJK.test(detail) ? detail : fallback), { status: res.status })
}

async function call(path, init) {
  let res
  try {
    res = await fetch(path, init)
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    throw fail(networkError())
  }
  if (!res.ok) throw fail(await readError(res))
  let text
  try {
    text = await res.text()
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    throw fail(networkError())
  }
  try {
    return text ? JSON.parse(text) : null
  } catch {
    return null
  }
}

const post = body => ({ method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })

export const agentApi = {
  status: () => call('/api/agent/status'),
  threads: () => call('/api/agent/threads'),
  thread: id => call(`/api/agent/threads/${encodeURIComponent(id)}`),
  stop: (threadId, runId, user = '') => call('/api/agent/stop', post({ thread_id: threadId, ...(runId ? { run_id: runId } : {}), user })),
  // 正在查的这一轮里追加一句：{status:'queued'} 是接上了；{status:'finished'} 是那一轮已经结束。
  steer: body => call('/api/agent/steer', post(body)),
  confirmCard: (id, user) => call(`/api/agent/cards/${encodeURIComponent(id)}/confirm`, post({ user })),
  rejectCard: (id, user) => call(`/api/agent/cards/${encodeURIComponent(id)}/reject`, post({ user })),
  uiHistory: () => call('/api/agent/ui/history'),
  uiUndo: user => call('/api/agent/ui/undo', post({ user })),
  roster: room => call(`/api/agent/roster?room=${encodeURIComponent(room || 'month')}`),
}

const BOT_NAMES = ['核算', '问答', '资料', '程序', '领队', '助手']
export const mentionsBot = text => new RegExp(`[@＠](${BOT_NAMES.join('|')})`).test(text || '')
export const toColleague = text => /^\s*[@＠](?!核算|问答|资料|程序|领队|助手)\S/.test(text || '')

export function askInRoom(detail) {
  window.dispatchEvent(new CustomEvent('ledger-room-ask', { detail }))
}
export function openLedgerRoom(detail) {
  window.dispatchEvent(new CustomEvent('ledger-room-open', { detail }))
}

/** 传一个附件。请求体就是文件本身，不是表单；onProgress 收到 0~1。
 *  返回 { promise, abort }，promise 落到 { file_id, name, size, kind, url }。 */
export function uploadFile(file, name, onProgress) {
  const xhr = new XMLHttpRequest()
  const promise = new Promise((resolve, reject) => {
    xhr.open('POST', `/api/agent/files?name=${encodeURIComponent(name)}`)
    xhr.setRequestHeader('content-type', file.type || 'application/octet-stream')
    xhr.upload.onprogress = e => { if (e.lengthComputable) onProgress?.(e.loaded / e.total) }
    xhr.onload = () => {
      let body = null
      try {
        body = xhr.responseText ? JSON.parse(xhr.responseText) : null
      } catch {
        body = null
      }
      if (xhr.status >= 200 && xhr.status < 300 && body?.file_id) return resolve(body)
      const detail = typeof body?.detail === 'string' && CJK.test(body.detail) ? body.detail : ''
      reject(fail(Object.assign(new Error(detail || (xhr.status === 413 ? '文件太大了' : '没传上，可以再试一次')), { status: xhr.status })))
    }
    xhr.onerror = () => reject(fail(networkError()))
    xhr.onabort = () => reject(Object.assign(new Error('已取消'), { name: 'AbortError' }))
    xhr.send(file)
  })
  return { promise, abort: () => xhr.abort() }
}

const LIVE_EVENTS = ['hello', 'presence', 'users', 'message', 'turn', 'task', 'card_update', 'ui_updated', 'data_updated', 'costs']

/** 订阅大家共用的实时事件：谁在线、群里新消息、任何一轮的进度、任务池、确认卡被谁处理、
 *  界面和数据变了、模型费用。断了 EventSource 自己会重连，这里不另做；连上、断开
 *  通过 onState(true/false) 告诉调用方。返回关闭函数。 */
export function openEvents(user, onEvent, onState = () => {}) {
  if (typeof EventSource === 'undefined') {
    onState(false)
    return () => {}
  }
  const source = new EventSource(`/api/agent/events?user=${encodeURIComponent(user)}`)
  source.onopen = () => onState(true)
  source.onerror = () => onState(false)
  for (const name of LIVE_EVENTS) {
    source.addEventListener(name, e => {
      let data = null
      try {
        data = JSON.parse(e.data)
      } catch {
        return
      }
      if (data !== null) onEvent(name, data)
    })
  }
  return () => {
    source.close()
    onState(false)
  }
}

/** 发一句话，边收边回调 onEvent(event, data)。流结束时 resolve——注意流可能在没收到
 *  done 之前就被代理关掉，这要由调用方按收到的事件判断。中途断网时 reject 一个
 *  network 错误，被 abort 时 reject AbortError。 */
export async function streamChat(body, { signal, onEvent, idleMs = 120000 }) {
  let res
  try {
    res = await fetch('/api/agent/chat', {
      ...post(body),
      headers: { 'content-type': 'application/json', accept: 'text/event-stream' },
      signal,
    })
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    throw fail(networkError())
  }
  if (!res.ok) throw fail(await readError(res))
  if (!res.body) throw new Error('助手没有返回内容')

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let event = ''
  let data = []

  const dispatch = () => {
    // done 的 data 可能就是 null（群里 @同事 时没有回执），照样要派发出去。
    if (data.length) {
      let payload
      let ok = true
      try {
        payload = JSON.parse(data.join('\n'))
      } catch {
        ok = false
      }
      if (ok) onEvent(event || 'message', payload)
    }
    event = ''
    data = []
  }

  const line = raw => {
    if (raw === '') return dispatch()
    if (raw.startsWith(':')) return
    const at = raw.indexOf(':')
    const field = at < 0 ? raw : raw.slice(0, at)
    let value = at < 0 ? '' : raw.slice(at + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') event = value
    else if (field === 'data') data.push(value)
  }

  // 上游断了而中间的代理不关连接时，流会一直挂着不来数据。太久没动静就当作断线，
  // 交给调用方去取回结果。
  let idle = false
  let idleTimer = null
  const touch = () => {
    clearTimeout(idleTimer)
    idleTimer = setTimeout(() => {
      idle = true
      reader.cancel().catch(() => {})
    }, idleMs)
  }
  touch()
  for (;;) {
    let chunk
    try {
      chunk = await reader.read()
    } catch (error) {
      clearTimeout(idleTimer)
      if (error?.name === 'AbortError' || signal?.aborted) throw Object.assign(new Error('已停止'), { name: 'AbortError' })
      throw fail(networkError())
    }
    const { value, done } = chunk
    if (idle) throw networkError()
    if (done) break
    touch()
    buffer += decoder.decode(value, { stream: true })
    // 结尾单独一个 \r 可能是被切开的 \r\n，留到下一块再处理，免得多出一个空行。
    const hold = buffer.endsWith('\r') ? 1 : 0
    const ready = buffer.slice(0, buffer.length - hold)
    const parts = ready.split(/\r\n|\r|\n/)
    buffer = parts.pop() + buffer.slice(buffer.length - hold)
    for (const raw of parts) line(raw)
  }
  clearTimeout(idleTimer)
  buffer += decoder.decode()
  if (buffer) for (const raw of buffer.split(/\r\n|\r|\n/)) line(raw)
  dispatch()
}
