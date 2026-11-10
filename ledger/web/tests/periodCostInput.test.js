import test from 'node:test'
import assert from 'node:assert/strict'
import {costMoney,periodCostRequest,verifyPeriodCostProposal,verifyPeriodCostReceipt,periodCostRevocation,outcomeUnknown} from '../src/periodCostInput.js'
const request=()=>periodCostRequest('s1','2026-09',17,{goods:'-40.10',dropship:'0',reshipment:'2'},' 原采购凭据核定 ')
const proposal=r=>({proposal_id:'proposal1',kind:'decision',proposal_sha:'a'.repeat(64),input_sha:'b'.repeat(64),store_id:r.store_id,period:r.period,expires_at:2000,effect:{kind:r.kind,values:r.values,reason:r.reason}})
test('money stays exact including signed costs and maximum amounts',()=>{
  assert.equal(costMoney('000999999999999.99'),'999999999999.99')
  assert.equal(costMoney('-40.1'),'-40.10');assert.equal(costMoney('-0.00'),'0.00')
  assert.equal(costMoney('1,802.27'),'1802.27');assert.equal(costMoney('1，802.27'),'1802.27')
  for(const bad of ['','NaN','Infinity','0.004','1e4','1000000000000',true,null,4])assert.throws(()=>costMoney(bad))
})
test('cost inputs require all three amounts and never carry payout or close fields',()=>{
  const r=request();assert.deepEqual(Object.keys(r.values).sort(),['costs','run_id'])
  assert.deepEqual(r.values.costs,{goods:'-40.10',dropship:'0.00',reshipment:'2.00'})
  assert.ok(!('payouts'in r)&&!('no_payout'in r)&&!('ignored_blockers'in r))
  for(const costs of [{goods:'40'},{goods:'40',dropship:'0',reshipment:''},{goods:'40',dropship:'0',reshipment:'0',payouts:[]}])
    assert.throws(()=>periodCostRequest('s1','2026-09',17,costs,'真实核定凭据'))
})
test('expired, mismatched and incomplete proposals never become confirmation commands',()=>{
  const r=request(),p=proposal(r),valid=verifyPeriodCostProposal(p,r,1000)
  assert.equal(valid.input_sha,p.input_sha)
  for(const change of [{period:'2026-08'},{store_id:'s2'},{expires_at:1000},{proposal_id:true},{input_sha:'a'},{effect:{...p.effect,kind:'rule_grant'}},{effect:{...p.effect,values:{...r.values,costs:{...r.values.costs,goods:'-40.11'}}}},{effect:{...p.effect,values:{...r.values,run_id:18}}}])
    assert.throws(()=>verifyPeriodCostProposal({...p,...change},r,1000))
})
test('applied cost receipt explicitly proves no close or payout approval',()=>{
  const r=request(),receipt={status:'applied',store_id:'s1',period:'2026-09',cost_input_id:'cost1',decision_id:'decision1',receipt_id:'receipt1',payouts_confirmed:false,period_closed:false}
  assert.equal(verifyPeriodCostReceipt(receipt,r),receipt)
  for(const change of [{period_closed:true},{payouts_confirmed:true},{receipt_id:true},{cost_input_id:''},{store_id:'s2'}])
    assert.throws(()=>verifyPeriodCostReceipt({...receipt,...change},r))
})
test('revocation binds exact current approval and preserves monetary inputs',()=>{
  const scope={decision_id:'d1',cost_input_id:'c1',store_id:'s1',period:'2026-09'}
  const detail={...scope,kind:'period_cost_input',action:'apply',values:{cost_input_id:'c1',costs:{goods:'40',dropship:'0',reshipment:'0'}},input_sha:'a'.repeat(64)}
  const cmd=periodCostRevocation(detail,scope,' 原批准凭据已被更正 ')
  assert.deepEqual(Object.keys(cmd).sort(),['input_sha','reason','request_id'])
  for(const change of [{decision_id:'d2'},{kind:'store_scope'},{values:{cost_input_id:'c2'}},{action:'revoke'}])assert.throws(()=>periodCostRevocation({...detail,...change},scope,'更正依据'))
})
test('only a definite rejection releases the original request; lost or failed replies keep it for an exact replay',()=>{
  for(const status of [400,403,404,409,422])assert.equal(outcomeUnknown(Object.assign(Error('rejected'),{status})),false)
  for(const error of [Error('连不上服务'),Object.assign(Error('server'),{status:500}),Object.assign(Error('gateway'),{status:502}),Error('尚未收到完整成本保存回执')])
    assert.equal(outcomeUnknown(error),true)
})
