/**
 * VoiceProfile 子表单(P2-F-03)。
 *
 * 受控值组件:`value: VoiceProfile + onChange: (next) => void`,不直接接
 * react-hook-form context。父级(`CharacterDetailDrawer` 编辑态)用 RHF
 * Controller 管理整体 Character form,把 voice_profile 子片段绑到本组件,
 * 让本组件保持纯受控、易测试、易在 P2-F-04 / P2-F-07 复用。
 *
 * 字段(对齐 P2-B-01 `VoiceProfile` Pydantic 模型):
 * - tone / sentence_style / self_address(string,必填)→ 文本框
 * - catchphrases / forbidden_phrases(string[])→ 标签输入(回车 / 逗号 添加)
 * - addresses_others(Record<string,string>)→ KV 行编辑器
 *
 * 不做:① 跨字段联动(forbidden_phrases 与正文 dirty diff 高亮留 P2-F-07
 * VoiceCheckPanel);② 拖拽排序 / 复杂校验(标签内容仅去前后空白 + 去重 +
 * 空字符串过滤,后端 P2-B-01 未要求字符集校验);③ 与 store / API 直连
 * (调用方负责)。
 */

import * as React from "react";

import type { VoiceProfile } from "@/api/characters";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

export interface VoiceProfileEditorProps {
  value: VoiceProfile;
  onChange: (next: VoiceProfile) => void;
  disabled?: boolean;
  /**
   * 字段级错误信息映射,从父级 RHF formState.errors.voice_profile 透传。
   * 例 `{ tone: "tone 不能为空" }`。
   */
  errors?: Partial<Record<keyof VoiceProfile, string | undefined>>;
}

export function VoiceProfileEditor(props: VoiceProfileEditorProps) {
  const { value, onChange, disabled, errors } = props;

  const update = React.useCallback(
    <K extends keyof VoiceProfile>(key: K, next: VoiceProfile[K]) => {
      onChange({ ...value, [key]: next });
    },
    [value, onChange],
  );

  return (
    <div
      className="space-y-4 rounded border border-border bg-card/30 p-3"
      data-testid="voice-profile-editor"
    >
      <FieldRow
        label="语气"
        htmlFor="vp-tone"
        required
        error={errors?.tone}
      >
        <Input
          id="vp-tone"
          data-testid="vp-tone"
          disabled={disabled}
          value={value.tone}
          onChange={(e) => update("tone", e.target.value)}
          placeholder="冷峻 / 戏谑 / 温和"
        />
      </FieldRow>

      <FieldRow
        label="句式"
        htmlFor="vp-sentence-style"
        required
        error={errors?.sentence_style}
      >
        <Input
          id="vp-sentence-style"
          data-testid="vp-sentence-style"
          disabled={disabled}
          value={value.sentence_style}
          onChange={(e) => update("sentence_style", e.target.value)}
          placeholder="短句直白 / 长句修饰"
        />
      </FieldRow>

      <FieldRow
        label="自称"
        htmlFor="vp-self-address"
        required
        error={errors?.self_address}
      >
        <Input
          id="vp-self-address"
          data-testid="vp-self-address"
          disabled={disabled}
          value={value.self_address}
          onChange={(e) => update("self_address", e.target.value)}
          placeholder="我 / 朕 / 在下"
        />
      </FieldRow>

      <FieldRow label="口头禅" htmlFor="vp-catchphrases">
        <TagInput
          id="vp-catchphrases"
          testId="vp-catchphrases"
          disabled={disabled}
          value={value.catchphrases ?? []}
          onChange={(tags) => update("catchphrases", tags)}
          placeholder="回车或逗号添加,如:在下不才"
        />
      </FieldRow>

      <FieldRow label="禁用词" htmlFor="vp-forbidden-phrases">
        <TagInput
          id="vp-forbidden-phrases"
          testId="vp-forbidden-phrases"
          disabled={disabled}
          value={value.forbidden_phrases ?? []}
          onChange={(tags) => update("forbidden_phrases", tags)}
          placeholder="回车或逗号添加该角色不会说的词"
        />
      </FieldRow>

      <FieldRow label="称呼他人" htmlFor="vp-addresses-others">
        <KvEditor
          testId="vp-addresses-others"
          disabled={disabled}
          value={value.addresses_others ?? {}}
          onChange={(kv) => update("addresses_others", kv)}
          keyPlaceholder="对方角色 ID"
          valuePlaceholder="称呼,如:师兄"
        />
      </FieldRow>
    </div>
  );
}

interface FieldRowProps {
  label: string;
  htmlFor: string;
  required?: boolean;
  error?: string;
  children: React.ReactNode;
}

function FieldRow(props: FieldRowProps) {
  return (
    <div className="space-y-1">
      <label
        htmlFor={props.htmlFor}
        className="block text-xs font-medium text-foreground"
      >
        {props.label}
        {props.required && <span className="ml-0.5 text-destructive">*</span>}
      </label>
      {props.children}
      {props.error ? (
        <p
          role="alert"
          className="text-xs text-destructive"
          data-testid={`${props.htmlFor}-error`}
        >
          {props.error}
        </p>
      ) : null}
    </div>
  );
}

export interface TagInputProps {
  id: string;
  testId: string;
  disabled?: boolean;
  value: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
}

/**
 * 标签输入组件(回车 / 逗号添加,Backspace 在空输入框删最后一个标签)。
 *
 * P2-F-04 起从 VoiceProfileEditor 模块 export 给 CharacterFormDialog 复用
 * (aliases / abilities / secrets / goals_long_term / goals_short_term 五个
 * `string[]` 字段)。沿用 P2-F-03 §6 留观项决策:**不前置抽到 ui/ 文件**,
 * 等 P2-F-05 / P2-F-06 PlotThread 出现第三处复用时再抽到
 * `web/src/components/ui/tag-input.tsx`。
 */
export function TagInput(props: TagInputProps) {
  const { id, testId, disabled, value, onChange, placeholder } = props;
  const [draft, setDraft] = React.useState("");

  const commit = (raw: string) => {
    const cleaned = raw
      .split(",")
      .map((s) => s.trim())
      .filter((s) => s.length > 0 && !value.includes(s));
    if (cleaned.length === 0) {
      return;
    }
    onChange([...value, ...cleaned]);
    setDraft("");
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter" || event.key === ",") {
      event.preventDefault();
      commit(draft);
    } else if (event.key === "Backspace" && draft.length === 0 && value.length > 0) {
      // Backspace 在空输入框删除最后一个标签(标签输入交互惯例)
      event.preventDefault();
      onChange(value.slice(0, -1));
    }
  };

  const removeAt = (index: number) => {
    onChange(value.filter((_, i) => i !== index));
  };

  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-1.5 rounded-md border border-input bg-background px-2 py-1.5",
        "focus-within:ring-2 focus-within:ring-ring",
        disabled && "cursor-not-allowed opacity-50",
      )}
      data-testid={testId}
    >
      {value.map((tag, idx) => (
        <span
          key={`${tag}-${idx}`}
          className="inline-flex items-center gap-1 rounded bg-emerald-100 px-2 py-0.5 text-xs text-emerald-900"
          data-testid={`${testId}-tag`}
        >
          <span className="break-all">{tag}</span>
          {!disabled && (
            <button
              type="button"
              aria-label={`删除标签 ${tag}`}
              onClick={() => removeAt(idx)}
              className="text-emerald-700 hover:text-emerald-900"
            >
              ×
            </button>
          )}
        </span>
      ))}
      <input
        id={id}
        type="text"
        disabled={disabled}
        value={draft}
        placeholder={value.length === 0 ? placeholder : ""}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={handleKeyDown}
        onBlur={() => draft.trim().length > 0 && commit(draft)}
        className="flex-1 min-w-[8rem] bg-transparent text-sm outline-none"
        data-testid={`${testId}-input`}
      />
    </div>
  );
}

export interface KvEditorProps {
  testId: string;
  disabled?: boolean;
  value: Record<string, string>;
  onChange: (next: Record<string, string>) => void;
  keyPlaceholder?: string;
  valuePlaceholder?: string;
}

/**
 * KV 行编辑器:每行一对 key / value Input + 删除按钮,底部"添加条目"。
 *
 * P2-F-04 起从 VoiceProfileEditor 模块 export 给 CharacterFormDialog 复用
 * (relationships 字段)。抽离时机同 TagInput,见 §TagInput docstring。
 */
export function KvEditor(props: KvEditorProps) {
  const { testId, disabled, value, onChange, keyPlaceholder, valuePlaceholder } =
    props;

  // 内部用 entries 数组保留行的"未提交 key"(允许暂时空 key 不丢行),
  // 提交到父 onChange 时用唯一非空 key 重建对象。
  const [rows, setRows] = React.useState<Array<{ key: string; value: string }>>(
    () => Object.entries(value).map(([k, v]) => ({ key: k, value: v })),
  );

  // 父值变更时(load detail 完成 / 取消编辑回填)同步重建行。
  // 用 JSON 字符串做依赖比较,避免每次父级 render 重建数组导致死循环。
  const valueSig = React.useMemo(() => JSON.stringify(value), [value]);
  React.useEffect(() => {
    setRows(Object.entries(value).map(([k, v]) => ({ key: k, value: v })));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [valueSig]);

  const flush = (next: Array<{ key: string; value: string }>) => {
    setRows(next);
    const acc: Record<string, string> = {};
    for (const row of next) {
      const k = row.key.trim();
      if (k.length === 0) continue;
      acc[k] = row.value;
    }
    onChange(acc);
  };

  const updateKey = (i: number, k: string) => {
    flush(rows.map((r, idx) => (idx === i ? { ...r, key: k } : r)));
  };
  const updateValue = (i: number, v: string) => {
    flush(rows.map((r, idx) => (idx === i ? { ...r, value: v } : r)));
  };
  const removeRow = (i: number) => {
    flush(rows.filter((_, idx) => idx !== i));
  };
  const addRow = () => {
    flush([...rows, { key: "", value: "" }]);
  };

  return (
    <div className="space-y-2" data-testid={testId}>
      {rows.length === 0 ? (
        <p className="text-xs text-muted-foreground">暂无条目。</p>
      ) : (
        <ul className="space-y-1">
          {rows.map((row, idx) => (
            <li
              key={idx}
              className="grid grid-cols-[10rem_1fr_2rem] gap-2"
              data-testid={`${testId}-row`}
            >
              <Input
                disabled={disabled}
                value={row.key}
                onChange={(e) => updateKey(idx, e.target.value)}
                placeholder={keyPlaceholder}
                aria-label="key"
              />
              <Input
                disabled={disabled}
                value={row.value}
                onChange={(e) => updateValue(idx, e.target.value)}
                placeholder={valuePlaceholder}
                aria-label="value"
              />
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={disabled}
                onClick={() => removeRow(idx)}
                aria-label="删除条目"
              >
                ×
              </Button>
            </li>
          ))}
        </ul>
      )}
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={disabled}
        onClick={addRow}
        data-testid={`${testId}-add`}
      >
        添加条目
      </Button>
    </div>
  );
}
