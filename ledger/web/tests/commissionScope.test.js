import test from 'node:test'
import assert from 'node:assert/strict'
import { initialStoreIds } from '../src/commissionScope.js'

test('an explicit all-store link cannot inherit a previously selected store', () => {
  assert.deepEqual(initialStoreIds({shops:''}, 'previous-shop'), [])
  assert.deepEqual(initialStoreIds({shops:null}, 'previous-shop'), [])
  assert.deepEqual(initialStoreIds({shops:'shop-a,shop-b'}, 'previous-shop'), ['shop-a','shop-b'])
})

test('entering commission without a store filter inherits the current ledger store', () => {
  assert.deepEqual(initialStoreIds({}, 'current-shop'), ['current-shop'])
  assert.deepEqual(initialStoreIds({}, ''), [])
})
