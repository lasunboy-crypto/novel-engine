/**
 * v1.1 w3-s7 H7 — StyleDriftPanel 测试。
 *
 * mock `useChapterStyleDriftQuery` 隔离面板渲染逻辑:
 * - loading / error / 空 / 单版本 / 多版本 5 态
 * - 多版本:图容器 + 每版本主导人称徽标(类别中文标签)
 * - 单版本:hint + 仍渲染 pov 徽标行
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

import { StyleDriftPanel } from "@/components/StyleDriftPanel";
import type { StyleFingerprintPoint } from "@/api/chapters";

const mockQuery = vi.fn();

vi.mock("@/api/chapters", async (importActual) => {
  const actual = await importActual<typeof import("@/api/chapters")>();
  return {
    ...actual,
    useChapterStyleDriftQuery: () => mockQuery(),
  };
});

function point(
  version_id: number,
  overrides: Partial<StyleFingerprintPoint> = {},
): StyleFingerprintPoint {
  return {
    version_id,
    creation_type: "rewrite",
    has_fingerprint: true,
    paragraph_length_mean: 80,
    sentence_length_mean: 20,
    simile_density: 0.1,
    exclamation_density: 0.05,
    dominant_pov: "third_person",
    ...overrides,
  };
}

function setData(points: StyleFingerprintPoint[]) {
  mockQuery.mockReturnValue({
    data: { chapter_id: "ch-0002", points },
    isPending: false,
    error: null,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("StyleDriftPanel", () => {
  it("loading 态显加载提示", () => {
    mockQuery.mockReturnValue({ data: undefined, isPending: true, error: null });
    render(<StyleDriftPanel workspaceId="ws" chapterId="ch-0002" />);
    expect(screen.getByTestId("style-drift-loading")).toBeInTheDocument();
  });

  it("error 态显错误信息", () => {
    mockQuery.mockReturnValue({
      data: undefined,
      isPending: false,
      error: new Error("boom"),
    });
    render(<StyleDriftPanel workspaceId="ws" chapterId="ch-0002" />);
    expect(screen.getByTestId("style-drift-error")).toHaveTextContent("boom");
  });

  it("空 points → 空态提示", () => {
    setData([]);
    render(<StyleDriftPanel workspaceId="ws" chapterId="ch-0002" />);
    expect(screen.getByTestId("style-drift-empty")).toBeInTheDocument();
  });

  it("单版本 → hint + pov 徽标行(无图)", () => {
    setData([point(1, { creation_type: "draft" })]);
    render(<StyleDriftPanel workspaceId="ws" chapterId="ch-0002" />);
    expect(screen.getByTestId("style-drift-single")).toBeInTheDocument();
    expect(screen.queryByTestId("style-drift-chart")).not.toBeInTheDocument();
    expect(screen.getByTestId("style-drift-pov-v1")).toHaveTextContent(
      "v1:第三人称",
    );
  });

  it("多版本 → 图容器 + 每版本人称徽标(漂移可见)", () => {
    setData([
      point(1, { creation_type: "draft", dominant_pov: "third_person" }),
      point(2, { dominant_pov: "first_person" }),
      point(3, {
        has_fingerprint: false,
        paragraph_length_mean: null,
        sentence_length_mean: null,
        simile_density: null,
        exclamation_density: null,
        dominant_pov: null,
      }),
    ]);
    render(<StyleDriftPanel workspaceId="ws" chapterId="ch-0002" />);
    expect(screen.getByTestId("style-drift-chart")).toBeInTheDocument();
    expect(screen.getByTestId("style-drift-pov-v1")).toHaveTextContent(
      "v1:第三人称",
    );
    expect(screen.getByTestId("style-drift-pov-v2")).toHaveTextContent(
      "v2:第一人称",
    );
    // 无指纹版本 → 占位 "—"
    expect(screen.getByTestId("style-drift-pov-v3")).toHaveTextContent("v3:—");
  });
});
