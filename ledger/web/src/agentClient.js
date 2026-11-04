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
  stop: threadId => call('/api/agent/stop', post({ thread_id: threadId })),
  confirmCard: id => call(`/api/agent/cards/${encodeURIComponent(id)}/confirm`, post({})),
  rejectCard: id => call(`/api/agent/cards/${encodeURIComponent(id)}/reject`, post({})),
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
    if (data.length) {
      let payload = null
      try {
        payload = JSON.parse(data.join('\n'))
      } catch {
        payload = null
      }
      if (payload !== null) onEvent(event || 'message', payload)
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
