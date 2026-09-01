<script setup>
import { useDialog, useMessage } from 'naive-ui'
import { computed, defineAsyncComponent, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { api } from '../api'
import PageHead from '../components/PageHead.vue'
import SystemStatusLine from '../components/SystemStatusLine.vue'
import { ago, bytes, count } from '../format'
import { useApp } from '../store'

const UploadPanel = defineAsyncComponent(() => import('../components/UploadPanel.vue'))
const FilePreviewPanel = defineAsyncComponent(() => import('../components/FilePreviewPanel.vue'))

const app = useApp()
const message = useMessage()
const dialog = useDialog()
const router = useRouter()

const LIVE_START = '2026-06'

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

const here = computed(() => app.currentStore)
const currentDetail = computed(() => (
  here.value ? app.storeDetails[here.value.id]?.data || null : null
))
const platformName = (id) => app.platforms.find((p) => p.id === id)?.name || id

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
  const msg = cell.blocking?.[0] || ''
  const coverage = /(商品成本|销售收入|发货运费).*?覆盖\s*([\d.]+)%/.exec(msg)
  if (coverage) return `${coverage[1]}覆盖 ${coverage[2]}%`
  if (msg) return msg.split(/[。；]/)[0]
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

const currentCell = computed(() =>
  tasks.value.find((t) => t.store_id === app.storeId && t.period === app.period) || null,
)

const pendingCounts = computed(() => {
  const byStatus = { pending: 0, ready: 0, evidence: 0 }
  for (const t of tasks.value) byStatus[t.status] = (byStatus[t.status] || 0) + 1
  return { total: tasks.value.length, ...byStatus }
})

const storeTaskMap = computed(() => {
  const map = new Map()
  for (const t of tasks.value) {
    if (!map.has(t.store_id)) map.set(t.store_id, [])
    map.get(t.store_id).push(t)
  }
  return map
})

const platformGroups = computed(() =>
  app.platforms
    .map((plat) => ({
      ...plat,
      stores: app.stores
        .filter((s) => s.platform === plat.id && storeTaskMap.value.has(s.id))
        .map((s) => {
          const items = storeTaskMap.value.get(s.id) || []
          const best = items.reduce((w, t) => (t.priority > w ? t.priority : w), 0)
          return { ...s, pendingCount: items.length, bestPriority: best }
        })
        .sort((a, b) => b.bestPriority - a.bestPriority || b.pendingCount - a.pendingCount),
    }))
    .filter((g) => g.stores.length > 0),
)

const storePeriodsForBar = computed(() => {
  if (!here.value) return []
  const items = storeTaskMap.value.get(here.value.id) || []
  return items
    .sort((a, b) => String(b.period).localeCompare(String(a.period)))
    .map((t) => ({ ...t, active: t.period === app.period }))
})

function pickStore(store) {
  const items = storeTaskMap.value.get(store.id) || []
  const firstPeriod = items.sort((a, b) => String(b.period).localeCompare(String(a.period)))[0]?.period || ''
  app.pick({ platform: store.platform, store: store.id, period: firstPeriod })
}

function pickPeriod(period) {
  app.pick({ period })
}

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

async function loadPeriodSnap(storeId, period) {
  const request = ++periodRequest
  periodSnap.value = null
  periodError.value = ''
  if (!storeId || !period) return
  periodLoading.value = true
  try {
    const data = await api.period(storeId, period)
    if (request === periodRequest) periodSnap.value = data
  } catch (error) {
    if (request === periodRequest) periodError.value = error.message
  } finally {
    if (request === periodRequest) periodLoading.value = false
  }
}

watch(() => app.storeId, (id) => {
  if (id) loadDetail(id).catch(() => {})
  else { periodSnap.value = null }
}, { immediate: true })

watch([() => app.storeId, () => app.period], ([storeId, period]) => {
  loadPeriodSnap(storeId, period)
}, { immediate: true })

const blockerRows = computed(() => {
  const snap = periodSnap.value
  if (!snap || !currentCell.value) return []
  if (currentCell.value.status === 'evidence') {
    return [{ title: '结账后收到新证据', detail: '原结账结果仍然冻结；请核对差异后决定是否反结账。' }]
  }
  if (currentCell.value.status === 'ready') {
    return [{ title: '资料和自检已经通过', detail: '这是人工确认点；确认前仍可查看损益和原始证据。', ok: true }]
  }
  const gaps = snap.gaps || []
  const missing = gaps.filter((g) => g.kind === 'missing')
  const blocking = gaps.filter((g) => g.severity === 'blocking' && g.kind !== 'blocking')
  const coverage = gaps.filter((g) => g.kind === 'coverage')
  return (missing.length ? [...missing, ...coverage] : [...blocking, ...coverage])
    .slice(0, 4)
    .map((g) => ({ title: g.title, detail: g.detail, source: g.source }))
})

function files() { return currentDetail.value?.files || [] }
function periods() { return currentDetail.value?.periods || [] }
const closedCount = computed(() => periods().filter((p) => p.state === 'closed').length)
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
  return files().find((f) => hints.some((h) => f.name.includes(h))) || null
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
    openCurrent()
  }
}

function periodStatus(period) {
  if (period.state === 'closed' && period.stale) return { text: '有新证据', tone: 'evidence' }
  if (period.state === 'closed') return { text: '已结账', tone: 'closed' }
  if (period.can_close) return { text: '可确认', tone: 'ready' }
  return { text: '待补证据', tone: 'pending' }
}

function detailState() {
  if (!currentCell.value) return { title: '当前账期没有待处理项', tone: 'quiet' }
  if (currentCell.value.status === 'ready') return { title: '资料已齐，可以人工确认', tone: 'ready' }
  if (currentCell.value.status === 'evidence') return { title: '结账后有新证据，冻结结果未改变', tone: 'evidence' }
  return { title: '证据未齐，暂不能确认', tone: 'pending' }
}

function primaryLabel() {
  if (currentCell.value?.status === 'ready') return '查看并确认'
  if (currentCell.value?.status === 'evidence') return '查看新证据'
  return '查看并处理'
}

function openCurrent() {
  if (!app.storeId || !app.period) return
  router.push({ name: 'period', params: { id: app.storeId }, query: { period: app.period } })
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
    if (store) pickStore(store)
    message.success('登记好了。把这家店的表放入 NAS 对应目录即可。')
  } catch (error) { message.error(error.message, { duration: 6000 }) }
}

onMounted(() => {
  app.loadOverview().catch(() => {})
  loadSystemState()
})
</script>

<template>
  <PageHead
    title="数据与店铺"
    :scope="app.storeId ? app.scopeParts : []"
    :hint="app.storeId ? '' : '先在顶栏选店铺和账期。没选店时，下面是全公司还没结完的账。'"
  >
    <template #actions>
      <n-button size="small" quaternary @click="adding = true">登记新店</n-button>
    </template>
  </PageHead>

  <SystemStatusLine :feed="orderFeed" :index-errors="indexErrors" @details="systemOpen = true" />

  <!-- 未选择店铺：显示全公司待办概览 -->
  <div v-if="!app.storeId" class="deliver-overview">
    <div v-if="!app.overview && app.loading" style="padding:var(--s5) 0"><n-skeleton text :repeat="6" /></div>
    <template v-else-if="pendingCounts.total">
      <div class="overview-stats">
        <div class="stat-card warn"><span class="stat-value num">{{ pendingCounts.pending }}</span><span class="stat-label">待补证据</span></div>
        <div class="stat-card accent"><span class="stat-value num">{{ pendingCounts.ready }}</span><span class="stat-label">可确认</span></div>
        <div class="stat-card ok"><span class="stat-value num">{{ pendingCounts.evidence }}</span><span class="stat-label">有新证据</span></div>
        <div class="stat-card"><span class="stat-value num">{{ pendingCounts.total }}</span><span class="stat-label">总待处理</span></div>
      </div>
      <p class="overview-hint">从上方 <b>店铺</b> 下拉框选择一家店，或点击下方快捷入口。</p>
      <section v-for="group in platformGroups" :key="group.id" class="platform-section">
        <h3>{{ group.name }} <span class="dim">{{ group.stores.length }}</span></h3>
        <div class="store-cards">
          <button v-for="store in group.stores" :key="store.id" type="button" class="store-card" @click="pickStore(store)">
            <span class="store-card-name">{{ store.name }}</span>
            <span class="store-card-count num">{{ store.pendingCount }} 个账期</span>
          </button>
        </div>
      </section>
    </template>
    <div v-else class="overview-empty">
      <b>全部店期已处理完毕</b>
      <span>从上方店铺下拉框选择任意一家店，可以查看历史账期和文件。</span>
    </div>
  </div>

  <!-- 已选店铺：显示该店详情 -->
  <div v-else class="deliver-store">
    <header class="store-head">
      <div>
        <div class="detail-crumb">{{ platformName(here?.platform) }}</div>
        <h2>{{ here?.name || app.storeId }}</h2>
        <p v-if="currentDetail" class="detail-scope">累计已结账 {{ closedCount }} 个店期（含历史年度）</p>
      </div>
    </header>

    <!-- 待处理账期快捷栏 -->
    <div v-if="storePeriodsForBar.length" class="period-bar">
      <span class="period-bar-label">待处理账期</span>
      <button
        v-for="item in storePeriodsForBar"
        :key="item.period"
        type="button"
        class="period-chip"
        :class="[item.status, { active: item.active }]"
        @click="pickPeriod(item.period)"
      >
        <span class="num">{{ item.periodLabel }}</span>
        <span class="period-chip-badge" :class="item.status">{{ item.statusLabel }}</span>
      </button>
    </div>
    <div v-else-if="!loadingStore" class="period-bar-empty">
      <span>该店铺当前没有待处理的账期。</span>
      <span class="muted">可从上方账期下拉框选择已结账的账期查看。</span>
    </div>

    <!-- 选中账期的详情 -->
    <section v-if="app.period" class="work-detail" aria-live="polite">
      <h3 class="period-title">{{ prettyPeriod(app.period) }}</h3>

      <div class="work-state" :class="detailState().tone">
        <span class="work-state-icon">
          <svg v-if="detailState().tone === 'ready'" viewBox="0 0 24 24"><path d="m5 12 5 5L20 7"/></svg>
          <svg v-else-if="detailState().tone === 'evidence'" viewBox="0 0 24 24"><path d="M12 2v10l4 4"/><circle cx="12" cy="12" r="10"/></svg>
          <svg v-else-if="detailState().tone === 'pending'" viewBox="0 0 24 24"><path d="M12 9v4"/><circle cx="12" cy="17" r="0.5" fill="currentColor"/><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/></svg>
          <svg v-else viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></svg>
        </span>
        <strong>{{ detailState().title }}</strong>
      </div>

      <div v-if="periodLoading" class="work-detail-loading"><n-skeleton text :repeat="5" /></div>
      <n-alert v-else-if="periodError" type="error" :bordered="false">{{ periodError }}</n-alert>
      <template v-else-if="periodSnap">
        <section v-if="blockerRows.length || currentCell" class="blocker-section">
          <h3>{{ currentCell?.status === 'ready' ? '确认前最后检查' : '影响确认的关键证据' }}</h3>
          <ol v-if="blockerRows.length" class="blocker-list">
            <li v-for="(row, idx) in blockerRows" :key="`${row.title}:${idx}`" :class="{ ok: row.ok }">
              <span class="blocker-index num">{{ idx + 1 }}</span>
              <span><b>{{ row.title }}</b><small>{{ row.detail }}</small></span>
            </li>
          </ol>
        </section>

        <div class="work-actions">
          <n-button type="primary" size="large" @click="openCurrent">{{ primaryLabel() }}</n-button>
          <n-button quaternary size="large" @click="openCurrent">
            <template #icon><svg viewBox="0 0 24 24" style="width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.8"><path d="M3 3v18h18"/><path d="m7 14 4-4 4 4 5-5"/></svg></template>
            查看损益
          </n-button>
        </div>

        <section class="evidence-section">
          <h3>相关证据</h3>
          <div class="evidence-table">
            <div v-for="row in evidenceRows" :key="row.id" class="evidence-row">
              <span class="evidence-icon" :class="row.tone">
                <svg v-if="row.tone === 'ok'" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 5 5L20 7"/></svg>
                <svg v-else-if="row.tone === 'working'" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>
                <svg v-else viewBox="0 0 24 24" aria-hidden="true"><path d="M7 3h7l4 4v14H7z"/><path d="M14 3v5h5"/></svg>
              </span>
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
                <td class="right xs num nowrap">{{ bytes(file.size / 1024) }}</td>
                <td class="right xs muted nowrap">{{ ago(file.updated_at) || '—' }}</td>
                <td class="right nowrap">
                  <button v-if="app.ingestMode === 'nas'" class="f-drop" @click="previewFile(file)">预览</button>
                  <button v-if="app.ingestMode === 'nas'" class="f-drop" @click="manageFile(file)">替换/撤下</button>
                  <button v-else class="f-drop" @click="drop(file.store_id || here.id, file.name, file.shared)">撤下</button>
                </td>
              </tr></tbody>
            </table>
          </div>
        </details>

        <details v-if="periods().length" class="supporting-detail other-periods">
          <summary>查看当前店铺其他账期</summary>
          <div class="other-period-list">
            <button v-for="p in periods().slice(0, 24)" :key="p.period" type="button" @click="pickPeriod(p.period)">
              <span class="num">{{ prettyPeriod(p.period) }}</span><span :class="periodStatus(p).tone">{{ periodStatus(p).text }}</span>
            </button>
          </div>
        </details>
      </template>
    </section>

    <div v-else class="period-prompt">
      <p>从上方 <b>账期</b> 下拉框选择一个账期，或点击上面的待处理标签。</p>
    </div>
  </div>

  <n-modal v-model:show="systemOpen" preset="card" title="系统状态" style="max-width:720px">
    <div class="system-modal-head"><div><b>{{ orderFeed?.last_error ? '实时同步当前暂停' : feedLag ? '实时同步正在追赶' : '实时同步正常' }}</b><p class="small muted">这些是排查系统问题时才需要看的内部水位，不影响日常处理店期。</p></div><n-button size="small" :loading="indexLoading" @click="loadSystemState">刷新</n-button></div>
    <dl class="system-facts">
      <div><dt>订单与成本</dt><dd>{{ feedLag ? `落后 ${count(feedLag)} 条` : '已跟上' }}</dd></div><div><dt>最近更新</dt><dd>{{ ago(orderFeed?.last_success) || '—' }}</dd></div>
      <div><dt>快照</dt><dd class="num">{{ orderFeed?.snapshot_id || '—' }}</dd></div><div><dt>消费水位</dt><dd class="num">{{ count(orderFeed?.consumed_seq || 0) }} / {{ count(orderFeed?.source_latest_seq || orderFeed?.health?.latest_seq || 0) }}</dd></div>
      <div><dt>原文件索引</dt><dd>{{ count(indexFiles.filter((f) => f.state === 'ready').length) }} 份已就绪</dd></div><div><dt>原文件热缓存</dt><dd>{{ bytes((indexStorage?.hot_bytes || 0) / 1024) }} / {{ bytes((indexStorage?.hot_limit_bytes || 0) / 1024) }}（{{ hotPercent.toFixed(1) }}%）</dd></div>
    </dl>
    <n-alert v-if="orderFeed?.last_error" type="warning" :bordered="false">{{ orderFeed.last_error }}</n-alert>
    <n-alert v-if="indexErrors.length" type="warning" :bordered="false" style="margin-top:var(--s3)">{{ indexErrors.length }} 份原文件需要处理。</n-alert>
  </n-modal>

  <n-modal v-model:show="adding" preset="dialog" title="登记新店" positive-text="登记" negative-text="算了" :positive-button-props="{ disabled: !draft.name.trim() || !draft.platform }" @positive-click="register">
    <p class="small muted" style="margin-bottom:var(--s3)">只要店名和平台，主体和税号可以之后补。</p><n-space vertical><n-input v-model:value="draft.name" placeholder="店铺名称，比如 淘宝喜必顺"/><n-select v-model:value="draft.platform" placeholder="选平台" :options="app.platforms.map((p) => ({ label: p.name, value: p.id }))"/></n-space>
  </n-modal>

  <n-modal :show="Boolean(missingSource)" preset="card" title="补充这份证据" style="max-width:680px" @update:show="(show) => { if (!show) missingSource = null }"><template v-if="missingSource"><h3>{{ missingSource.name }}</h3><p class="small muted" style="margin-top:var(--s2)">{{ missingSource.reason || '这份证据是本期确认所必需的。' }}</p><n-alert type="info" :bordered="false" style="margin-top:var(--s4)">把文件放入 NAS 上传区中"{{ platformName(here?.platform) }} / {{ here?.name }} / {{ missingSource.name }}"对应目录。文件关闭后，系统会自动索引和重算。</n-alert><p class="xs num muted" style="margin-top:var(--s3)">{{ app.nasUploadPath }}</p></template></n-modal>

  <UploadPanel v-if="explaining && app.ingestMode !== 'nas'" v-model:show="explaining"/>
  <FilePreviewPanel v-if="previewing" v-model:show="previewing" :target="previewTarget"/>
  <n-modal :show="Boolean(managing)" preset="card" title="在 NAS 中替换或撤下" style="max-width:680px" @update:show="(show) => { if (!show) managing = null }"><template v-if="managing"><p class="small"><b>{{ managing.name }}</b></p><p class="small muted preview-path">{{ managing.index?.path || '索引路径暂不可用' }}</p><n-alert type="info" :bordered="false" style="margin-top:var(--s3)"><b>替换：</b>把新文件放进对应的"00_上传区"目录。旧版本仍会完整留档。</n-alert><n-alert type="warning" :bordered="false" style="margin-top:var(--s3)"><b>撤下：</b>从"10_已接收"移走或删除。已结账期只标记有新证据，不自动反结账。</n-alert></template></n-modal>
</template>
