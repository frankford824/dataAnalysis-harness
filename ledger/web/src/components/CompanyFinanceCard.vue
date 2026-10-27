<script setup>
import {computed,ref} from 'vue'
import {money,stamp} from '../format'

const props=defineProps({group:{type:Object,required:true},stores:{type:Array,default:()=>[]}})
const emit=defineEmits(['open'])
const show=ref(false)
const basis=computed(()=>Array.isArray(props.group.current?.basis)?props.group.current.basis:null)
const unknown=computed(()=>Array.isArray(props.group.unknown_revenue_store_ids)?props.group.unknown_revenue_store_ids:null)
const saved=computed(()=>props.group.closed_allocations||[])
const gap=computed(()=>props.group.reason_code==='overhead_legacy_frozen_basis'?'已保存逐店冻结份额，统一公司基准尚未验证。':props.group.reason_code==='overhead_frozen_basis_unverified'?'已冻结公司基准仍有收入未验证，需要核对历史依据。':props.group.reason_code==='overhead_frozen_input_changed'?'当前公司依据与已保存冻结基准不一致。':'请按后台说明核对公司依据。')
const changeLabels={scope_added:'范围新增',scope_removed:'范围移除',current_revenue_unverified:'当前收入未确定',revenue_evidence_available:'当前收入依据已提供（冻结时未验证）',revenue_changed:'保存收入值变化'}
const totalLabels={same:'与冻结总额一致',changed:'当前总额与冻结总额不同',unknown:'当前总额未确定',unverified_legacy_basis:'统一基准未验证，历史总额暂不可比较'}
function name(id){return props.stores.find(store=>store.store_id===id)?.store||id}
function open(){show.value=true;emit('open')}
</script>

<template>
  <section class="company-finance-card" aria-label="公司财务待核事项" data-decision-kind="company_finance">
    <div><h3>{{group.title||'核对公司冻结公摊依据'}}</h3><p>{{group.summary}}</p><p>责任角色：{{group.role_label||'公司财务负责人'}}<span v-if="group.assignee_status==='role_unconfigured'">（角色未配置）</span></p><p>{{gap}}</p><p>当前范围：{{basis===null?'未提供':`${basis.length} 家店`}} · 收入未确定：{{unknown===null?'未提供':`${unknown.length} 家店`}} · 已保存结账记录：{{saved.length}} 家店</p><p>{{group.delivery_hint}}</p></div>
    <n-button @click="open">查看公司依据</n-button>
  </section>
  <n-drawer v-model:show="show" :width="860" style="max-width:100vw"><n-drawer-content title="公司冻结公摊只读证据" closable><section class="company-finance-evidence" aria-label="公司冻结公摊只读证据">
    <p>{{group.summary}}</p><p>责任角色：{{group.role_label||'公司财务负责人'}}<span v-if="group.assignee_status==='role_unconfigured'">（角色未配置）</span></p><p>{{group.delivery_hint}}</p>
    <h3>已结店保存记录</h3><p class="muted">以下为后端提供的原记录，未计算历史差额。</p>
    <div v-if="saved.length" class="scroll"><table><thead><tr><th>店铺</th><th>已冻结核算</th><th class="right">已保存兼职份额</th><th>来源版本引用</th></tr></thead><tbody><tr v-for="row in saved" :key="`${row.store_id}:${row.run_id}`"><td>{{name(row.store_id)}}</td><td>{{row.run_id??'未提供'}}</td><td class="right num">{{money(row.amount)}}</td><td class="source-reference"><small>代码：{{row.engine||'未留档'}}</small><small>模型：{{row.model_revision||'未留档'}}</small><small>输入：{{row.input_fingerprint||'未留档'}}</small></td></tr></tbody></table></div><p v-else>本摘要未提供已结店保存记录。</p>
    <h3>公司冻结依据</h3><template v-if="group.frozen"><p>已保存总额：{{money(group.frozen.total)}} · 留档时间：{{stamp(group.frozen.at)||'未提供'}}</p><p class="fingerprint">冻结输入指纹：{{group.frozen.input_sha||'未提供'}}</p><div v-if="Array.isArray(group.frozen.basis)" class="scroll"><table><thead><tr><th>冻结范围</th><th>基准核算</th><th class="right">基准收入</th><th class="right">基准份额</th></tr></thead><tbody><tr v-for="row in group.frozen.basis" :key="row.store_id"><td>{{name(row.store_id)}}</td><td>{{row.run_id??'未提供'}}</td><td class="right num">{{money(row.revenue)}}</td><td class="right num">{{money(group.frozen.shares?.[row.store_id])}}</td></tr></tbody></table></div></template><p v-else>统一公司基准未验证，不能从当前数据重建当时金额。</p>
    <h3>后台标记的依据变化</h3><p>{{totalLabels[group.total_evidence_status]||'总额比较状态未提供'}}</p><p v-if="group.comparison_status==='legacy_basis_unverified'">统一历史基准尚未验证，未生成历史差额。</p><div v-if="group.changes?.length" class="scroll"><table><thead><tr><th>店铺</th><th>后台标记</th><th class="right">冻结基准收入</th><th class="right">当前保存收入</th></tr></thead><tbody><tr v-for="row in group.changes" :key="row.store_id"><td>{{name(row.store_id)}}</td><td>{{changeLabels[row.kind]||'依据待核'}}</td><td class="right num">{{money(row.frozen_revenue)}}</td><td class="right num">{{money(row.current_revenue)}}</td></tr></tbody></table></div>
    <h3>当前待核依据</h3><p>当前总额：{{money(group.current?.total)}} · 当前已知收入合计：{{money(group.current?.basis_total)}}</p><p>收入未确定：{{unknown===null?'未提供':unknown.length?unknown.map(name).join('、'):'0 家店'}}</p><p class="fingerprint">当前输入指纹：{{group.current?.input_sha||'未提供'}}</p><div v-if="basis" class="scroll"><table><thead><tr><th>当前范围</th><th>当前核算</th><th class="right">保存收入</th></tr></thead><tbody><tr v-for="row in basis" :key="row.store_id"><td>{{name(row.store_id)}}</td><td>{{row.run_id??'未生成'}}</td><td class="right num">{{row.revenue==null?'—（未确定）':money(row.revenue)}}</td></tr></tbody></table></div><p v-else>本摘要未提供当前范围依据。</p>
  </section></n-drawer-content></n-drawer>
</template>

<style scoped>
.company-finance-card{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:18px;border:1px solid #e2e5eb;border-radius:8px;margin:16px 0}.company-finance-card h3,.company-finance-evidence h3{font-size:15px}.company-finance-card p{font-size:12px;color:#657184;margin-top:6px}.company-finance-evidence p{font-size:13px;line-height:1.7;overflow-wrap:anywhere}.company-finance-evidence h3{margin:24px 0 10px}.company-finance-evidence table{width:100%;min-width:640px;border-collapse:collapse}.company-finance-evidence th,.company-finance-evidence td{padding:10px;text-align:left;border-bottom:1px solid #e2e5eb}.company-finance-evidence .right{text-align:right}.company-finance-evidence .fingerprint{font-size:11px;color:#657184}.company-finance-evidence .scroll{max-height:280px;overflow:auto}.company-finance-evidence .source-reference{max-width:240px;overflow-wrap:anywhere}.source-reference small{display:block;font-size:11px;margin:4px 0}.company-finance-evidence .num{font-variant-numeric:tabular-nums}
@media(max-width:760px){.company-finance-card{align-items:flex-start;flex-direction:column;gap:12px}.company-finance-card :deep(button){min-height:40px}.company-finance-evidence th,.company-finance-evidence td{padding:8px}}
</style>
