<script setup>
import { computed, ref } from 'vue'
import { ChevronDown, ChevronRight, GripVertical } from '@lucide/vue'

defineOptions({ name: 'OrgTreeNode' })

const props = defineProps({
  node: { type: Object, required: true },
  depth: { type: Number, default: 0 },
  selectedId: { type: String, default: '' },
  matches: { type: Object, default: () => ({}) },
  expanded: { type: Object, required: true },
  draggingId: { type: String, default: '' },
})
const emit = defineEmits(['select', 'toggle', 'add', 'drag-start', 'drop'])

const open = computed(() => props.expanded[props.node.id] !== false)
const hasKids = computed(() => (props.node.children || []).length > 0)
const stores = computed(() => props.node.stores || [])
const cutText = computed(() => {
  const rate = Number(props.node.default_cut_rate || 0)
  return rate ? `${Number((rate * 100).toFixed(6))}%` : ''
})
const matched = computed(() => !!props.matches[props.node.id])
const dropTarget = ref(false)

function dragStart(event) {
  event.dataTransfer.setData('text/plain', props.node.id)
  event.dataTransfer.effectAllowed = 'move'
  emit('drag-start', props.node.id)
}
function allowDrop(event) {
  if (!props.draggingId || props.draggingId === props.node.id) return
  event.preventDefault()
  dropTarget.value = true
}
function leave() { dropTarget.value = false }
function drop(event) {
  event.preventDefault()
  event.stopPropagation()
  dropTarget.value = false
  const id = event.dataTransfer.getData('text/plain') || props.draggingId
  if (id && id !== props.node.id) emit('drop', { personId: id, parentId: props.node.id })
}
</script>

<template>
  <div class="org-node" :class="{ root: depth === 0 }">
    <div
      class="org-card"
      :class="{
        selected: selectedId === node.id,
        match: matched,
        drop: dropTarget,
        archived: node.archived,
      }"
      :style="{ marginLeft: depth * 22 + 'px' }"
      draggable="true"
      :aria-label="`${node.role} ${node.name}`"
      @click="emit('select', node)"
      @dragstart="dragStart"
      @dragover="allowDrop"
      @dragleave="leave"
      @drop="drop"
    >
      <button
        v-if="hasKids"
        class="org-toggle"
        type="button"
        :aria-label="open ? '收起下级' : '展开下级'"
        @click.stop="emit('toggle', node.id)"
      >
        <ChevronDown v-if="open" :size="14" />
        <ChevronRight v-else :size="14" />
      </button>
      <span v-else class="org-toggle spacer" />
      <GripVertical class="org-grip" :size="14" aria-hidden="true" />
      <div class="org-main">
        <div class="org-title">
          <strong>{{ node.name }}</strong>
          <span v-if="node.alias" class="org-alias">{{ node.alias }}</span>
          <span class="org-role">{{ node.role }}</span>
          <span v-if="cutText" class="org-cut">默认抽成 {{ cutText }}</span>
        </div>
        <div class="org-meta">
          <span v-if="node.child_count">{{ node.child_count }} 人</span>
          <span v-if="stores.length">{{ stores.map(s => s.name).join('、') }}</span>
          <span v-else-if="!node.child_count" class="muted">未分配店铺</span>
        </div>
      </div>
      <button class="org-add" type="button" @click.stop="emit('add', node)">＋ 下级</button>
    </div>
    <OrgTreeNode
      v-if="open"
      v-for="child in node.children"
      :key="child.id"
      :node="child"
      :depth="depth + 1"
      :selected-id="selectedId"
      :matches="matches"
      :expanded="expanded"
      :dragging-id="draggingId"
      @select="emit('select', $event)"
      @toggle="emit('toggle', $event)"
      @add="emit('add', $event)"
      @drag-start="emit('drag-start', $event)"
      @drop="emit('drop', $event)"
    />
  </div>
</template>
