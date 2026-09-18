<script setup>
import { computed, onActivated, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import { Search, Plus } from '@lucide/vue'
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
function startAdd(parent) { adding.value = parent || { id: '', name: '团队长' }; newName.value = ''; selectedId.value = parent?.id || '' }

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
    <div class="org-toolbar">
      <n-input v-model:value="search" class="org-search" placeholder="搜索人员、别名或店铺" clearable>
        <template #prefix><Search :size="15" /></template>
      </n-input>
      <n-button text :disabled="loading" @click="load">刷新</n-button>
      <n-button type="primary" @click="startAdd(null)"><Plus :size="14" style="margin-right:6px" />新建团队</n-button>
    </div>
    <p class="org-lead">团队和组都是具体的人。拖到另一位下面就是下级；店铺挂在负责人身上，下级跟着做。组长本人也算组里的成员。</p>
    <div v-if="error" class="commission-error" role="alert">{{ error }} <button class="text-button" @click="load">重试</button></div>
    <div class="org-layout" :class="{ loading }">
      <div class="org-tree" @dragend="draggingId=''">
        <div
          class="org-root-drop"
          @dragover.prevent
          @drop="dropRoot"
        >拖到这里成为团队长</div>
        <OrgTreeNode
          v-for="node in visibleTree"
          :key="node.id"
          :node="node"
          :selected-id="selectedId"
          :matches="matches"
          :expanded="expanded"
          :dragging-id="draggingId"
          @select="select"
          @toggle="toggle"
          @add="startAdd"
          @drag-start="draggingId=$event"
          @drop="drop"
        />
        <div v-if="!loading && !visibleTree.length" class="org-empty">
          {{ search ? '没有匹配的人员' : '还没有组织层级。先建一位团队长，再把其他人拖到他下面。' }}
        </div>
        <div v-if="unassigned.length && !search" class="org-unassigned">
          尚未挂到组织上的店铺：{{ unassigned.map(s => s.name).join('、') }}
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
        <h3>添加{{ adding?.id ? `到 ${adding.name} 下面` : '团队长' }}</h3>
        <label>姓名<input v-model="newName" maxlength="100" @keyup.enter="createPerson" /></label>
        <div class="org-add-actions">
          <n-button :disabled="saving" @click="adding=null">取消</n-button>
          <n-button type="primary" :loading="saving" :disabled="!newName.trim()" @click="createPerson">添加</n-button>
        </div>
      </div>
    </n-modal>
  </div>
</template>
