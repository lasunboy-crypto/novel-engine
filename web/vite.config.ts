import path from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 默认 5173(对齐 CLAUDE.md §部署形态);worktree 启动用 `pnpm dev -- --port 5174`。
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
  },
  build: {
    rollupOptions: {
      output: {
        // 把第三方库拆成独立 vendor chunk(v1.1 wave 7):
        // 业务代码改动只重下小的 index 包,大依赖各自独立缓存。
        // 两个大头:markdown 链(react-markdown + unified 生态)、charts(recharts + d3)。
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined
          // 图表(recharts + d3 + victory-vendor),正文不依赖,懒页面才用
          if (
            id.includes('/recharts/') ||
            id.includes('/d3-') ||
            id.includes('/victory-vendor/') ||
            id.includes('/internmap/')
          ) {
            return 'vendor-charts'
          }
          // markdown 渲染栈(react-markdown + remark/rehype/unified/micromark/*ast)
          if (
            id.includes('/react-markdown/') ||
            id.includes('/remark') ||
            id.includes('/rehype') ||
            id.includes('/micromark') ||
            id.includes('/mdast') ||
            id.includes('/hast') ||
            id.includes('/unist') ||
            id.includes('/unified/') ||
            id.includes('/vfile')
          ) {
            return 'vendor-markdown'
          }
          // Radix UI primitives
          if (id.includes('/@radix-ui/')) {
            return 'vendor-radix'
          }
          // 路由 / 数据请求 / 状态
          if (
            id.includes('/react-router') ||
            id.includes('/@remix-run/') ||
            id.includes('/@tanstack/') ||
            id.includes('/zustand/')
          ) {
            return 'vendor-router-data'
          }
          // 表单
          if (
            id.includes('/react-hook-form/') ||
            id.includes('/@hookform/') ||
            id.includes('/zod/')
          ) {
            return 'vendor-forms'
          }
          // 其余第三方(react / react-dom / lucide-react / clsx / tailwind-merge 等)
          return 'vendor'
        },
      },
    },
  },
})
