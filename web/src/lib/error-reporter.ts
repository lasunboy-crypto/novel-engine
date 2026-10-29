/**
 * 全局前端错误上报器(P4-F-06)。
 *
 * 兑现 OBSERVABILITY §8 "前端通过全局 ErrorBoundary + window.onerror + Promise
 * rejection 捕获错误,POST 到 `/api/v1/system/frontend-errors`" 字面要求。
 *
 * 三入口:
 *
 * - `installGlobalErrorReporter()` —— 在 main.tsx 启动时调用一次,注册
 *   `window.onerror` + `window.addEventListener("unhandledrejection")` 全局监听。
 * - `reportReactRenderError(error, info)` —— `<ErrorBoundary>` 的 `componentDidCatch`
 *   钩子内调用,记录 React 渲染树异常(`error_type="ReactRender"`)。
 * - `reportFrontendError(report)` —— 直传一条已构造好的报告;主要给 ErrorBoundary
 *   / future 5xx 拦截器复用,默认 swallow 失败避免循环。
 *
 * ## 节流
 *
 * 同一 `error_type::message` 在 `THROTTLE_MS=5000` 毫秒内只上报一次。防止:
 * (a) 单组件 render 死循环爆错;(b) `unhandledrejection` 在某些库内并发触发;
 * (c) 真 LLM 长跑期间网络抖动 retry 雪崩。**节流 = soft drop**,丢弃的不会重试。
 *
 * ## swallow 失败
 *
 * `reportFrontendError` 内部 `try/catch` 吃掉网络 / API 错误。原因:上报失败时
 * 若再触发 toast / `console.error`,会被 reporter 自己捕获导致死循环。
 * structlog 端的 `frontend_error_persist_failed` 是后端兜底,不在前端补。
 *
 * ## 字段映射
 *
 * 按 `web/src/api/types.ts` `FrontendErrorReport` schema 字面映射(对齐
 * OBSERVABILITY §8 + P4-B-10 落表字段):
 *
 * - `error_type` —— Error 子类名(`TypeError` / `RangeError` / ...)/
 *   `UnhandledRejection` / `ReactRender` / `Error`(默认)
 * - `message` —— `error.message`(unhandledrejection 时取 reason.message 或 String(reason))
 * - `stack` —— `error.stack`(后端再截前 1000 字)
 * - `url` —— `window.location.href`
 * - `user_action` —— 暂留 null(P4-F-06 无用户动作埋点;未来 P5+ 可接面包屑)
 * - `browser` —— `navigator.userAgent` 前 200 字截断(过长 UA 字段对调试无意义)
 * - `workspace_id` —— zustand `useActiveWorkspaceStore` 当前活动工作区 ID,null 表全局
 *
 * **类 A 冲突登记**:启动指令字面"message / stack / url / user_agent /
 * workspace_id / extra" vs types.ts 字面"error_type / message / stack / url /
 * user_action / browser / workspace_id" —— 按 spec(types.ts ≡ P4-B-10 OBSERVABILITY
 * §8 落表)走;`user_agent` → `browser`,`extra` 字段在 schema 不存在(后端
 * `extra="forbid"`)直接弃用。
 */

import type { components } from "@/api/types";
import { apiPost } from "@/api/client";
import { useActiveWorkspaceStore } from "@/store/activeWorkspace";

export type FrontendErrorReport =
  components["schemas"]["FrontendErrorReport"];

const THROTTLE_MS = 5000;
const BROWSER_UA_MAX = 200;

const lastReportedAt = new Map<string, number>();

let installed = false;

function throttleKey(report: FrontendErrorReport): string {
  return `${report.error_type}::${report.message}`;
}

function shouldDrop(report: FrontendErrorReport, now: number): boolean {
  const key = throttleKey(report);
  const prev = lastReportedAt.get(key);
  if (prev !== undefined && now - prev < THROTTLE_MS) {
    return true;
  }
  lastReportedAt.set(key, now);
  return false;
}

function currentBrowser(): string | null {
  if (typeof navigator === "undefined" || !navigator.userAgent) return null;
  const ua = navigator.userAgent;
  return ua.length > BROWSER_UA_MAX ? ua.slice(0, BROWSER_UA_MAX) : ua;
}

function currentUrl(): string | null {
  if (typeof window === "undefined" || !window.location) return null;
  return window.location.href;
}

function currentWorkspaceId(): string | null {
  try {
    return useActiveWorkspaceStore.getState().activeWorkspaceId;
  } catch {
    return null;
  }
}

function fillContext(
  partial: Pick<FrontendErrorReport, "error_type" | "message" | "stack">,
): FrontendErrorReport {
  return {
    error_type: partial.error_type,
    message: partial.message,
    stack: partial.stack ?? null,
    url: currentUrl(),
    user_action: null,
    browser: currentBrowser(),
    workspace_id: currentWorkspaceId(),
  };
}

/**
 * 直接上报一条已构造好的 `FrontendErrorReport`。失败 swallow。
 *
 * 调用方通常用 `reportReactRenderError` / `installGlobalErrorReporter` 间接
 * 触发;直接 import 仅在 future 自定义场景(如 axios 5xx 拦截器升级)使用。
 */
export async function reportFrontendError(
  report: FrontendErrorReport,
): Promise<void> {
  if (shouldDrop(report, Date.now())) return;
  try {
    await apiPost<Record<string, unknown>, FrontendErrorReport>(
      "/system/frontend-errors",
      report,
    );
  } catch {
    // 静默吞掉:reporter 自身失败不能再回调 reporter 否则死循环。
  }
}

/**
 * `<ErrorBoundary>` 在 `componentDidCatch` 内调用。`error_type` 固定 `ReactRender`,
 * 便于后端 LogsPage 按 React 渲染树异常筛选。
 */
export function reportReactRenderError(
  error: unknown,
  info?: { componentStack?: string | null },
): void {
  const err = toErrorLike(error);
  const stackParts: string[] = [];
  if (err.stack) stackParts.push(err.stack);
  if (info?.componentStack) {
    stackParts.push(`\n--- React component stack ---${info.componentStack}`);
  }
  const report = fillContext({
    error_type: "ReactRender",
    message: err.message,
    stack: stackParts.length > 0 ? stackParts.join("") : null,
  });
  void reportFrontendError(report);
}

interface ErrorLike {
  name: string;
  message: string;
  stack?: string;
}

function toErrorLike(value: unknown): ErrorLike {
  if (value instanceof Error) {
    return { name: value.name, message: value.message, stack: value.stack };
  }
  if (typeof value === "object" && value !== null) {
    const obj = value as Record<string, unknown>;
    const name = typeof obj.name === "string" ? obj.name : "Error";
    const message =
      typeof obj.message === "string" ? obj.message : safeString(value);
    const stack = typeof obj.stack === "string" ? obj.stack : undefined;
    return { name, message, stack };
  }
  return { name: "Error", message: safeString(value) };
}

function safeString(value: unknown): string {
  try {
    return typeof value === "string" ? value : JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}

function handleWindowError(event: ErrorEvent): void {
  const err = toErrorLike(event.error ?? new Error(event.message));
  const report = fillContext({
    error_type: err.name || "Error",
    message: err.message || event.message || "window.onerror",
    stack: err.stack ?? null,
  });
  void reportFrontendError(report);
}

function handleUnhandledRejection(event: PromiseRejectionEvent): void {
  const err = toErrorLike(event.reason);
  const report = fillContext({
    error_type: "UnhandledRejection",
    message: err.message || "unhandledrejection",
    stack: err.stack ?? null,
  });
  void reportFrontendError(report);
}

/**
 * 启动时调用一次。再次调用是 no-op(`installed` 守卫),便于 HMR / 测试。
 */
export function installGlobalErrorReporter(): void {
  if (installed) return;
  if (typeof window === "undefined") return;
  window.addEventListener("error", handleWindowError);
  window.addEventListener("unhandledrejection", handleUnhandledRejection);
  installed = true;
}

/**
 * 仅给测试用:重置节流状态。生产代码不要调用。
 */
export function __resetErrorReporterForTests(): void {
  lastReportedAt.clear();
  installed = false;
}
