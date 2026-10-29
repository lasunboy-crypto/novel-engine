import { useQuery } from "@tanstack/react-query";

import { apiGet } from "@/api/client";
import type { components } from "@/api/types";

export type CoreStateAuditOut = components["schemas"]["CoreStateAuditOut"];

export const CORE_STATE_AUDIT_QUERY_KEY = (workspaceId: string) =>
  ["core_state_audit", workspaceId] as const;

export function useCoreStateAuditQuery(workspaceId: string) {
  return useQuery<CoreStateAuditOut>({
    queryKey: CORE_STATE_AUDIT_QUERY_KEY(workspaceId),
    queryFn: () =>
      apiGet<CoreStateAuditOut>("/chapters/core-state", {
        workspaceId,
      }),
    enabled: workspaceId.length > 0,
  });
}
