/**
 * 角色 API hooks(P2-F-02 / P2-F-03 / P2-F-04)。
 *
 * 已含 hook:
 * - `useCharacterListQuery(workspaceId)` → `GET /api/v1/characters`(P2-B-14)
 * - `useCharacterDetailQuery(workspaceId, characterId)` → `GET /characters/{id}`
 * - `useUpdateCharacterMutation()` → `PUT /characters/{id}`(P2-F-03 VoiceProfile
 *   编辑器接入;PUT body 全量 Character,见 P2-B-14 §2 决策 3)
 * - `useCreateCharacterMutation()` → `POST /characters`(P2-F-04 新增)
 * - `useDeleteCharacterMutation()` → `DELETE /characters/{id}`(P2-F-04 删除)
 *
 * ## X-Workspace-Id 注入
 *
 * 角色路由要求 `X-Workspace-Id` header(P2-B-14 §2 决策 1 跨工作区视为 404)。
 * 与 chapters.ts 同模式,通过 `apiGet(path, { workspaceId })` 选项注入,
 * 调用方从 zustand 取活动工作区 ID 显式传入。
 *
 * ## queryKey 命名空间
 *
 * - 列表:`["characters", workspaceId]`
 * - 详情:`["character", workspaceId, characterId]`
 *
 * 不同前缀让 mutation invalidate 时按粒度分别命中:`POST` 影响列表;`PUT`
 * 影响列表 + 单条详情(`first_appearance_chapter` 等列字段可能改);`DELETE`
 * invalidate 列表 + `removeQueries` 单条详情(避免 stale cache 仍被读到)。
 *
 * ## `enabled` 短路
 *
 * 缺关键参(`workspaceId` / `characterId` 任一空)时 `enabled=false`,query
 * 不发,UI 状态保持 `isPending`。组件层做"参数齐全前不渲染数据栏"分支(对
 * 齐 ChaptersBoardPage 5 态分支套路)。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiDelete, apiGet, apiPost, apiPut } from "@/api/client";
import type { components } from "@/api/types";

export type CharacterOut = components["schemas"]["CharacterOut"];
export type Character = components["schemas"]["Character"];
export type CharacterDeleteResult =
  components["schemas"]["CharacterDeleteResult"];
export type VoiceProfile = components["schemas"]["VoiceProfile"];

export const CHARACTERS_QUERY_KEY = (workspaceId: string) =>
  ["characters", workspaceId] as const;

export const CHARACTER_DETAIL_QUERY_KEY = (
  workspaceId: string,
  characterId: string,
) => ["character", workspaceId, characterId] as const;

/**
 * 列出工作区内全部角色(对应 `GET /api/v1/characters`)。
 *
 * `workspaceId` 为 null/undefined/空字符串时 `enabled=false`,query 不发。
 *
 * 排序由后端 `character_service.list` 决定:按 `character_id` 升序(P2-B-14
 * 决策无显式排序约定,与 store 层一致字典序;前端不做二次排序)。
 */
export function useCharacterListQuery(workspaceId: string | null | undefined) {
  const enabled = typeof workspaceId === "string" && workspaceId.length > 0;
  return useQuery<CharacterOut[]>({
    queryKey: CHARACTERS_QUERY_KEY(workspaceId ?? ""),
    queryFn: () =>
      apiGet<CharacterOut[]>("/characters", {
        workspaceId: workspaceId as string,
      }),
    enabled,
  });
}

/**
 * 读单个角色(对应 `GET /api/v1/characters/{character_id}`)。
 *
 * 任一参为 null/undefined/空字符串 → `enabled=false`。
 *
 * 错误码语义(P2-B-14 §2 决策 1):
 * - 不存在 / 跨工作区 → 404 `CHARACTER_NOT_FOUND`(对方工作区存在不暴露)
 *
 * 调用方(`CharacterDetailDrawer`)按 isApiError 翻译提示。
 */
export function useCharacterDetailQuery(
  workspaceId: string | null | undefined,
  characterId: string | null | undefined,
) {
  const enabled =
    typeof workspaceId === "string" &&
    workspaceId.length > 0 &&
    typeof characterId === "string" &&
    characterId.length > 0;
  return useQuery<CharacterOut>({
    queryKey: CHARACTER_DETAIL_QUERY_KEY(
      workspaceId ?? "",
      characterId ?? "",
    ),
    queryFn: () =>
      apiGet<CharacterOut>(
        `/characters/${encodeURIComponent(characterId as string)}`,
        { workspaceId: workspaceId as string },
      ),
    enabled,
  });
}

export interface UpdateCharacterArgs {
  workspaceId: string;
  character: Character;
}

/**
 * 全量更新角色(对应 `PUT /api/v1/characters/{character_id}`)。
 *
 * body 是完整 `Character` schema(P2-B-14 §2 决策 3)。`character_id` 取自
 * `args.character.character_id`,URL 编码后拼路径;路由层校验
 * body.character_id ≠ URL → 422 `CHARACTER_ID_MISMATCH`(我们这边由调用方
 * 保证一致,不会触发)。
 *
 * 错误码语义(P2-B-14 §2 决策 1):
 * - 不存在 / 跨工作区 → 404 `CHARACTER_NOT_FOUND`(对方工作区存在不暴露)
 * - body.workspace_id ≠ header → 422 `CHARACTER_WORKSPACE_MISMATCH`
 * - 字段不合 schema → 422 `VALIDATION_FAILED`
 *
 * onSuccess invalidate 列表 + 单条详情(列字段可能改:`first_appearance_chapter`
 * 等)。
 */
export function useUpdateCharacterMutation() {
  const queryClient = useQueryClient();
  return useMutation<CharacterOut, Error, UpdateCharacterArgs>({
    mutationFn: ({ workspaceId, character }) =>
      apiPut<CharacterOut, Character>(
        `/characters/${encodeURIComponent(character.character_id)}`,
        character,
        { workspaceId },
      ),
    onSuccess: (_data, { workspaceId, character }) => {
      void queryClient.invalidateQueries({
        queryKey: CHARACTERS_QUERY_KEY(workspaceId),
      });
      void queryClient.invalidateQueries({
        queryKey: CHARACTER_DETAIL_QUERY_KEY(
          workspaceId,
          character.character_id,
        ),
      });
    },
  });
}

export interface CreateCharacterArgs {
  workspaceId: string;
  character: Character;
}

/**
 * 新建角色(对应 `POST /api/v1/characters`)。
 *
 * body 是完整 `Character` schema(P2-B-14 §2 决策 3 写操作 body 全量)。
 *
 * 错误码语义(P2-B-14 §2 决策 2):
 * - `character_id` 已存在 → 409 `CHARACTER_ALREADY_EXISTS`
 * - body.workspace_id ≠ header → 422 `CHARACTER_WORKSPACE_MISMATCH`
 * - 字段不合 schema → 422 `VALIDATION_FAILED`
 *
 * onSuccess invalidate 列表,新角色行立刻出现。
 */
export function useCreateCharacterMutation() {
  const queryClient = useQueryClient();
  return useMutation<CharacterOut, Error, CreateCharacterArgs>({
    mutationFn: ({ workspaceId, character }) =>
      apiPost<CharacterOut, Character>("/characters", character, {
        workspaceId,
      }),
    onSuccess: (_data, { workspaceId }) => {
      void queryClient.invalidateQueries({
        queryKey: CHARACTERS_QUERY_KEY(workspaceId),
      });
    },
  });
}

export interface DeleteCharacterArgs {
  workspaceId: string;
  characterId: string;
}

/**
 * 删除角色(对应 `DELETE /api/v1/characters/{character_id}`)。
 *
 * 错误码语义(P2-B-14 §2 决策 1):
 * - 不存在 / 跨工作区 → 404 `CHARACTER_NOT_FOUND`
 *
 * onSuccess invalidate 列表 + `removeQueries` 单条详情(避免 stale cache 被
 * 抽屉等读取到已删角色,触发 `CHARACTER_NOT_FOUND` 抖动)。删除时**不**清
 * Chroma 中 `entity_id=character_id` 的向量记忆——P2-B-14 §1 不做项 ⑤ 留 P3+
 * retriever 写入回填一同改造。
 */
export function useDeleteCharacterMutation() {
  const queryClient = useQueryClient();
  return useMutation<CharacterDeleteResult, Error, DeleteCharacterArgs>({
    mutationFn: ({ workspaceId, characterId }) =>
      apiDelete<CharacterDeleteResult>(
        `/characters/${encodeURIComponent(characterId)}`,
        { workspaceId },
      ),
    onSuccess: (_data, { workspaceId, characterId }) => {
      void queryClient.invalidateQueries({
        queryKey: CHARACTERS_QUERY_KEY(workspaceId),
      });
      queryClient.removeQueries({
        queryKey: CHARACTER_DETAIL_QUERY_KEY(workspaceId, characterId),
      });
    },
  });
}
