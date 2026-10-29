/**
 * ConfigPage(P6-F-05 Part 2 — 缩减范围):工作区配置完整版(三 tab + Writer
 * Profile CRUD + Writer Routing 覆盖)。
 *
 * 路由:`/config`。承接 P3-F-02 简版只读视图,P6-F-04 落三 tab + Profile
 * CRUD;本任务 P6-F-05 在其上扩 **Writer Routing 覆盖编辑器**(default profile
 * 下拉 + 路由规则数组增删 + 上下移)。
 *
 * ## P6-F-05 范围 vs 后续增量
 *
 * 原 P6-F-05 先落 Routing；后端随后已具备 reviewer / retrieval / cost_guard
 * schema。当前页新增成本守卫的单工作区日上限独立保存入口，走专属子资源，
 * 不借统一配置 PUT 清掉其他覆盖。Reviewer / retrieval 编辑器仍顺延；API key
 * 已在系统设置页管理。
 *
 * ## 三 tab 框架
 *
 * - **Tab 1 引擎默认**(read-only):展示当前合并后(引擎 config.yaml ⊕ 工作区
 *   project.yaml)的配置视图 — Writer Profiles 全字段 + Writer Routing + Agent
 *   Routing + 系统健康检查。承接 P3-F-02 简版的全部展示。
 * - **Tab 2 工作区覆盖**(CRUD):Writer Profile 新增 / 编辑 / 删除 + Writer
 *   Routing default / rules 编辑器；成本守卫日上限使用独立安全保存入口。
 * - **Tab 3 临时运行时**(info):说明 "临时运行时覆盖通过 RunControlPage /
 *   ChapterCardReviewPage 的 force_profile 字段实施",给跳转链接。
 *
 * ## 工作区覆盖层 state(扩 routing 段)
 *
 * - `overrideProfiles: Record<string, WriterProfileIn>` — 初始空,P6-F-04 已落
 * - `overrideRouting: RoutingConfigIn | null` — 初始 null(未覆盖),P6-F-05
 *   新增。null 表示 PUT body 省略 `writer_routing` 段(后端走引擎默认)
 *
 * dirty 判定:任一段非空(profiles 字典非空 OR routing 非 null)。
 *
 * ## 保存语义(对齐 P6-B-12 plan §字面 ⑦)
 *
 * PUT 是 **整体替换工作区覆盖层** — body 内的 `writer_profiles` dict +
 * `writer_routing` 段完整写入 `project.yaml`(`yaml.safe_dump`)。后端
 * `_make_router_factory` 每次 LLM 调用重读 yaml,**无独立 `:reload` 端点**。
 * 前端 PUT 成功后 `invalidate(["workspace_config", ws])` 让 GET 拉回新的合并
 * 视图。
 *
 * **v1.1 w3-s8(round-trip 修复)**:`generation` 段有专属端点(L1
 * GenerationParamsPanel,`PUT /config/generation`),后端统一 PUT 在 body 未提及
 * `generation` 时**保留**现有该段 —— 本页只发 writer 两段也不会冲掉 L1 设的生成
 * 参数(旧行为下会静默丢失)。其余 5 段无专属端点,沿用整体替换(省略 = 清空)。
 */

import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { isApiError } from "@/api/errors";
import {
  useUpdateWorkspaceConfigMutation,
  useWorkspaceConfigQuery,
  type RoutingConfigIn,
  type RoutingRuleOut,
  type WorkspaceConfigOut,
  type WorkspaceConfigUpdateIn,
  type WriterProfileIn,
  type WriterProfileOut,
} from "@/api/config";
import { useRoutableAgentsQuery } from "@/api/routable_agents";
import {
  useSystemHealthQuery,
  type HealthCheckItemOut,
} from "@/api/system";
import { useWorkspacesQuery, type WorkspaceOut } from "@/api/workspaces";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { CostGuardOverridePanel } from "@/components/CostGuardOverridePanel";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import { RoutingRuleEditor } from "@/components/RoutingRuleEditor";
import {
  WriterProfileForm,
  type WriterProfileFormSubmit,
} from "@/components/WriterProfileForm";
import { WriterProfileList } from "@/components/WriterProfileList";
import { useActiveWorkspace } from "@/store/activeWorkspace";

type TabKey = "engine_default" | "workspace_override" | "runtime";

const TAB_LABELS: Record<TabKey, string> = {
  engine_default: "引擎默认",
  workspace_override: "工作区覆盖",
  runtime: "临时运行时",
};

export default function ConfigPage() {
  const workspacesQuery = useWorkspacesQuery();
  const activeWorkspace = useActiveWorkspace(workspacesQuery.data);
  const workspaceId = activeWorkspace?.workspace_id ?? null;
  const configQuery = useWorkspaceConfigQuery(workspaceId);

  const [activeTab, setActiveTab] = useState<TabKey>("engine_default");

  return (
    <div className="space-y-6" data-testid="config-page">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold">配置</h1>
        <ConfigHeaderHint
          workspacesLoading={workspacesQuery.isPending}
          workspacesError={workspacesQuery.error}
          activeWorkspace={activeWorkspace}
        />
      </header>

      <TabsBar activeTab={activeTab} onTabChange={setActiveTab} />

      <ConfigContent
        activeTab={activeTab}
        workspacesLoading={workspacesQuery.isPending}
        workspacesError={workspacesQuery.error}
        workspacesEmpty={
          !workspacesQuery.isPending &&
          (workspacesQuery.data?.length ?? 0) === 0
        }
        activeWorkspace={activeWorkspace}
        configLoading={
          configQuery.isPending && configQuery.fetchStatus !== "idle"
        }
        configError={configQuery.error}
        config={configQuery.data ?? null}
        workspaceId={workspaceId}
      />
    </div>
  );
}

function TabsBar(props: {
  activeTab: TabKey;
  onTabChange: (tab: TabKey) => void;
}) {
  const tabs: TabKey[] = [
    "engine_default",
    "workspace_override",
    "runtime",
  ];
  return (
    <nav
      className="flex gap-1 border-b border-border"
      data-testid="config-tabs"
    >
      {tabs.map((t) => {
        const active = props.activeTab === t;
        return (
          <button
            key={t}
            type="button"
            onClick={() => props.onTabChange(t)}
            data-testid={`config-tab-${t}`}
            data-active={active ? "true" : "false"}
            className={
              "border-b-2 px-4 py-2 text-sm font-medium transition-colors " +
              (active
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground")
            }
          >
            {TAB_LABELS[t]}
          </button>
        );
      })}
    </nav>
  );
}

interface ConfigHeaderHintProps {
  workspacesLoading: boolean;
  workspacesError: unknown;
  activeWorkspace: WorkspaceOut | null;
}

function ConfigHeaderHint(props: ConfigHeaderHintProps) {
  if (props.workspacesLoading || props.workspacesError) {
    return null;
  }
  if (!props.activeWorkspace) {
    return (
      <p className="text-sm text-muted-foreground">当前没有活动工作区。</p>
    );
  }
  return (
    <p className="text-sm text-muted-foreground">
      当前活动工作区:
      <span
        className="ml-1 font-medium text-foreground"
        data-testid="config-active-workspace-display-name"
      >
        {props.activeWorkspace.display_name}
      </span>
      <span className="ml-1 text-xs">
        ({props.activeWorkspace.workspace_id})
      </span>
      <span className="ml-2 text-xs">
        合并源:引擎 config.yaml ⊕ workspaces/
        {props.activeWorkspace.workspace_id}/project.yaml
      </span>
    </p>
  );
}

interface ConfigContentProps {
  activeTab: TabKey;
  workspacesLoading: boolean;
  workspacesError: unknown;
  workspacesEmpty: boolean;
  activeWorkspace: WorkspaceOut | null;
  configLoading: boolean;
  configError: unknown;
  config: WorkspaceConfigOut | null;
  workspaceId: string | null;
}

function ConfigContent(props: ConfigContentProps) {
  if (props.workspacesLoading) {
    return (
      <div className="rounded border border-border p-6 text-sm text-muted-foreground">
        加载工作区列表中…
      </div>
    );
  }
  if (props.workspacesError) {
    return (
      <div
        data-testid="config-workspaces-error"
        className="rounded border border-destructive bg-destructive/10 p-6 text-sm text-destructive"
      >
        加载工作区失败:{formatError(props.workspacesError)}
      </div>
    );
  }
  if (props.workspacesEmpty) {
    return (
      <div className="rounded border border-dashed border-border p-6 text-sm text-muted-foreground">
        还没有工作区。请先到
        <Link to="/workspaces" className="mx-1 underline">
          工作区列表
        </Link>
        创建一个。
      </div>
    );
  }
  if (!props.activeWorkspace || !props.workspaceId) {
    return (
      <div className="rounded border border-dashed border-border p-6 text-sm text-muted-foreground">
        当前没有活动工作区。请到
        <Link to="/workspaces" className="mx-1 underline">
          工作区列表
        </Link>
        切换到一个工作区。
      </div>
    );
  }
  if (props.configLoading) {
    return (
      <div className="rounded border border-border p-6 text-sm text-muted-foreground">
        加载配置中…
      </div>
    );
  }
  if (props.configError) {
    return (
      <div
        data-testid="config-load-error"
        className="rounded border border-destructive bg-destructive/10 p-6 text-sm text-destructive"
      >
        加载配置失败:{formatError(props.configError)}
      </div>
    );
  }
  if (!props.config) {
    return null;
  }

  if (props.activeTab === "engine_default") {
    return (
      <EngineDefaultTab
        config={props.config}
        workspaceId={props.workspaceId}
      />
    );
  }
  if (props.activeTab === "workspace_override") {
    return (
      <WorkspaceOverrideTab
        config={props.config}
        workspaceId={props.workspaceId}
      />
    );
  }
  return <RuntimeTab workspaceId={props.workspaceId} />;
}

function EngineDefaultTab(props: {
  config: WorkspaceConfigOut;
  workspaceId: string;
}) {
  return (
    <div className="space-y-6" data-testid="config-tab-engine-default-content">
      <SystemHealthSection workspaceId={props.workspaceId} />
      <ProfilesReadOnlySection profiles={props.config.profiles} />
      <RoutingRulesSection
        defaultProfile={props.config.routing.default}
        rules={props.config.routing.rules ?? []}
      />
      <AgentRoutingSection
        agentRouting={props.config.routing.agent_routing ?? {}}
        defaultProfile={props.config.routing.default}
      />
    </div>
  );
}

function WorkspaceOverrideTab(props: {
  config: WorkspaceConfigOut;
  workspaceId: string;
}) {
  const updateMutation = useUpdateWorkspaceConfigMutation(props.workspaceId);
  const routableAgentsQuery = useRoutableAgentsQuery(props.workspaceId);

  const [overrideProfiles, setOverrideProfiles] = useState<
    Record<string, WriterProfileIn>
  >({});
  const [overrideRouting, setOverrideRouting] = useState<RoutingConfigIn | null>(
    null,
  );
  const [editingProfileId, setEditingProfileId] = useState<string | null>(null);
  const [isCreating, setIsCreating] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  const overriddenIds = useMemo(
    () => new Set(Object.keys(overrideProfiles)),
    [overrideProfiles],
  );

  const mergedProfiles = useMemo<Record<string, WriterProfileOut>>(() => {
    const merged: Record<string, WriterProfileOut> = { ...props.config.profiles };
    for (const [id, profile] of Object.entries(overrideProfiles)) {
      merged[id] = profileInToOut(id, profile);
    }
    return merged;
  }, [props.config.profiles, overrideProfiles]);

  function handleEdit(profileId: string) {
    setIsCreating(false);
    setEditingProfileId(profileId);
  }

  function handleDelete(profileId: string) {
    setDeleteTarget(profileId);
  }

  function confirmDelete() {
    if (!deleteTarget) return;
    setOverrideProfiles((prev) => {
      const next = { ...prev };
      delete next[deleteTarget];
      return next;
    });
    if (editingProfileId === deleteTarget) {
      setEditingProfileId(null);
    }
    setDeleteTarget(null);
  }

  function handleCreateClick() {
    setEditingProfileId(null);
    setIsCreating(true);
  }

  function handleFormSubmit(submitted: WriterProfileFormSubmit) {
    setOverrideProfiles((prev) => ({
      ...prev,
      [submitted.profileId]: submitted.profile,
    }));
    setIsCreating(false);
    setEditingProfileId(null);
  }

  function handleFormCancel() {
    setIsCreating(false);
    setEditingProfileId(null);
  }

  const allProfileIdsForCreate = useMemo(() => {
    return new Set([
      ...Object.keys(props.config.profiles),
      ...Object.keys(overrideProfiles),
    ]);
  }, [props.config.profiles, overrideProfiles]);

  const editingInitialProfile = useMemo<WriterProfileIn | undefined>(() => {
    if (!editingProfileId) return undefined;
    const existing = overrideProfiles[editingProfileId];
    if (existing) return existing;
    const base = props.config.profiles[editingProfileId];
    if (base) return profileOutToIn(base);
    return undefined;
  }, [editingProfileId, overrideProfiles, props.config.profiles]);

  function handleSave() {
    const profilesCount = Object.keys(overrideProfiles).length;
    const rulesCount = overrideRouting?.rules?.length ?? 0;
    const body: WorkspaceConfigUpdateIn = {
      writer_profiles: profilesCount > 0 ? overrideProfiles : null,
      writer_routing: overrideRouting,
    };
    updateMutation.mutate(body, {
      onSuccess: () => {
        const parts: string[] = [];
        if (profilesCount > 0) parts.push(`${profilesCount} 个 profile`);
        if (overrideRouting !== null) {
          parts.push(`writer_routing 段(${rulesCount} 条规则)`);
        }
        showSuccessToast(
          "已保存工作区配置",
          `${parts.join(" + ")} 已写入 project.yaml`,
        );
        setOverrideProfiles({});
        setOverrideRouting(null);
        setEditingProfileId(null);
        setIsCreating(false);
      },
      onError: (err) => {
        showApiErrorToast(err);
      },
    });
  }

  const dirty =
    Object.keys(overrideProfiles).length > 0 || overrideRouting !== null;

  const availableProfileIds = useMemo(
    () =>
      Object.keys(mergedProfiles).sort((a, b) => a.localeCompare(b)),
    [mergedProfiles],
  );

  return (
    <div
      className="space-y-4"
      data-testid="config-tab-workspace-override-content"
    >
      <section className="space-y-2">
        <header className="flex items-center justify-between">
          <div>
            <h2 className="text-lg font-semibold">Writer Profiles 覆盖</h2>
            <p className="text-xs text-muted-foreground">
              在合并视图基础上叠加;保存时**整体替换** project.yaml 内
              `writer_profiles` 段。删除内置 profile 被 UI 禁用(yaml dict-level
              merge 是加法语义,无法真删引擎默认)。
            </p>
          </div>
          <Button
            type="button"
            size="sm"
            onClick={handleCreateClick}
            disabled={isCreating}
            data-testid="config-add-profile"
          >
            新增 profile
          </Button>
        </header>

        <WriterProfileList
          mergedProfiles={mergedProfiles}
          overriddenProfileIds={overriddenIds}
          onEdit={handleEdit}
          onDelete={handleDelete}
        />

        {isCreating && (
          <WriterProfileForm
            mode="create"
            existingProfileIds={allProfileIdsForCreate}
            onSubmit={handleFormSubmit}
            onCancel={handleFormCancel}
          />
        )}

        {editingProfileId && editingInitialProfile && (
          <WriterProfileForm
            mode="edit"
            initialProfileId={editingProfileId}
            initialProfile={editingInitialProfile}
            onSubmit={handleFormSubmit}
            onCancel={handleFormCancel}
          />
        )}
      </section>

      <RoutingRuleEditor
        value={overrideRouting}
        availableProfileIds={availableProfileIds}
        engineDefaultRouting={{
          default: props.config.routing.default,
          rules: props.config.routing.rules ?? [],
        }}
        onChange={setOverrideRouting}
        agents={routableAgentsQuery.data?.agents ?? []}
        profileMeta={routableAgentsQuery.data?.profile_meta ?? {}}
      />

      <CostGuardOverridePanel
        workspaceId={props.workspaceId}
        config={props.config.cost_guard}
      />

      <section
        className="rounded border border-dashed border-border p-4 text-xs text-muted-foreground"
        data-testid="config-tab-workspace-override-todo-placeholder"
      >
        Reviewer 阈值 / 检索 top_k 的编辑器仍待补齐；API key 管理已在系统设置页。
        成本守卫的当前工作区单日上限可在上方独立保存。
      </section>

      <div className="flex items-center gap-3 border-t border-border pt-3">
        <Button
          type="button"
          onClick={handleSave}
          disabled={!dirty || updateMutation.isPending}
          data-testid="config-save-override"
        >
          {updateMutation.isPending ? "保存中…" : "保存到工作区"}
        </Button>
        {!dirty && (
          <p className="text-xs text-muted-foreground">
            没有未保存的覆盖项。
          </p>
        )}
        {dirty && (
          <p
            className="text-xs text-amber-700"
            data-testid="config-dirty-indicator"
          >
            {(() => {
              const segs: string[] = [];
              if (Object.keys(overrideProfiles).length > 0) {
                segs.push(`${Object.keys(overrideProfiles).length} 个 profile 覆盖`);
              }
              if (overrideRouting !== null) {
                segs.push(
                  `writer_routing 段(${overrideRouting.rules?.length ?? 0} 条规则)`,
                );
              }
              return `待保存:${segs.join(" + ")}`;
            })()}
          </p>
        )}
      </div>

      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        title="从工作区覆盖层移除该 profile?"
        description={
          deleteTarget
            ? `将从工作区覆盖层中移除 "${deleteTarget}" 的覆盖配置(若该 ID 也是引擎默认,合并视图仍会显示引擎默认版本)。`
            : ""
        }
        confirmLabel="移除"
        onConfirm={confirmDelete}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}

function RuntimeTab(props: { workspaceId: string }) {
  return (
    <div
      className="space-y-3 rounded border border-dashed border-border p-6 text-sm"
      data-testid="config-tab-runtime-content"
    >
      <h2 className="text-lg font-semibold">临时运行时覆盖</h2>
      <p className="text-muted-foreground">
        单次运行 / 单章节级 profile 临时覆盖通过以下入口实施,不在 ConfigPage 内
        持久化:
      </p>
      <ul className="ml-5 list-disc space-y-1 text-muted-foreground">
        <li>
          {" "}
          <Link
            to={`/w/${props.workspaceId}/run`}
            className="underline"
            data-testid="config-tab-runtime-link-run"
          >
            运行控制页
          </Link>
          {" "}
          顶部 WriterProfileSelector — 覆盖本次 pipeline 启动的 profile,任务结束
          失效。
        </li>
        <li>
          ChapterCardReviewPage 章节卡内 `force_profile` 字段 — 章节级持久化覆盖
          (P6-B-09 落地,与 yaml 配置层互不影响)。
        </li>
      </ul>
      <p className="text-xs text-muted-foreground">
        P6-F-04 Part 1 不在本 tab 提供新编辑能力。Part 2(P6-F-05,合并轮 125)
        将考虑是否加入 "全局临时 override session" 入口。
      </p>
    </div>
  );
}

interface SystemHealthSectionProps {
  workspaceId: string | null;
}

function SystemHealthSection(props: SystemHealthSectionProps) {
  const query = useSystemHealthQuery(props.workspaceId);

  return (
    <section
      className="space-y-2"
      data-testid="config-system-health-section"
    >
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">系统健康检查</h2>
        <button
          type="button"
          onClick={() => query.refetch()}
          disabled={query.isFetching}
          data-testid="config-system-health-refresh"
          className="text-xs underline text-muted-foreground disabled:opacity-50"
        >
          {query.isFetching ? "检查中…" : "重新检查"}
        </button>
      </div>
      <p className="text-xs text-muted-foreground">
        {props.workspaceId
          ? `OBSERVABILITY §6 字面 10 项中本任务覆盖 11 项(6 全局 + 5 工作区);claude_api 留 v2_wishlist(维护人无 API key)。`
          : `当前未指定工作区,仅跑 6 项全局检查。`}
      </p>
      {query.isPending ? (
        <p className="text-sm text-muted-foreground">加载中…</p>
      ) : query.error ? (
        <p
          data-testid="config-system-health-error"
          className="text-sm text-destructive"
        >
          加载失败:{formatError(query.error)}
        </p>
      ) : query.data ? (
        <SystemHealthReport data={query.data} />
      ) : null}
    </section>
  );
}

function SystemHealthReport(props: {
  data: {
    status: string;
    engine_version: string;
    checks: HealthCheckItemOut[];
    summary?:
      | { pass_count: number; fail_count: number; skip_count: number }
      | null;
  };
}) {
  const { status, engine_version, checks, summary } = props.data;
  return (
    <div className="space-y-2" data-testid="config-system-health-report">
      <div className="flex items-center gap-3 text-sm">
        <span
          className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-medium ${statusBadgeClass(
            status,
          )}`}
          data-testid="config-system-health-status"
        >
          status: {status}
        </span>
        <span className="text-muted-foreground">
          engine_version: {engine_version}
        </span>
        {summary && (
          <span className="text-muted-foreground">
            pass {summary.pass_count} / fail {summary.fail_count} / skip{" "}
            {summary.skip_count}
          </span>
        )}
      </div>
      <ul
        className="space-y-1 rounded border border-border p-2"
        data-testid="config-system-health-list"
      >
        {checks.map((c) => (
          <li
            key={c.name}
            data-testid={`config-system-health-item-${c.name}`}
            className="flex items-center gap-2 text-xs"
          >
            <span
              className={`inline-flex w-12 items-center justify-center rounded px-1.5 py-0.5 font-medium ${outcomeBadgeClass(
                c.outcome,
              )}`}
            >
              {c.outcome}
            </span>
            <span className="font-mono">{c.name}</span>
            {c.detail && (
              <span className="text-muted-foreground">— {c.detail}</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function statusBadgeClass(status: string): string {
  if (status === "ok") return "bg-emerald-100 text-emerald-700";
  if (status === "degraded") return "bg-amber-100 text-amber-700";
  if (status === "down") return "bg-rose-100 text-rose-700";
  return "bg-muted text-muted-foreground";
}

function outcomeBadgeClass(outcome: string): string {
  if (outcome === "pass") return "bg-emerald-100 text-emerald-700";
  if (outcome === "fail") return "bg-rose-100 text-rose-700";
  if (outcome === "skip") return "bg-zinc-100 text-zinc-700";
  return "bg-muted text-muted-foreground";
}

interface ProfilesReadOnlySectionProps {
  profiles: Record<string, WriterProfileOut>;
}

function ProfilesReadOnlySection(props: ProfilesReadOnlySectionProps) {
  const entries = Object.entries(props.profiles).sort(([a], [b]) =>
    a.localeCompare(b),
  );
  return (
    <section className="space-y-2" data-testid="config-profiles-section">
      <h2 className="text-lg font-semibold">Writer Profiles(合并后视图)</h2>
      <p className="text-xs text-muted-foreground">
        共 {entries.length} 个 profile;`generation_params` /
        `extra_request_kwargs` 折叠展示原始 JSON。**只读**;新增/编辑在"工作区
        覆盖"tab 内操作。
      </p>
      <div className="overflow-hidden rounded border border-border">
        <table
          className="w-full text-sm"
          data-testid="config-profiles-table"
        >
          <thead className="bg-muted/40 text-left text-xs uppercase tracking-wide text-muted-foreground">
            <tr>
              <th className="px-4 py-2 font-medium">profile_id</th>
              <th className="px-4 py-2 font-medium">provider</th>
              <th className="px-4 py-2 font-medium">model</th>
              <th className="px-4 py-2 font-medium">ctx</th>
              <th className="px-4 py-2 font-medium">cost (currency in/out /1k)</th>
              <th className="px-4 py-2 font-medium">base_url</th>
              <th className="px-4 py-2 font-medium">notes</th>
            </tr>
          </thead>
          <tbody>
            {entries.map(([profileId, profile]) => (
              <ProfileRow
                key={profileId}
                profileId={profileId}
                profile={profile}
              />
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

interface ProfileRowProps {
  profileId: string;
  profile: WriterProfileOut;
}

function ProfileRow(props: ProfileRowProps) {
  const { profileId, profile } = props;
  const hasExtra =
    Object.keys(profile.generation_params ?? {}).length > 0 ||
    Object.keys(profile.extra_request_kwargs ?? {}).length > 0;
  return (
    <>
      <tr
        data-testid="config-profile-row"
        data-profile-id={profileId}
        className="border-t border-border"
      >
        <td className="px-4 py-2 font-mono text-xs">{profileId}</td>
        <td className="px-4 py-2">{profile.provider}</td>
        <td className="px-4 py-2 font-mono text-xs">{profile.model}</td>
        <td className="px-4 py-2 text-muted-foreground">
          {profile.context_window?.toLocaleString() ?? "—"}
        </td>
        <td className="px-4 py-2 text-muted-foreground">
          {profile.cost_currency ?? "USD"}{" "}
          {(profile.cost_per_1k_input ?? 0).toFixed(4)} /{" "}
          {(profile.cost_per_1k_output ?? 0).toFixed(4)}
        </td>
        <td className="px-4 py-2 font-mono text-xs text-muted-foreground">
          {profile.base_url ?? "—"}
        </td>
        <td className="px-4 py-2 text-muted-foreground">
          {profile.notes || "—"}
        </td>
      </tr>
      {hasExtra && (
        <tr
          data-testid="config-profile-extra-row"
          data-profile-id={profileId}
          className="bg-muted/20"
        >
          <td colSpan={7} className="px-4 py-2 text-xs">
            <details>
              <summary className="cursor-pointer text-muted-foreground">
                generation_params / extra_request_kwargs
              </summary>
              <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-words rounded bg-background p-3 text-xs">
                {JSON.stringify(
                  {
                    generation_params: profile.generation_params ?? {},
                    extra_request_kwargs: profile.extra_request_kwargs ?? {},
                  },
                  null,
                  2,
                )}
              </pre>
            </details>
          </td>
        </tr>
      )}
    </>
  );
}

interface RoutingRulesSectionProps {
  defaultProfile: string;
  rules: readonly RoutingRuleOut[];
}

function RoutingRulesSection(props: RoutingRulesSectionProps) {
  return (
    <section className="space-y-2" data-testid="config-routing-rules-section">
      <h2 className="text-lg font-semibold">Writer Routing</h2>
      <p className="text-sm text-muted-foreground">
        默认 profile:
        <code
          className="mx-1 rounded bg-muted px-1.5 py-0.5 font-mono text-xs"
          data-testid="config-routing-default"
        >
          {props.defaultProfile}
        </code>
        (Writer 路由规则全部不命中时走默认)
      </p>
      {props.rules.length === 0 ? (
        <div
          data-testid="config-routing-rules-empty"
          className="rounded border border-dashed border-border p-4 text-sm text-muted-foreground"
        >
          未配置任何 Writer 路由规则,Writer 始终走默认 profile。
        </div>
      ) : (
        <div className="overflow-hidden rounded border border-border">
          <table
            className="w-full text-sm"
            data-testid="config-routing-rules-table"
          >
            <thead className="bg-muted/40 text-left text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="px-4 py-2 font-medium">#</th>
                <th className="px-4 py-2 font-medium">condition</th>
                <th className="px-4 py-2 font-medium">profile</th>
              </tr>
            </thead>
            <tbody>
              {props.rules.map((rule, idx) => (
                <tr
                  key={`${rule.condition}-${idx}`}
                  data-testid="config-routing-rule-row"
                  className="border-t border-border"
                >
                  <td className="px-4 py-2 text-muted-foreground">{idx + 1}</td>
                  <td className="px-4 py-2 font-mono text-xs">
                    {rule.condition}
                  </td>
                  <td className="px-4 py-2 font-mono text-xs">
                    {rule.profile}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

interface AgentRoutingSectionProps {
  agentRouting: Record<string, string>;
  defaultProfile: string;
}

function AgentRoutingSection(props: AgentRoutingSectionProps) {
  const entries = Object.entries(props.agentRouting).sort(([a], [b]) =>
    a.localeCompare(b),
  );
  return (
    <section className="space-y-2" data-testid="config-agent-routing-section">
      <h2 className="text-lg font-semibold">Agent Routing</h2>
      <p className="text-sm text-muted-foreground">
        其他 agent 的 profile 绑定(P3-plan 决策 1 选 B);未列出的 agent 走默认
        <code className="mx-1 rounded bg-muted px-1.5 py-0.5 font-mono text-xs">
          {props.defaultProfile}
        </code>
        。
      </p>
      {entries.length === 0 ? (
        <div
          data-testid="config-agent-routing-empty"
          className="rounded border border-dashed border-border p-4 text-sm text-muted-foreground"
        >
          未配置 agent_routing,所有 agent 都走默认 profile。
        </div>
      ) : (
        <div className="overflow-hidden rounded border border-border">
          <table
            className="w-full text-sm"
            data-testid="config-agent-routing-table"
          >
            <thead className="bg-muted/40 text-left text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="px-4 py-2 font-medium">agent</th>
                <th className="px-4 py-2 font-medium">profile</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(([agent, profile]) => (
                <tr
                  key={agent}
                  data-testid="config-agent-routing-row"
                  data-agent={agent}
                  className="border-t border-border"
                >
                  <td className="px-4 py-2 font-mono text-xs">{agent}</td>
                  <td className="px-4 py-2 font-mono text-xs">{profile}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function profileInToOut(
  profileId: string,
  p: WriterProfileIn,
): WriterProfileOut {
  return {
    profile_id: profileId,
    provider: p.provider,
    model: p.model,
    base_url: p.base_url ?? null,
    context_window: p.context_window,
    generation_params: p.generation_params,
    extra_request_kwargs: p.extra_request_kwargs,
    cost_per_1k_input: p.cost_per_1k_input,
    cost_per_1k_output: p.cost_per_1k_output,
    cost_currency: p.cost_currency,
    notes: p.notes,
  };
}

function profileOutToIn(p: WriterProfileOut): WriterProfileIn {
  // provider 在 Out 是 string,在 In 是受限的 4 档 Literal;assert 收窄。
  const allowed = ["claude", "ollama", "openai_compatible", "mock"] as const;
  const safeProvider = (allowed as readonly string[]).includes(p.provider)
    ? (p.provider as (typeof allowed)[number])
    : "claude";
  return {
    provider: safeProvider,
    model: p.model,
    base_url: p.base_url ?? null,
    context_window: p.context_window,
    generation_params: p.generation_params ?? {},
    extra_request_kwargs: p.extra_request_kwargs ?? {},
    cost_per_1k_input: p.cost_per_1k_input ?? 0,
    cost_per_1k_output: p.cost_per_1k_output ?? 0,
    cost_currency: p.cost_currency ?? "USD",
    notes: p.notes ?? "",
  };
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
