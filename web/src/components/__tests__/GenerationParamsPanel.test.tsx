/**
 * v1.1 wave 3 session 3 / L1 Q16 — GenerationParamsPanel 测试。
 * v2 W3.5 校准联动:anti_tic / word_count trim 升级为三态 select(跟随模型/强制开/强制关)。
 *
 * mock `@/api/generation_params` 三个 hook + Toast,隔离测面板 UI 逻辑:
 * - 触发按钮打开侧边浮层 + GET 预填表单
 * - 字数下限 > 上限 → 校验提示 + 保存禁用
 * - 改值点"保存默认" → 以正确 body 调 update mutation(trim 三态 follow→null / on→true / off→false)
 * - 点"应用到已有章节" → 以 scope + 字数 body 调 backfill mutation
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import GenerationParamsPanel from "@/components/GenerationParamsPanel";
import type { GenerationConfigOut } from "@/api/generation_params";

const mockUseGenerationConfigQuery = vi.fn();
const mockUpdateMutate = vi.fn();
const mockBackfillMutate = vi.fn();

vi.mock("@/api/generation_params", async (importActual) => {
  const actual =
    await importActual<typeof import("@/api/generation_params")>();
  return {
    ...actual,
    useGenerationConfigQuery: () => mockUseGenerationConfigQuery(),
    useUpdateGenerationConfigMutation: () => ({
      mutate: mockUpdateMutate,
      isPending: false,
    }),
    useBackfillWordCountMutation: () => ({
      mutate: mockBackfillMutate,
      isPending: false,
    }),
  };
});

const mockUseWorkspaceDetailQuery = vi.fn();
vi.mock("@/api/workspaces", async (importActual) => {
  const actual = await importActual<typeof import("@/api/workspaces")>();
  return {
    ...actual,
    useWorkspaceDetailQuery: () => mockUseWorkspaceDetailQuery(),
  };
});

const showSuccessToast = vi.fn();
const showApiErrorToast = vi.fn();
vi.mock("@/components/Toast", () => ({
  showSuccessToast: (...args: unknown[]) => showSuccessToast(...args),
  showApiErrorToast: (...args: unknown[]) => showApiErrorToast(...args),
}));

/** 完整 GenerationConfigOut(含 W3.5 trim 三态字段);overrides 覆盖单字段。 */
function baseConfig(
  overrides: Partial<GenerationConfigOut> = {},
): GenerationConfigOut {
  return {
    word_count_policy_version: 1,
    word_count_min: 2500,
    word_count_max: 3500,
    max_rewrites: 3,
    plot_node_budget: 3,
    progression_consistency: "auto",
    breakthrough_payoff: "auto",
    style_anchor_source: "book_anchor",
    // W3.5 校准联动三态:enabled=生效值;override=用户三态(null=跟随);calibration_default=校准默认。
    anti_tic_trim_enabled: false,
    anti_tic_trim_override: null,
    anti_tic_trim_calibration_default: false,
    word_count_trim_enabled: false,
    word_count_trim_override: null,
    word_count_trim_calibration_default: false,
    outline_autofix_enabled: true, // v2 W3.5-s4-step1 默认开
    character_backfill_enabled: true, // v2 W3.5-s4-step1 机制1 默认开
    core_state_hard_enabled: true,
    ...overrides,
  };
}

/** 默认 save body(两 trim 均 follow→null);overrides 覆盖单字段。 */
function baseSaveBody(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    word_count_policy_version: 1,
    word_count_min: 2500,
    word_count_max: 3500,
    max_rewrites: 3,
    anti_tic_trim_enabled: null, // follow → null(后端不落盘 → 跟随校准)
    word_count_trim_enabled: false, // 新篇幅策略关闭字数精简
    core_state_hard_enabled: true,
    ...overrides,
  };
}

function setConfig(data: GenerationConfigOut) {
  mockUseGenerationConfigQuery.mockReturnValue({
    data,
    isPending: false,
    isError: false,
    error: null,
  });
}

function setWorkspaceStyle(styleTemplate: string | null) {
  mockUseWorkspaceDetailQuery.mockReturnValue({
    data: { style_template: styleTemplate },
    isPending: false,
    isError: false,
    error: null,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  setConfig(baseConfig());
  // 默认非 3 题材(治愈日常)→ 两机制开关隐藏,既有断言(update body 不含两字段)不受影响。
  setWorkspaceStyle("healing_daily");
});

async function openPanel() {
  const user = userEvent.setup();
  render(<GenerationParamsPanel workspaceId="ws-test" />);
  await user.click(screen.getByTestId("generation-params-trigger"));
  return user;
}

describe("GenerationParamsPanel", () => {
  it("打开浮层后用 GET 配置预填三字段", async () => {
    await openPanel();
    expect(screen.getByTestId("generation-params-word-count-min")).toHaveValue(
      2500,
    );
    expect(screen.getByTestId("generation-params-word-count-max")).toHaveValue(
      3500,
    );
    expect(screen.getByTestId("generation-params-max-rewrites")).toHaveValue(3);
  });

  it("字数下限 > 上限 → 显示校验提示 + 保存禁用", async () => {
    const user = await openPanel();
    const minInput = screen.getByTestId("generation-params-word-count-min");
    await user.clear(minInput);
    await user.type(minInput, "5000");
    expect(
      screen.getByTestId("generation-params-range-error"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("generation-params-save")).toBeDisabled();
  });

  it("改值点保存默认 → 以正确 body 调 update mutation(trim 默认 follow→null)", async () => {
    const user = await openPanel();
    const minInput = screen.getByTestId("generation-params-word-count-min");
    await user.clear(minInput);
    await user.type(minInput, "2000");
    await user.click(screen.getByTestId("generation-params-save"));

    expect(mockUpdateMutate).toHaveBeenCalledTimes(1);
    expect(mockUpdateMutate.mock.calls[0][0]).toEqual(
      baseSaveBody({ word_count_min: 2000 }),
    );
  });

  it("关闭核心状态 HARD 只把 kill-switch 写入后续生成配置", async () => {
    const user = await openPanel();
    const checkbox = screen.getByTestId("generation-params-core-state-hard");
    expect(checkbox).toBeChecked();

    await user.click(checkbox);
    await user.click(screen.getByTestId("generation-params-save"));

    expect(mockUpdateMutate.mock.calls[0][0]).toEqual(
      baseSaveBody({ core_state_hard_enabled: false }),
    );
  });

  it("点应用到已有章节 → 以 scope + 字数 body 调 backfill mutation", async () => {
    const user = await openPanel();
    await user.click(screen.getByTestId("generation-params-backfill"));

    expect(mockBackfillMutate).toHaveBeenCalledTimes(1);
    expect(mockBackfillMutate.mock.calls[0][0]).toEqual({
      scope: "unapproved",
      word_count_min: 2500,
      word_count_max: 3500,
      word_count_policy_version: 1,
    });
  });

  it("加载错误态显示错误提示", async () => {
    mockUseGenerationConfigQuery.mockReturnValue({
      data: undefined,
      isPending: false,
      isError: true,
      error: new Error("boom"),
    });
    const user = userEvent.setup();
    render(<GenerationParamsPanel workspaceId="ws-test" />);
    await user.click(screen.getByTestId("generation-params-trigger"));
    expect(
      screen.getByTestId("generation-params-load-error"),
    ).toBeInTheDocument();
  });

  it("题材∈3(古典仙侠)→ 显示两机制开关 + 保存 body 带 progression/breakthrough", async () => {
    setWorkspaceStyle("classical_xianxia"); // 覆盖 beforeEach 的非 3 题材默认
    const user = await openPanel();

    const progSelect = screen.getByTestId(
      "generation-params-progression-consistency",
    );
    const payoffSelect = screen.getByTestId(
      "generation-params-breakthrough-payoff",
    );
    // GET 预填 auto/auto
    expect(progSelect).toHaveValue("auto");
    expect(payoffSelect).toHaveValue("auto");

    await user.selectOptions(progSelect, "on");
    await user.selectOptions(payoffSelect, "off");
    await user.click(screen.getByTestId("generation-params-save"));

    expect(mockUpdateMutate).toHaveBeenCalledTimes(1);
    expect(mockUpdateMutate.mock.calls[0][0]).toEqual(
      baseSaveBody({
        progression_consistency: "on",
        breakthrough_payoff: "off",
      }),
    );
  });

  it("非 3 题材 → 两机制开关隐藏 + 保存 body 不含两字段", async () => {
    setWorkspaceStyle("healing_daily");
    const user = await openPanel();

    expect(
      screen.queryByTestId("generation-params-genre-mechanisms"),
    ).not.toBeInTheDocument();

    await user.click(screen.getByTestId("generation-params-save"));
    expect(mockUpdateMutate.mock.calls[0][0]).toEqual(baseSaveBody());
  });

  // ===== W3.5 校准联动:trim 三态 =====

  it("篇幅仍是正常任务，保存采用 v1 且关闭字数精简", async () => {
    const user = await openPanel();
    expect(screen.queryByTestId("generation-params-word-count-trim")).not.toBeInTheDocument();
    expect(screen.getByTestId("generation-params-length-policy")).toHaveTextContent("正常写作要求");
    await user.click(screen.getByTestId("generation-params-save"));
    expect(mockUpdateMutate.mock.calls[0][0]).toEqual(baseSaveBody());
  });

  it("句式去口头禅选强制关 → body 带 anti_tic_trim_enabled: false", async () => {
    const user = await openPanel();
    const select = screen.getByTestId("generation-params-anti-tic-trim");
    expect(select).toHaveValue("follow");
    await user.selectOptions(select, "off");
    await user.click(screen.getByTestId("generation-params-save"));

    expect(mockUpdateMutate).toHaveBeenCalledTimes(1);
    expect(mockUpdateMutate.mock.calls[0][0]).toEqual(
      baseSaveBody({ anti_tic_trim_enabled: false }),
    );
  });

  it("旧精简开关不再暴露为新策略的可操作项", async () => {
    setConfig(baseConfig({word_count_trim_override: true, word_count_trim_enabled: true}));
    const user = await openPanel();
    expect(screen.queryByTestId("generation-params-word-count-trim")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("generation-params-save"));
    expect(mockUpdateMutate.mock.calls[0][0]).toMatchObject({word_count_policy_version: 1, word_count_trim_enabled: false});
  });

  it("GET override=null + calibration_default=true → select=follow,文案显示当前:开", async () => {
    setConfig(
      baseConfig({
        anti_tic_trim_override: null,
        anti_tic_trim_enabled: true, // 生效=校准默认
        anti_tic_trim_calibration_default: true,
      }),
    );
    await openPanel();
    const select = screen.getByTestId("generation-params-anti-tic-trim");
    expect(select).toHaveValue("follow");
    // follow 选项文案反映校准默认"当前:开"(切 deepseek 自动开的 UI 镜像)
    expect(select).toHaveTextContent("当前:开");
  });
});


it("约数模式只显示一个目标，保存与应用均保留约数语义", async () => {
  setConfig(baseConfig({ word_count_policy_version: 2, word_count_min: 2000, word_count_max: 2000 }));
  const user = await openPanel();
  expect(screen.getByText("目标字数（约）")).toBeInTheDocument();
  expect(screen.queryByTestId("generation-params-word-count-max")).not.toBeInTheDocument();
  const input = screen.getByTestId("generation-params-word-count-min");
  await user.clear(input);
  await user.type(input, "2100");
  await user.click(screen.getByTestId("generation-params-save"));
  expect(mockUpdateMutate).toHaveBeenCalledWith(
    baseSaveBody({ word_count_policy_version: 2, word_count_min: 2100, word_count_max: 2100 }), expect.any(Object));
  await user.click(screen.getByTestId("generation-params-backfill"));
  expect(mockBackfillMutate).toHaveBeenCalledWith(
    { scope: "unapproved", word_count_policy_version: 2, word_count_min: 2100, word_count_max: 2100 }, expect.any(Object));
});
