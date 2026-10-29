import { act, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const wsHarness = vi.hoisted(() => ({
  listeners: new Map<string, (frame: unknown) => void>(),
  subscribe: vi.fn(),
}));

vi.mock("@/ws/client", () => ({
  getWsClient: () => ({ subscribe: wsHarness.subscribe }),
}));

vi.mock("@/hooks/useChapterEvents", () => ({
  useChapterEvents: vi.fn(),
}));

import { PipelineLiveOutput } from "@/components/PipelineLiveOutput";

describe("PipelineLiveOutput 生命周期对账", () => {
  beforeEach(() => {
    wsHarness.listeners.clear();
    wsHarness.subscribe.mockReset();
    wsHarness.subscribe.mockImplementation(
      (channel: string, listener: (frame: unknown) => void) => {
        wsHarness.listeners.set(channel, listener);
        return vi.fn();
      },
    );
  });

  it("富记忆暂停会刷新权威状态、显示告警并提供章节入口", () => {
    const onStatusChanged = vi.fn();
    render(
      <MemoryRouter>
        <PipelineLiveOutput
          workspaceId="ws-demo"
          currentChapterId="ch-0003"
          onPipelineStatusChanged={onStatusChanged}
        />
      </MemoryRouter>,
    );

    const listener = wsHarness.listeners.get("pipeline.events");
    expect(listener).toBeDefined();
    act(() => {
      listener?.({
        channel: "pipeline.events",
        event: "pipeline_paused",
        timestamp: "2026-08-17T00:00:00Z",
        data: {
          workspace_id: "ws-demo",
          reason: "memory_enrichment_failed",
          chapter_id: "ch-0003",
          current_chapter: "ch-0003",
        },
      });
    });

    expect(onStatusChanged).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("pipeline-output-list")).toHaveTextContent(
      "memory_enrichment_failed",
    );
    expect(screen.getByTestId("pipeline-jump-editor")).toHaveAttribute(
      "href",
      "/chapters/ch-0003",
    );
  });

  it.each([
    "pipeline_started",
    "pipeline_resumed",
    "pipeline_stopped",
  ])("%s 都会触发状态重读", (event) => {
    const onStatusChanged = vi.fn();
    render(
      <MemoryRouter>
        <PipelineLiveOutput
          workspaceId="ws-demo"
          currentChapterId={null}
          onPipelineStatusChanged={onStatusChanged}
        />
      </MemoryRouter>,
    );

    const listener = wsHarness.listeners.get("pipeline.events");
    act(() => {
      listener?.({
        channel: "pipeline.events",
        event,
        timestamp: "2026-08-17T00:00:00Z",
        data: { workspace_id: "ws-demo" },
      });
    });

    expect(onStatusChanged).toHaveBeenCalledTimes(1);
  });

  it("其他工作区事件不会污染当前页面或触发刷新", () => {
    const onStatusChanged = vi.fn();
    render(
      <MemoryRouter>
        <PipelineLiveOutput
          workspaceId="ws-demo"
          currentChapterId={null}
          onPipelineStatusChanged={onStatusChanged}
        />
      </MemoryRouter>,
    );

    const listener = wsHarness.listeners.get("pipeline.events");
    act(() => {
      listener?.({
        channel: "pipeline.events",
        event: "pipeline_paused",
        timestamp: "2026-08-17T00:00:00Z",
        data: {
          workspace_id: "ws-other",
          reason: "memory_enrichment_failed",
        },
      });
    });

    expect(onStatusChanged).not.toHaveBeenCalled();
    expect(screen.getByText(/等待 WS 推送/)).toBeInTheDocument();
  });
});
