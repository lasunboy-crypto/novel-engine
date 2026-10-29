/**
 * 项目特定 toast helper(P1-F-05)。
 *
 * `Toaster` 容器与底层 `useToast` hook 已经在
 * `@/components/Toaster` 与 `@/hooks/use-toast` 提供;本文件只追加项目语义的
 * 便捷封装,后续 page / mutation 调用方只需 `import { showApiErrorToast,
 * showSuccessToast } from '@/components/Toast'`,无需直接接触底层 reducer。
 *
 * 红色 destructive variant → ApiError;
 * 默认 variant → 成功/信息提示。
 */

import { isApiError, type ApiError } from "@/api/errors";
import { toast } from "@/hooks/use-toast";

/**
 * 把 ApiError 翻译成红色 destructive toast。
 *
 * 显示规则:
 * - 标题:错误码(`<DOMAIN>_<REASON>` 字面)
 * - 正文:`message`,若 `requestId` 非 null 则附 ` · req=<id>` 便于排查
 *
 * 调用点示例(P1-F-08+ mutation onError):
 *
 *   try {
 *     await apiPost(...)
 *   } catch (e) {
 *     showApiErrorToast(e)
 *   }
 */
export function showApiErrorToast(error: unknown): void {
  if (isApiError(error)) {
    const description = error.requestId
      ? `${error.message} · req=${error.requestId}`
      : error.message;
    toast({
      variant: "destructive",
      title: error.code,
      description,
    });
    return;
  }
  // 非 ApiError(理论不应出现,client 已统一收敛):兜底展示
  const reason = error instanceof Error ? error.message : String(error);
  toast({
    variant: "destructive",
    title: "UNKNOWN_ERROR",
    description: reason,
  });
}

/**
 * 成功提示 toast(默认 variant)。
 *
 * 用于 mutation onSuccess 反馈("工作区已创建" / "章节已切换"等)。
 */
export function showSuccessToast(title: string, description?: string): void {
  toast({
    title,
    description,
  });
}

// 直通底层 toast,留给特殊场景(action button / 自定义 ReactNode 内容)
export { toast } from "@/hooks/use-toast";
export type { ApiError };
