import {test} from 'node:test'
import assert from 'node:assert/strict'
import {sanitizeTelemetry,apiTelemetryAction} from '../src/telemetry.js'
test('telemetry strips values and personal or order material',()=>{
  const event=sanitizeTelemetry({event_id:'id',kind:'manual_amount_edit',action:'manual_amount_edit',view:'period',period:'2030-01',store_id:'s1',duration_ms:90000000,amount:123,password:'secret',name:'张三',order_id:'order',body:{amount:123}})
  assert.equal(event.duration_ms,86400000)
  assert.deepEqual(Object.keys(event).sort(),['action','at','duration_ms','event_id','kind','outcome','period','store_id','view'].sort())
  assert.equal(sanitizeTelemetry({kind:'unknown',action:'manual_amount_edit'}),null)
})
test('only fixed actions are collected and URL values never become actions',()=>{
  assert.deepEqual(apiTelemetryAction('/api/stores/private/periods/2030-01/close','POST'),['manual_confirm','period_close'])
  assert.equal(apiTelemetryAction('/api/operations/month?order_id=private','GET'),null)
  assert.equal(apiTelemetryAction('/external/private','POST'),null)
})

test('waiting entry observations accept only fixed actions and discard all material and responsibility text',()=>{
  for(const action of ['waiting_summary_open','waiting_deliver_open']){
    const clean=sanitizeTelemetry({event_id:'fixed-event',kind:'operation_complete',action,view:'deliver',period:'2030-01',store_id:'',duration_ms:0,outcome:'observed',reason:'private-reason',summary:'private-summary',role_label:'private-role',name:'private-person',missing_material:'private-material',handoff_id:'private-handoff',amount:123})
    assert.deepEqual(Object.keys(clean).sort(),['action','at','duration_ms','event_id','kind','outcome','period','store_id','view'].sort())
    assert.equal(clean.outcome,'observed');assert.equal(clean.store_id,'');assert.equal(clean.duration_ms,0)
    assert.ok(!JSON.stringify(clean).includes('private-'))
  }
  assert.equal(sanitizeTelemetry({kind:'operation_complete',action:'waiting_deliver_open_private-reason'}),null)
})
