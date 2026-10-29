/**
 * 伏笔新增 / 全字段编辑 Dialog(P2-F-06)。
 *
 * 受控组件,沿用 `CharacterFormDialog`(P2-F-04)结构:shadcn `Dialog` 原语 +
 * RHF + zodResolver(基础字段)+ `useState`(`related_characters` 受控值字
 * 段)。两条入口都走本组件:
 *
 * - **新增**(`mode="create"`):页顶"新增伏笔"按钮 → 空白表单
 * - **编辑**(`mode="edit"`,`thread` 必传):行 actions"编辑" / 抽屉"切换编辑"
 *   → 带值打开,`thread_id` 改为只读(后端 PUT 不允许改 ID)
 *
 * 字段范围(沿用 NEXT_STEPS §5 字面 + P2-F-05 §6 留观项):
 *
 * - 暴露:thread_id(create-only)/ title / status / importance /
 *   planted_chapter / planned_payoff / actual_payoff / description /
 *   related_characters
 * - 透传(不暴露):last_updated_chapter / created_at——由 MemoryExtractor /
 *   chapter mutation 自动同步,前端编辑会破坏数据一致性
 */

import * as React from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";

import {
  useCreateThreadMutation,
  useUpdateThreadMutation,
  type PlotThread,
  type PlotThreadOut,
} from "@/api/threads";
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
import { TagInput } from "@/components/VoiceProfileEditor";
import { cn } from "@/lib/utils";
import {
  emptyStringToNull,
  PLOT_THREAD_STATUS_VALUES,
  PLOT_THREAD_TYPE_VALUES,
  plotThreadFormSchema,
  type PlotThreadFormInput,
  type PlotThreadFormOutput,
} from "@/schemas/plot_thread";

const STATUS_OPTIONS: ReadonlyArray<{
  value: (typeof PLOT_THREAD_STATUS_VALUES)[number];
  label: string;
}> = [
  { value: "planted", label: "埋下" },
  { value: "developing", label: "发展" },
  { value: "resolved", label: "已兑现" },
  { value: "abandoned", label: "已放弃" },
];

// G2(v1.1 wave 4 session 2):伏笔类型分类 4 档
const THREAD_TYPE_OPTIONS: ReadonlyArray<{
  value: (typeof PLOT_THREAD_TYPE_VALUES)[number];
  label: string;
}> = [
  { value: "plot_event", label: "剧情线" },
  { value: "foreshadowing", label: "伏笔" },
  { value: "metaphor", label: "隐喻" },
  { value: "callback", label: "回响" },
];

type DialogMode = "create" | "edit";

export interface PlotThreadFormDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  workspaceId: string;
  mode: DialogMode;
  /** 编辑模式必传;新建模式忽略。 */
  thread?: PlotThreadOut;
}

export function PlotThreadFormDialog(props: PlotThreadFormDialogProps) {
  const { open, onOpenChange, workspaceId, mode, thread } = props;
  const isEdit = mode === "edit" && thread !== undefined;

  const createMutation = useCreateThreadMutation();
  const updateMutation = useUpdateThreadMutation();

  const [relatedCharacters, setRelatedCharacters] = React.useState<string[]>(
    [],
  );

  const defaultValues: PlotThreadFormInput = React.useMemo(
    () => ({
      thread_id: thread?.thread_id ?? "",
      title: thread?.title ?? "",
      thread_type: thread?.thread_type ?? "plot_event",
      status: thread?.status ?? "planted",
      importance: thread?.importance ?? 3,
      planted_chapter: thread?.planted_chapter ?? "",
      planned_payoff: thread?.planned_payoff ?? "",
      actual_payoff: thread?.actual_payoff ?? "",
      description: thread?.description ?? "",
    }),
    [thread],
  );

  const form = useForm<
    PlotThreadFormInput,
    undefined,
    PlotThreadFormOutput
  >({
    resolver: zodResolver(plotThreadFormSchema),
    defaultValues,
    mode: "onBlur",
  });

  React.useEffect(() => {
    if (!open) return;
    form.reset(defaultValues);
    setRelatedCharacters(thread?.related_characters ?? []);
    // form 引用稳定,defaultValues 借 useMemo 稳定
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, thread, defaultValues]);

  const onSubmit = (values: PlotThreadFormOutput) => {
    const payload: PlotThread = {
      thread_id: isEdit ? thread.thread_id : values.thread_id,
      workspace_id: workspaceId,
      title: values.title,
      thread_type: values.thread_type,
      status: values.status,
      importance: values.importance,
      planted_chapter: values.planted_chapter,
      planned_payoff: emptyStringToNull(values.planned_payoff),
      actual_payoff: emptyStringToNull(values.actual_payoff),
      description: values.description,
      related_characters: relatedCharacters,
      // 编辑时透传后端管理字段;新建时给默认值。
      created_at: thread?.created_at ?? new Date().toISOString(),
      last_updated_chapter: thread?.last_updated_chapter ?? null,
    };

    if (isEdit) {
      updateMutation.mutate(
        { workspaceId, thread: payload },
        {
          onSuccess: () => {
            showSuccessToast(
              "已保存",
              `伏笔 ${payload.title}(${payload.thread_id})已更新`,
            );
            onOpenChange(false);
          },
          onError: (err) => showApiErrorToast(err),
        },
      );
    } else {
      createMutation.mutate(
        { workspaceId, thread: payload },
        {
          onSuccess: () => {
            showSuccessToast(
              "已新增",
              `伏笔 ${payload.title}(${payload.thread_id})已创建`,
            );
            onOpenChange(false);
          },
          onError: (err) => showApiErrorToast(err),
        },
      );
    }
  };

  const isSubmitting = createMutation.isPending || updateMutation.isPending;
  const errors = form.formState.errors;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{isEdit ? "编辑伏笔" : "新增伏笔"}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? "全字段编辑;伏笔 ID 在创建后不可修改。"
              : "填写新伏笔字段;最近更新章由后端自动同步。"}
          </DialogDescription>
        </DialogHeader>
        <form
          onSubmit={form.handleSubmit(onSubmit)}
          className="space-y-4"
          noValidate
          data-testid="plot-thread-form"
        >
          <FormField
            label="伏笔 ID"
            error={errors.thread_id?.message}
            hint={
              isEdit
                ? "创建后不可修改"
                : "工作区内唯一,长度 1-64;后端不要求字符集,但建议 t-1 / 主线-001 之类"
            }
            htmlFor="ptf-thread-id"
            required
          >
            <Input
              id="ptf-thread-id"
              data-testid="ptf-thread-id"
              autoFocus={!isEdit}
              disabled={isSubmitting || isEdit}
              readOnly={isEdit}
              placeholder="例如:t-1"
              {...form.register("thread_id")}
            />
          </FormField>

          <FormField
            label="标题"
            error={errors.title?.message}
            htmlFor="ptf-title"
            required
          >
            <Input
              id="ptf-title"
              data-testid="ptf-title"
              disabled={isSubmitting}
              placeholder="例如:祖传宝剑的真名"
              {...form.register("title")}
            />
          </FormField>

          <FormField
            label="类型"
            error={errors.thread_type?.message}
            htmlFor="ptf-thread-type"
            required
            hint="伏笔 / 隐喻 / 回响用于本章呼应链路(G2)"
          >
            <select
              id="ptf-thread-type"
              data-testid="ptf-thread-type"
              disabled={isSubmitting}
              className={selectClass}
              {...form.register("thread_type")}
            >
              {THREAD_TYPE_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </FormField>

          <FormField
            label="状态"
            error={errors.status?.message}
            htmlFor="ptf-status"
            required
          >
            <select
              id="ptf-status"
              data-testid="ptf-status"
              disabled={isSubmitting}
              className={selectClass}
              {...form.register("status")}
            >
              {STATUS_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </FormField>

          <FormField
            label="重要性(1-5)"
            error={errors.importance?.message}
            htmlFor="ptf-importance"
            required
          >
            <Input
              id="ptf-importance"
              data-testid="ptf-importance"
              type="number"
              min={1}
              max={5}
              step={1}
              disabled={isSubmitting}
              {...form.register("importance")}
            />
          </FormField>

          <FormField
            label="埋伏章"
            error={errors.planted_chapter?.message}
            htmlFor="ptf-planted-chapter"
            required
            hint="例如:ch-0001"
          >
            <Input
              id="ptf-planted-chapter"
              data-testid="ptf-planted-chapter"
              disabled={isSubmitting}
              placeholder="ch-0001"
              {...form.register("planted_chapter")}
            />
          </FormField>

          <FormField
            label="计划兑现章"
            error={errors.planned_payoff?.message}
            htmlFor="ptf-planned-payoff"
            hint="留空表示尚未规划"
          >
            <Input
              id="ptf-planned-payoff"
              data-testid="ptf-planned-payoff"
              disabled={isSubmitting}
              placeholder="ch-0050"
              {...form.register("planned_payoff")}
            />
          </FormField>

          <FormField
            label="实际兑现章"
            error={errors.actual_payoff?.message}
            htmlFor="ptf-actual-payoff"
            hint="留空表示尚未兑现"
          >
            <Input
              id="ptf-actual-payoff"
              data-testid="ptf-actual-payoff"
              disabled={isSubmitting}
              placeholder="ch-0050"
              {...form.register("actual_payoff")}
            />
          </FormField>

          <FormField
            label="描述"
            error={errors.description?.message}
            htmlFor="ptf-description"
            required
          >
            <textarea
              id="ptf-description"
              data-testid="ptf-description"
              rows={3}
              disabled={isSubmitting}
              placeholder="伏笔的详细描述"
              className={textareaClass}
              {...form.register("description")}
            />
          </FormField>

          <FormField label="相关角色" htmlFor="ptf-related-characters">
            <TagInput
              id="ptf-related-characters"
              testId="ptf-related-characters"
              disabled={isSubmitting}
              value={relatedCharacters}
              onChange={setRelatedCharacters}
              placeholder="角色 ID,回车 / 逗号添加"
            />
          </FormField>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={isSubmitting}
            >
              取消
            </Button>
            <Button
              type="submit"
              disabled={isSubmitting}
              data-testid="ptf-submit"
            >
              {isSubmitting ? "保存中…" : isEdit ? "保存" : "创建"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

const inputBase = cn(
  "flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm",
  "ring-offset-background placeholder:text-muted-foreground",
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
  "disabled:cursor-not-allowed disabled:opacity-50",
);

const textareaClass = cn(inputBase, "resize-y");
const selectClass = cn(inputBase, "h-10");

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
