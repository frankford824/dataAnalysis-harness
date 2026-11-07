<script setup>
/* 右边的助手：一段一段的会话，大家都能看到。左边打开的页面当背景带上，谁来办由后台定。
 */
import { ArrowUp, ArrowUpRight, Camera, Check, ChevronRight, CircleAlert, CircleX, CornerDownRight, Ellipsis, File, FileArchive, FileSpreadsheet, FileText, History, LoaderCircle, MessageSquare, PanelRightClose, Paperclip, Pencil, Presentation, Reply, RotateCw, Search, Square, SquarePen, Trash2, Undo2, X } from '@lucide/vue'
import { useDialog, useMessage } from 'naive-ui'
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch, watchEffect } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { agentApi, errorText, onAgentFailure, openEvents, streamChat, uploadFile } from '../agentClient'
import { setCosts, setCostsLive, yuan } from '../agentCosts'
import { renderMarkdown } from '../agentMarkdown'
import { useApp } from '../store'
import CostChip from './CostChip.vue'

const KEY = { width: 'ledger.agent.width', collapsed: 'ledger.agent.collapsed', user: 'ledger.agent.user', thread: 'ledger.agent.session', thinking: 'ledger.agent.thinking', notify: 'ledger.agent.notifyAsked' }
const MIN_W = 320
const MAX_W = 720
const DEFAULT_W = 400
const TAB_W = 36
const MAX_FILES = 10
const MAX_FILE_BYTES = 50 * 1024 * 1024
const FILE_HINT = '可以发截图、Excel（xlsx/xls/csv/WPS表格）、Word（docx/doc/rtf/WPS文字）、PDF、PPT'
const ACCEPT = 'image/*,.xlsx,.xlsm,.xls,.xlsb,.csv,.tsv,.et,.ett,.ods,.docx,.doc,.rtf,.wps,.wpt,.odt,.pdf,.pptx,.ppt,.pps,.ppsx,.dps,.odp,.txt,.zip'
const SUGGESTIONS = ['这家店本月利润为什么偏低？', '这笔退款金额不对，帮我查一下', '本月还有哪些店没结账？']
const isMac = typeof navigator !== 'undefined' && /Mac/i.test(navigator.platform || '')

const read = key => { try { return localStorage.getItem(key) } catch { return null } }
const write = (key, value) => { try { value == null ? localStorage.removeItem(key) : localStorage.setItem(key, String(value)) } catch {} }

const app = useApp()
const route = useRoute()
const router = useRouter()
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

// ---------------------------------------------------------------- 会话

// 会话由人自己开，不跟着页面换。id 在这边生成，第一句还没回来也能切走再切回。
function newId() {
  const bytes = new Uint8Array(6)
  crypto.getRandomValues(bytes)
  return [...bytes].map(b => b.toString(16).padStart(2, '0')).join('')
}
const threadId = ref(read(KEY.thread) || newId())
const threadTitle = ref('')
const messages = ref([])
const loadingThread = ref(false)
const historyOpen = ref(false)
const historyBox = ref(null)
const threads = ref([])
const threadsLoading = ref(false)
const threadsError = ref('')
const sessionQuery = ref('')
const renaming = ref('')
const renameDraft = ref('')
const removed = ref(null)
watch(threadId, value => write(KEY.thread, value))
const THINKING = [['off', '思考：关'], ['low', '思考：低'], ['medium', '思考：中'], ['high', '思考：高']]
const thinking = ref(THINKING.some(([v]) => v === read(KEY.thinking)) ? read(KEY.thinking) : 'off')
watch(thinking, value => write(KEY.thinking, value))

function normalize(m) {
  return {
    key: ++seq,
    id: m.id || '',
    runId: m.run_id || '',
    role: m.role === 'user' ? 'user' : 'assistant',
    user: m.user || '',
    bot: m.bot || m.receipt?.bot || '',
    questionId: m.receipt?.question_id || '',
    text: m.text || '',
    images: Array.isArray(m.images) ? m.images : [],
    files: Array.isArray(m.files) ? m.files : [],
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
  if (!id) return
  detach()
  historyOpen.value = false
  loadingThread.value = true
  openSteps.value = new Set()
  threadId.value = id
  try {
    const thread = await agentApi.thread(id)
    if (threadId.value !== id) return
    threadTitle.value = thread?.title || ''
    messages.value = (thread?.messages || []).map(normalize)
  } catch (e) {
    if (threadId.value !== id) return
    // 还没说过话的新会话服务端没有，当作空的。
    threadTitle.value = ''
    messages.value = []
    if (e.status !== 404) notice.value = errorText(e, '这段对话没打开，请稍后再试')
  } finally {
    if (threadId.value === id) {
      loadingThread.value = false
      follow(true)
    }
  }
}
function newThread() {
  detach()
  threadId.value = newId()
  threadTitle.value = ''
  messages.value = []
  openSteps.value = new Set()
  historyOpen.value = false
  loadingThread.value = false
  nextTick(() => textarea.value?.focus())
}
// 正在等回答时切到别的会话：这边不再接这条流，后台照样办完，切回来从服务端取。
function detach() {
  const job = activeJob
  if (!job) return
  job.detached = true
  if (liveRun.value) ownRuns.delete(liveRun.value)
  activeJob = null
  streaming.value = false
  liveRun.value = ''
  cancelRecovery()
  controller?.abort()
  controller = null
}

let searchTimer = null
async function loadThreads() {
  threadsLoading.value = true
  threadsError.value = ''
  const query = sessionQuery.value.trim()
  try {
    const list = (await agentApi.threads(query))?.threads || []
    if (query === sessionQuery.value.trim()) threads.value = list
  } catch (e) {
    threadsError.value = errorText(e, '会话列表没取到，请稍后再试')
  } finally {
    threadsLoading.value = false
  }
}
watch(sessionQuery, () => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(loadThreads, 250)
})
function toggleHistory() {
  historyOpen.value = !historyOpen.value
  menuOpen.value = false
  renaming.value = ''
  if (historyOpen.value) loadThreads()
}
const runningThreads = computed(() => new Set(Object.values(tasks.value).filter(t => RUNNING.has(t.status)).map(t => t.thread_id)))

const renameInput = ref(null)
function startRename(t) {
  renaming.value = t.id
  renameDraft.value = t.title || ''
  nextTick(() => {
    const el = Array.isArray(renameInput.value) ? renameInput.value[0] : renameInput.value
    el?.focus()
    el?.select()
  })
}
async function saveRename(t) {
  const title = renameDraft.value.trim()
  renaming.value = ''
  if (!title || title === t.title) return
  try {
    const result = await agentApi.renameThread(t.id, title, displayName.value)
    t.title = result?.title || title
    if (t.id === threadId.value) threadTitle.value = t.title
  } catch (e) {
    toast.warning(errorText(e, '没改成，请稍后再试'))
  }
}
let removedTimer = null
async function removeThread(t) {
  try {
    await agentApi.deleteThread(t.id, displayName.value)
  } catch (e) {
    toast.warning(errorText(e, '没删掉，请稍后再试'))
    return
  }
  threads.value = threads.value.filter(x => x.id !== t.id)
  removed.value = t
  clearTimeout(removedTimer)
  removedTimer = setTimeout(() => { removed.value = null }, 8000)
  if (t.id === threadId.value) {
    newThread()
    historyOpen.value = true
  }
}
async function undoRemove() {
  const t = removed.value
  if (!t) return
  removed.value = null
  clearTimeout(removedTimer)
  try {
    await agentApi.restoreThread(t.id, displayName.value)
    await loadThreads()
  } catch (e) {
    toast.warning(errorText(e, '没恢复成，请稍后再试'))
  }
}
function onPointerDown(e) {
  if (historyOpen.value && historyBox.value && !historyBox.value.contains(e.target)) historyOpen.value = false
  if (tasksOpen.value && taskBox.value && !taskBox.value.contains(e.target)) tasksOpen.value = false
  if (menuOpen.value && menuBox.value && !menuBox.value.contains(e.target)) menuOpen.value = false
}

// ---------------------------------------------------------------- 头像

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
// 会话大家都能看到，总得有个名字；没填称呼的就叫「同事」。
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
// 左边的页面默认一起带上；点掉以后，换到别的页面再自动带上。
const pageOff = ref(false)
const pageLabel = ref('')
const pageHeading = () => ((document.querySelector('.dock-main main h1') || document.querySelector('.dock-main h1'))?.textContent || '').replace(/\s+/g, ' ').trim()
let labelTimer = null
function readPageLabel() {
  const period = String(route.query.period || app.period || '')
  const name = route.name === 'period' ? app.currentStore?.name || pageHeading() || '这家店' : pageHeading() || document.title || '当前页面'
  pageLabel.value = [name.slice(0, 40), period && !name.includes(period) ? period : ''].filter(Boolean).join(' · ')
}
watch(() => route.fullPath, () => {
  pageOff.value = false
  readPageLabel()
  clearTimeout(labelTimer)
  labelTimer = setTimeout(readPageLabel, 600)
})
watch(() => app.currentStore?.name, readPageLabel)
function pageContext() {
  if (pageOff.value) return picked.value ? { selection: picked.value } : null
  const h1 = document.querySelector('.dock-main main h1') || document.querySelector('.dock-main h1')
  return {
    path: route.fullPath,
    name: route.name == null ? null : String(route.name),
    title: document.title,
    query: { ...route.query },
    params: { ...route.params },
    heading: (h1?.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 200),
    selection: picked.value,
    room: route.meta.room || (route.name === 'period' ? 'store' : 'month'),
    period: String(route.query.period || app.period || ''),
    store_id: route.name === 'period' ? String(route.params.id || '') : '',
  }
}

// ---------------------------------------------------------------- 附件

// 选中、粘贴、拖进来的文件马上开始传，传完拿到 file_id，发送时只带 id。
const attachments = ref([])
const fileInput = ref(null)
const notice = ref('')
const dragDepth = ref(0)
const preview = ref('')

const extOf = name => (/\.([a-z0-9]{1,8})$/i.exec(name || '')?.[1] || '').toLowerCase()
const IMAGE_EXT = /^(png|jpe?g|gif|webp|bmp)$/
const DOC_TYPES = [
  { key: 'sheet', label: '表格', icon: FileSpreadsheet, ext: /^(xlsx|xlsm|xltx|xls|xlsb|csv|tsv|et|ett|ods|numbers)$/ },
  { key: 'word', label: '文档', icon: FileText, ext: /^(docx|docm|doc|dot|rtf|wps|wpt|odt|pages|txt|md)$/ },
  { key: 'pdf', label: 'PDF', icon: FileText, ext: /^pdf$/ },
  { key: 'slides', label: '演示', icon: Presentation, ext: /^(pptx|pptm|ppsx|ppt|pps|pot|dps|dpt|odp|key)$/ },
  { key: 'zip', label: '压缩包', icon: FileArchive, ext: /^(zip|rar|7z)$/ },
]
const OTHER_DOC = { key: 'other', label: '文件', icon: File }
const docType = name => DOC_TYPES.find(t => t.ext.test(extOf(name))) || OTHER_DOC
function fileSize(n) {
  const v = Number(n) || 0
  if (v < 1024) return `${v} B`
  if (v < 1024 * 1024) return `${Math.round(v / 1024)} KB`
  return `${(v / 1024 / 1024).toFixed(v < 10 * 1024 * 1024 ? 1 : 0)} MB`
}
// 粘贴的截图名字都叫 image.png，换成带时间的，聊天里好认。
function fileName(file) {
  const name = file.name || ''
  if (name && !/^image\.\w+$/i.test(name)) return name
  const d = new Date()
  const pad = n => String(n).padStart(2, '0')
  return `截图-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}.${extOf(name) || (file.type || '').split('/')[1] || 'png'}`
}
function addFiles(list) {
  for (const file of [...(list || [])]) {
    if (attachments.value.length >= MAX_FILES) {
      notice.value = `一条消息最多带 ${MAX_FILES} 个附件`
      break
    }
    if (file.size > MAX_FILE_BYTES) {
      notice.value = `「${file.name || '这个文件'}」超过 50MB，发不了`
      continue
    }
    if (!file.size) {
      notice.value = `「${file.name || '这个文件'}」是空的`
      continue
    }
    const name = fileName(file)
    const image = file.type?.startsWith('image/') || IMAGE_EXT.test(extOf(name))
    attachments.value.push({ id: ++seq, file, name, size: file.size, kind: image ? 'image' : 'file', thumb: image ? URL.createObjectURL(file) : '', state: 'uploading', progress: 0, fileId: '', url: '', error: '' })
    upload(attachments.value[attachments.value.length - 1])
  }
}
function upload(item) {
  Object.assign(item, { state: 'uploading', progress: 0, error: '' })
  const job = uploadFile(item.file, item.name, p => { item.progress = p })
  item.abort = job.abort
  job.promise
    .then(r => Object.assign(item, { state: 'done', fileId: r.file_id, url: r.url || '', kind: r.kind || item.kind, size: r.size ?? item.size }))
    .catch(e => {
      if (e?.name !== 'AbortError') Object.assign(item, { state: 'error', error: errorText(e, '没传上') })
    })
}
function removeAttachment(item) {
  if (item.state === 'uploading') item.abort?.()
  if (item.thumb) URL.revokeObjectURL(item.thumb)
  attachments.value = attachments.value.filter(a => a.id !== item.id)
}
// 发出去之后输入框上方清空；缩略图换成服务端地址，本地的预览可以放掉了。
function takeAttachments() {
  const ready = attachments.value.filter(a => a.state === 'done')
  for (const a of attachments.value) if (a.thumb) URL.revokeObjectURL(a.thumb)
  attachments.value = []
  return ready.map(a => ({ file_id: a.fileId, name: a.name, size: a.size, kind: a.kind, url: a.url }))
}
const fileRefs = files => files.map(({ file_id, name }) => ({ file_id, name }))
function onPick(e) {
  addFiles(e.target.files)
  e.target.value = ''
}
function onPaste(e) {
  const data = e.clipboardData
  if (!data) return
  let files = [...(data.files || [])]
  if (!files.length) {
    files = [...(data.items || [])]
      .filter(item => item.kind === 'file')
      .map(item => item.getAsFile())
      .filter(Boolean)
  }
  if (!files.length) return
  // 从 Excel 复制单元格时剪贴板里同时有文字和一张图，这时候人要的是文字。
  if (data.getData('text/plain').trim() && files.every(f => f.type?.startsWith('image/'))) return
  e.preventDefault()
  addFiles(files)
}

// App.vue 在 window 上接管了拖放（拖进来的文件当台账上传）。落在面板上的文件是
// 发给助手的附件，所以这里一律截住，不让它冒泡上去。拖的是文字时不拦默认行为，方便把左边
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
let running = null
let activeJob = null
const liveRun = ref('')
// 自己这个页面发起的轮次和消息。广播回来时据此去重，不在列表里再出现一遍。
const ownRuns = reactive(new Set())
const ownMessages = new Set()
const preparing = computed(() => attachments.value.some(a => a.state === 'uploading'))
const uploadFailed = computed(() => attachments.value.some(a => a.state === 'error'))
const hasContent = computed(() => !!draft.value.trim() || attachments.value.some(a => a.state === 'done'))
const canSend = computed(() => !streaming.value && !preparing.value && !uploadFailed.value && hasContent.value)
const lastReply = computed(() => [...messages.value].reverse().find(m => m.role === 'assistant' && !m.remote))

function send(preset) {
  if (preparing.value || uploadFailed.value) return
  const fromDraft = typeof preset !== 'string'
  const text = (fromDraft ? draft.value : preset).trim()
  if (fromDraft && steerRun.value) return steer()
  if (streaming.value) return
  if (!text && !(fromDraft && hasContent.value)) return
  if (userName.value === null) return askName({ preset })
  if (fromDraft) draft.value = ''
  const files = fromDraft ? takeAttachments() : []
  shotTip.value = false
  primeAlerts()
  running = run({ text, files, page: pageContext(), thread: threadId.value })
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

function splitReply(reply, bot) {
  if (!bot) return reply
  if (!reply.bot) {
    reply.bot = bot
    return reply
  }
  if (reply.bot === bot) return reply
  if (!reply.text && !reply.steps.length && !reply.cards.length) {
    reply.bot = bot
    return reply
  }
  reply.streaming = false
  const next = { key: ++seq, id: '', role: 'assistant', bot, text: '', steps: [], cards: [], receipt: null, at: Date.now(), streaming: true, error: '', stopped: false, runId: reply.runId || '', remote: !!reply.remote }
  messages.value.push(next)
  return messages.value[messages.value.length - 1]
}
function apply(reply, event, data) {
  if (event === 'thread') {
    if (data.title) threadTitle.value = data.title
  } else if (event === 'step') {
    const step = reply.steps.find(s => s.id === data.id)
    if (step) Object.assign(step, data)
    else reply.steps.push({ ...data })
  } else if (event === 'delta') {
    reply.text += data.text || ''
  } else if (event === 'replace') {
    reply.text = data.text || ''
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
  const job = { detached: false }
  activeJob = job
  messages.value.push({ key: ++seq, id: '', role: 'user', user: userName.value || '', mine: true, text: payload.text, images: [], files: payload.files, at })
  const ask = messages.value[messages.value.length - 1]
  messages.value.push({ key: ++seq, id: '', role: 'assistant', text: '', steps: [], cards: [], receipt: null, at, streaming: true, error: '', stopped: false, recovering: false, retry: payload, ask })
  let reply = messages.value[messages.value.length - 1]
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
      { thread_id: payload.thread, text: payload.text, files: fileRefs(payload.files), page: payload.page, user: userName.value || '', thinking: thinking.value, ...(payload.trigger ? { trigger: payload.trigger } : {}), ...(payload.subject ? { subject: payload.subject } : {}) },
      {
        signal: controller.signal,
        onEvent: (event, raw) => {
          const data = raw || {}
          if (event === 'bot') reply = splitReply(reply, data.bot)
          if (event === 'thread') {
            turnThread = data.thread_id || payload.thread
            reply.retry = null
            if (data.run_id) {
              liveRun.value = data.run_id
              ownRuns.add(data.run_id)
              reply.runId = data.run_id
              // 公共事件流比这条流先到时，自己这一轮已经被当成别人的挂了一条，去掉它。
              const early = messages.value.find(x => x.remote && x.runId === data.run_id)
              if (early) dropMessage(early)
            }
            if (data.message_id) {
              ownMessages.add(String(data.message_id))
              ask.id = data.message_id
            }
          }
          // 别人已经在查差不多的问题，这句并进那一轮：那一轮的过程和回答照常看别人的。
          if (event === 'merged') {
            ownRuns.delete(data.run_id)
            liveRun.value = ''
            reply.runId = ''
            ask.merged = { runId: data.run_id || '', asker: data.asker || '同事' }
          }
          if (event === 'done' || event === 'error') finished = true
          apply(reply, event, event === 'done' ? raw : data)
          follow()
        },
      },
    )
    if (!finished) throw Object.assign(new Error('网络中断'), { network: true })
    reply.streaming = false
    if (isEmptyReply(reply)) dropMessage(reply)
    else alertHidden('助手回答好了', cut(reply.text, 60) || '回答好了，回来看看。', `done-${reply.runId || at}`)
  } catch (e) {
    reply.streaming = false
    if (job.detached) return
    if (e?.name === 'AbortError') reply.stopped = true
    else if (turnThread) await recover(reply, turnThread, payload, at)
    else {
      reply.error = e?.network ? '网络中断，这条没发出去。稍等一会儿再点「重试」。' : errorText(e)
    }
  } finally {
    reply.streaming = false
    if (activeJob === job) {
      activeJob = null
      streaming.value = false
      controller = null
      liveRun.value = ''
      follow()
    }
    if (historyOpen.value) loadThreads()
  }
}

// ---------------------------------------------------------------- 补充：不打断正在查的那一轮

// 这段会话正在办的时候，发送键变成「补充」，接进那一轮。
const steerTarget = ref('')
const myTask = computed(() => {
  const mine = Object.values(tasks.value).filter(t => RUNNING.has(t.status) && t.thread_id && t.thread_id === threadId.value)
  return mine.find(t => t.run_id === steerTarget.value) || mine.sort((a, b) => String(b.started_at).localeCompare(String(a.started_at)))[0] || null
})
const steerRun = computed(() => myTask.value?.run_id || liveRun.value)
const steering = ref(false)
const canSteer = computed(() => !steering.value && !preparing.value && !uploadFailed.value && hasContent.value)

async function steer() {
  if (!canSteer.value) return
  if (userName.value === null) return askName({})
  const text = draft.value.trim()
  const runId = steerRun.value
  const thread = threadId.value
  const files = takeAttachments()
  draft.value = ''
  // 补充的话放在那一轮回答的前面：回答是对两句话一起的。
  const note = { key: ++seq, id: '', role: 'user', user: userName.value || '', mine: true, text, images: [], files, at: Date.now() }
  const live = messages.value.findIndex(m => m.role === 'assistant' && m.runId === runId && m.streaming)
  if (live >= 0) messages.value.splice(live, 0, note)
  else messages.value.push(note)
  follow(true)
  steering.value = true
  try {
    const result = await agentApi.steer({ run_id: runId, thread_id: thread, text, files: fileRefs(files), user: userName.value || '' })
    if (result?.status === 'queued') return
    if (result?.stored) {
      notice.value = '刚才那一轮已经结束，这句补充记在对话里了；还要接着查的话再问一次。'
      return
    }
    dropMessage(note)
    await running
    running = run({ text, files, page: pageContext(), thread })
  } catch (e) {
    dropMessage(note)
    if (!draft.value) draft.value = text
    if (!attachments.value.length) attachments.value = files.map(f => ({ id: ++seq, file: null, name: f.name, size: f.size, kind: f.kind, thumb: '', state: 'done', progress: 1, fileId: f.file_id, url: f.url, error: '' }))
    notice.value = errorText(e, '补充没发出去，请再试一次')
  } finally {
    steering.value = false
    steerTarget.value = ''
  }
}
function startSteer(task) {
  steerTarget.value = task.run_id
  nextTick(() => textarea.value?.focus())
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
        if (thread?.title) threadTitle.value = thread.title
        follow(true)
      }
      return
    }
    if (!job.cancelled) {
      reply.recovering = false
      reply.error = '这次的结果还没取回来。过一会儿从「会话列表」重新打开这段对话看看。'
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
  if (!streaming.value && !recovery && myTask.value) return stopTask(myTask.value)
  const id = recovery?.id || threadId.value
  if (id) agentApi.stop(id, liveRun.value || recovery?.runId, displayName.value).catch(() => {})
  cancelRecovery()
  controller?.abort()
}

function retry(reply) {
  if (streaming.value || !reply.retry) return
  if (reply.ask) dropMessage(reply.ask)
  dropMessage(reply)
  running = run(reply.retry)
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
// 查的过程只露一行：正在做的那一步。查完收成一行摘要，点开才看每一步。没有步骤的闲聊什么都不显示。
const openTraces = ref(new Set())
const traceOpen = m => openTraces.value.has(m.key)
function toggleTrace(m) {
  if (openTraces.value.has(m.key)) openTraces.value.delete(m.key)
  else openTraces.value.add(m.key)
}
function nowStep(m) {
  if (!m.streaming || !m.steps.length) return null
  return [...m.steps].reverse().find(s => s.status === 'running') || (m.text ? null : m.steps[m.steps.length - 1])
}
function traceSummary(m) {
  const r = m.receipt || {}
  return [`查了 ${m.steps.length} 步`, r.ms ? `用时 ${seconds(r.ms)}` : '', r.cost_cny != null ? yuan(r.cost_cny) : '', m.stopped ? '已停止' : '']
    .filter(Boolean).join(' · ')
}

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

// ---------------------------------------------------------------- 任务：每个问题是一个任务

// 任务池是服务端给的，所有人的都在里面；这里只看当前会话的和自己提的。
const tasks = ref({})
const ACTIVE = new Set(['queued', 'running', 'waiting'])
const RUNNING = new Set(['queued', 'running'])
const TASK_STATUS = { queued: '排队中', running: '在查', waiting: '等确认', done: '已完成', stopped: '已停止', failed: '没查完' }
const isMineTask = t => ownRuns.has(t.run_id) || isSelf(t.user)
const visibleTasks = computed(() => Object.values(tasks.value).filter(t => t.thread_id === threadId.value || isMineTask(t)))
const activeTasks = computed(() => visibleTasks.value.filter(t => ACTIVE.has(t.status)).sort((a, b) => String(a.started_at).localeCompare(String(b.started_at))))
const taskOf = runId => (runId && tasks.value[runId]) || null
const taskByMessage = computed(() => new Map(visibleTasks.value.filter(t => t.message_id != null).map(t => [String(t.message_id), t])))
const tasksOpen = ref(false)
const taskBox = ref(null)
watch(() => activeTasks.value.length, n => { if (!n) tasksOpen.value = false })

function setTasks(list) {
  if (!Array.isArray(list)) return
  tasks.value = Object.fromEntries(list.filter(t => t?.run_id).map(t => [t.run_id, t]))
}
function putTask(t) {
  if (!t?.run_id) return
  tasks.value = { ...tasks.value, [t.run_id]: { ...(tasks.value[t.run_id] || {}), ...t } }
}
// 在跑的任务用时每秒走一次；没有在跑的就不开计时器。
const now = ref(Date.now())
let tick = null
watch(() => activeTasks.value.some(t => RUNNING.has(t.status)), on => {
  clearInterval(tick)
  tick = on ? setInterval(() => { now.value = Date.now() }, 1000) : null
}, { immediate: true })
function taskTime(t) {
  if (!ACTIVE.has(t.status)) return t.ms ? seconds(t.ms) : ''
  const started = toDate(t.started_at)
  return started ? seconds(Math.max(0, now.value - started.getTime())) : ''
}
const taskWho = t => (isMineTask(t) ? '你' : t.user || '同事')
async function stopTask(t) {
  if (t.stopping) return
  if (t.run_id === liveRun.value && streaming.value) return stop()
  putTask({ run_id: t.run_id, stopping: true })
  try {
    await agentApi.stop(t.thread_id, t.run_id, displayName.value)
  } catch (e) {
    toast.warning(errorText(e, '没有停下来，请稍后再试'))
  } finally {
    putTask({ run_id: t.run_id, stopping: false })
  }
}
// 提问底下挂的小卡片：回答没在跟着显示时（别人问的、切走又切回来的），才挂出进度；查完了看回答本身就够了。
function taskCard(m) {
  if (m.role !== 'user' || m.id === '' || m.id == null) return null
  const t = taskByMessage.value.get(String(m.id))
  if (!t || t.status === 'done') return null
  return messages.value.some(x => x.role === 'assistant' && x.runId === t.run_id && x.streaming) ? null : t
}
// 回答本身已经在一行一行显示进度时，卡片上不再重复那一行。
const liveReplyOf = runId => messages.value.some(x => x.role === 'assistant' && x.runId === runId && x.streaming && x.steps.length)
const followersText = t => (t.followers || []).filter(Boolean).map(name => (isSelf(name) ? '你' : name)).join('、')

// ---------------------------------------------------------------- 回答没紧跟在问题后面时，引用原问题

const indexById = computed(() => new Map(messages.value.map((m, i) => [m.id === '' || m.id == null ? '' : `${m.role}:${m.id}`, i])))
const cut = (text, n) => { const chars = [...String(text || '').replace(/\s+/g, ' ').trim()]; return chars.length > n ? `${chars.slice(0, n).join('')}…` : chars.join('') }
function quoteOf(m, i) {
  if (m.role !== 'assistant') return null
  const task = taskOf(m.runId)
  const id = m.receipt?.reply_to ?? task?.message_id ?? m.ask?.id
  if (id == null || id === '') return null
  const at = indexById.value.get(`user:${id}`)
  // 紧跟在问题后面的回答不用再引用一遍。
  if (at != null && at === i - 1) return null
  const ask = at != null ? messages.value[at] : null
  const who = m.receipt?.asker || task?.user || ask?.user || ''
  const text = ask?.text || task?.question || ''
  return { id, who: isSelf(who) ? '你' : who || '同事', text: cut(text, 20) || '附件', found: !!ask }
}
function jumpTo(id) {
  const el = list.value?.querySelector(`[data-mid="${CSS.escape(String(id))}"]`)
  if (!el) return
  el.scrollIntoView({ block: 'center', behavior: 'smooth' })
  el.classList.remove('agent-flash')
  void el.offsetWidth
  el.classList.add('agent-flash')
}

// ---------------------------------------------------------------- 回答里的「在左侧打开」

const jumpFlash = ref('')
let jumpTimer = null
async function onListClick(e) {
  const link = e.target.closest?.('a.md-jump')
  if (!link) return
  e.preventDefault()
  const path = (link.dataset.route || '').replace(/^#/, '')
  if (!path.startsWith('/')) return
  if (overlay.value) collapsed.value = true
  await router.push(path).catch(() => {})
  jumpFlash.value = link.textContent.replace(/^\s*在左侧打开\s*[·・:：]?\s*/, '').trim() || '对应页面'
  clearTimeout(jumpTimer)
  jumpTimer = setTimeout(() => { jumpFlash.value = '' }, 1000)
}

// ---------------------------------------------------------------- 实时事件（别人的轮次、任务、卡片、界面和数据变化）

let closeEvents = () => {}
function connectEvents() {
  closeEvents()
  closeEvents = openEvents(displayName.value, onLive, setCostsLive)
}
function onLive(name, data) {
  if (name === 'hello') {
    setCosts(data.costs)
    setTasks(data.tasks)
  } else if (name === 'costs') setCosts(data)
  else if (name === 'task') putTask(data)
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
  if ((runId && ownRuns.has(runId)) || (m.id && ownMessages.has(String(m.id)))) return
  if (data.thread_id !== threadId.value) return
  if (m.id && messages.value.some(x => x.id === m.id)) return
  const recent = messages.value.slice(-30).reverse()
  if (m.role === 'assistant') {
    // 别人那一轮一直在跟着 turn 显示，最终的回答落库后把它收成正式消息，不再另加一条。
    const live = messages.value.find(x => x.remote && !x.id && (runId ? x.runId === runId : true))
    if (live) {
      Object.assign(live, { id: m.id || '', bot: m.bot || live.bot || m.receipt?.bot || '', questionId: m.receipt?.question_id || live.questionId || '', text: m.text || live.text, streaming: false, at: m.at || live.at, receipt: m.receipt || live.receipt })
      if (m.steps?.length) live.steps = m.steps.map(s => ({ ...s }))
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

function onRemoteTurn({ thread_id: thread, run_id: runId, kind, data, bot }) {
  if (!runId || ownRuns.has(runId)) return
  if (thread !== threadId.value) return
  let reply = [...messages.value].reverse().find(x => x.role === 'assistant' && x.runId === runId && x.streaming)
    || [...messages.value].reverse().find(x => x.role === 'assistant' && x.runId === runId)
  if (!reply) {
    if (kind === 'done' || kind === 'error') return
    messages.value.push({ key: ++seq, id: '', runId, remote: true, role: 'assistant', bot: bot || '', text: '', steps: [], cards: [], receipt: null, at: Date.now(), streaming: true, error: '', stopped: false })
    reply = messages.value[messages.value.length - 1]
  }
  if (bot || kind === 'bot') reply = splitReply(reply, bot || data?.bot)
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
// 任务和一轮回答的用时按整秒说：「8秒」「1分05秒」。
function seconds(ms) {
  const s = Math.max(1, Math.round(Number(ms) / 1000))
  return s < 60 ? `${s}秒` : `${Math.floor(s / 60)}分${String(s % 60).padStart(2, '0')}秒`
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
  else if (tasksOpen.value) tasksOpen.value = false
  else if (historyOpen.value) historyOpen.value = false
  else if (askingName.value) { askingName.value = false; pendingSend = null }
  else if (shotTip.value) shotTip.value = false
}
// 左边开着抽屉或弹窗时，naive 的焦点锁会在 document 捕获阶段把焦点抢回去，输入框
// 就点不进去了。在更早的 window 捕获阶段截住面板里的 focus 事件，锁就看不见它。
function guardFocus(e) {
  if (e.target instanceof Node && root.value?.contains(e.target)) e.stopImmediatePropagation()
}
async function goTo(detail) {
  if (detail.storeId) await router.push({ name: 'period', params: { id: detail.storeId }, query: detail.period ? { period: detail.period } : {} }).catch(() => {})
  else if (detail.period) {
    app.pick({ period: detail.period })
    if (route.name !== 'board') await router.push({ name: 'board' }).catch(() => {})
  }
  collapsed.value = false
  await nextTick()
}
// 别处点「问助手」：员工异议打开原来那段会话；其他的开一段新会话，把话先填进输入框。
async function onOpenAgent(event) {
  const detail = event.detail || {}
  await goTo(detail)
  if (detail.questionId) {
    const found = await agentApi.threadOfQuestion(detail.questionId).catch(() => null)
    if (found?.thread_id) {
      if (threadId.value !== found.thread_id) await openThread(found.thread_id)
      await nextTick()
      const hit = messages.value.find(m => m.questionId && m.questionId === detail.questionId)
      if (hit?.id) jumpTo(hit.id)
      return
    }
  }
  newThread()
  if (detail.draft) draft.value = detail.draft
}
// 别处直接发一句（这笔不对、新资料到了）：开一段新会话发出去。
async function onAskAgent(event) {
  const detail = event.detail || {}
  if (!detail.storeId) return
  await goTo(detail)
  newThread()
  if (userName.value === null) {
    draft.value = detail.text || ''
    return askName(null)
  }
  const name = detail.subject?.node_name || ''
  running = run({
    text: name ? `这笔不对：${name}` : (detail.text || '这笔不对'),
    files: [],
    page: { ...pageContext(), room: 'store', name: 'period', period: detail.period || '', store_id: detail.storeId, params: { id: detail.storeId }, query: { period: detail.period || '' } },
    thread: threadId.value,
    trigger: detail.trigger || 'dispute',
    subject: detail.subject?.node_id ? { node_id: detail.subject.node_id, row: detail.subject.row || {} } : null,
  })
}

onMounted(() => {
  window.addEventListener('focus', guardFocus, true)
  window.addEventListener('resize', onResize)
  document.addEventListener('selectionchange', onSelection)
  document.addEventListener('pointerdown', onPointerDown, true)
  loadStatus().then(scheduleStatus)
  openThread(threadId.value)
  readPageLabel()
  labelTimer = setTimeout(readPageLabel, 800)
  window.addEventListener('ledger-agent-ask', onAskAgent)
  window.addEventListener('ledger-agent-open', onOpenAgent)
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
  clearTimeout(jumpTimer)
  clearTimeout(labelTimer)
  clearTimeout(searchTimer)
  clearTimeout(removedTimer)
  clearInterval(tick)
  for (const a of attachments.value) removeAttachment(a)
  window.removeEventListener('focus', guardFocus, true)
  window.removeEventListener('resize', onResize)
  document.removeEventListener('selectionchange', onSelection)
  document.removeEventListener('pointerdown', onPointerDown, true)
  window.removeEventListener('ledger-agent-ask', onAskAgent)
  window.removeEventListener('ledger-agent-open', onOpenAgent)
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
    aria-label="助手"
    @dragenter="onDragEnter"
    @dragover="onDragOver"
    @dragleave="onDragLeave"
    @drop="onDrop"
    @keydown.esc="onEsc"
  >
    <button v-if="collapsed" type="button" class="agent-tab" title="展开助手" @click="expand">
      <MessageSquare :size="16" aria-hidden="true" />
      <span class="agent-tab-text">助手</span>
      <span v-if="streaming || myTask" class="spin" />
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
          <strong>助手</strong>
          <span class="agent-status" :title="statusText"><i class="agent-dot" :class="{ on: status.online === true }" /><span class="agent-status-text">{{ statusText }}</span></span>
        </div>
        <div class="agent-actions">
          <CostChip compact placement="bottom-end" />
          <button type="button" class="agent-btn" title="开一段新会话" aria-label="新会话" @click="newThread">
            <SquarePen :size="14" aria-hidden="true" /><span class="agent-btn-label">新会话</span>
          </button>
          <div ref="historyBox" class="agent-history">
            <button type="button" class="agent-btn icon" :class="{ on: historyOpen }" title="会话列表" aria-label="会话列表" :aria-expanded="historyOpen" @click="toggleHistory">
              <History :size="15" aria-hidden="true" />
            </button>
            <div v-if="historyOpen" class="agent-menu agent-sessions">
              <label class="agent-search">
                <Search :size="13" aria-hidden="true" />
                <input v-model="sessionQuery" type="search" placeholder="找会话：标题或说过的话" aria-label="找会话" autocomplete="off">
              </label>
              <p v-if="removed" class="agent-removed">
                <span class="truncate">已删除「{{ removed.title || '未命名会话' }}」</span>
                <button type="button" class="link" @click="undoRemove">撤销</button>
              </p>
              <div v-if="threadsLoading && !threads.length" class="agent-menu-note"><span class="spin" /> 正在读取…</div>
              <div v-else-if="threadsError" class="agent-menu-note">{{ threadsError }}</div>
              <div v-else-if="!threads.length" class="agent-menu-note">{{ sessionQuery.trim() ? '没有找到。' : '还没有会话。' }}</div>
              <div class="agent-menu-list">
                <div v-for="t in threads" :key="t.id" class="agent-session" :class="{ on: t.id === threadId }">
                  <form v-if="renaming === t.id" class="agent-rename" @submit.prevent="saveRename(t)">
                    <input ref="renameInput" v-model="renameDraft" maxlength="80" aria-label="会话名称" @keydown.esc.stop.prevent="renaming = ''" @blur="saveRename(t)">
                  </form>
                  <template v-else>
                    <button type="button" class="agent-session-open" @click="openThread(t.id)">
                      <span class="agent-session-title">
                        <LoaderCircle v-if="runningThreads.has(t.id)" class="agent-spinner" :size="12" aria-label="正在办" />
                        <span class="truncate">{{ t.title || '未命名会话' }}</span>
                      </span>
                      <small>{{ t.user ? `${isSelf(t.user) ? '你' : t.user} · ` : '' }}{{ clock(t.updated_at) }}</small>
                    </button>
                    <span class="agent-session-tools">
                      <button type="button" class="agent-btn icon" title="改名" aria-label="改名" @click="startRename(t)"><Pencil :size="12" aria-hidden="true" /></button>
                      <button type="button" class="agent-btn icon" title="删除" aria-label="删除" @click="removeThread(t)"><Trash2 :size="12" aria-hidden="true" /></button>
                    </span>
                  </template>
                </div>
              </div>
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
      <div v-if="messages.length || threadTitle" class="agent-topic">
        <span class="agent-topic-title truncate" :title="threadTitle">{{ threadTitle || '新会话' }}</span>
      </div>
      <div class="agent-scroll">
        <div v-if="activeTasks.length" ref="taskBox" class="agent-tasks">
          <button type="button" class="agent-tasks-bar" :aria-expanded="tasksOpen" @click="tasksOpen = !tasksOpen">
            <LoaderCircle class="agent-spinner" :size="12" aria-hidden="true" />
            <b>{{ activeTasks.length }} 个任务进行中</b>
            <span class="truncate">{{ activeTasks[activeTasks.length - 1].line }}</span>
            <ChevronRight class="agent-caret" :class="{ open: tasksOpen }" :size="12" aria-hidden="true" />
          </button>
          <div v-if="tasksOpen" class="agent-tasks-list" role="list">
            <article v-for="t in activeTasks" :key="t.run_id" class="agent-task-row" role="listitem" :data-status="t.status">
              <header>
                <span class="agent-avatar" :style="{ background: tint(t.user || '同事') }" aria-hidden="true">{{ initial(t.user || '同事') }}</span>
                <b class="truncate">{{ taskWho(t) }}</b>
                <span class="agent-task-state">{{ TASK_STATUS[t.status] || t.status }}</span>
                <time class="num">{{ taskTime(t) }}</time>
                <button v-if="RUNNING.has(t.status)" type="button" class="agent-task-stop" :disabled="t.stopping" :title="`停止${taskWho(t) === '你' ? '' : ` ${taskWho(t)} 的`}这个任务`" @click="stopTask(t)"><Square :size="8" fill="currentColor" aria-hidden="true" />停止</button>
              </header>
              <p class="agent-task-q" :title="t.question">{{ t.question }}</p>
              <p v-if="t.line" class="agent-task-line">{{ t.line }}</p>
            </article>
          </div>
        </div>
        <div ref="list" class="agent-list" @scroll="onScroll" @click="onListClick">
          <div v-if="loadingThread" class="agent-loading"><span class="spin" /> 正在打开这段对话…</div>

          <section v-else-if="!messages.length" class="agent-empty">
            <div class="agent-kicker"><i />新会话</div>
            <h2>你好{{ userName ? `，${userName}` : '' }}。</h2>
            <p>想问什么直接说，查账、解释账单、看资料还缺什么、系统上的问题都行。左边打开的页面会一起带上。</p>
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
            <template v-for="(m, i) in messages" :key="m.key">
              <article v-if="m.role === 'user'" class="agent-user" :data-mid="m.id">
                <header class="agent-meta">
                  <span class="agent-who" :style="m.user && !isMine(m) ? { background: tint(m.user) } : null">{{ !m.user || isMine(m) ? '你' : m.user }}</span>
                  <time class="num">{{ clock(m.at) }}</time>
                </header>
                <ul v-if="m.images.length" class="agent-thumbs">
                  <li v-for="(src, i) in m.images" :key="i">
                    <button type="button" title="查看大图" @click="preview = src"><img :src="src" alt="附图" @load="follow()"></button>
                  </li>
                </ul>
                <ul v-if="m.files.some(f => f.kind === 'image')" class="agent-thumbs">
                  <li v-for="f in m.files.filter(f => f.kind === 'image')" :key="f.file_id">
                    <button type="button" title="查看大图" @click="preview = f.url"><img :src="f.url" :alt="f.name" @load="follow()"></button>
                  </li>
                </ul>
                <ul v-if="m.files.some(f => f.kind !== 'image')" class="agent-files">
                  <li v-for="f in m.files.filter(f => f.kind !== 'image')" :key="f.file_id">
                    <a class="agent-file" :data-type="docType(f.name).key" :href="f.url" :download="f.name" :title="`下载 ${f.name}`">
                      <component :is="docType(f.name).icon" :size="16" aria-hidden="true" />
                      <span class="agent-file-name truncate">{{ f.name }}</span>
                      <small class="num">{{ docType(f.name).label }}{{ f.size ? ` · ${fileSize(f.size)}` : '' }}</small>
                    </a>
                  </li>
                </ul>
                <p v-if="m.text" class="agent-user-text">{{ m.text }}</p>
                <p v-if="m.merged" class="agent-merged">{{ m.merged.asker }} 正在查同样的问题，结果出来一起看</p>
                <div v-for="t in [taskCard(m)].filter(Boolean)" :key="t.run_id" class="agent-task-card" :data-status="t.status">
                  <span class="agent-task-state">{{ TASK_STATUS[t.status] || t.status }}</span>
                  <span v-if="t.line && RUNNING.has(t.status) && !liveReplyOf(t.run_id)" class="agent-task-line truncate">{{ t.line }}</span>
                  <span v-if="taskTime(t)" class="num">{{ taskTime(t) }}</span>
                  <span v-if="Number(t.cost_cny) > 0" class="num">{{ yuan(t.cost_cny) }}</span>
                  <span v-if="followersText(t)" class="agent-task-followers">{{ followersText(t) }} 也在等</span>
                  <button v-if="isMineTask(t) && RUNNING.has(t.status)" type="button" class="link" @click="startSteer(t)">补充</button>
                </div>
              </article>

              <article v-else class="agent-reply" :class="{ live: m.streaming }" :data-bot="m.bot || ''">
                <header class="agent-meta">
                  <span class="agent-who ai"><i />助手</span>
                </header>
                <button v-for="q in [quoteOf(m, i)].filter(Boolean)" :key="q.id" type="button" class="agent-quote" :disabled="!q.found" :title="q.found ? '看原问题' : ''" @click="jumpTo(q.id)">
                  <Reply :size="12" aria-hidden="true" /><span class="truncate">回复 {{ q.who }}：{{ q.text }}</span>
                </button>

                <p v-if="nowStep(m)" class="agent-now"><LoaderCircle class="agent-spinner" :size="11" aria-hidden="true" /><span class="truncate">{{ nowStep(m).label }}</span></p>
                <button v-else-if="!m.streaming && m.steps.length" type="button" class="agent-trace" :aria-expanded="traceOpen(m)" @click="toggleTrace(m)">
                  <span>{{ traceSummary(m) }}</span><ChevronRight class="agent-caret" :class="{ open: traceOpen(m) }" :size="11" aria-hidden="true" />
                </button>
                <div v-if="!m.streaming && m.steps.length && traceOpen(m)" class="agent-steps">
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

                <div v-if="m.streaming && !m.text && !m.steps.length && !m.cards.length" class="agent-thinking">
                  <span class="agent-dots"><i /><i /><i /></span>在看…
                </div>

                <!-- renderMarkdown 先整体转义 HTML 再排版，v-html 里不会有回答自带的标签 -->
                <div v-if="m.text" class="agent-md" v-html="renderMarkdown(m.text, { caret: m.streaming })" />

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
                <p v-if="m.receipt && !m.steps.length && receipt(m.receipt)" class="agent-receipt num" :title="receiptTitle(m.receipt)">{{ receipt(m.receipt) }}</p>
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
        <div v-if="pageLabel && !pageOff" class="agent-picked agent-context" :title="`左边的「${pageLabel}」会一起发给助手`">
          <span class="truncate">正在看：{{ pageLabel }}</span>
          <button type="button" class="agent-x" aria-label="这次不带上左边的页面" title="这次不带上左边的页面" @click="pageOff = true"><X :size="12" /></button>
        </div>
        <div v-if="picked" class="agent-picked" :title="picked">
          <span class="truncate">附上左边选中的：「{{ picked }}」</span>
          <button type="button" class="agent-x" aria-label="不附选中的内容" @click="picked = ''"><X :size="12" /></button>
        </div>
        <ul v-if="attachments.length" class="agent-drafts" aria-label="待发的附件">
          <li v-for="a in attachments" :key="a.id" class="agent-draft" :class="[a.kind, a.state]" :data-type="a.kind === 'image' ? 'image' : docType(a.name).key" :title="a.error ? `${a.name}：${a.error}` : a.name">
            <button v-if="a.kind === 'image'" type="button" class="agent-draft-thumb" title="查看大图" @click="preview = a.thumb || a.url"><img :src="a.thumb || a.url" :alt="a.name"></button>
            <template v-else>
              <component :is="docType(a.name).icon" :size="16" class="agent-draft-icon" aria-hidden="true" />
              <span class="agent-draft-text"><span class="truncate">{{ a.name }}</span><small class="num">{{ fileSize(a.size) }}</small></span>
            </template>
            <span v-if="a.state === 'uploading'" class="agent-draft-wait" :aria-label="`正在上传 ${Math.round(a.progress * 100)}%`"><span class="spin" /><small v-if="a.progress > 0 && a.progress < 1" class="num">{{ Math.round(a.progress * 100) }}%</small></span>
            <button v-else-if="a.state === 'error'" type="button" class="agent-draft-retry" :disabled="!a.file" @click="upload(a)"><RotateCw :size="11" aria-hidden="true" />重试</button>
            <button type="button" class="agent-thumb-x" :aria-label="`移除 ${a.name}`" @click="removeAttachment(a)"><X :size="10" /></button>
          </li>
        </ul>
        <div class="agent-field" :class="{ live: streaming }">
          <textarea
            ref="textarea"
            v-model="draft"
            rows="1"
            :placeholder="steerRun ? '补充一句，这一轮接着办' : '直接说，可粘贴截图或文件'"
            aria-label="发给助手的消息"
            @keydown="onKeydown"
            @paste="onPaste"
          />
        </div>
        <div class="agent-toolbar">
          <button type="button" class="agent-tool" :title="`附上文件（每个不超过 50MB，一次最多 ${MAX_FILES} 个）。${FILE_HINT}`" @click="fileInput?.click()"><Paperclip :size="13" aria-hidden="true" />附件</button>
          <button type="button" class="agent-tool" :class="{ on: shotTip }" title="怎么截图" @click="shotTip = !shotTip"><Camera :size="13" aria-hidden="true" />截图</button>
          <select v-model="thinking" class="agent-think" :class="{ on: thinking !== 'off' }" title="让助手先想清楚再回答：开得越高越仔细，但更慢、花费更多" aria-label="思考等级">
            <option v-for="[value, label] in THINKING" :key="value" :value="value">{{ label }}</option>
          </select>
          <span class="agent-hint">Enter 发送 · Shift+Enter 换行</span>
          <template v-if="steerRun || streaming">
            <button v-if="steerRun" type="button" class="agent-send" :class="{ ready: canSteer }" :disabled="!canSteer" title="追加到正在查的这一轮，不用从头再来" @click="send()">补充<CornerDownRight :size="13" aria-hidden="true" /></button>
            <button type="button" class="agent-send stop" @click="stop"><Square :size="9" fill="currentColor" aria-hidden="true" />停止</button>
          </template>
          <button v-else type="button" class="agent-send" :class="{ ready: canSend }" :disabled="!canSend" @click="send()">发送<ArrowUp :size="13" aria-hidden="true" /></button>
        </div>
        <p v-if="shotTip" class="agent-shot-tip">
          <template v-if="isMac">按 <kbd>⌘</kbd><kbd>⇧</kbd><kbd>4</kbd> 截取屏幕，</template>
          <template v-else>按 <kbd>Win</kbd>+<kbd>Shift</kbd>+<kbd>S</kbd> 截取屏幕，</template>
          回到这里按 <kbd>{{ isMac ? '⌘' : 'Ctrl' }}</kbd>+<kbd>V</kbd> 粘贴。图片和文件也可以直接拖进来。<br>{{ FILE_HINT }}。
        </p>
        <input ref="fileInput" type="file" :accept="ACCEPT" multiple hidden @change="onPick">
      </footer>

      <div v-if="dragDepth > 0" class="agent-drop"><b>松开，作为附件发送</b><small>{{ FILE_HINT }}</small></div>
    </div>

    <Teleport to="body">
      <div v-if="jumpFlash" :key="jumpFlash" class="agent-jump-flash" role="status"><span>已在左侧打开：{{ jumpFlash }}</span></div>
    </Teleport>

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
/* 面板拖窄时头部放不下费用标签，「新会话」只留图标。 */
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
.agent-sessions { right: -40px; width: min(340px, calc(100vw - 24px)); padding: 8px; }
.agent-search { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; padding: 0 8px; border: 1px solid var(--n3); border-radius: var(--r-sm); color: var(--n5); }
.agent-search:focus-within { border-color: var(--accent); }
.agent-search input { flex: 1; min-width: 0; height: 30px; padding: 0; border: 0; outline: none; background: none; font-size: var(--t-sm); }
.agent-removed { display: flex; align-items: center; gap: 8px; margin: 0 0 6px; padding: 6px 8px; border-radius: var(--r-sm); background: var(--n1); font-size: var(--t-xs); color: var(--n7); }
.agent-removed .truncate { flex: 1; }
.agent-sessions .agent-menu-list { max-height: min(420px, 60vh); }
.agent-session { position: relative; display: flex; align-items: center; border-radius: var(--r-sm); }
.agent-session:hover { background: var(--n1); }
.agent-session.on { background: var(--accent-bg); }
.agent-session-open,
.agent-session-open:hover:not(:disabled) { display: flex; flex: 1; min-width: 0; flex-direction: column; gap: 1px; padding: 7px 8px; border: 0; background: none; color: var(--n8); font: 500 var(--t-sm)/1.45 var(--sans); text-align: left; }
.agent-session.on .agent-session-open { color: var(--accent); }
.agent-session-title { display: flex; align-items: center; gap: 5px; min-width: 0; }
.agent-session-open small { font-size: 11px; color: var(--n5); font-weight: 400; }
.agent-session-tools { display: flex; flex: none; padding-right: 4px; opacity: 0; }
.agent-session:hover .agent-session-tools,
.agent-session:focus-within .agent-session-tools { opacity: 1; }
.agent-rename { flex: 1; padding: 4px; }
.agent-rename input { width: 100%; height: 28px; padding: 0 8px; font-size: var(--t-sm); }
.agent-context .truncate { color: var(--n6); }
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

/* 消息列表 */
.agent-scroll { position: relative; flex: 1; min-height: 0; }
.agent-list {
  height: 100%;
  overflow-y: auto;
  scrollbar-gutter: stable;
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
  flex: none;
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
.agent-think { flex: none; height: 24px; padding: 0 4px; border: 1px solid transparent; border-radius: 6px; background: transparent; font-size: 12px; color: var(--n6); cursor: pointer; }
.agent-think:hover { background: var(--n1); }
.agent-think.on { color: var(--accent); border-color: currentColor; }
.agent-hint { flex: 0 1 auto; min-width: 0; margin-left: auto; overflow: hidden; text-overflow: ellipsis; font-size: 11px; color: var(--n5); white-space: nowrap; }
.agent-send,
.agent-send:hover:not(:disabled) {
  flex: none;
  white-space: nowrap;
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

/* 任务条：有任务在跑时才出现，浮在消息列表顶上，出现、消失都不挪动对话。 */
.agent-tasks { position: absolute; top: 0; left: 0; right: 0; z-index: 24; border-bottom: 1px solid var(--n2); background: var(--n0); }
.agent-tasks-bar,
.agent-tasks-bar:hover:not(:disabled) {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  height: 28px;
  padding: 0 var(--s5);
  border: 0;
  border-radius: 0;
  background: linear-gradient(90deg, var(--accent-bg), transparent 70%);
  color: var(--n6);
  font: 400 11.5px/1 var(--sans);
  text-align: left;
}
.agent-tasks-bar b { flex: none; font-weight: 560; color: var(--n8); }
.agent-tasks-bar .truncate { flex: 1; min-width: 0; }
.agent-tasks-bar:hover:not(:disabled) b { color: var(--accent); }
.agent-tasks-list {
  position: absolute;
  top: 100%;
  left: 8px;
  right: 8px;
  z-index: 25;
  max-height: min(360px, 55vh);
  overflow-y: auto;
  padding: 4px;
  border: 1px solid var(--n3);
  border-radius: var(--r-md);
  background: var(--n0);
  box-shadow: var(--shadow);
}
.agent-task-row { padding: 8px 8px 7px; border-radius: var(--r-sm); }
.agent-task-row + .agent-task-row { border-top: 1px solid var(--n2); }
.agent-task-row header { display: flex; align-items: center; gap: 6px; font-size: 12px; }
.agent-task-row header b { min-width: 0; font-weight: 560; color: var(--n8); }
.agent-task-row header time { margin-left: auto; font-size: 11px; color: var(--n5); }
.agent-task-row .agent-avatar { width: 18px; height: 18px; margin: 0; border: 0; font-size: 9.5px; }
.agent-task-q { margin: 3px 0 0 24px; font-size: 12.5px; color: var(--n9); line-height: 1.5; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.agent-task-line { margin: 1px 0 0 24px; font-size: 11.5px; color: var(--n5); }
.agent-task-state { flex: none; padding: 0 6px; border-radius: 3px; background: var(--n2); color: var(--n7); font-size: 10.5px; line-height: 17px; }
[data-status='running'] > .agent-task-state,
[data-status='running'] > header > .agent-task-state { background: var(--accent-bg); color: var(--accent); }
[data-status='waiting'] > .agent-task-state,
[data-status='waiting'] > header > .agent-task-state { background: var(--warn-bg); color: var(--warn); }
[data-status='failed'] > .agent-task-state { background: var(--bad-bg); color: var(--bad); }
.agent-task-stop,
.agent-task-stop:hover:not(:disabled) {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex: none;
  height: 20px;
  padding: 0 7px;
  border: 1px solid var(--n3);
  border-radius: 999px;
  background: var(--n0);
  color: var(--n7);
  font: 500 11px/1 var(--sans);
}
.agent-task-stop:hover:not(:disabled) { border-color: var(--n8); color: var(--n9); }

/* 群里提问底下的任务小卡片 */
.agent-task-card {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 8px;
  max-width: 100%;
  margin-top: 6px;
  padding: 3px 8px;
  border-radius: var(--r-sm);
  background: var(--n1);
  color: var(--n6);
  font-size: 11.5px;
  line-height: 1.5;
}
.agent-task-card .agent-task-line { margin: 0; max-width: 220px; }
.agent-task-card .link { font-size: 11.5px; }
.agent-task-followers { color: var(--n7); }
.agent-merged { margin-top: 6px; font-size: 12px; color: var(--n6); }
.agent-user.agent-flash { animation: agent-flash 1.2s ease; }
@keyframes agent-flash { 0%, 40% { background: var(--accent-bg); } 100% { background: transparent; } }

/* 回答上方：引用原问题、正在做的那一步、查完后的一行摘要 */
.agent-quote,
.agent-quote:hover:not(:disabled) {
  display: flex;
  align-items: center;
  gap: 5px;
  max-width: 100%;
  margin: -2px 0 8px;
  padding: 3px 8px;
  border: 0;
  border-left: 2px solid var(--n4);
  border-radius: 0 var(--r-sm) var(--r-sm) 0;
  background: var(--n1);
  color: var(--n6);
  font: 400 12px/1.5 var(--sans);
  text-align: left;
}
.agent-quote:hover:not(:disabled) { border-left-color: var(--accent); color: var(--n8); }
.agent-quote:disabled { opacity: 1; cursor: default; }
.agent-quote svg { flex: none; }
.agent-now { display: flex; align-items: center; gap: 6px; margin: 0 0 8px; font-size: 12.5px; color: var(--n5); }
.agent-now .agent-spinner { color: var(--n5); }
.agent-trace,
.agent-trace:hover:not(:disabled) {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin: 0 0 8px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--n5);
  font: 400 11.5px/1.6 var(--sans);
}
.agent-trace:hover:not(:disabled) { color: var(--n8); }
.agent-trace + .agent-steps { margin-top: -2px; }
/* 秒回的闲聊用不着「正在看」，等一会儿还没有字再出来。 */
.agent-thinking { animation: agent-late .2s ease .8s both; }
@keyframes agent-late { from { opacity: 0; } to { opacity: 1; } }

/* 回答里「在左侧打开」的链接 */
.agent-md :deep(a.md-jump) {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin: 2px 2px;
  padding: 1px 10px;
  border: 1px solid #c9d6fb;
  border-radius: 999px;
  background: var(--accent-bg);
  color: var(--accent);
  font-size: 12.5px;
  font-weight: 560;
  line-height: 1.7;
  text-decoration: none;
  vertical-align: baseline;
}
.agent-md :deep(a.md-jump)::before { content: '←'; font-weight: 400; }
.agent-md :deep(a.md-jump:hover) { border-color: var(--accent); background: var(--accent); color: #fff; }

/* 附件：消息里的文件卡片和输入框上方待发的 */
.agent-files { list-style: none; display: flex; flex-direction: column; gap: 6px; margin: 0 0 8px; padding: 0; }
.agent-file {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  column-gap: 8px;
  align-items: center;
  width: 240px;
  max-width: 100%;
  padding: 7px 10px;
  border: 1px solid var(--n3);
  border-radius: var(--r-sm);
  background: var(--n0);
  color: var(--n8);
  text-align: left;
  text-decoration: none;
}
.agent-file svg { grid-row: span 2; }
.agent-file-name { font-size: 12.5px; font-weight: 500; line-height: 1.4; }
.agent-file small { font-size: 11px; color: var(--n5); line-height: 1.4; }
.agent-file:hover { border-color: var(--n4); background: var(--n1); }
[data-type='sheet'] > svg { color: #1d7a46; }
[data-type='word'] > svg { color: #2b5fd9; }
[data-type='pdf'] > svg { color: #c8372d; }
[data-type='slides'] > svg { color: #d0661a; }
[data-type='zip'] > svg,
[data-type='other'] > svg { color: var(--n6); }
.agent-drafts { list-style: none; display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0 0; padding: 0; }
.agent-draft { position: relative; display: flex; align-items: center; gap: 6px; height: 52px; border-radius: var(--r-sm); }
.agent-draft.file { width: 180px; padding: 0 10px; border: 1px solid var(--n3); background: var(--n0); }
.agent-draft.error { border-color: #f2b8b5; }
.agent-draft-thumb,
.agent-draft-thumb:hover:not(:disabled) { display: block; height: 100%; padding: 0; border: 0; background: none; cursor: zoom-in; }
.agent-draft-thumb img { display: block; height: 52px; max-width: 96px; object-fit: cover; border: 1px solid var(--n3); border-radius: var(--r-sm); }
.agent-draft-icon { flex: none; }
.agent-draft-text { display: flex; flex-direction: column; min-width: 0; flex: 1; font-size: 12px; line-height: 1.4; color: var(--n8); }
.agent-draft-text small { font-size: 10.5px; color: var(--n5); }
.agent-draft-wait { display: inline-flex; align-items: center; gap: 4px; flex: none; font-size: 10.5px; color: var(--n6); }
.agent-draft.image .agent-draft-wait { position: absolute; inset: 0; justify-content: center; flex-direction: column; border-radius: var(--r-sm); background: rgba(255, 255, 255, .78); }
.agent-draft-wait .spin { width: 12px; height: 12px; }
.agent-draft-retry,
.agent-draft-retry:hover:not(:disabled) {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  flex: none;
  padding: 0;
  border: 0;
  background: none;
  color: var(--bad);
  font: 500 11px/1 var(--sans);
}
.agent-draft.image .agent-draft-retry { position: absolute; inset: auto 0 0; justify-content: center; padding: 3px 0; border-radius: 0 0 var(--r-sm) var(--r-sm); background: rgba(255, 255, 255, .9); }
.agent-drop { flex-direction: column; gap: 6px; padding: 0 24px; text-align: center; }
.agent-drop small { font-size: 11.5px; font-weight: 400; color: var(--n6); line-height: 1.6; }

/* 点了「在左侧打开」之后，左栏顶上亮一下，告诉人那边换了页。 */
.agent-jump-flash {
  position: fixed;
  top: 0;
  left: 0;
  right: var(--agent-dock, 0px);
  z-index: 4100;
  display: flex;
  justify-content: center;
  padding-top: 10px;
  pointer-events: none;
  animation: agent-jump 1s ease forwards;
}
.agent-jump-flash::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px; background: var(--accent); }
.agent-jump-flash span { padding: 4px 14px; border-radius: 999px; background: var(--accent); color: #fff; font: 560 12px/1.6 var(--sans); box-shadow: 0 6px 20px rgba(37, 79, 196, .28); }
@keyframes agent-jump {
  0% { opacity: 0; transform: translateY(-4px); }
  15%, 75% { opacity: 1; transform: none; }
  100% { opacity: 0; }
}

@container (max-width: 330px) {
  .agent-hint { display: none; }
}
@media (prefers-reduced-motion: reduce) {
  .agent-md :deep(.md-caret),
  .agent-field.live::after,
  .agent-dots i,
  .agent-spinner,
  .agent-user.agent-flash { animation: none; }
}
</style>

<style>
body.agent-resizing { cursor: col-resize; user-select: none; }
body.agent-resizing iframe { pointer-events: none; }
</style>
