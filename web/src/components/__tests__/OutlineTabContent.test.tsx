import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { OutlineOut } from "@/api/outline";

const mocks = vi.hoisted(() => ({
  regenerate: vi.fn(),
  invalidateQueries: vi.fn(),
  taskStart: vi.fn(),
  showApiErrorToast: vi.fn(),
}));

vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => ({ invalidateQueries: mocks.invalidateQueries }),
}));

vi.mock("@/api/outline", () => ({
  OUTLINE_QUERY_KEY: (workspaceId: string) => ["outline", workspaceId],
  useRegenerateOutlineSectionMutation: () => ({
    mutateAsync: mocks.regenerate,
    isPending: false,
  }),
}));

vi.mock("@/hooks/useAsyncTaskState", () => ({
  useAsyncTaskState: () => ({
    isRunning: false,
    start: mocks.taskStart,
    cancel: vi.fn(),
    isCancelling: false,
  }),
}));

vi.mock("@/components/Toast", () => ({
  showApiErrorToast: (...args: unknown[]) => mocks.showApiErrorToast(...args),
}));

import { OutlineTabContent } from "@/components/OutlineTabContent";

const outline = {
  world: null,
  characters: null,
  plot: null,
  volume_plans: null,
  mini_plan: {
    items: [
      {
        chapter_id: "ch-0001",
        chapter_number: 1,
        volume_id: "v1",
        arc_id: "arc-1",
        one_line_event: "林澈在暗室中首次触发青铜镜，看到导师留下的求救线索。",
        info_increment: "导师并非普通失踪，而是主动留下镜面线索。",
        linked_characters: ["lin-che"],
        linked_threads: ["thread-1"],
        chapter_focus: "",
        interaction_focus: "",
        breakthrough_payoff: "",
      },
    ],
  },
} satisfies OutlineOut;

beforeEach(() => {
  mocks.regenerate.mockReset();
  mocks.regenerate.mockResolvedValue({ task_id: "task_mini_plan" });
  mocks.invalidateQueries.mockReset();
  mocks.taskStart.mockReset();
  mocks.showApiErrorToast.mockReset();
});

describe("OutlineTabContent mini_plan", () => {
  it("确认前只读展示章级事件，并走产品单段重生任务", async () => {
    const onTaskAccepted = vi.fn();
    render(
      <OutlineTabContent
        workspaceId="w3-5-r9-qwen-acceptance-v2"
        outline={outline}
        section="mini_plan"
        regenerateParams={{
          premise: "同一前提",
          genre: "mystery",
          total_volumes: 2,
          chapters_per_volume: 6,
          tension_template: "heros_journey",
        }}
        onTaskAccepted={onTaskAccepted}
      />,
    );

    expect(screen.getByTestId("outline-section-mini-plan")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByText(/林澈在暗室中首次触发青铜镜/)).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("outline-regenerate-mini_plan"));

    await waitFor(() => {
      expect(mocks.regenerate).toHaveBeenCalledWith({
        workspaceId: "w3-5-r9-qwen-acceptance-v2",
        body: {
          section: "mini_plan",
          premise: "同一前提",
          keep_others: true,
          genre: "mystery",
          total_volumes: null,
          chapters_per_volume: null,
          tension_template: null,
        },
      });
    });
    expect(onTaskAccepted).toHaveBeenCalledWith("task_mini_plan");
    expect(mocks.taskStart).toHaveBeenCalledWith("task_mini_plan");
  });
});

it("重新生成世界时保留原书的卷章规模", async () => {
  render(<OutlineTabContent workspaceId="offline" outline={outline} section="world"
    regenerateParams={{ premise: "原前提", genre: "mystery", total_volumes: 2,
      chapters_per_volume: 6, tension_template: "heros_journey" }} />);
  fireEvent.click(screen.getByTestId("outline-regenerate-world"));
  await waitFor(() => expect(mocks.regenerate).toHaveBeenCalledWith({
    workspaceId: "offline", body: { section: "world", premise: "原前提", genre: "mystery",
      keep_others: true, total_volumes: 2, chapters_per_volume: 6, tension_template: null },
  }));
});
