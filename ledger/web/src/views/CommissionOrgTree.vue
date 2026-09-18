<script setup>
import { computed, onActivated, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import { Search, Plus, Users, Building2, AlertTriangle } from '@lucide/vue'
import { useCommission } from '../commissionStore'
import OrgTreeNode from '../components/OrgTreeNode.vue'
import OrgEditPanel from '../components/OrgEditPanel.vue'

const shared = useCommission()
const message = useMessage()
const tree = ref([])
const people = ref([])
const unassigned = ref([])
const error = ref('')
const loading = ref(false)
const saving = ref(false)
const search = ref('')
const selectedId = ref('')
const expanded = ref({})
const draggingId = ref('')
const adding = ref(null)
const newName = ref('')
const products = ref([])

async function call(path, options = {}) {
  const response = await fetch('/api/commission-v2' + path, {
    ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '未能保存，请检查填写内容')
  return body
}

function flatten(nodes, acc = []) {
  for (const node of nodes || []) {
    acc.push(node)
    flatten(node.children, acc)
  }
  return acc
}

const index = computed(() => Object.fromEntries(flatten(tree.value).map(node => [node.id, node])))
const selected = computed(() => index.value[selectedId.value] || null)
const matches = computed(() => {
  const q = search.value.trim().toLowerCase()
  const found = {}
  if (!q) return found
  for (const node of flatten(tree.value)) {
    const hay = [node.name, node.alias, node.employee_no, ...(node.stores || []).map(s => s.name)].join(' ').toLowerCase()
    if (hay.includes(q)) found[node.id] = true
  }
  return found
})
const visibleTree = computed(() => {
  const q = search.value.trim()
  if (!q) return tree.value
  const keep = new Set(Object.keys(matches.value))
  const walk = nodes => nodes
    .map(node => {
      const children = walk(node.children || [])
      if (keep.has(node.id) || children.length) return { ...node, children }
      return null
    })
    .filter(Boolean)
  return walk(tree.value)
})

const stats = computed(() => {
  const all = flatten(tree.value)
  const leaders = all.filter(n => n.role === '团队长').length
  const managers = all.filter(n => n.role === '组长').length
  const members = all.filter(n => n.role === '成员').length
  return { total: all.length, leaders, managers, members }
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await call('/org/tree')
    tree.value = data.tree || []
    people.value = data.people || []
    unassigned.value = data.unassigned_stores || []
    if (selectedId.value && !data.people.some(p => p.id === selectedId.value)) selectedId.value = ''
    shared.people = data.people || shared.people
  } catch (e) { error.value = e.message }
  finally { loading.value = false }
}

function toggle(id) { expanded.value = { ...expanded.value, [id]: expanded.value[id] === false } }
function select(node) { selectedId.value = node.id; adding.value = null }
function startAdd(parent) { adding.value = parent || { id: '', name: '根节点' }; newName.value = ''; selectedId.value = parent?.id || '' }

function expandAll() {
  const all = {}
  for (const node of flatten(tree.value)) all[node.id] = true
  expanded.value = all
}
function collapseAll() {
  const all = {}
  for (const node of flatten(tree.value)) all[node.id] = false
  expanded.value = all
}

async function createPerson() {
  const name = newName.value.trim()
  if (!name) return
  saving.value = true
  try {
    const person = await call('/org/person', {
      method: 'POST',
      body: JSON.stringify({
        person: { name, parent_id: adding.value?.id || '', alias: adding.value?.id ? '' : name },
        expected_revision: 0,
        reason: adding.value?.id ? `在${adding.value.name}下添加成员` : '新建团队',
      }),
    })
    adding.value = null
    newName.value = ''
    selectedId.value = person.id
    await load()
    message.success('已添加')
  } catch (e) { message.error(e.message) }
  finally { saving.value = false }
}

async function savePerson(form) {
  saving.value = true
  try {
    await call('/org/person', {
      method: 'POST',
      body: JSON.stringify({
        person: form,
        expected_revision: form.revision,
        reason: '更新组织人员',
      }),
    })
    await load()
    message.success('人员已保存')
  } catch (e) { message.error(e.message) }
  finally { saving.value = false }
}

async function saveStores({ person_id, store_ids }) {
  saving.value = true
  try {
    await call('/org/stores', {
      method: 'POST',
      body: JSON.stringify({ person_id, store_ids, reason: '调整负责店铺' }),
    })
    await load()
    message.success('店铺归属已保存')
  } catch (e) { message.error(e.message) }
  finally { saving.value = false }
}

async function drop({ personId, parentId }) {
  if (!personId || personId === parentId) return
  saving.value = true
  try {
    await call('/org/move', {
      method: 'POST',
      body: JSON.stringify({ person_ids: [personId], parent_id: parentId || '', reason: '拖拽调整上下级' }),
    })
    await load()
    message.success('上下级已调整')
  } catch (e) { message.error(e.message) }
  finally { saving.value = false; draggingId.value = '' }
}

function dropRoot(event) {
  event.preventDefault()
  const id = event.dataTransfer.getData('text/plain') || draggingId.value
  drop({ personId: id, parentId: '' })
}

watch(selectedId, async id => {
  products.value = []
  if (!id) return
  try {
    const query = new URLSearchParams({ limit: '40' })
    query.append('person_ids', id)
    const data = await call('/settings?' + query.toString())
    products.value = data.rows || []
  } catch { products.value = [] }
})
onActivated(load)
defineExpose({ reload: load })
</script>

<template>
  <div class="org-page">
    <div class="org-header">
      <div class="org-stats">
        <div class="org-stat"><Users :size="15" /><span>{{ stats.total }} 人</span></div>
        <div class="org-stat leader"><span class="org-stat-dot" />{{ stats.leaders }} 团队</div>
        <div class="org-stat manager" v-if="stats.managers"><span class="org-stat-dot" />{{ stats.managers }} 组</div>
        <div class="org-stat member"><span class="org-stat-dot" />{{ stats.members }} 成员</div>
      </div>
      <div class="org-toolbar">
        <n-input v-model:value="search" class="org-search" placeholder="搜索人员、别名或店铺" clearable>
          <template #prefix><Search :size="15" /></template>
        </n-input>
        <n-button text :disabled="loading" @click="expandAll">全部展开</n-button>
        <n-button text :disabled="loading" @click="collapseAll">全部收起</n-button>
        <n-button text :disabled="loading" @click="load">刷新</n-button>
        <n-button type="primary" size="small" @click="startAdd(null)"><Plus :size="14" style="margin-right:4px" />新建团队</n-button>
      </div>
    </div>
    <p class="org-lead">
      拖一个人到另一个人上面 → 变成他的下级。拖到顶部虚框 → 独立成团队长。
      <strong>组长即成员</strong>，店铺跟组织走。
    </p>
    <div v-if="error" class="commission-error" role="alert">{{ error }} <button class="text-button" @click="load">重试</button></div>
    <div class="org-layout" :class="{ loading }">
      <div class="org-tree-wrap" @dragend="draggingId=''">
        <div
          class="org-root-drop"
          :class="{ active: !!draggingId }"
          @dragover.prevent
          @drop="dropRoot"
        >
          <Building2 :size="16" />
          拖到这里成为独立团队长
        </div>
        <ul class="org-tree-root">
          <OrgTreeNode
            v-for="(node, idx) in visibleTree"
            :key="node.id"
            :node="node"
            :selected-id="selectedId"
            :matches="matches"
            :expanded="expanded"
            :dragging-id="draggingId"
            :is-last="idx === visibleTree.length - 1"
            @select="select"
            @toggle="toggle"
            @add="startAdd"
            @drag-start="draggingId=$event"
            @drop="drop"
          />
        </ul>
        <div v-if="!loading && !visibleTree.length" class="org-empty">
          {{ search ? '没有匹配的人员' : '还没有组织层级。先建一位团队长，再把其他人拖到他下面。' }}
        </div>
        <div v-if="unassigned.length && !search" class="org-unassigned">
          <AlertTriangle :size="14" style="flex:none" />
          <span>{{ unassigned.length }} 个店铺尚未挂到组织：{{ unassigned.slice(0, 8).map(s => s.name).join('、') }}{{ unassigned.length > 8 ? '…' : '' }}</span>
        </div>
      </div>
      <OrgEditPanel
        :person="selected"
        :people="people"
        :products="products"
        :saving="saving"
        @save="savePerson"
        @stores="saveStores"
        @close="selectedId=''"
      />
    </div>
    <n-modal :show="!!adding" :mask-closable="!saving" @update:show="open => { if (!open) adding = null }">
      <div class="org-add-dialog">
        <h3>{{ adding?.id ? `添加到「${adding.name}」下面` : '新建团队' }}</h3>
        <p class="org-add-hint">{{ adding?.id ? '添加后会成为这个人的下级' : '创建一个新的团队长节点' }}</p>
        <label>姓名<input v-model="newName" maxlength="100" placeholder="输入人员姓名" @keyup.enter="createPerson" /></label>
        <div class="org-add-actions">
          <n-button :disabled="saving" @click="adding=null">取消</n-button>
          <n-button type="primary" :loading="saving" :disabled="!newName.trim()" @click="createPerson">添加</n-button>
        </div>
      </div>
    </n-modal>
  </div>
</template>
