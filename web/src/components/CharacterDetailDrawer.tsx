/**
 * 角色详情抽屉(P2-F-02 / P2-F-03)。
 *
 * 受控组件:`open` + `onOpenChange` 由 `CharactersPage` 控制,`characterId`
 * 决定抽屉读哪个角色。从右侧滑入的 side sheet,基于 Radix Dialog 原语 +
 * 自定义 `data-state` 样式(项目未抽 shadcn Sheet 原语,本任务不前置抽,
 * 直接在本组件内复用 Radix Dialog Portal/Overlay/Content,避免新增 ui/ 文件)。
 *
 * 显示态:
 * - 加载中 → "加载中…"
 * - 加载失败 → 红色错误条(`isApiError` 翻译 code/message,跨工作区 / 不存在
 *   走 404 `CHARACTER_NOT_FOUND`,见 P2-B-14 §2 决策 1)
 * - 加载完成:
 *   - 只读态(默认) → 字段分组只读视图 + footer "切换编辑" 按钮
 *   - 编辑态(P2-F-03) → `CharacterEditForm` 包 `VoiceProfileEditor`,自带
 *     "取消 / 保存" 按钮,footer 不显示(避免双控按钮)。本任务范围内只
 *     允许编辑 VoiceProfile 字段;其他 Character 字段透传 PUT body 全量
 *     (P2-B-14 §2 决策 3),编辑入口留 P2-F-04。
 */

import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";

import {
  useCharacterDetailQuery,
  useUpdateCharacterMutation,
  type Character,
  type CharacterOut,
  type VoiceProfile,
} from "@/api/characters";
import { isApiError } from "@/api/errors";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { VoiceProfileEditor } from "@/components/VoiceProfileEditor";
import { cn } from "@/lib/utils";

export interface CharacterDetailDrawerProps {
  /** 受控:抽屉是否打开。 */
  open: boolean;
  /** 受控:打开 / 关闭状态变更(ESC / 遮罩 / 关闭按钮)。 */
  onOpenChange: (open: boolean) => void;
  /** 当前活动工作区 ID(从父级 page 透传,此处不读 zustand 保持组件可复用)。 */
  workspaceId: string | null;
  /** 选中要展示的角色 ID;为 null 时不发请求。 */
  characterId: string | null;
}

export function CharacterDetailDrawer(props: CharacterDetailDrawerProps) {
  const { open, onOpenChange, workspaceId, characterId } = props;
  // 编辑态在打开时复位为 false;关闭抽屉时也复位,避免下次打开仍处编辑态。
  const [isEditing, setIsEditing] = React.useState(false);
  React.useEffect(() => {
    if (!open) {
      setIsEditing(false);
    }
  }, [open]);

  const detailQuery = useCharacterDetailQuery(workspaceId, characterId);
  const showFooter = !isEditing; // 编辑态由内部 form 自带按钮,避免双控

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay
          className={cn(
            "fixed inset-0 z-50 bg-black/60",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0",
          )}
        />
        <DialogPrimitive.Content
          data-testid="character-detail-drawer"
          className={cn(
            "fixed right-0 top-0 z-50 flex h-full w-full max-w-xl flex-col gap-4 border-l border-border bg-background p-6 shadow-xl",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=closed]:slide-out-to-right data-[state=open]:slide-in-from-right",
            "duration-200",
          )}
        >
          <div className="flex items-start justify-between gap-4">
            <div className="space-y-1">
              <DialogPrimitive.Title className="text-lg font-semibold">
                角色详情
              </DialogPrimitive.Title>
              <DialogPrimitive.Description className="text-xs text-muted-foreground">
                {characterId ? `ID: ${characterId}` : "未选中角色"}
              </DialogPrimitive.Description>
            </div>
            <DialogPrimitive.Close
              className="rounded-sm opacity-70 transition-opacity hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring"
              aria-label="关闭"
            >
              <X className="h-4 w-4" />
            </DialogPrimitive.Close>
          </div>

          <div className="flex-1 overflow-y-auto pr-1">
            <DrawerBody
              detailQuery={detailQuery}
              isEditing={isEditing}
              workspaceId={workspaceId}
              onCancelEdit={() => setIsEditing(false)}
              onSaved={() => setIsEditing(false)}
            />
          </div>

          {showFooter && (
            <div className="flex items-center justify-end gap-2 border-t border-border pt-3">
              <Button
                variant="default"
                onClick={() => setIsEditing(true)}
                disabled={!detailQuery.data}
                data-testid="character-edit-toggle"
              >
                切换编辑
              </Button>
            </div>
          )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

interface DrawerBodyProps {
  detailQuery: ReturnType<typeof useCharacterDetailQuery>;
  isEditing: boolean;
  workspaceId: string | null;
  onCancelEdit: () => void;
  onSaved: () => void;
}

function DrawerBody(props: DrawerBodyProps) {
  const { detailQuery, isEditing, workspaceId, onCancelEdit, onSaved } = props;

  if (detailQuery.isPending && detailQuery.fetchStatus !== "idle") {
    return (
      <p className="text-sm text-muted-foreground" data-testid="drawer-loading">
        加载中…
      </p>
    );
  }
  if (detailQuery.error) {
    return (
      <div
        className="rounded border border-destructive bg-destructive/10 p-3 text-sm text-destructive"
        data-testid="drawer-error"
      >
        加载角色失败:{formatError(detailQuery.error)}
      </div>
    );
  }
  if (!detailQuery.data) {
    return (
      <p className="text-sm text-muted-foreground">未选中角色或参数不全。</p>
    );
  }

  if (isEditing) {
    if (!workspaceId) {
      // 理论不会走到:进入编辑前 detailQuery.data 必有,workspaceId 也必有
      return (
        <p className="text-sm text-destructive">缺少活动工作区,无法编辑。</p>
      );
    }
    return (
      <CharacterEditForm
        character={detailQuery.data}
        workspaceId={workspaceId}
        onCancel={onCancelEdit}
        onSaved={onSaved}
      />
    );
  }

  return <CharacterReadOnlyView character={detailQuery.data} />;
}

interface CharacterEditFormProps {
  character: CharacterOut;
  workspaceId: string;
  onCancel: () => void;
  onSaved: () => void;
}

/**
 * P2-F-03 范围内只编辑 VoiceProfile 字段,其他 Character 字段透传(PUT
 * body 全量,见 P2-B-14 §2 决策 3)。基础字段编辑入口留 P2-F-04。
 *
 * 不引入 react-hook-form / zodResolver:本表单只 3 个必填字符串,手写 onSubmit
 * 时校验 + setErrors 已经够用;引 RHF 反而要为单字段嵌套 Controller,行数
 * 更多。CreateWorkspaceDialog 用 RHF 是因为有字符集 / 数字转换等多种 zod
 * 校验。
 */
function CharacterEditForm(props: CharacterEditFormProps) {
  const { character, workspaceId, onCancel, onSaved } = props;
  const [voice, setVoice] = React.useState<VoiceProfile>(character.voice_profile);
  const [errors, setErrors] = React.useState<
    Partial<Record<keyof VoiceProfile, string>>
  >({});
  const updateMutation = useUpdateCharacterMutation();

  const validate = (v: VoiceProfile) => {
    const next: Partial<Record<keyof VoiceProfile, string>> = {};
    if (!v.tone.trim()) next.tone = "语气不能为空";
    if (!v.sentence_style.trim()) next.sentence_style = "句式不能为空";
    if (!v.self_address.trim()) next.self_address = "自称不能为空";
    return next;
  };

  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const errs = validate(voice);
    if (Object.keys(errs).length > 0) {
      setErrors(errs);
      return;
    }
    setErrors({});
    // 透传非 voice 字段,只覆盖 voice_profile。CharacterOut 与 Character schema
    // 字段同形(P2-B-14 CharacterOut 是空子类),直接展开即可。
    const payload: Character = { ...character, voice_profile: voice };
    updateMutation.mutate(
      { workspaceId, character: payload },
      {
        onSuccess: () => {
          showSuccessToast(
            "已保存",
            `角色 ${character.name}(${character.character_id})VoiceProfile 已更新`,
          );
          onSaved();
        },
        onError: (err) => showApiErrorToast(err),
      },
    );
  };

  const isSubmitting = updateMutation.isPending;

  return (
    <form
      onSubmit={handleSubmit}
      className="space-y-4"
      data-testid="character-edit-form"
      noValidate
    >
      <p className="rounded border border-dashed border-border p-2 text-xs text-muted-foreground">
        P2-F-03 仅 VoiceProfile 字段可编辑;基础字段 / 关系 / 能力等编辑入口将在
        P2-F-04 接入。
      </p>
      <section className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          VoiceProfile
        </h3>
        <VoiceProfileEditor
          value={voice}
          onChange={setVoice}
          disabled={isSubmitting}
          errors={errors}
        />
      </section>
      <div className="flex items-center justify-end gap-2 border-t border-border pt-3">
        <Button
          type="button"
          variant="outline"
          disabled={isSubmitting}
          onClick={onCancel}
        >
          取消
        </Button>
        <Button
          type="submit"
          disabled={isSubmitting}
          data-testid="character-save"
        >
          {isSubmitting ? "保存中…" : "保存"}
        </Button>
      </div>
    </form>
  );
}

interface CharacterReadOnlyViewProps {
  character: CharacterOut;
}

function CharacterReadOnlyView(props: CharacterReadOnlyViewProps) {
  const c = props.character;
  return (
    <div className="space-y-5 text-sm" data-testid="character-readonly-view">
      <Section title="基础信息">
        <Field label="名称" value={c.name} />
        <Field label="原型" value={c.archetype} />
        <Field label="别名" value={formatList(c.aliases)} />
        <Field label="首次出场章" value={c.first_appearance_chapter ?? "—"} />
        <Field label="最近更新章" value={c.last_updated_chapter ?? "—"} />
      </Section>
      <Section title="外貌 / 性格 / 背景">
        <Paragraph label="外貌" value={c.appearance} />
        <Paragraph label="性格" value={c.personality} />
        <Paragraph label="背景" value={c.background} />
      </Section>
      <Section title="目标">
        <Field label="长期" value={formatList(c.goals_long_term)} />
        <Field label="短期" value={formatList(c.goals_short_term)} />
      </Section>
      <Section title="VoiceProfile">
        <VoiceProfileReadOnly voice={c.voice_profile} />
      </Section>
      {c.relationships && Object.keys(c.relationships).length > 0 ? (
        <Section title="关系">
          <KvList kv={c.relationships} />
        </Section>
      ) : null}
      {c.abilities && c.abilities.length > 0 ? (
        <Section title="能力">
          <Field label="能力列表" value={formatList(c.abilities)} />
        </Section>
      ) : null}
      {c.secrets && c.secrets.length > 0 ? (
        <Section title="秘密(作者侧)">
          <Field label="条目" value={formatList(c.secrets)} />
        </Section>
      ) : null}
    </div>
  );
}

interface VoiceProfileReadOnlyProps {
  voice: VoiceProfile;
}

function VoiceProfileReadOnly(props: VoiceProfileReadOnlyProps) {
  const v = props.voice;
  return (
    <>
      <Field label="语气" value={v.tone} />
      <Field label="句式" value={v.sentence_style} />
      <Field label="自称" value={v.self_address} />
      <Field label="口头禅" value={formatList(v.catchphrases)} />
      <Field label="禁用词" value={formatList(v.forbidden_phrases)} />
      {v.addresses_others && Object.keys(v.addresses_others).length > 0 ? (
        <div className="space-y-1">
          <p className="text-xs font-medium text-muted-foreground">称呼他人</p>
          <KvList kv={v.addresses_others} />
        </div>
      ) : null}
    </>
  );
}

function Section(props: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {props.title}
      </h3>
      <div className="space-y-1.5 rounded border border-border bg-card/30 p-3">
        {props.children}
      </div>
    </section>
  );
}

function Field(props: { label: string; value: string }) {
  return (
    <div className="grid grid-cols-[6rem_1fr] gap-2">
      <span className="text-xs text-muted-foreground">{props.label}</span>
      <span className="break-words">{props.value || "—"}</span>
    </div>
  );
}

function Paragraph(props: { label: string; value: string }) {
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-muted-foreground">{props.label}</p>
      <p className="whitespace-pre-wrap break-words">{props.value || "—"}</p>
    </div>
  );
}

function KvList(props: { kv: Record<string, string> }) {
  return (
    <ul className="space-y-1">
      {Object.entries(props.kv).map(([k, v]) => (
        <li key={k} className="grid grid-cols-[8rem_1fr] gap-2">
          <span className="text-xs text-muted-foreground">{k}</span>
          <span className="break-words">{v}</span>
        </li>
      ))}
    </ul>
  );
}

function formatList(value: string[] | null | undefined): string {
  if (!value || value.length === 0) {
    return "—";
  }
  return value.join("、");
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
