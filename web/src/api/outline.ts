/**
 * 大纲 API hooks(P5-F-02 起 / P5-F-03 / P5-F-05 共用)。
 *
 * 端点对账(P5-B-04 / P5-B-09 全已落):
 *
 * - POST `/workspaces/{id}/outline:generate` — 4 段串行生成,响应 `TaskInfo`
 *   (`task_id` + result 含完整 outline);后端 P5-B-04 同步执行,WS publish 到
 *   `task.{task_id}.progress` 在响应期间内完成(前端订阅时机受同步路由限制,见
 *   `docs/impl/p5_f_02_*.md` 留观项)。
 * - POST `/workspaces/{id}/outline:regenerate-section` — 单段重生(P5-F-03 用)
 * - POST `/workspaces/{id}/outline:commit` — gate + 落 volumes + 异步生成首卡
 *   (P5-F-03 "确认并保存"按钮)
 * - GET `/workspaces/{id}/outline` — 4 段聚合读(P5-F-05 用)
 * - PUT `/workspaces/{id}/outline` — patch 单段或多段(P5-F-05 用)
 * - GET `/workspaces/{id}/outline:status` — outline 3 档状态(P5-F-02 判按钮可点)
 *
 * 命名约定:hook 名 = use<动作><对象>{Query|Mutation},与 workspaces.ts 既有
 * 模式一致。query 默认 staleTime 30s(由 main.tsx 全局设);mutation 成功后
 * invalidate 相关 query。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiGet, apiPost, apiPut } from "@/api/client";
import type { components } from "@/api/types";

// The server defaults this new field; older saved form values remain valid inputs.
type OutlineGenerateSchema = components["schemas"]["OutlineGenerateIn"];
export type OutlineGenerateIn = Omit<OutlineGenerateSchema, "planning_mode"> &
  Partial<Pick<OutlineGenerateSchema, "planning_mode">>;
export type OutlineRegenerateSectionIn =
  components["schemas"]["OutlineRegenerateSectionIn"];
export type OutlineUpdateIn = components["schemas"]["OutlineUpdateIn"];
export type OutlineOut = components["schemas"]["OutlineOut"];
export type OutlineCommitResult =
  components["schemas"]["OutlineCommitResult"];
export type OutlineStatusOut = components["schemas"]["OutlineStatusOut"];
export type TaskInfo = components["schemas"]["TaskInfo"];

/** outline 状态 3 档(对齐 `WorkspaceORM.outline_status` CHECK)。 */
export type OutlineStatus = "not_started" | "generating" | "committed";

/** 4 段 section enum(对齐 OutlineRegenerateSectionIn.section)。
 *
 * 注:v1.0.x wave 4 session 1 后端把 OutlineSectionLiteral 扩到 5 档(加
 * "mini_plan"),但前端 OutlineSection 类型保持 4 档不动 —— OutlineSectionView /
 * OutlineSectionEditor 沿用 4 段路由,mini_plan 在 OutlinePage 内独立 tab
 * 渲染(走 MiniPlanTabContent,不复用 OutlineSection 派发)。
 */
export type OutlineSection = "world" | "characters" | "plot" | "volume_plans";

// 4 段子结构 re-export(供 OutlineSectionView 等组件直接 import)。
export type WorldOutput = components["schemas"]["WorldOutput"];
export type CharactersOutput = components["schemas"]["CharactersOutput"];
export type CharacterOutlineItem =
  components["schemas"]["CharacterOutlineItem"];
export type PlotOutput = components["schemas"]["PlotOutput"];
export type VolumePlansOutput = components["schemas"]["VolumePlansOutput"];
export type VolumePlanItem = components["schemas"]["VolumePlanItem"];
export type VolumeArcPlan = components["schemas"]["VolumeArcPlan"];

// v1.0.x wave 4 session 1 加 — mini_plan 第 5 段
export type MiniPlanOutput = components["schemas"]["MiniPlanOutput"];
export type MiniPlanItem = components["schemas"]["MiniPlanItem"];

export const OUTLINE_QUERY_KEY = (workspaceId: string) =>
  ["outline", workspaceId] as const;

export const OUTLINE_STATUS_QUERY_KEY = (workspaceId: string) =>
  ["outline", workspaceId, "status"] as const;

/**
 * 取 outline 当前状态(对应 `GET /workspaces/{id}/outline:status`)。
 *
 * P5-F-02:NewBookPage 用于判定"生成"按钮是否可点(`not_started` 可点;
 * `generating` 进度态;`committed` 跳转 OutlinePage)。空 workspaceId 时禁用。
 */
export function useOutlineStatusQuery(workspaceId: string | undefined) {
  return useQuery<OutlineStatusOut>({
    queryKey: OUTLINE_STATUS_QUERY_KEY(workspaceId ?? ""),
    queryFn: () =>
      apiGet<OutlineStatusOut>(
        `/workspaces/${encodeURIComponent(workspaceId ?? "")}/outline:status`,
      ),
    enabled: typeof workspaceId === "string" && workspaceId.length > 0,
  });
}

/**
 * 读取 outline 4 段聚合(对应 `GET /workspaces/{id}/outline`)。
 *
 * P5-F-03 / P5-F-05 用;某段未生成时该字段为 null。
 */
export function useOutlineQuery(workspaceId: string | undefined) {
  return useQuery<OutlineOut>({
    queryKey: OUTLINE_QUERY_KEY(workspaceId ?? ""),
    queryFn: () =>
      apiGet<OutlineOut>(
        `/workspaces/${encodeURIComponent(workspaceId ?? "")}/outline`,
      ),
    enabled: typeof workspaceId === "string" && workspaceId.length > 0,
  });
}

/**
 * 生成大纲(对应 `POST /workspaces/{id}/outline:generate`)。
 *
 * 后端 P5-B-04 同步执行 4 段串行,响应到达时全段完成。响应 `TaskInfo` 含
 * `task_id`(`result.task_id` 与顶层 `task_id` 同值)+ `result.outline` 完整
 * 4 段产出。WS publish 到 `task.{task_id}.progress` 在响应到达前已结束;
 * 当前前端只能在响应后跳 done 态(同步路由现状,P3+ worker 化后改真增量)。
 *
 * mutation variables: `{workspaceId, body}`;成功后 invalidate outline + status
 * query 触发右侧 tab / 状态条重渲。
 */
export function useGenerateOutlineMutation() {
  const queryClient = useQueryClient();
  return useMutation<
    TaskInfo,
    Error,
    { workspaceId: string; body: OutlineGenerateIn }
  >({
    mutationFn: ({ workspaceId, body }) =>
      apiPost<TaskInfo, OutlineGenerateIn>(
        `/workspaces/${encodeURIComponent(workspaceId)}/outline:generate`,
        body,
      ),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({
        queryKey: OUTLINE_QUERY_KEY(variables.workspaceId),
      });
      void queryClient.invalidateQueries({
        queryKey: OUTLINE_STATUS_QUERY_KEY(variables.workspaceId),
      });
    },
  });
}

/**
 * 单段重新生成(对应 `POST /workspaces/{id}/outline:regenerate-section`)。
 *
 * P5-F-03 单 tab "重新生成此项"按钮用;`volume_plans` 段必须同时给
 * `total_volumes` / `chapters_per_volume` / `tension_template`(后端校验)。
 *
 * **v1.0.x Commit B(2026-05-21):后端改 BackgroundTasks 异步**,mutation
 * 返 TaskInfo(pending,task_id)而非 OutlineOut。caller 用 useTaskProgress
 * 监听 done event 再 invalidate `OUTLINE_QUERY_KEY` 拿最终 4 段聚合。本 hook
 * onSuccess **不再** 立即 invalidate(避免 task 没跑完就重 fetch 拿陈旧数据)。
 */
export function useRegenerateOutlineSectionMutation() {
  return useMutation<
    TaskInfo,
    Error,
    { workspaceId: string; body: OutlineRegenerateSectionIn }
  >({
    mutationFn: ({ workspaceId, body }) =>
      apiPost<TaskInfo, OutlineRegenerateSectionIn>(
        `/workspaces/${encodeURIComponent(workspaceId)}/outline:regenerate-section`,
        body,
      ),
  });
}

/**
 * 提交大纲(对应 `POST /workspaces/{id}/outline:commit`)。
 *
 * P5-F-03 "确认并保存"按钮用;后端跑 [HARD] gate(`OUTLINE_ARC_TOO_SHORT 422`
 * 列违规 arc 区间)+ 落 volumes 表 + 只启动异步 ch-0001 首卡任务。后续卡由
 * 正式 Run 在上一章富记忆 succeeded 后逐章生成。
 */
export function useCommitOutlineMutation() {
  const queryClient = useQueryClient();
  return useMutation<OutlineCommitResult, Error, string>({
    mutationFn: (workspaceId) =>
      apiPost<OutlineCommitResult>(
        `/workspaces/${encodeURIComponent(workspaceId)}/outline:commit`,
      ),
    onSuccess: (_data, workspaceId) => {
      void queryClient.invalidateQueries({
        queryKey: OUTLINE_STATUS_QUERY_KEY(workspaceId),
      });
    },
  });
}

/**
 * 更新 outline 单段或整段(对应 `PUT /workspaces/{id}/outline`)。
 *
 * P5-F-05 "保存"按钮用;body 4 段全 optional,仅非 None 段写入。
 */
export function useUpdateOutlineMutation() {
  const queryClient = useQueryClient();
  return useMutation<
    OutlineOut,
    Error,
    { workspaceId: string; body: OutlineUpdateIn }
  >({
    mutationFn: ({ workspaceId, body }) =>
      apiPut<OutlineOut, OutlineUpdateIn>(
        `/workspaces/${encodeURIComponent(workspaceId)}/outline`,
        body,
      ),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({
        queryKey: OUTLINE_QUERY_KEY(variables.workspaceId),
      });
      // v1.0.x wave 4 session 5:mini_plan 段更新会直接影响下游章节卡 derive_context
      // 输出(下一批生成时拉新 one_line_event),让章节卡列表 cache 失效让 UI 能
      // 反映 "outline 已改 + 章节卡未重生 stale" 提示态。
      if (variables.body.mini_plan !== undefined) {
        void queryClient.invalidateQueries({
          queryKey: ["chapter-cards", variables.workspaceId],
        });
      }
    },
  });
}
