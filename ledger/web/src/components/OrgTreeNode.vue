<script setup>
import { computed, ref } from 'vue'
import { ChevronDown, ChevronRight, Plus } from '@lucide/vue'

defineOptions({ name: 'OrgTreeNode' })

const props = defineProps({
  node: { type: Object, required: true },
  depth: { type: Number, default: 0 },
  selectedId: { type: String, default: '' },
  matches: { type: Object, default: () => ({}) },
  expanded: { type: Object, required: true },
  draggingId: { type: String, default: '' },
  isLast: { type: Boolean, default: false },
})
const emit = defineEmits(['select', 'toggle', 'add', 'drag-start', 'drop'])

const open = computed(() => props.expanded[props.node.id] !== false)
const hasKids = computed(() => (props.node.children || []).length > 0)
const stores = computed(() => props.node.stores || [])
const initials = computed(() => (props.node.name || '?').slice(0, 1))
const roleColors = { '团队长': 'leader', '组长': 'manager', '成员': 'member' }
const roleClass = computed(() => roleColors[props.node.role] || 'member')
const cutText = computed(() => {
  const rate = Number(props.node.default_cut_rate || 0)
  return rate ? `${Number((rate * 100).toFixed(4))}%` : ''
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
  <li class="tree-item" :class="{ 'is-last': isLast }">
    <div class="tree-line" v-if="depth > 0" />
    <div
      class="tree-card"
      :class="{
        selected: selectedId === node.id,
        match: matched,
        drop: dropTarget,
        archived: node.archived,
        [roleClass]: true,
      }"
      draggable="true"
      :aria-label="`${node.role} ${node.name}`"
      @click="emit('select', node)"
      @dragstart="dragStart"
      @dragover="allowDrop"
      @dragleave="leave"
      @drop="drop"
    >
      <div :class="['tree-avatar', roleClass]">{{ initials }}</div>
      <div class="tree-body">
        <div class="tree-name-row">
          <strong>{{ node.name }}</strong>
          <span v-if="node.alias && node.alias !== node.name" class="tree-alias">{{ node.alias }}</span>
          <span :class="['tree-role-badge', roleClass]">{{ node.role }}</span>
          <span v-if="cutText" class="tree-cut-badge">{{ cutText }}</span>
        </div>
        <div class="tree-detail" v-if="stores.length || node.child_count">
          <span v-if="node.child_count" class="tree-count">{{ node.child_count }} 人</span>
          <span v-if="stores.length" class="tree-stores">{{ stores.map(s => s.name).join('、') }}</span>
        </div>
        <div class="tree-detail" v-else>
          <span class="tree-empty">未分配店铺</span>
        </div>
      </div>
      <div class="tree-actions">
        <button
          v-if="hasKids"
          class="tree-toggle"
          type="button"
          :aria-label="open ? '收起' : '展开'"
          @click.stop="emit('toggle', node.id)"
        >
          <ChevronDown v-if="open" :size="14" />
          <ChevronRight v-else :size="14" />
          <span class="tree-toggle-count">{{ node.children.length }}</span>
        </button>
        <button class="tree-add-btn" type="button" title="添加下级" @click.stop="emit('add', node)">
          <Plus :size="12" />
        </button>
      </div>
    </div>
    <ul v-if="hasKids && open" class="tree-children">
      <OrgTreeNode
        v-for="(child, idx) in node.children"
        :key="child.id"
        :node="child"
        :depth="depth + 1"
        :selected-id="selectedId"
        :matches="matches"
        :expanded="expanded"
        :dragging-id="draggingId"
        :is-last="idx === node.children.length - 1"
        @select="emit('select', $event)"
        @toggle="emit('toggle', $event)"
        @add="emit('add', $event)"
        @drag-start="emit('drag-start', $event)"
        @drop="emit('drop', $event)"
      />
    </ul>
  </li>
</template>
