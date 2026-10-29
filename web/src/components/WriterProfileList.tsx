/**
 * WriterProfileList(P6-F-04 Part 1):工作区覆盖层下 Writer Profile 列表 + 行操作。
 *
 * 列出 4 内置 profile + 用户自定义 profile;行右侧"编辑" / "删除"按钮:
 *
 * - 内置 profile(`BUILTIN_PROFILE_IDS.has(profileId)` 命中):
 *   - 编辑按钮 enabled(允许覆盖字段)
 *   - 删除按钮 **disabled** + tooltip("内置 profile 不可删除")
 * - 自定义 profile:编辑 + 删除均 enabled
 *
 * 同一行还显示"已覆盖" badge — 当 `profileId in workspaceOverride.writer_profiles`
 * (即 PUT 时会写入工作区 project.yaml)。
 *
 * 删除自定义 profile = 从 `workspaceOverride.writer_profiles` 移除 key;若该
 * profile 在引擎默认里不存在,GET 重拉后该 profile 消失(因为合并视图缺该项)。
 *
 * ## 类 A / 类 B / 类 C 不触达
 *
 * 本组件纯展示 + 行内按钮,无 spec 假定。
 */

import { Button } from "@/components/ui/button";
import { BUILTIN_PROFILE_IDS, type WriterProfileOut } from "@/api/config";

interface WriterProfileListProps {
  /** 合并视图下的 profiles(引擎默认 ⊕ 已保存的 project.yaml + 当前会话内待保存的覆盖)。 */
  mergedProfiles: Record<string, WriterProfileOut>;
  /** 当前会话内未保存的覆盖 profile_id 集合(显示"已覆盖" badge)。 */
  overriddenProfileIds: ReadonlySet<string>;
  onEdit: (profileId: string) => void;
  onDelete: (profileId: string) => void;
}

export function WriterProfileList(props: WriterProfileListProps) {
  const { mergedProfiles, overriddenProfileIds, onEdit, onDelete } = props;
  const entries = Object.entries(mergedProfiles).sort(([a], [b]) =>
    a.localeCompare(b),
  );
  return (
    <div
      className="overflow-hidden rounded border border-border"
      data-testid="writer-profile-list"
    >
      <table className="w-full text-sm">
        <thead className="bg-muted/40 text-left text-xs uppercase tracking-wide text-muted-foreground">
          <tr>
            <th className="px-4 py-2 font-medium">profile_id</th>
            <th className="px-4 py-2 font-medium">来源</th>
            <th className="px-4 py-2 font-medium">provider / model</th>
            <th className="px-4 py-2 font-medium">ctx</th>
            <th className="px-4 py-2 font-medium">cost (in/out)</th>
            <th className="px-4 py-2 font-medium text-right">操作</th>
          </tr>
        </thead>
        <tbody>
          {entries.map(([profileId, profile]) => {
            const isBuiltin = BUILTIN_PROFILE_IDS.has(profileId);
            const isOverridden = overriddenProfileIds.has(profileId);
            return (
              <tr
                key={profileId}
                data-testid="writer-profile-row"
                data-profile-id={profileId}
                data-is-builtin={isBuiltin ? "true" : "false"}
                data-is-overridden={isOverridden ? "true" : "false"}
                className="border-t border-border"
              >
                <td className="px-4 py-2 font-mono text-xs">{profileId}</td>
                <td className="px-4 py-2 text-xs">
                  <SourceBadges
                    isBuiltin={isBuiltin}
                    isOverridden={isOverridden}
                  />
                </td>
                <td className="px-4 py-2 text-xs">
                  <span className="font-mono">{profile.provider}</span>
                  <span className="text-muted-foreground"> / </span>
                  <span className="font-mono">{profile.model}</span>
                </td>
                <td className="px-4 py-2 text-xs text-muted-foreground">
                  {profile.context_window?.toLocaleString() ?? "—"}
                </td>
                <td className="px-4 py-2 text-xs text-muted-foreground">
                  {profile.cost_currency ?? "USD"}{" "}
                  {(profile.cost_per_1k_input ?? 0).toFixed(4)} /{" "}
                  {(profile.cost_per_1k_output ?? 0).toFixed(4)}
                </td>
                <td className="px-4 py-2 text-right">
                  <div className="inline-flex gap-1">
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => onEdit(profileId)}
                      data-testid={`writer-profile-row-edit-${profileId}`}
                    >
                      编辑
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => onDelete(profileId)}
                      disabled={isBuiltin}
                      title={
                        isBuiltin ? "内置 profile 不可删除" : "从工作区覆盖层中移除"
                      }
                      data-testid={`writer-profile-row-delete-${profileId}`}
                    >
                      删除
                    </Button>
                  </div>
                </td>
              </tr>
            );
          })}
          {entries.length === 0 && (
            <tr>
              <td
                colSpan={6}
                className="px-4 py-6 text-center text-sm text-muted-foreground"
                data-testid="writer-profile-list-empty"
              >
                未配置任何 profile。
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function SourceBadges(props: { isBuiltin: boolean; isOverridden: boolean }) {
  return (
    <div className="inline-flex gap-1.5">
      {props.isBuiltin ? (
        <span className="inline-flex items-center rounded bg-zinc-100 px-1.5 py-0.5 text-xs font-medium text-zinc-700">
          内置
        </span>
      ) : (
        <span className="inline-flex items-center rounded bg-violet-100 px-1.5 py-0.5 text-xs font-medium text-violet-700">
          自定义
        </span>
      )}
      {props.isOverridden && (
        <span
          className="inline-flex items-center rounded bg-amber-100 px-1.5 py-0.5 text-xs font-medium text-amber-700"
          data-testid="writer-profile-row-overridden-badge"
        >
          已覆盖(待保存)
        </span>
      )}
    </div>
  );
}
