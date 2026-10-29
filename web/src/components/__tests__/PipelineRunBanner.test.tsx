/**
 * PipelineRunBanner 防回退测(v2 w1-s4 Run 面板停跑/中断横幅)。
 *
 * 钉住(plan §4 w1-s4 卡防回退护栏字面③"续跑按钮请求体"——组件层回调
 * 触发;空 body + X-Workspace-Id 的请求体由 useResumePipelineMutation /
 * useResetStaleChaptersMutation 既有实现承担):
 * 1. 孤儿章停跑(pause_reason=chapter_escalated)→ 横幅 + 直达章节页
 * 2. card_rejected → 横幅(文案区分)
 * 3. cost_guard_exceeded → 横幅 + 续跑按钮回调
 * 4. interrupted → 一键续跑 + reset-stale 自愈按钮回调 + watchdog 明细
 * 5. running / 普通 paused / undefined → 不渲染(零垂直占用)
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { PipelineStatusOut } from "@/api/pipeline";
import { PipelineRunBanner } from "@/components/PipelineRunBanner";

function _status(overrides: Partial<PipelineStatusOut>): PipelineStatusOut {
  return {
    run_id: "run_0123456789abcdef",
    workspace_id: "ws-demo",
    state: "paused",
    scope: "range",
    chapter_ids: ["ch-0001", "ch-0002", "ch-0003"],
    current_chapter: "ch-0002",
    progress: 0.33,
    completed_chapter_ids: ["ch-0001"],
    profile_override: null,
    pause_reason: null,
    error_details: null,
    failure_policy: "stop",
    skipped_chapter_ids: [],
    provider_call_cap: 1000,
    provider_call_count: 0,
    transport_retry_count: 0,
    pipeline_attempt_count: 0,
    upstream_generation_attempt_count: 0,
    attempt_usage_cost: {
      usage_known_attempts: 0,
      usage_unknown_attempts: 0,
      legacy_unclassified_attempts: 0,
      input_tokens_known: 0,
      output_tokens_known: 0,
      provider_request_id_count: 0,
      response_status_counts: {},
      configured_cost_basis_counts: {},
      configured_estimates: [],
      actual_billed: { status: "not_recorded", amount: null, currency: null },
    },
    ...overrides,
  };
}

function renderBanner(status: PipelineStatusOut | undefined) {
  const onResume = vi.fn();
  const onResetStale = vi.fn();
  render(
    <MemoryRouter>
      <PipelineRunBanner
        status={status}
        onResume={onResume}
        onResetStale={onResetStale}
        busy={false}
        isResettingStale={false}
      />
    </MemoryRouter>,
  );
  return { onResume, onResetStale };
}

describe("PipelineRunBanner 孤儿章/驳回卡停跑(w1-s1)", () => {
  it("chapter_escalated → 横幅 + 孤儿章 ID + 直达章节页链接", () => {
    renderBanner(_status({ pause_reason: "chapter_escalated" }));
    const banner = screen.getByTestId("pipeline-run-banner");
    expect(banner).toHaveAttribute("data-banner-kind", "chapter_escalated");
    expect(banner).toHaveTextContent("人工介入");
    expect(banner).toHaveTextContent("ch-0002");
    expect(screen.getByTestId("banner-goto-chapter")).toHaveAttribute(
      "href",
      "/chapters/ch-0002",
    );
  });

  it("card_rejected → 横幅文案区分(章节卡被驳回)", () => {
    renderBanner(_status({ pause_reason: "card_rejected" }));
    const banner = screen.getByTestId("pipeline-run-banner");
    expect(banner).toHaveAttribute("data-banner-kind", "card_rejected");
    expect(banner).toHaveTextContent("章节卡被驳回");
  });
});

describe("PipelineRunBanner 成本护栏停跑(w1-s3)", () => {
  it("cost_guard_exceeded → 横幅 + 续跑提示 + 续跑按钮回调", () => {
    const { onResume } = renderBanner(
      _status({ pause_reason: "cost_guard_exceeded" }),
    );
    const banner = screen.getByTestId("pipeline-run-banner");
    expect(banner).toHaveAttribute("data-banner-kind", "cost_guard_exceeded");
    expect(banner).toHaveTextContent("成本护栏触发");
    expect(banner).toHaveTextContent("次日成本日清零");
    fireEvent.click(screen.getByTestId("banner-resume-button"));
    expect(onResume).toHaveBeenCalledTimes(1);
  });
});

describe("PipelineRunBanner 运行中断(w1-s2/s3)", () => {
  it("interrupted → 一键续跑 + reset-stale 自愈按钮回调", () => {
    const { onResume, onResetStale } = renderBanner(
      _status({
        state: "interrupted",
        pause_reason: null,
        error_details: { reason: "engine_restarted_mid_run" },
      }),
    );
    const banner = screen.getByTestId("pipeline-run-banner");
    expect(banner).toHaveAttribute("data-banner-kind", "interrupted");
    expect(banner).toHaveTextContent("引擎中途重启");
    fireEvent.click(screen.getByTestId("banner-resume-button"));
    expect(onResume).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId("banner-reset-stale-button"));
    expect(onResetStale).toHaveBeenCalledTimes(1);
  });

  it("chapter_watchdog_timeout → 超时章节/耗时/预算明细(w1-s3)", () => {
    renderBanner(
      _status({
        state: "interrupted",
        pause_reason: null,
        error_details: {
          reason: "chapter_watchdog_timeout",
          chapter_id: "ch-0002",
          elapsed_seconds: 2500,
          timeout_seconds: 2400,
        },
      }),
    );
    const details = screen.getByTestId("banner-watchdog-details");
    expect(details).toHaveTextContent("ch-0002");
    expect(details).toHaveTextContent("2500");
    expect(details).toHaveTextContent("2400");
  });

  it("isResettingStale=true → 自愈按钮 disabled + 进行中文案", () => {
    render(
      <MemoryRouter>
        <PipelineRunBanner
          status={_status({
            state: "interrupted",
            error_details: { reason: "engine_restarted_mid_run" },
          })}
          onResume={vi.fn()}
          onResetStale={vi.fn()}
          busy={false}
          isResettingStale
        />
      </MemoryRouter>,
    );
    const btn = screen.getByTestId("banner-reset-stale-button");
    expect(btn).toBeDisabled();
    expect(btn).toHaveTextContent("正在重置");
  });
});

describe("PipelineRunBanner 不渲染场景", () => {
  it("running → 不渲染", () => {
    renderBanner(_status({ state: "running" }));
    expect(screen.queryByTestId("pipeline-run-banner")).toBeNull();
  });

  it("普通 paused(user_request)→ 不渲染(状态卡已覆盖)", () => {
    renderBanner(_status({ pause_reason: "user_request" }));
    expect(screen.queryByTestId("pipeline-run-banner")).toBeNull();
  });

  it("status=undefined → 不渲染", () => {
    renderBanner(undefined);
    expect(screen.queryByTestId("pipeline-run-banner")).toBeNull();
  });
});
