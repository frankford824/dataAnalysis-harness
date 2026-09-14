import test from 'node:test'
import assert from 'node:assert/strict'

import { sameSettlementScope, settlementDifference } from '../src/components/commissionSettlement.js'

test('settlement scope comparison ignores selector order but not business scope', () => {
  const current = { start: '2026-06', end: '2026-06', store_ids: ['b', 'a'], person_ids: ['p2', 'p1'] }
  assert.equal(sameSettlementScope(current, {
    start: '2026-06', end: '2026-06', store_ids: ['a', 'b'], person_ids: ['p1', 'p2'],
  }), true)
  assert.equal(sameSettlementScope(current, { ...current, end: '2026-07' }), false)
  assert.equal(sameSettlementScope(current, { ...current, person_ids: [] }), false)
})

test('settlement difference is currency-rounded and preserves zero', () => {
  assert.equal(settlementDifference(40.03, 30.03), 10)
  assert.equal(settlementDifference(30.03, 40.03), -10)
  assert.equal(settlementDifference(30.03, 30.03), 0)
  assert.equal(settlementDifference(null, 30.03), null)
})
