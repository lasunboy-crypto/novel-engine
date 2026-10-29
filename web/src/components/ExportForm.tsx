/**
 * ExportForm(P6-F-03):导出参数表单。
 *
 * 对齐 FRONTEND_SPEC §4.14 字面 + plan §"P6-F-03"字面:
 *
 * - 格式 radio 选 md / chaptered_md / epub / docx(4 档)
 * - 范围 radio 选 full / volume / range(3 档)
 *   - scope=full:无额外字段
 *   - scope=volume:`volume_id` 下拉(取自 useVolumesQuery)
 *   - scope=range:`start_chapter_id` + `end_chapter_id` 双下拉(取自
 *     useChapterListQuery)
 * - 选项 checkbox:
 *   - `include_metadata`(默认 true)— zip 内是否含 meta.json
 *   - `include_review_history`(默认 false)— 是否含 review_history.json
 * - "立即导出"按钮 → 调用 onSubmit(body)
 *
 * 字段显隐:scope 切换时自动显示 / 隐藏对应字段;**未走"全字段常显 +
 * 后端报错"模式**(对齐 P6-F-02 PipelineControlPanel 同款 client-side 跨字段
 * 约束预校验)。
 *
 * 不持有"提交中"state — 由调用方传 `submitting` 控制按钮 disabled。
 */

import * as React from "react";

import {
  EXPORT_FORMAT_OPTIONS,
  EXPORT_SCOPE_OPTIONS,
  type ExportCreateIn,
  type ExportFormat,
  type ExportScope,
} from "@/api/exports";
import {
  useChapterListQuery,
  type ChapterListItem,
} from "@/api/chapters";
import { useVolumesQuery, type VolumeOut } from "@/api/volumes";
import { Button } from "@/components/ui/button";

export interface ExportFormProps {
  workspaceId: string;
  /** 提交时调用,父组件负责调 mutation;校验失败前不会被调用。 */
  onSubmit: (body: ExportCreateIn) => void;
  /** 父组件 mutation pending → 按钮 disabled + 文案改成"提交中…"。 */
  submitting?: boolean;
}

export function ExportForm({
  workspaceId,
  onSubmit,
  submitting,
}: ExportFormProps) {
  const volumesQuery = useVolumesQuery(workspaceId);
  const chaptersQuery = useChapterListQuery(workspaceId);

  const [format, setFormat] = React.useState<ExportFormat>("md");
  const [scope, setScope] = React.useState<ExportScope>("full");
  const [volumeId, setVolumeId] = React.useState<string>("");
  const [startChapterId, setStartChapterId] = React.useState<string>("");
  const [endChapterId, setEndChapterId] = React.useState<string>("");
  const [includeLengthNotes, setIncludeLengthNotes] = React.useState(true);
  const [includeMetadata, setIncludeMetadata] = React.useState(true);
  const [includeReviewHistory, setIncludeReviewHistory] = React.useState(false);
  const [formError, setFormError] = React.useState<string | null>(null);

  const volumes: VolumeOut[] = volumesQuery.data?.volumes ?? [];
  const chapters: ChapterListItem[] = chaptersQuery.data ?? [];

  const handleSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setFormError(null);

    // Client-side 跨字段校验(scope 4 档 → 必填字段配对,对齐
    // ExportCreateIn schema 顶部 docstring)。
    if (scope === "volume" && !volumeId) {
      setFormError("scope=volume 必须选择卷");
      return;
    }
    if (scope === "range") {
      if (!startChapterId || !endChapterId) {
        setFormError("scope=range 必须选择起始与结束章节");
        return;
      }
      // 起始 / 结束章序号合理性(start <= end)— 按 chapter_number 比较
      const start = chapters.find((c) => c.chapter_id === startChapterId);
      const end = chapters.find((c) => c.chapter_id === endChapterId);
      if (start && end && start.chapter_number > end.chapter_number) {
        setFormError("起始章节序号不能大于结束章节序号");
        return;
      }
    }

    const body: ExportCreateIn = {
      format,
      scope,
      volume_id: scope === "volume" ? volumeId : null,
      start_chapter_id: scope === "range" ? startChapterId : null,
      end_chapter_id: scope === "range" ? endChapterId : null,
      include_length_notes: includeLengthNotes,
      include_metadata: includeMetadata,
      include_review_history: includeReviewHistory,
    };
    onSubmit(body);
  };

  return (
    <form
      className="space-y-5"
      onSubmit={handleSubmit}
      data-testid="export-form"
    >
      {/* 格式 4 档 */}
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">导出格式</legend>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {EXPORT_FORMAT_OPTIONS.map((opt) => (
            <label
              key={opt.value}
              className={`flex cursor-pointer items-start gap-2 rounded border p-2 text-sm transition ${
                format === opt.value
                  ? "border-primary bg-primary/5"
                  : "border-border hover:border-primary/50"
              }`}
            >
              <input
                type="radio"
                name="export-format"
                value={opt.value}
                checked={format === opt.value}
                onChange={() => setFormat(opt.value)}
                data-testid={`format-${opt.value}`}
                className="mt-1"
              />
              <span>
                <span className="block font-medium">{opt.label}</span>
                <span className="block text-xs text-muted-foreground">
                  {opt.hint}
                </span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      {/* 范围 3 档 */}
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">导出范围</legend>
        <div className="space-y-2">
          {EXPORT_SCOPE_OPTIONS.map((opt) => (
            <label
              key={opt.value}
              className={`flex cursor-pointer items-start gap-2 rounded border p-2 text-sm transition ${
                scope === opt.value
                  ? "border-primary bg-primary/5"
                  : "border-border hover:border-primary/50"
              }`}
            >
              <input
                type="radio"
                name="export-scope"
                value={opt.value}
                checked={scope === opt.value}
                onChange={() => setScope(opt.value)}
                data-testid={`scope-${opt.value}`}
                className="mt-1"
              />
              <span>
                <span className="block font-medium">{opt.label}</span>
                <span className="block text-xs text-muted-foreground">
                  {opt.hint}
                </span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      {/* scope=volume 时显:卷下拉 */}
      {scope === "volume" && (
        <div className="space-y-1">
          <label className="block text-sm font-medium" htmlFor="export-volume">
            选择卷
          </label>
          <select
            id="export-volume"
            value={volumeId}
            onChange={(e) => setVolumeId(e.target.value)}
            className="w-full rounded border border-input bg-background px-3 py-1.5 text-sm"
            data-testid="export-volume-select"
          >
            <option value="">-- 请选择 --</option>
            {volumes.map((v) => (
              <option key={v.volume_id} value={v.volume_id}>
                {v.title}({v.volume_id}· {v.chapter_count} 章)
              </option>
            ))}
          </select>
          {volumesQuery.isLoading && (
            <p className="text-xs text-muted-foreground">加载卷列表…</p>
          )}
        </div>
      )}

      {/* scope=range 时显:起始 + 结束章节下拉 */}
      {scope === "range" && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="space-y-1">
            <label
              className="block text-sm font-medium"
              htmlFor="export-start"
            >
              起始章节
            </label>
            <select
              id="export-start"
              value={startChapterId}
              onChange={(e) => setStartChapterId(e.target.value)}
              className="w-full rounded border border-input bg-background px-3 py-1.5 text-sm"
              data-testid="export-start-select"
            >
              <option value="">-- 请选择 --</option>
              {chapters.map((c) => (
                <option key={c.chapter_id} value={c.chapter_id}>
                  第 {c.chapter_number} 章 · {c.title}({c.chapter_id})
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1">
            <label
              className="block text-sm font-medium"
              htmlFor="export-end"
            >
              结束章节
            </label>
            <select
              id="export-end"
              value={endChapterId}
              onChange={(e) => setEndChapterId(e.target.value)}
              className="w-full rounded border border-input bg-background px-3 py-1.5 text-sm"
              data-testid="export-end-select"
            >
              <option value="">-- 请选择 --</option>
              {chapters.map((c) => (
                <option key={c.chapter_id} value={c.chapter_id}>
                  第 {c.chapter_number} 章 · {c.title}({c.chapter_id})
                </option>
              ))}
            </select>
          </div>
          {chaptersQuery.isLoading && (
            <p className="col-span-full text-xs text-muted-foreground">
              加载章节列表…
            </p>
          )}
        </div>
      )}

      {/* 2 checkbox 选项 */}
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">选项</legend>
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" checked={includeLengthNotes}
            onChange={(e) => setIncludeLengthNotes(e.target.checked)}
            data-testid="export-include-length-notes" className="mt-1" />
          <span>标题附超长编辑提示（关闭则导出纯净稿）</span>
        </label>
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            checked={includeMetadata}
            onChange={(e) => setIncludeMetadata(e.target.checked)}
            data-testid="export-include-metadata"
            className="mt-1"
          />
          <span>
            <span className="block">包含 meta.json</span>
            <span className="block text-xs text-muted-foreground">
              在 zip 中附作者 / 字数 / 卷章数等元数据(默认开启)
            </span>
          </span>
        </label>
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            checked={includeReviewHistory}
            onChange={(e) => setIncludeReviewHistory(e.target.checked)}
            data-testid="export-include-review-history"
            className="mt-1"
          />
          <span>
            <span className="block">包含审稿历史</span>
            <span className="block text-xs text-muted-foreground">
              在 zip 末尾附 review_history.json(每章 review_results 行)
            </span>
          </span>
        </label>
      </fieldset>

      {formError && (
        <p
          role="alert"
          data-testid="export-form-error"
          className="rounded border border-destructive bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {formError}
        </p>
      )}

      <Button
        type="submit"
        disabled={submitting}
        data-testid="export-submit"
        className="w-full sm:w-auto"
      >
        {submitting ? "提交中…" : "立即导出"}
      </Button>
    </form>
  );
}
