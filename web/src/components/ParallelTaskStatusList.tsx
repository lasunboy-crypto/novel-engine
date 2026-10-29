import type {
  ParallelTaskSnapshot,
  ParallelTaskState,
  ParallelTaskView,
} from "@/lib/parallelTaskStatus";

const STATUS_LABEL: Record<ParallelTaskState, string> = {
  queued: "等待",
  running: "运行中",
  succeeded: "成功",
  failed: "失败",
};

const STATUS_CLASS: Record<ParallelTaskState, string> = {
  queued: "border-slate-300 bg-slate-50 text-slate-700",
  running: "border-sky-300 bg-sky-50 text-sky-800",
  succeeded: "border-emerald-300 bg-emerald-50 text-emerald-800",
  failed: "border-rose-300 bg-rose-50 text-rose-800",
};

const REASON_LABEL: Record<string, string> = {
  technical_fail_open: "技术失败，按既有降级规则继续",
  semantic_failure: "语义校验未通过",
  empty_contract_skipped: "空合同，无需调用",
  not_run_after_earlier_failure: "前序失败，未启动",
};

function taskLabel(task: ParallelTaskView): string {
  if (task.judge === "setting") return "世界设定 Judge";
  if (task.judge === "must_vs_forbidden") return "必达/禁止 Judge";
  const chapter = task.chapter_id ?? task.subtask_id;
  return task.beat_id ? `${chapter} · ${task.beat_id}` : chapter;
}

interface ParallelTaskStatusListProps {
  title: string;
  snapshot: ParallelTaskSnapshot;
  testId: string;
}

export function ParallelTaskStatusList(props: ParallelTaskStatusListProps) {
  const { snapshot } = props;
  return (
    <section
      className="space-y-2 rounded border border-border bg-muted/20 p-3"
      data-testid={props.testId}
    >
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
        <span className="font-medium">{props.title}</span>
        <span className="text-muted-foreground">
          {snapshot.volume_id ? `${snapshot.volume_id} · ` : ""}
          并发上限 {snapshot.max_concurrency}
        </span>
      </div>
      <ul className="grid gap-1.5 sm:grid-cols-2">
        {snapshot.tasks.map((task) => (
          <li
            key={task.subtask_id}
            className="flex min-w-0 items-center justify-between gap-2 rounded border border-border bg-background px-2 py-1.5 text-xs"
            data-testid={`parallel-task-${task.subtask_id}`}
          >
            <span className="min-w-0 truncate" title={taskLabel(task)}>
              {taskLabel(task)}
            </span>
            <span className="flex shrink-0 items-center gap-1.5">
              {task.reason && (
                <span
                  className="max-w-48 truncate text-muted-foreground"
                  title={REASON_LABEL[task.reason] ?? task.reason}
                >
                  {REASON_LABEL[task.reason] ?? task.reason}
                </span>
              )}
              <span
                className={`rounded border px-1.5 py-0.5 ${STATUS_CLASS[task.status]}`}
              >
                {STATUS_LABEL[task.status]}
              </span>
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
