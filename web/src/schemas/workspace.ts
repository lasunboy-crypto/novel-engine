/**
 * 工作区表单 Zod schema(P1-F-08 起 / P2-F-01 加 workspace_id 字符集 regex)。
 *
 * 镜像后端 `WorkspaceCreateIn`(`src/api/schemas/workspaces.py:39`)字段约束,
 * 对齐 CLAUDE.md "Only validate at system boundaries":
 *
 * - `workspace_id` `regex(WORKSPACE_ID_PATTERN)` 镜像后端 `ID_PATTERN`
 *   (`src/core/models/__init__.py`)。字符集 `[a-zA-Z0-9_-]`,长度 1-64,
 *   防 path traversal(`..` / `/` / 空格 / 特殊符号一律拒绝)。pattern 已隐
 *   含 `min(1)`,无需再叠 `.min(1)`。`.trim()` 在 regex 之前,避免前后空格
 *   误判为长度合法但内容非法。
 * - 其余 3 个 string 字段保持 `.trim().min(1)`(后端 `min_length=1`)。
 * - `total_chapters` `coerce.number().int().nonnegative()`(后端 `int` + `ge=0`)
 *   - 空字符串 `""` 经 `Number("")` coerce 成 `0`,语义对齐"留空 = 未规划"
 *   - 非数字字符串 → NaN → number 类型校验失败
 *
 * 字段名走后端(`total_chapters` 而非任务模板字面 `target_chapters`),按
 * P1-F-07 沉淀 1"任务模板字面与 spec 冲突时按 spec / 后端 / types.ts 走"
 * 元规则(注:本处是字段命名差异,与 P1-F-07 路由方向冲突属同抽象层不同
 * 处置)。
 */

import { z } from "zod";

/** 镜像后端 `ID_PATTERN`(`src/core/models/__init__.py`),P2-B-05 落地。 */
export const WORKSPACE_ID_PATTERN = /^[a-zA-Z0-9_-]{1,64}$/;

export const createWorkspaceSchema = z.object({
  workspace_id: z
    .string()
    .trim()
    .regex(
      WORKSPACE_ID_PATTERN,
      "仅允许字母、数字、下划线、连字符,长度 1-64",
    ),
  display_name: z.string().trim().min(1, "显示名不可为空"),
  genre: z.string().trim().min(1, "题材不可为空"),
  premise: z.string().trim().min(1, "一句话设定不可为空"),
  total_chapters: z.coerce
    .number({ invalid_type_error: "请输入有效数字" })
    .int("必须是整数")
    .nonnegative("不可为负数"),
});

/** Zod parse 的"输入"类型(RHF 内部表单状态用)。 */
export type CreateWorkspaceFormInput = z.input<typeof createWorkspaceSchema>;

/** Zod parse 的"输出"类型(handleSubmit 回调拿到的数据 + 发给 API 的 body)。 */
export type CreateWorkspaceFormOutput = z.output<typeof createWorkspaceSchema>;
