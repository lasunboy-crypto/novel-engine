/**
 * Pipeline API hooks(P6-F-02)。
 *
 * 端点对账(P6-B-02 / P6-B-03 全已落,见 web/src/api/types.ts):
 *
 * - POST `/api/v1/pipeline:start` → `PipelineStartIn` body + `PipelineStartOut`
 * - POST `/api/v1/pipeline:pause` → 空 body + `PipelinePauseOut`
 * - POST `/api/v1/pipeline:resume` → 空 body + `PipelinePauseOut`(共用 shape)
 * - POST `/api/v1/pipeline:stop` → 空 body + `PipelinePauseOut`
 * - GET `/api/v1/pipeline/status` → `PipelineStatusOut`
 *
 * 所有端点需 `X-Workspace-Id` header(对齐 P6-B-02 决策 — workspace 路由
 * vs 流水线路由的边界仿 chapters.ts 章节路由模式)。
 *
 * ## queryKey 命名空间
 *
 * - 状态:`["pipeline-status", workspaceId]`
 *
 * mutation onSuccess 后 invalidate 同 key,触发 RunControlPage 顶部状态卡刷新;
 * 同时 chapter_finished WS 事件来时 RunControlPage 主动 invalidate
 * `["chapters", workspaceId]` 让 ChaptersBoardPage 跨页面对账。
 *
 * ## refetchInterval 轮询兜底
 *
 * WS 负责即时刷新；cached state 仍为 running 时每 5 秒做一次低频 REST
 * 对账。后端的 error / watchdog interrupted 没有新增 WS 事件，而且任何 WS
 * 都可能丢包；没有这层兜底，页面会永久假显示“运行中”。一旦读到非 running
 * 立即停止轮询。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiGet, apiPost } from "@/api/client";
import type { components } from "@/api/types";

export type PipelineStartIn = components["schemas"]["PipelineStartIn"];
export type PipelineStartOut = components["schemas"]["PipelineStartOut"];
export type PipelinePauseOut = components["schemas"]["PipelinePauseOut"];
export type PipelineStatusOut = components["schemas"]["PipelineStatusOut"];
export type PipelineState = PipelineStatusOut["state"];
export type PipelineScope = NonNullable<PipelineStatusOut["scope"]>;
export type PipelineFailurePolicy = PipelineStatusOut["failure_policy"];

export const PIPELINE_STATUS_QUERY_KEY = (workspaceId: string) =>
  ["pipeline-status", workspaceId] as const;

export const PIPELINE_RUNNING_REFETCH_INTERVAL_MS = 5_000;

export function pipelineStatusRefetchInterval(
  state: PipelineState | undefined,
): number | false {
  return state === "running" ? PIPELINE_RUNNING_REFETCH_INTERVAL_MS : false;
}

/**
 * 读流水线当前状态(对应 `GET /api/v1/pipeline/status`)。
 *
 * 无 active run 时后端返 `state="idle"` + 其他字段空(`run_id=null` /
 * `chapter_ids=[]` 等);**不会 404**。空 workspaceId 时禁用。
 */
export function usePipelineStatusQuery(
  workspaceId: string | null | undefined,
) {
  const enabled = typeof workspaceId === "string" && workspaceId.length > 0;
  return useQuery<PipelineStatusOut>({
    queryKey: PIPELINE_STATUS_QUERY_KEY(workspaceId ?? ""),
    queryFn: () =>
      apiGet<PipelineStatusOut>("/pipeline/status", {
        workspaceId: workspaceId as string,
      }),
    enabled,
    refetchInterval: (query) =>
      pipelineStatusRefetchInterval(query.state.data?.state),
  });
}

interface MutationArgs<TBody = undefined> {
  workspaceId: string;
  body?: TBody;
}

/**
 * 启动 pipeline 新 run(对应 `POST /api/v1/pipeline:start`)。
 *
 * 错误码(对齐 P6-B-02):
 * - 409 `PIPELINE_ALREADY_RUNNING`:已有 active run
 * - 422 `PIPELINE_INVALID_SCOPE`:scope 字段不合法 / 跨字段约束失败
 *
 * onSuccess 后 invalidate `["pipeline-status", workspaceId]` 触发顶部状态卡
 * 刷新。
 */
export function useStartPipelineMutation() {
  const queryClient = useQueryClient();
  return useMutation<PipelineStartOut, Error, MutationArgs<PipelineStartIn>>({
    mutationFn: ({ workspaceId, body }) =>
      apiPost<PipelineStartOut, PipelineStartIn>(
        "/pipeline:start",
        body as PipelineStartIn,
        { workspaceId },
      ),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({
        queryKey: PIPELINE_STATUS_QUERY_KEY(variables.workspaceId),
      });
    },
  });
}

/** 暂停 active run(对应 `POST /api/v1/pipeline:pause`)。 */
export function usePausePipelineMutation() {
  const queryClient = useQueryClient();
  return useMutation<PipelinePauseOut, Error, MutationArgs>({
    mutationFn: ({ workspaceId }) =>
      apiPost<PipelinePauseOut, Record<string, never>>(
        "/pipeline:pause",
        {} as Record<string, never>,
        { workspaceId },
      ),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({
        queryKey: PIPELINE_STATUS_QUERY_KEY(variables.workspaceId),
      });
    },
  });
}

/** 继续 paused run(对应 `POST /api/v1/pipeline:resume`)。 */
export function useResumePipelineMutation() {
  const queryClient = useQueryClient();
  return useMutation<PipelinePauseOut, Error, MutationArgs>({
    mutationFn: ({ workspaceId }) =>
      apiPost<PipelinePauseOut, Record<string, never>>(
        "/pipeline:resume",
        {} as Record<string, never>,
        { workspaceId },
      ),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({
        queryKey: PIPELINE_STATUS_QUERY_KEY(variables.workspaceId),
      });
    },
  });
}

/** 终止 active run(对应 `POST /api/v1/pipeline:stop`)。 */
export function useStopPipelineMutation() {
  const queryClient = useQueryClient();
  return useMutation<PipelinePauseOut, Error, MutationArgs>({
    mutationFn: ({ workspaceId }) =>
      apiPost<PipelinePauseOut, Record<string, never>>(
        "/pipeline:stop",
        {} as Record<string, never>,
        { workspaceId },
      ),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({
        queryKey: PIPELINE_STATUS_QUERY_KEY(variables.workspaceId),
      });
    },
  });
}

/** scope 4 档 + 中文显示标签(对齐 P6-B-02 `PipelineScope` Literal)。 */
export const PIPELINE_SCOPE_OPTIONS: ReadonlyArray<{
  value: PipelineScope;
  label: string;
  hint: string;
}> = [
  {
    value: "single",
    label: "单章(single)",
    hint: "只写 start_chapter_id 一章",
  },
  {
    value: "range",
    label: "区间(range)",
    hint: "从 start 写到 end(含),end_chapter_id 必填",
  },
  {
    value: "volume",
    label: "整卷(volume)",
    hint: "写完指定 volume 的全部章节",
  },
  {
    value: "until",
    label: "直到(until)",
    hint:
      "从 start 写到 end(含);end_chapter_id 空时默认到所在卷末",
  },
];

/** state 6 档 + 中文显示标签(v2 w1-s2 扩 interrupted,对齐 `PipelineState` Literal)。 */
export const PIPELINE_STATE_LABELS: Record<PipelineState, string> = {
  idle: "空闲",
  running: "运行中",
  paused: "已暂停",
  interrupted: "已中断(可续跑)",
  stopped: "已终止",
  error: "异常",
};

/** 失败策略 3 档 + 白话说明(v2 w1-s4 选择器 UI,对齐 `PipelineFailurePolicy` Literal + w1-s1 后端语义)。 */
export const PIPELINE_FAILURE_POLICY_OPTIONS: ReadonlyArray<{
  value: PipelineFailurePolicy;
  label: string;
  hint: string;
}> = [
  {
    value: "stop",
    label: "停下等人(stop,推荐)",
    hint: "某章写失败/进人工介入时暂停整个批量,人工解决后可一键续跑",
  },
  {
    value: "skip",
    label: "跳过继续(skip)",
    hint: "失败章标记跳过,继续写下一章;跳过章后续衔接按计划而非实际",
  },
  {
    value: "retry",
    label: "重试一次(retry)",
    hint: "失败章自动重试 1 次,仍失败按 stop 停下等人",
  },
];

/**
 * pause_reason 白话标签(v2 w1-s4;对齐后端 `PipelineStatusOut.pause_reason`
 * 注释枚举)。未知 reason 兜底显示原始 code(前向兼容)。
 */
export const PIPELINE_PAUSE_REASON_LABELS: Record<string, string> = {
  user_request: "用户手动暂停",
  chapter_invalidated: "章节已失效(上游被改写,需先解除阻塞)",
  chapter_escalated: "孤儿章:本章进入人工介入,批量已停",
  card_rejected: "章节卡被驳回,批量已停",
  continuity_ledger_not_ready: "正文已通过,连续性 checkpoint 待补",
  continuity_checkpoint_corruption:
    "连续性 checkpoint 损坏,已安全暂停并保留现场",
  memory_enrichment_not_ready:
    "上一章富记忆仍在处理,成功后可继续正式运行",
  memory_enrichment_failed:
    "上一章富记忆未成功；系统会先核对副作用和恢复资格，绝不盲目重放正文或审核",
  cost_guard_exceeded: "成本护栏触发:已达成本限额,批量已停",
  provider_call_cap_exceeded: "provider 调用预算已耗尽,批量已停",
  completed: "全部完成",
};

/**
 * interrupted / error 态 `error_details.reason` 白话标签(v2 w1-s2/s3)。
 * 未知 reason 兜底显示原始 code。
 */
export const PIPELINE_INTERRUPT_REASON_LABELS: Record<string, string> = {
  engine_restarted_mid_run: "引擎中途重启,运行被中断",
  chapter_watchdog_timeout: "章级看门狗超时:单章写作超过时间预算",
  pipeline_loop_conflict: "旧运行循环未退出,新循环让位",
};
