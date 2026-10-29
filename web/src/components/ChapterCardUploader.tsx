/**
 * ChapterCardUploader(P5-F-06)— 章节卡 JSON 文件上传按钮(补齐 ROADMAP
 * v1.1 §P1 验收清单 2)。
 *
 * UX 流程:
 * 1. 点"上传 ChapterCard JSON 文件"按钮 → 触发隐藏 `<input type="file">`
 * 2. 选 .json 文件 → `FileReader.readAsText` → `JSON.parse`
 * 3. 校验 `chapter_id` 与 URL 参数一致(本地预检,避免发请求才报 422)
 * 4. 调 `useUploadCardMutation`(`POST /chapters/{id}/card`)
 * 5. 成功 toast / 失败 toast(后端 422 `CHAPTER_ID_MISMATCH` /
 *    `VALIDATION_FAILED` 由 `showApiErrorToast` 翻译)
 *
 * ## 设计取舍
 *
 * - **不在组件内做完整 ChapterCard schema 校验**:本地 `JSON.parse` 通过 +
 *   `chapter_id` 一致后直接发请求,让后端 Pydantic v2 兜底完整校验。前端 zod
 *   schema 会与 OpenAPI 类型双源,维护成本不值。
 * - **隐藏 `<input>` + Button 触发**:对齐 NewBookForm 资产上传槽模式;`<input>`
 *   `key` 在每次成功后重置(让同名文件再次选择能触发 onChange)。
 */

import { useRef, useState } from "react";

import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { useUploadCardMutation } from "@/api/chapter_cards";
import type { ChapterCardOut } from "@/api/chapters";

interface ChapterCardUploaderProps {
  workspaceId: string;
  chapterId: string;
  onUploaded?: (card: ChapterCardOut) => void;
}

export function ChapterCardUploader(props: ChapterCardUploaderProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [inputKey, setInputKey] = useState(0);
  const uploadMutation = useUploadCardMutation();

  const triggerPick = () => {
    fileInputRef.current?.click();
  };

  const handleFile = async (file: File) => {
    let parsed: unknown;
    try {
      const text = await file.text();
      parsed = JSON.parse(text);
    } catch (err) {
      const reason = err instanceof Error ? err.message : "JSON 解析失败";
      showApiErrorToast(
        new Error(`无法解析 ${file.name}:${reason}`),
      );
      return;
    }
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      typeof (parsed as { chapter_id?: unknown }).chapter_id !== "string"
    ) {
      showApiErrorToast(new Error("JSON 缺失 chapter_id 字段或非合法对象"));
      return;
    }
    const cardChapterId = (parsed as { chapter_id: string }).chapter_id;
    if (cardChapterId !== props.chapterId) {
      showApiErrorToast(
        new Error(
          `文件内 chapter_id="${cardChapterId}" 与当前章节 ` +
            `"${props.chapterId}" 不一致,请确认上传到正确章节后再试`,
        ),
      );
      return;
    }
    try {
      const saved = await uploadMutation.mutateAsync({
        workspaceId: props.workspaceId,
        chapterId: props.chapterId,
        card: parsed as ChapterCardOut,
      });
      showSuccessToast(
        "章节卡已上传",
        `card_status=${saved.card_status}`,
      );
      props.onUploaded?.(saved);
    } catch (err) {
      showApiErrorToast(err);
    }
  };

  const onChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    void handleFile(file);
    setInputKey((k) => k + 1); // 重置 input 允许重选同名文件
  };

  return (
    <>
      <input
        key={inputKey}
        ref={fileInputRef}
        type="file"
        accept=".json,application/json"
        className="hidden"
        data-testid="card-uploader-input"
        onChange={onChange}
      />
      <Button
        variant="outline"
        size="sm"
        onClick={triggerPick}
        disabled={uploadMutation.isPending}
        data-testid="card-uploader-trigger"
      >
        {uploadMutation.isPending ? "上传中…" : "上传 ChapterCard JSON"}
      </Button>
    </>
  );
}
