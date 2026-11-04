import { ref } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'

/* 右边常驻台账助手，左边整个应用在 .dock-main 里自己滚，窗口不滚。浏览器替窗口
 * 记的位置在这里永远是 0，所以左栏滚到哪儿按历史条目的序号自己记。换页期间旧页面
 * 被拆掉时也会触发滚动，那一段不记，免得把要恢复的位置冲掉。 */
const leftPane = () => document.querySelector('.dock-main')
const historyKey = () => window.history.state?.position ?? 0
const leftScroll = new Map()
let settling = false
document.addEventListener('scroll', (e) => {
  if (!settling && e.target === leftPane()) leftScroll.set(historyKey(), { left: e.target.scrollLeft, top: e.target.scrollTop })
}, true)

/* 用 hash 路由：这套服务是内网里一个 uvicorn 进程直接把静态文件挂出去的，
 * 没有反向代理帮忙把任意路径回落到 index.html。history 模式下人刷新一次
 * 就是 404，而对账时刷新页面是很自然的动作。
 */
export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', name: 'board', component: () => import('./views/BoardView.vue') },
    { path: '/deliver', name: 'deliver', component: () => import('./views/DeliverView.vue') },
    { path: '/freight', name: 'freight-review', component: () => import('./views/FreightReview.vue') },
    {
      path: '/store/:id',
      name: 'period',
      component: () => import('./views/PeriodView.vue'),
      props: true,
    },
    {
      path: '/commission',
      component: () => import('./views/CommissionLayout.vue'),
      meta: { commission: true },
      children: [
        { path: '', name: 'commission', component: () => import('./views/CommissionWorkspace.vue') },
        { path: 'reports', name: 'commission-reports', component: () => import('./views/CommissionReports.vue') },
        { path: 'org', name: 'commission-org', component: () => import('./views/CommissionOrgTree.vue') },
      ],
    },
    {
      path: '/commission/legacy',
      name: 'commission-legacy',
      component: () => import('./views/CommissionView.vue'),
    },
    {
      path: '/labor', name: 'labor', component: () => import('./views/LaborView.vue'),
    },
    { path: '/ops', name: 'ops', component: () => import('./views/OpsView.vue') },
    {
      path: '/fees',
      name: 'fees',
      component: () => import('./views/FeesView.vue'),
    },
    {
      path: '/onboard/:sha',
      name: 'onboard',
      component: () => import('./views/OnboardView.vue'),
      props: true,
    },
    { path: '/:rest(.*)', redirect: '/' },
  ],

  /* 往回走就回到原来那个位置。
   *
   * 总览上一百多行店铺账期，翻到底点进一家看完，回来又在最顶上——人得重新找一遍
   * 刚才看到哪儿了，一次两次还行，对一晚上账就是折磨。`savedPosition` 是浏览器
   * 替我们记的，只在返回/前进时有值；新开一页当然还是从头看。
   *
   * 要等页面长回来再滚。每一页的数据都是挂载之后才去取的，路由切回来的那一刻
   * 文档还是个骨架，这时候滚到第 580 像素只会停在顶上——位置记了等于没记。
   */
  scrollBehavior: (to, from, savedPosition) => {
    const pane = leftPane()
    if (to.meta.commission && from.meta.commission && !savedPosition) {
      settling = false
      return false
    }
    if (!pane) {
      settling = false
      return savedPosition || { top: 0 }
    }
    const target = savedPosition ? leftScroll.get(historyKey()) : null
    if (!target) {
      pane.scrollTop = 0
      settling = false
      return false
    }
    return new Promise((resolve) => {
      let waited = 0
      const enough = () =>
        pane.scrollHeight >= target.top + pane.clientHeight
      const tick = () => {
        // 两秒还没长回来就按当前高度滚——多半是这一页的数据变短了（表被撤下、
        // 筛选变了）。宁可停在底部，也不能一直吊着不还给用户。
        if (enough() || waited > 2000) {
          pane.scrollTo(target.left, target.top)
          settling = false
          resolve(false)
        } else {
          waited += 50
          setTimeout(tick, 50)
        }
      }
      tick()
    })
  },
})

router.beforeEach(() => {
  settling = true
})
router.afterEach((to, from, failure) => {
  if (failure) settling = false
})

/** back 里有东西才显示返回按钮，不然点下去会跳出这个应用。
 *
 * 用的是路由器自己往 history.state 里写的 back，不是我们数的步数：数步数会在
 * 用户按浏览器返回键时算错，然后按钮要么该出现时不出现，要么点了跳到别人家。
 */
export const canBack = ref(false)
router.afterEach(() => {
  canBack.value = !!window.history.state?.back
})
