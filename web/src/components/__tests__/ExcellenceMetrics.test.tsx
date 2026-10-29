/**
 * ExcellenceMetrics 防回退测(v1.1 wave 7 / B,§7 硬要求)。
 *
 * 钉住 4 件易回退的事:
 * 1. 4 个度量都渲染 + 值正确
 * 2. **score_formulaic 反极性**:高分 = 警示红(其余维度高分 = 绿)+ ⚠ 标记
 * 3. **不误导成影响判定**:必带"不影响通过判定"注脚(纯度量语义)
 * 4. None 处理:单字段 None 显示"—";4 个全 None → 整组件不渲染
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

import { ExcellenceMetrics } from "@/components/ExcellenceMetrics";

describe("ExcellenceMetrics", () => {
  it("渲染 4 个度量 + 值正确", () => {
    render(
      <ExcellenceMetrics
        review={{
          score_imagery: 8.5,
          score_interiority: 7.0,
          score_formulaic: 2.5,
          score_immersion: 9.0,
        }}
      />,
    );
    expect(screen.getByTestId("excellence-score-imagery")).toHaveTextContent(
      "8.50",
    );
    expect(
      screen.getByTestId("excellence-score-interiority"),
    ).toHaveTextContent("7.00");
    expect(screen.getByTestId("excellence-score-formulaic")).toHaveTextContent(
      "2.50",
    );
    expect(screen.getByTestId("excellence-score-immersion")).toHaveTextContent(
      "9.00",
    );
  });

  it("带'不影响通过判定'注脚(纯度量语义,别误导)", () => {
    render(
      <ExcellenceMetrics review={{ score_imagery: 8.0 }} />,
    );
    expect(screen.getByTestId("excellence-metrics-note")).toHaveTextContent(
      "不影响通过判定",
    );
  });

  it("score_formulaic 反极性:高分 = 警示红 + ⚠ 标记", () => {
    render(<ExcellenceMetrics review={{ score_formulaic: 8.0 }} />);
    // 高套路化 = 差 → 红
    expect(screen.getByTestId("excellence-score-formulaic").className).toContain(
      "text-rose-700",
    );
    // 反极性 ⚠ 标记存在
    expect(screen.getByTestId("excellence-reverse-marker")).toBeInTheDocument();
  });

  it("score_formulaic 低分 = 绿(越低越好)", () => {
    render(<ExcellenceMetrics review={{ score_formulaic: 2.0 }} />);
    expect(screen.getByTestId("excellence-score-formulaic").className).toContain(
      "text-emerald-700",
    );
  });

  it("正极性维度高分 = 绿,且只有 formulaic 一个维度带反极性 ⚠ 标记", () => {
    render(
      <ExcellenceMetrics
        review={{
          score_imagery: 9.0,
          score_interiority: 9.0,
          score_formulaic: 2.0,
          score_immersion: 9.0,
        }}
      />,
    );
    // imagery 高 = 好 → 绿(与 formulaic 高=红 相反,钉住极性区别)
    expect(screen.getByTestId("excellence-score-imagery").className).toContain(
      "text-emerald-700",
    );
    // 4 维度里只有 formulaic 带反极性 ⚠ 标记(其余 3 个正极性无标记)
    expect(screen.getAllByTestId("excellence-reverse-marker")).toHaveLength(1);
  });

  it("单字段 None 显示'—'", () => {
    render(
      <ExcellenceMetrics
        review={{
          score_imagery: 8.0,
          score_interiority: null,
          score_formulaic: null,
          score_immersion: null,
        }}
      />,
    );
    expect(
      screen.getByTestId("excellence-score-interiority"),
    ).toHaveTextContent("—");
    expect(screen.getByTestId("excellence-score-imagery")).toHaveTextContent(
      "8.00",
    );
  });

  it("4 个全 None → 整组件不渲染(旧 review 不占空间)", () => {
    render(
      <ExcellenceMetrics
        review={{
          score_imagery: null,
          score_interiority: null,
          score_formulaic: null,
          score_immersion: null,
        }}
      />,
    );
    expect(screen.queryByTestId("excellence-metrics")).toBeNull();
  });

  it("4 个全缺省(undefined)→ 不渲染", () => {
    render(<ExcellenceMetrics review={{}} />);
    expect(screen.queryByTestId("excellence-metrics")).toBeNull();
  });
});
