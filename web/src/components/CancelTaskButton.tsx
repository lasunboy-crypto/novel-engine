/**
 * CancelTaskButton(v1.1 w4-s7 = J2.1):异步任务"取消"按钮。
 *
 * 与 `useAsyncTaskState` 配套:任务运行中(`isRunning`)时,caller 在原操作
 * 按钮旁渲染本组件,点击调 `useAsyncTaskState` 返回的 `cancel`(尽力中断,
 * 非瞬时 — 详 `useAsyncTaskState` docstring + J1 `useCancelTaskMutation`)。
 *
 * ## 设计(维护人拍板:方案 1 — 独立红色幽灵小按钮,无二次确认)
 *
 * - **独立按钮**(不复用原操作按钮文案):语义清晰,"进行中状态" vs "取消动作"
 *   不挤在一个按钮上。
 * - **红色幽灵风格**:取消是 destructive 语义但低频 + 可逆代价低,用 ghost +
 *   `text-destructive` 弱化视觉权重,不喧宾夺主。
 * - **无二次确认**:取消本身无破坏性(只是停掉一次生成,可重新触发),confirm
 *   对话框偏重 + 多一次点击。
 * - **`isCancelling` 时禁用 + 文案"取消中…"**:取消请求已发、任务尚未到 cancelled
 *   终态的过渡期反馈(防重复点击 + 告知"已在取消")。
 */

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface CancelTaskButtonProps {
  /** 取消回调(通常 = `useAsyncTaskState().cancel`)。 */
  onCancel: () => void;
  /** 取消请求已发、尚未收到终态;禁用按钮 + 显示"取消中…"。 */
  isCancelling: boolean;
  /** 额外 className(布局微调用)。 */
  className?: string;
  /** data-testid(便于各 caller 区分定位)。 */
  testId?: string;
}

export function CancelTaskButton({
  onCancel,
  isCancelling,
  className,
  testId = "cancel-task",
}: CancelTaskButtonProps) {
  return (
    <Button
      type="button"
      size="sm"
      variant="ghost"
      onClick={onCancel}
      disabled={isCancelling}
      data-testid={testId}
      className={cn(
        "text-destructive hover:bg-destructive/10 hover:text-destructive",
        className,
      )}
      title="取消正在跑的任务(尽力中断,可能在下一次 LLM 调用间隙才停)"
    >
      {isCancelling ? "取消中…" : "取消"}
    </Button>
  );
}
