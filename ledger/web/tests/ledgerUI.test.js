import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createPinia, setActivePinia } from 'pinia'
import { useApp } from '../src/store.js'
import { mergeRuleResponse, feePreviewKey } from '../src/components/ui/editableRules.js'
import { api } from '../src/api.js'

test('changing fee tabs preserves unsaved edits while refreshing server metadata',()=>{
  const saved=[{value:'平台服务费',major:'fee'}]
  const draft=[{value:'平台服务费',major:'service'}, {value:'物流费',major:'freight'}]
  const merged=mergeRuleResponse({rules:saved},draft,{rules:saved,unmatched:[{value:'其他'}]})
  assert.deepEqual(merged.draft,draft)
  assert.equal(merged.data.unmatched.length,1)
  assert.deepEqual(merged.data.rules,saved)
})
test('initial fee load and successful save replace the baseline without sharing editable objects',()=>{
  const incoming={rules:[{value:'运费',major:'freight'}]}
  const initial=mergeRuleResponse(null,[],incoming)
  assert.deepEqual(initial.draft,incoming.rules)
  initial.draft[0].value='local edit'
  assert.equal(incoming.rules[0].value,'运费')
  const reloaded=mergeRuleResponse(initial.data,initial.draft,incoming,true)
  assert.equal(reloaded.draft[0].value,'运费')
})
test('a fee preview applies only to the exact rules and store checked',()=>{
  const rows=[{value:'退款',major:'refund',exclude:false}]
  const key=feePreviewKey(rows,'shop-one')
  assert.notEqual(key,feePreviewKey(rows,'shop-two'))
  assert.notEqual(key,feePreviewKey([{...rows[0],exclude:true}],'shop-one'))
})
test('double-clicking a pending business action does not submit it twice or clear its busy state',async()=>{
  setActivePinia(createPinia());const app=useApp();let resolve;let submitted=0
  const first=app.run('正在保存',()=>{submitted++;return new Promise(r=>resolve=r)})
  await assert.rejects(app.run('再次保存',async()=>{submitted++}),/等待当前操作完成/)
  assert.equal(submitted,1);assert.equal(app.busy.label,'正在保存')
  resolve('done');assert.equal(await first,'done');assert.equal(app.busy,null)
})

test('server errors use a readable message instead of raw HTML',async()=>{
  const original=globalThis.fetch
  globalThis.fetch=async()=>new Response('<h1>Internal Server Error</h1>',{status:500})
  try{await assert.rejects(api.fees({section:'rules'}),/服务暂时未能完成请求/)}finally{globalThis.fetch=original}
})
test('cancellable reads pass the abort signal through to fetch',async()=>{
  const original=globalThis.fetch,controller=new AbortController();let captured
  globalThis.fetch=async(url,options)=>{captured=options.signal;return new Response('{"rows":[]}',{status:200})}
  try{await api.drill(123,'revenue',{limit:50},{signal:controller.signal});assert.equal(captured,controller.signal)}finally{globalThis.fetch=original}
})
