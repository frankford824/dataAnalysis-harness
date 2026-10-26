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

test('posted supplements cover only original gaps using the same denominator',()=>{
  const view=costCoverageView({expected:16811,covered:16264,coverage:16264/16811,uncovered:547},547,2962,false,441)
  assert.equal(view.percentage,'96.7%')
  assert.equal(view.afterPercentage,'99.4%')
  assert.equal(view.afterCovered,16705)
  assert.equal(view.afterGapLabel,'106 笔')
  assert.equal(view.gaps,547)
  assert.match(view.description,/已计入人工补录 441 笔/)
})

test('newly saved edits cannot inflate the captured posted count',()=>{
  const coverage={expected:100,covered:80,coverage:.8,uncovered:20}
  // 15 saved lines may exist, but this run has posted only 10.
  const before=costCoverageView(coverage,20,8,false,10)
  assert.equal(before.afterPercentage,'90.0%')
  assert.equal(before.afterGaps,10)
  const recomputed=costCoverageView(coverage,20,8,false,15)
  assert.equal(recomputed.afterPercentage,'95.0%')
})

test('all gaps supplemented reaches 100% without changing raw coverage or close rules',()=>{
  const coverage={expected:10,covered:7,coverage:.7,uncovered:3,passed:false}
  const view=costCoverageView(coverage,3,0,false,3)
  assert.equal(view.afterPercentage,'100.0%');assert.equal(view.afterGaps,0)
  assert.equal(view.percentage,'70.0%');assert.equal(coverage.passed,false)
})

test('missing, inconsistent or impossible counts stay uncounted instead of clamping to 100%',()=>{
  const coverage={expected:10,covered:7,coverage:.7,uncovered:3}
  for(const count of [undefined,null,-1,4,1.5,NaN,Infinity,'3']){
    const view=costCoverageView(coverage,3,0,false,count)
    assert.equal(view.afterKnown,false);assert.equal(view.afterPercentage,'未统计');assert.equal(view.afterGaps,null)
  }
  for(const c of [null,{}, {...coverage,expected:0},{...coverage,covered:8},{...coverage,covered:null}]){
    assert.equal(costCoverageView(c,3,0,false,1).afterKnown,false)
  }
})

test('closed counts are tied to the saved snapshot and do not imply full manual-period coverage',()=>{
  const coverage={expected:10,covered:7,coverage:.7,uncovered:3}
  const view=costCoverageView(coverage,3,0,true,1)
  assert.equal(view.afterPercentage,'80.0%');assert.equal(view.afterGaps,2)
  assert.match(view.description,/结账时保存/)
  assert.equal(costCoverageView(coverage,3,0,true).afterKnown,false)
})

test('zero posted supplements leaves the original rate and gap intact',()=>{
  const view=costCoverageView({expected:10,covered:7,coverage:.7,uncovered:3},3,0,false,0)
  assert.equal(view.afterPercentage,'70.0%');assert.equal(view.afterGaps,3)
})
