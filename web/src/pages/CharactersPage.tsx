/**
 * CharactersPage(P2-F-02 / P2-F-04):角色列表 + 增删改入口。
 *
 * 路由:`/characters`。列出当前活动工作区的全部角色。表格四列(P2-F-02
 * 既有)+ actions 列(P2-F-04 起):
 *
 * - 名称(`name`)
 * - 原型(`archetype`)
 * - 首次出场章(`first_appearance_chapter` ?? "—")
 * - 最近更新章(`last_updated_chapter` ?? "—")
 * - 操作:编辑 / 删除(P2-F-04)
 *
 * P2-F-04 入口:
 *
 * - 页顶"新增角色"按钮 → `CharacterFormDialog mode="create"` 空白全字段表单
 * - 行 "编辑" → `CharacterFormDialog mode="edit"` 带值打开
 * - 行 "删除" → `DeleteCharacterConfirmDialog` 客户端聚合 ChapterCard
 *   `related_characters` 显示引用数(>0 红色警告但仍允许删)
 *
 * 行点击仍打开 `CharacterDetailDrawer`(P2-F-02 既有,只读视图 + Drawer 内
 * 切换到 voice-only 编辑—— P2-F-03 沉淀)。Dialog 全字段编辑 + Drawer
 * voice-only 编辑两条路径并存:Dialog 是严肃全字段修改;Drawer 是 voice
 * 快编辑(自决项 1)。
 *
 * actions 列内的按钮 `onClick` 调 `event.stopPropagation()` 避免冒泡到行
 * 点击触发 Drawer 打开。
 */

import * as React from "react";
import { Link } from "react-router-dom";
import { useQueries } from "@tanstack/react-query";

import { CHAPTER_CARD_QUERY_KEY, useChapterListQuery } from "@/api/chapters";
import {
  useCharacterListQuery,
  useDeleteCharacterMutation,
  type CharacterOut,
} from "@/api/characters";
import { apiGet } from "@/api/client";
import { isApiError } from "@/api/errors";
import type { components } from "@/api/types";
import {
  useWorkspacesQuery,
  type WorkspaceOut,
} from "@/api/workspaces";
import { CharacterDetailDrawer } from "@/components/CharacterDetailDrawer";
import { CharacterFormDialog } from "@/components/CharacterFormDialog";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { useActiveWorkspace } from "@/store/activeWorkspace";

type ChapterCardOut = components["schemas"]["ChapterCardOut"];

export default function CharactersPage() {
  const workspacesQuery = useWorkspacesQuery();
  const activeWorkspace = useActiveWorkspace(workspacesQuery.data);
  const workspaceId = activeWorkspace?.workspace_id ?? null;
  const charactersQuery = useCharacterListQuery(workspaceId);

  const [selectedCharacterId, setSelectedCharacterId] = React.useState<
    string | null
  >(null);
  const [drawerOpen, setDrawerOpen] = React.useState(false);

  const [createOpen, setCreateOpen] = React.useState(false);
  const [editTarget, setEditTarget] = React.useState<CharacterOut | null>(null);
  const [deleteTarget, setDeleteTarget] = React.useState<CharacterOut | null>(
    null,
  );

  const handleRowClick = (characterId: string) => {
    setSelectedCharacterId(characterId);
    setDrawerOpen(true);
  };

  const handleEditClick = (character: CharacterOut) => {
    setEditTarget(character);
  };

  const handleDeleteClick = (character: CharacterOut) => {
    setDeleteTarget(character);
  };

  const onDeleteConfirmed = () => {
    // 若被删的角色正打开抽屉,关掉避免抽屉读 stale cache 报 404。
    if (deleteTarget && selectedCharacterId === deleteTarget.character_id) {
      setDrawerOpen(false);
      setSelectedCharacterId(null);
    }
  };

  return (
    <div className="space-y-4">
      <header className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold">角色</h1>
          <CharactersHeaderHint
            workspacesLoading={workspacesQuery.isPending}
            workspacesError={workspacesQuery.error}
            activeWorkspace={activeWorkspace}
          />
        </div>
        <Button
          onClick={() => setCreateOpen(true)}
          disabled={!workspaceId}
          data-testid="character-create-button"
        >
          新增角色
        </Button>
      </header>

      <CharactersContent
        workspacesLoading={workspacesQuery.isPending}
        workspacesError={workspacesQuery.error}
        workspacesEmpty={
          !workspacesQuery.isPending &&
          (workspacesQuery.data?.length ?? 0) === 0
        }
        activeWorkspace={activeWorkspace}
        charactersLoading={
          charactersQuery.isPending && charactersQuery.fetchStatus !== "idle"
        }
        charactersError={charactersQuery.error}
        characters={charactersQuery.data ?? []}
        onRowClick={handleRowClick}
        onEditClick={handleEditClick}
        onDeleteClick={handleDeleteClick}
      />

      <CharacterDetailDrawer
        open={drawerOpen}
        onOpenChange={setDrawerOpen}
        workspaceId={workspaceId}
        characterId={selectedCharacterId}
      />

      {workspaceId && createOpen && (
        <CharacterFormDialog
          open={createOpen}
          onOpenChange={setCreateOpen}
          workspaceId={workspaceId}
          mode="create"
        />
      )}

      {workspaceId && editTarget && (
        <CharacterFormDialog
          open={editTarget !== null}
          onOpenChange={(next) => {
            if (!next) setEditTarget(null);
          }}
          workspaceId={workspaceId}
          mode="edit"
          character={editTarget}
        />
      )}

      {workspaceId && deleteTarget && (
        <DeleteCharacterConfirmDialog
          workspaceId={workspaceId}
          target={deleteTarget}
          onClose={() => setDeleteTarget(null)}
          onDeleted={onDeleteConfirmed}
        />
      )}
    </div>
  );
}

interface CharactersHeaderHintProps {
  workspacesLoading: boolean;
  workspacesError: unknown;
  activeWorkspace: WorkspaceOut | null;
}

function CharactersHeaderHint(props: CharactersHeaderHintProps) {
  if (props.workspacesLoading || props.workspacesError) {
    return null;
  }
  if (!props.activeWorkspace) {
    return (
      <p className="text-sm text-muted-foreground">当前没有活动工作区。</p>
    );
  }
  return (
    <p className="text-sm text-muted-foreground">
      当前活动工作区:
      <span
        className="ml-1 font-medium text-foreground"
        data-testid="active-workspace-display-name"
      >
        {props.activeWorkspace.display_name}
      </span>
      <span className="ml-1 text-xs">
        ({props.activeWorkspace.workspace_id})
      </span>
    </p>
  );
}

interface CharactersContentProps {
  workspacesLoading: boolean;
  workspacesError: unknown;
  workspacesEmpty: boolean;
  activeWorkspace: WorkspaceOut | null;
  charactersLoading: boolean;
  charactersError: unknown;
  characters: readonly CharacterOut[];
  onRowClick: (characterId: string) => void;
  onEditClick: (character: CharacterOut) => void;
  onDeleteClick: (character: CharacterOut) => void;
}

function CharactersContent(props: CharactersContentProps) {
  if (props.workspacesLoading) {
    return (
      <div className="rounded border border-border p-6 text-sm text-muted-foreground">
        加载工作区列表中…
      </div>
    );
  }
  if (props.workspacesError) {
    return (
      <div
        data-testid="characters-workspaces-error"
        className="rounded border border-destructive bg-destructive/10 p-6 text-sm text-destructive"
      >
        加载工作区失败:{formatError(props.workspacesError)}
      </div>
    );
  }
  if (props.workspacesEmpty) {
    return (
      <div className="rounded border border-dashed border-border p-6 text-sm text-muted-foreground">
        还没有工作区。请先到
        <Link to="/workspaces" className="mx-1 underline">
          工作区列表
        </Link>
        创建一个。
      </div>
    );
  }
  if (!props.activeWorkspace) {
    return (
      <div className="rounded border border-dashed border-border p-6 text-sm text-muted-foreground">
        当前没有活动工作区。请到
        <Link to="/workspaces" className="mx-1 underline">
          工作区列表
        </Link>
        切换到一个工作区。
      </div>
    );
  }
  if (props.charactersLoading) {
    return (
      <div className="rounded border border-border p-6 text-sm text-muted-foreground">
        加载角色列表中…
      </div>
    );
  }
  if (props.charactersError) {
    return (
      <div
        data-testid="characters-list-error"
        className="rounded border border-destructive bg-destructive/10 p-6 text-sm text-destructive"
      >
        加载角色失败:{formatError(props.charactersError)}
      </div>
    );
  }
  if (props.characters.length === 0) {
    return (
      <div
        data-testid="characters-empty"
        className="rounded border border-dashed border-border p-6 text-sm text-muted-foreground"
      >
        还没有角色。点页顶"新增角色"创建,或用
        <code className="mx-1">novel workspace import-assets</code>
        CLI 批量导入(P2-B-13 静态资产导入工具)。
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded border border-border">
      <table className="w-full text-sm" data-testid="characters-table">
        <thead className="bg-muted/40 text-left text-xs uppercase tracking-wide text-muted-foreground">
          <tr>
            <th className="px-4 py-2 font-medium">名称</th>
            <th className="px-4 py-2 font-medium">原型</th>
            <th className="px-4 py-2 font-medium">首次出场章</th>
            <th className="px-4 py-2 font-medium">最近更新章</th>
            <th className="px-4 py-2 font-medium text-right">操作</th>
          </tr>
        </thead>
        <tbody>
          {props.characters.map((character) => (
            <CharacterRow
              key={character.character_id}
              character={character}
              onClick={props.onRowClick}
              onEdit={props.onEditClick}
              onDelete={props.onDeleteClick}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface CharacterRowProps {
  character: CharacterOut;
  onClick: (characterId: string) => void;
  onEdit: (character: CharacterOut) => void;
  onDelete: (character: CharacterOut) => void;
}

function CharacterRow(props: CharacterRowProps) {
  const { character, onClick, onEdit, onDelete } = props;
  const handleKeyDown = (event: React.KeyboardEvent<HTMLTableRowElement>) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onClick(character.character_id);
    }
  };
  return (
    <tr
      data-testid="character-row"
      data-character-id={character.character_id}
      tabIndex={0}
      role="button"
      onClick={() => onClick(character.character_id)}
      onKeyDown={handleKeyDown}
      className="cursor-pointer border-t border-border transition-colors hover:bg-emerald-50/30 focus:outline-none focus:ring-2 focus:ring-ring"
    >
      <td className="px-4 py-2">
        <div className="font-medium" data-testid="character-name">
          {character.name}
        </div>
        <div className="text-xs text-muted-foreground">
          {character.character_id}
        </div>
      </td>
      <td className="px-4 py-2" data-testid="character-archetype">
        {character.archetype || "—"}
      </td>
      <td
        className="px-4 py-2 text-muted-foreground"
        data-testid="character-first-appearance"
      >
        {character.first_appearance_chapter ?? "—"}
      </td>
      <td
        className="px-4 py-2 text-muted-foreground"
        data-testid="character-last-updated"
      >
        {character.last_updated_chapter ?? "—"}
      </td>
      <td className="px-4 py-2 text-right">
        <div className="inline-flex gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            data-testid="character-edit"
            onClick={(event) => {
              event.stopPropagation();
              onEdit(character);
            }}
          >
            编辑
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            data-testid="character-delete"
            onClick={(event) => {
              event.stopPropagation();
              onDelete(character);
            }}
          >
            删除
          </Button>
        </div>
      </td>
    </tr>
  );
}

interface DeleteCharacterConfirmDialogProps {
  workspaceId: string;
  target: CharacterOut;
  onClose: () => void;
  onDeleted: () => void;
}

/**
 * 删除前确认对话框。打开瞬间触发 `useQueries` 批量拿全工作区的 ChapterCard,
 * 统计 `related_characters` 含目标 character_id 的章节数量。
 *
 * 引用计数策略(P2-F-04 §2 自决项 4):
 * - 章节列表 query 与列表页常驻 query 同 queryKey,实际上 P1 列表已加载只
 *   命中缓存
 * - 单卡 query 复用 `CHAPTER_CARD_QUERY_KEY`,与 ChapterEditorPage 共享缓存
 *   (再次打开同章 editor 不需重取)
 * - 全部 cards 加载完成前 Dialog 显示"统计中…",完成后显示精确数;`count > 0`
 *   红色警示但仍允许删除(后端不阻拦,作者可能确实想删)
 */
function DeleteCharacterConfirmDialog(
  props: DeleteCharacterConfirmDialogProps,
) {
  const { workspaceId, target, onClose, onDeleted } = props;
  const chaptersQuery = useChapterListQuery(workspaceId);
  const chapterIds = React.useMemo(
    () => (chaptersQuery.data ?? []).map((c) => c.chapter_id),
    [chaptersQuery.data],
  );

  const cardsQueries = useQueries({
    queries: chapterIds.map((chapterId) => ({
      queryKey: CHAPTER_CARD_QUERY_KEY(workspaceId, chapterId),
      queryFn: () =>
        apiGet<ChapterCardOut>(
          `/chapters/${encodeURIComponent(chapterId)}/card`,
          { workspaceId },
        ),
      enabled: chapterIds.length > 0,
    })),
  });

  const isCounting =
    chaptersQuery.isPending ||
    cardsQueries.some((q) => q.isPending && q.fetchStatus !== "idle");

  const referencingChapters = React.useMemo(() => {
    const out: string[] = [];
    cardsQueries.forEach((q) => {
      const card = q.data;
      if (card && card.related_characters.includes(target.character_id)) {
        out.push(card.chapter_id);
      }
    });
    return out;
  }, [cardsQueries, target.character_id]);

  const deleteMutation = useDeleteCharacterMutation();

  const handleConfirm = () => {
    deleteMutation.mutate(
      { workspaceId, characterId: target.character_id },
      {
        onSuccess: () => {
          showSuccessToast(
            "已删除",
            `角色 ${target.name}(${target.character_id})已删除`,
          );
          onDeleted();
          onClose();
        },
        onError: (err) => showApiErrorToast(err),
      },
    );
  };

  const description = (
    <span data-testid="character-delete-reference-count">
      即将删除角色 <strong>{target.name}</strong>(
      <code>{target.character_id}</code>)。
      {isCounting ? (
        <span className="block pt-2 text-muted-foreground">统计引用中…</span>
      ) : referencingChapters.length === 0 ? (
        <span className="block pt-2 text-muted-foreground">
          该角色暂未被任何章节引用,删除安全。
        </span>
      ) : (
        <span className="block pt-2 font-medium text-destructive">
          ⚠ 该角色被 {referencingChapters.length} 章引用(
          {referencingChapters.slice(0, 3).join("、")}
          {referencingChapters.length > 3 ? " 等" : ""}
          )。删除后这些章节卡的 related_characters 字段将留下悬空 ID。
        </span>
      )}
    </span>
  );

  return (
    <ConfirmDialog
      open
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      title="确认删除角色"
      description={description}
      confirmLabel={deleteMutation.isPending ? "删除中…" : "删除"}
      cancelLabel="取消"
      variant="destructive"
      onConfirm={handleConfirm}
      isConfirmDisabled={deleteMutation.isPending}
    />
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
