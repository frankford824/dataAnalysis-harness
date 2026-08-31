<script setup>
import { computed, ref, watch } from 'vue'

const props = defineProps({
  platforms: { type: Array, default: () => [] },
  stores: { type: Array, default: () => [] },
  selected: { type: String, default: '' },
})

defineEmits(['select', 'register'])

const query = ref('')
const platform = ref('')

watch(
  () => props.stores.find((store) => store.id === props.selected)?.platform,
  (value) => {
    if (value) platform.value = value
  },
  { immediate: true },
)

const platformRows = computed(() => props.platforms.map((item) => ({
  ...item,
  count: props.stores.filter((store) => store.platform === item.id).length,
})).filter((item) => item.count))

const results = computed(() => {
  const word = query.value.trim().toLocaleLowerCase()
  const list = props.stores.filter((store) => (
    word
      ? store.name.toLocaleLowerCase().includes(word)
        || (props.platforms.find((item) => item.id === store.platform)?.name || '')
          .toLocaleLowerCase().includes(word)
      : !platform.value || store.platform === platform.value
  ))
  return list.slice(0, word ? 40 : 12)
})

function dot(store) {
  if (store.latest_state === 'closed') return 'closed'
  if (store.latest_state === 'open') return 'open'
  return 'none'
}
</script>

<template>
  <aside class="store-finder" aria-label="查找其他店铺">
    <h3>查找其他店铺</h3>
    <n-input v-model:value="query" clearable placeholder="输入店铺名称">
      <template #prefix>
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="11" cy="11" r="6.5" />
          <path d="m16 16 4 4" />
        </svg>
      </template>
    </n-input>

    <div v-if="!query.trim()" class="platform-tags">
      <button
        v-for="item in platformRows"
        :key="item.id"
        type="button"
        :class="{ on: platform === item.id }"
        @click="platform = platform === item.id ? '' : item.id"
      >
        {{ item.name }} <span class="num">{{ item.count }}</span>
      </button>
    </div>

    <div class="store-finder-results">
      <button
        v-for="store in results"
        :key="store.id"
        type="button"
        :class="{ selected: store.id === selected }"
        :title="store.name"
        @click="$emit('select', store)"
      >
        <i :class="dot(store)" />
        <span>{{ store.name }}</span>
        <svg v-if="store.id === selected" viewBox="0 0 24 24" aria-hidden="true">
          <path d="m5 12 4 4L19 6" />
        </svg>
      </button>
    </div>

    <footer>
      <button type="button" class="link" @click="$emit('register')">登记新店</button>
      <span>店铺配置</span>
    </footer>
  </aside>
</template>

<style scoped>
.store-finder {
  min-width: 0;
  padding: 16px 14px 12px;
  background: var(--n0);
  border: 1px solid var(--n3);
  border-radius: var(--r-lg);
}
.store-finder h3 { margin-bottom: 10px; font-size: var(--t-sm); font-weight: 620; color: var(--n7); }
.store-finder :deep(.n-input__prefix) { margin-right: 6px; }
.store-finder :deep(.n-input__prefix) svg { width: 15px; height: 15px; fill: none; stroke: currentColor; stroke-width: 1.8; }

.platform-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin: 10px 0;
}
.platform-tags button {
  border: 1px solid var(--n3);
  border-radius: 999px;
  padding: 3px 10px;
  background: var(--n0);
  color: var(--n7);
  font: 560 11.5px/1.5 var(--sans);
  white-space: nowrap;
  transition: all .12s;
}
.platform-tags button:hover { background: var(--n1); border-color: var(--n4); }
.platform-tags button.on { color: var(--accent); border-color: var(--accent); background: var(--accent-bg); }
.platform-tags .num { margin-left: 2px; opacity: .65; }
.platform-tags button.on .num { opacity: 1; }

.store-finder-results { max-height: 320px; overflow: auto; margin-top: 4px; }
.store-finder-results i { width: 7px; height: 7px; border-radius: 50%; background: var(--n4); flex-shrink: 0; }
.store-finder-results i.open { background: var(--warn); }
.store-finder-results i.closed { background: var(--ok); }
.store-finder-results i.none { background: var(--n0); border: 1.5px solid var(--n4); }
.store-finder-results button {
  display: grid;
  grid-template-columns: 8px minmax(0, 1fr) 16px;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 7px 8px;
  border: 0;
  border-radius: var(--r-sm);
  background: none;
  color: var(--n7);
  font: var(--t-xs)/1.4 var(--sans);
  text-align: left;
  transition: background .1s;
}
.store-finder-results button:hover { background: var(--n1); }
.store-finder-results button.selected { color: var(--accent); background: var(--accent-bg); font-weight: 620; }
.store-finder-results button span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.store-finder-results svg { width: 15px; fill: none; stroke: currentColor; stroke-width: 2; }
.store-finder footer { display: flex; justify-content: space-between; margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--n3); color: var(--n5); font-size: var(--t-xs); }
</style>
