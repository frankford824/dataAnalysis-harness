// Browser fixture: no financial data or live requests. Exercise native label
// activation, pointer/click ordering, keyboard opening and cancelled month drags.
import { createApp, h } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { NConfigProvider } from 'naive-ui'
import { useApp } from '../src/store'
import FilterBar from '../src/components/FilterBar.vue'
import PeriodStrip from '../src/components/PeriodStrip.vue'
import { uiTheme } from '../src/design/tokens.js'
import '../src/design.css'
import '../src/app.css'
import '../src/design/tokens.css'
import '../src/sketch.css'

const periods = ['2026-08', '2026-07', '2026-06', '2026-05']
const pinia = createPinia()
const Root = { setup() {
  const app = useApp()
  app.navigation = { stores: [], platforms: [], periods }
  app.period = periods[0]
  return () => h('main', { style: 'padding:32px' }, [
    h('h1', '月份选择交互回归'),
    h(FilterBar),
    h('p', { id: 'selected-period', role: 'status' }, app.period),
    h(PeriodStrip, { periods: periods.map(period => ({ period })), modelValue: app.period,
      'onUpdate:modelValue': period => app.pick({ period }) }),
    h('button', { id: 'outside', type: 'button' }, '外部焦点'),
  ])
} }
const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', name: 'board', component: Root }] })
document.documentElement.classList.add('sketch-theme')
createApp({ render: () => h(NConfigProvider, { themeOverrides: uiTheme(false) }, { default: () => h(RouterView) }) })
  .use(pinia).use(router).mount('#app')
