export type ParallelTaskState =
  | "queued"
  | "running"
  | "succeeded"
  | "failed";

export interface ParallelTaskView {
  subtask_id: string;
  status: ParallelTaskState;
  chapter_id?: string;
  chapter_number?: number;
  beat_id?: string;
  judge?: string;
  reason?: string;
}

export interface ParallelTaskSnapshot {
  parallel_group: string;
  max_concurrency: number;
  volume_id?: string;
  chapter_id?: string;
  tasks: ParallelTaskView[];
}

export function parseParallelTaskSnapshot(
  value: unknown,
  expectedGroup: string,
): ParallelTaskSnapshot | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  if (
    record.parallel_group !== expectedGroup ||
    !Number.isInteger(record.max_concurrency) ||
    !Array.isArray(record.tasks)
  ) {
    return null;
  }
  const allowed = new Set<ParallelTaskState>([
    "queued",
    "running",
    "succeeded",
    "failed",
  ]);
  if (
    !record.tasks.every((task) => {
      if (!task || typeof task !== "object") return false;
      const candidate = task as Record<string, unknown>;
      return (
        typeof candidate.subtask_id === "string" &&
        typeof candidate.status === "string" &&
        allowed.has(candidate.status as ParallelTaskState)
      );
    })
  ) {
    return null;
  }
  return record as unknown as ParallelTaskSnapshot;
}
