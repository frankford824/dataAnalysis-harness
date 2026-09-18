<script setup>
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { CARD_H, CARD_W, layoutTree } from '../orgLayout'

const props = defineProps({
  tree: { type: Array, default: () => [] },
  selectedId: { type: String, default: '' },
  matches: { type: Object, default: () => ({}) },
  collapsed: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['select', 'drop', 'add', 'toggle'])

const MIN_SCALE = 0.28
const MAX_SCALE = 2.6

const wrap = ref(null)
const canvas = ref(null)
let ctx = null
let dpr = 1
let scale = 1
let ox = 40
let oy = 40
let boxes = []
let edges = []
let hoverId = ''
let dropId = ''
let dragId = ''
let dragStart = null
let panStart = null
let draggingNode = false
let dragWorld = null
let raf = 0
let resizeObs = null

const roleColor = {
  团队长: { fill: '#6366f1', badge: '#ede9fe', text: '#4f46e5' },
  组长: { fill: '#0891b2', badge: '#e0f7fa', text: '#0e7490' },
  成员: { fill: '#64748b', badge: '#f1f5f9', text: '#475569' },
}

function layout() {
  const next = layoutTree(props.tree, props.collapsed)
  boxes = next.boxes
  edges = next.edges
}

function worldPoint(event) {
  const rect = canvas.value.getBoundingClientRect()
  return {
    x: (event.clientX - rect.left - ox) / scale,
    y: (event.clientY - rect.top - oy) / scale,
  }
}

function hit(point) {
  for (let i = boxes.length - 1; i >= 0; i--) {
    const box = boxes[i]
    if (point.x >= box.x && point.x <= box.x + box.w && point.y >= box.y && point.y <= box.y + box.h) return box
  }
  return null
}

function chevronHit(box, point) {
  if (!box.node.children?.length) return false
  const cx = box.x + box.w - 16
  const cy = box.y + 16
  return Math.hypot(point.x - cx, point.y - cy) <= 10
}

function descendants(id) {
  const found = new Set()
  const collect = nodes => {
    for (const node of nodes || []) {
      found.add(node.id)
      collect(node.children)
    }
  }
  const find = nodes => {
    for (const node of nodes || []) {
      if (node.id === id) {
        collect(node.children)
        return true
      }
      if (find(node.children)) return true
    }
    return false
  }
  find(props.tree)
  return found
}

function fit() {
  if (!boxes.length || !canvas.value) return
  const pad = 48
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const box of boxes) {
    minX = Math.min(minX, box.x)
    minY = Math.min(minY, box.y)
    maxX = Math.max(maxX, box.x + box.w)
    maxY = Math.max(maxY, box.y + box.h)
  }
  const w = canvas.value.clientWidth
  const h = canvas.value.clientHeight
  const next = Math.min((w - pad * 2) / Math.max(maxX - minX, 1), (h - pad * 2) / Math.max(maxY - minY, 1), 1.15)
  scale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, next))
  ox = (w - (maxX + minX) * scale) / 2
  oy = (h - (maxY + minY) * scale) / 2
  paint()
}

function focusId(id) {
  const box = boxes.find(item => item.id === id)
  if (!box || !canvas.value) return
  const w = canvas.value.clientWidth
  const h = canvas.value.clientHeight
  scale = Math.max(scale, 0.9)
  ox = w / 2 - (box.x + box.w / 2) * scale
  oy = h / 2 - (box.y + box.h / 2) * scale
  paint()
}

function roundRect(x, y, w, h, r) {
  ctx.beginPath()
  ctx.moveTo(x + r, y)
  ctx.arcTo(x + w, y, x + w, y + h, r)
  ctx.arcTo(x + w, y + h, x, y + h, r)
  ctx.arcTo(x, y + h, x, y, r)
  ctx.arcTo(x, y, x + w, y, r)
  ctx.closePath()
}

function drawEdge(edge, ghost) {
  const mx = (edge.from.x + edge.to.x) / 2
  ctx.beginPath()
  ctx.moveTo(edge.from.x, edge.from.y)
  ctx.bezierCurveTo(mx, edge.from.y, mx, edge.to.y, edge.to.x, edge.to.y)
  ctx.strokeStyle = ghost ? 'rgba(99,102,241,.35)' : '#c7d0de'
  ctx.lineWidth = 1.6
  ctx.stroke()
}

function drawCard(box) {
  const { node, x, y, w, h } = box
  const role = roleColor[node.role] || roleColor['成员']
  const selected = props.selectedId === node.id
  const matched = !!props.matches[node.id]
  const hovered = hoverId === node.id
  const dropping = dropId === node.id
  const dim = node.archived ? 0.45 : 1
  ctx.save()
  ctx.globalAlpha = dim
  ctx.shadowColor = selected || dropping ? 'rgba(99,102,241,.22)' : 'rgba(15,23,42,.06)'
  ctx.shadowBlur = selected || dropping ? 16 : 8
  ctx.shadowOffsetY = 2
  roundRect(x, y, w, h, 12)
  ctx.fillStyle = dropping ? '#f5f3ff' : '#fff'
  ctx.fill()
  ctx.shadowColor = 'transparent'
  ctx.lineWidth = selected ? 2.2 : 1.2
  ctx.strokeStyle = dropping ? '#6366f1' : selected ? '#818cf8' : matched ? '#f59e0b' : hovered ? '#c7d2fe' : '#e2e8f0'
  ctx.stroke()

  ctx.beginPath()
  ctx.arc(x + 18, y + 22, 11, 0, Math.PI * 2)
  ctx.fillStyle = role.fill
  ctx.fill()
  ctx.fillStyle = '#fff'
  ctx.font = '700 12px ui-sans-serif, system-ui, sans-serif'
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  ctx.fillText((node.name || '?').slice(0, 1), x + 18, y + 23)

  ctx.textAlign = 'left'
  ctx.fillStyle = '#0f172a'
  ctx.font = '650 13px ui-sans-serif, system-ui, sans-serif'
  ctx.fillText(clip(node.name || '', 8), x + 36, y + 18)

  const badge = node.role || '成员'
  ctx.font = '600 10px ui-sans-serif, system-ui, sans-serif'
  const bw = ctx.measureText(badge).width + 10
  roundRect(x + w - bw - (node.children?.length ? 26 : 10), y + 10, bw, 16, 8)
  ctx.fillStyle = role.badge
  ctx.fill()
  ctx.fillStyle = role.text
  ctx.textAlign = 'center'
  ctx.fillText(badge, x + w - bw / 2 - (node.children?.length ? 26 : 10), y + 18)

  const alias = node.alias && node.alias !== node.name ? node.alias : ''
  ctx.textAlign = 'left'
  ctx.fillStyle = '#64748b'
  ctx.font = '12px ui-sans-serif, system-ui, sans-serif'
  if (alias) ctx.fillText(clip(alias, 12), x + 36, y + 36)

  const stores = (node.stores || []).length
  const kids = node.child_count || 0
  const meta = [stores ? `${stores} 店` : '', kids ? `${kids} 人` : ''].filter(Boolean).join(' · ') || '未分配店铺'
  ctx.fillStyle = stores || kids ? '#94a3b8' : '#cbd5e1'
  ctx.font = '11px ui-sans-serif, system-ui, sans-serif'
  ctx.fillText(clip(meta, 16), x + 12, y + 56)

  if (node.children?.length) {
    const open = !props.collapsed[node.id]
    ctx.beginPath()
    ctx.arc(x + w - 16, y + 18, 8, 0, Math.PI * 2)
    ctx.fillStyle = '#f1f5f9'
    ctx.fill()
    ctx.strokeStyle = '#94a3b8'
    ctx.lineWidth = 1.4
    ctx.beginPath()
    if (open) {
      ctx.moveTo(x + w - 20, y + 15)
      ctx.lineTo(x + w - 16, y + 21)
      ctx.lineTo(x + w - 12, y + 15)
    } else {
      ctx.moveTo(x + w - 19, y + 14)
      ctx.lineTo(x + w - 13, y + 18)
      ctx.lineTo(x + w - 19, y + 22)
    }
    ctx.stroke()
  }
  ctx.restore()
}

function clip(text, max) {
  return text.length > max ? text.slice(0, max) + '…' : text
}

function paint() {
  if (!ctx || !canvas.value) return
  const w = canvas.value.width
  const h = canvas.value.height
  ctx.setTransform(1, 0, 0, 1, 0, 0)
  ctx.clearRect(0, 0, w, h)
  ctx.fillStyle = '#f6f8fb'
  ctx.fillRect(0, 0, w, h)
  drawDots()
  ctx.setTransform(dpr * scale, 0, 0, dpr * scale, dpr * ox, dpr * oy)
  for (const edge of edges) drawEdge(edge)
  if (draggingNode && dragWorld && dragId) {
    const source = boxes.find(item => item.id === dragId)
    if (source && dropId) {
      const target = boxes.find(item => item.id === dropId)
      if (target) drawEdge({ from: { x: target.x + target.w, y: target.y + target.h / 2 }, to: { x: dragWorld.x, y: dragWorld.y } }, true)
    }
  }
  for (const box of boxes) {
    if (draggingNode && box.id === dragId) continue
    drawCard(box)
  }
  if (draggingNode && dragWorld) {
    const source = boxes.find(item => item.id === dragId)
    if (source) drawCard({ ...source, x: dragWorld.x - CARD_W / 2, y: dragWorld.y - CARD_H / 2 })
  }
}

function drawDots() {
  ctx.fillStyle = '#e6ebf2'
  const gap = 22 * dpr
  for (let x = (ox * dpr) % gap; x < canvas.value.width; x += gap) {
    for (let y = (oy * dpr) % gap; y < canvas.value.height; y += gap) {
      ctx.beginPath()
      ctx.arc(x, y, 1.1 * dpr, 0, Math.PI * 2)
      ctx.fill()
    }
  }
}

function schedule() {
  if (raf) return
  raf = requestAnimationFrame(() => { raf = 0; paint() })
}

function resize() {
  if (!wrap.value || !canvas.value) return
  dpr = window.devicePixelRatio || 1
  const w = wrap.value.clientWidth
  const h = wrap.value.clientHeight
  canvas.value.width = Math.max(1, Math.floor(w * dpr))
  canvas.value.height = Math.max(1, Math.floor(h * dpr))
  canvas.value.style.width = w + 'px'
  canvas.value.style.height = h + 'px'
  paint()
}

function onWheel(event) {
  event.preventDefault()
  const rect = canvas.value.getBoundingClientRect()
  const sx = event.clientX - rect.left
  const sy = event.clientY - rect.top
  const point = { x: (sx - ox) / scale, y: (sy - oy) / scale }
  const next = Math.max(MIN_SCALE, Math.min(MAX_SCALE, scale * (event.deltaY < 0 ? 1.08 : 0.92)))
  ox = sx - point.x * next
  oy = sy - point.y * next
  scale = next
  schedule()
}

function onDown(event) {
  if (event.button !== 0) return
  const point = worldPoint(event)
  const box = hit(point)
  if (box && chevronHit(box, point)) {
    emit('toggle', box.id)
    return
  }
  if (box) {
    dragId = box.id
    dragStart = { x: event.clientX, y: event.clientY, point }
    draggingNode = false
    emit('select', box.node)
  } else {
    panStart = { x: event.clientX, y: event.clientY, ox, oy }
    dragId = ''
  }
}

function onMove(event) {
  const point = worldPoint(event)
  if (panStart) {
    ox = panStart.ox + event.clientX - panStart.x
    oy = panStart.oy + event.clientY - panStart.y
    canvas.value.style.cursor = 'grabbing'
    schedule()
    return
  }
  if (dragStart && dragId) {
    if (!draggingNode && Math.hypot(event.clientX - dragStart.x, event.clientY - dragStart.y) > 6) {
      draggingNode = true
    }
    if (draggingNode) {
      dragWorld = point
      const blocked = descendants(dragId)
      const over = hit(point)
      dropId = over && over.id !== dragId && !blocked.has(over.id) ? over.id : ''
      canvas.value.style.cursor = 'grabbing'
      schedule()
      return
    }
  }
  const over = hit(point)
  const next = over?.id || ''
  canvas.value.style.cursor = over ? (chevronHit(over, point) ? 'pointer' : 'grab') : 'default'
  if (next !== hoverId) {
    hoverId = next
    schedule()
  }
}

function onUp() {
  if (draggingNode && dragId) emit('drop', { personId: dragId, parentId: dropId })
  dragStart = null
  panStart = null
  draggingNode = false
  dragWorld = null
  dragId = ''
  dropId = ''
  if (canvas.value) canvas.value.style.cursor = 'default'
  schedule()
}

function onDblclick(event) {
  const box = hit(worldPoint(event))
  if (box) emit('add', box.node)
}

function onLeave() {
  if (!panStart && !draggingNode) {
    hoverId = ''
    schedule()
  }
}

watch(() => props.tree, () => {
  layout()
  fit()
}, { deep: true })

watch(() => props.collapsed, () => {
  layout()
  paint()
}, { deep: true })

watch(() => props.selectedId, () => schedule())
watch(() => props.matches, matches => {
  const first = Object.keys(matches || {})[0]
  if (first) focusId(first)
  else schedule()
})

onMounted(() => {
  ctx = canvas.value.getContext('2d')
  resize()
  layout()
  fit()
  resizeObs = new ResizeObserver(resize)
  resizeObs.observe(wrap.value)
  canvas.value.addEventListener('wheel', onWheel, { passive: false })
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
})

onUnmounted(() => {
  resizeObs?.disconnect()
  canvas.value?.removeEventListener('wheel', onWheel)
  window.removeEventListener('mousemove', onMove)
  window.removeEventListener('mouseup', onUp)
  if (raf) cancelAnimationFrame(raf)
})

defineExpose({ fit, focusId })
</script>

<template>
  <div ref="wrap" class="org-canvas-wrap">
    <canvas
      ref="canvas"
      class="org-canvas"
      @mousedown="onDown"
      @dblclick="onDblclick"
      @mouseleave="onLeave"
    />
    <div class="org-canvas-tools">
      <button type="button" @click="scale=Math.min(MAX_SCALE,scale*1.15);paint()">＋</button>
      <button type="button" @click="scale=Math.max(MIN_SCALE,scale/1.15);paint()">－</button>
      <button type="button" @click="fit()">适应</button>
    </div>
    <p v-if="!tree.length" class="org-canvas-empty">还没有组织层级。先新建一位团队长，再把其他人拖到他右边。</p>
  </div>
</template>
