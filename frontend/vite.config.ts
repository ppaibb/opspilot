import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  build: {
    rollupOptions: {
      output: {
        // 大依赖拆 vendor（rolldown 仅支持函数形式）：antd/图表独立缓存，
        // 业务代码改动不再击穿长缓存，首屏也可并行加载
        manualChunks(id: string) {
          if (!id.includes('node_modules')) return undefined
          if (id.includes('monaco')) return 'monaco'
          if (id.includes('echarts') || id.includes('zrender')) return 'charts'
          if (id.includes('antd') || id.includes('@ant-design') || /[/\\]rc-[\w-]+[/\\]/.test(id)) return 'antd'
          return 'vendor'
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      // 终端 WebSocket 必须单独配置 ws: true，走 /api/terminal/ws/* 路径
      '/api/terminal/ws': {
        target: 'ws://127.0.0.1:8001',
        changeOrigin: true,
        ws: true,
      },
      '/api': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
        ws: true, // K8s Pod 日志/Exec 等 WebSocket 也走 /api 前缀，必须允许 upgrade
      },
    },
  },
})
