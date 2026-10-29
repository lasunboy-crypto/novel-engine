/**
 * NewBookPage(v1.0.x wave 6 s1+s4 合并后瘦身版)。
 *
 * 路由 `/workspaces/new`(对齐 FRONTEND_SPEC §4.2)。左右双列骨架:
 *
 * - **左侧**:`NewBookForm` 表单(workspace_id / display_name / genre /
 *   premise / total_volumes / chapters_per_volume / tension_template +
 *   world.md / characters.md 两个 assets import 槽)
 * - **右侧**:仅显示"创建工作区"进度态(creating / uploading_assets / done)
 *   + 跳转 OutlinePage 引导(本会话 wave 6 s1+s4 合并后,outline:generate 触发
 *   迁移到 OutlinePage —— 解决用户无机会在 project.yaml 就位前阻止 generate 的
 *   时序问题,详 wave 6 s1 真根因转向记录)
 *
 * 瘦身后提交链路(POST 二连 + 跳转):
 *
 * 1. `POST /workspaces` 创建工作区(`total_chapters` = total_volumes ×
 *    chapters_per_volume,`activate=true`)
 * 2. (任一资产 markdown 非空时)`POST /workspaces/{id}/assets:import` 上传
 *    `world.md / characters.md` 内容
 * 3. **navigate('/w/{id}/outline', {state: {formValues, assetsResult,
 *    autoStart: true}})** —— 用户在 OutlinePage 看到工作流面板(autoStart=true
 *    时立即触发 outline:generate;autoStart=false 时显示"生成大纲"按钮等用户
 *    主动点 + 写 project.yaml 引导)
 *
 * **wave 6 s1+s4 真根因转向**(2026-05-24):
 * - 原 NewBookPage 在创建工作区后**立即触发** outline:generate(三步链路一气
 *   呵成)→ 用户无机会在创建 ↔ generate 之间手写 project.yaml → 第一次整段
 *   大纲必走 engine default(local_qwen_32k 本地);云端切换路径只能逐段
 *   :regenerate-section(因 yaml 已就位)
 * - wave 6 s1 plan-kickoff 误诊为"outline:generate 端点未接 router",grep 验真
 *   两端点 router 路径完全对称(详 docs/impl/v1_0_hotfix_wave6_s1_newbookpage
 *   _timing_outline_bridge.md §"真根因转向记录" 段)
 * - X1 方案:NewBookPage 瘦身只创建 + 跳 OutlinePage,OutlinePage 加三档状态
 *   分支(未生成 / 已生成未 commit / 已 commit)各对应不同 workflow stage
 * - 合并 s4(原 F5 NewBookPage / OutlinePage 桥接):同根 — OutlinePage 加三档
 *   分支天然解决"用户离开 NewBookPage 后中间态恢复"问题
 *
 * outline 工作流 UI 整段迁出本页 → `OutlineWorkflowPanel`(共享子组件 + OutlinePage 复用)
 */

import { useState } from "react";
import { CreativeBookForm } from "@/components/CreativeBookForm";
import { saveAuthoring } from "@/api/authoring";
import { useNavigate } from "react-router-dom";

import {
  NewBookForm,
  type NewBookFormSubmitPayload,
} from "@/components/NewBookForm";

type NewBookFormValues = NewBookFormSubmitPayload["values"];
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import { isApiError } from "@/api/errors";
import {
  useImportAssetsMutation,
  type AssetsImportOut,
} from "@/api/assets";
import { useCreateWorkspaceMutation } from "@/api/workspaces";
import { cn } from "@/lib/utils";
import { POST_V8_ABC_SELECTION_UI } from "@/selection/postV8AbcSelection";

type CreationStage = "creating" | "uploading_assets" | "done";

const STAGE_LABELS: Record<CreationStage, string> = {
  creating: "创建工作区",
  uploading_assets: "上传资产",
  done: "创建完成,跳转大纲页",
};

type SubmissionState =
  | { kind: "idle" }
  | { kind: "creating"; stage: CreationStage; workspaceId: string }
  | { kind: "error"; message: string; workspaceId: string | null };

export default function NewBookPage() {
  const navigate = useNavigate();
  const [advanced, setAdvanced] = useState(Boolean(POST_V8_ABC_SELECTION_UI));
  const [submission, setSubmission] = useState<SubmissionState>({
    kind: "idle",
  });

  const createMutation = useCreateWorkspaceMutation();
  const importAssetsMutation = useImportAssetsMutation();

  const handleSubmit = async (payload: NewBookFormSubmitPayload) => {
    const { values, world_md_content, characters_md_content } = payload;
    const totalChapters = values.total_volumes * values.chapters_per_volume;

    setSubmission({
      kind: "creating",
      stage: "creating",
      workspaceId: values.workspace_id,
    });

    let workspaceCreated = false;
    let assetsResult: AssetsImportOut | null = null;
    try {
      if (
        POST_V8_ABC_SELECTION_UI &&
        (world_md_content !== null || characters_md_content !== null)
      ) {
        throw new Error("选型活动禁止导入任何 workspace 资产");
      }
      // ① 创建工作区
      await createMutation.mutateAsync({
        workspace_id: values.workspace_id,
        display_name: values.display_name,
        genre: values.genre,
        premise: values.premise,
        total_chapters: totalChapters,
        default_voice: values.default_voice,
        // I1:"" 哨兵(不使用模板)→ null;7 档枚举原样透传。
        style_template: values.style_template || null,
        activate: true,
      });
      workspaceCreated = true;
      if (!advanced) {
        await saveAuthoring(values.workspace_id, { expected_revision: 0, premise: values.premise, genre: values.genre });
      }

      // ② 上传资产(任一非空时)
      const hasAssetUpload = Boolean(world_md_content || characters_md_content);
      if (hasAssetUpload) {
        setSubmission({
          kind: "creating",
          stage: "uploading_assets",
          workspaceId: values.workspace_id,
        });
        assetsResult = await importAssetsMutation.mutateAsync({
          workspaceId: values.workspace_id,
          body: {
            world_md_content: world_md_content,
            characters_md_content: characters_md_content,
          },
        });
      }

      // ③ 跳 OutlinePage(autoStart=false:OutlinePage 显示 IdlePanel +
      // "生成大纲" 按钮,让用户先核对工作区设置 / project.yaml routing 后
      // 主动触发 outline:generate。原 autoStart=true 与 wave 6 s1 决策 2 字面
      // "让用户先配 routing 再触发" 冲突,绕过 IdlePanel 暂停点 — 详
      // docs/impl/v1_0_hotfix_wave6_s2.5_*.md(2026-05-25 修复)。
      setSubmission({
        kind: "creating",
        stage: "done",
        workspaceId: values.workspace_id,
      });
      showSuccessToast(
        "工作区已创建",
        `跳转大纲页,点击"生成大纲"按钮开始生成(工作区 ${values.workspace_id})`,
      );
      navigate(
        `/w/${encodeURIComponent(values.workspace_id)}/outline`,
        {
          state: {
            formValues: values,
            assetsResult,
            autoStart: false,
          } satisfies OutlineNavigateState,
        },
      );
    } catch (err) {
      const message = isApiError(err)
        ? `${err.code}: ${err.message}`
        : err instanceof Error
          ? err.message
          : "未知错误";
      setSubmission({
        kind: "error",
        message,
        workspaceId: workspaceCreated ? values.workspace_id : null,
      });
      showApiErrorToast(err);
    }
  };

  const disabled = submission.kind === "creating";

  const submitButtonLabel = (() => {
    if (submission.kind === "creating") {
      return STAGE_LABELS[submission.stage] + "…";
    }
    return "创建工作区";
  })();

  return (
    <div className="space-y-4">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold">新书启动</h1>
        {POST_V8_ABC_SELECTION_UI ? (
          <p className="text-sm text-muted-foreground">
            当前为任务 {POST_V8_ABC_SELECTION_UI.candidate} 的独立三章选型入口。
            创建后进入大纲页并按正常产品流程生成、审核、提交和运行；不得修改
            provider、型号或冻结输入。
          </p>
        ) : (
          <p className="text-sm text-muted-foreground">
            从一句创意开始，先选择并确认小说规划，再生成全书大纲。创建工作区不会调用模型。
          </p>
        )}
      </header>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <section
          data-testid="nb-left"
          className="rounded border border-border bg-card p-4"
        >
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            输入
          </h2>
          {!POST_V8_ABC_SELECTION_UI && <label className="mb-4 block text-sm"><input type="checkbox" checked={advanced} disabled={disabled} onChange={(e) => setAdvanced(e.target.checked)} /> 使用原有高级建书／导入流程</label>}
          {advanced ? <NewBookForm
            onSubmit={handleSubmit}
            disabled={disabled}
            submitButtonLabel={submitButtonLabel}
          /> : <CreativeBookForm onSubmit={handleSubmit} disabled={disabled} submitButtonLabel={submitButtonLabel} />}
        </section>

        <section
          data-testid="nb-right"
          className="rounded border border-border bg-card p-4"
        >
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            创建进度
          </h2>
          <CreationProgressPanel submission={submission} />
        </section>
      </div>
    </div>
  );
}

/** v1.0.x wave 6 s1+s4 起 navigate state shape(NewBookPage → OutlinePage)。 */
export interface OutlineNavigateState {
  formValues: NewBookFormValues;
  assetsResult: AssetsImportOut | null;
  autoStart: boolean;
}

interface CreationProgressPanelProps {
  submission: SubmissionState;
}

function CreationProgressPanel(props: CreationProgressPanelProps) {
  const { submission } = props;

  if (submission.kind === "idle") {
    return (
      <div className="rounded border border-dashed border-border p-4 text-sm text-muted-foreground">
        填写左侧表单后点"创建工作区",此处显示创建 / 上传 / 跳转大纲页进度。
        大纲生成在大纲页发起(可先编辑 project.yaml 切云端)。
      </div>
    );
  }

  if (submission.kind === "error") {
    return (
      <div className="rounded border border-destructive bg-destructive/10 p-3 text-sm text-destructive">
        <p className="font-medium">创建失败</p>
        <p className="mt-1 break-all">{submission.message}</p>
        {submission.workspaceId && (
          <p className="mt-2 text-xs">
            已创建的工作区 ID:
            <code className="ml-1">{submission.workspaceId}</code>
            ;可改 ID 重提或在工作区页面手动清理。
          </p>
        )}
      </div>
    );
  }

  // creating:渲染 stage 列表
  const currentStage = submission.stage;
  const stages: CreationStage[] = ["creating", "uploading_assets", "done"];
  return (
    <div className="space-y-3">
      <div
        data-testid="nb-creation-badge"
        className="rounded bg-sky-50 p-2 text-sm text-sky-700"
      >
        <p className="font-medium">创建中…</p>
        <p className="mt-0.5 text-xs">
          工作区:<code>{submission.workspaceId}</code>
        </p>
      </div>
      <ol className="space-y-1.5 text-sm" data-testid="nb-stage-list">
        {stages.map((stage) => {
          const stageIdx = stages.indexOf(stage);
          const currentIdx = stages.indexOf(currentStage);
          const state =
            stageIdx < currentIdx
              ? "done"
              : stageIdx === currentIdx
                ? "active"
                : "pending";
          const icon =
            state === "done" ? "✓" : state === "active" ? "●" : "○";
          const cls =
            state === "done"
              ? "text-emerald-600"
              : state === "active"
                ? "text-sky-600 font-medium"
                : "text-muted-foreground";
          return (
            <li
              key={stage}
              className={cn("flex items-center gap-2", cls)}
            >
              <span aria-hidden="true">{icon}</span>
              <span>{STAGE_LABELS[stage]}</span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
