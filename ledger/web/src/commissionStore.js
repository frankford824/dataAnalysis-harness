import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { useApp } from './store'

export const useCommission = defineStore('commission', () => {
  const app = useApp()
  const storeIds = ref([]), personIds = ref([]), people = ref([])
  const reportPeople = ref([])
  const start = ref(''), end = ref(''), ready = ref(false), refreshTick = ref(0)
  const settingsSearch = ref(''), settingsState = ref(''), reportView = ref('people')
  const updated = ref({}), loading = ref({}), initError = ref('')
  let bootstrap, rosterLoad
  const storeOptions = computed(() => app.stores.map(s => ({ value: s.id, label: s.name, group:app.platforms.find(p=>p.id===s.platform)?.name || s.platform || '' })))
  const personOptions = computed(() => {
    const options = new Map(people.value.map(p => [p.id, { value:p.id, label:p.name + (p.employee_no ? `（${p.employee_no}）` : '') }]))
    for(const p of reportPeople.value)if(!options.has(p.id))options.set(p.id,{value:p.id,label:p.name})
    return [...options.values()]
  })
  const scope = computed(() => ({ store_ids: [...storeIds.value].sort(), person_ids: [...personIds.value].sort() }))
  async function loadPeople() {
    if(rosterLoad)return rosterLoad
    rosterLoad=(async()=>{
      const response = await fetch('/api/commission-v2/people')
      if (!response.ok) throw new Error('人员名单加载失败，请重试')
      people.value = (await response.json()).people
    })()
    try { await rosterLoad } finally { rosterLoad=null }
  }
  async function init(query = {}) {
    if (ready.value) return
    if (bootstrap) return bootstrap
    bootstrap = (async () => {
      initError.value = ''
      try {
        await Promise.all([app.loadNavigation(), loadPeople()])
        const month = app.period || app.periods[0] || new Date().toLocaleDateString('sv-SE', { timeZone: 'Asia/Shanghai' }).slice(0, 7)
        storeIds.value = query.shops ? String(query.shops).split(',').filter(Boolean) : app.storeId ? [app.storeId] : []
        personIds.value = query.people ? String(query.people).split(',').filter(Boolean) : []
        start.value = /^\d{4}-\d{2}$/.test(query.from || '') ? query.from : month
        end.value = /^\d{4}-\d{2}$/.test(query.to || '') ? query.to : start.value
        if(['people','stores','breakdown','coverage'].includes(query.view))reportView.value=query.view
        ready.value = true
      } catch (error) { initError.value = error.message }
      finally { bootstrap = null }
    })()
    return bootstrap
  }
  function clear() { storeIds.value = []; personIds.value = [] }
  function refresh() { refreshTick.value++;loadPeople().catch(()=>{}) }
  async function changed() { await loadPeople(); refresh() }
  return { storeIds, personIds, people, reportPeople, start, end, ready, refreshTick, settingsSearch, settingsState,
    reportView, updated, loading, initError, storeOptions, personOptions, scope, init, loadPeople, clear, refresh, changed }
})
