import { test } from 'node:test'
import assert from 'node:assert/strict'
import { reportRowActions } from '../src/commissionRowActions.js'

test('person rows keep profit, payout and detail as separate actions', () => {
  const actions = reportRowActions({status: '已结账', amount: 2006.11}, {
    profit: true, payout: true, detail: true,
  })
  assert.deepEqual(actions.map(item => [item.key, item.label, item.kind]), [
    ['profit', '利润构成', 'quiet'],
    ['payout', '确认提成', 'main'],
    ['detail', '明细', 'quiet'],
  ])
})

test('confirmed payouts relabel only the main action', () => {
  const actions = reportRowActions({status: '已人工确认', amount: 147.99}, {
    profit: false, payout: true, detail: true,
  })
  assert.deepEqual(actions.map(item => item.label), ['修改确认', '明细'])
  assert.equal(actions[0].kind, 'main')
})
