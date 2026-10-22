<script setup>
import {computed,h,onMounted,onUnmounted,ref,watch} from 'vue'
import {NButton} from 'naive-ui'
import {useRoute,useRouter} from 'vue-router'
import {api} from '../api'
import {stamp} from '../format'
import {amountText as expenseText,basisSourceLabel,canStart,identityLabel,memberRows,percentage,quantityLabel,trialState} from '../freightShipment'
import {useLatest} from './ui/useLatest'
import LedgerTable from './ui/LedgerTable.vue'
import FilePreviewPanel from './FilePreviewPanel.vue'

const props=defineProps({period:String,storeId:{type:String,default:''},platform:{type:String,default:''},refreshToken:Number})
const route=useRoute(),router=useRouter()
const job=ref(null),data=ref(null),selectedJob=ref(''),availableJob=ref(''),reason=ref(''),term=ref(''),applied=ref(''),page=ref(1)
const loading=ref(false),starting=ref(false),exporting=ref(false),error=ref(''),pollError=ref(false)
const detail=ref(null),detailLoading=ref(false),detailError=ref(''),showDetail=ref(false),preview=ref(null)
const stateRequest=useLatest(),resultRequest=useLatest(),detailRequest=useLatest()
let serial=0,detailSerial=0,timer,alive=true
const valid=computed(()=>/^\d{4}-(0[1-9]|1[0-2])$/.test(props.period || ''))
const scope=computed(()=>({store_id:props.storeId,platform:props.platform}))
const summary=computed(()=>data.value?.summary || {})
const erpGoodsRule=computed(()=>data.value?.rule_version==='shipment-erp-goods-amount.v2')
const selectedGroup=computed(()=>data.value?.groups.find(g=>g.id===reason.value))
const detailMembers=computed(()=>memberRows(detail.value))
const active=computed(()=>!canStart(job.value))
const oldVisible=computed(()=>data.value && job.value?.job_id!==data.value.job_id)

function pin(id){
  if(!alive)return
  if(route.query.shipment_job===id && route.query.period===props.period && route.query.view==='shipments')return
  router.replace({query:{...route.query,period:props.period,store_id:props.storeId,platform:props.platform,view:'shipments',shipment_job:id}})
}
async function readResult(id,clear=false){
  if(!valid.value || !id)return
  const ticket=++serial,period=props.period,wanted={...scope.value,reason:reason.value,q:applied.value,offset:(page.value-1)*30,limit:30}
  loading.value=true;error.value=''
  if(clear){data.value=null;detail.value=null;showDetail.value=false}
  try{
    const found=await resultRequest.run(signal=>api.freightShipmentResult(id,wanted,{signal}))
    if(!found || ticket!==serial || period!==props.period || !alive)return
    if(found.value.period!==period){await latest(true,true);return}
    data.value=found.value;selectedJob.value=id;pin(id)
  }catch(e){if(ticket===serial)error.value=e.message}
  finally{if(ticket===serial)loading.value=false}
}
async function latest(initial=false,ignorePin=false){
  if(!valid.value)return
  const period=props.period
  try{
    const found=await stateRequest.run(signal=>api.freightShipmentLatest(period,{signal}))
    if(!found || period!==props.period)return
    job.value=found.value.job;pollError.value=false
    const id=found.value.result?.job_id
    if(initial){
      const pinned=!ignorePin && typeof route.query.shipment_job==='string' && /^[a-f0-9]{32}$/.test(route.query.shipment_job)?route.query.shipment_job:''
      selectedJob.value=pinned || id || ''
      if(pinned && id && pinned!==id)availableJob.value=id
      if(selectedJob.value)await readResult(selectedJob.value,true)
      else {data.value=null;pin('')}
    }else if(id && id!==selectedJob.value){
      if(!data.value){selectedJob.value=id;await readResult(id,true)}else availableJob.value=id
    }
  }catch(e){if(period===props.period){if(initial)error.value=e.message;else pollError.value=true}}
}
async function start(){
  if(starting.value || active.value || !valid.value)return
  const period=props.period;starting.value=true;error.value=''
  try{const value=await api.freightShipmentStart(period);if(period===props.period)job.value=value}
  catch(e){if(period===props.period)error.value=e.message}
  finally{starting.value=false}
}
function select(id){reason.value=id;page.value=1;if(selectedJob.value)readResult(selectedJob.value)}
function search(){applied.value=term.value.trim();page.value=1;if(selectedJob.value)readResult(selectedJob.value)}
async function open(row){
  const ticket=++detailSerial
  detailRequest.cancel();detail.value=null;detailError.value='';showDetail.value=true;detailLoading.value=true
  const id=selectedJob.value,filter=JSON.stringify(scope.value)
  try{
    const found=await detailRequest.run(signal=>api.freightShipmentDetail(id,row.charge_id,scope.value,{signal}))
    if(found && id===selectedJob.value && filter===JSON.stringify(scope.value))detail.value=found.value
  }catch(e){if(ticket===detailSerial)detailError.value=e.message}finally{if(ticket===detailSerial)detailLoading.value=false}
}
async function download(){
  if(!data.value || exporting.value)return
  exporting.value=true;error.value=''
  const snapshot=data.value
  try{
    const {job_id,...filters}=snapshot.scope
    const blob=await api.freightShipmentExport(job_id,{...filters,scope_fingerprint:snapshot.scope_fingerprint})
    const url=URL.createObjectURL(blob),link=document.createElement('a')
    link.href=url;link.download=`包裹分摊试算-${snapshot.period}-${selectedGroup.value?.title || '全部'}.csv`;link.click()
    setTimeout(()=>URL.revokeObjectURL(url),1000)
  }catch(e){error.value=e.message}finally{exporting.value=false}
}
function original(member){
  router.push({name:'period',params:{id:member.store_id},query:{period:member.original_period || props.period}})
}
function viewSource(source){if(source?.file_sha)preview.value={sha256:source.file_sha,file:source.file || '金额依据原表',sheet:source.sheet,row_no:source.row_no}}
function showNew(){const id=availableJob.value;availableJob.value='';reason.value='';page.value=1;readResult(id,true)}
onMounted(async()=>{await latest(true);if(alive)timer=setInterval(()=>{if(!document.hidden)latest()},4000)})
onUnmounted(()=>{alive=false;clearInterval(timer);stateRequest.cancel();resultRequest.cancel();detailRequest.cancel()})
watch(()=>props.period,()=>{
  ++serial;stateRequest.cancel();resultRequest.cancel();detailRequest.cancel()
  data.value=null;job.value=null;detail.value=null;showDetail.value=false;selectedJob.value='';availableJob.value='';reason.value='';term.value='';applied.value='';page.value=1;loading.value=false
  if(valid.value)latest(true,true)
})
watch(()=>JSON.stringify(scope.value),()=>{detailRequest.cancel();showDetail.value=false;reason.value='';page.value=1;if(selectedJob.value)readResult(selectedJob.value,true)})
watch(()=>props.refreshToken,()=>latest())

const columns=[
  {title:'运单及店铺候选',key:'tracking_no',minWidth:220,mobileWidth:170,render:r=>h('div',{class:'shipment-id'},[
    h('strong',r.tracking_no),h('small',r.stores.length?r.stores.join('；'):'店铺未定')])},
  {title:'来源运费',key:'amount',width:135,mobileWidth:120,align:'right',render:r=>h('span',{title:r.amount},expenseText(r.amount))},
  {title:'本包分摊基数',key:'basis_total',width:145,mobileWidth:130,align:'right',render:r=>r.basis_total==null?'—':expenseText(r.basis_total)},
  {title:'方法与状态',key:'status',minWidth:200,mobileWidth:180,render:r=>h('div',[r.status==='allocated'?r.method_label:'资料待核',h('small',{class:'shipment-secondary'},r.reason_label || '试算可分摊，原账未改')])},
  {title:'依据',key:'action',width:110,mobileWidth:100,render:r=>h(NButton,{text:true,type:'primary',size:'small',onClick:()=>open(r)},()=> '查看分摊')},
]
const memberColumns=[
  {title:'实际发货商品',key:'product_name',minWidth:200,mobileWidth:180,render:r=>h('div',{class:'shipment-id'},[h('strong',r.product_name || r.product_id),h('small',`商品 ${r.product_id || '待核'}`),h('small',quantityLabel(r))])},
  {title:'订单身份',key:'child_id',minWidth:220,mobileWidth:190,render:r=>h('div',{class:'shipment-id'},[h('small',`主单 ${r.order_id || '待核'}`),h('small',`${identityLabel(r)} ${r.child_id || '见ERP发货记录'}`),r.is_reship?h('small','补发商品'):null])},
  {title:'本次分摊权重',key:'basis',width:150,mobileWidth:130,align:'right',render:r=>r.allocation?(detail.value?.charge.basis_unit==='ratio'?percentage(r.allocation.basis_weight):expenseText(r.allocation.basis_amount)):'—'},
  {title:'分摊比例',key:'factor',width:120,mobileWidth:110,align:'right',render:r=>percentage(r.allocation?.factor)},
  {title:'商品分摊运费',key:'cost',width:145,mobileWidth:130,align:'right',render:r=>r.allocation?h('span',{title:`计算值 ${r.allocation.amount}`},expenseText(r.allocation.amount)):'尚未分摊'},
  {title:'对照依据',key:'action',width:155,mobileWidth:140,render:r=>h('div',{class:'shipment-evidence-actions'},[h(NButton,{size:'small',text:true,type:'primary',disabled:!r.store_id,onClick:()=>original(r)},()=> '店铺原账'),r.basis_reference?.file_sha?h(NButton,{size:'small',text:true,type:'primary',onClick:()=>viewSource(r.basis_reference)},()=> '金额原表'):null])},
]
</script>

<template>
  <section class="shipment-trial">
    <div class="shipment-toolbar"><div><h2>按实际包裹分摊运费</h2><p>全月自动试算，按问题查看。原账和提成不变。</p></div><n-button type="primary" :disabled="!valid || active" :loading="starting" @click="start">{{data?'重新试算':'生成试算'}}</n-button></div>
    <n-alert v-if="error" type="error" :bordered="false">{{error}} <n-button text size="small" @click="latest(true,true)">读取最新试算</n-button></n-alert>
    <p v-if="!valid" class="shipment-empty">请选择一个月份查看包裹试算。</p>
    <div v-if="job && active" class="shipment-progress" role="status"><strong>{{trialState(job)}} · {{job.stage}}</strong><span v-if="job.total">{{job.done}} / {{job.total}}</span><p>{{oldVisible?'下方保留上一份结果，完成后可切换新试算。':'正在读取账单、确认发货范围和分配依据，无需逐单操作。'}}</p></div>
    <n-alert v-else-if="job && ['failed','interrupted'].includes(job.state)" type="warning" :bordered="false">{{trialState(job)}}：{{job.error}}<div v-if="data">下方仍是之前生成的试算记录。</div></n-alert>
    <p v-if="pollError" class="shipment-note">暂时无法检查试算进度，已展示的结果仍保留。</p>
    <div v-if="availableJob" class="shipment-progress" role="status"><span>新试算已完成，当前仍显示原试算。</span><n-button size="small" @click="showNew">查看新结果</n-button></div>
    <n-spin :show="loading">
      <template v-if="data">
        <p class="shipment-snapshot">账单月 {{data.period}} · 资料固定于 {{stamp(data.input_captured_at || data.created_at)}} · 记录 {{data.job_id.slice(0,8)}}</p>
        <n-alert v-if="data.rules_current===false" type="warning" :bordered="false">此记录使用历史规则，仅供对照；重新试算使用当前规则。</n-alert>
        <n-alert v-if="data.input_pending" type="warning" :bordered="false">生成时订单资料仍在同步；此结果仅使用当时已接收资料，不能作为完整分摊结论。</n-alert>
        <div class="shipment-metrics">
          <div><span>本范围来源金额</span><strong>{{expenseText(summary.source_amount)}}</strong><small>{{summary.charge_count}} 条收费；含待核候选</small></div>
          <div><span>试算可分摊金额</span><strong>{{expenseText(summary.allocated_amount)}}</strong><small>{{summary.allocated_count}} 条收费；尚未改账</small></div>
          <div class="shipment-pending"><span>待核金额（净额）</span><strong>{{expenseText(summary.pending_amount)}}</strong><small>{{summary.pending_count}} 条；含退费 {{expenseText(summary.pending_refund)}}</small></div>
        </div>
        <p class="shipment-note">来源金额 = 可分摊金额 + 待核净额，均非应补扣额。收费为正、退费冲减为负。筛选不缩小分摊分母，未定归属不能当成各店费用。</p>
        <div class="shipment-group-head"><h3>按问题查看</h3><n-button text size="small" @click="select('')">全部收费</n-button></div>
        <div class="shipment-groups"><button v-for="group in data.groups" :key="group.id" :class="{selected:reason===group.id}" :aria-pressed="reason===group.id" @click="select(group.id)"><strong>{{group.title}}</strong><span>{{group.count}} 条 · 净额 {{expenseText(group.amount)}}</span></button></div>
        <div class="shipment-table-head"><div><h3>{{selectedGroup?.title || '全部收费'}}</h3><span>{{data.count}} 条结果，点击“查看分摊”回查依据。</span></div><div class="shipment-search"><n-input v-model:value="term" size="small" placeholder="查运单号" clearable @keyup.enter="search"/><n-button size="small" @click="search">查找</n-button><n-button size="small" :loading="exporting" :disabled="loading || !data.count" @click="download">导出本组</n-button></div></div>
        <p class="shipment-mobile-note">左右滑动查看金额、状态和分摊依据。</p>
        <LedgerTable :columns="columns" :rows="data.rows" :row-key="r=>r.charge_id" :loading="loading" :max-height="500" empty="本范围暂无收费记录；未归属店铺的记录请在全部店铺查看"/>
        <div class="shipment-pages"><span>共 {{data.count}} 条</span><n-button size="small" :disabled="page<=1 || loading" @click="page--;readResult(selectedJob)">上一页</n-button><span>{{page}} / {{Math.max(1,Math.ceil(data.count/30))}}</span><n-button size="small" :disabled="page*30>=data.count || loading" @click="page++;readResult(selectedJob)">下一页</n-button></div>
        <details class="shipment-explanation"><summary>分摊规则与资料范围</summary><template v-if="erpGoodsRule"><p>同包多商品按聚水潭“商品金额”占比分摊：本商品的包内明细金额 ÷ 本包全部商品明细金额 × 本包运费。商品金额已是行合计，不再乘数量，也不扣退款。</p><p>只取本包实际发出的明细，套装内点胶、相纸等按金额汇总，不将不同商品的数量相加比较。单商品直接归属；多商品金额缺失、为负或全部为零时保留待核。补发同样按本次发货的商品金额，不能用原单金额代替缺失金额。</p></template><template v-else><p>同包多商品优先用原表分配率；没有分配率时，用实付减退款后的非负金额。拆包只计本次发出份额，跨主单先换成可比较金额。缺数量、金额或身份依据时保留待核。</p><p>补发不按新增零收入吞掉运费。单商品直接归属，多商品需有原单金额及补发范围依据。只有同主单商品的基数全部确认为零，且原核算允许比例分摊时才均分；缺失金额不当作零。</p></template><p>{{data.notice}} 原生无独立子单的平台按其商品行核对，不伪造子订单号。</p><p>规则 {{data.rule_version}} · 订单快照 {{data.snapshot_id}} · 已接收增量 {{data.consumed_seq}}</p><p>输入校验值 {{data.input_fingerprint}}</p><p v-for="(warning,i) in data.warnings" :key="i">{{warning.file}}：{{warning.reason}}</p></details>
      </template>
      <p v-else-if="valid && !loading && !active && !error" class="shipment-empty">本月尚无包裹分摊试算。生成后会固定记录，方便与原账核对。</p>
    </n-spin>
    <n-drawer :show="showDetail" :width="'min(1080px,100vw)'" @update:show="v=>{showDetail=v;if(!v)detailRequest.cancel()}"><n-drawer-content class="shipment-detail" title="本次运费怎么分" closable>
      <n-spin :show="detailLoading"><n-alert v-if="detailError" type="error">{{detailError}}</n-alert>
        <template v-if="detail"><div class="shipment-detail-head"><div><h3>{{detail.charge.tracking_no}}</h3><p>{{detail.charge.packet?.carrier || '承运商未提供'}} · {{detail.charge.packet?.ship_time || '发货时点待核'}}</p></div><div><span>本次来源运费</span><strong :title="detail.charge.amount">{{expenseText(detail.charge.amount)}}</strong></div></div>
          <n-alert v-if="detail.charge.status==='pending'" type="warning" :bordered="false"><strong>{{detail.charge.reason_label}}</strong><div>该笔保留待核，本次没有分给商品；不代表原账未计入，也不能重复补扣。</div></n-alert>
          <div v-else class="shipment-allocation-rule"><strong>{{detail.charge.method_label}}</strong><span v-if="detail.charge.basis_total!=null">本包分摊基数 {{expenseText(detail.charge.basis_total)}}</span><span v-else>直接归属或按原表比例在本包内重新归一，未将缺失金额填成零。</span><p>基数仅用于分费用，不是新增销售额。分摊合计等于来源运费；页面按分显示可能有舍入差，导出保留计算精度。</p></div>
          <h3 class="shipment-detail-title">本次实际发货商品</h3><p class="shipment-note">“店铺原账”打开整店对应账期，原账总额不能与本行试算直接相减。</p><p class="shipment-mobile-note">左右滑动查看金额权重、比例和商品分摊运费。</p>
          <LedgerTable :columns="memberColumns" :rows="detailMembers" :row-key="r=>r.target_key" :max-height="470" empty="尚无可追溯的发货商品行"/>
          <div class="shipment-source"><strong>收费来源</strong><span>{{detail.charge.file}} · {{detail.charge.sheet}} · 留档行 {{detail.charge.row_no}}</span><span>账单扫描时间 {{detail.charge.scan_time}}</span><n-button size="small" @click="viewSource(detail.charge)">查看原文件</n-button></div>
          <details class="shipment-explanation"><summary>查看商品金额与发货明细依据</summary>
            <div v-for="member in detailMembers" :key="member.target_key">
              <p>{{member.product_name || member.product_id}} · {{quantityLabel(member)}} · {{basisSourceLabel(member)}}</p>
              <p v-if="member.basis_amount!=null">{{member.basis_source==='erp_goods_amount'?'本包聚水潭商品金额':'原单权重'}}：{{member.basis_unit==='ratio'?percentage(member.basis_amount):expenseText(member.basis_amount)}}。</p>
              <ul v-if="member.basis_source==='erp_goods_amount'"><li v-for="line in member.erp_items || []" :key="line.internal_item_id">ERP订单 {{line.internal_order_id}} · 明细 {{line.internal_item_id}} · {{line.sku_id}} {{line.product_name}} · 数量 {{line.quantity ?? '未知'}} · 商品金额 {{expenseText(line.goods_amount)}}</li></ul>
              <n-button v-for="(source,i) in member.basis_reference?.platform_sources || []" :key="i" text size="small" @click="viewSource(source)">金额原表 {{i+1}}</n-button>
            </div>
            <p>输入校验值 {{detail.input_fingerprint}}</p><p>此处为固定试算记录；查看或导出不会改变已结账金额和提成。</p>
          </details>
        </template>
      </n-spin>
    </n-drawer-content></n-drawer>
    <FilePreviewPanel :show="!!preview" :target="preview" @update:show="v=>{if(!v)preview=null}"/>
  </section>
</template>

<style scoped>
.shipment-detail :deep(.shipment-evidence-actions){display:flex;gap:8px;flex-wrap:wrap;align-items:flex-start}
.shipment-detail :deep(.shipment-id strong){display:block;font-weight:550;white-space:normal;overflow-wrap:anywhere}.shipment-detail :deep(.shipment-id small){display:block;font-size:11px;color:#7a8698;white-space:normal;line-height:1.7}
.shipment-toolbar,.shipment-table-head,.shipment-group-head,.shipment-detail-head{display:flex;justify-content:space-between;align-items:flex-start;gap:18px}.shipment-toolbar{margin:20px 0}.shipment-toolbar h2{font-size:18px;font-weight:550;margin:0 0 7px}.shipment-toolbar p,.shipment-table-head span,.shipment-detail-head p{font-size:13px;color:#657184;line-height:1.7;margin:0}.shipment-progress{padding:15px 18px;background:#eef3ff;border-radius:7px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;color:#36579a;margin:16px 0;font-size:13px}.shipment-progress p{width:100%;margin:0;color:#657184}.shipment-progress strong{font-weight:550}.shipment-snapshot{font-size:12px;color:#718097;margin:20px 0 12px}.shipment-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.shipment-metrics>div{padding:18px 20px;background:#fff;border:1px solid #e2e7ef;border-radius:8px;min-width:0}.shipment-metrics span,.shipment-detail-head span{font-size:13px;color:#627087;display:block}.shipment-metrics strong,.shipment-detail-head strong{display:block;font-size:24px;font-weight:550;color:#203148;margin:8px 0;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.shipment-metrics small{font-size:12px;color:#718097;line-height:1.7;display:block}.shipment-metrics .shipment-pending{background:#fffaf1;border-color:#efdfbe}.shipment-pending strong{color:#945b12}.shipment-note{color:#718097;font-size:12px;line-height:1.8;margin:12px 0 22px}.shipment-group-head{align-items:center;margin:26px 0 12px}.shipment-group-head h3,.shipment-table-head h3{font-size:16px;font-weight:550;margin:0 0 4px}.shipment-groups{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:9px;margin-bottom:24px}.shipment-groups button{border:1px solid #e1e7ef;border-radius:7px;background:white;text-align:left;padding:13px 15px;cursor:pointer;color:#34445a;font:inherit;display:grid;gap:5px}.shipment-groups button.selected{border-color:#4268d8;background:#f1f5ff}.shipment-groups strong{font-size:13px;font-weight:550}.shipment-groups span{font-size:12px;color:#718097}.shipment-table-head{margin-bottom:14px;align-items:center}.shipment-search{display:flex;align-items:center;gap:8px;min-width:0}.shipment-search .n-input{width:180px}.shipment-pages{display:flex;justify-content:flex-end;align-items:center;gap:12px;font-size:12px;color:#718097;margin-top:14px}.shipment-empty{padding:28px;background:white;border:1px solid #e2e7ef;border-radius:8px;color:#718097;line-height:1.8}.shipment-explanation{font-size:12px;color:#657184;line-height:1.8;margin-top:20px;overflow-wrap:anywhere}.shipment-explanation summary{color:#4265be;cursor:pointer}.shipment-explanation p{margin:8px 0}.shipment-trial :deep(.shipment-id strong){display:block;font-weight:550;color:#26364d;white-space:normal;overflow-wrap:anywhere}.shipment-trial :deep(.shipment-id small),.shipment-trial :deep(.shipment-secondary){display:block;font-size:11px;color:#7a8698;white-space:normal;line-height:1.7}.shipment-detail-head{margin-bottom:18px}.shipment-detail-head h3{margin:0 0 6px;font-size:18px;overflow-wrap:anywhere}.shipment-detail-head>div:last-child{text-align:right;flex-shrink:0}.shipment-allocation-rule{padding:15px 18px;background:#f1f5ff;border-radius:7px;font-size:13px;color:#36579a;display:flex;gap:18px;flex-wrap:wrap}.shipment-allocation-rule p{width:100%;margin:0;color:#657184;font-size:12px}.shipment-detail-title{font-size:16px;font-weight:550;margin:22px 0 8px}.shipment-source{display:flex;align-items:center;gap:12px;font-size:12px;line-height:1.8;flex-wrap:wrap;margin-top:20px;color:#657184}.shipment-mobile-note{display:none}.n-alert{margin:16px 0}
@media(max-width:700px){.shipment-metrics{grid-template-columns:1fr 1fr}.shipment-metrics>div:last-child{grid-column:1/-1}.shipment-metrics>div{padding:14px}.shipment-metrics strong{font-size:20px}.shipment-toolbar{flex-wrap:wrap}.shipment-groups{grid-template-columns:1fr 1fr}.shipment-groups button{padding:12px;min-width:0}.shipment-table-head{align-items:flex-start;flex-direction:column}.shipment-search{width:100%;flex-wrap:wrap}.shipment-search .n-input{width:160px;flex:1}.shipment-pages{gap:8px;flex-wrap:wrap}.shipment-mobile-note{display:block;font-size:12px;color:#718097;margin:8px 0}.shipment-detail-head{flex-direction:column;gap:10px}.shipment-detail-head>div:last-child{text-align:left}.shipment-detail-head strong{font-size:21px}}
</style>
