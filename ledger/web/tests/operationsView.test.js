import {test} from 'node:test'
import assert from 'node:assert/strict'
import {comparisonCopy,sortedOperations,workflowLabels} from '../src/operationsView.js'
test('negative and zero comparison bases remain explicit',()=>{
  assert.match(comparisonCopy({state:'negative_base',delta:'4',rate:null},String),/上期为负/)
  assert.match(comparisonCopy({state:'zero_base',delta:'4',rate:null},String),/上期为零/)
  assert.equal(comparisonCopy(null,String),'缺少可比数据')
})
test('ranking preserves missing values and unclaimed business',()=>{
  const rows=[{id:'a',metrics:{profit_after_labor:null}},{id:'u',unclaimed:true,metrics:{profit_after_labor:100}},{id:'b',metrics:{profit_after_labor:1}}]
  assert.deepEqual(sortedOperations(rows,'profit_after_labor').map(row=>row.id),['b','a','u'])
  assert.deepEqual(workflowLabels,{completed:'完成',processing:'处理中',needs_decision:'需决定'})
})
