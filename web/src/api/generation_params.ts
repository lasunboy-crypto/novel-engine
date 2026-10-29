/**
 * 工作区级生成参数 hook(v1.1 wave 3 session 3 / L1 Q16)。
 *
 * 后端入口:
 * - `GET  /api/v1/workspaces/{id}/config/generation`:返回引擎默认 config.yaml
 *   `generation:` 段 ⊕ 工作区 project.yaml `generation:` 段字段级合并后的生成
 *   参数(word_count_min/max + max_rewrites)。
 * - `PUT  /api/v1/workspaces/{id}/config/generation`:**只改 generation 段**
 *   (服务端读现有覆盖层 → 替换 generation 键 → 整体存回),保留 reviewer /
 *   retrieval / cost_guard / writer 等其他覆盖不动。
 * - `POST /api/v1/workspaces/{id}/chapter-cards:reset-word-count`(G6.2 backfill):
 *   把已生成章节卡的 word_count_min/max 批量重置到指定值;L1 浮层"应用到已有
 *   章节"按钮复用该端点。
 *
 * 语义分层:
 * - **改默认**(影响后续新生成卡):`useUpdateGenerationConfigMutation`(写 project.yaml)。
 * - **应用到已有卡**:`useBackfillWordCountMutation`(改 cards/<id>.json,不动默认)。
 *
 * queryKey:`["generation_config", workspaceId]` —— 每工作区独立缓存,PUT /
 * backfill 成功后 invalidate 让 GET 拉回新视图。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiGet, apiPost, apiPut } from "@/api/client";
import type { components } from "@/api/types";

export type GenerationConfigOut =
  components["schemas"]["GenerationConfigOut"];
export type GenerationConfigIn = components["schemas"]["GenerationConfigIn"];
export type ChapterCardsResetWordCountIn =
  components["schemas"]["ChapterCardsResetWordCountIn"];
export type ChapterCardsResetWordCountResult =
  components["schemas"]["ChapterCardsResetWordCountResult"];

export const GENERATION_CONFIG_QUERY_KEY = (workspaceId: string) =>
  ["generation_config", workspaceId] as const;

/** 拉工作区生成参数(`GET /workspaces/{id}/config/generation`)。 */
export function useGenerationConfigQuery(workspaceId: string) {
  return useQuery<GenerationConfigOut>({
    queryKey: GENERATION_CONFIG_QUERY_KEY(workspaceId),
    queryFn: () =>
      apiGet<GenerationConfigOut>(
        `/workspaces/${encodeURIComponent(workspaceId)}/config/generation`,
      ),
    enabled: workspaceId.length > 0,
  });
}

/**
 * 保存工作区生成参数(`PUT /workspaces/{id}/config/generation`)。
 *
 * body 字段全可选;只发想覆盖的字段(缺失字段由引擎默认填补)。成功后
 * invalidate 本工作区 generation_config query。
 *
 * 错误码:422 `CONFIG_YAML_INVALID`(word_count_min > word_count_max / 字段
 * 越界)/ 404 `WORKSPACE_NOT_FOUND`。
 */
export function useUpdateGenerationConfigMutation(workspaceId: string) {
  const queryClient = useQueryClient();
  return useMutation<GenerationConfigOut, Error, GenerationConfigIn>({
    mutationFn: (body) =>
      apiPut<GenerationConfigOut, GenerationConfigIn>(
        `/workspaces/${encodeURIComponent(workspaceId)}/config/generation`,
        body,
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: GENERATION_CONFIG_QUERY_KEY(workspaceId),
      });
    },
  });
}

/**
 * 把字数区间批量应用到已生成章节卡(`POST /chapter-cards:reset-word-count`,G6.2)。
 *
 * `scope='unapproved'`(默认)保护用户审过字数预算的 approved 卡;`'all'` 含
 * approved。`word_count_min/max` 留空 → 回退 ChapterCard model default(2500/4000)。
 */
export function useBackfillWordCountMutation(workspaceId: string) {
  const queryClient = useQueryClient();
  return useMutation<
    ChapterCardsResetWordCountResult,
    Error,
    ChapterCardsResetWordCountIn
  >({
    mutationFn: (body) =>
      apiPost<ChapterCardsResetWordCountResult, ChapterCardsResetWordCountIn>(
        `/workspaces/${encodeURIComponent(workspaceId)}/chapter-cards:reset-word-count`,
        body,
      ),
    onSuccess: () => {
      // backfill 改的是各卡 word_count,不改 generation 默认;invalidate 章节卡相关
      // 缓存让 board / review 视图拉回新字数。queryKey 前缀广一些以覆盖各章节卡 query。
      queryClient.invalidateQueries({ queryKey: ["chapter_cards"] });
      queryClient.invalidateQueries({ queryKey: ["chapters"] });
    },
  });
}
