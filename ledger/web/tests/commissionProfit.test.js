import { test } from 'node:test'
import assert from 'node:assert/strict'
import { excludedProductIds, liveProfitTotals, matchesProduct, sameIds } from '../src/commissionProfit.js'

const products = [
  {product_id:'a', product_name:'样品A', profit:100.1},
  {product_id:'b', product_name:'正品B', profit:40.25},
  {product_id:'c', product_name:'赠品C', profit:-5.2},
]

test('live totals follow included products and ignore search', () => {
  const totals = liveProfitTotals(products, ['a', 'c'])
  assert.equal(totals.all, 135.15)
  assert.equal(totals.included, 94.9)
  assert.equal(totals.excluded, 40.25)
  assert.equal(totals.excludedCount, 1)
  assert.equal(totals.includedCount, 2)
})

test('search only matches visible products and does not change ids', () => {
  assert.equal(matchesProduct(products[0], '样品'), true)
  assert.equal(matchesProduct(products[1], '样品'), false)
  assert.deepEqual(excludedProductIds(products, ['a', 'c']), ['b'])
  assert.equal(sameIds(['b', 'a'], ['a', 'b']), true)
  assert.equal(sameIds(['a'], ['a', 'b']), false)
})
