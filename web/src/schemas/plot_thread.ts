/**
 * 伏笔表单 Zod schema(P2-F-06 起)。
 *
 * 镜像后端 `PlotThread` Pydantic 模型(`src/core/models/plot_thread.py`)中
 * **用户可编辑**字段。基础字段对应 `PlotThreadFormDialog` 的 RHF +
 * zodResolver,`related_characters` 由 React state 管(TagInput 受控值组件,
 * 沿用 P2-F-04 §2 自决项 5 模式)。
 *
 * 校验范围(对齐 CLAUDE.md "Only validate at system boundaries"):
 *
 * - `thread_id`:non-empty + 长度 1-64;**不加** ID_PATTERN regex 字符集——
 *   后端 P2-B-15 沿用 P2-B-14 §1 不做项 ④,不强加字符集,前端不擅自收紧。
 * - `title` / `description` / `planted_chapter`:trim + min(1)(后端必填字
 *   符串,空字符串虽合 schema 但 UX 上无意义,前端兜住)。
 * - `status`:`Literal["planted","developing","resolved","abandoned"]`,与
 *   `PlotThread.status` 字面 enum 一致。
 * - `importance`:整数 1-5,与后端 `Query(ge=1,le=5)` / Pydantic `Field(ge=1,
 *   le=5)` 一致。RHF + native input[type=number] 拿到的是字符串,Zod 用
 *   `coerce.number()` 把 "3" 转 3。
 * - `planned_payoff` / `actual_payoff`:可空字符串,RHF 表单内一律字符串;
 *   提交前由 dialog 把 trim 后空串转 null(避免 zod transform 让 RHF
 *   `Input/Output` 类型不一致触发 resolver 推断报错)。
 *
 * 数组字段不在本 schema 内,由 dialog 内部 state 管:
 *
 * - `related_characters`:`string[]` 默认 `[]`,TagInput 受控
 *
 * 透传不暴露(由 chapter mutation / MemoryExtractor 自动同步,沿用
 * P2-F-04 §2 决策 2 同向):
 *
 * - `last_updated_chapter`:不进表单,提交时透传 `character?.last_updated_chapter`
 * - `created_at`:服务器管理(POST/PUT 透传旧值,后端忽略)
 */

import { z } from "zod";

export const PLOT_THREAD_STATUS_VALUES = [
  "planted",
  "developing",
  "resolved",
  "abandoned",
] as const;

// G2(v1.1 wave 4 session 2):伏笔类型分类 4 档,与 `PlotThread.thread_type`
// Literal 一致。
export const PLOT_THREAD_TYPE_VALUES = [
  "plot_event",
  "foreshadowing",
  "metaphor",
  "callback",
] as const;

export const plotThreadFormSchema = z.object({
  thread_id: z
    .string()
    .trim()
    .min(1, "伏笔 ID 不可为空")
    .max(64, "伏笔 ID 长度不可超过 64"),
  title: z.string().trim().min(1, "标题不可为空"),
  thread_type: z.enum(PLOT_THREAD_TYPE_VALUES),
  status: z.enum(PLOT_THREAD_STATUS_VALUES),
  importance: z.coerce
    .number()
    .int("重要性必须为整数")
    .min(1, "重要性最小 1")
    .max(5, "重要性最大 5"),
  planted_chapter: z.string().trim().min(1, "埋伏章不可为空"),
  planned_payoff: z.string(),
  actual_payoff: z.string(),
  description: z.string().trim().min(1, "描述不可为空"),
});

export type PlotThreadFormInput = z.input<typeof plotThreadFormSchema>;
export type PlotThreadFormOutput = z.output<typeof plotThreadFormSchema>;

/** 把表单字符串转后端可空字符串字段:trim 后空串 → null。 */
export function emptyStringToNull(value: string): string | null {
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}
