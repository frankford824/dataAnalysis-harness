<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NButton, NPagination } from 'naive-ui'
import { money } from '../format.js'
import { createSpring } from './ui/springMotion.js'

const props = defineProps({ rows: { type: Array, default: () => [] }, period: { type: String, default: '' } })
const expanded = ref(false), dragging = ref(false), page = ref(1), host = ref(null), fraction = ref(0)
const shown = computed(() => props.rows.slice((page.value - 1) * 6, page.value * 6))
let gesture = null, observer = null
const spring = createSpring(0, value => { fraction.value = Math.max(0, Math.min(1, value)); paint() })
function paint() { if (host.value) host.value.style.height = `${host.value.firstElementChild.scrollHeight * fraction.value}px` }
function toggle() { expanded.value = !expanded.value; spring.to(expanded.value ? 1 : 0) }
function start(event) {
  if (event.button !== 0) return
  spring.jump(fraction.value); dragging.value = true
  gesture = { pointer: event.pointerId, y: event.clientY, start: fraction.value, previous: expanded.value, moved: false, el: event.currentTarget }
  event.currentTarget.setPointerCapture(event.pointerId)
}
function move(event) { if (!gesture) return; gesture.moved ||= Math.abs(event.clientY - gesture.y) > 4; spring.jump(Math.max(0, Math.min(1, gesture.start + (event.clientY - gesture.y) / 180))) }
function finish(cancelled = false) {
  if (!gesture) return
  const g = gesture; gesture = null; dragging.value = false
  try { g.el.releasePointerCapture(g.pointer) } catch { /* lost capture is reversible */ }
  expanded.value = cancelled ? g.previous : g.moved ? fraction.value > .5 : !g.previous
  spring.to(expanded.value ? 1 : 0)
}
function keydown(event) { if (event.key === 'Escape' && gesture) { event.preventDefault(); event.stopPropagation(); finish(true) } }
function blur(){finish(true)}
watch([page, () => props.rows], () => { page.value=Math.min(page.value,Math.max(1,Math.ceil(props.rows.length/6)));nextTick(paint) })
watch(host, element => { observer?.disconnect(); if(element)observer?.observe(element.firstElementChild); nextTick(paint) })
watch(() => props.period, () => { finish(true); expanded.value = false; page.value = 1; spring.jump(0) })
onMounted(() => { observer = new ResizeObserver(paint); if (host.value) observer.observe(host.value.firstElementChild); document.addEventListener('keydown', keydown, true);window.addEventListener('blur',blur) })
onBeforeUnmount(() => { finish(true);spring.destroy(); observer?.disconnect(); document.removeEventListener('keydown', keydown, true);window.removeEventListener('blur',blur) })
</script>
<template>
  <section v-if="rows.length" class="allocation-fan" aria-label="兼职分摊关系">
    <button class="allocation-fan-grip" type="button" :aria-expanded="expanded" @pointerdown="start" @pointermove="move" @pointerup="finish(false)" @pointercancel="finish(true)" @lostpointercapture="gesture && finish(true)" @click="e=>{if(e.detail===0)toggle()}"><span aria-hidden="true"/>{{expanded?'收回分摊关系':'拉开分摊关系'}} · {{rows.length}} 家店</button>
    <div ref="host" class="allocation-fan-reveal"><div>
      <p class="xs muted">与下方明细表使用同一份已返回结果；已结账金额仍以原结账记录为准。</p>
      <div class="allocation-fan-cards"><article v-for="row in shown" :key="row.store_id || row.store" class="allocation-fan-card"><h3>{{row.store}}</h3><p class="xs muted">销售占比 {{row.share==null?'待核对':(row.share*100).toFixed(2)+'%'}}</p><strong class="num">{{row.amount==null?'—':'¥ '+money(row.amount)}}</strong></article></div>
      <NPagination v-if="rows.length>6" v-model:page="page" :item-count="rows.length" :page-size="6" :disabled="dragging" />
    </div></div>
  </section>
</template>
