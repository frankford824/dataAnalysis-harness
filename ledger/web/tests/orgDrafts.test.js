import { test } from 'node:test'
import assert from 'node:assert/strict'
import { reconcileOrgDraft } from '../src/components/ui/orgDrafts.js'

test('saving shop responsibilities preserves an independent person draft and its revision', () => {
  const original={id:'p',note:'saved',revision:2},draft={...original,note:'still editing'}
  const next=reconcileOrgDraft(draft,original,{...original},true)
  assert.deepEqual(next.draft,draft);assert.deepEqual(next.original,original);assert.equal(next.accepted,false)
})
test('a successful person save accepts the authoritative revision without retaining a false dirty state', () => {
  const original={id:'p',note:'old',percent:1,revision:2},draft={...original,note:'new',percent:'2.50'}
  const saved={...draft,percent:2.5,revision:3}
  const next=reconcileOrgDraft(draft,original,saved,true)
  assert.deepEqual(next.draft,saved);assert.deepEqual(next.original,saved);assert.equal(next.accepted,true)
})
test('saving person information preserves unsaved shop selections', () => {
  const next=reconcileOrgDraft(['shop-a','shop-b'],['shop-a'],['shop-a'],true)
  assert.deepEqual(next.draft,['shop-a','shop-b']);assert.deepEqual(next.original,['shop-a']);assert.equal(next.accepted,false)
})
test('switching people resets drafts to that persons saved state', () => {
  const next=reconcileOrgDraft({id:'a',note:'draft'},{id:'a',note:''},{id:'b',note:'saved'},false)
  assert.deepEqual(next.draft,{id:'b',note:'saved'});assert.equal(next.accepted,true)
})
test('a conflicting server edit preserves the draft and old revision for the existing conflict guard', () => {
  const next=reconcileOrgDraft({name:'mine',revision:1},{name:'old',revision:1},{name:'theirs',revision:2},true)
  assert.deepEqual(next.draft,{name:'mine',revision:1});assert.equal(next.original.revision,1)
})
test('empty numeric inputs remain distinct from explicit zero and returned objects are independent', () => {
  const incoming={percent:0,revision:2}
  const next=reconcileOrgDraft({percent:'',revision:1},{percent:5,revision:1},incoming,true)
  assert.equal(next.accepted,false)
  next.draft.percent='changed';assert.equal(incoming.percent,0)
})

test('server-trimmed names acknowledge the save while exact notes and employee numbers remain drafts', () => {
  const original={name:'old',note:'memo',employee_no:'001',revision:1}
  const draft={...original,name:'  new  '},saved={...original,name:'new',revision:2}
  assert.equal(reconcileOrgDraft(draft,original,saved,true).accepted,true)
  assert.equal(reconcileOrgDraft({...original,note:' memo '},original,original,true).accepted,false)
  assert.equal(reconcileOrgDraft({...original,employee_no:'1'},original,original,true).accepted,false)
})
