<script setup>
import {computed,onUnmounted,ref,watch} from 'vue'
import {api} from '../api'
import {stamp} from '../format'
import {clientRequestId} from '../telemetry'

const props=defineProps({show:Boolean,storeId:{type:String,required:true},period:{type:String,required:true},subject:{type:Object,default:null}})
const emit=defineEmits(['update:show'])
const items=ref([]),text=ref(''),busy=ref(false),error=ref('')
const pending=computed(()=>items.value.some(item=>['queued','claimed'].includes(item.state)))
const subjectLabel=computed(()=>{
  const s=props.subject;if(!s)return ''
  const row=s.row||{}
  return [s.node_name||s.node_id,row.file_name,row.sheet,row.row_no!=null?`第 ${row.row_no} 行`:''].filter(Boolean).join(' · ')
})
let timer
async function load(){
  try{items.value=(await api.agentQuestions(props.storeId,props.period)).items;error.value=''}catch(reason){error.value=reason.message}
  clearTimeout(timer);if(props.show&&pending.value)timer=setTimeout(load,4000)
}
watch(()=>[props.show,props.storeId,props.period],()=>{if(props.show)load();else clearTimeout(timer)},{immediate:true})
onUnmounted(()=>clearTimeout(timer))
async function ask(){
  const question=text.value.trim()
  if(!question||busy.value)return
  busy.value=true;error.value=''
  try{
    await api.agentAsk({request_id:clientRequestId(),store_id:props.storeId,period:props.period,question,...(props.subject?{subject:{node_id:props.subject.node_id,row:props.subject.row}}:{})})
    text.value='';await load()
  }catch(reason){error.value=reason.message}finally{busy.value=false}
}
const stateLabel={queued:'排队中',claimed:'Agent 正在核查',answered:'已回答',failed:'未能回答'}
</script>

<template>
  <n-drawer :show="show" :width="520" style="max-width:100vw" @update:show="value=>emit('update:show',value)">
    <n-drawer-content :title="subject?'这笔不对':'问 Agent'" closable>
      <p v-if="subject" class="subject" data-dispute-subject>异议对象：{{subjectLabel}}</p>
      <p class="hint">{{subject?'说明你认为哪里不对，Agent 会追溯原始资料和计算规则；核查结果会出现在月度工作台“待你确认”里。':'就本店本月的账单提问。'}}回答里的金额由核算引擎填入，Agent 看不到也不能编造金额。</p>
      <div class="ask">
        <n-input v-model:value="text" type="textarea" :maxlength="500" show-count :placeholder="subject?'例如：这单已经全额退款，不应计入收入':'例如：为什么本月净利润是负的？'" :input-props="{'aria-label':'向 Agent 提问'}" @keydown.enter.ctrl.prevent="ask"/>
        <n-button type="primary" :loading="busy" :disabled="!text.trim()" @click="ask">{{subject?'提交异议':'提问'}}</n-button>
      </div>
      <n-alert v-if="error" type="warning" :bordered="false" style="margin-top:12px">{{error}}</n-alert>
      <ol class="thread" aria-label="问答记录">
        <li v-for="item in items" :key="item.question_id" :data-question-state="item.state">
          <p class="question"><strong>{{item.subject?'异议':'问'}}</strong>{{item.question}}<small>{{item.subject?.node_name?`${item.subject.node_name} · `:''}}{{stamp(item.asked_at)}}</small></p>
          <p v-if="item.state==='answered'" class="answer"><strong>答</strong>{{item.answer}}</p>
          <p v-else-if="item.state==='failed'" class="failure">{{item.failure}}</p>
          <p v-else class="waiting">{{stateLabel[item.state]}}…</p>
        </li>
      </ol>
      <n-empty v-if="!items.length&&!error" description="还没有提问"/>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped>
.hint{font-size:12px;color:#657184;line-height:1.6;margin-bottom:12px}.subject{font-size:13px;color:#8b5914;background:#fffaf0;border:1px solid #f0d9a8;border-radius:6px;padding:8px 10px;margin-bottom:10px;overflow-wrap:anywhere}.ask{display:flex;flex-direction:column;gap:10px;align-items:flex-end}.thread{list-style:none;padding:0;margin-top:20px;display:flex;flex-direction:column;gap:16px}.thread li{border:1px solid #e2e5eb;border-radius:8px;padding:12px 14px}.thread p{font-size:13px;line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere}.thread strong{margin-right:8px;color:#35669a}.question small{display:block;color:#8a94a6;font-size:11px}.answer{margin-top:8px}.failure{margin-top:8px;color:#8b5914}.waiting{margin-top:8px;color:#657184}
</style>
