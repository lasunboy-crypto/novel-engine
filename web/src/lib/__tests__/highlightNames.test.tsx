/**
 * highlightNames 防回退测(v2 W3.5 UI 缺口1/2):名字子串高亮。
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

import { highlightNames } from "@/lib/highlightNames";

describe("highlightNames", () => {
  it("命中名字 → 包 mark(红字)", () => {
    render(<p>{highlightNames("顾崇山在 world 段被写成顾盛礼", ["顾崇山", "顾盛礼"])}</p>);
    const marks = screen.getAllByTestId("highlighted-name");
    expect(marks).toHaveLength(2);
    expect(marks[0]).toHaveTextContent("顾崇山");
    expect(marks[1]).toHaveTextContent("顾盛礼");
  });

  it("长名优先(小陆 不被 陆 抢匹配)", () => {
    render(<p>{highlightNames("小陆与小纪混为一人", ["小陆", "小纪", "陆"])}</p>);
    const marks = screen.getAllByTestId("highlighted-name");
    const texts = marks.map((m) => m.textContent);
    expect(texts).toContain("小陆");
    expect(texts).toContain("小纪");
  });

  it("空名字列表 → 原样返回(无 mark)", () => {
    render(<p data-testid="wrap">{highlightNames("无任何名字", [])}</p>);
    expect(screen.queryByTestId("highlighted-name")).toBeNull();
    expect(screen.getByTestId("wrap")).toHaveTextContent("无任何名字");
  });

  it("名字不在文本中 → 不高亮", () => {
    render(<p data-testid="wrap">{highlightNames("一段普通文字", ["张三"])}</p>);
    expect(screen.queryByTestId("highlighted-name")).toBeNull();
  });

  it("正则特殊字符名字不报错", () => {
    render(<p>{highlightNames("代号 A.B 出现矛盾", ["A.B"])}</p>);
    expect(screen.getByTestId("highlighted-name")).toHaveTextContent("A.B");
  });
});
