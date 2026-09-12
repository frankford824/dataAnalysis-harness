<script setup>
import { ref, computed, watch, onUnmounted } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { useMessage } from 'naive-ui'
import { commissionRequest, latestRequest } from '../components/commissionRequest'
import { money } from '../format'
const message=useMessage(), request=latestRequest()
const period=ref(new Date().toLocaleDateString('sv-SE',{timeZone:'Asia/Shanghai'}).slice(0,7))
const data=ref(null), name=ref('兼职人工费用'), amount=ref(null), loading=ref(false), saving=ref(false), error=ref('')
const dirty=computed(()=>data.value && (name.value!==data.value.name || amount.value!==data.value.amount))
const disabled=computed(()=>loading.value||saving.value||data.value?.period!==period.value)
function reset(){name.value=data.value?.name||'兼职人工费用';amount.value=data.value?.amount??null}
async function load(){loading.value=true;error.value='';try{const result=await request.run(signal=>commissionRequest(`/labor?period=${period.value}`,{signal}));if(result){data.value=result.value;reset()}}catch(e){error.value=e.message}finally{loading.value=false}}
async function save(){if(saving.value)return;if(Math.abs(amount.value*100-Math.round(amount.value*100))>0.00001){message.error('金额最多保留两位小数');return}saving.value=true;error.value='';try{data.value=await commissionRequest('/labor',{body:{period:period.value,name:name.value,amount:amount.value,revision:data.value.revision}});reset();message.success('已保存')}catch(e){error.value=e.message}finally{saving.value=false}}
watch(period,load,{immediate:true})
onUnmounted(()=>request.cancel())
onBeforeRouteLeave(()=>!dirty.value||window.confirm('修改尚未保存，确定离开吗？'))
</script>
<template>
  <section class="labor-page">
    <header><div><h1>兼职人工费用</h1><p>按各店当月销售收入占比分摊，金额合计与录入总额一致。</p></div><label>月份 <input v-model="period" type="month" :disabled="dirty||loading||saving" aria-label="费用月份" /></label></header>
    <n-alert v-if="error" type="error" role="alert">{{error}} <n-button v-if="!dirty" text @click="load">重试</n-button></n-alert>
    <n-alert v-if="data?.closed_stores" type="info">本月有 {{data.closed_stores}} 家店已结账，费用仍可录入。此处展示公摊结果，已结账记录保持不变。</n-alert>
    <n-alert v-if="data?.incomplete" type="warning">部分店铺销售收入尚未确定，当前分摊结果仅供核对。</n-alert>
    <n-alert v-if="data?.amount!=null && !data?.settled" type="info">该月暂无可分摊的销售收入，总额已保存，暂未分摊。</n-alert>
    <div class="labor-editor"><label>费项科目<n-input v-model:value="name" :disabled="disabled" maxlength="80" aria-label="费项科目" /></label><label>本月总金额（元）<n-input-number v-model:value="amount" :disabled="disabled" :min="0" :max="999999999999.99" :update-value-on-input="true" :show-button="false" placeholder="请输入金额" aria-label="本月总金额" /></label><div class="labor-actions"><n-button :disabled="!dirty||saving" @click="reset">取消修改</n-button><n-button type="primary" :loading="saving" :disabled="disabled||!dirty||amount===null||!name.trim()" @click="save">保存</n-button></div></div>
    <div class="labor-summary"><span>参与分摊的销售收入 <strong>{{data?'¥'+money(data.basis_total):'—'}}</strong></span><span>兼职人工费用 <strong>{{data?.amount==null?'未填写':'¥'+money(data.amount)}}</strong></span></div>
    <n-spin :show="loading"><div class="labor-table"><table><thead><tr><th>店铺</th><th>销售收入</th><th>销售占比</th><th>分摊金额</th></tr></thead><tbody><tr v-for="row in data?.rows||[]" :key="row.store_id"><td>{{row.store}}</td><td>{{row.sales==null?'待核对':money(row.sales)}}</td><td>{{row.share==null?'—':(row.share*100).toFixed(2)+'%'}}</td><td>{{row.amount==null?'—':money(row.amount)}}</td></tr><tr v-if="!loading&&!data?.rows?.length"><td colspan="4" class="labor-empty">该月暂无店铺销售数据</td></tr></tbody></table></div></n-spin>
  </section>
</template>
<style scoped>
.labor-page{max-width:1200px;padding:28px;margin:auto}.labor-page header{display:flex;justify-content:space-between;gap:24px;align-items:center;margin-bottom:24px}.labor-page h1{font-size:23px;margin:0 0 8px}.labor-page p{color:#718096;margin:0}.labor-page label{display:grid;gap:8px;font-size:13px}.labor-page input[type=month]{padding:8px;border:1px solid #dce2eb;border-radius:6px;background:white}.labor-editor{display:grid;grid-template-columns:1fr 1fr auto;gap:20px;align-items:end;padding:24px;background:white;border:1px solid #e5eaf1;border-radius:8px;margin:20px 0}.labor-actions{display:flex;gap:8px}.labor-summary{display:flex;gap:36px;padding:10px 0 24px;color:#64748b}.labor-summary strong{color:#172b4d;margin-left:10px;font-variant-numeric:tabular-nums}.labor-table{overflow:auto;border:1px solid #e5eaf1;border-radius:8px;background:white}.labor-table table{width:100%;border-collapse:collapse}.labor-table th,.labor-table td{padding:14px 18px;text-align:right;border-bottom:1px solid #edf1f6;font-variant-numeric:tabular-nums}.labor-table th{background:#f6f8fc;font-weight:500}.labor-table th:first-child,.labor-table td:first-child{text-align:left}.labor-empty{text-align:center!important;color:#718096;padding:40px!important}.n-alert{margin-bottom:12px}@media(max-width:700px){.labor-page{padding:16px}.labor-page header{align-items:start;flex-direction:column}.labor-editor{grid-template-columns:1fr;padding:16px}.labor-summary{flex-direction:column;gap:12px}.labor-actions{justify-content:flex-end}}
</style>
