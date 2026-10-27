import test from 'node:test'
import assert from 'node:assert/strict'
import {policyDecisionValues,shiftedMonth,policyStatusLabels} from '../src/policyView.js'
test('scope defaults explicit anchor month and range remains finite',()=>{
 assert.equal(shiftedMonth('2026-07'),'2027-06')
 assert.deepEqual(policyDecisionValues({kind:'store_scope',included:false,mode:'month',period:'2026-07'}),{included:false,valid_from:'2026-07',valid_until:'2026-07'})
 assert.deepEqual(policyDecisionValues({kind:'store_scope',included:true,mode:'range',from:'2026-06',until:'2026-07'}),{included:true,valid_from:'2026-06',valid_until:'2026-07'})
 assert.throws(()=>policyDecisionValues({kind:'store_scope',mode:'range',from:'2026-07',until:'2026-06'}),/起止月份/)
})
test('grant uses backend contract and timezone-bearing execution bounds without computing money',()=>{
 const result=policyDecisionValues({kind:'rule_grant',mode:'range',from:'2026-06',until:'2026-07',rule:{id:'fixed.v1',contract_sha:'backend-sha'},executionFrom:'2026-01-01T00:00:00Z',executionUntil:'2027-01-01T00:00:00Z'})
 assert.equal(result.rule_sha,'backend-sha');assert.equal(result.period_from,'2026-06');assert.equal(result.period_until,'2026-07');assert.equal(result.valid_until,'2027-01-01T00:00:00.000Z')
 assert.equal(result.amount,undefined);assert.equal(policyStatusLabels.version_changed,'规则已更新，需重新授权')
 assert.throws(()=>policyDecisionValues({kind:'rule_grant',mode:'month',period:'2026-06',rule:{id:'fixed.v1'},executionFrom:'2026-01-01',executionUntil:'2027-01-01'}),/后台提供/)
})
