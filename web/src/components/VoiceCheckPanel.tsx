/**
 * VoiceCheckPanel(P2-F-07):右栏"voice 校验"子面板。
 *
 * 与 ReviewHistoryPanel 同栏垂直堆叠(VoiceCheck 在上,ReviewHistory 在下)。
 * 数据流分两条:
 *
 * 1. **角色 voice_profile 摘要**:由 ChapterCard.related_characters 决定展示
 *    哪些角色 → `useQueries` 并行拉每个 `GET /characters/{id}` 详情 → 每个角
 *    色卡片展示 tone / sentence_style / self_address + catchphrases /
 *    forbidden_phrases(对齐 P2-B-11 reviewer.md 6 字段对账规范的可视化子集;
 *    addresses_others 过细不展示,留 v2)
 * 2. **score_voice_consistency 联动**:从父级(ChapterEditorPage)hoist 的
 *    reviewSummary 数组取最新条目,显示该次审稿的 voice 一致性分 + 颜色档
 *    (>=8 绿 / >=6.5 amber / <6.5 红,对齐 PROJECT_SPEC §4.4 阈值)+
 *    hard_constraints_passed 状态。reviewSummary 为 null / 空数组时显示
 *    "暂无审稿"hint
 *
 * P2-B-18 顺延任务把 6 档分项 + hard_constraints_passed 通过 ReviewSummaryEntry
 * 暴露,本组件直接消费 review_summary 末位条目。无需新增 API hook。
 */

import { useQueries, type UseQueryResult } from "@tanstack/react-query";

import { apiGet } from "@/api/client";
import {
  CHARACTER_DETAIL_QUERY_KEY,
  type CharacterOut,
  type VoiceProfile,
} from "@/api/characters";
import { isApiError } from "@/api/errors";
import type { ReviewSummaryEntry } from "@/api/tasks";
import { useChapterCardQuery } from "@/api/chapters";

interface VoiceCheckPanelProps {
  workspaceId: string | null | undefined;
  chapterId: string | null | undefined;
  /** 父级从 ReviewHistoryPanel hoist 的 review_summary 数组;null = 无任务 / 任务无摘要 */
  reviewSummary: ReviewSummaryEntry[] | null;
}

export function VoiceCheckPanel(props: VoiceCheckPanelProps) {
  const cardQuery = useChapterCardQuery(props.workspaceId, props.chapterId);
  const relatedCharacters = cardQuery.data?.related_characters ?? [];

  // 并行拉每个出场角色详情。useQueries 适配"动态数量"场景(角色数 0..N)——
  // 顶层 hook 规则不允许 useCharacterDetailQuery 进 .map。queryKey 命名空间与
  // characters.ts CHARACTER_DETAIL_QUERY_KEY 一致,缓存命中。
  // useQueries 默认推 `data: unknown`,显式 cast 到 UseQueryResult<CharacterOut>[]
  // 复用 queryFn 的返回类型(apiGet<CharacterOut>),不引入运行时风险。
  const characterQueries = useQueries({
    queries: relatedCharacters.map((cid) => ({
      queryKey: CHARACTER_DETAIL_QUERY_KEY(props.workspaceId ?? "", cid),
      queryFn: () =>
        apiGet<CharacterOut>(`/characters/${encodeURIComponent(cid)}`, {
          workspaceId: props.workspaceId as string,
        }),
      enabled:
        typeof props.workspaceId === "string" && props.workspaceId.length > 0,
    })),
  }) as UseQueryResult<CharacterOut, Error>[];

  // reviewSummary 末位条目即"最新一次审稿"(P1-B-26 决策 6 / orchestrator 顺
  // 序写入)。null 兜底由 ScoreSection 内部分支。
  const latestReview =
    props.reviewSummary && props.reviewSummary.length > 0
      ? props.reviewSummary[props.reviewSummary.length - 1]
      : null;

  return (
    <section
      data-testid="voice-check-panel"
      className="flex min-w-0 flex-col rounded border border-border bg-card"
    >
      <header className="border-b border-border px-4 py-2">
        <h2 className="text-sm font-medium">Voice 校验</h2>
      </header>
      <div className="flex flex-col gap-3 p-4 text-xs">
        <ScoreSection latestReview={latestReview} />
        <CharacterVoicesSection
          cardLoading={cardQuery.isPending && cardQuery.fetchStatus !== "idle"}
          cardError={cardQuery.error}
          relatedCharacters={relatedCharacters}
          characterQueries={characterQueries}
        />
      </div>
    </section>
  );
}

interface ScoreSectionProps {
  latestReview: ReviewSummaryEntry | null;
}

function ScoreSection(props: ScoreSectionProps) {
  if (!props.latestReview) {
    return (
      <div
        data-testid="voice-score-empty"
        className="rounded border border-dashed border-border p-2 text-muted-foreground"
      >
        暂无审稿,触发 :write / :rewrite 后此处显示 voice 一致性分。
      </div>
    );
  }
  const score = props.latestReview.score_voice_consistency;
  const tone = scoreToneClass(score);
  return (
    <div
      data-testid="voice-score"
      className="flex flex-wrap items-center gap-2"
    >
      <span className="text-muted-foreground">最新审稿 voice 分</span>
      <span
        data-testid="voice-score-value"
        className={`rounded px-2 py-0.5 text-xs font-medium tabular-nums ${tone}`}
      >
        {props.latestReview.score_voice_consistency.toFixed(2)}
      </span>
      <span className="text-muted-foreground">
        v{props.latestReview.version_reviewed}
      </span>
      {props.latestReview.hard_constraints_passed ? (
        <span
          data-testid="voice-hard-pass"
          className="rounded bg-emerald-100 px-1.5 py-0.5 text-emerald-700"
        >
          硬约束通过
        </span>
      ) : (
        <span
          data-testid="voice-hard-fail"
          className="rounded bg-rose-100 px-1.5 py-0.5 text-rose-700"
        >
          硬约束未通过
        </span>
      )}
    </div>
  );
}

interface CharacterVoicesSectionProps {
  cardLoading: boolean;
  cardError: unknown;
  relatedCharacters: string[];
  characterQueries: UseQueryResult<CharacterOut, Error>[];
}

function CharacterVoicesSection(props: CharacterVoicesSectionProps) {
  if (props.cardLoading) {
    return (
      <div className="text-muted-foreground">加载章节卡中…</div>
    );
  }
  if (props.cardError) {
    const apiErr = isApiError(props.cardError) ? props.cardError : null;
    const message = apiErr
      ? `${apiErr.code}: ${apiErr.message}`
      : props.cardError instanceof Error
        ? props.cardError.message
        : "未知错误";
    return (
      <div data-testid="voice-card-error" className="text-destructive">
        加载章节卡失败:{message}
      </div>
    );
  }
  if (props.relatedCharacters.length === 0) {
    return (
      <div
        data-testid="voice-related-empty"
        className="rounded border border-dashed border-border p-2 text-muted-foreground"
      >
        本章节卡未声明出场角色(related_characters 为空)。
      </div>
    );
  }
  return (
    <ul data-testid="voice-character-list" className="space-y-2">
      {props.relatedCharacters.map((cid, index) => {
        const query = props.characterQueries[index];
        return (
          <CharacterVoiceItem
            key={cid}
            characterId={cid}
            loading={query?.isPending && query.fetchStatus !== "idle"}
            error={query?.error}
            character={query?.data}
          />
        );
      })}
    </ul>
  );
}

interface CharacterVoiceItemProps {
  characterId: string;
  loading: boolean | undefined;
  error: unknown;
  character: CharacterOut | undefined;
}

function CharacterVoiceItem(props: CharacterVoiceItemProps) {
  if (props.loading) {
    return (
      <li className="rounded border border-border p-2 text-muted-foreground">
        {props.characterId}:加载中…
      </li>
    );
  }
  if (props.error) {
    const apiErr = isApiError(props.error) ? props.error : null;
    if (apiErr?.code === "CHARACTER_NOT_FOUND") {
      return (
        <li
          data-testid="voice-character-not-found"
          className="rounded border border-dashed border-border p-2 text-muted-foreground"
        >
          {props.characterId}:角色未配置(章节卡引用了不存在的角色 ID)
        </li>
      );
    }
    const message = apiErr
      ? `${apiErr.code}: ${apiErr.message}`
      : props.error instanceof Error
        ? props.error.message
        : "未知错误";
    return (
      <li className="rounded border border-destructive bg-destructive/10 p-2 text-destructive">
        {props.characterId}:{message}
      </li>
    );
  }
  if (!props.character) {
    return null;
  }
  return (
    <li
      data-testid="voice-character-item"
      data-character-id={props.character.character_id}
      className="rounded border border-border p-2"
    >
      <div className="flex items-baseline gap-2">
        <span className="font-medium text-foreground">
          {props.character.name}
        </span>
        <span className="text-muted-foreground">
          ({props.character.character_id})
        </span>
      </div>
      <VoiceProfileSummary voice={props.character.voice_profile} />
    </li>
  );
}

function VoiceProfileSummary(props: { voice: VoiceProfile }) {
  const { voice } = props;
  return (
    <dl className="mt-1 space-y-0.5 text-muted-foreground">
      <Row label="语气" value={voice.tone} />
      <Row label="句式" value={voice.sentence_style} />
      <Row label="自称" value={voice.self_address} />
      {voice.catchphrases && voice.catchphrases.length > 0 ? (
        <Row label="口头禅" value={voice.catchphrases.join(" / ")} />
      ) : null}
      {voice.forbidden_phrases && voice.forbidden_phrases.length > 0 ? (
        <Row
          label="禁忌词"
          value={voice.forbidden_phrases.join(" / ")}
          tone="warning"
        />
      ) : null}
    </dl>
  );
}

function Row(props: {
  label: string;
  value: string;
  tone?: "warning";
}) {
  const valueClass =
    props.tone === "warning" ? "text-rose-700" : "text-foreground";
  return (
    <div className="flex gap-1.5">
      <dt className="shrink-0 text-muted-foreground">{props.label}:</dt>
      <dd className={`min-w-0 break-words ${valueClass}`}>{props.value}</dd>
    </div>
  );
}

function scoreToneClass(score: number): string {
  if (score >= 8.0) return "bg-emerald-100 text-emerald-700";
  if (score >= 6.5) return "bg-amber-100 text-amber-700";
  return "bg-rose-100 text-rose-700";
}
