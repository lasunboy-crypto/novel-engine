/**
 * PipelineSkippedSummary 防回退测(v2 W3.5 UI 缺口4)。
 *
 * 钉住:
 * 1. 无跳过章 → 不渲染(零垂直占用)
 * 2. 有跳过章 → 显眼汇总条 + 每条链到该章编辑页
 * 3. join 章节标题;列表未就位降级显示 ID
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { ChapterListItem } from "@/api/chapters";
import type { PipelineStatusOut } from "@/api/pipeline";

const _chapters: Partial<ChapterListItem>[] = [
  { chapter_id: "ch-0002", title: "夜探藏经阁", chapter_number: 2, status: "human_intervention" },
  { chapter_id: "ch-0005", title: "断崖遇险", chapter_number: 5, status: "human_intervention" },
];

vi.mock("@/api/chapters", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/api/chapters")>();
  return {
    ...original,
    useChapterListQuery: () => ({ data: _chapters }),
  };
});

import { PipelineSkippedSummary } from "@/components/PipelineSkippedSummary";

function _status(overrides: Partial<PipelineStatusOut>): PipelineStatusOut {
  return {
    run_id: "run_0123456789abcdef",
    workspace_id: "ws-demo",
    state: "running",
    scope: "range",
    chapter_ids: ["ch-0001", "ch-0002", "ch-0003", "ch-0004", "ch-0005"],
    current_chapter: "ch-0003",
    progress: 0.4,
    completed_chapter_ids: ["ch-0001"],
    profile_override: null,
    pause_reason: null,
    error_details: null,
    failure_policy: "skip",
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

function renderSummary(status: PipelineStatusOut | undefined) {
  render(
    <MemoryRouter>
      <PipelineSkippedSummary workspaceId="ws-demo" status={status} />
    </MemoryRouter>,
  );
}

describe("PipelineSkippedSummary(缺口4)", () => {
  it("无跳过章 → 不渲染", () => {
    renderSummary(_status({ skipped_chapter_ids: [] }));
    expect(screen.queryByTestId("pipeline-skipped-summary")).toBeNull();
  });

  it("status undefined → 不渲染", () => {
    renderSummary(undefined);
    expect(screen.queryByTestId("pipeline-skipped-summary")).toBeNull();
  });

  it("有跳过章 → 汇总条 + N 计数 + 每条链到章编辑页", () => {
    renderSummary(_status({ skipped_chapter_ids: ["ch-0002", "ch-0005"] }));
    const summary = screen.getByTestId("pipeline-skipped-summary");
    expect(summary).toHaveTextContent("本次跳过 2 章待处理");
    const rows = screen.getAllByTestId("pipeline-skipped-row");
    expect(rows).toHaveLength(2);
    // join 标题
    expect(rows[0]).toHaveTextContent("夜探藏经阁");
    expect(rows[1]).toHaveTextContent("断崖遇险");
    // 链到章编辑页
    const links = screen.getAllByTestId("pipeline-skipped-chapter-link");
    expect(links[0]).toHaveAttribute("href", "/chapters/ch-0002");
    expect(links[1]).toHaveAttribute("href", "/chapters/ch-0005");
  });
});
