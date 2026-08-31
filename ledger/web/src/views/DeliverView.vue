<script setup>
import { useDialog, useMessage } from 'naive-ui'
import { computed, defineAsyncComponent, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { api } from '../api'
import StoreFinder from '../components/StoreFinder.vue'
import SystemStatusLine from '../components/SystemStatusLine.vue'
import TaskQueue from '../components/TaskQueue.vue'
import { ago, bytes, count } from '../format'
import { useApp } from '../store'

const UploadPanel = defineAsyncComponent(() => import('../components/UploadPanel.vue'))
const FilePreviewPanel = defineAsyncComponent(() => import('../components/FilePreviewPanel.vue'))

const app = useApp()
const message = useMessage()
const dialog = useDialog()
const router = useRouter()

const LIVE_START = '2026-06'
const picked = app.noted('deliver.store', '')
const selectedTaskKey = app.noted('deliver.task', '')

const loadingStore = ref('')
const loadError = ref('')
const periodSnap = ref(null)
const periodLoading = ref(false)
const periodError = ref('')
let periodRequest = 0

const adding = ref(false)
const explaining = ref(false)
const draft = ref({ name: '', platform: '' })
const indexFiles = ref([])
const indexStorage = ref(null)
const indexErrors = ref([])
const indexLoading = ref(false)
const orderFeed = ref(null)
const systemOpen = ref(false)
const previewing = ref(false)
const previewTarget = ref(null)
const managing = ref(null)
const missingSource = ref(null)

const here = computed(() => app.stores.find((store) => store.id === picked.value) || null)
const currentDetail = computed(() => (
  here.value ? app.storeDetails[here.value.id]?.data || null : null
))
const platformName = (id) => app.platforms.find((platform) => platform.id === id)?.name || id

const indexByFile = computed(() => {
  const map = new Map()
  for (const file of indexFiles.value) {
    map.set(`${file.store_id}:${file.path.split(/[\\/]/).pop()}`, file)
  }
  return map
})
const hotPercent = computed(() => {
  if (!indexStorage.value?.hot_limit_bytes) return 0
  return Math.min(100, (indexStorage.value.hot_bytes / indexStorage.value.hot_limit_bytes) * 100)
})

function prettyPeriod(period) {
  const match = /^(\d{4})-(\d{2})$/.exec(period || '')
  return match ? `${match[1]}年${Number(match[2])}月` : period || '未知账期'
}

function taskStatus(cell) {
  if (cell.state === 'closed' && cell.stale) return { id: 'evidence', label: '有新证据', priority: 1 }
  if (cell.can_close) return { id: 'ready', label: '可确认', priority: 2 }
  return { id: 'pending', label: '待补证据', priority: 0 }
}

function shortReason(cell) {
  if (cell.state === 'closed' && cell.stale) return '结账后收到新证据，冻结结果没有改变'
  if (cell.can_close) return '资料和自检已通过，可以人工确认'
  if (cell.missing?.length) return `缺${cell.missing.join('、')}`
  const message = cell.blocking?.[0] || ''
  const coverage = /(商品成本|销售收入|发货运费).*?覆盖\s*([\d.]+)%/.exec(message)
  if (coverage) return `${coverage[1]}覆盖 ${coverage[2]}%`
  if (message) return message.split(/[。；]/)[0]
  if (cell.profit === null) return '关键金额还没有算齐'
  return '仍有证据需要核对'
}

const tasks = computed(() => (app.overview?.cells || [])
  .filter((cell) => (
    /^\d{4}-\d{2}$/.test(cell.period || '')
    && cell.period >= LIVE_START
    && (cell.state !== 'closed' || cell.stale)
  ))
  .map((cell) => {
    const status = taskStatus(cell)
    return {
      ...cell,
      key: `${cell.store_id}:${cell.period}`,
      status: status.id,
      statusLabel: status.label,
      priority: status.priority,
      periodLabel: prettyPeriod(cell.period),
      reason: shortReason(cell),
      platformName: platformName(cell.platform),
    }
  })
  .sort((a, b) => (
    a.priority - b.priority
    || String(b.period).localeCompare(String(a.period))
    || String(a.store).localeCompare(String(b.store), 'zh-CN')
  )))

const selectedTask = computed(() => tasks.value.find((task) => task.key === selectedTaskKey.value) || null)

async function loadDetail(id, force = false) {
  if (!id) return null
  loadingStore.value = id
  loadError.value = ''
  try {
    return await app.loadStoreDetail(id, force)
  } catch (error) {
    if (error.name !== 'AbortError' && loadingStore.value === id) loadError.value = error.message
    throw error
  } finally {
    if (loadingStore.value === id) loadingStore.value = ''
  }
}

async function loadPeriod(task) {
  const request = ++periodRequest
  periodSnap.value = null
  periodError.value = ''
  if (!task) return
  periodLoading.value = true
  try {
    const data = await api.period(task.store_id, task.period)
    if (request === periodRequest) periodSnap.value = data
  } catch (error) {
    if (request === periodRequest) periodError.value = error.message
  } finally {
    if (request === periodRequest) periodLoading.value = false
  }
}

function selectTask(task) {
  if (!task) return
  selectedTaskKey.value = task.key
  picked.value = task.store_id
  app.pick({ platform: task.platform, store: task.store_id, period: task.period })
  loadDetail(task.store_id).catch(() => {})
}

function chooseStore(store) {
  picked.value = store.id
  app.pick({ platform: store.platform, store: store.id })
  const task = tasks.value.find((item) => item.store_id === store.id)
  if (task) selectTask(task)
  else {
    selectedTaskKey.value = ''
    periodSnap.value = null
    loadDetail(store.id).catch(() => {})
  }
}

watch(tasks, (list) => {
  if (selectedTask.value || !list.length) return
  const preferred = list.find((task) => (
    task.store_id === app.storeId && (!app.period || task.period === app.period)
  )) || list[0]
  selectTask(preferred)
}, { immediate: true })

watch(selectedTask, (task) => loadPeriod(task), { immediate: true })

const blockerRows = computed(() => {
  const snap = periodSnap.value
  if (!snap || !selectedTask.value) return []
  if (selectedTask.value.status === 'evidence') {
    return [{ title: '结账后收到新证据', detail: '原结账结果仍然冻结；请核对差异后决定是否反结账。' }]
  }
  if (selectedTask.value.status === 'ready') {
    return [{ title: '资料和自检已经通过', detail: '这是人工确认点；确认前仍可查看损益和原始证据。', ok: true }]
  }
  const gaps = snap.gaps || []
  const missing = gaps.filter((gap) => gap.kind === 'missing')
  const blocking = gaps.filter((gap) => gap.severity === 'blocking' && gap.kind !== 'blocking')
  const coverage = gaps.filter((gap) => gap.kind === 'coverage')
  return (missing.length ? [...missing, ...coverage] : [...blocking, ...coverage])
    .slice(0, 4)
    .map((gap) => ({ title: gap.title, detail: gap.detail, source: gap.source }))
})

function files() { return currentDetail.value?.files || [] }
function periods() { return currentDetail.value?.periods || [] }
const closedCount = computed(() => periods().filter((period) => period.state === 'closed').length)
function indexed(file) { return indexByFile.value.get(`${file.store_id}:${file.name}`) || null }

const sourceHints = {
  settlement: ['对账', '支付宝', '微信'],
  freight: ['运费'],
  order_cost: ['聚水潭'],
  order_detail: ['订单明细'],
  after_sales: ['售后'],
}

function sourceFile(source) {
  const hints = sourceHints[source.id] || [source.name]
  return files().find((file) => hints.some((hint) => file.name.includes(hint))) || null
}

const feedLag = computed(() => Math.max(
  0,
  Number(orderFeed.value?.source_latest_seq || orderFeed.value?.health?.latest_seq || 0)
  - Number(orderFeed.value?.consumed_seq || 0),
))

const evidenceRows = computed(() => {
  const rows = []
  if (orderFeed.value?.enabled) {
    rows.push({
      id: '__order_feed__', name: '订单台实时订单与日期成本',
      status: orderFeed.value.last_error ? '需核对' : feedLag.value ? '同步中' : '已到',
      tone: orderFeed.value.last_error ? 'warn' : feedLag.value ? 'working' : 'ok',
      detail: orderFeed.value.last_error
        ? '源端当前未就绪，台账保持最后一次完整证据'
        : feedLag.value ? `落后约 ${count(feedLag.value)} 条，正在追` : '已跟上最新订单、售后和成本',
      action: '查看订单',
    })
  }
  for (const source of periodSnap.value?.sources || []) {
    const file = sourceFile(source)
    rows.push({
      ...source, file,
      status: source.arrived ? '已到' : '缺失',
      tone: source.arrived ? 'ok' : 'bad',
      detail: source.arrived
        ? file ? `${bytes(file.size / 1024)} · 更新于 ${ago(file.updated_at) || '未知'}` : '数据已进入本期计算'
        : source.reason || '结账需要这份证据',
      action: source.arrived ? (file ? '预览原表' : '查看证据') : '去补表',
    })
  }
  return rows
})

function evidenceAction(row) {
  if (row.id === '__order_feed__') {
    window.open('http://192.168.0.155:8001/orders', '_blank', 'noopener,noreferrer')
  } else if (!row.arrived) {
    missingSource.value = row
  } else if (row.file) {
    previewFile(row.file)
  } else {
    openSelected()
  }
}

function periodStatus(period) {
  if (period.state === 'closed' && period.stale) return { text: '有新证据', tone: 'evidence' }
  if (period.state === 'closed') return { text: '已结账', tone: 'closed' }
  if (period.can_close) return { text: '可确认', tone: 'ready' }
  return { text: '待补证据', tone: 'pending' }
}

function detailState() {
  if (!selectedTask.value) return { title: '当前店铺没有待处理店期', tone: 'quiet' }
  if (selectedTask.value.status === 'ready') return { title: '资料已齐，可以人工确认', tone: 'ready' }
  if (selectedTask.value.status === 'evidence') return { title: '结账后有新证据，冻结结果未改变', tone: 'evidence' }
  return { title: '证据未齐，暂不能确认', tone: 'pending' }
}

function primaryLabel() {
  if (selectedTask.value?.status === 'ready') return '查看并确认'
  if (selectedTask.value?.status === 'evidence') return '查看新证据'
  return '查看并处理'
}

function openSelected() {
  if (!selectedTask.value) return
  app.pick({ platform: selectedTask.value.platform, store: selectedTask.value.store_id, period: selectedTask.value.period })
  router.push({ name: 'period', params: { id: selectedTask.value.store_id }, query: { period: selectedTask.value.period } })
}

async function loadSystemState() {
  indexLoading.value = true
  try {
    const feedRequest = api.orderFeedStatus()
    if (app.ingestMode === 'nas') {
      const [filesResult, storageResult, errorsResult, feedResult] = await Promise.all([
        api.indexFiles(), api.indexStorage(), api.indexErrors(), feedRequest,
      ])
      indexFiles.value = filesResult.files || []
      indexStorage.value = storageResult
      indexErrors.value = errorsResult.files || []
      orderFeed.value = feedResult
    } else orderFeed.value = await feedRequest
  } catch (reason) {
    message.error(`系统状态读取失败：${reason.message}`, { duration: 6000 })
  } finally {
    indexLoading.value = false
  }
}

function previewFile(file) {
  const found = indexed(file)
  if (!found?.sha256) return
  previewTarget.value = { ...found, file: file.name, name: file.name, sha256: found.sha256 }
  previewing.value = true
}
function manageFile(file) { managing.value = { ...file, index: indexed(file) } }

function drop(storeId, name, shared = false) {
  dialog.warning({
    title: shared ? '撤下全公司共用表' : '撤下这张表',
    content: `${name}。撤下后会重新计算，损益表上的数可能变化。`,
    positiveText: '撤下', negativeText: '算了',
    onPositiveClick: async () => {
      try {
        const result = await app.run('正在撤下并重算', () => api.dropFile(storeId, name))
        app.invalidate(result.stores?.length ? result.stores : [here.value?.id || storeId])
        await Promise.all([app.loadNavigation(true), app.loadOverview(true)])
        if (here.value?.id) await loadDetail(here.value.id, true)
        message.success('撤下了')
      } catch (error) { message.error(error.message, { duration: 6000 }) }
    },
  })
}

async function register() {
  if (!draft.value.name.trim() || !draft.value.platform) return
  const id = `${draft.value.platform}_${Date.now().toString(36)}`
  try {
    await api.addStore({ id, name: draft.value.name.trim(), platform: draft.value.platform })
    adding.value = false
    draft.value = { name: '', platform: '' }
    app.invalidate([id])
    await Promise.all([app.loadNavigation(true), app.loadOverview(true)])
    const store = app.stores.find((item) => item.id === id)
    if (store) chooseStore(store)
    message.success('登记好了。把这家店的表放入 NAS 对应目录即可。')
  } catch (error) { message.error(error.message, { duration: 6000 }) }
}

onMounted(() => {
  app.loadOverview().catch(() => {})
  loadSystemState()
})
</script>

<template>
  <header class="workbench-heading">
    <div><h1>数据与店铺</h1><p>先处理有问题的店期，再沿着原因回到订单或原始文件。</p></div>
  </header>

  <SystemStatusLine :feed="orderFeed" :index-errors="indexErrors" @details="systemOpen = true" />

  <div class="deliver-workbench">
    <TaskQueue :tasks="tasks" :selected="selectedTask?.key || ''" :loading="!app.overview && app.loading" @select="selectTask" />

    <section class="work-detail" aria-live="polite">
      <template v-if="selectedTask">
        <header class="work-detail-head">
          <div>
            <div class="detail-crumb">{{ selectedTask.platformName }}</div>
            <h2>{{ selectedTask.store }} · {{ selectedTask.periodLabel }}</h2>
            <p v-if="currentDetail" class="detail-scope">当前店铺累计已结账 {{ closedCount }} 个店期（含历史年度）</p>
          </div>
        </header>

        <div class="work-state" :class="detailState().tone"><span class="work-state-icon">!</span><strong>{{ detailState().title }}</strong></div>

        <div v-if="periodLoading" class="work-detail-loading"><n-skeleton text :repeat="7" /></div>
        <n-alert v-else-if="periodError" type="error" :bordered="false">{{ periodError }}</n-alert>
        <template v-else>
          <section class="blocker-section">
            <h3>{{ selectedTask.status === 'ready' ? '确认前最后检查' : '影响确认的关键证据' }}</h3>
            <ol v-if="blockerRows.length" class="blocker-list">
              <li v-for="(row, index) in blockerRows" :key="`${row.title}:${index}`" :class="{ ok: row.ok }">
                <span class="blocker-index num">{{ index + 1 }}</span>
                <span><b>{{ row.title }}</b><small>{{ row.detail }}</small></span>
              </li>
            </ol>
            <p v-else class="small muted">正在整理这家店的具体原因。</p>
          </section>

          <div class="work-actions"><n-button type="primary" size="large" @click="openSelected">{{ primaryLabel() }}</n-button><button class="secondary-action" type="button" @click="openSelected">查看损益</button></div>

          <section class="evidence-section">
            <h3>相关证据</h3>
            <div class="evidence-table">
              <div v-for="row in evidenceRows" :key="row.id" class="evidence-row">
                <span class="evidence-icon" :class="row.tone"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 3h7l4 4v14H7z"/><path d="M14 3v5h5"/></svg></span>
                <span class="evidence-copy"><b>{{ row.name }}</b><small>{{ row.detail }}</small></span>
                <span class="evidence-status" :class="row.tone"><i/>{{ row.status }}</span>
                <button class="link" type="button" @click="evidenceAction(row)">{{ row.action }}</button>
              </div>
            </div>
          </section>

          <details v-if="files().length" class="supporting-detail">
            <summary>查看该店全部文件（{{ files().length }}）</summary>
            <div class="scroll store-files compact-files">
              <table class="files"><thead><tr><th>文件</th><th class="right">大小</th><th class="right">更新</th><th/></tr></thead>
                <tbody><tr v-for="file in files()" :key="`${file.store_id}:${file.name}`">
                  <td class="f-name">{{ file.name }}<span v-if="file.shared" class="pill">全公司共用</span></td>
                  <td class="right xs num nowrap">{{ bytes(file.size / 1024) }}</td><td class="right xs muted nowrap">{{ ago(file.updated_at) || '—' }}</td>
                  <td class="right nowrap"><button v-if="app.ingestMode === 'nas'" class="f-drop" @click="previewFile(file)">预览</button><button v-if="app.ingestMode === 'nas'" class="f-drop" @click="manageFile(file)">替换/撤下</button><button v-else class="f-drop" @click="drop(file.store_id || here.id, file.name, file.shared)">撤下</button></td>
                </tr></tbody>
              </table>
            </div>
          </details>

          <details v-if="periods().length" class="supporting-detail other-periods">
            <summary>查看当前店铺其他账期</summary>
            <div class="other-period-list">
              <button v-for="period in periods().slice(0, 24)" :key="period.period" type="button" @click="router.push({ name: 'period', params: { id: here.id }, query: { period: period.period } })">
                <span class="num">{{ prettyPeriod(period.period) }}</span><span :class="periodStatus(period).tone">{{ periodStatus(period).text }}</span>
              </button>
            </div>
          </details>
        </template>
      </template>
      <div v-else class="work-detail-empty"><b>当前没有待处理店期</b><span>可以从右侧查找其他店铺，查看文件和历史账期。</span></div>
    </section>

    <StoreFinder :platforms="app.platforms" :stores="app.stores" :selected="here?.id || ''" @select="chooseStore" @register="adding = true" />
  </div>

  <n-modal v-model:show="systemOpen" preset="card" title="系统状态" style="max-width:720px">
    <div class="system-modal-head"><div><b>{{ orderFeed?.last_error ? '实时同步当前暂停' : feedLag ? '实时同步正在追赶' : '实时同步正常' }}</b><p class="small muted">这些是排查系统问题时才需要看的内部水位，不影响日常处理店期。</p></div><n-button size="small" :loading="indexLoading" @click="loadSystemState">刷新</n-button></div>
    <dl class="system-facts">
      <div><dt>订单与成本</dt><dd>{{ feedLag ? `落后 ${count(feedLag)} 条` : '已跟上' }}</dd></div><div><dt>最近更新</dt><dd>{{ ago(orderFeed?.last_success) || '—' }}</dd></div>
      <div><dt>快照</dt><dd class="num">{{ orderFeed?.snapshot_id || '—' }}</dd></div><div><dt>消费水位</dt><dd class="num">{{ count(orderFeed?.consumed_seq || 0) }} / {{ count(orderFeed?.source_latest_seq || orderFeed?.health?.latest_seq || 0) }}</dd></div>
      <div><dt>原文件索引</dt><dd>{{ count(indexFiles.filter((file) => file.state === 'ready').length) }} 份已就绪</dd></div><div><dt>原文件热缓存</dt><dd>{{ bytes((indexStorage?.hot_bytes || 0) / 1024) }} / {{ bytes((indexStorage?.hot_limit_bytes || 0) / 1024) }}（{{ hotPercent.toFixed(1) }}%）</dd></div>
    </dl>
    <n-alert v-if="orderFeed?.last_error" type="warning" :bordered="false">{{ orderFeed.last_error }}</n-alert>
    <n-alert v-if="indexErrors.length" type="warning" :bordered="false" style="margin-top:var(--s3)">
      {{ indexErrors.length }} 份原文件需要处理。
    </n-alert>
  </n-modal>

  <n-modal v-model:show="adding" preset="dialog" title="登记新店" positive-text="登记" negative-text="算了" :positive-button-props="{ disabled: !draft.name.trim() || !draft.platform }" @positive-click="register">
    <p class="small muted" style="margin-bottom:var(--s3)">只要店名和平台，主体和税号可以之后补。</p><n-space vertical><n-input v-model:value="draft.name" placeholder="店铺名称，比如 淘宝喜必顺"/><n-select v-model:value="draft.platform" placeholder="选平台" :options="app.platforms.map((platform) => ({ label: platform.name, value: platform.id }))"/></n-space>
  </n-modal>

  <n-modal :show="Boolean(missingSource)" preset="card" title="补充这份证据" style="max-width:680px" @update:show="(show) => { if (!show) missingSource = null }"><template v-if="missingSource"><h3>{{ missingSource.name }}</h3><p class="small muted" style="margin-top:var(--s2)">{{ missingSource.reason || '这份证据是本期确认所必需的。' }}</p><n-alert type="info" :bordered="false" style="margin-top:var(--s4)">把文件放入 NAS 上传区中“{{ platformName(here?.platform) }} / {{ here?.name }} / {{ missingSource.name }}”对应目录。文件关闭后，系统会自动索引和重算。</n-alert><p class="xs num muted" style="margin-top:var(--s3)">{{ app.nasUploadPath }}</p></template></n-modal>

  <UploadPanel v-if="explaining && app.ingestMode !== 'nas'" v-model:show="explaining"/>
  <FilePreviewPanel v-if="previewing" v-model:show="previewing" :target="previewTarget"/>
  <n-modal :show="Boolean(managing)" preset="card" title="在 NAS 中替换或撤下" style="max-width:680px" @update:show="(show) => { if (!show) managing = null }"><template v-if="managing"><p class="small"><b>{{ managing.name }}</b></p><p class="small muted preview-path">{{ managing.index?.path || '索引路径暂不可用' }}</p><n-alert type="info" :bordered="false" style="margin-top:var(--s3)"><b>替换：</b>把新文件放进对应的“00_上传区”目录。旧版本仍会完整留档。</n-alert><n-alert type="warning" :bordered="false" style="margin-top:var(--s3)"><b>撤下：</b>从“10_已接收”移走或删除。已结账期只标记有新证据，不自动反结账。</n-alert></template></n-modal>
</template>
