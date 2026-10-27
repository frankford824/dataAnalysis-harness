<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { MoveHorizontal } from '@lucide/vue'
import { createSpring } from './springMotion.js'

const props = defineProps({ periods: { type: Array, required: true }, modelValue: { type: String, default: '' }, compact: Boolean })
const emit = defineEmits(['select','open'])
const handle = ref(null), active = ref(false)
let gesture = null, suppressClick = false
const spring = createSpring(0, x => { if(handle.value)handle.value.style.transform=`translateX(${x}px)` })
function step(direction) { const index=props.periods.indexOf(props.modelValue);if(index<0)return;const next=props.periods[index+direction];if(next && next!==props.modelValue)emit('select',next) }
function start(event) {
  if(event.button!==0)return
  suppressClick=false
  const x=spring.value;spring.jump(x);active.value=true;event.currentTarget.focus({preventScroll:true})
  gesture={pointer:event.pointerId,x:event.clientX,origin:x,delta:0,moved:false,el:event.currentTarget}
  event.currentTarget.setPointerCapture(event.pointerId)
}
function move(event) { if(!gesture)return;gesture.delta=event.clientX-gesture.x;gesture.moved ||= Math.abs(gesture.delta)>4;spring.jump(gesture.origin+gesture.delta*.3) }
function finish(cancelled=false) {
  if(!gesture)return
  const g=gesture;gesture=null;active.value=false
  try{g.el.releasePointerCapture(g.pointer)}catch{ /* capture can end with the browser gesture */ }
  suppressClick=cancelled||g.moved
  if(!cancelled&&g.moved&&Math.abs(g.delta)>36)step(g.delta>0?1:-1)
  spring.to(0)
}
// Open on click, after the popover's outside-pointer check has completed.
// A completed drag (including a reversal) or cancellation must not open a menu.
function click(event){if(event.detail===0||!suppressClick)emit('open');suppressClick=false}
function keydown(event) {
  if(event.key==='Escape'&&gesture){event.preventDefault();event.stopPropagation();finish(true)}
  else if(event.key==='ArrowLeft'){event.preventDefault();step(1)}
  else if(event.key==='ArrowRight'){event.preventDefault();step(-1)}
}
function blur(){finish(true)}
onMounted(()=>window.addEventListener('blur',blur))
onBeforeUnmount(()=>{finish(true);spring.destroy();window.removeEventListener('blur',blur)})
</script>
<template><button ref="handle" class="month-grip" :class="{compact,active}" type="button" aria-label="左右拖动切换已有月份，点击打开月份选择" aria-haspopup="listbox" @pointerdown="start" @pointermove="move" @pointerup="finish(false)" @pointercancel="finish(true)" @lostpointercapture="gesture && finish(true)" @keydown="keydown" @click="click"><MoveHorizontal :size="16"/><span v-if="!compact">拖动换月</span></button></template>
