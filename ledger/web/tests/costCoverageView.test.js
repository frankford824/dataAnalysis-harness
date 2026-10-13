import { test } from 'node:test'
import assert from 'node:assert/strict'
import { costCoverageView } from '../src/costCoverageView.js'

test('fully covered orders explicitly show 100% and zero gaps',()=>{
  const view=costCoverageView({expected:225,covered:225,coverage:1,uncovered:0},0,0)
  assert.equal(view.percentage,'100.0%');assert.equal(view.gapLabel,'0 笔')
  assert.match(view.description,/无需补录/);assert.equal(view.attention,false)
})
test('missing or empty denominator is not a zero-gap success',()=>{
  for(const coverage of [null,{}, {expected:0,coverage:0}, {expected:10,coverage:null}]){
    const view=costCoverageView(coverage)
    assert.equal(view.percentage,'未统计');assert.equal(view.gapLabel,'待确认');assert.equal(view.attention,true)
  }
})
test('passing threshold does not hide remaining gaps',()=>{
  const view=costCoverageView({expected:100,covered:98,coverage:.98,uncovered:2,passed:true},2)
  assert.equal(view.gaps,2);assert.equal(view.attention,true);assert.match(view.description,/2 笔/)
})
test('source anomalies are not mislabeled as order gaps',()=>{
  const view=costCoverageView({expected:10,covered:10,coverage:1,uncovered:0},0,4)
  assert.equal(view.gaps,0);assert.match(view.description,/来源成本记录待核对/)
})
test('closed snapshot explicitly preserves historical meaning',()=>{
  const view=costCoverageView({expected:100,covered:98,coverage:.98,uncovered:2},2,3,true)
  assert.equal(view.title,'结账时成本覆盖');assert.match(view.description,/不会自动改写历史账/)
})
