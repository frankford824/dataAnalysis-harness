<script setup>
import { computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useCommission } from '../commissionStore'
import '../commission.css'
const state = useCommission(), route = useRoute(), router = useRouter()
const section = computed(() => route.name === 'commission-reports' ? 'reports' : 'settings')
const stamp = computed(() => state.updated[section.value] ? new Date(state.updated[section.value]).toLocaleTimeString('zh-CN', { hour12: false, timeZone:'Asia/Shanghai' }) : '')
let poll
function refresh() { if (document.visibilityState === 'visible') state.refresh() }
onMounted(() => {
  state.init(route.query)
  poll = setInterval(refresh, 20000)
  window.addEventListener('focus', refresh)
  document.addEventListener('visibilitychange', refresh)
})
onUnmounted(() => { clearInterval(poll); window.removeEventListener('focus', refresh); document.removeEventListener('visibilitychange', refresh) })
watch(() => route.query, query => {
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
})
watch(() => [state.storeIds, state.personIds, state.start, state.end, state.reportView], () => {
  if (!state.ready || !route.meta.commission) return
  const query = { shops: state.storeIds.join(','), people: state.personIds.join(','), from: state.start, to: state.end, view:state.reportView }
  if (Object.entries(query).some(([k,v]) => String(route.query[k] || '') !== v)) router.replace({ query })
}, { deep: true })
</script>
<template>
  <section class="commission-area">
    <header class="commission-heading"><h1>{{ section === 'settings' ? '提成设置' : '金额汇总' }}</h1><div class="commission-updated" aria-live="polite"><span>{{ state.loading[section] ? '正在更新…' : stamp ? `更新于 ${stamp}` : '' }}</span><button class="refresh-button" type="button" aria-label="刷新列表" :disabled="state.loading[section] || !state.ready" @click="state.refresh"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5M5.2 8a7 7 0 0 1 11.6-3L20 8M4 16l3.2 3A7 7 0 0 0 18.8 16"/></svg></button></div></header>
    <nav class="commission-mobile-nav" aria-label="提成菜单"><router-link :to="{name:'commission',query:route.query}">提成设置</router-link><router-link :to="{name:'commission-reports',query:route.query}">金额汇总</router-link></nav>
    <div class="commission-filters">
      <label>店铺<n-select v-model:value="state.storeIds" :options="state.storeOptions" multiple filterable clearable max-tag-count="responsive" placeholder="全部店铺" aria-label="筛选店铺" :disabled="!state.ready" /></label>
      <label>人员<n-select v-model:value="state.personIds" :options="state.personOptions" multiple filterable clearable max-tag-count="responsive" placeholder="全部人员" aria-label="筛选人员" :disabled="!state.ready" /></label>
      <button class="text-button clear-filters" :disabled="!state.storeIds.length && !state.personIds.length" @click="state.clear">清空</button>
    </div>
    <div v-if="state.initError" class="commission-error" role="alert">{{ state.initError }} <button class="text-button" @click="state.init(route.query)">重试</button></div>
    <router-view v-slot="{Component}"><keep-alive><component :is="Component" /></keep-alive></router-view>
  </section>
</template>
