import { test } from 'node:test'
import assert from 'node:assert/strict'
import { periodDrillTarget } from '../src/periodDrill.js'

test('pending receipt and promotion open their own evidence, not the cost editor', () => {
  for (const id of ['n_receipt', 'n_refund', 'n_ad', 'n_logistics']) {
    assert.equal(periodDrillTarget({ id, unavailable_reason: '本月相关对账流水身份待核对' }), 'facts')
  }
})
test('only the explicit goods-cost review path opens the cost editor', () => {
  assert.equal(periodDrillTarget({ id: 'n_goods', unavailable_reason: '已算现有成本，支持人工确认金额' }), 'pricing')
  assert.equal(periodDrillTarget({ id: 'n_goods', unavailable_reason: '推广控制表月份待确认' }), 'facts')
  assert.equal(periodDrillTarget({ id: 'n_goods', available: true }), 'facts')
})
