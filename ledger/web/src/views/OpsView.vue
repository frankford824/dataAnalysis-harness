<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import { useRefresh } from '../refresh'
import { ago, stamp } from '../format'
import PageHead from '../components/PageHead.vue'
import { executionRows, health, notificationRows, probeRows } from '../opsView'

const status = ref(null), notifications = ref(null), events = ref(null)
const loading = ref(false), error = ref(''), checkedAt = ref('')

async function load() {
  if (loading.value) return
  loading.value = true
  try {
    const [s, n, e] = await Promise.all([api.sentinelStatus(), api.sentinelNotifications(), api.sentinelEvents()])
    status.value = s; notifications.value = n; events.value = e
    error.value = ''; checkedAt.value = new Date().toISOString()
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

const overall = computed(() => health(status.value))
const probes = computed(() => probeRows(status.value))
const executions = computed(() => executionRows(events.value))
const notices = computed(() => notificationRows(notifications.value))

onMounted(load)
useRefresh(load)
</script>

<template>
  <section class="ops-view">
    <PageHead title="运维状态" hint="只读展示哨兵检查结果。恢复、发布和回滚只由哨兵按固定动作执行，这里没有操作按钮。">
      <template #actions>
        <span v-if="checkedAt" class="small muted">{{ ago(checkedAt) }}刷新</span>
      </template>
    </PageHead>

    <n-alert v-if="error" type="error" :bordered="false" role="alert">{{ error }}</n-alert>

    <n-alert :type="overall.tone" :bordered="false" :title="overall.label" aria-label="运维总体状态">
      {{ overall.hint }}
    </n-alert>

    <h2>检查项</h2>
    <p v-if="!probes.length" class="small muted">还没有检查记录。</p>
    <n-table v-else size="small" :bordered="false" aria-label="哨兵检查项">
      <thead><tr><th>检查项</th><th>结果</th><th>原因</th><th>需要的处理</th><th>检查时间</th></tr></thead>
      <tbody>
        <tr v-for="row in probes" :key="row.id">
          <td class="mono">{{ row.id }}</td>
          <td :class="{ warn: row.status !== 'ok' }">{{ row.label }}</td>
          <td>{{ row.reason }}</td>
          <td>{{ row.action }}</td>
          <td :title="stamp(row.at)">{{ ago(row.at) }}</td>
        </tr>
      </tbody>
    </n-table>

    <h2>站内通知</h2>
    <p v-if="!notices.length" class="small muted">没有通知。</p>
    <ul v-else class="ops-list" aria-label="运维通知">
      <li v-for="row in notices" :key="row.id">
        <strong>{{ row.title }}</strong>
        <span v-if="row.reason"> · {{ row.reason }}</span>
        <span v-if="row.action" class="small"> · {{ row.action }}</span>
        <span class="small muted" :title="stamp(row.at)"> · {{ ago(row.at) }}</span>
      </li>
    </ul>

    <h2>发布与恢复记录</h2>
    <p v-if="!executions.length" class="small muted">没有发布或恢复记录。</p>
    <n-table v-else size="small" :bordered="false" aria-label="发布与恢复记录">
      <thead><tr><th>动作</th><th>请求</th><th>结果</th><th>说明</th><th>回执</th><th>时间</th></tr></thead>
      <tbody>
        <tr v-for="row in executions" :key="row.id">
          <td>{{ row.kind }}</td>
          <td class="mono">{{ row.scope }}</td>
          <td :class="{ warn: !['completed', 'ok'].includes(row.status) }">{{ row.status }}</td>
          <td>{{ row.reason }}<span v-if="row.action"> {{ row.action }}</span></td>
          <td class="mono" :title="row.receipt">{{ row.receipt.slice(0, 12) }}</td>
          <td :title="stamp(row.at)">{{ ago(row.at) }}</td>
        </tr>
      </tbody>
    </n-table>
  </section>
</template>

<style scoped>
.ops-view { display: flex; flex-direction: column; gap: 14px; }
.ops-view h2 { font-size: 15px; margin: 10px 0 0; }
.ops-list { margin: 0; padding-left: 18px; line-height: 1.8; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; overflow-wrap: anywhere; }
.warn { color: #c2410c; }
</style>
