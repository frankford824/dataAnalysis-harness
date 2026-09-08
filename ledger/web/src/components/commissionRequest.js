// A superseded response never owns the visible result, even if abort is ignored.
export function latestRequest() {
  let sequence = 0, controller
  function cancel() { sequence++; controller?.abort() }
  async function run(request) {
    cancel()
    const ticket = sequence
    controller = new AbortController()
    try {
      const value = await request(controller.signal)
      return ticket === sequence ? { value } : null
    } catch (error) {
      if (ticket !== sequence || error.name === 'AbortError') return null
      throw error
    }
  }
  return { run, cancel }
}

export async function commissionRequest(path, { signal, body } = {}) {
  const response = await fetch('/api/commission-v2' + path, {
    signal, ...(body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
  })
  if (!response.ok) {
    const result = await response.json().catch(() => ({}))
    throw new Error(typeof result.detail === 'string' ? result.detail : '加载失败，请重试')
  }
  return response.json()
}
