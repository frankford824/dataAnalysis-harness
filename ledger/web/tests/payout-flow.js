// Serve with pnpm dev, then open /static/tests/payout-flow.html.
// Mounts the real report component; all API calls are isolated fixture data.
import {createApp, h, KeepAlive} from 'vue'
import {createPinia} from 'pinia'
import {NConfigProvider} from 'naive-ui'
import {createRouter, createMemoryHistory} from 'vue-router'
import Reports from '../src/views/CommissionReports.vue'
import {useCommission} from '../src/commissionStore.js'
import '../src/commission.css'

const targets=[
  {person_id:'p1',person:'测试甲',store_id:'s1',store:'测试店一',period:'2026-06',run_id:101,status:'试算',amount:10},
  {person_id:'p1',person:'测试甲',store_id:'s2',store:'测试店二',period:'2026-06',run_id:102,status:'试算',amount:20},
]
const riskMode=new URLSearchParams(location.search).has('risk')
let contextRequests=0, saved=null
window.fetch=async (url,options={})=>{
  const path=String(url)
  const reply=body=>new Response(JSON.stringify(body),{status:200,headers:{'Content-Type':'application/json'}})
  if(path.includes('/reports/query'))return reply({sales_pending_scopes:[{store_id:'s1',period:'2026-06',person_id:'p1',sales_pending_products:['test-product']}],view:'people',items:[{person_id:'p1',person:'测试甲',amount:30,trial_amount:30,confirmed_amount:null,confirmation_state:'pending',stores:2,periods:1,status:'试算'}],count:1,total:30,run_ids:[101,102],selection:{start:'2026-06',end:'2026-06'},confirmation_scopes:targets,person_confirmation_scopes:targets,available_people:[]})
  if(path.includes('/payout-confirmations/context')){
    if(!path.includes('run_id=101'))throw new Error('Incorrect selected run')
    contextRequests++
    return reply({store_id:'s1',store:'测试店一',period:'2026-06',run_id:101,source_sha:'fixture',people:[{person_id:'p2',person:'测试乙',suggested:5},{person_id:'p1',person:'测试甲',suggested:10},...Array.from({length:10},(_,i)=>({person_id:`p${i+3}`,person:`测试成员${i+3}`,suggested:i+1}))],history:[],
      allocation_risk:riskMode?{pending_count:388,can_confirm:true,close_override_id:77,close_reason:'已核对并人工结账',has_allocation_evidence:true,ignored_findings:[{id:'allocation_basis',name:'分配依据待核对'},{id:'source_sync_pending',name:'订单数据仍在同步'}]}:null})
  }
  if(path.endsWith('/payout-confirmations')){saved=JSON.parse(options.body);return reply({id:'saved'})}
  if(path.includes('/settlements'))return reply({settlements:[]})
  if(path.endsWith('/people'))return reply({people:[]})
  throw new Error(`Unexpected API ${path}`)
}
const app=createApp({render:()=>h(NConfigProvider,null,{default:()=>h(KeepAlive,null,{default:()=>h(Reports)})})})
app.use(createRouter({history:createMemoryHistory(),routes:[{path:'/',component:Reports}]}))
const pinia=createPinia();app.use(pinia)
const state=useCommission(pinia)
state.ready=true;state.start='2026-06';state.end='2026-06';state.reportView='people'
app.mount('#app')
const wait=async condition=>{for(let i=0;i<150;i++){if(condition())return;await new Promise(r=>setTimeout(r,40))}throw new Error('Timed out waiting for UI')}
const buttons=()=>[...document.querySelectorAll('button')]
const assert=(value,message)=>{if(!value)throw new Error(message)}
try{
  await wait(()=>buttons().some(b=>b.textContent==='测试甲'))
  assert(document.querySelector('a[href*="sales-attribution/audit"]'),'Missing global sales attribution audit export')
  buttons().find(b=>b.textContent==='测试甲').click()
  await wait(()=>document.querySelector('.payout-targets button'))
  assert(document.querySelector('.payout-targets').textContent.includes('编辑 →'),'Missing entry label')
  assert(document.body.textContent.includes('全部提成人员'),'Missing whole-store scope before selection')
  ;[...document.querySelectorAll('.payout-targets button')].find(b=>b.textContent.includes('测试店一')).click()
  await wait(()=>document.querySelector('.payout-focused input'))
  await new Promise(r=>setTimeout(r,350))
  const modal=document.querySelector('.payout-editor')
  const box=modal.getBoundingClientRect(),footer=modal.querySelector('.n-card__footer').getBoundingClientRect()
  assert(box.top>=0&&box.bottom<=innerHeight+1,'Editor overflows viewport')
  assert(footer.bottom<=innerHeight+1,'Save footer is outside viewport')
  const body=modal.querySelector('.n-card-content')
  assert(body.scrollHeight>body.clientHeight,'Twelve-person form must scroll inside modal')
  assert(contextRequests===1,'Editor must load selected context once')
  assert(document.querySelector('.payout-people label').classList.contains('payout-focused'),'Selected person must be first')
  assert(document.querySelector('.payout-focused').textContent.includes('测试甲'),'Wrong person highlighted')
  const save=()=>buttons().find(b=>b.textContent.includes('保存实发并归档'))
  assert(save().disabled,'Whole-store acknowledgment and reason are required')
  if(riskMode){
    assert(modal.textContent.includes('388 项主子订单金额缺少分配依据'),'Risk count must be shown before editing')
    assert(modal.textContent.includes('已核对并人工结账'),'Prior close basis must be visible')
    assert(modal.querySelector('a[href="/api/runs/101/allocation.csv"]'),'Evidence link missing')
  }
  const amount=document.querySelector('.payout-focused input')
  amount.value='12.50';amount.dispatchEvent(new Event('input',{bubbles:true}))
  const reason=document.querySelector('textarea')
  reason.value='测试调整';reason.dispatchEvent(new Event('input',{bubbles:true}))
  await new Promise(r=>setTimeout(r,0))
  assert(save().disabled,'Reason alone must not acknowledge all people')
  document.querySelector('[role="checkbox"]').click()
  if(riskMode){
    assert(save().disabled,'Whole-store checkbox must not waive allocation risk')
    const riskBox=[...modal.querySelectorAll('[role="checkbox"]')].find(box=>box.textContent.includes('分配依据已补齐'))
    assert(riskBox,'Separate allocation-risk acknowledgment missing')
    if(new URLSearchParams(location.search).has('preview')){
      document.querySelector('#test-result').textContent='PASS: 风险和结账依据可见；未单独确认前保存按钮不可用（仅测试数据）'
    }else riskBox.click()
  }
  if(!new URLSearchParams(location.search).has('preview')){
    await wait(()=>!save().disabled)
    save().click()
    await wait(()=>saved)
    assert(saved.run_id===101 && saved.store_id==='s1','Wrong save scope')
    assert(saved.payouts.length===12,'Must retain all other store members')
    assert(saved.payouts.find(p=>p.person_id==='p1').amount==='12.50','Changed value lost')
    assert(saved.payouts.find(p=>p.person_id==='p2').amount==='5.00','Other person changed')
    if(riskMode)assert(saved.allocation_risk_ack===true&&saved.allocation_override_id===77,'Risk attestation missing from save')
    document.querySelector('#test-result').textContent=riskMode
      ? 'PASS: 风险可见 → 全店范围确认 → 单独确认分配风险 → 保存人工实发（仅测试数据）'
      : 'PASS: 点击人员 → 选择店铺月份 → 打开并突出人员 → 核对整店范围 → 保存全部人员（仅测试数据）'
  }
}catch(error){document.querySelector('#test-result').textContent=`FAIL: ${error.message}`;console.error(error)}
