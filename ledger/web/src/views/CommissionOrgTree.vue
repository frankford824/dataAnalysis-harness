<script setup>
import { computed, onActivated, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import {
  Search,
  Plus,
  Users,
  AlertTriangle,
  Sparkles,
  LayoutGrid,
  Network,
  RefreshCw,
  Building2,
  Check,
  ChevronRight,
  ArrowRight,
} from '@lucide/vue'
import { useCommission } from '../commissionStore'
import OrgMacColumnView from '../components/OrgMacColumnView.vue'
import OrgMindMap from '../components/OrgMindMap.vue'
import OrgEditPanel from '../components/OrgEditPanel.vue'

const shared = useCommission()
const message = useMessage()

// View mode: 'mac' (macOS Miller columns) | 'mindmap' (XMind canvas)
const viewMode = ref(localStorage.getItem('org_view_mode') || 'mac')
watch(viewMode, val => localStorage.setItem('org_view_mode', val))

const tree = ref([])
const people = ref([])
const unassigned = ref([])
const error = ref('')
const loading = ref(false)
const saving = ref(false)
const search = ref('')
const selectedId = ref('')
const collapsed = ref({})
const adding = ref(null)
const newName = ref('')
const products = ref([])
const mindmapBoard = ref(null)

// Smart Auto-Organize Modal state
const showSmartModal = ref(false)
const smartLoading = ref(false)
const smartSuggestions = ref([])
const selectedSmartIds = ref([])

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

const stats = computed(() => {
  const all = flatten(tree.value)
  return {
    total: all.length,
    leaders: tree.value.length,
    members: all.length - tree.value.length,
  }
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
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

function select(node) {
  selectedId.value = node.id
  adding.value = null
}

function startAdd(parent) {
  adding.value = parent || { id: '', name: '根节点' }
  newName.value = ''
  if (parent?.id) selectedId.value = parent.id
}

function toggle(id) {
  collapsed.value = { ...collapsed.value, [id]: !collapsed.value[id] }
}

function expandAll() {
  collapsed.value = {}
}

function collapseAll() {
  const all = {}
  for (const node of flatten(tree.value)) {
    if (node.children?.length) all[node.id] = true
  }
  collapsed.value = all
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
    message.success('已添加人员')
  } catch (e) {
    message.error(e.message)
  } finally {
    saving.value = false
  }
}

async function savePerson(form) {
  saving.value = true
  try {
    await call('/org/person', {
      method: 'POST',
      body: JSON.stringify({ person: form, expected_revision: form.revision, reason: '更新组织人员' }),
    })
    await load()
    message.success('人员信息已保存')
  } catch (e) {
    message.error(e.message)
  } finally {
    saving.value = false
  }
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
  } catch (e) {
    message.error(e.message)
  } finally {
    saving.value = false
  }
}

async function drop({ personId, parentId }) {
  if (!personId || personId === parentId) return
  saving.value = true
  try {
    await call('/org/move', {
      method: 'POST',
      body: JSON.stringify({ person_ids: [personId], parent_id: parentId || '', reason: '调整上下级关系' }),
    })
    await load()
    message.success(parentId ? '已调整至新的上级' : '已升级为独立团队长')
  } catch (e) {
    message.error(e.message)
  } finally {
    saving.value = false
  }
}

// Smart Auto-Organize Preview & Apply
async function openSmartOrganize() {
  smartLoading.value = true
  showSmartModal.value = true
  try {
    const res = await call('/org/infer')
    smartSuggestions.value = res.suggestions || []
    selectedSmartIds.value = smartSuggestions.value.map(s => s.person_id)
  } catch (e) {
    message.error('获取归属建议失败: ' + e.message)
    showSmartModal.value = false
  } finally {
    smartLoading.value = false
  }
}

function toggleSmartSelect(pid) {
  if (selectedSmartIds.value.includes(pid)) {
    selectedSmartIds.value = selectedSmartIds.value.filter(id => id !== pid)
  } else {
    selectedSmartIds.value.push(pid)
  }
}

async function applySmartOrganize() {
  const chosen = smartSuggestions.value
    .filter(s => selectedSmartIds.value.includes(s.person_id))
    .map(s => ({ person_id: s.person_id, parent_id: s.suggested_parent_id }))

  if (!chosen.length) {
    message.warning('请至少勾选一位要理顺的人员')
    return
  }

  smartLoading.value = true
  try {
    const res = await call('/org/infer/apply', {
      method: 'POST',
      body: JSON.stringify({ items: chosen, reason: '智能按店铺理顺架构' }),
    })
    message.success(`已一键理顺 ${res.applied || chosen.length} 位人员的组织架构！`)
    showSmartModal.value = false
    await load()
  } catch (e) {
    message.error('应用归属失败: ' + e.message)
  } finally {
    smartLoading.value = false
  }
}

watch(selectedId, async id => {
  products.value = []
  if (!id) return
  try {
    const query = new URLSearchParams({ limit: '40' })
    query.append('person_ids', id)
    products.value = (await call('/settings?' + query.toString())).rows || []
  } catch {
    products.value = []
  }
})

onActivated(load)
defineExpose({ reload: load })
</script>

<template>
  <div class="org-container">
    <!-- TOP APP BAR -->
    <header class="org-navbar">
      <!-- Left: Title & Quick Stats -->
      <div class="org-nav-left">
        <div class="org-nav-brand">
          <Building2 :size="18" class="text-indigo-600" />
          <h1>销售组织架构</h1>
        </div>
        <div class="org-stat-pills">
          <span class="org-pill leaders">
            <span class="dot" />
            {{ stats.leaders }} 团队
          </span>
          <span class="org-pill members">
            <span class="dot" />
            {{ stats.total }} 人员
          </span>
          <span v-if="unassigned.length" class="org-pill unassigned" title="尚未分配到具体人员的店铺">
            <AlertTriangle :size="11" />
            {{ unassigned.length }} 待分配店铺
          </span>
        </div>
      </div>

      <!-- Center: High-End View Switcher -->
      <div class="org-nav-center">
        <div class="org-view-switcher">
          <button
            type="button"
            class="org-view-btn"
            :class="{ active: viewMode === 'mac' }"
            @click="viewMode = 'mac'"
          >
            <LayoutGrid :size="15" />
            <span>macOS 栏式分级</span>
          </button>
          <button
            type="button"
            class="org-view-btn"
            :class="{ active: viewMode === 'mindmap' }"
            @click="viewMode = 'mindmap'"
          >
            <Network :size="15" />
            <span>XMind 思维导图</span>
          </button>
        </div>
      </div>

      <!-- Right: Action Buttons -->
      <div class="org-nav-right">
        <!-- 智能理顺团队快捷按钮 -->
        <button
          type="button"
          class="org-smart-btn"
          title="根据店铺负责人前缀一键识别团队成员归属"
          @click="openSmartOrganize"
        >
          <Sparkles :size="14" class="text-indigo-500" />
          <span>智能理顺团队</span>
        </button>

        <!-- 视图专属按钮 -->
        <template v-if="viewMode === 'mindmap'">
          <n-button text size="small" :disabled="loading" @click="expandAll">全部展开</n-button>
          <n-button text size="small" :disabled="loading" @click="collapseAll">全部收起</n-button>
          <n-button text size="small" :disabled="loading" @click="mindmapBoard?.fit()">适应画布</n-button>
        </template>

        <button
          type="button"
          class="org-action-icon-btn"
          title="刷新数据"
          :disabled="loading"
          @click="load"
        >
          <RefreshCw :size="15" :class="{ 'animate-spin': loading }" />
        </button>

        <n-button
          type="primary"
          size="small"
          @click="startAdd(null)"
        >
          <Plus :size="14" style="margin-right: 4px" />
          <span>新建团队</span>
        </n-button>
      </div>
    </header>

    <!-- ERROR BANNER -->
    <div v-if="error" class="commission-error" role="alert">
      {{ error }}
      <button class="text-button" @click="load">重试</button>
    </div>

    <!-- MAIN WORKSPACE -->
    <main class="org-main-area" :class="{ loading }">
      <!-- VIEW 1: macOS Miller Columns -->
      <OrgMacColumnView
        v-if="viewMode === 'mac'"
        :tree="tree"
        :people="people"
        :products="products"
        :selected-id="selectedId"
        :saving="saving"
        @select="select"
        @save-person="savePerson"
        @save-stores="saveStores"
        @move="drop"
        @add-child="startAdd"
        @create-team="startAdd(null)"
      />

      <!-- VIEW 2: XMind Mind Map Canvas -->
      <div v-else class="org-mindmap-view-shell">
        <OrgMindMap
          ref="mindmapBoard"
          :tree="tree"
          :people="people"
          :selected-id="selectedId"
          :collapsed="collapsed"
          @select="select"
          @drop="drop"
          @add="startAdd"
          @toggle="toggle"
        />

        <!-- Slide-out Edit Inspector for Mindmap view -->
        <transition name="slide-panel">
          <OrgEditPanel
            v-if="selected"
            class="mindmap-inspector-dock"
            :person="selected"
            :people="people"
            :products="products"
            :saving="saving"
            @save="savePerson"
            @stores="saveStores"
            @close="selectedId = ''"
          />
        </transition>
      </div>
    </main>

    <!-- SMART AUTO-ORGANIZE MODAL -->
    <n-modal
      :show="showSmartModal"
      :mask-closable="!smartLoading"
      @update:show="open => { if (!open && !smartLoading) showSmartModal = false }"
    >
      <div class="org-smart-dialog">
        <div class="org-smart-dialog-header">
          <div class="org-smart-dialog-icon">
            <Sparkles :size="20" class="text-indigo-600" />
          </div>
          <div>
            <h3>智能按店铺理顺架构</h3>
            <p>系统自动扫描店铺负责前缀，检测到以下人员与团队长的对应归属关系：</p>
          </div>
        </div>

        <div v-if="smartLoading && !smartSuggestions.length" class="org-smart-loading">
          <RefreshCw :size="24" class="animate-spin text-indigo-500" />
          <p>正在分析店铺前缀与人员关系...</p>
        </div>

        <div v-else-if="!smartSuggestions.length" class="org-smart-empty">
          <Check :size="28" class="text-emerald-500" />
          <p>当前所有人员已合理归属于对应团队长，无需调整！</p>
        </div>

        <div v-else class="org-smart-list">
          <div
            v-for="item in smartSuggestions"
            :key="item.person_id"
            class="org-smart-item"
            :class="{ selected: selectedSmartIds.includes(item.person_id) }"
            @click="toggleSmartSelect(item.person_id)"
          >
            <input
              type="checkbox"
              :checked="selectedSmartIds.includes(item.person_id)"
              @click.stop="toggleSmartSelect(item.person_id)"
            />
            <div class="org-smart-names">
              <span class="smart-member">{{ item.person_name }}</span>
              <ArrowRight :size="13" class="smart-arrow" />
              <span class="smart-leader">{{ item.suggested_parent_name }} 团队</span>
            </div>
            <div class="org-smart-reason">
              {{ item.reason }}
            </div>
          </div>
        </div>

        <div class="org-smart-actions">
          <span class="org-smart-count">
            已选 {{ selectedSmartIds.length }} / {{ smartSuggestions.length }} 位人员
          </span>
          <div class="org-smart-btns">
            <n-button :disabled="smartLoading" @click="showSmartModal = false">取消</n-button>
            <n-button
              type="primary"
              :loading="smartLoading"
              :disabled="!selectedSmartIds.length"
              @click="applySmartOrganize"
            >
              一键应用组织架构
            </n-button>
          </div>
        </div>
      </div>
    </n-modal>

    <!-- ADD PERSON / TEAM MODAL -->
    <n-modal
      :show="!!adding"
      :mask-closable="!saving"
      @update:show="open => { if (!open) adding = null }"
    >
      <div class="org-add-dialog">
        <h3>{{ adding?.id ? `添加到「${adding.name}」下面` : '新建团队' }}</h3>
        <p class="org-add-hint">{{ adding?.id ? '添加后会成为该节点的下级成员' : '创建一个新的独立团队长' }}</p>
        <label>
          姓名
          <input
            v-model="newName"
            maxlength="100"
            placeholder="输入人员真实姓名"
            @keyup.enter="createPerson"
          />
        </label>
        <div class="org-add-actions">
          <n-button :disabled="saving" @click="adding = null">取消</n-button>
          <n-button
            type="primary"
            :loading="saving"
            :disabled="!newName.trim()"
            @click="createPerson"
          >
            确认添加
          </n-button>
        </div>
      </div>
    </n-modal>
  </div>
</template>
