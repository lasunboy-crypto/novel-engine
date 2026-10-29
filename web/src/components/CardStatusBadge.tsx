/**
 * CardStatusBadge(P5-F-06)— 章节卡 `card_status` 4 档徽章。
 *
 * 与 `ChapterStateBadge`(`Chapter.status` 7 档)**并列共存**(决策 6 拍 C
 * 双状态独立 — ChapterCard 生命周期独立于 Chapter 主体)。两徽章一起出现
 * 在 ChapterCardReviewPage 顶部、章节列表行等需要同时表达"章节进度"+
 * "章节卡审核进度"的场景。
 *
 * ## 4 档颜色映射(对齐后端 `CARD_STATUS_VALUES`)
 *
 * | card_status | 颜色 | 中文标签 |
 * |-------------|------|---------|
 * | draft | slate 灰 | 草稿 |
 * | pending_review | amber 黄 | 待审核 |
 * | approved | emerald 绿 | 已通过 |
 * | rejected | rose 红 | 已退回 |
 *
 * 未识别值 / null → 灰兜底"未知状态"。
 */

import { cn } from "@/lib/utils";

export type CardStatus =
  | "draft"
  | "pending_review"
  | "approved"
  | "rejected";

const STATUS_LABEL: Record<CardStatus, string> = {
  draft: "草稿",
  pending_review: "待审核",
  approved: "已通过",
  rejected: "已退回",
};

const STATUS_BADGE_CLASS: Record<CardStatus, string> = {
  draft: "bg-slate-100 text-slate-700",
  pending_review: "bg-amber-100 text-amber-700",
  approved: "bg-emerald-100 text-emerald-700",
  rejected: "bg-rose-100 text-rose-700",
};

const FALLBACK_LABEL = "未知状态";
const FALLBACK_CLASS = "bg-zinc-100 text-zinc-500";

interface CardStatusBadgeProps {
  status: CardStatus | string | null | undefined;
  className?: string;
}

export function CardStatusBadge(props: CardStatusBadgeProps) {
  const known =
    typeof props.status === "string" && props.status in STATUS_LABEL;
  const label = known
    ? STATUS_LABEL[props.status as CardStatus]
    : FALLBACK_LABEL;
  const badgeClass = known
    ? STATUS_BADGE_CLASS[props.status as CardStatus]
    : FALLBACK_CLASS;

  return (
    <span
      data-testid="card-status-badge"
      data-card-status={props.status ?? "unknown"}
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
