import {createApp,h,onMounted} from 'vue'
import {createPinia} from 'pinia'
import {NConfigProvider} from 'naive-ui'
import {useCommission} from '../src/commissionStore'
import LedgerMultiSelect from '../src/components/ui/LedgerMultiSelect.vue'
import '../src/commission.css'

let renamed=false
window.fetch=async(url)=>{
  const path=String(url)
  if(path==='/api/navigation')return new Response(JSON.stringify({
    stores:[{id:'douyin_qianhuajian',name:renamed?'蔡果-抖店浅花涧':'抖音浅花涧节日装饰',aliases:['抖音浅花涧节日装饰'],platform:'douyin'}],
    platforms:[{id:'douyin',name:'抖音'}],periods:['2026-06'],default_period:'2026-06',data_revision:renamed?'new':'old',
  }),{headers:{'Content-Type':'application/json'}})
  if(path==='/api/commission-v2/people')return new Response(JSON.stringify({people:[]}))
  throw new Error('Unexpected fixture request: '+path)
}
const Root={setup(){
  const state=useCommission()
  onMounted(()=>state.init({shops:'douyin_qianhuajian'}))
  return ()=>h(NConfigProvider,null,{default:()=>h('main',{style:'padding:32px;max-width:900px'},[
    h('h1','店名同步回归测试'),
    h('p','提成筛选 → 订单台更名 → 页面刷新 → 新旧名称均可搜索；店铺 ID 不变'),
    h('button',{onClick:()=>{renamed=true}},'模拟订单台更名'),
    h('button',{onClick:()=>state.refresh({force:true})},'刷新店铺'),
    h('p',{'aria-label':'当前店名'},state.storeOptions[0]?.label || '加载中'),
    h(LedgerMultiSelect,{modelValue:state.storeIds,'onUpdate:modelValue':v=>state.storeIds=v,
      options:state.storeOptions,label:'家店铺','aria-label':'筛选店铺'}),
    h('p',{'aria-label':'选中店铺ID'},state.storeIds.join(',') || '未选择'),
  ])})
}}
createApp(Root).use(createPinia()).mount('#app')
