/**
 * shadcn 标准 useToast hook(P1-F-05)。
 *
 * 模块级 reducer + listeners 数组,支持两种调用入口:
 * - `useToast()` hook:在 React 组件内调用,拿到 `toast` 函数 + 当前队列
 * - `toast(...)` 函数(默认导出):任意位置调用(含非 React 上下文),
 *   推送一条 toast 到模块级 store,所有 mounted Toaster 自动重渲染
 *
 * 队列上限 `TOAST_LIMIT=1`(shadcn 默认值),保证同一时刻只显示 1 条;
 * 关闭后 `TOAST_REMOVE_DELAY` ms 内退场动画期保留在队列。
 *
 * 项目特定 helper(`showApiErrorToast` 等)在 `@/components/Toast` 中实现,
 * 此处只放底层标准 hook。
 */

import * as React from "react";

import type { ToastActionElement, ToastProps } from "@/components/ui/toast";

const TOAST_LIMIT = 1;
const TOAST_REMOVE_DELAY = 1_000_000;

// v1.1 wave 7:自管理 auto-dismiss 时长(ms)。Radix Toast 内部 5000ms timer 在
// React19 + Radix1.2.15 + Vite7 下同帧触发坏掉(详 `Toaster.tsx` 注释),Toaster
// 用 `duration={Infinity}` 关掉它 → 但这样 toast 永不自动消失(只能点 X / 被下一条
// 替换),会一直停在屏上。改由本 store 起一个 setTimeout,`TOAST_AUTO_DISMISS_MS`
// 后主动 dismiss(controlled `open` 翻 false → Radix 正常退场),绕开坏 timer。
// 0 = 不自动消失(保留旧行为的逃生阀,当前不用)。
const TOAST_AUTO_DISMISS_MS = 4000;

type ToasterToast = ToastProps & {
  id: string;
  title?: React.ReactNode;
  description?: React.ReactNode;
  action?: ToastActionElement;
};

// B-08 hot-fix(2026-05-19):shadcn 模板用 `const actionTypes = {...} as const`
// + `typeof actionTypes` 派生 type,但 ESLint @typescript-eslint/no-unused-vars
// 视 const 仅在 type 位置被使用 = unused。改为直接 type literal 等价,无运行时常量。

let count = 0;

function genId() {
  count = (count + 1) % Number.MAX_SAFE_INTEGER;
  return count.toString();
}

type Action =
  | {
      type: "ADD_TOAST";
      toast: ToasterToast;
    }
  | {
      type: "UPDATE_TOAST";
      toast: Partial<ToasterToast> & { id: string };
    }
  | {
      type: "DISMISS_TOAST";
      toastId?: ToasterToast["id"];
    }
  | {
      type: "REMOVE_TOAST";
      toastId?: ToasterToast["id"];
    };

interface State {
  toasts: ToasterToast[];
}

const toastTimeouts = new Map<string, ReturnType<typeof setTimeout>>();

const addToRemoveQueue = (toastId: string) => {
  if (toastTimeouts.has(toastId)) {
    return;
  }

  const timeout = setTimeout(() => {
    toastTimeouts.delete(toastId);
    dispatch({
      type: "REMOVE_TOAST",
      toastId,
    });
  }, TOAST_REMOVE_DELAY);

  toastTimeouts.set(toastId, timeout);
};

export const reducer = (state: State, action: Action): State => {
  switch (action.type) {
    case "ADD_TOAST":
      return {
        ...state,
        toasts: [action.toast, ...state.toasts].slice(0, TOAST_LIMIT),
      };

    case "UPDATE_TOAST":
      return {
        ...state,
        toasts: state.toasts.map((t) =>
          t.id === action.toast.id ? { ...t, ...action.toast } : t,
        ),
      };

    case "DISMISS_TOAST": {
      const { toastId } = action;

      if (toastId) {
        addToRemoveQueue(toastId);
      } else {
        state.toasts.forEach((t) => {
          addToRemoveQueue(t.id);
        });
      }

      return {
        ...state,
        toasts: state.toasts.map((t) =>
          t.id === toastId || toastId === undefined
            ? {
                ...t,
                open: false,
              }
            : t,
        ),
      };
    }
    case "REMOVE_TOAST":
      if (action.toastId === undefined) {
        return {
          ...state,
          toasts: [],
        };
      }
      return {
        ...state,
        toasts: state.toasts.filter((t) => t.id !== action.toastId),
      };
  }
};

const listeners: Array<(state: State) => void> = [];

let memoryState: State = { toasts: [] };

function dispatch(action: Action) {
  memoryState = reducer(memoryState, action);
  listeners.forEach((listener) => {
    listener(memoryState);
  });
}

type Toast = Omit<ToasterToast, "id">;

function toast({ ...props }: Toast) {
  const id = genId();

  const update = (next: Partial<ToasterToast>) =>
    dispatch({
      type: "UPDATE_TOAST",
      toast: { ...next, id },
    });
  const dismiss = () => dispatch({ type: "DISMISS_TOAST", toastId: id });

  dispatch({
    type: "ADD_TOAST",
    toast: {
      ...props,
      id,
      open: true,
      onOpenChange: (open) => {
        if (!open) dismiss();
      },
    },
  });

  // v1.1 wave 7:自管理 auto-dismiss(见 TOAST_AUTO_DISMISS_MS 注释)。
  // 用户先点 X / 被下一条替换 → 此计时器 fire 时 dismiss 已是 no-op(DISMISS_TOAST
  // 对已关闭 / 已移除的 id 无副作用),无需额外清理。
  if (TOAST_AUTO_DISMISS_MS > 0) {
    setTimeout(dismiss, TOAST_AUTO_DISMISS_MS);
  }

  return {
    id,
    dismiss,
    update,
  };
}

function useToast() {
  const [state, setState] = React.useState<State>(memoryState);

  React.useEffect(() => {
    listeners.push(setState);
    return () => {
      const index = listeners.indexOf(setState);
      if (index > -1) {
        listeners.splice(index, 1);
      }
    };
  }, []);

  return {
    ...state,
    toast,
    dismiss: (toastId?: string) => dispatch({ type: "DISMISS_TOAST", toastId }),
  };
}

export { useToast, toast };
export type { Toast, ToasterToast };
