<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { NButton, NDrawer, NDrawerContent, NPagination } from 'naive-ui'
import { GripVertical, Paperclip, Undo2, Redo2 } from '@lucide/vue'
import { money } from '../format.js'
import { tokens } from '../design/tokens.js'
import { createSpring } from './ui/springMotion.js'
import { createReviewGroups } from './ui/reviewGroups.js'

const props = defineProps({ show: Boolean, evidenceOpen: Boolean, items: { type: Array, required: true }, scopeKey: { type: String, required: true }, canOpen: { type: Function, required: true } })
const emit = defineEmits(['update:show', 'open'])
const entries = computed(() => props.items.map((item, index) => ({ id: `${index}:${item.kind || ''}:${item.node || item.metric || item.orders || item.source || ''}`, item, index })))
const groups = createReviewGroups(entries.value.map(e => e.id), props.scopeKey)
const revision = ref(0), page = ref(1), stage = ref(null), dragging = ref(false), status = ref('抓住事项拖入临时组；拉开底边查看完整说明。')
const opened = reactive({}), roots = new Map(), springs = new Map(), groupRoots = new Map(), groupSprings = new Map()
const membership = computed(() => { revision.value; return groups.membership })
const visible = computed(() => entries.value.slice((page.value - 1) * 4, page.value * 4))
const canUndo = computed(() => { revision.value; return groups.canUndo }), canRedo = computed(() => { revision.value; return groups.canRedo })
const groupCount = id => entries.value.filter(e => membership.value[e.id] === id).length
const pageGroupCount = id => visible.value.filter(e => membership.value[e.id] === id).length
const dimensions = reactive({ height: 520, pendingLabel: 0, groupsLabel: 0 })
let gesture = null, observer = null, layoutFrame = 0, lastWidth = 0
const homes = new Map(), groupHomes = new Map()
const clamp = (value, min, max) => Math.max(min, Math.min(max, value))
function bindCard(id, element) { if (element) roots.set(id, element); else roots.delete(id) }
function motion(id) {
  if (!springs.has(id)) {
    const value = { x: 0, y: 0, angle: 0 }
    const draw = () => { const el = roots.get(id); if (el) el.style.transform = `translate3d(${value.x}px,${value.y}px,0) rotate(${value.angle}deg)` }
    value.mx = createSpring(0, x => { value.x = x; draw() })
    value.my = createSpring(0, y => { value.y = y; draw() })
    value.ma = createSpring(0, a => { value.angle = a; draw() })
    springs.set(id, value)
  }
  return springs.get(id)
}
function queueLayout() { if (!layoutFrame) layoutFrame = requestAnimationFrame(() => { layoutFrame = 0; layout() }) }
function groupMotion(id) {
  if (!groupSprings.has(id)) {
    const value = { x: 0, y: 0 }, draw = () => { const el = groupRoots.get(id); if (el) el.style.transform = `translate(${value.x}px,${value.y}px)` }
    value.mx = createSpring(0, x => { value.x = x; draw() }); value.my = createSpring(0, y => { value.y = y; draw() }); groupSprings.set(id, value)
  }
  return groupSprings.get(id)
}
function layout(instant = false) {
  const host = stage.value
  if (!host?.clientWidth) return
  const width = host.clientWidth, columns = width >= 780 ? 4 : 2, gap = tokens.space['4'], cw = (width - gap * (columns - 1)) / columns
  const groupColumns = width < 640 ? 1 : 2, gw = (width - gap * (groupColumns - 1)) / groupColumns
  let y = tokens.geometry['board-label']
  for (let offset = 0; offset < visible.value.length; offset += columns) {
    let height = tokens.geometry['pending-min']
    visible.value.slice(offset, offset + columns).forEach((entry, column) => {
      const el = roots.get(entry.id); if (!el) return
      el.style.width = `${membership.value[entry.id] ? gw - tokens.geometry['group-inset'] * 2 : cw}px`
      const cardHeight = membership.value[entry.id] ? tokens.geometry['pending-min'] : el.offsetHeight
      homes.set(entry.id, { x: column * (cw + gap), y, width: cw, height: cardHeight }); height = Math.max(height, cardHeight)
    })
    y += height + tokens.space['6']
  }
  dimensions.groupsLabel = y; y += tokens.geometry['board-label']
  for (let row = 0; row < Math.ceil(2 / groupColumns); row++) {
    let tallest = tokens.geometry['group-min']
    ;['A', 'B'].slice(row * groupColumns, (row + 1) * groupColumns).forEach((id, column) => {
      let innerY = tokens.geometry['group-header']
      for (const entry of visible.value.filter(e => membership.value[e.id] === id)) {
        const el = roots.get(entry.id)
        homes.set('group:' + entry.id, { x: column * (gw + gap) + tokens.geometry['group-inset'], y: y + innerY })
        innerY += (el?.offsetHeight || 88) + tokens.space['3']
      }
      const height = Math.max(tokens.geometry['group-min'], innerY + tokens.geometry['group-footer'])
      const home = { x: column * (gw + gap), y, width: gw, height }; groupHomes.set(id, home); tallest = Math.max(tallest, height)
      const el = groupRoots.get(id); if (el) Object.assign(el.style, { width: `${gw}px`, height: `${height}px` })
      if (!(gesture?.kind === 'group' && gesture.id === id)) { const m = groupMotion(id); m.mx[instant ? 'jump' : 'to'](home.x); m.my[instant ? 'jump' : 'to'](home.y) }
    })
    y += tallest + gap
  }
  dimensions.height = y
  for (const entry of visible.value) {
    if (gesture?.kind === 'card' && gesture.id === entry.id) continue
    const home = membership.value[entry.id] ? homes.get('group:' + entry.id) : homes.get(entry.id)
    if (!home) continue
    const m = motion(entry.id); m.mx[instant ? 'jump' : 'to'](home.x); m.my[instant ? 'jump' : 'to'](home.y); m.ma.to(0)
  }
  lastWidth = width
}
function changed(label) { revision.value++; status.value = label; nextTick(() => layout()); }
function move(id, group) { if (groups.move([id], group)) changed(group ? `已放入临时组 ${group}，原事项金额与核对状态保持不变。` : '已抽回待核对清单。') }
function merge(from, into) { if (groups.merge(from, into)) changed(`临时组 ${from} 已并入 ${into}；可撤回或单独抽出。`) }
function split(id) { if (groups.split(id)) changed('已拆开临时组，原始事项仍逐条保留。') }
function openEntry(item){emit('open',item);if(item.node==='__sources__')emit('update:show',false)}
function clearTarget() { groupRoots.forEach(el => el?.classList.remove('is-target')); roots.forEach(el => el?.classList.remove('is-target')) }
function start(event, kind, id) {
  if (event.button !== 0 || gesture || event.target.closest('select,[data-open-evidence]')) return
  const element = event.currentTarget, m = kind === 'card' ? motion(id) : null
  if (m) { m.mx.jump(m.x); m.my.jump(m.y); m.ma.jump(m.angle) }
  gesture = { kind, id, element, pointer: event.pointerId, startX: event.clientX, startY: event.clientY, lastX: event.clientX, lastY: event.clientY, time: performance.now(), vx: 0, vy: 0, moved: false, originX: m?.x || 0, originY: m?.y || 0, target: null, source: membership.value[id] }
  if (kind === 'group') {
    const group = groupMotion(id); group.mx.jump(group.x); group.my.jump(group.y); gesture.originX=group.x;gesture.originY=group.y
    gesture.memberOrigins=Object.fromEntries(visible.value.filter(e=>membership.value[e.id]===id).map(e=>{const value=motion(e.id);value.mx.jump(value.x);value.my.jump(value.y);roots.get(e.id)?.classList.add('moving-group');return[e.id,{x:value.x,y:value.y}]}))
    groupRoots.get(id)?.classList.add('dragging')
  }
  if (kind === 'paper') { springs.get('paper:'+id)?.destroy();gesture.originOpen = !!opened[id]; gesture.originHeight = roots.get(id)?.querySelector('.review-explanation')?.clientHeight || 0 }
  dragging.value = true; element.setPointerCapture(event.pointerId)
  if (kind === 'card') { roots.get(id)?.classList.add('dragging'); element.querySelector('.review-card-handle')?.focus({ preventScroll: true }) }
}
function drag(event) {
  const g = gesture; if (!g || event.pointerId !== g.pointer) return
  const dx = event.clientX - g.startX, dy = event.clientY - g.startY, now = performance.now(), dt = Math.max(8, now - g.time)
  g.vx = clamp((event.clientX - g.lastX) / dt * 1000, -1400, 1400); g.vy = clamp((event.clientY - g.lastY) / dt * 1000, -1400, 1400)
  g.lastX = event.clientX; g.lastY = event.clientY; g.time = now; g.moved ||= Math.hypot(dx, dy) > 4
  if (g.kind === 'paper') {
    const explanation = roots.get(g.id)?.querySelector('.review-explanation'), inner = explanation?.firstElementChild
    if (explanation && inner) { explanation.style.height = `${clamp(g.originHeight + dy, 0, inner.scrollHeight)}px`; queueLayout() }
    return
  }
  if (g.kind === 'card') {
    const m = motion(g.id), el = roots.get(g.id)
    m.mx.jump(clamp(g.originX + dx, -8, stage.value.clientWidth - el.offsetWidth + 8)); m.my.jump(clamp(g.originY + dy, 24, dimensions.height - el.offsetHeight)); m.ma.to(clamp(g.vx / 240, -4, 4))
  } else {
    const group = groupMotion(g.id); group.mx.jump(g.originX + dx); group.my.jump(g.originY + dy)
    visible.value.filter(e => membership.value[e.id] === g.id).forEach(entry => { const home = g.memberOrigins[entry.id], m = motion(entry.id); m.mx.jump(home.x + dx); m.my.jump(home.y + dy) })
  }
  clearTarget(); g.target = null
  for (const id of ['A', 'B']) {
    if (g.kind === 'group' && id === g.id) continue
    const rect = groupRoots.get(id)?.getBoundingClientRect()
    if (rect && event.clientX >= rect.left - 12 && event.clientX <= rect.right + 12 && event.clientY >= rect.top - 12 && event.clientY <= rect.bottom + 12) {
      g.target = { kind: 'group', id }; groupRoots.get(id).classList.add('is-target'); break
    }
  }
  if (!g.target && g.kind === 'card') {
    for (const entry of visible.value) {
      if (entry.id === g.id || membership.value[entry.id]) continue
      const rect = roots.get(entry.id)?.getBoundingClientRect()
      if (rect && event.clientX >= rect.left && event.clientX <= rect.right && event.clientY >= rect.top && event.clientY <= rect.bottom) { g.target = { kind: 'peer', id: entry.id }; roots.get(entry.id).classList.add('is-target'); break }
    }
  }
  status.value = g.target ? '松手放入目标；改变方向或按 Esc 可以反悔。' : '空白处松手会回位；组内事项可向外抽出。'
}
function finish(event, cancelled = false) {
  const g = gesture; if (!g || (event && event.pointerId !== g.pointer)) return
  if(event && !cancelled && g.moved){
    const previous={x:g.lastX,y:g.lastY,vx:g.vx,vy:g.vy,time:g.time};drag(event)
    if(performance.now()-previous.time<90 && Math.hypot(event.clientX-previous.x,event.clientY-previous.y)<1){g.vx=previous.vx;g.vy=previous.vy}
  }
  gesture = null; dragging.value = false; clearTarget()
  try { g.element.releasePointerCapture(g.pointer) } catch { /* capture may already be lost */ }
  roots.get(g.id)?.classList.remove('dragging')
  if(g.kind==='group'){groupRoots.get(g.id)?.classList.remove('dragging');roots.forEach(el=>el.classList.remove('moving-group'))}
  if (g.kind === 'paper') {
    const explanation = roots.get(g.id)?.querySelector('.review-explanation'), max = explanation?.firstElementChild?.scrollHeight || 0, height = explanation?.clientHeight || 0
    opened[g.id] = cancelled ? g.originOpen : g.moved ? height + g.vy * .03 > max / 2 : !g.originOpen
    reveal(g.id, opened[g.id], height); return
  }
  if (!cancelled && g.moved) {
    if (g.kind === 'group' && g.target?.kind === 'group') merge(g.id, g.target.id)
    else if (g.kind === 'card' && g.target?.kind === 'group') move(g.id, g.target.id)
    else if (g.kind === 'card' && g.target?.kind === 'peer') {
      const empty = ['A', 'B'].find(id => !groupCount(id)); if (empty && groups.move([g.id, g.target.id], empty)) changed('两项已归入同一临时处理组。')
    } else if (g.kind === 'card' && g.source) move(g.id, null)
  }
  if (g.kind === 'card') { const m = motion(g.id); m.mx.to(m.x, cancelled ? 0 : g.vx); m.my.to(m.y, cancelled ? 0 : g.vy) }
  if(g.kind==='group'){
    const group=groupMotion(g.id);group.mx.to(group.x,cancelled?0:g.vx*.5);group.my.to(group.y,cancelled?0:g.vy*.5)
    Object.keys(g.memberOrigins||{}).forEach(id=>{const m=motion(id);m.mx.to(m.x,cancelled?0:g.vx*.5);m.my.to(m.y,cancelled?0:g.vy*.5)})
  }
  status.value = cancelled ? '已反悔，回到本次动作前的位置。' : status.value
  nextTick(() => layout())
}
function reveal(id, value, from) {
  const el = roots.get(id)?.querySelector('.review-explanation'); if (!el) return
  opened[id] = value
  const key = 'paper:' + id, previous = springs.get(key); previous?.destroy()
  const spring = createSpring(from ?? el.clientHeight, height => { el.style.height = `${Math.max(0, height)}px`; queueLayout() }, { stiffness: tokens.motion['evidence-stiffness'], damping: tokens.motion['evidence-damping'] })
  springs.set(key, spring); spring.to(value ? el.firstElementChild.scrollHeight : 0)
}
function keydown(event) { if (event.key === 'Escape' && gesture) { event.preventDefault(); event.stopPropagation(); finish(null, true) } }
function blur() { if(gesture)finish(null,true) }
watch(() => [props.scopeKey, entries.value.map(e => e.id).join('|')], () => {
  finish(null, true); groups.reset(entries.value.map(e => e.id), props.scopeKey); revision.value++; page.value = 1
  Object.keys(opened).forEach(key => delete opened[key]); springs.forEach(m => { if (m.destroy) m.destroy(); else { m.mx.destroy(); m.my.destroy(); m.ma.destroy() } }); springs.clear()
  status.value = '核算范围已更新，按新的原始事项重新整理。'; nextTick(() => layout(true))
})
function restoreOpen() { for (const entry of visible.value) { const el = roots.get(entry.id)?.querySelector('.review-explanation'); if (el) el.style.height = opened[entry.id] ? `${el.firstElementChild.scrollHeight}px` : '0px' } }
watch([page, () => props.show], () => { finish(null, true); nextTick(() => { restoreOpen(); layout(true) }) })
watch(stage, element => { observer?.disconnect(); if (element) observer?.observe(element); nextTick(() => { restoreOpen(); layout(true) }) })
onMounted(() => { observer = new ResizeObserver(() => { if (stage.value?.clientWidth !== lastWidth) { finish(null, true); layout(true) } }); if (stage.value) observer.observe(stage.value); document.addEventListener('keydown', keydown, true);window.addEventListener('blur',blur); nextTick(() => layout(true)) })
onBeforeUnmount(() => { finish(null, true); observer?.disconnect(); document.removeEventListener('keydown', keydown, true);window.removeEventListener('blur',blur); cancelAnimationFrame(layoutFrame); springs.forEach(m => { if (m.destroy) m.destroy(); else { m.mx.destroy(); m.my.destroy(); m.ma.destroy() } }); groupSprings.forEach(m => { m.mx.destroy(); m.my.destroy() }) })
</script>

<template>
  <NDrawer :show="show" :width="'min(1120px, 100vw)'" :close-on-esc="!dragging && !evidenceOpen" :mask-closable="!dragging && !evidenceOpen" @update:show="emit('update:show', $event)">
    <NDrawerContent title="临时处理桌面" closable>
      <div class="temporary-workspace-toolbar">
        <p class="xs muted">仅整理本页阅读顺序，原金额、原始依据和核对状态保持不变。</p>
        <div class="row"><NButton :disabled="!canUndo || dragging" @click="groups.undo() && changed('已撤回临时分组。')"><template #icon><Undo2 :size="16" /></template>撤回</NButton><NButton :disabled="!canRedo || dragging" @click="groups.redo() && changed('已重做临时分组。')"><template #icon><Redo2 :size="16" /></template>重做</NButton></div>
      </div>
      <div ref="stage" class="review-stage" :style="{height: `${dimensions.height}px`}">
        <span class="review-stage-label">待核对 · 第 {{(page-1)*4+1}}–{{Math.min(page*4,items.length)}} 项 / 共 {{items.length}} 项</span>
        <span class="review-stage-label" :style="{top:`${dimensions.groupsLabel}px`}">临时处理组 · 不作金额合计</span>
        <section v-for="id in ['A','B']" :key="id" :ref="el=>{if(el)groupRoots.set(id,el)}" class="review-group">
          <button class="review-group-handle" type="button" :disabled="!pageGroupCount(id)" :aria-label="`整体拖动临时组 ${id}`" @pointerdown="start($event,'group',id)" @pointermove="drag" @pointerup="finish" @pointercancel="finish($event,true)" @lostpointercapture="gesture && finish(null,true)"><span><Paperclip :size="16"/>临时组 {{id}}</span><span class="xs muted">本页 {{pageGroupCount(id)}} / 共 {{groupCount(id)}} 项</span></button>
          <span v-if="!pageGroupCount(id)" class="review-group-empty">把相关事项放在一起</span>
          <footer><span>临时阅读草稿</span><div><button type="button" :disabled="!groupCount(id)||dragging" @click="merge(id,id==='A'?'B':'A')">并入 {{id==='A'?'B':'A'}}</button><button type="button" :disabled="!groupCount(id)||dragging" @click="split(id)">拆开</button></div></footer>
        </section>
        <article v-for="entry in visible" :key="entry.id" :ref="el=>bindCard(entry.id,el)" class="review-card" :class="{grouped:membership[entry.id]}" :data-group="membership[entry.id]||'pending'" @pointerdown="start($event,'card',entry.id)" @pointermove="drag" @pointerup="finish" @pointercancel="finish($event,true)" @lostpointercapture="gesture?.kind==='card' && finish(null,true)">
          <button type="button" class="review-card-handle" :aria-label="`拖动事项 ${entry.index+1}：${entry.item.title}`" @click="e=>{if(e.detail===0)reveal(entry.id,!opened[entry.id])}"><GripVertical :size="12"/><span>{{entry.item.title}}</span><span v-if="entry.item.amount!==null && entry.item.amount!==undefined" class="num">{{money(entry.item.amount)}}</span></button>
          <div v-if="!membership[entry.id]" class="review-card-summary"><p>{{entry.item.detail}}</p></div>
          <div class="review-explanation"><div class="review-explanation-inner"><p>{{entry.item.detail}}</p><label>临时归组<select :value="membership[entry.id]||''" @change="move(entry.id,$event.target.value||null)"><option value="">待核对</option><option value="A">临时组 A</option><option value="B">临时组 B</option></select></label><NButton data-open-evidence size="small" :disabled="!canOpen(entry.item)" @click="openEntry(entry.item)">{{canOpen(entry.item)?entry.item.orders?'查看订单明细':entry.item.node==='__sources__'?'查看所需资料':'打开原始依据':'本事项暂无逐项明细入口'}}</NButton></div></div>
          <button type="button" class="review-card-peel" :aria-expanded="!!opened[entry.id]" @pointerdown.stop="start($event,'paper',entry.id)" @pointermove.stop="drag" @pointerup.stop="finish" @pointercancel.stop="finish($event,true)" @click="e=>{if(e.detail===0)reveal(entry.id,!opened[entry.id])}"><span aria-hidden="true"/>{{opened[entry.id]?'收回核对说明':'拉开核对说明'}}</button>
        </article>
      </div>
      <div class="review-status" role="status" aria-live="polite">{{status}} <span class="muted">Esc 取消当前手势</span></div>
      <NPagination v-if="items.length>4" v-model:page="page" :item-count="items.length" :page-size="4" :disabled="dragging" />
    </NDrawerContent>
  </NDrawer>
</template>
