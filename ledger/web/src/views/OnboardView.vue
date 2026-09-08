<script setup>
/* 接一张系统没见过的表。
 *
 * 这件事是一次性的：接完之后没人会再回来看这张映射。而错的映射不报错，只是静默
 * 少算钱。所以每一列旁边都要摆着「识别情况」和样例值，逼人当场核对——只给一个
 * 下拉框，人会一路点确定。
 */
import { useMessage } from 'naive-ui'
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useLatest } from '../components/ui/useLatest'
import PageHead from '../components/PageHead.vue'
import { api } from '../api'
import { count } from '../format'
import { useApp } from '../store'

const props = defineProps({ sha: { type: String, required: true } })

const app = useApp()
const route = useRoute()
const router = useRouter()
const message = useMessage()

const draft = ref(null)
const roleList = ref([])
const loading = ref(false)
const failed = ref('')
const assist = ref(null)
const tried = ref(null)

//: 人改过的列。序号 → 角色。模型的建议回来时不能盖掉这些。
const picked = ref({})
const templateId = ref('')
const source = ref('')
const headerRow = ref(null)
const displayHeaderRow=computed({get:()=>headerRow.value==null?1:headerRow.value+1,set:value=>{headerRow.value=Math.max(0,(value||1)-1)}})

const CONFIDENCE = {
  sure: { label: '已识别', type: 'success' },
  ask: { label: '待确认', type: 'warning' },
  guess: { label: '待核对', type: 'default' },
  unknown: { label: '未识别', type: 'error' },
}

const columns = computed(() => draft.value?.columns || [])
const needsYou = computed(() => columns.value.filter((c) => !c.settled))
const rest = computed(() => columns.value.filter((c) => c.settled))

function roleOf(col) {
  if (col.index in picked.value) return picked.value[col.index]
  return col.role || ''
}

const draftRequest=useLatest(),suggestionRequest=useLatest()
let draftSerial=0
async function load() {
  const serial=++draftSerial;const sha=props.sha
  loading.value=true;failed.value='';tried.value=null;assist.value=null;suggestionRequest.cancel()
  try{
    const params={sheet:route.query.sheet,header_row:headerRow.value,source:source.value}
    const response=await draftRequest.run(async signal=>{
      const value=await api.draft(sha,params,{signal})
      const roles=await api.roles(source.value||value.source,{signal})
      return {value,roles}
    })
    if(!response)return
    draft.value=response.value.value;roleList.value=response.value.roles.roles||[]
    templateId.value=templateId.value||draft.value.suggest_id||`import_${sha.slice(0,12)}`;source.value=source.value||draft.value.source;headerRow.value=headerRow.value??draft.value.header_row
    suggestionRequest.run(signal=>api.assist(sha,params,{signal})).then(result=>{
      if(result && serial===draftSerial){assist.value=result.value.assist;if(result.value.columns)mergeAssist(result.value.columns)}
    }).catch(()=>{})
  }catch(e){if(serial===draftSerial)failed.value=e.message}
  finally{if(serial===draftSerial)loading.value=false}
}

/** 模型回来的映射只填人没动过的列。 */
function mergeAssist(cols) {
  if (!draft.value) return
  const mine = picked.value
  draft.value.columns = draft.value.columns.map((c) => {
    const fresh = cols.find((x) => x.index === c.index)
    return fresh && !(c.index in mine) ? fresh : c
  })
}

watch(()=>[props.sha,route.query.sheet],()=>{picked.value={};templateId.value='';source.value='';headerRow.value=null;load()},{immediate:true})
watch(()=>app.uiRefresh,()=>load())

function commit() {
  const roles = {}
  for (const c of columns.value) roles[c.index] = roleOf(c)
  return {
    sha: props.sha,
    sheet: draft.value.sheet || '',
    header_row: headerRow.value,
    template_id: templateId.value,
    source: source.value,
    roles,
    match_columns: draft.value.match_columns || [],
    time_slots: draft.value.time_slots || {},
    total_row_marker: draft.value.total_row_marker || null,
    model_revision: draft.value.model_revision,
  }
}

async function tryIt() {
  if(!source.value){message.warning('请先选择文件类型');return}
  if(!Object.values(commit().roles).some(Boolean)){message.warning('请先指定列含义');return}
  try {
    const request=commit(), signature=JSON.stringify(request)
    const result=await app.run('正在试算', () => api.onboardTry(request))
    if(signature!==JSON.stringify(commit())){message.warning('设置已修改，请重新试算');return}
    tried.value=result
  } catch (e) {
    message.error(e.message, { duration: 6000 })
  }
}

async function save() {
  if(!tried.value?.ok){message.warning('请先完成试算');return}
  try {
    const res = await app.run('正在保存并重算', () => api.onboard(commit()))
    app.invalidate()
    message.success(`接上了：${res.template_id}`)
    router.push('/')
  } catch (e) {
    message.error(`保存失败：${e.message}`, { duration: 6000 })
  }
}

function change(col, role) {
  picked.value = { ...picked.value, [col.index]: role }
  // 改了映射，之前那次试算就不算数了。留着会让人照着旧结果点保存。
  tried.value = null
}

const roleOptions = computed(() => [
  { label: '忽略此列', value: '' },
  ...roleList.value.map((r) => ({ label: r.name || r.role, value: r.role })),
])
watch([source,headerRow,templateId],()=>{tried.value=null})
function warningText(text){
  if(text.includes('还没定挂到哪个数据源'))return '请选择文件类型。'
  const missing=/有 (\d+) 列是数字但没映上：([^。]+)/.exec(text)
  if(missing)return `${missing[1]} 列数值尚未指定含义：${missing[2]}。`
  return text
}
function columnHint(column){
  const matched=roleList.value.find(role=>role.role===column.role)
  if(matched)return `已识别为${matched.name || '已有字段'}，请核对样例`
  return '尚未识别，请根据样例选择'
}
</script>

<template>
  <n-spin :show="loading">
    <n-alert v-if="failed" type="error" :bordered="false">{{ failed }}</n-alert>

    <template v-else-if="draft">
      <PageHead title="新表设置" hint="核对每一列的含义，试算后再保存。" />
      <div class="small muted" style="margin-bottom: var(--s4)">
        {{ draft.file }}
        <template v-if="draft.sheet"> · {{ draft.sheet }}</template>
        · {{ count(draft.rows) }} 行
      </div>

      <n-alert
        v-for="(w, i) in draft.warnings || []"
        :key="i"
        type="warning"
        :bordered="false"
        style="margin-bottom: var(--s2)"
      >
        {{ warningText(w) }}
      </n-alert>

      <div class="card">
        <header><h2>文件设置</h2></header>
        <n-space vertical>
          <n-input-number :disabled="loading || !!app.busy" v-model:value="displayHeaderRow" size="small" :min="1" aria-label="表头行号" @update:value="load">
            <template #prefix>表头在第</template>
            <template #suffix>行</template>
          </n-input-number>
          <n-select :disabled="loading || !!app.busy"
            v-model:value="source"
            size="small"
            :options="(draft.sources || []).map((s) => ({ label: s.name, value: s.id }))"
            placeholder="选择文件类型" aria-label="文件类型"
            style="max-width: 320px"
            @update:value="load"
          />
          <details><summary class="small muted">更多设置</summary><n-input :disabled="loading || !!app.busy" v-model:value="templateId" size="small" placeholder="格式编号" aria-label="格式编号" style="max-width:320px;margin-top:8px"/></details>
        </n-space>
      </div>

      <div v-if="assist?.ok" class="card"><header><h2>识别建议</h2></header><p class="small">请结合样例核对列含义。</p></div>

      <div class="card">
        <header>
          <h2>待确认列（{{ needsYou.length }}）</h2>
          <span class="sub">请选择每一列的含义</span>
        </header>
        <n-table class="onboard-columns" size="small" :bordered="false">
          <thead>
            <tr>
              <th>列名</th>
              <th>样例</th>
              <th>识别情况</th>
              <th style="width: 220px">列含义</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in needsYou" :key="c.index">
              <td>
                {{ c.column }}
                <n-tag size="tiny" :type="CONFIDENCE[c.confidence]?.type || 'default'" :bordered="false">
                  {{ CONFIDENCE[c.confidence]?.label || c.confidence }}
                </n-tag>
              </td>
              <td class="xs muted num">{{ (c.samples || []).slice(0, 2).join(' / ') }}</td>
              <td class="xs muted">{{ columnHint(c) }}</td>
              <td>
                <n-select :disabled="loading || !!app.busy"
                  :aria-label="`选择${c.column}的含义`" :value="roleOf(c)"
                  size="small"
                  filterable
                  :options="roleOptions"
                  @update:value="(v) => change(c, v)"
                />
              </td>
            </tr>
          </tbody>
        </n-table>
        <p v-if="!needsYou.length" class="small muted">所有列均已确认。</p>

        <div v-if="rest.length" class="panel">
          <h3>另外 {{ rest.length }} 列</h3>
          <n-table size="small" :bordered="false">
            <tbody>
              <tr v-for="c in rest" :key="c.index">
                <td>{{ c.column }}</td>
                <td class="xs muted">{{ columnHint(c) }}</td>
                <td style="width: 220px">
                  <n-select :disabled="loading || !!app.busy"
                    :aria-label="`选择${c.column}的含义`" :value="roleOf(c)"
                    size="small"
                    filterable
                    :options="roleOptions"
                    @update:value="(v) => change(c, v)"
                  />
                </td>
              </tr>
            </tbody>
          </n-table>
        </div>
      </div>

      <div class="card">
        <header>
          <h2>试算</h2>
          <span class="sub">核对试算结果后保存</span>
        </header>
        <div class="row">
          <n-button size="small" :loading="!!app.busy" :disabled="loading || !!app.busy" @click="tryIt">试算</n-button>
          <n-button size="small" type="primary" :disabled="!tried?.ok || !!app.busy || loading" :loading="!!app.busy" @click="save">
            保存并重算
          </n-button>
        </div>

        <template v-if="tried">
          <n-alert
            :type="tried.ok ? 'success' : 'error'"
            :bordered="false"
            style="margin-top: var(--s3)"
          >
            {{ tried.summary }}
          </n-alert>
          <n-table v-if="tried.roles?.length" size="small" :bordered="false" style="margin-top: var(--s3)">
            <thead>
              <tr>
                <th>列含义</th>
                <th>取自哪列</th>
                <th class="right">有值的行</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in tried.roles" :key="r.role">
                <td>{{ roleList.find(item=>item.role===r.role)?.name || r.role }}</td>
                <td class="xs muted">{{ r.column }}</td>
                <td class="right num">{{ count(r.filled) }}</td>
              </tr>
            </tbody>
          </n-table>
        </template>
      </div>
    </template>
  </n-spin>
</template>
