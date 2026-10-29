/**
 * VersionSelector(P1-F-13):章节版本下拉选择器(原生 `<select>`)。
 *
 * 任务模板 p1_plan.md:406 字面 "中栏顶加版本下拉选择(`v1` / `v2` / `final`)"。
 * 维护人 2026-05-06 拍 A:用原生 `<select>` 不装 shadcn `<Select>`。理由:
 * - 选项固定 3-N 个,无搜索 / 多选需求
 * - bundle 已 666KB,Q4 单次新装能省则省
 * - a11y 原生 `<select>` 自动满足,不需要 Radix 焦点陷阱
 *
 * ## 选项构造
 *
 * 后端 `GET /chapters/{id}/versions` 返 `[v1, v2, ...]` 数字数组(P1-B-19
 * 决策 6 字面),P1 阶段 final.md 是指向最新版的别名,**不作为独立 version_id
 * 列项**。所以下拉选项 = `versions.map(v => v.version_id)`,最新版加
 * "(最新)" 后缀提示。
 *
 * 任务模板字面 "v1 / v2 / final" 中的 `final` 在 P1 等价于 "v{max}(最新)"——
 * 不引入额外的 `final` 选项避免与后端语义脱节(类 A 冲突按规格源走)。
 *
 * ## 版本切换语义
 *
 * 本组件只暴露选中事件,不调任何 mutation。父组件(ChapterContentPanel)用
 * 受控 state 控制 selectedVersionId → 触发 `useChapterVersionQuery` 重新拉
 * 对应版本正文。**不调 `:switch-version` 端点**(任务模板字面"(可选)",
 * P1-F-13 待审 1 拍 A——不实施 mutation,只本地切换查看,服务端 final 指针
 * 由 orchestrator 在闭环结束时自动管理,P1-B-19 决策 6 字面)。
 */

import type { ChangeEvent } from "react";

interface VersionSelectorProps {
  versions: ReadonlyArray<{ version_id: number }>;
  selectedVersionId: number | null;
  onSelect: (versionId: number) => void;
  disabled?: boolean;
}

export function VersionSelector(props: VersionSelectorProps) {
  if (props.versions.length === 0) {
    return null;
  }

  const latestVersionId =
    props.versions[props.versions.length - 1]?.version_id ?? null;

  const handleChange = (event: ChangeEvent<HTMLSelectElement>) => {
    const next = Number.parseInt(event.target.value, 10);
    if (Number.isInteger(next) && next >= 1) {
      props.onSelect(next);
    }
  };

  return (
    <label className="flex items-center gap-1 text-xs text-muted-foreground">
      <span>版本</span>
      <select
        data-testid="chapter-version-selector"
        className="rounded border border-border bg-background px-2 py-0.5 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
        value={props.selectedVersionId ?? ""}
        onChange={handleChange}
        disabled={props.disabled}
      >
        {props.versions.map((v) => {
          const isLatest = v.version_id === latestVersionId;
          return (
            <option key={v.version_id} value={v.version_id}>
              v{v.version_id}
              {isLatest ? "(最新)" : ""}
            </option>
          );
        })}
      </select>
    </label>
  );
}
