<script setup>
import { computed, ref, watch } from 'vue'
import { useApp } from '../store'
import { User, Store, Package, X } from '@lucide/vue'

const props = defineProps({
  person: { type: Object, default: null },
  people: { type: Array, default: () => [] },
  products: { type: Array, default: () => [] },
  saving: { type: Boolean, default: false },
})
const emit = defineEmits(['save', 'stores', 'close'])
const app = useApp()

const form = ref(blank())
const storeIds = ref([])
watch(() => props.person, person => {
  if (!person) { form.value = blank(); storeIds.value = []; return }
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
  storeIds.value = (person.stores || []).map(s => s.id)
}, { immediate: true })

const parentOptions = computed(() => props.people
  .filter(p => p.id !== form.value.id)
  .map(p => ({ value: p.id, label: p.alias ? `${p.name}（${p.alias}）` : p.name })))
const storeOptions = computed(() => app.stores.map(s => ({ value: s.id, label: s.name })))
const initials = computed(() => (props.person?.name || '?').slice(0, 1))
const roleColors = { '团队长': 'leader', '组长': 'manager', '成员': 'member' }
const roleClass = computed(() => roleColors[props.person?.role] || 'member')

function blank() {
  return { id: '', name: '', alias: '', employee_no: '', parent_id: null, default_cut_rate: null, note: '', archived: false, revision: 0 }
}
function submit() {
  emit('save', {
    ...form.value,
    parent_id: form.value.parent_id || '',
    default_cut_rate: form.value.default_cut_rate == null || form.value.default_cut_rate === ''
      ? '' : (Number(form.value.default_cut_rate) / 100).toFixed(8),
  })
}
function saveStores() { emit('stores', { person_id: form.value.id, store_ids: [...storeIds.value] }) }
</script>

<template>
  <aside class="org-panel" :class="{ empty: !person }" :aria-label="person ? '编辑人员' : '人员详情'">
    <template v-if="!person">
      <div class="org-panel-empty">
        <User :size="40" />
        <h3>选择一位人员</h3>
        <p>点击左侧树中的人员卡片查看详情，<br/>或拖动卡片调整上下级关系。</p>
      </div>
    </template>
    <template v-else>
      <header class="org-panel-header">
        <div class="org-panel-identity">
          <div :class="['org-panel-avatar', roleClass]">{{ initials }}</div>
          <div>
            <h2>{{ person.name }}</h2>
            <span :class="['tree-role-badge', roleClass]">{{ person.role }}</span>
          </div>
        </div>
        <button class="org-panel-close" @click="emit('close')" title="关闭"><X :size="16" /></button>
      </header>

      <div class="org-panel-form">
        <div class="org-field">
          <label>姓名</label>
          <input v-model="form.name" maxlength="100" aria-label="姓名" />
        </div>
        <div class="org-field">
          <label>别名 / 团队名</label>
          <input v-model="form.alias" maxlength="100" placeholder="如：运营一部" aria-label="别名" />
        </div>
        <div class="org-field-row">
          <div class="org-field">
            <label>工号</label>
            <input v-model="form.employee_no" aria-label="工号" />
          </div>
          <div class="org-field">
            <label>默认抽成</label>
            <div class="org-cut-input">
              <input v-model="form.default_cut_rate" type="number" min="0" max="100" step="0.01" aria-label="默认抽成" />
              <span>%</span>
            </div>
          </div>
        </div>
        <div class="org-field">
          <label>上级</label>
          <n-select v-model:value="form.parent_id" :options="parentOptions" clearable filterable placeholder="无上级 = 团队长" size="small" />
        </div>
        <div class="org-field">
          <label>备注</label>
          <input v-model="form.note" aria-label="备注" placeholder="备注信息" />
        </div>
        <label class="org-check"><input v-model="form.archived" type="checkbox" />已停用</label>
        <n-button type="primary" block :loading="saving" :disabled="!form.name.trim()" @click="submit">保存人员</n-button>
      </div>

      <div class="org-panel-section">
        <h3><Store :size="14" /> 负责店铺</h3>
        <p>挂在此人名下的店铺，下级共同负责。</p>
        <n-select v-model:value="storeIds" :options="storeOptions" multiple filterable placeholder="选择店铺" size="small" />
        <n-button size="small" style="margin-top:8px" :disabled="saving" @click="saveStores">保存店铺</n-button>
      </div>

      <div v-if="products.length" class="org-panel-section">
        <h3><Package :size="14" /> 参与商品</h3>
        <p>当前生效的提成设置。改点数请到提成设置页。</p>
        <ul class="org-product-list">
          <li v-for="row in products" :key="row.store_id+row.product_id">
            <strong>{{ row.product_name || row.product_id }}</strong>
            <span>{{ row.people.filter(p => p.person_id === person.id).map(p => `${p.source==='hierarchy'?'组织抽成':(p.duty==='cut'?'抽点':'做货')} ${(Number(p.rate)*100).toFixed(2)}%`).join('、') }}</span>
          </li>
        </ul>
      </div>
    </template>
  </aside>
</template>
