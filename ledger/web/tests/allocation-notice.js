import {createApp,h,ref} from 'vue'
import AllocationNotice from '../src/components/AllocationNotice.vue'

createApp({setup(){
  const historical=ref(true)
  return ()=>h('main',{style:'max-width:900px;margin:24px auto;padding:16px'},[
    h('h1','分配依据提示回归测试'),
    h('button',{onClick:()=>historical.value=!historical.value},'切换核算状态'),
    h(AllocationNotice,{snapshot:historical.value ? {run_id:102,has_allocation_evidence:true,
      allocation_correction:{from_run:101,verified_orders:3772,pending_orders:[{order_id:'test'}]}}
      : {run_id:103,has_allocation_evidence:true,allocation_pending_count:2}})
  ])
}}).mount('#app')
