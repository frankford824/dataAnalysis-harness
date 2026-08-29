<script setup>
import { computed, ref, watch } from 'vue'

import { api } from '../api'

const props = defineProps({
  show: { type: Boolean, default: false },
  target: { type: Object, default: null },
})
const emit = defineEmits(['update:show'])

const loading = ref(false)
const error = ref('')
const result = ref(null)
const offset = ref(0)
const copied = ref(false)
const pageSize = 15

const fileName = computed(() => props.target?.file || props.target?.name || '原文件')
const columns = computed(() => {
  const width = Math.max(0, ...(result.value?.rows || []).map((row) => row.cells?.length || 0))
  return Array.from({ length: width }, (_, index) => index)
})
const matched = computed(() => new Set((props.target?.matches || []).map((item) => item.column_index)))

function columnName(index) {
  let value = index + 1
  let out = ''
  while (value) {
    value -= 1
    out = String.fromCharCode(65 + (value % 26)) + out
    value = Math.floor(value / 26)
  }
  return out
}

async function load(nextOffset = offset.value) {
  if (!props.target?.sha256) return
  offset.value = Math.max(0, nextOffset)
  loading.value = true
  error.value = ''
  try {
    result.value = await api.indexPreview({
      sha: props.target.sha256,
      sheet: props.target.sheet || '',
      offset: offset.value,
      limit: pageSize,
    })
  } catch (reason) {
    error.value = reason.message
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.show, props.target?.sha256, props.target?.sheet, props.target?.row_no],
  ([show]) => {
    if (!show) return
    const centered = props.target?.row_no ? Math.max(0, props.target.row_no - 6) : 0
    load(centered)
  },
  { immediate: true },
)

async function copyPath() {
  const path = props.target?.path || result.value?.metadata?.path || ''
  if (!path) return
  try {
    if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable')
    await navigator.clipboard.writeText(path)
  } catch {
    const textarea = document.createElement('textarea')
    textarea.value = path
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.select()
    document.execCommand('copy')
    textarea.remove()
  }
  copied.value = true
  setTimeout(() => (copied.value = false), 1500)
}
</script>

<template>
  <n-drawer
    :show="show"
    :width="'92vw'"
    placement="bottom"
    height="72vh"
    @update:show="(value) => emit('update:show', value)"
  >
    <n-drawer-content :title="`原文件预览 · ${fileName}`" closable>
      <div class="spread preview-toolbar">
        <div class="small muted">
          <span v-if="target?.sheet">{{ target.sheet }} · </span>
          第 {{ offset + 1 }}–{{ offset + (result?.rows?.length || 0) }} 个索引行
          <template v-if="result?.metadata?.total_rows"> / 共 {{ result.metadata.total_rows }} 行</template>
        </div>
        <div class="row">
          <n-button size="tiny" :disabled="offset === 0 || loading" @click="load(offset - pageSize)">上一页</n-button>
          <n-button size="tiny" :disabled="(result?.rows?.length || 0) < pageSize || loading" @click="load(offset + pageSize)">下一页</n-button>
          <n-button size="tiny" secondary @click="copyPath">{{ copied ? '已复制' : '复制文件路径' }}</n-button>
        </div>
      </div>

      <n-alert v-if="error" type="error" :bordered="false">{{ error }}</n-alert>
      <n-spin v-else :show="loading">
        <div v-if="result?.rows?.length" class="preview-grid-wrap">
          <table class="preview-grid">
            <thead>
              <tr>
                <th class="preview-row-no">行</th>
                <th
                  v-for="column in columns"
                  :key="column"
                  :class="{ matched: matched.has(column) }"
                >{{ columnName(column) }}</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="row in result.rows"
                :key="`${row.sheet}:${row.row_no}`"
                :class="{ focus: row.row_no === target?.row_no }"
              >
                <th class="preview-row-no">{{ row.row_no }}</th>
                <td
                  v-for="column in columns"
                  :key="column"
                  :class="{ matched: matched.has(column) }"
                  :title="row.cells?.[column] || ''"
                >{{ row.cells?.[column] || '' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <n-empty v-else description="没有可预览的索引行" />
      </n-spin>

      <p class="xs muted preview-path">{{ target?.path || result?.metadata?.path }}</p>
    </n-drawer-content>
  </n-drawer>
</template>
