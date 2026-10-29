/**
 * 未保存修改导航拦截(P1-F-05)。
 *
 * 拦截两类离开:
 * 1. **In-app 导航**(NavLink / 编程式 navigate):用 React Router v6 `useBlocker`
 *    拦截,弹 ConfirmDialog 询问"放弃修改吗"。仅在 data router(`createBrowserRouter`
 *    + `RouterProvider`)下可用,这是 P1-F-05 把 App.tsx 迁到 data router 的根因。
 * 2. **浏览器关闭/刷新**:`beforeunload` 事件兜底,弹浏览器原生确认框
 *    (文案由浏览器接管,无法定制)。
 *
 * 挂在 AppShell 顶层即可,无需 Provider 包裹。订阅 `useUnsavedChanges` 模块级
 * store 拿当前脏状态。
 */

import * as React from "react";
import { useBlocker } from "react-router-dom";

import { ConfirmDialog } from "@/components/ConfirmDialog";
import {
  resetUnsavedChanges,
  subscribeUnsavedChanges,
} from "@/hooks/useUnsavedChanges";

export function UnsavedChangesGuard() {
  const [isDirty, setIsDirty] = React.useState(false);

  React.useEffect(() => {
    const unsubscribe = subscribeUnsavedChanges(setIsDirty);
    return unsubscribe;
  }, []);

  // beforeunload:浏览器关闭/刷新拦截。preventDefault + returnValue 是跨浏览器
  // 兼容写法(Chromium 看 returnValue,Firefox 看 preventDefault)。
  React.useEffect(() => {
    if (!isDirty) return;
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => {
      window.removeEventListener("beforeunload", handler);
    };
  }, [isDirty]);

  // useBlocker:in-app 导航拦截。仅在 dirty 时启用 blocker(避免 clean 状态
  // 拦截无关导航)。返回 blocker 状态:'unblocked' / 'blocked' / 'proceeding'。
  const blocker = useBlocker(isDirty);

  const handleConfirm = () => {
    if (blocker.state === "blocked") {
      resetUnsavedChanges();
      blocker.proceed();
    }
  };

  const handleCancel = () => {
    if (blocker.state === "blocked") {
      blocker.reset();
    }
  };

  return (
    <ConfirmDialog
      open={blocker.state === "blocked"}
      onOpenChange={(next) => {
        if (!next && blocker.state === "blocked") {
          blocker.reset();
        }
      }}
      title="放弃未保存的修改?"
      description="当前页面有未保存的修改,离开将丢失所有改动。"
      confirmLabel="放弃修改并离开"
      cancelLabel="留在本页"
      variant="destructive"
      onConfirm={handleConfirm}
      onCancel={handleCancel}
    />
  );
}
