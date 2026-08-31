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

const grouped = computed(() => {
  const groups = []
  let currentPeriod = ''
  for (const task of visible.value) {
    if (task.period !== currentPeriod) {
      currentPeriod = task.period
      groups.push({ type: 'header', period: task.period, label: task.periodLabel })
    }
    groups.push({ type: 'task', task })
  }
  return groups
})
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
      <template v-for="item in grouped" :key="item.type === 'header' ? `h:${item.period}` : item.task.key">
        <div v-if="item.type === 'header'" class="task-period-header">
          <span>{{ item.label }}</span>
        </div>
        <button
          v-else
          type="button"
          class="task-row"
          :class="[item.task.status, { selected: item.task.key === selected }]"
          @click="$emit('select', item.task)"
        >
          <span class="task-status-dot" :class="item.task.status" />
          <span class="task-copy">
            <span class="task-title" :title="item.task.store">{{ item.task.store }}</span>
            <span class="task-reason">{{ item.task.reason }}</span>
          </span>
          <span class="task-badge" :class="item.task.status">{{ item.task.statusLabel }}</span>
        </button>
      </template>
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
  border-radius: 999px;
  padding: 5px 12px;
  color: var(--n7);
  background: var(--n0);
  font: 560 var(--t-xs)/1.4 var(--sans);
  white-space: nowrap;
  transition: all .12s;
}
.task-filters button:hover { background: var(--n1); border-color: var(--n4); }
.task-filters button.on { color: var(--accent); border-color: var(--accent); background: var(--accent-bg); }
.task-filters .num { margin-left: 3px; opacity: .7; }
.task-filters button.on .num { opacity: 1; }
.task-list { max-height: calc(100vh - 268px); overflow: auto; }

.task-period-header {
  position: sticky;
  top: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 18px 6px;
  background: var(--n1);
  border-bottom: 1px solid var(--n3);
  font: 620 var(--t-xs)/1.4 var(--sans);
  color: var(--n6);
  letter-spacing: .02em;
}
.task-period-header::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--n3);
}

.task-row {
  position: relative;
  display: grid;
  grid-template-columns: 10px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 56px;
  padding: 9px 14px 9px 18px;
  border: 0;
  border-bottom: 1px solid var(--n2);
  background: var(--n0);
  color: var(--n9);
  text-align: left;
  cursor: pointer;
  transition: background .1s;
}
.task-row:last-child { border-bottom: 0; }
.task-row:hover { background: var(--n1); }
.task-row.selected { background: #fffaf1; box-shadow: inset 3px 0 var(--warn); }
.task-row.selected.ready { background: var(--accent-bg); box-shadow: inset 3px 0 var(--accent); }
.task-row.selected.evidence { background: #f2faf6; box-shadow: inset 3px 0 var(--ok); }

.task-status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
  align-self: start;
  margin-top: 5px;
  background: var(--warn);
}
.task-status-dot.ready { background: var(--accent); }
.task-status-dot.evidence { background: var(--ok); }

.task-copy { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.task-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--t-sm); font-weight: 560; line-height: 1.4; }
.task-reason { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--n5); font-size: var(--t-xs); line-height: 1.4; }

.task-badge {
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  white-space: nowrap;
  flex-shrink: 0;
  background: var(--warn-bg);
  color: var(--warn);
}
.task-badge.ready { background: var(--accent-bg); color: var(--accent); }
.task-badge.evidence { background: var(--ok-bg); color: var(--ok); }

.task-loading { padding: 20px; }
.task-empty { min-height: 180px; display: grid; place-content: center; gap: 3px; padding: 24px; text-align: center; color: var(--n6); }
.task-empty b { color: var(--n8); }
.task-empty span { font-size: var(--t-sm); }
</style>
