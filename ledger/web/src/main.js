import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import './design.css'
import './app.css'
import './ledger-ui.css'
import './workflow.css'
import { router } from './router'
import { accessibleTabs } from './components/ui/accessibleTabs'

// 部署了新版本，而这一页是旧版本打开的。
//
// 每个视图是单独一个分片，文件名带内容哈希。新版本一上去，旧文件名就没了，于是
// 分片加载失败时显示重试入口，保留当前页面，避免自动刷新丢失未保存的编辑。这种「点了没反应」最招人烦，而且看不出是版本问题。
window.addEventListener('vite:preloadError', event => { event.preventDefault();window.dispatchEvent(new Event('ledger:page-load-error')) })

createApp(App).use(createPinia()).use(router).directive('ledger-tabs',accessibleTabs).mount('#app')
