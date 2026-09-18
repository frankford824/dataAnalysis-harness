import {createApp,h,ref,onMounted} from 'vue'
import {NConfigProvider} from 'naive-ui'
import Profit from '../src/components/ProfitCompositionDrawer.vue'
import '../src/commission.css'
let calls=[]
const products=Array.from({length:120},(_,i)=>({product_id:`p${i}`,product_name:`商品${i}`,orders:1,product_sales:100,sales:100,gross:50,profit:20,rate:.03,lines:[]}))
window.fetch=async url=>{
  const q=new URL(String(url),'http://localhost').searchParams;calls.push(Object.fromEntries(q))
  const id=q.get('product_id')
  const rows=id?products.filter(p=>p.product_id===id).map(p=>({...p,lines:[{order_id:'test-order',profit:20}]})):products
  return new Response(JSON.stringify({store_id:'s',period:'2026-06',run_id:1,person_id:'a',person:'测试',products:rows,excluded_product_ids:[],commission_trial:72}),{headers:{'Content-Type':'application/json'}})
}
const Wrapper={setup(){const target=ref(null);onMounted(()=>target.value={store_id:'s',period:'2026-06',person_id:'a',person:'测试',run_id:1});return()=>h(Profit,{target:target.value})}}
createApp({render:()=>h(NConfigProvider,null,{default:()=>h(Wrapper)})}).mount('#app')
const wait=async f=>{for(let i=0;i<150;i++){if(f())return;await new Promise(r=>setTimeout(r,40))}throw Error('Timed out')}
const assert=(x,msg)=>{if(!x)throw Error(msg)}
try{
  await wait(()=>document.querySelectorAll('.profit-product').length===50)
  assert(calls.length===1&&calls[0].include_orders==='false','Initial load fetched all orders')
  assert(document.body.textContent.includes('2,400.00'),'Total must cover all 120 products')
  ;[...document.querySelectorAll('button')].find(b=>b.textContent==='下一页').click()
  await wait(()=>document.querySelector('.profit-product')?.textContent.includes('商品50'))
  assert(calls.length===1,'Changing local product page should not reload data')
  document.querySelector('.profit-product').click()
  await wait(()=>document.body.textContent.includes('test-order'))
  assert(calls.length===2&&calls[1].product_id==='p50','Order detail must load only selected product')
  document.querySelector('#test-result').textContent='PASS: 商品每页50行、合计覆盖全部、翻页不重查、订单点击后按需读取'
}catch(e){document.querySelector('#test-result').textContent='FAIL: '+e.message;console.error(e)}
