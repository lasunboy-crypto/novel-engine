/**
 * ChapterContentPanel(P1-F-12 落地 → P1-F-13 加版本切换)。
 *
 * 数据流:
 *
 * 1. `useChapterVersionsQuery` 拉版本号列表(`v1.md` / `v2.md` / ...,后端按
 *    1..N 升序返)
 * 2. **P1-F-13 起**:本地 `selectedVersionId` 受控 state,默认初始化为最新版
 *    (`useEffect` 同步 versions 数据),用户从 `<VersionSelector>` 切换后
 *    更新 state
 * 3. `useChapterVersionQuery(selectedVersionId)` 拉对应版本正文字符串
 * 4. `<ReactMarkdown>{content}</ReactMarkdown>` 渲染
 *
 * 版本空数组(章节未写作)→ 显示"尚未写作"hint;下拉不渲染。
 *
 * ## 不调 `:switch-version`(P1-F-13 待审 1 拍 A)
 *
 * 任务模板字面"切换调 `POST /chapters/{id}:switch-version`(可选)",维护人
 * 2026-05-06 拍 A —— 不实施 mutation,只本地切换查看。理由:
 * - 字面"(可选)"
 * - 服务端 final 指针由 orchestrator 在 :write / :rewrite / :revise 闭环结
 *   束时自动指向最新生产版本(P1-B-19 决策 6 字面),P1-F-13 不该越界
 * - P1 单工作区 + 同步执行,用户手动切回旧版作 final 是边缘场景
 *
 * ## P1 渲染样式策略
 *
 * 不装 `@tailwindcss/typography`(Q2 不在范围,新装要 Q4 流程)。改用基础
 * Tailwind 类配合 ReactMarkdown 默认输出的 HTML 标签写一组最小可读样式
 * (`prose-style` 容器,见 `index.css`)。P2/P3 真有"小说排版精修"需求再
 * 评估装 typography 插件(留观项 v2_wishlist 已记)。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";

import {
  useChapterVersionQuery,
  useChapterVersionsQuery,
} from "@/api/chapters";
import { isApiError } from "@/api/errors";
import { VersionSelector } from "@/components/VersionSelector";

interface ChapterContentPanelProps {
  workspaceId: string | null | undefined;
  chapterId: string | null | undefined;
  /**
   * v1.1 acceptance 修复2(孤儿章人工解决):`human_intervention` 状态时
   * 页面层传 true,解锁"编辑正文"模式(textarea + 保存 → PUT content)。
   * 其余状态(及未传)保持只读渲染,零行为变化。
   */
  canManualEdit?: boolean;
  /** 保存回调(页面层接 mutation);resolve 新版本号供面板跳转选中。 */
  onSaveContent?: (content: string) => Promise<{ version_id: number }>;
  isSavePending?: boolean;
  onWordCountChange?: (count: number | null) => void;
}

export function ChapterContentPanel(props: ChapterContentPanelProps) {
  const versionsQuery = useChapterVersionsQuery(
    props.workspaceId,
    props.chapterId,
  );

  // B-08 hot-fix(2026-05-19):`versions = data?.versions ?? []` 短路求值
  // 每渲染新数组引用,触发下游 useMemo / useEffect 依赖 versions 抖动。
  // 包 useMemo 稳定引用,deps 走原始 `data?.versions`(query cache 稳定)。
  const versions = useMemo(
    () => versionsQuery.data?.versions ?? [],
    [versionsQuery.data?.versions],
  );
  const latestVersionId = useMemo(() => {
    if (versions.length === 0) return null;
    return versions[versions.length - 1].version_id;
  }, [versions]);

  // P1-F-13:受控 selectedVersionId,默认初始化为最新版;用户切换覆盖。
  const [selectedVersionId, setSelectedVersionId] = useState<number | null>(
    null,
  );

  // 章节切换 / 版本列表刷新时,把选中版本同步到"最新版":
  // - selectedVersionId null → 首次拿到 versions 时初始化为最新
  // - 当前选中已不在新 versions 列表中(被新一轮 :rewrite 重置版本)→ 回最新
  // chapterId 变更也走 useEffect 自动 reset(latestVersionId 切换 → 重跑)
  useEffect(() => {
    if (latestVersionId === null) {
      setSelectedVersionId(null);
      return;
    }
    setSelectedVersionId((prev) => {
      if (prev === null) return latestVersionId;
      const stillExists = versions.some((v) => v.version_id === prev);
      if (!stillExists) return latestVersionId;
      return prev;
    });
  }, [latestVersionId, versions]);

  const versionQuery = useChapterVersionQuery(
    props.workspaceId,
    props.chapterId,
    selectedVersionId,
  );

  const displayedCount = versionQuery.data?.word_count ?? null;
  const onWordCountChange = props.onWordCountChange;
  useEffect(() => {
    onWordCountChange?.(displayedCount);
  }, [displayedCount, onWordCountChange]);

  // v1.1 acceptance 修复2:人工编辑模式(canManualEdit 才可进)。
  // draft 初始化 = 进入编辑时当前显示的版本正文;保存成功 → 退出编辑 +
  // 跳转选中新版本;取消 → 丢弃 draft 原样退出。章节切换自动退出。
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  useEffect(() => {
    setEditing(false);
  }, [props.chapterId]);
  const currentContent = versionQuery.data?.content;
  const { onSaveContent } = props;
  const handleSave = useCallback(() => {
    if (onSaveContent === undefined) return;
    void onSaveContent(draft).then((out) => {
      setEditing(false);
      setSelectedVersionId(out.version_id);
    });
    // 失败路径:页面层 mutation onError 已 toast,编辑态保留供用户重试。
  }, [onSaveContent, draft]);

  const versionsLoading =
    versionsQuery.isPending && versionsQuery.fetchStatus !== "idle";
  const versionsError = versionsQuery.error;
  const versionsEmpty =
    !versionsQuery.isPending &&
    (versionsQuery.data?.versions.length ?? 0) === 0;
  const isLatestSelected =
    selectedVersionId !== null && selectedVersionId === latestVersionId;

  return (
    <section
      data-testid="chapter-content-panel"
      className="notranslate flex h-full min-w-0 flex-col rounded border border-border bg-card"
      translate="no"
    >
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-4 py-2">
        <h2 className="text-sm font-medium">章节正文</h2>
        {!versionsLoading && !versionsError && versions.length > 0 ? (
          <VersionSelector
            versions={versions}
            selectedVersionId={selectedVersionId}
            onSelect={setSelectedVersionId}
          />
        ) : null}
        <VersionLabel
          versionsLoading={versionsLoading}
          versionsError={versionsError}
          selectedVersionId={selectedVersionId}
          isLatest={isLatestSelected}
          wordCount={versionQuery.data?.word_count}
        />
        {props.canManualEdit === true &&
        !editing &&
        currentContent !== undefined ? (
          <button
            type="button"
            data-testid="chapter-content-edit-button"
            className="ml-auto rounded border border-border px-2 py-0.5 text-xs hover:bg-accent"
            onClick={() => {
              setDraft(currentContent);
              setEditing(true);
            }}
          >
            编辑正文
          </button>
        ) : null}
        {editing ? (
          <span className="ml-auto flex items-center gap-2">
            <button
              type="button"
              data-testid="chapter-content-save-button"
              className="rounded bg-primary px-2 py-0.5 text-xs text-primary-foreground hover:opacity-90 disabled:opacity-50"
              disabled={props.isSavePending === true || draft.length === 0}
              onClick={handleSave}
            >
              {props.isSavePending === true ? "保存中…" : "保存为新版本"}
            </button>
            <button
              type="button"
              data-testid="chapter-content-cancel-button"
              className="rounded border border-border px-2 py-0.5 text-xs hover:bg-accent"
              disabled={props.isSavePending === true}
              onClick={() => setEditing(false)}
            >
              取消
            </button>
          </span>
        ) : null}
      </header>
      {editing ? (
        <textarea
          data-testid="chapter-content-edit-textarea"
          className="flex-1 resize-none bg-background p-6 font-mono text-sm leading-relaxed text-foreground outline-none"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          disabled={props.isSavePending === true}
        />
      ) : (
        <PanelBody
          versionsLoading={versionsLoading}
          versionsError={versionsError}
          versionsEmpty={versionsEmpty}
          selectedVersionId={selectedVersionId}
          versionLoading={
            versionQuery.isPending && versionQuery.fetchStatus !== "idle"
          }
          versionError={versionQuery.error}
          content={currentContent}
        />
      )}
    </section>
  );
}

interface VersionLabelProps {
  versionsLoading: boolean;
  versionsError: unknown;
  selectedVersionId: number | null;
  isLatest: boolean;
  wordCount: number | undefined;
}

function VersionLabel(props: VersionLabelProps) {
  if (props.versionsLoading || props.versionsError) return null;
  if (props.selectedVersionId === null) {
    return (
      <span className="text-xs text-muted-foreground">尚无版本</span>
    );
  }
  const wc =
    typeof props.wordCount === "number" ? `${props.wordCount} 字符` : "";
  const tag = props.isLatest ? "（最新）" : "";
  return (
    <span
      data-testid="chapter-version-label"
      className="text-xs text-muted-foreground"
    >
      {tag}
      {wc ? ` · ${wc}` : ""}
    </span>
  );
}

interface PanelBodyProps {
  versionsLoading: boolean;
  versionsError: unknown;
  versionsEmpty: boolean;
  selectedVersionId: number | null;
  versionLoading: boolean;
  versionError: unknown;
  content: string | undefined;
}

function PanelBody(props: PanelBodyProps) {
  if (props.versionsLoading) {
    return (
      <div className="p-4 text-sm text-muted-foreground">
        加载版本号列表中…
      </div>
    );
  }
  if (props.versionsError) {
    const message = formatError(props.versionsError);
    return (
      <div
        data-testid="chapter-versions-error"
        className="p-4 text-sm text-destructive"
      >
        加载版本号列表失败:{message}
      </div>
    );
  }
  if (props.versionsEmpty || props.selectedVersionId === null) {
    return (
      <div
        data-testid="chapter-content-empty"
        className="p-4 text-sm text-muted-foreground"
      >
        尚未写作。后续 P1-F-14 章节编辑器工具栏可触发 :write 生成首版。
      </div>
    );
  }
  if (props.versionLoading) {
    return (
      <div className="p-4 text-sm text-muted-foreground">加载正文中…</div>
    );
  }
  if (props.versionError) {
    const message = formatError(props.versionError);
    return (
      <div
        data-testid="chapter-version-error"
        className="p-4 text-sm text-destructive"
      >
        加载正文失败:{message}
      </div>
    );
  }
  if (props.content === undefined) {
    return (
      <div className="p-4 text-sm text-muted-foreground">没有正文数据。</div>
    );
  }
  // v1.0.x B2-03:`key={props.selectedVersionId}` 强制版本切换时整棵 article
  // 子树 unmount + 新版 remount。`<ReactMarkdown>` 切 content 字符串时内部
  // 会 reconcile 上一版生成的 ast(h1/p/ul/li/code…)与本版 ast,版本间结构
  // 差异大时(短开篇 → 长章 + 列表 / 代码块)曾撞 NotFoundError "Failed to
  // execute 'removeChild' on 'Node': The node to be removed is not a child"
  // ——key 变化让 React 走 unmount→remount 路径,绕开跨版本 fiber diff。
  return (
    <article
      key={props.selectedVersionId}
      data-testid="chapter-content-markdown"
      className="prose-style flex-1 overflow-auto p-6 text-sm leading-relaxed text-foreground"
    >
      <ReactMarkdown>{props.content}</ReactMarkdown>
    </article>
  );
}

function formatError(error: unknown): string {
  if (isApiError(error)) {
    return `${error.code}: ${error.message}`;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "未知错误";
}
