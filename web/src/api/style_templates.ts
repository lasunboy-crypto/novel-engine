/**
 * 写作风格模板 API hook(v1.1 wave 4 s3 / I1)。
 *
 * 封装 `GET /api/v1/style-templates`:返回 6 个预设模板的元数据 + 10 维度 +
 * 风格指南正文预览。NewBookForm 用于渲染"写作风格模板"下拉 + 选中预览。
 */

import { useQuery } from "@tanstack/react-query";

import { apiGet } from "@/api/client";
import type { components } from "@/api/types";

export type StyleTemplateOut = components["schemas"]["StyleTemplateOut"];

export const STYLE_TEMPLATES_QUERY_KEY = ["style-templates"] as const;

/**
 * 列出预设写作风格模板(对应 `GET /api/v1/style-templates`)。
 *
 * 模板是引擎级静态资源,内容稳定,可长缓存(沿用全局 staleTime)。
 */
export function useStyleTemplatesQuery() {
  return useQuery<StyleTemplateOut[]>({
    queryKey: STYLE_TEMPLATES_QUERY_KEY,
    queryFn: () => apiGet<StyleTemplateOut[]>("/style-templates"),
  });
}
