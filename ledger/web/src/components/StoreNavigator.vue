<script setup>
import { computed, h, ref, watch } from 'vue'

const props = defineProps({
  platforms: { type: Array, default: () => [] },
  stores: { type: Array, default: () => [] },
  activePlatform: { type: String, default: '' },
  selectedStore: { type: String, default: '' },
})

const emit = defineEmits(['select-platform', 'select-store'])
const query = ref('')
const expandedKeys = ref([])

const platformKey = (id) => `platform:${id}`

const menuOptions = computed(() => {
  const word = query.value.trim().toLocaleLowerCase()
  return props.platforms.flatMap((platform) => {
    const all = props.stores.filter((store) => store.platform === platform.id)
    if (!all.length) return []
    const platformMatches = platform.name.toLocaleLowerCase().includes(word)
    const stores = !word || platformMatches
      ? all
      : all.filter((store) => store.name.toLocaleLowerCase().includes(word))
    if (!stores.length) return []
    return [{
      key: platformKey(platform.id),
      label: platform.name,
      kind: 'platform',
      platform,
      count: all.length,
      children: stores.map((store) => ({
        key: store.id,
        label: store.name,
        kind: 'store',
        store,
      })),
    }]
  })
})

const shownExpandedKeys = computed(() =>
  query.value.trim() ? menuOptions.value.map((option) => option.key) : expandedKeys.value,
)

const selected = computed(() => props.stores.find((store) => store.id === props.selectedStore))
const selectedPlatform = computed(() =>
  props.platforms.find((platform) => platform.id === selected.value?.platform),
)

watch(
  () => props.activePlatform,
  (id) => {
    if (id && !query.value.trim()) expandedKeys.value = [platformKey(id)]
  },
  { immediate: true },
)

function updateExpanded(keys) {
  if (query.value.trim()) return
  const platformKeys = keys.filter((key) => String(key).startsWith('platform:'))
  const latest = platformKeys.find((key) => !expandedKeys.value.includes(key)) || platformKeys.at(-1)
  expandedKeys.value = latest ? [latest] : []
  if (latest) emit('select-platform', String(latest).slice('platform:'.length))
}

function select(key, option) {
  if (option.kind === 'store') emit('select-store', option.store)
}

function renderLabel(option) {
  if (option.kind === 'platform') {
    return h('span', { class: 'platform-menu-label' }, option.label)
  }
  const state = option.store.latest_state || 'none'
  return h('span', { class: 'store-menu-label', title: option.store.name }, [
    h('span', { class: ['store-state-dot', `is-${state}`] }),
    h('span', { class: 'store-menu-name' }, option.store.name),
  ])
}

function renderExtra(option) {
  if (option.kind === 'platform') {
    return h('span', { class: 'platform-menu-count num' }, option.count)
  }
  if (!option.store.file_count) return null
  return h('span', { class: 'store-menu-count num' }, `${option.store.file_count} 张`)
}
</script>

<template>
  <aside class="store-navigator" aria-label="店铺导航">
    <div class="navigator-head">
      <div>
        <h2>店铺导航</h2>
        <div class="xs muted">{{ stores.length }} 家店 · {{ menuOptions.length }} 个平台</div>
      </div>
    </div>

    <div class="navigator-search">
      <n-input v-model:value="query" size="small" clearable placeholder="搜索平台或店铺">
        <template #prefix>
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="11" cy="11" r="6.5" />
            <path d="m16 16 4 4" />
          </svg>
        </template>
      </n-input>
    </div>

    <n-scrollbar class="navigator-scroll">
      <n-menu
        v-if="menuOptions.length"
        :value="selectedStore"
        :expanded-keys="shownExpandedKeys"
        :options="menuOptions"
        :indent="18"
        :root-indent="12"
        :render-label="renderLabel"
        :render-extra="renderExtra"
        accordion
        @update:value="select"
        @update:expanded-keys="updateExpanded"
      />
      <n-empty v-else size="small" description="没有匹配的店铺" />
    </n-scrollbar>

    <div class="navigator-foot">
      <span class="navigator-foot-label">当前</span>
      <span class="navigator-current" :title="selected?.name || ''">
        {{ selectedPlatform?.name || '未选平台' }}
        <template v-if="selected"> / {{ selected.name }}</template>
      </span>
    </div>
  </aside>
</template>

<style scoped>
.store-navigator {
  min-width: 0;
  min-height: 560px;
  max-height: calc(100vh - 92px);
  display: flex;
  flex-direction: column;
  background: #fbfcfe;
  border-right: 1px solid var(--n3);
}
.navigator-head {
  min-height: 68px;
  padding: 16px 18px 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.navigator-head h2 { margin: 0 0 2px; font-size: var(--t-md); font-weight: 660; }
.navigator-search { padding: 0 14px 12px; }
.navigator-search svg {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
}
.navigator-scroll {
  flex: 1 1 auto;
  min-height: 280px;
  border-top: 1px solid var(--n2);
  border-bottom: 1px solid var(--n2);
}
:deep(.n-menu) { padding: 8px 9px 12px; }
:deep(.n-menu-item) { margin: 1px 0; }
:deep(.n-menu-item-content) { border-radius: 7px; }
:deep(.n-menu-item-content::before) { left: 0; right: 0; border-radius: 7px; }
:deep(.n-menu-item-content--selected::before) {
  background: #edf3ff !important;
  box-shadow: inset 3px 0 0 var(--accent);
}
:deep(.n-menu-item-content--selected .store-menu-name) { color: var(--n9); font-weight: 650; }
:deep(.n-menu-item-content-header) { min-width: 0; }
:deep(.platform-menu-label) { font-size: var(--t-sm); font-weight: 620; color: var(--n8); }
:deep(.platform-menu-count),
:deep(.store-menu-count) {
  color: var(--n5);
  font-size: 11px;
  white-space: nowrap;
}
:deep(.platform-menu-count) {
  min-width: 22px;
  text-align: center;
  padding: 1px 6px;
  border: 1px solid var(--n3);
  border-radius: 999px;
  background: var(--n0);
}
:deep(.store-menu-label) { display: flex; align-items: center; gap: 9px; min-width: 0; }
:deep(.store-menu-name) { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--t-sm); }
:deep(.store-state-dot) {
  width: 6px;
  height: 6px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--n4);
}
:deep(.store-state-dot.is-closed) { background: var(--ok); }
:deep(.store-state-dot.is-open) { background: var(--warn); }
.navigator-foot {
  min-height: 46px;
  padding: 0 16px;
  display: flex;
  align-items: center;
  gap: 9px;
  font-size: var(--t-xs);
}
.navigator-foot-label { color: var(--n5); }
.navigator-current {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--n7);
}
@media (max-width: 900px) {
  .store-navigator { min-height: 420px; max-height: 520px; border-right: 0; border-bottom: 1px solid var(--n3); }
}
</style>
