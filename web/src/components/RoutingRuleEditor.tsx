/**
 * RoutingRuleEditor(P6-F-05 Part 2):工作区 Writer Routing 规则覆盖编辑器。
 *
 * 编辑两段(对齐 `RoutingConfigIn` schema):
 *
 * - `default`: 默认 profile_id 字符串(下拉,从合并视图所有 profile 中选)。
 * - `rules`: 路由规则数组,每行 `{condition, profile}` 两字段 — `condition`
 *   是受限 AST 表达式字符串(如 `tension_level >= 8` / `is_arc_finale ==
 *   True`),`profile` 下拉选 profile_id。
 *
 * 增删 + 上下移按钮调整顺序(自决项 11 — `react-beautiful-dnd` 未装,走简版
 * 上下移按钮)。`condition` 字符串前端不做语法校验,提交后由后端 AST 求值器
 * (`src/core/routing/rule_eval.py`)拒绝非法表达式 + 返 422
 * `CONFIG_YAML_INVALID`(error toast 显字面错误码 + Pydantic 详情)。
 *
 * `agent_routing` 段(v1.1 wave 3 H5 起暴露):同段编辑各 agent → profile 绑定,
 * 与 rules / default **合用同一份 `RoutingConfigIn` value**(单一 override 开关 +
 * 单一保存路径)。后端 PUT writer_routing 整段替换,合用一份数据天然不与 rules
 * 互相清空(H5 拍板路 A)。`agents` 清单由父级从 `GET /config/routable-agents`
 * 注入;为空时不渲染 agent_routing 段(向后兼容,EngineDefaultTab 只读视图不受影响)。
 *
 * ## 受控 / 非受控边界
 *
 * 本组件完全受控:`value` (`RoutingConfigIn | null`) 由父级 state 维护,
 * `onChange` 把整段新值回传父级。`null` 表示"工作区未覆盖 routing 段"
 * (PUT body 中省略 `writer_routing`)。父级根据 dirty 比对决定保存按钮启用。
 */

import { useMemo } from "react";

import type {
  RoutableAgentOut,
  RoutableProfileMeta,
} from "@/api/routable_agents";
import { AgentRoutingEditor } from "@/components/AgentRoutingEditor";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { RoutingConfigIn, RoutingRuleIn } from "@/api/config";

interface RoutingRuleEditorProps {
  /** 当前工作区覆盖层的 routing 段(null = 未覆盖,完全走引擎默认)。 */
  value: RoutingConfigIn | null;
  /** 合并视图下所有可选 profile_id,供 default / 规则 profile 下拉。 */
  availableProfileIds: readonly string[];
  /** 引擎默认 routing(只读基线,用作 "开启覆盖" 时的初值)。 */
  engineDefaultRouting: {
    default: string;
    rules: readonly { condition: string; profile: string }[];
  };
  /** value 变更回调,null = 撤销覆盖。 */
  onChange: (next: RoutingConfigIn | null) => void;
  /** 可路由 agent 清单(GET /config/routable-agents);为空 → 不渲染 agent_routing 段。 */
  agents?: readonly RoutableAgentOut[];
  /** profile_id → 元信息(校准 / thinking,W3.5-s5);省略 → AgentRoutingEditor 不渲染校准/judge 提示。 */
  profileMeta?: Record<string, RoutableProfileMeta>;
}

export function RoutingRuleEditor(props: RoutingRuleEditorProps) {
  const {
    value,
    availableProfileIds,
    engineDefaultRouting,
    onChange,
    agents = [],
    profileMeta = {},
  } = props;
  const isOverridden = value !== null;

  const currentDefault = value?.default ?? engineDefaultRouting.default;
  const currentRules = useMemo<readonly RoutingRuleIn[]>(
    () => value?.rules ?? engineDefaultRouting.rules,
    [value, engineDefaultRouting.rules],
  );

  function ensureOverride(base?: Partial<RoutingConfigIn>): RoutingConfigIn {
    if (value) return value;
    return {
      default: engineDefaultRouting.default,
      rules: engineDefaultRouting.rules.map((r) => ({ ...r })),
      agent_routing: null,
      ...(base ?? {}),
    };
  }

  function handleEnableOverride() {
    onChange(ensureOverride());
  }

  function handleDisableOverride() {
    onChange(null);
  }

  function handleDefaultChange(next: string) {
    const base = ensureOverride();
    onChange({ ...base, default: next });
  }

  function handleRuleChange(idx: number, patch: Partial<RoutingRuleIn>) {
    const base = ensureOverride();
    const nextRules = (base.rules ?? []).map((r, i) =>
      i === idx ? { ...r, ...patch } : r,
    );
    onChange({ ...base, rules: nextRules });
  }

  function handleAddRule() {
    const base = ensureOverride();
    const nextRules: RoutingRuleIn[] = [
      ...(base.rules ?? []),
      { condition: "", profile: availableProfileIds[0] ?? "" },
    ];
    onChange({ ...base, rules: nextRules });
  }

  function handleDeleteRule(idx: number) {
    const base = ensureOverride();
    const nextRules = (base.rules ?? []).filter((_, i) => i !== idx);
    onChange({ ...base, rules: nextRules });
  }

  function handleMoveRule(idx: number, delta: -1 | 1) {
    const base = ensureOverride();
    const rules = [...(base.rules ?? [])];
    const target = idx + delta;
    if (target < 0 || target >= rules.length) return;
    [rules[idx], rules[target]] = [rules[target], rules[idx]];
    onChange({ ...base, rules });
  }

  function handleAgentRoutingChange(
    agentName: string,
    profileId: string | null,
  ) {
    const base = ensureOverride();
    const nextAgentRouting: Record<string, string> = {
      ...(base.agent_routing ?? {}),
    };
    if (profileId === null) {
      delete nextAgentRouting[agentName];
    } else {
      nextAgentRouting[agentName] = profileId;
    }
    onChange({ ...base, agent_routing: nextAgentRouting });
  }

  const agentRoutingOverrides: Record<string, string> =
    (value?.agent_routing as Record<string, string> | null | undefined) ?? {};

  return (
    <section
      className="space-y-3"
      data-testid="routing-rule-editor"
      data-is-overridden={isOverridden ? "true" : "false"}
    >
      <header className="flex items-center justify-between">
        <div>
          <h3 className="text-base font-semibold">Writer Routing 覆盖</h3>
          <p className="text-xs text-muted-foreground">
            默认 profile + 路由规则(按顺序优先匹配,全部不命中走默认)。
            `condition` 受限 AST 表达式(示例:
            <code className="mx-1 rounded bg-muted px-1 py-0.5 font-mono text-xs">
              tension_level &gt;= 8
            </code>
            / <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
              is_arc_finale == True
            </code>
            )。
          </p>
        </div>
        {isOverridden ? (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={handleDisableOverride}
            data-testid="routing-rule-editor-disable-override"
            title="撤销 routing 段覆盖,本会话不再保存该段"
          >
            撤销覆盖
          </Button>
        ) : (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={handleEnableOverride}
            data-testid="routing-rule-editor-enable-override"
            title="开启工作区 routing 段覆盖,以下编辑会保存到 project.yaml"
          >
            开启覆盖
          </Button>
        )}
      </header>

      <div className="space-y-3 rounded border border-border p-3">
        <DefaultProfileSelect
          value={currentDefault}
          availableProfileIds={availableProfileIds}
          disabled={!isOverridden}
          onChange={handleDefaultChange}
        />

        <RulesTable
          rules={currentRules}
          availableProfileIds={availableProfileIds}
          disabled={!isOverridden}
          onChange={handleRuleChange}
          onDelete={handleDeleteRule}
          onMove={handleMoveRule}
          onAdd={handleAddRule}
        />

        {!isOverridden && (
          <p className="text-xs text-muted-foreground">
            当前显示引擎默认。点 "开启覆盖" 按钮可编辑并保存到工作区 project.yaml。
          </p>
        )}
      </div>

      {agents.length > 0 && (
        <div className="space-y-2 rounded border border-border p-3">
          <div>
            <h4 className="text-sm font-medium">Agent 模型指定覆盖</h4>
            <p className="text-xs text-muted-foreground">
              给本工作区的各 agent 单独指定 profile(与上方 default / 规则同段保存)。
              选"不覆盖"=回退引擎默认 agent_routing → default。质量把关类 agent 须用
              thinking-on profile。
            </p>
          </div>
          <AgentRoutingEditor
            agents={agents}
            availableProfiles={availableProfileIds}
            profileMeta={profileMeta}
            overrides={agentRoutingOverrides}
            onChange={handleAgentRoutingChange}
            fallbackOptionLabel="不覆盖"
            disabled={!isOverridden}
            testIdPrefix="workspace-agent-routing"
          />
        </div>
      )}
    </section>
  );
}

function DefaultProfileSelect(props: {
  value: string;
  availableProfileIds: readonly string[];
  disabled: boolean;
  onChange: (next: string) => void;
}) {
  return (
    <div className="flex items-center gap-3">
      <label
        htmlFor="routing-default-select"
        className="text-sm font-medium"
      >
        默认 profile
      </label>
      <select
        id="routing-default-select"
        data-testid="routing-default-select"
        className="rounded border border-input bg-background px-2 py-1 text-sm disabled:opacity-50"
        value={props.value}
        disabled={props.disabled}
        onChange={(e) => props.onChange(e.target.value)}
      >
        {props.availableProfileIds.map((id) => (
          <option key={id} value={id}>
            {id}
          </option>
        ))}
        {!props.availableProfileIds.includes(props.value) && (
          <option value={props.value}>{props.value}(未在 profile 列表)</option>
        )}
      </select>
      <span className="text-xs text-muted-foreground">
        所有路由规则不命中时走此 profile。
      </span>
    </div>
  );
}

function RulesTable(props: {
  rules: readonly RoutingRuleIn[];
  availableProfileIds: readonly string[];
  disabled: boolean;
  onChange: (idx: number, patch: Partial<RoutingRuleIn>) => void;
  onDelete: (idx: number) => void;
  onMove: (idx: number, delta: -1 | 1) => void;
  onAdd: () => void;
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium">路由规则({props.rules.length} 条)</h4>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={props.onAdd}
          disabled={props.disabled}
          data-testid="routing-rule-add"
        >
          新增规则
        </Button>
      </div>

      {props.rules.length === 0 ? (
        <div
          data-testid="routing-rules-empty"
          className="rounded border border-dashed border-border p-3 text-xs text-muted-foreground"
        >
          未配置任何规则,Writer 始终走默认 profile。
        </div>
      ) : (
        <div className="overflow-hidden rounded border border-border">
          <table className="w-full text-sm" data-testid="routing-rules-table">
            <thead className="bg-muted/40 text-left text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">#</th>
                <th className="px-3 py-2 font-medium">condition</th>
                <th className="px-3 py-2 font-medium">profile</th>
                <th className="px-3 py-2 font-medium text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {props.rules.map((rule, idx) => (
                <tr
                  key={idx}
                  data-testid="routing-rule-row"
                  data-row-index={idx}
                  className="border-t border-border align-top"
                >
                  <td className="px-3 py-2 text-muted-foreground">{idx + 1}</td>
                  <td className="px-3 py-2">
                    <Input
                      type="text"
                      value={rule.condition}
                      disabled={props.disabled}
                      onChange={(e) =>
                        props.onChange(idx, { condition: e.target.value })
                      }
                      placeholder="tension_level >= 8"
                      data-testid={`routing-rule-row-${idx}-condition`}
                      className="h-8 font-mono text-xs"
                    />
                  </td>
                  <td className="px-3 py-2">
                    <select
                      value={rule.profile}
                      disabled={props.disabled}
                      onChange={(e) =>
                        props.onChange(idx, { profile: e.target.value })
                      }
                      data-testid={`routing-rule-row-${idx}-profile`}
                      className="h-8 rounded border border-input bg-background px-2 text-xs disabled:opacity-50"
                    >
                      {props.availableProfileIds.map((id) => (
                        <option key={id} value={id}>
                          {id}
                        </option>
                      ))}
                      {!props.availableProfileIds.includes(rule.profile) && (
                        <option value={rule.profile}>
                          {rule.profile}(未在 profile 列表)
                        </option>
                      )}
                    </select>
                  </td>
                  <td className="px-3 py-2 text-right">
                    <div className="inline-flex gap-1">
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={props.disabled || idx === 0}
                        onClick={() => props.onMove(idx, -1)}
                        title="上移"
                        data-testid={`routing-rule-row-${idx}-move-up`}
                        className="h-7 w-7 p-0"
                      >
                        ↑
                      </Button>
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={
                          props.disabled || idx === props.rules.length - 1
                        }
                        onClick={() => props.onMove(idx, 1)}
                        title="下移"
                        data-testid={`routing-rule-row-${idx}-move-down`}
                        className="h-7 w-7 p-0"
                      >
                        ↓
                      </Button>
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={props.disabled}
                        onClick={() => props.onDelete(idx)}
                        title="删除规则"
                        data-testid={`routing-rule-row-${idx}-delete`}
                      >
                        删除
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
