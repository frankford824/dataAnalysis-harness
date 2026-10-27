import test from 'node:test'
import assert from 'node:assert/strict'
import {onlyCompanyHistoryRemains} from '../src/companyFinanceView.js'
const frozen=()=>({company_finance_only:true,can_advance:false,advance_block_reason:'company_finance_decision'})
test('backend-only historical conflict disables the ineffective action',()=>assert.equal(onlyCompanyHistoryRemains(frozen()),true))
test('real source work keeps monthly processing available',()=>assert.equal(onlyCompanyHistoryRemains({company_finance_only:false,can_advance:true,advance_block_reason:null}),false))
test('a company card alone cannot disable gathering or synchronization',()=>assert.equal(onlyCompanyHistoryRemains({decision_groups:[{kind:'company_finance'}],stores:[{stage:'company_finance',issues:[{code:'overhead_frozen_input_changed'}],actions:[]}]}),false))
test('backend advance permission is not replaced by a frontend classification',()=>assert.equal(onlyCompanyHistoryRemains({...frozen(),can_advance:true}),false))
test('missing or unrelated backend blocker does not create a historical decision',()=>{assert.equal(onlyCompanyHistoryRemains(null),false);assert.equal(onlyCompanyHistoryRemains({...frozen(),advance_block_reason:'maintenance'}),false)})
test('a closed month may still have a backend historical evidence decision',()=>assert.equal(onlyCompanyHistoryRemains({...frozen(),stores:[{status:'completed'}]}),true))
