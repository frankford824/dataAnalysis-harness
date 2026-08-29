<script setup>
/* 数据与店铺。
 *
 * 平台先选、店铺后选、右侧只加载当前店铺。以前进入页面会并发读取全部店铺详情，
 * 店铺从十几家涨到五十多家以后，导航要等三秒以上，而且左栏会铺成一条很长的树。
 * 现在列表只使用启动时已有的店铺注册表，详情按需读取并缓存；快速切店时旧请求即使
 * 后回来也不会盖住当前店铺的加载状态。
 */
import { useDialog, useMessage } from 'naive-ui'
import { computed, defineAsyncComponent, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { api } from '../api'
import DropZone from '../components/DropZone.vue'
import StoreNavigator from '../components/StoreNavigator.vue'
import { ago, bytes, count } from '../format'
import { useApp } from '../store'

const UploadPanel = defineAsyncComponent(() => import('../components/UploadPanel.vue'))
const FilePreviewPanel = defineAsyncComponent(() => import('../components/FilePreviewPanel.vue'))

const app = useApp()
const message = useMessage()
const dialog = useDialog()
const router = useRouter()

const loadingStore = ref('')
const loadError = ref('')
const adding = ref(false)
const explaining = ref(false)
const draft = ref({ name: '', platform: '' })
const indexFiles = ref([])
const indexStorage = ref(null)
const indexErrors = ref([])
const indexLoading = ref(false)
const previewing = ref(false)
const previewTarget = ref(null)
const managing = ref(null)

const indexByFile = computed(() => {
  const map = new Map()
  for (const file of indexFiles.value) map.set(`${file.store_id}:${file.path.split(/[\\/]/).pop()}`, file)
  return map
})
const hotPercent = computed(() => {
  if (!indexStorage.value?.hot_limit_bytes) return 0
  return Math.min(100, (indexStorage.value.hot_bytes / indexStorage.value.hot_limit_bytes) * 100)
})

// 平台、每个平台上次看的店都记住。平台很多时来回切换，不该每次从第一家重新找。
const activePlatform = app.noted('deliver.platform', '')
const picked = app.noted('deliver.store', '')
const rememberedStores = app.noted('deliver.platformStores', {})

const here = computed(() => app.stores.find((store) => store.id === picked.value) || null)
const currentDetail = computed(() =>
  here.value ? app.storeDetails[here.value.id]?.data || null : null,
)
const detailLoading = computed(() => !!here.value && loadingStore.value === here.value.id)
const platformLabel = computed(
  () => app.platforms.find((platform) => platform.id === activePlatform.value)?.name || '',
)

/** 这家店最近一次交表是什么时候。放在店名底下，省得为了知道「新不新」去逐行扫表格。 */
const lastUpdated = computed(() => {
  const newest = (currentDetail.value?.files || []).reduce(
    (latest, file) => file.updated_at > latest ? file.updated_at : latest,
    '',
  )
  return newest ? ago(newest) : ''
})

/** 外部筛选条选了平台或店铺时，这一页跟着定位，但左栏仍保留同平台的全部店铺。 */
function syncSelection() {
  if (!app.stores.length) return
  const externallyPicked = app.stores.find((store) => store.id === app.storeId)
  if (externallyPicked) {
    activePlatform.value = externallyPicked.platform
    picked.value = externallyPicked.id
    return
  }
  const validPlatforms = new Set(app.stores.map((store) => store.platform))
  if (app.platform && validPlatforms.has(app.platform)) activePlatform.value = app.platform
  if (!validPlatforms.has(activePlatform.value)) activePlatform.value = app.stores[0]?.platform || ''
  const list = app.stores.filter((store) => store.platform === activePlatform.value)
  if (!list.some((store) => store.id === picked.value)) {
    const remembered = rememberedStores.value?.[activePlatform.value]
    picked.value = list.find((store) => store.id === remembered)?.id || list[0]?.id || ''
  }
}

watch(
  [
    () => app.stores.map((store) => `${store.id}:${store.platform}`).join('|'),
    () => app.platform,
    () => app.storeId,
  ],
  syncSelection,
  { immediate: true },
)

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

watch(
  () => here.value?.id || '',
  (id) => loadDetail(id).catch(() => {}),
  { immediate: true },
)

function remember(store) {
  rememberedStores.value = { ...rememberedStores.value, [store.platform]: store.id }
}

function choosePlatform(id) {
  if (id === activePlatform.value) return
  activePlatform.value = id
  const list = app.stores.filter((store) => store.platform === id)
  const remembered = rememberedStores.value?.[id]
  const next = list.find((store) => store.id === remembered) || list[0] || null
  picked.value = next?.id || ''
  app.pick({ platform: id, store: next?.id || '' })
}

function chooseStore(store) {
  activePlatform.value = store.platform
  picked.value = store.id
  remember(store)
  app.pick({ platform: store.platform, store: store.id })
}

function files() {
  return currentDetail.value?.files || []
}

function periods() {
  return currentDetail.value?.periods || []
}

function sortedPeriods(items) {
  return [...items].sort((left, right) => {
    const leftKnown = /^\d{4}-\d{2}$/.test(left.period || '')
    const rightKnown = /^\d{4}-\d{2}$/.test(right.period || '')
    if (leftKnown !== rightKnown) return leftKnown ? -1 : 1
    return String(right.period).localeCompare(String(left.period))
  })
}

const openPeriods = computed(() => sortedPeriods(periods().filter((item) => item.state !== 'closed')))
const closedPeriods = computed(() => sortedPeriods(periods().filter((item) => item.state === 'closed')))

function indexed(file) {
  return indexByFile.value.get(`${file.store_id}:${file.name}`) || null
}

async function loadIndexState() {
  if (app.ingestMode !== 'nas') return
  indexLoading.value = true
  try {
    const [filesResult, storageResult, errorsResult] = await Promise.all([
      api.indexFiles(), api.indexStorage(), api.indexErrors(),
    ])
    indexFiles.value = filesResult.files || []
    indexStorage.value = storageResult
    indexErrors.value = errorsResult.files || []
  } catch (reason) {
    message.error(`索引状态读取失败：${reason.message}`, { duration: 6000 })
  } finally {
    indexLoading.value = false
  }
}

function previewFile(file) {
  const found = indexed(file)
  if (!found?.sha256) return
  previewTarget.value = {
    ...found,
    file: file.name,
    name: file.name,
    sha256: found.sha256,
  }
  previewing.value = true
}

onMounted(loadIndexState)
watch(() => app.ingestMode, loadIndexState)

function drop(storeId, name, shared = false) {
  dialog.warning({
    title: shared ? '撤下全公司共用表' : '撤下这张表',
    content: shared
      ? `${name}。它供所有店使用，撤下后所有已有数据的店都会重算。`
      : `${name}。撤下后这家店会重算，损益表上的数会变。`,
    positiveText: '撤下',
    negativeText: '算了',
    onPositiveClick: async () => {
      try {
        const hadOverview = !!app.overview
        const result = await app.run('正在撤下并重算', () => api.dropFile(storeId, name))
        const affected = result.stores?.length ? result.stores : [here.value?.id || storeId]
        app.invalidate(affected)
        await app.loadNavigation(true)
        if (hadOverview) await app.loadOverview(true)
        if (here.value?.id) await loadDetail(here.value.id, true)
        message.success('撤下了')
      } catch (error) {
        message.error(error.message, { duration: 6000 })
      }
    },
  })
}

async function register() {
  if (!draft.value.name.trim() || !draft.value.platform) return
  const id = `${draft.value.platform}_${Date.now().toString(36)}`
  try {
    const hadOverview = !!app.overview
    await api.addStore({ id, name: draft.value.name.trim(), platform: draft.value.platform })
    adding.value = false
    const platform = draft.value.platform
    draft.value = { name: '', platform: '' }
    app.invalidate([id])
    await app.loadNavigation(true)
    if (hadOverview) await app.loadOverview(true)
    const store = app.stores.find((item) => item.id === id)
    activePlatform.value = platform
    picked.value = id
    if (store) remember(store)
    app.pick({ platform, store: id })
    message.success('登记好了。把这家店的表拖进来就能算账。')
  } catch (error) {
    message.error(error.message, { duration: 6000 })
  }
}

function open(period = '') {
  if (!here.value) return
  const fallback = periods()[0]?.period || ''
  app.pick({ store: here.value.id, period: period || fallback })
  router.push({
    name: 'period',
    params: { id: here.value.id },
    query: { period: period || fallback },
  })
}
</script>

<template>
  <!-- 常驻的上传入口在顶栏，每一页同一个位置，这里不再摆第二个——上一版页面标题右边
       又放了一个同样的蓝色「上传表格」，一屏之内两个主操作，两个都不显眼了。 -->
  <div class="spread deliver-heading">
    <div>
      <h1>数据与店铺</h1>
      <p class="small muted">
        按平台找店，右边看这家店交了哪些表。
        <template v-if="app.ingestMode === 'nas'">
          文件放入 NAS 对应的平台、店铺和数据源目录，系统自动索引并核算。
        </template>
        <template v-else>
          表落到哪家店看文件名里的店名，落到哪个账期看表里的日期。
          <button class="link" @click="explaining = true">怎么传</button>
        </template>
      </p>
      <p v-if="app.ingestMode === 'nas'" class="xs num nas-path">{{ app.nasUploadPath }}</p>
    </div>
    <n-button size="small" @click="adding = true">登记新店</n-button>
  </div>

  <n-alert
    v-if="app.ingestMode === 'nas'"
    :type="indexErrors.length ? 'warning' : 'success'"
    :bordered="false"
    class="nas-index-status"
  >
    <div class="spread">
      <div>
        <b>NAS 自动索引</b>
        <span class="small muted">
          · {{ count(indexFiles.filter((file) => file.state === 'ready').length) }} 份已就绪
          · 原文件热缓存 {{ bytes((indexStorage?.hot_bytes || 0) / 1024) }}
          / {{ bytes((indexStorage?.hot_limit_bytes || 0) / 1024) }}
          · {{ indexStorage?.hot_entries || 0 }} 个 SHA
        </span>
        <div class="hot-meter" :title="`热缓存使用 ${hotPercent.toFixed(1)}%`">
          <i :style="{ width: `${hotPercent}%` }" />
        </div>
        <div v-if="indexErrors.length" class="xs" style="margin-top: var(--s2)">
          {{ indexErrors.length }} 份需要处理：
          <span v-for="item in indexErrors.slice(0, 3)" :key="item.path" class="neg">
            {{ item.path.split(/[\\/]/).pop() }}{{ item.error ? `（${item.error}）` : '' }}
          </span>
        </div>
      </div>
      <n-button size="tiny" :loading="indexLoading" @click="loadIndexState">刷新状态</n-button>
    </div>
  </n-alert>

  <div class="deliver-layout">
    <StoreNavigator
      :platforms="app.platforms"
      :stores="app.stores"
      :active-platform="activePlatform"
      :selected-store="picked"
      @select-platform="choosePlatform"
      @select-store="chooseStore"
    />

    <section v-if="here" class="store-detail" aria-live="polite">
      <header class="store-detail-head">
        <div class="grow">
          <div class="detail-crumb">{{ platformLabel }}</div>
          <h2>{{ here.name }}</h2>
          <div v-if="currentDetail" class="detail-meta small muted">
            {{ count(files().length) }} 张表<template v-if="lastUpdated">
              · 最近更新 {{ lastUpdated }}</template>
          </div>
        </div>
        <n-button size="small" secondary @click="open()">查看损益</n-button>
      </header>

      <div v-if="detailLoading" class="detail-loading">
        <div class="detail-loading-title">
          <span class="orbit-loader" />
          <div>
            <b>正在读取店铺资料</b>
            <div class="xs muted">文件和账期加载完成后会在这里出现</div>
          </div>
        </div>
        <n-skeleton text :repeat="5" />
      </div>

      <n-alert v-else-if="loadError" type="error" :bordered="false">
        {{ loadError }}
        <button class="link" style="margin-left: var(--s2)" @click="loadDetail(here.id, true)">
          重试
        </button>
      </n-alert>

      <template v-else-if="currentDetail">
        <!-- 点一下会跳到那个月的损益，所以长得像一排链接标签而不是标签页。带下划线的
             标签页表示「在本页换一个视图」，而这里是要离开这一页。 -->
        <div v-if="periods().length" class="detail-period-groups">
          <section class="detail-period-group open-group">
            <div class="detail-periods-label">
              <b>待确认结账</b><span>{{ openPeriods.length }} 期</span>
            </div>
            <div class="detail-periods">
              <button
                v-for="period in openPeriods"
                :key="period.period"
                class="period-chip open"
                :class="[{ on: period.period === app.period, ready: period.can_close, blocked: period.can_close === false }]"
                :title="`看 ${period.period} 的损益`"
                @click="open(period.period)"
              >
                <i aria-hidden="true" />{{ period.period }}
                <span>{{ period.can_close ? '待确认' : period.can_close === false ? '待完善' : '未结账' }}</span>
              </button>
              <span v-if="!openPeriods.length" class="xs muted">没有待确认账期</span>
            </div>
          </section>
          <section class="detail-period-group closed-group">
            <div class="detail-periods-label">
              <b>已结账归档</b><span>{{ closedPeriods.length }} 期</span>
            </div>
            <div class="detail-periods">
              <button
                v-for="period in closedPeriods"
                :key="period.period"
                class="period-chip closed"
                :class="{ on: period.period === app.period }"
                :title="`查看已结账的 ${period.period}`"
                @click="open(period.period)"
              >
                <i aria-hidden="true">✓</i>{{ period.period }}
                <span>已结账</span>
              </button>
              <span v-if="!closedPeriods.length" class="xs muted">暂无已结账账期</span>
            </div>
          </section>
        </div>

        <h3 class="detail-files-title">文件</h3>

        <div v-if="files().length" class="scroll tall store-files">
          <table class="files">
            <colgroup>
              <col />
              <col class="f-by" />
              <col class="f-size" />
              <col class="f-when" />
              <col class="f-act" />
            </colgroup>
            <thead>
              <tr>
                <th>文件</th>
                <th>交表人</th>
                <th class="right">大小</th>
                <th class="right">更新</th>
                <th />
              </tr>
            </thead>
            <tbody>
              <tr v-for="file in files()" :key="`${file.store_id}:${file.name}`">
                <td class="f-name">
                  {{ file.name }}
                  <span v-if="file.shared" class="pill">全公司共用</span>
                  <span v-if="file.versions > 1" class="pill">{{ file.versions }} 版</span>
                </td>
                <td class="xs muted truncate">{{ file.by || '—' }}</td>
                <td class="right xs num nowrap">{{ bytes(file.size / 1024) }}</td>
                <td class="right xs muted nowrap">{{ ago(file.updated_at) || '—' }}</td>
                <td class="right">
                  <template v-if="app.ingestMode === 'nas'">
                    <button
                      class="f-drop"
                      :disabled="!indexed(file)?.sha256"
                      @click="previewFile(file)"
                    >预览</button>
                    <button class="f-drop" @click="managing = { ...file, index: indexed(file) }">
                      替换/撤下
                    </button>
                  </template>
                  <button
                    v-else
                    class="f-drop"
                    @click="drop(file.store_id || here.id, file.name, file.shared)"
                  >撤下</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <DropZone v-else />
      </template>
    </section>

    <section v-else class="store-detail store-detail-empty">
      先选择一个有店铺的平台。
    </section>
  </div>

  <n-modal
    v-model:show="adding"
    preset="dialog"
    title="登记新店"
    positive-text="登记"
    negative-text="算了"
    :positive-button-props="{ disabled: !draft.name.trim() || !draft.platform }"
    @positive-click="register"
  >
    <p class="small muted" style="margin-bottom: var(--s3)">
      只要店名和平台，主体和税号等到要开票时再说。
    </p>
    <p class="xs muted" style="margin-bottom: var(--s3)">
      店名要和导出文件名里写的一致。平台导出的名字不同，可以之后补充别名。
    </p>
    <n-space vertical>
      <n-input v-model:value="draft.name" placeholder="店铺名称，比如 淘宝喜必顺" />
      <n-select
        v-model:value="draft.platform"
        placeholder="选平台"
        :options="app.platforms.map((platform) => ({ label: platform.name, value: platform.id }))"
      />
    </n-space>
  </n-modal>

  <UploadPanel v-if="explaining && app.ingestMode !== 'nas'" v-model:show="explaining" />
  <FilePreviewPanel
    v-if="previewing"
    v-model:show="previewing"
    :target="previewTarget"
  />

  <n-modal v-model:show="managing" preset="card" title="在 NAS 中替换或撤下" style="max-width: 680px">
    <template v-if="managing">
      <p class="small"><b>{{ managing.name }}</b></p>
      <p class="small muted preview-path">{{ managing.index?.path || '索引路径暂不可用' }}</p>
      <n-alert type="info" :bordered="false" style="margin-top: var(--s3)">
        <b>替换：</b>把新文件放进对应的 <span class="num">00_上传区</span> 目录。系统按内容生成新版本，旧字节仍保留。
      </n-alert>
      <n-alert type="warning" :bordered="false" style="margin-top: var(--s3)">
        <b>撤下：</b>从 <span class="num">10_已接收</span> 移走或删除这份文件。只有 NAS 在线、连续三次扫描缺失并持续十分钟后才会撤表重算；已结账期只标记 stale，不自动反结账。
      </n-alert>
      <p class="xs muted" style="margin-top: var(--s3)">
        不建议直接编辑“已接收”文件；请复制到上传区、编辑完成并关闭 Excel/WPS 后再投递。
      </p>
    </template>
  </n-modal>
</template>
