/**
 * 活动任务 zustand store(P1-F-13 起)。
 *
 * 章节维度的"最近一次任务"映射:`chapterId → taskId`。
 *
 * ## 写入路径(P1-F-14 起,本任务范围内不写)
 *
 * `:write` / `:rewrite` / `:revise` 端点跑完返回 `TaskInfo`,P1-F-14 工具栏
 * mutation 拿到 `task_id` 后调 `setChapterTaskId(chapterId, taskId)` 写入
 * 本 store,供 `ReviewHistoryPanel`(本任务实质化)读取展示审稿摘要。
 *
 * P1-F-13 只暴露 `useChapterTaskId` 读取 hook + `setChapterTaskId` setter
 * (P1-F-14 触发动作时调用)。
 *
 * ## 不持久化
 *
 * 不接 `persist` 中间件 —— 本 store 是"本次浏览会话内章节→最近任务"的临时
 * UI 映射,页面刷新重建即可,无需跨刷新存活。v1.1 E3 起 task_registry 后端
 * 已落 SQLite(任务状态跨重启持久 + 中断态 `interrupted`),任务状态的真源在
 * 后端 `GET /tasks/{id}`;本 store 仅做"当前会话点过哪个任务"的轻量索引,
 * 不复制后端真源,故仍不持久化。
 *
 * ## key 选 chapterId 而非 (workspaceId, chapterId)
 *
 * P1 单工作区切换是显式动作(WorkspacesPage `:activate`),切换工作区时章节
 * 上下文 reset 是预期行为;前端 `chapter_id` 在工作区内已唯一,跨工作区
 * `chapter_id` 命名一致是边缘场景(P1 同步执行也不会同时跑两个工作区的同
 * chapter_id 任务)。简化 key 减少 store 复杂度。
 */

import { create } from "zustand";

interface ActiveTaskState {
  /** chapterId → 最近一次 :write / :rewrite / :revise 的 task_id */
  chapterTaskMap: Record<string, string>;
  setChapterTaskId: (chapterId: string, taskId: string) => void;
  clearChapterTaskId: (chapterId: string) => void;
}

export const useActiveTaskStore = create<ActiveTaskState>((set) => ({
  chapterTaskMap: {},
  setChapterTaskId: (chapterId, taskId) =>
    set((state) => ({
      chapterTaskMap: { ...state.chapterTaskMap, [chapterId]: taskId },
    })),
  clearChapterTaskId: (chapterId) =>
    set((state) => {
      if (!(chapterId in state.chapterTaskMap)) return state;
      const next = { ...state.chapterTaskMap };
      delete next[chapterId];
      return { chapterTaskMap: next };
    }),
}));

/**
 * 读 store 中本章节最近一次 task_id。无记录返回 null。
 *
 * 调用方(`ReviewHistoryPanel`)在 null 时显示"暂无审稿历史"hint,非 null
 * 时调 `useTaskInfoQuery(taskId)` 拉 TaskInfo 渲染 review_summary。
 */
export function useChapterTaskId(
  chapterId: string | null | undefined,
): string | null {
  return useActiveTaskStore((s) => {
    if (typeof chapterId !== "string" || chapterId.length === 0) return null;
    return s.chapterTaskMap[chapterId] ?? null;
  });
}
