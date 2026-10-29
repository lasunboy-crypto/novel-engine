/**
 * 未保存修改注册 hook(P1-F-05)。
 *
 * 任意 page / form 组件调用 `useUnsavedChanges(isDirty)`,把脏状态写入模块级
 * store。`UnsavedChangesGuard` 订阅该 store,拦截 in-app 导航与
 * `beforeunload`(浏览器关闭/刷新)。
 *
 * 设计取舍:
 * - **模块级 store**(而非 React Context):Guard 不需要被 Provider 包裹,
 *   挂在 AppShell 顶层即可;hook 与 Guard 解耦,降低组件树侵入度
 * - **bool 单值**(而非"哪些字段脏"):P1 只需"放弃修改吗"二档语义,字段
 *   级 diff 留给 P1-F-12 起 Editor 真正接进来后视需求扩展
 * - **组件 unmount 时自动清零**:防止 page 切走后 isDirty 残留误拦截
 */

import * as React from "react";

let dirty = false;
const listeners = new Set<(value: boolean) => void>();

function setDirty(next: boolean) {
  if (dirty === next) return;
  dirty = next;
  listeners.forEach((listener) => listener(dirty));
}

/**
 * 订阅当前脏状态。仅供 `UnsavedChangesGuard` 使用,page 组件不需要。
 */
export function subscribeUnsavedChanges(
  listener: (value: boolean) => void,
): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * 同步读取当前脏状态(供 Guard `useBlocker` 闭包内调用)。
 */
export function getUnsavedChangesSnapshot(): boolean {
  return dirty;
}

/**
 * 在组件内注册"我现在脏了"。
 *
 * - `isDirty` 变化时同步写入模块级 store
 * - 组件 unmount 时自动清为 false(防止跨页面残留)
 *
 * 调用点示例(P1-F-12 ChapterEditor):
 *   const form = useForm(...)
 *   useUnsavedChanges(form.formState.isDirty)
 */
export function useUnsavedChanges(isDirty: boolean): void {
  React.useEffect(() => {
    setDirty(isDirty);
  }, [isDirty]);

  React.useEffect(() => {
    return () => {
      setDirty(false);
    };
  }, []);
}

/**
 * 强制重置脏状态(供 Guard 在用户确认放弃后调用)。
 */
export function resetUnsavedChanges(): void {
  setDirty(false);
}

/**
 * 直接设置脏状态。供非 React 上下文(测试 helper / 程序化触发)使用;
 * page 组件内仍应使用 `useUnsavedChanges(isDirty)` hook 注册。
 */
export function setUnsavedChanges(value: boolean): void {
  setDirty(value);
}
