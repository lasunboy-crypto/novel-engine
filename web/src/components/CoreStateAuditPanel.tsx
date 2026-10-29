import { useCoreStateAuditQuery } from "@/api/core_state";
import type { ChapterCardOut } from "@/api/chapters";

const BOOK_TIME_KIND_LABELS: Record<string, string> = {
  exact: "明确时间点 / 明确数值时长",
  bounded_interval: "原文明示闭区间",
  ordered_relative: "有序自然时间关系",
  genuinely_unanchored: "真正未锚定片段",
};

const BOOK_TIME_RELATION_LABELS: Record<string, string> = {
  same_instant: "同一时刻",
  after: "晚于上一主线节点",
  same_narrative_day: "同一叙事日",
  same_night: "同一叙事夜",
  next_narrative_day: "下一叙事日",
  resume_anchor: "恢复到最近明确主线节点",
};

const BOOK_TIME_PHASE_LABELS: Record<string, string> = {
  morning: "清晨 / 早晨",
  daytime: "白天",
  afternoon: "午后",
  night: "夜间",
  around_midnight: "零点前后",
  pre_dawn: "黎明前",
};

function formatBookTime(input: {
  kind?: string | null;
  rawTimeGap: string;
  relations?: string[] | null;
  dayPhase?: string | null;
  approximate?: { value: number; unit: string } | null;
}) {
  const details = [BOOK_TIME_KIND_LABELS[input.kind ?? ""] ?? input.kind ?? "未知类型"];
  if (input.relations?.length) {
    details.push(
      input.relations
        .map((relation) => BOOK_TIME_RELATION_LABELS[relation] ?? relation)
        .join("、"),
    );
  }
  if (input.dayPhase) {
    details.push(`时段 ${BOOK_TIME_PHASE_LABELS[input.dayPhase] ?? input.dayPhase}`);
  }
  if (input.approximate) details.push("近似量仅作语义提示，不生成分钟上下界");
  return `${input.rawTimeGap}（${details.join("；")}）`;
}

type CoreStateCardView = Pick<
  ChapterCardOut,
  | "core_state_contract_version"
  | "core_state_projection_hash"
  | "core_state_transitions"
  | "book_clock_constraint"
>;

export function CoreStateAuditPanel(props: {
  workspaceId: string;
  card?: CoreStateCardView;
}) {
  const query = useCoreStateAuditQuery(props.workspaceId);

  if (query.isPending) {
    return (
      <section className="rounded border border-border p-4 text-sm text-muted-foreground">
        加载核心状态审计账本…
      </section>
    );
  }
  if (query.isError || !query.data) {
    return (
      <section
        data-testid="core-state-audit-error"
        className="rounded border border-destructive bg-destructive/10 p-4 text-sm text-destructive"
      >
        核心状态审计账本加载失败。
      </section>
    );
  }

  const { projection } = query.data;
  const items = query.data.items ?? [];
  const candidates = items.filter((item) => item.item_type === "candidate");
  const history = items.filter((item) => item.item_type !== "candidate");
  const cardTransitions = props.card?.core_state_transitions ?? [];
  const typedBookTime =
    (projection.book_time_contract_version ?? 0) === 1
      ? projection.book_time?.current_node
      : undefined;
  const cardClock = props.card?.book_clock_constraint;

  return (
    <section
      data-testid="core-state-audit-panel"
      className="space-y-4 rounded border border-border bg-card p-4"
    >
      <div>
        <h2 className="text-sm font-semibold">全书核心状态与书内时钟</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          这里是批准正文形成的当前权威，不是第二套剧情规划。新章节卡只读取当前投影，
          任何变化仍须先在卡片中声明 from→to，再由批准正文精确证据落账。
        </p>
      </div>

      <div className="rounded bg-muted/40 p-3 text-xs">
        <p>
          投影 hash：
          <code className="break-all">{projection.projection_hash}</code>
        </p>
        <p className="mt-1">
          主时间线：
          {typedBookTime
            ? formatBookTime({
                kind: typedBookTime.kind,
                rawTimeGap: typedBookTime.raw_time_gap,
                relations: typedBookTime.relations,
                dayPhase: typedBookTime.day_phase,
                approximate: typedBookTime.approximate_quantity,
              })
            : projection.book_clock
            ? `${projection.book_clock.minimum_minutes}–${projection.book_clock.maximum_minutes} 分钟（相对开篇）`
            : "未知 / SOFT（不会伪造累计）"}
        </p>
        {typedBookTime && projection.book_time?.latest_absolute_anchor && (
          <p className="mt-1">最近明确数值锚：已保留；ordered / unanchored 不会清空。</p>
        )}
      </div>

      {props.card && (
        <div className="rounded border border-sky-200 bg-sky-50 p-3 text-xs text-sky-900">
          <h3 className="font-medium">本章卡冻结合同</h3>
          {(props.card.core_state_contract_version ?? 0) === 0 ? (
            <p className="mt-1">legacy v0 / 本章未启用核心状态 HARD。</p>
          ) : (
            <div className="mt-1 space-y-1">
              <p>
                投影：
                <code className="break-all">
                  {props.card.core_state_projection_hash ?? "（缺失）"}
                </code>
              </p>
              <p>
                本章时钟：
                {cardClock?.contract_version === 1
                  ? formatBookTime({
                      kind: cardClock.kind,
                      rawTimeGap: cardClock.time_gap,
                      relations: cardClock.relations,
                      dayPhase: cardClock.day_phase,
                      approximate: cardClock.approximate_quantity,
                    })
                  : cardClock?.is_hard && cardClock.current_interval
                    ? `${cardClock.current_interval.minimum_minutes}–${cardClock.current_interval.maximum_minutes} 分钟`
                    : `SOFT（${cardClock?.soft_reason || "未建立"}）`}
              </p>
              <div>
                获准变化（{cardTransitions.length}）：
                {cardTransitions.length === 0 ? (
                  <span>无</span>
                ) : (
                  <ul className="mt-1 list-disc space-y-1 pl-5">
                    {cardTransitions.map((transition) => (
                      <li key={transition.state_key}>
                        {transition.state_key}：{transition.from_value ?? "（未建立）"} →{" "}
                        {transition.to_value}（{transition.reason}）
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          )}
        </div>
      )}

      <div className="space-y-2">
        <h3 className="text-xs font-medium">当前有效事实（{projection.facts.length}）</h3>
        {projection.facts.length === 0 ? (
          <p className="text-xs text-muted-foreground">尚无已准入核心事实。</p>
        ) : (
          projection.facts.map((fact) => (
            <div
              key={fact.revision_id}
              className="rounded border border-border p-2 text-xs"
            >
              <p className="font-medium">
                {fact.entity_id}.{fact.attribute} = {fact.value}
              </p>
              <p className="mt-1 text-muted-foreground">
                生效 ch-{String(fact.valid_from_chapter).padStart(4, "0")} · 来源{" "}
                {fact.source_chapter_id}/v{fact.source_version_id}
              </p>
              <p className="mt-1">证据：“{fact.evidence_quote}”</p>
            </div>
          ))
        )}
      </div>

      <p className="rounded border border-amber-200 bg-amber-50 p-3 text-[11px] text-amber-800">
        若批准来源本身有误，请从对应章节执行 Rewind；对象、角色和书内时钟统一撤回，
        避免只改一半因果链。
      </p>

      {history.length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer">Revision / 来源历史（{history.length}）</summary>
          <div className="mt-2 space-y-2">
            {history.map((item, index) => (
              <div
                key={`${item.revision_id ?? item.state_key}-${index}`}
                className="rounded border p-2"
              >
                <p>
                  {item.state_key} = {item.value} · {item.status}
                </p>
                <p className="text-muted-foreground">
                  {item.source_chapter_id}/v{item.source_version_id} · 生效 ch-
                  {String(item.valid_from_chapter).padStart(4, "0")}
                  {item.valid_to_chapter
                    ? ` 至 ch-${String(item.valid_to_chapter).padStart(4, "0")}`
                    : " 至今"}
                </p>
                <p>证据：“{item.evidence_quote}”</p>
              </div>
            ))}
          </div>
        </details>
      )}

      {candidates.length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer">
            未准入 / 冲突候选（{candidates.length}）
          </summary>
          <div className="mt-2 space-y-2">
            {candidates.map((item, index) => (
              <div key={`${item.state_key}-${index}`} className="rounded border p-2">
                <p>
                  {item.state_key} = {item.value} · {item.status}
                </p>
                <p className="text-muted-foreground">{item.reason}</p>
              </div>
            ))}
          </div>
        </details>
      )}
    </section>
  );
}
