/**
 * 伏笔详情抽屉(P2-F-06)。
 *
 * 受控组件:`open` + `onOpenChange` 由 `PlotThreadsPage` 控制,`threadId`
 * 决定抽屉读哪个伏笔。从右侧滑入,基于 Radix Dialog 原语,沿用
 * `CharacterDetailDrawer`(P2-F-02)同模式 — 不前置抽 shadcn `Sheet` 原语。
 *
 * 显示态:
 * - 加载中 → "加载中…"
 * - 加载失败 → 红色错误条(`isApiError` 翻译 code/message,跨工作区 / 不存在
 *   走 404 `PLOT_THREAD_NOT_FOUND`,见 P2-B-15 §2 决策沿用 P2-B-14 §2 决策 1)
 * - 加载完成 → 字段分组只读视图 + footer "切换编辑" 按钮
 *   - 编辑按钮点击 → 关闭抽屉同时打开 `PlotThreadFormDialog mode="edit"`
 *     (由父级 PlotThreadsPage 编排,本组件只负责"切到编辑"的回调通知)
 *
 * 与 CharacterDetailDrawer 的差异:本组件不内嵌 voice-only 子表单(伏笔无
 * voice 概念),编辑入口直接走 Dialog 全字段(行 actions "编辑" 与抽屉"切换
 * 编辑"两条入口最终落到同一 Dialog,语义统一)。
 */

import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";

import { isApiError } from "@/api/errors";
import {
  useThreadDetailQuery,
  type PlotThreadOut,
} from "@/api/threads";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const STATUS_LABEL: Record<PlotThreadOut["status"], string> = {
  planted: "埋下",
  developing: "发展",
  resolved: "已兑现",
  abandoned: "已放弃",
};

export interface PlotThreadDetailDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  workspaceId: string | null;
  threadId: string | null;
  /** 抽屉里的"切换编辑"按钮点击回调:父级关闭抽屉并打开 Dialog 编辑模式。 */
  onEditRequest?: (thread: PlotThreadOut) => void;
}

export function PlotThreadDetailDrawer(props: PlotThreadDetailDrawerProps) {
  const { open, onOpenChange, workspaceId, threadId, onEditRequest } = props;
  const detailQuery = useThreadDetailQuery(workspaceId, threadId);

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay
          className={cn(
            "fixed inset-0 z-50 bg-black/60",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0",
          )}
        />
        <DialogPrimitive.Content
          data-testid="plot-thread-detail-drawer"
          className={cn(
            "fixed right-0 top-0 z-50 flex h-full w-full max-w-xl flex-col gap-4 border-l border-border bg-background p-6 shadow-xl",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=closed]:slide-out-to-right data-[state=open]:slide-in-from-right",
            "duration-200",
          )}
        >
          <div className="flex items-start justify-between gap-4">
            <div className="space-y-1">
              <DialogPrimitive.Title className="text-lg font-semibold">
                伏笔详情
              </DialogPrimitive.Title>
              <DialogPrimitive.Description className="text-xs text-muted-foreground">
                {threadId ? `ID: ${threadId}` : "未选中伏笔"}
              </DialogPrimitive.Description>
            </div>
            <DialogPrimitive.Close
              className="rounded-sm opacity-70 transition-opacity hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring"
              aria-label="关闭"
            >
              <X className="h-4 w-4" />
            </DialogPrimitive.Close>
          </div>

          <div className="flex-1 overflow-y-auto pr-1">
            <DrawerBody detailQuery={detailQuery} />
          </div>

          <div className="flex items-center justify-end gap-2 border-t border-border pt-3">
            <Button
              variant="default"
              onClick={() =>
                detailQuery.data && onEditRequest?.(detailQuery.data)
              }
              disabled={!detailQuery.data || !onEditRequest}
              data-testid="plot-thread-edit-toggle"
            >
              切换编辑
            </Button>
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

interface DrawerBodyProps {
  detailQuery: ReturnType<typeof useThreadDetailQuery>;
}

function DrawerBody(props: DrawerBodyProps) {
  const { detailQuery } = props;

  if (detailQuery.isPending && detailQuery.fetchStatus !== "idle") {
    return (
      <p className="text-sm text-muted-foreground" data-testid="drawer-loading">
        加载中…
      </p>
    );
  }
  if (detailQuery.error) {
    return (
      <div
        className="rounded border border-destructive bg-destructive/10 p-3 text-sm text-destructive"
        data-testid="drawer-error"
      >
        加载伏笔失败:{formatError(detailQuery.error)}
      </div>
    );
  }
  if (!detailQuery.data) {
    return (
      <p className="text-sm text-muted-foreground">未选中伏笔或参数不全。</p>
    );
  }

  return <PlotThreadReadOnlyView thread={detailQuery.data} />;
}

interface PlotThreadReadOnlyViewProps {
  thread: PlotThreadOut;
}

function PlotThreadReadOnlyView(props: PlotThreadReadOnlyViewProps) {
  const t = props.thread;
  return (
    <div className="space-y-5 text-sm" data-testid="plot-thread-readonly-view">
      <Section title="基础信息">
        <Field label="标题" value={t.title} />
        <Field label="状态" value={STATUS_LABEL[t.status]} />
        <Field label="重要性" value={String(t.importance)} />
      </Section>
      <Section title="章节关联">
        <Field label="埋伏章" value={t.planted_chapter ?? "—"} />
        <Field label="计划兑现章" value={t.planned_payoff ?? "—"} />
        <Field label="实际兑现章" value={t.actual_payoff ?? "—"} />
        <Field label="最近更新章" value={t.last_updated_chapter ?? "—"} />
      </Section>
      <Section title="描述">
        <Paragraph value={t.description} />
      </Section>
      {t.related_characters && t.related_characters.length > 0 ? (
        <Section title="相关角色">
          <Field label="角色 ID 列表" value={t.related_characters.join("、")} />
        </Section>
      ) : null}
    </div>
  );
}

function Section(props: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {props.title}
      </h3>
      <div className="space-y-1.5 rounded border border-border bg-card/30 p-3">
        {props.children}
      </div>
    </section>
  );
}

function Field(props: { label: string; value: string }) {
  return (
    <div className="grid grid-cols-[6rem_1fr] gap-2">
      <span className="text-xs text-muted-foreground">{props.label}</span>
      <span className="break-words">{props.value || "—"}</span>
    </div>
  );
}

function Paragraph(props: { value: string }) {
  return (
    <p className="whitespace-pre-wrap break-words">{props.value || "—"}</p>
  );
}

function formatError(error: unknown): string {
  if (isApiError(error)) {
    return `${error.code}: ${error.message}`;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "未知错误";
}
