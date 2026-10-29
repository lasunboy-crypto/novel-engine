/**
 * v1.1 wave 3 H5 — RoutingRuleEditor agent_routing 段集成测试。
 *
 * 核心验证(H5 路 A 拍板的"合用同一份数据不打架"保证):
 * - 编辑 agent_routing 时 rules / default 不被清空(同一 RoutingConfigIn 整段保存)。
 * - 反向:编辑 rules 时 agent_routing 保留。
 * - 未开启覆盖时 agent 下拉禁用。
 */

import { useState } from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { RoutingRuleEditor } from "@/components/RoutingRuleEditor";
import type { RoutingConfigIn } from "@/api/config";
import type { RoutableAgentOut } from "@/api/routable_agents";

const AGENTS: RoutableAgentOut[] = [
  {
    agent_name: "writer",
    display_name: "写作",
    description: "正文写作",
    llm_driven: true,
    is_llm_judge: false,
    current_profile: "p_default",
    default_profile: "p_default",
  },
  {
    agent_name: "reviewer",
    display_name: "审核",
    description: "质量打分",
    llm_driven: true,
    is_llm_judge: false,
    current_profile: "p_default",
    default_profile: "p_default",
  },
];

const AVAILABLE = ["p_default", "p_fast", "p_strong"];
const ENGINE_DEFAULT = {
  default: "p_default",
  rules: [{ condition: "tension_level >= 9", profile: "p_strong" }],
};

function Harness(props: { initial: RoutingConfigIn | null }) {
  const [value, setValue] = useState<RoutingConfigIn | null>(props.initial);
  return (
    <>
      <RoutingRuleEditor
        value={value}
        availableProfileIds={AVAILABLE}
        engineDefaultRouting={ENGINE_DEFAULT}
        onChange={setValue}
        agents={AGENTS}
      />
      <pre data-testid="value-dump">{JSON.stringify(value)}</pre>
    </>
  );
}

describe("RoutingRuleEditor — agent_routing 集成", () => {
  it("未开启覆盖时 agent 下拉禁用", () => {
    render(<Harness initial={null} />);
    expect(
      screen.getByTestId("workspace-agent-routing-select-writer"),
    ).toBeDisabled();
  });

  it("编辑 agent_routing 保留已有 rules + default(不打架)", async () => {
    const user = userEvent.setup();
    render(
      <Harness
        initial={{
          default: "p_default",
          rules: [{ condition: "is_arc_finale == True", profile: "p_strong" }],
          agent_routing: {},
        }}
      />,
    );

    await user.selectOptions(
      screen.getByTestId("workspace-agent-routing-select-writer"),
      "p_fast",
    );

    const dumped = JSON.parse(
      screen.getByTestId("value-dump").textContent ?? "{}",
    );
    // agent_routing 写入 writer
    expect(dumped.agent_routing).toEqual({ writer: "p_fast" });
    // rules + default 原样保留
    expect(dumped.default).toBe("p_default");
    expect(dumped.rules).toEqual([
      { condition: "is_arc_finale == True", profile: "p_strong" },
    ]);
  });

  it("编辑 rules 保留已有 agent_routing(反向不打架)", async () => {
    const user = userEvent.setup();
    render(
      <Harness
        initial={{
          default: "p_default",
          rules: [],
          agent_routing: { reviewer: "p_strong" },
        }}
      />,
    );

    await user.click(screen.getByTestId("routing-rule-add"));

    const dumped = JSON.parse(
      screen.getByTestId("value-dump").textContent ?? "{}",
    );
    // 新增了一条 rule
    expect(dumped.rules).toHaveLength(1);
    // agent_routing 原样保留
    expect(dumped.agent_routing).toEqual({ reviewer: "p_strong" });
  });
});
