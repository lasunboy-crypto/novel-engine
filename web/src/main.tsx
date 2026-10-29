import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import './index.css'
import App from './App.tsx'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { installGlobalErrorReporter } from '@/lib/error-reporter'

// 全局 QueryClient(P1-F-07)。
// - staleTime 30s:WorkspacesPage 等列表页 mount 30s 内命中缓存,避免切路由频繁 refetch
//   (P1 单机不痛,但属于通用陷阱;cf. 维护人轻提示)
// - retry 1:网络抖动 1 次容错;失败 toast ~1s 内出现而非默认 ~7s,
//   匹配 P1 单机本地后端"连不上就是连不上"语义
// - mutations 默认 retry 0 不改:状态变更自动重试可能放大副作用
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
    },
  },
})

// P4-F-06:启动时一次性注册 window.onerror + unhandledrejection 监听,捕获
// React ErrorBoundary 边界外的同步 / 异步异常(浏览器扩展冲突 / 第三方脚本 /
// Promise reject 等)+ 上报 POST /api/v1/system/frontend-errors。
// `installGlobalErrorReporter` 内置 idempotent 守卫,HMR 重跑不会多挂监听。
installGlobalErrorReporter()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ErrorBoundary>
        <App />
      </ErrorBoundary>
    </QueryClientProvider>
  </StrictMode>,
)
