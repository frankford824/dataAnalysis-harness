import {test} from 'node:test'
import assert from 'node:assert/strict'
import {boardColumns,comparisonCopy,identityColumns,managedPart,sortedOperations,workflowLabels} from '../src/operationsView.js'
test('core board columns keep the summary set and expand to every metric and component',()=>{
  const metrics=[{key:'net_income'},{key:'refund_rate'},{key:'aftersales_cost'},{key:'brushing_cost',component_of:'aftersales_cost'},{key:'profit_after_labor'}]
  assert.deepEqual(boardColumns(metrics).map(item=>item.key),['net_income','aftersales_cost','profit_after_labor'])
  assert.equal(boardColumns(metrics,true).length,5)
})
test('identity columns follow the workbook dimensions',()=>{
  assert.deepEqual(identityColumns('store'),{platform:true,team:false,commission:false})
  assert.deepEqual(identityColumns('person'),{platform:false,team:true,commission:true})
  assert.deepEqual(identityColumns('team'),{platform:false,team:false,commission:true})
})
test('managed split keeps unknown separate from zero',()=>{
  assert.deepEqual(managedPart({managed_metrics:null},'net_income'),{known:false,value:null})
  assert.deepEqual(managedPart({managed_metrics:{net_income:'0.00'}},'net_income'),{known:true,value:'0.00'})
  assert.deepEqual(managedPart({managed_metrics:{net_income:null}},'net_income'),{known:true,value:null})
})
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
