/**
 * CraftFocusView 防回退测(v1.1 wave 7 / B,§7)。
 *
 * 钉住 craft_focus(wave 5 s3)只读展示的非空 / 空 / 纯空白三态。
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

import { CraftFocusView } from "@/components/ChapterCardReviewPanel";

describe("CraftFocusView", () => {
  it("非空 craft_focus → 显示原文", () => {
    render(
      <CraftFocusView craftFocus="ch-3 高潮处深挖林晚内心;其余过场克制留白" />,
    );
    expect(screen.getByTestId("craft-focus-text")).toHaveTextContent(
      "ch-3 高潮处深挖林晚内心",
    );
    expect(screen.queryByTestId("craft-focus-empty")).toBeNull();
  });

  it("空 craft_focus → 显示空态提示(不留白屏)", () => {
    render(<CraftFocusView craftFocus="" />);
    expect(screen.getByTestId("craft-focus-empty")).toBeInTheDocument();
    expect(screen.queryByTestId("craft-focus-text")).toBeNull();
  });

  it("纯空白 craft_focus → 当作空(trim)", () => {
    // 用表达式形式传真换行(JSX 字符串属性不处理 \n 转义)
    render(<CraftFocusView craftFocus={"   \n  "} />);
    expect(screen.getByTestId("craft-focus-empty")).toBeInTheDocument();
  });
});
