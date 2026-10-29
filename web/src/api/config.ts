/**
 * 工作区配置 API hook(P3-F-02 GET / P6-F-04 PUT + 4 In schema re-export)。
 *
 * 后端入口:
 * - `GET /api/v1/workspaces/{workspace_id}/config`(P3-B-10):合并视图(引擎默认
 *   ⊕ project.yaml)`WorkspaceConfigOut`。
 * - `PUT /api/v1/workspaces/{workspace_id}/config`(P6-B-12):整体替换工作区覆盖
 *   层(`writer_profiles` / `writer_routing` 两段)。body 用 `WorkspaceConfigUpdateIn`
 *   (In 与 Out 分离:Out 是合并后视图;In 是 *只* 工作区覆盖层,不能直接 PUT Out)。
 *
 * ## queryKey 命名空间
 *
 * `["workspace_config", workspaceId]`——独立前缀。PUT 成功后由 mutation
 * `onSuccess` 触发 `invalidateQueries({ queryKey: ["workspace_config", id] })`
 * 让 GET 拉回新的合并视图。
 *
 * ## 热加载语义
 *
 * 后端 `_make_router_factory` 每次 LLM 调用重读 `project.yaml`,**无独立
 * `:reload` 端点**;PUT 落盘后下次 LLM 调用即热加载(详 P6-B-12 impl §6 + plan
 * §P6-F-04 拍板字面)。
 *
 * ## BUILTIN_PROFILE_IDS
 *
 * 4 内置 profile_id 前端常量,与后端 `src/services/config_service.py`
 * `BUILTIN_PROFILE_IDS` frozenset 字面一致。**仅供 ConfigPage UI 禁用"删除"按钮**
 * (内置不可删,自定义可删);后端 yaml dict-level merge 是加法语义,工作区根本
 * 无法删引擎默认 profile,无需走 API。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiGet, apiPut } from "@/api/client";
import type { components } from "@/api/types";

export type WorkspaceConfigOut = components["schemas"]["WorkspaceConfigOut"];
export type WorkspaceConfigUpdateIn =
  components["schemas"]["WorkspaceConfigUpdateIn"];
export type CostGuardConfigIn = components["schemas"]["CostGuardConfigIn"];
export type CostGuardConfigOut = components["schemas"]["CostGuardConfigOut"];
export type WriterProfileOut = components["schemas"]["WriterProfileOut"];
export type WriterProfileIn = components["schemas"]["WriterProfileIn"];
export type RoutingConfigOut = components["schemas"]["RoutingConfigOut"];
export type RoutingConfigIn = components["schemas"]["RoutingConfigIn"];
export type RoutingRuleOut = components["schemas"]["RoutingRuleOut"];
export type RoutingRuleIn = components["schemas"]["RoutingRuleIn"];

export const WORKSPACE_CONFIG_QUERY_KEY = (workspaceId: string) =>
  ["workspace_config", workspaceId] as const;

/**
 * 4 内置 profile_id 前端常量(P6-B-12 BUILTIN_PROFILE_IDS frozenset 镜像)。
 *
 * 用法:`BUILTIN_PROFILE_IDS.has(profileId)` → 内置 (UI 禁用删除按钮) / 否则可删。
 */
export const BUILTIN_PROFILE_IDS: ReadonlySet<string> = new Set([
  "cloud_quality",
  "cloud_premium",
  "local_qwen_32k",
  "local_qwen_safe",
]);

/**
 * 拉工作区合并后配置(对应 `GET /api/v1/workspaces/{workspace_id}/config`)。
 *
 * `workspaceId` 为 null/undefined/空字符串时 `enabled=false`,query 不发。
 *
 * 后端回退语义:工作区目录 / `project.yaml` 不存在时返引擎默认(200,不抛 404),
 * 调用方拿到的始终是合法 `WorkspaceConfigOut`,**无需特判 404**。
 */
export function useWorkspaceConfigQuery(
  workspaceId: string | null | undefined,
) {
  const enabled = typeof workspaceId === "string" && workspaceId.length > 0;
  return useQuery<WorkspaceConfigOut>({
    queryKey: WORKSPACE_CONFIG_QUERY_KEY(workspaceId ?? ""),
    queryFn: () =>
      apiGet<WorkspaceConfigOut>(
        `/workspaces/${encodeURIComponent(workspaceId as string)}/config`,
      ),
    enabled,
  });
}

/**
 * PUT 工作区配置覆盖层(对应 `PUT /api/v1/workspaces/{workspace_id}/config`)。
 *
 * body 是"工作区覆盖层",**不是**合并后视图。
 * 成功后 invalidate `["workspace_config", workspaceId]` 让 GET 拉回新的合并视图。
 *
 * 错误码:
 * - 404 `WORKSPACE_NOT_FOUND`(工作区目录不存在)
 * - 422 `CONFIG_YAML_INVALID`(覆盖层与引擎默认合并后违反交叉校验,如 routing.default
 *   引用了未定义的 profile)
 * - 422 `VALIDATION_FAILED`(Pydantic 字段级错误,如 extra 字段 / 类型不符)
 */
export function useUpdateWorkspaceConfigMutation(workspaceId: string) {
  const queryClient = useQueryClient();
  return useMutation<WorkspaceConfigOut, Error, WorkspaceConfigUpdateIn>({
    mutationFn: (body) =>
      apiPut<WorkspaceConfigOut, WorkspaceConfigUpdateIn>(
        `/workspaces/${encodeURIComponent(workspaceId)}/config`,
        body,
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: WORKSPACE_CONFIG_QUERY_KEY(workspaceId),
      });
    },
  });
}

/**
 * 局部更新工作区成本守卫，不触碰 project.yaml 的其他配置段。
 */
export function useUpdateWorkspaceCostGuardMutation(workspaceId: string) {
  const queryClient = useQueryClient();
  return useMutation<CostGuardConfigOut, Error, CostGuardConfigIn>({
    mutationFn: (body) =>
      apiPut<CostGuardConfigOut, CostGuardConfigIn>(
        `/workspaces/${encodeURIComponent(workspaceId)}/config/cost-guard`,
        body,
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: WORKSPACE_CONFIG_QUERY_KEY(workspaceId),
      });
    },
  });
}
