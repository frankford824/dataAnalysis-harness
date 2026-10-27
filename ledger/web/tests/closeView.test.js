import {test} from 'node:test'
import assert from 'node:assert/strict'
import {finalCloseIssues,needsMonthWorkspace} from '../src/closeView.js'
test('final company readiness is visible without duplicating engine findings',()=>{
  const issues=finalCloseIssues({findings:[{id:'cost',passed:false}],close_verification:{issues:[{code:'cost',message:'成本待确认'},{code:'overhead_total_unknown',message:'公司总额待确认'}]}})
  assert.deepEqual(issues,[{code:'overhead_total_unknown',message:'公司总额待确认'}])
  assert.equal(needsMonthWorkspace(issues,'2030-01'),true)
  assert.equal(needsMonthWorkspace(issues,'未知账期'),false)
})
test('closed history and intrinsic cost review do not become company decisions',()=>{
  assert.deepEqual(finalCloseIssues({close_verification:{issues:[{code:'already_closed'}]}}),[])
  assert.equal(needsMonthWorkspace([{code:'cost_decision_required'}],'2030-01'),false)
})
