import { test } from 'node:test'
import assert from 'node:assert/strict'
import { changeText,expenseText,freightEvidence,reviewConclusion,taskStores,trialState } from '../src/freightReview.js'

test('expense and difference formatting distinguish zero from unavailable',()=>{
  assert.equal(expenseText(0),'¥0.00')
  assert.equal(expenseText(null),'尚未形成')
  assert.equal(changeText(null),'暂不能比较')
  assert.equal(changeText(0),'¥0.00')
  assert.equal(changeText(-12.34),'−¥12.34')
  assert.equal(changeText(12.34),'+¥12.34')
})
test('a comparison is not labeled as verified freight or an approved correction',()=>{
  assert.match(reviewConclusion({summary:{store_count:2},groups:[{id:'changes'}]}).title,/原账未更正/)
  assert.match(reviewConclusion({summary:{store_count:2},groups:[]}).hint,/不等于.*逐笔核实/)
  assert.match(reviewConclusion({summary:{store_count:2},groups:[{id:'syncing'}]}).title,/资料不完整/)
  assert.match(reviewConclusion({summary:{store_count:2,changed_store_count:1},groups:[{id:'syncing'},{id:'changes'}]}).hint,/先复核 1 家/)
  assert.match(reviewConclusion({summary:{store_count:2},groups:[{id:'evidence'}]}).hint,/不按零元/)
  assert.match(reviewConclusion({summary:{store_count:0},groups:[]}).title,/暂无核算/)
})
test('tasks group stores without asking the user to process underlying orders',()=>{
  const stores=[{store_id:'a',issues:['changes','allocation']},{store_id:'b',issues:['syncing']}]
  assert.deepEqual(taskStores({stores},'changes'),[stores[0]])
  assert.deepEqual(taskStores({stores},'allocation'),[stores[0]])
  assert.deepEqual(taskStores({stores},''),stores)
  assert.equal(trialState({trial_status:'syncing'}),'试算资料未齐')
})
test('drill references retain two exact records and explain incomplete followups',()=>{
  const row={store:'店',store_id:'s',period:'2026-06',shown_run_id:11,trial_run_id:22,
    closed:true,trial_amount:null,shown_at:'old',trial_at:'new'}
  const result=freightEvidence(row)
  assert.deepEqual(result.records.map(r=>r.run_id),[11,22])
  assert.match(result.records[0].hint,/不会自动改写/)
  assert.match(result.records[1].hint,/尚不完整/)
  assert.equal(result.expense,true)
  assert.equal(freightEvidence({...row,trial_run_id:11}).records.length,1)
})
