import test from 'node:test'
import assert from 'node:assert/strict'
import {feeRequestId} from '../src/feeJobState.js'
test('intranet HTTP works without crypto.randomUUID',()=>{
  const random={getRandomValues:bytes=>{bytes.fill(123);return bytes}}
  assert.match(feeRequestId(random),/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
})
