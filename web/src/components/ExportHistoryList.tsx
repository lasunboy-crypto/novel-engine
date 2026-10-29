/**
 * ExportHistoryList(P6-F-03):导出历史列表 + 状态徽章 + 重新下载。
 *
 * 对齐 plan §"P6-F-03" 字面 ⑥:历史导出列表(最近 10 次)+ 重新下载按钮 +
 * 状态徽章(pending / running / done / failed)。
 *
 * 数据源:`useExportsListQuery(workspaceId)` 后端返按 created_at 倒序的
 * `ExportItemOut[]`,本组件不再排序。
 *
 * 重新下载策略:`status=done` → 启用按钮,点击触发 `<a download>` 链接;
 * 其他状态(pending / running / failed)→ 按钮禁用 + tooltip 说明。
 *
 * 不持有 state — 父 ExportPage 控制 query 刷新时机(create mutation onSuccess +
 * WS task done event 都触发 invalidate)。
 */

import {
  buildExportDownloadUrl,
  EXPORT_FORMAT_OPTIONS,
  EXPORT_STATUS_LABELS,
  useExportsListQuery,
  type ExportItemOut,
} from "@/api/exports";
import { Button } from "@/components/ui/button";

export interface ExportHistoryListProps {
  workspaceId: string;
}

const FORMAT_LABEL_BY_VALUE: Record<string, string> = Object.fromEntries(
  EXPORT_FORMAT_OPTIONS.map((opt) => [opt.value, opt.label]),
);

function formatBytes(n: number | null): string {
  if (n === null || n === undefined) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

function formatRelativeTime(iso: string): string {
  try {
    const dt = new Date(iso);
    return dt.toLocaleString("zh-CN", { hour12: false });
  } catch {
    return iso;
  }
}

function StatusBadge({ status }: { status: ExportItemOut["status"] }) {
  const meta = EXPORT_STATUS_LABELS[status];
  const toneClass = {
    neutral: "bg-muted text-muted-foreground",
    info: "bg-blue-100 text-blue-900 dark:bg-blue-900 dark:text-blue-100",
    success:
      "bg-green-100 text-green-900 dark:bg-green-900 dark:text-green-100",
    danger:
      "bg-red-100 text-red-900 dark:bg-red-900 dark:text-red-100",
  }[meta.tone];
  return (
    <span
      data-testid={`export-status-${status}`}
      className={`inline-flex rounded px-1.5 py-0.5 text-xs font-medium ${toneClass}`}
    >
      {meta.label}
    </span>
  );
}

export function ExportHistoryList({ workspaceId }: ExportHistoryListProps) {
  const query = useExportsListQuery(workspaceId);

  if (query.isLoading) {
    return (
      <p className="text-sm text-muted-foreground">加载导出历史…</p>
    );
  }
  if (query.isError) {
    return (
      <p className="text-sm text-destructive">
        加载失败:{(query.error as Error)?.message ?? "未知错误"}
      </p>
    );
  }

  const items: ExportItemOut[] = query.data?.items ?? [];

  if (items.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="export-history-empty">
        还没有导出记录。选好格式 / 范围后点击「立即导出」。
      </p>
    );
  }

  return (
    <ul className="space-y-2" data-testid="export-history-list">
      {items.map((item) => {
        const canDownload = item.status === "done";
        const downloadUrl = canDownload
          ? buildExportDownloadUrl(workspaceId, item.export_id)
          : undefined;
        return (
          <li
            key={item.export_id}
            className="rounded border border-border p-3 text-sm"
            data-testid={`export-history-row-${item.export_id}`}
          >
            <div className="flex flex-wrap items-center gap-2">
              <code className="rounded bg-muted px-1.5 py-0.5 text-xs">
                {item.export_id}
              </code>
              <StatusBadge status={item.status} />
              <span className="text-muted-foreground">
                {FORMAT_LABEL_BY_VALUE[item.format] ?? item.format}
                {" · "}
                {item.scope}
              </span>
              <span className="ml-auto text-xs text-muted-foreground">
                {formatRelativeTime(item.created_at)}
              </span>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
              <span>章节数:{item.chapter_count ?? "—"}</span>
              <span>大小:{formatBytes(item.file_size_bytes)}</span>
              {item.error && (
                <span
                  className="text-destructive"
                  title={item.error}
                  data-testid={`export-error-${item.export_id}`}
                >
                  错误:{item.error}
                </span>
              )}
              <div className="ml-auto">
                {canDownload ? (
                  <a href={downloadUrl} download>
                    <Button
                      size="sm"
                      variant="outline"
                      data-testid={`export-download-${item.export_id}`}
                    >
                      重新下载
                    </Button>
                  </a>
                ) : (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled
                    title={
                      item.status === "failed"
                        ? "任务失败,无可下载文件"
                        : "任务尚未完成"
                    }
                  >
                    重新下载
                  </Button>
                )}
              </div>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
