<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  Crown,
  User,
  Users,
  Store,
  Plus,
  Minus,
  Maximize2,
  RotateCcw,
  GripVertical,
  ChevronRight,
  ChevronDown,
  Building2,
  Check,
} from '@lucide/vue'

const props = defineProps({
  tree: { type: Array, default: () => [] },
  people: { type: Array, default: () => [] },
  selectedId: { type: String, default: '' },
  collapsed: { type: Object, default: () => ({}) },
})

const emit = defineEmits(['select', 'drop', 'add', 'toggle'])

const viewport = ref(null)
const surface = ref(null)

// Pan & Zoom state
const scale = ref(1)
const ox = ref(60)
const oy = ref(120)
const MIN_SCALE = 0.25
const MAX_SCALE = 2.4

let isPanning = false
let panStartX = 0
let panStartY = 0
let panStartOx = 0
let panStartOy = 0

// Dragging node to change hierarchy
const draggingId = ref('')
const dropTargetId = ref('')

// Node layout coordinates
const CARD_W = 210
const CARD_H = 72
const ROOT_W = 240
const ROOT_H = 80
const H_GAP = 96
const V_GAP = 20

// Measure tree heights
function getVisibleChildren(node) {
  if (props.collapsed[node.id]) return []
  return node.children || []
}

function measureNode(node) {
  const kids = getVisibleChildren(node)
  if (!kids.length) {
    node._h = CARD_H
    return CARD_H
  }
  let h = 0
  for (const child of kids) {
    h += measureNode(child) + V_GAP
  }
  node._h = Math.max(CARD_H, h - V_GAP)
  return node._h
}

// Compute all node boxes and SVG connection curves
const layout = computed(() => {
  const boxes = []
  const edges = []

  const roots = props.tree || []
  if (!roots.length) {
    return { rootBox: null, boxes, edges, bounds: { minX: 0, minY: 0, maxX: 400, maxY: 300 } }
  }

  // Measure all roots
  let totalRootsH = 0
  for (const root of roots) {
    totalRootsH += measureNode(root) + V_GAP
  }
  totalRootsH = Math.max(ROOT_H, totalRootsH - V_GAP)

  const rootX = 40
  const rootY = 40 + (totalRootsH - ROOT_H) / 2
  const rootBox = {
    id: '__root__',
    name: '聚水潭电商 销售组织体系',
    x: rootX,
    y: rootY,
    w: ROOT_W,
    h: ROOT_H,
    teamCount: roots.length,
    personCount: props.people.length,
  }

  function placeNode(node, x, y, parentPos) {
    const cardY = y + (node._h - CARD_H) / 2
    const box = {
      id: node.id,
      node,
      x,
      y: cardY,
      w: CARD_W,
      h: CARD_H,
      parentId: parentPos?.id || '',
    }
    boxes.push(box)

    if (parentPos) {
      edges.push({
        id: `${parentPos.id}->${node.id}`,
        from: { x: parentPos.x + parentPos.w, y: parentPos.y + parentPos.h / 2 },
        to: { x: box.x, y: box.y + box.h / 2 },
        active: props.selectedId === node.id || props.selectedId === parentPos.id,
      })
    }

    let curY = y
    for (const child of getVisibleChildren(node)) {
      placeNode(child, x + CARD_W + H_GAP, curY, box)
      curY += child._h + V_GAP
    }
  }

  let curY = 40
  for (const root of roots) {
    placeNode(root, rootX + ROOT_W + H_GAP, curY, rootBox)
    curY += root._h + V_GAP
  }

  // Calculate bounding box for auto-fit
  let minX = rootX
  let minY = Math.min(rootY, 40)
  let maxX = rootX + ROOT_W
  let maxY = curY

  for (const box of boxes) {
    minX = Math.min(minX, box.x)
    minY = Math.min(minY, box.y)
    maxX = Math.max(maxX, box.x + box.w)
    maxY = Math.max(maxY, box.y + box.h)
  }

  return { rootBox, boxes, edges, bounds: { minX, minY, maxX, maxY } }
})

// SVG Bezier Curves
function makeBezier(edge) {
  const { from, to } = edge
  const dx = Math.max(40, (to.x - from.x) * 0.5)
  return `M ${from.x} ${from.y} C ${from.x + dx} ${from.y}, ${to.x - dx} ${to.y}, ${to.x} ${to.y}`
}

// Pan & Zoom controls
function onWheel(event) {
  event.preventDefault()
  if (!viewport.value) return
  const rect = viewport.value.getBoundingClientRect()
  const mouseX = event.clientX - rect.left
  const mouseY = event.clientY - rect.top

  const zoomFactor = event.deltaY < 0 ? 1.08 : 0.92
  const nextScale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, scale.value * zoomFactor))

  ox.value = mouseX - (mouseX - ox.value) * (nextScale / scale.value)
  oy.value = mouseY - (mouseY - oy.value) * (nextScale / scale.value)
  scale.value = nextScale
}

function onMouseDown(event) {
  // Only pan on left click on blank surface
  if (event.button !== 0) return
  const target = event.target
  if (target.closest('.mindmap-card') || target.closest('.mindmap-controls') || target.closest('button')) {
    return
  }
  isPanning = true
  panStartX = event.clientX
  panStartY = event.clientY
  panStartOx = ox.value
  panStartOy = oy.value
  if (viewport.value) viewport.value.style.cursor = 'grabbing'
}

function onMouseMove(event) {
  if (!isPanning) return
  ox.value = panStartOx + (event.clientX - panStartX)
  oy.value = panStartOy + (event.clientY - panStartY)
}

function onMouseUp() {
  if (isPanning) {
    isPanning = false
    if (viewport.value) viewport.value.style.cursor = 'default'
  }
}

// Fit to view
function fit() {
  if (!viewport.value || !layout.value.bounds) return
  const { minX, minY, maxX, maxY } = layout.value.bounds
  const w = viewport.value.clientWidth
  const h = viewport.value.clientHeight
  const pad = 64

  const spanW = Math.max(maxX - minX, 100)
  const spanH = Math.max(maxY - minY, 100)

  const fitScale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, Math.min((w - pad * 2) / spanW, (h - pad * 2) / spanH, 1.1)))
  scale.value = fitScale
  ox.value = (w - (maxX + minX) * fitScale) / 2
  oy.value = (h - (maxY + minY) * fitScale) / 2
}

function resetZoom() {
  scale.value = 1
  ox.value = 80
  oy.value = 80
}

function zoomIn() {
  scale.value = Math.min(MAX_SCALE, scale.value * 1.18)
}

function zoomOut() {
  scale.value = Math.max(MIN_SCALE, scale.value / 1.18)
}

// Drag & Drop to change hierarchy
function onCardDragStart(event, node) {
  draggingId.value = node.id
  event.dataTransfer.effectAllowed = 'move'
  event.dataTransfer.setData('text/plain', node.id)
}

function onCardDragOver(event, targetId) {
  event.preventDefault()
  if (draggingId.value && draggingId.value !== targetId) {
    dropTargetId.value = targetId
  }
}

function onCardDragLeave(targetId) {
  if (dropTargetId.value === targetId) {
    dropTargetId.value = ''
  }
}

function onCardDrop(event, targetId) {
  event.preventDefault()
  const sourceId = draggingId.value || event.dataTransfer.getData('text/plain')
  dropTargetId.value = ''
  draggingId.value = ''
  if (!sourceId || sourceId === targetId) return
  // If targetId is root, turn into a root team
  emit('drop', { personId: sourceId, parentId: targetId === '__root__' ? '' : targetId })
}

function onCardDragEnd() {
  draggingId.value = ''
  dropTargetId.value = ''
}

// Auto fit on tree load
watch(() => props.tree, () => {
  nextTick(fit)
}, { deep: true })

onMounted(() => {
  if (viewport.value) {
    viewport.value.addEventListener('wheel', onWheel, { passive: false })
  }
  window.addEventListener('mousemove', onMouseMove)
  window.addEventListener('mouseup', onMouseUp)
  fit()
})

onUnmounted(() => {
  if (viewport.value) {
    viewport.value.removeEventListener('wheel', onWheel)
  }
  window.removeEventListener('mousemove', onMouseMove)
  window.removeEventListener('mouseup', onMouseUp)
})

defineExpose({ fit, resetZoom })
</script>

<template>
  <div
    ref={viewport}
    class="mindmap-viewport"
    @mousedown="onMouseDown"
  >
    <!-- Background Grid -->
    <div class="mindmap-grid-layer" />

    <!-- Transform Surface (Cards + SVG Edges) -->
    <div
      ref="surface"
      class="mindmap-surface"
      :style="{
        transform: `translate3d(${ox}px, ${oy}px, 0) scale(${scale})`,
      }"
    >
      <!-- SVG Connecting Curves Layer -->
      <svg
        class="mindmap-edges-layer"
        :style="{
          width: `${(layout.bounds.maxX || 2000) + 400}px`,
          height: `${(layout.bounds.maxY || 1500) + 400}px`,
        }"
      >
        <path
          v-for="edge in layout.edges"
          :key="edge.id"
          :d="makeBezier(edge)"
          :class="['mindmap-edge', { active: edge.active }]"
        />
      </svg>

      <!-- Center Root Hub Card -->
      <div
        v-if="layout.rootBox"
        class="mindmap-root-card"
        :class="{ 'drop-target': dropTargetId === '__root__' }"
        :style="{
          left: `${layout.rootBox.x}px`,
          top: `${layout.rootBox.y}px`,
          width: `${layout.rootBox.w}px`,
          height: `${layout.rootBox.h}px`,
        }"
        @dragover="onCardDragOver($event, '__root__')"
        @dragleave="onCardDragLeave('__root__')"
        @drop="onCardDrop($event, '__root__')"
      >
        <div class="mindmap-root-icon">
          <Building2 :size="24" />
        </div>
        <div class="mindmap-root-content">
          <div class="mindmap-root-title">聚水潭电商销售组织</div>
          <div class="mindmap-root-stats">
            <span>{{ layout.rootBox.teamCount }} 个团队</span>
            <span>·</span>
            <span>{{ layout.rootBox.personCount }} 位成员</span>
          </div>
        </div>
      </div>

      <!-- DOM Node Cards -->
      <div
        v-for="box in layout.boxes"
        :key="box.id"
        class="mindmap-card"
        :class="[
          box.node.role === '团队长' ? 'role-leader' : box.node.children?.length ? 'role-manager' : 'role-member',
          {
            active: selectedId === box.id,
            'drop-target': dropTargetId === box.id,
            'has-children': box.node.children?.length > 0,
          },
        ]"
        :style="{
          left: `${box.x}px`,
          top: `${box.y}px`,
          width: `${box.w}px`,
          height: `${box.h}px`,
        }"
        draggable="true"
        @dragstart="onCardDragStart($event, box.node)"
        @dragend="onCardDragEnd"
        @dragover="onCardDragOver($event, box.id)"
        @dragleave="onCardDragLeave(box.id)"
        @drop="onCardDrop($event, box.id)"
        @click.stop="emit('select', box.node)"
      >
        <GripVertical :size="13" class="mindmap-drag-handle" title="拖拽更改上下级" />

        <div
          class="mindmap-avatar"
          :class="box.node.role === '团队长' ? 'leader' : box.node.children?.length ? 'manager' : 'member'"
        >
          {{ (box.node.name || '?').slice(0, 1) }}
        </div>

        <div class="mindmap-card-body">
          <div class="mindmap-card-row">
            <span class="mindmap-card-name">{{ box.node.name }}</span>
            <span
              class="mindmap-badge"
              :class="box.node.role === '团队长' ? 'leader' : box.node.children?.length ? 'manager' : 'member'"
            >
              {{ box.node.role }}
            </span>
          </div>

          <div class="mindmap-card-sub">
            <span v-if="box.node.alias" class="mindmap-alias">{{ box.node.alias }}</span>
            <span v-if="(box.node.stores || []).length" class="mindmap-store-pill">
              <Store :size="10" />
              {{ box.node.stores.length }} 店
            </span>
            <span v-if="box.node.default_cut_rate" class="mindmap-cut-pill">
              {{ (Number(box.node.default_cut_rate) * 100).toFixed(1) }}%
            </span>
          </div>
        </div>

        <!-- 折叠/展开按钮 -->
        <button
          v-if="box.node.children?.length"
          type="button"
          class="mindmap-toggle-btn"
          :class="{ collapsed: collapsed[box.id] }"
          @click.stop="emit('toggle', box.id)"
          :title="collapsed[box.id] ? '展开子级' : '收起子级'"
        >
          <span class="mindmap-kid-count">{{ box.node.children.length }}</span>
        </button>

        <!-- 快速添加下级按钮 -->
        <button
          type="button"
          class="mindmap-add-btn"
          title="添加下级成员"
          @click.stop="emit('add', box.node)"
        >
          <Plus :size="12" />
        </button>
      </div>
    </div>

    <!-- Floating Glassmorphic Dock Controls -->
    <div class="mindmap-dock">
      <span class="mindmap-zoom-label">{{ Math.round(scale * 100) }}%</span>
      <button type="button" class="mindmap-dock-btn" title="放大" @click="zoomIn">
        <Plus :size="14" />
      </button>
      <button type="button" class="mindmap-dock-btn" title="缩小" @click="zoomOut">
        <Minus :size="14" />
      </button>
      <div class="mindmap-dock-divider" />
      <button type="button" class="mindmap-dock-btn" title="全屏适应" @click="fit">
        <Maximize2 :size="13" />
        <span class="text-xs">适应</span>
      </button>
      <button type="button" class="mindmap-dock-btn" title="重置原点" @click="resetZoom">
        <RotateCcw :size="13" />
      </button>
    </div>
  </div>
</template>
