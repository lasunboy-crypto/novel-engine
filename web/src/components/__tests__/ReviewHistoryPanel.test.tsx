/**
 * W3.5-R6：持久化审稿历史直接展示 API 返回的稳定 regression ID。
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ReviewSummaryEntry } from "@/api/chapters";

const mockHistoryQuery = vi.fn();

vi.mock("@/api/chapters", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/api/chapters")>();
  return {
    ...original,
    useChapterReviewHistoryQuery: () => mockHistoryQuery(),
  };
});

import { ReviewHistoryPanel } from "@/components/ReviewHistoryPanel";

function review(
  overrides: Partial<ReviewSummaryEntry> = {},
): ReviewSummaryEntry {
  return {
    version_reviewed: 2,
    verdict: "rewrite",
    score_overall: 7,
    score_plot_progression: 7,
    score_tension_match: 7,
    score_prose_quality: 7,
    score_consistency: 7,
    score_hook_management: 7,
    score_voice_consistency: 7,
    hard_constraints_passed: false,
    revise_targets: [],
    ...overrides,
  };
}

describe("ReviewHistoryPanel W3.5-R6 regression ID", () => {
  it("展示后端稳定 ID，不从审稿中文文案反解析身份", () => {
    const regressionId = "b".repeat(64);
    mockHistoryQuery.mockReturnValue({
      data: [review({ hard_constraint_regressions: [regressionId] })],
      isPending: false,
      fetchStatus: "idle",
      error: null,
    });

    render(
      <ReviewHistoryPanel
        workspaceId="ws-r6"
        chapterId="ch-0006"
        selectedIndex={null}
        onSelectIndex={vi.fn()}
        onSummaryChange={vi.fn()}
      />,
    );

    expect(screen.getByTestId("review-hard-regressions")).toHaveTextContent(
      regressionId,
    );
  });
});
