/**
 * 静态资产 import API hooks(P5-F-02 起 / P5-F-06 复用)。
 *
 * 端点对账(P5-B-08 已落):POST `/workspaces/{id}/assets:import`,body
 * `{world_md_content?, characters_md_content?}` 两字段均可选;响应 `AssetsImportOut`
 * 含 `workspace_id` / `files_processed[]` / `counts{character|plot_thread|timeline|fact}`。
 *
 * P5-F-02 NewBookPage 用法(决策 7=C 摊到 NewBookPage):左侧表单底部两个文件选择
 * 槽,触发上传后从 `counts["character"]` / `counts["fact"]` 取值显示
 * "角色 N 个 / 世界设定 M 条"。
 */

import { useMutation } from "@tanstack/react-query";

import { apiPost } from "@/api/client";
import type { components } from "@/api/types";

export type AssetsImportIn = components["schemas"]["AssetsImportIn"];
export type AssetsImportOut = components["schemas"]["AssetsImportOut"];

/**
 * 导入静态资产 markdown(对应 `POST /workspaces/{id}/assets:import`)。
 *
 * 两字段都可选:空字符串 / undefined 视为不上传该文件(既有 `data/assets/`
 * 下同名文件不动)。失败抛 ApiError,工作区不存在时 `WORKSPACE_INVALID 422`
 * (路由层翻译);markdown 解析失败时 `ASSETS_IMPORT_INVALID 422`。
 */
export function useImportAssetsMutation() {
  return useMutation<
    AssetsImportOut,
    Error,
    { workspaceId: string; body: AssetsImportIn }
  >({
    mutationFn: ({ workspaceId, body }) =>
      apiPost<AssetsImportOut, AssetsImportIn>(
        `/workspaces/${encodeURIComponent(workspaceId)}/assets:import`,
        body,
      ),
  });
}
