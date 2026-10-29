/**
 * EscalationDetailsBanner(v1.1 acceptance 修复2 起;v2 W3.5 UI 缺口3 扩全原因)。
 *
 * 仅当章节状态 = `human_intervention` 且 `GET /chapters/{id}/status` 返回的
 * `escalation_details` 非空时渲染。展示"为什么进人工介入"。
 *
 * v2 W3.5 UI 缺口3 起覆盖**全部 5 类升人工原因**(后端 chapter_orchestrator
 * 各出口都填了详情):
 * - `must_infeasible_pre_write`：写前预检不可达 must（`findings` 列表)
 * - `rewrite_budget_exhausted`：重写预算耗尽(`issues` + `suggestions` + 分数)
 * - `re_review_failed`：人工改稿后重审未过(同上)
 * - `reviewer_parse_failed_after_retry`：审稿连续解析失败(模型 + 原始输出预览)
 *
 * 动作:① 提交后重审(`onReReview`,所有原因都可——人工改完正文重跑写后审校);
 * ② 忽略写前预检并重写(`onIgnoreFeasibility`,仅 `must_infeasible_pre_write`)。
 * 「强制通过」逃生口在页面右上角工具栏(ChapterEditorPage Header)。
 *
 * 设计:纯展示 + 回调组件(数据由页面层 `useChapterStatusQuery` 拉);非
 * human_intervention / 无详情 → 返 null(零垂直占用,不影响正常章节页)。
 */

import type { ChapterStatusOut } from "@/api/chapters";

interface EscalationDetailsBannerProps {
  status: ChapterStatusOut | undefined;
  /**
   * v2 W3.5 UI 缺口3:人工改正文后「提交后重审」——重跑写后审校(reviewer +
   * 写后 LLM 闸 + validator)。通过→完结,不通过→回人工带新详情。所有升人工
   * 原因都可用(改完正文都想再过一遍质量闸)。
   */
  onReReview?: () => void;
  isReReviewing?: boolean;
  /**
   * v2 Fix 2:人工确认 Fix D 写前预检误判 → 忽略预检并带完整 must 重写。
   * 仅 `reason === "must_infeasible_pre_write"` 时渲染按钮。
   */
  onIgnoreFeasibility?: () => void;
  isIgnoringFeasibility?: boolean;
}

interface Finding {
  must_text?: string;
  confidence?: number;
  evidence?: string;
  suggestion?: string;
}

interface EscalationIssue {
  issue_type?: string;
  description?: string;
  suggestion?: string;
  paragraph_anchor?: string;
}

interface EscalationDetails {
  reason?: string;
  checked_at_state?: string;
  summary?: string;
  findings?: Finding[];
  issues?: EscalationIssue[];
  suggestions?: string[];
  last_verdict?: string;
  score_overall?: number;
  rewrite_count?: number;
  max_rewrites?: number;
  model_used?: string;
  raw_output_preview?: string;
  attempts?: string[];
}

const REASON_LABELS: Record<string, string> = {
  must_infeasible_pre_write: "写前可行性预检：必达事件已被前文消费",
  rewrite_budget_exhausted: "重写预算耗尽",
  re_review_failed: "人工改稿后重审未通过",
  reviewer_parse_failed_after_retry: "审稿输出连续解析失败",
};

export function EscalationDetailsBanner(props: EscalationDetailsBannerProps) {
  const details = props.status?.escalation_details as
    | EscalationDetails
    | null
    | undefined;
  if (props.status?.status !== "human_intervention" || !details) {
    return null;
  }
  const reason = details.reason ?? "unknown";
  const findings = Array.isArray(details.findings) ? details.findings : [];
  const issues = Array.isArray(details.issues) ? details.issues : [];
  const suggestions = Array.isArray(details.suggestions)
    ? details.suggestions
    : [];
  const hasTechnical =
    Boolean(details.model_used) || Boolean(details.raw_output_preview);

  return (
    <section
      data-testid="escalation-details-banner"
      className="rounded border border-amber-500/50 bg-amber-500/10 p-4 text-sm"
    >
      <div className="flex flex-wrap items-center gap-2 font-medium text-amber-700 dark:text-amber-400">
        <span>⚠ 本章进入人工介入</span>
        <span className="rounded bg-amber-500/20 px-1.5 py-0.5 text-xs">
          {REASON_LABELS[reason] ?? reason}
        </span>
        {typeof details.score_overall === "number" ? (
          <span className="text-xs text-muted-foreground">
            综合分 {details.score_overall.toFixed(1)}
          </span>
        ) : null}
        {typeof details.rewrite_count === "number" &&
        typeof details.max_rewrites === "number" ? (
          <span className="text-xs text-muted-foreground">
            重写 {details.rewrite_count}/{details.max_rewrites}
          </span>
        ) : null}
      </div>

      {details.summary ? (
        <p
          data-testid="escalation-summary"
          className="mt-2 text-foreground"
        >
          {details.summary}
        </p>
      ) : null}

      {/* must_infeasible_pre_write:不可达必达事件逐条。 */}
      {findings.length > 0 ? (
        <ul className="mt-3 space-y-3">
          {findings.map((f, i) => (
            <li
              key={i}
              data-testid="escalation-finding"
              className="rounded border border-border bg-card p-3"
            >
              <div className="font-medium text-foreground">
                不可达必达事件
                {typeof f.confidence === "number" ? (
                  <span className="ml-2 text-xs text-muted-foreground">
                    置信 {(f.confidence * 100).toFixed(0)}%
                  </span>
                ) : null}
              </div>
              {f.must_text ? (
                <p className="mt-1 text-foreground">{f.must_text}</p>
              ) : null}
              {f.evidence ? (
                <p className="mt-1 text-xs text-muted-foreground">
                  判定依据：{f.evidence}
                </p>
              ) : null}
              {f.suggestion ? (
                <p className="mt-2 rounded bg-accent/50 p-2 text-xs text-foreground">
                  💡 处置建议：{f.suggestion}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}

      {/* rewrite_budget_exhausted / re_review_failed:审校命中问题逐条。 */}
      {issues.length > 0 ? (
        <ul className="mt-3 space-y-3">
          {issues.map((iss, i) => (
            <li
              key={i}
              data-testid="escalation-issue"
              className="rounded border border-border bg-card p-3"
            >
              <div className="font-medium text-foreground">
                {iss.issue_type
                  ? `审校问题 · ${iss.issue_type}`
                  : "审校问题"}
              </div>
              {iss.description ? (
                <p className="mt-1 text-foreground">{iss.description}</p>
              ) : null}
              {iss.paragraph_anchor ? (
                <p className="mt-1 text-xs text-muted-foreground">
                  定位：{iss.paragraph_anchor}
                </p>
              ) : null}
              {iss.suggestion ? (
                <p className="mt-2 rounded bg-accent/50 p-2 text-xs text-foreground">
                  💡 改动建议：{iss.suggestion}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}

      {/* 通用整体改进建议列表(reviewer suggestions)。 */}
      {suggestions.length > 0 ? (
        <div
          data-testid="escalation-suggestions"
          className="mt-3 rounded border border-border bg-card p-3"
        >
          <div className="font-medium text-foreground">整体改进建议</div>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-xs text-foreground">
            {suggestions.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* reviewer 解析失败:技术细节(模型 + 原始输出预览)。 */}
      {hasTechnical ? (
        <details
          data-testid="escalation-technical"
          className="mt-3 rounded border border-border bg-card p-3 text-xs"
        >
          <summary className="cursor-pointer font-medium text-foreground">
            技术细节(排障用)
          </summary>
          {details.model_used ? (
            <p className="mt-2 text-muted-foreground">
              模型：{details.model_used}
            </p>
          ) : null}
          {Array.isArray(details.attempts) && details.attempts.length > 0 ? (
            <p className="mt-1 text-muted-foreground">
              尝试次数：{details.attempts.length}
            </p>
          ) : null}
          {details.raw_output_preview ? (
            <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded bg-muted p-2 text-muted-foreground">
              {details.raw_output_preview}
            </pre>
          ) : null}
        </details>
      ) : null}

      <p className="mt-3 text-xs text-muted-foreground">
        在下方「章节正文」面板编辑正文修正后保存，再点下方「提交后重审」重跑写后审校；
        或用右上角「强制通过」直接批准(跳过审校)。批准 / 重审通过后系统自动补抽取本章记忆。
      </p>

      <div className="mt-3 flex flex-wrap gap-2 border-t border-amber-500/30 pt-3">
        {props.onReReview ? (
          <button
            type="button"
            data-testid="re-review-button"
            disabled={props.isReReviewing}
            onClick={props.onReReview}
            className="rounded bg-amber-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-700 disabled:opacity-50"
          >
            {props.isReReviewing ? "正在重审…" : "提交后重审"}
          </button>
        ) : null}
        {reason === "must_infeasible_pre_write" && props.onIgnoreFeasibility ? (
          <button
            type="button"
            data-testid="ignore-feasibility-button"
            disabled={props.isIgnoringFeasibility}
            onClick={props.onIgnoreFeasibility}
            className="rounded border border-amber-600 px-3 py-1.5 text-xs font-medium text-amber-700 hover:bg-amber-600/10 disabled:opacity-50 dark:text-amber-400"
          >
            {props.isIgnoringFeasibility
              ? "正在重写…"
              : "确认误判 · 忽略预检并写作"}
          </button>
        ) : null}
      </div>
      {reason === "must_infeasible_pre_write" && props.onIgnoreFeasibility ? (
        <p className="mt-2 text-xs text-muted-foreground">
          若你复核后确认这是<strong className="text-foreground">误判</strong>
          （该必达事件并未真被前文写完，只是表层相似），可「忽略预检并写作」——
          章节带完整必达事件写作，写后审校仍会兜底验真。
        </p>
      ) : null}
    </section>
  );
}
