<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  tasks: { type: Array, default: () => [] },
  selected: { type: String, default: '' },
  loading: { type: Boolean, default: false },
})

defineEmits(['select'])

const filter = ref('all')
const filters = computed(() => [
  { id: 'all', label: '全部', count: props.tasks.length },
  { id: 'pending', label: '待补证据', count: props.tasks.filter((task) => task.status === 'pending').length },
  { id: 'ready', label: '可确认', count: props.tasks.filter((task) => task.status === 'ready').length },
  { id: 'evidence', label: '有新证据', count: props.tasks.filter((task) => task.status === 'evidence').length },
])
const visible = computed(() => (
  filter.value === 'all' ? props.tasks : props.tasks.filter((task) => task.status === filter.value)
))
</script>

<template>
  <section class="task-queue" aria-label="待处理店期">
    <header class="task-queue-head">
      <div>
        <h2>今天需要处理</h2>
        <p>全公司待处理 {{ tasks.length }} 个店期</p>
      </div>
    </header>

    <div class="task-filters" role="tablist" aria-label="筛选待处理状态">
      <button
        v-for="item in filters"
        :key="item.id"
        type="button"
        :class="{ on: filter === item.id }"
        @click="filter = item.id"
      >
        {{ item.label }} <span class="num">{{ item.count }}</span>
      </button>
    </div>

    <div v-if="loading && !tasks.length" class="task-loading">
      <n-skeleton text :repeat="8" />
    </div>
    <div v-else-if="visible.length" class="task-list">
      <button
        v-for="(task, index) in visible"
        :key="task.key"
        type="button"
        class="task-row"
        :class="[task.status, { selected: task.key === selected }]"
        @click="$emit('select', task)"
      >
        <span class="task-rank num">{{ index + 1 }}</span>
        <span class="task-copy">
          <span class="task-title" :title="`${task.store} · ${task.periodLabel}`">
            {{ task.store }} · {{ task.periodLabel }}
          </span>
          <span class="task-reason">{{ task.reason }}</span>
        </span>
        <span class="task-status">{{ task.statusLabel }}</span>
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="m9 5 7 7-7 7" />
        </svg>
      </button>
    </div>
    <div v-else class="task-empty">
      <b>这一组暂时没有待办</b>
      <span>切换上面的状态，或者从右侧查找其他店铺。</span>
    </div>
  </section>
</template>

<style scoped>
.task-queue {
  min-width: 0;
  background: var(--n0);
  border: 1px solid var(--n3);
  border-radius: var(--r-lg);
  overflow: hidden;
}
.task-queue-head { padding: 20px 20px 12px; }
.task-queue-head h2 { font-size: var(--t-xl); letter-spacing: -.02em; }
.task-queue-head p { margin-top: 2px; color: var(--n6); font-size: var(--t-sm); }
.task-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 0 20px 14px;
  border-bottom: 1px solid var(--n3);
}
.task-filters button {
  border: 1px solid var(--n3);
  border-radius: var(--r-sm);
  padding: 5px 10px;
  color: var(--n7);
  background: var(--n0);
  font: 560 var(--t-xs)/1.4 var(--sans);
  white-space: nowrap;
}
.task-filters button.on { color: var(--accent); border-color: #cbd8ff; background: var(--accent-bg); }
.task-filters .num { margin-left: 3px; }
.task-list { max-height: calc(100vh - 268px); overflow: auto; }
.task-row {
  position: relative;
  display: grid;
  grid-template-columns: 24px minmax(0, 1fr) auto 16px;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 68px;
  padding: 11px 14px 11px 18px;
  border: 0;
  border-bottom: 1px solid var(--n3);
  background: var(--n0);
  color: var(--n9);
  text-align: left;
  cursor: pointer;
}
.task-row:last-child { border-bottom: 0; }
.task-row:hover { background: var(--n1); }
.task-row.selected { background: #fffaf1; box-shadow: inset 3px 0 var(--warn); }
.task-row.selected.ready { background: var(--accent-bg); box-shadow: inset 3px 0 var(--accent); }
.task-row.selected.evidence { background: #f2faf6; box-shadow: inset 3px 0 var(--ok); }
.task-rank { align-self: start; padding-top: 1px; color: var(--n5); font-size: var(--t-sm); }
.task-copy { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.task-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--t-md); font-weight: 620; }
.task-reason { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--n6); font-size: var(--t-xs); }
.task-status { color: var(--warn); font-size: var(--t-xs); font-weight: 620; white-space: nowrap; }
.task-row.ready .task-status { color: var(--accent); }
.task-row.evidence .task-status { color: var(--ok); }
.task-row svg { width: 15px; height: 15px; fill: none; stroke: var(--n5); stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.task-loading { padding: 20px; }
.task-empty { min-height: 180px; display: grid; place-content: center; gap: 3px; padding: 24px; text-align: center; color: var(--n6); }
.task-empty b { color: var(--n8); }
.task-empty span { font-size: var(--t-sm); }
</style>
