import test from 'node:test'
import assert from 'node:assert/strict'
import { executionRows, health, notificationRows, probeRows } from '../src/opsView.js'

test('health only follows the backend verdict; missing or unknown is never shown as healthy', () => {
  assert.equal(health({ health: 'ok' }).label, '运行正常')
  assert.equal(health({ health: 'attention' }).label, '需要处理')
  assert.equal(health({ health: 'unknown' }).label, '状态未知')
  assert.equal(health(null).label, '状态未知')
  assert.equal(health({ health: 'surprise' }).label, '状态未知')
})

test('failed and unknown probes sort first and carry the configured action', () => {
  const rows = probeRows({ status: 'available', items: [
    { id: 'disk', status: 'ok', reason: 'ok', at: '2030-01-01T00:00:00Z', body: { reason: 'ok', required_action: '清理磁盘' } },
    { id: 'jst', status: 'unknown', reason: 'probe_evidence_unavailable', at: '2030-01-01T00:00:00Z', body: { required_action: '重新登录聚水潭' } },
    { id: 'manager', status: 'failed', reason: 'backlog', at: '2030-01-01T00:00:00Z', body: { reason: 'backlog', required_action: '检查积压' } },
  ] })
  assert.deepEqual(rows.map(r => [r.id, r.label]), [['manager', '未通过'], ['jst', '未知'], ['disk', '通过']])
  assert.equal(rows[1].reason, '探针取不到证据')
  assert.equal(rows[1].action, '重新登录聚水潭')
  assert.equal(rows[2].action, '', 'A passing probe shows no pending action')
})

test('unavailable evidence yields no rows instead of stale ones', () => {
  assert.deepEqual(probeRows({ status: 'unknown', reason: 'sentinel_not_observed', items: [] }), [])
  assert.deepEqual(notificationRows({ status: 'unknown', items: [{ event_id: 'x' }] }), [])
})

test('execution history excludes probe transitions and keeps receipts', () => {
  const rows = executionRows({ status: 'available', items: [
    { id: 'p', kind: 'probe', scope: 'disk', status: 'failed', at: 'a', body: {} },
    { id: 'r', kind: 'release', scope: 'req-1', status: 'completed', at: 'b', body: { receipt_sha: 'f'.repeat(64) } },
  ] })
  assert.deepEqual(rows.map(r => r.id), ['r'])
  assert.equal(rows[0].receipt, 'f'.repeat(64))
})
