/**
 * 角色表单 Zod schema(P2-F-04 起)。
 *
 * 镜像后端 `Character` Pydantic 模型(`src/core/models/character.py`)中**用户
 * 可编辑**字段。基础字段对应 `CharacterFormDialog` 的 RHF + zodResolver,
 * `voice_profile` / 数组 / 字典字段由 React state 管(VoiceProfileEditor /
 * TagInput / KvEditor 受控值组件,不进 RHF 体系——P2-F-03 §2 自决项 2)。
 *
 * 校验范围(对齐 CLAUDE.md "Only validate at system boundaries"):
 *
 * - `character_id`:non-empty + 长度 1-64;**不加** ID_PATTERN regex 字符集
 *   ——后端 P2-B-14 §1 不做项 ④ 显式不要求 `Character.character_id` 字符集
 *   校验,前端若强加 regex 会与后端行为分裂(后端允许的字符前端拒)。
 * - `name` / `archetype` / `appearance` / `personality` / `background`:trim +
 *   min(1)(后端 `Character` 这五字段是 `str` 必填,默认空字符串也合 schema,
 *   但 UX 上"角色名为空" 是用户无意义,前端兜住)。
 *
 * 数组 / 字典 / VoiceProfile 字段不在本 schema 内,由 dialog 内部 state 管:
 *
 * - `aliases` / `goals_long_term` / `goals_short_term` / `abilities` /
 *   `secrets`:`string[]` 默认 `[]`,TagInput 受控
 * - `relationships`:`Record<string,string>` 默认 `{}`,KvEditor 受控
 * - `voice_profile`:`VoiceProfile` 默认空对象,VoiceProfileEditor 受控
 *   (语气 / 句式 / 自称必填,提交前自定义校验,沿用 P2-F-03 套路)
 */

import { z } from "zod";

export const characterFormSchema = z.object({
  character_id: z
    .string()
    .trim()
    .min(1, "角色 ID 不可为空")
    .max(64, "角色 ID 长度不可超过 64"),
  name: z.string().trim().min(1, "角色名不可为空"),
  archetype: z.string().trim().min(1, "原型不可为空"),
  appearance: z.string().trim().min(1, "外貌不可为空"),
  personality: z.string().trim().min(1, "性格不可为空"),
  background: z.string().trim().min(1, "背景不可为空"),
});

export type CharacterFormInput = z.input<typeof characterFormSchema>;
export type CharacterFormOutput = z.output<typeof characterFormSchema>;
