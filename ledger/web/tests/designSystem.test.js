import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { tokens, tokenCss, uiTheme } from '../src/design/tokens.js'
import { advanceSpring, createSpring } from '../src/components/ui/springMotion.js'
import { createReviewGroups } from '../src/components/ui/reviewGroups.js'

test('native and component-library colors/type resolve from the same committed tokens', () => {
  assert.equal(readFileSync(new URL('../src/design/tokens.css',import.meta.url),'utf8').replace(/\r\n/g,'\n'),tokenCss())
  for(const dark of [false,true]){const theme=uiTheme(dark),mode=dark?'dark':'light';assert.equal(theme.common.primaryColor,tokens.colors.action[mode]);assert.equal(theme.common.cardColor,tokens.colors.paper[mode]);assert.equal(theme.common.fontSize,`${tokens.type.body}px`)}
})
test('the UI publisher avoids PowerShell automatic variables and never restarts the engine',()=>{
  const publisher=readFileSync(new URL('../../../tools/deploy/win/deploy-static-ui.ps1',import.meta.url),'utf8')
  assert.doesNotMatch(publisher,/\$(?:home|host|pid|input|args|error|pwd|profile|PSVersionTable|ExecutionContext)\s*=/i)
  assert.doesNotMatch(publisher,/\b(?:Stop-Process|Restart-Service|Stop-ScheduledTask|Start-ScheduledTask|Set-ExecutionPolicy)\b/i)
  assert.ok(publisher.includes("StartsWith('ledger/ledger/static/'"))
})
test('reading groups preserve identifiers, expose copies, and never carry amounts', () => {
  const model=createReviewGroups(['one','two','three'],'store:period:run1')
  assert.equal(model.move(['one'],'A'),true);assert.equal(model.move(['two'],'B'),true)
  assert.equal(model.merge('A','B'),true);assert.deepEqual(model.membership,{one:'B',two:'B',three:null})
  const external=model.membership;external.one=null;assert.equal(model.membership.one,'B')
  assert.equal(model.undo(),true);assert.deepEqual(model.membership,{one:'A',two:'B',three:null})
  assert.equal(model.redo(),true);assert.equal(model.move(['one'],null),true);assert.equal(model.split('B'),true)
  assert.deepEqual(model.membership,{one:null,two:null,three:null})
})
test('stale identifiers and invalid drop targets cannot move any current item', () => {
  const model=createReviewGroups(['one','two'],'run1')
  assert.equal(model.move(['one','unknown'],'A'),false);assert.equal(model.move(['one'],'closed'),false);assert.equal(model.merge('A','A'),false)
  model.move(['one'],'A');model.reset(['new'],'run2')
  assert.equal(model.canUndo,false);assert.equal(model.move(['one'],'B'),false);assert.deepEqual(model.membership,{new:null})
})
test('a newly made grouping edit invalidates redo instead of replaying stale membership',()=>{
  const model=createReviewGroups(['one','two'],'run');model.move(['one'],'A');model.move(['two'],'B');model.undo();model.move(['two'],'A')
  assert.equal(model.canRedo,false);assert.deepEqual(model.membership,{one:'A',two:'A'})
})
test('pixel motion converges after a long frame and a change of direction',()=>{
  let current={value:300,velocity:1200}
  for(let i=0;i<120;i++)current=advanceSpring(current,0,i===0?5:1/60,240,23)
  assert.deepEqual(current,{value:0,velocity:0})
  current={value:20,velocity:300};for(let i=0;i<120;i++)current=advanceSpring(current,-30,1/60,240,23)
  assert.deepEqual(current,{value:-30,velocity:0})
})
test('grabbing in-flight motion freezes its current position; reduced motion settles immediately',()=>{
  let update,callback,id=0
  const spring=createSpring(0,v=>{update=v},{schedule:fn=>{callback=fn;return++id},cancel:()=>{callback=null},reduced:()=>false})
  spring.to(100);callback(16);const current=spring.value;assert.ok(current>0&&current<100)
  spring.jump(current);assert.equal(callback,null);assert.equal(spring.value,current);assert.equal(spring.velocity,0)
  const reduced=createSpring(4,v=>{update=v},{reduced:()=>true});reduced.to(10);assert.equal(update,10);assert.equal(reduced.value,10)
})
