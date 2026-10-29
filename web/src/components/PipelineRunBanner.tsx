/**
 * PipelineRunBanner(v2 w1-s4 Run 面板停跑/中断横幅)。
 *
 * 复用 EscalationDetailsBanner 模式:纯展示组件(数据 + 回调由页面层进),
 * 不命中任何场景 → 返 null(零垂直占用)。覆盖三类场景:
 *
 * 1. **孤儿章/驳回卡停跑**(paused + pause_reason=chapter_escalated /
 *    card_rejected,w1-s1 失败策略 stop):amber 横幅 + 直达章节编辑页 +
 *    处置说明(人工解决后回本页点"继续")。孤儿章 = `current_chapter`
 *    (`_dispose_chapter_failure` stop_mode=pause 时停在 blocked 章)。
 * 2. **成本护栏停跑**(paused + pause_reason=cost_guard_exceeded,w1-s3):
 *    amber 横幅 + 白话续跑提示(次日成本日清零 / 调高限额后可点"继续")。
 * 3. **运行中断**(state=interrupted,w1-s2/s3):orange 横幅 + 按
 *    error_details.reason 白话翻译(watchdog 超时含章节/耗时明细)+
 *    一键续跑按钮 + reset-stale 一键自愈入口。
 */

import { Link } from "react-router-dom";

import {
  PIPELINE_INTERRUPT_REASON_LABELS,
  type PipelineStatusOut,
} from "@/api/pipeline";

interface PipelineRunBannerProps {
  status: PipelineStatusOut | undefined;
  /** "一键续跑"按钮回调(页面层 resume mutation)。 */
  onResume: () => void;
  /** "重置中断章"按钮回调(页面层 reset-stale mutation,w1-s2 章自愈)。 */
  onResetStale: () => void;
  /** 任一 pipeline mutation 进行中(按钮 disabled)。 */
  busy: boolean;
  /** reset-stale mutation 进行中(按钮文案 + disabled)。 */
  isResettingStale: boolean;
}

export function PipelineRunBanner(props: PipelineRunBannerProps) {
  const { status, onResume, onResetStale, busy, isResettingStale } = props;
  if (!status) return null;

  // 场景 1:孤儿章 / 驳回卡停跑(失败策略 stop 的产物)。
  if (
    status.state === "paused" &&
    (status.pause_reason === "chapter_escalated" ||
      status.pause_reason === "card_rejected")
  ) {
    const blockedChapterId = status.current_chapter;
    const isEscalated = status.pause_reason === "chapter_escalated";
    return (
      <section
        data-testid="pipeline-run-banner"
        data-banner-kind={status.pause_reason}
        className="rounded border border-amber-500/50 bg-amber-500/10 p-4 text-sm"
      >
        <div className="flex items-center gap-2 font-medium text-amber-700">
          <span>
            ⚠ 批量生成已停:
            {isEscalated
              ? "本章进入人工介入(孤儿章)"
              : "本章章节卡被驳回"}
          </span>
          {blockedChapterId && (
            <code className="rounded bg-amber-500/20 px-1.5 py-0.5 text-xs">
              {blockedChapterId}
            </code>
          )}
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          {isEscalated
            ? "该章写作中触发升级条件(必达事件不可达 / 重写预算耗尽等),需人工处置。"
            : "该章章节卡处于驳回状态,需先改卡通过审核。"}
          在章节页解决后,回到本页点「继续」从断点续跑(已完成的章不会重写)。
        </p>
        {blockedChapterId && (
          <div className="mt-3 flex flex-wrap gap-3 text-xs">
            <Link
              to={`/chapters/${encodeURIComponent(blockedChapterId)}`}
              className="rounded bg-amber-600 px-3 py-1.5 font-medium text-white hover:bg-amber-700"
              data-testid="banner-goto-chapter"
            >
              → 打开章节页处理
            </Link>
          </div>
        )}
      </section>
    );
  }

  // 场景 2:Run 级成本护栏停跑(w1-s3,可 resume 续跑)。
  if (
    status.state === "paused" &&
    status.pause_reason === "cost_guard_exceeded"
  ) {
    return (
      <section
        data-testid="pipeline-run-banner"
        data-banner-kind="cost_guard_exceeded"
        className="rounded border border-amber-500/50 bg-amber-500/10 p-4 text-sm"
      >
        <div className="font-medium text-amber-700">
          💰 成本护栏触发,批量生成已停
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          本工作区成本已达限额(config.yaml cost_guard 段)。次日成本日清零、
          或调高限额后,点「继续」从断点续跑;也可点「终止」放弃本次运行。
        </p>
        <div className="mt-3">
          <button
            type="button"
            data-testid="banner-resume-button"
            disabled={busy}
            onClick={onResume}
            className="rounded bg-amber-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-700 disabled:opacity-50"
          >
            继续(成本已回落时)
          </button>
        </div>
      </section>
    );
  }

  // 场景 3:运行中断(interrupted,可一键续跑 + reset-stale 自愈)。
  if (status.state === "interrupted") {
    const details = (status.error_details ?? {}) as {
      reason?: string;
      chapter_id?: string;
      elapsed_seconds?: number;
      timeout_seconds?: number;
    };
    const reasonLabel = details.reason
      ? PIPELINE_INTERRUPT_REASON_LABELS[details.reason] ?? details.reason
      : "运行被中断";
    return (
      <section
        data-testid="pipeline-run-banner"
        data-banner-kind="interrupted"
        className="rounded border border-orange-500/50 bg-orange-500/10 p-4 text-sm"
      >
        <div className="font-medium text-orange-700">
          ⏸ 运行已中断:{reasonLabel}
        </div>
        {details.reason === "chapter_watchdog_timeout" && (
          <p className="mt-1 text-xs text-orange-800" data-testid="banner-watchdog-details">
            超时章节 <code>{details.chapter_id ?? "?"}</code>:已耗时{" "}
            {details.elapsed_seconds ?? "?"} 秒,超过预算{" "}
            {details.timeout_seconds ?? "?"} 秒。
          </p>
        )}
        <p className="mt-2 text-xs text-muted-foreground">
          已完成的章节都在,可一键续跑从断点继续(已过审章不会重写)。
          若有章节卡在写作中间态(审稿中/重写中/修订中),先点「重置中断章」
          自愈翻回起草态,再续跑。
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          <button
            type="button"
            data-testid="banner-resume-button"
            disabled={busy}
            onClick={onResume}
            className="rounded bg-orange-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-orange-700 disabled:opacity-50"
          >
            一键续跑
          </button>
          <button
            type="button"
            data-testid="banner-reset-stale-button"
            disabled={busy || isResettingStale}
            onClick={onResetStale}
            className="rounded border border-orange-600 px-3 py-1.5 text-xs font-medium text-orange-700 hover:bg-orange-500/10 disabled:opacity-50"
          >
            {isResettingStale ? "正在重置…" : "重置中断章(自愈)"}
          </button>
        </div>
      </section>
    );
  }

  return null;
}
