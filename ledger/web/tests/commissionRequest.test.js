import { test } from 'node:test'
import assert from 'node:assert/strict'
import { latestRequest } from '../src/components/commissionRequest.js'

test('a slow superseded request cannot replace the newest filter result', async () => {
  const request=latestRequest()
  let finishFirst,firstSignal
  const first=request.run(signal=>{firstSignal=signal;return new Promise(resolve=>{finishFirst=resolve})})
  const second=await request.run(async()=>({store:'second'}))
  finishFirst({store:'first'})
  assert.equal(firstSignal.aborted,true)
  assert.equal(await first,null)
  assert.deepEqual(second.value,{store:'second'})
})
test('leaving the page ignores a late response and a late failure', async () => {
  const request=latestRequest();let fail
  const old=request.run(()=>new Promise((resolve,reject)=>{fail=reject}))
  request.cancel();fail(new Error('old connection failed'))
  assert.equal(await old,null)
})
test('an active error remains visible and a retry can recover', async () => {
  const request=latestRequest()
  await assert.rejects(request.run(async()=>{throw new Error('network offline')}),/network offline/)
  assert.deepEqual(await request.run(async()=>[1,2]),{value:[1,2]})
})
