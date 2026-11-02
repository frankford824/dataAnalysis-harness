/* 运维页只读：状态文字全部由后端给出的 status/reason 决定，页面不推断健康。 */

export const HEALTH = {
  ok: { tone: 'success', label: '运行正常', hint: '所有检查项都在有效期内且通过。' },
  attention: { tone: 'error', label: '需要处理', hint: '有检查项未通过，按下方「需要的处理」逐项核对。' },
  unknown: { tone: 'warning', label: '状态未知', hint: '哨兵没有运行、检查已过期或证据不可读；未知不等于正常。' },
}

const STATUS = { ok: '通过', failed: '未通过', unknown: '未知' }

const REASONS = {
  sentinel_not_observed: '哨兵尚未运行过',
  sentinel_evidence_unavailable: '哨兵记录不可读',
  sentinel_observation_stale: '检查结果已过期',
  probe_evidence_unavailable: '探针取不到证据',
}

export function health(value) {
  return HEALTH[value?.health] || HEALTH.unknown
}

export function statusLabel(status) {
  return STATUS[status] || STATUS.unknown
}

export function reasonLabel(reason) {
  return REASONS[reason] || reason || ''
}

export function probeRows(value) {
  const rows = value?.status === 'available' ? value.items || [] : []
  const rank = { failed: 0, unknown: 1, ok: 2 }
  return rows
    .map(row => ({
      id: row.id,
      status: row.status,
      label: statusLabel(row.status),
      reason: reasonLabel(row.body?.reason ?? row.reason),
      action: row.status === 'ok' ? '' : row.body?.required_action || '',
      at: row.at,
    }))
    .sort((a, b) => (rank[a.status] ?? 1) - (rank[b.status] ?? 1) || a.id.localeCompare(b.id))
}

export function executionRows(value) {
  const rows = value?.status === 'available' ? value.items || [] : []
  return rows
    .filter(row => row.kind !== 'probe')
    .map(row => ({
      id: row.id,
      kind: row.kind,
      scope: row.scope,
      status: row.status,
      reason: row.body?.reason || '',
      action: row.body?.required_action || '',
      receipt: row.body?.receipt_sha || '',
      at: row.at,
    }))
}

export function notificationRows(value) {
  const rows = value?.status === 'available' ? value.items || [] : []
  return rows.map(row => ({
    id: row.event_id,
    title: row.title,
    reason: reasonLabel(row.body?.reason),
    action: row.body?.required_action || '',
    at: row.at,
  }))
}
