import { useQuery } from "@tanstack/react-query";
import { apiGet, apiPost, apiPut } from "@/api/client";
import type { components } from "@/api/types";
import type { TaskInfo } from "@/api/tasks";

export type AuthoringView = components["schemas"]["AuthoringView"];
export type AuthoringDraft = components["schemas"]["AuthoringDraft"];
export type AuthoringPreferences = components["schemas"]["AuthoringPreferences"];
export type AuthoringSaveIn = components["schemas"]["AuthoringSaveIn"];
export type AuthoringActionIn = components["schemas"]["AuthoringActionIn"];
export const authoringKey = (id: string) => ["authoring", id] as const;
const endpoint = (id: string) => `/workspaces/${encodeURIComponent(id)}/authoring`;
export const saveAuthoring = (id: string, body: AuthoringSaveIn) => apiPut<AuthoringView>(endpoint(id), body);
export const actAuthoring = (id: string, body: AuthoringActionIn) => apiPost<TaskInfo>(`${endpoint(id)}:act`, body);
export function useAuthoringQuery(id: string, enabled = true) {
  return useQuery({ queryKey: authoringKey(id), queryFn: () => apiGet<AuthoringView>(endpoint(id)), retry: false, enabled });
}
