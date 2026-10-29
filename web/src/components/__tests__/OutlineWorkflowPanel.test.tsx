/**
 * OutlineWorkflowPanel R8 回归：完整 server checkpoint 可恢复 ready，且
 * generating 状态绝不再次暴露“生成大纲”入口。
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { NewBookFormValues } from "@/components/OutlineWorkflowPanel";
import type { RegenerateParams } from "@/components/OutlineTabContent";

const mocks = vi.hoisted(() => ({
  outlineStatus: "generating" as "not_started" | "generating" | "committed",
  outlineData: undefined as unknown,
  taskInfo: undefined as unknown,
  generate: vi.fn(),
  commit: vi.fn(),
  regenerate: vi.fn(),
  invalidateQueries: vi.fn(),
  taskStart: vi.fn(),
  taskCancel: vi.fn(),
  taskReset: vi.fn(),
  showApiErrorToast: vi.fn(),
  showSuccessToast: vi.fn(),
  regenerateParams: undefined as RegenerateParams | undefined,
  asyncTaskOptions: undefined as
    | { onMessage?: (frame: unknown) => void }
    | undefined,
  recoveryTaskOptions: undefined as
    | {
        onSuccess?: (frame: { data?: { task_id?: string } }) => void;
        onFailed?: (
          message: string,
          frame: { data?: { error?: { code?: string; details?: Record<string, unknown> } } },
        ) => void;
      }
    | undefined,
}));

vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => ({ invalidateQueries: mocks.invalidateQueries }),
}));

vi.mock("@/api/outline", () => ({
  OUTLINE_QUERY_KEY: (workspaceId: string) => ["outline", workspaceId],
  useGenerateOutlineMutation: () => ({
    mutateAsync: mocks.generate,
    isPending: false,
  }),
  useOutlineStatusQuery: () => ({
    data: { outline_status: mocks.outlineStatus },
  }),
  useOutlineQuery: () => ({
    data: mocks.outlineData,
    isLoading: false,
    isError: false,
    error: null,
  }),
  useCommitOutlineMutation: () => ({
    mutateAsync: mocks.commit,
    isPending: false,
    isSuccess: false,
    data: undefined,
  }),
  useRegenerateOutlineSectionMutation: () => ({
    mutateAsync: mocks.regenerate,
    isPending: false,
  }),
}));

vi.mock("@/api/tasks", () => ({
  TASK_QUERY_KEY: (taskId: string) => ["task", taskId],
  useTaskInfoQuery: () => ({
    data: mocks.taskInfo,
    isPending: false,
    isError: false,
  }),
}));

vi.mock("@/hooks/useAsyncTaskState", () => ({
  useAsyncTaskState: (options: {
    kind?: string;
    onMessage?: (frame: unknown) => void;
    onSuccess?: (frame: { data?: { task_id?: string } }) => void;
  }) => {
    if (options.kind === "outline_section") {
      mocks.recoveryTaskOptions = options;
    } else {
      mocks.asyncTaskOptions = options;
    }
    return {
    taskId: null,
    meta: null,
    isRunning: false,
    start: mocks.taskStart,
    cancel: mocks.taskCancel,
    reset: mocks.taskReset,
    isCancelling: false,
    };
  },
}));

vi.mock("@/components/OutlineTabContent", () => ({
  OutlineTabContent: ({
    section,
    regenerateParams,
  }: {
    section: string;
    regenerateParams: RegenerateParams;
  }) => {
    mocks.regenerateParams = regenerateParams;
    return <div data-testid={`outline-tab-content-${section}`} />;
  },
}));

vi.mock("@/components/Toast", () => ({
  showApiErrorToast: (...args: unknown[]) => mocks.showApiErrorToast(...args),
  showSuccessToast: (...args: unknown[]) => mocks.showSuccessToast(...args),
}));

import { OutlineWorkflowPanel } from "@/components/OutlineWorkflowPanel";

const formValues = {
  display_name: "R8",
  workspace_id: "w3-5-r8-qwen-canary-v1",
  genre: "mystery",
  premise: "同一前提",
  total_volumes: 2,
  chapters_per_volume: 6,
  tension_template: "heros_journey",
  style_template: "",
  default_voice: "third_person",
} as NewBookFormValues;

const completeOutline = {
  world: {},
  characters: { characters: [] },
  plot: {},
  volume_plans: { volumes: [] },
  mini_plan: { items: [] },
};

beforeEach(() => {
  mocks.outlineStatus = "generating";
  mocks.outlineData = completeOutline;
  mocks.taskInfo = {
    task_id: "task_r8_outline",
    type: "outline_generate",
    status: "done",
    progress: 100,
    result: {
      task_id: "task_r8_outline",
      outline: completeOutline,
    },
  };
  mocks.generate.mockReset();
  mocks.commit.mockReset();
  mocks.regenerate.mockReset();
  mocks.invalidateQueries.mockReset();
  mocks.taskStart.mockReset();
  mocks.taskCancel.mockReset();
  mocks.taskReset.mockReset();
  mocks.showApiErrorToast.mockReset();
  mocks.showSuccessToast.mockReset();
  mocks.regenerateParams = undefined;
  mocks.asyncTaskOptions = undefined;
  mocks.recoveryTaskOptions = undefined;
});

describe("OutlineWorkflowPanel mini-plan parallel status", () => {
  it("mini_plan 独立于卷规划，并按完整快照显示四路状态", async () => {
    mocks.outlineStatus = "not_started";
    mocks.outlineData = undefined;
    mocks.taskInfo = undefined;
    mocks.generate.mockResolvedValueOnce({ task_id: "task_parallel_outline" });

    render(
      <OutlineWorkflowPanel
        workspaceId="w3-5-r8-qwen-canary-v1"
        formValues={formValues}
        autoStart={false}
        onViewWorkspace={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("owp-generate-trigger"));
    await waitFor(() => expect(mocks.taskStart).toHaveBeenCalled());

    act(() => {
      mocks.asyncTaskOptions?.onMessage?.({
        event: "stage",
        data: {
          stage: "mini_plan",
          parallel_group: "mini_plan_beat_alignment",
          volume_id: "v2",
          max_concurrency: 4,
          tasks: [
            {
              subtask_id: "beat:v2:ch-0013",
              chapter_id: "ch-0013",
              beat_id: "v2-beat-1",
              status: "running",
            },
            {
              subtask_id: "beat:v2:ch-0014",
              chapter_id: "ch-0014",
              beat_id: "v2-beat-1",
              status: "queued",
            },
          ],
        },
      });
    });

    expect(screen.getByText("章级事件与 Beat 对齐中")).toBeInTheDocument();
    expect(screen.getByTestId("owp-beat-parallel-status")).toHaveTextContent(
      "v2 · 并发上限 4",
    );
    expect(screen.getByText("运行中")).toBeInTheDocument();
    expect(screen.getByText("等待")).toBeInTheDocument();
  });
});

describe("OutlineWorkflowPanel checkpoint recovery", () => {
  it("确切 task done + 五段结果一致 → 恢复可提交态且不触发 Provider", async () => {
    render(
      <OutlineWorkflowPanel
        workspaceId="w3-5-r8-qwen-canary-v1"
        formValues={formValues}
        autoStart={false}
        recoveryTaskId="task_r8_outline"
        onViewWorkspace={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("owp-commit-outline")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("owp-generate-trigger")).not.toBeInTheDocument();
    expect(mocks.generate).not.toHaveBeenCalled();
  });

  it("提交成功只提示异步生成首卡，不再声称预生成前五章", async () => {
    mocks.commit.mockResolvedValueOnce({
      workspace_id: "w3-5-r8-qwen-canary-v1",
      volumes_count: 2,
      gates_passed: true,
      first_card_task_id: "task_first_card",
    });

    render(
      <OutlineWorkflowPanel
        workspaceId="w3-5-r8-qwen-canary-v1"
        formValues={formValues}
        autoStart={false}
        recoveryTaskId="task_r8_outline"
        onViewWorkspace={vi.fn()}
      />,
    );

    fireEvent.click(await screen.findByTestId("owp-commit-outline"));

    await waitFor(() => {
      expect(mocks.showSuccessToast).toHaveBeenCalledWith(
        "大纲已提交",
        "2 卷已落库,首卡正在异步生成",
      );
    });
    expect(mocks.showSuccessToast).not.toHaveBeenCalledWith(
      "大纲已提交",
      expect.stringContaining("前 5 章"),
    );
  });

  it("单段重生 task done + 最新五段一致 → 刷新后恢复可提交态", async () => {
    mocks.taskInfo = {
      task_id: "task_r9_regenerated_plot",
      type: "outline_regenerate_section",
      status: "done",
      progress: 100,
      result: completeOutline,
    };

    render(
      <OutlineWorkflowPanel
        workspaceId="w3-5-r9-qwen-acceptance-v2"
        formValues={formValues}
        autoStart={false}
        recoveryTaskId="task_r9_regenerated_plot"
        onViewWorkspace={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("owp-commit-outline")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("owp-generate-trigger")).not.toBeInTheDocument();
    expect(mocks.generate).not.toHaveBeenCalled();
  });

  it("刷新丢失表单时从工作区与一致卷规划恢复单段重生参数", async () => {
    const persistedOutline = {
      ...completeOutline,
      volume_plans: {
        volumes: [
          { chapter_count: 6, tension_template: "heros_journey" },
          { chapter_count: 6, tension_template: "heros_journey" },
        ],
      },
    };
    mocks.outlineData = persistedOutline;
    mocks.taskInfo = {
      task_id: "task_r9_regenerated_plot",
      type: "outline_regenerate_section",
      status: "done",
      progress: 100,
      result: persistedOutline,
    };

    render(
      <OutlineWorkflowPanel
        workspaceId="w3-5-r9-qwen-acceptance-v2"
        formValues={null}
        persistedWorkspaceInputs={{ premise: "同一前提", genre: "mystery" }}
        autoStart={false}
        recoveryTaskId="task_r9_regenerated_plot"
        onViewWorkspace={vi.fn()}
      />,
    );

    await screen.findByTestId("outline-tab-content-world");
    expect(mocks.regenerateParams).toEqual({
      premise: "同一前提",
      genre: "mystery",
      total_volumes: 2,
      chapters_per_volume: 6,
      tension_template: "heros_journey",
    });
    expect(mocks.generate).not.toHaveBeenCalled();
  });

  it("刷新后卷间参数不一致时保持重生入口 fail closed", async () => {
    const inconsistentOutline = {
      ...completeOutline,
      volume_plans: {
        volumes: [
          { chapter_count: 6, tension_template: "heros_journey" },
          { chapter_count: 7, tension_template: "heros_journey" },
        ],
      },
    };
    mocks.outlineData = inconsistentOutline;
    mocks.taskInfo = {
      task_id: "task_r9_regenerated_plot",
      type: "outline_regenerate_section",
      status: "done",
      progress: 100,
      result: inconsistentOutline,
    };

    render(
      <OutlineWorkflowPanel
        workspaceId="w3-5-r9-qwen-acceptance-v2"
        formValues={null}
        persistedWorkspaceInputs={{ premise: "同一前提", genre: "mystery" }}
        autoStart={false}
        recoveryTaskId="task_r9_regenerated_plot"
        onViewWorkspace={vi.fn()}
      />,
    );

    expect(
      await screen.findByTestId("owp-regeneration-params-missing"),
    ).toHaveTextContent("持久化重生参数缺失或不一致");
    expect(screen.queryByTestId("outline-tab-content-world")).not.toBeInTheDocument();
    expect(mocks.regenerateParams).toBeUndefined();
    expect(mocks.generate).not.toHaveBeenCalled();
  });

  it("确认前暴露章级事件预览入口，提示上游重生后的顺序", async () => {
    render(
      <OutlineWorkflowPanel
        workspaceId="w3-5-r8-qwen-canary-v1"
        formValues={formValues}
        autoStart={false}
        recoveryTaskId="task_r8_outline"
        onViewWorkspace={vi.fn()}
      />,
    );

    const miniPlanTab = await screen.findByRole("tab", { name: "章级事件" });
    fireEvent.click(miniPlanTab);

    expect(
      screen.getByTestId("outline-tab-content-mini_plan"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("owp-regeneration-order-hint")).toHaveTextContent(
      "卷规划 → 章级事件",
    );
  });

  it("generating + checkpoint 不完整 → 禁止再次生成", () => {
    mocks.outlineData = { ...completeOutline, mini_plan: null };

    render(
      <OutlineWorkflowPanel
        workspaceId="w3-5-r8-qwen-canary-v1"
        formValues={formValues}
        autoStart={false}
        recoveryTaskId="task_r8_outline"
        onViewWorkspace={vi.fn()}
      />,
    );

    expect(screen.getByText("正在核对大纲任务")).toBeInTheDocument();
    expect(screen.queryByTestId("owp-generate-trigger")).not.toBeInTheDocument();
    expect(screen.queryByTestId("owp-commit-outline")).not.toBeInTheDocument();
    expect(mocks.generate).not.toHaveBeenCalled();
  });

  it.each(["cancelled", "running", "pending", "cancel_requested"])(
    "原任务 %s 时仅明确取消允许手动重新生成，保持原表单参数",
    async (status) => {
      mocks.outlineData = { ...completeOutline, mini_plan: null };
      mocks.taskInfo = { task_id: "task_cancelled_outline", type: "outline_generate", status,
        error: status === "cancelled" ? { code: "TASK_CANCELLED" } : null };
      mocks.generate.mockResolvedValueOnce({ task_id: "task_new_after_cancel" });
      render(<OutlineWorkflowPanel workspaceId={formValues.workspace_id}
        formValues={formValues} autoStart={false} recoveryTaskId="task_cancelled_outline"
        onViewWorkspace={vi.fn()} />);
      expect(mocks.generate).not.toHaveBeenCalled();
      expect(screen.queryByTestId("owp-commit-outline")).not.toBeInTheDocument();
      if (status !== "cancelled") {
        expect(screen.queryByTestId("owp-retry-generate")).not.toBeInTheDocument();
        return;
      }
      expect(await screen.findByText("生成已取消")).toBeInTheDocument();
      fireEvent.click(screen.getByTestId("owp-retry-generate"));
      await waitFor(() => expect(mocks.generate).toHaveBeenCalledWith({
        workspaceId: formValues.workspace_id,
        body: {
          premise: formValues.premise, genre: formValues.genre,
          total_volumes: formValues.total_volumes,
          chapters_per_volume: formValues.chapters_per_volume,
          tension_template: formValues.tension_template,
        },
      }));
      expect(mocks.generate).toHaveBeenCalledTimes(1);
      expect(mocks.regenerate).not.toHaveBeenCalled();
    },
  );

  it("取消任务不匹配或原表单缺失时不开放重发", () => {
    mocks.taskInfo = { task_id: "another_task", type: "outline_generate", status: "cancelled" };
    const view = render(<OutlineWorkflowPanel workspaceId={formValues.workspace_id}
      formValues={formValues} autoStart={false} recoveryTaskId="task_cancelled_outline"
      onViewWorkspace={vi.fn()} />);
    expect(screen.queryByTestId("owp-retry-generate")).not.toBeInTheDocument();
    view.unmount();
    mocks.taskInfo = { task_id: "task_cancelled_outline", type: "outline_generate", status: "cancelled" };
    render(<OutlineWorkflowPanel workspaceId={formValues.workspace_id}
      formValues={null} autoStart={false} recoveryTaskId="task_cancelled_outline"
      onViewWorkspace={vi.fn()} />);
    expect(screen.queryByTestId("owp-retry-generate")).not.toBeInTheDocument();
    expect(mocks.generate).not.toHaveBeenCalled();
  });

  it("五段齐全但 task 结果不一致 → 禁止提前提交", () => {
    mocks.taskInfo = {
      ...(mocks.taskInfo as Record<string, unknown>),
      result: {
        task_id: "task_r8_outline",
        outline: { ...completeOutline, plot: { changed: true } },
      },
    };

    render(
      <OutlineWorkflowPanel
        workspaceId="w3-5-r8-qwen-canary-v1"
        formValues={formValues}
        autoStart={false}
        recoveryTaskId="task_r8_outline"
        onViewWorkspace={vi.fn()}
      />,
    );

    expect(screen.getByText("正在核对大纲任务")).toBeInTheDocument();
    expect(screen.queryByTestId("owp-commit-outline")).not.toBeInTheDocument();
    expect(mocks.generate).not.toHaveBeenCalled();
  });

  it("POST 结果不明确时硬停且不暴露付费重试按钮", async () => {
    mocks.outlineStatus = "not_started";
    mocks.outlineData = undefined;
    mocks.taskInfo = undefined;
    mocks.generate.mockRejectedValueOnce(new Error("connection lost"));

    render(
      <OutlineWorkflowPanel
        workspaceId="w3-5-r8-qwen-canary-v1"
        formValues={formValues}
        autoStart={false}
        onViewWorkspace={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("owp-generate-trigger"));

    await waitFor(() => {
      expect(screen.getByTestId("owp-ambiguous-request-stop")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("owp-retry-generate")).not.toBeInTheDocument();
    expect(mocks.generate).toHaveBeenCalledTimes(1);
  });

  it("收到 202 后把确切 task_id 交给页面持久化", async () => {
    mocks.outlineStatus = "not_started";
    mocks.outlineData = undefined;
    mocks.taskInfo = undefined;
    mocks.generate.mockResolvedValueOnce({ task_id: "task_new_outline" });
    const onTaskAccepted = vi.fn();

    render(
      <OutlineWorkflowPanel
        workspaceId="w3-5-r8-qwen-canary-v1"
        formValues={formValues}
        autoStart={false}
        onTaskAccepted={onTaskAccepted}
        onViewWorkspace={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("owp-generate-trigger"));

    await waitFor(() => {
      expect(onTaskAccepted).toHaveBeenCalledWith("task_new_outline");
    });
    expect(mocks.taskStart).toHaveBeenCalledWith("task_new_outline");
  });

  it("未开始状态已有四段时只开放小纲续生成，并使用持久化参数", async () => {
    mocks.outlineStatus = "not_started";
    mocks.outlineData = {
      ...completeOutline,
      volume_plans: { volumes: [{ volume_id: "v1", chapter_count: 12, tension_template: "hero_journey" }] },
      mini_plan: null,
    };
    mocks.taskInfo = undefined;
    mocks.regenerate.mockResolvedValueOnce({ task_id: "task_resume_staged_mini" });
    const onTaskAccepted = vi.fn();

    render(<OutlineWorkflowPanel workspaceId="xianxia-test"
      formValues={{ ...formValues, premise: "错误旧前提", genre: "mystery" }}
      persistedWorkspaceInputs={{ premise: "修仙前提", genre: "xianxia" }}
      autoStart={false} onTaskAccepted={onTaskAccepted}
      onViewWorkspace={vi.fn()} />);

    expect(screen.queryByTestId("owp-generate-trigger")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("owp-resume-staged-mini-plan"));
    await waitFor(() => expect(mocks.regenerate).toHaveBeenCalledWith({
      workspaceId: "xianxia-test",
      body: {
        section: "mini_plan", premise: "修仙前提", keep_others: true,
        genre: "xianxia", total_volumes: null, chapters_per_volume: null,
        tension_template: null,
      },
    }));
    expect(onTaskAccepted).toHaveBeenCalledWith("task_resume_staged_mini");
    expect(mocks.generate).not.toHaveBeenCalled();
  });

  it("持久化的小纲任务仍在运行时只显示等待，不提供第二次生成", () => {
    mocks.outlineStatus = "not_started";
    mocks.outlineData = {
      ...completeOutline,
      volume_plans: { volumes: [{ volume_id: "v1", chapter_count: 12, tension_template: "hero_journey" }] },
      mini_plan: null,
    };
    mocks.taskInfo = {
      task_id: "task_staged_mini", type: "outline_regenerate_section",
      status: "pending", progress: 0,
    };
    render(<OutlineWorkflowPanel workspaceId="xianxia-test"
      formValues={null} autoStart={false} recoveryTaskId="task_staged_mini"
      onViewWorkspace={vi.fn()} />);
    expect(screen.getByTestId("owp-staged-mini-plan-running")).toHaveTextContent("不要重复触发");
    expect(screen.queryByTestId("owp-resume-staged-mini-plan")).not.toBeInTheDocument();
    expect(mocks.regenerate).not.toHaveBeenCalled();
  });

  it("未开始状态只凭四段不足以恢复已完成小纲，仍须匹配任务结果", async () => {
    mocks.outlineStatus = "not_started";
    mocks.outlineData = completeOutline;
    mocks.taskInfo = {
      task_id: "task_staged_mini", type: "outline_regenerate_section",
      status: "done", progress: 100, result: completeOutline,
    };
    const { unmount } = render(<OutlineWorkflowPanel workspaceId="xianxia-test"
      formValues={null} autoStart={false} recoveryTaskId="task_staged_mini"
      onViewWorkspace={vi.fn()} />);
    await screen.findByTestId("owp-commit-outline");
    unmount();
    mocks.taskInfo = { ...mocks.taskInfo as object, result: { ...completeOutline, plot: { changed: true } } };
    render(<OutlineWorkflowPanel workspaceId="xianxia-test"
      formValues={null} autoStart={false} recoveryTaskId="task_staged_mini"
      onViewWorkspace={vi.fn()} />);
    expect(screen.queryByTestId("owp-commit-outline")).not.toBeInTheDocument();
  });

  it.each([
    { code: "OUTLINE_BUILD_FAILED", message: "卷规划 schema 失败", details: { stage: "volume_plans", model_used: "hy3" } },
    { code: "INTERNAL_ERROR", message: "Expecting value: line 1 column 1 (char 0)", details: undefined },
    { code: "INTERNAL_ERROR", message: "Unrelated internal failure", details: undefined },
  ])("明确失败的卷规划只从三段 checkpoint 恢复，不暴露整本重跑: $code", async (error) => {
    mocks.outlineStatus = "not_started";
    mocks.outlineData = {
      world: {},
      characters: { characters: [] },
      plot: {},
      volume_plans: null,
      mini_plan: null,
    };
    mocks.taskInfo = {
      task_id: "task_failed_volume",
      type: "outline_generate",
      status: "failed",
      progress: 60,
      error,
    };
    mocks.regenerate.mockResolvedValueOnce({ task_id: "task_recover_volume" });
    const onTaskAccepted = vi.fn();

    render(
      <OutlineWorkflowPanel
        workspaceId="post-v8-abc-hy3-selection-v1"
        formValues={{
          ...formValues,
          workspace_id: "post-v8-abc-hy3-selection-v1",
          total_volumes: 1,
          chapters_per_volume: 3,
        }}
        autoStart={false}
        recoveryTaskId="task_failed_volume"
        onTaskAccepted={onTaskAccepted}
        onViewWorkspace={vi.fn()}
      />,
    );

    if (error.message === "Unrelated internal failure") {
      await screen.findByTestId("owp-retry-generate");
      expect(screen.queryByTestId("owp-resume-checkpoint")).not.toBeInTheDocument();
      expect(mocks.regenerate).not.toHaveBeenCalled();
      return;
    }
    const resume = await screen.findByTestId("owp-resume-checkpoint");
    expect(resume).toHaveTextContent("从 checkpoint 重生卷规划");
    expect(screen.queryByTestId("owp-retry-generate")).not.toBeInTheDocument();
    fireEvent.click(resume);

    await waitFor(() => {
      expect(mocks.regenerate).toHaveBeenCalledWith({
        workspaceId: "post-v8-abc-hy3-selection-v1",
        body: expect.objectContaining({
          section: "volume_plans",
          keep_others: true,
          total_volumes: 1,
          chapters_per_volume: 3,
        }),
      });
    });
    expect(onTaskAccepted).toHaveBeenCalledWith("task_recover_volume");
    expect(mocks.taskStart).toHaveBeenCalledWith("task_recover_volume");
  });
  it.each([
    { code: "OUTLINE_BUILD_FAILED", message: "非法回响引用", details: { stage: "mini_plan" } },
    { code: "OUTLINE_CONSISTENCY_CONFLICT", message: "终审待复核", details: { stage: "volume_plans", contradictions: ["原失败证据"] } },
    { code: "INTERNAL_ERROR", message: "已渲染 Prompt 超过活动阶段可执行上界，已在 Provider 前停止: agent=outline_builder,upper_bound=66202,stage_cap=65536,evidence=s2-complete-one-volume-three-chapter-outline-assembler" },
  ])("失败的小纲刷新后仍只恢复小纲 checkpoint", async (error) => {
    mocks.outlineData = { ...completeOutline, mini_plan: null,
      volume_plans: { volumes: [{ volume_id: "v1", chapter_count: 12, tension_template: "hero_journey" }] },
    };
    mocks.taskInfo = {
      task_id: "task_failed_mini", type: "outline_regenerate_section", status: "failed",
      progress: 80, error,
    };
    mocks.regenerate.mockResolvedValueOnce({ task_id: "task_resume_mini" });
    render(<OutlineWorkflowPanel workspaceId="w3-5-r8-qwen-canary-v1"
      formValues={error.code === "OUTLINE_CONSISTENCY_CONFLICT" ? null : formValues}
      persistedWorkspaceInputs={{ premise: formValues.premise, genre: formValues.genre }}
      autoStart={false} recoveryTaskId="task_failed_mini"
      onViewWorkspace={vi.fn()} />);
    const resume = await screen.findByTestId("owp-resume-checkpoint");
    expect(resume).toHaveTextContent(error.code === "OUTLINE_CONSISTENCY_CONFLICT"
      ? "重新审核并继续章级事件" : "继续生成章级事件");
    expect(screen.queryByTestId("owp-retry-generate")).not.toBeInTheDocument();
    fireEvent.click(resume);
    await waitFor(() => expect(mocks.regenerate).toHaveBeenCalledWith({
      workspaceId: "w3-5-r8-qwen-canary-v1",
      body: expect.objectContaining({ section: "mini_plan", keep_others: true }),
    }));
    expect(mocks.generate).not.toHaveBeenCalled();
  });

  it("主线失败从已保存世界人物恢复，再接卷规划，不触发整本重生", async () => {
    mocks.outlineData = { world: {}, characters: { characters: [] }, plot: null, volume_plans: null, mini_plan: null };
    mocks.taskInfo = { task_id: "failed_plot", type: "outline_generate", status: "failed", progress: 40,
      error: { code: "OUTLINE_CONSISTENCY_CONFLICT", message: "主线身份混写", details: { stage: "plot" } } };
    mocks.regenerate.mockResolvedValue({ task_id: "recovered_plot" });
    const { rerender } = render(<OutlineWorkflowPanel workspaceId="narrative-test" formValues={formValues}
      autoStart={false} recoveryTaskId="failed_plot" onViewWorkspace={vi.fn()} />);
    const resume = await screen.findByTestId("owp-resume-checkpoint");
    expect(resume).toHaveTextContent("从 checkpoint 重生主线");
    expect(screen.queryByTestId("owp-retry-generate")).not.toBeInTheDocument();
    fireEvent.click(resume);
    await waitFor(() => expect(mocks.regenerate).toHaveBeenCalledWith({ workspaceId: "narrative-test",
      body: expect.objectContaining({ section: "plot", keep_others: true }) }));
    mocks.outlineData = Object.assign({}, mocks.outlineData, { plot: {} });
    await act(async () => mocks.recoveryTaskOptions?.onSuccess?.({ data: { task_id: "recovered_plot" } }));
    rerender(<OutlineWorkflowPanel workspaceId="narrative-test" formValues={formValues}
      autoStart={false} recoveryTaskId="failed_plot" onViewWorkspace={vi.fn()} />);
    expect(await screen.findByTestId("owp-resume-checkpoint")).toHaveTextContent("从 checkpoint 重生卷规划");
    expect(mocks.generate).not.toHaveBeenCalled();
    expect(mocks.regenerate).toHaveBeenCalledTimes(1);
  });

  it.each([true, false])("主线单段完成后刷新只认同源任务结果: %s", async sameSource => {
    mocks.outlineData = { world: {}, characters: { characters: [] }, plot: {}, volume_plans: null, mini_plan: null };
    mocks.taskInfo = { task_id: "recovered_plot", type: "outline_regenerate_section", status: "done", progress: 100,
      result: sameSource ? mocks.outlineData : Object.assign({}, mocks.outlineData, { plot: { changed: true } }) };
    render(<OutlineWorkflowPanel workspaceId="narrative-test" formValues={formValues}
      autoStart={false} recoveryTaskId="recovered_plot" onViewWorkspace={vi.fn()} />);
    if (sameSource) expect(await screen.findByTestId("owp-resume-checkpoint")).toHaveTextContent("从 checkpoint 重生卷规划");
    else expect(screen.queryByTestId("owp-resume-checkpoint")).not.toBeInTheDocument();
    expect(mocks.generate).not.toHaveBeenCalled();
    expect(mocks.regenerate).not.toHaveBeenCalled();
  });


  it("人物失败仅从已保存世界续接，再进入主线，禁止整本重生", async () => {
    mocks.outlineData = { world: {}, characters: null, plot: null, volume_plans: null, mini_plan: null };
    mocks.taskInfo = { task_id: "failed_characters", type: "outline_generate", status: "failed", progress: 20,
      error: { code: "OUTLINE_CONSISTENCY_CONFLICT", message: "人物时间矛盾", details: { stage: "characters" } } };
    mocks.regenerate.mockResolvedValue({ task_id: "recovered_characters" });
    const { rerender } = render(<OutlineWorkflowPanel workspaceId="narrative-test" formValues={formValues}
      autoStart={false} recoveryTaskId="failed_characters" onViewWorkspace={vi.fn()} />);
    const resume = await screen.findByTestId("owp-resume-checkpoint");
    expect(resume).toHaveTextContent("从 checkpoint 重生人物");
    expect(screen.queryByTestId("owp-retry-generate")).not.toBeInTheDocument();
    fireEvent.click(resume);
    await waitFor(() => expect(mocks.regenerate).toHaveBeenCalledWith({ workspaceId: "narrative-test",
      body: expect.objectContaining({ section: "characters", keep_others: true }) }));
    mocks.outlineData = Object.assign({}, mocks.outlineData, { characters: { characters: [] } });
    await act(async () => mocks.recoveryTaskOptions?.onSuccess?.({ data: { task_id: "recovered_characters" } }));
    rerender(<OutlineWorkflowPanel workspaceId="narrative-test" formValues={formValues}
      autoStart={false} recoveryTaskId="failed_characters" onViewWorkspace={vi.fn()} />);
    expect(await screen.findByTestId("owp-resume-checkpoint")).toHaveTextContent("从 checkpoint 重生主线");
    expect(mocks.generate).not.toHaveBeenCalled();
    expect(mocks.regenerate).toHaveBeenCalledTimes(1);
  });

  it.each([true, false])("人物单段完成后刷新只接受同源结果: %s", async sameSource => {
    mocks.outlineData = { world: {}, characters: { characters: [] }, plot: null, volume_plans: null, mini_plan: null };
    mocks.taskInfo = { task_id: "recovered_characters", type: "outline_regenerate_section", status: "done", progress: 100,
      result: sameSource ? mocks.outlineData : Object.assign({}, mocks.outlineData, { characters: { changed: true } }) };
    render(<OutlineWorkflowPanel workspaceId="narrative-test" formValues={formValues}
      autoStart={false} recoveryTaskId="recovered_characters" onViewWorkspace={vi.fn()} />);
    if (sameSource) expect(await screen.findByTestId("owp-resume-checkpoint")).toHaveTextContent("从 checkpoint 重生主线");
    else expect(screen.queryByTestId("owp-resume-checkpoint")).not.toBeInTheDocument();
    expect(mocks.generate).not.toHaveBeenCalled();
    expect(mocks.regenerate).not.toHaveBeenCalled();
  });

});


describe("规划范围进入正式请求和恢复", () => {
  it("建书选择进入大纲生成请求", async () => {
    mocks.outlineStatus = "not_started";
    mocks.outlineData = undefined;
    mocks.taskInfo = undefined;
    mocks.generate.mockResolvedValueOnce({ task_id: "task_scope" });
    render(<OutlineWorkflowPanel workspaceId={formValues.workspace_id}
      formValues={{ ...formValues, planning_mode: "opening_segment" }} autoStart={false}
      onViewWorkspace={vi.fn()} />);
    fireEvent.click(screen.getByTestId("owp-generate-trigger"));
    await waitFor(() => expect(mocks.generate).toHaveBeenCalledWith({
      workspaceId: formValues.workspace_id,
      body: expect.objectContaining({ planning_mode: "opening_segment", total_volumes: 2, chapters_per_volume: 6 }),
    }));
    expect(mocks.generate).toHaveBeenCalledTimes(1);
  });

  it("刷新后从保存的原始范围恢复，不把扩章后的章数当成新请求", async () => {
    const persisted = { ...completeOutline,
      world: { story_intent: { version: 4, planning_scope: {
        mode: "opening_segment", total_volumes: 2, chapters_per_volume: 6, reference_chapter_words: 2000,
      } } },
      volume_plans: { volumes: [
        { chapter_count: 7, tension_template: "heros_journey" },
        { chapter_count: 8, tension_template: "heros_journey" },
      ] },
    };
    mocks.outlineData = persisted;
    mocks.taskInfo = { task_id: "task_scope_restore", type: "outline_regenerate_section", status: "done", progress: 100, result: persisted };
    render(<OutlineWorkflowPanel workspaceId={formValues.workspace_id} formValues={null}
      persistedWorkspaceInputs={{ premise: "同一前提", genre: "都市" }} autoStart={false}
      recoveryTaskId="task_scope_restore" onViewWorkspace={vi.fn()} />);
    await screen.findByTestId("outline-tab-content-world");
    expect(mocks.regenerateParams).toEqual({ premise: "同一前提", genre: "都市", total_volumes: 2,
      chapters_per_volume: 6, tension_template: "heros_journey", planning_mode: "opening_segment" });
    expect(mocks.generate).not.toHaveBeenCalled();
  });
});
