/**
 * NewBookPage 表单 Zod schema(P5-F-02)。
 *
 * 字段对齐:
 * - 工作区元数据:`WorkspaceCreateIn`(`workspace_id` / `display_name` /
 *   `genre` / `premise` / `total_chapters` / `activate`)
 * - outline 生成入参:`OutlineGenerateIn`(`premise` / `total_volumes` /
 *   `chapters_per_volume` / `tension_template` / `genre`)
 *
 * 类 A 处置(plan §P5-F-02 字面 vs 后端规格源,按规格源走):
 * - **Writer Profile 默认选择**:`WorkspaceCreateIn` / `OutlineGenerateIn` 均
 *   无 writer_profile 字段,无后端承接 — 不暴露(写章节阶段才有 profile_override)
 * - **单章字数范围**:`WorkspaceCreateIn` 无对应字段 — 不暴露
 * - **风格指南上传槽**:`assets:import` 仅接 world_md_content / characters_md_content
 *   两字段,无 style_guide — 不暴露(详 docs/impl/p5_f_02_*.md §4)
 *
 * 提交链路:workspace_id 创建走 `POST /workspaces`(`total_chapters = total_volumes
 * × chapters_per_volume` 计算)→ 资产文件可选上传 `POST /assets:import` → outline
 * 生成 `POST /workspaces/{id}/outline:generate`。
 */

import { z } from "zod";
import type { OutlineGenerateIn } from "@/api/outline";
import { WORKSPACE_ID_PATTERN } from "@/schemas/workspace";

export const PLANNING_MODE_OPTIONS = [
  { value: "whole_book", label: "完整故事" },
  { value: "opening_segment", label: "长篇开局" },
] as const satisfies ReadonlyArray<{ value: NonNullable<OutlineGenerateIn["planning_mode"]>; label: string }>;

/** 张力曲线模板 4 档(对齐 `OutlineGenerateIn.tension_template`)。 */
export const TENSION_TEMPLATE_OPTIONS = [
  { value: "heros_journey", label: "英雄之旅" },
  { value: "steady_daily", label: "平稳日常" },
  { value: "strong_stimulation", label: "强刺激爽文" },
  { value: "free", label: "自由(均匀)" },
] as const;

export type TensionTemplateValue =
  (typeof TENSION_TEMPLATE_OPTIONS)[number]["value"];

/**
 * 默认人称视角 3 档(对齐后端 `WorkspaceCreateIn.default_voice` +
 * `WorkspaceORM.default_voice` 枚举,v1.1 wave 3 s4 / L2)。
 *
 * - `third_person` = 项目级硬约束默认(CLAUDE.md §核心硬约束第 11 条,中文长篇优先)
 * - `first_person` = 第一人称(用户显式选)
 * - `free` = 关闭 G10 跨章节视角硬约束,只 warn log(用户选 / 实验)
 *
 * label 用"全称解释版"(维护人 wave 3 s4 拍板),`free` 旁另有 warn 提示。
 */
export const DEFAULT_VOICE_OPTIONS = [
  { value: "third_person", label: "第三人称视角(全书统一第三人称)" },
  { value: "first_person", label: "第一人称视角(全书统一第一人称)" },
  { value: "free", label: "自由视角(允许跨章节切换)" },
] as const;

export type DefaultVoiceValue =
  (typeof DEFAULT_VOICE_OPTIONS)[number]["value"];

/**
 * 写作风格模板下拉候选(v1.1 wave 4 s3 / I1)。空串 `""` = 不使用模板
 * (后端 `WorkspaceCreateIn.style_template` 传 null,Writer 退化为既有隐式
 * 风格控制)。7 档预设 value 对齐后端 `StyleTemplateName` 枚举;label / 维度
 * 元数据 / 正文预览由 `GET /style-templates` 端点提供(NewBookForm 选中后预览)。
 */
export const STYLE_TEMPLATE_NONE = "" as const;

export const STYLE_TEMPLATE_OPTIONS = [
  { value: STYLE_TEMPLATE_NONE, label: "不使用模板(由前后文自然形成风格)" },
  { value: "comedic_burst", label: "搞笑爽文" },
  { value: "grand_epic", label: "宏大叙事" },
  { value: "heroic_battle", label: "热血战斗" },
  { value: "dark_mystery", label: "暗黑悬疑" },
  { value: "healing_daily", label: "治愈日常" },
  { value: "classical_xianxia", label: "古典仙侠" },
  { value: "xianxia_growth", label: "修仙成长" },
] as const;

export type StyleTemplateValue =
  (typeof STYLE_TEMPLATE_OPTIONS)[number]["value"];

/**
 * 流派下拉候选(对齐 FRONTEND_SPEC §4.2:"玄幻/科幻/都市/历史/悬疑/言情/武侠/...,可自定义")。
 * value 用 snake_case 英文(后端 genre 字段无强约束,UI 中文展示)。
 */
export const GENRE_OPTIONS = [
  { value: "xianxia", label: "仙侠" },
  { value: "xuanhuan", label: "玄幻" },
  { value: "scifi", label: "科幻" },
  { value: "urban", label: "都市" },
  { value: "history", label: "历史" },
  { value: "mystery", label: "悬疑" },
  { value: "romance", label: "言情" },
  { value: "wuxia", label: "武侠" },
] as const;

export const newBookSchema = z.object({
  planning_mode: z.enum(["whole_book", "opening_segment"]).optional(),
  workspace_id: z
    .string()
    .trim()
    .regex(
      WORKSPACE_ID_PATTERN,
      "仅允许字母、数字、下划线、连字符,长度 1-64",
    ),
  display_name: z.string().trim().min(1, "显示名不可为空"),
  genre: z.string().trim().min(1, "题材不可为空"),
  premise: z
    .string()
    .trim()
    .min(1, "一句话设定不可为空")
    .max(500, "一句话设定不可超过 500 字"),
  total_volumes: z.coerce
    .number({ invalid_type_error: "请输入有效数字" })
    .int("必须是整数")
    .min(1, "目标卷数至少为 1")
    .max(20, "目标卷数最多 20"),
  // P6-F-02 摊入 P5 候选 2:每卷至少 6 章(后端 OutlineBuilder 生成
  // volume_plans 时,每卷需切 ≥ 2 arc + 后端 outline:commit [HARD] gate
  // 要求每个副本类 arc ≥ 3 章 → 2 × 3 = 6 章最小阈值)。**类 B 处置**:
  // 启动指令字面 `Volume.arcs min_length=2` 在 NewBookPage 这一层不可直接
  // 校验(NewBookForm 没有 arcs 编辑器,Volume.arcs 是后端 OutlineBuilder
  // 输出),按工程实情 + plan §决策字面"每卷至少 6 章"提前 client-side
  // reject,避免后端 outline:commit 阶段才 422 `OUTLINE_ARC_TOO_SHORT`
  // 浪费一轮 LLM 生成。
  chapters_per_volume: z.coerce
    .number({ invalid_type_error: "请输入有效数字" })
    .int("必须是整数")
    .min(6, "每卷至少 6 章(每卷 ≥ 2 arc × 每 arc ≥ 3 章)")
    .max(200, "每卷章数最多 200"),
  tension_template: z.enum([
    "heros_journey",
    "steady_daily",
    "strong_stimulation",
    "free",
  ]),
  // L2(v1.1 wave 3 s4):默认人称视角,后端 WorkspaceCreateIn.default_voice
  // 3 档枚举同源;'free' 关闭 G10 跨章节视角硬约束(NewBookForm 加 warn 提示)。
  default_voice: z.enum(["third_person", "first_person", "free"]),
  // I1(v1.1 wave 4 s3):写作风格模板。`""` = 不使用(提交时转 null);7 档对齐
  // 后端 StyleTemplateName。选中后据模板生成工作区 prompts/style_guide.md。
  style_template: z.enum([
    STYLE_TEMPLATE_NONE,
    "comedic_burst",
    "grand_epic",
    "heroic_battle",
    "dark_mystery",
    "healing_daily",
    "classical_xianxia",
    "xianxia_growth",
  ]),
});

/**
 * 只供带临时 selection token/config 的独立选型前端构建使用。
 * `newBookSchema` 本身保持每卷至少 6 章，普通 New Book 合同零变化。
 */
export function buildPostV8AbcSelectionBookSchema(expected: {
  workspace_id: string;
  display_name: string;
  genre: string;
  premise: string;
  total_volumes: number;
  chapters_per_volume: number;
  tension_template: string;
  default_voice: string;
  style_template: string;
}) {
  const selectionBase = newBookSchema.extend({
    workspace_id: z
      .string()
      .trim()
      .refine(
        (value) => value === expected.workspace_id,
        "选型活动 workspace ID 必须逐字等于冻结值",
      ),
    chapters_per_volume: z.coerce
      .number({ invalid_type_error: "请输入有效数字" })
      .int("必须是整数")
      .refine((value) => value === 3, "选型活动固定为每卷 3 章"),
  });
  return selectionBase.superRefine((value, ctx) => {
    for (const [field, expectedValue] of Object.entries(expected)) {
      const actual = value[field as keyof typeof value];
      if (actual !== expectedValue) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: [field],
          message: "选型活动字段已冻结，不允许修改",
        });
      }
    }
  });
}

export type NewBookFormInput = z.input<typeof newBookSchema>;
export type NewBookFormOutput = z.output<typeof newBookSchema>;
