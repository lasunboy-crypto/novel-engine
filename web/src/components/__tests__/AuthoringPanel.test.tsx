import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import { AuthoringPanel } from "@/components/AuthoringPanel";
import { CreativeBookForm } from "@/components/CreativeBookForm";
import type { AuthoringView } from "@/api/authoring";

const mocks = vi.hoisted(() => ({ save: vi.fn(), act: vi.fn(), get: vi.fn(), cancel: vi.fn() }));
vi.mock("@/api/authoring", () => ({ saveAuthoring: mocks.save, actAuthoring: mocks.act, authoringKey: (id: string) => ["authoring", id] }));
vi.mock("@/api/client", () => ({ apiGet: mocks.get }));
vi.mock("@/api/tasks", () => ({ useCancelTaskMutation: () => ({ mutate: mocks.cancel, isPending: false }) }));

function fixture(): AuthoringView {
  return { draft: { version: 1, revision: 2, premise: "少年修行", genre: "xianxia", status: "draft", pending_changes: false, last_action: "",
    preferences: { total_volumes: 5, chapters_per_volume: 30, volume_chapters: [], fixed_chapter_counts: false,
      opening: "brief", voice: "third_person", reading_reference: "", payoff: "balanced", romance: "suggested",
      reference_chapter_words: 2000, style_template: "xianxia_growth", tension_template: "heros_journey" },
    proposal: { title: "山村",
      entities: [{ entity_id: "hero", kind: "character", name: "许禾", alternatives: ["林远"] }], milestones: [],
      sections: [{ key: "plot", title: "主线剧情", selected: "a", options: [
        { option_id: "a", label: "外出求道", content: "[[hero]]外出求道" },
        { option_id: "b", label: "加入宗门", content: "[[hero]]加入宗门" }] }] } }, history: [1] };
}
function mount(view = fixture()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><AuthoringPanel workspaceId="book" view={view} /></QueryClientProvider>);
}
beforeEach(() => {
  vi.clearAllMocks();
  mocks.save.mockResolvedValue({ ...fixture(), draft: { ...fixture().draft!, revision: 3 } });
  mocks.act.mockResolvedValue({ task_id: "task-one", status: "pending" });
  mocks.get.mockResolvedValue({ task_id: "task-one", status: "running" });
});

it("choosing names and plot has no model action; save is one zero-call request", async () => {
  mount();
  fireEvent.change(screen.getByLabelText("主线剧情"), { target: { value: "b" } });
  fireEvent.change(screen.getByLabelText("许禾的名称"), { target: { value: "林远" } });
  expect(screen.getByText("林远加入宗门")).toBeInTheDocument();
  expect(mocks.save).not.toHaveBeenCalled(); expect(mocks.act).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("保存选择 · 不调用模型"));
  await waitFor(() => expect(mocks.save).toHaveBeenCalledOnce());
  expect(mocks.save.mock.calls[0][1]).toMatchObject({ expected_revision: 2, selections: { plot: "b" }, names: { hero: "林远" } });
  expect(mocks.act).not.toHaveBeenCalled();
});

it("batches unsaved choices before exactly one prepare action using new revision", async () => {
  mount();
  fireEvent.change(screen.getByLabelText("开篇铺垫"), { target: { value: "direct" } });
  fireEvent.click(screen.getByText("整理完整规划"));
  await waitFor(() => expect(mocks.act).toHaveBeenCalledOnce());
  expect(mocks.act.mock.calls[0][1]).toMatchObject({ action: "prepare", expected_revision: 3 });
});

it("reopening an active task polls it without issuing a new generation", async () => {
  mount({ ...fixture(), active_task_id: "task-one" });
  await screen.findByText("暂停任务");
  expect(screen.getByText("整理完整规划")).toBeDisabled();
  expect(mocks.act).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("暂停任务"));
  expect(mocks.cancel).toHaveBeenCalledWith("task-one");
});

it("stale save stops before any paid action", async () => {
  mocks.save.mockRejectedValue(new Error("规划已更新"));
  mount();
  fireEvent.change(screen.getByLabelText("主线剧情"), { target: { value: "b" } });
  fireEvent.click(screen.getByText("整理完整规划"));
  await screen.findByText("规划已更新");
  expect(mocks.act).not.toHaveBeenCalled();
});

it("new book only requires an idea; identifiers and growth template are supplied", () => {
  const submit = vi.fn();
  render(<CreativeBookForm onSubmit={submit} disabled={false} submitButtonLabel="开始策划" />);
  fireEvent.change(screen.getByLabelText("最初的创意"), { target: { value: "少年修仙" } });
  fireEvent.click(screen.getByText("开始策划"));
  expect(submit).toHaveBeenCalledOnce();
  expect(submit.mock.calls[0][0].values).toMatchObject({ premise: "少年修仙", style_template: "xianxia_growth", planning_mode: "whole_book", total_volumes: 5 });
});
