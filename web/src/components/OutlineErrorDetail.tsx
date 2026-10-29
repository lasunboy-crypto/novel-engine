/**
 * OutlineErrorDetail(v2 W3.5 UI 缺口1 + 缺口2):大纲生成 / 提交失败的结构化错误展示。
 *
 * 把后端结构化错误(原落通用 INTERNAL_ERROR / 被 toast 吞 details)列清楚,替代
 * 单行 toast,让用户看懂"哪里冲突、为什么、怎么修":
 * - `OUTLINE_CONSISTENCY_CONFLICT`(缺口1):逐条列跨段矛盾 + 依据,红字高亮涉及实体名。
 * - `OUTLINE_ARC_TOO_SHORT` / `OUTLINE_VOLUME_SEAM_REPLAY`(缺口2):逐条列结构闸违规
 *   (哪卷哪弧 / 哪两章),红字高亮实体名。
 * - `OUTLINE_BUILD_FAILED`:某段 LLM 输出失败,提示哪段可重新生成。
 * - 其它:回退单行 message。
 *
 * 纯展示组件;`names` 来自调用方(大纲角色 / 卷 名),供 highlightNames 子串高亮。
 * "重新生成 / 重做"按钮由调用方在外层渲染(本组件只负责把冲突说清楚)。
 */

import { highlightNames } from "@/lib/highlightNames";

export interface OutlineErrorLike {
  code?: string;
  message?: string;
  details?: Record<string, unknown> | null;
}

interface OutlineErrorDetailProps {
  error: OutlineErrorLike;
  /** 已知实体名(角色 / 卷 / 弧),供红字高亮。可空(降级为纯文字)。 */
  names?: string[];
}

interface ViolationRow {
  volume_id?: string;
  arc_id?: string;
  arc_type?: string;
  chapter_range?: string;
  span_length?: number;
  prev_chapter_id?: string;
  next_chapter_id?: string;
  confidence?: number;
  evidence?: string;
}

interface ConflictDetail {
  reason?: string;
  source_quotes?: { path?: string; quote?: string }[];
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((v): v is string => typeof v === "string")
    : [];
}

export function OutlineErrorDetail(props: OutlineErrorDetailProps) {
  const { error, names = [] } = props;
  const code = error.code ?? "";
  const details = (error.details ?? {}) as Record<string, unknown>;

  // ---- 缺口1:大纲跨段一致性硬伤 ----
  if (code === "OUTLINE_CONSISTENCY_CONFLICT") {
    const contradictions = asStringArray(details.contradictions);
    const conflictDetails = Array.isArray(details.contradiction_details)
      ? (details.contradiction_details as ConflictDetail[])
      : [];
    const coverageGaps = asStringArray(details.coverage_gaps);
    const evidence =
      typeof details.evidence === "string" ? details.evidence : "";
    return (
      <section
        data-testid="outline-error-consistency"
        className="rounded border border-destructive bg-destructive/10 p-3 text-sm"
      >
        <p className="font-medium text-destructive">大纲跨段一致性冲突</p>
        <p className="mt-1 text-xs text-muted-foreground">
          审核发现不能同时成立的事实，已在出卡前拦截。
          {typeof details.candidate_id === "string"
            ? " 下方可查看隔离候选并准备定点修复。"
            : " 请核对原文后修复对应段。"}
        </p>
        {contradictions.length > 0 ? (
          <ul className="mt-2 space-y-2" data-testid="outline-contradictions">
            {contradictions.map((c, i) => (
              <li
                key={i}
                data-testid="outline-contradiction"
                className="rounded border border-border bg-card p-2 text-foreground"
              >
                {highlightNames(c, names)}
                {typeof details.candidate_id !== "string" && conflictDetails[i] && (
                  <div className="mt-1 space-y-1 text-xs text-muted-foreground">
                    {conflictDetails[i].reason && <p>{conflictDetails[i].reason}</p>}
                    {conflictDetails[i].source_quotes?.map((item, quoteIndex) =>
                      typeof item.quote === "string" && typeof item.path === "string" ? (
                        <p key={quoteIndex}>原文 {item.path}：“{highlightNames(item.quote, names)}”</p>
                      ) : null)}
                  </div>
                )}
              </li>
            ))}
          </ul>
        ) : null}
        {evidence ? (
          <p className="mt-2 text-xs text-muted-foreground">
            判定依据：{highlightNames(evidence, names)}
          </p>
        ) : null}
        {coverageGaps.length > 0 ? (
          <div className="mt-2">
            <p className="text-xs font-medium text-foreground">覆盖提醒</p>
            <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-muted-foreground">
              {coverageGaps.map((g, i) => (
                <li key={i}>{highlightNames(g, names)}</li>
              ))}
            </ul>
          </div>
        ) : null}
      </section>
    );
  }

  // ---- 缺口2:确认期结构闸违规(arc 太短 / 卷界重演) ----
  if (
    code === "OUTLINE_ARC_TOO_SHORT" ||
    code === "OUTLINE_VOLUME_SEAM_REPLAY"
  ) {
    const violations = Array.isArray(details.violations)
      ? (details.violations as ViolationRow[])
      : [];
    const isArcTooShort = code === "OUTLINE_ARC_TOO_SHORT";
    return (
      <section
        data-testid="outline-error-structure-gate"
        className="rounded border border-destructive bg-destructive/10 p-3 text-sm"
      >
        <p className="font-medium text-destructive">
          {isArcTooShort
            ? "结构闸:副本类弧段章数不足(需 ≥ 3 章)"
            : "结构闸:新卷首章重演了前卷末章已完结的事件"}
        </p>
        {violations.length > 0 ? (
          <ul className="mt-2 space-y-2" data-testid="outline-violations">
            {violations.map((v, i) => (
              <li
                key={i}
                data-testid="outline-violation"
                className="rounded border border-border bg-card p-2 text-foreground"
              >
                {isArcTooShort ? (
                  <span>
                    {v.volume_id ? <>卷 {highlightNames(v.volume_id, names)} · </> : null}
                    弧 {highlightNames(v.arc_id ?? "?", names)}
                    {v.arc_type ? `(${v.arc_type})` : ""}
                    {v.chapter_range ? ` · 章节范围 ${v.chapter_range}` : ""}
                    {typeof v.span_length === "number"
                      ? ` · 仅 ${v.span_length} 章`
                      : ""}
                  </span>
                ) : (
                  <span>
                    {highlightNames(v.prev_chapter_id ?? "?", names)} →{" "}
                    {highlightNames(v.next_chapter_id ?? "?", names)}
                    {typeof v.confidence === "number"
                      ? `(置信 ${(v.confidence * 100).toFixed(0)}%)`
                      : ""}
                    {v.evidence ? (
                      <span className="mt-1 block text-xs text-muted-foreground">
                        依据：{highlightNames(v.evidence, names)}
                      </span>
                    ) : null}
                  </span>
                )}
              </li>
            ))}
          </ul>
        ) : null}
      </section>
    );
  }

  // ---- 大纲某段 LLM 输出失败 ----
  if (code === "OUTLINE_BUILD_FAILED") {
    const stage =
      typeof details.stage === "string" ? details.stage : undefined;
    return (
      <section
        data-testid="outline-error-build-failed"
        className="rounded border border-destructive bg-destructive/10 p-3 text-sm"
      >
        <p className="font-medium text-destructive">
          大纲生成失败{stage ? `(${stage} 段)` : ""}
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          该段 LLM 输出无法解析。请「重新生成」该段或整本重试。
        </p>
        {error.message ? (
          <p className="mt-1 break-all text-xs text-muted-foreground">
            {error.message}
          </p>
        ) : null}
      </section>
    );
  }

  // ---- 兜底:单行 message ----
  return (
    <section
      data-testid="outline-error-generic"
      className="rounded border border-destructive bg-destructive/10 p-3 text-sm text-destructive"
    >
      <p className="font-medium">生成失败</p>
      <p className="mt-1 break-all">
        {error.code ? `${error.code}: ` : ""}
        {error.message ?? "未知错误"}
      </p>
    </section>
  );
}
