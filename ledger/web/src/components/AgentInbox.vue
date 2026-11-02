<script setup>
import {computed,ref,watch} from 'vue'
import {useMessage} from 'naive-ui'
import {api} from '../api'
import {stamp} from '../format'
import {clientRequestId} from '../telemetry'

const props=defineProps({period:{type:String,required:true}})
const emit=defineEmits(['changed'])
const message=useMessage()
const inbox=ref(null),error=ref(''),busy=ref(false)
const upgrade=computed(()=>inbox.value?.upgrade)
const changed=computed(()=>(upgrade.value?.items||[]).filter(item=>item.status!=='same'&&!item.confirmed))
const disputes=computed(()=>inbox.value?.disputes||[])
const count=computed(()=>(upgrade.value?.needs_confirmation?1:0)+disputes.value.length)

async function load(){
  if(!props.period)return
  try{inbox.value=await api.agentInbox(props.period);error.value=''}catch(reason){error.value=reason.message}
}
watch(()=>props.period,load,{immediate:true})
function money(value){return typeof value==='number'?value.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2}):value==null?'暂不可用':String(value)}
async function run(task,done){
  if(busy.value)return
  busy.value=true;error.value=''
  try{await task();if(done)message.success(done);await load();emit('changed')}catch(reason){error.value=reason.message}finally{busy.value=false}
}
const recompute=()=>run(async()=>{for(const item of changed.value.filter(i=>i.status==='pending_recompute'))await api.recompute(item.store_id)},'已提交重算，完成后这里会更新')
const confirmUpgrade=()=>run(()=>api.upgradeConfirm({request_id:clientRequestId(),period:props.period,input_sha:upgrade.value.input_sha}),'已确认新版本结果，Agent 继续自动处理')
const acknowledge=item=>run(()=>api.agentQuestionAcknowledge(item.question_id),'已处理')
defineExpose({load,count})
</script>

<template>
  <section v-if="count||error" class="agent-inbox" aria-label="待你确认">
    <h3>待你确认 · {{count}} 件</h3>
    <n-alert v-if="error" type="warning" :bordered="false">{{error}}<n-button text @click="load">重试</n-button></n-alert>
    <article v-if="upgrade?.needs_confirmation" class="card" data-inbox-card="upgrade">
      <header><strong>新版本核对</strong><span>{{upgrade.same}} 家店结果与旧版本一致，{{upgrade.changed}} 家有变化<template v-if="upgrade.pending_recompute">，{{upgrade.pending_recompute}} 家待重算</template></span></header>
      <p class="advice">Agent 建议：逐家看下面的变化，确认无误后点“确认新版本结果”。确认前 Agent 不会自动改这个月的账。</p>
      <ul>
        <li v-for="item in changed" :key="item.store_id">
          <strong>{{item.store_name}}</strong>
          <span v-if="item.status==='pending_recompute'" class="muted"> · 待新版本重算</span>
          <span v-else-if="item.same_input===false" class="muted"> · 期间资料也有更新</span>
          <table v-if="item.differences.length"><tbody><tr v-for="diff in item.differences.slice(0,12)" :key="diff.node_id"><td>{{diff.name}}</td><td>{{money(diff.before)}}</td><td>→</td><td>{{money(diff.after)}}</td></tr></tbody></table>
          <p v-if="item.differences.length>12" class="muted">另有 {{item.differences.length-12}} 项变化，请到店铺页查看</p>
        </li>
      </ul>
      <footer>
        <n-button v-if="upgrade.pending_recompute" :loading="busy" @click="recompute">先重算这些店铺</n-button>
        <n-button v-else type="primary" :loading="busy" @click="confirmUpgrade">确认新版本结果</n-button>
      </footer>
    </article>
    <article v-for="item in disputes" :key="item.question_id" class="card" data-inbox-card="dispute" :data-question-state="item.state">
      <header><strong>员工异议 · {{item.store_name}}</strong><span>{{item.subject?.node_name}} · {{item.asked_by}} · {{stamp(item.asked_at)}}</span></header>
      <p class="question">{{item.question}}</p>
      <p v-if="item.state==='answered'" class="advice">Agent 核查：{{item.answer}}</p>
      <p v-else class="advice">{{item.failure||'Agent 未能给出核查结果'}}</p>
      <footer>
        <n-button type="primary" :loading="busy" @click="acknowledge(item)">认可，已处理</n-button>
        <router-link :to="{name:'period',params:{id:item.store_id},query:{period:item.period}}"><n-button>去这家店修改</n-button></router-link>
      </footer>
    </article>
  </section>
</template>

<style scoped>
.agent-inbox{border:1px solid #f0d9a8;background:#fffaf0;border-radius:8px;padding:18px;margin:16px 0;display:flex;flex-direction:column;gap:12px}.agent-inbox h3{font-size:15px}.card{background:white;border:1px solid #eadfca;border-radius:8px;padding:14px 16px}.card header{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;font-size:13px}.card header span{color:#657184;font-size:12px}.advice,.question{font-size:13px;line-height:1.7;margin-top:8px;white-space:pre-wrap;overflow-wrap:anywhere}.advice{color:#35506e}.card ul{padding-left:18px;font-size:12px;margin-top:8px}.card li{margin:8px 0}.card table{border-collapse:collapse;margin-top:4px}.card td{padding:2px 10px 2px 0;font-variant-numeric:tabular-nums}.muted{color:#8a94a6}.card footer{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}
</style>
