import {
  RouterProvider,
  createBrowserRouter,
  useRouteError,
} from 'react-router-dom'
import { AppShell } from '@/components/AppShell'
import HomePage from '@/pages/HomePage'
import WorkspacesPage from '@/pages/WorkspacesPage'
import WorkspaceDetailPage from '@/pages/WorkspaceDetailPage'
import NewBookPage from '@/pages/NewBookPage'
import OutlinePage from '@/pages/OutlinePage'
import VolumePlanPage from '@/pages/VolumePlanPage'
import ChaptersBoardPage from '@/pages/ChaptersBoardPage'
import ChapterCardReviewPage from '@/pages/ChapterCardReviewPage'
import ChapterEditorPage from '@/pages/ChapterEditorPage'
import RunControlPage from '@/pages/RunControlPage'
import ExportPage from '@/pages/ExportPage'
import CharactersPage from '@/pages/CharactersPage'
import PlotThreadsPage from '@/pages/PlotThreadsPage'
import ConfigPage from '@/pages/ConfigPage'
import SettingsPage from '@/pages/SettingsPage'
import MetricsPage from '@/pages/MetricsPage'
import LogsPage from '@/pages/LogsPage'
import NotFoundPage from '@/pages/NotFoundPage'

// P4-F-06:稳定的 ErrorBoundary 触发探针。`/__throw` 路由在 render 时同步抛
// 一个固定 Error,供前端走查与后续故障排查"验证 ErrorBoundary 仍然挂着"。
// 不出现在 nav,普通用户不会访问;后端 frontend_error event 也可借此 round-trip
// 验证。命名前缀 `__` 与 `__resetErrorReporterForTests` 同源,表"工程内部接口"。
function ErrorBoundaryProbe(): never {
  throw new Error('ErrorBoundary 手动触发(/__throw 探针)')
}

// P4-F-06:react-router v6 data router 默认会在路由层捕获 render 错误并渲染
// 一个内置的 default error UI,使得外层 React `<ErrorBoundary>` 拿不到错误、
// 上报通道也走不通。`RouterErrorEscalator` 作为根 `errorElement` 把 router
// 捕获的错误用 `useRouteError` 取出来后**重新抛出**,让错误穿透 router 内部
// boundary 冒泡到 main.tsx 顶层的 `<ErrorBoundary>` — 既保留 SPA 单一兜底页
// 体验,也确保 frontend_error 事件统一从 `reportReactRenderError` 上报。
function RouterErrorEscalator(): never {
  const error = useRouteError()
  if (error instanceof Error) throw error
  throw new Error(typeof error === 'string' ? error : JSON.stringify(error))
}

// data router(`createBrowserRouter`):P1-F-05 起替代 `<BrowserRouter>` JSX 写法,
// 让 `useBlocker` 在 `UnsavedChangesGuard` 内可用(v6 硬限制:`useBlocker` 仅
// 支持 data router)。Layout Route 模式不变,仅从 JSX 改为对象数组形态。
const router = createBrowserRouter([
  {
    element: <AppShell />,
    errorElement: <RouterErrorEscalator />,
    children: [
      { index: true, element: <HomePage /> },
      { path: 'workspaces', element: <WorkspacesPage /> },
      { path: 'workspaces/new', element: <NewBookPage /> },
      { path: 'workspaces/:id', element: <WorkspaceDetailPage /> },
      {
        path: 'w/:workspaceId/outline',
        element: <OutlinePage />,
      },
      {
        path: 'w/:workspaceId/volumes/:volumeId',
        element: <VolumePlanPage />,
      },
      {
        path: 'w/:workspaceId/cards/:chapterId/review',
        element: <ChapterCardReviewPage />,
      },
      {
        path: 'w/:workspaceId/run',
        element: <RunControlPage />,
      },
      {
        path: 'w/:workspaceId/export',
        element: <ExportPage />,
      },
      { path: 'chapters', element: <ChaptersBoardPage /> },
      { path: 'chapters/:id', element: <ChapterEditorPage /> },
      { path: 'characters', element: <CharactersPage /> },
      { path: 'plot-threads', element: <PlotThreadsPage /> },
      { path: 'config', element: <ConfigPage /> },
      { path: 'settings', element: <SettingsPage /> },
      { path: 'metrics', element: <MetricsPage /> },
      { path: 'logs', element: <LogsPage /> },
      { path: 'logs/trace/:traceId', element: <LogsPage /> },
      { path: '__throw', element: <ErrorBoundaryProbe /> },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
])

export default function App() {
  // v1.1 wave 7:opt-in `v7_startTransition`(导航 state 更新走 React.startTransition,
  // 非阻塞)消除 dev console 30+ 条 future-flag 警告(走查发现的噪声)。这是 v7
  // 稳定路径的 RouterProvider 级 prop(非 createBrowserRouter future);preview 走查
  // 确认导航不破。`v7_relativeSplatPath` 未触发警告(本应用 splat 路由无相对导航)
  // → 不提前 opt-in,保守不动 splat 解析行为。
  return <RouterProvider router={router} future={{ v7_startTransition: true }} />
}
