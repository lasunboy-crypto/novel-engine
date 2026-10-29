/**
 * PipelineControlPanel 防回退测(v2 w1-s4 Run 面板完整化)。
 *
 * 钉住(plan §4 w1-s4 卡防回退护栏字面①"失败策略表单提交体"):
 * 1. 失败策略 3 档 radio 渲染 + 默认 stop
 * 2. 默认提交体 failure_policy="stop"
 * 3. 选 skip 后提交体 failure_policy="skip"(w1-s1 后端透传不回退)
 * 4. interrupted 态按钮语义:resume / stop 可点,start / pause 禁用
 *    (w1-s2 断点续跑 UI 入口)
 * 5. StatusCard pause_reason 白话化 + watchdog error_details 结构化
 *    (w1-s3 看门狗/成本护栏状态展示)+ skipped 计数
 *
 * WriterProfileSelector 内部走 useWorkspaceConfigQuery → vi.mock 掉整个
 * 组件,避免引入 QueryClientProvider(对齐 NewBookForm.test 手法)。
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import type { PipelineStatusOut } from "@/api/pipeline";

vi.mock("@/components/WriterProfileSelector", () => ({
  WriterProfileSelector: () => null,
}));

import { PipelineControlPanel } from "@/components/PipelineControlPanel";

function _status(overrides: Partial<PipelineStatusOut>): PipelineStatusOut {
  return {
    run_id: "run_0123456789abcdef",
    workspace_id: "ws-demo",
    state: "running",
    scope: "range",
    chapter_ids: ["ch-0001", "ch-0002", "ch-0003"],
    current_chapter: "ch-0002",
    progress: 0.33,
    completed_chapter_ids: ["ch-0001"],
    profile_override: null,
    pause_reason: null,
    error_details: null,
    failure_policy: "stop",
    skipped_chapter_ids: [],
    provider_call_cap: 1000,
    provider_call_count: 0,
    transport_retry_count: 0,
    pipeline_attempt_count: 0,
    upstream_generation_attempt_count: 0,
    attempt_usage_cost: {
      usage_known_attempts: 0,
      usage_unknown_attempts: 0,
      legacy_unclassified_attempts: 0,
      input_tokens_known: 0,
      output_tokens_known: 0,
      provider_request_id_count: 0,
      response_status_counts: {},
      configured_cost_basis_counts: {},
      configured_estimates: [],
      actual_billed: {
        status: "not_recorded",
        amount: null,
        currency: null,
      },
    },
    ...overrides,
  };
}

function renderPanel(status: PipelineStatusOut | undefined) {
  const onStart = vi.fn().mockResolvedValue(undefined);
  const onPause = vi.fn().mockResolvedValue(undefined);
  const onResume = vi.fn().mockResolvedValue(undefined);
  const onStop = vi.fn().mockResolvedValue(undefined);
  render(
    <PipelineControlPanel
      workspaceId="ws-demo"
      status={status}
      statusLoading={false}
      statusError={null}
      onStart={onStart}
      onPause={onPause}
      onResume={onResume}
      onStop={onStop}
      busy={false}
    />,
  );
  return { onStart, onPause, onResume, onStop };
}

describe("PipelineControlPanel 失败策略选择器(w1-s4)", () => {
  it("渲染 3 档 radio,默认选中 stop", () => {
    renderPanel(_status({ state: "idle", run_id: null, chapter_ids: [] }));
    const stop = screen.getByTestId(
      "failure-policy-radio-stop",
    ) as HTMLInputElement;
    const skip = screen.getByTestId(
      "failure-policy-radio-skip",
    ) as HTMLInputElement;
    const retry = screen.getByTestId(
      "failure-policy-radio-retry",
    ) as HTMLInputElement;
    expect(stop.checked).toBe(true);
    expect(skip.checked).toBe(false);
    expect(retry.checked).toBe(false);
  });

  it("默认提交体 failure_policy='stop'", async () => {
    const { onStart } = renderPanel(
      _status({ state: "idle", run_id: null, chapter_ids: [] }),
    );
    fireEvent.change(screen.getByTestId("pipeline-start-chapter-id"), {
      target: { value: "ch-0001" },
    });
    fireEvent.click(screen.getByTestId("pipeline-start-btn"));
    await vi.waitFor(() => expect(onStart).toHaveBeenCalledTimes(1));
    expect(onStart.mock.calls[0][0]).toMatchObject({
      scope: "single",
      start_chapter_id: "ch-0001",
      failure_policy: "stop",
      provider_call_cap: 1000,
    });
  });

  it("选 skip 后提交体 failure_policy='skip'", async () => {
    const { onStart } = renderPanel(
      _status({ state: "idle", run_id: null, chapter_ids: [] }),
    );
    fireEvent.click(screen.getByTestId("failure-policy-radio-skip"));
    fireEvent.change(screen.getByTestId("pipeline-start-chapter-id"), {
      target: { value: "ch-0001" },
    });
    fireEvent.click(screen.getByTestId("pipeline-start-btn"));
    await vi.waitFor(() => expect(onStart).toHaveBeenCalledTimes(1));
    expect(onStart.mock.calls[0][0]).toMatchObject({
      failure_policy: "skip",
    });
  });

  it("provider_call_cap 可设定并进入启动请求", async () => {
    const { onStart } = renderPanel(
      _status({ state: "idle", run_id: null, chapter_ids: [] }),
    );
    fireEvent.change(screen.getByTestId("pipeline-start-chapter-id"), {
      target: { value: "ch-0001" },
    });
    fireEvent.change(screen.getByTestId("pipeline-provider-call-cap"), {
      target: { value: "96" },
    });
    fireEvent.click(screen.getByTestId("pipeline-start-btn"));
    await vi.waitFor(() => expect(onStart).toHaveBeenCalledTimes(1));
    expect(onStart.mock.calls[0][0]).toMatchObject({
      provider_call_cap: 96,
    });
  });
});

describe("PipelineControlPanel interrupted 态按钮语义(w1-s2 续跑)", () => {
  it("interrupted:resume / stop 可点,start / pause 禁用", () => {
    renderPanel(
      _status({
        state: "interrupted",
        error_details: { reason: "engine_restarted_mid_run" },
      }),
    );
    expect(screen.getByTestId("pipeline-resume-btn")).not.toBeDisabled();
    expect(screen.getByTestId("pipeline-stop-btn")).not.toBeDisabled();
    expect(screen.getByTestId("pipeline-start-btn")).toBeDisabled();
    expect(screen.getByTestId("pipeline-pause-btn")).toBeDisabled();
  });

  it("interrupted:点继续触发 onResume(续跑入口不回退)", () => {
    const { onResume } = renderPanel(
      _status({
        state: "interrupted",
        error_details: { reason: "engine_restarted_mid_run" },
      }),
    );
    fireEvent.click(screen.getByTestId("pipeline-resume-btn"));
    expect(onResume).toHaveBeenCalledTimes(1);
  });
});

describe("PipelineControlPanel 连续性 checkpoint 恢复", () => {
  it("只开放继续/终止，并说明正文已通过且只补 checkpoint", () => {
    const { onStart, onResume } = renderPanel(
      _status({
        state: "error",
        error_details: {
          error_type: "ContinuityLedgerNotReadyError",
          message: "continuity ledger extraction failed",
          chapter_id: "ch-0001",
        },
      }),
    );

    expect(screen.getByTestId("pipeline-start-btn")).toBeDisabled();
    expect(screen.getByTestId("pipeline-pause-btn")).toBeDisabled();
    expect(screen.getByTestId("pipeline-resume-btn")).not.toBeDisabled();
    expect(screen.getByTestId("pipeline-stop-btn")).not.toBeDisabled();

    const recovery = screen.getByTestId(
      "pipeline-continuity-checkpoint-recovery",
    );
    expect(recovery).toHaveTextContent("正文已通过，只补连续性 checkpoint");
    expect(recovery).toHaveTextContent("ch-0001");

    fireEvent.click(screen.getByTestId("pipeline-start-btn"));
    expect(onStart).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("pipeline-resume-btn"));
    expect(onResume).toHaveBeenCalledTimes(1);
  });

  it("未来同类故障的 paused 状态也显示只补 checkpoint 并开放继续", () => {
    renderPanel(
      _status({
        state: "paused",
        pause_reason: "continuity_ledger_not_ready",
        current_chapter: "ch-0002",
      }),
    );

    expect(screen.getByTestId("pipeline-start-btn")).toBeDisabled();
    expect(screen.getByTestId("pipeline-resume-btn")).not.toBeDisabled();
    expect(screen.getByTestId("pipeline-pause-reason")).toHaveTextContent(
      "正文已通过,连续性 checkpoint 待补",
    );
    expect(
      screen.getByTestId("pipeline-continuity-checkpoint-recovery"),
    ).toHaveTextContent("ch-0002");
  });

  it("checkpoint 损坏明确显示安全暂停，不伪装成普通失败", () => {
    renderPanel(
      _status({
        state: "paused",
        pause_reason: "continuity_checkpoint_corruption",
        current_chapter: "ch-0001",
      }),
    );

    expect(screen.getByTestId("pipeline-pause-reason")).toHaveTextContent(
      "连续性 checkpoint 损坏,已安全暂停并保留现场",
    );
    expect(screen.getByTestId("pipeline-start-btn")).toBeDisabled();
  });

  it.each([
    ["memory_enrichment_not_ready", "上一章富记忆仍在处理"],
    ["memory_enrichment_failed", "上一章富记忆未成功"],
  ])("富记忆暂停原因 %s 使用可行动白话", (pauseReason, label) => {
    renderPanel(
      _status({
        state: "paused",
        pause_reason: pauseReason,
        current_chapter: "ch-0002",
      }),
    );

    const reason = screen.getByTestId("pipeline-pause-reason");
    expect(reason).toHaveTextContent(label);
    expect(reason).toHaveTextContent(pauseReason);
    expect(screen.getByTestId("pipeline-resume-btn")).not.toBeDisabled();
  });

  it("已知富记忆失败只开放 checkpoint 恢复，并展示程序化校验结论", () => {
    renderPanel(
      _status({
        state: "paused",
        pause_reason: "memory_enrichment_failed",
        current_chapter: "ch-0003",
        error_details: {
          reason: "memory_enrichment_failed",
          error_type: "MemoryEnrichmentNotReadyError",
          chapter_id: "ch-0003",
          memory_status: "failed",
          recovery_kind: "memory_retry_safe",
          recovery_ready: true,
          recovery_block_reason: null,
          failure_stages: ["memory_extractor"],
          failure_types: ["MemoryExtractionError"],
          result_unknown: false,
        },
      }),
    );

    const recovery = screen.getByTestId("pipeline-memory-recovery");
    expect(recovery).toHaveTextContent("正文和审核已保留");
    expect(recovery).toHaveTextContent("只重跑富记忆步骤");
    expect(recovery).toHaveTextContent("不重写正文、章节卡或审核");
    expect(recovery).toHaveTextContent("不需要人工逐字找引号或逗号");
    expect(
      screen.getByTestId("pipeline-memory-failure-summary"),
    ).toHaveTextContent("memory_extractor");
    expect(screen.getByTestId("pipeline-resume-btn")).not.toBeDisabled();
  });

  it("未知副作用的富记忆失败明确拒绝盲目重放", () => {
    renderPanel(
      _status({
        state: "paused",
        pause_reason: "memory_enrichment_failed",
        current_chapter: "ch-0003",
        error_details: {
          reason: "memory_enrichment_failed",
          error_type: "MemoryEnrichmentNotReadyError",
          chapter_id: "ch-0003",
          memory_status: "failed",
          recovery_kind: "memory_manual_review",
          recovery_ready: false,
          recovery_block_reason: "unknown_side_effects",
          result_unknown: true,
        },
      }),
    );

    const recovery = screen.getByTestId("pipeline-memory-recovery");
    expect(recovery).toHaveTextContent("拒绝盲目重放");
    expect(recovery).toHaveTextContent("unknown_side_effects");
    expect(recovery).toHaveTextContent("存在未知副作用");
    expect(screen.getByTestId("pipeline-resume-btn")).toBeDisabled();
  });
});

describe("PipelineControlPanel 章节卡修复恢复", () => {
  it("卡片尚未批准时保留原 run，开始和继续都禁用", () => {
    renderPanel(
      _status({
        state: "error",
        error_details: {
          reason: "card_generation_failed",
          message: "card contract ambiguous",
          chapter_id: "ch-0003",
          recovery_kind: "chapter_card_repair",
          recovery_ready: false,
          recovery_block_reason: "chapter_card_not_approved",
        },
      }),
    );

    expect(screen.getByTestId("pipeline-start-btn")).toBeDisabled();
    expect(screen.getByTestId("pipeline-resume-btn")).toBeDisabled();
    expect(
      screen.getByTestId("pipeline-card-repair-recovery"),
    ).toHaveTextContent("请先在章节卡页修复并批准本章");
  });

  it("卡片批准且写前现场为空净时只开放同 run 继续", () => {
    const { onStart, onResume } = renderPanel(
      _status({
        state: "error",
        error_details: {
          reason: "card_generation_failed",
          message: "card contract ambiguous",
          chapter_id: "ch-0003",
          recovery_kind: "chapter_card_repair",
          recovery_ready: true,
        },
      }),
    );

    expect(screen.getByTestId("pipeline-start-btn")).toBeDisabled();
    expect(screen.getByTestId("pipeline-resume-btn")).not.toBeDisabled();
    expect(
      screen.getByTestId("pipeline-card-repair-recovery"),
    ).toHaveTextContent("请点「继续」从同一 run 写本章");
    fireEvent.click(screen.getByTestId("pipeline-start-btn"));
    expect(onStart).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("pipeline-resume-btn"));
    expect(onResume).toHaveBeenCalledTimes(1);
  });

  it("现场不为空净时显示安全阻止，不开放继续", () => {
    renderPanel(
      _status({
        state: "error",
        error_details: {
          reason: "card_generation_failed",
          chapter_id: "ch-0003",
          recovery_kind: "chapter_card_repair",
          recovery_ready: false,
          recovery_block_reason: "checkpoint_not_clean",
        },
      }),
    );

    expect(screen.getByTestId("pipeline-start-btn")).toBeDisabled();
    expect(screen.getByTestId("pipeline-resume-btn")).toBeDisabled();
    expect(
      screen.getByTestId("pipeline-card-repair-recovery"),
    ).toHaveTextContent("已安全阻止恢复");
  });
});

describe("PipelineControlPanel Provider Reviewer checkpoint 恢复", () => {
  it("现场完整时只开放同 run 继续，并说明不重放 Writer", () => {
    const { onStart, onResume } = renderPanel(
      _status({
        state: "error",
        current_chapter: "ch-0007",
        error_details: {
          error_type: "LLMAccessError",
          message: "provider access denied",
          chapter_id: "ch-0007",
          recovery_kind: "provider_review_checkpoint",
          recovery_ready: true,
          status_code: 403,
          provider_code: "AllocationQuota.FreeTierOnly",
          provider_message: "Free quota exhausted",
          provider_request_id: "req-provider-403",
        },
      }),
    );

    expect(screen.getByTestId("pipeline-start-btn")).toBeDisabled();
    expect(screen.getByTestId("pipeline-pause-btn")).toBeDisabled();
    expect(screen.getByTestId("pipeline-resume-btn")).not.toBeDisabled();
    expect(screen.getByTestId("pipeline-stop-btn")).not.toBeDisabled();
    const recovery = screen.getByTestId("pipeline-provider-review-recovery");
    expect(recovery).toHaveTextContent("只恢复未完成的 Reviewer");
    expect(recovery).toHaveTextContent("不会重放 Writer");
    expect(recovery).toHaveTextContent("ch-0007");
    const fields = screen.getByTestId("pipeline-provider-error-fields");
    expect(fields).toHaveTextContent("HTTP 403");
    expect(fields).toHaveTextContent("AllocationQuota.FreeTierOnly");
    expect(fields).toHaveTextContent("req-provider-403");

    fireEvent.click(screen.getByTestId("pipeline-start-btn"));
    expect(onStart).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("pipeline-resume-btn"));
    expect(onResume).toHaveBeenCalledTimes(1);
  });

  it.each(["RemoteProtocolError", "ReadTimeout", "SSEStreamError"])(
    "%s 只在后端确认可恢复时开放原 run 继续",
    (errorType) => {
      const { onStart, onResume } = renderPanel(_status({
        state: "error", current_chapter: "ch-0002",
        error_details: {
          error_type: errorType, message: "connection interrupted",
          chapter_id: "ch-0002", recovery_kind: "provider_review_checkpoint",
          recovery_ready: true,
        },
      }));
      expect(screen.getByTestId("pipeline-start-btn")).toBeDisabled();
      expect(screen.getByTestId("pipeline-resume-btn")).not.toBeDisabled();
      expect(screen.getByTestId("pipeline-provider-review-recovery")).toHaveTextContent("不会重放 Writer");
      fireEvent.click(screen.getByTestId("pipeline-start-btn"));
      fireEvent.click(screen.getByTestId("pipeline-resume-btn"));
      expect(onStart).not.toHaveBeenCalled();
      expect(onResume).toHaveBeenCalledTimes(1);
    },
  );

  it.each(["LLMAuthError", "RemoteProtocolError"])("%s checkpoint 不完整时禁止开始和继续", (errorType) => {
    renderPanel(
      _status({
        state: "error",
        current_chapter: "ch-0007",
        error_details: {
          error_type: errorType,
          message: "legacy 403",
          chapter_id: "ch-0007",
          recovery_kind: "provider_review_checkpoint",
          recovery_ready: false,
          recovery_block_reason: "checkpoint_not_clean",
        },
      }),
    );

    expect(screen.getByTestId("pipeline-start-btn")).toBeDisabled();
    expect(screen.getByTestId("pipeline-resume-btn")).toBeDisabled();
    expect(
      screen.getByTestId("pipeline-provider-review-recovery"),
    ).toHaveTextContent("已安全阻止恢复");
  });
});

describe("PipelineControlPanel 首章核心状态基线恢复", () => {
  it("卡片尚未重批时禁止开始和继续，并给出产品路径", () => {
    renderPanel(
      _status({
        state: "error",
        current_chapter: "ch-0001",
        error_details: {
          error_type: "CoreStateConflictError",
          message: "life_status 首次出现缺少基线",
          chapter_id: "ch-0001",
          recovery_kind: "core_state_initial_baseline",
          recovery_ready: false,
          recovery_block_reason: "card_not_reapproved",
        },
      }),
    );

    expect(screen.getByTestId("pipeline-start-btn")).toBeDisabled();
    expect(screen.getByTestId("pipeline-resume-btn")).toBeDisabled();
    expect(screen.getByTestId("pipeline-core-state-recovery")).toHaveTextContent(
      "拒绝当前卡，再重新批准",
    );
  });

  it("双证据恢复闭合后只开放同 run 继续", () => {
    const { onStart, onResume } = renderPanel(
      _status({
        state: "error",
        current_chapter: "ch-0001",
        error_details: {
          error_type: "CoreStateConflictError",
          chapter_id: "ch-0001",
          recovery_kind: "core_state_initial_baseline",
          recovery_ready: true,
        },
      }),
    );

    expect(screen.getByTestId("pipeline-start-btn")).toBeDisabled();
    expect(screen.getByTestId("pipeline-resume-btn")).not.toBeDisabled();
    expect(screen.getByTestId("pipeline-core-state-recovery")).toHaveTextContent(
      "不重写正文、不重审",
    );
    fireEvent.click(screen.getByTestId("pipeline-start-btn"));
    expect(onStart).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("pipeline-resume-btn"));
    expect(onResume).toHaveBeenCalledTimes(1);
  });
});

describe("PipelineControlPanel 非首章核心状态 checkpoint 恢复", () => {
  it("同源 transition 现场只开放原 run 继续", () => {
    const { onStart, onResume } = renderPanel(
      _status({
        state: "error",
        current_chapter: "ch-0005",
        error_details: {
          error_type: "CoreStateConflictError",
          chapter_id: "ch-0005",
          recovery_kind: "core_state_transition_checkpoint",
          recovery_ready: true,
        },
      }),
    );

    expect(screen.getByTestId("pipeline-start-btn")).toBeDisabled();
    expect(screen.getByTestId("pipeline-resume-btn")).not.toBeDisabled();
    expect(screen.getByTestId("pipeline-core-state-recovery")).toHaveTextContent(
      "不重写正文、不重审、不重跑摘要",
    );
    fireEvent.click(screen.getByTestId("pipeline-start-btn"));
    expect(onStart).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("pipeline-resume-btn"));
    expect(onResume).toHaveBeenCalledTimes(1);
  });

  it("任一 checkpoint 证据不干净时继续保持禁用", () => {
    renderPanel(
      _status({
        state: "error",
        current_chapter: "ch-0005",
        error_details: {
          error_type: "CoreStateConflictError",
          chapter_id: "ch-0005",
          recovery_kind: "core_state_transition_checkpoint",
          recovery_ready: false,
          recovery_block_reason: "checkpoint_not_clean",
        },
      }),
    );

    expect(screen.getByTestId("pipeline-start-btn")).toBeDisabled();
    expect(screen.getByTestId("pipeline-resume-btn")).toBeDisabled();
    expect(screen.getByTestId("pipeline-core-state-recovery")).toHaveTextContent(
      "已安全阻止恢复",
    );
  });
});

describe("PipelineControlPanel StatusCard 状态展示(w1-s3)", () => {
  it("展示 R5 活动上限与四类持久化计数", () => {
    renderPanel(
      _status({
        provider_call_cap: 96,
        provider_call_count: 17,
        transport_retry_count: 2,
        pipeline_attempt_count: 3,
        upstream_generation_attempt_count: 4,
      }),
    );
    const counters = screen.getByTestId("pipeline-attempt-counters");
    expect(counters).toHaveTextContent("provider: 17/96");
    expect(counters).toHaveTextContent("transport retry: 2");
    expect(counters).toHaveTextContent("pipeline attempt: 3");
    expect(counters).toHaveTextContent("upstream generation: 4");
  });

  it("并列展示 usage、配置估算与未导入实际账单，不混币种", () => {
    renderPanel(
      _status({
        attempt_usage_cost: {
          usage_known_attempts: 1,
          usage_unknown_attempts: 1,
          legacy_unclassified_attempts: 0,
          input_tokens_known: 100,
          output_tokens_known: 20,
          provider_request_id_count: 1,
          response_status_counts: { completed: 1, usage_missing: 1 },
          configured_cost_basis_counts: {
            conservative_reserve: 1,
            metered_usage: 1,
          },
          configured_estimates: [{ currency: "RMB", amount: 0.01152 }],
          actual_billed: {
            status: "not_recorded",
            amount: null,
            currency: null,
          },
        },
      }),
    );

    const accounting = screen.getByTestId("pipeline-attempt-accounting");
    expect(accounting).toHaveTextContent("Provider usage: 100 in / 20 out");
    expect(accounting).toHaveTextContent("known 1 · unknown 1 · legacy 0");
    expect(accounting).toHaveTextContent("配置估算: RMB 0.0115");
    expect(accounting).toHaveTextContent("request ID 1");
    expect(accounting).toHaveTextContent("实际账单: 未导入");
    expect(accounting).toHaveTextContent("不能由 usage 推算");
    expect(accounting).not.toHaveTextContent("US$0.0115");
  });

  it("provider cap 暂停原因使用白话标签", () => {
    renderPanel(
      _status({
        state: "paused",
        pause_reason: "provider_call_cap_exceeded",
      }),
    );
    const reason = screen.getByTestId("pipeline-pause-reason");
    expect(reason).toHaveTextContent("provider 调用预算已耗尽");
    expect(reason).toHaveTextContent("provider_call_cap_exceeded");
  });

  it("pause_reason=cost_guard_exceeded → 白话标签 + 原始 code", () => {
    renderPanel(
      _status({ state: "paused", pause_reason: "cost_guard_exceeded" }),
    );
    const reason = screen.getByTestId("pipeline-pause-reason");
    expect(reason).toHaveTextContent("成本护栏触发");
    expect(reason).toHaveTextContent("cost_guard_exceeded");
  });

  it("interrupted + chapter_watchdog_timeout → 结构化明细(章节/耗时/预算)", () => {
    renderPanel(
      _status({
        state: "interrupted",
        error_details: {
          reason: "chapter_watchdog_timeout",
          chapter_id: "ch-0002",
          elapsed_seconds: 2500,
          timeout_seconds: 2400,
        },
      }),
    );
    const block = screen.getByTestId("pipeline-interrupt-details");
    expect(block).toHaveTextContent("章级看门狗超时");
    const watchdog = screen.getByTestId("pipeline-watchdog-details");
    expect(watchdog).toHaveTextContent("ch-0002");
    expect(watchdog).toHaveTextContent("2500");
    expect(watchdog).toHaveTextContent("2400");
  });

  it("skip 策略下 skipped_chapter_ids 非空 → 显示跳过计数", () => {
    renderPanel(
      _status({
        failure_policy: "skip",
        skipped_chapter_ids: ["ch-0002"],
      }),
    );
    expect(screen.getByTestId("pipeline-skipped-count")).toHaveTextContent(
      "跳过 1 章",
    );
    expect(screen.getByTestId("pipeline-failure-policy")).toHaveTextContent(
      "skip",
    );
  });

  it("error 态 error_details → 结构化错误类型 + message", () => {
    renderPanel(
      _status({
        state: "error",
        error_details: {
          error_type: "RuntimeError",
          message: "写章异常",
          chapter_id: "ch-0003",
        },
      }),
    );
    const block = screen.getByTestId("pipeline-error-details");
    expect(block).toHaveTextContent("RuntimeError");
    expect(block).toHaveTextContent("写章异常");
    expect(block).toHaveTextContent("ch-0003");
    expect(screen.getByTestId("pipeline-start-btn")).not.toBeDisabled();
    expect(screen.getByTestId("pipeline-resume-btn")).toBeDisabled();
  });
});

describe("摘要恢复的 Provider 说明", () => {
  it.each(["core_state_transition_checkpoint", "core_state_initial_baseline"])("明确告知摘要重新调用及原预算，同时保留原 run 继续入口 %s", (recoveryKind) => {
    const { onResume } = renderPanel(_status({
      state: "error", current_chapter: "ch-0002",
      error_details: {
        error_type: "CoreStateConflictError", chapter_id: "ch-0002",
        recovery_kind: recoveryKind, recovery_ready: true,
        summary_refresh_required: true,
      },
    }));
    expect(screen.getByTestId("pipeline-core-state-recovery")).toHaveTextContent(
      "摘要会调用模型并计入原预算",
    );
    expect(screen.getByTestId("pipeline-core-state-recovery")).not.toHaveTextContent("不重跑摘要");
    fireEvent.click(screen.getByTestId("pipeline-resume-btn"));
    expect(onResume).toHaveBeenCalledTimes(1);
  });
});

it("已归档审核恢复只开放同一活动继续，不要求更换账户", () => {
  const { onResume } = renderPanel(_status({
    state: "error", current_chapter: "ch-0003",
    error_details: {
      error_type: "ValueError", chapter_id: "ch-0003",
      recovery_kind: "known_review_archive_checkpoint", recovery_ready: true,
    },
  }));
  expect(screen.getByTestId("pipeline-start-btn")).toBeDisabled();
  expect(screen.getByTestId("pipeline-resume-btn")).not.toBeDisabled();
  expect(screen.getByTestId("pipeline-provider-review-recovery")).toHaveTextContent("复用已保存的审核响应");
  expect(screen.getByTestId("pipeline-provider-review-recovery")).not.toHaveTextContent("密钥");
  fireEvent.click(screen.getByTestId("pipeline-resume-btn"));
  expect(onResume).toHaveBeenCalledTimes(1);
});
