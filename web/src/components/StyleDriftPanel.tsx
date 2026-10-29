/**
 * StyleDriftPanel(v1.1 w3-s7 H7):章节风格指纹漂移折线图。
 *
 * 数据源:`GET /chapters/{id}/style-drift`(后端读 G5 落盘的
 * `chapter_versions.style_fingerprint`)。展示 v1→vN 的 4 个数值维度漂移趋势
 * + 每版本主导人称(dominant_pov)。
 *
 * ## 设计决策
 *
 * - **双 Y 轴**:段落 / 句长均值(几十~几百字符)与比喻 / 感叹密度(0~1)量纲
 *   差 2 个数量级,共用一轴会把密度线压成贴底直线 → 左轴 = 长度均值,右轴 =
 *   密度。
 * - **主导人称单独成行徽标**:dominant_pov 是类别值(third/first/mixed),无法
 *   画进数值折线;改用版本徽标行展示(跨章节视角一致是 G10 硬约束的核心关注,
 *   一眼看出哪个版本人称漂了)。
 * - **缺指纹点 connectNulls**:G5 落地前的旧版本 style_fingerprint 为 null →
 *   后端各维度返 null,折线 connectNulls 跨过(诚实留缺口语义)。
 * - **单版本不画线**:只有 v1 时折线退化为单点无"趋势"语义,显 hint。
 */

import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  useChapterStyleDriftQuery,
  type StyleFingerprintPoint,
} from "@/api/chapters";
import { isApiError } from "@/api/errors";

interface StyleDriftPanelProps {
  workspaceId: string | null | undefined;
  chapterId: string | null | undefined;
}

const POV_LABEL: Record<string, string> = {
  third_person: "第三人称",
  first_person: "第一人称",
  mixed: "混合",
};

const POV_BADGE_CLASS: Record<string, string> = {
  third_person: "bg-emerald-100 text-emerald-700",
  first_person: "bg-amber-100 text-amber-700",
  mixed: "bg-rose-100 text-rose-700",
};

export function StyleDriftPanel(props: StyleDriftPanelProps) {
  const query = useChapterStyleDriftQuery(props.workspaceId, props.chapterId);

  return (
    <section
      data-testid="style-drift-panel"
      className="flex min-w-0 flex-col rounded border border-border bg-card"
    >
      <div className="flex items-baseline justify-between border-b border-border px-3 py-2">
        <h2 className="text-sm font-medium">风格指纹漂移</h2>
        <span className="text-xs text-muted-foreground">v1 → vN 各版本对比</span>
      </div>
      <div className="p-3">
        <StyleDriftBody query={query} />
      </div>
    </section>
  );
}

function StyleDriftBody({
  query,
}: {
  query: ReturnType<typeof useChapterStyleDriftQuery>;
}) {
  if (query.isPending) {
    return (
      <p
        data-testid="style-drift-loading"
        className="text-sm text-muted-foreground"
      >
        加载风格指纹中…
      </p>
    );
  }
  if (query.error) {
    const message = isApiError(query.error)
      ? `${query.error.code}: ${query.error.message}`
      : query.error instanceof Error
        ? query.error.message
        : "未知错误";
    return (
      <p
        data-testid="style-drift-error"
        className="text-sm text-destructive"
      >
        加载风格指纹失败:{message}
      </p>
    );
  }

  const points = query.data?.points ?? [];
  if (points.length === 0) {
    return (
      <p
        data-testid="style-drift-empty"
        className="rounded border border-dashed border-border p-3 text-sm text-muted-foreground"
      >
        暂无风格指纹数据 — 章节写作产出版本后,这里会显示 v1→vN 的风格漂移趋势。
      </p>
    );
  }
  if (points.length === 1) {
    return (
      <div className="space-y-2">
        <p
          data-testid="style-drift-single"
          className="rounded border border-dashed border-border p-3 text-sm text-muted-foreground"
        >
          只有 1 个版本,暂无漂移趋势可对比(重写 / 修订产生新版本后出现折线)。
        </p>
        <PovBadgeRow points={points} />
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="h-64 w-full" data-testid="style-drift-chart">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart
            data={points}
            margin={{ top: 8, right: 8, bottom: 8, left: 0 }}
          >
            <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
            <XAxis
              dataKey="version_id"
              tick={{ fontSize: 11 }}
              allowDecimals={false}
              tickFormatter={(v) => `v${String(v)}`}
              label={{
                value: "版本",
                position: "insideBottom",
                offset: -2,
                style: { fontSize: 11, fill: "#71717a" },
              }}
            />
            <YAxis
              yAxisId="length"
              tick={{ fontSize: 11 }}
              width={36}
              label={{
                value: "字符均值",
                angle: -90,
                position: "insideLeft",
                style: { fontSize: 10, fill: "#71717a" },
              }}
            />
            <YAxis
              yAxisId="density"
              orientation="right"
              tick={{ fontSize: 11 }}
              width={36}
            />
            <Tooltip
              contentStyle={{ fontSize: 12 }}
              labelFormatter={(label) => `版本 v${String(label)}`}
            />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            <Line
              yAxisId="length"
              type="monotone"
              name="段落均值"
              dataKey="paragraph_length_mean"
              stroke="#2563eb"
              strokeWidth={2}
              connectNulls
              isAnimationActive={false}
            />
            <Line
              yAxisId="length"
              type="monotone"
              name="句长均值"
              dataKey="sentence_length_mean"
              stroke="#7c3aed"
              strokeWidth={2}
              connectNulls
              isAnimationActive={false}
            />
            <Line
              yAxisId="density"
              type="monotone"
              name="比喻密度"
              dataKey="simile_density"
              stroke="#059669"
              strokeWidth={2}
              connectNulls
              isAnimationActive={false}
            />
            <Line
              yAxisId="density"
              type="monotone"
              name="感叹密度"
              dataKey="exclamation_density"
              stroke="#d97706"
              strokeWidth={2}
              connectNulls
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <PovBadgeRow points={points} />
    </div>
  );
}

/** 每版本主导人称徽标行(dominant_pov 类别值,跨版本视角漂移一眼可见)。 */
function PovBadgeRow({ points }: { points: StyleFingerprintPoint[] }) {
  return (
    <div
      data-testid="style-drift-pov-row"
      className="flex flex-wrap items-center gap-1.5 text-xs"
    >
      <span className="text-muted-foreground">主导人称:</span>
      {points.map((p) => {
        const pov = p.dominant_pov;
        const label = pov ? POV_LABEL[pov] ?? pov : "—";
        const badgeClass = pov
          ? POV_BADGE_CLASS[pov] ?? "bg-muted text-muted-foreground"
          : "bg-muted text-muted-foreground";
        return (
          <span
            key={p.version_id}
            data-testid={`style-drift-pov-v${p.version_id}`}
            className={`rounded px-1.5 py-0.5 ${badgeClass}`}
            title={p.has_fingerprint ? undefined : "该版本无风格指纹(G5 落地前)"}
          >
            v{p.version_id}:{label}
          </span>
        );
      })}
    </div>
  );
}
