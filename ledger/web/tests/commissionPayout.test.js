import { test } from 'node:test'
import assert from 'node:assert/strict'
import { suggestedManualPayout } from '../src/commissionPayout.js'

test('manual close prefills the after-labor trial instead of the raw engine amount', () => {
  const filled = suggestedManualPayout({ amount: 2347.44, amount_after_labor: 2044.95 })
  assert.equal(filled.amount, '2044.95')
  assert.equal(filled.trial, 2347.44)
  assert.equal(filled.suggested, 2044.95)
})

test('manual close keeps an empty box when the engine has no trial amount', () => {
  const empty = suggestedManualPayout({ amount: null, sales: 1000 })
  assert.equal(empty.amount, '')
  assert.equal(empty.suggested, null)
})

test('manual close falls back to the raw trial only when after-labor is missing', () => {
  const fallback = suggestedManualPayout({ amount: 17 })
  assert.equal(fallback.amount, '17.00')
  assert.equal(fallback.suggested, null)
})
