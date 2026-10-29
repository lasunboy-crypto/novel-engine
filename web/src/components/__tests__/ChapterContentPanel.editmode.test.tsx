/**
 * ChapterContentPanel 人工编辑模式防回退测(v1.1 acceptance 修复2,孤儿章)。
 *
 * mock versions / version 查询隔离面板;钉住:
 * 1. canManualEdit=false(默认正常章)→ 无「编辑正文」按钮(零行为变化)
 * 2. canManualEdit=true → 出现「编辑正文」;点击 → textarea + 保存/取消
 * 3. 保存 → onSaveContent(草稿) 被调,resolve 后退出编辑
 * 4. 取消 → 退出编辑不调 onSaveContent
 * 5. 保存空草稿被禁用(min_length 防呆)
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ChapterContentPanel } from "@/components/ChapterContentPanel";

const mockVersions = vi.fn();
const mockVersion = vi.fn();

vi.mock("@/api/chapters", async (importActual) => {
  const actual = await importActual<typeof import("@/api/chapters")>();
  return {
    ...actual,
    useChapterVersionsQuery: () => mockVersions(),
    useChapterVersionQuery: () => mockVersion(),
  };
});

beforeEach(() => {
  vi.clearAllMocks();
  mockVersions.mockReturnValue({
    data: { chapter_id: "ch-0007", versions: [{ version_id: 1 }] },
    isPending: false,
    fetchStatus: "idle",
    error: null,
  });
  mockVersion.mockReturnValue({
    data: { content: "原 LLM 版正文。", word_count: 7 },
    isPending: false,
    fetchStatus: "idle",
    error: null,
  });
});

describe("ChapterContentPanel 编辑模式", () => {
  it("canManualEdit 未传(正常章)→ 无编辑按钮", () => {
    render(<ChapterContentPanel workspaceId="ws" chapterId="ch-0001" />);
    expect(
      screen.queryByTestId("chapter-content-edit-button"),
    ).toBeNull();
    // 只读渲染仍在
    expect(
      screen.getByTestId("chapter-content-markdown"),
    ).toBeInTheDocument();
  });

  it("canManualEdit=true → 出现编辑按钮;点击进编辑态(textarea + 保存/取消)", async () => {
    const user = userEvent.setup();
    render(
      <ChapterContentPanel
        workspaceId="ws"
        chapterId="ch-0007"
        canManualEdit
        onSaveContent={vi.fn()}
      />,
    );
    const editBtn = screen.getByTestId("chapter-content-edit-button");
    await user.click(editBtn);
    const textarea = screen.getByTestId(
      "chapter-content-edit-textarea",
    ) as HTMLTextAreaElement;
    // 草稿初始化为当前正文
    expect(textarea.value).toBe("原 LLM 版正文。");
    expect(screen.getByTestId("chapter-content-save-button")).toBeInTheDocument();
    expect(
      screen.getByTestId("chapter-content-cancel-button"),
    ).toBeInTheDocument();
  });

  it("保存 → onSaveContent(编辑后草稿) 被调,resolve 后退出编辑", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue({ version_id: 2 });
    render(
      <ChapterContentPanel
        workspaceId="ws"
        chapterId="ch-0007"
        canManualEdit
        onSaveContent={onSave}
      />,
    );
    await user.click(screen.getByTestId("chapter-content-edit-button"));
    const textarea = screen.getByTestId("chapter-content-edit-textarea");
    await user.clear(textarea);
    await user.type(textarea, "人工修复后的正文");
    await user.click(screen.getByTestId("chapter-content-save-button"));

    expect(onSave).toHaveBeenCalledWith("人工修复后的正文");
    // resolve 后退出编辑态(回到只读)
    await waitFor(() => {
      expect(
        screen.queryByTestId("chapter-content-edit-textarea"),
      ).toBeNull();
    });
  });

  it("取消 → 退出编辑不调 onSaveContent", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    render(
      <ChapterContentPanel
        workspaceId="ws"
        chapterId="ch-0007"
        canManualEdit
        onSaveContent={onSave}
      />,
    );
    await user.click(screen.getByTestId("chapter-content-edit-button"));
    await user.click(screen.getByTestId("chapter-content-cancel-button"));
    expect(onSave).not.toHaveBeenCalled();
    expect(
      screen.queryByTestId("chapter-content-edit-textarea"),
    ).toBeNull();
  });

  it("空草稿 → 保存按钮禁用(min_length 防呆)", async () => {
    const user = userEvent.setup();
    render(
      <ChapterContentPanel
        workspaceId="ws"
        chapterId="ch-0007"
        canManualEdit
        onSaveContent={vi.fn()}
      />,
    );
    await user.click(screen.getByTestId("chapter-content-edit-button"));
    const textarea = screen.getByTestId("chapter-content-edit-textarea");
    await user.clear(textarea);
    expect(screen.getByTestId("chapter-content-save-button")).toBeDisabled();
  });

  it("isSavePending → 保存按钮显「保存中…」+ textarea 禁用", async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <ChapterContentPanel
        workspaceId="ws"
        chapterId="ch-0007"
        canManualEdit
        onSaveContent={vi.fn()}
        isSavePending={false}
      />,
    );
    await user.click(screen.getByTestId("chapter-content-edit-button"));
    rerender(
      <ChapterContentPanel
        workspaceId="ws"
        chapterId="ch-0007"
        canManualEdit
        onSaveContent={vi.fn()}
        isSavePending
      />,
    );
    expect(screen.getByTestId("chapter-content-save-button")).toHaveTextContent(
      "保存中…",
    );
    expect(screen.getByTestId("chapter-content-edit-textarea")).toBeDisabled();
  });
});
