/**
 * PipelineControlPanel(P6-F-02 RunControlPage 主控)。
 *
 * 顶部状态卡(idle / running / paused / interrupted / stopped / error 6 档
 * 徽章 + 当前 run_id + scope + failure_policy + 进度 + 跳过计数 +
 * pause_reason 白话 + error/interrupted 详情结构化,v2 w1-s4)+ 启动控制
 * 表单(scope 4 档 radio + 对应字段自动显隐 + 失败策略 3 档 radio〔v2
 * w1-s4〕+ Writer Profile 临时覆盖)+ 4 控制按钮(start / pause / resume /
 * stop;resume / stop 认 interrupted,v2 w1-s2 断点续跑)。
 *
 * 字面对齐 FRONTEND_SPEC §4.10:
 * - 当前流水线状态(运行中/暂停/空闲)
 * - 启动控制:从某章开始 / 写 N 章 / 写到某章 / 写完整卷
 * - Writer Profile 临时覆盖(本次运行内有效)
 * - 暂停 / 继续 / 终止 按钮
 *
 * scope 4 档 → 字段映射(对齐 P6-B-02 `_validate_scope_params`):
 * - single:start_chapter_id 必填
 * - range:start_chapter_id + end_chapter_id 必填
 * - volume:volume_id 必填
 * - until:start_chapter_id 必填,end_chapter_id 可选(空则到当前卷末)
 *
 * 按钮可点性(状态驱动,对齐 P6-B-02 错误码 `PIPELINE_ALREADY_RUNNING 409` /
 * `PIPELINE_NOT_RUNNING 409`):
 * - state=idle / stopped / 普通 error:start 可点;pause / resume 禁用(error
 *   状态下 stop 也可点)。连续性 checkpoint 待补的 error 只开放 resume / stop；
 *   卡生成 error 保留原 run，卡片经产品审核修复且现场为空净后开放 resume；
 *   Provider 认证/访问拒绝或审核连接中断且最新正文尚未审稿时，只开放原 run resume
 * - state=running:pause / stop 可点;start 禁用(409)+ resume 禁用(running →
 *   running 是后端幂等但 UI 隐藏避免误操作)
 * - state=paused:resume / stop 可点;start 禁用 + pause 禁用
 *
 * Profile 临时覆盖:**直接 fetch** `/config` profiles(复用 P3-F-03 既有
 * `WriterProfileSelector` 模式);value=null 时 PipelineStartIn.profile_override
 * 也传 null,后端走默认 routing。
 */

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { WriterProfileSelector } from "@/components/WriterProfileSelector";
import {
  PIPELINE_FAILURE_POLICY_OPTIONS,
  PIPELINE_INTERRUPT_REASON_LABELS,
  PIPELINE_PAUSE_REASON_LABELS,
  PIPELINE_SCOPE_OPTIONS,
  PIPELINE_STATE_LABELS,
  type PipelineFailurePolicy,
  type PipelineScope,
  type PipelineStartIn,
  type PipelineStatusOut,
} from "@/api/pipeline";
import { cn } from "@/lib/utils";

const PROVIDER_REVIEW_ERROR_TYPES = new Set([
  "LLMAuthError", "LLMAccessError", "RemoteProtocolError", "ReadTimeout", "SSEStreamError",
]);

interface PipelineControlPanelProps {
  workspaceId: string;
  status: PipelineStatusOut | undefined;
  statusLoading: boolean;
  statusError: Error | null;
  /** "开始"按钮触发,父组件 mutateAsync 后切窗口、订阅 WS。 */
  onStart: (body: PipelineStartIn) => Promise<void>;
  /** "暂停"按钮触发。 */
  onPause: () => Promise<void>;
  /** "继续"按钮触发。 */
  onResume: () => Promise<void>;
  /** "终止"按钮触发。 */
  onStop: () => Promise<void>;
  /** mutation 是否进行中(任意一个),用于按钮 spinner / disabled。 */
  busy: boolean;
}

export function PipelineControlPanel(props: PipelineControlPanelProps) {
  const {
    workspaceId,
    status,
    statusLoading,
    statusError,
    onStart,
    onPause,
    onResume,
    onStop,
    busy,
  } = props;

  // 启动表单 state(scope 4 档 + provider hard cap + profile/failure policy)。
  const [scope, setScope] = useState<PipelineScope>("single");
  const [startChapterId, setStartChapterId] = useState<string>("");
  const [endChapterId, setEndChapterId] = useState<string>("");
  const [volumeId, setVolumeId] = useState<string>("");
  const [profileOverride, setProfileOverride] = useState<string | null>(null);
  const [failurePolicy, setFailurePolicy] =
    useState<PipelineFailurePolicy>("stop");
  const [providerCallCap, setProviderCallCap] = useState<string>("1000");
  const [formError, setFormError] = useState<string | null>(null);

  // status 变化时重置表单错误(避免上次错误持续显示)。
  useEffect(() => {
    setFormError(null);
  }, [status?.state]);

  const state = status?.state ?? "idle";
  const errorDetails = status?.error_details as {
    error_type?: string;
    reason?: string;
    recovery_kind?: string;
    recovery_ready?: boolean;
    summary_refresh_required?: boolean;
  } | null;
  const isContinuityCheckpointRecovery =
    state === "error" &&
    errorDetails?.error_type === "ContinuityLedgerNotReadyError";
  const isCardGenerationRepair =
    state === "error" &&
    errorDetails?.reason === "card_generation_failed" &&
    errorDetails?.recovery_kind === "chapter_card_repair";
  const isCardGenerationRepairReady =
    isCardGenerationRepair && errorDetails?.recovery_ready === true;
  const isProviderReviewRecovery =
    state === "error" &&
    ((errorDetails?.recovery_kind === "known_review_archive_checkpoint") ||
      (PROVIDER_REVIEW_ERROR_TYPES.has(errorDetails?.error_type ?? "") &&
        errorDetails?.recovery_kind === "provider_review_checkpoint"));
  const isProviderReviewRecoveryReady =
    isProviderReviewRecovery && errorDetails?.recovery_ready === true;
  const isCoreStateRecovery =
    state === "error" &&
    errorDetails?.error_type === "CoreStateConflictError" &&
    (errorDetails?.recovery_kind === "core_state_initial_baseline" ||
      errorDetails?.recovery_kind === "core_state_transition_checkpoint");
  const isCoreStateRecoveryReady =
    isCoreStateRecovery && errorDetails?.recovery_ready === true;
  const isMemoryCheckpointRecovery =
    state === "paused" &&
    errorDetails?.error_type === "MemoryEnrichmentNotReadyError";
  const isMemoryCheckpointRecoveryReady =
    isMemoryCheckpointRecovery && errorDetails?.recovery_ready === true;
  // interrupted 挡新 start(后端 409,须先 resume 续跑或 stop 放弃,w1-s2)。
  const canStart =
    !busy &&
    (state === "idle" ||
      state === "stopped" ||
      (state === "error" &&
        !isContinuityCheckpointRecovery &&
        !isCardGenerationRepair &&
        !isProviderReviewRecovery &&
        !isCoreStateRecovery));
  const canPause = !busy && state === "running";
  // interrupted、连续性 checkpoint 及已完成产品修复的卡 error 均续原 run。
  const canResume =
    !busy &&
    ((state === "paused" &&
      (!isMemoryCheckpointRecovery || isMemoryCheckpointRecoveryReady)) ||
      state === "interrupted" ||
      isContinuityCheckpointRecovery ||
      isCardGenerationRepairReady ||
      isProviderReviewRecoveryReady ||
      isCoreStateRecoveryReady);
  // error 状态下也允许 stop(对齐后端 `_load_active_or_error_run`,P6-B-02
  // 决策 7 + P6-B-03 持久化版同语义);interrupted 同理(stop = 放弃续跑)。
  const canStop =
    !busy &&
    (state === "running" ||
      state === "paused" ||
      state === "interrupted" ||
      state === "error");

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setFormError(null);
    // client-side 跨字段约束(对齐后端 P6-B-02 `_validate_scope_params`,
    // 提前 reject 避免无效 422)
    if (scope === "single" && startChapterId.trim().length === 0) {
      setFormError("scope=single 必须填 start_chapter_id");
      return;
    }
    if (
      scope === "range" &&
      (startChapterId.trim().length === 0 || endChapterId.trim().length === 0)
    ) {
      setFormError("scope=range 必须填 start_chapter_id 与 end_chapter_id");
      return;
    }
    if (scope === "volume" && volumeId.trim().length === 0) {
      setFormError("scope=volume 必须填 volume_id");
      return;
    }
    if (scope === "until" && startChapterId.trim().length === 0) {
      setFormError(
        "scope=until 必须填 start_chapter_id(end 可空,默认到所在卷末)",
      );
      return;
    }
    const parsedProviderCallCap = Number(providerCallCap);
    if (
      !Number.isInteger(parsedProviderCallCap) ||
      parsedProviderCallCap < 1
    ) {
      setFormError("provider_call_cap 必须是 >= 1 的整数");
      return;
    }
    const body: PipelineStartIn = {
      scope,
      start_chapter_id: scope === "volume" ? null : startChapterId.trim() || null,
      end_chapter_id:
        scope === "range" || scope === "until"
          ? endChapterId.trim() || null
          : null,
      volume_id: scope === "volume" ? volumeId.trim() || null : null,
      profile_override: profileOverride,
      // v2 w1-s4:失败策略选择器(3 档 radio,默认 stop)。
      failure_policy: failurePolicy,
      provider_call_cap: parsedProviderCallCap,
    };
    await onStart(body);
  };

  return (
    <section className="space-y-4" data-testid="run-control-panel">
      <StatusCard
        status={status}
        loading={statusLoading}
        error={statusError}
      />

      <form
        onSubmit={handleSubmit}
        className="space-y-3 rounded border border-border bg-card p-4"
        data-testid="pipeline-start-form"
      >
        <h3 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          启动控制
        </h3>

        <fieldset className="space-y-1.5" disabled={!canStart}>
          <legend className="text-xs font-medium text-foreground">
            范围(scope)
          </legend>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {PIPELINE_SCOPE_OPTIONS.map((opt) => (
              <label
                key={opt.value}
                className={cn(
                  "flex cursor-pointer items-start gap-1.5 rounded border border-border p-2 text-xs transition-colors",
                  scope === opt.value
                    ? "border-sky-500 bg-sky-50 text-sky-900"
                    : "hover:bg-secondary/50",
                )}
              >
                <input
                  type="radio"
                  name="scope"
                  value={opt.value}
                  checked={scope === opt.value}
                  onChange={() => setScope(opt.value)}
                  data-testid={`scope-radio-${opt.value}`}
                  className="mt-0.5"
                />
                <div>
                  <div className="font-medium">{opt.label}</div>
                  <div className="text-[10px] text-muted-foreground">
                    {opt.hint}
                  </div>
                </div>
              </label>
            ))}
          </div>
        </fieldset>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {(scope === "single" ||
            scope === "range" ||
            scope === "until") && (
            <Field label="start_chapter_id" htmlFor="pipeline-start-ch">
              <Input
                id="pipeline-start-ch"
                data-testid="pipeline-start-chapter-id"
                value={startChapterId}
                onChange={(e) => setStartChapterId(e.target.value)}
                placeholder="例如:ch-0001"
                disabled={!canStart}
              />
            </Field>
          )}
          {scope === "range" && (
            <Field label="end_chapter_id" htmlFor="pipeline-end-ch">
              <Input
                id="pipeline-end-ch"
                data-testid="pipeline-end-chapter-id"
                value={endChapterId}
                onChange={(e) => setEndChapterId(e.target.value)}
                placeholder="例如:ch-0003"
                disabled={!canStart}
              />
            </Field>
          )}
          {scope === "until" && (
            <Field
              label="end_chapter_id(可选)"
              htmlFor="pipeline-end-ch-opt"
              hint="留空默认到所在卷末"
            >
              <Input
                id="pipeline-end-ch-opt"
                data-testid="pipeline-end-chapter-id-opt"
                value={endChapterId}
                onChange={(e) => setEndChapterId(e.target.value)}
                placeholder="留空 = 写到所在卷末"
                disabled={!canStart}
              />
            </Field>
          )}
          {scope === "volume" && (
            <Field label="volume_id" htmlFor="pipeline-volume-id">
              <Input
                id="pipeline-volume-id"
                data-testid="pipeline-volume-id"
                value={volumeId}
                onChange={(e) => setVolumeId(e.target.value)}
                placeholder="例如:vol-1"
                disabled={!canStart}
              />
            </Field>
          )}
        </div>

        <fieldset className="space-y-1.5" disabled={!canStart}>
          <legend className="text-xs font-medium text-foreground">
            失败策略(failure_policy)— 某章写失败/进人工介入时怎么办
          </legend>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            {PIPELINE_FAILURE_POLICY_OPTIONS.map((opt) => (
              <label
                key={opt.value}
                className={cn(
                  "flex cursor-pointer items-start gap-1.5 rounded border border-border p-2 text-xs transition-colors",
                  failurePolicy === opt.value
                    ? "border-sky-500 bg-sky-50 text-sky-900"
                    : "hover:bg-secondary/50",
                )}
              >
                <input
                  type="radio"
                  name="failure-policy"
                  value={opt.value}
                  checked={failurePolicy === opt.value}
                  onChange={() => setFailurePolicy(opt.value)}
                  data-testid={`failure-policy-radio-${opt.value}`}
                  className="mt-0.5"
                />
                <div>
                  <div className="font-medium">{opt.label}</div>
                  <div className="text-[10px] text-muted-foreground">
                    {opt.hint}
                  </div>
                </div>
              </label>
            ))}
          </div>
        </fieldset>

        <Field
          label="provider_call_cap"
          htmlFor="pipeline-provider-call-cap"
          hint="本次 run 的真实 provider 物理调用硬上限；暂停或重启不会清零"
        >
          <Input
            id="pipeline-provider-call-cap"
            data-testid="pipeline-provider-call-cap"
            type="number"
            min={1}
            step={1}
            value={providerCallCap}
            onChange={(event) => setProviderCallCap(event.target.value)}
            disabled={!canStart}
          />
        </Field>

        {workspaceId && (
          <div className="border-t border-dashed border-border pt-2">
            <WriterProfileSelector
              workspaceId={workspaceId}
              value={profileOverride}
              onChange={setProfileOverride}
              disabled={!canStart}
            />
          </div>
        )}

        {formError && (
          <p
            role="alert"
            data-testid="pipeline-form-error"
            className="rounded border border-destructive bg-destructive/10 px-2 py-1 text-xs text-destructive"
          >
            {formError}
          </p>
        )}

        <div
          className="flex flex-wrap gap-2 border-t border-border pt-3"
          data-testid="pipeline-actions"
        >
          <Button
            type="submit"
            size="sm"
            disabled={!canStart}
            data-testid="pipeline-start-btn"
          >
            开始
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={!canPause}
            onClick={() => {
              void onPause();
            }}
            data-testid="pipeline-pause-btn"
          >
            暂停
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={!canResume}
            onClick={() => {
              void onResume();
            }}
            data-testid="pipeline-resume-btn"
          >
            继续
          </Button>
          <Button
            type="button"
            size="sm"
            variant="destructive"
            disabled={!canStop}
            onClick={() => {
              void onStop();
            }}
            data-testid="pipeline-stop-btn"
          >
            终止
          </Button>
        </div>
      </form>
    </section>
  );
}

// ---- StatusCard:顶部状态徽章 + run_id + scope + 进度 -------------------

interface StatusCardProps {
  status: PipelineStatusOut | undefined;
  loading: boolean;
  error: Error | null;
}

function StatusCard(props: StatusCardProps) {
  const { status, loading, error } = props;

  if (loading) {
    return (
      <div
        data-testid="pipeline-status-card"
        className="rounded border border-border bg-card p-4 text-sm text-muted-foreground"
      >
        加载流水线状态…
      </div>
    );
  }
  if (error) {
    return (
      <div
        data-testid="pipeline-status-card"
        className="rounded border border-destructive bg-destructive/10 p-4 text-sm text-destructive"
      >
        读取流水线状态失败:{error.message}
      </div>
    );
  }
  if (!status) {
    return null;
  }

  const stateLabel = PIPELINE_STATE_LABELS[status.state];
  const stateColor = stateColorFor(status.state);
  const progressPct =
    typeof status.progress === "number"
      ? Math.round(status.progress * 100)
      : 0;
  const completedCount = status.completed_chapter_ids.length;
  const skippedCount = status.skipped_chapter_ids.length;
  const totalCount = status.chapter_ids.length;
  const pauseReasonLabel = status.pause_reason
    ? PIPELINE_PAUSE_REASON_LABELS[status.pause_reason] ?? status.pause_reason
    : null;

  return (
    <div
      data-testid="pipeline-status-card"
      className="space-y-2 rounded border border-border bg-card p-4"
    >
      <div className="flex flex-wrap items-baseline gap-3">
        <span
          data-testid="pipeline-state-badge"
          className={cn(
            "rounded px-2 py-0.5 text-xs font-medium",
            stateColor,
          )}
        >
          {stateLabel}
        </span>
        {status.run_id && (
          <span className="text-xs text-muted-foreground">
            run_id:{" "}
            <code className="break-all" data-testid="pipeline-run-id">
              {status.run_id}
            </code>
          </span>
        )}
        {status.scope && (
          <span className="text-xs text-muted-foreground">
            scope: <code>{status.scope}</code>
          </span>
        )}
        {status.run_id && (
          <span
            className="text-xs text-muted-foreground"
            data-testid="pipeline-failure-policy"
          >
            失败策略: <code>{status.failure_policy}</code>
          </span>
        )}
      </div>

      {status.run_id && (
        <div
          className="grid grid-cols-2 gap-1 text-xs text-muted-foreground sm:grid-cols-4"
          data-testid="pipeline-attempt-counters"
        >
          <span>
            provider: {status.provider_call_count}/{status.provider_call_cap}
          </span>
          <span>transport retry: {status.transport_retry_count}</span>
          <span>pipeline attempt: {status.pipeline_attempt_count}</span>
          <span>
            upstream generation: {status.upstream_generation_attempt_count}
          </span>
        </div>
      )}

      {status.run_id && (
        <div
          className="space-y-1 rounded border border-border bg-muted/20 px-2 py-1.5 text-xs text-muted-foreground"
          data-testid="pipeline-attempt-accounting"
        >
          <p>
            Provider usage: {status.attempt_usage_cost.input_tokens_known} in /{" "}
            {status.attempt_usage_cost.output_tokens_known} out
          </p>
          <p>
            known {status.attempt_usage_cost.usage_known_attempts} · unknown{" "}
            {status.attempt_usage_cost.usage_unknown_attempts} · legacy{" "}
            {status.attempt_usage_cost.legacy_unclassified_attempts} · request ID{" "}
            {status.attempt_usage_cost.provider_request_id_count}
          </p>
          <p>
            配置估算:{" "}
            {status.attempt_usage_cost.configured_estimates.length > 0
              ? status.attempt_usage_cost.configured_estimates
                  .map(
                    (item) =>
                      `${item.currency} ${item.amount.toFixed(4)}`,
                  )
                  .join(" · ")
              : "无"}
          </p>
          <p>
            估算依据:{" "}
            {Object.entries(status.attempt_usage_cost.configured_cost_basis_counts)
              .map(([basis, count]) => `${basis} ${count}`)
              .join(" · ") || "无"}
          </p>
          <p>实际账单: 未导入；不能由 usage 推算。</p>
        </div>
      )}

      {totalCount > 0 && (
        <div className="space-y-1">
          <div className="flex justify-between text-xs text-muted-foreground">
            <span>
              进度:{completedCount} / {totalCount} 章({progressPct}%)
              {skippedCount > 0 && (
                <span
                  className="ml-1 text-orange-600"
                  data-testid="pipeline-skipped-count"
                >
                  · 跳过 {skippedCount} 章
                </span>
              )}
            </span>
            {status.current_chapter && (
              <span>
                当前章节:
                <code data-testid="pipeline-current-chapter">
                  {status.current_chapter}
                </code>
              </span>
            )}
          </div>
          <div className="h-1.5 overflow-hidden rounded bg-secondary">
            <div
              className="h-full bg-sky-500 transition-all"
              style={{ width: `${progressPct}%` }}
              data-testid="pipeline-progress-bar"
            />
          </div>
        </div>
      )}

      {pauseReasonLabel && (
        <p
          className="text-xs text-amber-700"
          data-testid="pipeline-pause-reason"
        >
          暂停原因:{pauseReasonLabel}
          <code className="ml-1 text-[10px] text-muted-foreground">
            ({status.pause_reason})
          </code>
        </p>
      )}
      {status.state === "paused" &&
        status.pause_reason === "continuity_ledger_not_ready" && (
          <ContinuityCheckpointRecoveryNotice
            chapterId={status.current_chapter}
          />
        )}
      <ErrorDetailsBlock
        state={status.state}
        errorDetails={status.error_details}
      />
    </div>
  );
}

/**
 * error / interrupted 详情结构化展示(v2 w1-s4)。
 *
 * - interrupted(w1-s2/s3):按 `reason` 白话翻译;`chapter_watchdog_timeout`
 *   额外列 chapter_id / elapsed_seconds / timeout_seconds。
 * - error:列 error_type / message / chapter_id。
 * - 未知形态兜底:原始 JSON(前向兼容)。
 */
function ErrorDetailsBlock(props: {
  state: PipelineStatusOut["state"];
  errorDetails: PipelineStatusOut["error_details"];
}) {
  const { state, errorDetails } = props;
  if (!errorDetails) return null;

  const details = errorDetails as {
    reason?: string;
    chapter_id?: string;
    elapsed_seconds?: number;
    timeout_seconds?: number;
    error_type?: string;
    message?: string;
    recovery_kind?: string;
    recovery_ready?: boolean;
    summary_refresh_required?: boolean;
    recovery_block_reason?: string;
    status_code?: number;
    provider_code?: string;
    provider_message?: string;
    provider_request_id?: string;
    memory_status?: string;
    failure_stages?: string[];
    failure_types?: string[];
    result_unknown?: boolean;
  };

  if (state === "interrupted" && details.reason) {
    const reasonLabel =
      PIPELINE_INTERRUPT_REASON_LABELS[details.reason] ?? details.reason;
    return (
      <div
        className="space-y-1 rounded border border-orange-300 bg-orange-50 px-2 py-1.5 text-xs text-orange-900"
        data-testid="pipeline-interrupt-details"
      >
        <p>
          中断原因:{reasonLabel}
          <code className="ml-1 text-[10px] text-orange-700">
            ({details.reason})
          </code>
        </p>
        {details.reason === "chapter_watchdog_timeout" && (
          <p data-testid="pipeline-watchdog-details">
            超时章节 <code>{details.chapter_id ?? "?"}</code>:已耗时{" "}
            {details.elapsed_seconds ?? "?"} 秒,超过预算{" "}
            {details.timeout_seconds ?? "?"} 秒
          </p>
        )}
      </div>
    );
  }

  if (
    state === "paused" &&
    details.error_type === "MemoryEnrichmentNotReadyError"
  ) {
    return (
      <MemoryCheckpointRecoveryNotice
        chapterId={details.chapter_id}
        status={details.memory_status}
        ready={details.recovery_ready === true}
        blockReason={details.recovery_block_reason}
        failureStages={details.failure_stages}
        failureTypes={details.failure_types}
        resultUnknown={details.result_unknown === true}
      />
    );
  }

  if (
    state === "error" &&
    details.error_type === "ContinuityLedgerNotReadyError"
  ) {
    return (
      <ContinuityCheckpointRecoveryNotice
        chapterId={details.chapter_id}
        message={details.message}
      />
    );
  }

  if (
    state === "error" &&
    details.error_type === "CoreStateConflictError" &&
    (details.recovery_kind === "core_state_initial_baseline" ||
      details.recovery_kind === "core_state_transition_checkpoint")
  ) {
    return (
      <CoreStateRecoveryNotice
        chapterId={details.chapter_id}
        message={details.message}
        ready={details.recovery_ready === true}
        blockReason={details.recovery_block_reason}
        recoveryKind={details.recovery_kind}
        summaryRefresh={details.summary_refresh_required === true}
      />
    );
  }

  if (
    state === "error" &&
    (details.recovery_kind === "known_review_archive_checkpoint" ||
      (PROVIDER_REVIEW_ERROR_TYPES.has(details.error_type ?? "") &&
        details.recovery_kind === "provider_review_checkpoint"))
  ) {
    return (
      <ProviderReviewRecoveryNotice
        archived={details.recovery_kind === "known_review_archive_checkpoint"}
        chapterId={details.chapter_id}
        message={details.message}
        ready={details.recovery_ready === true}
        blockReason={details.recovery_block_reason}
        statusCode={details.status_code}
        providerCode={details.provider_code}
        providerMessage={details.provider_message}
        providerRequestId={details.provider_request_id}
      />
    );
  }

  if (
    state === "error" &&
    details.reason === "card_generation_failed" &&
    details.recovery_kind === "chapter_card_repair"
  ) {
    return (
      <CardGenerationRecoveryNotice
        chapterId={details.chapter_id}
        message={details.message}
        ready={details.recovery_ready === true}
        blockReason={details.recovery_block_reason}
      />
    );
  }

  if (details.error_type || details.message) {
    return (
      <div
        className="space-y-0.5 rounded border border-destructive/40 bg-destructive/5 px-2 py-1.5 text-xs text-destructive"
        data-testid="pipeline-error-details"
      >
        <p>
          错误类型:<code>{details.error_type ?? "unknown"}</code>
          {details.chapter_id && (
            <>
              {" "}
              · 章节 <code>{details.chapter_id}</code>
            </>
          )}
        </p>
        {details.message && <p className="break-words">{details.message}</p>}
      </div>
    );
  }

  // 未知形态:原始 JSON 兜底(前向兼容)。
  return (
    <p
      className="rounded border border-destructive/40 bg-destructive/5 px-2 py-1 text-xs text-destructive"
      data-testid="pipeline-error-details"
    >
      错误详情:
      <code className="break-all">{JSON.stringify(errorDetails)}</code>
    </p>
  );
}

function MemoryCheckpointRecoveryNotice(props: {
  chapterId?: string;
  status?: string;
  ready: boolean;
  blockReason?: string;
  failureStages?: string[];
  failureTypes?: string[];
  resultUnknown: boolean;
}) {
  const isRunning = props.status === "running";
  const isPending = props.status === "pending";
  const isSafeRetry = props.status === "failed" && props.ready;
  return (
    <div
      className="space-y-1 rounded border border-amber-300 bg-amber-50 px-2 py-1.5 text-xs text-amber-900"
      data-testid="pipeline-memory-recovery"
    >
      <p className="font-medium">正文和审核已保留，停在富记忆 checkpoint</p>
      <p>
        {isSafeRetry
          ? "系统已确认这是已知失败、数据库已完整回滚；点「继续」只重跑富记忆步骤，不重写正文、章节卡或审核。"
          : isPending
            ? "富记忆尚未取得 Worker；点「继续」会通过同一原子租约恢复，重复操作不会制造重复请求。"
            : isRunning
              ? "已有富记忆 Worker 正在处理；系统不会另起一个重复 Worker，完成后再点「继续」。"
              : "当前现场不满足自动恢复条件；系统会保留现场并拒绝盲目重放，需先查明失败原因。"}
      </p>
      <p>JSON 字符和字段合法性由程序校验，不需要人工逐字找引号或逗号。</p>
      <p>
        章节:<code>{props.chapterId ?? "?"}</code> · 状态:
        <code>{props.status ?? "unknown"}</code>
      </p>
      {(props.failureStages?.length || props.failureTypes?.length) && (
        <p data-testid="pipeline-memory-failure-summary">
          失败阶段:
          <code>{props.failureStages?.join(", ") || "unknown"}</code>
          {" · "}
          类型:<code>{props.failureTypes?.join(", ") || "unknown"}</code>
        </p>
      )}
      {!props.ready && props.blockReason && (
        <p>
          自动恢复阻断:<code>{props.blockReason}</code>
          {props.resultUnknown ? "（存在未知副作用）" : ""}
        </p>
      )}
    </div>
  );
}

function ContinuityCheckpointRecoveryNotice(props: {
  chapterId?: string | null;
  message?: string;
}) {
  return (
    <div
      className="space-y-1 rounded border border-amber-300 bg-amber-50 px-2 py-1.5 text-xs text-amber-900"
      data-testid="pipeline-continuity-checkpoint-recovery"
    >
      <p className="font-medium">正文已通过，只补连续性 checkpoint</p>
      <p>
        已批准的正文和版本会保留；请点「继续」从原 run
        补齐连续性小账本，不要重新开始本章写作。
      </p>
      {props.chapterId && (
        <p>
          待恢复章节:<code>{props.chapterId}</code>
        </p>
      )}
      {props.message && (
        <p className="break-words text-amber-800">{props.message}</p>
      )}
    </div>
  );
}

function CardGenerationRecoveryNotice(props: {
  chapterId?: string;
  message?: string;
  ready: boolean;
  blockReason?: string;
}) {
  return (
    <div
      className="space-y-1 rounded border border-amber-300 bg-amber-50 px-2 py-1.5 text-xs text-amber-900"
      data-testid="pipeline-card-repair-recovery"
    >
      <p className="font-medium">章节卡修复保留原 run</p>
      <p>
        {props.ready
          ? "卡片已通过产品审核且写前现场为空净，请点「继续」从同一 run 写本章。"
          : "请先在章节卡页修复并批准本章；完成前不会开放「开始」或「继续」。"}
      </p>
      {props.chapterId && (
        <p>
          待恢复章节:<code>{props.chapterId}</code>
        </p>
      )}
      {props.blockReason === "checkpoint_not_clean" && (
        <p className="text-destructive">
          检测到正文、租约或 attempt 现场不为空净，已安全阻止恢复。
        </p>
      )}
      {props.message && (
        <p className="break-words text-amber-800">{props.message}</p>
      )}
    </div>
  );
}

function CoreStateRecoveryNotice(props: {
  chapterId?: string;
  message?: string;
  ready: boolean;
  blockReason?: string;
  recoveryKind?: string;
  summaryRefresh?: boolean;
}) {
  const isTransitionCheckpoint =
    props.recoveryKind === "core_state_transition_checkpoint";
  return (
    <div
      className="space-y-1 rounded border border-amber-300 bg-amber-50 px-2 py-1.5 text-xs text-amber-900"
      data-testid="pipeline-core-state-recovery"
    >
      <p className="font-medium">
        {props.summaryRefresh
          ? "正文已保留，只重新提取状态摘要"
          : isTransitionCheckpoint
          ? "正文已保留，只补批准的核心状态迁移"
          : "正文已保留，补齐开篇前角色状态基线"}
      </p>
      <p>
        {props.ready && props.summaryRefresh
          ? "已核对批准卡、正文版本与调用账本；请点「继续」重新提取摘要并完成入库。摘要会调用模型并计入原预算，旧摘要保留，正文与审核结果不变。"
          : isTransitionCheckpoint
          ? props.ready
            ? props.summaryRefresh
              ? "已核对批准卡、正文版本与调用账本；请点「继续」重新提取摘要并完成入库。摘要会调用模型并计入原预算，旧摘要保留，正文与审核结果不变。"
              : "批准卡、正文、版本、小账本和 transition 已精确对齐；请点「继续」从同一 run 处理剩余核心步骤。含混的首次关系登记会由原出卡器核对并计入原预算，其他本地迁移不增加模型调用；不重写正文、不重审、不重跑摘要。"
            : "批准卡、正文候选、版本、小账本、租约或 attempt 账本未能全部对齐，已安全阻止恢复。"
          : props.ready
            ? "系统已用两个独立的上游逐字证据建立基线，并重新冻结章节卡；请点「继续」从同一 run 提交本地 checkpoint，不重写正文、不重审。"
            : props.blockReason === "card_not_reapproved"
              ? "请先到章节卡审核页：拒绝当前卡，再重新批准。系统只会在双上游证据与现有正文完全一致时机械补基线。"
              : "来源、版本、章节卡、租约或 attempt 账本未能全部对齐，已安全阻止恢复。"}
      </p>
      {props.chapterId && (
        <p>
          待恢复章节:<code>{props.chapterId}</code>
        </p>
      )}
      {props.message && (
        <p className="break-words text-amber-800">{props.message}</p>
      )}
    </div>
  );
}

function ProviderReviewRecoveryNotice(props: {
  archived?: boolean;
  chapterId?: string;
  message?: string;
  ready: boolean;
  blockReason?: string;
  statusCode?: number;
  providerCode?: string;
  providerMessage?: string;
  providerRequestId?: string;
}) {
  return (
    <div
      className="space-y-1 rounded border border-amber-300 bg-amber-50 px-2 py-1.5 text-xs text-amber-900"
      data-testid="pipeline-provider-review-recovery"
    >
      <p className="font-medium">{props.archived ? "正文与已有审核结果已保留" : "正文已保留，只恢复未完成的 Reviewer"}</p>
      <p>
        {props.ready && props.archived
          ? "点「继续」可复用已保存的审核响应，只补完尚未完成的检查，不会重新写正文。"
          : props.ready
          ? "请先处理上方错误所示的连接、认证、权限或配额问题，再点「继续」；系统会复用同一 run 和当前正文版本，只重试 Reviewer，不会重放 Writer。"
          : "Reviewer checkpoint 现场不完整，已禁止「开始」和「继续」以避免重复付费。"}
      </p>
      {props.chapterId && (
        <p>
          待恢复章节:<code>{props.chapterId}</code>
        </p>
      )}
      {(props.statusCode || props.providerCode || props.providerRequestId) && (
        <p data-testid="pipeline-provider-error-fields">
          {props.statusCode && <>HTTP {props.statusCode}</>}
          {props.providerCode && (
            <>
              {" "}· code <code>{props.providerCode}</code>
            </>
          )}
          {props.providerRequestId && (
            <>
              {" "}· request <code>{props.providerRequestId}</code>
            </>
          )}
        </p>
      )}
      {props.providerMessage && (
        <p className="break-words text-amber-800">{props.providerMessage}</p>
      )}
      {props.blockReason === "checkpoint_not_clean" && (
        <p className="text-destructive">
          检测到版本、审稿、租约或 attempt 账本不一致，已安全阻止恢复。
        </p>
      )}
      {props.message && !props.providerMessage && (
        <p className="break-words text-amber-800">{props.message}</p>
      )}
    </div>
  );
}

function stateColorFor(state: PipelineStatusOut["state"]): string {
  switch (state) {
    case "running":
      return "bg-sky-100 text-sky-800";
    case "paused":
      return "bg-amber-100 text-amber-800";
    case "interrupted":
      return "bg-orange-100 text-orange-800";
    case "stopped":
      return "bg-emerald-100 text-emerald-800";
    case "error":
      return "bg-destructive/10 text-destructive";
    case "idle":
    default:
      return "bg-secondary text-secondary-foreground";
  }
}

// ---- Field 轻量包装 ----------------------------------------------------

interface FieldProps {
  label: string;
  htmlFor: string;
  hint?: string;
  children: React.ReactNode;
}

function Field(props: FieldProps) {
  return (
    <div className="space-y-1">
      <label
        htmlFor={props.htmlFor}
        className="block text-xs font-medium text-foreground"
      >
        {props.label}
      </label>
      {props.children}
      {props.hint && (
        <p className="text-[10px] text-muted-foreground">{props.hint}</p>
      )}
    </div>
  );
}
