import type {
  RoutableAgentOut,
  RoutableProfileMeta,
} from "@/api/routable_agents";

/** agent 生效 profile(显式 override 优先,否则取 current_profile = 下拉"当前"展示值)。 */
export function effectiveProfileId(
  agent: RoutableAgentOut,
  overrides: Record<string, string>,
): string {
  return overrides[agent.agent_name] ?? agent.current_profile;
}

/**
 * 算"生效 profile 为 thinking-off 的 LLM-as-judge agent"(judge 硬护栏 UI 拦截依据)。
 *
 * 返回这类 agent 的 agent_name 列表;非空 → 父级禁用保存(后端 hard guard 会 422)。
 * 镜像后端 `LLM_AS_JUDGE_AGENTS`(`is_llm_judge`)+ `is_thinking_on_profile`
 * (`profileMeta[*].thinking_on`)。profileMeta 未含生效 profile(理论上不会)时
 * **不**误报(保守:缺信息不拦)。
 */
export function findThinkingOffJudges(
  agents: readonly RoutableAgentOut[],
  overrides: Record<string, string>,
  profileMeta: Record<string, RoutableProfileMeta>,
): string[] {
  const offending: string[] = [];
  for (const agent of agents) {
    if (!agent.is_llm_judge) continue;
    const meta = profileMeta[effectiveProfileId(agent, overrides)];
    if (meta && !meta.thinking_on) offending.push(agent.agent_name);
  }
  return offending;
}

/** writer 生效 profile 的模型校准元信息(校准状态显示 / 未校准警告依据);无 writer 行返 null。 */
export function resolveWriterCalibration(
  agents: readonly RoutableAgentOut[],
  overrides: Record<string, string>,
  profileMeta: Record<string, RoutableProfileMeta>,
): RoutableProfileMeta | null {
  const writer = agents.find((a) => a.agent_name === "writer");
  if (!writer) return null;
  return profileMeta[effectiveProfileId(writer, overrides)] ?? null;
}
