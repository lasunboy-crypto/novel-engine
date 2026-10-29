/**
 * useCancelTaskMutation 单测(v1.1 w4-s6 = J1)。
 *
 * 验证:① 调 `POST /tasks/{id}:cancel`(taskId 经 encodeURIComponent);② 成功后
 * invalidate `["task", taskId]` 让 useTaskInfoQuery 重拉。mock `@/api/client` 的
 * apiPost,不真发请求。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";

const apiPostMock = vi.fn();

vi.mock("@/api/client", () => ({
  apiGet: vi.fn(),
  apiPost: (...args: unknown[]) => apiPostMock(...args),
}));

import { useCancelTaskMutation, TASK_QUERY_KEY } from "@/api/tasks";

function makeWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
  };
}

describe("useCancelTaskMutation", () => {
  beforeEach(() => {
    apiPostMock.mockReset();
  });

  it("POSTs to /tasks/{id}:cancel and returns TaskInfo", async () => {
    apiPostMock.mockResolvedValue({ task_id: "task_abc", status: "running" });
    const client = new QueryClient();
    const { result } = renderHook(() => useCancelTaskMutation(), {
      wrapper: makeWrapper(client),
    });

    result.current.mutate("task_abc");

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(apiPostMock).toHaveBeenCalledWith("/tasks/task_abc:cancel");
    expect(result.current.data).toEqual({
      task_id: "task_abc",
      status: "running",
    });
  });

  it("encodeURIComponent escapes the task id in the path", async () => {
    apiPostMock.mockResolvedValue({ task_id: "a/b", status: "running" });
    const client = new QueryClient();
    const { result } = renderHook(() => useCancelTaskMutation(), {
      wrapper: makeWrapper(client),
    });

    result.current.mutate("a/b");

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(apiPostMock).toHaveBeenCalledWith("/tasks/a%2Fb:cancel");
  });

  it("invalidates the task query on success", async () => {
    apiPostMock.mockResolvedValue({ task_id: "task_xyz", status: "running" });
    const client = new QueryClient();
    const spy = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useCancelTaskMutation(), {
      wrapper: makeWrapper(client),
    });

    result.current.mutate("task_xyz");

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(spy).toHaveBeenCalledWith({ queryKey: TASK_QUERY_KEY("task_xyz") });
  });
});
