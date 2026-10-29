/**
 * 任务 API hooks(P1-F-13 起)。
 *
 * 已含 hook:
 * - `useTaskInfoQuery(taskId)` → `GET /api/v1/tasks/{task_id}`
 *
 * ## 类型化 result(P1-B-26 落地)
 *
 * `routes/tasks.py` 的 `response_model` 仍是基类 `SuccessEnvelope[TaskInfo]`
 * (基类 `result: dict | None`),P1-B-26 只升级了 `:write` / `:rewrite` /
 * `:revise` 三条动作端点的 `response_model` 到子类。`GET /tasks/{task_id}`
 * 返回的本体是同一份 `TaskInfo` 实例,只是 OpenAPI 声明走基类——子类字段
 * (`type` Literal + `result` 类型化)在运行时**仍然存在**(子类多态保证),
 * 前端按 `task.type` 字面 narrowing 后即可类型安全 cast 到子类。
 *
 * 见 `web/src/components/ReviewHistoryPanel.tsx` 的 `narrowChapterWriteTask`
 * 工具函数。
 *
 * ## queryKey 命名空间
 *
 * - 单任务:`["task", taskId]`
 *
 * v1.1 E3 起 task_registry 落全局 `_tasks.db` SQLite,**任务状态跨服务器重启
 * 持久**;重启前残留的进行中任务被启动扫描翻 `interrupted`。404 `TASK_NOT_FOUND`
 * 现在只表示"任务从未登记 / 超容量上限淘汰",不再因重启产生。前端不做 retry。
 *
 * ## `enabled` 短路
 *
 * `taskId` null/undefined/空 → `enabled=false`,query 不发(任务尚未触发,
 * P1-F-14 才会写 task_id 到 zustand)。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiGet, apiPost } from "@/api/client";
import type { components } from "@/api/types";

export type TaskInfo = components["schemas"]["TaskInfo"];
export type TaskInfoChapterWrite =
  components["schemas"]["TaskInfoChapterWrite"];
export type TaskInfoChapterRevise =
  components["schemas"]["TaskInfoChapterRevise"];
export type WriteResultSummary = components["schemas"]["WriteResultSummary"];
export type ReviewSummaryEntry = components["schemas"]["ReviewSummaryEntry"];
export type ReviewIssueOut = components["schemas"]["ReviewIssueOut"];

export const TASK_QUERY_KEY = (taskId: string) => ["task", taskId] as const;

/**
 * 读单个任务状态(对应 `GET /api/v1/tasks/{task_id}`)。
 *
 * `taskId` 为 null/undefined/空字符串 → `enabled=false`。
 *
 * 错误码:
 * - 404 `TASK_NOT_FOUND` → 任务从未登记或超容量上限被淘汰
 *
 * v1.1 E3 起 task_registry 落 SQLite,任务状态跨重启持久(含 `interrupted`
 * 中断态);章节审稿历史另走 `review_results` 表真源(`ReviewHistoryPanel`
 * B-01 hot-fix 起已不依赖本 query)。
 */
export function useTaskInfoQuery(taskId: string | null | undefined) {
  const enabled = typeof taskId === "string" && taskId.length > 0;
  return useQuery<TaskInfo>({
    queryKey: TASK_QUERY_KEY(taskId ?? ""),
    queryFn: () =>
      apiGet<TaskInfo>(`/tasks/${encodeURIComponent(taskId as string)}`),
    enabled,
  });
}

/**
 * 取消正在跑的任务(对应 `POST /api/v1/tasks/{task_id}:cancel`,v1.1 w4-s6 = J1)。
 *
 * **尽力中断语义**:后端置 `cancel_requested` + 向 in-flight worker 发取消信号,
 * worker 的 LLM 调用在下一个 chunk 间(openai_compatible 真流式)/ 下一次调用前
 * (claude/ollama 单次阻塞)中断 → 翻 `status="cancelled"`。**非瞬时**:本 mutation
 * 立即返回的 TaskInfo 此刻通常仍是 `running`;真正终态以 WS `task.{id}.progress`
 * 的 `cancelled` 事件 / 轮询 `GET /tasks/{id}` 为准。
 *
 * `onSuccess` invalidate `["task", taskId]` 让任一 `useTaskInfoQuery` 重新拉最新
 * 状态。已终态任务后端幂等放过(返回原状态);未命中返 404 `TASK_NOT_FOUND`。
 *
 * **J2.1(w4-s7)落点**:取消按钮 UI 进 `useAsyncTaskState` wrapper 的 `cancel`
 * API surface,调用本 mutation(J1 只出 API client,UI 推 J2.1,详
 * `docs/plan/v1_1_candidates.md` §J2.1 衔接段)。
 */
export function useCancelTaskMutation() {
  const queryClient = useQueryClient();
  return useMutation<TaskInfo, Error, string>({
    mutationFn: (taskId) =>
      apiPost<TaskInfo>(`/tasks/${encodeURIComponent(taskId)}:cancel`),
    onSuccess: (_data, taskId) => {
      queryClient.invalidateQueries({ queryKey: TASK_QUERY_KEY(taskId) });
    },
  });
}
