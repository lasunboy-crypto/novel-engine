/**
 * v1.1 wave 3 H4 — SettingsPage(API key 管理)测试。
 *
 * mock `@/api/api_keys` 的两个 hook(保留真 `API_KEY_PROVIDERS` 常量)+ Toast,
 * 隔离测页面 UI 逻辑:4 provider 渲染 / 脱敏状态展示 / 填 key + 保存调 mutation /
 * 加载错误态。
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import SettingsPage from "@/pages/SettingsPage";
import type { ApiKeyListOut } from "@/api/api_keys";
import type { RoutableAgentsListOut } from "@/api/routable_agents";

const mockUseApiKeysQuery = vi.fn();
const mockMutate = vi.fn();
const mockUseUpdateApiKeyMutation = vi.fn();

vi.mock("@/api/api_keys", async (importActual) => {
  const actual = await importActual<typeof import("@/api/api_keys")>();
  return {
    ...actual,
    useApiKeysQuery: () => mockUseApiKeysQuery(),
    useUpdateApiKeyMutation: () => mockUseUpdateApiKeyMutation(),
  };
});

const mockUseRoutableAgentsQuery = vi.fn();
const mockAgentRoutingMutate = vi.fn();
const mockUseUpdateGlobalAgentRoutingMutation = vi.fn();

vi.mock("@/api/routable_agents", async (importActual) => {
  const actual =
    await importActual<typeof import("@/api/routable_agents")>();
  return {
    ...actual,
    useRoutableAgentsQuery: () => mockUseRoutableAgentsQuery(),
    useUpdateGlobalAgentRoutingMutation: () =>
      mockUseUpdateGlobalAgentRoutingMutation(),
  };
});

const showSuccessToast = vi.fn();
const showApiErrorToast = vi.fn();
vi.mock("@/components/Toast", () => ({
  showSuccessToast: (...args: unknown[]) => showSuccessToast(...args),
  showApiErrorToast: (...args: unknown[]) => showApiErrorToast(...args),
}));

function setQueryData(data: ApiKeyListOut) {
  mockUseApiKeysQuery.mockReturnValue({
    data,
    isPending: false,
    error: null,
  });
}

function setRoutableAgents(data: RoutableAgentsListOut) {
  mockUseRoutableAgentsQuery.mockReturnValue({
    data,
    isPending: false,
    error: null,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  mockUseUpdateApiKeyMutation.mockReturnValue({
    mutate: mockMutate,
    isPending: false,
    variables: undefined,
  });
  // 默认空 agent 清单(API key 相关测试不关心 agent 面板)。
  mockUseRoutableAgentsQuery.mockReturnValue({
    data: { agents: [], available_profiles: [], profile_meta: {} },
    isPending: false,
    error: null,
  });
  mockUseUpdateGlobalAgentRoutingMutation.mockReturnValue({
    mutate: mockAgentRoutingMutate,
    isPending: false,
  });
});

describe("SettingsPage — API key 管理", () => {
  it("渲染全部 4 个 provider 行", () => {
    setQueryData({ api_keys: [] });
    render(<SettingsPage />);
    const rows = screen.getAllByTestId("settings-api-key-row");
    expect(rows).toHaveLength(4);
    expect(
      screen.getByTestId("settings-api-key-input-claude"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("settings-api-key-input-openai_compatible"),
    ).toBeInTheDocument();
  });

  it("已配置 provider 显示脱敏值 + 已配置标识", () => {
    setQueryData({
      api_keys: [
        { provider: "claude", configured: true, masked_value: "sk-ant-***" },
      ],
    });
    render(<SettingsPage />);
    const claudeRow = screen
      .getAllByTestId("settings-api-key-row")
      .find((el) => el.getAttribute("data-provider") === "claude")!;
    expect(claudeRow).toHaveTextContent("已配置");
    expect(claudeRow).toHaveTextContent("sk-ant-***");
  });

  it("未配置 provider 显示未配置", () => {
    setQueryData({ api_keys: [] });
    render(<SettingsPage />);
    const dsRow = screen
      .getAllByTestId("settings-api-key-row")
      .find((el) => el.getAttribute("data-provider") === "deepseek")!;
    expect(dsRow).toHaveTextContent("未配置");
  });

  it("空输入时保存按钮禁用", () => {
    setQueryData({ api_keys: [] });
    render(<SettingsPage />);
    expect(
      screen.getByTestId("settings-api-key-save-claude"),
    ).toBeDisabled();
  });

  it("填入 key 后点保存,以正确 body 调 mutation", async () => {
    setQueryData({ api_keys: [] });
    const user = userEvent.setup();
    render(<SettingsPage />);

    await user.type(
      screen.getByTestId("settings-api-key-input-dashscope"),
      "sk-test-dashscope-key",
    );
    await user.click(screen.getByTestId("settings-api-key-save-dashscope"));

    expect(mockMutate).toHaveBeenCalledTimes(1);
    expect(mockMutate.mock.calls[0][0]).toEqual({
      provider: "dashscope",
      api_key: "sk-test-dashscope-key",
    });
  });

  it("加载失败时显示错误态", () => {
    mockUseApiKeysQuery.mockReturnValue({
      data: undefined,
      isPending: false,
      error: new Error("boom"),
    });
    render(<SettingsPage />);
    expect(
      screen.getByTestId("settings-api-keys-error"),
    ).toBeInTheDocument();
  });
});

const AGENTS_FIXTURE: RoutableAgentsListOut = {
  agents: [
    {
      agent_name: "writer",
      display_name: "写作",
      description: "正文写作",
      llm_driven: true,
      is_llm_judge: false,
      current_profile: "cloud_quality", // == default → 非显式覆盖
      default_profile: "cloud_quality",
    },
    {
      agent_name: "reviewer",
      display_name: "审核",
      description: "质量打分",
      llm_driven: true,
      is_llm_judge: false,
      current_profile: "cloud_premium", // != default → 显式覆盖
      default_profile: "cloud_quality",
    },
  ],
  available_profiles: ["cloud_premium", "cloud_quality"],
  // 两 profile 均已校准 + thinking-on(claude),基础 routing 测不触发校准/judge 提示。
  profile_meta: {
    cloud_quality: {
      model: "claude-sonnet-4-6",
      thinking_on: true,
      calibrated: true,
      calibrated_at: "2026-06-16",
    },
    cloud_premium: {
      model: "claude-opus-4-7",
      thinking_on: true,
      calibrated: true,
      calibrated_at: "2026-06-16",
    },
  },
};

// W3.5-s5:judge 硬护栏 UI 拦截 fixture(含一个 is_llm_judge agent + thinking 双 profile)。
const JUDGE_FIXTURE: RoutableAgentsListOut = {
  agents: [
    {
      agent_name: "writer",
      display_name: "写作",
      description: "正文写作",
      llm_driven: true,
      is_llm_judge: false,
      current_profile: "ds_thinking",
      default_profile: "ds_thinking",
    },
    {
      agent_name: "must_achieve_validator",
      display_name: "命中校验",
      description: "must_achieve 命中判定(治理 judge)",
      llm_driven: true,
      is_llm_judge: true,
      current_profile: "ds_thinking", // 初始 thinking-on(合法)
      default_profile: "ds_thinking",
    },
  ],
  available_profiles: ["ds_standard", "ds_thinking"],
  profile_meta: {
    ds_standard: {
      model: "deepseek-v4-pro",
      thinking_on: false, // thinking-off → judge 绑它会被拦
      calibrated: true,
      calibrated_at: "2026-06-16",
    },
    ds_thinking: {
      model: "deepseek-v4-pro",
      thinking_on: true,
      calibrated: true,
      calibrated_at: "2026-06-16",
    },
  },
};

// W3.5-s5:校准状态 fixture(writer 初始绑未校准 qwen,可切已校准 deepseek)。
const CALIB_FIXTURE: RoutableAgentsListOut = {
  agents: [
    {
      agent_name: "writer",
      display_name: "写作",
      description: "正文写作",
      llm_driven: true,
      is_llm_judge: false,
      current_profile: "qwen_std", // 未校准
      default_profile: "qwen_std",
    },
  ],
  available_profiles: ["ds_std", "qwen_std"],
  profile_meta: {
    qwen_std: {
      model: "qwen3.7-max",
      thinking_on: false,
      calibrated: false, // 未校准
      calibrated_at: null,
    },
    ds_std: {
      model: "deepseek-v4-pro",
      thinking_on: false,
      calibrated: true,
      calibrated_at: "2026-06-16",
    },
  },
};

describe("SettingsPage — 全局 Agent Routing", () => {
  it("渲染每个 agent 一行 + 下拉", () => {
    setQueryData({ api_keys: [] });
    setRoutableAgents(AGENTS_FIXTURE);
    render(<SettingsPage />);
    const rows = screen.getAllByTestId("settings-agent-routing-row");
    expect(rows).toHaveLength(2);
    expect(
      screen.getByTestId("settings-agent-routing-select-writer"),
    ).toBeInTheDocument();
  });

  it("初始无改动时保存按钮禁用", () => {
    setQueryData({ api_keys: [] });
    setRoutableAgents(AGENTS_FIXTURE);
    render(<SettingsPage />);
    expect(screen.getByTestId("settings-agent-routing-save")).toBeDisabled();
  });

  it("改 agent profile 后保存,以完整 agent_routing(排除=默认项)调 mutation", async () => {
    setQueryData({ api_keys: [] });
    setRoutableAgents(AGENTS_FIXTURE);
    const user = userEvent.setup();
    render(<SettingsPage />);

    // writer 从 cloud_quality(默认)改成 cloud_premium → 成为显式覆盖
    await user.selectOptions(
      screen.getByTestId("settings-agent-routing-select-writer"),
      "cloud_premium",
    );
    await user.click(screen.getByTestId("settings-agent-routing-save"));

    expect(mockAgentRoutingMutate).toHaveBeenCalledTimes(1);
    // baseline 含 reviewer(显式覆盖)+ 新增 writer
    expect(mockAgentRoutingMutate.mock.calls[0][0]).toEqual({
      agent_routing: { reviewer: "cloud_premium", writer: "cloud_premium" },
    });
  });

  it("把显式覆盖的 agent 设回'走默认'→ 从 agent_routing 移除", async () => {
    setQueryData({ api_keys: [] });
    setRoutableAgents(AGENTS_FIXTURE);
    const user = userEvent.setup();
    render(<SettingsPage />);

    // reviewer 初始 = cloud_premium(显式),选 fallback 选项 → 移除
    await user.selectOptions(
      screen.getByTestId("settings-agent-routing-select-reviewer"),
      "__fallback__",
    );
    await user.click(screen.getByTestId("settings-agent-routing-save"));

    expect(mockAgentRoutingMutate.mock.calls[0][0]).toEqual({
      agent_routing: {},
    });
  });

  it("加载失败时显示错误态", () => {
    setQueryData({ api_keys: [] });
    mockUseRoutableAgentsQuery.mockReturnValue({
      data: undefined,
      isPending: false,
      error: new Error("boom"),
    });
    render(<SettingsPage />);
    expect(
      screen.getByTestId("settings-agent-routing-error"),
    ).toBeInTheDocument();
  });
});

describe("SettingsPage — W3.5-s5 judge 硬护栏 UI 拦截", () => {
  it("judge 初始绑 thinking-on 时无警告 + 保存按钮不因 judge 禁用", () => {
    setQueryData({ api_keys: [] });
    setRoutableAgents(JUDGE_FIXTURE);
    render(<SettingsPage />);
    // 无 thinking-off judge → 无拦截 banner
    expect(
      screen.queryByTestId("settings-judge-thinking-off-warning"),
    ).not.toBeInTheDocument();
    // 无改动 → 保存禁用(但原因是 !dirty,非 judge)
    expect(screen.getByTestId("settings-agent-routing-save")).toBeDisabled();
    // judge 行有"治理校验"标签
    expect(
      screen.getAllByTestId("settings-agent-routing-judge-tag").length,
    ).toBeGreaterThanOrEqual(1);
  });

  it("把 judge 切到 thinking-off profile → 行内警告 + 顶部 banner + 保存禁用", async () => {
    setQueryData({ api_keys: [] });
    setRoutableAgents(JUDGE_FIXTURE);
    const user = userEvent.setup();
    render(<SettingsPage />);

    await user.selectOptions(
      screen.getByTestId(
        "settings-agent-routing-select-must_achieve_validator",
      ),
      "ds_standard", // thinking-off
    );

    // 行内红色警告
    expect(
      screen.getByTestId(
        "settings-agent-routing-judge-warning-must_achieve_validator",
      ),
    ).toBeInTheDocument();
    // 顶部拦截 banner 含 agent 名
    const banner = screen.getByTestId("settings-judge-thinking-off-warning");
    expect(banner).toHaveTextContent("must_achieve_validator");
    // 保存被禁用(后端必 422,前置拦截)
    expect(screen.getByTestId("settings-agent-routing-save")).toBeDisabled();
  });

  it("judge 切 thinking-off 后保存按钮禁用,不调 mutation", async () => {
    setQueryData({ api_keys: [] });
    setRoutableAgents(JUDGE_FIXTURE);
    const user = userEvent.setup();
    render(<SettingsPage />);

    await user.selectOptions(
      screen.getByTestId(
        "settings-agent-routing-select-must_achieve_validator",
      ),
      "ds_standard",
    );
    // 按钮禁用 → 点击不触发 mutation(disabled 按钮 click 无效)
    const saveBtn = screen.getByTestId("settings-agent-routing-save");
    expect(saveBtn).toBeDisabled();
    await user.click(saveBtn);
    expect(mockAgentRoutingMutate).not.toHaveBeenCalled();
  });
});

describe("SettingsPage — W3.5-s5 校准状态显示 + 未校准警告", () => {
  it("writer 绑未校准模型 → 未校准 banner + 行内徽章 data-calibrated=false", () => {
    setQueryData({ api_keys: [] });
    setRoutableAgents(CALIB_FIXTURE);
    render(<SettingsPage />);

    const banner = screen.getByTestId("settings-writer-uncalibrated-warning");
    expect(banner).toHaveTextContent("qwen3.7-max");
    const badge = screen.getByTestId(
      "settings-agent-routing-writer-calibration",
    );
    expect(badge).toHaveAttribute("data-calibrated", "false");
  });

  it("writer 切到已校准模型 → 未校准 banner 消失 + 徽章 data-calibrated=true", async () => {
    setQueryData({ api_keys: [] });
    setRoutableAgents(CALIB_FIXTURE);
    const user = userEvent.setup();
    render(<SettingsPage />);

    await user.selectOptions(
      screen.getByTestId("settings-agent-routing-select-writer"),
      "ds_std", // 已校准 deepseek
    );

    expect(
      screen.queryByTestId("settings-writer-uncalibrated-warning"),
    ).not.toBeInTheDocument();
    const badge = screen.getByTestId(
      "settings-agent-routing-writer-calibration",
    );
    expect(badge).toHaveAttribute("data-calibrated", "true");
    expect(badge).toHaveTextContent("2026-06-16");
  });
});
