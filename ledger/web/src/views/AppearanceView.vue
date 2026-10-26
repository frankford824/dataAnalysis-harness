<script setup>
import { computed, ref } from 'vue'
import { tokens } from '../design/tokens.js'
import PageHead from '../components/PageHead.vue'
import LedgerTabs from '../components/ui/LedgerTabs.vue'
const tab = ref('foundation'), example = ref(false)
const colors = computed(() => Object.entries(tokens.colors))
const names = { canvas:'工作台',paper:'纸面',surface:'辅助纸面',ink:'正文墨色',muted:'次要文字',line:'数据分隔线',pencil:'铅笔轮廓',action:'操作蓝','on-action':'按钮文字','action-soft':'选择底色',success:'已封存',warning:'待核对',danger:'错误状态',shadow:'纸张阴影',scrim:'依据遮罩' }
const labels = {caption:'来源与说明',body:'表格与控件',section:'区块标题',heading:'金额与小标题',title:'当前任务',display:'128,000.00'}
</script>
<template>
  <section class="ui-pattern-page">
    <PageHead title="样式规范" hint="整套界面共用一份色板、字号、间距、组件与动效 token。" />
    <div class="ui-control-bar" data-region="function"><LedgerTabs v-model="tab" label="样式规范内容" :options="[{key:'foundation',label:'基础变量'},{key:'components',label:'组件规则'},{key:'compatibility',label:'兼容与区块'}]"/></div>
    <section v-if="tab==='foundation'" data-region="display">
      <h2>语义色板</h2><p class="xs muted">颜色配合文字和标记表达状态；浅色与深色分别定义。</p>
      <div class="token-palette"><article v-for="[key,value] in colors" :key="key" class="token-swatch"><div class="token-swatch-color" :style="{background:`var(--sk-color-${key})`}"/><h3>{{names[key]}}</h3><code>color.{{key}}</code><p class="xs muted">浅色 {{value.light}} · 深色 {{value.dark}}</p></article></div>
      <h2 class="ui-pattern-heading">字号阶梯</h2><div v-for="[key,size] in Object.entries(tokens.type)" :key="key" class="ui-rule row" style="justify-content:space-between;flex-wrap:wrap"><span :style="{fontSize:`var(--sk-type-${key})`}">{{labels[key]}}</span><span class="xs num muted">type.{{key}} · {{size}}px</span></div>
      <h2 class="ui-pattern-heading">间距阶梯</h2><div v-for="[key,size] in Object.entries(tokens.space)" :key="key" class="ui-rule row"><span class="xs num">space.{{key}}</span><span class="spacing-sample" :style="{width:`var(--sk-space-${key})`}"/><span class="xs num muted">{{size}}px</span></div>
    </section>
    <section v-else-if="tab==='components'" data-region="display"><article v-for="[key,rule] in Object.entries(tokens.components)" :key="key" class="ui-rule"><h2>{{({button:'按钮',table:'表格',paper:'纸张对象',group:'临时处理组',field:'输入与选择',evidence:'原始依据',motion:'动效'})[key]}}</h2><p>{{rule.rule}}</p><div v-if="key==='button'" class="row" style="margin-top:var(--s3)"><n-button type="primary" @click="example=!example">{{example?'已响应 · 再点恢复':'主要操作示例'}}</n-button><n-button disabled>禁用示例</n-button></div><table v-if="key==='table'"><thead><tr><th>已确认零值</th><th>缺少数据</th></tr></thead><tbody><tr><td class="num">0.00</td><td class="muted">—</td></tr></tbody></table></article></section>
    <section v-else data-region="display"><h2>老用户继续走熟悉的路径</h2><article v-for="rule in tokens.compatibility.rules" :key="rule" class="ui-rule"><p>{{rule}}</p></article><h2 class="ui-pattern-heading">每页沿一个任务展开</h2><article v-for="key in tokens.blocks.order" :key="key" class="ui-rule"><h3>{{({hero:'Hero · 当前任务',function:'功能 · 操作区',display:'显示 · 结果与明细'})[key]}}</h3><p>{{tokens.blocks[key]}}</p></article></section>
  </section>
</template>
