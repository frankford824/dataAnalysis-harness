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
    return reply({id:'fixture',count:1,stores:1,new_count:1,rows:[{store_id:'s1',store:'测试店铺',product_id:'123456789001',valid_from:'2026-06-01T00:00:00',valid_to:'',mode:'distribute',before:[],after:body.changes[0].allocations.map(p=>({...p,name:people.find(x=>x.id===p.person_id).name}))}]})
  }
  if(path.endsWith('/settings/apply/fixture')){applied=true;return reply({count:1,stores:1})}
  unexpected.push(path);throw new Error(`Unexpected write ${path}`)
}
const Wrapper={setup(){const batch=ref();const open=()=>batch.value.open({kind:'new',store_id:'s1'});onMounted(open);return()=>h('div',[h('button',{onClick:open},'打开批量新增测试'),h(Batch,{ref:batch,stores:[{id:'s1',name:'测试店铺'}],people})])}}
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
  document.querySelector('#test-result').textContent='PASS: 按组织选人 → 补上级抽成 → 预览保留身份和来源 → 仅保存商品规则；底部操作在屏内'
}catch(e){document.querySelector('#test-result').textContent='FAIL: '+e.message;console.error(e)}
