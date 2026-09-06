import { defineConfig } from 'vite'
import { resolve } from 'node:path'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// MPA 三入口：
//   /register -> register.html（部署到游戏服 80 端口 web_server.py）
//   /import   -> import.html（外服账号迁移页，同 80 端口 web_server.py）
//   /admin    -> admin.html（部署到管理端 FastAPI 独立进程）
// 开发期代理：/api -> 游戏服(注册 API)；/admin/api -> 管理端 FastAPI
export default defineConfig({
  resolve: {
    alias: { '@': resolve(__dirname, 'src') },
  },
  plugins: [
    vue(),
    AutoImport({
      imports: ['vue', 'vue-router', 'pinia'],
      // 样式改为 main.ts 全量引入（必须在 hng-theme.scss 之前），
      // 否则按需注入的组件样式会把 :root 主题变量覆盖回白色
      resolvers: [ElementPlusResolver({ importStyle: false })],
      dts: 'auto-imports.d.ts',
    }),
    Components({
      resolvers: [ElementPlusResolver({ importStyle: false })],
      dts: 'components.d.ts',
    }),
  ],
  build: {
    rollupOptions: {
      input: {
        register: resolve(__dirname, 'register.html'),
        import: resolve(__dirname, 'import.html'),
        admin: resolve(__dirname, 'admin.html'),
      },
    },
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:80',
      '/admin/api': {
        target: 'http://127.0.0.1:8080',
        rewrite: (p) => p.replace(/^\/admin/, ''),
      },
    },
  },
})
