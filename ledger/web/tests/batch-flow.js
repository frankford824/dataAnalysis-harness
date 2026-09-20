import {createApp,h,ref,onMounted} from 'vue'
import {NConfigProvider,NMessageProvider} from 'naive-ui'
import {createRouter,createMemoryHistory} from 'vue-router'
import Batch from '../src/components/CommissionBatchDialog.vue'
import '../src/commission.css'
const people=[{id:'leader',name:'测试组长',alias:'运营一组',default_cut_rate:'.01'},{id:'member',name:'测试成员',parent_id:'leader'}]
let preview=null,applied=false,unexpected=[]
window.fetch=async(url,options={})=>{
  const path=String(url),body=options.body?JSON.parse(options.body):null
  const reply=data=>new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}})
  if(path.endsWith('/org/fill-hierarchy'))return reply({added:[{person_id:'leader',rate:'.01',source:'hierarchy',duty:'cut'}]})
  if(path.endsWith('/settings/preview')){
    preview=body
    if(body.operation==='classification')return reply({id:'fixture',count:1,stores:1,new_count:0,rows:[{store_id:'s1',store:'测试店铺',product_id:'123456789001',valid_from:'2026-06-01T00:00:00',valid_to:'',mode:'distribute',before:[],after:[],managed:body.template.managed,managed_team:'运营一组'}]})
    return reply({id:'fixture',count:1,stores:1,new_count:1,rows:[{store_id:'s1',store:'测试店铺',product_id:'123456789001',valid_from:'2026-06-01T00:00:00',valid_to:'',mode:'distribute',before:[],after:body.changes[0].allocations.map(p=>({...p,name:people.find(x=>x.id===p.person_id).name}))}]})
  }
  if(path.endsWith('/settings/apply/fixture')){applied=true;return reply({count:1,stores:1})}
  unexpected.push(path);throw new Error(`Unexpected write ${path}`)
}
const Wrapper={setup(){const batch=ref();const open=()=>batch.value.open({kind:'new',store_id:'s1'});onMounted(open);return()=>h('div',[h('button',{onClick:open},'打开批量新增测试'),h('button',{onClick:()=>batch.value.open({targets:[{store_id:'s1',product_id:'123456789001',revision:1}]})},'打开批量分类测试'),h(Batch,{ref:batch,stores:[{id:'s1',name:'测试店铺'}],people})])}}
const app=createApp({render:()=>h(NConfigProvider,null,{default:()=>h(NMessageProvider,null,{default:()=>h(Wrapper)})})})
app.use(createRouter({history:createMemoryHistory(),routes:[{path:'/',component:Wrapper},{name:'commission-org',path:'/org',component:{render:()=>null}}]}));app.mount('#app')
const wait=async(fn)=>{for(let i=0;i<150;i++){if(fn())return;await new Promise(r=>setTimeout(r,40))}throw new Error('Timed out')}
const assert=(value,text)=>{if(!value)throw new Error(text)}
const button=text=>[...document.querySelectorAll('button')].find(b=>b.textContent.includes(text))
const fill=(el,text)=>{el.value=text;el.dispatchEvent(new Event('input',{bubbles:true}))}
try{
  await wait(()=>document.querySelector('.batch-dialog'))
  fill(document.querySelector('[aria-label="批量宝贝ID"]'),'123456789001')
  document.querySelector('[aria-label="批量人员1"] .n-base-selection').click()
  await wait(()=>[...document.querySelectorAll('.n-base-select-option')].some(e=>e.textContent==='测试成员'))
  assert(document.body.textContent.includes('运营一组'),'Organization grouping missing')
  ;[...document.querySelectorAll('.n-base-select-option')].find(e=>e.textContent==='测试成员').click()
  fill(document.querySelector('[aria-label="批量比例1"]'),'3')
  button('按组织补上级抽成').click()
  await wait(()=>document.body.textContent.includes('来自上级抽成'))
  await new Promise(r=>setTimeout(r,350))
  const footer=document.querySelector('.batch-dialog .n-card__footer').getBoundingClientRect()
  assert(footer.bottom<=innerHeight+1,'Batch footer outside viewport')
  assert(document.body.textContent.includes('不修改组织上下级'),'Scope missing')
  button('预览修改').click()
  await wait(()=>preview)
  const allocations=preview.changes[0].allocations
  assert(allocations.some(p=>p.person_id==='leader'&&p.source==='hierarchy'&&p.duty==='cut'),'Hierarchy source lost')
  await wait(()=>button('确认保存1条设置'))
  button('确认保存1条设置').click()
  await wait(()=>applied)
  await new Promise(r=>setTimeout(r,50))
  assert(unexpected.length===0,'Unrequested organization/default-duty write')
  assert(!Object.hasOwn(preview.changes[0],'managed'),'Default must preserve classification')
  for(const category of ['managed','unmanaged']){
    preview=null;applied=false
    button('打开批量分类测试').click()
    await wait(()=>document.querySelector('.classification-settings [role="checkbox"]'))
    document.querySelector('.classification-settings [role="checkbox"]').click()
    const choice=document.querySelector('[aria-label="批量托管分类"]')
    choice.value=category;choice.dispatchEvent(new Event('change',{bubbles:true}))
    if(category==='managed'){
      button('预览修改').click()
      await wait(()=>document.querySelector('[role="alert"]')?.textContent.includes('托管团队'))
      assert(preview===null,'Missing team must not submit')
      document.querySelector('[aria-label="批量托管团队"] .n-base-selection').click()
      await wait(()=>[...document.querySelectorAll('.n-base-select-option')].some(e=>e.textContent==='运营一组'))
      ;[...document.querySelectorAll('.n-base-select-option')].find(e=>e.textContent==='运营一组').click()
    }
    await new Promise(r=>setTimeout(r,0))
    assert(!document.querySelector('[aria-label="批量人员1"]'),'Classification-only must not require people')
    button('预览修改').click();await wait(()=>preview)
    assert(preview.operation==='classification','Wrong operation')
    assert(preview.template.managed===(category==='managed'),'Classification lost')
    assert(preview.template.managed_team_id===(category==='managed'?'leader':''),'Team lost or stale')
    await wait(()=>button('确认保存1条设置'))
    assert(document.body.textContent.includes(category==='managed'?'托管商品 · 运营一组':'非托管商品'),'Preview classification missing')
    button('确认保存1条设置').click();await wait(()=>applied)
    await new Promise(r=>setTimeout(r,350))
  }
  document.querySelector('#test-result').textContent='PASS: 默认保持分类；仅修改分类 → 缺团队拦截 → 设为托管/非托管 → 预览 → 保存；不重填人员'
}catch(e){document.querySelector('#test-result').textContent='FAIL: '+e.message;console.error(e)}
