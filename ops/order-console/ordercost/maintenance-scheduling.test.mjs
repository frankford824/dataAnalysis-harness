import {test} from 'node:test';
import assert from 'node:assert/strict';
import {advanceChanges} from './maintenance-scheduling.mjs';

test('a bounded page is persisted only after all unique SKUs and months', async () => {
  const calls=[]; const state={cursor:'old',watermark:'7'};
  const result=await advanceChanges({state,months:['June','July'],batchSize:1,asOf:x=>x,since:'start',
    read:async params=>{assert.equal(params.cursor,'old');assert.equal(params.since,null);return {data:[{sku_id:'A'},{sku_id:'A'},{sku_id:'B'}],next_cursor:'next',watermark:9};},
    refresh:async(skus,day)=>calls.push([day,skus]),save:async checkpoint=>calls.push(checkpoint)});
  assert.equal(calls.length,5); assert.equal(calls[4].cursor,'next');assert.equal(result.more,true);
});
test('failed history request never advances the cursor',async()=>{
  let saves=0;
  await assert.rejects(advanceChanges({state:{cursor:'old'},months:['June'],batchSize:1,asOf:x=>x,
    read:async()=>({data:[{sku_id:'A'},{sku_id:'B'}],next_cursor:'next'}),
    refresh:async ids=>{if(ids[0]==='B')throw Error('upstream unavailable');},save:async()=>saves++}),/unavailable/);
  assert.equal(saves,0);
});
test('malformed or nonadvancing pages do not clear the saved checkpoint',async()=>{
  for(const page of [{}, {data:[],next_cursor:'old'}]) {
    let saves=0;
    await assert.rejects(advanceChanges({state:{cursor:'old'},read:async()=>page,save:async()=>saves++}));
    assert.equal(saves,0);
  }
});
