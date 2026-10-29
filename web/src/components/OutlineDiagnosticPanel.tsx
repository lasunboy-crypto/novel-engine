import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost } from "@/api/client";
import type { components } from "@/api/types";
import { Button } from "@/components/ui/button";
import { OutlineRepairPanel } from "@/components/OutlineRepairPanel";

type Diagnostic = components["schemas"]["DiagnosticOut"];
type Task = components["schemas"]["TaskInfo"];
const reviewNames: Record<string, string> = {
  world: "世界设定", characters: "人物", plot: "主线", volume_plans: "卷规划", cross: "跨段关系",
};

function ReviewResult({ value }: { value: unknown }) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const result = value as Record<string, unknown>;
  const items = Array.isArray(result.findings) ? result.findings : Array.isArray(result.contradictions) ? result.contradictions : [];
  const label = result.outcome === "conflict" ? "发现冲突" : result.outcome === "compatible" ? "本包可承接" : result.outcome === "unknown" ? "资料不足，尚不能判断" : "完整审核结果";
  return <div className="mt-2 space-y-2 text-sm">
    <p className="font-medium">{label}</p>
    {typeof result.summary === "string" && <p>{result.summary}</p>}
    {typeof result.evidence === "string" && <p>{result.evidence}</p>}
    {items.length === 0 && <p>没有报告具体冲突。</p>}
    {items.map((item, index) => {
      const finding = item as Record<string, unknown>;
      const quotes = Array.isArray(finding.source_quotes) ? finding.source_quotes : [];
      return <div className="border-l pl-3" key={index}>
        <p>{typeof finding.desc === "string" ? finding.desc : typeof finding.reason === "string" ? finding.reason : "核查结果"}</p>
        {typeof finding.desc === "string" && typeof finding.reason === "string" && <p>{finding.reason}</p>}
        {quotes.map((q, quoteIndex) => <blockquote key={quoteIndex} className="mt-1 text-muted-foreground">“{String((q as Record<string, unknown>).quote ?? "")}”</blockquote>)}
      </div>;
    })}
    {Array.isArray(result.coverage_gaps) && result.coverage_gaps.length > 0 && <div><p>承接提醒：</p>{result.coverage_gaps.map((gap, index) => <p key={index}>{String(gap)}</p>)}</div>}
    {Array.isArray(result.requested_paths) && result.requested_paths.length > 0 && <p>模型要求补看原文，当前结论保持未决。</p>}
  </div>;
}

export function OutlineDiagnosticPanel({ workspaceId }: { workspaceId: string }) {
  const base = `/workspaces/${encodeURIComponent(workspaceId)}/outline-diagnostics`;
  const queryClient = useQueryClient();
  const storageKey = `outline-diagnostic:${workspaceId}`;
  const [diagnosticId, setDiagnosticId] = useState<string | null>(() => localStorage.getItem(storageKey));
  const [prepared, setPrepared] = useState<Diagnostic | null>(null);
  const [enteredId, setEnteredId] = useState("");
  const [taskId, setTaskId] = useState<string | null>(() => localStorage.getItem(`${storageKey}:task`));
  const prepare = useMutation({ mutationFn: () => apiPost<Diagnostic>(`${base}:prepare`),
    onSuccess: (data) => { setPrepared(data); setDiagnosticId(data.diagnostic_id); localStorage.setItem(storageKey, data.diagnostic_id); void queryClient.invalidateQueries({ queryKey: ["outline-diagnostic", workspaceId] }); } });
  const prepareMax = useMutation({ mutationFn: () => apiPost<Diagnostic>(`${base}:prepare-max-hard`),
    onSuccess: (data) => { setPrepared(data); setDiagnosticId(data.diagnostic_id); localStorage.setItem(storageKey, data.diagnostic_id); void queryClient.invalidateQueries({ queryKey: ["outline-diagnostic", workspaceId] }); } });
  const prepareSplit = useMutation({ mutationFn: () => apiPost<Diagnostic>(`${base}:prepare-split-hard`),
    onSuccess: (data) => { setPrepared(data); setDiagnosticId(data.diagnostic_id); localStorage.setItem(storageKey, data.diagnostic_id); void queryClient.invalidateQueries({ queryKey: ["outline-diagnostic", workspaceId] }); } });
  const task = useQuery({ queryKey: ["outline-diagnostic-task", taskId], enabled: !!taskId,
    queryFn: () => apiGet<Task>(`/tasks/${taskId}`),
    refetchInterval: (q) => q.state.data && ["done", "failed", "cancelled", "interrupted"].includes(q.state.data.status) ? false : 1500 });
  const state = useQuery({ queryKey: ["outline-diagnostic", workspaceId, diagnosticId], enabled: !!diagnosticId,
    queryFn: () => apiGet<Diagnostic>(`${base}/${diagnosticId}`),
    refetchInterval: task.data?.status === "running" || task.data?.status === "pending" ? 1500 : false });
  useEffect(() => {
    if (task.data && !["pending", "running"].includes(task.data.status)) {
      void queryClient.invalidateQueries({ queryKey: ["outline-diagnostic", workspaceId] });
    }
  }, [task.data, queryClient, workspaceId]);
  const data = state.data ?? prepared;
  const start = useMutation({ mutationFn: () => apiPost<Task>(`${base}/${data!.diagnostic_id}:start`, { request_hash: data!.request_hash }),
    onSuccess: (value) => { setTaskId(value.task_id); localStorage.setItem(`${storageKey}:task`, value.task_id); } });
  const cancel = useMutation({ mutationFn: () => apiPost<Task>(`/tasks/${taskId}:cancel`) });
  const active = !!taskId && (!task.data || ["pending", "running"].includes(task.data.status));
  const error = prepare.error ?? prepareMax.error ?? prepareSplit.error ?? start.error ?? cancel.error ?? state.error ?? task.error;
  return <details className="my-4 rounded border p-4"><summary>大纲审核与定点修复</summary>
    <p className="my-2 text-sm">完整审核和分包对照保留原文证据。已确认的硬矛盾可选择原文字段，生成候选补丁，复审通过后再写回。</p>
    <div className="flex flex-wrap gap-2">
      <Button onClick={() => prepare.mutate()} disabled={prepare.isPending || active}>准备分包对照（零调用）</Button>
      <Button onClick={() => prepareMax.mutate()} disabled={prepareMax.isPending || active}>准备最高强度完整审核（零调用）</Button>
      <Button onClick={() => prepareSplit.mutate()} disabled={prepareSplit.isPending || active}>准备五项拆分审核（零调用）</Button>
    </div>
    <div className="mt-2 flex gap-2"><input className="min-w-0 flex-1 rounded border px-2 py-1 text-sm" aria-label="已有审核编号" placeholder="已有审核编号 diagnostic_..." value={enteredId} onChange={event => setEnteredId(event.target.value)} />
      <Button variant="outline" disabled={!/^diagnostic_[0-9a-f]{64}$/.test(enteredId)} onClick={() => { setDiagnosticId(enteredId); setPrepared(null); localStorage.setItem(storageKey, enteredId); }}>读取已有审核</Button></div>
    {error && <p role="alert">{error instanceof Error ? error.message : "操作失败"}</p>}
    {data && <div className="mt-3 space-y-2">
      <p>{data.jobs.length} 个任务；最多 {data.physical_request_cap} 次物理请求；独立上限 ¥{data.cost_cap_cny}。</p>
      <p className="text-sm">候选消息保守预留 ¥{data.all_attempts_reserve_cny.toFixed(3)}，最终请求仍须经配置核对。</p>
      {!data.authorization_present && <p>已准备，尚未登记本次付费授权。</p>}
      <Button disabled={!data.authorization_present || active || start.isPending || data.jobs.every(j => ["completed", "invalid"].includes(j.status)) || data.jobs.some(j => j.status === "started")}
        onClick={() => start.mutate()}>开始／继续已授权诊断</Button>
      {active && <Button variant="outline" disabled={cancel.isPending} onClick={() => cancel.mutate()}>取消诊断</Button>}
      {task.data && <p>任务状态：{({ pending: "等待", running: "运行中", done: "已完成", failed: "未完成", interrupted: "已中断", cancelled: "已取消" })[task.data.status]}</p>}
      {task.data?.error && <p role="alert">{String(task.data.error.message ?? "诊断未完成")}</p>}
      {data.jobs.map(j => <div className="rounded border p-2" key={j.job_id}>
        <p>{j.job_id === "full" ? "完整大纲对照" : `局部审核：${reviewNames[j.job_id] ?? j.job_id}`}：{({ pending: "尚未调用", started: "已启动，等待确认结果", completed: "已收到可解析结果", invalid: "结果校验不合格" })[j.status]}</p>
        {j.error && <p>{j.error}</p>}
        {j.result != null && <details><summary>查看审核结果</summary><ReviewResult value={j.result} /></details>}
      </div>)}
      <p className="text-sm">引用校验只证明原文存在。是否误报、漏检仍需阅读结果，不能据此宣布全书通过。</p>
      {(data.jobs.length === 1 && data.jobs[0]?.status === "completed" ||
        data.jobs.length === 5 && data.jobs.some(j => j.status === "completed" && !!j.result && typeof j.result === "object" && !Array.isArray(j.result) && (j.result as Record<string, unknown>).outcome === "conflict")) &&
        <OutlineRepairPanel workspaceId={workspaceId} diagnostic={data} />}
    </div>}
  </details>;
}
