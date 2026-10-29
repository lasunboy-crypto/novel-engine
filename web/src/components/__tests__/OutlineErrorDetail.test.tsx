/**
 * OutlineErrorDetail 防回退测(v2 W3.5 UI 缺口1 + 缺口2):大纲错误结构化展示。
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

import { OutlineErrorDetail } from "@/components/OutlineErrorDetail";

describe("OutlineErrorDetail", () => {
  it("缺口1 一致性冲突:列每条矛盾 + 依据 + 红字高亮名字", () => {
    render(
      <OutlineErrorDetail
        error={{
          code: "OUTLINE_CONSISTENCY_CONFLICT",
          message: "跨段矛盾",
          details: {
            stage: "volume_plans",
            contradictions: [
              "顾崇山在 world 段被写成顾盛礼",
              "小陆与小纪被混为一人",
            ],
            coverage_gaps: [],
            evidence: "world 段与 characters 段名字互斥",
          },
        }}
        names={["顾崇山", "顾盛礼", "小陆", "小纪"]}
      />,
    );
    expect(screen.getByTestId("outline-error-consistency")).toBeInTheDocument();
    expect(screen.getAllByTestId("outline-contradiction")).toHaveLength(2);
    expect(screen.getByTestId("outline-contradictions")).toHaveTextContent(
      "顾盛礼",
    );
    // 名字红字高亮(至少角色名命中)。
    expect(screen.getAllByTestId("highlighted-name").length).toBeGreaterThan(0);
    expect(screen.getByText(/world 段与 characters 段名字互斥/)).toBeInTheDocument();
  });

  it("缺口2 arc 太短:列违规弧段(哪卷哪弧 + 章数)", () => {
    render(
      <OutlineErrorDetail
        error={{
          code: "OUTLINE_ARC_TOO_SHORT",
          message: "arc 太短",
          details: {
            violations: [
              {
                volume_id: "v02",
                arc_id: "arc-3",
                arc_type: "dungeon",
                chapter_range: "5-6",
                span_length: 2,
              },
            ],
          },
        }}
      />,
    );
    expect(
      screen.getByTestId("outline-error-structure-gate"),
    ).toBeInTheDocument();
    const violation = screen.getByTestId("outline-violation");
    expect(violation).toHaveTextContent("arc-3");
    expect(violation).toHaveTextContent("仅 2 章");
  });

  it("缺口2 卷界重演:列前后章 + 依据", () => {
    render(
      <OutlineErrorDetail
        error={{
          code: "OUTLINE_VOLUME_SEAM_REPLAY",
          message: "卷界重演",
          details: {
            violations: [
              {
                prev_chapter_id: "ch-0012",
                next_chapter_id: "ch-0013",
                confidence: 0.88,
                evidence: "新卷首章重演了前卷决战",
              },
            ],
          },
        }}
      />,
    );
    const violation = screen.getByTestId("outline-violation");
    expect(violation).toHaveTextContent("ch-0012");
    expect(violation).toHaveTextContent("ch-0013");
    expect(violation).toHaveTextContent("88%");
    expect(violation).toHaveTextContent("新卷首章重演了前卷决战");
  });

  it("OUTLINE_BUILD_FAILED:提示哪段失败", () => {
    render(
      <OutlineErrorDetail
        error={{
          code: "OUTLINE_BUILD_FAILED",
          message: "mini_plan JSON 非法",
          details: { stage: "mini_plan", model_used: "deepseek-v4-pro" },
        }}
      />,
    );
    expect(screen.getByTestId("outline-error-build-failed")).toHaveTextContent(
      "mini_plan",
    );
  });

  it("未知 code → 兜底单行 message", () => {
    render(
      <OutlineErrorDetail
        error={{ code: "INTERNAL_ERROR", message: "未知错误" }}
      />,
    );
    expect(screen.getByTestId("outline-error-generic")).toHaveTextContent(
      "未知错误",
    );
  });
});
