/**
 * 活动工作区 zustand store(P1-F-10)。
 *
 * P1-F-07 留下的跨任务 marker:推算"当前活动工作区"的逻辑散落在
 * `api/workspaces.ts:activeWorkspaceFromList`,P1-F-10 起所有页面都需要
 * 读"当前活动工作区"(WorkspacesPage `*` 标识 / WorkspaceDetailPage 看板 /
 * P1-F-11 ChaptersBoardPage 跨页跳转携带 X-Workspace-Id)。本任务装入
 * zustand 后把该推算迁到 selector,避免散落多份逻辑。
 *
 * ## store 状态最小化
 *
 * 只存 `activeWorkspaceId: string | null`(用户主动选定的工作区 ID),不存
 * 整个 `WorkspaceOut` 对象——避免与 react-query 缓存重复持有同一份 server
 * state。组件读 active workspace 时调 `useActiveWorkspace(workspaces)` 把
 * 列表与 store ID 组合算出对应的 `WorkspaceOut`。
 *
 * ## selector 推算优先级
 *
 * 1. **store `activeWorkspaceId` 命中且工作区仍存在**:返回该工作区。覆盖
 *    "用户从详情页返回列表,期望仍看见自己刚浏览的那个为当前活动"等场景。
 * 2. **store 为 null 或命中工作区已被 purge**:fallback 到 `last_active_at`
 *    字典序最大者(P1-F-07 决策 2 推算)——保证首次访问 / 后端权威切换后
 *    UI 与服务端 `_active` 软链一致。
 *
 * ISO 8601 UTC 字符串字典序与时间序一致(对齐 CLAUDE.md §命名约定)。
 *
 * ## 何时写入
 *
 * - WorkspaceDetailPage "查看所有章节"按钮:`setActiveWorkspaceId(id)` 然后
 *   navigate `/chapters`,让 ChaptersBoardPage(P1-F-11)读到对应工作区
 * - 工作区被 purge 后,store 中的 ID 自然失效,selector 兜回 last_active_at
 *   max,无需显式清理(失效检查在 selector 内)
 *
 * ## 不放进 store 的东西
 *
 * - 工作区列表数据本身:react-query 已管,store 持有副本会重复
 * - active task / chapter editor dirty:留给后续任务自己开 store
 * - persist 中间件:P1 单机本地不需要跨 tab / 跨刷新一致(刷新后从首条
 *   活动工作区 fallback 即可),P2 多人远程模式再评估
 */

import { useMemo } from "react";
import { create } from "zustand";

import type { WorkspaceOut } from "@/api/workspaces";

interface ActiveWorkspaceState {
  activeWorkspaceId: string | null;
  setActiveWorkspaceId: (id: string | null) => void;
}

export const useActiveWorkspaceStore = create<ActiveWorkspaceState>((set) => ({
  activeWorkspaceId: null,
  setActiveWorkspaceId: (id) => set({ activeWorkspaceId: id }),
}));

/**
 * 纯函数:从工作区列表与 store 中的 activeWorkspaceId 推算"当前活动工作区"。
 *
 * 决策树见模块顶部 docstring "selector 推算优先级"。空列表返回 null。
 */
export function selectActiveWorkspace(
  workspaces: readonly WorkspaceOut[] | undefined,
  storedActiveId: string | null,
): WorkspaceOut | null {
  if (!workspaces || workspaces.length === 0) return null;
  if (storedActiveId !== null) {
    const hit = workspaces.find((w) => w.workspace_id === storedActiveId);
    if (hit) return hit;
  }
  let active = workspaces[0];
  for (let i = 1; i < workspaces.length; i += 1) {
    if (workspaces[i].last_active_at > active.last_active_at) {
      active = workspaces[i];
    }
  }
  return active;
}

/**
 * 组件 hook:`useActiveWorkspace(workspaces)` 取最终的"当前活动工作区"。
 *
 * 内部读 store + 调 `selectActiveWorkspace`,`useMemo` 缓存避免每次 render
 * 都遍历列表。组件改用本 hook 后无需再 import `selectActiveWorkspace`。
 */
export function useActiveWorkspace(
  workspaces: readonly WorkspaceOut[] | undefined,
): WorkspaceOut | null {
  const storedActiveId = useActiveWorkspaceStore((s) => s.activeWorkspaceId);
  return useMemo(
    () => selectActiveWorkspace(workspaces, storedActiveId),
    [workspaces, storedActiveId],
  );
}
