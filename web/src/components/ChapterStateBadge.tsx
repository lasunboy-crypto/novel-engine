/**
 * ChapterStateBadge(P1-F-11 起 / P2-F-01 合并升 7 档)。
 *
 * 章节状态徽标的**唯一**组件。同时承载两类来源:
 *
 * | 来源 | 取值 | nullable |
 * |------|------|---------|
 * | `GET /chapters` 列表项 `status`(P2-B-17 起 7 档) | `ChapterListItem["status"]` | 非 null |
 * | WS `state_transition` 事件 `to`(7 档 ChapterState.value) | string | 进入页未观测前 null |
 *
 * P1 阶段曾有两套并存:
 *
 * - `ChapterStateBadge`(P1-F-11):3 档 fs 推断(`approved` / `not_started` /
 *   `unknown`)
 * - `LiveChapterStateBadge`(P1-F-15):7 档 ChapterState
 *
 * P2-B-17 把后端 `chapters.status` 升 7 档落库后,list 端点 status 与 WS
 * state_transition.to 枚举空间一致,P2-F-01 合并为本组件单一真相,删除
 * `LiveChapterStateBadge.tsx`。`v2_wishlist.md` "LiveChapterStateBadge /
 * ChapterStateBadge 合并" 段已结。
 *
 * ## 8 档颜色映射(对齐 `src/core/state_machine/states.py::ChapterState`,P6-B-01 扩 `invalidated`)
 *
 * | status | 颜色 | 中文标签 |
 * |--------|------|---------|
 * | drafting | sky 蓝 | 起草中 |
 * | reviewing | amber 黄 | 审稿中 |
 * | rewriting | indigo 蓝紫 | 重写中 |
 * | revising | violet 紫 | 修订中 |
 * | approved | emerald 绿 | 已通过 |
 * | published | emerald 绿(更深) | 已发布 |
 * | human_intervention | rose 红 | 人工介入 |
 * | invalidated | orange 橙 | 已失效 |
 *
 * `invalidated`(P6-B-01 / P6-F-06)= rewind 上游章节后被标废的下游章节,需
 * 显式 `:unblock` 才能恢复 :write;与 human_intervention(待人工修复)语义
 * 不同 — 故选 orange(警告色)与 rose(干预色)区分。
 *
 * `status` 为 null / 8 档外字符串(防御后端加新档前端先报)→ 灰"等待事件"
 * 兜底。WS 来源进入编辑器页后、首次 transition 到达前显示此灰兜底(对齐
 * P1-F-15 待审 1 拍 A,不拉 `GET /chapters/{id}/status` 做 baseline)。
 *
 * ## prop 名定 `status`(对齐 PROJECT_SPEC §3.5 + P2-B-17 决策 1)
 *
 * P1-F-15 LiveChapterStateBadge 用 prop `state`,P2-F-01 合并时统一改 `status`,
 * 对齐 PROJECT_SPEC §3.5 ``Chapter.status`` 字段名 + P2-B-17 拍板"不双字段
 * 并存"。ChapterEditorPage 内部变量 `lastChapterState` 保留(语义"最后观
 * 测到的状态",事件式),JSX prop 上传时映射到 `status`。
 */

import { cn } from "@/lib/utils";

// B-04 hot-fix(2026-05-19):"published" 档移除前端 mapping,v1 内无任何
// caller 转 PUBLISHED,后端 state literal 暂留(删后端要动 6+ 文件 + 测试,
// 推 v2 真定义"发布"概念时启用)。chapter 若意外 status='published' 走
// FALLBACK 灰兜底(防御后端加新档前端先报)。
const STATUS_LABEL: Record<string, string> = {
  drafting: "起草中",
  reviewing: "审稿中",
  rewriting: "重写中",
  revising: "修订中",
  approved: "已通过",
  human_intervention: "人工介入",
  invalidated: "已失效",
};

const STATUS_BADGE_CLASS: Record<string, string> = {
  drafting: "bg-sky-100 text-sky-700",
  reviewing: "bg-amber-100 text-amber-700",
  rewriting: "bg-indigo-100 text-indigo-700",
  revising: "bg-violet-100 text-violet-700",
  approved: "bg-emerald-100 text-emerald-700",
  human_intervention: "bg-rose-100 text-rose-700",
  invalidated: "bg-orange-100 text-orange-700",
};

const FALLBACK_LABEL = "等待事件";
const FALLBACK_CLASS = "bg-zinc-100 text-zinc-500";

interface ChapterStateBadgeProps {
  /** 8 档 ChapterStatus(P6-B-01 起含 `invalidated`);null = 尚未观测到任何 state(WS 来源进入页初始态)。 */
  status: string | null;
  className?: string;
}

export function ChapterStateBadge(props: ChapterStateBadgeProps) {
  const known = props.status !== null && props.status in STATUS_LABEL;
  const label = known ? STATUS_LABEL[props.status as string] : FALLBACK_LABEL;
  const badgeClass = known
    ? STATUS_BADGE_CLASS[props.status as string]
    : FALLBACK_CLASS;

  return (
    <span
      data-testid="chapter-state-badge"
      data-status={props.status ?? "waiting"}
      className={cn(
        "whitespace-nowrap rounded px-1.5 py-0.5 text-xs font-medium",
        badgeClass,
        props.className,
      )}
    >
      {label}
    </span>
  );
}
