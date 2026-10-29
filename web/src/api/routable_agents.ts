/**
 * 可路由 agent 清单 + 全局 agent routing hook(v1.1 wave 3 H5)。
 *
 * 后端入口:
 * - `GET  /api/v1/config/routable-agents`(wave 2 H3):全 16 个 LLM-driven agent
 *   清单(中文 display_name / description / current_profile / default_profile)+
 *   available_profiles。可选 `workspace_id` query —— 给定时 current_profile 反映
 *   工作区合并视图;省略时反映引擎默认(全局视图)。
 * - `PUT  /api/v1/config/agent-routing`(wave 3 H5,路 B):**整段替换**引擎默认
 *   config.yaml 的 writer_routing.agent_routing(全局层,所有工作区共用)。body
 *   `agent_routing` 即新的完整映射;成功后返刷新的全局 routable-agents 视图。
 *
 * 两层语义(H5 拍板):
 * - **全局默认**:本文件 `useUpdateGlobalAgentRoutingMutation`(写 config.yaml)。
 * - **工作区覆盖**:走 `@/api/config` 的 `useUpdateWorkspaceConfigMutation`
 *   (写 project.yaml 的 writer_routing.agent_routing 段)。
 *
 * ## queryKey 命名空间
 *
 * `["routable_agents", workspaceId ?? "__global__"]` —— 全局视图与各工作区视图
 * 独立缓存。全局 PUT 成功后 invalidate 全局 key 让 GET 拉回新视图。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiGet, apiPut } from "@/api/client";
import type { components } from "@/api/types";

export type RoutableAgentOut = components["schemas"]["RoutableAgentOut"];
export type RoutableAgentsListOut =
  components["schemas"]["RoutableAgentsListOut"];
export type RoutableProfileMeta =
  components["schemas"]["RoutableProfileMeta"];
export type EngineAgentRoutingUpdateIn =
  components["schemas"]["EngineAgentRoutingUpdateIn"];

const GLOBAL_SCOPE = "__global__";

export const ROUTABLE_AGENTS_QUERY_KEY = (workspaceId?: string | null) =>
  ["routable_agents", workspaceId ?? GLOBAL_SCOPE] as const;

/**
 * 拉可路由 agent 清单(`GET /api/v1/config/routable-agents`)。
 *
 * `workspaceId` 省略 / null → 全局引擎默认视图;给定 → 工作区合并视图。
 */
export function useRoutableAgentsQuery(workspaceId?: string | null) {
  const scoped = typeof workspaceId === "string" && workspaceId.length > 0;
  const path = scoped
    ? `/config/routable-agents?workspace_id=${encodeURIComponent(workspaceId as string)}`
    : "/config/routable-agents";
  return useQuery<RoutableAgentsListOut>({
    queryKey: ROUTABLE_AGENTS_QUERY_KEY(scoped ? workspaceId : null),
    queryFn: () => apiGet<RoutableAgentsListOut>(path),
  });
}

/**
 * 整段替换引擎默认 agent_routing(`PUT /api/v1/config/agent-routing`)。
 *
 * body `agent_routing` 即新的完整映射(列出的 agent 用对应 profile,未列出的
 * 回退 default;空 dict 清空所有覆盖)。成功后 invalidate 全局视图 query。
 *
 * 错误码:
 * - 422 `CONFIG_YAML_INVALID`(引用未声明 profile / LLM-judge agent 切到
 *   thinking-off profile / config.yaml 结构异常)
 * - 422 `VALIDATION_FAILED`(body extra 字段 / 类型不符)
 */
export function useUpdateGlobalAgentRoutingMutation() {
  const queryClient = useQueryClient();
  return useMutation<
    RoutableAgentsListOut,
    Error,
    EngineAgentRoutingUpdateIn
  >({
    mutationFn: (body) =>
      apiPut<RoutableAgentsListOut, EngineAgentRoutingUpdateIn>(
        "/config/agent-routing",
        body,
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ROUTABLE_AGENTS_QUERY_KEY(null),
      });
    },
  });
}
