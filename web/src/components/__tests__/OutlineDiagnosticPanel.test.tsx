import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { OutlineDiagnosticPanel } from "@/components/OutlineDiagnosticPanel";
import { apiGet, apiPost } from "@/api/client";

vi.mock("@/api/client", () => ({ apiGet: vi.fn(), apiPost: vi.fn() }));
const data = { diagnostic_id: "diagnostic_test", request_hash: "a".repeat(64), source_hash: "b".repeat(64),
  authorization_present: false, first_attempt_reserve_cny: 9.5, all_attempts_reserve_cny: 28.5,
  cost_cap_cny: 29, physical_request_cap: 9, full_book_verdict: false,
  jobs: ["full", "history-001", "history-002"].map(job_id => ({ job_id, status: "pending", input_upper_bound: 100, result: null, error: null })) };
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><OutlineDiagnosticPanel workspaceId="test" /></QueryClientProvider>);
}
beforeEach(() => {
  vi.clearAllMocks();
  const values = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
    clear: () => values.clear(),
  });
});
describe("只读诊断", () => {
  it.each([
    ["准备分包对照（零调用）", "prepare"],
    ["准备最高强度完整审核（零调用）", "prepare-max-hard"],
    ["准备五项拆分审核（零调用）", "prepare-split-hard"],
  ])("%s 不启动付费任务，缺少独立授权时按钮禁用", async (label, endpoint) => {
    vi.mocked(apiPost).mockResolvedValue(data); vi.mocked(apiGet).mockResolvedValue(data);
    mount();
    expect(apiPost).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText(label));
    await screen.findByText("已准备，尚未登记本次付费授权。");
    expect(screen.getByText("开始／继续已授权诊断")).toBeDisabled();
    expect(apiPost).toHaveBeenCalledTimes(1);
    expect(apiPost).toHaveBeenCalledWith(`/workspaces/test/outline-diagnostics:${endpoint}`);
    expect(screen.getByText(/独立上限 ¥29/)).toBeInTheDocument();
  });
  it("刷新可恢复独立任务并取消，不重发审核", async () => {
    localStorage.setItem("outline-diagnostic:test", data.diagnostic_id);
    localStorage.setItem("outline-diagnostic:test:task", "task_old");
    vi.mocked(apiGet).mockImplementation(async path => path.startsWith("/tasks/") ? { task_id: "task_old", status: "running" } : data);
    vi.mocked(apiPost).mockResolvedValue({ task_id: "task_old", status: "cancelled" });
    mount();
    const cancel = await screen.findByText("取消诊断");
    fireEvent.click(cancel);
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith("/tasks/task_old:cancel"));
    expect(apiPost).toHaveBeenCalledTimes(1);
  });
  it("状态不明时不开放重发", async () => {
    vi.mocked(apiPost).mockResolvedValue({ ...data, authorization_present: true, jobs: [{ ...data.jobs[0], status: "started" }] });
    vi.mocked(apiGet).mockResolvedValue({ ...data, authorization_present: true, jobs: [{ ...data.jobs[0], status: "started" }] });
    mount(); fireEvent.click(screen.getByText("准备分包对照（零调用）"));
    await screen.findByText(/已启动，等待确认结果/);
    expect(screen.getByText("开始／继续已授权诊断")).toBeDisabled();
  });
  it("模型证据用可读文字显示", async () => {
    const result = { ...data, jobs: [{ ...data.jobs[0], status: "completed", result: {
      outcome: "conflict", summary: "两处时间写法互斥", findings: [{ reason: "同一事件被安排在不同时间", source_quotes: [
        { path: "/plot/a", quote: "开篇已完成" }, { path: "/volume/a", quote: "未来第一次完成" },
      ] }], requested_paths: [],
    } }] };
    vi.mocked(apiPost).mockResolvedValue(result); vi.mocked(apiGet).mockResolvedValue(result);
    mount(); fireEvent.click(screen.getByText("准备分包对照（零调用）"));
    fireEvent.click(await screen.findByText("查看审核结果"));
    expect(screen.getByText("两处时间写法互斥")).toBeInTheDocument();
    expect(screen.getByText("“开篇已完成”")).toBeInTheDocument();
    expect(screen.queryByText(/source_quotes/)).not.toBeInTheDocument();
  });
});
