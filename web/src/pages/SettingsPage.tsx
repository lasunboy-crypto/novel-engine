/**
 * SettingsPage(v1.1 wave 3 H4)— 全局设置页(路由 `/settings`)。
 *
 * 与工作区级 ConfigPage(`/config`)区分:本页放**进程级**(全工作区共享)
 * 配置。当前承载 **API key 管理**;后续 H5 全局 agent routing 默认切换器
 * 可加入本页。
 *
 * ## API key 管理
 *
 * 接 P6-B-16 已就绪的 `GET/PUT /config/api-keys`(详 `@/api/api_keys`)。
 * 列 4 个 provider(claude / dashscope / deepseek / openai_compatible)的配置
 * 状态 + 密码型输入框填新 key + 脱敏回显(`masked_value`,如 `sk-ant-***`)。
 * 保存后后端原地刷新 Settings 单例,无需重启;invalidate 拉回最新脱敏状态。
 *
 * **不依赖活动工作区** —— API key 是进程级凭证,建第一个工作区之前即可配置。
 */

import { useEffect, useMemo, useState } from "react";

import {
  API_KEY_PROVIDERS,
  useApiKeysQuery,
  useUpdateApiKeyMutation,
  type ApiKeyProvider,
  type ApiKeyStatusOut,
} from "@/api/api_keys";
import { isApiError } from "@/api/errors";
import {
  useRoutableAgentsQuery,
  useUpdateGlobalAgentRoutingMutation,
  type RoutableAgentsListOut,
} from "@/api/routable_agents";
import { AgentRoutingEditor } from "@/components/AgentRoutingEditor";
import {
  findThinkingOffJudges,
  resolveWriterCalibration,
} from "@/components/agentRoutingUtils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";

/** 从全局 routable-agents 视图算"显式非默认绑定"(current != default 即显式覆盖)。 */
function deriveExplicitOverrides(
  data: RoutableAgentsListOut | undefined,
): Record<string, string> {
  const result: Record<string, string> = {};
  for (const agent of data?.agents ?? []) {
    if (agent.current_profile !== agent.default_profile) {
      result[agent.agent_name] = agent.current_profile;
    }
  }
  return result;
}

export default function SettingsPage() {
  const apiKeysQuery = useApiKeysQuery();

  const statusByProvider = new Map<string, ApiKeyStatusOut>(
    (apiKeysQuery.data?.api_keys ?? []).map((item) => [item.provider, item]),
  );

  return (
    <div className="space-y-6" data-testid="settings-page">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold">设置</h1>
        <p className="text-sm text-muted-foreground">
          全局(进程级)配置 —— 对所有工作区生效,无需先选定工作区。
        </p>
      </header>

      <section className="space-y-3" data-testid="settings-api-keys-section">
        <div className="space-y-1">
          <h2 className="text-lg font-semibold">API key 管理</h2>
          <p className="text-xs text-muted-foreground">
            填入各 LLM provider 的 API key;保存后立即生效(后端热刷新,无需重启)。
            已配置的 key 只回显前几位脱敏值,无法反查。ollama 走本地端点,不需要 key。
          </p>
        </div>

        {apiKeysQuery.isPending ? (
          <div className="rounded border border-border p-6 text-sm text-muted-foreground">
            加载 API key 状态中…
          </div>
        ) : apiKeysQuery.error ? (
          <div
            data-testid="settings-api-keys-error"
            className="rounded border border-destructive bg-destructive/10 p-6 text-sm text-destructive"
          >
            加载失败:{formatError(apiKeysQuery.error)}
          </div>
        ) : (
          <div className="space-y-3" data-testid="settings-api-keys-list">
            {API_KEY_PROVIDERS.map((p) => (
              <ApiKeyRow
                key={p.provider}
                provider={p.provider}
                label={p.label}
                hint={p.hint}
                status={statusByProvider.get(p.provider) ?? null}
              />
            ))}
          </div>
        )}
      </section>

      <GlobalAgentRoutingSection />
    </div>
  );
}

function GlobalAgentRoutingSection() {
  const agentsQuery = useRoutableAgentsQuery();
  const updateMutation = useUpdateGlobalAgentRoutingMutation();

  const baseline = useMemo(
    () => deriveExplicitOverrides(agentsQuery.data),
    [agentsQuery.data],
  );
  const [overrides, setOverrides] = useState<Record<string, string>>(baseline);

  // 数据刷新后(保存成功 invalidate / 首次加载)重置编辑基线。
  useEffect(() => {
    setOverrides(baseline);
  }, [baseline]);

  const dirty = useMemo(
    () => JSON.stringify(overrides) !== JSON.stringify(baseline),
    [overrides, baseline],
  );

  // W3.5-s5:judge 硬护栏 UI 拦截 + writer 校准状态(均从当前编辑态 + profile_meta 派生)。
  const agents = agentsQuery.data?.agents ?? [];
  const profileMeta = agentsQuery.data?.profile_meta ?? {};
  const thinkingOffJudges = findThinkingOffJudges(
    agents,
    overrides,
    profileMeta,
  );
  const writerCalibration = resolveWriterCalibration(
    agents,
    overrides,
    profileMeta,
  );
  // judge 绑 thinking-off → 后端 hard guard 必 422 → 前置禁用保存(不可逆地白跑一趟)。
  const hasBlockingJudge = thinkingOffJudges.length > 0;

  function handleChange(agentName: string, profileId: string | null) {
    setOverrides((prev) => {
      const next = { ...prev };
      if (profileId === null) {
        delete next[agentName];
      } else {
        next[agentName] = profileId;
      }
      return next;
    });
  }

  function handleSave() {
    updateMutation.mutate(
      { agent_routing: overrides },
      {
        onSuccess: () =>
          showSuccessToast(
            "已保存全局 agent routing",
            "已写入引擎默认 config.yaml,对所有工作区生效",
          ),
        onError: (err) => showApiErrorToast(err),
      },
    );
  }

  return (
    <section className="space-y-3" data-testid="settings-agent-routing-section">
      <div className="space-y-1">
        <h2 className="text-lg font-semibold">全局 Agent Routing(引擎默认)</h2>
        <p className="text-xs text-muted-foreground">
          给每个 LLM-driven agent 指定默认模型 profile,写入引擎默认 config.yaml,
          <strong>对所有工作区生效</strong>。选"走默认"=不显式绑定,回退到
          writer_routing.default。单个工作区可在「工作区配置」页另行覆盖。
          质量把关类 agent(校验 / 合并)必须用 thinking-on 的 profile,否则保存被拒。
        </p>
      </div>

      {agentsQuery.isPending ? (
        <div className="rounded border border-border p-6 text-sm text-muted-foreground">
          加载 agent 清单中…
        </div>
      ) : agentsQuery.error ? (
        <div
          data-testid="settings-agent-routing-error"
          className="rounded border border-destructive bg-destructive/10 p-6 text-sm text-destructive"
        >
          加载失败:{formatError(agentsQuery.error)}
        </div>
      ) : (
        <div className="space-y-3">
          {writerCalibration && !writerCalibration.calibrated && (
            <div
              className="rounded border border-amber-300 bg-amber-50 p-3 text-xs text-amber-800"
              data-testid="settings-writer-uncalibrated-warning"
            >
              ⚠ 当前 writer 模型 <strong>{writerCalibration.model}</strong>{" "}
              未校准:正式大纲生成会在任何 Provider 请求前被阻断，不能跳过
              volume_refine。请切到已校准模型,或先完成正式校准活动(
              <code className="font-mono">
                model_calibration
              </code>
              )。
            </div>
          )}
          {hasBlockingJudge && (
            <div
              className="rounded border border-destructive bg-destructive/10 p-3 text-xs text-destructive"
              data-testid="settings-judge-thinking-off-warning"
            >
              ⚠ 以下治理校验 agent 绑定了非思考模式(thinking-off)的模型,后端会拒绝
              保存:<strong>{thinkingOffJudges.join("、")}</strong>。请改用 thinking-on
              的 profile 后再保存。
            </div>
          )}
          <AgentRoutingEditor
            agents={agentsQuery.data?.agents ?? []}
            availableProfiles={agentsQuery.data?.available_profiles ?? []}
            profileMeta={profileMeta}
            overrides={overrides}
            onChange={handleChange}
            fallbackOptionLabel="走默认"
            disabled={updateMutation.isPending}
            testIdPrefix="settings-agent-routing"
          />
          <div className="flex items-center gap-3 border-t border-border pt-3">
            <Button
              type="button"
              onClick={handleSave}
              disabled={!dirty || updateMutation.isPending || hasBlockingJudge}
              data-testid="settings-agent-routing-save"
            >
              {updateMutation.isPending ? "保存中…" : "保存全局配置"}
            </Button>
            {!dirty && !hasBlockingJudge && (
              <p className="text-xs text-muted-foreground">没有未保存的改动。</p>
            )}
            {hasBlockingJudge && (
              <p className="text-xs text-destructive">
                有治理校验 agent 绑定 thinking-off 模型,无法保存。
              </p>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

interface ApiKeyRowProps {
  provider: ApiKeyProvider;
  label: string;
  hint: string;
  status: ApiKeyStatusOut | null;
}

function ApiKeyRow(props: ApiKeyRowProps) {
  const { provider, label, hint, status } = props;
  const [draft, setDraft] = useState("");
  const updateMutation = useUpdateApiKeyMutation();
  const saving =
    updateMutation.isPending && updateMutation.variables?.provider === provider;
  const configured = status?.configured ?? false;

  function handleSave() {
    const apiKey = draft.trim();
    if (apiKey.length === 0) return;
    updateMutation.mutate(
      { provider, api_key: apiKey },
      {
        onSuccess: (next) => {
          setDraft("");
          showSuccessToast(
            `已保存 ${label} 的 API key`,
            next.masked_value ? `脱敏值:${next.masked_value}` : undefined,
          );
        },
        onError: (err) => showApiErrorToast(err),
      },
    );
  }

  return (
    <div
      className="rounded border border-border p-4"
      data-testid="settings-api-key-row"
      data-provider={provider}
    >
      <div className="flex items-center justify-between">
        <div className="space-y-0.5">
          <div className="flex items-center gap-2">
            <span className="font-medium">{label}</span>
            <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
              {provider}
            </code>
          </div>
          <StatusBadge configured={configured} maskedValue={status?.masked_value} />
        </div>
      </div>

      <div className="mt-3 flex items-end gap-2">
        <div className="flex-1 space-y-1">
          <label
            className="text-xs text-muted-foreground"
            htmlFor={`api-key-input-${provider}`}
          >
            {configured ? "更新 key(留空则不改)" : "填入 key"}
            <span className="ml-2">{hint}</span>
          </label>
          <Input
            id={`api-key-input-${provider}`}
            type="password"
            autoComplete="off"
            placeholder="粘贴 API key…"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            data-testid={`settings-api-key-input-${provider}`}
          />
        </div>
        <Button
          type="button"
          onClick={handleSave}
          disabled={draft.trim().length === 0 || saving}
          data-testid={`settings-api-key-save-${provider}`}
        >
          {saving ? "保存中…" : "保存"}
        </Button>
      </div>
    </div>
  );
}

function StatusBadge(props: {
  configured: boolean;
  maskedValue?: string | null;
}) {
  if (props.configured) {
    return (
      <span
        className="inline-flex items-center gap-1 text-xs text-emerald-700"
        data-testid="settings-api-key-status"
        data-configured="true"
      >
        已配置
        {props.maskedValue && (
          <code className="rounded bg-emerald-50 px-1.5 py-0.5 font-mono">
            {props.maskedValue}
          </code>
        )}
      </span>
    );
  }
  return (
    <span
      className="text-xs text-muted-foreground"
      data-testid="settings-api-key-status"
      data-configured="false"
    >
      未配置
    </span>
  );
}

function formatError(error: unknown): string {
  if (isApiError(error)) {
    return `${error.code}: ${error.message}`;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "未知错误";
}
