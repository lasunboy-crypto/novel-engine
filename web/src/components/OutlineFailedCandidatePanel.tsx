import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost } from "@/api/client";
import type { components } from "@/api/types";
import { Button } from "@/components/ui/button";

type Repair = components["schemas"]["RepairOut"];
type Diagnostic = components["schemas"]["DiagnosticOut"];
type Task = components["schemas"]["TaskInfo"];
type Quote = { path: string; quote: string };
type Conflict = { desc: string; reason: string; source_quotes: Quote[] };
type Candidate = {
  candidate_id: string;
  source_hash: string;
  review: { contradictions: Conflict[]; coverage_gaps: string[]; repair_scope: string };
};

export function OutlineFailedCandidatePanel({ workspaceId, candidateId, onContinueMiniPlan, continueDisabled = false }: {
  workspaceId: string; candidateId: string; onContinueMiniPlan?: () => void; continueDisabled?: boolean;
}) {
  const base = `/workspaces/${encodeURIComponent(workspaceId)}`;
  const storage = `outline-failed-repair:${workspaceId}:${candidateId}`;
  const queryClient = useQueryClient();
  const [paths, setPaths] = useState<string[]>([]);
  const [direction, setDirection] = useState("");
  const [repairId, setRepairId] = useState<string | null>(() => localStorage.getItem(storage));
  const [reviewId, setReviewId] = useState<string | null>(() => localStorage.getItem(`${storage}:review`));
  const [patchTaskId, setPatchTaskId] = useState<string | null>(() => localStorage.getItem(`${storage}:patch-task`));
  const [reviewTaskId, setReviewTaskId] = useState<string | null>(() => localStorage.getItem(`${storage}:review-task`));
  const candidate = useQuery({ queryKey: ["outline-failed-candidate", workspaceId, candidateId],
    queryFn: () => apiGet<Candidate>(`${base}/outline-failed-candidates/${candidateId}`) });
  const repair = useQuery({ queryKey: ["outline-failed-repair", workspaceId, repairId], enabled: !!repairId,
    queryFn: () => apiGet<Repair>(`${base}/outline-repairs/${repairId}`),
    refetchInterval: q => !q.state.data || ["pending", "started"].includes(q.state.data.status) ? 2000 : false });
  const review = useQuery({ queryKey: ["outline-failed-review", workspaceId, reviewId], enabled: !!reviewId,
    queryFn: () => apiGet<Diagnostic>(`${base}/outline-diagnostics/${reviewId}`),
    refetchInterval: q => !q.state.data || q.state.data.jobs.some(job => ["pending", "started"].includes(job.status)) ? 2000 : false });
  const patchTask = useQuery({ queryKey: ["outline-failed-patch-task", patchTaskId], enabled: !!patchTaskId,
    queryFn: () => apiGet<Task>(`/tasks/${patchTaskId}`),
    refetchInterval: q => !q.state.data || ["pending", "running"].includes(q.state.data.status) ? 2000 : false });
  const reviewTask = useQuery({ queryKey: ["outline-failed-review-task", reviewTaskId], enabled: !!reviewTaskId,
    queryFn: () => apiGet<Task>(`/tasks/${reviewTaskId}`),
    refetchInterval: q => !q.state.data || ["pending", "running"].includes(q.state.data.status) ? 2000 : false });
  const prepare = useMutation({ mutationFn: () => apiPost<Repair>(`${base}/outline-failed-candidates/${candidateId}:prepare-repair`, {
    conflict_indexes: conflictRows.map((_, index) => index), allowed_paths: paths, direction,
    ...(continuing ? { parent_repair_id: repairId, parent_review_id: reviewId }
      : repair.data?.parent_repair_id && repair.data.parent_review_id
        ? { parent_repair_id: repair.data.parent_repair_id, parent_review_id: repair.data.parent_review_id } : {}),
  }), onSuccess: value => {
    setRepairId(value.repair_id); localStorage.setItem(storage, value.repair_id);
    setPaths([]);
    setReviewId(null); setPatchTaskId(null); setReviewTaskId(null);
    localStorage.removeItem(`${storage}:review`); localStorage.removeItem(`${storage}:patch-task`);
    localStorage.removeItem(`${storage}:review-task`);
  } });
  const startPatch = useMutation({ mutationFn: () => apiPost<Task>(`${base}/outline-repairs/${repairId}:start`, {
    request_hash: repair.data!.request_hash,
  }), onSuccess: value => { setPatchTaskId(value.task_id); localStorage.setItem(`${storage}:patch-task`, value.task_id); } });
  const prepareReview = useMutation({ mutationFn: () => apiPost<Diagnostic>(`${base}/outline-repairs/${repairId}:prepare-review`),
    onSuccess: value => { setReviewId(value.diagnostic_id); localStorage.setItem(`${storage}:review`, value.diagnostic_id); } });
  const startReview = useMutation({ mutationFn: () => apiPost<Task>(`${base}/outline-diagnostics/${reviewId}:start`, {
    request_hash: review.data!.request_hash,
  }), onSuccess: value => { setReviewTaskId(value.task_id); localStorage.setItem(`${storage}:review-task`, value.task_id); } });
  const commit = useMutation({ mutationFn: () => apiPost<Repair>(`${base}/outline-repairs/${repairId}:commit`, {
    review_id: reviewId,
  }), onSuccess: () => { void queryClient.invalidateQueries(); } });
  const reviewJobs = review.data?.jobs ?? [];
  const reviewEnded = reviewJobs.length > 0 && reviewJobs.every(job => ["completed", "invalid", "skipped"].includes(job.status));
  const continuedConflicts = reviewJobs.flatMap(job => {
    const value = job.result;
    return job.status === "completed" && value && typeof value === "object" && !Array.isArray(value)
      && Array.isArray((value as Record<string, unknown>).contradictions)
      ? (value as { contradictions: Array<{ desc: string; reason: string; source_quotes: Array<{path: string; quote: string}> }> }).contradictions : [];
  });
  const continuing = repair.data?.status === "staged" && reviewJobs.length === 5 && reviewEnded && continuedConflicts.length > 0;
  const conflictRows = continuing ? continuedConflicts
    : repair.data?.source_conflicts?.length ? repair.data.source_conflicts : candidate.data?.review.contradictions ?? [];
  const choices = Array.from(new Set(conflictRows.flatMap(item => item.source_quotes.map(q => q.path))))
    .filter(path => /^(world|characters|plot|volume_plans)\./.test(path));
  const contractPaths = choices.filter(path => path.includes(".dramatic_contract."))
    .map(path => path.slice(0, path.indexOf(".dramatic_contract.") + ".dramatic_contract".length));
  const choicesWithContracts = Array.from(new Set([...choices, ...contractPaths]));
  const reviewLabels: Record<string, string> = { world: "世界", characters: "人物", plot: "主线", volume_plans: "卷规划", cross: "跨段", full: "完整" };
  const statusLabels: Record<string, string> = { pending: "待审核", started: "审核中", completed: "已完成", invalid: "结果无效", skipped: "未执行" };
  const result = reviewJobs.find(job => job.job_id === "full")?.result;
  const legacyPassed = reviewJobs.length === 1 && reviewJobs[0].job_id === "full"
    && reviewJobs[0].status === "completed" && !!result && typeof result === "object" && !Array.isArray(result)
    && Array.isArray((result as Record<string, unknown>).contradictions)
    && (result as { contradictions: unknown[] }).contradictions.length === 0
    && (result as Record<string, unknown>).repair_scope === "none";
  const splitPassed = reviewJobs.length === 5 && ["world", "characters", "plot", "volume_plans", "cross"].every(id => {
    const job = reviewJobs.find(item => item.job_id === id);
    return job?.status === "completed" && !!job.result && typeof job.result === "object" && !Array.isArray(job.result)
      && (job.result as Record<string, unknown>).outcome === "compatible";
  });
  const reviewPassed = legacyPassed || splitPassed;
  const canPrepare = !repair.data || repair.data.status === "invalid"
    || (repair.data.status === "pending" && !repair.data.authorization_present)
    || continuing;
  const error = candidate.error ?? prepare.error ?? repair.error ?? startPatch.error ?? prepareReview.error
    ?? review.error ?? startReview.error ?? commit.error;

  return <details className="rounded border p-3 text-sm" open>
    <summary className="font-medium">隔离候选定点修复</summary>
    <p className="mt-2">卷规划未写入正式大纲；审核原文与候选保留在隔离区。选择全部冲突所需字段，补丁和完整复审通过后才写回。</p>
    {candidate.isLoading && <p>正在读取失败候选…</p>}
    {error && <p role="alert">{error instanceof Error ? error.message : "操作失败"}</p>}
    {conflictRows.map((conflict, index) => <div className="mt-2 rounded border p-2" key={index}>
      <p>{conflict.desc}</p><p className="text-muted-foreground">{conflict.reason}</p>
      {conflict.source_quotes.map((quote, quoteIndex) => <p key={quoteIndex}>原文：“{quote.quote}”</p>)}
    </div>)}
    {conflictRows.length > 0 && canPrepare && <div className="mt-3 space-y-2">
      {choicesWithContracts.map(path => <label className="block" key={path}>
        <input type="checkbox" checked={paths.includes(path)} onChange={event => setPaths(current =>
          event.target.checked
            ? [...current.filter(item => !(item.startsWith(path + ".") || path.startsWith(item + "."))), path]
            : current.filter(item => item !== path))} />
        {path.endsWith(".dramatic_contract") ? " 重写该 Beat 的戏剧合同" : ` 修改原文：${path}`}
      </label>)}
      <textarea className="w-full rounded border p-2" aria-label="全部冲突的修复方向" value={direction}
        onChange={event => setDirection(event.target.value)} placeholder="写明应保留哪一侧事实，以及戏剧合同应属于哪个 Beat。" />
      <Button disabled={!paths.length || !direction.trim() || prepare.isPending} onClick={() => prepare.mutate()}>{continuing ? "在已修候选上准备下一轮补丁（零调用）" : "准备隔离候选补丁（零调用）"}</Button>
      {repair.data && <p>{continuing ? "下一轮从当前已修候选接续，前轮修改和复审证据保留。" : repair.data.status === "pending" ? "可调整尚未授权的方案；原记录保留。" : "上次方案未获通过，请调整所选字段或修复方向后重新准备。"}</p>}
    </div>}
    {repair.data && <div className="mt-3 space-y-2">
      <p>补丁状态：{repair.data.status}；模型请求上限 {repair.data.physical_request_cap} 次／¥{repair.data.cost_cap_cny}。</p>
      {!repair.data.authorization_present && repair.data.status === "pending" && <p>本次请求尚未获独立付费授权。</p>}
      {repair.data.status === "pending" && <Button disabled={!repair.data.authorization_present || startPatch.isPending}
        onClick={() => startPatch.mutate()}>生成已授权补丁</Button>}
      {repair.data.status === "started" && <Button disabled={startPatch.isPending} onClick={() => startPatch.mutate()}>从已保存响应恢复</Button>}
      {repair.data.error && <p role="alert">{repair.data.error}</p>}
      {!!repair.data.proposal && typeof repair.data.proposal === "object" && !Array.isArray(repair.data.proposal) && <details className="rounded border p-2">
        <summary>查看模型提交的候选补丁</summary>
        {Array.isArray((repair.data.proposal as Record<string, unknown>).changes) &&
          (repair.data.proposal as { changes: { path: string; old: string; new: string }[] }).changes.map(change => <div className="mt-2" key={change.path}>
            <p className="font-medium">{change.path}</p><p>原文：{change.old}</p><p>候选：{change.new}</p>
          </div>)}
        {Array.isArray((repair.data.proposal as Record<string, unknown>).contract_changes) &&
          (repair.data.proposal as { contract_changes: { path: string; old: Record<string, unknown>; new: Record<string, unknown> }[] }).contract_changes.map(change => <div className="mt-2" key={change.path}>
            <p className="font-medium">{change.path}</p><pre className="whitespace-pre-wrap">原文：{JSON.stringify(change.old, null, 2)}</pre><pre className="whitespace-pre-wrap">候选：{JSON.stringify(change.new, null, 2)}</pre>
          </div>)}
        <p>{String((repair.data.proposal as Record<string, unknown>).explanation ?? "")}</p>
      </details>}
      {patchTask.data?.error && <p role="alert">{String(patchTask.data.error.message ?? "补丁未完成")}</p>}
      {repair.data.status === "staged" && !reviewId && <Button disabled={prepareReview.isPending} onClick={() => prepareReview.mutate()}>准备完整候选复审（零调用）</Button>}
      {repair.data.status === "committed" && <div className="space-y-2">
        <p>完整复审通过，候选已写回正式四段。下一步可生成章节小纲。</p>
        {onContinueMiniPlan && <Button disabled={continueDisabled} onClick={onContinueMiniPlan}>继续生成章节小纲</Button>}
      </div>}
    </div>}
    {review.data && repair.data?.status === "staged" && <div className="mt-3 space-y-2">
      <p>完整复审：{reviewJobs.map(job => `${reviewLabels[job.job_id] ?? "其他"}：${statusLabels[job.status] ?? "状态未明"}`).join("；")}；上限 {review.data.physical_request_cap} 次／¥{review.data.cost_cap_cny}。</p>
      {!review.data.authorization_present && <p>复审仍需独立付费授权。</p>}
      {reviewJobs.some(job => job.status === "pending") && <Button disabled={!review.data.authorization_present || startReview.isPending}
        onClick={() => startReview.mutate()}>开始已授权复审</Button>}
      {reviewJobs.some(job => job.status === "started") && <Button disabled={startReview.isPending} onClick={() => startReview.mutate()}>恢复复审结果</Button>}
      {reviewTask.data?.error && <p role="alert">{String(reviewTask.data.error.message ?? "复审未完成")}</p>}
      {reviewJobs.length > 0 && reviewJobs.every(job => job.status === "completed") && <p>{reviewPassed ? "全部硬矛盾已通过完整复审。" : "复审未完整通过，不能写回。"}</p>}
      {reviewPassed && <Button disabled={commit.isPending} onClick={() => commit.mutate()}>写回通过复审的候选</Button>}
    </div>}
  </details>;
}
