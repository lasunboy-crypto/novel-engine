/**
 * 伏笔(PlotThread)API hooks(P2-F-05 / P2-F-06)。
 *
 * - `useThreadListQuery(workspaceId, { status?, importance? })` →
 *   `GET /api/v1/threads`(P2-B-15);query 参数透传后端 `?status=` / `?importance=`
 * - `useThreadDetailQuery(workspaceId, threadId)` → `GET /threads/{id}`
 * - `useCreateThreadMutation()` → `POST /api/v1/threads`(P2-F-06)
 * - `useUpdateThreadMutation()` → `PUT /threads/{id}`(P2-F-06;PUT body 全量
 *   `PlotThread`,见 P2-B-15 沿用 P2-B-14 §2 决策 3)
 * - `useDeleteThreadMutation()` → `DELETE /threads/{id}`(P2-F-06)
 *
 * ## X-Workspace-Id 注入
 *
 * 与 characters.ts 同模式,通过 `apiGet/apiPost/apiPut/apiDelete` 的
 * `{ workspaceId }` 选项注入。跨工作区视为 404(P2-B-15 §2 决策沿用
 * P2-B-14 §2 决策 1)。
 *
 * ## queryKey 命名空间
 *
 * - 列表:`["threads", workspaceId, status ?? null, importance ?? null]`
 *   把 query 参数嵌进 key,让同 workspace 不同筛选条件分别缓存(切换筛选不
 *   重取已访问过的组合)。null 占位区分"未指定"与具体值。
 * - 详情:`["thread", workspaceId, threadId]`
 *
 * 不同前缀让 mutation invalidate 时按粒度分别命中:`POST` / `PUT` 影响列表;
 * `PUT` 同时 invalidate 单条详情;`DELETE` invalidate 列表 + `removeQueries`
 * 单条详情(避免 stale cache 仍被读到)。**列表 invalidate 用前缀短匹配**
 * (`["threads", workspaceId]`)以命中 status/importance 不同组合的所有缓存。
 *
 * ## `enabled` 短路
 *
 * 缺关键参(`workspaceId` / `threadId` 任一空)时 `enabled=false`,query 不
 * 发,UI 状态保持 `isPending`。组件层做 5 态分支(对齐 ChaptersBoardPage /
 * CharactersPage 套路)。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiDelete, apiGet, apiPost, apiPut } from "@/api/client";
import type { components } from "@/api/types";

export type PlotThread = components["schemas"]["PlotThread"];
export type PlotThreadOut = components["schemas"]["PlotThreadOut"];
export type PlotThreadDeleteResult =
  components["schemas"]["PlotThreadDeleteResult"];
export type PlotThreadStatus = PlotThread["status"];

export interface ThreadListFilter {
  status?: PlotThreadStatus | null;
  importance?: number | null;
}

export const THREADS_QUERY_KEY = (
  workspaceId: string,
  filter?: ThreadListFilter,
) =>
  [
    "threads",
    workspaceId,
    filter?.status ?? null,
    filter?.importance ?? null,
  ] as const;

export const THREAD_DETAIL_QUERY_KEY = (
  workspaceId: string,
  threadId: string,
) => ["thread", workspaceId, threadId] as const;

/**
 * 列出工作区内全部伏笔(对应 `GET /api/v1/threads`)。
 *
 * 可选 query 参数:
 *
 * - `status`:`"planted" | "developing" | "resolved" | "abandoned"`,后端服务
 *   层 in-memory 过滤(P2-B-15 §2 决策 1),OpenAPI 由 `Literal` 暴露 enum
 * - `importance`:1-5 整数,后端 `Query(ge=1, le=5)` 校验,非法值 422
 *
 * `workspaceId` 为 null/undefined/空字符串时 `enabled=false`,query 不发。
 * 排序由后端 `thread_service.list_threads` 决定(store 层默认字典序,前端
 * 不做二次排序)。
 */
export function useThreadListQuery(
  workspaceId: string | null | undefined,
  filter?: ThreadListFilter,
) {
  const enabled = typeof workspaceId === "string" && workspaceId.length > 0;
  const status = filter?.status ?? null;
  const importance = filter?.importance ?? null;

  return useQuery<PlotThreadOut[]>({
    queryKey: THREADS_QUERY_KEY(workspaceId ?? "", filter),
    queryFn: () => {
      const params = new URLSearchParams();
      if (status) params.set("status", status);
      if (typeof importance === "number") {
        params.set("importance", String(importance));
      }
      const qs = params.toString();
      const path = qs.length > 0 ? `/threads?${qs}` : "/threads";
      return apiGet<PlotThreadOut[]>(path, {
        workspaceId: workspaceId as string,
      });
    },
    enabled,
  });
}

/**
 * 读单个伏笔(对应 `GET /api/v1/threads/{thread_id}`)。
 *
 * P2-F-06 详情抽屉用,P2-F-05 暂不调用,但 hook 先就位避免 P2-F-06 临时改
 * 这个文件触发命名 / queryKey 漂移(沿用 characters.ts 同步铺好 detail
 * hook 的套路)。
 *
 * 错误码语义(P2-B-15 §2 决策沿用 P2-B-14):
 * - 不存在 / 跨工作区 → 404 `PLOT_THREAD_NOT_FOUND`
 */
export function useThreadDetailQuery(
  workspaceId: string | null | undefined,
  threadId: string | null | undefined,
) {
  const enabled =
    typeof workspaceId === "string" &&
    workspaceId.length > 0 &&
    typeof threadId === "string" &&
    threadId.length > 0;
  return useQuery<PlotThreadOut>({
    queryKey: THREAD_DETAIL_QUERY_KEY(workspaceId ?? "", threadId ?? ""),
    queryFn: () =>
      apiGet<PlotThreadOut>(
        `/threads/${encodeURIComponent(threadId as string)}`,
        { workspaceId: workspaceId as string },
      ),
    enabled,
  });
}

/**
 * 列表 invalidate 用前缀短匹配,命中 status/importance 不同组合的所有缓存。
 * 不传第三 / 第四档 key 段,react-query 默认前缀语义即覆盖全部子组合。
 */
const THREADS_LIST_PREFIX = (workspaceId: string) =>
  ["threads", workspaceId] as const;

export interface CreateThreadArgs {
  workspaceId: string;
  thread: PlotThread;
}

/**
 * 新建伏笔(对应 `POST /api/v1/threads`)。
 *
 * body 是完整 `PlotThread` schema(P2-B-15 沿用 P2-B-14 §2 决策 3 写操作 body
 * 全量)。
 *
 * 错误码语义(P2-B-15 §2 决策沿用 P2-B-14):
 * - `thread_id` 已存在 → 409 `PLOT_THREAD_ALREADY_EXISTS`
 * - body.workspace_id ≠ header → 422 `PLOT_THREAD_WORKSPACE_MISMATCH`
 * - 字段不合 schema → 422 `VALIDATION_FAILED`
 *
 * onSuccess invalidate 列表前缀(命中所有 status/importance 组合)。
 */
export function useCreateThreadMutation() {
  const queryClient = useQueryClient();
  return useMutation<PlotThreadOut, Error, CreateThreadArgs>({
    mutationFn: ({ workspaceId, thread }) =>
      apiPost<PlotThreadOut, PlotThread>("/threads", thread, { workspaceId }),
    onSuccess: (_data, { workspaceId }) => {
      void queryClient.invalidateQueries({
        queryKey: THREADS_LIST_PREFIX(workspaceId),
      });
    },
  });
}

export interface UpdateThreadArgs {
  workspaceId: string;
  thread: PlotThread;
}

/**
 * 全量更新伏笔(对应 `PUT /api/v1/threads/{thread_id}`)。
 *
 * body 是完整 `PlotThread` schema(P2-B-15 沿用 P2-B-14 §2 决策 3)。
 * `thread_id` 取自 `args.thread.thread_id`,URL 编码后拼路径;路由层校验
 * body.thread_id ≠ URL → 422 `PLOT_THREAD_ID_MISMATCH`(调用方保证一致)。
 *
 * 错误码语义:
 * - 不存在 / 跨工作区 → 404 `PLOT_THREAD_NOT_FOUND`
 * - body.workspace_id ≠ header → 422 `PLOT_THREAD_WORKSPACE_MISMATCH`
 * - 字段不合 schema → 422 `VALIDATION_FAILED`
 *
 * onSuccess invalidate 列表前缀 + 单条详情(`status` / `importance` /
 * `actual_payoff` 等列字段可能改,影响列表与抽屉)。
 */
export function useUpdateThreadMutation() {
  const queryClient = useQueryClient();
  return useMutation<PlotThreadOut, Error, UpdateThreadArgs>({
    mutationFn: ({ workspaceId, thread }) =>
      apiPut<PlotThreadOut, PlotThread>(
        `/threads/${encodeURIComponent(thread.thread_id)}`,
        thread,
        { workspaceId },
      ),
    onSuccess: (_data, { workspaceId, thread }) => {
      void queryClient.invalidateQueries({
        queryKey: THREADS_LIST_PREFIX(workspaceId),
      });
      void queryClient.invalidateQueries({
        queryKey: THREAD_DETAIL_QUERY_KEY(workspaceId, thread.thread_id),
      });
    },
  });
}

export interface DeleteThreadArgs {
  workspaceId: string;
  threadId: string;
}

/**
 * 删除伏笔(对应 `DELETE /api/v1/threads/{thread_id}`)。
 *
 * 错误码语义:
 * - 不存在 / 跨工作区 → 404 `PLOT_THREAD_NOT_FOUND`
 *
 * onSuccess invalidate 列表前缀 + `removeQueries` 单条详情(避免 stale cache
 * 被抽屉读取触发 `PLOT_THREAD_NOT_FOUND` 抖动)。删除时**不**清 Chroma 中
 * `entity_id=thread_id` 的向量记忆——P2-B-14 §1 不做项 ⑤ 同向(P2-B-15 不
 * 单独登记),留 P3+ retriever 写入回填一同改造。
 */
export function useDeleteThreadMutation() {
  const queryClient = useQueryClient();
  return useMutation<PlotThreadDeleteResult, Error, DeleteThreadArgs>({
    mutationFn: ({ workspaceId, threadId }) =>
      apiDelete<PlotThreadDeleteResult>(
        `/threads/${encodeURIComponent(threadId)}`,
        { workspaceId },
      ),
    onSuccess: (_data, { workspaceId, threadId }) => {
      void queryClient.invalidateQueries({
        queryKey: THREADS_LIST_PREFIX(workspaceId),
      });
      queryClient.removeQueries({
        queryKey: THREAD_DETAIL_QUERY_KEY(workspaceId, threadId),
      });
    },
  });
}
