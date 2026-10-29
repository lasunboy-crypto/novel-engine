/**
 * 全局 React 错误兜底(P4-F-06)。
 *
 * 兑现 OBSERVABILITY §8 + FRONTEND_SPEC §5.3 + p4_plan §P4-F-06 字面"全局
 * ErrorBoundary 包裹 AppShell 子树,捕获 React 渲染错误 + 显示 fallback UI
 * (可重试)"要求。
 *
 * ## 边界划分
 *
 * 只捕 React 组件树 render / lifecycle / hook 抛出的同步异常(`getDerivedStateFromError`
 * + `componentDidCatch` 配对)。**不捕**:
 *
 * - `setTimeout` / `setInterval` 内异步抛出 → 由 `window.onerror` 兜底(见
 *   `lib/error-reporter.ts`)
 * - `Promise` 链未 catch → 由 `unhandledrejection` 兜底
 * - 事件处理器内同步异常 → React 19 不再走 ErrorBoundary,由 `window.onerror`
 *   兜底(同 React 18+ 行为)
 *
 * 三档兜底由 `installGlobalErrorReporter()` 统一注册,本类只负责 React render
 * 异常的"页面级 fallback UI",上报通过 `reportReactRenderError`。
 *
 * ## fallback 设计取舍
 *
 * - **不复用 shadcn `Button` 组件**:ErrorBoundary 可能在 QueryClient / Router
 *   尚未挂载完成时触发(Provider 自身异常场景),fallback 不依赖任何 Provider
 *   上下文,纯 HTML + tailwind。Button class 散在内联 className 里。
 * - **保留 `error.stack` 折叠展开**:`<details>` 默认收起,供维护人 / 开发人员
 *   手动展开诊断;非开发者用户看到的就是"消息 + 重新加载"两件套。
 * - **"重新加载"按钮**:`window.location.reload()` 触发整页 reload —— 清掉所有
 *   React 状态 + reset router。"返回主页"按钮:`window.location.href = "/"`,
 *   走整页 navigation 避免 router 状态卡死。
 *
 * ## 上报路径
 *
 * `componentDidCatch` 调 `reportReactRenderError(error, info)`,内部对接
 * `POST /api/v1/system/frontend-errors`(error_type="ReactRender")+
 * 5s 节流。上报失败 swallow,不二次抛错。
 */

import { Component, type ErrorInfo, type ReactNode } from "react";
import { reportReactRenderError } from "@/lib/error-reporter";

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

export class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    reportReactRenderError(error, {
      componentStack: info.componentStack ?? null,
    });
  }

  private handleReload = (): void => {
    window.location.reload();
  };

  private handleGoHome = (): void => {
    window.location.href = "/";
  };

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div
          role="alert"
          className="flex min-h-screen flex-col items-center justify-center bg-background px-6 py-12 text-foreground"
          data-testid="error-boundary-fallback"
        >
          <div className="w-full max-w-2xl rounded-lg border border-destructive/40 bg-destructive/5 p-8 shadow-sm">
            <h1 className="text-2xl font-semibold text-destructive">
              页面出错了
            </h1>
            <p className="mt-2 text-sm text-muted-foreground">
              小说创作引擎前端在渲染本页面时遇到未捕获异常;后端已收到自动上报。
              可以点击下面的按钮恢复;若反复出现,请到 LogsPage 按 `frontend_error`
              事件回查。
            </p>

            <div className="mt-6 rounded border border-border bg-background p-4 text-sm">
              <p className="font-mono text-destructive">
                {this.state.error.name}: {this.state.error.message}
              </p>
              {this.state.error.stack ? (
                <details className="mt-3 text-xs text-muted-foreground">
                  <summary className="cursor-pointer select-none">
                    展开堆栈
                  </summary>
                  <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-all font-mono">
                    {this.state.error.stack}
                  </pre>
                </details>
              ) : null}
            </div>

            <div className="mt-6 flex gap-3">
              <button
                type="button"
                onClick={this.handleReload}
                className="inline-flex h-10 items-center justify-center rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                重新加载
              </button>
              <button
                type="button"
                onClick={this.handleGoHome}
                className="inline-flex h-10 items-center justify-center rounded-md border border-input bg-background px-4 text-sm font-medium transition-colors hover:bg-accent hover:text-accent-foreground"
              >
                返回主页
              </button>
            </div>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
