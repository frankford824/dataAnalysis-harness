// Real editor with isolated, in-memory API fixtures. No production writes.
import {createApp,h,ref,onMounted,KeepAlive} from 'vue'
import {createPinia} from 'pinia'
import {createRouter,createMemoryHistory,RouterView} from 'vue-router'
import {NConfigProvider,NMessageProvider,NDialogProvider} from 'naive-ui'
import Workspace from '../src/views/CommissionWorkspace.vue'
import {useCommission} from '../src/commissionStore'
import {useApp} from '../src/store'
import '../src/commission.css'
const unspecified=new URLSearchParams(location.search).has('unspecified')
const people=[{id:'leader',name:'测试组长',alias:'运营一组',parent_id:'',archived:0},{id:'member',name:'测试成员',employee_no:'582',parent_id:'leader',archived:0}]
const row={scheme_id:'s',revision:1,store_id:'s1',product_id:'1000161277346',product_name:'测试商品',people:[{person_id:'member',name:'测试成员',rate:'.05',duty:null}],state:'enabled'}
let saved=null, storeMemberReads=0
window.fetch=async(url,options={})=>{
  const path=String(url),reply=data=>new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}})
  if(path.endsWith('/schemes/s'))return reply({editor_context:{setting:{valid_from:'2026-05-01T00:00:00',valid_to:'',mode:'distribute'},people:[{person_id:'member',name:'测试成员',rate:'.05',duty:unspecified?null:'cut',duty_source:unspecified?'unspecified':'product'}]},id:'s',revision:1,active_version:'v',product_name:row.product_name,versions:[{id:'v',recorded_at:'2026-05-01',actor:'test',reason:'登记',body:{segments:[{valid_from:'2026-05-01T00:00:00',valid_to:'',mode:'distribute',allocations:[{person_id:'member',rate:'.05',duty:unspecified?undefined:'cut'}]}]}}]})
  if(path.endsWith('/settings')&&options.method==='POST'){saved=JSON.parse(options.body);row.setting={managed:saved.managed,managed_team_id:saved.managed_team_id};return reply({revision:2})}
  if(path.includes('/store-members')){storeMemberReads++;return reply({members:[{person_id:'member',duty:'cut',suggested_duty:'cut',confirmed:false}]})}
  if(path.endsWith('/people'))return reply({people})
  if(path.includes('/settings/count'))return reply({total:1})
  if(path.includes('/settings'))return reply({rows:[row],count:1,total:1,has_more:false})
  throw new Error(`Unexpected API ${path}`)
}
const Wrapper={setup(){const editor=ref();const open=()=>editor.value.edit(row);onMounted(open);return()=>h('div',[h('button',{onClick:open},'重新打开测试编辑'),h(Workspace,{ref:editor})])}}
const app=createApp({render:()=>h(NConfigProvider,null,{default:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(RouterView,null,{default:({Component})=>h(KeepAlive,null,{default:()=>Component})})})})})})
const pinia=createPinia();app.use(pinia)
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:Wrapper}]});app.use(router)
const state=useCommission(pinia);state.ready=true;state.people=people;state.start='2026-06';state.end='2026-07'
useApp(pinia).navigation={stores:[{id:'s1',name:'测试店'}],platforms:[]}
await router.isReady()
app.mount('#app')
const wait=async fn=>{for(let i=0;i<150;i++){if(fn())return;await new Promise(r=>setTimeout(r,40))}throw new Error('Timed out')}
const assert=(v,m)=>{if(!v)throw new Error(m)}
try{
  await wait(()=>document.querySelector('[aria-label="身份1"] .n-base-selection'))
  if(unspecified){
    assert(document.querySelector('[aria-label="身份1"]').textContent.includes('未指定'),'Missing duty falsely displayed as produce')
    ;[...document.querySelectorAll('button')].find(e=>e.textContent==='保存').click()
    await wait(()=>saved)
    assert(!Object.hasOwn(saved.allocations[0],'duty'),'Saving silently assigned a duty')
    assert(storeMemberReads===0,'Editor must not read inferred member duties')
    await wait(()=>document.querySelector('.table-assignee'))
    assert(document.querySelector('.table-assignee').textContent.includes('未指定'),'List fabricated produce')
    document.querySelector('#test-result').textContent='PASS: 无身份显示未指定；打开并保存不补做货；列表未指定'
  }else{
  const duty=document.querySelector('[aria-label="身份1"]')
  assert(duty.textContent.includes('抽点')&&!duty.textContent.includes('（'),'Duty label still truncated')
  duty.querySelector('.n-base-selection').click()
  await wait(()=>document.querySelector('.n-base-select-option'))
  assert([...document.querySelectorAll('.n-base-select-option')].map(e=>e.textContent).join('|')==='做货|抽点','Duty options not concise')
  ;[...document.querySelectorAll('.n-base-select-option')].find(e=>e.textContent==='做货').click()
  document.querySelector('.managed-setting [role="checkbox"]').click()
  await wait(()=>document.querySelector('[aria-label="托管团队"]'))
  document.querySelector('[aria-label="托管团队"] .n-base-selection').click()
  await wait(()=>[...document.querySelectorAll('.n-base-select-option')].some(e=>e.textContent==='运营一组'))
  assert(![...document.querySelectorAll('.n-base-select-option')].some(e=>e.textContent==='测试成员'),'Non-team offered')
  ;[...document.querySelectorAll('.n-base-select-option')].find(e=>e.textContent==='运营一组').click()
  await new Promise(r=>setTimeout(r,350))
  const box=document.querySelector('.n-drawer').getBoundingClientRect()
  assert(box.right<=innerWidth+1&&box.left>=-1,'Drawer outside viewport')
  assert([...document.querySelectorAll('.allocation-row > *')].every(e=>e.getBoundingClientRect().right<=innerWidth+1),'Allocation controls overflow')
  assert(document.body.textContent.includes('个人毛利、利润及提成不变'),'Scope missing')
  ;[...document.querySelectorAll('button')].find(e=>e.textContent==='保存').click()
  await wait(()=>saved)
  assert(saved.managed===true&&saved.managed_team_id==='leader','Managed classification missing')
  assert(saved.allocations[0].duty==='produce'&&saved.allocations[0].rate==='0.05000000','Duty/rate lost')
  assert(saved.expected_revision===1&&saved.valid_from,'Missing optimistic lock or effective date')
  document.querySelector('#test-result').textContent='PASS: 完整身份 → 托管勾选 → 团队选择 → 生效时间/版本/比例保存；布局不溢出'
  }
}catch(e){document.querySelector('#test-result').textContent='FAIL: '+e.message;console.error(e)}
