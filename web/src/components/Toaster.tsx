/**
 * Toaster 渲染容器(P1-F-05)。
 *
 * 挂在 AppShell 顶层,订阅 `useToast` 队列并渲染 shadcn `Toast` 原语。
 * 队列由模块级 store 维护,任何位置调用 `toast(...)` 都会被 mounted Toaster
 * 拾起渲染。
 *
 * P1-F-08 沉淀:每个 `<Toast>` 显式 `duration={Infinity}` 关掉 Radix Toast
 * 内部的 auto-dismiss timer。原因:在我们当前 React 19 + Radix 1.2.15 +
 * Vite 7 组合下,Radix Toast 默认 5000ms timer 在某条件下会被立即触发
 * (P1-F-08 验收阶段栈追踪:`onClose → onOpenChange(false)` 在 toast mount
 * 同帧内即触发,导致 ADD_TOAST + DISMISS_TOAST 在 React 批处理下只渲染了
 * 最终 `open=false` 那一帧,toast 永远看不见)。`Infinity` 让 Radix 不设
 * timer,toast 持续显示直到 (a) 用户点 X 关闭,或 (b) TOAST_LIMIT=1 让下一条
 * toast 替换它。
 *
 * **v1.1 wave 7 收口**:`duration={Infinity}` 保留(Radix 内部 timer 仍坏),但
 * "永不自动消失"的副作用由 `use-toast.ts` 的 store 级自管理计时器
 * (`TOAST_AUTO_DISMISS_MS`)补上 —— ADD_TOAST 后起 setTimeout 主动 dismiss
 * (controlled `open` 翻 false → Radix 正常退场动画),绕开坏 timer。本组件无需改。
 */

import {
  Toast,
  ToastClose,
  ToastDescription,
  ToastProvider,
  ToastTitle,
  ToastViewport,
} from "@/components/ui/toast";
import { useToast } from "@/hooks/use-toast";

export function Toaster() {
  const { toasts } = useToast();

  return (
    <ToastProvider>
      {toasts.map(({ id, title, description, action, ...props }) => (
        <Toast key={id} duration={Infinity} {...props}>
          <div className="grid gap-1">
            {title ? <ToastTitle>{title}</ToastTitle> : null}
            {description ? (
              <ToastDescription>{description}</ToastDescription>
            ) : null}
          </div>
          {action}
          <ToastClose />
        </Toast>
      ))}
      <ToastViewport />
    </ToastProvider>
  );
}
