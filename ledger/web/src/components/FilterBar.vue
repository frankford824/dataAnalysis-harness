<script setup>
/* 平台 / 店铺 / 账期 + 检索。
 *
 * 这条要一直横在顶上，所有页面共用一套选择。上一版每页各管各的，从展板点进一家店
 * 再切到数据交付，选中的店就没了——多店铺时每切一次页要重选一次，等于不能用。
 *
 * 三个下拉必须带标签：光看「淘宝天猫 / 天猫皇莉诗 / 2026-06」分不清哪个是平台、
 * 哪个是店、哪个是账期。中间的 › 是级联，不是装饰。
 *
 * 检索在这里而不是单开一页：人要查一个订单号的时候，手上正开着某一页，不该被
 * 赶去另一个地方再回来。
 */
import { computed, defineAsyncComponent, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { prettyPeriod } from '../format'
import { canBack } from '../router'
import { useApp } from '../store'

const SearchPanel = defineAsyncComponent(() => import('./SearchPanel.vue'))

const app = useApp()
const router = useRouter()
const route = useRoute()

//: 返回要回到刚才那个滚动位置，所以必须走浏览器的后退，不能 push 一个新地址。
//: push 出去的是一次新的前进，位置记忆对它不生效。
const backable = computed(() => canBack.value && route.name !== 'board')

const searching = ref(false)
const term = ref('')

const platformOptions = computed(() => [
  { label: '全部平台', value: '' },
  ...app.platforms.map((p) => ({ label: p.name, value: p.id })),
])

const storeOptions = computed(() => [
  { label: '全部店铺', value: '' },
  ...app.visibleStores.map((s) => ({ label: s.name, value: s.id })),
])

const periodOptions = computed(() => [
  { label: '全部账期', value: '' },
  ...app.periods.map((p) => ({
    label: prettyPeriod(p) || p || '未知账期',
    value: p,
  })),
])

function onStore(id) {
  app.pick({ store: id })
  // 只有已经在某家店的账期页上，换店才该换页——那一页的内容就是这家店的。
  // 提成、数据、总览把店当筛选用：人在配提成时换一家店，应该留在这一页看着
  // 配置换成那一家，而不是被拽去损益表。上一版一律 push 到店页，提成页上
  // 点店铺下拉就像没反应（其实人已经不在这一页了），配提成按钮也点不到。
  if (id && route.name === 'period') {
    router.push({ name: 'period', params: { id }, query: { period: app.period } })
  }
}

function onPeriod(v) {
  app.pick({ period: v })
  // 店页上看的是地址栏里的账期（`?period=`），不是筛选条上的值。只改全局状态
  // 的话，下拉框已经是 7 月，底下那排按钮和损益表还停在 6 月——人会以为点了没反应。
  if (route.name === 'period' && route.params.id && v) {
    router.replace({
      name: 'period',
      params: { id: route.params.id },
      query: { period: v },
    })
  }
}

function submit() {
  if (term.value.trim()) searching.value = true
}
</script>

<template>
  <div class="locator">
    <n-button
      v-if="backable"
      size="small"
      quaternary
      class="locator-back"
      title="回到上一页，还停在你刚才看的位置"
      @click="router.back()"
    >
      ← 返回
    </n-button>

    <label class="locator-field platform">
      <span class="locator-label">平台</span>
      <n-select
        :value="app.platform"
        :options="platformOptions"
        size="small"
        @update:value="(v) => app.pick({ platform: v })"
      />
    </label>
    <span class="locator-sep" aria-hidden="true">›</span>
    <label class="locator-field store">
      <span class="locator-label">店铺</span>
      <n-select
        :value="app.storeId"
        :options="storeOptions"
        size="small"
        filterable
        @update:value="onStore"
      />
    </label>
    <span class="locator-sep" aria-hidden="true">›</span>
    <label class="locator-field period">
      <span class="locator-label">账期</span>
      <n-select
        :value="app.period"
        :options="periodOptions"
        size="small"
        title="有数据的月份才会出现在这儿。账期不用预先建：表一交上来，它落在哪个月，哪个月就自己出现了。"
        @update:value="onPeriod"
      />
    </label>

    <div class="locator-search">
      <span class="locator-label">检索</span>
      <n-input
        v-model:value="term"
        size="small"
        placeholder="订单号、金额、科目、文件名"
        clearable
        @keyup.enter="submit"
      >
        <template #suffix>
          <n-button text size="tiny" :disabled="!term.trim()" @click="submit">查</n-button>
        </template>
      </n-input>
    </div>

    <SearchPanel v-if="searching" v-model:show="searching" :term="term" />
  </div>
</template>
