import {test} from 'node:test'
import assert from 'node:assert/strict'
import {AFTER_LABOR_LABEL, reportMetricText, storeLaborFor} from '../src/commissionReportMetrics.js'

const money = value => value == null ? '—' : Number(value).toFixed(2)

test('managed sales distinguish real zero from missing evidence', () => {
  assert.equal(reportMetricText({kind:'person',managed_sales:818.47},'managed_sales',money),'818.47')
  assert.equal(reportMetricText({kind:'person',managed_sales:0},'managed_sales',money),'0.00')
  assert.equal(reportMetricText({kind:'person'},'managed_sales',money),'—')
  assert.equal(reportMetricText({managed_sales:null,managed_sales_pending:true},'managed_sales',money),'待核对')
})

test('labor display is an existing allocation, not another deduction', () => {
  assert.equal(reportMetricText({labor_cost:30},'labor_cost',money),'30.00')
  assert.equal(reportMetricText({labor_pending:true},'labor_cost',money),'待分摊')
  assert.equal(reportMetricText({profit_after_labor:90,labor_cost:30},'profit_after_labor',money),'90.00')
  assert.equal(AFTER_LABOR_LABEL,'利润额（扣兼职）')
})

test('profit drawer keeps shop labor even without a paginated shop header', () => {
  const row={kind:'person',store_id:'s',period:'2026-06',labor_cost:30,store_labor_cost:50}
  assert.equal(storeLaborFor(row,[]),50)
  assert.equal(storeLaborFor({...row,store_labor_cost:0},[]),0)
  assert.equal(storeLaborFor({...row,store_labor_cost:undefined},[]),undefined)
  assert.equal(storeLaborFor({...row,store_labor_cost:undefined},[{kind:'store',store_id:'s',period:'2026-06',labor_cost:50}]),50)
})
