/**
 * MustAchieveCheckList(P6-F-01)— 关键字段达成对比块。
 *
 * 对齐 FRONTEND_SPEC §4.9 字面"must_achieve 逐项打勾 / 未达成红色"+ plan
 * §"P6-F-01" ④ 字面"关键字段达成对比走 P6-B-11 已落 `GET /chapters/{id}/
 * validators/latest` 端点"。
 *
 * ## 数据流
 *
 * 接 `report: ValidatorReportOut | undefined` + `error: unknown`(由父组件
 * useChapterValidatorReportQuery 提供)。空数据态:
 *
 * - `report` 为 undefined 且 query loading → 显示"加载校验报告中…"
 * - `report` 为 undefined 且 query error 是 `VALIDATOR_REPORT_NOT_FOUND` →
 *   显示"先 :write 写出正文,再做关键字段达成对比"(plan §"P6-F-01" ④
 *   字面"404 VALIDATOR_REPORT_NOT_FOUND 时显示提示")
 * - `report` 为 undefined 且 query error 是其他 → 显示原始错误
 * - `report` 有数据 → 显示 10 个 validator 的 pass/fail + 关键 details
 *
 * ## 渲染策略
 *
 * - 顶部一行"整体 ✓ / ✗"+ 失败计数。
 * - must_achieve / must_unresolved / forbidden / character_existence 4 个
 *   validator 单独展开 details(逐项打勾 / 命中红色 / missing IDs 列表)。
 * - 其余 6 个 validator(word_count / timeline_monotonic /
 *   death_foreshadowing / voice_forbidden / unfinished_actions_continuation /
 *   scene_continuation_location)简版显示 pass/fail + 单行 details summary。
 */

import { isApiError } from "@/api/errors";
import type { ValidatorReportOut, ValidatorResultOut } from "@/api/validators";

interface MustAchieveCheckListProps {
  cardMustAchieve: string[];
  cardMustUnresolved: string[];
  cardForbidden: string[];
  cardRelatedCharacters: string[];
  report: ValidatorReportOut | undefined;
  isLoading: boolean;
  error: unknown;
}

export function MustAchieveCheckList(props: MustAchieveCheckListProps) {
  const { report, isLoading, error } = props;

  if (isLoading) {
    return (
      <section
        data-testid="validator-report-loading"
        className="rounded border border-border bg-card p-4 text-sm text-muted-foreground"
      >
        加载关键字段达成对比中…
      </section>
    );
  }
  if (error) {
    return <ValidatorErrorView error={error} />;
  }
  if (!report) {
    return null;
  }
  const byName = indexByName(report.validators);
  return (
    <section
      data-testid="validator-report-panel"
      className="space-y-3 rounded border border-emerald-200 bg-emerald-50/40 p-4"
    >
      <header className="flex flex-wrap items-center gap-3 border-b border-emerald-100 pb-2">
        <h3 className="text-sm font-semibold uppercase tracking-wide text-emerald-900">
          关键字段达成对比({report.passed ? "全过" : "存在失败"})
        </h3>
        <span className="text-xs text-emerald-800">
          {report.passed ? (
            <strong className="text-emerald-700">✓ 全部 validator 通过</strong>
          ) : (
            <strong className="text-rose-700">
              ✗ 失败 {report.failed_constraint_names?.length ?? 0} 项 ·{" "}
              {report.failed_constraint_names?.join(", ") || "—"}
            </strong>
          )}
        </span>
        <span className="ml-auto text-[10px] text-muted-foreground">
          基于 final.md({report.chapter_text_length} 字符)
        </span>
      </header>

      <MustAchieveBlock
        title="must_achieve(必达事件)"
        cardItems={props.cardMustAchieve}
        result={byName.get("must_achieve")}
        missingKey="items_missing"
      />
      <MustAchieveBlock
        title="must_unresolved(必保未解悬念)"
        cardItems={props.cardMustUnresolved}
        result={byName.get("must_unresolved")}
        missingKey="items_resolved"
        missingLabel="已被解决(违反必保)"
      />
      <ForbiddenBlock
        cardForbidden={props.cardForbidden}
        result={byName.get("forbidden")}
      />
      <CharacterExistenceBlock
        cardRelatedCharacters={props.cardRelatedCharacters}
        result={byName.get("character_existence")}
      />
      <OtherValidatorsBlock validators={report.validators} />
    </section>
  );
}

function ValidatorErrorView(props: { error: unknown }) {
  const apiErr = isApiError(props.error) ? props.error : null;
  if (apiErr?.code === "VALIDATOR_REPORT_NOT_FOUND") {
    return (
      <section
        data-testid="validator-report-empty"
        className="rounded border border-sky-200 bg-sky-50 p-4 text-sm text-sky-900"
      >
        <p className="mb-1 font-medium">尚未生成关键字段达成对比</p>
        <p className="text-xs text-sky-800">
          先点"重新写作"或在 chapter 页触发 :write 写出正文,再回到本页查看
          must_achieve / forbidden / character_existence 等 10 项 validator 的
          逐项达成情况。
        </p>
      </section>
    );
  }
  const message = apiErr
    ? `${apiErr.code}: ${apiErr.message}`
    : props.error instanceof Error
      ? props.error.message
      : "未知错误";
  return (
    <section
      data-testid="validator-report-error"
      className="rounded border border-destructive bg-destructive/10 p-4 text-sm text-destructive"
    >
      加载关键字段达成对比失败:{message}
    </section>
  );
}

interface MustAchieveBlockProps {
  title: string;
  cardItems: string[];
  result: ValidatorResultOut | undefined;
  /** details 内"未达成"键名 — `must_achieve` 用 items_missing,`must_unresolved` 用 items_resolved。 */
  missingKey: "items_missing" | "items_resolved";
  /** 自定义 "未达成" 标签;默认"未达成"。 */
  missingLabel?: string;
}

function MustAchieveBlock(props: MustAchieveBlockProps) {
  const { result, cardItems } = props;
  if (!result) return null;
  const missing = readStringArray(result.details, props.missingKey);
  const missingSet = new Set(missing);
  const missingLabel = props.missingLabel ?? "未达成";
  return (
    <div className="rounded border border-emerald-100 bg-white p-3">
      <p className="mb-2 text-xs font-semibold text-emerald-900">
        {props.title}{" "}
        <span className={result.passed ? "text-emerald-700" : "text-rose-700"}>
          [{result.passed ? "✓ 通过" : "✗ 失败"}]
        </span>
      </p>
      {cardItems.length === 0 ? (
        <p className="text-xs text-muted-foreground">(章节卡未列任何项)</p>
      ) : (
        <ul className="space-y-1 text-xs">
          {cardItems.map((item, i) => {
            const hit = missingSet.has(item);
            return (
              <li
                key={i}
                className={
                  hit
                    ? "flex gap-2 text-rose-700"
                    : "flex gap-2 text-emerald-700"
                }
              >
                <span aria-hidden="true">{hit ? "✗" : "✓"}</span>
                <span className="break-words">
                  {item}
                  {hit && (
                    <span className="ml-2 text-[10px] text-rose-600">
                      ({missingLabel})
                    </span>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

interface ForbiddenBlockProps {
  cardForbidden: string[];
  result: ValidatorResultOut | undefined;
}

function ForbiddenBlock(props: ForbiddenBlockProps) {
  const { result, cardForbidden } = props;
  if (!result) return null;
  const hits = readStringArray(result.details, "forbidden_hits");
  const hitSet = new Set(hits);
  return (
    <div className="rounded border border-emerald-100 bg-white p-3">
      <p className="mb-2 text-xs font-semibold text-emerald-900">
        forbidden(禁止元素){" "}
        <span className={result.passed ? "text-emerald-700" : "text-rose-700"}>
          [{result.passed ? "✓ 通过" : "✗ 失败"}]
        </span>
      </p>
      {cardForbidden.length === 0 ? (
        <p className="text-xs text-muted-foreground">(章节卡未列禁止项)</p>
      ) : (
        <ul className="space-y-1 text-xs">
          {cardForbidden.map((item, i) => {
            const hit = hitSet.has(item);
            return (
              <li
                key={i}
                className={
                  hit
                    ? "flex gap-2 text-rose-700"
                    : "flex gap-2 text-emerald-700"
                }
              >
                <span aria-hidden="true">{hit ? "✗" : "✓"}</span>
                <span className="break-words">
                  {item}
                  {hit && (
                    <span className="ml-2 text-[10px] text-rose-600">
                      (命中,违反禁止)
                    </span>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

interface CharacterExistenceBlockProps {
  cardRelatedCharacters: string[];
  result: ValidatorResultOut | undefined;
}

function CharacterExistenceBlock(props: CharacterExistenceBlockProps) {
  const { result, cardRelatedCharacters } = props;
  if (!result) return null;
  const missing = readStringArray(result.details, "missing_character_ids");
  const missingSet = new Set(missing);
  return (
    <div className="rounded border border-emerald-100 bg-white p-3">
      <p className="mb-2 text-xs font-semibold text-emerald-900">
        character_existence(出场角色存在性){" "}
        <span className={result.passed ? "text-emerald-700" : "text-rose-700"}>
          [{result.passed ? "✓ 通过" : "✗ 失败"}]
        </span>
      </p>
      {cardRelatedCharacters.length === 0 ? (
        <p className="text-xs text-muted-foreground">(章节卡未列关联角色)</p>
      ) : (
        <ul className="space-y-1 text-xs">
          {cardRelatedCharacters.map((charId, i) => {
            const isMissing = missingSet.has(charId);
            return (
              <li
                key={i}
                className={
                  isMissing
                    ? "flex gap-2 text-rose-700"
                    : "flex gap-2 text-emerald-700"
                }
              >
                <span aria-hidden="true">{isMissing ? "✗" : "✓"}</span>
                <code className="break-all font-mono">{charId}</code>
                {isMissing && (
                  <span className="text-[10px] text-rose-600">
                    (工作区 characters 表无该 ID;可走 :retry-references 修复)
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function OtherValidatorsBlock(props: { validators: ValidatorResultOut[] }) {
  const highlighted = new Set([
    "must_achieve",
    "must_unresolved",
    "forbidden",
    "character_existence",
  ]);
  const others = props.validators.filter((v) => !highlighted.has(v.name));
  if (others.length === 0) return null;
  return (
    <div className="rounded border border-emerald-100 bg-white p-3">
      <p className="mb-2 text-xs font-semibold text-emerald-900">
        其他 validator(节奏 / 时间线 / 衔接 6 类)
      </p>
      <ul className="space-y-1 text-xs">
        {others.map((v) => (
          <li
            key={v.name}
            className={
              v.passed
                ? "flex flex-wrap gap-2 text-emerald-700"
                : "flex flex-wrap gap-2 text-rose-700"
            }
          >
            <span aria-hidden="true">{v.passed ? "✓" : "✗"}</span>
            <code className="font-mono">{v.name}</code>
            <span className="text-[10px] text-muted-foreground">
              {summarizeDetails(v)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function indexByName(
  validators: ValidatorResultOut[],
): Map<string, ValidatorResultOut> {
  const m = new Map<string, ValidatorResultOut>();
  for (const v of validators) m.set(v.name, v);
  return m;
}

function readStringArray(
  details: Record<string, unknown> | undefined,
  key: string,
): string[] {
  if (!details) return [];
  const raw = details[key];
  if (!Array.isArray(raw)) return [];
  return raw.filter((x): x is string => typeof x === "string");
}

function summarizeDetails(v: ValidatorResultOut): string {
  const d = v.details ?? {};
  switch (v.name) {
    case "word_count": {
      const actual = d.actual_word_count;
      const min = d.word_count_min;
      const max = d.word_count_max;
      return `actual=${actual} · range=${min}–${max}`;
    }
    case "timeline_monotonic": {
      const violations = readStringArray(d, "violations").length;
      return `violations=${violations}`;
    }
    case "death_foreshadowing": {
      const dead = readStringArray(d, "dead_character_ids");
      return dead.length === 0 ? "(无)" : `dead=${dead.join(", ")}`;
    }
    case "voice_forbidden": {
      const violations = Array.isArray(d.character_violations)
        ? (d.character_violations as unknown[]).length
        : 0;
      return `violations=${violations}`;
    }
    case "unfinished_actions_continuation": {
      const hit = readStringArray(d, "hit_actions").length;
      const total = d.unfinished_actions_total ?? 0;
      return `hit=${hit}/${total}`;
    }
    case "scene_continuation_location": {
      const found = Array.isArray(d.location_keywords_found)
        ? (d.location_keywords_found as unknown[]).length
        : 0;
      return `keywords_found=${found}`;
    }
    default:
      return "";
  }
}
