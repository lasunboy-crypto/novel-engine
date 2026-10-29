/**
 * ExcellenceMetrics(v1.1 wave 7 / B 卓越性度量 UI 化)
 *
 * 展示 reviewer 的 4 个"卓越性"纯度量维度:画面感 / 内心戏 / 套路化 / 沉浸感
 * (score_imagery / score_interiority / score_formulaic / score_immersion)。
 * 在 ReviewHistoryPanel 的唯一稿审稿历史中复用。
 *
 * ## 关键语义(UI 必须明确,别误导维护人)
 *
 * - 这 4 个维度**永不进 score_overall 加权、永不影响 verdict**(纯度量,
 *   governance §2.16/§2.17/§2.21)。仅供唯一稿质量观察。故本组件
 *   与 verdict / score_overall **视觉上分开**(虚线框 + 独立色系),且带
 *   "仅质感参考,不影响通过判定"注脚。
 * - **score_formulaic 是唯一"反极性"维度**:越高 = 越套路 = 越差;其余 3 个越高
 *   越好。UI 上 formulaic 用反过来的颜色档(高分 = 警示红)+ ⚠ 标记。
 *
 * ## None 处理
 *
 * LLM 漏产 / 主审退化 / 旧 review(wave 5 前)→ 字段为 None,显示"—"。
 * 4 个**全 None → 整个组件返 null**(旧 review 不占空间)。
 */

import type { ReviewSummaryEntry } from "@/api/chapters";

type ExcellenceFields = Pick<
  ReviewSummaryEntry,
  "score_imagery" | "score_interiority" | "score_formulaic" | "score_immersion"
>;

interface ExcellenceMetricsProps {
  review: ExcellenceFields;
}

interface MetricSpec {
  key: "imagery" | "interiority" | "formulaic" | "immersion";
  label: string;
  score: number | null | undefined;
  /** true = 反极性(score_formulaic:越高越套路越差) */
  reverse: boolean;
}

export function ExcellenceMetrics(props: ExcellenceMetricsProps) {
  const { review } = props;
  const metrics: MetricSpec[] = [
    { key: "imagery", label: "画面感", score: review.score_imagery, reverse: false },
    {
      key: "interiority",
      label: "内心戏",
      score: review.score_interiority,
      reverse: false,
    },
    {
      key: "formulaic",
      label: "套路化",
      score: review.score_formulaic,
      reverse: true,
    },
    {
      key: "immersion",
      label: "沉浸感",
      score: review.score_immersion,
      reverse: false,
    },
  ];

  // 4 个全 None(旧 review / LLM 全漏产)→ 不渲染,不占空间。
  if (metrics.every((m) => m.score == null)) return null;

  return (
    <div
      data-testid="excellence-metrics"
      className="space-y-1 rounded border border-dashed border-indigo-200 bg-indigo-50/40 p-2"
    >
      <div className="flex flex-wrap items-baseline gap-x-1.5">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-indigo-700">
          卓越性度量
        </span>
        <span
          data-testid="excellence-metrics-note"
          className="text-[10px] text-indigo-500"
        >
          仅质感参考,不影响通过判定
        </span>
      </div>
      <dl className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-[11px] sm:grid-cols-4">
        {metrics.map((m) => (
          <MetricCell key={m.key} spec={m} />
        ))}
      </dl>
    </div>
  );
}

function MetricCell(props: { spec: MetricSpec }) {
  const { key, label, score, reverse } = props.spec;
  const colorClass =
    score == null
      ? "text-muted-foreground"
      : reverse
        ? formulaicColor(score)
        : normalColor(score);
  return (
    <div className="flex items-baseline justify-between gap-1">
      <dt className="truncate text-muted-foreground">
        {label}
        {reverse ? (
          <span
            data-testid="excellence-reverse-marker"
            title="反极性:越高越套路越差"
            className="ml-0.5 text-rose-400"
          >
            ⚠
          </span>
        ) : null}
      </dt>
      <dd
        data-testid={`excellence-score-${key}`}
        className={`tabular-nums ${colorClass}`}
      >
        {score == null ? "—" : score.toFixed(2)}
      </dd>
    </div>
  );
}

// 正极性:>=8 绿 / >=6.5 琥珀 / <6.5 红。
function normalColor(score: number): string {
  return score >= 8
    ? "text-emerald-700"
    : score >= 6.5
      ? "text-amber-700"
      : "text-rose-700";
}

// 反极性(score_formulaic 越低越好):<=3 绿 / <=5 琥珀 / >5 红
function formulaicColor(score: number): string {
  return score <= 3
    ? "text-emerald-700"
    : score <= 5
      ? "text-amber-700"
      : "text-rose-700";
}
