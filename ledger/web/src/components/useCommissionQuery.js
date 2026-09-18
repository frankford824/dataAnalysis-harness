import { computed, onActivated, onDeactivated, onUnmounted, ref, watch } from 'vue'
import { useCommission } from '../commissionStore'
import { latestRequest } from './commissionRequest'

export function useCommissionQuery(section, getKey, fetcher, mayRefresh = () => true, { followTick = true, delay = 180, remember = false } = {}) {
  const state = useCommission(), data = ref(null), error = ref(''), loading = ref(false), loadedKey = ref('')
  const key = computed(getKey), request = latestRequest()
  const remembered = new Map()
  const prefetches = new Map()
  let cacheEpoch = 0
  function rememberValue(key, value) {
    remembered.delete(key); remembered.set(key, value)
    while(remembered.size>12)remembered.delete(remembered.keys().next().value)
  }
  let active = false, timer, generation = 0
  const stale = computed(() => !!data.value && key.value !== loadedKey.value)
  async function load() {
    if (!active || !state.ready || !mayRefresh()) { loading.value = false; return }
    const current = generation
    const wanted = key.value
    if (remember && remembered.has(wanted)) {
      data.value = remembered.get(wanted)
      loadedKey.value = wanted
      loading.value = false
    } else {
      loading.value = true
    }
    error.value = ''
    try {
      const result = await request.run(signal => prefetches.get(wanted)?.promise || fetcher(signal))
      if (result && wanted === key.value) {
        data.value = result.value; loadedKey.value = wanted
        if (remember) rememberValue(wanted, result.value)
        state.updated[section] = Date.now()
      }
    } catch (e) { if(current === generation) error.value = e.message }
    finally { if (current === generation) loading.value = false }
  }
  function schedule(wait = delay) {
    generation++; clearTimeout(timer); request.cancel()
    if (!active) return
    if (!(remember && remembered.has(key.value))) loading.value = true
    timer = setTimeout(load, wait)
  }
  function prefetch(nextKey, nextFetcher) {
    if (!active || !remember || !nextKey || remembered.has(nextKey) || prefetches.has(nextKey) || prefetches.size>=1) return
    const controller=new AbortController(), epoch=cacheEpoch
    const promise=nextFetcher(controller.signal).then(value=>{
      if(value&&epoch===cacheEpoch&&!controller.signal.aborted)rememberValue(nextKey,value)
      return value
    }).finally(()=>{if(prefetches.get(nextKey)?.controller===controller)prefetches.delete(nextKey)})
    promise.catch(()=>{})
    prefetches.set(nextKey,{promise,controller})
  }
  function stopPrefetch() { cacheEpoch++;for(const item of prefetches.values())item.controller.abort();prefetches.clear() }
  function forget() { remembered.clear();stopPrefetch() }
  watch(key, () => schedule())
  watch(() => state.ready, () => schedule(0))
  if (followTick) {
    watch(() => state.refreshTick, () => { if (!loading.value && mayRefresh()) { forget(); schedule(0) } })
  }
  watch(loading, value => { state.loading[section] = value })
  onActivated(() => { active = true; schedule(0) })
  function stop() { active = false; generation++; clearTimeout(timer); request.cancel();stopPrefetch(); loading.value = false }
  onDeactivated(stop); onUnmounted(stop)
  return { data, error, loading, stale, load: () => schedule(0), prefetch, forget }
}
