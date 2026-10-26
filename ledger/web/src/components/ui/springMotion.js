import { tokens } from '../../design/tokens.js'

// Pixel/opacity motion only. No ledger amounts are accepted or transformed here.
export function advanceSpring(state, target, elapsed, stiffness, damping) {
  const dt = Math.min(Math.max(elapsed, 0), 1 / 30)
  const steps = Math.max(1, Math.ceil(dt * 120)), step = dt / steps
  let { value, velocity } = state
  for (let i = 0; i < steps; i++) {
    velocity += ((target - value) * stiffness - velocity * damping) * step
    value += velocity * step
  }
  return Math.abs(velocity) < .04 && Math.abs(target - value) < .04
    ? { value: target, velocity: 0 } : { value, velocity }
}

export function createSpring(initial, draw, options = {}) {
  const stiffness = options.stiffness ?? tokens.motion['paper-stiffness']
  const damping = options.damping ?? tokens.motion['paper-damping']
  const schedule = options.schedule ?? (callback => requestAnimationFrame(callback))
  const cancel = options.cancel ?? (id => cancelAnimationFrame(id))
  const reduced = options.reduced ?? (() => window.matchMedia('(prefers-reduced-motion: reduce)').matches)
  let state = { value: initial, velocity: 0 }, target = initial, frame = 0, previous = 0
  function stop() { if (frame) cancel(frame); frame = 0; previous = 0 }
  function tick(time) {
    frame = 0
    state = advanceSpring(state, target, previous ? (time - previous) / 1000 : 1 / 60, stiffness, damping)
    previous = time; draw(state.value)
    if (state.value !== target || state.velocity !== 0) frame = schedule(tick)
    else previous = 0
  }
  return {
    get value() { return state.value },
    get velocity() { return state.velocity },
    jump(value) { stop(); target = value; state = { value, velocity: 0 }; draw(value) },
    to(value, velocity) {
      target = value
      if (velocity !== undefined) state.velocity = velocity
      if (reduced()) { this.jump(value); return }
      if (!frame) { previous = 0; frame = schedule(tick) }
    },
    stop,
    destroy: stop,
  }
}
