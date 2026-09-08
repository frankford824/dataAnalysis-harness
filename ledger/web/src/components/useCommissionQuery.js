import { computed, onActivated, onDeactivated, onUnmounted, ref, watch } from 'vue'
import { useCommission } from '../commissionStore'
import { latestRequest } from './commissionRequest'

export function useCommissionQuery(section, getKey, fetcher, mayRefresh = () => true) {
  const state = useCommission(), data = ref(null), error = ref(''), loading = ref(false), loadedKey = ref('')
  const key = computed(getKey), request = latestRequest()
  let active = false, timer, generation = 0
  const stale = computed(() => !!data.value && key.value !== loadedKey.value)
  async function load() {
    if (!active || !state.ready || !mayRefresh()) { loading.value = false; return }
    const current = generation
    const wanted = key.value
    loading.value = true; error.value = ''
    try {
      const result = await request.run(signal => fetcher(signal))
      if (result && wanted === key.value) {
        data.value = result.value; loadedKey.value = wanted
        state.updated[section] = Date.now()
      }
    } catch (e) { if(current === generation) error.value = e.message }
    finally { if (current === generation) loading.value = false }
  }
  function schedule(delay = 180) {
    generation++; clearTimeout(timer); request.cancel()
    if (!active) return
    loading.value = true
    timer = setTimeout(load, delay)
  }
  watch(key, () => schedule())
  watch(() => state.ready, () => schedule(0))
  watch(() => state.refreshTick, () => { if (!loading.value && mayRefresh()) schedule(0) })
  watch(loading, value => { state.loading[section] = value })
  onActivated(() => { active = true; schedule(0) })
  function stop() { active = false; generation++; clearTimeout(timer); request.cancel(); loading.value = false }
  onDeactivated(stop); onUnmounted(stop)
  return { data, error, loading, stale, load: () => schedule(0) }
}
