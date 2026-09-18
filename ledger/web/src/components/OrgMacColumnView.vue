<script setup>
import { computed, ref, watch } from 'vue'
import {
  Crown,
  User,
  Users,
  ChevronRight,
  Plus,
  Search,
  Store,
  Package,
  GripVertical,
  ArrowRight,
  Briefcase,
  AlertCircle,
  X,
} from '@lucide/vue'
import { useApp } from '../store'

const props = defineProps({
  tree: { type: Array, default: () => [] },
  people: { type: Array, default: () => [] },
  products: { type: Array, default: () => [] },
  selectedId: { type: String, default: '' },
  saving: { type: Boolean, default: false },
})

const emit = defineEmits(['select', 'save-person', 'save-stores', 'move', 'add-child', 'create-team'])

const app = useApp()
const teamSearch = ref('')
const selectedTeamId = ref('')
const activePersonId = ref('')
const dragOverTargetId = ref('')
const draggingPersonId = ref('')

// Flatten helpers
function flatten(nodes, acc = []) {
  for (const node of nodes || []) {
    acc.push(node)
    flatten(node.children, acc)
  }
  return acc
}

const allNodes = computed(() => flatten(props.tree))
const nodeMap = computed(() => Object.fromEntries(allNodes.value.map(n => [n.id, n])))

// Separate actual Teams (roots) vs true unassigned people
// If a root has 0 children and 0 stores, or is marked as unassigned, we categorize them
const teams = computed(() => {
  const q = teamSearch.value.trim().toLowerCase()
  return props.tree.filter(team => {
    if (!q) return true
    const haystack = [team.name, team.alias, ...(team.stores || []).map(s => s.name)].join(' ').toLowerCase()
    return haystack.includes(q)
  })
})

// Current selected team
const currentTeam = computed(() => {
  if (!selectedTeamId.value && teams.value.length) {
    return teams.value[0]
  }
  return nodeMap.value[selectedTeamId.value] || teams.value[0] || null
})

// Keep selected team in sync
watch(currentTeam, team => {
  if (team && !selectedTeamId.value) {
    selectedTeamId.value = team.id
  }
}, { immediate: true })

// Active person for Column 3 Inspector
const activePerson = computed(() => {
  const targetId = activePersonId.value || props.selectedId || currentTeam.value?.id
  return nodeMap.value[targetId] || null
})

// When external selectedId changes, sync
watch(() => props.selectedId, id => {
  if (id && nodeMap.value[id]) {
    activePersonId.value = id
    // Find which team this person belongs to
    let cur = nodeMap.value[id]
    while (cur?.parent_id && nodeMap.value[cur.parent_id]) {
      cur = nodeMap.value[cur.parent_id]
    }
    if (cur?.id) selectedTeamId.value = cur.id
  }
})

// Inspector form state
const form = ref(blankForm())
const formStoreIds = ref([])

watch(activePerson, person => {
  if (!person) {
    form.value = blankForm()
    formStoreIds.value = []
    return
  }
  form.value = {
    id: person.id,
    name: person.name || '',
    alias: person.alias || '',
    employee_no: person.employee_no || '',
    parent_id: person.parent_id || null,
    default_cut_rate: person.default_cut_rate ? Number((Number(person.default_cut_rate) * 100).toFixed(6)) : null,
    note: person.note || '',
    archived: !!person.archived,
    revision: person.revision || 0,
  }
  formStoreIds.value = (person.stores || []).map(s => s.id)
}, { immediate: true })

function blankForm() {
  return { id: '', name: '', alias: '', employee_no: '', parent_id: null, default_cut_rate: null, note: '', archived: false, revision: 0 }
}

const parentOptions = computed(() => {
  return props.people
    .filter(p => p.id !== form.value.id)
    .map(p => ({ value: p.id, label: p.alias ? `${p.name}（${p.alias}）` : p.name }))
})

const storeOptions = computed(() => app.stores.map(s => ({ value: s.id, label: s.name })))

function selectTeam(team) {
  selectedTeamId.value = team.id
  activePersonId.value = team.id
  emit('select', team)
}

function selectMember(person) {
  activePersonId.value = person.id
  emit('select', person)
}

// Drag & Drop handlers
function onDragStart(event, person) {
  draggingPersonId.value = person.id
  event.dataTransfer.effectAllowed = 'move'
  event.dataTransfer.setData('text/plain', person.id)
}

function onDragOver(event, targetId) {
  event.preventDefault()
  event.dataTransfer.dropEffect = 'move'
  if (draggingPersonId.value !== targetId) {
    dragOverTargetId.value = targetId
  }
}

function onDragLeave(targetId) {
  if (dragOverTargetId.value === targetId) {
    dragOverTargetId.value = ''
  }
}

function onDrop(event, targetParentId) {
  event.preventDefault()
  const personId = draggingPersonId.value || event.dataTransfer.getData('text/plain')
  dragOverTargetId.value = ''
  draggingPersonId.value = ''
  if (!personId || personId === targetParentId) return
  emit('move', { personId, parentId: targetParentId })
}

function onDragEnd() {
  dragOverTargetId.value = ''
  draggingPersonId.value = ''
}

function submitPerson() {
  emit('save-person', {
    ...form.value,
    parent_id: form.value.parent_id || '',
    default_cut_rate: form.value.default_cut_rate == null || form.value.default_cut_rate === ''
      ? '' : (Number(form.value.default_cut_rate) / 100).toFixed(8),
  })
}

function submitStores() {
  emit('save-stores', {
    person_id: form.value.id,
    store_ids: [...formStoreIds.value],
  })
}

function removeStore(sid) {
  formStoreIds.value = formStoreIds.value.filter(id => id !== sid)
  submitStores()
}

const roleTheme = {
  团队长: { bg: 'bg-indigo-50 text-indigo-700 border-indigo-200', dot: '#6366f1' },
  组长: { bg: 'bg-cyan-50 text-cyan-700 border-cyan-200', dot: '#0891b2' },
  成员: { bg: 'bg-slate-100 text-slate-700 border-slate-200', dot: '#64748b' },
}

const initials = computed(() => (activePerson.value?.name || '?').slice(0, 1))
</script>

<template>
  <div class="mac-columns-shell">
    <!-- COLUMN 1: TEAMS LIST -->
    <aside class="mac-column col-teams" aria-label="团队总览">
      <div class="mac-col-header">
        <div class="mac-col-title">
          <Users :size="16" class="text-indigo-600" />
          <span>销售团队</span>
          <span class="mac-count-pill">{{ teams.length }}</span>
        </div>
        <button
          type="button"
          class="mac-mini-btn"
          title="新建独立团队"
          @click="emit('create-team')"
        >
          <Plus :size="14" />
          <span>新团队</span>
        </button>
      </div>

      <div class="mac-search-wrap">
        <Search :size="14" class="mac-search-icon" />
        <input
          v-model="teamSearch"
          type="text"
          placeholder="过滤团队或店铺..."
          class="mac-col-input"
        />
      </div>

      <div class="mac-list-scroll">
        <div
          v-for="team in teams"
          :key="team.id"
          class="mac-team-card"
          :class="{
            active: selectedTeamId === team.id,
            'drop-target': dragOverTargetId === team.id,
          }"
          @click="selectTeam(team)"
          @dragover="onDragOver($event, team.id)"
          @dragleave="onDragLeave(team.id)"
          @drop="onDrop($event, team.id)"
        >
          <div class="mac-team-avatar">
            {{ (team.name || '?').slice(0, 1) }}
          </div>
          <div class="mac-team-info">
            <div class="mac-team-name-row">
              <span class="mac-team-name">{{ team.name }}</span>
              <span v-if="team.alias" class="mac-team-alias">{{ team.alias }}</span>
            </div>
            <div class="mac-team-meta-row">
              <span class="mac-meta-tag members">
                <User :size="11" />
                {{ (team.children || []).length + 1 }} 人
              </span>
              <span v-if="(team.stores || []).length" class="mac-meta-tag stores">
                <Store :size="11" />
                {{ team.stores.length }} 店
              </span>
              <span v-if="team.default_cut_rate" class="mac-meta-tag cut">
                抽 {{ (Number(team.default_cut_rate) * 100).toFixed(1) }}%
              </span>
            </div>
          </div>
          <ChevronRight :size="15" class="mac-team-arrow" />
        </div>

        <div v-if="!teams.length" class="mac-empty-hint">
          无匹配团队
        </div>
      </div>
    </aside>

    <!-- COLUMN 2: TEAM HIERARCHY TREE -->
    <section class="mac-column col-hierarchy" aria-label="团队架构">
      <div class="mac-col-header">
        <div class="mac-col-title">
          <Crown :size="16" class="text-amber-500" />
          <span class="truncate">{{ currentTeam?.name || '团队' }} 组织架构</span>
          <span class="mac-count-pill">
            {{ ((currentTeam?.child_count || 0) + 1) }} 人
          </span>
        </div>
        <button
          v-if="currentTeam"
          type="button"
          class="mac-mini-btn primary"
          title="在当前团队添加成员"
          @click="emit('add-child', currentTeam)"
        >
          <Plus :size="14" />
          <span>加成员</span>
        </button>
      </div>

      <div v-if="!currentTeam" class="mac-col-empty">
        <Users :size="32" class="mac-dim-icon" />
        <p>请在左侧选择一个销售团队</p>
      </div>

      <div v-else class="mac-list-scroll mac-tree-container">
        <!-- 团队长固定置顶卡片 -->
        <div
          class="mac-member-card leader"
          :class="{
            active: activePersonId === currentTeam.id,
            'drop-target': dragOverTargetId === currentTeam.id,
          }"
          @click="selectMember(currentTeam)"
          @dragover="onDragOver($event, currentTeam.id)"
          @dragleave="onDragLeave(currentTeam.id)"
          @drop="onDrop($event, currentTeam.id)"
        >
          <div class="mac-member-avatar leader">
            {{ (currentTeam.name || '?').slice(0, 1) }}
          </div>
          <div class="mac-member-content">
            <div class="mac-member-top">
              <span class="mac-member-name">{{ currentTeam.name }}</span>
              <span class="mac-role-badge leader">团队长</span>
              <span v-if="currentTeam.default_cut_rate" class="mac-cut-badge">
                抽成 {{ (Number(currentTeam.default_cut_rate) * 100).toFixed(1) }}%
              </span>
            </div>
            <div class="mac-member-stores">
              <span v-if="(currentTeam.stores || []).length" class="mac-stores-text">
                负责 {{ currentTeam.stores.length }} 家店铺：{{ currentTeam.stores.map(s => s.name).slice(0, 2).join('、') }}{{ currentTeam.stores.length > 2 ? '等' : '' }}
              </span>
              <span v-else class="mac-empty-text">组长本人也是成员，暂未指定直属店铺</span>
            </div>
          </div>
        </div>

        <!-- 下级成员列表（组长与组员） -->
        <div class="mac-subordinates-tree">
          <div
            v-for="(member, idx) in currentTeam.children || []"
            :key="member.id"
            class="mac-tree-branch"
          >
            <!-- 树枝连接导线 -->
            <div class="mac-tree-line" />

            <!-- 成员卡片 -->
            <div
              class="mac-member-card"
              :class="{
                active: activePersonId === member.id,
                'drop-target': dragOverTargetId === member.id,
                manager: member.children?.length > 0,
              }"
              draggable="true"
              @dragstart="onDragStart($event, member)"
              @dragend="onDragEnd"
              @dragover="onDragOver($event, member.id)"
              @dragleave="onDragLeave(member.id)"
              @drop="onDrop($event, member.id)"
              @click="selectMember(member)"
            >
              <GripVertical :size="14" class="mac-drag-handle" title="按住拖拽以调整层级或转调团队" />
              <div :class="['mac-member-avatar', member.children?.length ? 'manager' : 'member']">
                {{ (member.name || '?').slice(0, 1) }}
              </div>
              <div class="mac-member-content">
                <div class="mac-member-top">
                  <span class="mac-member-name">{{ member.name }}</span>
                  <span :class="['mac-role-badge', member.children?.length ? 'manager' : 'member']">
                    {{ member.children?.length ? '组长' : '成员' }}
                  </span>
                  <span v-if="member.default_cut_rate" class="mac-cut-badge">
                    抽 {{ (Number(member.default_cut_rate) * 100).toFixed(1) }}%
                  </span>
                </div>
                <div class="mac-member-stores">
                  <span v-if="(member.stores || []).length" class="mac-stores-text">
                    {{ member.stores.map(s => s.name).slice(0, 2).join('、') }}{{ member.stores.length > 2 ? ` 等${member.stores.length}店` : '' }}
                  </span>
                  <span v-else class="mac-empty-text">待分配店铺</span>
                </div>
              </div>
            </div>

            <!-- 若该成员是组长，展示其孙级成员 -->
            <div v-if="member.children?.length" class="mac-nested-children">
              <div
                v-for="sub in member.children"
                :key="sub.id"
                class="mac-member-card nested"
                :class="{
                  active: activePersonId === sub.id,
                  'drop-target': dragOverTargetId === sub.id,
                }"
                draggable="true"
                @dragstart="onDragStart($event, sub)"
                @dragend="onDragEnd"
                @dragover="onDragOver($event, sub.id)"
                @dragleave="onDragLeave(sub.id)"
                @drop="onDrop($event, sub.id)"
                @click.stop="selectMember(sub)"
              >
                <GripVertical :size="13" class="mac-drag-handle" />
                <div class="mac-member-avatar sub-member">
                  {{ (sub.name || '?').slice(0, 1) }}
                </div>
                <div class="mac-member-content">
                  <div class="mac-member-top">
                    <span class="mac-member-name">{{ sub.name }}</span>
                    <span class="mac-role-badge member">组员</span>
                  </div>
                  <div class="mac-member-stores">
                    <span v-if="(sub.stores || []).length" class="mac-stores-text">
                      {{ sub.stores.map(s => s.name).slice(0, 2).join('、') }}
                    </span>
                    <span v-else class="mac-empty-text">跟随组内</span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div v-if="!(currentTeam.children || []).length" class="mac-empty-branch">
            <p>该团队下暂无其他成员。</p>
            <p class="mac-empty-sub">可将其他成员拖拽至此处，或点击右上角「加成员」。</p>
          </div>
        </div>
      </div>
    </section>

    <!-- COLUMN 3: INSPECTOR DETAIL PANEL -->
    <main class="mac-column col-inspector" aria-label="人员档案与配置">
      <div v-if="!activePerson" class="mac-col-empty">
        <User :size="36" class="mac-dim-icon" />
        <h3>选择一位人员</h3>
        <p>在左侧选择团队或成员，即可查看并编辑其店铺与提成权限。</p>
      </div>

      <template v-else>
        <!-- 顶部身份信息卡片 -->
        <header class="mac-inspector-header">
          <div class="mac-avatar-large" :class="activePerson.role === '团队长' ? 'leader' : activePerson.children?.length ? 'manager' : 'member'">
            {{ initials }}
          </div>
          <div class="mac-header-meta">
            <div class="mac-header-title-row">
              <h2>{{ activePerson.name }}</h2>
              <span :class="['mac-role-pill', activePerson.role === '团队长' ? 'leader' : activePerson.children?.length ? 'manager' : 'member']">
                {{ activePerson.role }}
              </span>
            </div>
            <p class="mac-header-sub">
              工号：{{ activePerson.employee_no || '未填写' }} · 归属：{{ activePerson.parent_id ? (nodeMap[activePerson.parent_id]?.name || '上级') : '独立负责人' }}
            </p>
          </div>
        </header>

        <!-- 表单与详情内容 -->
        <div class="mac-inspector-scroll">
          <div class="mac-form-group">
            <h4 class="mac-section-title">
              <Briefcase :size="14" />
              <span>人员基本信息</span>
            </h4>
            
            <div class="mac-form-grid">
              <div class="mac-field">
                <label>姓名</label>
                <input v-model="form.name" class="mac-input" placeholder="输入姓名" />
              </div>
              <div class="mac-field">
                <label>别名 / 团队名</label>
                <input v-model="form.alias" class="mac-input" placeholder="例如：运营一部" />
              </div>
              <div class="mac-field">
                <label>工号</label>
                <input v-model="form.employee_no" class="mac-input" placeholder="员工工号" />
              </div>
              <div class="mac-field">
                <label>默认抽成比例</label>
                <div class="mac-input-suffix">
                  <input
                    v-model="form.default_cut_rate"
                    type="number"
                    min="0"
                    max="100"
                    step="0.01"
                    class="mac-input"
                    placeholder="如 2.5"
                  />
                  <span>%</span>
                </div>
              </div>
            </div>

            <div class="mac-field mt-3">
              <label>上级直属负责人</label>
              <n-select
                v-model:value="form.parent_id"
                :options="parentOptions"
                clearable
                filterable
                placeholder="无上级 = 独立团队长"
                size="small"
              />
            </div>

            <div class="mac-field mt-3">
              <label>备注</label>
              <input v-model="form.note" class="mac-input" placeholder="补充说明信息" />
            </div>

            <div class="mac-form-actions">
              <label class="mac-checkbox">
                <input v-model="form.archived" type="checkbox" />
                <span>停用此账号</span>
              </label>
              <n-button
                type="primary"
                size="small"
                :loading="saving"
                :disabled="!form.name.trim()"
                @click="submitPerson"
              >
                保存人员信息
              </n-button>
            </div>
          </div>

          <!-- 负责店铺管理 -->
          <div class="mac-form-group">
            <div class="mac-section-header">
              <h4 class="mac-section-title">
                <Store :size="14" />
                <span>负责店铺配置 ({{ formStoreIds.length }})</span>
              </h4>
            </div>
            <p class="mac-section-desc">
              挂在此人名下的店铺，下级成员共同负责。改动后请点击保存。
            </p>

            <div class="mac-store-tags-box">
              <span
                v-for="sid in formStoreIds"
                :key="sid"
                class="mac-store-chip"
              >
                <Store :size="12" />
                <span>{{ storeOptions.find(o => o.value === sid)?.label || sid }}</span>
                <button type="button" class="mac-chip-del" @click="removeStore(sid)"><X :size="12" /></button>
              </span>
              <span v-if="!formStoreIds.length" class="mac-no-store-tip">
                暂未分配专属店铺
              </span>
            </div>

            <div class="mac-store-add-row">
              <n-select
                v-model:value="formStoreIds"
                :options="storeOptions"
                multiple
                filterable
                placeholder="搜索并选择关联店铺..."
                size="small"
                class="flex-1"
              />
              <n-button
                size="small"
                type="default"
                :loading="saving"
                @click="submitStores"
              >
                保存店铺
              </n-button>
            </div>
          </div>

          <!-- 参与商品与提成规则 -->
          <div class="mac-form-group">
            <h4 class="mac-section-title">
              <Package :size="14" />
              <span>当前生效提成商品 ({{ products.length }})</span>
            </h4>
            <p class="mac-section-desc">
              该人员在各个商品中的做货或抽成设置。点数修改请前往「提成设置」页面。
            </p>

            <div v-if="products.length" class="mac-product-table-wrap">
              <table class="mac-product-table">
                <thead>
                  <tr>
                    <th>店铺 / 商品</th>
                    <th>职责类型</th>
                    <th class="text-right">提成点数</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="row in products"
                    :key="row.store_id + row.product_id"
                  >
                    <td>
                      <div class="mac-prod-title">{{ row.product_name || row.product_id }}</div>
                      <div class="mac-prod-store">{{ row.store_name }}</div>
                    </td>
                    <td>
                      <span
                        v-for="p in row.people.filter(p => p.person_id === activePerson.id)"
                        :key="p.person_id"
                        class="mac-duty-tag"
                        :class="p.source === 'hierarchy' ? 'hierarchy' : p.duty === 'cut' ? 'cut' : 'produce'"
                      >
                        {{ p.source === 'hierarchy' ? '组织抽点' : p.duty === 'cut' ? '抽点' : '做货' }}
                      </span>
                    </td>
                    <td class="text-right mac-prod-rate">
                      {{
                        row.people
                          .filter(p => p.person_id === activePerson.id)
                          .map(p => `${(Number(p.rate) * 100).toFixed(2)}%`)
                          .join('、')
                      }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div v-else class="mac-empty-products">
              暂无参与的商品提成明细
            </div>
          </div>
        </div>
      </template>
    </main>
  </div>
</template>
