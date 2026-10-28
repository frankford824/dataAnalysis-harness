// Explicit synthetic transport; the rendered PeriodView and form are real.
import {createApp, h} from 'vue'
import {createPinia} from 'pinia'
import {createMemoryHistory, createRouter} from 'vue-router'
import {NConfigProvider, NDialogProvider, NMessageProvider, zhCN, dateZhCN} from 'naive-ui'
import PeriodView from '../src/views/PeriodView.vue'
import {api} from '../src/api'
import {useApp} from '../src/store'
import {accessibleTabs} from '../src/components/ui/accessibleTabs'
import '../src/design.css'
import '../src/app.css'
import '../src/ledger-ui.css'
import '../src/workflow.css'

const scope={id:'cost_synthetic',name:'成本完整性合成店',platform:'taobao'}
const assetFetch=window.fetch.bind(window)
window.fetch=(input,options)=>{
  const url=new URL(typeof input==='string'?input:input.url,location.href)
  if(url.pathname.startsWith('/api/'))throw Error('Unmocked API access in synthetic cost UI regression')
  return assetFetch(input,options)
}
let scenario='unknown',closed=false
window.costRegression={closeCalls:[],scenario:'unknown'}
const emptyPage={items:[],total:0,matching:0,category_counts:[],reason_counts:[]}
function snapshot(){
  const complete=scenario==='posted',pending=scenario==='posting'
  return {store_id:scope.id,store:scope.name,platform:'taobao',period:'2026-09',run_id:1,
    state:closed?'closed':'open',can_close:complete&&!closed,source_sync_pending:false,
    pricing_pending_count:complete?0:1,
    statement:[
      {id:'n_receipt',name:'销售收入',value:100,available:true,depth:0},
      {id:'n_goods',name:'商品成本',value:complete?-40:null,available:complete,depth:0},
      {id:'gross_profit',name:'毛利',value:complete?60:null,available:complete,depth:0},
      {id:'net_profit',name:'利润',value:complete?60:null,available:complete,depth:0}],
    findings:complete?[]:[{id:'required_cost_unresolved',name:'成本未完整',message:'一笔必需成本尚未补齐',passed:false,blocking:true}],
    close_verification:{allowed:complete&&!closed,issues:complete?[]:[{code:pending?'recompute_pending':'required_cost_unresolved',message:pending?'补录正在自动核算':'一笔必需成本尚未补齐',hard:true}]},
    cost_coverage:{passed:true,expected:20,covered:19,uncovered:1,coverage:.95},
    cost_review:{observed:{goods:38,dropship:0,reshipment:0},known_partial:{goods:38,dropship:0,reshipment:0},
      amount_complete:{goods:complete,dropship:true,reshipment:true},requires_human:!complete,
      line_count:scenario==='unknown'?0:1,line_total:scenario==='unknown'?0:2,posted_line_total:complete?2:0,
      line_revision:scenario==='unknown'?0:1,line_posting_pending:pending,coverage_lines:complete?0:1,posted_line_count:complete?1:0},
    cost_line_posting:{total:complete?2:0},calculation_inputs:{metric_totals:{},unavailable_metrics:complete?[]:['goods_cost'],inapplicable_metrics:[]},
    commission:{people:[{person_id:'p1',person:'合成人员',sales:100,gross:complete?60:null,
      amount:complete?3:17,amount_after_labor:complete?3:15,amount_complete:complete}],
      total:complete?3:null,amount_complete:complete,unassigned_orders:0},gaps:[],sources:[],assumptions:[]}
}
api.store=async()=>({store:scope,periods:[{period:'2026-09'}],files:[],needed:[],sources:[]})
api.period=async()=>structuredClone(snapshot())
api.navigation=async()=>({stores:[scope],platforms:[{id:'taobao',name:'淘宝'}],periods:['2026-09'],ingest_mode:'nas'})
api.bootstrap=async()=>({stores:[scope],platforms:[{id:'taobao',name:'淘宝'}],periods:['2026-09'],statement:[],sources:[]})
api.pricingStatus=async()=>({pending_count:scenario==='posted'?0:1,groups:[],status:'pending'})
api.pricingGaps=async()=>structuredClone(emptyPage)
api.coverageGaps=async()=>structuredClone(emptyPage)
api.close=async(...args)=>{
  window.costRegression.closeCalls.push(args)
  if(scenario!=='posted')throw Error('Synthetic incomplete cost must not be submitted for closing')
  closed=true
  return {state:'closed',run_id:1}
}
api.manualCostPreview=async()=>{throw Error('Unknown total must not be auto-previewed')}
api.telemetry=async()=>({ok:true})

const pinia=createPinia()
const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}}]})
await router.push('/?period=2026-09')
await router.isReady()
const app=createApp({setup:()=>()=>h(NConfigProvider,{locale:zhCN,dateLocale:dateZhCN},
  {default:()=>h(NDialogProvider,null,{default:()=>h(NMessageProvider,null,{default:()=>h('div',[
    h('p',{id:'test-result'},'WAIT: synthetic cost completeness'),
    h('button',{id:'scenario-unknown',onClick:()=>select('unknown')},'模拟必需成本未齐'),
    h('button',{id:'scenario-posted',onClick:()=>select('posted')},'模拟补录已入账'),
    h('button',{id:'scenario-posting',onClick:()=>select('posting')},'模拟补录待核算'),
    h(PeriodView,{id:scope.id})])})})})})
app.use(pinia);app.use(router);app.directive('ledger-tabs',accessibleTabs)
const store=useApp(pinia)
await store.loadNavigation();await store.loadModel();app.mount('#app')
function select(value){
  scenario=value;closed=false;window.costRegression.scenario=value
  store.invalidate([scope.id])
}
window.selectCostScenario=select
