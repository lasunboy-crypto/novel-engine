/**
 * WriterProfileSelector(P3-F-03):写作触发面板的"本次写作 Profile 覆盖"
 * 下拉选择器。
 *
 * P3-plan §F-2 字面"覆盖只对单次写作生效,不持久化"——本组件不写 store /
 * 不调 mutation,纯受控:`value: string | null` + `onChange` 回调,选中后
 * 由父组件(`ChapterEditorPage`)持有 state 并透传给 `ChapterActionsToolbar`
 * 触发 `:write` / `:rewrite` 时带 `profile_override` 字段(P3-B-11 后端类 C
 * 顺延已暴露)。
 *
 * ## 选项构造
 *
 * 从 `useWorkspaceConfigQuery(workspaceId)` 拿 `profiles: dict<id, profile>`
 * (P3-F-02 已建 hook,P3-B-10 GET 端点回退语义保证 200 总能拿到合法
 * `WorkspaceConfigOut`)。**第一项始终是 "auto(按 routing 规则)"**,value="" 对应
 * `value=null`,语义是"不覆盖,走 RoutingConfig.rules / agent_routing"。后续
 * 按字典序枚举 profile,显示 `profile_id (provider/model)`(对账后端日志
 * 用,前端 ConfigPage 列表也是这种风格)。
 *
 * ## native `<select>` 而非 shadcn `<Select>`
 *
 * 沿用 P1-F-13 `VersionSelector` 拍板:维护人 2026-05-06 拍 A 用原生
 * `<select>`,选项固定 3-N 个 + bundle 已 747KB(P3-F-02 基线)+ a11y 原生
 * 自动满足,不引入 Radix 焦点陷阱依赖。
 *
 * ## 5 态分支
 *
 * - workspaceId 缺失:渲染禁用占位(组件层短路,父级一般不会渲染本组件)
 * - config loading:禁用 + label 显示"加载中…"
 * - config error / empty profiles:禁用 + label 显示"配置不可用",
 *   父级 :write 仍可触发(profile_override=null 走默认 routing)
 * - config 正常:渲染 select,默认 auto 选中
 */

import type { ChangeEvent } from "react";

import { useWorkspaceConfigQuery } from "@/api/config";

interface WriterProfileSelectorProps {
  workspaceId: string;
  /** 选中的 profile_id;`null` = "auto(走 routing 规则)"。 */
  value: string | null;
  onChange: (value: string | null) => void;
  disabled?: boolean;
}

export function WriterProfileSelector(props: WriterProfileSelectorProps) {
  const configQuery = useWorkspaceConfigQuery(props.workspaceId);

  const handleChange = (event: ChangeEvent<HTMLSelectElement>) => {
    const next = event.target.value;
    props.onChange(next === "" ? null : next);
  };

  // P3-F-03 自决:loading / error / empty 时仍渲染 select 但禁用,label 文
  // 案分支提示;父级触发 :write 时 value=null,后端走 RoutingConfig 规则。
  let labelText = "Profile";
  let isDisabled = props.disabled ?? false;
  let profileEntries: Array<[string, { provider: string; model: string }]> = [];

  if (configQuery.isPending) {
    labelText = "Profile(加载中…)";
    isDisabled = true;
  } else if (configQuery.error) {
    labelText = "Profile(配置不可用)";
    isDisabled = true;
  } else if (configQuery.data) {
    const profiles = configQuery.data.profiles;
    profileEntries = Object.entries(profiles)
      .map<[string, { provider: string; model: string }]>(([id, p]) => [
        id,
        { provider: p.provider, model: p.model },
      ])
      .sort(([a], [b]) => a.localeCompare(b));
    if (profileEntries.length === 0) {
      labelText = "Profile(无可选)";
      isDisabled = true;
    }
  }

  return (
    <label className="flex items-center gap-1 text-xs text-muted-foreground">
      <span>{labelText}</span>
      <select
        data-testid="writer-profile-selector"
        className="rounded border border-border bg-background px-2 py-0.5 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
        value={props.value ?? ""}
        onChange={handleChange}
        disabled={isDisabled}
      >
        <option value="">auto(按 routing 规则)</option>
        {profileEntries.map(([id, p]) => (
          <option key={id} value={id}>
            {id} ({p.provider}/{p.model})
          </option>
        ))}
      </select>
    </label>
  );
}
