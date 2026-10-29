/**
 * AppShell(全局 layout:header + nav + 侧栏 + main + Toaster +
 * UnsavedChangesGuard)。
 *
 * P1-P5 历史:静态 nav 链接 8 条 + "大纲"动态二级链接(P5-F-05);P6-F-02
 * 起补两块,P6-F-03 起再加 "导出" 链接(同 outline / 运行控制模式):
 *
 * 1. **"运行控制"动态二级链接**(对齐 FRONTEND_SPEC §4.10 + plan §P6-F-02
 *    "AppShell nav 加'运行控制'链接 → `/w/:workspaceId/run`"):当前工作区
 *    上下文存在时跳 `/w/:id/run`,否则跳 `/workspaces`(沿用"大纲"链接同模式)。
 * 2. **"导出"动态二级链接**(P6-F-03,对齐 FRONTEND_SPEC §4.14):当前工作区
 *    上下文存在时跳 `/w/:id/export`,否则跳 `/workspaces`。
 * 3. **task done 全局兜底**(对齐 P5 候选 4 摊入):订阅 active task store 中
 *    所有 `chapterTaskMap` 值的 `task.{taskId}.progress` 频道,接到 `done` 事件
 *    时 invalidate `["chapters", workspaceId]` / `["chapter-card",
 *    workspaceId, chapterId]` / `["chapter-versions", workspaceId, chapterId]`,
 *    解决"task done 后 UI 不自动刷新"的隐式 bug。**仅订阅,不持有 state** —
 *    避免与既有 page 层 `useTaskProgress` 双源冲突。
 *
 * task done 兜底为何放全局:WS 推送 done 事件时,用户可能已离开触发页面
 * (例:从 ChapterCardReviewPage `:write` 后切到 ChaptersBoardPage 看进度),
 * page 层 `useTaskProgress` 已 unmount,event 丢失。全局兜底永远挂着,无论
 * 用户在哪个 page 都能 invalidate 触发刷新。
 */

import { useEffect } from "react";
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { cn } from '@/lib/utils'
import { AlertBadge } from '@/components/AlertBadge'
import { Toaster } from '@/components/Toaster'
import { UnsavedChangesGuard } from '@/components/UnsavedChangesGuard'
import { useActiveWorkspace, useActiveWorkspaceStore } from '@/store/activeWorkspace'
import {
  useActiveTaskStore,
} from '@/store/activeTask'
import { useWorkspacesQuery } from '@/api/workspaces'
import { getWsClient } from '@/ws/client'

interface StaticLink {
  to: string
  label: string
  end: boolean
}

const STATIC_NAV_LINKS: readonly StaticLink[] = [
  { to: '/', label: '主页', end: true },
  { to: '/workspaces', label: '工作区', end: false },
  { to: '/chapters', label: '章节', end: false },
  { to: '/characters', label: '角色', end: false },
  { to: '/plot-threads', label: '伏笔', end: false },
  { to: '/metrics', label: '指标', end: false },
  { to: '/logs', label: '日志', end: false },
  { to: '/config', label: '配置', end: false },
  { to: '/settings', label: '设置', end: false },
]

// 从当前 pathname 推算"当前工作区 ID":
//   - `/workspaces/:id` / `/workspaces/:id/...`
//   - `/w/:workspaceId/...`(P5-F-04 起卷规划 / P5-F-05 起大纲 / P6-F-02 起运行控制)
// 都无匹配时返 null(fallback 到 zustand store)。
function workspaceIdFromPath(pathname: string): string | null {
  let m = /^\/workspaces\/([^/]+)/.exec(pathname)
  if (m && m[1] !== 'new') return m[1]
  m = /^\/w\/([^/]+)/.exec(pathname)
  if (m) return m[1]
  return null
}

export function AppShell() {
  const location = useLocation()
  const storeId = useActiveWorkspaceStore((s) => s.activeWorkspaceId)

  // v1.0.x Commit B(2026-05-21,扩范围 1b)— 修 frontend store 不持久化 bug。
  // 根因:[activeWorkspace.ts:38](src/store/activeWorkspace.ts:38) P1 设计决策
  // zustand 不接 persist → 刷新浏览器后 storeId=null → 老的
  // `workspaceIdFromPath(pathname) ?? storeId` 返 null → 顶部 nav '导出/大纲/
  // 运行控制' href 全 fallback `/workspaces` → 用户实测"点了像没反应"
  // (实际是跳回工作区列表)。
  //
  // 修法:加第三档 fallback 用 useActiveWorkspace 内置 selector,从工作区
  // 列表 last_active_at 最大者推算。优先级 URL > store > workspaceList[last_active_at max]。
  // P1 设计意图"不持久化"保留(刷新后从首条恢复 ≈ 后端 _active 软链一致)。
  const workspacesQuery = useWorkspacesQuery()
  const activeWorkspace = useActiveWorkspace(workspacesQuery.data)
  const currentWorkspaceId =
    workspaceIdFromPath(location.pathname) ??
    storeId ??
    activeWorkspace?.workspace_id ??
    null

  // "大纲"链接(P5-F-05):有工作区上下文时直接跳 `/w/:id/outline`;
  // 无上下文则跳 `/workspaces`(让用户先挑工作区)。
  const outlineHref = currentWorkspaceId
    ? `/w/${currentWorkspaceId}/outline`
    : '/workspaces'

  // "运行控制"链接(P6-F-02):同 outline 模式。
  const runHref = currentWorkspaceId
    ? `/w/${currentWorkspaceId}/run`
    : '/workspaces'

  // "导出"链接(P6-F-03):同 outline / 运行控制模式。
  const exportHref = currentWorkspaceId
    ? `/w/${currentWorkspaceId}/export`
    : '/workspaces'

  // P6-F-02:全局 task done 兜底订阅 — 详组件顶部 docstring。
  useGlobalTaskDoneFallback(currentWorkspaceId)

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <header className="border-b border-border">
        <div className="flex h-14 items-center gap-6 px-6">
          <span className="font-semibold">长篇小说创作引擎</span>
          <nav className="flex items-center gap-1">
            {STATIC_NAV_LINKS.map((link) => (
              <NavLink
                key={link.to}
                to={link.to}
                end={link.end}
                className={({ isActive }) =>
                  cn(
                    'rounded px-3 py-1.5 text-sm transition-colors',
                    isActive
                      ? 'bg-secondary text-secondary-foreground'
                      : 'text-muted-foreground hover:text-foreground',
                  )
                }
              >
                {link.label}
              </NavLink>
            ))}
            <NavLink
              to={outlineHref}
              className={() => {
                const isOnOutline = /^\/w\/[^/]+\/outline$/.test(
                  location.pathname,
                )
                return cn(
                  'rounded px-3 py-1.5 text-sm transition-colors',
                  isOnOutline
                    ? 'bg-secondary text-secondary-foreground'
                    : 'text-muted-foreground hover:text-foreground',
                )
              }}
              data-testid="nav-outline"
              title={
                currentWorkspaceId
                  ? `工作区 ${currentWorkspaceId} 的大纲`
                  : '先选工作区再编辑大纲'
              }
            >
              大纲
            </NavLink>
            <NavLink
              to={runHref}
              className={() => {
                const isOnRun = /^\/w\/[^/]+\/run$/.test(
                  location.pathname,
                )
                return cn(
                  'rounded px-3 py-1.5 text-sm transition-colors',
                  isOnRun
                    ? 'bg-secondary text-secondary-foreground'
                    : 'text-muted-foreground hover:text-foreground',
                )
              }}
              data-testid="nav-run-control"
              title={
                currentWorkspaceId
                  ? `工作区 ${currentWorkspaceId} 的运行控制`
                  : '先选工作区再打开运行控制'
              }
            >
              运行控制
            </NavLink>
            <NavLink
              to={exportHref}
              className={() => {
                const isOnExport = /^\/w\/[^/]+\/export$/.test(
                  location.pathname,
                )
                return cn(
                  'rounded px-3 py-1.5 text-sm transition-colors',
                  isOnExport
                    ? 'bg-secondary text-secondary-foreground'
                    : 'text-muted-foreground hover:text-foreground',
                )
              }}
              data-testid="nav-export"
              title={
                currentWorkspaceId
                  ? `工作区 ${currentWorkspaceId} 的导出`
                  : '先选工作区再打开导出页'
              }
            >
              导出
            </NavLink>
          </nav>
          <div className="ml-auto">
            <AlertBadge />
          </div>
        </div>
      </header>
      <div className="flex flex-1">
        <aside className="w-56 border-r border-border p-4">
          <p className="text-xs text-muted-foreground">侧栏占位</p>
          <p className="mt-1 text-xs text-muted-foreground">
            P1-F-NN 起填工作区切换 / 章节快速跳转。
          </p>
        </aside>
        <main className="flex-1 p-6">
          <Outlet />
        </main>
      </div>
      <Toaster />
      <UnsavedChangesGuard />
    </div>
  )
}

/**
 * 全局 task done 兜底订阅(P6-F-02 摊入 P5 候选 4)。
 *
 * 订阅 `chapterTaskMap` 中所有当前已知 task_id 的 `task.{id}.progress`
 * 频道,event=`done` 时 invalidate 当前 workspace 的 chapter list /
 * chapter-card / chapter-versions / chapter-version 全部 query keys。
 *
 * **不持有 state**:仅触发 invalidate,具体数据由 react-query 重 fetch。
 * 与 page 层既有 `useTaskProgress`(NewBookPage / ChapterCardReviewPage)
 * 共存不冲突 — page 层管 UI 子状态,全局兜底只管 query 刷新。
 *
 * **workspace_id 缺失短路**:无活动工作区时无法 invalidate(query key 含
 * workspace_id),订阅但不触发 invalidate(下次有工作区时再回流);也可
 * 选择不订阅,选订阅是为了不丢日志态 timeline 序。
 */
function useGlobalTaskDoneFallback(
  currentWorkspaceId: string | null,
): void {
  const chapterTaskMap = useActiveTaskStore((s) => s.chapterTaskMap)
  const queryClient = useQueryClient()

  useEffect(() => {
    const entries = Object.entries(chapterTaskMap)
    if (entries.length === 0) return

    const client = getWsClient()
    const unsubscribers = entries.map(([chapterId, taskId]) => {
      const channel = `task.${taskId}.progress`
      return client.subscribe<{ stage?: string; progress?: number }>(
        channel,
        (frame) => {
          if (frame.event !== 'done') return
          if (!currentWorkspaceId) return
          // task done → 刷新当前章节 + 列表(章节 status / latest_version_id 可能改了)
          void queryClient.invalidateQueries({
            queryKey: ['chapters', currentWorkspaceId],
          })
          void queryClient.invalidateQueries({
            queryKey: ['chapter-card', currentWorkspaceId, chapterId],
          })
          void queryClient.invalidateQueries({
            queryKey: ['chapter-versions', currentWorkspaceId, chapterId],
          })
          void queryClient.invalidateQueries({
            queryKey: ['chapter-version', currentWorkspaceId, chapterId],
          })
        },
      )
    })

    return () => {
      for (const unsub of unsubscribers) unsub()
    }
  }, [chapterTaskMap, currentWorkspaceId, queryClient])
}
