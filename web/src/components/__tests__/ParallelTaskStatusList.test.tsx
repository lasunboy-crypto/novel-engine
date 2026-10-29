import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ParallelTaskStatusList } from "@/components/ParallelTaskStatusList";
import { parseParallelTaskSnapshot } from "@/lib/parallelTaskStatus";

describe("ParallelTaskStatusList", () => {
  it("按后端四态原样展示双 Judge，不把技术 fail-open 画成成功", () => {
    const snapshot = parseParallelTaskSnapshot(
      {
        parallel_group: "chapter_card_semantic_gates",
        chapter_id: "ch-0003",
        max_concurrency: 2,
        tasks: [
          {
            subtask_id: "card:setting",
            judge: "setting",
            status: "succeeded",
          },
          {
            subtask_id: "card:must",
            judge: "must_vs_forbidden",
            status: "failed",
            reason: "technical_fail_open",
          },
        ],
      },
      "chapter_card_semantic_gates",
    );
    expect(snapshot).not.toBeNull();

    render(
      <ParallelTaskStatusList
        title="章节卡写前双 Judge"
        snapshot={snapshot!}
        testId="parallel-list"
      />,
    );

    expect(screen.getByText("世界设定 Judge")).toBeInTheDocument();
    expect(screen.getByText("必达/禁止 Judge")).toBeInTheDocument();
    expect(screen.getByText("成功")).toBeInTheDocument();
    expect(screen.getByText("失败")).toBeInTheDocument();
    expect(
      screen.getByTitle("技术失败，按既有降级规则继续"),
    ).toBeInTheDocument();
  });

  it("拒绝未知状态，避免 UI 猜测终态", () => {
    expect(
      parseParallelTaskSnapshot(
        {
          parallel_group: "mini_plan_beat_alignment",
          max_concurrency: 4,
          tasks: [{ subtask_id: "x", status: "completed-ish" }],
        },
        "mini_plan_beat_alignment",
      ),
    ).toBeNull();
  });
});
