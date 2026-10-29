/**
 * Volume API hooks(P5-F-04)。
 *
 * 端点对账(P5-B-09 全已落):
 *
 * - GET `/workspaces/{id}/volumes` — 工作区卷列表(按 volume_id 升序)
 * - GET `/workspaces/{id}/volumes/{vid}` — 单卷(含 arcs / tension_curve)
 * - PUT `/workspaces/{id}/volumes/{vid}` — patch(title / summary /
 *   chapter_count / tension_template / arcs / tension_curve;后端整体
 *   `Volume.model_validate` 校验,违规 422 `WORKSPACE_INVALID`)
 * - POST `/workspaces/{id}/volumes/{vid}:apply-template` — 应用模板
 *   (4 档:heros_journey / steady_daily / strong_stimulation / free)
 *   响应回显 volume_id + template + tension_curve;arcs_json 不动
 *   (对齐 plan §P5-B-09 + FRONTEND_SPEC §4.6)
 *
 * 命名约定:与 outline.ts / workspaces.ts 一致。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiGet, apiPost, apiPut } from "@/api/client";
import type { components } from "@/api/types";

export type VolumeOut = components["schemas"]["VolumeOut"];
export type VolumeListOut = components["schemas"]["VolumeListOut"];
export type VolumeUpdateIn = components["schemas"]["VolumeUpdateIn"];
export type ApplyTemplateIn = components["schemas"]["ApplyTemplateIn"];
export type ApplyTemplateOut = components["schemas"]["ApplyTemplateOut"];
export type Arc = components["schemas"]["Arc"];
export type TensionCurvePoint = components["schemas"]["TensionCurvePoint"];

export type TensionTemplate = VolumeOut["tension_template"];

export const TENSION_TEMPLATE_LABELS: Record<TensionTemplate, string> = {
  heros_journey: "英雄之旅",
  steady_daily: "稳态日常",
  strong_stimulation: "强刺激",
  free: "自由",
};

export const TENSION_TEMPLATE_OPTIONS: ReadonlyArray<{
  value: TensionTemplate;
  label: string;
}> = [
  { value: "heros_journey", label: TENSION_TEMPLATE_LABELS.heros_journey },
  { value: "steady_daily", label: TENSION_TEMPLATE_LABELS.steady_daily },
  {
    value: "strong_stimulation",
    label: TENSION_TEMPLATE_LABELS.strong_stimulation,
  },
  { value: "free", label: TENSION_TEMPLATE_LABELS.free },
];

export const VOLUMES_QUERY_KEY = (workspaceId: string) =>
  ["volumes", workspaceId] as const;

export const VOLUME_QUERY_KEY = (workspaceId: string, volumeId: string) =>
  ["volumes", workspaceId, volumeId] as const;

/**
 * 取工作区卷列表(对应 `GET /workspaces/{id}/volumes`)。
 *
 * VolumePlanPage 顶部 tabs 用,显示所有卷供切换。空 workspaceId 时禁用。
 */
export function useVolumesQuery(workspaceId: string | undefined) {
  return useQuery<VolumeListOut>({
    queryKey: VOLUMES_QUERY_KEY(workspaceId ?? ""),
    queryFn: () =>
      apiGet<VolumeListOut>(
        `/workspaces/${encodeURIComponent(workspaceId ?? "")}/volumes`,
      ),
    enabled: typeof workspaceId === "string" && workspaceId.length > 0,
  });
}

/**
 * 取单卷详情(对应 `GET /workspaces/{id}/volumes/{vid}`)。
 *
 * VolumePlanPage 主体内容用 — 含 arcs / tension_curve。
 * workspaceId / volumeId 任一空时禁用。
 */
export function useVolumeQuery(
  workspaceId: string | undefined,
  volumeId: string | undefined,
) {
  const enabled =
    typeof workspaceId === "string" &&
    workspaceId.length > 0 &&
    typeof volumeId === "string" &&
    volumeId.length > 0;
  return useQuery<VolumeOut>({
    queryKey: VOLUME_QUERY_KEY(workspaceId ?? "", volumeId ?? ""),
    queryFn: () =>
      apiGet<VolumeOut>(
        `/workspaces/${encodeURIComponent(
          workspaceId ?? "",
        )}/volumes/${encodeURIComponent(volumeId ?? "")}`,
      ),
    enabled,
  });
}

/**
 * 更新卷字段(对应 `PUT /workspaces/{id}/volumes/{vid}`)。
 *
 * VolumePlanPage "保存"按钮用;后端整体 `Volume.model_validate` 校验
 * 不变式(curve 长度 vs chapter_count / arc 区间合法 / arc_id 唯一),
 * 违规 422 `WORKSPACE_INVALID`。成功后 invalidate 单卷 + 列表 query。
 */
export function useUpdateVolumeMutation() {
  const queryClient = useQueryClient();
  return useMutation<
    VolumeOut,
    Error,
    { workspaceId: string; volumeId: string; body: VolumeUpdateIn }
  >({
    mutationFn: ({ workspaceId, volumeId, body }) =>
      apiPut<VolumeOut, VolumeUpdateIn>(
        `/workspaces/${encodeURIComponent(
          workspaceId,
        )}/volumes/${encodeURIComponent(volumeId)}`,
        body,
      ),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({
        queryKey: VOLUME_QUERY_KEY(variables.workspaceId, variables.volumeId),
      });
      void queryClient.invalidateQueries({
        queryKey: VOLUMES_QUERY_KEY(variables.workspaceId),
      });
    },
  });
}

/**
 * 应用张力曲线模板(对应 `POST /workspaces/{id}/volumes/{vid}:apply-template`)。
 *
 * VolumePlanPage "应用模板"按钮用;后端查表填 tension_curve_json + 同步落
 * `tension_template` 字段(自决项 1),不动 arcs_json。响应仅含 volume_id +
 * template + tension_curve;客户端按需 invalidate 触发 GET /volumes/{vid}
 * 重读完整卷。
 */
export function useApplyTemplateMutation() {
  const queryClient = useQueryClient();
  return useMutation<
    ApplyTemplateOut,
    Error,
    { workspaceId: string; volumeId: string; body: ApplyTemplateIn }
  >({
    mutationFn: ({ workspaceId, volumeId, body }) =>
      apiPost<ApplyTemplateOut, ApplyTemplateIn>(
        `/workspaces/${encodeURIComponent(
          workspaceId,
        )}/volumes/${encodeURIComponent(volumeId)}:apply-template`,
        body,
      ),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({
        queryKey: VOLUME_QUERY_KEY(variables.workspaceId, variables.volumeId),
      });
      void queryClient.invalidateQueries({
        queryKey: VOLUMES_QUERY_KEY(variables.workspaceId),
      });
    },
  });
}
