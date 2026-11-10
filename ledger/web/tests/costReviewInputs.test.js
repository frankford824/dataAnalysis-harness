import test from 'node:test'
import assert from 'node:assert/strict'
import {initialCostInputs, needsManualCostInput, sourceCostText} from '../src/costReviewInputs.js'

test('a recognised subtotal is filled in so the person can confirm it, and is still labelled incomplete', () => {
  const review = {observed: {goods: 38, dropship: 0, reshipment: 0},
    amount_complete: {goods: false, dropship: true, reshipment: true}, requires_human: true}
  assert.deepEqual(initialCostInputs(review), {goods: '38.00', dropship: '0.00', reshipment: '0.00'})
  assert.equal(needsManualCostInput(review), true)
  assert.match(sourceCostText(review, 'goods'), /已识别部分 38\.00 元.*总额待补齐/)
})

test('posted supplements do not force another whole-period cost confirmation', () => {
  const review = {observed: {goods: 38, dropship: 0, reshipment: 0},
    amount_complete: {goods: true, dropship: true, reshipment: true},
    line_count: 1, line_total: 2, posted_line_total: 2, line_posting_pending: false, requires_human: false}
  assert.equal(needsManualCostInput(review), false)
  // Existing preview adds the two-yuan supplement once; keep the source basis.
  assert.equal(initialCostInputs(review).goods, '38.00')
})

test('pending recompute waits rather than asking to type the same amount again', () => {
  assert.equal(needsManualCostInput({requires_human: true, line_posting_pending: true}), false)
})

test('missing completeness and unknown or malformed numbers never prefill zero', () => {
  for (const value of [null, undefined, '', ' ', false, true, [], [0], {}, NaN, Infinity, 'unknown']) {
    assert.equal(initialCostInputs({observed: {goods: value}, amount_complete: {goods: true}}).goods, '')
  }
  assert.equal(initialCostInputs({observed: {goods: 38}, requires_human: false}).goods, '38.00')
  assert.equal(sourceCostText({observed: {goods: null}}, 'goods'), '总额待补齐')
  assert.equal(sourceCostText({observed: {goods: 0}, amount_complete: {goods: false}}, 'goods'), '总额待补齐')
  assert.equal(sourceCostText({observed: {goods: 0}, amount_complete: {goods: true}}, 'goods'), '0.00 元')
})

test('complete existing costs retain their values without manual re-entry', () => {
  assert.deepEqual(initialCostInputs({observed: {goods: 12.34, dropship: 5, reshipment: -2.5},
    amount_complete: {goods: true, dropship: true, reshipment: true}}),
  {goods: '12.34', dropship: '5.00', reshipment: '-2.50'})
})
