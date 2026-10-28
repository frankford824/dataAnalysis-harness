// Explicit synthetic transport; the real BoardView owns the interaction.
import {createApp,h} from 'vue'
import {createPinia} from 'pinia'
import {createMemoryHistory,createRouter} from 'vue-router'
import {NConfigProvider,NDialogProvider,NMessageProvider,zhCN,dateZhCN} from 'naive-ui'
import BoardView from '../src/views/BoardView.vue'
import {api} from '../src/api'
import {useApp} from '../src/store'
import {accessibleTabs} from '../src/components/ui/accessibleTabs'
import '../src/design.css'
import '../src/app.css'
import '../src/ledger-ui.css'
import '../src/workflow.css'

const scope={id:'refresh_synthetic',name:'复核合成店',platform:'taobao'}
const originalFetch=window.fetch.bind(window)
window.fetch=(input,options)=>{
  const url=new URL(typeof input==='string'?input:input.url,location.href)
  if(url.pathname.startsWith('/api/'))throw Error('Unmocked API in synthetic close refresh regression')
  return originalFetch(input,options)
}
const state=window.closeRefreshRegression={phase:'source',previews:[],confirmations:[]}
const navigation={stores:[scope],platforms:[{id:'taobao',name:'淘宝'}],periods:['2026-09'],ingest_mode:'nas'}
api.navigation=async()=>navigation
api.bootstrap=async()=>({...navigation,statement:[],sources:[]})
api.operationsReport=async()=>({period:'2026-09',summary:{net_income:'100.00',gross_profit:'50.00',profit_after_labor:'40.00'},
  version:'synthetic-declared',input_sha:'input-one',scope_sha:'scope-one',metrics:[],rows:[],total:0,issues:[],run_refs:[]})
api.operationsExport=()=>'#synthetic-export'
api.automationRuleContracts=async()=>({enabled:false,rules:[]})
api.automationMonth=async()=>({period:'2026-09',status:'needs_decision',input_sha:'input-one',
  counts:{total:1,completed:0,processing:0,needs_decision:1,waiting:0},
  stores:[{store_id:scope.id,store:scope.name,status:'needs_decision',stage:'ready_for_close',message:'等待本批复核',actions:[]}],
  decision_groups:[{kind:'close',title:'确认月结',count:1,store_ids:[scope.id]}],waiting_groups:[],policies:[],
  close:{eligible_store_ids:[scope.id],blocked_store_ids:[],can_preview:true}})
api.automationClosePreview=async body=>{
  state.previews.push(JSON.parse(JSON.stringify(body)))
  if(state.phase==='source')return {status:'processing',proposal_id:null,financial_applied:false,
    request_id:body.request_id,message:'正在重新核查上游来源',source_refresh:{required:true,status:'processing',refresh_id:'source-one'}}
  if(state.phase==='quote')return {status:'processing',proposal_id:null,financial_applied:false,
    request_id:body.request_id,message:'正在核对本批原日历史报价',source_quote_refresh:{required:true,status:'processing',refresh_id:'quote-one',completed_quotes:0,total_quotes:1}}
  if(state.phase==='pending')return {status:'pending',proposal_id:null,financial_applied:false,request_id:body.request_id,message:'本批上游暂不可用，尚未结账'}
  return {proposal_id:'proposal-one',proposal_sha:'proposal-sha',input_sha:'input-one',eligible:[{store_id:scope.id,run_id:1}],blocked:[]}
}
api.automationCloseConfirm=async body=>{
  state.confirmations.push(JSON.parse(JSON.stringify(body)))
  if(body.proposal_id!=='proposal-one'||body.input_sha!=='input-one')throw Error('Wrong synthetic financial proposal')
  return {status:'completed',completed:[{store_id:scope.id,run_id:1}],unfinished:[]}
}
api.telemetry=async()=>({ok:true})
const pinia=createPinia(),router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}}]})
await router.push('/');await router.isReady()
const app=createApp({setup:()=>()=>h(NConfigProvider,{locale:zhCN,dateLocale:dateZhCN},{default:()=>h(NDialogProvider,null,
  {default:()=>h(NMessageProvider,null,{default:()=>h('div',[h('p',{id:'test-result'},'WAIT: declared synthetic close refresh'),h(BoardView)])})})})})
app.use(pinia);app.use(router);app.directive('ledger-tabs',accessibleTabs)
const store=useApp(pinia);store.pick({period:'2026-09',store:'',platform:''})
await store.loadNavigation();await store.loadModel();app.mount('#app')
