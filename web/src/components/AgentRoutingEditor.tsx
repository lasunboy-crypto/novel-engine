/**
 * AgentRoutingEditor(v1.1 wave 3 H5;W3.5-s5 扩 judge 硬护栏拦截 + 校准状态):
 * agent → profile 绑定编辑器(纯展示受控)。
 *
 * SettingsPage(全局默认层)用本组件。两层差异由父级管理(初值 / 保存路径 /
 * fallback 语义),本组件只负责:列出 agent 清单,每个 agent 一个原生 `<select>`
 * 下拉(对齐 RoutingRuleEditor 既有范式,零新组件)。
 *
 * 受控契约:
 * - `overrides`:仅含**显式绑定**的 agent(agent_name → profile_id);未列入的
 *   agent 走 fallback(下拉显示 fallback 选项)。
 * - `onChange(agent, profileId | null)`:选具体 profile → profileId;选 fallback
 *   选项 → null(父级据此从 overrides 删除该 agent)。
 *
 * W3.5-s5 新增(plan §4.1):
 * - **judge 硬护栏 UI 拦截**:`is_llm_judge` 的 agent 若**生效 profile** thinking-off
 *   → 行内红色警告(后端 `_assert_llm_judge_thinking_on` 会 422,前置提示)。父级据
 *   `findThinkingOffJudges` 禁用保存。
 * - **校准状态显示**:writer 行展示其生效模型的校准徽章(已校准 / 未校准);校准
 *   keyed on writer 模型(= 生成期 `outline_service._resolve_length_calibration`)。
 *
 * 生效 profile 语义(全局 SettingsPage + 工作区 ConfigPage〔经 RoutingRuleEditor〕共用):
 * `effective = overrides[agent] ?? current_profile` —— 显式绑定优先,未绑则取
 * `current_profile`(= 下拉 fallback 选项"当前:X"展示的值,两视图一致:全局视图
 * current_profile 为引擎已解析值,工作区视图为合并已解析值)。**注**:fallback 后
 * 真实保存解析的目标与各视图 fallback 语义有关(全局→default / 工作区→引擎解析),
 * 与下拉显示的"当前"可能有罕见差异;UI 拦截的主目标 = 用户**显式**把 judge 切到
 * thinking-off profile(plan §4.1 描述的常见误操作),该情形 effective=override 必中。
 * fallback 落到 thinking-off 默认的罕见边角由后端 hard guard 422 兜底。
 */

import type {
  RoutableAgentOut,
  RoutableProfileMeta,
} from "@/api/routable_agents";
import { effectiveProfileId } from "@/components/agentRoutingUtils";

const FALLBACK_VALUE = "__fallback__";

interface AgentRoutingEditorProps {
  agents: readonly RoutableAgentOut[];
  availableProfiles: readonly string[];
  /** profile_id → 元信息(model / thinking_on / calibrated / calibrated_at,W3.5-s5)。 */
  profileMeta: Record<string, RoutableProfileMeta>;
  /** 仅含显式绑定的 agent(agent_name → profile_id)。 */
  overrides: Record<string, string>;
  /** profileId=null 表示选了 fallback 选项(父级从 overrides 删除该 agent)。 */
  onChange: (agentName: string, profileId: string | null) => void;
  /** fallback 选项的展示文案(全局 = "走默认 default";工作区 = "不覆盖")。 */
  fallbackOptionLabel: string;
  disabled?: boolean;
  testIdPrefix: string;
}

export function AgentRoutingEditor(props: AgentRoutingEditorProps) {
  const {
    agents,
    availableProfiles,
    profileMeta,
    overrides,
    onChange,
    fallbackOptionLabel,
    disabled = false,
    testIdPrefix,
  } = props;

  return (
    <div
      className="overflow-hidden rounded border border-border"
      data-testid={`${testIdPrefix}-table`}
    >
      <table className="w-full text-sm">
        <thead className="bg-muted/40 text-left text-xs uppercase tracking-wide text-muted-foreground">
          <tr>
            <th className="px-4 py-2 font-medium">agent</th>
            <th className="px-4 py-2 font-medium">profile</th>
          </tr>
        </thead>
        <tbody>
          {agents.map((agent) => {
            const selected = overrides[agent.agent_name] ?? FALLBACK_VALUE;
            const selectId = `${testIdPrefix}-select-${agent.agent_name}`;
            const effectiveMeta =
              profileMeta[effectiveProfileId(agent, overrides)];
            const judgeThinkingOff =
              agent.is_llm_judge &&
              effectiveMeta != null &&
              !effectiveMeta.thinking_on;
            const isWriter = agent.agent_name === "writer";
            return (
              <tr
                key={agent.agent_name}
                data-testid={`${testIdPrefix}-row`}
                data-agent={agent.agent_name}
                className="border-t border-border align-top"
              >
                <td className="px-4 py-2">
                  <div className="space-y-0.5">
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{agent.display_name}</span>
                      <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
                        {agent.agent_name}
                      </code>
                      {agent.is_llm_judge && (
                        <span
                          className="rounded bg-sky-50 px-1.5 py-0.5 text-[10px] font-medium text-sky-700"
                          data-testid={`${testIdPrefix}-judge-tag`}
                          title="治理校验类 agent:必须用思考模式(thinking-on)的模型"
                        >
                          治理校验
                        </span>
                      )}
                    </div>
                    <p className="text-xs text-muted-foreground">
                      {agent.description}
                    </p>
                  </div>
                </td>
                <td className="px-4 py-2">
                  <select
                    id={selectId}
                    data-testid={selectId}
                    className="h-8 w-full max-w-xs rounded border border-input bg-background px-2 text-xs disabled:opacity-50"
                    value={selected}
                    disabled={disabled}
                    onChange={(e) => {
                      const v = e.target.value;
                      onChange(
                        agent.agent_name,
                        v === FALLBACK_VALUE ? null : v,
                      );
                    }}
                  >
                    <option value={FALLBACK_VALUE}>
                      {fallbackOptionLabel}(当前:{agent.current_profile})
                    </option>
                    {availableProfiles.map((id) => (
                      <option key={id} value={id}>
                        {id}
                        {id === agent.default_profile ? "(默认)" : ""}
                      </option>
                    ))}
                  </select>
                  {judgeThinkingOff && (
                    <p
                      className="mt-1 text-xs font-medium text-destructive"
                      data-testid={`${testIdPrefix}-judge-warning-${agent.agent_name}`}
                    >
                      ⚠ 治理校验类 agent 必须用思考模式(thinking-on)的模型,当前选择
                      会被后端拒绝(无法保存)。
                    </p>
                  )}
                  {isWriter && effectiveMeta != null && (
                    <CalibrationBadge
                      meta={effectiveMeta}
                      testId={`${testIdPrefix}-writer-calibration`}
                    />
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** writer 行的模型校准状态徽章(已校准 → 绿;未校准 → 琥珀警告)。 */
function CalibrationBadge(props: {
  meta: RoutableProfileMeta;
  testId: string;
}) {
  const { meta, testId } = props;
  if (meta.calibrated) {
    return (
      <p
        className="mt-1 text-xs text-emerald-700"
        data-testid={testId}
        data-calibrated="true"
      >
        ✓ 模型 {meta.model} 已校准
        {meta.calibrated_at ? ` · ${meta.calibrated_at}` : ""}(长度感知拆章生效)
      </p>
    );
  }
  return (
    <p
      className="mt-1 text-xs text-amber-700"
      data-testid={testId}
      data-calibrated="false"
    >
      ⚠ 模型 {meta.model} 未校准:正式大纲生成会在 Provider 请求前被阻断，
      不允许静默跳过长度感知拆章。请先完成正式校准活动。
    </p>
  );
}
