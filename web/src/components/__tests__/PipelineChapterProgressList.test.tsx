/**
 * PipelineChapterProgressList 防回退测(v2 w1-s4 Run 面板逐章进度)。
 *
 * 钉住(plan §4 w1-s4 卡防回退护栏字面②"进度列表渲染"):
 * 1. run 维度四态标记:completed / skipped / current / pending
 * 2. completed 优先于 current(paused 时 current=最后处理章可能已完成,
 *    不得显示"当前章"误读)
 * 3. join 章节列表:标题 + 章号 + ChapterStateBadge;缺信息降级显示 ID
 * 4. 无 active run(chapter_ids=[])→ 不渲染
 * 5. 每行直达章节编辑页链接
 *
 * useChapterListQuery → vi.mock(对齐 NewBookForm.test 手法,聚焦渲染)。
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { ChapterListItem } from "@/api/chapters";
import type { PipelineStatusOut } from "@/api/pipeline";

const _chapters: Partial<ChapterListItem>[] = [
  { chapter_id: "ch-0001", title: "初入山门", chapter_number: 1, status: "approved" },
  { chapter_id: "ch-0002", title: "夜探藏经阁", chapter_number: 2, status: "human_intervention" },
  { chapter_id: "ch-0003", title: "比武大会", chapter_number: 3, status: "drafting" },
];

vi.mock("@/api/chapters", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/api/chapters")>();
  return {
    ...original,
    useChapterListQuery: () => ({ data: _chapters }),
  };
});

import { PipelineChapterProgressList } from "@/components/PipelineChapterProgressList";

function _status(overrides: Partial<PipelineStatusOut>): PipelineStatusOut {
  return {
    run_id: "run_0123456789abcdef",
    workspace_id: "ws-demo",
    state: "running",
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

function renderList(status: PipelineStatusOut | undefined) {
  render(
    <MemoryRouter>
      <PipelineChapterProgressList workspaceId="ws-demo" status={status} />
    </MemoryRouter>,
  );
}

describe("PipelineChapterProgressList(w1-s4)", () => {
  it("四态标记:completed / current / pending 按 run 维度渲染", () => {
    renderList(_status({}));
    const rows = screen.getAllByTestId("pipeline-progress-row");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveAttribute("data-run-mark", "completed");
    expect(rows[1]).toHaveAttribute("data-run-mark", "current");
    expect(rows[2]).toHaveAttribute("data-run-mark", "pending");
  });

  it("skip 策略跳过章 → skipped 标记(w1-s1 skipped_chapter_ids)", () => {
    renderList(
      _status({
        skipped_chapter_ids: ["ch-0002"],
        current_chapter: "ch-0003",
        failure_policy: "skip",
      }),
    );
    const rows = screen.getAllByTestId("pipeline-progress-row");
    expect(rows[1]).toHaveAttribute("data-run-mark", "skipped");
    expect(rows[1]).toHaveTextContent("已跳过");
  });

  it("completed 优先于 current:paused 时最后处理章已完成不显示'当前章'", () => {
    renderList(
      _status({
        state: "paused",
        current_chapter: "ch-0001",
        completed_chapter_ids: ["ch-0001"],
      }),
    );
    const rows = screen.getAllByTestId("pipeline-progress-row");
    expect(rows[0]).toHaveAttribute("data-run-mark", "completed");
  });

  it("join 章节列表:标题 + 章号 + 章状态徽章", () => {
    renderList(_status({}));
    const rows = screen.getAllByTestId("pipeline-progress-row");
    expect(rows[0]).toHaveTextContent("第 1 章");
    expect(rows[0]).toHaveTextContent("初入山门");
    // ChapterStateBadge 渲染章自身 8 档状态
    expect(rows[0]).toHaveTextContent("已通过");
    expect(rows[1]).toHaveTextContent("人工介入");
  });

  it("每行直达章节编辑页链接", () => {
    renderList(_status({}));
    const links = screen.getAllByTestId("pipeline-progress-chapter-link");
    expect(links[0]).toHaveAttribute("href", "/chapters/ch-0001");
  });

  it("无 active run(chapter_ids=[])→ 不渲染", () => {
    renderList(
      _status({ run_id: null, state: "idle", chapter_ids: [] }),
    );
    expect(screen.queryByTestId("pipeline-chapter-progress")).toBeNull();
  });

  it("status=undefined(加载中)→ 不渲染", () => {
    renderList(undefined);
    expect(screen.queryByTestId("pipeline-chapter-progress")).toBeNull();
  });
});
