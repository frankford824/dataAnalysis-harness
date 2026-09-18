<script setup>
import { computed, ref, watch } from 'vue'
import { useApp } from '../store'

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
  <aside class="org-panel" :aria-label="person ? '编辑人员' : '人员详情'">
    <header>
      <div>
        <h2>{{ person ? person.name : '选择一位人员' }}</h2>
        <p>{{ person ? `${person.role}。组长本人也是组里的成员，店铺跟组织走。` : '点树上的人，或把人拖到另一位下面调整上下级。' }}</p>
      </div>
      <n-button v-if="person" text @click="emit('close')">关闭</n-button>
    </header>
    <template v-if="person">
      <label>姓名<input v-model="form.name" maxlength="100" aria-label="姓名" /></label>
      <label>别名 / 团队名<input v-model="form.alias" maxlength="100" placeholder="例如：运营一部、宋永康组" aria-label="别名" /></label>
      <label>工号<input v-model="form.employee_no" aria-label="工号" /></label>
      <label>上级
        <n-select v-model:value="form.parent_id" :options="parentOptions" clearable filterable placeholder="无上级，即团队长" />
      </label>
      <label>作为上级时的默认抽成
        <span class="cut-field"><input v-model="form.default_cut_rate" type="number" min="0" max="100" step="0.01" aria-label="默认抽成" /><span>%</span></span>
        <small>下级商品保存时，可一键把这个比例补到本商品上。单条商品仍可改。</small>
      </label>
      <label>备注<input v-model="form.note" aria-label="备注" /></label>
      <label class="check"><input v-model="form.archived" type="checkbox" />已停用</label>
      <n-button type="primary" block :loading="saving" :disabled="!form.name.trim()" @click="submit">保存人员</n-button>
      <div class="org-stores">
        <h3>负责店铺</h3>
        <p>挂在这个人身上的店，下级一起做。商品级做货/抽点仍在提成设置里。</p>
        <n-select v-model:value="storeIds" :options="storeOptions" multiple filterable placeholder="选择店铺" />
        <n-button style="margin-top:10px" :disabled="saving" @click="saveStores">保存店铺归属</n-button>
      </div>
      <div v-if="products.length" class="org-products">
        <h3>参与的商品</h3>
        <p>这里只看当前生效的设置。改点数请到提成设置。</p>
        <ul>
          <li v-for="row in products" :key="row.store_id+row.product_id">
            <strong>{{ row.product_name || row.product_id }}</strong>
            <span>{{ row.people.filter(p => p.person_id === person.id).map(p => `${p.source==='hierarchy'?'组织抽成':(p.duty==='cut'?'抽点':'做货')} ${(Number(p.rate)*100).toFixed(2)}%`).join('、') }}</span>
          </li>
        </ul>
      </div>
    </template>
  </aside>
</template>
