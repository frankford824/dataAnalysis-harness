import {test} from 'node:test'
import assert from 'node:assert/strict'
import {canConfirmStatement,recomputeReviewedStore} from '../src/statementReview.js'
test('review requires a listed candidate, acknowledgement, person and reason',()=>{
  const group={can_confirm:true,members:[{anchor:'source-one'}]}
  assert.equal(canConfirmStatement(group,'source-one','平台原始流水核对依据',true,'财务'),true)
  for(const change of [
    [group,'forged','平台原始流水核对依据',true,'财务'],
    [group,'source-one','平台原始流水核对依据',false,'财务'],
    [group,'source-one','已核对',true,'财务'],
    [group,'source-one','平台原始流水核对依据',true,' '],
    [{...group,can_confirm:false},'source-one','平台原始流水核对依据',true,'财务'],
    [group,'source-one','平台原始流水核对依据',true,'财务',true],
  ])assert.equal(canConfirmStatement(...change),false)
})
test('saved review does not claim recomputation succeeded when API returns failure',async()=>{
  await assert.rejects(recomputeReviewedStore('s',async()=>({failure:{why:'输入尚未就绪'}})),/输入尚未就绪/)
  const response={periods:[{run_id:2}],failure:null}
  assert.equal(await recomputeReviewedStore('s',async()=>response),response)
})
