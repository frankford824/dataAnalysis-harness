<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import { useApp } from '../store'
import { money, percent } from '../format'
import PageHead from '../components/PageHead.vue'

const app = useApp()
const message = useMessage()
const section = ref('products')
const status = ref(null)
const people = ref([])
const externalUsers = ref([])
const data = ref({})
const search = ref('')
const cursor = ref('')
const cursorStack = ref([])
const missing = ref(false)
const loading = ref(false)
const error = ref('')
const busy = ref(false)
const declaredName = ref('')
const loginName = ref('')
const loginPassword = ref('')
const showLogin = ref(false)
const showPerson = ref(false)
const personForm = ref({})
const importFile = ref(null)
const importDate = ref('2026-06-01')
const batchId = ref('')
const importStatus = ref('review')
const batch = ref(null)
const selected = ref(null)
const selectedIds = ref([])
const resolvingRow = ref(null)
const showEditor = ref(false)
const editor = ref({})
const editStore = ref('')
const editProduct = ref('')
const reason = ref('')
const terminateAt = ref('')
const terminateMode = ref('hold')
const showTerminate = ref(false)
const showDetail = ref(false)
const detail = ref(null)
const detailId = ref('')
const detailOffset = ref(0)
const showAccount = ref(false)
const accountName = ref('')
const accountPassword = ref('')
const showPassword = ref(false)
const oldPassword = ref('')
const newPassword = ref('')
const policyForm = ref({effective_from:'2026-06-01',store_id:'',base_node:'net_profit',on_loss:'inherit',wages:'skip_preview'})
let sequence = 0
let timer

const tabs = [['products', '店铺商品'], ['schemes', '提成关系'], ['people', '人员'],
  ['imports', '历史迁移'], ['policies', '计算口径'], ['payout', '账期提成'], ['calculations', '计算版本'], ['history', '变更记录']]
const roles = ['高级组长', '组长', '运营1', '运营2', '运营3', '运营', '固定分成']
const clone = (value) => JSON.parse(JSON.stringify(value))
const canWrite = computed(() => status.value?.auth_mode === 'declared' ? !!declaredName.value.trim() : !!status.value?.actor)
const storeName = (id) => app.stores.find(s => s.id === id)?.name || (id.startsWith('unmapped:') ? '待绑定台账店铺' : id)
const personName = (id) => people.value.find(p => p.id === id)?.name || id
const currentActor = computed(() => status.value?.actor?.name || (declaredName.value ? `${declaredName.value}（登记身份）` : '尚未登录'))
const beijingNow = () => new Date().toLocaleString('sv-SE', { timeZone: 'Asia/Shanghai' }).replace(' ', 'T').slice(0, 16)
const displayTime = (value) => value ? new Date(value.includes('+') || value.endsWith('Z') ? value : `${value}+08:00`).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—'
const namesFor = (s) => (s.allocations || []).map(a => `${personName(a.person_id)} ${percent(Number(a.rate))}`).join('、')
const actionName = (value) => ({ 'policy.publish':'启用计算口径', 'operator.create':'创建操作账号', 'operator.password':'修改登录密码', 'import.resolve':'确认迁移记录', 'scheme.resolve':'确认关系', 'scheme.bulk':'批量调整', 'scheme.publish': '启用方案', 'scheme.save': '保存草稿', 'scheme.import': '迁移启用', 'scheme.terminate': '终止关系', 'scheme.restore': '恢复为草稿', 'person.save': '调整人员', 'person.import': '迁移人员', 'import.stage': '登记原表', 'import.activate': '启用迁移' }[value] || value)
const query = (params) => { const q = new URLSearchParams(); Object.entries(params).forEach(([k,v]) => { if (v !== '' && v != null) q.set(k, v) }); return q.toString() }

async function call(path, options = {}) {
  const headers = { ...(options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }), ...(options.headers || {}) }
  if (declaredName.value) headers['X-Commission-Actor'] = declaredName.value.trim()
  const response = await fetch(`/api/commission-v2${path}`, { ...options, headers })
  const body = await response.json()
  if (!response.ok) {
    if (response.status === 401) showLogin.value = true
    throw new Error(typeof body.detail === 'string' ? body.detail : '请求未完成，请检查输入')
  }
  return body
}
async function act(fn) {
  busy.value = true
  try { return await fn() } catch (e) { message.error(e.message, { duration: 6000 }); return null }
  finally { busy.value = false }
}
async function refreshStatus() {
  const old = status.value
  status.value = await call('/status')
  if (old && (old.revision !== status.value.revision || old.catalog_refreshed_at !== status.value.catalog_refreshed_at)) load()
}
async function loadPeople() { const result = await call('/people'); people.value = result.people; externalUsers.value = result.external_users }
async function load() {
  const n = ++sequence
  loading.value = true; error.value = ''
  try {
    let next
    const params = { store_id: app.storeId, search: search.value, after: cursor.value, limit: 60 }
    if (section.value === 'products') next = await call(`/products?${query({ ...params, missing: missing.value })}`)
    else if (section.value === 'schemes') next = await call(`/schemes?${query(params)}`)
    else if (section.value === 'people') { await loadPeople(); next = {} }
    else if (section.value === 'policies') next = await call('/policies')
    else if (section.value === 'payout') next = await call(`/payout?${query({ store_id: app.storeId, period: app.period })}`)
    else if (section.value === 'calculations') next = await call(`/calculations?${query({ store_id: app.storeId, period: app.period })}`)
    else if (section.value === 'history') next = await call(`/history?${query({ after: cursor.value || 0 })}`)
    else { await refreshStatus(); if (batchId.value) await loadBatch(); next = {} }
    if (n === sequence) data.value = next
  } catch (e) { if (n === sequence) error.value = e.message }
  finally { if (n === sequence) loading.value = false }
}
function nextPage() { selectedIds.value=[]; cursorStack.value.push(cursor.value); cursor.value = data.value.next_after; load() }
function previousPage() { selectedIds.value=[]; cursor.value = cursorStack.value.pop() || ''; load() }
function reset() { selectedIds.value=[]; cursor.value = ''; cursorStack.value = []; load() }
watch([section, () => app.storeId, () => app.period, missing], reset)
let searchTimer
watch(search, () => { clearTimeout(searchTimer); searchTimer = setTimeout(reset, 250) })
onMounted(async () => {
  await act(async () => { await refreshStatus(); await loadPeople(); await load() })
  timer = setInterval(() => refreshStatus().catch(() => {}), 5000)
})
onUnmounted(() => { clearInterval(timer); clearTimeout(searchTimer) })

async function login() {
  await act(async () => { await call('/session', { method: 'POST', body: JSON.stringify({ name: loginName.value, password: loginPassword.value }) }); loginPassword.value = ''; showLogin.value = false; await refreshStatus(); message.success('已登录') })
}
async function logout() { await act(async () => { await call('/session', { method: 'DELETE' }); await refreshStatus() }) }
function newPerson(existing = null) {
  personForm.value = existing ? { ...existing } : { name: '', employee_no: '', external_user_id: '', note: '', archived: false }
  reason.value = ''; showPerson.value = true
}
async function savePerson() {
  await act(async () => { await call('/people', { method: 'POST', body: JSON.stringify({ person: personForm.value, expected_revision: personForm.value.revision || 0, reason: reason.value }) }); showPerson.value = false; await loadPeople(); await refreshStatus(); message.success('人员及变更记录已保存') })
}
async function openScheme(row) {
  resolvingRow.value = null
  await act(async () => {
    const id = row.scheme_id || row.id
    selected.value = id ? await call(`/schemes/${id}`) : null
    editStore.value = selected.value?.store_id || row.store_id || app.storeId || ''
    editProduct.value = selected.value?.product_id || row.product_id || ''
    const versions = selected.value?.versions || []
    const version = versions.find(v => v.id === (selected.value.draft_version || selected.value.active_version))
    editor.value = clone(version?.body || { product_name: row.product_name || '', segments: [{ valid_from: beijingNow(), valid_to: '', mode: 'distribute', total_rate: '0.05', allocations: [] }] })
    reason.value = ''; showEditor.value = true
  })
}
function addSegment() { editor.value.segments.push({ valid_from: beijingNow(), valid_to: '', mode: 'distribute', total_rate: '0.05', allocations: [] }) }
function changeMode(s) { if (s.mode !== 'distribute') { s.allocations = []; s.total_rate = '0' } }
async function saveScheme(publish = false) {
  await act(async () => {
    const path = resolvingRow.value ? `/imports/${batchId.value}/resolve/${resolvingRow.value.row_no}` : '/schemes'
    await call(path, { method: 'POST', body: JSON.stringify({ store_id: editStore.value, product_id: editProduct.value, body: editor.value, expected_revision: selected.value?.revision || 0, reason: reason.value, publish }) })
    showEditor.value = false; await refreshStatus(); await load()
    message.success(publish ? '已启用，受影响账期已排队重算' : '草稿已保存，正式关系尚未改变')
  })
}
async function openImportRow(row) {
  await openScheme({store_id:row.store_id,product_id:row.product_id,product_name:row.payload.product_name})
  resolvingRow.value = row
  const found = await call(`/schemes?${query({store_id:row.store_id,search:row.product_id})}`)
  const existing = found.schemes.find(s=>s.product_id===row.product_id && s.store_id===row.store_id)
  if (existing) selected.value = await call(`/schemes/${existing.id}`)
  editor.value = { product_name:row.payload.product_name || '', segments:[{valid_from:batch.value.effective_from,valid_to:'',mode:row.payload.mode,amount_hold:row.payload.amount_hold || '',total_rate:row.payload.total_rate,allocations:row.payload.resolved_allocations.map(a=>({role:a.role,rate:a.rate,person_id:people.value.filter(p=>p.name===a.person).length===1 ? people.value.find(p=>p.name===a.person).id : ''}))}] }
  reason.value = ''
}
async function bulkApply() {
  await act(async()=>{
    const template = editor.value.segments.at(-1)
    if (!template) throw new Error('请先登记新的生效时间段')
    const chosen = (data.value.schemes || []).filter(s=>selectedIds.value.includes(s.id))
    if (chosen.some(s=>s.draft_version)) throw new Error('选择中有未发布草稿，请先比较并处理草稿')
    const schemes = chosen.map(s=>({store_id:s.store_id,product_id:s.product_id,expected_revision:s.revision,publish:true,body:{product_name:s.product_name,source:{type:'bulk_template',template_scheme_id:selected.value?.id || ''},segments:[...(s.body.segments || []).filter(old=>old.valid_from<template.valid_from).map(old=>({...old,valid_to:old.valid_to && old.valid_to<template.valid_from ? old.valid_to : template.valid_from})),clone(template)]}}))
    await call('/bulk',{method:'POST',body:JSON.stringify({reason:reason.value,schemes})})
    showEditor.value=false; selectedIds.value=[]; await refreshStatus(); load(); message.success('批量新版本已启用，之前时间区间保留')
  })
}
async function publishDraft() {
  await act(async () => { await call(`/schemes/${selected.value.id}/publish`, { method: 'POST', body: JSON.stringify({ expected_revision: selected.value.revision, reason: reason.value }) }); showEditor.value = false; await refreshStatus(); load(); message.success('草稿已启用，已排队重算') })
}
async function restoreVersion(version) {
  editor.value = clone(version.body)
  reason.value = `参考第${version.revision}版调整`
  message.info('历史内容已载入；保存将新增版本，原记录保留')
}
function requestTerminate() { terminateAt.value = beijingNow(); terminateMode.value = 'hold'; reason.value = ''; showTerminate.value = true }
async function terminate() {
  await act(async () => { await call(`/schemes/${selected.value.id}/terminate`, { method: 'POST', body: JSON.stringify({ at: terminateAt.value, mode: terminateMode.value, reason: reason.value, expected_revision: selected.value.revision }) }); showTerminate.value = false; showEditor.value = false; await refreshStatus(); load(); message.success('关系已按指定时间终止，历史版本保留') })
}
async function syncCatalog() { await act(async () => { await call('/catalog/refresh', { method: 'POST' }); await refreshStatus(); message.success('目录同步已排队') }) }
async function upload() {
  if (!importFile.value) return
  await act(async () => { const form = new FormData(); form.append('file', importFile.value); await call(`/imports?${query({ effective_from: importDate.value })}`, { method: 'POST', body: form }); await refreshStatus(); message.success('原文件已收下，正在检查全部工作表和绑定关系') })
}
async function loadBatch(after = 0) { batch.value = await call(`/imports/${batchId.value}?${query({ status: importStatus.value, after })}`) }
async function chooseBatch(id) { batchId.value = id; await act(() => loadBatch()) }
async function activateBatch() {
  await act(async () => { await call(`/imports/${batchId.value}/activate`, { method: 'POST', body: JSON.stringify({ reason: reason.value }) }); await refreshStatus(); message.success('合格关系启用已排队，待确认记录不会自动启用') })
}
async function showCalculation(id, offset = 0) {
  await act(async () => { detailId.value = id; detailOffset.value = offset; detail.value = await call(`/details/${id}?offset=${offset}`); showDetail.value = true })
}
async function createAccount() {
  await act(async () => { await call('/operators', { method: 'POST', body: JSON.stringify({ name: accountName.value, password: accountPassword.value }) }); accountPassword.value = ''; showAccount.value = false; message.success('操作账号已创建') })
}
async function changePassword() {
  await act(async()=>{ await call('/password',{method:'POST',body:JSON.stringify({old_password:oldPassword.value,new_password:newPassword.value})}); oldPassword.value='';newPassword.value='';showPassword.value=false;await refreshStatus();showLogin.value=true;message.success('密码已修改，请重新登录') })
}
async function savePolicy() { await act(async()=>{ await call('/policies',{method:'POST',body:JSON.stringify({...policyForm.value,expected_revision:data.value.revisions?.[policyForm.value.store_id] || 0,reason:reason.value})}); await refreshStatus(); await load(); message.success('新计算口径已保存，相关账期排队重算') }) }
function ratesText(body) { return (body?.segments || []).map(s => `${s.valid_from.slice(0,10)}起：${s.mode === 'exclude' ? '不提成' : s.mode === 'hold' ? '待确认' : namesFor(s)}`).join('；') }
</script>

<template>
  <div class="commission-workspace">
    <PageHead title="提成" hint="按店铺和宝贝登记分点关系，保留有效时间、每次变更和账期结果。" />
    <div class="toolbar">
      <span class="dim">{{ currentActor }}</span>
      <template v-if="status?.auth_mode === 'declared'"><label>操作人 <input v-model="declaredName" placeholder="每次操作将记录此姓名" aria-label="操作人" /></label></template>
      <n-button v-else-if="!status?.actor" size="small" @click="showLogin = true">登录提成管理</n-button>
      <n-button v-else size="small" @click="logout">退出登录</n-button>
      <n-button v-if="status?.actor" size="small" @click="showPassword = true">修改密码</n-button>
      <n-button v-if="status?.actor?.admin" size="small" @click="showAccount = true">创建操作账号</n-button>
      <router-link to="/commission/legacy">旧版回查</router-link>
    </div>
    <div class="scope-note">
      目录 {{ status?.counts.catalog?.toLocaleString() || 0 }} 个商品 · 已启用 {{ status?.counts.active?.toLocaleString() || 0 }} 条关系 · {{ status?.counts.pending || 0 }} 家店待重算
      <span v-if="status?.catalog_refreshed_at"> · 目录更新 {{ displayTime(status.catalog_refreshed_at) }}</span>
      <span v-if="status?.feed"> · 订单{{ status.feed.caught_up ? '已追平本轮更新' : '正在追赶更新' }}{{ status.feed.unmapped_stores ? ` · ${status.feed.unmapped_stores}家店待确认映射` : '' }}</span>
    </div>
    <nav class="tabs" aria-label="提成工作区"><button v-for="[key,label] in tabs" :key="key" :class="{active:section===key}" @click="section=key">{{ label }}</button></nav>
    <div v-if="status?.jobs.some(j => ['queued','running'].includes(j.status))" class="notice">正在处理：{{ status.jobs.filter(j => ['queued','running'].includes(j.status)).map(j => ({catalog:'目录同步',import:'原表核查',activate:'方案启用'}[j.kind] || j.kind)).join('、') }}。完成后会更新，已结账结果保留。</div>
    <div v-if="status?.jobs[0]?.status === 'failed'" class="error">最近任务未完成：{{ status.jobs[0].error }}</div>
    <div v-if="status?.feed?.last_error" class="error">订单同步提示：{{ status.feed.last_error }}</div>
    <div v-if="error" class="error" role="alert">{{ error }} <n-button size="tiny" @click="load">重试</n-button></div>

    <template v-if="section === 'products' || section === 'schemes'">
      <div class="toolbar">
        <input v-model="search" placeholder="搜索宝贝ID或商品名称" aria-label="搜索商品" />
        <label v-if="section==='products'"><input v-model="missing" type="checkbox" /> 仅尚未登记方案</label>
        <n-button v-if="section==='products'" :disabled="!canWrite" :loading="busy" @click="syncCatalog">同步商品目录</n-button>
        <n-button :disabled="!canWrite" @click="openScheme({})">新增关系</n-button>
        <a :href="`/api/commission-v2/export/schemes?${query({store_id:app.storeId})}`">导出现行关系</a>
        <a :href="`/api/commission-v2/export/schemes?${query({store_id:app.storeId,all_versions:true})}`">导出全部历史</a>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th v-if="section==='schemes'"><input type="checkbox" aria-label="选择本页关系" :checked="!!data.schemes?.length && selectedIds.length===data.schemes.length" @change="selectedIds=$event.target.checked ? data.schemes.map(s=>s.id) : []" /></th><th>店铺</th><th>宝贝与名称</th><th>{{ section==='products' ? '目录状态' : '有效区间与分点' }}</th><th>关系</th><th></th></tr></thead>
        <tbody v-if="section==='products'"><tr v-for="p in data.products || []" :key="`${p.store_id}:${p.product_id}`">
          <td>{{ storeName(p.store_id) }}</td><td><div>{{ p.product_name || '名称待补' }}</div><small>{{ p.product_id }}</small></td>
          <td>{{ p.payload.origin==='order_observation' ? '新订单已发现' : (p.payload.listed ? '在售' : '目录未标在售') }}<br /><small v-if="p.payload.origin==='order_observation'">等待外部目录汇总</small><small v-else>历史销售 {{ p.payload.sales?.lines_total ?? p.payload.lines_total ?? 0 }} 行</small></td>
          <td>{{ p.active_version ? '已启用' : p.draft_version ? '有草稿' : '待登记' }}</td>
          <td><n-button size="small" :disabled="p.store_id.startsWith('unmapped:')" @click="openScheme(p)">{{ p.scheme_id ? '查看 / 调整' : '登记' }}</n-button></td>
        </tr></tbody>
        <tbody v-else><tr v-for="s in data.schemes || []" :key="s.id"><td><input v-model="selectedIds" type="checkbox" :value="s.id" :aria-label="`选择关系 ${s.product_id}`" /></td><td>{{ storeName(s.store_id) }}</td><td>{{ s.product_name || '名称待补' }}<br /><small>{{ s.product_id === '*' ? '店铺默认' : s.product_id }}</small></td><td class="wide">{{ ratesText(s.body) }}</td><td>{{ s.active_version ? '已启用' : '未启用' }}{{ s.draft_version ? ' · 有草稿' : '' }}<br /><small>第{{ s.revision }}版</small></td><td><n-button size="small" @click="openScheme(s)">查看 / 调整</n-button></td></tr></tbody>
      </table></div>
      <p v-if="!loading && !(data.products?.length || data.schemes?.length)" class="empty">暂无记录。可同步目录，或登记历史商品关系。</p>
      <p v-if="selectedIds.length" class="dim">本页已选择{{ selectedIds.length }}条。打开任一关系后，可从新生效时间起批量套用末段方案。</p>
      <div class="toolbar"><n-button :disabled="!cursorStack.length" @click="previousPage">上一页</n-button><n-button :disabled="!data.has_more" @click="nextPage">下一页</n-button><span v-if="loading">正在读取…</span></div>
    </template>

    <template v-if="section==='people'">
      <div class="toolbar"><n-button :disabled="!canWrite" @click="newPerson()">登记人员</n-button><span class="dim">ERP账号仅用于身份核对。历史离职人员也可登记，不自动合并同名人员。</span></div>
      <div class="table-wrap"><table><thead><tr><th>姓名</th><th>工号</th><th>ERP账号ID</th><th>状态</th><th>说明</th><th></th></tr></thead><tbody><tr v-for="p in people" :key="p.id"><td>{{ p.name }}</td><td>{{ p.employee_no || '—' }}</td><td>{{ p.external_user_id || '未对应' }}</td><td>{{ p.archived ? '已归档' : '使用中' }}</td><td>{{ p.note }}</td><td><n-button :disabled="!canWrite" size="small" @click="newPerson(p)">调整</n-button></td></tr></tbody></table></div>
    </template>

    <template v-if="section==='imports'">
      <div class="notice">原表按五组完整表头识别；冲突、占位或身份疑点会保留待确认。合格记录按指定日期启用，此前关系和已结账结果保留。</div>
      <div class="toolbar"><label>统一生效日期 <input v-model="importDate" type="date" /></label><input type="file" accept=".xlsx" aria-label="原始提成Excel" @change="importFile=$event.target.files[0]" /><n-button :disabled="!canWrite || !importFile" :loading="busy" @click="upload">上传并核查原表</n-button></div>
      <div class="table-wrap"><table><thead><tr><th>来源</th><th>生效时间</th><th>核查</th><th>状态</th><th></th></tr></thead><tbody><tr v-for="b in status?.imports || []" :key="b.id"><td>{{ b.filename }}<br /><small>{{ b.sha.slice(0,16) }}</small></td><td>{{ b.effective_from.replace('T',' ') }}</td><td>{{ b.summary.rows?.toLocaleString() }}行 · 合格{{ b.summary.ready || 0 }} · 待确认{{ b.summary.review || 0 }}</td><td>{{ b.status==='activated' ? `已启用${b.summary.activated_groups}组` : '待启用' }}</td><td><n-button size="small" @click="chooseBatch(b.id)">查看核查</n-button></td></tr></tbody></table></div>
      <section v-if="batch" class="panel"><h3>{{ batch.filename }}</h3><div class="toolbar"><select v-model="importStatus" @change="loadBatch()" aria-label="核查状态"><option value="review">待确认</option><option value="ready">合格</option><option value="">全部</option></select><a :href="`/api/commission-v2/export/import/${batchId}`">导出完整核查表</a><input v-model="reason" placeholder="启用原因" aria-label="迁移启用原因" /><n-button :disabled="!canWrite || !reason.trim() || batch.status==='activated'" :loading="busy" @click="activateBatch">按指定日期启用合格关系</n-button></div>
        <div class="table-wrap"><table><thead><tr><th>原行</th><th>原店铺 / 宝贝</th><th>原人员分点</th><th>核查结果</th><th></th></tr></thead><tbody><tr v-for="r in batch.rows" :key="r.row_no"><td>{{ r.row_no }}</td><td>{{ r.payload.source_store }}<br /><small>{{ r.product_id }}</small></td><td>{{ r.payload.allocations.filter(a=>a.person).map(a=>`${a.role} ${a.person} ${a.rate_raw || '空'}`).join('；') }}</td><td>{{ r.issues.join('；') || (r.status==='resolved' ? '已人工处理' : '合格') }}</td><td><n-button size="small" :disabled="!canWrite" @click="openImportRow(r)">核对并登记</n-button></td></tr></tbody></table></div><n-button v-if="batch.has_more" @click="loadBatch(batch.next_after)">后续记录</n-button>
      </section>
    </template>

    <template v-if="section==='policies'">
      <div class="notice">计算口径按账期月份生效，人员分点仍按订单时间匹配。当前登记新版本不会覆盖已结账结果。</div>
      <section class="panel form-modal"><div class="form-grid"><label>适用范围<select v-model="policyForm.store_id"><option value="">全部店铺</option><option v-for="s in app.stores" :key="s.id" :value="s.id">{{ s.name }}</option></select></label><label>生效月份首日<input v-model="policyForm.effective_from" type="date" /></label><label>基数<select v-model="policyForm.base_node"><option v-for="b in data.bases || []" :key="b.id" :value="b.id">{{ b.name }}</option></select></label><label>亏损订单<select v-model="policyForm.on_loss"><option value="inherit">沿用各店原设置</option><option value="deduct">倒扣</option><option value="skip">不计</option></select></label><label>涉及工资扣减的关系<select v-model="policyForm.wages"><option value="pending">保留关系，暂不计算金额</option><option value="skip_preview">先不扣工资，标记试算</option></select></label><label>调整原因<input v-model="reason" /></label></div><n-button type="primary" :disabled="!canWrite || !reason.trim()" :loading="busy" @click="savePolicy">保存新口径版本</n-button></section>
      <h3>口径历史</h3><div class="table-wrap"><table><thead><tr><th>范围</th><th>生效</th><th>基数</th><th>工资</th><th>操作人及原因</th></tr></thead><tbody><tr v-for="p in data.policies || []" :key="p.id"><td>{{ p.store_id ? storeName(p.store_id) : '全部店铺' }}</td><td>{{ p.effective_from }}</td><td>{{ data.bases?.find(b=>b.id===p.body.base_node)?.name || p.body.base_node }}</td><td>{{ p.body.wages==='skip_preview' ? '先不扣工资试算' : '工资口径待确认' }}</td><td>{{ p.actor }} · {{ p.reason }}</td></tr></tbody></table></div>
    </template>
    <template v-if="section==='payout'">
      <div class="notice">{{ data.note }}</div><p class="total">已算部分提成 <strong>{{ money(Number(data.total || 0)) }}</strong></p>
      <div class="table-wrap"><table><thead><tr><th>人员</th><th>提成金额</th><th>参与店期</th></tr></thead><tbody><tr v-for="p in data.people || []" :key="p.person_id"><td>{{ p.person }}</td><td class="num">{{ money(Number(p.amount)) }}</td><td>{{ p.stores }}</td></tr></tbody></table></div>
      <h3>店铺账期</h3><div class="table-wrap"><table><thead><tr><th>店铺</th><th>月份</th><th>状态</th><th>基数</th><th>提成</th><th>说明</th><th></th></tr></thead><tbody><tr v-for="s in data.stores || []" :key="s.store_id+s.period"><td>{{ storeName(s.store_id) }}</td><td>{{ s.period }}</td><td>{{ s.state==='closed' ? '已结账 · 冻结版本' : '未结账' }}</td><td class="num">{{ money(s.base_total) }}<br /><small>{{ s.base_name }} · {{ s.on_loss==='skip' ? '亏损不计' : '亏损倒扣' }}</small></td><td class="num">{{ money(s.total) }}</td><td>{{ s.notes.join('；') }}<small v-if="s.engine==='legacy'">历史算法结果</small></td><td><n-button v-if="s.calculation_id" size="small" @click="showCalculation(s.calculation_id)">逐单明细</n-button></td></tr></tbody></table></div>
    </template>
    <template v-if="section==='calculations'">
      <div class="notice">这里保留每次计算。非当前展示版本为历史或调整试算；不会覆盖已结账结果。</div>
      <div class="table-wrap"><table><thead><tr><th>店铺 / 账期</th><th>计算时间</th><th>版本性质</th><th>基数 / 提成</th><th>待分配订单</th><th></th></tr></thead><tbody><tr v-for="c in data.calculations || []" :key="c.id"><td>{{ storeName(c.store_id) }}<br />{{ c.period }}</td><td>{{ displayTime(c.at) }}</td><td>{{ c.shown ? (c.closed ? '已结账版本' : '当前结果') : '历史 / 调整试算' }}</td><td>{{ money(c.base_total) }} / {{ money(c.total) }}</td><td>{{ c.unassigned_orders }}</td><td><n-button size="small" @click="showCalculation(c.id)">查看与导出</n-button></td></tr></tbody></table></div>
    </template>
    <template v-if="section==='history'">
      <div class="toolbar"><a href="/api/commission-v2/export/history">导出完整变更记录</a></div>
      <div class="table-wrap"><table><thead><tr><th>时间</th><th>操作人</th><th>动作</th><th>原因</th><th>变更内容</th></tr></thead><tbody><tr v-for="e in data.events || []" :key="e.id"><td>{{ displayTime(e.at) }}</td><td>{{ e.actor }}</td><td>{{ actionName(e.action) }}</td><td>{{ e.reason }}</td><td><template v-if="e.after.body">{{ storeName(e.after.store_id) }} / {{ e.after.product_id }}<br />{{ ratesText(e.after.body) }}</template><template v-else>{{ e.after.name || e.after.filename || '记录已留存，可导出完整前后值' }}</template></td></tr></tbody></table></div><n-button v-if="data.events?.length" @click="nextPage">后续变更</n-button>
    </template>

    <n-modal v-model:show="showLogin" preset="card" title="登录提成管理" class="form-modal" style="max-width:440px">
      <p>登录仅用于记录提成配置的操作人，查看数据无需登录。</p><label>账号<input v-model="loginName" autocomplete="username" /></label><label>密码<input v-model="loginPassword" type="password" autocomplete="current-password" @keyup.enter="login" /></label><n-button type="primary" :loading="busy" @click="login">登录</n-button>
      <p v-if="status?.counts.operator===0" class="dim">尚未初始化管理账号，请由部署管理员创建。</p>
    </n-modal>
    <n-modal v-model:show="showPerson" preset="card" title="人员登记与调整" class="form-modal" style="max-width:560px">
      <label>姓名<input v-model="personForm.name" /></label><label>工号<input v-model="personForm.employee_no" /></label><label>对应ERP账号（可留空）<select v-model="personForm.external_user_id"><option value="">未对应</option><option v-for="u in externalUsers" :key="u.user_id" :value="u.user_id">{{ u.user_name }} · {{ u.user_id }}{{ u.enabled ? '' : ' · 停用' }}</option></select></label><label>说明<input v-model="personForm.note" /></label><label><input v-model="personForm.archived" type="checkbox" /> 人员已归档（历史关系保留）</label><label>变更原因<input v-model="reason" /></label><n-button type="primary" :disabled="!reason.trim()" :loading="busy" @click="savePerson">保存人员</n-button>
    </n-modal>
    <n-modal v-model:show="showEditor" preset="card" title="提成关系与有效时间" class="form-modal relation-modal" style="width:min(1040px,95vw)">
      <div class="form-grid"><label>店铺<select v-model="editStore" :disabled="!!selected"><option value="">选择店铺</option><option v-for="s in app.stores" :key="s.id" :value="s.id">{{ s.name }}</option></select></label><label>宝贝ID（*表示店铺默认）<input v-model="editProduct" :disabled="!!selected" /></label><label>商品名称<input v-model="editor.product_name" /></label></div>
      <p class="dim">时间均为北京时间。包含生效时刻，不包含失效时刻；到期后进入待分配，不自动恢复旧人员或店铺默认。</p>
      <p v-if="resolvingRow" class="notice">原表第{{ resolvingRow.row_no }}行：{{ resolvingRow.payload.allocations.filter(a=>a.person).map(a=>`${a.role} ${a.person} ${a.rate_raw}`).join('；') }}。请明确对应人员后保存；原始记录会保留。</p>
      <section v-for="(s,i) in editor.segments || []" :key="i" class="segment">
        <div class="form-grid"><label>生效时间<input v-model="s.valid_from" type="datetime-local" step="1" /></label><label>失效时间（空表示持续）<input v-model="s.valid_to" type="datetime-local" step="1" /></label><label>处理方式<select v-model="s.mode" @change="changeMode(s)"><option value="distribute">分配提成</option><option value="exclude">明确不提成</option><option value="hold">待确认，不分配</option></select></label></div>
        <label v-if="s.mode==='distribute'"><input type="checkbox" :checked="s.amount_hold==='wage_pending'" @change="s.amount_hold=$event.target.checked ? 'wage_pending' : ''" /> 此关系涉及工资扣减，是否先试算由“计算口径”中的工资选项决定</label>
        <div v-if="s.mode==='distribute'" class="allocation">
          <div v-for="(a,j) in s.allocations" :key="j" class="allocation-row"><select v-model="a.person_id" aria-label="提成人员"><option value="">选择人员</option><option v-for="p in people" :key="p.id" :value="p.id">{{ p.name }}{{ p.employee_no ? ` · ${p.employee_no}` : '' }}{{ p.archived ? '（已归档）' : '' }}</option></select><select v-model="a.role" aria-label="提成角色"><option v-for="r in roles" :key="r">{{ r }}</option></select><label>点数 <input type="number" min="0" max="100" step="0.01" :value="Number(a.rate)*100" @input="a.rate=(Number($event.target.value)/100).toFixed(8)" />%</label><n-button size="small" @click="s.allocations.splice(j,1)">移除此人</n-button></div>
          <div class="toolbar"><n-button size="small" @click="s.allocations.push({person_id:'',role:'运营',rate:'0'})">添加分点人员</n-button><label>总点数 <input type="number" min="0" max="100" step="0.01" :value="Number(s.total_rate)*100" @input="s.total_rate=(Number($event.target.value)/100).toFixed(8)" />%</label><span>已分配 {{ percent(s.allocations.reduce((v,a)=>v+Number(a.rate),0)) }}</span></div>
        </div>
        <n-button v-if="editor.segments.length>1" size="tiny" @click="editor.segments.splice(i,1)">移除此时间段</n-button>
      </section>
      <div class="toolbar"><n-button @click="addSegment">添加时间段</n-button><n-button v-if="selected?.active_version" :disabled="!canWrite" @click="requestTerminate">终止 / 删除当前关系</n-button></div>
      <label>变更原因<input v-model="reason" placeholder="说明换人、调点或时间调整的原因" /></label>
      <div class="toolbar"><n-button :disabled="!canWrite || !reason.trim()" :loading="busy" @click="saveScheme(false)">保存草稿</n-button><n-button type="primary" :disabled="!canWrite || !reason.trim()" :loading="busy" @click="saveScheme(true)">保存并启用新版本</n-button><n-button v-if="selected?.draft_version" :disabled="!canWrite || !reason.trim()" :loading="busy" @click="publishDraft">启用原已保存草稿</n-button></div>
      <div v-if="selectedIds.length" class="notice">批量操作会从当前末段的生效时间起更新所选关系，之前区间保留。<n-button :disabled="!canWrite || !reason.trim()" :loading="busy" @click="bulkApply">为所选{{ selectedIds.length }}条启用末段方案</n-button></div>
      <section v-if="selected?.versions.length" class="version-list"><h3>全部历史版本</h3><article v-for="v in selected.versions" :key="v.id"><strong>第{{ v.revision }}版</strong> · {{ displayTime(v.recorded_at) }} · {{ v.actor }} · {{ v.reason }}<p>{{ ratesText(v.body) }}</p><n-button size="tiny" @click="restoreVersion(v)">载入此版作为新草稿</n-button></article></section>
    </n-modal>
    <n-modal v-model:show="showTerminate" preset="card" title="按时间终止关系" class="form-modal" style="max-width:520px"><p>历史关系和计算保留，指定时间之后采用以下处理。</p><label>终止时间<input v-model="terminateAt" type="datetime-local" step="1" /></label><label>之后的订单<select v-model="terminateMode"><option value="hold">待确认，不分配</option><option value="exclude">明确不提成</option></select></label><label>原因<input v-model="reason" /></label><n-button type="primary" :loading="busy" :disabled="!reason.trim()" @click="terminate">确认终止并保留历史</n-button></n-modal>
    <n-modal v-model:show="showDetail" preset="card" title="逐订单提成明细" class="form-modal" style="width:min(1280px,96vw)"><div class="toolbar"><span>{{ detail?.calculation.period }} · {{ displayTime(detail?.calculation.at) }}</span><a :href="`/api/commission-v2/export/details/${detailId}`">导出全部明细</a></div><div class="table-wrap"><table><thead><tr><th>订单</th><th>宝贝</th><th>人员 / 角色</th><th>下单时间</th><th>基数</th><th>点数</th><th>提成</th><th>匹配结果</th></tr></thead><tbody><tr v-for="(r,i) in detail?.rows || []" :key="i"><td>{{ r.order_id }}<br /><small>{{ r.sub_order_id }}</small></td><td>{{ r.product_name }}<br /><small>{{ r.product_id }}</small></td><td>{{ r.person || '未分配' }} {{ r.role }}</td><td>{{ r.order_at }}</td><td class="num">{{ money(Number(r.base)) }}</td><td>{{ percent(Number(r.share)) }}</td><td class="num">{{ r.amount == null ? '—' : money(Number(r.amount)) }}</td><td>{{ ({distribute:'已分配',exclude:'不提成',hold:'待确认',expired:'关系已失效',unassigned:'没有关系',wage_pending:'工资口径待确认',missing_order_time:'缺下单时间',missing_product_id:'缺宝贝ID'})[r.status] || r.status }}</td></tr></tbody></table></div><div class="toolbar"><n-button :disabled="detailOffset===0" @click="showCalculation(detailId,Math.max(0,detailOffset-100))">上一页</n-button><n-button :disabled="!detail?.has_more" @click="showCalculation(detailId,detailOffset+100)">下一页</n-button></div></n-modal>
    <n-modal v-model:show="showAccount" preset="card" title="创建提成操作账号" class="form-modal" style="max-width:480px"><label>账号<input v-model="accountName" /></label><label>初始密码（至少12位）<input v-model="accountPassword" type="password" /></label><n-button type="primary" :loading="busy" @click="createAccount">创建账号</n-button></n-modal>
    <n-modal v-model:show="showPassword" preset="card" title="修改提成管理密码" class="form-modal" style="max-width:480px"><label>原密码<input v-model="oldPassword" type="password" autocomplete="current-password" /></label><label>新密码（至少12位）<input v-model="newPassword" type="password" autocomplete="new-password" /></label><n-button type="primary" :loading="busy" @click="changePassword">修改密码并退出其他会话</n-button></n-modal>
  </div>
</template>

<style scoped>
.commission-workspace { max-width:1440px;margin:0 auto;padding-bottom:40px }
.toolbar { display:flex;align-items:center;flex-wrap:wrap;gap:12px;margin:16px 0 }
.scope-note,.dim,small { color:#6b7280;font-size:12px }
.scope-note { margin:12px 0;line-height:1.8 }
.tabs { display:flex;gap:8px;overflow-x:auto;border-bottom:1px solid #e2e5eb;margin:20px 0 12px }
.tabs button { background:transparent;border:0;border-bottom:2px solid transparent;border-radius:0;padding:12px 15px;white-space:nowrap;color:#5c6573;cursor:pointer }
.tabs button.active { color:#1f5eff;border-bottom-color:#1f5eff;font-weight:600 }
.notice { padding:12px 16px;background:#f3f6fa;border:1px solid #e1e7ee;border-radius:8px;margin:12px 0;line-height:1.7 }
.error { background:#fff1ed;color:#982c20;padding:14px;border-radius:8px;margin:12px 0 }
.table-wrap { overflow:auto;border:1px solid #e2e5eb;border-radius:10px;background:#fff }
table { width:100%;min-width:760px;border-collapse:collapse;font-size:13px }
th,td { text-align:left;padding:13px 14px;border-bottom:1px solid #eef0f4;vertical-align:top;line-height:1.65 }
th { color:#6b7280;font-weight:550;white-space:nowrap;background:#fafbfd }
td.wide { min-width:280px;max-width:520px }
td.num { text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap }
td small { display:inline-block;overflow-wrap:anywhere }
input:not([type=checkbox]):not([type=file]),select { border:1px solid #d7dce3;border-radius:6px;background:white;padding:8px 10px;color:#202631;min-width:0;font:inherit;box-sizing:border-box }
input[type=number] { width:100px }
.toolbar>input { min-width:240px }
label { font-size:13px }
.form-modal label { display:flex;flex-direction:column;gap:7px;margin:12px 0 }
.form-modal label:has(input[type=checkbox]) { flex-direction:row;align-items:center }
.form-grid { display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px }
.segment { padding:16px;border:1px solid #e1e5eb;border-radius:9px;margin:14px 0;background:#fafbfd }
.allocation-row { display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:8px 0 }
.allocation-row>select:first-child { min-width:180px }
.allocation-row label,.allocation .toolbar label { display:flex;flex-direction:row;align-items:center }
.version-list article { padding:12px 0;border-top:1px solid #e2e5eb }
.version-list p { color:#657080;font-size:13px }
.panel { margin-top:24px }
.empty { text-align:center;padding:40px;color:#6b7280 }
.total { margin:20px 0;font-size:15px }.total strong { font-size:28px;margin-left:16px }
a { color:#1f5eff;text-decoration:none;font-size:13px }
@media(max-width:720px) { .form-grid { grid-template-columns:1fr }.tabs button { padding:10px }.toolbar { gap:8px }.toolbar>input { min-width:160px;flex:1 }.allocation-row { align-items:stretch }.allocation-row>select { max-width:100% } }
</style>
