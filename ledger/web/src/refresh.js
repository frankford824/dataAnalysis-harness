/* 页面怎么接刷新总线。
 *
 * useRefresh：登记「这一页要新数时怎么重取」。返回 Promise 的话，顶上的转圈会等
 * 它跑完；重取时不要清空旧内容。处理函数收到 (scope, { silent })，silent 是后台
 * 静默刷新，人正在改东西的页可以借此跳过。
 *
 * useLivePoll：只在后台活儿真在跑的时候轮询进度，跑完就停。轮询绕开本地副本。
 *
 * 常用的几页切走后保活（AppBody 的 keep-alive）。藏起来的页不接刷新、不轮询；
 * 切回来时先照旧显示，再静默重取一次。pauseWhileHidden 让页面上的 watch 藏起来
 * 时先不跑，切回来如果源变过再补一次。
 *
 * startAutoRefresh：人走开、页面可见、没开弹窗时，每十分钟静默全部刷新一次。
 */

import { onActivated, onDeactivated, onUnmounted, ref, watch } from 'vue'
import { withLive } from './api.js'
import { useApp } from './store.js'

function usePageActive() {
  const active = ref(true)
  onActivated(() => { active.value = true })
  onDeactivated(() => { active.value = false })
  return active
}

export function useRefresh(handler) {
  const active = usePageActive()
  let shown = false
  const off = useApp().onRefresh((scope, options) => active.value ? handler(scope, options) : undefined)
  onActivated(() => {
    if (shown) Promise.resolve().then(() => handler('page', { silent: true })).catch(() => {})
    shown = true
  })
  onUnmounted(off)
}

export function pauseWhileHidden(...watchers) {
  onDeactivated(() => watchers.forEach((handle) => handle.pause()))
  onActivated(() => watchers.forEach((handle) => handle.resume()))
}

export function useLivePoll(active, poll, every) {
  const visible = usePageActive()
  let timer = null
  const stop = () => { clearInterval(timer); timer = null }
  watch(() => visible.value && (typeof active === 'function' ? active() : active.value), (on) => {
    stop()
    if (on) timer = setInterval(() => { if (!document.hidden) withLive(poll) }, every)
  }, { immediate: true })
  onUnmounted(stop)
  return stop
}

const AUTO_EVERY = 10 * 60 * 1000
const IDLE_FOR = 60 * 1000

function overlayOpen() {
  return [...document.querySelectorAll('.n-modal, .n-drawer, [role="dialog"]')]
    .some((el) => !el.closest('.agent') && el.getClientRects().length > 0)
}

export function startAutoRefresh(app, pane) {
  let lastActive = Date.now()
  const touch = () => { lastActive = Date.now() }
  const events = ['pointermove', 'pointerdown', 'keydown', 'wheel', 'scroll', 'touchstart']
  for (const name of events) pane.addEventListener(name, touch, { capture: true, passive: true })
  const timer = setInterval(() => {
    const now = Date.now()
    if (now - app.lastUpdated < AUTO_EVERY || now - lastActive < IDLE_FOR) return
    if (document.visibilityState !== 'visible' || app.busy || app.refreshing || overlayOpen()) return
    app.refresh('all', { silent: true })
  }, 15000)
  return () => {
    clearInterval(timer)
    for (const name of events) pane.removeEventListener(name, touch, { capture: true })
  }
}
