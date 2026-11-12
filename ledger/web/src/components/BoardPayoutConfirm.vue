<script setup>
// 月度看板上的「确认应发」：应发取看板已保存提成（结账时确认的金额），逐人逐店确认，
// 与提成页「核定实发」写同一份记录。只有已结账的店月可以确认。
import {computed,ref} from 'vue'
import {useMessage} from 'naive-ui'
import {commissionRequest} from './commissionRequest'
import {money} from '../format'

const props=defineProps({period:{type:String,default:''},runRefs:{type:Array,default:()=>[]},stores:{type:Array,default:()=>[]}})
const emit=defineEmits(['saved'])
const message=useMessage()
const open=ref(false),items=ref([]),loading=ref(false),reason=ref(''),riskAccepted=ref(false),saving=ref('')
const storeName=id=>props.stores.find(row=>row.store_id===id)?.store||id
const closedRefs=computed(()=>props.runRefs.filter(row=>row.state==='closed'&&row.run_id))
const openCount=computed(()=>props.runRefs.filter(row=>row.state!=='closed').length)
const validMoney=value=>/^-?\d+(?:\.\d{1,2})?$/.test(String(value??'').trim())
const confirmedCount=computed(()=>items.value.filter(item=>item.state==='confirmed').length)
const hasRisk=computed(()=>items.value.some(item=>item.context?.allocation_risk?.can_confirm))
const pending=computed(()=>items.value.filter(item=>item.state==='pending'&&ready(item)))

function blockedReason(context){
  if(context.statement_risk?.length)return '对账或推广证据有冲突，需先更正原始账单'
  if(context.allocation_risk&&!context.allocation_risk.can_confirm)return `${context.allocation_risk.pending_count} 项分配依据待核对，需先处理`
  return ''
}
function ready(item){
  if(!item.context||item.error||!reason.value.trim())return false
  if(item.context.allocation_risk&&!riskAccepted.value)return false
  return item.people.length?item.people.every(person=>validMoney(person.amount)):true
}
function total(item){return item.people.every(person=>validMoney(person.amount))?money(item.people.reduce((sum,person)=>sum+Number(person.amount),0)):'—'}

async function load(){
  open.value=true;loading.value=true;reason.value='';riskAccepted.value=false
  items.value=closedRefs.value.map(row=>({store_id:row.store_id,run_id:row.run_id,store:storeName(row.store_id),state:'loading',context:null,people:[],error:''}))
  let next=0
  async function worker(){
    while(next<items.value.length){
      const item=items.value[next++]
      try{
        const query=new URLSearchParams({store_id:item.store_id,period:props.period,run_id:String(item.run_id)})
        const context=await commissionRequest(`/payout-confirmations/context?${query}`)
        const confirmed=new Map((context.latest?.payouts||[]).map(person=>[person.person_id,person.amount]))
        item.context=context
        item.store=context.store||item.store
        item.people=context.people.map(person=>({...person,amount:confirmed.has(person.person_id)?Number(confirmed.get(person.person_id)).toFixed(2)
          :person.saved!=null?Number(person.saved).toFixed(2):''}))
        item.error=blockedReason(context)
        item.state=context.latest?'confirmed':'pending'
      }catch(error){item.state='error';item.error=error.message}
    }
  }
  await Promise.all([worker(),worker(),worker(),worker()])
  loading.value=false
}

async function save(item){
  const context=item.context
  await commissionRequest('/payout-confirmations',{body:{
    store_id:context.store_id,period:context.period,run_id:context.run_id,
    source_sha:context.source_sha,expected_confirmation_id:context.latest?.id||'',
    payouts:item.people.map(({person_id,amount})=>({person_id,amount})),
    no_payout:!item.people.length,reason:`月度看板确认应发：${reason.value.trim()}`,
    allocation_risk_ack:!!context.allocation_risk&&riskAccepted.value,
    allocation_override_id:context.allocation_risk?.close_override_id??null,
  }})
  const query=new URLSearchParams({store_id:item.store_id,period:props.period,run_id:String(item.run_id)})
  item.context=await commissionRequest(`/payout-confirmations/context?${query}`)
  item.state='confirmed'
}
async function saveOne(item){
  if(!ready(item)||saving.value)return
  saving.value=item.store_id
  try{await save(item);message.success(`${item.store} 应发已确认`);emit('saved')}
  catch(error){item.error=error.message;message.error(`${item.store}：${error.message}`)}
  finally{saving.value=''}
}
async function saveAll(){
  if(!pending.value.length||saving.value)return
  saving.value='__all__';let done=0,failed=0
  for(const item of [...pending.value]){
    try{await save(item);done++}catch(error){item.error=error.message;failed++}
  }
  saving.value=''
  if(failed)message.warning(`已确认 ${done} 家，${failed} 家未保存，原因见各店`);else message.success(`已确认 ${done} 家应发`)
  emit('saved')
}
</script>

<template>
  <n-button type="primary" size="small" :disabled="!period||!closedRefs.length" @click="load">确认应发</n-button>
  <n-modal v-model:show="open" preset="card" title="确认应发提成" :mask-closable="!saving" :closable="!saving" style="width:min(860px,calc(100vw - 32px))">
    <p class="payout-help">{{period}} · 已结账 {{closedRefs.length}} 家，已确认 {{confirmedCount}} 家<template v-if="openCount"> · 另有 {{openCount}} 家待结账，结账后才能确认</template>。应发默认取看板「已保存提成」（结账时确认的金额），可逐人修改；保存后与提成页「核定实发」为同一份记录。</p>
    <n-spin :show="loading">
      <div class="payout-stores">
        <section v-for="item in items" :key="item.store_id" class="payout-store">
          <header>
            <strong>{{item.store}}</strong>
            <n-tag size="small" :bordered="false" :type="item.state==='confirmed'?'success':item.state==='error'||item.error?'error':'warning'">
              {{item.state==='loading'?'读取中':item.state==='confirmed'?'已确认':item.state==='error'?'读取失败':item.error?'暂不能确认':'待确认'}}
            </n-tag>
            <span class="payout-total">应发合计 ¥{{total(item)}}</span>
            <n-button size="tiny" :disabled="!ready(item)||!!saving" :loading="saving===item.store_id" @click="saveOne(item)">{{item.state==='confirmed'?'重新确认':'确认本店'}}</n-button>
          </header>
          <p v-if="item.error" class="payout-error">{{item.error}}</p>
          <p v-else-if="item.context?.allocation_risk" class="payout-error">本店有 {{item.context.allocation_risk.pending_count}} 项分配依据待核对，结账时已人工确认。</p>
          <p v-if="item.context&&!item.people.length" class="payout-muted">本期没有提成人员，确认即记为无需提成。</p>
          <div v-for="person in item.people" :key="person.person_id" class="payout-person">
            <span>{{person.person}}</span>
            <small>看板已保存提成 {{person.saved==null?'—':money(person.saved)}}</small>
            <n-input v-model:value="person.amount" size="small" inputmode="decimal" placeholder="应发金额" :aria-label="`${item.store}${person.person}应发`" style="width:140px"/>
          </div>
        </section>
        <p v-if="!loading&&!items.length" class="payout-muted">本月还没有已结账的店。</p>
      </div>
    </n-spin>
    <n-input v-model:value="reason" type="textarea" :rows="2" maxlength="400" show-count placeholder="填写确认依据，例如：已按看板核对本月提成" style="margin-top:12px"/>
    <n-checkbox v-if="hasRisk" v-model:checked="riskAccepted" style="margin-top:8px">部分店仍有分配依据待核对；我已知风险，按上面金额逐人核定应发</n-checkbox>
    <template #footer>
      <div class="payout-footer">
        <n-button :disabled="!!saving" @click="open=false">关闭</n-button>
        <n-button type="primary" :disabled="!pending.length||!!saving" :loading="saving==='__all__'" @click="saveAll">全部待确认按以上金额确认（{{pending.length}} 家）</n-button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.payout-help{margin:0 0 12px;color:#475569;font-size:13px;line-height:1.6}
.payout-stores{max-height:56vh;overflow:auto;display:flex;flex-direction:column;gap:10px}
.payout-store{border:1px solid #e2e8f0;border-radius:8px;padding:10px 12px}
.payout-store header{display:flex;align-items:center;gap:10px}
.payout-total{margin-left:auto;color:#334155;font-size:13px}
.payout-person{display:flex;align-items:center;gap:12px;margin-top:8px}
.payout-person span{min-width:90px}
.payout-person small{color:#64748b;flex:1}
.payout-error{margin:6px 0 0;color:#b45309;font-size:12px}
.payout-muted{margin:6px 0 0;color:#94a3b8;font-size:12px}
.payout-footer{display:flex;justify-content:flex-end;gap:8px}
</style>
