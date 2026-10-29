/**
 * API key 凭证管理 hook(v1.1 wave 3 H4 — 接 P6-B-16 已就绪的后端端点)。
 *
 * 后端入口(``/config/api-keys`` 是**进程级**资源,非工作区子资源 —— API key
 * 全工作区共享 ``Settings.<provider>_api_key``):
 * - ``GET  /api/v1/config/api-keys``:全 provider 脱敏状态列表 ``ApiKeyListOut``。
 * - ``PUT  /api/v1/config/api-keys``:写入单 provider key 到 ``.env`` + 原地刷新
 *   ``Settings`` 单例(无需重启)+ WS publish ``api_key_updated``。body 用
 *   ``ApiKeyIn``;返回该 provider 脱敏状态 ``ApiKeyStatusOut``。
 *
 * ## queryKey 命名空间
 *
 * ``["api_keys"]``(全局,不带 workspaceId —— key 是进程级凭证)。PUT 成功后
 * mutation ``onSuccess`` invalidate 让 GET 拉回最新脱敏状态。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiGet, apiPut } from "@/api/client";
import type { components } from "@/api/types";

export type ApiKeyListOut = components["schemas"]["ApiKeyListOut"];
export type ApiKeyStatusOut = components["schemas"]["ApiKeyStatusOut"];
export type ApiKeyIn = components["schemas"]["ApiKeyIn"];

/** ``ApiKeyIn.provider`` 受限枚举(对齐后端 ``_KNOWN_API_KEY_PROVIDERS``)。 */
export type ApiKeyProvider = ApiKeyIn["provider"];

/**
 * 4 个支持 API key 写入的 provider + 中文展示名(对齐后端
 * ``_KNOWN_API_KEY_PROVIDERS`` 枚举字面;ollama 走本地 HTTP 端点,不在此列)。
 */
export const API_KEY_PROVIDERS: ReadonlyArray<{
  provider: ApiKeyProvider;
  label: string;
  hint: string;
}> = [
  { provider: "claude", label: "Claude (Anthropic)", hint: "形如 sk-ant-... 开头" },
  { provider: "dashscope", label: "通义千问 (DashScope)", hint: "形如 sk-... 32 字符" },
  { provider: "deepseek", label: "DeepSeek", hint: "形如 sk-... 开头" },
  {
    provider: "openai_compatible",
    label: "OpenAI 兼容 (通配)",
    hint: "Moonshot / 智谱 / OpenAI 官方等兼容 endpoint 共用此 key",
  },
];

export const API_KEYS_QUERY_KEY = ["api_keys"] as const;

/** 拉全 provider API key 脱敏状态(``GET /api/v1/config/api-keys``)。 */
export function useApiKeysQuery() {
  return useQuery<ApiKeyListOut>({
    queryKey: API_KEYS_QUERY_KEY,
    queryFn: () => apiGet<ApiKeyListOut>("/config/api-keys"),
  });
}

/**
 * 写入单 provider API key(``PUT /api/v1/config/api-keys``)。
 *
 * 成功后 invalidate ``["api_keys"]`` 让 GET 拉回最新脱敏状态。
 *
 * 错误码:
 * - 422 ``VALIDATION_FAILED``(provider 越界 / api_key 短于 8 字符)
 * - 422 ``API_KEY_PROVIDER_UNKNOWN``(provider 不在 KNOWN_PROVIDERS)
 * - 500 ``API_KEY_WRITE_FAILED``(.env 写入失败)
 */
export function useUpdateApiKeyMutation() {
  const queryClient = useQueryClient();
  return useMutation<ApiKeyStatusOut, Error, ApiKeyIn>({
    mutationFn: (body) =>
      apiPut<ApiKeyStatusOut, ApiKeyIn>("/config/api-keys", body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: API_KEYS_QUERY_KEY });
    },
  });
}
