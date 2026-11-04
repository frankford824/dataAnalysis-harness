<script setup>
/* 台账助手：常驻在右边的对话面板，每一页都在。
 *
 * 发出去的每一句都带上左边此刻打开的页面（路由、标题、选中的文字），人不用再
 * 解释「我说的是哪家店哪个月」。步骤只显示服务端给的那句人话，不出现代码、SQL、
 * 路径这类东西——看的人是财务和店长。
 */
import { ArrowUp, ArrowUpRight, Camera, Check, ChevronRight, CircleAlert, CircleX, History, LoaderCircle, MessageSquare, PanelRightClose, Paperclip, Square, SquarePen, X } from '@lucide/vue'
import { computed, nextTick, onMounted, onUnmounted, ref, watch, watchEffect } from 'vue'
import { useRoute } from 'vue-router'

import { agentApi, streamChat } from '../agentClient'
import { renderMarkdown } from '../agentMarkdown'
import { useApp } from '../store'

const KEY = { width: 'ledger.agent.width', collapsed: 'ledger.agent.collapsed', user: 'ledger.agent.user', thread: 'ledger.agent.thread' }
const MIN_W = 320
const MAX_W = 720
const DEFAULT_W = 400
const TAB_W = 36
const MAX_IMAGES = 8
const SUGGESTIONS = ['这家店本月利润为什么偏低？', '这笔退款金额不对，帮我查一下', '本月还有哪些店没结账？']
const isMac = typeof navigator !== 'undefined' && /Mac/i.test(navigator.platform || '')

const read = key => { try { return localStorage.getItem(key) } catch { return null } }
const write = (key, value) => { try { value == null ? localStorage.removeItem(key) : localStorage.setItem(key, String(value)) } catch {} }

const app = useApp()
const route = useRoute()
const root = ref(null)
let seq = 0

// ---------------------------------------------------------------- 宽度与收起

const viewport = ref(window.innerWidth)
const width = ref(Number(read(KEY.width)) || DEFAULT_W)
const storedCollapsed = read(KEY.collapsed)
const collapsed = ref(storedCollapsed == null ? window.innerWidth < 1100 : storedCollapsed === 'true')
// 窄屏上两栏放不下，展开时直接盖在页面上。
const overlay = computed(() => viewport.value < 760)
const maxWidth = computed(() => Math.max(MIN_W, Math.min(MAX_W, viewport.value - 480)))
const panelWidth = computed(() => Math.round(Math.min(maxWidth.value, Math.max(MIN_W, width.value))))
const dockWidth = computed(() => (collapsed.value || overlay.value ? TAB_W : panelWidth.value))
const resizing = ref(false)

watchEffect(() => document.documentElement.style.setProperty('--agent-dock', `${dockWidth.value}px`))
watch(collapsed, value => write(KEY.collapsed, value))
watch(resizing, value => document.body.classList.toggle('agent-resizing', value))

function startResize(e) {
  if (e.button !== 0) return
  e.preventDefault()
  const startX = e.clientX
  const startW = panelWidth.value
  resizing.value = true
  const move = ev => { width.value = Math.min(maxWidth.value, Math.max(MIN_W, startW + startX - ev.clientX)) }
  const up = () => {
    resizing.value = false
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', up)
    window.removeEventListener('pointercancel', up)
    write(KEY.width, Math.round(width.value))
  }
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', up)
  window.addEventListener('pointercancel', up)
}
function nudge(delta) {
  width.value = Math.min(maxWidth.value, Math.max(MIN_W, panelWidth.value + delta))
  write(KEY.width, Math.round(width.value))
}
function resetWidth() {
  width.value = DEFAULT_W
  write(KEY.width, DEFAULT_W)
}
function expand() {
  collapsed.value = false
  nextTick(() => {
    textarea.value?.focus()
    follow(true)
  })
}
const onResize = () => (viewport.value = window.innerWidth)

// ---------------------------------------------------------------- 在线状态

const status = ref({ online: null, month_cost_cny: 0, month_budget_cny: 0 })
const statusText = computed(() => (status.value.online === true ? '在线' : status.value.online === false ? '助手重启中' : '连接中'))
const costTitle = computed(() => {
  const { month_cost_cny: used, month_budget_cny: budget } = status.value
  return budget ? `本月已用 ¥${money(used)} / 预算 ¥${money(budget)}` : ''
})
let statusTimer = null
async function loadStatus() {
  if (document.hidden && status.value.online !== null) return
  try {
    status.value = { ...status.value, ...(await agentApi.status()) }
  } catch {
    status.value = { ...status.value, online: false }
  }
}

// ---------------------------------------------------------------- 话题

const threadId = ref(read(KEY.thread) || null)
const threadTitle = ref('')
const messages = ref([])
const loadingThread = ref(false)
const historyOpen = ref(false)
const historyBox = ref(null)
const threads = ref([])
const threadsLoading = ref(false)
const threadsError = ref('')
watch(threadId, value => write(KEY.thread, value))

function normalize(m) {
  return {
    key: ++seq,
    role: m.role === 'user' ? 'user' : 'assistant',
    text: m.text || '',
    images: Array.isArray(m.images) ? m.images : [],
    steps: (m.steps || []).map(s => ({ ...s })),
    cards: (m.cards || []).map(c => ({ ...c })),
    receipt: m.receipt || null,
    at: m.at || '',
    streaming: false,
    error: '',
    stopped: false,
  }
}

async function openThread(id) {
  if (streaming.value || !id) return
  historyOpen.value = false
  loadingThread.value = true
  openSteps.value = new Set()
  try {
    const thread = await agentApi.thread(id)
    threadId.value = thread?.id || id
    threadTitle.value = thread?.title || ''
    messages.value = (thread?.messages || []).map(normalize)
  } catch (e) {
    if (e.status === 404) newThread()
    else notice.value = e.message
  } finally {
    loadingThread.value = false
    follow(true)
  }
}
function newThread() {
  if (streaming.value) return
  threadId.value = null
  threadTitle.value = ''
  messages.value = []
  openSteps.value = new Set()
  historyOpen.value = false
  nextTick(() => textarea.value?.focus())
}
async function loadThreads() {
  threadsLoading.value = true
  threadsError.value = ''
  try {
    threads.value = (await agentApi.threads())?.threads || []
  } catch (e) {
    threadsError.value = e.message
  } finally {
    threadsLoading.value = false
  }
}
function toggleHistory() {
  historyOpen.value = !historyOpen.value
  if (historyOpen.value) loadThreads()
}
function onPointerDown(e) {
  if (historyOpen.value && historyBox.value && !historyBox.value.contains(e.target)) historyOpen.value = false
}

// ---------------------------------------------------------------- 称呼

const userName = ref(read(KEY.user)) // null 表示还没问过；空字符串是「不填」
const askingName = ref(false)
const nameDraft = ref('')
const nameInput = ref(null)
let pendingSend = null
function askName(next) {
  pendingSend = next
  nameDraft.value = userName.value || ''
  askingName.value = true
  historyOpen.value = false
  nextTick(() => nameInput.value?.focus())
}
function saveName(skip) {
  const value = skip ? '' : nameDraft.value.trim().slice(0, 40)
  userName.value = value
  write(KEY.user, value)
  askingName.value = false
  const next = pendingSend
  pendingSend = null
  if (next) send(next.preset)
  else nextTick(() => textarea.value?.focus())
}

// ---------------------------------------------------------------- 左边正在看什么

const picked = ref('')
function onSelection() {
  const sel = document.getSelection()
  if (!sel) return
  // 在面板里点、打字也会改选区，不能因此丢掉左边刚选中的那段。
  if (sel.anchorNode && root.value?.contains(sel.anchorNode)) return
  picked.value = sel.toString().replace(/[ \t]+\n/g, '\n').trim().slice(0, 2000)
}
function pageContext() {
  const h1 = document.querySelector('.dock-main main h1') || document.querySelector('.dock-main h1')
  return {
    path: route.fullPath,
    name: route.name == null ? null : String(route.name),
    title: document.title,
    query: { ...route.query },
    params: { ...route.params },
    heading: (h1?.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 200),
    selection: picked.value,
  }
}

// ---------------------------------------------------------------- 附图

const attachments = ref([])
const fileInput = ref(null)
const notice = ref('')
const dragDepth = ref(0)
const preview = ref('')

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => resolve(img)
    img.onerror = reject
    img.src = src
  })
}
// 长边压到 1600，统一转 JPEG。截图原图动辄几 MB，看清数字用不着那么大。
async function downscale(file) {
  const url = URL.createObjectURL(file)
  try {
    const img = await loadImage(url)
    const scale = Math.min(1, 1600 / Math.max(img.naturalWidth || 1, img.naturalHeight || 1))
    const w = Math.max(1, Math.round((img.naturalWidth || 1) * scale))
    const h = Math.max(1, Math.round((img.naturalHeight || 1) * scale))
    const canvas = document.createElement('canvas')
    canvas.width = w
    canvas.height = h
    const ctx = canvas.getContext('2d')
    // 透明底的 PNG 直接转 JPEG 会变成黑底。
    ctx.fillStyle = '#fff'
    ctx.fillRect(0, 0, w, h)
    ctx.drawImage(img, 0, 0, w, h)
    return canvas.toDataURL('image/jpeg', 0.85)
  } finally {
    URL.revokeObjectURL(url)
  }
}
function imageName(file) {
  const base = (file.name || '').replace(/\.[^.]+$/, '')
  if (!base || base === 'image') {
    const d = new Date()
    const pad = n => String(n).padStart(2, '0')
    return `截图-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}.jpg`
  }
  return `${base}.jpg`
}
async function addFiles(list) {
  const files = [...(list || [])]
  const images = files.filter(f => f.type?.startsWith('image/'))
  if (images.length < files.length) notice.value = '这里只能附图片。要交的表格请用左边的「上传表格」。'
  for (const file of images) {
    if (attachments.value.length >= MAX_IMAGES) {
      notice.value = `一次最多附 ${MAX_IMAGES} 张图`
      break
    }
    const id = ++seq
    attachments.value.push({ id, name: imageName(file), data_url: '', loading: true })
    try {
      const dataUrl = await downscale(file)
      const item = attachments.value.find(a => a.id === id)
      if (item) Object.assign(item, { data_url: dataUrl, loading: false })
    } catch {
      attachments.value = attachments.value.filter(a => a.id !== id)
      notice.value = `「${file.name || '这张图'}」打不开，换一张试试`
    }
  }
}
function removeAttachment(item) {
  attachments.value = attachments.value.filter(a => a.id !== item.id)
}
function onPick(e) {
  addFiles(e.target.files)
  e.target.value = ''
}
function onPaste(e) {
  const data = e.clipboardData
  if (!data) return
  let files = [...(data.files || [])].filter(f => f.type?.startsWith('image/'))
  if (!files.length) {
    files = [...(data.items || [])]
      .filter(item => item.kind === 'file' && item.type.startsWith('image/'))
      .map(item => item.getAsFile())
      .filter(Boolean)
  }
  if (!files.length) return
  // 从 Excel 复制单元格时剪贴板里同时有文字和一张图，这时候人要的是文字。
  if (data.getData('text/plain').trim()) return
  e.preventDefault()
  addFiles(files)
}

// App.vue 在 window 上接管了拖放（拖进来的文件当台账上传）。落在面板上的文件是
// 附图，所以这里一律截住，不让它冒泡上去。拖的是文字时不拦默认行为，方便把左边
// 选中的字直接拖进输入框。
const hasFiles = e => [...(e.dataTransfer?.types || [])].includes('Files')
function onDragEnter(e) {
  e.stopPropagation()
  if (!hasFiles(e)) return
  e.preventDefault()
  dragDepth.value += 1
}
function onDragOver(e) {
  e.stopPropagation()
  if (!hasFiles(e)) return
  e.preventDefault()
  e.dataTransfer.dropEffect = 'copy'
}
function onDragLeave(e) {
  e.stopPropagation()
  if (hasFiles(e)) dragDepth.value = Math.max(0, dragDepth.value - 1)
}
function onDrop(e) {
  e.stopPropagation()
  dragDepth.value = 0
  if (!hasFiles(e)) return
  e.preventDefault()
  const files = [...(e.dataTransfer?.files || [])]
  if (!files.length) return
  if (collapsed.value) expand()
  addFiles(files)
}

// ---------------------------------------------------------------- 输入框

const draft = ref('')
const textarea = ref(null)
const shotTip = ref(false)
function autosize() {
  const el = textarea.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 180)}px`
}
watch(draft, () => nextTick(autosize))
function onKeydown(e) {
  if (e.key !== 'Enter' || e.shiftKey || e.isComposing || e.keyCode === 229) return
  e.preventDefault()
  send()
}

// ---------------------------------------------------------------- 滚动

const list = ref(null)
const stick = ref(true)
function onScroll() {
  const el = list.value
  if (el) stick.value = el.scrollHeight - el.scrollTop - el.clientHeight < 48
}
function follow(force = false) {
  nextTick(() => {
    const el = list.value
    if (!el || !(stick.value || force)) return
    el.scrollTop = el.scrollHeight
    stick.value = true
  })
}

// ---------------------------------------------------------------- 发送

const streaming = ref(false)
let controller = null
const preparing = computed(() => attachments.value.some(a => a.loading))
const canSend = computed(() => !streaming.value && !preparing.value && !!(draft.value.trim() || attachments.value.length))
const lastMessage = computed(() => messages.value[messages.value.length - 1])

function send(preset) {
  if (streaming.value || preparing.value) return
  const fromDraft = typeof preset !== 'string'
  const text = (fromDraft ? draft.value : preset).trim()
  const images = attachments.value.filter(a => a.data_url).map(({ name, data_url }) => ({ name, data_url }))
  if (!text && !images.length) return
  if (userName.value === null) return askName({ preset })
  if (fromDraft) draft.value = ''
  attachments.value = []
  shotTip.value = false
  run({ text, images, page: pageContext() })
}

function apply(reply, event, data) {
  if (event === 'thread') {
    if (data.thread_id) threadId.value = data.thread_id
    if (data.title) threadTitle.value = data.title
  } else if (event === 'step') {
    const step = reply.steps.find(s => s.id === data.id)
    if (step) Object.assign(step, data)
    else reply.steps.push({ ...data })
  } else if (event === 'delta') {
    reply.text += data.text || ''
  } else if (event === 'card') {
    const card = reply.cards.find(c => c.card_id === data.card_id)
    if (card) Object.assign(card, data)
    else reply.cards.push({ ...data })
  } else if (event === 'done') {
    reply.receipt = data
  } else if (event === 'error') {
    reply.error = data.message || '这一轮没有完成'
  }
}

async function run(payload) {
  notice.value = ''
  const at = new Date().toISOString()
  messages.value.push({ key: ++seq, role: 'user', text: payload.text, images: payload.images.map(i => i.data_url), at })
  messages.value.push({ key: ++seq, role: 'assistant', text: '', steps: [], cards: [], receipt: null, at, streaming: true, error: '', stopped: false, retry: payload })
  const reply = messages.value[messages.value.length - 1]
  picked.value = ''
  streaming.value = true
  follow(true)
  controller = new AbortController()
  try {
    await streamChat(
      { thread_id: threadId.value, text: payload.text, images: payload.images, page: payload.page, user: userName.value || '' },
      { signal: controller.signal, onEvent: (event, data) => { apply(reply, event, data); follow() } },
    )
  } catch (e) {
    if (e?.name === 'AbortError') reply.stopped = true
    else {
      if (e?.offline) status.value = { ...status.value, online: false }
      reply.error = e?.offline || status.value.online === false
        ? '助手正在重启，这条没发出去。稍等一会儿再点「重试」。'
        : e?.message || '这一轮没有完成'
    }
  } finally {
    reply.streaming = false
    streaming.value = false
    controller = null
    follow()
    if (historyOpen.value) loadThreads()
  }
}

function stop() {
  const id = threadId.value
  if (id) agentApi.stop(id).catch(() => {})
  controller?.abort()
}

function retry(reply) {
  if (streaming.value || !reply.retry) return
  const at = messages.value.indexOf(reply)
  if (at > 0 && messages.value[at - 1].role === 'user') messages.value.splice(at - 1, 2)
  run(reply.retry)
}

// ---------------------------------------------------------------- 步骤与确认卡

const openSteps = ref(new Set())
const stepKey = (m, s) => `${m.key}:${s.id}`
const isOpen = (m, s) => openSteps.value.has(stepKey(m, s))
function toggleStep(m, s) {
  if (!s.detail) return
  const key = stepKey(m, s)
  if (openSteps.value.has(key)) openSteps.value.delete(key)
  else openSteps.value.add(key)
}
// 回答已经结束而步骤还停在 running，说明是被停掉或断开了，不再转圈。
const stepState = (m, s) => (s.status === 'running' && !m.streaming ? 'halted' : s.status)

const CARD_STATUS = { applied: ['ok', '已修改'], rejected: ['', '没有改'], failed: ['bad', '没改成'] }
async function decide(card, action) {
  if (card.busy) return
  card.busy = true
  card.message = ''
  try {
    const result = await (action === 'confirm' ? agentApi.confirmCard : agentApi.rejectCard)(card.card_id)
    if (result?.status) card.status = result.status
    card.message = result?.message || ''
    if (card.status === 'applied') app.uiRefresh++
  } catch (e) {
    card.message = e.message
  } finally {
    card.busy = false
  }
}

// ---------------------------------------------------------------- 格式

function toDate(value) {
  if (value == null || value === '') return null
  const d = typeof value === 'number' ? new Date(value < 1e12 ? value * 1000 : value) : new Date(value)
  return Number.isNaN(d.getTime()) ? null : d
}
function clock(value) {
  const d = toDate(value)
  if (!d) return ''
  const hm = d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  return d.toDateString() === new Date().toDateString() ? hm : `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`
}
function duration(ms) {
  if (ms == null || Number.isNaN(Number(ms))) return ''
  const n = Number(ms)
  if (n < 1000) return `${Math.round(n)}ms`
  if (n < 60000) return `${(n / 1000).toFixed(1)}s`
  return `${Math.floor(n / 60000)}分${Math.round((n % 60000) / 1000)}秒`
}
function money(value) {
  const n = Number(value) || 0
  return n >= 1 ? n.toFixed(2) : n.toFixed(4)
}
function receipt(r) {
  return [
    r.model,
    r.tokens != null ? `${Number(r.tokens).toLocaleString('zh-CN')} tokens` : '',
    r.cost_cny != null ? `¥${money(r.cost_cny)}` : '',
  ].filter(Boolean).join(' · ')
}
const cellText = v => (v == null || v === '' ? '—' : String(v))

// ---------------------------------------------------------------- 键盘与焦点

function onEsc(e) {
  // 左边开着抽屉时，Esc 会顺着冒到 document 把抽屉关掉；在面板里按的 Esc 只管面板。
  e.stopPropagation()
  if (preview.value) preview.value = ''
  else if (historyOpen.value) historyOpen.value = false
  else if (askingName.value) { askingName.value = false; pendingSend = null }
  else if (shotTip.value) shotTip.value = false
}
// 左边开着抽屉或弹窗时，naive 的焦点锁会在 document 捕获阶段把焦点抢回去，输入框
// 就点不进去了。在更早的 window 捕获阶段截住面板里的 focus 事件，锁就看不见它。
function guardFocus(e) {
  if (e.target instanceof Node && root.value?.contains(e.target)) e.stopImmediatePropagation()
}

onMounted(() => {
  window.addEventListener('focus', guardFocus, true)
  window.addEventListener('resize', onResize)
  document.addEventListener('selectionchange', onSelection)
  document.addEventListener('pointerdown', onPointerDown, true)
  loadStatus()
  statusTimer = setInterval(loadStatus, 30000)
  if (threadId.value) openThread(threadId.value)
  nextTick(autosize)
})
onUnmounted(() => {
  controller?.abort()
  clearInterval(statusTimer)
  window.removeEventListener('focus', guardFocus, true)
  window.removeEventListener('resize', onResize)
  document.removeEventListener('selectionchange', onSelection)
  document.removeEventListener('pointerdown', onPointerDown, true)
  document.documentElement.style.removeProperty('--agent-dock')
  document.body.classList.remove('agent-resizing')
})
</script>

<template>
  <aside
    ref="root"
    class="agent"
    :class="{ collapsed, overlay: overlay && !collapsed, resizing }"
    :style="{ width: `${dockWidth}px` }"
    aria-label="台账助手"
    @dragenter="onDragEnter"
    @dragover="onDragOver"
    @dragleave="onDragLeave"
    @drop="onDrop"
    @keydown.esc="onEsc"
  >
    <button v-if="collapsed" type="button" class="agent-tab" title="展开台账助手" @click="expand">
      <MessageSquare :size="16" aria-hidden="true" />
      <span class="agent-tab-text">台账助手</span>
      <span v-if="streaming" class="spin" />
      <i v-else class="agent-dot" :class="{ on: status.online === true }" />
    </button>

    <div v-show="!collapsed" class="agent-inner" :style="overlay ? null : { width: `${panelWidth}px` }">
      <div
        v-if="!overlay"
        class="agent-resize"
        role="separator"
        aria-orientation="vertical"
        aria-label="调整助手面板宽度"
        :aria-valuenow="panelWidth"
        :aria-valuemin="MIN_W"
        :aria-valuemax="maxWidth"
        tabindex="0"
        title="拖动调整宽度，双击恢复默认"
        @pointerdown="startResize"
        @dblclick="resetWidth"
        @keydown.left.prevent="nudge(16)"
        @keydown.right.prevent="nudge(-16)"
      />

      <header class="agent-head">
        <div class="agent-brand">
          <strong>台账助手</strong>
          <span class="agent-status" :title="costTitle"><i class="agent-dot" :class="{ on: status.online === true }" />{{ statusText }}</span>
        </div>
        <div class="agent-actions">
          <button type="button" class="agent-btn" :disabled="streaming" title="开一个新话题" @click="newThread">
            <SquarePen :size="14" aria-hidden="true" />新话题
          </button>
          <div ref="historyBox" class="agent-history">
            <button type="button" class="agent-btn icon" :class="{ on: historyOpen }" title="历史话题" aria-label="历史话题" :aria-expanded="historyOpen" @click="toggleHistory">
              <History :size="15" aria-hidden="true" />
            </button>
            <div v-if="historyOpen" class="agent-menu">
              <div class="agent-menu-head">最近的话题</div>
              <div v-if="threadsLoading && !threads.length" class="agent-menu-note"><span class="spin" /> 正在读取…</div>
              <div v-else-if="threadsError" class="agent-menu-note">{{ threadsError }}</div>
              <div v-else-if="!threads.length" class="agent-menu-note">还没有聊过。</div>
              <div class="agent-menu-list">
                <button
                  v-for="t in threads"
                  :key="t.id"
                  type="button"
                  class="agent-menu-item"
                  :class="{ on: t.id === threadId }"
                  :disabled="streaming"
                  @click="openThread(t.id)"
                >
                  <span class="truncate">{{ t.title || '未命名话题' }}</span>
                  <small>{{ t.user ? `${t.user} · ` : '' }}{{ clock(t.updated_at) }}</small>
                </button>
              </div>
              <footer class="agent-menu-foot">
                <span class="truncate">称呼：{{ userName || '未填' }}</span>
                <button type="button" class="link" @click="askName(null)">修改</button>
              </footer>
            </div>
          </div>
          <button type="button" class="agent-btn icon" title="收起助手" aria-label="收起助手" @click="collapsed = true">
            <PanelRightClose :size="15" aria-hidden="true" />
          </button>
        </div>
      </header>
      <div v-if="threadTitle && messages.length" class="agent-topic truncate" :title="threadTitle">{{ threadTitle }}</div>

      <div class="agent-scroll">
        <div ref="list" class="agent-list" @scroll="onScroll">
          <div v-if="loadingThread" class="agent-loading"><span class="spin" /> 正在打开这段对话…</div>

          <section v-else-if="!messages.length" class="agent-empty">
            <div class="agent-kicker"><i />台账助手</div>
            <h2>你好{{ userName ? `，${userName}` : '' }}。<br>哪里的账看着不对？</h2>
            <p>我能看到你左边正打开的页面。说清是哪家店、哪一笔，或者直接贴一张截图。</p>
            <ol class="agent-suggest">
              <li v-for="(s, i) in SUGGESTIONS" :key="s">
                <button type="button" @click="send(s)">
                  <span class="num">0{{ i + 1 }}</span>
                  <span class="agent-suggest-text">{{ s }}</span>
                  <ArrowUpRight :size="14" aria-hidden="true" />
                </button>
              </li>
            </ol>
          </section>

          <template v-else>
            <template v-for="m in messages" :key="m.key">
              <article v-if="m.role === 'user'" class="agent-user">
                <header class="agent-meta">
                  <span class="agent-who">你</span>
                  <time class="num">{{ clock(m.at) }}</time>
                </header>
                <ul v-if="m.images.length" class="agent-thumbs">
                  <li v-for="(src, i) in m.images" :key="i">
                    <button type="button" title="查看大图" @click="preview = src"><img :src="src" alt="附图" @load="follow()"></button>
                  </li>
                </ul>
                <p v-if="m.text" class="agent-user-text">{{ m.text }}</p>
              </article>

              <article v-else class="agent-reply" :class="{ live: m.streaming }">
                <header class="agent-meta">
                  <span class="agent-who ai"><i />台账助手</span>
                </header>

                <div v-if="m.steps.length" class="agent-steps">
                  <div v-for="s in m.steps" :key="s.id" class="agent-step" :data-status="stepState(m, s)">
                    <button type="button" class="agent-step-head" :class="{ expandable: !!s.detail }" :aria-expanded="s.detail ? isOpen(m, s) : undefined" @click="toggleStep(m, s)">
                      <ChevronRight class="agent-caret" :class="{ open: isOpen(m, s) }" :size="12" aria-hidden="true" />
                      <span class="agent-step-label">{{ s.label }}</span>
                      <LoaderCircle v-if="stepState(m, s) === 'running'" class="agent-spinner" :size="13" aria-label="进行中" />
                      <Check v-else-if="s.status === 'done'" class="agent-ok" :size="13" aria-label="完成" />
                      <CircleX v-else-if="s.status === 'error'" class="agent-err" :size="13" aria-label="没成功" />
                      <span class="agent-step-ms num">{{ duration(s.ms) }}</span>
                    </button>
                    <p v-if="s.detail && isOpen(m, s)" class="agent-step-detail">{{ s.detail }}</p>
                  </div>
                </div>

                <div v-if="m.streaming && !m.text && !m.steps.length" class="agent-thinking">
                  <span class="agent-dots"><i /><i /><i /></span>正在看你说的情况…
                </div>

                <!-- renderMarkdown 先整体转义 HTML 再排版，v-html 里不会有回答自带的标签 -->
                <div v-if="m.text || (m.streaming && m.steps.length)" class="agent-md" v-html="renderMarkdown(m.text, { caret: m.streaming })" />

                <section v-for="c in m.cards" :key="c.card_id" class="agent-card" :data-status="c.status">
                  <header>
                    <strong>{{ c.title }}</strong>
                    <span v-if="CARD_STATUS[c.status]" class="pill" :class="CARD_STATUS[c.status][0]">{{ CARD_STATUS[c.status][1] }}</span>
                  </header>
                  <p v-if="c.summary" class="agent-card-summary">{{ c.summary }}</p>
                  <table v-if="c.items?.length" class="agent-card-table">
                    <thead><tr><th>项目</th><th>原来</th><th aria-hidden="true" /><th>改成</th></tr></thead>
                    <tbody>
                      <tr v-for="(item, i) in c.items" :key="i">
                        <td>{{ item.label }}</td>
                        <td class="before num">{{ cellText(item.before) }}</td>
                        <td class="arrow" aria-hidden="true">→</td>
                        <td class="after num">{{ cellText(item.after) }}</td>
                      </tr>
                    </tbody>
                  </table>
                  <div v-if="c.status === 'pending'" class="agent-card-actions">
                    <button type="button" class="primary tiny" :disabled="c.busy" @click="decide(c, 'confirm')">
                      <span v-if="c.busy" class="spin" /> 确认修改
                    </button>
                    <button type="button" class="tiny" :disabled="c.busy" @click="decide(c, 'reject')">不用了</button>
                  </div>
                  <p v-if="c.message" class="agent-card-message">{{ c.message }}</p>
                </section>

                <p v-if="m.error" class="agent-error">
                  <CircleAlert :size="14" aria-hidden="true" />
                  <span>{{ m.error }}</span>
                  <button v-if="m.retry && m === lastMessage && !streaming" type="button" class="link" @click="retry(m)">重试</button>
                </p>
                <p v-if="m.stopped" class="agent-stopped">已停止。查到的内容都留着，可以接着说。</p>
                <p v-if="m.receipt" class="agent-receipt num" :title="m.receipt.ms ? `用时 ${duration(m.receipt.ms)}` : ''">{{ receipt(m.receipt) }}</p>
              </article>
            </template>
          </template>
        </div>
        <button v-if="!stick && messages.length" type="button" class="agent-latest" @click="follow(true)">回到最新</button>
      </div>

      <footer class="agent-composer">
        <form v-if="askingName" class="agent-ask" @submit.prevent="saveName(false)">
          <label for="agent-name">怎么称呼你？</label>
          <input id="agent-name" ref="nameInput" v-model="nameDraft" maxlength="40" placeholder="比如：小王" autocomplete="off">
          <button type="submit" class="primary tiny">好的</button>
          <button type="button" class="link" @click="saveName(true)">不填</button>
        </form>
        <p v-if="notice" class="agent-notice">
          <span>{{ notice }}</span>
          <button type="button" class="agent-x" aria-label="知道了" @click="notice = ''"><X :size="12" /></button>
        </p>
        <div v-if="picked" class="agent-picked" :title="picked">
          <span class="truncate">附上左边选中的：「{{ picked }}」</span>
          <button type="button" class="agent-x" aria-label="不附选中的内容" @click="picked = ''"><X :size="12" /></button>
        </div>
        <ul v-if="attachments.length" class="agent-thumbs drafts">
          <li v-for="a in attachments" :key="a.id">
            <span v-if="a.loading" class="agent-thumb-wait"><span class="spin" /></span>
            <button v-else type="button" title="查看大图" @click="preview = a.data_url"><img :src="a.data_url" :alt="a.name"></button>
            <button type="button" class="agent-thumb-x" :aria-label="`移除 ${a.name}`" @click="removeAttachment(a)"><X :size="10" /></button>
          </li>
        </ul>
        <div class="agent-field" :class="{ live: streaming }">
          <textarea
            ref="textarea"
            v-model="draft"
            rows="1"
            placeholder="描述你发现的问题，可粘贴截图…"
            aria-label="给台账助手的消息"
            @keydown="onKeydown"
            @paste="onPaste"
          />
        </div>
        <div class="agent-toolbar">
          <button type="button" class="agent-tool" title="附上图片" @click="fileInput?.click()"><Paperclip :size="13" aria-hidden="true" />附件</button>
          <button type="button" class="agent-tool" :class="{ on: shotTip }" title="怎么截图" @click="shotTip = !shotTip"><Camera :size="13" aria-hidden="true" />截图</button>
          <span class="agent-hint">Enter 发送 · Shift+Enter 换行</span>
          <button v-if="streaming" type="button" class="agent-send stop" @click="stop"><Square :size="9" fill="currentColor" aria-hidden="true" />停止</button>
          <button v-else type="button" class="agent-send" :class="{ ready: canSend }" :disabled="!canSend" @click="send()">发送<ArrowUp :size="13" aria-hidden="true" /></button>
        </div>
        <p v-if="shotTip" class="agent-shot-tip">
          <template v-if="isMac">按 <kbd>⌘</kbd><kbd>⇧</kbd><kbd>4</kbd> 截取屏幕，</template>
          <template v-else>按 <kbd>Win</kbd>+<kbd>Shift</kbd>+<kbd>S</kbd> 截取屏幕，</template>
          回到这里按 <kbd>{{ isMac ? '⌘' : 'Ctrl' }}</kbd>+<kbd>V</kbd> 粘贴。图片也可以直接拖进来。
        </p>
        <input ref="fileInput" type="file" accept="image/*" multiple hidden @change="onPick">
      </footer>

      <div v-if="dragDepth > 0" class="agent-drop">松开，作为附图发给助手</div>
    </div>

    <div v-if="preview" class="agent-lightbox" role="dialog" aria-label="查看大图" @click="preview = ''">
      <img :src="preview" alt="附图原图" @click.stop>
      <button type="button" class="agent-lightbox-x" aria-label="关闭" @click="preview = ''"><X :size="18" /></button>
    </div>
  </aside>
</template>

<style scoped>
.agent {
  position: relative;
  flex: none;
  height: 100vh;
  background: var(--n0);
  border-left: 1px solid var(--n3);
  color: var(--n8);
  font-size: var(--t-md);
  line-height: 1.6;
}
.agent-inner {
  position: relative;
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
}
.agent.overlay .agent-inner {
  position: fixed;
  inset: 0;
  z-index: 4200;
  background: var(--n0);
}

/* 收起后贴在右边的竖条 */
.agent-tab,
.agent-tab:hover:not(:disabled) {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  width: 100%;
  height: 100%;
  padding: 18px 0;
  border: 0;
  border-radius: 0;
  background: var(--n0);
  color: var(--n7);
  font: 560 var(--t-sm)/1 var(--sans);
}
.agent-tab:hover:not(:disabled) { background: var(--n1); color: var(--accent); }
.agent-tab-text { writing-mode: vertical-rl; letter-spacing: .24em; }

.agent-resize {
  position: absolute;
  top: 0;
  bottom: 0;
  left: -4px;
  width: 8px;
  z-index: 5;
  cursor: col-resize;
  touch-action: none;
}
.agent-resize::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: 3px;
  width: 2px;
  background: transparent;
  transition: background .15s;
}
.agent-resize:hover::after,
.agent-resize:focus-visible::after,
.agent.resizing .agent-resize::after { background: var(--accent); }
.agent-resize:focus-visible { outline: none; }

/* 头部 */
.agent-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--s2);
  min-height: 52px;
  padding: 0 var(--s3) 0 var(--s5);
  border-bottom: 1px solid var(--n2);
}
.agent-brand { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
.agent-brand strong { font-size: var(--t-base); font-weight: 640; white-space: nowrap; }
.agent-status { display: inline-flex; align-items: center; gap: 5px; font-size: 11.5px; color: var(--n6); white-space: nowrap; }
.agent-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--n4); flex: none; }
.agent-dot.on { background: var(--ok); box-shadow: 0 0 0 3px var(--ok-bg); }
.agent-actions { display: flex; align-items: center; gap: 2px; flex: none; }
.agent-btn,
.agent-btn:hover:not(:disabled) {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 28px;
  padding: 0 8px;
  border: 0;
  border-radius: var(--r-sm);
  background: none;
  color: var(--n6);
  font: 500 var(--t-xs)/1 var(--sans);
}
.agent-btn.icon { width: 28px; padding: 0; justify-content: center; }
.agent-btn:hover:not(:disabled),
.agent-btn.on { background: var(--n2); color: var(--n9); }
.agent-history { position: relative; }
.agent-menu {
  position: absolute;
  top: 34px;
  right: -40px;
  z-index: 30;
  width: 288px;
  padding: 6px;
  border: 1px solid var(--n3);
  border-radius: var(--r-md);
  background: var(--n0);
  box-shadow: var(--shadow);
}
.agent-menu-head { padding: 6px 8px 4px; font-size: 11px; font-weight: 560; letter-spacing: .08em; color: var(--n5); }
.agent-menu-note { padding: 10px 8px; font-size: var(--t-xs); color: var(--n6); }
.agent-menu-list { max-height: 320px; overflow-y: auto; }
.agent-menu-item,
.agent-menu-item:hover:not(:disabled) {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 1px;
  width: 100%;
  padding: 7px 8px;
  border: 0;
  border-radius: var(--r-sm);
  background: none;
  color: var(--n8);
  font: 500 var(--t-sm)/1.45 var(--sans);
  text-align: left;
}
.agent-menu-item:hover:not(:disabled) { background: var(--n1); }
.agent-menu-item.on { background: var(--accent-bg); color: var(--accent); }
.agent-menu-item small { font-size: 11px; color: var(--n5); font-weight: 400; }
.agent-menu-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--s2);
  margin-top: 4px;
  padding: 8px 8px 4px;
  border-top: 1px solid var(--n2);
  font-size: 11.5px;
  color: var(--n6);
}
.agent-menu-foot .link { font-size: 11.5px; }
.agent-topic {
  padding: 6px var(--s5);
  border-bottom: 1px solid var(--n2);
  font-size: var(--t-xs);
  color: var(--n6);
}

/* 消息列表 */
.agent-scroll { position: relative; flex: 1; min-height: 0; }
.agent-list {
  height: 100%;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: var(--s2) var(--s5) var(--s5);
}
.agent-loading { display: flex; align-items: center; gap: 8px; padding: var(--s6) 0; color: var(--n6); font-size: var(--t-sm); }
.agent-latest,
.agent-latest:hover:not(:disabled) {
  position: absolute;
  left: 50%;
  bottom: 10px;
  transform: translateX(-50%);
  padding: 4px 12px;
  border: 1px solid var(--n3);
  border-radius: 999px;
  background: var(--n0);
  box-shadow: var(--shadow);
  color: var(--n7);
  font: 500 var(--t-xs)/1.4 var(--sans);
}
.agent-latest:hover:not(:disabled) { color: var(--accent); border-color: var(--n4); }

/* 空态 */
.agent-empty { padding: 44px 0 var(--s5); }
.agent-kicker { display: flex; align-items: center; gap: 10px; font-size: 11px; letter-spacing: .14em; color: var(--n6); }
.agent-kicker i { width: 24px; height: 1px; background: var(--accent); }
.agent-empty h2 {
  margin: 14px 0 10px;
  font-size: var(--t-2xl);
  font-weight: 600;
  line-height: 1.32;
  letter-spacing: -.02em;
  color: var(--n9);
}
.agent-empty > p { font-size: var(--t-sm); color: var(--n6); line-height: 1.75; }
.agent-suggest { list-style: none; margin: var(--s5) 0 0; padding: 0; border-top: 1px solid var(--n3); }
.agent-suggest li { border-bottom: 1px solid var(--n3); }
.agent-suggest button,
.agent-suggest button:hover:not(:disabled) {
  display: flex;
  align-items: center;
  gap: 14px;
  width: 100%;
  padding: 13px 2px;
  border: 0;
  border-radius: 0;
  background: none;
  color: var(--n8);
  font: 500 var(--t-md)/1.5 var(--sans);
  text-align: left;
}
.agent-suggest button:hover:not(:disabled) { background: linear-gradient(90deg, var(--accent-bg), transparent); color: var(--n9); }
.agent-suggest .num { font-size: 11px; color: var(--n5); }
.agent-suggest-text { flex: 1; min-width: 0; }
.agent-suggest svg { color: var(--n5); flex: none; }
.agent-suggest button:hover svg { color: var(--accent); }

/* 一轮对话 */
.agent-user { padding-top: 22px; }
.agent-reply + .agent-user { margin-top: 18px; border-top: 1px solid var(--n2); }
.agent-meta { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.agent-who {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 18px;
  padding: 0 6px;
  border-radius: 3px;
  background: var(--n8);
  color: var(--n0);
  font-size: 10.5px;
  letter-spacing: .1em;
  line-height: 1;
}
.agent-who.ai { padding: 0; background: none; color: var(--n6); letter-spacing: .14em; }
.agent-who.ai i { width: 5px; height: 5px; border-radius: 50%; background: var(--accent); }
.agent-meta time { font-size: 11px; color: var(--n5); }
.agent-user-text {
  font-size: var(--t-lg);
  font-weight: 600;
  line-height: 1.5;
  letter-spacing: -.01em;
  color: var(--n9);
  white-space: pre-wrap;
  word-break: break-word;
}
.agent-reply { padding-top: 18px; }

.agent-thumbs { list-style: none; display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 8px; padding: 0; }
.agent-thumbs li { position: relative; }
.agent-thumbs button:not(.agent-thumb-x),
.agent-thumbs button:not(.agent-thumb-x):hover:not(:disabled) { display: block; padding: 0; border: 0; background: none; border-radius: var(--r-sm); cursor: zoom-in; }
.agent-thumbs img {
  display: block;
  height: 72px;
  max-width: 140px;
  object-fit: cover;
  border: 1px solid var(--n3);
  border-radius: var(--r-sm);
}
.agent-thumbs.drafts { margin: 8px 0 0; }
.agent-thumbs.drafts img,
.agent-thumb-wait { height: 52px; max-width: 96px; }
.agent-thumb-wait { display: flex; align-items: center; justify-content: center; width: 64px; border: 1px dashed var(--n4); border-radius: var(--r-sm); }
.agent-thumb-x,
.agent-thumb-x:hover:not(:disabled) {
  position: absolute;
  top: -6px;
  right: -6px;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  padding: 0;
  border: 1px solid var(--n0);
  border-radius: 50%;
  background: var(--n8);
  color: var(--n0);
}
.agent-thumb-x:hover:not(:disabled) { background: var(--bad); }

/* 步骤：一行一条，细到不抢回答的戏 */
.agent-steps { margin: 2px 0 12px 4px; border-left: 1px solid var(--n3); }
.agent-step-head,
.agent-step-head:hover:not(:disabled) {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  min-height: 28px;
  padding: 3px 0 3px 10px;
  border: 0;
  border-radius: 0;
  background: none;
  color: var(--n7);
  font: 400 var(--t-sm)/1.45 var(--sans);
  text-align: left;
  cursor: default;
}
.agent-step-head.expandable { cursor: pointer; }
.agent-step-head.expandable:hover { color: var(--n9); }
.agent-caret { flex: none; color: var(--n4); transition: transform .15s ease; }
.agent-step-head:not(.expandable) .agent-caret { opacity: .35; }
.agent-caret.open { transform: rotate(90deg); }
.agent-step-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.agent-step[data-status='running'] .agent-step-label { color: var(--n9); }
.agent-step[data-status='halted'] .agent-step-label { color: var(--n5); }
.agent-ok { flex: none; color: var(--ok); }
.agent-err { flex: none; color: var(--bad); }
.agent-spinner { flex: none; color: var(--accent); animation: spin .9s linear infinite; }
.agent-step-ms { margin-left: auto; padding-left: 8px; flex: none; font-size: 11px; color: var(--n5); }
.agent-step-detail {
  margin: 0 0 6px 29px;
  font-size: var(--t-xs);
  line-height: 1.65;
  color: var(--n6);
  white-space: pre-wrap;
  word-break: break-word;
}

.agent-thinking { display: flex; align-items: center; gap: 10px; color: var(--n6); font-size: var(--t-sm); }
.agent-dots { display: inline-flex; gap: 4px; }
.agent-dots i { width: 5px; height: 5px; border-radius: 50%; background: var(--accent); animation: agent-dot 1.2s ease-in-out infinite; }
.agent-dots i:nth-child(2) { animation-delay: .15s; opacity: .75; }
.agent-dots i:nth-child(3) { animation-delay: .3s; opacity: .5; }
@keyframes agent-dot {
  0%, 80%, 100% { transform: translateY(0); opacity: .4; }
  40% { transform: translateY(-3px); opacity: 1; }
}

/* 回答正文 */
.agent-md { font-size: var(--t-md); line-height: 1.8; color: var(--n8); word-break: break-word; }
.agent-md :deep(p) { margin: 0 0 .75em; }
.agent-md > :deep(:last-child) { margin-bottom: 0; }
.agent-md :deep(h3), .agent-md :deep(h4), .agent-md :deep(h5), .agent-md :deep(h6) { margin: 1em 0 .4em; font-size: var(--t-md); font-weight: 640; color: var(--n9); }
.agent-md :deep(ul), .agent-md :deep(ol) { margin: 0 0 .75em; padding-left: 1.4em; }
.agent-md :deep(li) { margin: .15em 0; }
.agent-md :deep(strong) { font-weight: 640; color: var(--n9); }
.agent-md :deep(code) { padding: 1px 5px; border-radius: 4px; background: var(--n2); font: var(--t-mono) var(--num); }
.agent-md :deep(pre) { margin: 0 0 .75em; padding: 10px 12px; overflow-x: auto; border-radius: var(--r-sm); background: var(--n1); }
.agent-md :deep(pre code) { padding: 0; background: none; }
.agent-md :deep(blockquote) { margin: 0 0 .75em; padding-left: 12px; border-left: 2px solid var(--n3); color: var(--n6); }
.agent-md :deep(hr) { margin: 1em 0; border: 0; border-top: 1px solid var(--n3); }
.agent-md :deep(a) { color: var(--accent); }
.agent-md :deep(.md-table) { margin: 0 0 .75em; overflow-x: auto; }
.agent-md :deep(table) { width: auto; min-width: 60%; font-variant-numeric: tabular-nums; }
.agent-md :deep(th) { padding: 4px 14px 6px 0; font-size: 11.5px; }
.agent-md :deep(td) { padding: 6px 14px 6px 0; font-size: var(--t-sm); line-height: 1.5; }
.agent-md :deep(.md-caret) {
  display: inline-block;
  width: 2px;
  height: 1.05em;
  margin-left: 2px;
  vertical-align: -.17em;
  background: var(--accent);
  animation: agent-caret 1s steps(1) infinite;
}
@keyframes agent-caret { 50% { opacity: 0; } }

/* 确认卡：要改账的时候才出现 */
.agent-card { margin: 14px 0 0; padding: 14px 16px; border: 1px solid var(--n3); border-radius: var(--r-md); background: var(--n0); }
.agent-card[data-status='pending'] { border-color: #c9d6fb; box-shadow: 0 0 0 3px var(--accent-bg); }
.agent-card > header { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--s2); }
.agent-card > header strong { font-size: var(--t-md); font-weight: 640; color: var(--n9); line-height: 1.5; }
.agent-card-summary { margin-top: 4px; font-size: var(--t-sm); color: var(--n6); line-height: 1.65; }
.agent-card-table { margin-top: 10px; }
.agent-card-table th { padding: 0 10px 5px 0; font-size: 11px; }
.agent-card-table td { padding: 6px 10px 6px 0; font-size: var(--t-sm); vertical-align: top; }
.agent-card-table .before { color: var(--n6); }
.agent-card-table .after { color: var(--n9); font-weight: 600; }
.agent-card-table .arrow { width: 14px; color: var(--n5); }
.agent-card-actions { display: flex; align-items: center; gap: var(--s2); margin-top: 12px; }
.agent-card-actions .spin { width: 10px; height: 10px; border-width: 1.5px; border-color: rgba(255,255,255,.4); border-top-color: #fff; }
.agent-card-message { margin-top: 8px; font-size: var(--t-xs); color: var(--n7); }
.agent-card[data-status='failed'] .agent-card-message { color: var(--bad); }

.agent-error { display: flex; align-items: flex-start; gap: 6px; margin-top: 10px; font-size: var(--t-sm); color: var(--bad); line-height: 1.6; }
.agent-error svg { flex: none; margin-top: 3px; }
.agent-error .link { flex: none; font-size: var(--t-sm); }
.agent-stopped { margin-top: 10px; font-size: var(--t-xs); color: var(--n6); }
.agent-receipt { margin-top: 10px; font-size: 11px; color: var(--n5); letter-spacing: .01em; }

/* 输入区：没有框，一条细底线；回复中一小段蓝线来回扫 */
.agent-composer { flex: none; padding: var(--s2) var(--s5) var(--s3); background: var(--n0); container-type: inline-size; }
.agent-ask {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
  padding: 10px 12px;
  border-radius: var(--r-md);
  background: var(--accent-bg);
  font-size: var(--t-sm);
}
.agent-ask label { font-weight: 600; color: var(--n9); }
.agent-ask input { flex: 1; min-width: 100px; padding: 5px 8px; }
.agent-notice,
.agent-picked {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  padding: 6px 10px;
  border-radius: var(--r-sm);
  font-size: var(--t-xs);
  line-height: 1.5;
}
.agent-notice { background: var(--warn-bg); color: var(--warn); }
.agent-notice span { flex: 1; }
.agent-picked { background: var(--n1); color: var(--n7); }
.agent-picked .truncate { flex: 1; }
.agent-x,
.agent-x:hover:not(:disabled) { display: flex; flex: none; padding: 2px; border: 0; background: none; color: inherit; opacity: .7; }
.agent-x:hover:not(:disabled) { opacity: 1; }
.agent-field { position: relative; border-bottom: 1px solid var(--n3); }
.agent-field::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: -1px;
  height: 1px;
  background: var(--n8);
  transform: scaleX(0);
  transform-origin: right center;
  transition: transform .35s ease;
}
.agent-field:focus-within::after { transform: scaleX(1); transform-origin: left center; }
.agent-field.live::after {
  right: auto;
  width: 30%;
  background: var(--accent);
  transform: none;
  animation: agent-sweep 1.4s ease-in-out infinite alternate;
}
@keyframes agent-sweep { from { transform: translateX(0); } to { transform: translateX(233.33%); } }
.agent-field textarea {
  display: block;
  width: 100%;
  min-height: 46px;
  max-height: 180px;
  padding: 12px 0 10px;
  border: 0;
  outline: none;
  background: transparent;
  color: var(--n9);
  font: var(--t-base)/1.6 var(--sans);
  resize: none;
}
.agent-field textarea::placeholder { color: var(--n5); }
.agent-toolbar { display: flex; align-items: center; gap: 14px; padding-top: 10px; }
.agent-tool,
.agent-tool:hover:not(:disabled) {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 0;
  border: 0;
  background: none;
  color: var(--n6);
  font: 500 var(--t-xs)/1.5 var(--sans);
}
.agent-tool:hover:not(:disabled),
.agent-tool.on { color: var(--accent); }
.agent-hint { margin-left: auto; font-size: 11px; color: var(--n5); white-space: nowrap; }
.agent-send,
.agent-send:hover:not(:disabled) {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 28px;
  padding: 0 12px;
  border: 1px solid var(--n3);
  border-radius: 999px;
  background: var(--n0);
  color: var(--n6);
  font: 560 var(--t-xs)/1 var(--sans);
}
.agent-send:disabled { opacity: 1; color: var(--n5); }
.agent-send.ready { border-color: var(--n8); color: var(--n9); }
.agent-send.ready:hover:not(:disabled) { border-color: var(--accent); background: var(--accent); color: #fff; }
.agent-send.stop { border-color: var(--n8); color: var(--n9); }
.agent-send.stop:hover:not(:disabled) { background: var(--n9); color: var(--n0); }
.agent-shot-tip { margin-top: 8px; font-size: var(--t-xs); color: var(--n6); line-height: 1.8; }
.agent-shot-tip kbd {
  display: inline-block;
  min-width: 18px;
  padding: 0 4px;
  border: 1px solid var(--n3);
  border-bottom-width: 2px;
  border-radius: 4px;
  background: var(--n1);
  font: 11px/16px var(--num);
  text-align: center;
  color: var(--n7);
}

.agent-drop {
  position: absolute;
  inset: 8px;
  z-index: 20;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 2px dashed var(--accent);
  border-radius: var(--r-lg);
  background: rgba(241, 245, 255, .92);
  color: var(--accent);
  font-size: var(--t-md);
  font-weight: 600;
  pointer-events: none;
}

.agent-lightbox {
  position: fixed;
  inset: 0;
  z-index: 5000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px;
  background: rgba(20, 23, 28, .78);
  cursor: zoom-out;
}
.agent-lightbox img { max-width: 100%; max-height: 100%; border-radius: var(--r-sm); box-shadow: 0 20px 60px rgba(0, 0, 0, .35); cursor: default; }
.agent-lightbox-x,
.agent-lightbox-x:hover:not(:disabled) {
  position: absolute;
  top: 16px;
  right: 16px;
  display: flex;
  padding: 6px;
  border: 0;
  border-radius: 50%;
  background: rgba(255, 255, 255, .14);
  color: #fff;
}

@container (max-width: 330px) {
  .agent-hint { display: none; }
}
@media (prefers-reduced-motion: reduce) {
  .agent-md :deep(.md-caret),
  .agent-field.live::after,
  .agent-dots i,
  .agent-spinner { animation: none; }
}
</style>

<style>
body.agent-resizing { cursor: col-resize; user-select: none; }
body.agent-resizing iframe { pointer-events: none; }
</style>
