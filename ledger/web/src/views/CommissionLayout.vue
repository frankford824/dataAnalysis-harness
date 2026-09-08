<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useCommission } from '../commissionStore'
import '../commission.css'
import LedgerMultiSelect from '../components/ui/LedgerMultiSelect.vue'
import { Search, RefreshCw, X } from '@lucide/vue'
const state = useCommission(), route = useRoute(), router = useRouter()
const page=ref(null),allFilters=ref(false)
const applied=computed(()=>[...state.storeIds.map(id=>({type:'store',id,name:state.storeOptions.find(o=>o.value===id)?.label||'未找到的店铺'})),...state.personIds.map(id=>({type:'person',id,name:state.personOptions.find(o=>o.value===id)?.label||'未找到的人员'}))])
const visibleFilters=computed(()=>allFilters.value?applied.value:applied.value.slice(0,6))
function removeFilter(filter){const key=filter.type==='store'?'storeIds':'personIds';state[key]=state[key].filter(id=>id!==filter.id)}
const section = computed(() => route.name === 'commission-reports' ? 'reports' : 'settings')
const stamp = computed(() => state.updated[section.value] ? new Date(state.updated[section.value]).toLocaleTimeString('zh-CN', { hour12: false, timeZone:'Asia/Shanghai' }) : '')
let poll, alive=true
const localQueries=new Set()
const queryKey=query=>JSON.stringify(['shops','people','from','to','view'].map(key=>String(query[key]||'')))
function refresh() { if (document.visibilityState === 'visible') state.refresh() }
onMounted(() => {
  state.init(route.query).then(()=>{if(alive){applyQuery(route.query);syncQuery()}})
  poll = setInterval(refresh, 20000)
  window.addEventListener('focus', refresh)
  document.addEventListener('visibilitychange', refresh)
})
onUnmounted(() => { alive=false;clearInterval(poll); window.removeEventListener('focus', refresh); document.removeEventListener('visibilitychange', refresh) })
function applyQuery(query) {
  if (!state.ready || !route.meta.commission) return
  for (const [param,field] of [['shops','storeIds'],['people','personIds']]) {
    if (param in query) {
      const ids = String(query[param] || '').split(',').filter(Boolean)
      if (ids.join(',') !== state[field].join(',')) state[field] = ids
    }
  }
  if (/^\d{4}-\d{2}$/.test(query.from || '')) state.start = query.from
  if (/^\d{4}-\d{2}$/.test(query.to || '')) state.end = query.to
  if(['people','stores','breakdown','coverage'].includes(query.view))state.reportView=query.view
}
watch(() => route.query, query => {if(!localQueries.has(queryKey(query)))applyQuery(query)})
function syncQuery() {
  if (!state.ready || !route.meta.commission) return
  const query = { shops: state.storeIds.join(','), people: state.personIds.join(','), from: state.start, to: state.end, view:state.reportView }
  if (Object.entries(query).some(([k,v]) => String(route.query[k] || '') !== v)) {
    const key=queryKey(query);localQueries.add(key)
    router.replace({query}).finally(()=>setTimeout(()=>localQueries.delete(key),0))
  }
}
watch(() => [state.storeIds, state.personIds, state.start, state.end, state.reportView], syncQuery, { deep: true })
</script>
<template>
  <section class="commission-area workflow-area">
    <header class="commission-heading"><h1>{{section==='settings'?'提成设置':'金额汇总'}}</h1><div class="workflow-heading-actions"><span class="workflow-updated" aria-live="polite">{{state.loading[section]?'正在更新…':stamp?`更新于 ${stamp}`:''}}</span><template v-if="section==='settings'"><n-button :disabled="!page||!state.ready||page.busy" @click="page?.menu('import')">导入表格</n-button><n-button type="primary" :disabled="!page||!state.ready||page.busy" @click="page?.edit()">新增设置</n-button></template></div></header>
    <nav class="commission-mobile-nav" aria-label="提成菜单"><router-link :to="{name:'commission',query:route.query}">提成设置</router-link><router-link :to="{name:'commission-reports',query:route.query}">金额汇总</router-link></nav>
    <div class="workflow-scope">
      <div class="workflow-scope-controls">
        <n-input v-if="section==='settings'" v-model:value="state.settingsSearch" class="workflow-product-search" placeholder="搜索商品或宝贝ID" aria-label="搜索商品" clearable><template #prefix><Search :size="15"/></template></n-input>
        <LedgerMultiSelect v-model="state.storeIds" :options="state.storeOptions" label="家店铺" aria-label="筛选店铺" :disabled="!state.ready" :show-chips="false"/>
        <LedgerMultiSelect v-model="state.personIds" :options="state.personOptions" label="位人员" aria-label="筛选人员" :disabled="!state.ready" :show-chips="false"/>
        <n-button text class="workflow-refresh" :disabled="state.loading[section]||!state.ready" @click="state.refresh"><RefreshCw :size="14" style="margin-right:6px"/>刷新</n-button>
      </div>
      <div v-if="applied.length" class="workflow-applied" aria-label="当前筛选条件"><button v-for="filter in visibleFilters" :key="filter.type+filter.id" class="workflow-active-chip" :title="filter.name" :aria-label="`移除${filter.type==='store'?'店铺':'人员'}${filter.name}`" @click="removeFilter(filter)"><span>{{filter.name}}</span><X :size="12"/></button><button v-if="applied.length>6" class="text-button" @click="allFilters=!allFilters">{{allFilters?'收起':`展开其余 ${applied.length-6} 项`}}</button><button class="text-button" @click="state.clear">清空筛选</button></div>
    </div>
    <div v-if="state.initError" class="commission-error" role="alert">{{state.initError}} <button class="text-button" @click="state.init(route.query)">重试</button></div>
    <router-view v-slot="{Component}"><keep-alive><component :is="Component" ref="page" /></keep-alive></router-view>
  </section>
</template>
