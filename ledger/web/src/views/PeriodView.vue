<script setup>
/* 一家店一个账期：损益表、自检、该交的表、质量、结账。
 *
 * 损益表每一行都能点开——这是这套系统和一张普通报表的唯一区别。数字点不开，
 * 对不上账时人就只能回去用 Excel 手工核。
 *
 * 排版上，损益表占主栏，剩下四块收进右边一栏的标签页里。它们回答的是同一个
 * 问题的另一半——「这张表能不能信」——所以必须和数字同屏；但它们又不是每次都
 * 要看，竖着铺开就把页面拉到三四屏长，人滚到底就忘了上面的数是多少。
 */
import { useDialog, useMessage } from 'naive-ui'
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api } from '../api'
import DrillDrawer from '../components/DrillDrawer.vue'
import DropZone from '../components/DropZone.vue'
import FixPanel from '../components/FixPanel.vue'
import GapList from '../components/GapList.vue'
import PageHead from '../components/PageHead.vue'
import PeriodStrip from '../components/PeriodStrip.vue'
import { count, money, percent, stamp } from '../format'
import { useApp } from '../store'

const props = defineProps({ id: { type: String, required: true } })

const app = useApp()
const route = useRoute()
const router = useRouter()
const message = useMessage()
const dialog = useDialog()

const info = ref(null)
const snap = ref(null)
const loading = ref(false)
const failed = ref('')
const drill = ref(null)

const period = computed(() => route.query.period || app.period || '')

// 接口只给平台 id。人看的是「淘宝天猫」，不是 taobao。
const platformName = computed(() => {
  const id = info.value?.store?.platform
  return app.platforms.find((p) => p.id === id)?.name || id || ''
})

const closed = computed(() => snap.value?.state === 'closed')

async function load() {
  loading.value = true
  failed.value = ''
  try {
    info.value = await api.store(props.id)
    const want = period.value || info.value.periods?.[0]?.period
    if (want) {
      snap.value = await api.period(props.id, want)
      app.pick({ store: props.id, period: want })
    } else {
      snap.value = null
    }
  } catch (e) {
    failed.value = e.message
  } finally {
    loading.value = false
  }
}

watch(() => [props.id, period.value], load, { immediate: true })

function go(p) {
  app.pick({ period: p })
  router.replace({ name: 'period', params: { id: props.id }, query: { period: p } })
}

async function recompute() {
  try {
    await app.run('正在重算', () => api.recompute(props.id))
    app.invalidate()
    await load()
    message.success('算完了')
  } catch (e) {
    message.error(`没算成：${e.message}`, { duration: 6000 })
  }
}

async function close() {
  try {
    await app.run('正在结账', () => api.close(props.id, period.value))
    app.invalidate()
    await load()
    message.success('结账了')
  } catch (e) {
    message.error(`结不了：${e.message}`, { duration: 6000 })
  }
}

const why = ref('')
const asking = ref(false)

/** 反结账要写理由。账已经报出去了，改回去必须留下是谁、为什么。 */
async function reopen() {
  if (!why.value.trim()) return
  try {
    await app.run('正在反结账', () => api.reopen(props.id, period.value, why.value.trim()))
    asking.value = false
    why.value = ''
    app.invalidate()
    await load()
    message.success('已改回未结账')
  } catch (e) {
    message.error(e.message, { duration: 6000 })
  }
}

function openDrill(row, only = 'counted') {
  if (!row.drillable || !snap.value?.run_id) return
  drill.value = {
    runId: snap.value.run_id, node: row.id, name: row.name,
    value: row.value, only,
  }
}

const bad = computed(() => (snap.value?.findings || []).filter((f) => !f.passed))
const historicalArchive = computed(() => snap.value?.archive?.kind === 'legacy_final_summary')
const historicalChecks = computed(() => historicalArchive.value ? (snap.value?.findings || []) : [])
//: 真正拦着结账的那几条。灰掉的结账按钮不说明理由，人只能猜是不是坏了。
const blockers = computed(() => bad.value.filter((f) => f.blocking))
const fixing = ref(false)
const missingSources = computed(() =>
  (snap.value?.sources || []).filter((s) => s.arrived === false && s.status !== 'not_applicable').length,
)

const gaps = computed(() => snap.value?.gaps || [])

/* 没进利润的钱。
 *
 * 要查的那一桶排最前，其余按金额。后端已经按这个顺序给了，这里只补一件事：
 * 让「算进合计的」和「不算进合计的」在视觉上分开——四行金额差几个数量级，
 * 不分开的话人第一眼会以为合计算错了。 */
const unlinkedRows = computed(() => snap.value?.unlinked_buckets || [])

/* 「哪几桶算进合计」是后来才加进快照的字段，早先算的快照里没有，而已结账的账期
 * 按设计就不会再重算——那份快照要一直保持结账那天的样子。所以这里不能默认「没写
 * 就是不算」：那样合计的笔数会显示成 0，看起来像刚数错了一遍。老快照就老老实实
 * 说不知道，金额照旧用快照里存着的那个总额。 */
const unlinkedGraded = computed(() =>
  unlinkedRows.value.some((b) => typeof b.counted === 'boolean'),
)

const unlinkedNeedsWork = computed(() => ({
  count: unlinkedRows.value
    .filter((b) => b.counted)
    .reduce((a, b) => a + (b.count || 0), 0),
}))

/** 点一条缺口，落到它的来源行。没有损益表节点的（挂不上订单、认不出的费项）走专用入口。 */
function openGap(gap) {
  if (gap.node === '__sources__') {
    rail.value = 'sources'
    return
  }
  if (!snap.value?.run_id) return
  if (gap.node && gap.node.startsWith('__')) {
    drill.value = {
      runId: snap.value.run_id,
      node: gap.node,
      name: gap.title,
      value: gap.amount,
      only: gap.only || 'all',
    }
    return
  }
  const row = (snap.value?.statement || []).find((r) => r.id === gap.node)
  if (row) openDrill(row, gap.only || 'counted')
}

function openFinding(f) {
  if (f.tab) {
    rail.value = f.tab
    return
  }
  if (!f.drill || !snap.value?.run_id) return
  drill.value = {
    runId: snap.value.run_id,
    node: f.drill,
    name: f.name || f.label,
    value: f.amount,
    only: f.only || 'counted',
  }
}

// 右栏默认停在最需要人处理的那一块：有缺口就停在缺口，有没交的表就停在该交的表，
// 都没有才停在质量。默认永远停在第一个标签的话，真正要人处理的那条就藏在后面。
const rail = ref('gaps')
watch(
  () => snap.value,
  () => {
    rail.value = gaps.value.length
      ? 'gaps'
      : missingSources.value
        ? 'sources'
        : historicalArchive.value
          ? 'checks'
          : 'quality'
  },
)
</script>

<template>
  <n-spin :show="loading">
    <n-alert v-if="failed" type="error" :bordered="false">{{ failed }}</n-alert>

    <template v-else-if="info">
      <PageHead
        kicker="店铺账期"
        :title="info.store?.name || ''"
        :scope="[platformName, period, info.store?.entity].filter(Boolean)"
        :hint="`已收 ${count(info.files?.length || 0)} 张表`"
      >
        <template #actions>
          <n-button size="small" @click="fixing = true">数字不对？</n-button>
          <n-button v-if="app.ingestMode !== 'nas'" size="small" @click="recompute">重算</n-button>
          <n-tag v-else size="small" type="info" :bordered="false">索引更新后自动计算</n-tag>
          <n-button
            v-if="!closed"
            type="primary"
            :disabled="!snap?.can_close"
            :title="snap?.can_close ? `结账 ${period}` : (blockers[0]?.name || '还不能结账')"
            @click="close"
          >
            结账 {{ period }}
          </n-button>
          <n-button v-else size="small" @click="asking = true">反结账</n-button>
        </template>
      </PageHead>

      <PeriodStrip
        :periods="info.periods || []"
        :model-value="period"
        @update:model-value="go"
      />

      <template v-if="snap">
        <n-alert
          v-if="closed"
          type="success"
          :bordered="false"
          style="margin-bottom: var(--s4)"
        >
          已结账{{ snap.at ? `于 ${stamp(snap.at)}` : '' }}{{ snap.by ? ` · ${snap.by}` : '' }}
          <template v-if="snap.stale"> · 之后又交了新表，数字还是结账那一版</template>
          <div v-if="snap.note" class="small" style="margin-top: 4px">{{ snap.note }}</div>
        </n-alert>

        <n-alert
          v-if="snap.archive?.kind === 'legacy_final_summary'"
          type="info"
          :bordered="false"
          title="台账上线前的历史终态"
          style="margin-bottom: var(--s4)"
        >
          <div class="small">
            这期按只读结果归档，没有改写原文件。来源：{{ snap.archive.source_path }}
            · {{ snap.archive.sheet }} · 第 {{ snap.archive.row_numbers?.join('、') }} 行
          </div>
          <div class="xs muted num" style="margin-top: 4px">
            SHA-256 {{ snap.archive.source_sha256 }}
            <template v-if="Math.abs(snap.archive.legacy_adjustment || 0) > 0.005">
              · 历史结账调整 {{ money(snap.archive.legacy_adjustment) }}
            </template>
          </div>
        </n-alert>

        <!-- 结账按钮灰着而不说为什么，是最容易被理解成「系统坏了」的一种状态。
             拦路的那几条本来就在自检里，但那是右栏第二个标签页，得先点开才看得见。 -->
        <n-alert
          v-else-if="snap.can_close"
          type="success"
          :bordered="false"
          style="margin-bottom: var(--s4)"
        >
          这一期可以结账了。右边若还有提示，是要人看一眼的，不拦结账。
        </n-alert>
        <n-alert
          v-else-if="blockers.length"
          type="error"
          :bordered="false"
          title="这个账期还结不了"
          style="margin-bottom: var(--s4)"
        >
          <div v-for="f in blockers" :key="f.id" class="small" style="margin-top: 2px">
            {{ f.name }}：{{ f.head || f.message }}
          </div>
          <div class="row" style="margin-top: var(--s3)">
            <n-button size="tiny" @click="rail = 'checks'">看自检明细</n-button>
            <n-button size="tiny" @click="fixing = true">怎么改</n-button>
          </div>
        </n-alert>

        <div class="cols">
          <div class="card" style="margin-top: 0">
            <header>
              <h2>损益表</h2>
              <span class="sub">点任意一行看它是怎么来的</span>
            </header>
            <div class="statement">
              <div
                v-for="row in snap.statement || []"
                :key="row.id"
                class="line"
                :class="[`lv${row.level}`, { total: row.is_total, drillable: row.drillable }]"
                @click="openDrill(row)"
              >
                <span>{{ row.name }}</span>
                <span v-if="!row.available" class="na">—</span>
                <span v-else class="amt" :class="{ neg: row.value < 0 }">
                  {{ row.display === 'percent' ? percent(row.value) : money(row.value) }}
                </span>
              </div>
            </div>
            <p v-if="(snap.missing_sources || []).length" class="why" style="margin-top: var(--s3)">
              破折号的行是还不知道，不是零。缺：{{ snap.missing_sources.join('、') }}
            </p>
          </div>

          <div class="card rail" style="margin-top: 0">
            <n-tabs v-model:value="rail" type="line" size="small">
              <n-tab-pane name="gaps">
                <template #tab>
                  要看的
                  <n-badge
                    v-if="gaps.length"
                    :value="gaps.length"
                    :type="gaps.some((g) => g.severity === 'blocking') ? 'error' : 'warning'"
                    style="margin-left: 6px"
                  />
                </template>
                <p v-if="historicalArchive" class="xs muted" style="margin-bottom: var(--s3)">
                  历史账只报告终态文件里能证明的事项；没有订单级原始行的检查不会伪造成通过。
                </p>
                <p v-else class="xs muted" style="margin-bottom: var(--s3)">
                  这个账期的空值项和异常项都在这儿。能点的点开就是它的来源明细。
                </p>
                <GapList :gaps="gaps" clickable @open="openGap" />
              </n-tab-pane>

              <n-tab-pane name="checks">
                <template #tab>
                  自检
                  <n-badge
                    v-if="bad.length"
                    :value="bad.length"
                    :type="bad.some((f) => f.blocking) ? 'error' : 'warning'"
                    style="margin-left: 6px"
                  />
                </template>
                <p v-if="historicalArchive" class="xs muted" style="margin-bottom: var(--s3)">
                  这些检查针对只读终态文件、店期映射、金额勾稽和关账边界；不会声称旧账通过了后来才建立的订单级门禁。
                </p>
                <p v-else class="xs muted" style="margin-bottom: var(--s3)">
                  拦路的那条不解决就结不了账。每条都能点进去看是哪些行——只给一段话不给行号，
                  等于让人自己再对一遍账。
                </p>
                <n-alert
                  v-for="f in bad"
                  :key="f.id"
                  :type="f.blocking ? 'error' : 'warning'"
                  :title="`${f.name}${f.blocking ? ' · 拦着结账' : ''}`"
                  :bordered="false"
                  style="margin-bottom: var(--s2)"
                >
                  {{ f.head || f.message }}
                  <ul v-if="f.buckets?.length" class="bullets">
                    <li v-for="(b, i) in f.buckets" :key="i">
                      {{ b.label }} · {{ count(b.count) }} 笔 · {{ money(b.amount) }}
                      <button class="link" @click="openFinding(b)">看这些行 →</button>
                    </li>
                  </ul>
                  <ul v-else-if="f.lines?.length" class="bullets">
                    <li v-for="(line, i) in f.lines" :key="i">{{ line }}</li>
                  </ul>
                  <div v-if="f.drill || f.tab" class="row" style="margin-top: var(--s2)">
                    <n-button size="tiny" type="primary" @click="openFinding(f)">
                      {{ f.tab === 'sources' ? '去看该交的表' : '看这些行' }}
                    </n-button>
                  </div>
                </n-alert>
                <template v-if="historicalArchive && !bad.length">
                  <n-alert
                    v-for="f in historicalChecks"
                    :key="f.id"
                    type="success"
                    :title="f.name"
                    :bordered="false"
                    style="margin-bottom: var(--s2)"
                  >
                    {{ f.head || f.message }}
                    <ul v-if="f.lines?.length" class="bullets">
                      <li v-for="(line, i) in f.lines" :key="i">{{ line }}</li>
                    </ul>
                  </n-alert>
                </template>
                <n-alert v-else-if="!bad.length" type="success" :bordered="false">
                  {{ (snap.findings || []).length }} 项检查都过了
                </n-alert>
                <div
                  v-if="snap.unclassified?.length"
                  class="row"
                  style="margin-top: var(--s3)"
                >
                  <n-button
                    size="small"
                    @click="router.push({
                      name: 'fees',
                      query: { label: snap.unclassified[0].label },
                    })"
                  >
                    去归类这 {{ snap.unclassified.length }} 个未归类费项
                  </n-button>
                </div>
              </n-tab-pane>

              <n-tab-pane name="sources">
                <template #tab>
                  该交的表
                  <n-badge
                    v-if="missingSources"
                    :value="missingSources"
                    type="warning"
                    style="margin-left: 6px"
                  />
                </template>
                <p v-if="historicalArchive" class="xs muted" style="margin-bottom: var(--s2)">
                  台账上线前按终态结果关账。这里只陈述已经归档的依据；当前模型要求的原始表不倒推、不补写“已交”。
                </p>
                <p v-else class="xs muted" style="margin-bottom: var(--s2)">
                  缺一张，损益表上就有一行出不了数。
                </p>
                <div class="scroll">
                  <n-table size="small" :bordered="false" :single-line="false" class="src-table">
                    <tbody>
                      <tr v-for="s in snap.sources || []" :key="s.id">
                        <td class="small nowrap">{{ s.name }}</td>
                        <td class="wrap-cell">
                          <n-tag v-if="s.arrived" size="small" type="success" :bordered="false">
                            {{ s.status === 'archived' ? '已归档' : '已交' }}
                          </n-tag>
                          <n-tag
                            v-else-if="s.status === 'not_applicable'"
                            size="small"
                            :bordered="false"
                          >不追溯</n-tag>
                          <span v-else class="src-miss">{{ s.reason || '没交' }}</span>
                          <div
                            v-if="s.reason && (s.arrived || s.status === 'not_applicable')"
                            class="xs muted"
                            style="margin-top: 3px"
                          >
                            {{ s.reason }}
                          </div>
                        </td>
                      </tr>
                    </tbody>
                  </n-table>
                </div>
              </n-tab-pane>

              <n-tab-pane v-if="historicalArchive || snap.quality?.length" name="quality" tab="质量">
                <template v-if="historicalArchive">
                  <p class="xs muted" style="margin-bottom: var(--s2)">
                    历史终态的质量只核对原件、映射和金额勾稽；订单级挂钩与覆盖没有分母，明确显示“不适用”。
                  </p>
                  <div class="scroll">
                    <n-table size="small" :bordered="false" :single-line="false">
                      <thead><tr><th>检查</th><th>结果</th><th>依据</th></tr></thead>
                      <tbody>
                        <tr v-for="q in snap.archive?.quality_checks || []" :key="q.id">
                          <td class="small nowrap">{{ q.name }}</td>
                          <td class="nowrap">
                            <n-tag
                              size="small"
                              :type="q.status === 'passed' ? 'success' : 'default'"
                              :bordered="false"
                            >{{ q.result }}</n-tag>
                          </td>
                          <td class="xs muted wrap-cell">{{ q.detail }}</td>
                        </tr>
                      </tbody>
                    </n-table>
                  </div>
                </template>
                <template v-else>
                  <p class="xs muted" style="margin-bottom: var(--s2)">
                    挂钩是这张表有多少行认到了订单，覆盖是订单里有多少拿到了这项数。
                    百分比底下写的是它由哪两个数算出来的——光看 94% 不知道是差 7 笔还是差 767 笔。
                    画横杠的那格不是零，是这项不适用：全公司表不评挂钩，偶发科目不评覆盖。
                  </p>
                  <div class="scroll">
                    <n-table size="small" :bordered="false">
                      <thead>
                        <tr>
                          <th>项目</th>
                          <th class="right">表里行数</th>
                          <th class="right">挂钩</th>
                          <th class="right">覆盖</th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr v-for="q in snap.quality" :key="q.metric">
                          <td class="small">
                            {{ q.name }}
                            <div v-if="q.company_wide || q.occasional" class="xs muted">
                              {{ q.company_wide ? '全公司表' : '' }}{{ q.company_wide && q.occasional ? ' · ' : '' }}{{ q.occasional ? '偶发科目' : '' }}
                            </div>
                          </td>
                          <td class="right num xs">{{ count(q.rows) }}</td>
                          <td class="right num xs">
                            {{ q.hit_rate === null ? '—' : percent(q.hit_rate) }}
                            <div v-if="q.hit_rate !== null" class="xs muted num">
                              {{ count(q.linked) }}/{{ count(q.rows) }}
                            </div>
                          </td>
                          <td class="right num xs">
                            {{ q.coverage === null ? '—' : percent(q.coverage) }}
                            <div v-if="q.coverage !== null" class="xs muted num">
                              {{ count(q.covered) }}/{{ count(q.expected) }}{{ q.expect_label ? ` ${q.expect_label}` : '' }}
                            </div>
                          </td>
                        </tr>
                      </tbody>
                    </n-table>
                  </div>
                </template>
              </n-tab-pane>

              <n-tab-pane v-if="historicalArchive || snap.unlinked_buckets?.length" name="unlinked" tab="没进利润的钱">
                <template v-if="historicalArchive">
                  <n-alert type="info" :bordered="false" title="没有订单级未归属证据">
                    {{ snap.archive?.unlinked_evidence?.detail }}
                  </n-alert>
                  <p class="xs muted" style="margin-top: var(--s3)">
                    这里显示“无法从终态汇总反推”，而不是显示0；0代表已逐行检查且确实没有，两者不能混用。
                  </p>
                </template>
                <template v-else>
                  <p class="xs muted" style="margin-bottom: var(--s3)">
                    挂不上任何订单的钱，按「为什么挂不上」分开摆——处置完全不同。
                    点一行就能看到这些钱在哪个文件第几行。排第一行的那类要人去查，灰掉的几类各有各的解释。
                  </p>
                  <div class="scroll">
                    <n-table size="small" :bordered="false">
                      <thead>
                        <tr>
                          <th>为什么挂不上</th>
                          <th class="right">笔数</th>
                          <th class="right">金额</th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr
                          v-for="(b, i) in unlinkedRows"
                          :key="i"
                          class="tap"
                          style="cursor: pointer"
                          :class="{ quiet: unlinkedGraded && !b.counted }"
                          @click="openGap({
                            node: `__unlinked__:${b.label}`,
                            title: b.label,
                            amount: b.amount,
                            only: 'all',
                          })"
                        >
                          <td class="small">
                            {{ b.label }}
                            <div v-if="b.why" class="xs muted">{{ b.why }}</div>
                            <button class="link" type="button">看这些行 →</button>
                          </td>
                          <td class="right num xs">{{ count(b.count) }}</td>
                          <td class="right num">{{ money(b.amount) }}</td>
                        </tr>
                      </tbody>
                      <tfoot>
                        <tr>
                          <td class="small strong">要查归属的合计</td>
                          <td class="right num xs">
                            {{ unlinkedGraded ? count(unlinkedNeedsWork.count) : '—' }}
                          </td>
                          <td class="right num strong">{{ money(snap.unlinked_total) }}</td>
                        </tr>
                      </tfoot>
                    </n-table>
                  </div>
                  <p class="why" style="margin-top: var(--s3)">
                    合计只算「要查归属」那一类，所以它和上面几行加起来不相等。
                    几类相加得到的是一个没有业务含义的净额——别家店的运单、规则排除的非经营流水、
                    别的账期的订单收款，三者相减的巧合。真正要人查的只有
                    <b class="num">{{ money(snap.unlinked_total) }}</b>。
                  </p>
                </template>
              </n-tab-pane>
            </n-tabs>
          </div>
        </div>
      </template>

      <n-empty v-else description="这家店还没有算出来的账期" style="padding: var(--s7) 0">
        <template #extra><DropZone /></template>
      </n-empty>
    </template>

    <n-modal
      v-model:show="asking"
      preset="dialog"
      title="反结账"
      positive-text="确定"
      negative-text="算了"
      :positive-button-props="{ disabled: !why.trim() }"
      @positive-click="reopen"
    >
      <p class="small muted" style="margin-bottom: var(--s3)">
        这个账期已经报出去了。写清为什么要改回去——这条会记在账期历史里。
      </p>
      <n-input v-model:value="why" type="textarea" :rows="3" placeholder="比如：运费表交漏了一张，已补传并等待自动计算" />
    </n-modal>

    <FixPanel
      v-model:show="fixing"
      :store-id="props.id"
      :period="period"
      :unclassified="snap?.unclassified || []"
      :auto-mode="app.ingestMode === 'nas'"
      @recompute="recompute"
    />

    <DrillDrawer
      v-if="drill"
      :run-id="drill.runId"
      :node="drill.node"
      :title="drill.name"
      :only="drill.only || 'counted'"
      :store-id="props.id"
      :period="period"
      :platform="info.store?.platform || ''"
      :live-index="app.ingestMode === 'nas'"
      @close="drill = null"
    />
  </n-spin>
</template>
