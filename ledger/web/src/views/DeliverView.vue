<script setup>
/* 数据与店铺。
 *
 * 平台先选、店铺后选、右侧只加载当前店铺。以前进入页面会并发读取全部店铺详情，
 * 店铺从十几家涨到五十多家以后，导航要等三秒以上，而且左栏会铺成一条很长的树。
 * 现在列表只使用启动时已有的店铺注册表，详情按需读取并缓存；快速切店时旧请求即使
 * 后回来也不会盖住当前店铺的加载状态。
 */
import { useDialog, useMessage } from 'naive-ui'
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { api } from '../api'
import DropZone from '../components/DropZone.vue'
import UploadPanel from '../components/UploadPanel.vue'
import { ago, bytes, count } from '../format'
import { useApp } from '../store'

const app = useApp()
const message = useMessage()
const dialog = useDialog()
const router = useRouter()

const detail = ref({})
const loadingStore = ref('')
const loadError = ref('')
const search = ref('')
const adding = ref(false)
const explaining = ref(false)
const draft = ref({ name: '', platform: '' })
let requestSequence = 0

// 平台、每个平台上次看的店都记住。平台很多时来回切换，不该每次从第一家重新找。
const activePlatform = app.noted('deliver.platform', '')
const picked = app.noted('deliver.store', '')
const rememberedStores = app.noted('deliver.platformStores', {})

const platformCards = computed(() =>
  app.platforms
    .map((platform) => ({
      ...platform,
      count: app.stores.filter((store) => store.platform === platform.id).length,
    }))
    .filter((platform) => platform.count),
)

const platformStores = computed(() =>
  app.stores.filter((store) => store.platform === activePlatform.value),
)

const visibleStores = computed(() => {
  const word = search.value.trim().toLocaleLowerCase()
  if (!word) return platformStores.value
  return platformStores.value.filter((store) => store.name.toLocaleLowerCase().includes(word))
})

const here = computed(() => app.stores.find((store) => store.id === picked.value) || null)
const currentDetail = computed(() => (here.value ? detail.value[here.value.id] || null : null))
const detailLoading = computed(() => !!here.value && loadingStore.value === here.value.id)
const platformLabel = computed(
  () => platformCards.value.find((platform) => platform.id === activePlatform.value)?.name || '',
)

/** 外部筛选条选了平台或店铺时，这一页跟着定位，但左栏仍保留同平台的全部店铺。 */
function syncSelection() {
  if (!app.stores.length) return
  const externallyPicked = app.stores.find((store) => store.id === app.storeId)
  if (externallyPicked) {
    activePlatform.value = externallyPicked.platform
    picked.value = externallyPicked.id
    return
  }
  const validPlatforms = new Set(platformCards.value.map((platform) => platform.id))
  if (app.platform && validPlatforms.has(app.platform)) activePlatform.value = app.platform
  if (!validPlatforms.has(activePlatform.value)) activePlatform.value = platformCards.value[0]?.id || ''
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
  if (!id || (detail.value[id] && !force)) return detail.value[id]
  const sequence = ++requestSequence
  loadingStore.value = id
  loadError.value = ''
  try {
    const got = await api.store(id)
    detail.value = { ...detail.value, [id]: got }
    return got
  } catch (error) {
    if (sequence === requestSequence) loadError.value = error.message
    throw error
  } finally {
    if (sequence === requestSequence) loadingStore.value = ''
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
  search.value = ''
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

function drop(storeId, name) {
  dialog.warning({
    title: '撤下这张表',
    content: `${name}。撤下后这家店会重算，损益表上的数会变。`,
    positiveText: '撤下',
    negativeText: '算了',
    onPositiveClick: async () => {
      try {
        await app.run('正在撤下并重算', () => api.dropFile(storeId, name))
        app.invalidate()
        await app.load(true)
        await loadDetail(storeId, true)
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
    await api.addStore({ id, name: draft.value.name.trim(), platform: draft.value.platform })
    adding.value = false
    const platform = draft.value.platform
    draft.value = { name: '', platform: '' }
    await app.load(true)
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
  <div class="spread deliver-heading">
    <div>
      <h1>数据与店铺</h1>
      <div class="small muted">
        先选平台，再选店铺；右侧只读取当前店铺，切换后立即联动。
      </div>
      <div class="xs muted" style="margin-top: var(--s1)">
        表落到哪家店，看文件名里的店名；落到哪个账期，看表里的日期。
        <button class="link" @click="explaining = true">怎么传</button>
      </div>
    </div>
    <n-space size="small">
      <n-button size="small" type="primary" @click="explaining = true">上传表格</n-button>
      <n-button size="small" @click="adding = true">登记新店</n-button>
    </n-space>
  </div>

  <div class="deliver-layout">
    <aside class="card store-browser" aria-label="平台和店铺">
      <div class="store-browser-title">
        <div>
          <b>平台</b>
          <span class="xs muted">{{ count(app.stores.length) }} 家店</span>
        </div>
      </div>

      <div class="platform-switcher" role="tablist" aria-label="选择平台">
        <button
          v-for="platform in platformCards"
          :key="platform.id"
          class="platform-choice"
          :class="{ on: platform.id === activePlatform }"
          role="tab"
          :aria-selected="platform.id === activePlatform"
          @click="choosePlatform(platform.id)"
        >
          <span>{{ platform.name }}</span>
          <span class="platform-count num">{{ platform.count }}</span>
        </button>
      </div>

      <n-input
        v-model:value="search"
        class="store-search"
        size="small"
        clearable
        :placeholder="`搜索${platformLabel || '当前平台'}店铺`"
      />

      <div class="store-list-scroll" role="listbox" :aria-label="`${platformLabel}店铺`">
        <button
          v-for="store in visibleStores"
          :key="store.id"
          class="store-choice"
          :class="{ on: store.id === picked }"
          role="option"
          :aria-selected="store.id === picked"
          @click="chooseStore(store)"
        >
          <span class="store-choice-name">
            <span class="store-dot" />
            <span>{{ store.name }}</span>
          </span>
          <span class="store-choice-tail">
            <span v-if="detail[store.id]" class="xs muted num">
              {{ count((detail[store.id].files || []).length) }} 张
            </span>
            <span v-else class="xs muted">详情</span>
            <span aria-hidden="true">›</span>
          </span>
        </button>
        <div v-if="!visibleStores.length" class="store-empty">
          当前平台没有匹配的店铺。
        </div>
      </div>

      <div class="store-browser-foot xs muted">
        {{ platformLabel || '未选平台' }} · {{ count(platformStores.length) }} 家
      </div>
    </aside>

    <section v-if="here" class="card store-detail" aria-live="polite">
      <header class="store-detail-head">
        <div>
          <div class="detail-breadcrumb">
            <span>{{ platformLabel }}</span><span aria-hidden="true">/</span><span>店铺详情</span>
          </div>
          <h2>{{ here.name }}</h2>
        </div>
        <n-space size="small" align="center">
          <span v-if="currentDetail" class="sub">{{ count(files().length) }} 张表</span>
          <n-button size="small" secondary @click="open()">查看损益</n-button>
        </n-space>
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
        <div v-if="files().length" class="scroll tall store-files">
          <n-table size="small" :bordered="false">
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
              <tr v-for="file in files()" :key="file.name">
                <td class="xs">
                  {{ file.name }}
                  <n-tag v-if="file.versions > 1" size="tiny" :bordered="false">
                    {{ file.versions }} 版
                  </n-tag>
                </td>
                <td class="xs muted">{{ file.by || '—' }}</td>
                <td class="right xs num">{{ bytes(file.size / 1024) }}</td>
                <td class="right xs muted nowrap">{{ ago(file.updated_at) || '—' }}</td>
                <td class="right">
                  <n-button size="tiny" quaternary type="error" @click="drop(here.id, file.name)">
                    撤下
                  </n-button>
                </td>
              </tr>
            </tbody>
          </n-table>
        </div>
        <DropZone v-else />

        <div v-if="periods().length" class="panel period-links">
          <div class="spread">
            <h3>算过的账期</h3>
            <span class="xs muted">点击进入该月损益</span>
          </div>
          <div class="row wrap" style="margin-top: var(--s2)">
            <n-button
              v-for="period in periods()"
              :key="period.period"
              size="tiny"
              @click="open(period.period)"
            >
              {{ period.period }}
              <span class="xs muted" style="margin-left: 4px">
                {{ period.state === 'closed' ? '已结' : '未结' }}
              </span>
            </n-button>
          </div>
        </div>
      </template>
    </section>

    <section v-else class="card store-detail store-detail-empty">
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

  <UploadPanel v-model:show="explaining" />
</template>
