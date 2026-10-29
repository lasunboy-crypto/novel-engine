/**
 * 工作区 API hooks(P1-F-07 列表+切换 / P1-F-08 创建 / P1-F-09 生命周期 / P1-F-10 详情)。
 *
 * 用 TanStack Query 封装工作区列表/详情查询 + 7 个 mutation
 * (switch / create / archive / clean / purge / backup / restore)。
 *
 * ## 路由名约定(重要)
 *
 * 切换走后端 `POST /workspaces/{id}:switch`(P1-B-18 + types.ts 第 71 行)。
 * NEXT_STEPS / docs/plan/p1_plan §P1-F-07 字面写 `:activate` 已过时,
 * 实际路由以 `API_CONTRACT.md` + `src/api/routes/workspaces.py` 实现 +
 * `web/src/api/types.ts` 自动生成 operation 名为准——这是 P1-F-06
 * "心跳协议方向以 spec 为准" 同处置(规格源冲突时按 spec 实现走)。
 * UI 文案保留"切换/激活"白话,代码层调 `:switch`。
 *
 * ## "当前活动工作区"推算迁移(P1-F-10 收口)
 *
 * 后端 `WorkspaceOut` 没有 `is_active` 字段;`_active` 软链是后端内部实现,
 * API 不暴露。前端推算:`last_active_at` 字典序最大者(ISO 8601 UTC 字典序
 * 与时间序一致)。**P1-F-10 起该推算迁移到 `web/src/store/activeWorkspace.ts`
 * `selectActiveWorkspace`**(zustand store + selector),原 `activeWorkspaceFromList`
 * 已删,组件改用 `useActiveWorkspace(workspaces)` hook。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiGet, apiPost } from "@/api/client";
import type { components } from "@/api/types";

export type WorkspaceOut = components["schemas"]["WorkspaceOut"];
export type WorkspaceCreateIn = components["schemas"]["WorkspaceCreateIn"];
export type WorkspaceArchiveResult =
  components["schemas"]["WorkspaceArchiveResult"];
export type WorkspaceCleanResult =
  components["schemas"]["WorkspaceCleanResult"];
export type WorkspacePurgeIn = components["schemas"]["WorkspacePurgeIn"];
export type WorkspacePurgeResult =
  components["schemas"]["WorkspacePurgeResult"];
export type WorkspaceDestroyIn = components["schemas"]["WorkspaceDestroyIn"];
export type WorkspaceDestroyResult =
  components["schemas"]["WorkspaceDestroyResult"];
// P6-B-07 把 P1 既有 `WorkspaceBackupResult` 重命名为 `BackupInfoOut`(对齐
// `BackupService.create_*_backup` 返 dataclass payload — 新字段集
// `backup_id` / `size_bytes` / `schema_version` / `engine_version` /
// `backup_type` / `chapter_id` 等)。P6-F-03 起 ExportPage 完整接入新字段集;
// 旧 `WorkspaceBackupResult` 别名 (P6-F-01 临时补救保留) 本任务删除,调用方
// 直接 import `BackupInfoOut`。
export type BackupInfoOut = components["schemas"]["BackupInfoOut"];
export type BackupListOut = components["schemas"]["BackupListOut"];
export type RestoreIn = components["schemas"]["RestoreIn"];
export type RestoreResultOut = components["schemas"]["RestoreResultOut"];
export type PurgeBackupsIn = components["schemas"]["PurgeBackupsIn"];
export type PurgeBackupsResultOut =
  components["schemas"]["PurgeBackupsResultOut"];

export const WORKSPACES_QUERY_KEY = ["workspaces"] as const;

export const BACKUPS_QUERY_KEY = (workspaceId: string) =>
  ["workspaces", workspaceId, "backups"] as const;

export const WORKSPACE_DETAIL_QUERY_KEY = (workspaceId: string) =>
  ["workspaces", workspaceId] as const;

/**
 * 列出所有工作区(对应 `GET /api/v1/workspaces`)。
 *
 * 默认 `staleTime` 由全局 QueryClient 设为 30s(`main.tsx`),
 * 30s 内重新 mount 命中缓存,无重复请求。
 */
export function useWorkspacesQuery() {
  return useQuery<WorkspaceOut[]>({
    queryKey: WORKSPACES_QUERY_KEY,
    queryFn: () => apiGet<WorkspaceOut[]>("/workspaces"),
  });
}

/**
 * 取单个工作区详情(对应 `GET /api/v1/workspaces/{id}`)。
 *
 * P1-F-10 工作区主页用,首次进入 `/workspaces/:id` 直链时无需先取列表。
 * queryKey `["workspaces", id]` 与列表 key `["workspaces"]` 兼容
 * `invalidateQueries({ queryKey: ["workspaces"] })`(react-query partial
 * match 把列表 + 全部详情一并 invalidate)。
 */
export function useWorkspaceDetailQuery(workspaceId: string | undefined) {
  return useQuery<WorkspaceOut>({
    queryKey: WORKSPACE_DETAIL_QUERY_KEY(workspaceId ?? ""),
    queryFn: () =>
      apiGet<WorkspaceOut>(
        `/workspaces/${encodeURIComponent(workspaceId ?? "")}`,
      ),
    enabled: typeof workspaceId === "string" && workspaceId.length > 0,
  });
}

/**
 * 切换活动工作区(对应 `POST /api/v1/workspaces/{id}:switch`)。
 *
 * 成功后 invalidateQueries(['workspaces'])触发列表重取,新的
 * `last_active_at` 会让目标工作区在前端推算下成为"当前活动"。
 */
export function useSwitchWorkspaceMutation() {
  const queryClient = useQueryClient();
  return useMutation<WorkspaceOut, Error, string>({
    mutationFn: (workspaceId) =>
      apiPost<WorkspaceOut>(
        `/workspaces/${encodeURIComponent(workspaceId)}:switch`,
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: WORKSPACES_QUERY_KEY });
    },
  });
}

/**
 * 创建工作区(对应 `POST /api/v1/workspaces`)。
 *
 * 成功后 invalidateQueries(['workspaces'])触发列表重取。UI 副作用
 * (关 Dialog / form.reset / showSuccessToast)由调用方处理,保持 hook 复用。
 *
 * `activate` 字段后端默认 true,P1 单机本地用户创建后立即想用,固定传 true
 * 不暴露给用户(对齐 CLI `novel workspace create` 直觉)。
 */
export function useCreateWorkspaceMutation() {
  const queryClient = useQueryClient();
  return useMutation<WorkspaceOut, Error, WorkspaceCreateIn>({
    mutationFn: (body) =>
      apiPost<WorkspaceOut, WorkspaceCreateIn>("/workspaces", body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: WORKSPACES_QUERY_KEY });
    },
  });
}

/**
 * 归档工作区(对应 `POST /api/v1/workspaces/{id}:archive`)。
 *
 * 已归档(后端 `meta.status="archived"`)再次归档 → 后端 409
 * `WORKSPACE_ALREADY_ARCHIVED`,前端按"决策 4 archived row 禁用 :archive 按钮"
 * 兜住典型路径,异常路径走 toast。
 *
 * 成功后 invalidate ['workspaces'],status 切到 archived,row 视觉 dimmed。
 */
export function useArchiveWorkspaceMutation() {
  const queryClient = useQueryClient();
  return useMutation<WorkspaceArchiveResult, Error, string>({
    mutationFn: (workspaceId) =>
      apiPost<WorkspaceArchiveResult>(
        `/workspaces/${encodeURIComponent(workspaceId)}:archive`,
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: WORKSPACES_QUERY_KEY });
    },
  });
}

/**
 * 清理运行时数据(对应 `POST /api/v1/workspaces/{id}:clean`)。
 *
 * 后端删 `data/chroma/`、工作区内 `archives/`(prompts 归档,**非项目级
 * `archives/<id>.tar.gz`**)、`logs/`;**保留**正文 / 大纲 / 人设 / SQLite
 * db / `backups/`。语义"清理副产物",非破坏性,前端无需二次确认。
 */
export function useCleanWorkspaceMutation() {
  const queryClient = useQueryClient();
  return useMutation<WorkspaceCleanResult, Error, string>({
    mutationFn: (workspaceId) =>
      apiPost<WorkspaceCleanResult>(
        `/workspaces/${encodeURIComponent(workspaceId)}:clean`,
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: WORKSPACES_QUERY_KEY });
    },
  });
}

/**
 * 物理删除工作区(对应 `POST /api/v1/workspaces/{id}:purge`)。
 *
 * 后端硬要求 `body.confirm == workspace_id`(PROJECT_SPEC §9.3 二次确认),
 * 不匹配 → 422 `PURGE_CONFIRM_MISMATCH`(注意:**非** `WORKSPACE_PURGE_*`,
 * NEXT_STEPS / p1_plan 字面前缀过时)。前端 PurgeConfirmDialog 预校验
 * 即时反馈 + 后端兜底。
 */
export function usePurgeWorkspaceMutation() {
  const queryClient = useQueryClient();
  return useMutation<
    WorkspacePurgeResult,
    Error,
    { workspaceId: string; confirm: string }
  >({
    mutationFn: ({ workspaceId, confirm }) =>
      apiPost<WorkspacePurgeResult, WorkspacePurgeIn>(
        `/workspaces/${encodeURIComponent(workspaceId)}:purge`,
        { confirm },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: WORKSPACES_QUERY_KEY });
    },
  });
}

/**
 * 完全销毁工作区(对应 `POST /api/v1/workspaces/{id}:destroy`,P7-B-07 起新增)。
 *
 * **三方全删**:工作区目录(含工作区内 backups/ + data/novel.db + chroma/ 等
 * 全部数据)+ 顶级 archives/<id>.tar.gz(若存在)。对比 `purge` 仅删工作区
 * 目录保留 backups 与顶级 archive,`destroy` 完全不可逆(无任何 restore 路径)。
 * 生产场景:写完一本书后想完全清磁盘释放空间;书已导出 epub/docx 等成品到
 * 工作区外。
 *
 * 后端硬要求 `body.confirm == workspace_id`(strict 二次确认,对齐
 * `PurgeConfirmMismatch` 同模式);不匹配 → 422
 * `WORKSPACE_DESTROY_CONFIRM_INVALID`;工作区目录不存在 → 404
 * `WORKSPACE_NOT_FOUND`。前端 DestroyConfirmDialog 预校验 confirm 字段即时
 * 反馈,后端兜底所有异常路径走 toast。
 *
 * 成功响应 `WorkspaceDestroyResult` 含 `deleted_paths`(实际删除的绝对路径
 * 列表,0-2 项)— 工作区目录必含,顶级 archive 若存在亦含。
 *
 * onSuccess invalidate `WORKSPACES_QUERY_KEY` 让工作区列表立即移除该项;
 * **不 invalidate backups list**(工作区已被销毁,backups 子查询永远空响应)。
 */
export function useDestroyWorkspaceMutation() {
  const queryClient = useQueryClient();
  return useMutation<
    WorkspaceDestroyResult,
    Error,
    { workspaceId: string; confirm: string }
  >({
    mutationFn: ({ workspaceId, confirm }) =>
      apiPost<WorkspaceDestroyResult, WorkspaceDestroyIn>(
        `/workspaces/${encodeURIComponent(workspaceId)}:destroy`,
        { confirm },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: WORKSPACES_QUERY_KEY });
    },
  });
}

/**
 * 立即手动备份工作区(对应 `POST /api/v1/workspaces/{id}:backup`,P6-B-07 起
 * .zip 模式)。
 *
 * 后端走 `BackupService.create_manual_backup`,落到工作区内
 * `backups/manual_<timestamp>.zip`(全量 db + outline + cards + volumes +
 * assets + chapters + meta.json)。原目录不动,纯添加,无需二次确认。
 * 响应 `BackupInfoOut` 新字段集(backup_id / size_bytes / schema_version 等)。
 *
 * onSuccess 后 invalidate `["workspaces", id, "backups"]` 同步备份列表(若
 * 调用方有 useBackupsListQuery mount)+ 顶部 ['workspaces'] 列表。
 */
export function useBackupWorkspaceMutation() {
  const queryClient = useQueryClient();
  return useMutation<BackupInfoOut, Error, string>({
    mutationFn: (workspaceId) =>
      apiPost<BackupInfoOut>(
        `/workspaces/${encodeURIComponent(workspaceId)}:backup`,
      ),
    onSuccess: (_data, workspaceId) => {
      void queryClient.invalidateQueries({ queryKey: WORKSPACES_QUERY_KEY });
      void queryClient.invalidateQueries({
        queryKey: BACKUPS_QUERY_KEY(workspaceId),
      });
    },
  });
}

/**
 * 列出工作区备份(对应 `GET /api/v1/workspaces/{id}/backups`,P6-B-07 起新增)。
 *
 * 返最近 30 份 auto + 全部 manual,按 created_at 倒序。**不含**内部
 * `pre_restore_*.zip`(后端 BackupService 过滤)。
 *
 * 留观:本任务 ExportPage 暂不在 UI 上消费(归档动作组只复用既有 backup /
 * archive / clean / purge 4 mutation),P6-acceptance 期可加备份历史面板;
 * hook 先就位避免 P6-F-04 / F-05 再补回。
 */
export function useBackupsListQuery(workspaceId: string | null | undefined) {
  const enabled = typeof workspaceId === "string" && workspaceId.length > 0;
  return useQuery<BackupListOut>({
    queryKey: BACKUPS_QUERY_KEY(workspaceId ?? ""),
    queryFn: () =>
      apiGet<BackupListOut>(
        `/workspaces/${encodeURIComponent(workspaceId ?? "")}/backups`,
      ),
    enabled,
  });
}

/**
 * 从备份恢复工作区(对应 `POST /api/v1/workspaces/{id}:restore`,P6-B-07 新语义)。
 *
 * **P7-F-01 重写**:原 `useRestoreWorkspaceMutation` 是 P1 期"从项目级
 * archives/<id>.tar.gz un-archive"模式(无 body),与 P6-B-07 引入的
 * `BackupService.restore_from_backup` 新语义(body `{ backup_id, confirm }`,
 * 从工作区内 backups/<backup_id>.zip 解压覆盖)字面冲突。决策 6=B 落地 —
 * 不补独立 `:unarchive` 端点,后端 BackupService.restore 已是 SSOT,前端
 * 改"从备份恢复 dialog"模式接入。原残破 hook 移除,改名
 * `useRestoreWorkspaceFromBackupMutation`,body `RestoreIn = { backup_id,
 * confirm }`,响应 `RestoreResultOut = { workspace_id, backup_id,
 * pre_restore_backup_id }`。
 *
 * 后端校验:`confirm` 必须等于 URL 中的 `workspace_id`(对齐
 * `PurgeConfirmMismatch` 同模式,restore 是覆盖性操作 strict 同款防呆);
 * `backup_id` 对应 zip 不存在 → 404 `BACKUP_NOT_FOUND`;zip 结构损坏 →
 * 422 `BACKUP_INVALID_FORMAT`;schema_version 不兼容 → 422
 * `RESTORE_VERSION_INCOMPATIBLE`。前端 dialog 预校验 confirm 字段即时反馈,
 * 后端兜底所有异常路径走 toast。
 *
 * 成功后 invalidate `['workspaces']`(刷新工作区列表 + 当前详情)+
 * `['workspaces', id, 'backups']`(覆盖会落 `pre_restore_<ts>.zip` 入 backups
 * 列表)。
 */
export function useRestoreWorkspaceFromBackupMutation() {
  const queryClient = useQueryClient();
  return useMutation<
    RestoreResultOut,
    Error,
    { workspaceId: string; backupId: string; confirm: string }
  >({
    mutationFn: ({ workspaceId, backupId, confirm }) =>
      apiPost<RestoreResultOut, RestoreIn>(
        `/workspaces/${encodeURIComponent(workspaceId)}:restore`,
        { backup_id: backupId, confirm },
      ),
    onSuccess: (_data, { workspaceId }) => {
      void queryClient.invalidateQueries({ queryKey: WORKSPACES_QUERY_KEY });
      void queryClient.invalidateQueries({
        queryKey: BACKUPS_QUERY_KEY(workspaceId),
      });
    },
  });
}

/**
 * 清空工作区全部备份(对应 `POST /api/v1/workspaces/{id}:purge-backups`,v1.1 w3-s8 F1)。
 *
 * 删 backups/ 下全部备份 zip(auto + manual + pre_restore),释放磁盘。
 * `prune` 只削超额 auto,manual 永不自动清 → 本入口补"全清"。**不可逆**。
 *
 * 后端校验:`confirm` 必须等于 URL 中的 `workspace_id`(对齐 restore / purge
 * 同款防呆);不匹配 → 422 `PURGE_BACKUPS_CONFIRM_MISMATCH`。前端
 * `PurgeConfirmDialog` 预校验 confirm 即时反馈,后端兜底。
 *
 * 成功后 invalidate `['workspaces', id, 'backups']`(备份列表回空)。
 */
export function usePurgeWorkspaceBackupsMutation() {
  const queryClient = useQueryClient();
  return useMutation<
    PurgeBackupsResultOut,
    Error,
    { workspaceId: string; confirm: string }
  >({
    mutationFn: ({ workspaceId, confirm }) =>
      apiPost<PurgeBackupsResultOut, PurgeBackupsIn>(
        `/workspaces/${encodeURIComponent(workspaceId)}:purge-backups`,
        { confirm },
      ),
    onSuccess: (_data, { workspaceId }) => {
      void queryClient.invalidateQueries({
        queryKey: BACKUPS_QUERY_KEY(workspaceId),
      });
    },
  });
}

