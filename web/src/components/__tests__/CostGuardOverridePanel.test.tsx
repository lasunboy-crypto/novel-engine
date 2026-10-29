import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CostGuardOverridePanel } from "@/components/CostGuardOverridePanel";

const mockMutate = vi.fn();

vi.mock("@/api/config", async (importActual) => {
  const actual = await importActual<typeof import("@/api/config")>();
  return {
    ...actual,
    useUpdateWorkspaceCostGuardMutation: () => ({
      mutate: mockMutate,
      isPending: false,
    }),
  };
});

vi.mock("@/components/Toast", () => ({
  showSuccessToast: vi.fn(),
  showApiErrorToast: vi.fn(),
}));

const config = {
  cost_currency: "RMB",
  daily_usd_limit: 100,
  monthly_usd_limit: 1000,
  per_workspace_daily_limit: 30,
  action_on_exceed: "pause" as const,
  notify_via: ["log", "websocket"] as ("log" | "websocket")[],
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe("CostGuardOverridePanel", () => {
  it("展示当前上限，修改为 80 后只提交该字段", async () => {
    const user = userEvent.setup();
    render(
      <CostGuardOverridePanel workspaceId="ws-test" config={config} />,
    );

    const input = screen.getByTestId("config-cost-guard-daily-limit");
    expect(input).toHaveValue(30);
    await user.clear(input);
    await user.type(input, "80");
    await user.click(screen.getByTestId("config-cost-guard-save"));

    expect(mockMutate).toHaveBeenCalledTimes(1);
    expect(mockMutate.mock.calls[0][0]).toEqual({
      per_workspace_daily_limit: 80,
    });
    expect(screen.getByText("RMB30.00 / 日")).toBeInTheDocument();
    expect(screen.queryByText(/US\$/)).not.toBeInTheDocument();
  });

  it("币种改变必须显式提交，不能换算既有数字", async () => {
    const user = userEvent.setup();
    render(<CostGuardOverridePanel workspaceId="ws-test" config={config} />);

    await user.selectOptions(
      screen.getByTestId("config-cost-guard-currency"),
      "USD",
    );
    await user.click(screen.getByTestId("config-cost-guard-save"));

    expect(mockMutate).toHaveBeenCalledWith(
      { cost_currency: "USD" },
      expect.any(Object),
    );
  });

  it("负数不是合法成本上限，阻止保存", async () => {
    const user = userEvent.setup();
    render(
      <CostGuardOverridePanel workspaceId="ws-test" config={config} />,
    );

    const input = screen.getByTestId("config-cost-guard-daily-limit");
    await user.clear(input);
    await user.type(input, "-1");

    expect(screen.getByTestId("config-cost-guard-error")).toBeInTheDocument();
    expect(screen.getByTestId("config-cost-guard-save")).toBeDisabled();
  });
});
