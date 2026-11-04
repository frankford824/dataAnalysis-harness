/* 台账助手的后端调用。
 *
 * 对话接口是 POST 回来一条 text/event-stream，EventSource 只能发 GET，所以这里用
 * fetch 拿到流之后自己按 SSE 的规矩拆行。data 一律是 JSON。
 */

async function readError(res) {
  const text = await res.text().catch(() => '')
  let detail = null
  try {
    const body = text ? JSON.parse(text) : null
    detail = typeof body?.detail === 'string' ? body.detail : typeof body?.message === 'string' ? body.message : null
  } catch {
    detail = null
  }
  return Object.assign(
    new Error(detail || (res.status >= 500 ? '助手暂时没有响应，请稍后再试' : res.status === 404 ? '没有找到这段对话' : `请求失败（${res.status}）`)),
    { status: res.status },
  )
}

async function call(path, init) {
  let res
  try {
    res = await fetch(path, init)
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    throw Object.assign(new Error('连不上助手服务'), { offline: true })
  }
  if (!res.ok) throw await readError(res)
  const text = await res.text()
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

/** 发一句话，边收边回调 onEvent(event, data)。流正常结束时 resolve，中途断开或被
 *  abort 时 reject（AbortError 由调用方区分）。 */
export async function streamChat(body, { signal, onEvent }) {
  let res
  try {
    res = await fetch('/api/agent/chat', {
      ...post(body),
      headers: { 'content-type': 'application/json', accept: 'text/event-stream' },
      signal,
    })
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    throw Object.assign(new Error('连不上助手服务'), { offline: true })
  }
  if (!res.ok) throw await readError(res)
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

  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    // 结尾单独一个 \r 可能是被切开的 \r\n，留到下一块再处理，免得多出一个空行。
    const hold = buffer.endsWith('\r') ? 1 : 0
    const ready = buffer.slice(0, buffer.length - hold)
    const parts = ready.split(/\r\n|\r|\n/)
    buffer = parts.pop() + buffer.slice(buffer.length - hold)
    for (const raw of parts) line(raw)
  }
  buffer += decoder.decode()
  if (buffer) for (const raw of buffer.split(/\r\n|\r|\n/)) line(raw)
  dispatch()
}
