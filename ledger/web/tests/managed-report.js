import {createApp,h,KeepAlive} from 'vue'
import {createPinia} from 'pinia'
import {NConfigProvider} from 'naive-ui'
import {createRouter,createMemoryHistory} from 'vue-router'
import Reports from '../src/views/CommissionReports.vue'
import {useCommission} from '../src/commissionStore'
import '../src/commission.css'
const base={kind:'managed_detail',store:'淘宝美食专家',store_id:'s1',period:'2026-06',team:'淘系运营一部',team_id:'t1',status:'其中托管部分',amount:null,confirmed_amount:null,notes:'其中托管部分，不重复相加'}
const people=[{...base,key:'a',subject:'做货甲',person:'做货甲',person_id:'a',sales:100,gross:55,profit_after_labor:40,trial_amount:.8},
  {...base,key:'b',subject:'抽点乙',person:'抽点乙',person_id:'b',sales:0,gross:0,profit_after_labor:0,trial_amount:1.2}]
const tree=[{...base,key:'team',subject:'淘系运营一部 · 托管合计',sales:100,gross:55,profit_after_labor:40,trial_amount:2,
  children:people.map(p=>({...p,children:[{...p,key:p.key+'-product',subject:'托管生日派对布置商品',product_id:'1054398586749'}]}))}]
const creator=new URLSearchParams(location.search).has('creator')
const metrics=new URLSearchParams(location.search).has('metrics')
window.fetch=async(url,options={})=>{
  const path=String(url),reply=data=>new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}})
  if(path.endsWith('/reports/query')){
    const scope=JSON.parse(options.body)
    const creatorRows=[{...base,kind:'person',person:'李素林',status:'已结账',sales:6842.17,
      creator_cost:1534.78,creator_gross:5307.39,creator_profit:4020.76,
      gross:2122.96,profit_after_labor:1608.3,amount:80.67,trial_amount:80.67}]
    const metricPeople=[{...base,kind:'person',person:'做货甲',person_id:'a',sales:100,creator_cost:40,creator_gross:60,creator_profit:50,
      managed_sales:100,labor_cost:20,store_labor_cost:20,profit_after_labor:80,trial_amount:1.6,amount:1.6,stores:1,periods:1},
      {...base,kind:'person',person:'抽点乙',person_id:'b',sales:0,creator_cost:0,creator_gross:0,creator_profit:0,managed_sales:0,labor_cost:0,store_labor_cost:20,profit_after_labor:0,trial_amount:2.4,amount:2.4,stores:1,periods:1},
      {...base,kind:'person',person:'待核对样本',person_id:'pending',managed_sales:null,managed_sales_pending:true,labor_cost:null,labor_pending:true,profit_after_labor:null,stores:0,periods:0}]
    const shownPeople=scope.person_ids?.length?metricPeople.filter(p=>scope.person_ids.includes(p.person_id)):metricPeople
    const metricRows=scope.view==='store_people'?[{...base,kind:'store',person:'店铺合计',sales:200,managed_sales:100,labor_cost:20,profit_after_labor:80},...shownPeople]:shownPeople
    const managedTree=scope.person_ids?.length?tree.map(t=>({...t,children:t.children.filter(p=>scope.person_ids.includes(p.person_id))})).filter(t=>t.children.length):tree
    const items=scope.view==='managed'?managedTree:metrics?metricRows:creator?creatorRows:[{...base,kind:'managed',person:'托管商品',managed_sales:100,sales:100}]
    return reply({view:scope.view,items,count:items.length,total:999,
      missing_periods:0,trial_periods:0,run_ids:[1],fingerprint:'fixture',selection:{start:'2026-06',end:'2026-06'},available_people:[]})
  }
  if(path.includes('/settlements'))return reply({settlements:[]})
  if(path.endsWith('/people'))return reply({people:[]})
  if(path.includes('/export/reports/managed'))return new Response('\ufeff托管团队,人员,托管销售额\n淘系运营一部,做货甲,100\n',{headers:{'Content-Type':'text/csv'}})
  throw new Error('Unexpected request: '+path)
}
const app=createApp({render:()=>h(NConfigProvider,null,{default:()=>h(KeepAlive,null,{default:()=>h(Reports)})})})
app.use(createRouter({history:createMemoryHistory(),routes:[{path:'/',component:Reports}]}))
const pinia=createPinia();app.use(pinia)
const state=useCommission(pinia);state.ready=true;state.start='2026-06';state.end='2026-06';state.reportView='store_people'
app.mount('#app')
