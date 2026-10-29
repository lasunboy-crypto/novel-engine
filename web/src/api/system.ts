/**
 * 系统级 API client(P4-acceptance-followup)。
 *
 * 当前覆盖:
 * - ``GET /api/v1/system/health``(可选 ``?workspace_id=``):6 项全局 + 5 项工作区
 *   健康检查;P5+ 升级 ConfigPage doctor 面板。
 */

import { useQuery } from "@tanstack/react-query";

import { apiGet } from "@/api/client";
import type { components } from "@/api/types";

export type HealthCheckItemOut =
  components["schemas"]["HealthCheckItemOut"];
export type HealthSummary = components["schemas"]["HealthSummary"];
export type HealthData = components["schemas"]["HealthData"];

export const SYSTEM_HEALTH_QUERY_KEY = (
  workspaceId: string | null,
) => ["system_health", workspaceId] as const;

/**
 * 拉系统健康检查(OBSERVABILITY §6)。
 *
 * - ``workspaceId`` 给定时附带 5 项工作区检查;未给只跑 6 项全局检查
 * - 不缓存太久(staleTime 默认 0;手动刷新或路由切换重新拉)
 */
export function useSystemHealthQuery(workspaceId: string | null | undefined) {
  const normalized = workspaceId && workspaceId.length > 0 ? workspaceId : null;
  const path = normalized
    ? `/system/health?workspace_id=${encodeURIComponent(normalized)}`
    : "/system/health";
  return useQuery<HealthData>({
    queryKey: SYSTEM_HEALTH_QUERY_KEY(normalized),
    queryFn: () => apiGet<HealthData>(path),
  });
}
