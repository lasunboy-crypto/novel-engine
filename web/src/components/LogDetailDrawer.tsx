/**
 * 日志详情抽屉(P4-F-04)。
 *
 * 受控:`open` + `onOpenChange` 由父级 `LogsPage` 控制,`entry` 为当前选中
 * 的 `LogEntryOut`(null 时只显示空 placeholder)。基于 Radix Dialog +
 * 自定义 `data-state` 样式(沿用 P2-F-02 `CharacterDetailDrawer` 模式,
 * 项目不抽 shadcn Sheet 原语)。
 *
 * ## 渲染内容
 *
 * 1. **核心字段表格**:OBSERVABILITY §2.2 必含字段 + 上下文字段(timestamp /
 *    level / logger / event / trace_id / request_id / workspace_id /
 *    chapter_id / module / message)
 * 2. **prompt_hash 字段**:若 `entry.extra.prompt_hash` 存在,显示 hash 文本
 *    + 文件路径提示(`workspaces/<wid>/archives/prompts/<hash>.txt`),
 *    **不渲染为可点击链接**(类 C 冲突,见 impl §4 登记 — 后端无静态文件
 *    挂载,前端 file:// 链接受浏览器同源策略阻止)
 * 3. **trace_id 跳转**:若 `entry.trace_id` 存在,显示 `<Link to>` 到
 *    `/logs/trace/{trace_id}` 跳到 Trace 视图
 * 4. **extra 段 JSON 折叠**:剔除已经独立显示的字段(prompt_hash),其余
 *    用 `<pre><code>` 显示格式化 JSON
 *
 * ## 设计取舍
 *
 * - 抽屉宽度 `max-w-2xl`(比 CharacterDetailDrawer 的 `max-w-xl` 稍宽,
 *   日志 JSON 字段普遍较密)
 * - JSON 渲染用 `JSON.stringify(value, null, 2)`,**不引入语法高亮库**
 *   (避免 P4-F-04 引入新依赖,工程量超 90 分钟粒度)
 * - 点 trace_id 链接关闭抽屉,LogsPage 路由跳转后由父级 prop 控制
 */

import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { Link } from "react-router-dom";

import type { LogEntry } from "@/api/logs";
import { cn } from "@/lib/utils";

export interface LogDetailDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  entry: LogEntry | null;
}

const CORE_FIELDS: Array<{ key: keyof LogEntry; label: string }> = [
  { key: "timestamp", label: "时间" },
  { key: "level", label: "级别" },
  { key: "logger", label: "Logger" },
  { key: "event", label: "事件" },
  { key: "trace_id", label: "Trace ID" },
  { key: "request_id", label: "Request ID" },
  { key: "workspace_id", label: "Workspace ID" },
  { key: "chapter_id", label: "Chapter ID" },
  { key: "module", label: "Module" },
  { key: "message", label: "Message" },
];

export function LogDetailDrawer(props: LogDetailDrawerProps) {
  const { open, onOpenChange, entry } = props;

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
          data-testid="log-detail-drawer"
          className={cn(
            "fixed right-0 top-0 z-50 flex h-full w-full max-w-2xl flex-col gap-4 border-l border-border bg-background p-6 shadow-xl",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=closed]:slide-out-to-right data-[state=open]:slide-in-from-right",
            "duration-200",
          )}
        >
          <div className="flex items-start justify-between gap-4">
            <div className="space-y-1">
              <DialogPrimitive.Title className="text-lg font-semibold">
                日志详情
              </DialogPrimitive.Title>
              <DialogPrimitive.Description className="text-xs text-muted-foreground">
                {entry
                  ? `${entry.event}(${entry.timestamp})`
                  : "未选中日志条目"}
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
            {entry ? (
              <LogEntryBody entry={entry} onClose={() => onOpenChange(false)} />
            ) : (
              <p className="text-sm text-muted-foreground">未选中日志条目。</p>
            )}
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

interface LogEntryBodyProps {
  entry: LogEntry;
  onClose: () => void;
}

function LogEntryBody({ entry, onClose }: LogEntryBodyProps) {
  const extra = entry.extra ?? {};
  const promptHash =
    typeof extra.prompt_hash === "string" ? (extra.prompt_hash as string) : null;
  const otherExtra = Object.fromEntries(
    Object.entries(extra).filter(([k]) => k !== "prompt_hash"),
  );

  return (
    <div className="space-y-5 text-sm">
      <section>
        <h3 className="mb-2 text-xs font-semibold uppercase text-muted-foreground">
          核心字段
        </h3>
        <dl
          data-testid="log-detail-core-fields"
          className="grid grid-cols-[120px_1fr] gap-x-3 gap-y-1.5"
        >
          {CORE_FIELDS.map(({ key, label }) => {
            const value = entry[key];
            return (
              <div className="contents" key={key as string}>
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="break-all font-mono text-xs">
                  {value === null || value === undefined || value === ""
                    ? "—"
                    : String(value)}
                </dd>
              </div>
            );
          })}
        </dl>
      </section>

      {entry.trace_id ? (
        <section>
          <h3 className="mb-2 text-xs font-semibold uppercase text-muted-foreground">
            Trace 跳转
          </h3>
          <Link
            to={`/logs/trace/${encodeURIComponent(entry.trace_id)}`}
            onClick={onClose}
            data-testid="log-detail-trace-link"
            className="text-sm underline"
          >
            查看 Trace 视图({entry.trace_id})
          </Link>
        </section>
      ) : null}

      {promptHash ? (
        <section>
          <h3 className="mb-2 text-xs font-semibold uppercase text-muted-foreground">
            Prompt 归档
          </h3>
          <p
            className="font-mono text-xs"
            data-testid="log-detail-prompt-hash"
          >
            {promptHash}
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            归档文件路径(本地磁盘):
            <code className="ml-1 break-all">
              workspaces/{entry.workspace_id ?? "<workspace_id>"}/archives/prompts/{promptHash}.txt
            </code>
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            P4-F-04 范围内不渲染为可点击链接(后端未挂载 static
            files,见 impl §4 类 C 登记)。
          </p>
        </section>
      ) : null}

      <section>
        <h3 className="mb-2 text-xs font-semibold uppercase text-muted-foreground">
          其它字段({Object.keys(otherExtra).length})
        </h3>
        {Object.keys(otherExtra).length === 0 ? (
          <p className="text-xs text-muted-foreground">无</p>
        ) : (
          <pre
            data-testid="log-detail-extra-json"
            className="max-h-96 overflow-auto rounded border border-border bg-muted/40 p-3 font-mono text-xs"
          >
            <code>{JSON.stringify(otherExtra, null, 2)}</code>
          </pre>
        )}
      </section>
    </div>
  );
}
