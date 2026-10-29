import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost } from "@/api/client";
import type { components } from "@/api/types";
import { Button } from "@/components/ui/button";

type Diagnostic = components["schemas"]["DiagnosticOut"];
type Task = components["schemas"]["TaskInfo"];
type Repair = components["schemas"]["RepairOut"];
type Conflict = { desc: string; reason: string; source_quotes: { path: string; quote: string }[] };
const done = (task: Task | undefined) => !!task && ["done", "failed", "cancelled", "interrupted"].includes(task.status);

export function OutlineRepairPanel({ workspaceId, diagnostic }: { workspaceId: string; diagnostic: Diagnostic }) {
  const queryClient = useQueryClient();
  const base = `/workspaces/${encodeURIComponent(workspaceId)}`;
  const key = `outline-repair:${workspaceId}`;
  const [repairId, setRepairId] = useState<string | null>(() => localStorage.getItem(key));
  const [reviewId, setReviewId] = useState<string | null>(() => localStorage.getItem(`${key}:review`));
  const [patchTaskId, setPatchTaskId] = useState<string | null>(() => localStorage.getItem(`${key}:patch-task`));
  const [reviewTaskId, setReviewTaskId] = useState<string | null>(() => localStorage.getItem(`${key}:review-task`));
  const [conflictIndex, setConflictIndex] = useState(0);
  const [paths, setPaths] = useState<string[]>([]);
  const [direction, setDirection] = useState("");
  const result = diagnostic.jobs.find(job => job.status === "completed" && !!job.result && typeof job.result === "object" && !Array.isArray(job.result)
    && Array.isArray((job.result as Record<string, unknown>).contradictions)
    && ((job.result as { contradictions: unknown[] }).contradictions.length > 0))?.result ?? diagnostic.jobs[0]?.result;
  const conflicts: Conflict[] = result && typeof result === "object" && !Array.isArray(result) && Array.isArray((result as Record<string, unknown>).contradictions)
    ? (result as { contradictions: Conflict[] }).contradictions : [];
  const repair = useQuery({ queryKey: ["outline-repair", workspaceId, repairId], enabled: !!repairId,
    queryFn: () => apiGet<Repair>(`${base}/outline-repairs/${repairId}`), refetchInterval: 2000 });
  const patchTask = useQuery({ queryKey: ["outline-repair-task", patchTaskId], enabled: !!patchTaskId,
    queryFn: () => apiGet<Task>(`/tasks/${patchTaskId}`), refetchInterval: q => done(q.state.data) ? false : 1500 });
  const review = useQuery({ queryKey: ["outline-repair-review", reviewId], enabled: !!reviewId,
    queryFn: () => apiGet<Diagnostic>(`${base}/outline-diagnostics/${reviewId}`), refetchInterval: 2000 });
  const reviewTask = useQuery({ queryKey: ["outline-repair-review-task", reviewTaskId], enabled: !!reviewTaskId,
    queryFn: () => apiGet<Task>(`/tasks/${reviewTaskId}`), refetchInterval: q => done(q.state.data) ? false : 1500 });
  const prepare = useMutation({ mutationFn: () => apiPost<Repair>(`${base}/outline-repairs:prepare`, {
    diagnostic_id: diagnostic.diagnostic_id, conflict_index: conflictIndex, allowed_paths: paths, direction,
  }), onSuccess: data => { setRepairId(data.repair_id); setReviewId(null); localStorage.setItem(key, data.repair_id); localStorage.removeItem(`${key}:review`); void queryClient.invalidateQueries({ queryKey: ["outline-repair"] }); } });
  const startPatch = useMutation({ mutationFn: () => apiPost<Task>(`${base}/outline-repairs/${repairId}:start`, { request_hash: repair.data!.request_hash }),
    onSuccess: task => { setPatchTaskId(task.task_id); localStorage.setItem(`${key}:patch-task`, task.task_id); } });
  const prepareReview = useMutation({ mutationFn: () => apiPost<Diagnostic>(`${base}/outline-repairs/${repairId}:prepare-review`),
    onSuccess: data => { setReviewId(data.diagnostic_id); localStorage.setItem(`${key}:review`, data.diagnostic_id); } });
  const startReview = useMutation({ mutationFn: () => apiPost<Task>(`${base}/outline-diagnostics/${reviewId}:start`, { request_hash: review.data!.request_hash }),
    onSuccess: task => { setReviewTaskId(task.task_id); localStorage.setItem(`${key}:review-task`, task.task_id); } });
  const commit = useMutation({ mutationFn: () => apiPost<Repair>(`${base}/outline-repairs/${repairId}:commit`, { review_id: reviewId }),
    onSuccess: () => { void queryClient.invalidateQueries(); } });
  const error = prepare.error ?? startPatch.error ?? prepareReview.error ?? startReview.error ?? commit.error ?? repair.error ?? review.error;
  const reviewJobs = review.data?.jobs ?? [];
  const deltaPassed = reviewJobs.length === 2 && ["resolution", "regression"].every(id => {
    const job = reviewJobs.find(item => item.job_id === id);
    return job?.status === "completed" && !!job.result && typeof job.result === "object" && !Array.isArray(job.result)
      && (job.result as Record<string, unknown>).outcome === "compatible";
  });
  const reviewResult = reviewJobs[0]?.result;
  const legacyPassed = reviewJobs.length === 1 && reviewJobs[0].status === "completed" && !!reviewResult && typeof reviewResult === "object" && !Array.isArray(reviewResult)
    && Array.isArray((reviewResult as Record<string, unknown>).contradictions)
    && ((reviewResult as { contradictions: unknown[] }).contradictions.length === 0)
    && (reviewResult as Record<string, unknown>).repair_scope === "none";
  const splitPassed = reviewJobs.length === 5 && ["world", "characters", "plot", "volume_plans", "cross"].every(id => {
    const job = reviewJobs.find(item => item.job_id === id);
    return job?.status === "completed" && !!job.result && typeof job.result === "object" && !Array.isArray(job.result)
      && (job.result as Record<string, unknown>).outcome === "compatible";
  });
  const passed = deltaPassed || legacyPassed || splitPassed;
  return <div className="mt-4 rounded border p-3 text-sm">
    <p className="font-medium">定点修复</p>
    <p>一次只改同一段内被这条硬矛盾引用的字段。候选须先证明原冲突消除，再检查修改字段是否引入新矛盾；两项都通过才写回。</p>
    {conflicts.length === 0 && <p>这份审核没有硬矛盾，无需修复。</p>}
    {conflicts.map((conflict, index) => <div className="mt-3 rounded border p-2" key={index}>
      <p>{conflict.desc}</p>
      <p className="text-muted-foreground">{conflict.reason}</p>
      {Array.from(new Set(conflict.source_quotes.map(q => q.path))).map(path => <label className="mt-2 block" key={path}>
        <input type="checkbox" checked={index === conflictIndex && paths.includes(path)} onChange={event => {
          setConflictIndex(index); setPaths(current => event.target.checked ? [...(index === conflictIndex ? current : []), path] : current.filter(value => value !== path));
        }} /> 修改 {path}
      </label>)}
    </div>)}
    {conflicts.length > 0 && <div className="mt-2 space-y-2">
      <textarea className="w-full rounded border p-2" aria-label="修复方向" placeholder="例如：保留主线中第四章首次改采样点，人物关系只能写此前相识，不得写此前已改。" value={direction} onChange={event => setDirection(event.target.value)} />
      <Button disabled={!paths.length || !direction.trim() || prepare.isPending} onClick={() => prepare.mutate()}>准备来源锁定的修复（零调用）</Button>
    </div>}
    {error && <p role="alert">{error instanceof Error ? error.message : "操作失败"}</p>}
    {repair.data && <div className="mt-3 space-y-2">
      <p>修复状态：{repair.data.status}；可改 {Object.keys(repair.data.allowed_fields).length} 个字段；需重验 {repair.data.dependent_sections.join("、")}。</p>
      <p>生成补丁最多 {repair.data.physical_request_cap} 次物理请求，独立费用上限 ¥{repair.data.cost_cap_cny}。</p>
      {!repair.data.authorization_present && repair.data.status === "pending" && <p>已准备，尚未登记本次付费授权。</p>}
      {repair.data.status === "pending" && <Button disabled={!repair.data.authorization_present || startPatch.isPending} onClick={() => startPatch.mutate()}>生成已授权候选补丁</Button>}
      {repair.data.status === "started" && <div><p>只会尝试读取已保存响应；结果不明时禁止再次付费调用。</p><Button disabled={startPatch.isPending} onClick={() => startPatch.mutate()}>从账本和响应恢复</Button></div>}
      {repair.data.error && <p role="alert">{repair.data.error}</p>}
      {!!repair.data.proposal && typeof repair.data.proposal === "object" && !Array.isArray(repair.data.proposal) && <details className="rounded border p-2">
        <summary>查看模型提交的补丁原文</summary>
        {Array.isArray((repair.data.proposal as Record<string, unknown>).changes) && ((repair.data.proposal as { changes: { path: string; old: string; new: string }[] }).changes).map(change => <div className="mt-2" key={change.path}>
          <p className="font-medium">{change.path}</p><p>原文：{change.old}</p><p>候选：{change.new}</p>
        </div>)}
        <p>{String((repair.data.proposal as Record<string, unknown>).explanation ?? "")}</p>
      </details>}
      {patchTask.data?.error && <p role="alert">{String(patchTask.data.error.message ?? "补丁生成未完成")}</p>}
      {repair.data.status === "staged" && <Button disabled={prepareReview.isPending} onClick={() => prepareReview.mutate()}>准备候选复审（零调用）</Button>}
      {repair.data.status === "committed" && <p>补丁已写回；请继续生成章节小纲，最新四段将使用这次复审凭据。</p>}
    </div>}
    {review.data && repair.data?.status === "staged" && <div className="mt-3 space-y-2">
      <p>候选复审：{reviewJobs.map(job => `${job.job_id === "resolution" ? "原冲突" : job.job_id === "regression" ? "新增矛盾" : "完整审核"} ${job.status}`).join("；")}；最多 {review.data.physical_request_cap} 次请求，独立上限 ¥{review.data.cost_cap_cny}。</p>
      {!review.data.authorization_present && <p>复审仍需独立付费授权。</p>}
      {reviewJobs.some(job => job.status === "pending") && !reviewJobs.some(job => job.status === "invalid") && <Button disabled={!review.data.authorization_present || startReview.isPending} onClick={() => startReview.mutate()}>开始／继续已授权复审</Button>}
      {reviewJobs.some(job => job.status === "started") && <Button disabled={startReview.isPending} onClick={() => startReview.mutate()}>从已保存复审响应恢复</Button>}
      {reviewTask.data?.error && <p role="alert">{String(reviewTask.data.error.message ?? "复审未完成")}</p>}
      {reviewJobs.every(job => ["completed", "invalid"].includes(job.status)) && <p>{passed ? "候选复审已通过，可提交候选。" : "复审未通过，候选未写回。"}</p>}
      {passed && <Button disabled={commit.isPending} onClick={() => commit.mutate()}>写回通过复审的补丁</Button>}
    </div>}
  </div>;
}
