<script setup>
import { ref,onMounted } from 'vue'
import { api } from '../api'
import { money } from '../format'
import FilePreviewPanel from './FilePreviewPanel.vue'
import {canConfirmStatement,recomputeReviewedStore} from '../statementReview'
const props=defineProps({storeId:String,period:String,runId:Number})
const emit=defineEmits(['close','updated'])
const data=ref(null),error=ref(''),busy=ref(false),offset=ref(0)
const chosen=ref({}),reason=ref({}),ack=ref({}),reviewer=ref(''),receipt=ref(''),preview=ref(null)
const revokeReason=ref('')
async function retryRecompute(){
  if(busy.value)return
  busy.value=true;error.value=''
  try{await recomputeReviewedStore(props.storeId,api.recompute);emit('updated')}
  catch(e){error.value=e.message}finally{busy.value=false}
}
async function revoke(item){
  if(busy.value)return
  busy.value=true;error.value=''
  try{await api.revokeStatementReview(props.storeId,props.period,{run_id:props.runId,group_id:item.group_id,
    reason:revokeReason.value,reviewer:reviewer.value});receipt.value='撤销依据已保存，正在按当前原始证据重算';
    await recomputeReviewedStore(props.storeId,api.recompute);emit('updated')
  }catch(e){error.value=e.message;await load()}finally{busy.value=false}
}
async function load(){
  try{error.value='';data.value=await api.statementReview(props.storeId,props.period,props.runId,offset.value)}
  catch(e){error.value=e.message}
}
async function save(group){
  if(busy.value)return
  busy.value=true;error.value='';receipt.value=''
  try{
    const saved=await api.confirmStatementReview(props.storeId,props.period,{run_id:props.runId,
      group_id:group.group_id,chosen:chosen.value[group.group_id],reason:reason.value[group.group_id]||'',
      acknowledged:!!ack.value[group.group_id],reviewer:reviewer.value})
    receipt.value=`核对依据已保存，审计编号 ${saved.audit_id}。正在重新核算；未改原文件、已结账结果或实发。`
    await recomputeReviewedStore(props.storeId,api.recompute)
    emit('updated')
  }catch(e){error.value=e.message;await load()}
  finally{busy.value=false}
}
function showSource(row){preview.value={sha256:row.file_sha,file:row.file_name,sheet:row.sheet,row_no:row.row_no,path:''}}
async function page(delta){offset.value=Math.max(0,offset.value+delta);await load()}
onMounted(load)
</script>
<template>
  <n-drawer :show="true" :width="1000" @update:show="v=>{if(!v&&!busy)emit('close')}">
    <n-drawer-content title="对账流水关联核对" closable>
      <n-alert type="info" :bordered="false">
        核对的是同一笔资金对应哪个原订单，不是直接改金额。先点击原文件核对平台流水；
        只有同店、完整流水号、时间、科目及金额一致的候选组可确认。
        确认仅选择一份原始记录，其他候选不重复入账；原文件和审计记录永久保留。
      </n-alert>
      <n-alert v-if="error" type="error">{{ error }}</n-alert>
      <n-alert v-if="data?.read_only" type="warning">当前记录已结账或核算依据已更新，仅供查看；不能直接改历史账或使用过期依据确认。</n-alert>
      <n-button v-if="data?.read_only||error" :disabled="busy" @click="emit('updated')">返回并刷新当前核算版本</n-button>
      <n-alert v-if="receipt" type="success">{{ receipt }}</n-alert>
      <n-button v-if="receipt||data?.needs_recompute" :disabled="busy" @click="retryRecompute">重试重算 / 刷新核对结果</n-button>
      <n-alert v-if="data?.needs_recompute" type="warning">当前为旧版留档，没有完整候选证据。请先重新核算本店；不能在旧记录上直接确认。</n-alert>
      <n-spin :show="busy">
        <n-empty v-if="data && !data.total" description="当前核算没有待处理的流水关联组" />
        <section v-for="group in data?.groups||[]" :key="group.group_id" style="border:1px solid var(--line);padding:16px;margin-top:16px">
          <h3>{{ group.can_confirm ? '金额已互证，选择正确原订单记录' : '需要补充原始证据' }}</h3>
          <n-alert v-if="group.reason" type="warning">{{ group.reason }}</n-alert>
          <n-radio-group v-model:value="chosen[group.group_id]" :disabled="!group.can_confirm||busy">
            <div v-for="row in group.members" :key="row.anchor" style="padding:12px 0;overflow-wrap:anywhere">
              <n-radio :value="row.anchor" :disabled="row.can_select===false">{{ row.file_name }} · {{ row.sheet||'CSV' }} · 第 {{ row.row_no }} 行</n-radio>
              <div>流水号：{{ row.event_id }} · {{ row.time }} · {{ row.subject }} · 金额 {{ money((row.income||0)+(row.outgo||0)) }}</div>
              <div>原表字段「{{ row.order_label||'订单引用' }}」：{{ row.order||'未提供' }} · 子订单/费用引用：{{ row.child||'未提供' }}</div>
              <div v-if="row.can_select===false">尚未匹配原订单，不能直接选为已确认记录</div>
              <n-button size="small" @click="showSource(row)">查看原文件</n-button>
            </div>
          </n-radio-group>
          <template v-if="group.can_confirm">
            <n-input v-model:value="reviewer" placeholder="核对人姓名（按填写内容留档）" maxlength="80" />
            <n-input v-model:value="reason[group.group_id]" type="textarea" placeholder="填写平台流水与原订单的核对依据，至少8字；不要只写已核对" maxlength="500" />
            <n-checkbox v-model:checked="ack[group.group_id]">我已核对平台原始流水，确认只采用所选记录的订单关联；金额不修改</n-checkbox>
            <n-button type="primary" :loading="busy" :disabled="!canConfirmStatement(group,chosen[group.group_id],reason[group.group_id],ack[group.group_id],reviewer,busy)" @click="save(group)">保存依据并重算</n-button>
          </template>
        </section>
        <div style="margin-top:16px;display:flex;gap:12px">
          <n-button :disabled="offset===0||busy" @click="page(-20)">上一页</n-button>
          <span>共 {{ data?.total||0 }} 组</span>
          <n-button :disabled="offset+20>=(data?.total||0)||busy" @click="page(20)">下一页</n-button>
        </div>
        <details v-if="data?.history?.length" style="margin-top:16px"><summary>已保存核对依据</summary>
          <n-input v-model:value="reviewer" placeholder="核对人姓名（按填写内容留档）" maxlength="80" />
          <n-input v-model:value="revokeReason" placeholder="如需撤销，填写原因（至少8字）" maxlength="500" />
          <p v-for="item in data.history" :key="item.group_id">{{ item.at }} · {{ item.actor }} · {{ item.reason }}
            <n-button v-if="item.action!=='revoke'" size="small" :disabled="busy||data.read_only||!reviewer.trim()||revokeReason.trim().length<8" @click="revoke(item)">撤销核对并重算</n-button>
            <span v-else>已撤销</span>
          </p>
        </details>
      </n-spin>
    </n-drawer-content>
  </n-drawer>
  <FilePreviewPanel v-if="preview" :show="true" :target="preview" @update:show="v=>{if(!v)preview=null}" />
</template>
