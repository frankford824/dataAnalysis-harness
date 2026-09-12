<script setup>
import { useMessage } from 'naive-ui'
import { LayoutDashboard, Store, Coins, Tags, RefreshCw } from '@lucide/vue'
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { useApp } from '../store'
import FilterBar from './FilterBar.vue'

const IntakeResult = defineAsyncComponent(() => import('./IntakeResult.vue'))
const UploadPanel = defineAsyncComponent(() => import('./UploadPanel.vue'))

const props = defineProps({ dropped: { type: Array, default: null } })
const emit = defineEmits(['taken'])

const app = useApp()
const message = useMessage()
const router = useRouter()
function refreshCurrent(){app.uiRefresh++;app.loadNavigation(true).catch(e=>message.error(e.message))}

// 懒加载页面时给导航一个明确反馈。保留很短的最小展示时间，避免快请求只闪一下；
// 真正的数据加载由页面自己的骨架屏接手，两层各自说明自己在等什么。
const routeLoading = ref(false)
const pageLoadError=ref(false)
function pageFailed(){routeLoading.value=false;pageLoadError.value=true}
function reloadPage(){window.location.reload()}
let routeTimer = null
const stopBefore = router.beforeEach((to, from) => {
  if (to.fullPath === from.fullPath) return
  clearTimeout(routeTimer)
  routeTimer = setTimeout(() => (routeLoading.value = true), 120)
})
const stopAfter = router.afterEach(() => {
  clearTimeout(routeTimer)
  routeLoading.value = false
})
const stopError=router.onError(pageFailed)

// 鼠标经过或浏览器空闲时提前取页面代码；数据不会预取，仍以当前筛选为准。
function preloadDeliver() {
  import('../views/DeliverView.vue').catch(() => {})
}

const readyCount = computed(
  () => (app.overview?.cells || []).filter((c) => c.can_close && c.state !== 'closed').length,
)

// 上传要多久取决于表有多大，淘宝一个月的表能跑十几秒。不显示已用秒数的话，人会
// 以为卡死了，然后刷新——刷新会让这次上传的结果看不见。
const secs = ref(0)
let tick = null
watch(
  () => app.busy,
  (busy) => {
    clearInterval(tick)
    secs.value = 0
    if (busy) tick = setInterval(() => (secs.value = Math.round((Date.now() - busy.since) / 1000)), 1000)
  },
)
onUnmounted(() => {
  clearInterval(tick)
  clearTimeout(routeTimer)
  stopBefore()
  stopAfter()
  stopError()
  window.removeEventListener('ledger:page-load-error',pageFailed)
})

async function take(files) {
  if (!files?.length) return
  if (app.ingestMode === 'nas') {
    message.info(`网页上传已停用，请放到 ${app.nasUploadPath}`)
    return
  }
  try {
    // 收完不跳页：人正开着某一页交表，被甩到别处是最讨厌的一种「帮忙」。算出来的
    // 账期在结果面板里列着，要去点一下就行。
    await app.submit(files)
  } catch (e) {
    message.error(`没收下：${e.message}`, { duration: 6000 })
  }
}

watch(
  () => props.dropped,
  (files) => {
    if (files?.length) {
      take(files)
      emit('taken')
    }
  },
)

const explaining = ref(false)

onMounted(() => {
  window.addEventListener('ledger:page-load-error',pageFailed)
  app.loadNavigation().catch((e) => message.error(e.message, { duration: 6000 }))
  if ('requestIdleCallback' in window) window.requestIdleCallback(preloadDeliver, { timeout: 1500 })
  else setTimeout(preloadDeliver, 500)
})

defineExpose({ take })
</script>

<template>
  <div class="shell workflow-shell" :class="{'commission-shell':$route.meta.commission}">
    <nav class="side">
      <div class="brand">
        台账
        <div class="brand-scope">{{ app.periodLabel }}</div>
      </div>
      <router-link
        class="navlink"
        :class="{ on: $route.name === 'board' }"
        to="/"
        :title="readyCount ? `${readyCount} 个店期可以结账` : '总览'"
      >
        <LayoutDashboard class="nav-icon" aria-hidden="true"/>总览<span v-if="readyCount" class="count">{{ readyCount }}</span>
      </router-link>
      <router-link
        class="navlink"
        :class="{ on: $route.name === 'deliver' }"
        to="/deliver"
        @mouseenter="preloadDeliver"
        @focus="preloadDeliver"
      >
        <Store class="nav-icon" aria-hidden="true"/>数据与店铺<span class="count">{{ app.stores.length || '' }}</span>
      </router-link>
      <router-link
        class="navlink"
        :class="{ on: $route.name === 'commission' || $route.name === 'commission-reports' }"
        :to="{name:'commission',query:$route.meta.commission ? $route.query : {}}"
      >
        <Coins class="nav-icon" aria-hidden="true"/>提成
      </router-link>
      <nav v-if="$route.meta.commission" class="commission-subnav" aria-label="提成菜单">
        <router-link :to="{name:'commission',query:$route.query}">提成设置</router-link>
        <router-link :to="{name:'commission-reports',query:$route.query}">金额汇总</router-link>
      </nav>
      <router-link class="navlink" :class="{ on: $route.name === 'fees' }" to="/fees">
        <Tags class="nav-icon" aria-hidden="true"/>费项
      </router-link>
      <router-link class="navlink" :class="{on:$route.name==='labor'}" to="/labor"><Coins class="nav-icon" aria-hidden="true"/>兼职人工费用</router-link>
      <div class="grow" />
    </nav>

    <div class="body">
      <header v-if="!['commission', 'commission-reports', 'labor'].includes($route.name)" class="topbar">
        <FilterBar />
        <n-button quaternary size="small" :disabled="app.loading || !!app.busy" aria-label="刷新当前页面" title="刷新当前页面" @click="refreshCurrent"><RefreshCw :size="16"/></n-button>
        <!-- 上传只有这一个固定入口，每一页都在同一个地方。上一版侧栏最下角那个
             「交表」，位置和用词都在让人猜：交给谁、是不是报送、和结账什么关系。

             这里点开的是说明屏而不是直接弹选文件框：文件一旦送出去就没有再问
             「传到哪家店哪个月」的机会了，而这两件事恰恰是不用人选的——不说清楚，
             「不用选」看起来就是「没得选」。拖到窗口里的仍然直收，不经这一屏。 -->
        <n-button v-if="app.ingestMode !== 'nas'" size="small" type="primary" @click="explaining = true">
          上传表格
        </n-button>
        <n-tag v-else size="small" :bordered="false">
          自动收表
        </n-tag>
      </header>
      <div class="route-progress" :class="{ on: routeLoading }" aria-hidden="true">
        <span />
      </div>
      <main class="page">
        <n-alert v-if="pageLoadError" type="error" class="ledger-page-error" :bordered="false">页面未能加载，请刷新后重试。 <n-button size="small" @click="reloadPage">刷新页面</n-button></n-alert>
        <router-view v-slot="{ Component, route }">
          <transition name="page-shift">
            <div :key="route.meta.commission ? 'commission' : route.name" class="route-page">
              <component :is="Component" />
            </div>
          </transition>
        </router-view>
      </main>
    </div>

    <div v-if="app.busy" class="busy" role="status" aria-live="polite">
      <span class="spin" />
      <span>{{ app.busy.label }}</span>
      <!-- 阶段和百分比是这条提示存在的理由：转圈只能证明「还没返回」，证明不了
           「还在干活」。人分不出这两件事就会去刷新，一刷新这次交表的结果就没了。 -->
      <span v-if="app.busy.phase" class="dim">{{ app.busy.phase }}</span>
      <span v-if="app.busy.percent != null" class="num">{{ app.busy.percent }}%</span>
      <span class="num">{{ secs }}s</span>
      <span v-if="secs > 20" class="dim">别刷新</span>
    </div>

    <UploadPanel v-if="explaining && app.ingestMode !== 'nas'" v-model:show="explaining" />
    <IntakeResult v-if="app.showIntake || app.intake" />
  </div>
</template>
