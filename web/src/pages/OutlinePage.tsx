/**
 * OutlinePage(P5-F-05 + v1.0.x wave 6 s1+s4 合并加三档状态分支)。
 *
 * 路由:`/w/:workspaceId/outline`(对齐 FRONTEND_SPEC §4.3)。
 *
 * **wave 6 s1+s4 合并改造**(2026-05-24,真根因转向后):本页加 outline_status
 * 三档状态分支 —— 对应 NewBookPage 瘦身后(只创建工作区不立即 generate)用户
 * 在本页推进大纲流程的不同 stage:
 *
 * - `outline_status == not_started`:render `OutlineWorkflowPanel`
 *   (autoStart=navState.autoStart),formValues 来自 useLocation state(NewBookPage
 *   navigate 传过来)。autoStart=true 时立即触发 outline:generate;false 时显示
 *   "生成大纲"按钮 + project.yaml 引导(用户可先写 yaml 切云端再点)
 * - `outline_status == generating`:render `OutlineWorkflowPanel`(internal
 *   state machine 接管 stage progress + 完成后切 ready 5 tab + commit)。刷新丢失
 *   navigate state 后,单段重生参数可由工作区详情 + 一致的当前卷规划安全恢复
 * - `outline_status == committed`:render `OutlineCommittedView`(原 P5-F-05
 *   既有 6 tab 编辑器 — 预览 / 编辑 / 保存,行为不变)
 *
 * 真根因转向 + 合并 s4 详 `docs/impl/v1_0_hotfix_wave6_s1_newbookpage_timing
 * _outline_bridge.md`(本会话 impl 文档)。
 *
 * P5-F-05 既有(committed 分支保留):
 *   1. 顶部:工作区链接 + 标题 + 历史版本下拉(P5 简版占位)
 *   2. tab 切换条:world / characters / plot / volume_plans / mini_plan /
 *      chapter_summary 6 tab
 *   3. 每 tab:预览 / 编辑双态切换
 *   4. 底部:保存按钮 + 撤销按钮 + dirty 提示
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";

import { isApiError } from "@/api/errors";
import {
  useOutlineQuery,
  useUpdateOutlineMutation,
  type CharactersOutput,
  type MiniPlanOutput,
  type OutlineOut,
  type OutlineSection,
  type OutlineUpdateIn,
  type PlotOutput,
  type VolumePlansOutput,
  type WorldOutput,
} from "@/api/outline";
import { useWorkspaceDetailQuery } from "@/api/workspaces";
import { ChapterSummaryTabContent } from "@/components/ChapterSummaryTabContent";
import { MiniPlanTabContent } from "@/components/MiniPlanTabContent";
import { OutlineHistoryDropdown } from "@/components/OutlineHistoryDropdown";
import { OutlineSectionEditor } from "@/components/OutlineSectionEditor";
import { OutlineSectionView } from "@/components/OutlineSectionView";
import { OutlineDiagnosticPanel } from "@/components/OutlineDiagnosticPanel";
import { OutlineWorkflowPanel } from "@/components/OutlineWorkflowPanel";
import { AuthoringPanel } from "@/components/AuthoringPanel";
import { useAuthoringQuery } from "@/api/authoring";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { useUnsavedChanges } from "@/hooks/useUnsavedChanges";
import { cn } from "@/lib/utils";
import { useActiveWorkspaceStore } from "@/store/activeWorkspace";
import type { OutlineNavigateState } from "@/pages/NewBookPage";
import { POST_V8_ABC_SELECTION_UI } from "@/selection/postV8AbcSelection";
import { useNavigate } from "react-router-dom";

/**
 * v1.0.x wave 4 session 5:OutlinePage tab value 扩到 6 档。
 *
 * - 4 段 outline(world / characters / plot / volume_plans):走 outline.dirty
 *   计算 + PUT /outline patch
 * - `mini_plan`:第 5 段(wave 4 session 1),走 outline.mini_plan + PUT /outline
 * - `chapter_summary`:第 6 段(wave 4 session 5),独立资源,走 chapter-summaries
 *   API(GET 列表 + PUT 单条),不进 outline.dirty 计算
 */
type OutlineTabValue = OutlineSection | "mini_plan" | "chapter_summary";

const TAB_LABELS: ReadonlyArray<{ value: OutlineTabValue; label: string }> = [
  { value: "world", label: "世界观" },
  { value: "characters", label: "核心人设" },
  { value: "plot", label: "主线" },
  { value: "volume_plans", label: "卷规划" },
  { value: "mini_plan", label: "章级事件" },
  { value: "chapter_summary", label: "章节摘要" },
];

const EMPTY_OUTLINE: OutlineOut = {
  world: null,
  characters: null,
  plot: null,
  volume_plans: null,
  mini_plan: null,
};

export default function OutlinePage() {
  const { workspaceId } = useParams<{ workspaceId: string }>();

  if (!workspaceId) {
    return (
      <div className="rounded border border-destructive bg-destructive/10 p-6 text-sm text-destructive">
        URL 缺少 workspaceId 参数。
        <Link to="/workspaces" className="ml-2 underline">
          回到工作区列表
        </Link>
      </div>
    );
  }

  return <><OutlinePageInner workspaceId={workspaceId} /><OutlineDiagnosticPanel key={workspaceId} workspaceId={workspaceId} /></>;
}

/**
 * v1.0.x wave 6 s1+s4 合并:OutlinePage 三档状态分支派发器。
 *
 * 根据 `WorkspaceOut.outline_status`(GET /workspaces/{id} 响应字段,wave 6
 * s1+s4 起新增)派发不同视图:
 * - `not_started` / `generating` → `OutlineWorkflowPanel`(共享子组件,
 *   含"生成大纲"按钮 + 进度 + 4 tab + commit)
 * - `committed` → `OutlineCommittedView`(原 P5-F-05 6 tab 编辑器,行为不变)
 *
 * formValues / autoStart 通过 NewBookPage navigate state 透传(useLocation
 * 拿)。无 state(用户直接打开 URL / 刷新页面)时 formValues=null + autoStart
 * =false;not_started 仍显示安全引导,generating 的 ready checkpoint 则把工作区
 * premise/genre 交给 OutlineWorkflowPanel,再与当前卷规划共同恢复重生参数。
 */
function OutlinePageInner({ workspaceId }: { workspaceId: string }) {
  const navigate = useNavigate();
  const location = useLocation();
  const setActiveWorkspaceId = useActiveWorkspaceStore(
    (s) => s.setActiveWorkspaceId,
  );

  // v1.0.x B2-02:进入大纲页时同步 zustand,让 AppShell 顶部 nav 拼出正确的
  // /w/:workspaceId/* href(对齐 RunControlPage / ExportPage 模式)。
  useEffect(() => {
    setActiveWorkspaceId(workspaceId);
  }, [workspaceId, setActiveWorkspaceId]);

  // wave 6 s1+s4:拉工作区 detail 获取 outline_status(3 档:not_started /
  // generating / committed)。
  const workspaceQuery = useWorkspaceDetailQuery(workspaceId);
  const authoringEnabled = workspaceQuery.data?.authoring_enabled === true;
  const authoringQuery = useAuthoringQuery(workspaceId, authoringEnabled);
  const draft = authoringQuery.data?.draft;
  const outlineStatus = workspaceQuery.data?.outline_status;

  // NewBookPage navigate state(formValues / assetsResult / autoStart)。
  // 无 state 时 navState 为 null —— OutlineWorkflowPanel idle 状态显示引导。
  const navState = location.state as OutlineNavigateState | null;
  // POST-V8 ABC 的 New Book 输入本来就是编译期冻结合同。刷新会丢失
  // react-router location.state；仅当 workspace id、premise、genre 三者仍与
  // 当前 exact-candidate 合同逐字一致时，才允许从该合同恢复表单参数。普通工作区
  // 或任一身份/输入漂移继续 fail closed，绝不猜测卷数、章数或张力模板。
  const selectionRecoveryValues =
    POST_V8_ABC_SELECTION_UI &&
    workspaceId === POST_V8_ABC_SELECTION_UI.values.workspace_id &&
    workspaceQuery.data?.premise === POST_V8_ABC_SELECTION_UI.values.premise &&
    workspaceQuery.data?.genre === POST_V8_ABC_SELECTION_UI.values.genre
      ? POST_V8_ABC_SELECTION_UI.values
      : null;
  const recoveryTaskId = useMemo(
    () => new URLSearchParams(location.search).get("task_id"),
    [location.search],
  );
  const persistOutlineTaskId = useCallback(
    (taskId: string) => {
      const params = new URLSearchParams(location.search);
      params.set("task_id", taskId);
      navigate(
        {
          pathname: location.pathname,
          search: `?${params.toString()}`,
        },
        { replace: true, state: location.state },
      );
    },
    [location.pathname, location.search, location.state, navigate],
  );

  if (workspaceQuery.isPending || (authoringEnabled && authoringQuery.isPending)) {
    return (
      <div className="rounded border border-border p-6 text-sm text-muted-foreground">
        加载工作区元数据…
      </div>
    );
  }

  if (workspaceQuery.isError || authoringQuery.isError) {
    const message = authoringQuery.isError ? "小说规划读取失败，请刷新；不会退回旧生成流程。" : isApiError(workspaceQuery.error)
      ? `${workspaceQuery.error.code}: ${workspaceQuery.error.message}`
      : workspaceQuery.error instanceof Error
        ? workspaceQuery.error.message
        : "未知错误";
    return (
      <div className="rounded border border-destructive bg-destructive/10 p-3 text-sm text-destructive">
        加载工作区元数据失败:{message}
      </div>
    );
  }

  // not_started / generating 两档 → OutlineWorkflowPanel 接管
  if (outlineStatus === "not_started" || outlineStatus === "generating") {
    return (
      <div className="space-y-4">
        <header className="space-y-2">
          <p className="text-xs text-muted-foreground">
            <Link to={`/workspaces/${workspaceId}`} className="underline">
              ← 工作区
            </Link>
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-2xl font-semibold">大纲</h1>
            <span className="text-xs text-muted-foreground">
              工作区 <code>{workspaceId}</code> · 当前 outline_status:
              <code className="ml-1">{outlineStatus}</code>
            </span>
          </div>
        </header>
        {draft && authoringQuery.data && <AuthoringPanel key={draft.revision + draft.status} workspaceId={workspaceId} view={authoringQuery.data} />}
        {(!draft || draft.status === "confirmed") && <OutlineWorkflowPanel
          workspaceId={workspaceId}
          formValues={draft ? { workspace_id: workspaceId, display_name: draft.proposal?.title ?? "新故事", premise: draft.premise, genre: draft.genre,
            total_volumes: draft.preferences!.total_volumes, chapters_per_volume: draft.preferences!.chapters_per_volume,
            planning_mode: "whole_book", tension_template: draft.preferences!.tension_template, default_voice: draft.preferences!.voice, style_template: workspaceQuery.data?.style_template ?? "" } : navState?.formValues ?? selectionRecoveryValues}
          persistedWorkspaceInputs={
            workspaceQuery.data
              ? {
                  premise: workspaceQuery.data.premise,
                  genre: workspaceQuery.data.genre,
                }
              : null
          }
          initialAssetsResult={navState?.assetsResult ?? null}
          autoStart={
            !draft && outlineStatus === "not_started"
              ? Boolean(navState?.autoStart)
              : false
          }
          recoveryTaskId={recoveryTaskId}
          onTaskAccepted={persistOutlineTaskId}
          onViewWorkspace={(wid) => {
            navigate(`/workspaces/${encodeURIComponent(wid)}`);
          }}
          onAbandon={() => {
            navigate("/workspaces/new");
          }}
        />}
      </div>
    );
  }

  // committed 档 → 既有 6 tab 编辑器(行为不变)
  return <OutlineCommittedView workspaceId={workspaceId} />;
}

/**
 * 原 P5-F-05 OutlinePage 6 tab 编辑器(committed 状态下的视图)。
 *
 * wave 6 s1+s4 合并改造时从 OutlinePageInner 抽出,行为完全不变 —— 仅作为
 * "outline_status == committed" 分支的渲染目标。
 */
function OutlineCommittedView({ workspaceId }: { workspaceId: string }) {
  const outlineQuery = useOutlineQuery(workspaceId);
  const updateMutation = useUpdateOutlineMutation();

  const server = outlineQuery.data;

  const [draft, setDraft] = useState<OutlineOut>(EMPTY_OUTLINE);
  const [activeTab, setActiveTab] = useState<OutlineTabValue>("world");
  // mini_plan 走 outline patch 路径,与 4 段同 dirty 计算;chapter_summary 是
  // 独立资源(走 chapter-summaries API),不进 editMode 字典(各卡自管编辑态)
  const [editMode, setEditMode] = useState<
    Record<OutlineSection | "mini_plan", boolean>
  >({
    world: false,
    characters: false,
    plot: false,
    volume_plans: false,
    mini_plan: false,
  });

  useEffect(() => {
    if (server) {
      setDraft({
        world: server.world ?? null,
        characters: server.characters ?? null,
        plot: server.plot ?? null,
        volume_plans: server.volume_plans ?? null,
        mini_plan: server.mini_plan ?? null,
      });
    }
  }, [server]);

  const dirtyBySection = useMemo(
    () => computeDirtyBySection(server, draft),
    [server, draft],
  );
  const isDirty =
    dirtyBySection.world ||
    dirtyBySection.characters ||
    dirtyBySection.plot ||
    dirtyBySection.volume_plans ||
    dirtyBySection.mini_plan;
  useUnsavedChanges(isDirty);

  const isBusy = updateMutation.isPending || outlineQuery.isFetching;

  const handleSave = () => {
    if (!isDirty) return;
    const body: OutlineUpdateIn = {};
    if (dirtyBySection.world) body.world = draft.world ?? null;
    if (dirtyBySection.characters) body.characters = draft.characters ?? null;
    if (dirtyBySection.plot) body.plot = draft.plot ?? null;
    if (dirtyBySection.volume_plans) {
      body.volume_plans = draft.volume_plans ?? null;
    }
    if (dirtyBySection.mini_plan) body.mini_plan = draft.mini_plan ?? null;
    updateMutation.mutate(
      { workspaceId, body },
      {
        onSuccess: () => {
          showSuccessToast("大纲已保存");
        },
        onError: (err) => showApiErrorToast(err),
      },
    );
  };

  const handleRevert = () => {
    if (!server) return;
    setDraft({
      world: server.world ?? null,
      characters: server.characters ?? null,
      plot: server.plot ?? null,
      volume_plans: server.volume_plans ?? null,
      mini_plan: server.mini_plan ?? null,
    });
  };

  const toggleEdit = (section: OutlineSection | "mini_plan") =>
    setEditMode((prev) => ({ ...prev, [section]: !prev[section] }));

  if (outlineQuery.isPending) {
    return (
      <div className="rounded border border-border p-6 text-sm text-muted-foreground">
        加载大纲…
      </div>
    );
  }

  if (outlineQuery.error) {
    return <OutlineLoadError error={outlineQuery.error} />;
  }

  const onChangeWorld = (next: WorldOutput) =>
    setDraft((d) => ({ ...d, world: next }));
  const onChangeCharacters = (next: CharactersOutput) =>
    setDraft((d) => ({ ...d, characters: next }));
  const onChangePlot = (next: PlotOutput) =>
    setDraft((d) => ({ ...d, plot: next }));
  const onChangeVolumePlans = (next: VolumePlansOutput) =>
    setDraft((d) => ({ ...d, volume_plans: next }));
  const onChangeMiniPlan = (next: MiniPlanOutput) =>
    setDraft((d) => ({ ...d, mini_plan: next }));

  return (
    <div className="space-y-4">
      <header className="space-y-2">
        <p className="text-xs text-muted-foreground">
          <Link to={`/workspaces/${workspaceId}`} className="underline">
            ← 工作区
          </Link>
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold">大纲</h1>
          <span className="text-xs text-muted-foreground">
            工作区 <code>{workspaceId}</code> · 4 段可分别预览 / 编辑
          </span>
          <div className="ml-auto">
            <OutlineHistoryDropdown items={[]} disabled={isBusy} />
          </div>
        </div>
      </header>

      <nav
        className="flex flex-wrap items-center gap-1 border-b border-border"
        data-testid="outline-tabs"
        aria-label="大纲段切换"
        role="tablist"
      >
        {TAB_LABELS.map((t) => {
          const isActive = t.value === activeTab;
          // chapter_summary 是独立资源(各卡自管 PUT),不进 outline dirty 指示;
          // 其余 5 段(world / characters / plot / volume_plans / mini_plan)走
          // dirty 计算给 tab 标 ● 标记
          const tabDirty =
            t.value === "chapter_summary"
              ? false
              : dirtyBySection[t.value];
          return (
            <button
              key={t.value}
              type="button"
              role="tab"
              aria-selected={isActive}
              onClick={() => setActiveTab(t.value)}
              className={cn(
                "rounded-t border border-b-0 px-3 py-1.5 text-sm",
                isActive
                  ? "border-border bg-background font-medium"
                  : "border-transparent text-muted-foreground hover:text-foreground",
              )}
              data-testid={`outline-tab-${t.value}`}
            >
              {t.label}
              {tabDirty && (
                <span
                  aria-label="未保存"
                  className="ml-1 text-amber-600"
                  data-testid={`outline-tab-${t.value}-dirty`}
                >
                  ●
                </span>
              )}
            </button>
          );
        })}
      </nav>

      <SectionPane
        workspaceId={workspaceId}
        section={activeTab}
        editing={
          activeTab === "chapter_summary"
            ? false
            : editMode[activeTab]
        }
        onToggleEdit={
          activeTab === "chapter_summary"
            ? undefined
            : () => toggleEdit(activeTab)
        }
        draft={draft}
        onChangeWorld={onChangeWorld}
        onChangeCharacters={onChangeCharacters}
        onChangePlot={onChangePlot}
        onChangeVolumePlans={onChangeVolumePlans}
        onChangeMiniPlan={onChangeMiniPlan}
      />

      <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
        <Button
          onClick={handleSave}
          disabled={!isDirty || isBusy}
          data-testid="save-outline-button"
        >
          {updateMutation.isPending ? "保存中…" : "保存"}
        </Button>
        <Button
          variant="outline"
          onClick={handleRevert}
          disabled={!isDirty || isBusy}
          data-testid="revert-outline-button"
        >
          撤销修改
        </Button>
        {isDirty && (
          <span className="text-xs text-amber-700" data-testid="outline-dirty-hint">
            未保存的修改({sumDirty(dirtyBySection)} 段)
          </span>
        )}
      </div>
    </div>
  );
}

interface SectionPaneProps {
  /** P6-F-06:候选 4 摊入 — 透传给 VolumePlansEditor 让卷卡片可链 VolumePlanPage。 */
  workspaceId: string;
  section: OutlineTabValue;
  editing: boolean;
  /** chapter_summary tab 各卡自管编辑态,本 prop 不需要 → undefined。 */
  onToggleEdit?: () => void;
  draft: OutlineOut;
  onChangeWorld: (next: WorldOutput) => void;
  onChangeCharacters: (next: CharactersOutput) => void;
  onChangePlot: (next: PlotOutput) => void;
  onChangeVolumePlans: (next: VolumePlansOutput) => void;
  onChangeMiniPlan: (next: MiniPlanOutput) => void;
}

function SectionPane(props: SectionPaneProps) {
  const previewOutline: OutlineOut = props.draft;

  // chapter_summary tab 独立资源,各卡自管编辑态,不走 outline draft patch 流程
  if (props.section === "chapter_summary") {
    return (
      <section
        className="rounded border border-border p-3"
        data-testid="outline-pane-chapter_summary"
        role="tabpanel"
      >
        <ChapterSummaryTabContent workspaceId={props.workspaceId} />
      </section>
    );
  }

  // mini_plan tab 走 outline draft patch 流程(与 4 段同 dirty 计算 + PUT /outline)
  if (props.section === "mini_plan") {
    return (
      <section
        className="rounded border border-border p-3"
        data-testid="outline-pane-mini_plan"
        role="tabpanel"
      >
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs text-muted-foreground">
            {props.editing
              ? "编辑态:修改 one_line_event;保存后整段 patch + invalidate 章节卡 cache。"
              : "预览态:只读章级事件表格。"}
          </p>
          {props.onToggleEdit && (
            <Button
              size="sm"
              variant={props.editing ? "secondary" : "outline"}
              onClick={props.onToggleEdit}
              data-testid="outline-edit-toggle-mini_plan"
            >
              {props.editing ? "← 回到预览" : "编辑此段"}
            </Button>
          )}
        </div>
        <MiniPlanTabContent
          miniPlan={props.draft.mini_plan ?? null}
          editing={props.editing}
          onChange={props.onChangeMiniPlan}
        />
      </section>
    );
  }

  // 4 段 outline:复用 OutlineSectionView / OutlineSectionEditor
  return (
    <section
      className="rounded border border-border p-3"
      data-testid={`outline-pane-${props.section}`}
      role="tabpanel"
    >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          {props.editing
            ? "编辑态:字段直接修改,保存后整段 patch 提交;切回预览不丢草稿。"
            : "预览态:只读结构化展示(复用 OutlineSectionView)。"}
        </p>
        {props.onToggleEdit && (
          <Button
            size="sm"
            variant={props.editing ? "secondary" : "outline"}
            onClick={props.onToggleEdit}
            data-testid={`outline-edit-toggle-${props.section}`}
          >
            {props.editing ? "← 回到预览" : "编辑此段"}
          </Button>
        )}
      </div>

      {props.editing ? (
        <OutlineSectionEditor
          workspaceId={props.workspaceId}
          section={props.section}
          world={props.draft.world ?? null}
          characters={props.draft.characters ?? null}
          plot={props.draft.plot ?? null}
          volumePlans={props.draft.volume_plans ?? null}
          onChangeWorld={props.onChangeWorld}
          onChangeCharacters={props.onChangeCharacters}
          onChangePlot={props.onChangePlot}
          onChangeVolumePlans={props.onChangeVolumePlans}
        />
      ) : (
        <OutlineSectionView outline={previewOutline} section={props.section} />
      )}
    </section>
  );
}

function OutlineLoadError({ error }: { error: unknown }) {
  const message = isApiError(error)
    ? `${error.code}: ${error.message}`
    : error instanceof Error
      ? error.message
      : "未知错误";
  return (
    <div className="rounded border border-destructive bg-destructive/10 p-3 text-sm text-destructive">
      加载大纲失败:{message}
    </div>
  );
}

// ---- dirty helpers ----

/**
 * v1.0.x wave 4 session 5:dirty 计算扩到 5 段(加 mini_plan)。chapter_summary
 * 是独立资源,不进 outline dirty 计算(各卡自管 PUT)。
 */
type DirtyBySection = Record<OutlineSection | "mini_plan", boolean>;

function computeDirtyBySection(
  server: OutlineOut | undefined,
  draft: OutlineOut,
): DirtyBySection {
  if (!server) {
    return {
      world: false,
      characters: false,
      plot: false,
      volume_plans: false,
      mini_plan: false,
    };
  }
  return {
    world: !sectionEqual(server.world, draft.world),
    characters: !sectionEqual(server.characters, draft.characters),
    plot: !sectionEqual(server.plot, draft.plot),
    volume_plans: !sectionEqual(server.volume_plans, draft.volume_plans),
    mini_plan: !sectionEqual(server.mini_plan, draft.mini_plan),
  };
}

function sectionEqual(a: unknown, b: unknown): boolean {
  // null / undefined 视为相等(后端 GET 段可返 null);其余 JSON 序列化比对。
  if (a == null && b == null) return true;
  if (a == null || b == null) return false;
  return JSON.stringify(a) === JSON.stringify(b);
}

function sumDirty(d: DirtyBySection): number {
  return (
    Number(d.world) +
    Number(d.characters) +
    Number(d.plot) +
    Number(d.volume_plans) +
    Number(d.mini_plan)
  );
}
