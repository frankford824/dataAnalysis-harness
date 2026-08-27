import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'
import Components from 'unplugin-vue-components/vite'
import { NaiveUiResolver } from 'unplugin-vue-components/resolvers'

const apiProxy = process.env.LEDGER_API_PROXY || 'http://127.0.0.1:8000'

// 产物直接落进后端的 static 目录，由 FastAPI 挂载出去。分两个服务部署会多一个
// 要一起启停的东西，而这套系统是一个人维护的。
export default defineConfig({
  plugins: [
    vue(),
    Components({ resolvers: [NaiveUiResolver()], dts: false }),
  ],
  base: '/static/',
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    outDir: fileURLToPath(new URL('../ledger/static', import.meta.url)),
    emptyOutDir: true,
    // 生产不公开源码映射。确需排错时由CI单独产出 hidden map，不跟应用包一起发布。
    sourcemap: process.env.BUILD_SOURCEMAP === '1' ? 'hidden' : false,
  },
  server: {
    proxy: {
      '/api': apiProxy,
    },
  },
})
