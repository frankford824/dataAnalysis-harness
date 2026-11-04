<script setup>
/* 台账助手：常驻在右边的对话面板，每一页都在。
 *
 * 发出去的每一句都带上左边此刻打开的页面（路由、标题、选中的文字），人不用再
 * 解释「我说的是哪家店哪个月」。步骤只显示服务端给的那句人话，不出现代码、SQL、
 * 路径这类东西——看的人是财务和店长。
 */
import { ArrowUp, ArrowUpRight, AtSign, Camera, Check, ChevronRight, CircleAlert, CircleX, Ellipsis, History, LoaderCircle, MessageSquare, PanelRightClose, Paperclip, RotateCw, Square, SquarePen, Undo2, X } from '@lucide/vue'
import { useDialog, useMessage } from 'naive-ui'
import { computed, nextTick, onMounted, onUnmounted, ref, watch, watchEffect } from 'vue'
import { useRoute } from 'vue-router'

import { agentApi, errorText, onAgentFailure, openEvents, streamChat } from '../agentClient'
import { setCosts, setCostsLive, yuan } from '../agentCosts'
import { renderMarkdown } from '../agentMarkdown'
import { useApp } from '../store'
import CostChip from './CostChip.vue'

const KEY = { width: 'ledger.agent.width', collapsed: 'ledger.agent.collapsed', user: 'ledger.agent.user', thread: 'ledger.agent.thread', mode: 'ledger.agent.mode', notify: 'ledger.agent.notifyAsked' }
const GROUP = 'group'
const GROUP_TITLE = '公共群聊'
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
const toast = useMessage()
const dialog = useDialog()
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
  if (inGroup.value) groupUnread.value = 0
  nextTick(() => {
    textarea.value?.focus()
    follow(true)
  })
}
const onResize = () => (viewport.value = window.innerWidth)

// ---------------------------------------------------------------- 在线状态

const status = ref({ online: null })
const statusText = computed(() => (status.value.online === true ? '在线' : status.value.online === false ? '助手重启中' : '连接中'))
let statusTimer = null
// 在线时 30 秒问一次；离线时 10 秒，助手一回来就能看见。
function scheduleStatus() {
  clearTimeout(statusTimer)
  statusTimer = setTimeout(async () => {
    await loadStatus()
    scheduleStatus()
  }, status.value.online === true ? 30000 : 10000)
}
async function loadStatus() {
  if (document.hidden && status.value.online !== null) return
  try {
    const result = await agentApi.status()
    status.value = { ...status.value, ...result, online: result?.online === true }
  } catch {
    status.value = { ...status.value, online: false }
  }
}
function markOffline() {
  if (status.value.online === false) return
  status.value = { ...status.value, online: false }
  scheduleStatus()
}
onAgentFailure(markOffline)

// ---------------------------------------------------------------- 话题

// 「群聊」是所有人共用的一个固定话题，「话题」是各人自己的对话，和以前一样。
// 第一次打开默认在群聊。
const mode = ref(read(KEY.mode) === 'topic' ? 'topic' : 'group')
const inGroup = computed(() => mode.value === 'group')
const threadId = ref(inGroup.value ? GROUP : read(KEY.thread) || null)
const threadTitle = ref(inGroup.value ? GROUP_TITLE : '')
const messages = ref([])
const loadingThread = ref(false)
const historyOpen = ref(false)
const historyBox = ref(null)
const threads = ref([])
const threadsLoading = ref(false)
const threadsError = ref('')
const groupUnread = ref(0)
watch(mode, value => write(KEY.mode, value))
watch(threadId, value => { if (value !== GROUP) write(KEY.thread, value) })

function normalize(m) {
  return {
    key: ++seq,
    id: m.id || '',
    runId: m.run_id || '',
    role: m.role === 'user' ? 'user' : 'assistant',
    user: m.user || '',
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
    threadTitle.value = id === GROUP ? GROUP_TITLE : thread?.title || ''
    messages.value = (thread?.messages || []).map(normalize)
  } catch (e) {
    // 群聊还没人说过话时服务端可能还没建这个话题，当作空的群。
    if (e.status === 404 && id === GROUP) {
      threadId.value = GROUP
      threadTitle.value = GROUP_TITLE
      messages.value = []
    } else if (e.status === 404) newThread()
    else notice.value = errorText(e, '这段对话没打开，请稍后再试')
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
function switchMode(next) {
  if (next === mode.value || streaming.value) return
  mode.value = next
  historyOpen.value = false
  if (next === 'group') {
    groupUnread.value = 0
    openThread(GROUP)
  } else {
    const id = read(KEY.thread)
    if (id) openThread(id)
    else newThread()
  }
}
async function loadThreads() {
  threadsLoading.value = true
  threadsError.value = ''
  try {
    threads.value = ((await agentApi.threads())?.threads || []).filter(t => t.id !== GROUP)
  } catch (e) {
    threadsError.value = errorText(e, '话题列表没取到，请稍后再试')
  } finally {
    threadsLoading.value = false
  }
}
function toggleHistory() {
  historyOpen.value = !historyOpen.value
  menuOpen.value = false
  if (historyOpen.value) loadThreads()
}
function onPointerDown(e) {
  if (historyOpen.value && historyBox.value && !historyBox.value.contains(e.target)) historyOpen.value = false
  if (menuOpen.value && menuBox.value && !menuBox.value.contains(e.target)) menuOpen.value = false
  if (mentionOpen.value && mentionBox.value && !mentionBox.value.contains(e.target)) mentionOpen.value = false
}

// ---------------------------------------------------------------- 群里谁在线

const online = ref([])
// 中文称呼取最后一个字（小王、老张、小李不至于都显示成「小」「老」），其他取首字母。
function initial(name) {
  const chars = [...(name || '').trim()]
  if (!chars.length) return '?'
  return /[\u3400-\u9fff]/.test(chars[chars.length - 1]) ? chars[chars.length - 1] : chars[0].toUpperCase()
}
function tint(name) {
  let h = 0
  for (const ch of name || '') h = (h * 31 + ch.codePointAt(0)) % 360
  return `hsl(${h} 42% 48%)`
}

// ---------------------------------------------------------------- 称呼

const userName = ref(read(KEY.user)) // null 表示还没问过；空字符串是「不填」
// 群里总得有个名字给别人看；没填称呼的就叫「同事」。
const displayName = computed(() => userName.value || '同事')
const isSelf = name => !!userName.value && name === userName.value
const isMine = m => !!m.mine || isSelf(m.user)
const askingName = ref(false)
const nameDraft = ref('')
const nameInput = ref(null)
let pendingSend = null
function askName(next) {
  pendingSend = next
  nameDraft.value = userName.value || ''
  askingName.value = true
  historyOpen.value = false
  menuOpen.value = false
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
// 群里助手默认每句都接；只有以 @某位同事 开头的话是同事之间说的，助手不插嘴。
// 和服务端用同一条规则，只用来决定要不要摆「正在看」的占位，不拦着不发。
const toColleague = text => /^\s*[@＠](?!助手)\S/.test(text)
const mentionOpen = ref(false)
const mentionBox = ref(null)
const colleagues = computed(() => online.value.filter(name => !isSelf(name)))
function mention(name) {
  mentionOpen.value = false
  draft.value = `@${name} ${draft.value.replace(/^\s*[@＠]\S+\s*/, '')}`
  nextTick(() => {
    const el = textarea.value
    if (!el) return
    el.focus()
    el.setSelectionRange(el.value.length, el.value.length)
  })
}

// ---------------------------------------------------------------- 滚动

const list = ref(null)
const stick = ref(true)
function onScroll() {
  const el = list.value
  // 拖宽度时文字重新折行，滚动条会跟着跳，那不是人在往上翻。
  if (el && !resizing.value) stick.value = el.scrollHeight - el.scrollTop - el.clientHeight < 48
}
// 面板变宽变窄、输入框长高都会改列表尺寸；原来贴着底的，改完还贴着底。
let listObserver = null
function pinOnResize() {
  const el = list.value
  if (el && stick.value) el.scrollTop = el.scrollHeight
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
let currentRun = ''
// 自己这个页面发起的轮次和消息。广播回来时据此去重，不在列表里再出现一遍。
const ownRuns = new Set()
const ownMessages = new Set()
const preparing = computed(() => attachments.value.some(a => a.loading))
const canSend = computed(() => !streaming.value && !preparing.value && !!(draft.value.trim() || attachments.value.length))
const lastReply = computed(() => [...messages.value].reverse().find(m => m.role === 'assistant' && !m.remote))

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
  primeAlerts()
  run({ text, images, page: pageContext(), thread: threadId.value })
}

function setReceipt(reply, data) {
  const r = data && typeof data === 'object' && 'receipt' in data ? data.receipt : data
  reply.receipt = r && typeof r === 'object' && (r.model || r.tokens != null || r.cost_cny != null) ? r : null
}
const isEmptyReply = r => !r.text && !r.steps.length && !r.cards.length && !r.error
function dropMessage(m) {
  const at = messages.value.indexOf(m)
  if (at >= 0) messages.value.splice(at, 1)
}

function apply(reply, event, data) {
  if (event === 'thread') {
    if (data.thread_id) threadId.value = data.thread_id
    if (data.title && threadId.value !== GROUP) threadTitle.value = data.title
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
    setReceipt(reply, data)
  } else if (event === 'error') {
    reply.error = errorText(data.message)
  }
}

async function run(payload) {
  notice.value = ''
  const at = Date.now()
  const group = payload.thread === GROUP
  // 群里 @同事 的话助手不接，服务端只回 thread 和 done；这时不摆「正在看」的占位。
  const expectReply = !group || !toColleague(payload.text)
  messages.value.push({ key: ++seq, id: '', role: 'user', user: userName.value || '', mine: true, text: payload.text, images: payload.images.map(i => i.data_url), at })
  const ask = messages.value[messages.value.length - 1]
  let reply = { key: ++seq, id: '', role: 'assistant', text: '', steps: [], cards: [], receipt: null, at, streaming: true, error: '', stopped: false, recovering: false, retry: payload, ask }
  if (expectReply) {
    messages.value.push(reply)
    reply = messages.value[messages.value.length - 1]
  }
  // 没有占位时，出错了才把这条说明挂到列表里。
  const surface = () => {
    if (!messages.value.includes(reply)) {
      messages.value.push(reply)
      reply = messages.value[messages.value.length - 1]
    }
  }
  picked.value = ''
  streaming.value = true
  follow(true)
  controller = new AbortController()
  // 收到 thread 之后，这句话已经在服务端落了库、助手在跑了。这之后断开不能再给
  // 「重试」——重发一遍就是同一个问题问两次、钱花两份。
  let turnThread = null
  let finished = false
  try {
    await streamChat(
      { thread_id: payload.thread, text: payload.text, images: payload.images, page: payload.page, user: userName.value || '' },
      {
        signal: controller.signal,
        onEvent: (event, raw) => {
          const data = raw || {}
          if (event === 'thread') {
            turnThread = data.thread_id || payload.thread
            reply.retry = null
            if (data.run_id) {
              currentRun = data.run_id
              ownRuns.add(data.run_id)
              reply.runId = data.run_id
            }
            if (data.message_id) {
              ownMessages.add(data.message_id)
              ask.id = data.message_id
            }
          }
          if (event === 'done' || event === 'error') finished = true
          if (event === 'card' && group && data.status === 'pending') alertHidden('群里有一张确认卡等人处理', data.title || '助手提出了一处修改，需要有人确认。', `card-${data.card_id}`)
          apply(reply, event, event === 'done' ? raw : data)
          follow()
        },
      },
    )
    if (!finished) throw Object.assign(new Error('网络中断'), { network: true })
    reply.streaming = false
    if (isEmptyReply(reply)) dropMessage(reply)
  } catch (e) {
    reply.streaming = false
    if (e?.name === 'AbortError') {
      if (expectReply) reply.stopped = true
    } else if (turnThread && expectReply) await recover(reply, turnThread, payload, at)
    else if (!turnThread) {
      surface()
      reply.error = e?.network ? '网络中断，这条没发出去。稍等一会儿再点「重试」。' : errorText(e)
    }
  } finally {
    reply.streaming = false
    streaming.value = false
    controller = null
    currentRun = ''
    follow()
    if (historyOpen.value) loadThreads()
  }
}

// ---------------------------------------------------------------- 断线后取回结果

const RECOVER_EVERY = 5000
const RECOVER_FOR = 15 * 60 * 1000
let recovery = null

function settled(list, payload, sentAt, ids = {}) {
  // 群里别人也在说话，不能只看最后一条；知道自己那句的 id 时，看它后面有没有助手的回答。
  const asked = ids.messageId ? list.findIndex(m => m.id === ids.messageId) : -1
  if (asked >= 0) return list.slice(asked + 1).some(m => m.role === 'assistant' && (!ids.runId || !m.run_id || m.run_id === ids.runId))
  const last = list[list.length - 1]
  if (!last || last.role !== 'assistant') return false
  const user = [...list].reverse().find(m => m.role === 'user')
  if (!user || (user.text || '').trim() !== payload.text) return false
  const answeredAt = toDate(last.at)
  const askedAt = toDate(user.at)
  if (answeredAt && askedAt && answeredAt < askedAt) return false
  // 服务端没给提问时间时，退一步用本机发送时间比；留几分钟余量给两边时钟不准。
  if (answeredAt && !askedAt && answeredAt.getTime() < sentAt - 5 * 60 * 1000) return false
  return true
}

async function recover(reply, id, payload, sentAt) {
  const job = { reply, id, runId: reply.runId || '', cancelled: false, timer: null, wake: null }
  const ids = { messageId: reply.ask?.id || '', runId: job.runId }
  recovery = job
  reply.recovering = true
  reply.error = ''
  follow()
  const deadline = Date.now() + RECOVER_FOR
  try {
    while (!job.cancelled && Date.now() < deadline) {
      await new Promise(resolve => {
        job.wake = resolve
        job.timer = setTimeout(resolve, RECOVER_EVERY)
      })
      if (job.cancelled) return
      let thread
      try {
        thread = await agentApi.thread(id)
      } catch {
        continue
      }
      if (job.cancelled) return
      const list = thread?.messages || []
      if (!settled(list, payload, sentAt, ids)) continue
      if (threadId.value === id) {
        messages.value = list.map(normalize)
        if (thread?.title && id !== GROUP) threadTitle.value = thread.title
        follow(true)
      }
      return
    }
    if (!job.cancelled) {
      reply.recovering = false
      reply.error = '这次的结果还没取回来。过一会儿从「历史话题」重新打开这段对话看看。'
    }
  } finally {
    if (recovery === job) recovery = null
  }
}
function cancelRecovery() {
  const job = recovery
  if (!job) return
  job.cancelled = true
  clearTimeout(job.timer)
  job.wake?.()
  job.reply.recovering = false
  job.reply.stopped = true
}

function stop() {
  const id = recovery?.id || threadId.value
  if (id) agentApi.stop(id, currentRun || recovery?.runId).catch(() => {})
  cancelRecovery()
  controller?.abort()
}

function retry(reply) {
  if (streaming.value || !reply.retry) return
  if (reply.ask) dropMessage(reply.ask)
  dropMessage(reply)
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
    const result = await (action === 'confirm' ? agentApi.confirmCard : agentApi.rejectCard)(card.card_id, displayName.value)
    if (result?.status) card.status = result.status
    card.message = result?.message || ''
    if (card.status !== 'pending') card.by = displayName.value
    if (card.status === 'applied') app.refresh('page', { silent: true })
  } catch (e) {
    card.message = errorText(e, '没有处理成功，请稍后再试')
  } finally {
    card.busy = false
  }
}
const cardBy = c => (c.by && c.status && c.status !== 'pending' ? `由 ${c.by} ${c.status === 'rejected' ? '拒绝' : '确认'}` : '')

// ---------------------------------------------------------------- 实时事件（群聊、别人的轮次、卡片、界面和数据变化）

let closeEvents = () => {}
function connectEvents() {
  closeEvents()
  closeEvents = openEvents(displayName.value, onLive, setCostsLive)
}
function onLive(name, data) {
  if (name === 'hello') {
    if (Array.isArray(data.users)) online.value = data.users.filter(Boolean)
    setCosts(data.costs)
  } else if (name === 'costs') setCosts(data)
  else if (name === 'presence') online.value = (data.users || []).filter(Boolean)
  else if (name === 'message') onRemoteMessage(data)
  else if (name === 'turn') onRemoteTurn(data)
  else if (name === 'card_update') onCardUpdate(data)
  else if (name === 'ui_updated') onUiUpdated(data)
  else if (name === 'data_updated') app.refresh('page', { silent: true })
}

function onRemoteMessage(data) {
  const m = data.message
  if (!m) return
  const runId = data.run_id || m.run_id || ''
  if ((runId && ownRuns.has(runId)) || (m.id && ownMessages.has(m.id))) return
  const fromColleague = m.role === 'user' && !isSelf(m.user)
  if (data.thread_id === GROUP && fromColleague && userName.value && (m.text || '').includes(`@${userName.value}`)) {
    alertHidden(`${m.user || '同事'} 在群里提到了你`, m.text || '[图片]', `mention-${m.id || Date.now()}`)
  }
  if (data.thread_id !== threadId.value) {
    if (data.thread_id === GROUP && fromColleague) groupUnread.value += 1
    return
  }
  if (collapsed.value && data.thread_id === GROUP && fromColleague) groupUnread.value += 1
  if (m.id && messages.value.some(x => x.id === m.id)) return
  const recent = messages.value.slice(-30).reverse()
  if (m.role === 'assistant') {
    // 别人那一轮一直在跟着 turn 显示，最终的回答落库后把它收成正式消息，不再另加一条。
    const live = messages.value.find(x => x.remote && !x.id && (runId ? x.runId === runId : true))
    if (live) {
      Object.assign(live, { id: m.id || '', text: m.text || live.text, streaming: false, at: m.at || live.at })
      if (m.cards?.length) live.cards = m.cards.map(c => ({ ...c }))
      follow()
      return
    }
    if (!runId && (streaming.value || recovery)) return
  }
  // 广播可能比 POST 流里的 thread 事件先到：本地已有同样内容、还没 id 的那条就是它。
  const local = recent.find(x => !x.id && x.role === m.role && (x.text || '').trim() === (m.text || '').trim() && (m.role === 'assistant' || x.mine))
  if (local) {
    local.id = m.id || ''
    return
  }
  messages.value.push(normalize({ ...m, run_id: runId }))
  follow()
}

function onRemoteTurn({ thread_id: thread, run_id: runId, kind, data }) {
  if (!runId || ownRuns.has(runId)) return
  if (kind === 'card' && thread === GROUP && data?.status === 'pending') {
    alertHidden('群里有一张确认卡等人处理', data.title || '助手提出了一处修改，需要有人确认。', `card-${data.card_id}`)
  }
  if (thread !== threadId.value) return
  let reply = messages.value.find(x => x.role === 'assistant' && x.runId === runId)
  if (!reply) {
    if (kind === 'done' || kind === 'error') return
    messages.value.push({ key: ++seq, id: '', runId, remote: true, role: 'assistant', text: '', steps: [], cards: [], receipt: null, at: Date.now(), streaming: true, error: '', stopped: false })
    reply = messages.value[messages.value.length - 1]
  }
  if (kind === 'done') {
    reply.streaming = false
    setReceipt(reply, data)
    if (isEmptyReply(reply)) dropMessage(reply)
  } else if (kind === 'error') {
    reply.streaming = false
    reply.error = errorText(data?.message)
  } else if (!reply.id) {
    apply(reply, kind, data || {})
  }
  follow()
}

function onCardUpdate({ card_id: id, status: next, message, by }) {
  for (const m of messages.value) {
    for (const c of m.cards || []) {
      if (c.card_id !== id) continue
      if (next) c.status = next
      if (message != null) c.message = message
      if (by) c.by = by
    }
  }
}

// ---------------------------------------------------------------- 提醒（页面在后台时）

let audio = null
// 浏览器要求在人点过东西之后才能出声、才能问通知权限，所以在第一次发消息时准备。
function primeAlerts() {
  try {
    if (!audio && window.AudioContext) audio = new AudioContext()
  } catch {
    audio = null
  }
  if (!('Notification' in window) || Notification.permission !== 'default' || read(KEY.notify)) return
  write(KEY.notify, '1')
  try {
    Promise.resolve(Notification.requestPermission()).catch(() => {})
  } catch {}
}
function chime() {
  if (!audio) return
  try {
    audio.resume?.()
    const t = audio.currentTime
    const osc = audio.createOscillator()
    const gain = audio.createGain()
    osc.type = 'sine'
    osc.frequency.setValueAtTime(880, t)
    osc.frequency.exponentialRampToValueAtTime(660, t + 0.16)
    gain.gain.setValueAtTime(0.0001, t)
    gain.gain.exponentialRampToValueAtTime(0.05, t + 0.02)
    gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.24)
    osc.connect(gain).connect(audio.destination)
    osc.start(t)
    osc.stop(t + 0.26)
  } catch {}
}
function alertHidden(title, body, tag) {
  if (!document.hidden) return
  chime()
  if (!('Notification' in window) || Notification.permission !== 'granted') return
  try {
    const note = new Notification(title, { body: String(body).slice(0, 140), tag })
    note.onclick = () => {
      window.focus()
      collapsed.value = false
      switchMode('group')
      groupUnread.value = 0
      note.close()
    }
  } catch {}
}

// ---------------------------------------------------------------- 界面修改：刷新与撤销

const uiStale = ref('')
const uiHistory = ref({ items: [], can_undo: false })
const menuOpen = ref(false)
const menuBox = ref(null)
const undoing = ref(false)
const lastUiChange = computed(() => uiHistory.value.items?.[0] || null)
let reloadTimer = null

async function loadUiHistory() {
  try {
    const result = await agentApi.uiHistory()
    uiHistory.value = { items: Array.isArray(result?.items) ? result.items : [], can_undo: result?.can_undo === true }
  } catch {
    uiHistory.value = { items: [], can_undo: false }
  }
}
function toggleMenu() {
  menuOpen.value = !menuOpen.value
  historyOpen.value = false
  if (menuOpen.value) loadUiHistory()
}
// 人正在输入框里写东西时不能直接刷掉页面，改成给一个按钮让他自己决定。
const composing = () =>
  streaming.value || !!recovery || !!draft.value.trim() || attachments.value.length > 0 || document.activeElement === textarea.value
function onUiUpdated(data) {
  const summary = (data.summary || '').trim()
  uiHistory.value = { ...uiHistory.value, can_undo: data.can_undo === true }
  loadUiHistory()
  if (composing()) {
    uiStale.value = summary ? `界面已更新：${summary}` : '界面已更新'
    return
  }
  toast.success(summary ? `界面已更新：${summary}` : '界面已更新', { duration: 1500 })
  clearTimeout(reloadTimer)
  reloadTimer = setTimeout(() => window.location.reload(), 1500)
}
const reloadPage = () => window.location.reload()
function undoUi() {
  menuOpen.value = false
  const last = lastUiChange.value
  dialog.warning({
    title: '撤销上次界面修改',
    content: last?.summary ? `将撤回「${last.summary}」，界面恢复到这次修改之前的样子。` : '界面将恢复到上次修改之前的样子。',
    positiveText: '撤销',
    negativeText: '取消',
    onPositiveClick: async () => {
      undoing.value = true
      try {
        const result = await agentApi.uiUndo(displayName.value)
        const text = result?.message || '已撤销，界面马上刷新'
        if (['failed', 'error', 'rejected'].includes(result?.status)) toast.warning(text)
        else toast.success(text)
        loadUiHistory()
      } catch (e) {
        toast.error(errorText(e, '没有撤销成功，请稍后再试'))
      } finally {
        undoing.value = false
      }
    },
  })
}

// ---------------------------------------------------------------- 格式

// 时间一律按北京时间显示。用的人在国内，浏览器的时区却可能被设成别处，
// 同一段对话里历史和刚发的消息差出几个小时会让人以为记错了。
const TZ = 'Asia/Shanghai'
const dayFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' })
const dateFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: TZ, month: 'numeric', day: 'numeric' })
const timeFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: TZ, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
function toDate(value) {
  if (value == null || value === '') return null
  let d
  if (typeof value === 'number') d = new Date(value < 1e12 ? value * 1000 : value)
  // 不带时区的 ISO 串按北京时间理解，不交给浏览器按本地时区猜。
  else if (/^\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d(\.\d+)?)?$/.test(value)) d = new Date(`${value.replace(' ', 'T')}+08:00`)
  else d = new Date(value)
  return Number.isNaN(d.getTime()) ? null : d
}
function clock(value) {
  const d = toDate(value)
  if (!d) return ''
  const hm = timeFormat.format(d)
  return dayFormat.format(d) === dayFormat.format(Date.now()) ? hm : `${dateFormat.format(d)} ${hm}`
}
function duration(ms) {
  if (ms == null || Number.isNaN(Number(ms))) return ''
  const n = Number(ms)
  if (n < 1000) return `${Math.round(n)}ms`
  if (n < 60000) return `${(n / 1000).toFixed(1)}s`
  return `${Math.floor(n / 60000)}分${Math.round((n % 60000) / 1000)}秒`
}
// 每条回答底下只轻轻标一句「本次 ¥0.12」，模型、用量和用时放在悬停提示里。
function receipt(r) {
  return r.cost_cny != null ? `本次 ${yuan(r.cost_cny)}` : r.model || ''
}
function receiptTitle(r) {
  return [
    r.model,
    r.tokens != null ? `${Number(r.tokens).toLocaleString('zh-CN')} tokens` : '',
    r.ms ? `用时 ${duration(r.ms)}` : '',
  ].filter(Boolean).join(' · ')
}
const cellText = v => (v == null || v === '' ? '—' : String(v))

// ---------------------------------------------------------------- 键盘与焦点

function onEsc(e) {
  // 左边开着抽屉时，Esc 会顺着冒到 document 把抽屉关掉；在面板里按的 Esc 只管面板。
  e.stopPropagation()
  if (preview.value) preview.value = ''
  else if (menuOpen.value) menuOpen.value = false
  else if (mentionOpen.value) mentionOpen.value = false
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
  loadStatus().then(scheduleStatus)
  if (threadId.value) openThread(threadId.value)
  connectEvents()
  loadUiHistory()
  nextTick(autosize)
  if ('ResizeObserver' in window && list.value) {
    listObserver = new ResizeObserver(pinOnResize)
    listObserver.observe(list.value)
  }
})
// 换了称呼，在线名单里的名字也要跟着换。
watch(displayName, () => connectEvents())
onUnmounted(() => {
  cancelRecovery()
  controller?.abort()
  onAgentFailure(null)
  closeEvents()
  listObserver?.disconnect()
  clearTimeout(statusTimer)
  clearTimeout(reloadTimer)
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
      <b v-if="groupUnread" class="agent-unread" :aria-label="`群聊有 ${groupUnread} 条新消息`">{{ groupUnread > 99 ? '99+' : groupUnread }}</b>
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
          <span class="agent-status" :title="statusText"><i class="agent-dot" :class="{ on: status.online === true }" /><span class="agent-status-text">{{ statusText }}</span></span>
        </div>
        <div class="agent-actions">
          <CostChip compact placement="bottom-end" />
          <button v-if="!inGroup" type="button" class="agent-btn" :disabled="streaming" title="开一个新话题" aria-label="新话题" @click="newThread">
            <SquarePen :size="14" aria-hidden="true" /><span class="agent-btn-label">新话题</span>
          </button>
          <div v-if="!inGroup" ref="historyBox" class="agent-history">
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
          <div ref="menuBox" class="agent-history">
            <button type="button" class="agent-btn icon" :class="{ on: menuOpen }" title="更多" aria-label="更多" :aria-expanded="menuOpen" @click="toggleMenu">
              <Ellipsis :size="15" aria-hidden="true" />
            </button>
            <div v-if="menuOpen" class="agent-menu agent-more" role="menu">
              <button v-if="uiHistory.can_undo" type="button" class="agent-menu-item" role="menuitem" :disabled="undoing" @click="undoUi">
                <span class="agent-menu-line"><Undo2 :size="13" aria-hidden="true" />撤销上次界面修改</span>
                <small v-if="lastUiChange" class="truncate">{{ lastUiChange.summary }}{{ lastUiChange.at ? ` · ${clock(lastUiChange.at)}` : '' }}</small>
              </button>
              <button type="button" class="agent-menu-item" role="menuitem" @click="askName(null)">
                <span class="agent-menu-line"><SquarePen :size="13" aria-hidden="true" />修改称呼</span>
                <small>现在叫：{{ userName || '未填' }}</small>
              </button>
            </div>
          </div>
          <button type="button" class="agent-btn icon" title="收起助手" aria-label="收起助手" @click="collapsed = true">
            <PanelRightClose :size="15" aria-hidden="true" />
          </button>
        </div>
      </header>
      <div class="agent-topic">
        <div class="agent-mode" role="tablist" aria-label="对话类型">
          <button type="button" role="tab" :aria-selected="inGroup" :class="{ on: inGroup }" :disabled="streaming && !inGroup" :title="GROUP_TITLE" @click="switchMode('group')">
            群聊<b v-if="groupUnread && !inGroup" class="agent-unread">{{ groupUnread > 99 ? '99+' : groupUnread }}</b>
          </button>
          <button type="button" role="tab" :aria-selected="!inGroup" :class="{ on: !inGroup }" :disabled="streaming && inGroup" title="只有你自己能看到的对话" @click="switchMode('topic')">话题</button>
        </div>
        <div v-if="inGroup" class="agent-people" :title="online.length ? `在线：${online.join('、')}` : ''">
          <span v-for="name in online.slice(0, 5)" :key="name" class="agent-avatar" :style="{ background: tint(name) }" :aria-label="name">{{ initial(name) }}</span>
          <span v-if="online.length > 5" class="agent-avatar more">+{{ online.length - 5 }}</span>
          <small v-if="online.length">{{ online.length }} 人在线</small>
        </div>
        <span v-else-if="threadTitle && messages.length" class="agent-topic-title truncate" :title="threadTitle">{{ threadTitle }}</span>
      </div>

      <div class="agent-scroll">
        <div ref="list" class="agent-list" @scroll="onScroll">
          <div v-if="loadingThread" class="agent-loading"><span class="spin" /> 正在打开这段对话…</div>

          <section v-else-if="!messages.length && inGroup" class="agent-empty">
            <div class="agent-kicker"><i />{{ GROUP_TITLE }}</div>
            <h2>这里的话，大家都看得到。</h2>
            <p>直接提问，助手会回答；以 <b>@同事</b> 开头的话只发给同事，助手不插话，对方会收到提醒。助手提出的修改，群里任何人都可以确认。</p>
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
              <article v-if="m.role === 'user'" class="agent-user" :class="{ chat: inGroup, mine: inGroup && isMine(m) }">
                <header class="agent-meta">
                  <span class="agent-who" :style="inGroup && !isMine(m) ? { background: tint(m.user || '同事') } : null">{{ !inGroup || isMine(m) ? '你' : m.user || '同事' }}</span>
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
                    <component
                      :is="s.detail ? 'button' : 'div'"
                      :type="s.detail ? 'button' : undefined"
                      class="agent-step-head"
                      :class="{ expandable: !!s.detail }"
                      :aria-expanded="s.detail ? isOpen(m, s) : undefined"
                      @click="s.detail && toggleStep(m, s)"
                    >
                      <ChevronRight v-if="s.detail" class="agent-caret" :class="{ open: isOpen(m, s) }" :size="12" aria-hidden="true" />
                      <span v-else class="agent-caret-space" aria-hidden="true" />
                      <span class="agent-step-label">{{ s.label }}</span>
                      <LoaderCircle v-if="stepState(m, s) === 'running'" class="agent-spinner" :size="13" aria-label="进行中" />
                      <Check v-else-if="s.status === 'done'" class="agent-ok" :size="13" aria-label="完成" />
                      <CircleX v-else-if="s.status === 'error'" class="agent-err" :size="13" aria-label="没成功" />
                      <span class="agent-step-ms num">{{ duration(s.ms) }}</span>
                    </component>
                    <p v-if="s.detail && isOpen(m, s)" class="agent-step-detail">{{ s.detail }}</p>
                  </div>
                </div>

                <div v-if="m.streaming && !m.text && !m.steps.length" class="agent-thinking">
                  <span class="agent-dots"><i /><i /><i /></span>{{ m.remote ? '助手正在回答…' : '正在看你说的情况…' }}
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
                  <p v-if="cardBy(c)" class="agent-card-by">{{ cardBy(c) }}</p>
                </section>

                <p v-if="m.recovering" class="agent-recovering"><span class="spin" /> 连接中断，正在取回结果…</p>
                <p v-if="m.error" class="agent-error">
                  <CircleAlert :size="14" aria-hidden="true" />
                  <span>{{ m.error }}</span>
                  <button v-if="m.retry && m === lastReply && !streaming" type="button" class="link" @click="retry(m)">重试</button>
                </p>
                <p v-if="m.stopped" class="agent-stopped">已停止。查到的内容都留着，可以接着说。</p>
                <p v-if="m.receipt && receipt(m.receipt)" class="agent-receipt num" :title="receiptTitle(m.receipt)">{{ receipt(m.receipt) }}</p>
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
        <p v-if="uiStale" class="agent-notice agent-stale" role="status">
          <span>{{ uiStale }}</span>
          <button type="button" class="primary tiny" @click="reloadPage"><RotateCw :size="12" aria-hidden="true" />刷新以查看新界面</button>
        </p>
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
            :placeholder="inGroup ? '直接提问助手会回答；以 @同事 开头只发给同事' : '描述你发现的问题，可粘贴截图…'"
            :aria-label="inGroup ? '发到公共群聊的消息' : '给台账助手的消息'"
            @keydown="onKeydown"
            @paste="onPaste"
          />
        </div>
        <div class="agent-toolbar">
          <button type="button" class="agent-tool" title="附上图片" @click="fileInput?.click()"><Paperclip :size="13" aria-hidden="true" />附件</button>
          <button type="button" class="agent-tool" :class="{ on: shotTip }" title="怎么截图" @click="shotTip = !shotTip"><Camera :size="13" aria-hidden="true" />截图</button>
          <div v-if="inGroup" ref="mentionBox" class="agent-mention">
            <button type="button" class="agent-tool" :class="{ on: mentionOpen || toColleague(draft) }" title="@一位在线同事，这条只发给同事" :aria-expanded="mentionOpen" @click="mentionOpen = !mentionOpen"><AtSign :size="13" aria-hidden="true" />同事</button>
            <div v-if="mentionOpen" class="agent-menu agent-mention-menu" role="menu">
              <div class="agent-menu-head">只发给同事，助手不回答</div>
              <div v-if="!colleagues.length" class="agent-menu-note">现在没有其他同事在线，也可以直接输入「@称呼」。</div>
              <div class="agent-menu-list">
                <button v-for="name in colleagues" :key="name" type="button" class="agent-menu-item agent-mention-item" role="menuitem" @click="mention(name)">
                  <span class="agent-avatar" :style="{ background: tint(name) }">{{ initial(name) }}</span>
                  <span class="truncate">{{ name }}</span>
                </button>
              </div>
            </div>
          </div>
          <span class="agent-hint">{{ inGroup ? '' : 'Enter 发送 · Shift+Enter 换行' }}</span>
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
.agent-actions :deep(.cost-chip) { margin-right: 4px; }
/* 面板拖窄时头部放不下费用标签，「新话题」只留图标。 */
.agent-head { container-type: inline-size; }
@container (max-width: 380px) {
  .agent-btn-label, .agent-status-text, .agent-actions :deep(.cost-chip svg) { display: none; }
}
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
.agent-mention { position: relative; display: flex; }
.agent-menu.agent-mention-menu { top: auto; right: auto; bottom: 28px; left: -8px; width: 220px; }
.agent-menu-item.agent-mention-item,
.agent-menu-item.agent-mention-item:hover:not(:disabled) { flex-direction: row; align-items: center; gap: 8px; }
.agent-mention-item .agent-avatar { margin-left: 0; border: 0; }
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
.agent-more { right: 0; width: 240px; }
.agent-menu-line { display: inline-flex; align-items: center; gap: 6px; }
.agent-menu-item small.truncate { display: block; }
.agent-topic {
  display: flex;
  align-items: center;
  gap: var(--s3);
  min-height: 38px;
  padding: 5px var(--s5);
  border-bottom: 1px solid var(--n2);
  font-size: var(--t-xs);
  color: var(--n6);
}
.agent-topic-title { flex: 1; min-width: 0; text-align: right; }
.agent-mode { display: inline-flex; flex: none; padding: 2px; border-radius: var(--r-sm); background: var(--n2); }
.agent-mode button,
.agent-mode button:hover:not(:disabled) {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 24px;
  padding: 0 10px;
  border: 0;
  border-radius: 4px;
  background: none;
  color: var(--n6);
  font: 500 var(--t-xs)/1 var(--sans);
}
.agent-mode button:hover:not(:disabled) { color: var(--n9); }
.agent-mode button.on { background: var(--n0); color: var(--n9); box-shadow: 0 1px 2px rgba(15, 23, 42, .08); }
.agent-unread {
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 8px;
  background: var(--bad, #d14343);
  color: #fff;
  font: 600 10px/16px var(--sans);
  text-align: center;
}
.agent-tab .agent-unread { writing-mode: horizontal-tb; }
.agent-people { display: flex; align-items: center; justify-content: flex-end; flex: 1; min-width: 0; }
.agent-avatar {
  display: inline-grid;
  place-items: center;
  width: 22px;
  height: 22px;
  margin-left: -5px;
  border: 2px solid var(--n0);
  border-radius: 50%;
  color: #fff;
  font: 600 10.5px/1 var(--sans);
}
.agent-avatar:first-child { margin-left: 0; }
.agent-avatar.more { background: var(--n4); color: var(--n8); }
.agent-people small { margin-left: 8px; font-size: 11px; color: var(--n5); white-space: nowrap; }

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

/* 群聊：别人说的话靠左、标上名字；自己说的靠右、用强调色。 */
.agent-user.chat { padding-top: 14px; }
.agent-user.chat + .agent-user.chat { border-top: 0; }
.agent-user.chat .agent-user-text { font-size: var(--t-md); font-weight: 500; line-height: 1.6; }
.agent-user.mine { text-align: right; }
.agent-user.mine .agent-meta { flex-direction: row-reverse; }
.agent-user.mine .agent-who { background: var(--accent); }
.agent-user.mine .agent-thumbs { justify-content: flex-end; }
.agent-user.mine .agent-user-text {
  display: inline-block;
  max-width: 92%;
  padding: 7px 11px;
  border-radius: 10px 10px 2px 10px;
  background: var(--accent-bg);
  text-align: left;
}

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
.agent-caret.open { transform: rotate(90deg); }
.agent-caret-space { flex: none; width: 12px; }
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
/* 数字折成两行就没法对着看了：单元格不折行，表格放不下时在自己的框里横着滚。 */
.agent-md :deep(.md-table) { max-width: 100%; margin: 0 0 .75em; overflow-x: auto; overscroll-behavior-x: contain; }
.agent-md :deep(table) { width: auto; min-width: 60%; font-variant-numeric: tabular-nums; }
.agent-md :deep(th) { padding: 4px 14px 6px 0; font-size: 11.5px; white-space: nowrap; }
.agent-md :deep(td) { padding: 6px 14px 6px 0; font-size: var(--t-sm); line-height: 1.5; white-space: nowrap; }
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
.agent-card-table .before { color: var(--n6); white-space: nowrap; }
.agent-card-table .after { color: var(--n9); font-weight: 600; white-space: nowrap; }
.agent-card-table .arrow { width: 14px; color: var(--n5); }
.agent-card-actions { display: flex; align-items: center; gap: var(--s2); margin-top: 12px; }
.agent-card-actions .spin { width: 10px; height: 10px; border-width: 1.5px; border-color: rgba(255,255,255,.4); border-top-color: #fff; }
.agent-card-message { margin-top: 8px; font-size: var(--t-xs); color: var(--n7); }
.agent-card-by { margin-top: 6px; font-size: 11px; color: var(--n5); }
.agent-card[data-status='failed'] .agent-card-message { color: var(--bad); }

.agent-error { display: flex; align-items: flex-start; gap: 6px; margin-top: 10px; font-size: var(--t-sm); color: var(--bad); line-height: 1.6; }
.agent-error svg { flex: none; margin-top: 3px; }
.agent-error .link { flex: none; font-size: var(--t-sm); }
.agent-stopped { margin-top: 10px; font-size: var(--t-xs); color: var(--n6); }
.agent-recovering { display: flex; align-items: center; gap: 8px; margin-top: 10px; font-size: var(--t-sm); color: var(--n6); }
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
.agent-notice.agent-stale { align-items: center; background: var(--accent-bg); color: var(--n8); }
.agent-stale .tiny { display: inline-flex; align-items: center; gap: 4px; flex: none; }
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
