<script setup>
/* 左边这一栏只干一件事：在五十多家店里点到想看的那一家。
 *
 * 分两级是因为店名普遍带平台前缀，平铺时开头几个字都一样，眼睛要逐字比对才能分开。
 * 一次只展开一个平台：九个平台全摊开是五十多行，等于没分级。双击表头能把当前平台也
 * 收起来，用来一眼看全有哪些平台。搜索时反过来全展开，否则命中的店藏在收起的平台里，
 * 搜了也看不见。
 *
 * 层级靠字号，不靠字重。上一版平台名和店名都是 13px，只差 33px 缩进和 30 的字重，
 * 结果选中的店比它所属的平台标题还粗——十九行店名成了一堵均匀的字墙，没有落点。
 */
import { computed, h, ref } from 'vue'

const props = defineProps({
  platforms: { type: Array, default: () => [] },
  stores: { type: Array, default: () => [] },
  activePlatform: { type: String, default: '' },
  selectedStore: { type: String, default: '' },
})

const emit = defineEmits(['select-platform', 'select-store'])
const query = ref('')

const platformKey = (id) => `platform:${id}`

/* 状态点说的是这家店最近一个账期算到哪一步。「一份表都没交」用空心圈，不用灰实心点：
   它和「交了表但还没结账」是两件事，只差一档灰度的话看不出来。 */
const STATE_TEXT = {
  closed: '最近一个账期已结账',
  open: '最近一个账期还没结账',
  none: '还没算过账',
}

const menuOptions = computed(() => {
  const word = query.value.trim().toLocaleLowerCase()
  return props.platforms.flatMap((platform) => {
    const all = props.stores.filter((store) => store.platform === platform.id)
    if (!all.length) return []
    const platformMatches = platform.name.toLocaleLowerCase().includes(word)
    const stores = !word || platformMatches
      ? all
      : all.filter((store) => store.name.toLocaleLowerCase().includes(word))
    if (!stores.length) return []
    return [{
      key: platformKey(platform.id),
      label: platform.name,
      kind: 'platform',
      platform,
      count: all.length,
      children: stores.map((store) => ({
        key: store.id,
        label: store.name,
        kind: 'store',
        store,
      })),
    }]
  })
})

/* 哪个平台被手动收起了。存 id 不存开关：换平台之后这个 id 自然就对不上，新平台照常
   是展开的，不用另写一段去复位。 */
const collapsedPlatform = ref('')

/* 展开的就是当前平台，不另存一份。
 *
 * 上一版另存了一个 ref 记展开了谁。刷新时店铺注册表还没到，选项列表是空的，组件库这时
 * 会回一次「展开的是空的」，那个 ref 就被清掉了；等店铺到了，当前平台并没有变，负责
 * 重新展开它的 watch 也就不再触发——于是刷新之后九个平台全是收起的，选中的店藏在里面。 */
const shownExpandedKeys = computed(() => {
  if (query.value.trim()) return menuOptions.value.map((option) => option.key)
  if (!props.activePlatform || collapsedPlatform.value === props.activePlatform) return []
  return [platformKey(props.activePlatform)]
})

/** 点开另一个平台就等于切到那个平台。单击只做展开这类没代价的事——总有一家店是选中的，
 *  单击一下就把它所在的平台收掉会让人找不到自己在哪儿，收起交给双击。 */
function updateExpanded(keys) {
  if (query.value.trim()) return
  const mine = platformKey(props.activePlatform)
  const opened = keys
    .filter((key) => String(key).startsWith('platform:'))
    .find((key) => key !== mine)
  if (opened) {
    emit('select-platform', String(opened).slice('platform:'.length))
  } else if (keys.includes(mine)) {
    collapsedPlatform.value = ''
  }
}

/* 双击平台表头收起它，再双击展开：想一眼看全有哪些平台，就双击一下当前这个。
 *
 * 状态得在第一下按下时就记住，因为双击的第一下已经被单击那条路走过一遍了：双击一个
 * 还没选中的平台，第一下把它切成了当前平台，接着的 dblclick 就会把刚点开的又收起来
 * ——人要的是「展开看看」，得到的是「什么都没有」。所以只认「按下之前就已经是当前
 * 平台」的那一次，收还是展也按按下之前的状态算。 */
let activeBeforeClick = ''
let collapsedBeforeClick = false
function rememberActive(event) {
  if (event.detail > 1) return
  activeBeforeClick = props.activePlatform
  collapsedBeforeClick = collapsedPlatform.value === props.activePlatform
}

function toggleCollapse(event) {
  if (query.value.trim()) return
  // 从行往下找，不是从点到的地方往上找：这样点在计数、箭头、行末的空白上都算数，
  // 而店铺那一行里没有这个标记，双击它不会误收起整个平台。
  const row = event.target.closest('.n-menu-item-content')
  const id = row?.querySelector('[data-platform]')?.dataset.platform
  if (!id || id !== activeBeforeClick) return
  collapsedPlatform.value = collapsedBeforeClick ? '' : id
}

function select(key, option) {
  if (option.kind === 'store') emit('select-store', option.store)
}

function renderLabel(option) {
  if (option.kind === 'platform') {
    return h('span', {
      class: 'platform-label',
      'data-platform': option.platform.id,
      title: `${option.platform.name}，${option.count} 家店。双击收起`,
    }, option.label)
  }
  const store = option.store
  const state = store.latest_state || 'none'
  return h('span', { class: 'store-label', title: `${store.name} · ${STATE_TEXT[state]}` }, [
    h('span', { class: ['store-dot', `is-${state}`] }),
    h('span', { class: 'store-name' }, store.name),
  ])
}

/* 计数一律「数字 + 单位」。上一版平台那一栏只写「19」，不写单位——19 家店还是 19 张表，
   看的人只能自己猜。数字单独一格、等宽、右对齐到同一列，一列扫下来才比得出多少；
   单位比数字淡一档，先看见的是数，不是那个「家」字。 */
function countTag(value, unit, hint) {
  return h('span', { class: 'count-tag', title: `${value} ${unit}${hint}` }, [
    h('span', { class: 'count-num num' }, value),
    h('span', { class: 'count-unit' }, unit),
  ])
}

function renderExtra(option) {
  if (option.kind === 'platform') return countTag(option.count, '家', '店')
  if (!option.store.file_count) return null
  return countTag(option.store.file_count, '张', '当前生效的表')
}
</script>

<template>
  <aside class="store-nav" aria-label="店铺导航">
    <div class="store-nav-head">
      <h3>店铺</h3>
      <!-- 两段各自包一层，间距全交给 flex 的 gap。直接把分隔点摊在文字里不行：
           模板里换行处的空白会被编译时收掉一侧，点号左边有空格右边没有。 -->
      <span class="store-nav-stat">
        <span><span class="n num">{{ stores.length }}</span> 家店</span>
        <span class="sep" aria-hidden="true">·</span>
        <span><span class="n num">{{ menuOptions.length }}</span> 个平台</span>
      </span>
    </div>

    <!-- 留白挂在外面这层 div 上。组件库那个输入框的边框是按它自己的根元素铺满画的，
         padding 写在根元素上会把可视的框一起撑大：框宽出去 32 像素，框里文字底下
         还空一条 12 像素的白，看着像输入框没画完。 -->
    <div class="store-nav-search">
      <n-input v-model:value="query" clearable placeholder="搜索平台或店铺">
        <template #prefix>
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="11" cy="11" r="6.5" />
            <path d="m16 16 4 4" />
          </svg>
        </template>
      </n-input>
    </div>

    <!-- 用原生滚动，和这套界面里长表格的做法一致（app.css 的 .scroll）。组件库那个
         自绘滚动条内层写的是 height:100%，而这一层的高度又取决于内容——两边互相等，
         浏览器只能按内容高算，于是展开一个平台就被卡片裁掉，底下几家店滚不到。 -->
    <div
      v-if="menuOptions.length"
      class="store-nav-list"
      @mousedown="rememberActive"
      @dblclick="toggleCollapse"
    >
      <n-menu
        :value="selectedStore"
        :expanded-keys="shownExpandedKeys"
        :options="menuOptions"
        :indent="16"
        :root-indent="10"
        :render-label="renderLabel"
        :render-extra="renderExtra"
        accordion
        @update:value="select"
        @update:expanded-keys="updateExpanded"
      />
    </div>
    <p v-else class="store-nav-blank small muted">
      没有叫「{{ query.trim() }}」的平台或店铺。
    </p>
  </aside>
</template>

<style scoped>
/* 高度自己封顶、内部自己滚。上一版让这一栏和右边的详情共用一张卡片，卡片被这一栏
   的一屏高撑满，右边只有十行文件时底下空出四百多像素白，看起来像表没加载完。
   减掉的那 208px 是顶栏、页面标题区和上下留白——它们都在这一栏上方，是固定的。 */
.store-nav {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 380px;
  max-height: calc(100vh - 208px);
  background: var(--n0);
  border: 1px solid var(--n3);
  border-radius: var(--r-lg);
  overflow: hidden;
}

.store-nav-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--s2);
  padding: var(--s4) var(--s4) var(--s3);
}

/* 数字看得见、单位退一档。上一版整句都是 11px 的浅灰，「52」和「家」一样重，
   扫过去只是一团灰字，看不出这里报的是两个数。 */
.store-nav-stat {
  display: inline-flex;
  gap: 5px;
  font-size: var(--t-xs);
  color: var(--n5);
  white-space: nowrap;
}
.store-nav-stat .n { font-weight: 620; color: var(--n7); }
.store-nav-stat .sep { color: var(--n4); }

/* 用组件库默认的 34px 高，和这套界面里别的输入框一样（design.css 的 input 是
   13px 字加 7px 内边距，也是 33 上下）。上一版写了 size="small"，28px 高——比
   底下 33px 一行的店铺还矮，是一屏里最小的那个可点区域，而它是搜索框。 */
.store-nav-search { padding: 0 var(--s4) var(--s3); }
/* 图标和占位符之间原本只有 4px，放大镜几乎贴着字。 */
.store-nav-search :deep(.n-input__prefix) { margin-right: 7px; }
.store-nav-search svg {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  color: var(--n5);
}

.store-nav-list {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  border-top: 1px solid var(--n2);
  scrollbar-width: thin;
}

.store-nav-blank { padding: var(--s5) var(--s4); border-top: 1px solid var(--n2); }

/* 组件库那套配色已经从 design.css 映过来了，选中态直接改它的变量就行——上一版是拿
   `!important` 盖 ::before 的底色，每次升级组件库都得回来看还灵不灵。 */
:deep(.n-menu) { padding: var(--s2) var(--s2) var(--s3); }
:deep(.n-menu-item) {
  --n-item-height: 33px;
  --n-item-color-active: var(--accent-bg);
  --n-item-color-active-hover: var(--accent-bg);
  margin: 1px 0;
}
:deep(.n-menu-item-content) { border-radius: var(--r-sm); }
:deep(.n-menu-item-content::before) { left: 0; right: 0; border-radius: var(--r-sm); }
:deep(.n-menu-item-content--selected::before) { box-shadow: inset 3px 0 0 var(--accent); }

/* 行内是「名字 + 张数」。上一版这里是 display:block，里头的名字又是块级 flex，
   于是组件库那个贴行尾的 extra 被挤到了下一行——选中的店底下就多出一个孤零零的
   「10 张」，比店名还靠左。 */
:deep(.n-menu-item-content-header) {
  display: flex;
  align-items: center;
  gap: var(--s2);
  min-width: 0;
}
:deep(.n-menu-item-content-header__extra) { flex: 0 0 auto; margin-left: auto; }

:deep(.platform-label) {
  font-size: var(--t-xs);
  font-weight: 600;
  letter-spacing: .02em;
  color: var(--n6);
}
:deep(.n-menu-item-content--child-active .platform-label) { color: var(--accent); }

/* 计数。数字那一格定宽右对齐，一列 19 / 2 / 6 / 17 的十位数才对得齐，
   不然「2」和「17」的起笔错开，比不出哪个平台店多。 */
:deep(.count-tag) {
  display: inline-flex;
  align-items: baseline;
  gap: 2px;
  font-size: var(--t-xs);
  color: var(--n6);
}
:deep(.count-num) { min-width: 2ch; text-align: right; }
:deep(.count-unit) { font-size: 11px; color: var(--n5); }
:deep(.n-menu-item-content--selected .count-tag),
:deep(.n-menu-item-content--selected .count-unit) { color: var(--accent); }

:deep(.store-label) { display: flex; align-items: center; gap: var(--s2); flex: 1 1 auto; min-width: 0; }
:deep(.store-name) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--t-md);
}
:deep(.n-menu-item-content--selected .store-name) { font-weight: 620; }

:deep(.store-dot) {
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  border-radius: 50%;
}
:deep(.store-dot.is-closed) { background: var(--ok); }
:deep(.store-dot.is-open) { background: var(--warn); }
:deep(.store-dot.is-none) { border: 1.5px solid var(--n4); }

@media (max-width: 900px) {
  .store-nav { min-height: 0; max-height: 44vh; }
}
</style>
