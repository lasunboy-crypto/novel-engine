/**
 * 角色新增 / 全字段编辑 Dialog(P2-F-04)。
 *
 * 受控组件,基于 shadcn `Dialog` 原语 + RHF + zodResolver(基础字段)+
 * `useState`(数组 / 字典 / VoiceProfile 受控值字段)。两条入口都走本组件:
 *
 * - **新增**(`mode="create"`):页顶"新增角色"按钮 → 空白表单
 * - **编辑**(`mode="edit"`,`character` 必传):行 actions 菜单"编辑" →
 *   带值打开,`character_id` 改为只读(后端 PUT 不允许改 ID,改 ID 等价 delete
 *   + create)
 *
 * 字段范围(P2-F-04 §2 自决项 2):
 *
 * - 暴露:character_id(create-only)/ name / archetype / appearance /
 *   personality / background / aliases / goals_long_term / goals_short_term /
 *   abilities / secrets / relationships / voice_profile
 * - 透传(不暴露):first_appearance_chapter / last_updated_chapter /
 *   current_state——由 chapter mutation / MemoryExtractor 等后端流程自动同步,
 *   前端编辑会破坏数据一致性
 *
 * VoiceProfile 校验沿用 P2-F-03 自决项 1(手写 onSubmit 校验):语气 / 句式 /
 * 自称三必填字符串,空 → setVoiceErrors,不进 RHF 体系。
 */

import * as React from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";

import {
  useCreateCharacterMutation,
  useUpdateCharacterMutation,
  type Character,
  type CharacterOut,
  type VoiceProfile,
} from "@/api/characters";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  KvEditor,
  TagInput,
  VoiceProfileEditor,
} from "@/components/VoiceProfileEditor";
import { cn } from "@/lib/utils";
import {
  characterFormSchema,
  type CharacterFormInput,
  type CharacterFormOutput,
} from "@/schemas/character";

const EMPTY_VOICE: VoiceProfile = {
  tone: "",
  sentence_style: "",
  self_address: "",
  catchphrases: [],
  forbidden_phrases: [],
  addresses_others: {},
};

type DialogMode = "create" | "edit";

export interface CharacterFormDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  workspaceId: string;
  mode: DialogMode;
  /** 编辑模式必传;新建模式忽略。 */
  character?: CharacterOut;
}

export function CharacterFormDialog(props: CharacterFormDialogProps) {
  const { open, onOpenChange, workspaceId, mode, character } = props;
  const isEdit = mode === "edit" && character !== undefined;

  const createMutation = useCreateCharacterMutation();
  const updateMutation = useUpdateCharacterMutation();

  // 数组 / 字典 / VoiceProfile 走 React state(VoiceProfileEditor / TagInput /
  // KvEditor 受控值组件)。RHF 只管 6 个 string 基础字段,简化 schema +
  // resolver。
  const [aliases, setAliases] = React.useState<string[]>([]);
  const [goalsLong, setGoalsLong] = React.useState<string[]>([]);
  const [goalsShort, setGoalsShort] = React.useState<string[]>([]);
  const [abilities, setAbilities] = React.useState<string[]>([]);
  const [secrets, setSecrets] = React.useState<string[]>([]);
  const [relationships, setRelationships] = React.useState<
    Record<string, string>
  >({});
  const [voice, setVoice] = React.useState<VoiceProfile>(EMPTY_VOICE);
  const [voiceErrors, setVoiceErrors] = React.useState<
    Partial<Record<keyof VoiceProfile, string>>
  >({});

  const defaultValues: CharacterFormInput = React.useMemo(
    () => ({
      character_id: character?.character_id ?? "",
      name: character?.name ?? "",
      archetype: character?.archetype ?? "",
      appearance: character?.appearance ?? "",
      personality: character?.personality ?? "",
      background: character?.background ?? "",
    }),
    [character],
  );

  const form = useForm<
    CharacterFormInput,
    undefined,
    CharacterFormOutput
  >({
    resolver: zodResolver(characterFormSchema),
    defaultValues,
    mode: "onBlur",
  });

  // 打开对话框 / 切换 character 时重置整个表单 state(基础字段 RHF + 受控
  // 值组件 7 个 state)。关闭时不重置,等下次 open 触发再重置——避免关闭
  // 动画期间 form 闪空。
  React.useEffect(() => {
    if (!open) {
      return;
    }
    form.reset(defaultValues);
    setAliases(character?.aliases ?? []);
    setGoalsLong(character?.goals_long_term ?? []);
    setGoalsShort(character?.goals_short_term ?? []);
    setAbilities(character?.abilities ?? []);
    setSecrets(character?.secrets ?? []);
    setRelationships(character?.relationships ?? {});
    setVoice(character?.voice_profile ?? EMPTY_VOICE);
    setVoiceErrors({});
    // form 是稳定引用 (useForm 返回的对象 mutable 但引用稳定,RHF 文档建议
    // 不进 deps。defaultValues 借 useMemo 稳定。)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, character, defaultValues]);

  const validateVoice = (
    v: VoiceProfile,
  ): Partial<Record<keyof VoiceProfile, string>> => {
    const errs: Partial<Record<keyof VoiceProfile, string>> = {};
    if (!v.tone.trim()) errs.tone = "语气不能为空";
    if (!v.sentence_style.trim()) errs.sentence_style = "句式不能为空";
    if (!v.self_address.trim()) errs.self_address = "自称不能为空";
    return errs;
  };

  const onSubmit = (values: CharacterFormOutput) => {
    const vErrs = validateVoice(voice);
    if (Object.keys(vErrs).length > 0) {
      setVoiceErrors(vErrs);
      return;
    }
    setVoiceErrors({});

    const payload: Character = {
      character_id: isEdit ? character.character_id : values.character_id,
      workspace_id: workspaceId,
      name: values.name,
      archetype: values.archetype,
      appearance: values.appearance,
      personality: values.personality,
      background: values.background,
      aliases,
      goals_long_term: goalsLong,
      goals_short_term: goalsShort,
      abilities,
      secrets,
      relationships,
      voice_profile: voice,
      // 编辑时透传后端管理字段;新建时设默认空。
      first_appearance_chapter: character?.first_appearance_chapter ?? null,
      last_updated_chapter: character?.last_updated_chapter ?? null,
      current_state: character?.current_state ?? {},
    };

    if (isEdit) {
      updateMutation.mutate(
        { workspaceId, character: payload },
        {
          onSuccess: () => {
            showSuccessToast(
              "已保存",
              `角色 ${payload.name}(${payload.character_id})已更新`,
            );
            onOpenChange(false);
          },
          onError: (err) => showApiErrorToast(err),
        },
      );
    } else {
      createMutation.mutate(
        { workspaceId, character: payload },
        {
          onSuccess: () => {
            showSuccessToast(
              "已新增",
              `角色 ${payload.name}(${payload.character_id})已创建`,
            );
            onOpenChange(false);
          },
          onError: (err) => showApiErrorToast(err),
        },
      );
    }
  };

  const isSubmitting = createMutation.isPending || updateMutation.isPending;
  const errors = form.formState.errors;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{isEdit ? "编辑角色" : "新增角色"}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? "全字段编辑;角色 ID 在创建后不可修改。"
              : "填写新角色全字段;首次出场章 / 最近更新章由后端自动同步。"}
          </DialogDescription>
        </DialogHeader>
        <form
          onSubmit={form.handleSubmit(onSubmit)}
          className="space-y-4"
          noValidate
          data-testid="character-form"
        >
          <FormField
            label="角色 ID"
            error={errors.character_id?.message}
            hint={
              isEdit
                ? "创建后不可修改"
                : "工作区内唯一,长度 1-64;后端不要求字符集,但建议英文 / 数字 / -_"
            }
            htmlFor="cf-character-id"
            required
          >
            <Input
              id="cf-character-id"
              data-testid="cf-character-id"
              autoFocus={!isEdit}
              disabled={isSubmitting || isEdit}
              readOnly={isEdit}
              placeholder="例如:lin-yan"
              {...form.register("character_id")}
            />
          </FormField>

          <FormField
            label="名称"
            error={errors.name?.message}
            htmlFor="cf-name"
            required
          >
            <Input
              id="cf-name"
              data-testid="cf-name"
              disabled={isSubmitting}
              placeholder="例如:林衍"
              {...form.register("name")}
            />
          </FormField>

          <FormField
            label="原型"
            error={errors.archetype?.message}
            htmlFor="cf-archetype"
            required
          >
            <Input
              id="cf-archetype"
              data-testid="cf-archetype"
              disabled={isSubmitting}
              placeholder="例如:萌新剑修 / 反派宗师"
              {...form.register("archetype")}
            />
          </FormField>

          <FormField label="别名" htmlFor="cf-aliases">
            <TagInput
              id="cf-aliases"
              testId="cf-aliases"
              disabled={isSubmitting}
              value={aliases}
              onChange={setAliases}
              placeholder="回车或逗号添加"
            />
          </FormField>

          <FormField
            label="外貌"
            error={errors.appearance?.message}
            htmlFor="cf-appearance"
            required
          >
            <textarea
              id="cf-appearance"
              data-testid="cf-appearance"
              rows={2}
              disabled={isSubmitting}
              placeholder="外貌描述"
              className={textareaClass}
              {...form.register("appearance")}
            />
          </FormField>

          <FormField
            label="性格"
            error={errors.personality?.message}
            htmlFor="cf-personality"
            required
          >
            <textarea
              id="cf-personality"
              data-testid="cf-personality"
              rows={2}
              disabled={isSubmitting}
              placeholder="性格描述"
              className={textareaClass}
              {...form.register("personality")}
            />
          </FormField>

          <FormField
            label="背景"
            error={errors.background?.message}
            htmlFor="cf-background"
            required
          >
            <textarea
              id="cf-background"
              data-testid="cf-background"
              rows={3}
              disabled={isSubmitting}
              placeholder="背景故事"
              className={textareaClass}
              {...form.register("background")}
            />
          </FormField>

          <FormField label="长期目标" htmlFor="cf-goals-long">
            <TagInput
              id="cf-goals-long"
              testId="cf-goals-long"
              disabled={isSubmitting}
              value={goalsLong}
              onChange={setGoalsLong}
              placeholder="一条目标一个标签"
            />
          </FormField>

          <FormField label="短期目标" htmlFor="cf-goals-short">
            <TagInput
              id="cf-goals-short"
              testId="cf-goals-short"
              disabled={isSubmitting}
              value={goalsShort}
              onChange={setGoalsShort}
              placeholder="一条目标一个标签"
            />
          </FormField>

          <FormField label="能力" htmlFor="cf-abilities">
            <TagInput
              id="cf-abilities"
              testId="cf-abilities"
              disabled={isSubmitting}
              value={abilities}
              onChange={setAbilities}
              placeholder="一项能力一个标签"
            />
          </FormField>

          <FormField label="秘密(作者侧)" htmlFor="cf-secrets">
            <TagInput
              id="cf-secrets"
              testId="cf-secrets"
              disabled={isSubmitting}
              value={secrets}
              onChange={setSecrets}
              placeholder="一条秘密一个标签"
            />
          </FormField>

          <FormField label="关系" htmlFor="cf-relationships">
            <KvEditor
              testId="cf-relationships"
              disabled={isSubmitting}
              value={relationships}
              onChange={setRelationships}
              keyPlaceholder="对方角色 ID"
              valuePlaceholder="关系描述"
            />
          </FormField>

          <section className="space-y-2">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              VoiceProfile
            </h3>
            <VoiceProfileEditor
              value={voice}
              onChange={setVoice}
              disabled={isSubmitting}
              errors={voiceErrors}
            />
          </section>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={isSubmitting}
            >
              取消
            </Button>
            <Button
              type="submit"
              disabled={isSubmitting}
              data-testid="cf-submit"
            >
              {isSubmitting ? "保存中…" : isEdit ? "保存" : "创建"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

const textareaClass = cn(
  "flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm",
  "ring-offset-background placeholder:text-muted-foreground",
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
  "disabled:cursor-not-allowed disabled:opacity-50",
  "resize-y",
);

interface FormFieldProps {
  label: string;
  htmlFor: string;
  error?: string;
  hint?: string;
  required?: boolean;
  children: React.ReactNode;
}

function FormField(props: FormFieldProps) {
  const { label, htmlFor, error, hint, required, children } = props;
  return (
    <div className="space-y-1">
      <label
        htmlFor={htmlFor}
        className="block text-sm font-medium text-foreground"
      >
        {label}
        {required && <span className="ml-0.5 text-destructive">*</span>}
      </label>
      {children}
      {error ? (
        <p
          role="alert"
          className="text-xs text-destructive"
          data-testid={`${htmlFor}-error`}
        >
          {error}
        </p>
      ) : hint ? (
        <p className="text-xs text-muted-foreground">{hint}</p>
      ) : null}
    </div>
  );
}
