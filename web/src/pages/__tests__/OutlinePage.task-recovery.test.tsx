/** R8 回归：outline task_id 走产品 URL 持久化，刷新后可绑定确切任务。 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("@/api/authoring", () => ({ useAuthoringQuery: () => ({ data: { draft: null }, isPending: false, isError: false }) }));
vi.mock("@/components/OutlineDiagnosticPanel", () => ({ OutlineDiagnosticPanel: () => null }));
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";

const mocks = vi.hoisted(() => ({
  recoveryTaskId: null as string | null,
  formValues: null as Record<string, unknown> | null,
  onTaskAccepted: undefined as ((taskId: string) => void) | undefined,
  persistedWorkspaceInputs: null as {
    premise: string;
    genre: string;
  } | null,
  setActiveWorkspaceId: vi.fn(),
  workspace: {
    outline_status: "generating",
    premise: "同一前提",
    genre: "mystery",
  },
}));

vi.mock("@/api/workspaces", () => ({
  useWorkspaceDetailQuery: () => ({
    data: mocks.workspace,
    isPending: false,
    isError: false,
  }),
}));

vi.mock("@/store/activeWorkspace", () => ({
  useActiveWorkspaceStore: () => mocks.setActiveWorkspaceId,
}));

vi.mock("@/components/OutlineWorkflowPanel", () => ({
  OutlineWorkflowPanel: (props: {
    formValues?: Record<string, unknown> | null;
    recoveryTaskId?: string | null;
    onTaskAccepted?: (taskId: string) => void;
    persistedWorkspaceInputs?: { premise: string; genre: string } | null;
  }) => {
    mocks.formValues = props.formValues ?? null;
    mocks.recoveryTaskId = props.recoveryTaskId ?? null;
    mocks.onTaskAccepted = props.onTaskAccepted;
    mocks.persistedWorkspaceInputs = props.persistedWorkspaceInputs ?? null;
    return (
      <button
        type="button"
        data-testid="accept-outline-task"
        onClick={() => props.onTaskAccepted?.("task_new_outline")}
      >
        接收任务
      </button>
    );
  },
}));

vi.mock("@/selection/postV8AbcSelection", () => ({
  POST_V8_ABC_SELECTION_UI: {
    mode: "post-v8-abc-selection-short-v1",
    candidate: "hy3",
    values: {
      workspace_id: "post-v8-abc-hy3-selection-v1",
      display_name: "POST-V8 ABC 三章选择活动 hy3",
      genre: "mystery",
      premise: "冻结前提",
      total_volumes: 1,
      chapters_per_volume: 3,
      tension_template: "heros_journey",
      default_voice: "third_person",
      style_template: "dark_mystery",
    },
  },
}));

import OutlinePage from "@/pages/OutlinePage";

function SearchProbe() {
  return <div data-testid="location-search">{useLocation().search}</div>;
}

beforeEach(() => {
  mocks.recoveryTaskId = null;
  mocks.formValues = null;
  mocks.onTaskAccepted = undefined;
  mocks.persistedWorkspaceInputs = null;
  mocks.setActiveWorkspaceId.mockReset();
  mocks.workspace = {
    outline_status: "generating",
    premise: "同一前提",
    genre: "mystery",
  };
});

describe("OutlinePage task checkpoint URL", () => {
  it("读取旧 task_id，并在收到 202 后 replace 为新 task_id", async () => {
    render(
      <MemoryRouter
        initialEntries={[
          "/w/w3-5-r8-qwen-canary-v1/outline?task_id=task_existing_outline",
        ]}
      >
        <Routes>
          <Route
            path="/w/:workspaceId/outline"
            element={
              <>
                <OutlinePage />
                <SearchProbe />
              </>
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    expect(mocks.recoveryTaskId).toBe("task_existing_outline");
    expect(mocks.persistedWorkspaceInputs).toEqual({
      premise: "同一前提",
      genre: "mystery",
    });
    fireEvent.click(screen.getByTestId("accept-outline-task"));

    await waitFor(() => {
      expect(screen.getByTestId("location-search")).toHaveTextContent(
        "?task_id=task_new_outline",
      );
    });
    expect(mocks.recoveryTaskId).toBe("task_new_outline");
  });

  it("exact selection 刷新后只从逐字匹配的冻结合同恢复表单参数", () => {
    mocks.workspace = {
      outline_status: "generating",
      premise: "冻结前提",
      genre: "mystery",
    };

    render(
      <MemoryRouter
        initialEntries={[
          "/w/post-v8-abc-hy3-selection-v1/outline?task_id=task_failed_volume",
        ]}
      >
        <Routes>
          <Route path="/w/:workspaceId/outline" element={<OutlinePage />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(mocks.formValues).toEqual(
      expect.objectContaining({
        workspace_id: "post-v8-abc-hy3-selection-v1",
        premise: "冻结前提",
        total_volumes: 1,
        chapters_per_volume: 3,
        tension_template: "heros_journey",
      }),
    );
  });

  it("selection workspace 输入漂移时不恢复冻结表单", () => {
    mocks.workspace = {
      outline_status: "generating",
      premise: "已漂移前提",
      genre: "mystery",
    };

    render(
      <MemoryRouter
        initialEntries={[
          "/w/post-v8-abc-hy3-selection-v1/outline?task_id=task_failed_volume",
        ]}
      >
        <Routes>
          <Route path="/w/:workspaceId/outline" element={<OutlinePage />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(mocks.formValues).toBeNull();
  });
});
