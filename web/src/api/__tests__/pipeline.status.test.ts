import { describe, expect, it } from "vitest";

import {
  PIPELINE_RUNNING_REFETCH_INTERVAL_MS,
  pipelineStatusRefetchInterval,
} from "@/api/pipeline";

describe("pipeline status 轮询兜底", () => {
  it("只在 cached 状态仍为 running 时低频对账", () => {
    expect(pipelineStatusRefetchInterval("running")).toBe(
      PIPELINE_RUNNING_REFETCH_INTERVAL_MS,
    );
    expect(pipelineStatusRefetchInterval("paused")).toBe(false);
    expect(pipelineStatusRefetchInterval("error")).toBe(false);
    expect(pipelineStatusRefetchInterval("stopped")).toBe(false);
    expect(pipelineStatusRefetchInterval(undefined)).toBe(false);
  });
});
