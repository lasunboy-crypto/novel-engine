/**
 * NewBookForm 表单测试。
 *
 * - v1.1 wave 3 s4 / L2:默认人称视角 radio(3 档 + free warn + 透传)
 * - v1.1 wave 4 s3 / I1:写作风格模板下拉(7 档 + 选中预览 + 透传 + "" 哨兵)
 *
 * NewBookForm 自身不调创建 API(提交时 callback payload 给上层),但 I1 起内部
 * 用 `useStyleTemplatesQuery` 取模板预览 → 这里 vi.mock 该 hook,避免引入
 * QueryClientProvider 依赖,聚焦表单行为。
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { StyleTemplateOut } from "@/api/style_templates";

const _comedic: StyleTemplateOut = {
  name: "comedic_burst",
  label: "搞笑爽文",
  description: "搞笑+阳光、节奏快、每章打脸,适合玄幻 / 都市轻松向。",
  dimensions: {
    genre_tone: "搞笑",
    narrative_density: "快",
    rhetoric_density: "朴素",
    dialogue_ratio: "对话主导",
    pov_switching: "单 POV 严守",
    power_pacing: "每章打脸",
    worldbuilding_density: "每卷新设定",
    emotional_tone: "阳光",
    chapter_length: "偏短(约 2800-3500 字)",
    event_density: "多事件(每章约 4-5 件)",
  },
  preview: "# 写作风格指南:搞笑爽文\n\n轻松解压读着爽。",
};

vi.mock("@/api/style_templates", () => ({
  useStyleTemplatesQuery: () => ({ data: [_comedic] }),
}));

import { NewBookForm } from "@/components/NewBookForm";

function renderForm() {
  const onSubmit = vi.fn();
  render(
    <NewBookForm
      onSubmit={onSubmit}
      disabled={false}
      submitButtonLabel="创建工作区"
    />,
  );
  return { onSubmit };
}

describe("NewBookForm 默认人称视角 radio", () => {
  it("渲染 3 档 radio,默认选中第三人称", () => {
    renderForm();
    const third = screen.getByTestId(
      "nb-default-voice-third_person",
    ) as HTMLInputElement;
    const first = screen.getByTestId(
      "nb-default-voice-first_person",
    ) as HTMLInputElement;
    const free = screen.getByTestId(
      "nb-default-voice-free",
    ) as HTMLInputElement;
    expect(third.checked).toBe(true);
    expect(first.checked).toBe(false);
    expect(free.checked).toBe(false);
  });

  it("选 'free' 显示 warn 提示,切回第三人称提示消失", async () => {
    const user = userEvent.setup();
    renderForm();
    expect(
      screen.queryByTestId("nb-default-voice-free-warn"),
    ).not.toBeInTheDocument();

    await user.click(screen.getByTestId("nb-default-voice-free"));
    expect(
      screen.getByTestId("nb-default-voice-free-warn"),
    ).toBeInTheDocument();

    await user.click(screen.getByTestId("nb-default-voice-third_person"));
    expect(
      screen.queryByTestId("nb-default-voice-free-warn"),
    ).not.toBeInTheDocument();
  });

  it("提交时 payload 透传所选 default_voice", async () => {
    const user = userEvent.setup();
    const { onSubmit } = renderForm();

    await user.type(screen.getByTestId("nb-display-name"), "测试书名");
    await user.type(screen.getByTestId("nb-workspace-id"), "test-ws");
    await user.type(screen.getByTestId("nb-premise"), "一句话设定");
    await user.click(screen.getByTestId("nb-default-voice-first_person"));

    await user.click(screen.getByTestId("nb-submit"));

    expect(onSubmit).toHaveBeenCalledTimes(1);
    const payload = onSubmit.mock.calls[0][0];
    expect(payload.values.default_voice).toBe("first_person");
  });
});

describe("NewBookForm 写作风格模板下拉(I1)", () => {
  it("渲染 8 档下拉(含修仙成长),默认'不使用'(空串)", () => {
    renderForm();
    const select = screen.getByTestId("nb-style-template") as HTMLSelectElement;
    expect(select.options).toHaveLength(8);
    expect(Array.from(select.options).some((option) => option.value === "xianxia_growth")).toBe(true);
    expect(select.value).toBe("");
    // 未选模板时不显示预览。
    expect(
      screen.queryByTestId("nb-style-template-preview"),
    ).not.toBeInTheDocument();
  });

  it("选中模板后显示预览(描述 + 维度)", async () => {
    const user = userEvent.setup();
    renderForm();
    await user.selectOptions(
      screen.getByTestId("nb-style-template"),
      "comedic_burst",
    );
    const preview = screen.getByTestId("nb-style-template-preview");
    expect(preview).toBeInTheDocument();
    expect(preview).toHaveTextContent("每章打脸");
    expect(preview).toHaveTextContent("对话主导");
  });

  it("提交时 payload 透传所选 style_template", async () => {
    const user = userEvent.setup();
    const { onSubmit } = renderForm();

    await user.type(screen.getByTestId("nb-display-name"), "测试书名");
    await user.type(screen.getByTestId("nb-workspace-id"), "test-ws");
    await user.type(screen.getByTestId("nb-premise"), "一句话设定");
    await user.selectOptions(
      screen.getByTestId("nb-style-template"),
      "comedic_burst",
    );

    await user.click(screen.getByTestId("nb-submit"));

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit.mock.calls[0][0].values.style_template).toBe(
      "comedic_burst",
    );
  });

  it("不选模板时 payload.style_template 为空串(上层转 null)", async () => {
    const user = userEvent.setup();
    const { onSubmit } = renderForm();

    await user.type(screen.getByTestId("nb-display-name"), "测试书名");
    await user.type(screen.getByTestId("nb-workspace-id"), "test-ws");
    await user.type(screen.getByTestId("nb-premise"), "一句话设定");

    await user.click(screen.getByTestId("nb-submit"));

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit.mock.calls[0][0].values.style_template).toBe("");
  });
});


describe("NewBookForm 规划范围", () => {
  it("默认完整故事，可选择长篇开局并随创建表单提交", async () => {
    const user = userEvent.setup();
    const { onSubmit } = renderForm();
    expect(screen.getByTestId("nb-planning-mode")).toHaveValue("whole_book");
    await user.type(screen.getByTestId("nb-display-name"), "新故事");
    await user.type(screen.getByTestId("nb-workspace-id"), "scope-test");
    await user.type(screen.getByTestId("nb-premise"), "普通人的成长");
    await user.selectOptions(screen.getByTestId("nb-planning-mode"), "opening_segment");
    await user.click(screen.getByTestId("nb-submit"));
    expect(onSubmit.mock.calls[0][0].values.planning_mode).toBe("opening_segment");
  });
});
