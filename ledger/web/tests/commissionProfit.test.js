import { test } from 'node:test'
import assert from 'node:assert/strict'
import { excludedProductIds, liveProfitTotals, matchesProduct, profitCompositionCsv, profitCompositionExportRows, sameIds } from '../src/commissionProfit.js'

const products = [
  {product_id:'a', product_name:'样品A', profit:100.1},
  {product_id:'b', product_name:'正品B', profit:40.25},
  {product_id:'c', product_name:'赠品C', profit:-5.2},
]

test('creator export separates effective output from archived payout evidence', () => {
  const data={products:[{product_id:'a',product_sales:16736.38,sales:16736.38,
    creator_cost:6498.57,creator_gross:10237.81,creator_profit:6059.18,
    gross:4095.12,cost:2599.43,profit:2423.67,sales_rule_versions:['version-1']},
    {product_id:'b',sales:null,sales_pending:true,creator_pending:true,cost:0,gross:12,profit:10}]}
  const rows=profitCompositionExportRows(data,[])
  assert.equal(rows[0].做货成本,6498.57)
  assert.equal(rows[0].做货毛利,10237.81)
  assert.equal(rows[0]['做货创造利润（未扣兼职）'],6059.18)
  assert.equal(rows[0].原核算分摊成本,2599.43)
  assert.equal(rows[0]['原核算阶梯利润（未扣兼职）'],2423.67)
  assert.equal(rows[0].销售身份规则版本,'version-1')
  assert.equal(rows[1].本人销售额,null)
  assert.equal(rows[1].销售归属状态,'待确认身份')
  assert.equal(rows[1].创造业绩归属状态,'待确认身份或商品金额')
  assert.equal(rows[1].原核算分摊成本,0)
})

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

test('export names the person and says profit is before labor', () => {
  const csv = profitCompositionCsv({
    person: '陈慨', store: '陈慨-淘宝拾梦小屋99', period: '2026-06',
    products: [{product_id: 'p1', product_name: '样品', orders: 2, sales: 90.79,
      gross: 69.49, profit: -88.3, rate: 0.05}],
  }, [])
  const [header, row] = csv.replace(/^\uFEFF/, '').split(/\r\n/)
  assert.match(header, /人员/)
  assert.match(header, /商品销售收入（全额）/)
  assert.match(header, /本人销售额/)
  assert.match(header, /做货毛利/)
  assert.match(header, /做货创造利润（未扣兼职）/)
  assert.match(header, /原核算阶梯利润（未扣兼职）/)
  assert.match(header, /利润口径/)
  assert.match(row, /^陈慨,/)
  assert.match(row, /-88.3/)
  assert.match(row, /单人做货归全额/)
  assert.match(row, /抽点不参与/)
  const exported = profitCompositionExportRows({
    person: '陈慨', store: '店', period: '2026-06',
    products: [{product_id: 'p1', product_name: 'A', profit: -5, rate: 0.05}],
  }, ['p1'])
  assert.equal(exported[0]['是否计入阶梯'], '计入')
  assert.equal(exported[0]['本人点数'], '5%')
})

test('export keeps this persons 1.5% share and lists mixed shares', () => {
  const [half] = profitCompositionExportRows({
    person: '王岩', store: '天猫皇莉诗旗舰店', period: '2026-06',
    products: [{product_id: 'p1', product_name: 'A', profit: 10, rate: 0.015, rates: [0.015]}],
  }, ['p1'])
  assert.equal(half['本人点数'], '1.5%')
  const [mixed] = profitCompositionExportRows({
    person: '王岩', store: '店', period: '2026-06',
    products: [{product_id: 'p2', rate_mixed: true, rates: [0.015, 0.03]}],
  }, [])
  assert.equal(mixed['本人点数'], '1.5% / 3%')
})

test('long product ids stay exact text in Excel and formulas stay inert', () => {
  const csv=profitCompositionCsv({products:[{product_id:'123456789012345678',product_name:'=危险公式'}]},[])
  assert.ok(csv.includes("'123456789012345678"))
  assert.ok(csv.includes("'=危险公式"))
})
