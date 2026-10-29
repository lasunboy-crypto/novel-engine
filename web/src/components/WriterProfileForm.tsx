/**
 * WriterProfileForm(P6-F-04 Part 1):新增 / 编辑单个 Writer Profile 的受控表单。
 *
 * 字段对齐 P6-B-12 `WriterProfileIn` schema(provider / model / base_url /
 * context_window / generation_params / extra_request_kwargs /
 * cost_per_1k_input / cost_per_1k_output / notes)+ 新增模式额外的 profile_id
 * (由 dict key 给出,WriterProfileIn 字段集本身**不含** profile_id)。
 *
 * ## 模式
 *
 * - `mode === "create"`:profile_id 字段可编辑;校验"不为空 + 不与既有 ID 重复"。
 * - `mode === "edit"`:profile_id 字段只读展示;表单仅改其余 9 字段。
 *
 * ## generation_params / extra_request_kwargs
 *
 * 用 textarea 接收 JSON 字符串,onChange 时即时 try-parse;解析失败时 form-level
 * 错误"必须是合法 JSON object";空字符串等价 `{}`。
 *
 * ## 类 A / 类 B 不触达
 *
 * 本组件纯前端受控状态,字段集严格对齐 schema(In 字段集),无 spec 冲突。
 */

import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { WriterProfileIn } from "@/api/config";

const PROVIDERS = ["claude", "ollama", "openai_compatible", "mock"] as const;
type Provider = (typeof PROVIDERS)[number];

export interface WriterProfileFormSubmit {
  profileId: string;
  profile: WriterProfileIn;
}

interface WriterProfileFormProps {
  mode: "create" | "edit";
  /** edit 模式必填(profile_id 由父级带入,不可改);create 模式可省略。 */
  initialProfileId?: string;
  initialProfile?: WriterProfileIn;
  /** 已存在的 profile_id 集合(create 模式去重校验用)。 */
  existingProfileIds?: ReadonlySet<string>;
  onSubmit: (submitted: WriterProfileFormSubmit) => void;
  onCancel: () => void;
}

const EMPTY_PROFILE: WriterProfileIn = {
  provider: "claude",
  model: "",
  base_url: null,
  context_window: 200000,
  generation_params: {},
  extra_request_kwargs: {},
  cost_per_1k_input: 0,
  cost_per_1k_output: 0,
  cost_currency: "USD",
  notes: "",
};

function jsonifyObject(value: Record<string, unknown> | undefined): string {
  if (!value || Object.keys(value).length === 0) {
    return "{}";
  }
  return JSON.stringify(value, null, 2);
}

export function WriterProfileForm(props: WriterProfileFormProps) {
  const {
    mode,
    initialProfileId,
    initialProfile,
    existingProfileIds,
    onSubmit,
    onCancel,
  } = props;
  const base = initialProfile ?? EMPTY_PROFILE;

  const [profileId, setProfileId] = useState(initialProfileId ?? "");
  const [provider, setProvider] = useState<Provider>(
    PROVIDERS.includes(base.provider as Provider)
      ? (base.provider as Provider)
      : "claude",
  );
  const [model, setModel] = useState(base.model);
  const [baseUrl, setBaseUrl] = useState(base.base_url ?? "");
  const [contextWindow, setContextWindow] = useState(base.context_window);
  const [costInput, setCostInput] = useState(base.cost_per_1k_input ?? 0);
  const [costOutput, setCostOutput] = useState(base.cost_per_1k_output ?? 0);
  const [costCurrency, setCostCurrency] = useState(base.cost_currency ?? "USD");
  const [notes, setNotes] = useState(base.notes ?? "");
  const [generationParamsText, setGenerationParamsText] = useState(
    jsonifyObject(base.generation_params),
  );
  const [extraKwargsText, setExtraKwargsText] = useState(
    jsonifyObject(base.extra_request_kwargs),
  );

  const [formError, setFormError] = useState<string | null>(null);

  const idLooksValid = useMemo(() => /^[a-z][a-z0-9_]*$/.test(profileId), [profileId]);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);

    if (mode === "create") {
      if (profileId.trim().length === 0) {
        setFormError("profile_id 不能为空");
        return;
      }
      if (!idLooksValid) {
        setFormError("profile_id 仅允许小写字母 / 数字 / 下划线,且以字母开头");
        return;
      }
      if (existingProfileIds && existingProfileIds.has(profileId)) {
        setFormError(`profile_id "${profileId}" 已存在(覆盖时请用"编辑")`);
        return;
      }
    }

    if (model.trim().length === 0) {
      setFormError("model 不能为空");
      return;
    }
    if (!Number.isFinite(contextWindow) || contextWindow <= 0) {
      setFormError("context_window 必须 > 0");
      return;
    }
    if (provider === "openai_compatible" && baseUrl.trim().length === 0) {
      setFormError(
        "provider=openai_compatible 时 base_url 必填(该 provider 无内置默认 endpoint)",
      );
      return;
    }

    let generationParams: Record<string, unknown>;
    let extraKwargs: Record<string, unknown>;
    try {
      generationParams = parseJsonObject(generationParamsText);
    } catch (err) {
      setFormError(`generation_params 解析失败:${(err as Error).message}`);
      return;
    }
    try {
      extraKwargs = parseJsonObject(extraKwargsText);
    } catch (err) {
      setFormError(`extra_request_kwargs 解析失败:${(err as Error).message}`);
      return;
    }

    const profile: WriterProfileIn = {
      provider,
      model: model.trim(),
      base_url: baseUrl.trim().length === 0 ? null : baseUrl.trim(),
      context_window: contextWindow,
      generation_params: generationParams,
      extra_request_kwargs: extraKwargs,
      cost_per_1k_input: costInput,
      cost_per_1k_output: costOutput,
      cost_currency: costCurrency,
      notes,
    };
    onSubmit({ profileId: profileId.trim(), profile });
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="space-y-3 rounded border border-border bg-muted/20 p-4"
      data-testid="writer-profile-form"
    >
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">
          {mode === "create" ? "新增 Writer Profile" : `编辑 ${profileId}`}
        </h3>
      </div>

      <Field label="profile_id" required>
        <Input
          value={profileId}
          onChange={(e) => setProfileId(e.target.value)}
          disabled={mode === "edit"}
          data-testid="writer-profile-form-profile-id"
          placeholder="如 qwen_creative"
        />
      </Field>

      <Field label="provider" required>
        <div className="flex gap-3">
          {PROVIDERS.map((p) => (
            <label
              key={p}
              className="flex items-center gap-1.5 text-sm cursor-pointer"
            >
              <input
                type="radio"
                name="provider"
                value={p}
                checked={provider === p}
                onChange={() => setProvider(p)}
                data-testid={`writer-profile-form-provider-${p}`}
              />
              <span className="font-mono text-xs">{p}</span>
            </label>
          ))}
        </div>
      </Field>

      <Field label="model" required>
        <Input
          value={model}
          onChange={(e) => setModel(e.target.value)}
          data-testid="writer-profile-form-model"
          placeholder="如 claude-sonnet-4-6 / qwen2.5:32b"
        />
      </Field>

      <Field
        label="base_url"
        hint="非默认 endpoint 时填;留空 → SDK 默认"
      >
        <Input
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          data-testid="writer-profile-form-base-url"
          placeholder="如 http://localhost:11434"
        />
      </Field>

      <Field label="context_window" required>
        <Input
          type="number"
          min={1}
          value={contextWindow}
          onChange={(e) => setContextWindow(Number(e.target.value))}
          data-testid="writer-profile-form-context-window"
        />
      </Field>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        <Field label="cost_currency">
          <select
            value={costCurrency}
            onChange={(event) => setCostCurrency(event.target.value)}
            className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm"
            data-testid="writer-profile-form-cost-currency"
          >
            <option value="USD">USD</option>
            <option value="RMB">RMB</option>
          </select>
        </Field>
        <Field label={`cost_per_1k_input (${costCurrency})`}>
          <Input
            type="number"
            step="0.0001"
            min={0}
            value={costInput}
            onChange={(e) => setCostInput(Number(e.target.value))}
            data-testid="writer-profile-form-cost-input"
          />
        </Field>
        <Field label={`cost_per_1k_output (${costCurrency})`}>
          <Input
            type="number"
            step="0.0001"
            min={0}
            value={costOutput}
            onChange={(e) => setCostOutput(Number(e.target.value))}
            data-testid="writer-profile-form-cost-output"
          />
        </Field>
      </div>

      <Field
        label="generation_params"
        hint="JSON object,例 {&quot;temperature&quot;: 0.8, &quot;max_tokens&quot;: 4096}"
      >
        <textarea
          value={generationParamsText}
          onChange={(e) => setGenerationParamsText(e.target.value)}
          rows={3}
          className="w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-xs"
          data-testid="writer-profile-form-generation-params"
        />
      </Field>

      <Field
        label="extra_request_kwargs"
        hint="provider 扩展字段;ollama 可传 {&quot;think&quot;: false}"
      >
        <textarea
          value={extraKwargsText}
          onChange={(e) => setExtraKwargsText(e.target.value)}
          rows={3}
          className="w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-xs"
          data-testid="writer-profile-form-extra-kwargs"
        />
      </Field>

      <Field label="notes">
        <Input
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          data-testid="writer-profile-form-notes"
          placeholder="自由文本说明,供 ConfigPage 展示"
        />
      </Field>

      {formError && (
        <div
          data-testid="writer-profile-form-error"
          className="rounded border border-destructive bg-destructive/10 p-2 text-xs text-destructive"
        >
          {formError}
        </div>
      )}

      <div className="flex gap-2 pt-1">
        <Button
          type="submit"
          size="sm"
          data-testid="writer-profile-form-submit"
        >
          {mode === "create" ? "添加到覆盖层" : "更新覆盖项"}
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={onCancel}
          data-testid="writer-profile-form-cancel"
        >
          取消
        </Button>
      </div>
    </form>
  );
}

interface FieldProps {
  label: string;
  required?: boolean;
  hint?: string;
  children: React.ReactNode;
}

function Field(props: FieldProps) {
  return (
    <label className="block space-y-1 text-xs">
      <span className="font-medium">
        {props.label}
        {props.required && <span className="text-destructive">*</span>}
      </span>
      {props.children}
      {props.hint && (
        <span className="block text-muted-foreground">{props.hint}</span>
      )}
    </label>
  );
}

function parseJsonObject(text: string): Record<string, unknown> {
  const trimmed = text.trim();
  if (trimmed.length === 0) {
    return {};
  }
  const parsed = JSON.parse(trimmed) as unknown;
  if (
    typeof parsed !== "object" ||
    parsed === null ||
    Array.isArray(parsed)
  ) {
    throw new Error("必须是 JSON object(不能是数组或基本类型)");
  }
  return parsed as Record<string, unknown>;
}
