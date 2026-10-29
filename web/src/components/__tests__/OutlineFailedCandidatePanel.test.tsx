import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { OutlineFailedCandidatePanel } from "@/components/OutlineFailedCandidatePanel";
import { apiGet, apiPost } from "@/api/client";
vi.mock("@/api/client", () => ({ apiGet: vi.fn(), apiPost: vi.fn() }));
const storageKey = "outline-failed-repair:test:candidate";
const jobs = ["world", "characters", "plot", "volume_plans", "cross"].map(job_id => ({
  job_id, status: "completed", result: { outcome: "compatible" }, input_upper_bound: 10, error: null,
}));
function mount(reviewJobs = jobs, status = "staged", authorized = true, metadata = {}) {
  localStorage.setItem(storageKey, "repair_test");
  if (status === "staged") localStorage.setItem(`${storageKey}:review`, "diagnostic_test");
  vi.mocked(apiGet).mockImplementation(async path => {
    if (path.includes("outline-failed-candidates/")) return { candidate_id: "candidate", source_hash: "s",
      review: { contradictions: [{ desc: "测试冲突", reason: "两端相反", source_quotes: [{ path: "world.power_system", quote: "原文" }] }], coverage_gaps: [], repair_scope: "upstream" } };
    if (path.includes("outline-diagnostics/")) return { jobs: reviewJobs, authorization_present: true };
    if (path.startsWith("/tasks/")) return { status: "done" };
    return { repair_id: "repair_test", status, authorization_present: authorized, physical_request_cap: 1, cost_cap_cny: 5, ...metadata };
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><OutlineFailedCandidatePanel workspaceId="test" candidateId="candidate" /></QueryClientProvider>);
}
beforeEach(() => {
  vi.clearAllMocks();
  const values = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (k: string) => values.get(k) ?? null,
    setItem: (k: string, v: string) => { values.set(k, v); }, removeItem: (k: string) => values.delete(k) });
});
describe("隔离候选复审交接", () => {
  it("补丁与复审终态停止轮询，不重复查询已结束的任务", async () => {
    vi.useFakeTimers();
    try {
      localStorage.setItem(`${storageKey}:patch-task`, "patch_done");
      localStorage.setItem(`${storageKey}:review-task`, "review_done");
      mount();
      await act(async () => { await vi.advanceTimersByTimeAsync(100); });
      expect(screen.getByRole("button", { name: "写回通过复审的候选" })).toBeEnabled();
      const count = vi.mocked(apiGet).mock.calls.length;
      await act(async () => { await vi.advanceTimersByTimeAsync(4500); });
      expect(apiGet).toHaveBeenCalledTimes(count);
    } finally {
      vi.useRealTimers();
    }
  });
  it.each(["pending", "invalid"])("%s 的后续补丁调整仍使用冻结新冲突与前轮来源", async status => {
    mount(jobs, status, false, {
      parent_repair_id: "parent_repair", parent_review_id: "parent_review",
      source_conflicts: [{ desc: "冻结的主线冲突", reason: "主线两端相反",
        source_quotes: [{ path: "plot.core_conflict", quote: "冻结原文" }] }],
    });
    vi.mocked(apiPost).mockResolvedValue({ repair_id: "adjusted_repair" });
    await screen.findByText("冻结的主线冲突");
    expect(screen.queryByText("测试冲突")).toBeNull();
    fireEvent.click(screen.getByLabelText("修改原文：plot.core_conflict"));
    fireEvent.change(screen.getByRole("textbox", { name: "全部冲突的修复方向" }), { target: { value: "调整该轮主线修法" } });
    fireEvent.click(screen.getByRole("button", { name: "准备隔离候选补丁（零调用）" }));
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith(expect.stringContaining(":prepare-repair"), {
      conflict_indexes: [0], allowed_paths: ["plot.core_conflict"], direction: "调整该轮主线修法",
      parent_repair_id: "parent_repair", parent_review_id: "parent_review",
    }));
  });
  it("五项复审全部通过才出现正式写回按钮", async () => {
    mount();
    expect(await screen.findByRole("button", { name: "写回通过复审的候选" })).toBeEnabled();
  });
  it.each(["unknown", "conflict"])("跨段结论 %s 不开放写回", async outcome => {
    mount(jobs.map(job => job.job_id === "cross" ? { ...job, result: { outcome } } : job));
    await screen.findByText("复审未完整通过，不能写回。");
    expect(screen.queryByRole("button", { name: "写回通过复审的候选" })).toBeNull();
  });
  it("漏返回一项不能算完整通过", async () => {
    mount(jobs.slice(0, 4));
    await screen.findByText("复审未完整通过，不能写回。");
    expect(screen.queryByRole("button", { name: "写回通过复审的候选" })).toBeNull();
  });
  it("尚未登记付费授权的方案可重新准备", async () => {
    mount(jobs, "pending", false);
    expect(await screen.findByRole("textbox", { name: "全部冲突的修复方向" })).toBeInTheDocument();
  });
  it("已授权待发方案不允许改写", async () => {
    mount(jobs, "pending", true);
    await screen.findByRole("button", { name: "生成已授权补丁" });
    expect(screen.queryByRole("textbox", { name: "全部冲突的修复方向" })).toBeNull();
  });
  it("复审仍在进行时不把已有完成项误认成整轮失败", async () => {
    mount(jobs.map(job => job.job_id === "world" ? job : { ...job, status: "started" }));
    await screen.findByRole("button", { name: "恢复复审结果" });
    expect(screen.queryByRole("textbox", { name: "全部冲突的修复方向" })).toBeNull();
  });

  it("下一轮只按本輪新冲突选择字段，并携带前轮候选和复审", async () => {
    const reviewJobs = jobs.map(job => job.job_id === "plot" ? { ...job, result: { outcome: "conflict",
      contradictions: [{ desc: "主线新冲突", reason: "同一事件两次", source_quotes: [{ path: "plot.core_conflict", quote: "新原文" }] }] } } : job);
    vi.mocked(apiPost).mockResolvedValue({ repair_id: "next_repair" });
    mount(reviewJobs);
    await screen.findByText("主线新冲突");
    expect(screen.queryByText("测试冲突")).toBeNull();
    fireEvent.click(screen.getByLabelText("修改原文：plot.core_conflict"));
    fireEvent.change(screen.getByRole("textbox", { name: "全部冲突的修复方向" }), { target: { value: "只修新主线冲突" } });
    fireEvent.click(screen.getByRole("button", { name: "在已修候选上准备下一轮补丁（零调用）" }));
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith(expect.stringContaining(":prepare-repair"), {
      conflict_indexes: [0], allowed_paths: ["plot.core_conflict"], direction: "只修新主线冲突",
      parent_repair_id: "repair_test", parent_review_id: "diagnostic_test",
    }));
  });

});
