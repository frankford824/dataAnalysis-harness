<script setup>
import {computed,ref,watch} from 'vue'
import {useMessage} from 'naive-ui'
import {api} from '../api'
import {initialCostInputs,sourceCostText} from '../costReviewInputs'
import {periodCostRequest,verifyPeriodCostProposal,verifyPeriodCostReceipt,periodCostRevocation,outcomeUnknown} from '../periodCostInput'
import {money,prettyPeriod} from '../format'
const props=defineProps({show:Boolean,revoke:Boolean,storeId:String,period:String,snapshot:Object})
const emit=defineEmits(['update:show','saved']),message=useMessage()
const rows=[['goods','商品成本'],['dropship','代发成本'],['reshipment','补发成本']]
const costs=ref({goods:'',dropship:'',reshipment:''}),reason=ref(''),busy=ref(false),error=ref('')
const basis=ref(null),proposal=ref(null),submission=ref(null),sourceRequest=ref(null),unsettled=ref(false)
const scope=()=>JSON.stringify([props.storeId,props.period,props.snapshot?.run_id,props.snapshot?.period_cost_input?.id,props.revoke])
const form=()=>JSON.stringify([costs.value,reason.value])
const invalidated=computed(()=>!!basis.value&&basis.value!==scope())
watch(()=>props.show,show=>{
  if(!show)return
  costs.value=initialCostInputs(props.snapshot?.cost_review);reason.value=''
  basis.value=scope();proposal.value=null;submission.value=null;sourceRequest.value=null;error.value='';unsettled.value=false
})
watch([costs,reason],()=>{if(!busy.value&&!unsettled.value){proposal.value=null;submission.value=null;sourceRequest.value=null}},{deep:true})
const blockReason=computed(()=>{
  if(props.revoke){
    if(!props.snapshot?.period_cost_input?.decision_id)return '没有可撤销的本期成本批准'
    if(!reason.value.trim()||reason.value.trim().length>500)return '请填写不超过500字的撤销依据'
    return ''
  }
  try{periodCostRequest(props.storeId,props.period,props.snapshot?.run_id,costs.value,reason.value);return ''}
  catch(error){return error.message}
})
const eligible=computed(()=>!blockReason.value)
function close(){if(!busy.value)emit('update:show',false)}
async function preview(){
  if(busy.value||!eligible.value)return
  busy.value=true;error.value='';const expectedScope=scope(),expectedForm=form()
  try{
    let request,result,command
    if(props.revoke){
      const item=props.snapshot.period_cost_input
      request={decision_id:item.decision_id,cost_input_id:item.id,store_id:props.storeId,period:props.period,reason:reason.value.trim()}
      result=await api.automationDecision(request.decision_id);command=periodCostRevocation(result,request,reason.value)
    }else{
      request=periodCostRequest(props.storeId,props.period,props.snapshot?.run_id,{...costs.value},reason.value)
      result=await api.automationDecisionPreview(request);command=verifyPeriodCostProposal(result,request)
    }
    if(expectedScope!==scope()||expectedForm!==form())throw Error('核算依据或金额已更新，请按当前依据重新预览')
    basis.value=expectedScope;sourceRequest.value=request;submission.value=command;proposal.value=result
  }catch(e){error.value=e.message;proposal.value=null;submission.value=null}
  finally{busy.value=false}
}
async function confirm(){
  if(busy.value||(invalidated.value&&!unsettled.value)||!proposal.value)return
  busy.value=true;error.value=''
  try{
    let result
    if(props.revoke){
      result=await api.automationDecisionRevoke(sourceRequest.value.decision_id,submission.value)
      if(result?.status!=='revoked'||result.decision_id!==sourceRequest.value.decision_id||!result.receipt_id)throw Error('尚未收到完整撤销回执；可按原请求重试核对')
    }else{
      if(!unsettled.value)verifyPeriodCostProposal(proposal.value,sourceRequest.value)
      result=await api.automationDecisionApply(submission.value);verifyPeriodCostReceipt(result,sourceRequest.value)
    }
    unsettled.value=false
    message.success(props.revoke?'成本批准已撤销，核算队列会自动更新':'本期成本已保存，核算队列会自动更新')
    emit('saved',result);emit('update:show',false)
  }catch(e){error.value=e.message;if(outcomeUnknown(e))unsettled.value=true}
  finally{busy.value=false}
}
</script>
<template>
  <n-modal :show="show" @update:show="value=>{if(!value)close()}" preset="card" :title="revoke?'撤销本期成本批准':'确认本期成本'" style="max-width:620px;width:calc(100vw - 32px)">
    <p>{{prettyPeriod(period)}} · {{revoke?'撤销后重新按原始资料核算本店成本。':'保存本店本期已核定的三项成本，个人提成与月结仍须分别确认。'}}</p>
    <p class="small muted">{{revoke?'已结账和已核定实发的月份继续保护。':'缺少完整总额时，请先补原始凭据。逐单缺口继续保留，已有补录不能与整期金额重复相加。'}}</p>
    <n-alert v-if="unsettled" type="warning" :bordered="false" role="status">上次提交可能已经保存，只是没有收到回执。请点「核对提交结果」按原请求重新核对，不会重复保存。</n-alert>
    <n-alert v-else-if="invalidated" type="warning" :bordered="false">核算依据已更新，请重新预览本期成本。</n-alert>
    <template v-if="!proposal">
      <div v-if="!revoke" class="period-cost-fields">
        <label v-for="[key,label] in rows" :key="key"><span>{{label}}总额</span><small>{{sourceCostText(snapshot?.cost_review,key)}}</small>
          <n-input v-model:value="costs[key]" :disabled="busy" inputmode="decimal" :maxlength="20" :input-props="{'aria-label':'本期'+label+'总额'}"/>
        </label>
      </div>
      <n-input v-model:value="reason" :disabled="busy" type="textarea" :maxlength="500" :placeholder="revoke?'填写撤销依据':'填写已有核定凭据及成本批准依据'" :input-props="{'aria-label':revoke?'成本撤销依据':'成本批准依据'}"/>
    </template>
    <template v-else>
      <dl class="period-cost-summary"><template v-for="[key,label] in rows" :key="key"><dt>{{label}}</dt><dd>{{money(revoke?proposal.values.costs[key]:proposal.effect.values.costs[key])}} 元</dd></template></dl>
      <dl v-if="!revoke && proposal.effect.preview?.metrics" class="period-cost-summary">
        <template v-for="[key,label] in [['gross_profit','毛利'],['net_profit','利润']]" :key="key">
          <dt>{{label}}影响</dt><dd>{{proposal.effect.preview.metrics[key]?.before==null?'待补齐':money(proposal.effect.preview.metrics[key].before)}} → {{proposal.effect.preview.metrics[key]?.after==null?'待补齐':money(proposal.effect.preview.metrics[key].after)}}</dd>
        </template>
      </dl>
      <p class="period-cost-reason">{{sourceRequest.reason}}</p>
      <p class="small muted">{{revoke?'确认撤销后重新核算原始资料，个人提成仍须另行核定。':'确认后自动重算本店本期。订单和人员明细尚未证明的金额仍保持待核。'}}</p>
    </template>
    <n-alert v-if="error" type="error" :bordered="false" role="alert">{{error}}</n-alert>
    <div class="period-cost-actions">
      <n-button :disabled="busy" @click="close">取消</n-button>
      <n-button v-if="proposal&&!unsettled" :disabled="busy" @click="proposal=null;submission=null">返回修改</n-button>
      <span v-if="!proposal && blockReason" class="small muted">{{blockReason}}</span>
      <n-button v-if="!proposal" type="primary" :loading="busy" :disabled="!eligible" @click="preview">{{revoke?'预览撤销成本批准':'预览本期成本'}}</n-button>
      <n-button v-else-if="unsettled" type="primary" :loading="busy" @click="confirm">核对提交结果</n-button>
      <n-button v-else type="primary" :loading="busy" :disabled="invalidated" @click="confirm">{{revoke?'确认撤销成本批准':'确认保存成本'}}</n-button>
    </div>
  </n-modal>
</template>
<style scoped>
.period-cost-fields{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin:18px 0}
.period-cost-fields label{display:flex;flex-direction:column;gap:7px;font-size:13px}
.period-cost-fields small{color:#657184;line-height:1.5}
.period-cost-summary{display:grid;grid-template-columns:1fr auto;gap:12px;margin:20px 0;font-size:14px}
.period-cost-summary dd{margin:0;font-variant-numeric:tabular-nums}
.period-cost-reason{white-space:pre-wrap;overflow-wrap:anywhere}
.period-cost-actions{display:flex;justify-content:flex-end;flex-wrap:wrap;gap:10px;margin-top:20px}
@media(max-width:560px){.period-cost-fields{grid-template-columns:1fr}}
</style>
