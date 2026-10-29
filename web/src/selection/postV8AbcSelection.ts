/**
 * POST-V8 ABC 三章选型活动的编译期 New Book 锁。
 *
 * 普通构建不设置两个 VITE 变量，本模块返回 null，既有六章 schema 与表单完全
 * 不变。选型构建必须同时给出固定 mode 和三候选之一；真正的活动授权仍由后端
 * 临时 selection token/config 校验，前端开关本身不授予任何 Provider 权限。
 */

import type { NewBookFormInput } from "@/schemas/new_book";

export const POST_V8_ABC_SELECTION_MODE =
  "post-v8-abc-selection-short-v1" as const;

export const POST_V8_ABC_SELECTION_CANDIDATES = [
  "hy3",
  "deepseek-v4-flash",
  "qwen3.8-27b",
] as const;

export type PostV8AbcSelectionCandidate =
  (typeof POST_V8_ABC_SELECTION_CANDIDATES)[number];

const PREMISE =
  "一座封闭的旧城档案馆停电后，一名修复师发现三份互相矛盾的失踪记录，必须在天亮前判断谁改写了档案。";

export interface PostV8AbcSelectionUiContract {
  mode: typeof POST_V8_ABC_SELECTION_MODE;
  candidate: PostV8AbcSelectionCandidate;
  values: NewBookFormInput;
}

function isCandidate(value: string): value is PostV8AbcSelectionCandidate {
  return (POST_V8_ABC_SELECTION_CANDIDATES as readonly string[]).includes(value);
}

export function productWorkspaceId(
  candidate: PostV8AbcSelectionCandidate,
): string {
  return `post-v8-abc-${candidate}-selection-v1`;
}

export function productDisplayName(
  candidate: PostV8AbcSelectionCandidate,
): string {
  return `POST-V8 ABC 三章选择活动 ${candidate}`;
}

export function buildPostV8AbcSelectionValues(
  candidate: PostV8AbcSelectionCandidate,
): NewBookFormInput {
  return {
    workspace_id: productWorkspaceId(candidate),
    display_name: productDisplayName(candidate),
    genre: "mystery",
    premise: PREMISE,
    total_volumes: 1,
    chapters_per_volume: 3,
    tension_template: "heros_journey",
    default_voice: "third_person",
    style_template: "dark_mystery",
  };
}

function resolveContract(): PostV8AbcSelectionUiContract | null {
  const mode = (import.meta.env.VITE_POST_V8_ABC_SELECTION_MODE ?? "").trim();
  const candidate = (
    import.meta.env.VITE_POST_V8_ABC_SELECTION_CANDIDATE ?? ""
  ).trim();
  if (!mode && !candidate) {
    return null;
  }
  if (mode !== POST_V8_ABC_SELECTION_MODE || !isCandidate(candidate)) {
    throw new Error(
      "POST-V8 ABC selection 前端配置不完整或候选非法，已拒绝启动",
    );
  }
  return {
    mode,
    candidate,
    values: buildPostV8AbcSelectionValues(candidate),
  };
}

export const POST_V8_ABC_SELECTION_UI = resolveContract();
