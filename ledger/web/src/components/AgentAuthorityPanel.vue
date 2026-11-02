<script setup>
import {computed,ref,watch} from 'vue'
import {useMessage} from 'naive-ui'
import {api} from '../api'
import {stamp} from '../format'
import {clientRequestId} from '../telemetry'

const props=defineProps({period:{type:String,required:true}})
const emit=defineEmits(['changed'])
const message=useMessage()
const status=ref(null),error=ref(''),busy=ref(false)
const applied=computed(()=>(status.value?.changes||[]).filter(change=>change.status==='applied'))
const standing=computed(()=>status.value?.standing||{})
const label=computed(()=>!status.value?'正在读取':standing.value.active?(standing.value.outdated_rules?.length?'规则口径已更新，待你同意':'长期自动处理中'):'未开启')

async function load(){
  if(!props.period)return
  try{status.value=await api.agentAuthority(props.period);error.value=''}catch(reason){error.value=reason.message}
}
watch(()=>props.period,load,{immediate:true})
async function consent(){
  if(busy.value)return
  busy.value=true;error.value=''
  try{
    const preview=await api.agentAuthorityPreview({request_id:clientRequestId(),period_from:props.period,standing:true,reason:'长期授权 Agent 按已发布规则自动处理'})
    const result=await api.agentAuthorityApply({request_id:clientRequestId(),proposal_id:preview.proposal_id,proposal_sha:preview.proposal_sha,input_sha:preview.input_sha})
    message.success(`已开启，Agent 开始自动处理（${result.grant_count} 项）`);await load();emit('changed')
  }catch(reason){error.value=reason.message}finally{busy.value=false}
}
async function stop(){
  if(busy.value)return
  busy.value=true;error.value=''
  try{
    const result=await api.agentAuthorityRevoke({request_id:clientRequestId(),period:props.period,reason:'人工停止 Agent 自动处理'})
    if(result.failed?.length)error.value=`已停止 ${result.revoked} 项，${result.failed.length} 项未停止：${result.failed[0].reason}`
    else message.success('已停止自动处理，已完成的改动保留待你复核')
    await load();emit('changed')
  }catch(reason){error.value=reason.message}finally{busy.value=false}
}
async function undo(change){
  if(busy.value)return
  busy.value=true;error.value=''
  try{
    const current=await api.automationDecision(change.decision_id)
    await api.automationDecisionRevoke(change.decision_id,{request_id:clientRequestId(),reason:'人工撤回 Agent 自动处理的改动',input_sha:current.input_sha})
    message.success('已撤回这项改动，系统重新核算');await load();emit('changed')
  }catch(reason){error.value=reason.message}finally{busy.value=false}
}
defineExpose({load})
</script>

<template>
  <section class="agent-authority" aria-label="Agent 自动处理授权" :data-standing="standing.active?'active':'off'">
    <div class="head">
      <div>
        <h3>Agent 自动处理 · {{label}}</h3>
        <p v-if="standing.active">{{standing.actor}} 于 {{stamp(standing.at)}} 同意；新店铺、新月份自动覆盖，直到有人停止。</p>
        <p>Agent 判断该用哪条规则，金额由核算引擎按原始依据计算；已结账月份不会被改动，结账与实发仍由你最终确认。</p>
        <p v-if="standing.outdated_rules?.length" class="warn">以下规则的计算口径有更新，同意后才会继续自动处理：{{standing.outdated_rules.join('、')}}</p>
        <p v-if="status&&!status.enabled" class="warn">服务器的自动处理开关未启用，同意会保存，启用后才执行。</p>
      </div>
      <div class="actions">
        <n-button v-if="!standing.active||standing.outdated_rules?.length" type="primary" :loading="busy" @click="consent">{{standing.active?'同意新口径':'同意，长期自动处理'}}</n-button>
        <n-popconfirm v-if="status?.delegated" positive-text="确认停止" negative-text="取消" @positive-click="stop">
          <template #trigger><n-button :disabled="busy">停止自动处理</n-button></template>
          停止后 Agent 不再自动改账，已完成的改动保留。确认停止？
        </n-popconfirm>
      </div>
    </div>
    <n-alert v-if="error" type="warning" :bordered="false">{{error}}<n-button text @click="load">重试</n-button></n-alert>
    <details v-if="status?.changes?.length" class="changes" :open="applied.length>0">
      <summary>本月 Agent 已自动处理 {{applied.length}} 项（共 {{status.changes.length}} 条记录），结账即视为你已确认</summary>
      <ul><li v-for="change in status.changes" :key="change.decision_id" :data-change-status="change.status">
        <strong>{{change.label}}</strong> · {{change.store_name}} · {{stamp(change.at)}} · {{change.status==='applied'?'已生效':change.status==='revoked'?'已撤回':'已结账冻结'}}
        <n-popconfirm v-if="change.can_revoke" positive-text="确认撤回" negative-text="取消" @positive-click="undo(change)">
          <template #trigger><n-button text size="small" :disabled="busy">撤回此项</n-button></template>
          撤回后本店本月重新核算，这项不再自动处理。确认撤回？
        </n-popconfirm>
      </li></ul>
    </details>
  </section>
</template>

<style scoped>
.agent-authority{border:1px solid #cfe0f5;background:#f6faff;border-radius:8px;padding:18px;margin:16px 0}.head{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}.head h3{font-size:15px}.head p{font-size:12px;color:#657184;margin-top:6px;line-height:1.6}.warn{color:#8b5914!important}.actions{display:flex;flex-direction:column;gap:8px;flex-shrink:0}.changes{font-size:12px;color:#657184;margin-top:14px}.changes summary{cursor:pointer;color:#356bc6}.changes ul{padding-left:20px}.changes li{margin:8px 0}
@media(max-width:760px){.head{flex-direction:column}.actions{flex-direction:row;flex-wrap:wrap}}
</style>
