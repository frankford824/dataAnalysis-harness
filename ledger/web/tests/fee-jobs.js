// Real FeesView, isolated API fixture; no production rule or amount writes.
import {createApp,h} from 'vue'
import {createPinia} from 'pinia'
import {createRouter,createMemoryHistory,RouterView} from 'vue-router'
import {NConfigProvider,NDialogProvider,NMessageProvider} from 'naive-ui'
import Fees from '../src/views/FeesView.vue'
import {useApp} from '../src/store'
import {accessibleTabs} from '../src/components/ui/accessibleTabs'
import '../src/design.css'
import '../src/app.css'
import '../src/ledger-ui.css'
let rules=['运费','手续费'].map(value=>({platform:'taobao',field:'subject',how:'contains',value,major:'software_fee',minor:'',stage:'after',exclude:false,count_without_order:false,note:'',by:'',at:''}))
let job=new URLSearchParams(location.search).has('recover')?{id:'fixture-job',saved:true,status:'running',total:2,completed:1,succeeded:1,failed:0,percent:75,stores:[{store_id:'s1',store:'测试店甲',state:'done',phase:'已完成核算'},{store_id:'s2',store:'测试店乙',state:'running',phase:'归类核算',percent:50}]}:null
window.fetch=async(url,options={})=>{
  const path=String(url),reply=x=>new Response(JSON.stringify(x),{headers:{'Content-Type':'application/json'}})
  if(path==='/api/fees/jobs')return reply({job})
  if(path.includes('/retry')){job={...job,status:'done',completed:2,succeeded:2,failed:0,percent:100,error:'',stores:job.stores.map(s=>({...s,state:'done',phase:'已完成核算',error:''}))};return reply({job})}
  if(path==='/api/fees/preview')return reply({fee_revision:'base',periods:[]})
  if(path==='/api/fees'&&options.method==='POST'){
    const body=JSON.parse(options.body);if(!body.request_id||body.expected_fee_revision!=='base')throw new Error('Missing idempotency or revision guard')
    rules=body.rules
    job={id:'fixture-job',saved:true,status:'failed',total:2,completed:2,succeeded:1,failed:1,percent:100,error:'部分店铺核算失败，可仅重试失败项',stores:[{store_id:'s1',store:'测试店甲',state:'done',phase:'已完成核算'},{store_id:'s2',store:'测试店乙',state:'failed',phase:'核算失败',error:'测试：临时资源不足'}]}
    return reply({saved:true,job})
  }
  if(path.startsWith('/api/fees?'))return reply({rules,fee_revision:'base',unmatched:[],known:[],log:[],platforms:[{id:'taobao',name:'淘宝天猫'}],majors:[{id:'software_fee',name:'平台服务费'}],fields:[{id:'subject',name:'业务描述',platform:'*'}],hows:[{id:'contains',name:'包含'}],stages:[{id:'after',name:'补充归类'}]})
  throw new Error('Unexpected API '+path)
}
const app=createApp({render:()=>h(NConfigProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(NMessageProvider,null,{default:()=>h(RouterView)})})})})
const pinia=createPinia();app.use(pinia)
app.directive('ledger-tabs',accessibleTabs)
app.use(createRouter({history:createMemoryHistory(),routes:[{path:'/',component:Fees}]}))
const state=useApp(pinia);state.storeId='s1';state.noted('fees.tab','rules').value='rules'
app.mount('#app')
