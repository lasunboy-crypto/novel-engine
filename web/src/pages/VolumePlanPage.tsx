/**
 * VolumePlanPage(P5-F-04):卷规划 + 张力曲线编辑页。
 *
 * 路由:`/w/:workspaceId/volumes/:volumeId`(对齐 FRONTEND_SPEC §4.6)。
 *
 * 结构:
 *   1. 顶部:卷 tabs(切换 volume_id;NavLink 走 URL 而非 useState)+ 卷信息
 *      (title / summary / 章数 / 当前模板)
 *   2. 模板下拉(4 档)+ "应用模板"按钮 → POST /volumes/{vid}:apply-template
 *   3. TensionCurveChart(草稿态 useState 驱动,实时重绘)
 *   4. VolumeChapterTable(三列;改 tension → 草稿态 useState 更新)
 *   5. "保存"按钮 + "撤销修改"按钮(草稿态有改动时显)
 *
 * 数据流:
 *   - `useVolumeQuery` 服务端权威 → `draftPoints` / `draftTemplate` useState
 *     草稿态(初始化:服务端 → 草稿)
 *   - apply-template 走 mutation(后端写库),成功后 query refetch + 草稿态
 *     重新初始化
 *   - 表格改 tension → 仅改草稿 useState;曲线 re-render;保存按钮 enable
 *   - 保存调 useUpdateVolumeMutation(只 patch tension_curve + tension_template)
 *   - chapter_count / arcs 本页不编辑(留 P5-F-05 OutlinePage / P6 拖拽)
 *
 * 表格-曲线双向绑定 useState + onChange(自决项 2);曲线拖拽 P6-F-07 已落
 * 纯 Recharts SVG pointer event 实现(P5 留观项 1 v1 内解决,见
 * TensionCurveChart.tsx 头部 docstring)。
 *
 * P6-F-07 新增辅助按钮:
 *   - "重置当前曲线":把草稿 tension 全部刷成 5(中性张力)+ 模板切 free;
 *     不动 chapter_count / arcs / is_arc_finale。**未保存**,可撤销。
 *   - "导出 JSON":把当前草稿 tension_curve 数组转 JSON 触发浏览器下载,
 *     文件名 `tension_curve_<volume_id>_<ISO_DATE>.json`(便于离线备份)
 */

import { useEffect, useMemo, useState } from "react";
import { Link, NavLink, useParams } from "react-router-dom";

import { isApiError } from "@/api/errors";
import {
  TENSION_TEMPLATE_LABELS,
  TENSION_TEMPLATE_OPTIONS,
  useApplyTemplateMutation,
  useUpdateVolumeMutation,
  useVolumeQuery,
  useVolumesQuery,
  type TensionCurvePoint,
  type TensionTemplate,
  type VolumeOut,
} from "@/api/volumes";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import { TensionCurveChart } from "@/components/TensionCurveChart";
import { VolumeChapterTable } from "@/components/VolumeChapterTable";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { useUnsavedChanges } from "@/hooks/useUnsavedChanges";
import { cn } from "@/lib/utils";
import { useActiveWorkspaceStore } from "@/store/activeWorkspace";

export default function VolumePlanPage() {
  const { workspaceId, volumeId } = useParams<{
    workspaceId: string;
    volumeId: string;
  }>();

  if (!workspaceId || !volumeId) {
    return (
      <div className="rounded border border-destructive bg-destructive/10 p-6 text-sm text-destructive">
        URL 缺少 workspaceId / volumeId 参数。
        <Link to="/workspaces" className="ml-2 underline">
          回到工作区列表
        </Link>
      </div>
    );
  }

  return <VolumePlanPageInner workspaceId={workspaceId} volumeId={volumeId} />;
}

interface InnerProps {
  workspaceId: string;
  volumeId: string;
}

function VolumePlanPageInner({ workspaceId, volumeId }: InnerProps) {
  const volumesQuery = useVolumesQuery(workspaceId);
  const volumeQuery = useVolumeQuery(workspaceId, volumeId);
  const setActiveWorkspaceId = useActiveWorkspaceStore(
    (s) => s.setActiveWorkspaceId,
  );

  // v1.0.x B2-02:进入卷规划页时同步 zustand,让 AppShell 顶部 nav 拼出正确的
  // /w/:workspaceId/* href(对齐 RunControlPage / ExportPage 模式)。
  useEffect(() => {
    setActiveWorkspaceId(workspaceId);
  }, [workspaceId, setActiveWorkspaceId]);

  const volume = volumeQuery.data;

  const [draftPoints, setDraftPoints] = useState<TensionCurvePoint[]>([]);
  const [draftTemplate, setDraftTemplate] = useState<TensionTemplate>("free");
  const [pendingTemplate, setPendingTemplate] = useState<TensionTemplate>("free");

  useEffect(() => {
    if (!volume) return;
    setDraftPoints((volume.tension_curve ?? []).map((p) => ({ ...p })));
    setDraftTemplate(volume.tension_template);
    setPendingTemplate(volume.tension_template);
  }, [volume]);

  const isDirty = useMemo(
    () => computeDirty(volume, draftPoints, draftTemplate),
    [volume, draftPoints, draftTemplate],
  );
  useUnsavedChanges(isDirty);

  const updateMutation = useUpdateVolumeMutation();
  const applyTemplateMutation = useApplyTemplateMutation();

  const isBusy =
    updateMutation.isPending ||
    applyTemplateMutation.isPending ||
    volumeQuery.isFetching;

  const handleTensionChange = (chapterNumber: number, nextTension: number) => {
    setDraftPoints((prev) =>
      prev.map((p) =>
        p.chapter_number === chapterNumber
          ? { ...p, tension: nextTension }
          : p,
      ),
    );
    if (draftTemplate !== "free") setDraftTemplate("free");
  };

  const handleApplyTemplate = () => {
    applyTemplateMutation.mutate(
      {
        workspaceId,
        volumeId,
        body: { template: pendingTemplate },
      },
      {
        onSuccess: () => {
          showSuccessToast(
            `已应用模板 "${TENSION_TEMPLATE_LABELS[pendingTemplate]}"`,
            "曲线已更新,可继续微调后保存。",
          );
        },
        onError: (err) => showApiErrorToast(err),
      },
    );
  };

  const handleSave = () => {
    if (!volume) return;
    updateMutation.mutate(
      {
        workspaceId,
        volumeId,
        body: {
          tension_curve: draftPoints,
          tension_template: draftTemplate,
        },
      },
      {
        onSuccess: () => {
          showSuccessToast("卷已保存");
        },
        onError: (err) => showApiErrorToast(err),
      },
    );
  };

  const handleRevert = () => {
    if (!volume) return;
    setDraftPoints((volume.tension_curve ?? []).map((p) => ({ ...p })));
    setDraftTemplate(volume.tension_template);
  };

  const handleResetCurve = () => {
    if (!volume) return;
    setDraftPoints((prev) => prev.map((p) => ({ ...p, tension: 5 })));
    if (draftTemplate !== "free") setDraftTemplate("free");
  };

  const handleExportJson = () => {
    if (draftPoints.length === 0) return;
    const payload = JSON.stringify(
      {
        volume_id: volumeId,
        tension_template: draftTemplate,
        tension_curve: draftPoints,
        exported_at: new Date().toISOString(),
      },
      null,
      2,
    );
    const blob = new Blob([payload], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    const stamp = new Date().toISOString().slice(0, 10);
    a.href = url;
    a.download = `tension_curve_${volumeId}_${stamp}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  return (
    <div className="space-y-4">
      <header className="space-y-2">
        <p className="text-xs text-muted-foreground">
          <Link to={`/workspaces/${workspaceId}`} className="underline">
            ← 工作区
          </Link>
        </p>
        <h1 className="text-2xl font-semibold">卷规划</h1>
        <p className="text-sm text-muted-foreground">
          工作区 <code>{workspaceId}</code> · 拖拽曲线点或在表格中改 tension(1-10
          整数),曲线实时重绘。
        </p>
      </header>

      <VolumeTabs
        workspaceId={workspaceId}
        currentVolumeId={volumeId}
        volumes={volumesQuery.data?.volumes ?? []}
        loading={volumesQuery.isPending}
      />

      {volumeQuery.isPending ? (
        <div className="rounded border border-border p-6 text-sm text-muted-foreground">
          加载卷 {volumeId} …
        </div>
      ) : volumeQuery.error ? (
        <VolumeLoadError error={volumeQuery.error} />
      ) : !volume ? (
        <div className="rounded border border-border p-6 text-sm text-muted-foreground">
          未找到卷 {volumeId}。
        </div>
      ) : (
        <>
          <VolumeInfoCard volume={volume} />

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">张力曲线模板</CardTitle>
              <CardDescription className="text-xs">
                选择一档预设模板覆盖当前曲线(后端按 chapter_count 线性插值缩放;arcs 不动)
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="flex flex-wrap items-center gap-2">
                <label
                  htmlFor="tension-template-select"
                  className="text-sm text-muted-foreground"
                >
                  模板:
                </label>
                <select
                  id="tension-template-select"
                  value={pendingTemplate}
                  onChange={(e) =>
                    setPendingTemplate(e.target.value as TensionTemplate)
                  }
                  disabled={isBusy}
                  className="rounded border border-input bg-background px-2 py-1 text-sm disabled:cursor-not-allowed disabled:opacity-50"
                  data-testid="tension-template-select"
                >
                  {TENSION_TEMPLATE_OPTIONS.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </select>
                <Button
                  onClick={handleApplyTemplate}
                  disabled={isBusy}
                  data-testid="apply-template-button"
                >
                  {applyTemplateMutation.isPending ? "应用中…" : "应用模板"}
                </Button>
                <span className="text-xs text-muted-foreground">
                  当前生效模板:
                  <span className="ml-1 font-medium">
                    {TENSION_TEMPLATE_LABELS[draftTemplate]}
                  </span>
                </span>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">张力曲线</CardTitle>
              <CardDescription className="text-xs">
                X 章节序号(1-{volume.chapter_count}),Y 张力 1-10;弧线终章高亮 amber 点;
                <span className="ml-1 text-emerald-700">按住任意点上下拖动可改 tension(整数 1-10)</span>
              </CardDescription>
            </CardHeader>
            <CardContent>
              <TensionCurveChart
                points={draftPoints}
                onTensionChange={handleTensionChange}
                disabled={isBusy}
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">章节张力表</CardTitle>
              <CardDescription className="text-xs">
                直接修改"张力"列,上方曲线实时重绘;模板字段会自动切到"自由"
              </CardDescription>
            </CardHeader>
            <CardContent>
              <VolumeChapterTable
                points={draftPoints}
                onTensionChange={handleTensionChange}
                disabled={isBusy}
              />
            </CardContent>
          </Card>

          <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
            <Button
              onClick={handleSave}
              disabled={!isDirty || isBusy}
              data-testid="save-volume-button"
            >
              {updateMutation.isPending ? "保存中…" : "保存"}
            </Button>
            <Button
              variant="outline"
              onClick={handleRevert}
              disabled={!isDirty || isBusy}
              data-testid="revert-volume-button"
            >
              撤销修改
            </Button>
            <Button
              variant="outline"
              onClick={handleResetCurve}
              disabled={isBusy || draftPoints.length === 0}
              data-testid="reset-curve-button"
              title="把所有章节 tension 重置为 5(中性张力)+ 模板切自由,可继续微调"
            >
              重置当前曲线
            </Button>
            <Button
              variant="outline"
              onClick={handleExportJson}
              disabled={draftPoints.length === 0}
              data-testid="export-curve-json-button"
              title="导出当前草稿 tension_curve 为 JSON 文件,便于离线备份"
            >
              导出 JSON
            </Button>
            {isDirty && (
              <span className="text-xs text-amber-700">未保存的修改</span>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function computeDirty(
  volume: VolumeOut | undefined,
  draftPoints: readonly TensionCurvePoint[],
  draftTemplate: TensionTemplate,
): boolean {
  if (!volume) return false;
  if (draftTemplate !== volume.tension_template) return true;
  const orig = volume.tension_curve ?? [];
  if (orig.length !== draftPoints.length) return true;
  for (let i = 0; i < orig.length; i++) {
    const a = orig[i];
    const b = draftPoints[i];
    if (
      a.chapter_number !== b.chapter_number ||
      a.tension !== b.tension ||
      a.is_arc_finale !== b.is_arc_finale
    ) {
      return true;
    }
  }
  return false;
}

interface VolumeTabsProps {
  workspaceId: string;
  currentVolumeId: string;
  volumes: readonly VolumeOut[];
  loading: boolean;
}

function VolumeTabs({
  workspaceId,
  currentVolumeId,
  volumes,
  loading,
}: VolumeTabsProps) {
  if (loading) {
    return (
      <div
        className="rounded border border-border p-2 text-xs text-muted-foreground"
        data-testid="volume-tabs-loading"
      >
        加载卷列表…
      </div>
    );
  }
  if (volumes.length === 0) {
    return (
      <div className="rounded border border-border p-3 text-sm text-muted-foreground">
        当前工作区暂无卷数据 — 先在 NewBookPage 完成大纲生成 + 提交。
      </div>
    );
  }
  return (
    <nav
      className="flex flex-wrap items-center gap-1 border-b border-border"
      data-testid="volume-tabs"
      aria-label="卷切换"
    >
      {volumes.map((v) => (
        <NavLink
          key={v.volume_id}
          to={`/w/${workspaceId}/volumes/${v.volume_id}`}
          end
          className={() =>
            cn(
              "rounded-t border border-b-0 px-3 py-1.5 text-sm",
              v.volume_id === currentVolumeId
                ? "border-border bg-background font-medium"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )
          }
        >
          {v.title}
          <span className="ml-1 text-xs text-muted-foreground">
            ({v.chapter_count} 章)
          </span>
        </NavLink>
      ))}
    </nav>
  );
}

function VolumeInfoCard({ volume }: { volume: VolumeOut }) {
  return (
    <Card data-testid="volume-info-card">
      <CardHeader className="pb-2">
        <CardTitle className="text-base">
          {volume.title}
          <span className="ml-2 text-xs font-normal text-muted-foreground">
            id: <code>{volume.volume_id}</code>
          </span>
        </CardTitle>
        <CardDescription className="text-xs">
          {volume.chapter_count} 章 · 模板{" "}
          {TENSION_TEMPLATE_LABELS[volume.tension_template]} ·{" "}
          {volume.arcs?.length ?? 0} 个弧
        </CardDescription>
      </CardHeader>
      <CardContent>
        <p className="whitespace-pre-wrap text-sm text-muted-foreground">
          {volume.summary || "(暂无卷概要)"}
        </p>
        {volume.arcs && volume.arcs.length > 0 && (
          <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
            {volume.arcs.map((arc) => (
              <li key={arc.arc_id}>
                <span className="font-medium text-foreground">
                  {arc.arc_type}
                </span>{" "}
                · ch {arc.chapter_range[0]}-{arc.chapter_range[1]} ·{" "}
                {arc.phase}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function VolumeLoadError({ error }: { error: unknown }) {
  const message = isApiError(error)
    ? `${error.code}: ${error.message}`
    : error instanceof Error
      ? error.message
      : "未知错误";
  return (
    <div className="rounded border border-destructive bg-destructive/10 p-3 text-sm text-destructive">
      加载卷失败:{message}
    </div>
  );
}
