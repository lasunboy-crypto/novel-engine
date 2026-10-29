/**
 * 创建工作区 Dialog(P1-F-08)。
 *
 * 受控组件,基于 shadcn `Dialog` 原语 + React Hook Form + Zod。调用方传
 * `open` / `onOpenChange` 管理打开状态。提交合法时调
 * `useCreateWorkspaceMutation`,成功后由本组件负责 form.reset + 关 Dialog +
 * showSuccessToast,失败由本组件 showApiErrorToast(Dialog 不关、字段保留)。
 *
 * 字段顺序对齐 P1-F-08 决策 6:
 *   display_name → workspace_id → genre → total_chapters → premise
 *
 * 不复用 `ConfirmDialog`(P1-F-08 决策 3):后者是简单 yes/no 模式,塞 form
 * 进去会污染其 API。
 *
 * `activate` 字段不暴露给用户,固定 true(P1-F-08 决策 4)。
 *
 * `total_chapters`:UI 默认 100,字段下方 hint "留空或填 0 表示未规划"
 * (P1-F-08 决策 1 + 用户审议补充 1)。
 *
 * `workspace_id`:字段下方 hint "创建后不可修改;改名需 purge 重建"
 * (P1-F-08 用户审议补充 3)。
 */

import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";

import { useCreateWorkspaceMutation } from "@/api/workspaces";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  createWorkspaceSchema,
  type CreateWorkspaceFormInput,
  type CreateWorkspaceFormOutput,
} from "@/schemas/workspace";
import { cn } from "@/lib/utils";

export interface CreateWorkspaceDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const DEFAULT_VALUES: CreateWorkspaceFormInput = {
  workspace_id: "",
  display_name: "",
  genre: "",
  premise: "",
  total_chapters: 100,
};

export function CreateWorkspaceDialog(props: CreateWorkspaceDialogProps) {
  const { open, onOpenChange } = props;
  const createMutation = useCreateWorkspaceMutation();

  const form = useForm<
    CreateWorkspaceFormInput,
    undefined,
    CreateWorkspaceFormOutput
  >({
    resolver: zodResolver(createWorkspaceSchema),
    defaultValues: DEFAULT_VALUES,
    mode: "onBlur",
  });

  // Dialog 关闭时重置表单(避免下次打开仍带上次输入)。
  // 不用 useEffect 因 form / createMutation 对象引用每 render 变,会触发死循环;
  // 改在 onOpenChange 关闭路径里同步 reset,逻辑直白且无依赖循环风险。
  const handleOpenChange = (next: boolean) => {
    if (!next) {
      form.reset(DEFAULT_VALUES);
    }
    onOpenChange(next);
  };

  const onSubmit = (values: CreateWorkspaceFormOutput) => {
    createMutation.mutate(
      // v1.1 w3-s7:补 default_voice(w3-s4 L2 给 WorkspaceCreateIn 加必填字段
      // 时只改了 NewBookPage,漏了本快速创建对话框 → 前端 typecheck 自 w3-s4
      // 起红)。快速创建无人称选择 UI,默认项目级硬约束第三人称(CLAUDE.md §11);
      // 需要选第一人称 / free 走 NewBookPage 完整流程。
      { ...values, default_voice: "third_person", activate: true },
      {
        onSuccess: (workspace) => {
          showSuccessToast(
            "已创建工作区",
            `${workspace.display_name} (${workspace.workspace_id})`,
          );
          handleOpenChange(false);
        },
        onError: (error) => {
          showApiErrorToast(error);
        },
      },
    );
  };

  const isSubmitting = createMutation.isPending;
  const errors = form.formState.errors;

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>新建工作区</DialogTitle>
          <DialogDescription>
            填写一句话设定与基础元数据,创建后会自动激活为当前活动工作区。
          </DialogDescription>
        </DialogHeader>
        <form
          onSubmit={form.handleSubmit(onSubmit)}
          className="space-y-4"
          noValidate
          data-testid="create-workspace-form"
        >
          <FormField
            label="显示名"
            error={errors.display_name?.message}
            htmlFor="cw-display-name"
            required
          >
            <Input
              id="cw-display-name"
              data-testid="cw-display-name"
              autoFocus
              disabled={isSubmitting}
              placeholder="例如:测试-阿尔法"
              {...form.register("display_name")}
            />
          </FormField>

          <FormField
            label="工作区 ID"
            error={errors.workspace_id?.message}
            hint="创建后不可修改;改名需 purge 重建"
            htmlFor="cw-workspace-id"
            required
          >
            <Input
              id="cw-workspace-id"
              data-testid="cw-workspace-id"
              disabled={isSubmitting}
              placeholder="例如:demo-alpha"
              {...form.register("workspace_id")}
            />
          </FormField>

          <FormField
            label="题材"
            error={errors.genre?.message}
            htmlFor="cw-genre"
            required
          >
            <Input
              id="cw-genre"
              data-testid="cw-genre"
              disabled={isSubmitting}
              placeholder="例如:scifi / fantasy / xianxia"
              {...form.register("genre")}
            />
          </FormField>

          <FormField
            label="目标章数"
            error={errors.total_chapters?.message}
            hint="留空或填 0 表示未规划,后续可继续创建章节"
            htmlFor="cw-total-chapters"
          >
            <Input
              id="cw-total-chapters"
              data-testid="cw-total-chapters"
              type="number"
              min={0}
              step={1}
              disabled={isSubmitting}
              placeholder="100"
              {...form.register("total_chapters")}
            />
          </FormField>

          <FormField
            label="一句话设定"
            error={errors.premise?.message}
            htmlFor="cw-premise"
            required
          >
            <textarea
              id="cw-premise"
              data-testid="cw-premise"
              rows={3}
              disabled={isSubmitting}
              placeholder="一句话讲清这本书的核心冲突或世界观"
              className={cn(
                "flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm",
                "ring-offset-background placeholder:text-muted-foreground",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
                "disabled:cursor-not-allowed disabled:opacity-50",
                "resize-y",
              )}
              {...form.register("premise")}
            />
          </FormField>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => handleOpenChange(false)}
              disabled={isSubmitting}
            >
              取消
            </Button>
            <Button
              type="submit"
              disabled={isSubmitting}
              data-testid="cw-submit"
            >
              {isSubmitting ? "创建中…" : "创建"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

interface FormFieldProps {
  label: string;
  htmlFor: string;
  error?: string;
  hint?: string;
  required?: boolean;
  children: React.ReactNode;
}

function FormField(props: FormFieldProps) {
  const { label, htmlFor, error, hint, required, children } = props;
  return (
    <div className="space-y-1">
      <label
        htmlFor={htmlFor}
        className="block text-sm font-medium text-foreground"
      >
        {label}
        {required && <span className="ml-0.5 text-destructive">*</span>}
      </label>
      {children}
      {error ? (
        <p
          role="alert"
          className="text-xs text-destructive"
          data-testid={`${htmlFor}-error`}
        >
          {error}
        </p>
      ) : hint ? (
        <p className="text-xs text-muted-foreground">{hint}</p>
      ) : null}
    </div>
  );
}
