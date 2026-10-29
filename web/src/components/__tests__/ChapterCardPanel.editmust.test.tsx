/**
 * ChapterCardPanel must_achieve 编辑防回退测(v1.1 acceptance 修复2 followup)。
 *
 * 钉住:
 * 1. canEditMustAchieve 未传(正常章)→ 无「编辑必达事件」按钮(只读零行为变化)
 * 2. canEditMustAchieve=true → 出现按钮;点击进编辑态(must_achieve 各条 textarea)
 * 3. 删条目 → 保存 → mutateAsync 收到原卡 + 删后 must_achieve,**card_status 等其余
 *    字段原样保留**(关键不变量:别破坏 card_status,否则后续 :write gate 被拦)
 * 4. 加条目 + 空条目 trim 剔除
 * 5. 取消 → 不调 mutateAsync,回只读
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ChapterCardPanel } from "@/components/ChapterCardPanel";
import type { ChapterCardOut } from "@/api/chapters";

const mockCard = vi.fn();
const mockMutateAsync = vi.fn();
const mockIsPending = { value: false };

vi.mock("@/api/chapters", async (importActual) => {
  const actual = await importActual<typeof import("@/api/chapters")>();
  return {
    ...actual,
    useChapterCardQuery: () => mockCard(),
    useUpdateChapterCardMutation: () => ({
      mutateAsync: mockMutateAsync,
      isPending: mockIsPending.value,
    }),
  };
});

vi.mock("@/components/Toast", () => ({
  showSuccessToast: vi.fn(),
  showApiErrorToast: vi.fn(),
}));

const _CARD = {
  chapter_id: "ch-0007",
  must_achieve: ["沈舟在防空洞救下流浪汉", "沈舟接诊时制服发狂病患"],
  card_status: "approved",
  setting: "老街废弃防空洞",
  chapter_focus: "来自大纲的结构焦点",
} as unknown as ChapterCardOut;

beforeEach(() => {
  vi.clearAllMocks();
  mockIsPending.value = false;
  mockCard.mockReturnValue({
    data: _CARD,
    isPending: false,
    fetchStatus: "idle",
    error: null,
  });
  mockMutateAsync.mockResolvedValue(_CARD);
});

describe("ChapterCardPanel must_achieve 编辑", () => {
  it("canEditMustAchieve 未传(正常章)→ 无编辑按钮,只读 JSON 在", () => {
    render(<ChapterCardPanel workspaceId="ws" chapterId="ch-0001" />);
    expect(screen.queryByTestId("card-edit-must-button")).toBeNull();
    expect(screen.getByTestId("chapter-card-json")).toBeInTheDocument();
  });

  it("canEditMustAchieve=true → 出现按钮;点击进编辑态(列出 must_achieve 各条)", async () => {
    const user = userEvent.setup();
    render(
      <ChapterCardPanel workspaceId="ws" chapterId="ch-0007" canEditMustAchieve />,
    );
    await user.click(screen.getByTestId("card-edit-must-button"));
    expect(screen.getByTestId("card-must-achieve-editor")).toBeInTheDocument();
    expect(
      (screen.getByTestId("must-achieve-input-0") as HTMLTextAreaElement).value,
    ).toBe("沈舟在防空洞救下流浪汉");
    expect(
      (screen.getByTestId("must-achieve-input-1") as HTMLTextAreaElement).value,
    ).toBe("沈舟接诊时制服发狂病患");
  });

  it("删一条 → 保存 → mutateAsync 收到原卡+删后 must_achieve,card_status 保留", async () => {
    const user = userEvent.setup();
    render(
      <ChapterCardPanel workspaceId="ws" chapterId="ch-0007" canEditMustAchieve />,
    );
    await user.click(screen.getByTestId("card-edit-must-button"));
    await user.click(screen.getByTestId("must-achieve-remove-0")); // 删第一条(已被前文消费的那条)
    await user.click(screen.getByTestId("must-achieve-save"));

    expect(mockMutateAsync).toHaveBeenCalledTimes(1);
    const arg = mockMutateAsync.mock.calls[0][0];
    expect(arg.workspaceId).toBe("ws");
    expect(arg.chapterId).toBe("ch-0007");
    expect(arg.card.must_achieve).toEqual(["沈舟接诊时制服发狂病患"]);
    // 关键不变量:其余字段原样保留(card_status / 大纲透传 chapter_focus / setting)
    expect(arg.card.card_status).toBe("approved");
    expect(arg.card.chapter_focus).toBe("来自大纲的结构焦点");
    expect(arg.card.setting).toBe("老街废弃防空洞");
    // resolve 后退出编辑态(回只读)
    await waitFor(() => {
      expect(screen.queryByTestId("card-must-achieve-editor")).toBeNull();
    });
  });

  it("加一条 + 改内容 → 保存 → must_achieve 含新条目", async () => {
    const user = userEvent.setup();
    render(
      <ChapterCardPanel workspaceId="ws" chapterId="ch-0007" canEditMustAchieve />,
    );
    await user.click(screen.getByTestId("card-edit-must-button"));
    await user.click(screen.getByTestId("must-achieve-add"));
    await user.type(screen.getByTestId("must-achieve-input-2"), "新增必达事件");
    await user.click(screen.getByTestId("must-achieve-save"));

    const arg = mockMutateAsync.mock.calls[0][0];
    expect(arg.card.must_achieve).toContain("新增必达事件");
    expect(arg.card.must_achieve).toHaveLength(3);
  });

  it("空条目(trim 后为空)保存时剔除", async () => {
    const user = userEvent.setup();
    render(
      <ChapterCardPanel workspaceId="ws" chapterId="ch-0007" canEditMustAchieve />,
    );
    await user.click(screen.getByTestId("card-edit-must-button"));
    await user.click(screen.getByTestId("must-achieve-add")); // 加一条留空
    await user.click(screen.getByTestId("must-achieve-save"));

    const arg = mockMutateAsync.mock.calls[0][0];
    // 原 2 条保留,空的那条被剔除
    expect(arg.card.must_achieve).toEqual([
      "沈舟在防空洞救下流浪汉",
      "沈舟接诊时制服发狂病患",
    ]);
  });

  it("取消 → 不调 mutateAsync,回只读", async () => {
    const user = userEvent.setup();
    render(
      <ChapterCardPanel workspaceId="ws" chapterId="ch-0007" canEditMustAchieve />,
    );
    await user.click(screen.getByTestId("card-edit-must-button"));
    await user.click(screen.getByTestId("must-achieve-cancel"));
    expect(mockMutateAsync).not.toHaveBeenCalled();
    expect(screen.getByTestId("chapter-card-json")).toBeInTheDocument();
  });
});
