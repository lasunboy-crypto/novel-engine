/**
 * NewBookPage 左侧表单(P5-F-02)。
 *
 * 负责采集"新书启动"输入(workspace_id / display_name / genre / premise /
 * total_volumes / chapters_per_volume / tension_template),底部含 world.md /
 * characters.md 两个静态资产 import 槽(决策 7=C 摊到 NewBookPage)。表单
 * 自身不调 API,提交时把校验后的值 + 资产 markdown 内容 callback 给上层
 * `NewBookPage` 做"创建工作区 → 上传资产 → 生成大纲"链路编排(理由:三步
 * 之间存在 task lifecycle 状态需要在 NewBookPage 层管理)。
 *
 * `disabled` prop 由上层在 generating / committed 态下传入,表单字段统一禁用
 * (UnsavedChangesGuard 不接入,因为提交是单次性的,无"未保存修改"概念)。
 */

import { useRef } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  DEFAULT_VOICE_OPTIONS,
  PLANNING_MODE_OPTIONS,
  GENRE_OPTIONS,
  STYLE_TEMPLATE_NONE,
  STYLE_TEMPLATE_OPTIONS,
  TENSION_TEMPLATE_OPTIONS,
  buildPostV8AbcSelectionBookSchema,
  newBookSchema,
  type NewBookFormInput,
  type NewBookFormOutput,
} from "@/schemas/new_book";
import { useStyleTemplatesQuery } from "@/api/style_templates";
import { cn } from "@/lib/utils";
import { POST_V8_ABC_SELECTION_UI } from "@/selection/postV8AbcSelection";

const DEFAULT_VALUES: NewBookFormInput = {
  workspace_id: "",
  display_name: "",
  genre: "xianxia",
  premise: "",
  total_volumes: 5,
  planning_mode: "whole_book",
  chapters_per_volume: 30,
  tension_template: "heros_journey",
  default_voice: "third_person",
  style_template: STYLE_TEMPLATE_NONE,
};

/**
 * 上层提交回调入参:已校验的表单值 + 两个资产文件 markdown 文本(未选则 null)。
 */
export interface NewBookFormSubmitPayload {
  values: NewBookFormOutput;
  world_md_content: string | null;
  characters_md_content: string | null;
}

export interface NewBookFormProps {
  /** 上层提交回调;表单内部仅做 zod 校验 + 读文件,后续 API 编排在 NewBookPage 层。 */
  onSubmit: (payload: NewBookFormSubmitPayload) => void;
  /** 上层提供的"提交中 / 已提交"禁用态。 */
  disabled: boolean;
  /** 上层希望表单展示的提交按钮文案(idle / generating / ready / error 4 态)。 */
  submitButtonLabel: string;
}

export function NewBookForm(props: NewBookFormProps) {
  const { onSubmit, disabled, submitButtonLabel } = props;
  const selectionContract = POST_V8_ABC_SELECTION_UI;
  const selectionLocked = selectionContract !== null;
  const formSchema = selectionContract
    ? buildPostV8AbcSelectionBookSchema(selectionContract.values)
    : newBookSchema;

  const form = useForm<NewBookFormInput, undefined, NewBookFormOutput>({
    resolver: zodResolver(formSchema),
    defaultValues: selectionContract?.values ?? DEFAULT_VALUES,
    mode: "onBlur",
  });

  const worldFileRef = useRef<HTMLInputElement>(null);
  const charactersFileRef = useRef<HTMLInputElement>(null);

  const errors = form.formState.errors;
  const selectedVoice = form.watch("default_voice");
  const selectedStyleTemplate = form.watch("style_template");

  // I1:写作风格模板预览(元数据 + 10 维度 + 正文)。模板是引擎级静态资源。
  const styleTemplatesQuery = useStyleTemplatesQuery();
  const selectedStyleMeta =
    selectedStyleTemplate === STYLE_TEMPLATE_NONE
      ? undefined
      : styleTemplatesQuery.data?.find(
          (t) => t.name === selectedStyleTemplate,
        );

  const handleFormSubmit = async (values: NewBookFormOutput) => {
    const worldFile = worldFileRef.current?.files?.[0] ?? null;
    const charactersFile = charactersFileRef.current?.files?.[0] ?? null;
    const [worldText, charactersText] = await Promise.all([
      worldFile ? worldFile.text() : Promise.resolve<string | null>(null),
      charactersFile
        ? charactersFile.text()
        : Promise.resolve<string | null>(null),
    ]);
    onSubmit({
      values,
      world_md_content: worldText,
      characters_md_content: charactersText,
    });
  };

  return (
    <form
      onSubmit={form.handleSubmit(handleFormSubmit)}
      className="space-y-4"
      noValidate
      data-testid="new-book-form"
    >
      {selectionContract && (
        <div
          role="status"
          data-testid="nb-selection-lock"
          className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800"
        >
          选型活动任务 {selectionContract.candidate}：本页输入、三章结构与“无导入资产”
          已冻结。Provider 权限仍由后端临时活动配置单独校验。
        </div>
      )}
      <FormField
        label="显示名"
        error={errors.display_name?.message}
        htmlFor="nb-display-name"
        required
      >
        <Input
          id="nb-display-name"
          data-testid="nb-display-name"
          autoFocus={!selectionLocked}
          disabled={disabled}
          readOnly={selectionLocked}
          placeholder="例如:废柴炼丹师"
          {...form.register("display_name")}
        />
      </FormField>

      <FormField
        label="工作区 ID"
        error={errors.workspace_id?.message}
        hint="创建后不可修改;改名需 purge 重建"
        htmlFor="nb-workspace-id"
        required
      >
        <Input
          id="nb-workspace-id"
          data-testid="nb-workspace-id"
          disabled={disabled}
          readOnly={selectionLocked}
          placeholder="例如:waste-alchemist"
          {...form.register("workspace_id")}
        />
      </FormField>

      <FormField
        label="题材"
        error={errors.genre?.message}
        htmlFor="nb-genre"
        required
      >
        <select
          id="nb-genre"
          data-testid="nb-genre"
          disabled={disabled}
          aria-disabled={selectionLocked}
          tabIndex={selectionLocked ? -1 : undefined}
          className={cn(
            "flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            "disabled:cursor-not-allowed disabled:opacity-50",
            selectionLocked && "pointer-events-none",
          )}
          {...form.register("genre")}
        >
          {GENRE_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </FormField>

      <FormField
        label="一句话设定"
        error={errors.premise?.message}
        htmlFor="nb-premise"
        required
      >
        <textarea
          id="nb-premise"
          data-testid="nb-premise"
          rows={3}
          disabled={disabled}
          readOnly={selectionLocked}
          placeholder="一句话讲清这本书的核心冲突或世界观"
          className={cn(
            "flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm",
            "placeholder:text-muted-foreground",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            "disabled:cursor-not-allowed disabled:opacity-50",
            "resize-y",
          )}
          {...form.register("premise")}
        />
      </FormField>

      {!selectionLocked && (
        <FormField label="本次大纲范围" htmlFor="nb-planning-mode"
          hint="完整故事安排整体结局；长篇开局完成一段成长或经历，为后续发展留出空间。">
          <select id="nb-planning-mode" data-testid="nb-planning-mode" disabled={disabled}
            className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
            {...form.register("planning_mode")}>
            {PLANNING_MODE_OPTIONS.map((opt) => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
          </select>
        </FormField>
      )}

      <div className="grid grid-cols-2 gap-3">
        <FormField
          label="目标卷数"
          error={errors.total_volumes?.message}
          htmlFor="nb-total-volumes"
          required
        >
          <Input
            id="nb-total-volumes"
            data-testid="nb-total-volumes"
            type="number"
            min={1}
            max={20}
            step={1}
            disabled={disabled}
            readOnly={selectionLocked}
            {...form.register("total_volumes")}
          />
        </FormField>

        <FormField
          label="每卷章数"
          error={errors.chapters_per_volume?.message}
          hint={
            selectionLocked
              ? "选型活动内部合同固定 1 卷 3 章"
              : "每卷至少 6 章(≥ 2 arc × 3 章)"
          }
          htmlFor="nb-chapters-per-volume"
          required
        >
          <Input
            id="nb-chapters-per-volume"
            data-testid="nb-chapters-per-volume"
            type="number"
            min={selectionLocked ? 3 : 6}
            max={200}
            step={1}
            disabled={disabled}
            readOnly={selectionLocked}
            {...form.register("chapters_per_volume")}
          />
        </FormField>
      </div>

      {selectionLocked ? (
        <p
          className="rounded border border-border bg-muted/30 px-3 py-2 text-xs text-muted-foreground"
          data-testid="nb-chapters-per-volume-note"
        >
          技术检查点就是正式第 1 章；三章依次承担起因、升级、高潮/交代，不另建试跑章。
        </p>
      ) : (
        /* W3.5-s5:普通产品仍把每卷章数作为宏观参考。 */
        <p
          className="rounded border border-border bg-muted/30 px-3 py-2 text-xs text-muted-foreground"
          data-testid="nb-chapters-per-volume-note"
        >
          💡 每卷章数为<strong>宏观参考</strong>;启用长度感知拆章(deepseek)后,
          重头戏多的卷实际章数可能适度增加。
        </p>
      )}

      <FormField
        label="张力曲线模板"
        error={errors.tension_template?.message}
        htmlFor="nb-tension-template"
        required
      >
        <select
          id="nb-tension-template"
          data-testid="nb-tension-template"
          disabled={disabled}
          aria-disabled={selectionLocked}
          tabIndex={selectionLocked ? -1 : undefined}
          className={cn(
            "flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            "disabled:cursor-not-allowed disabled:opacity-50",
            selectionLocked && "pointer-events-none",
          )}
          {...form.register("tension_template")}
        >
          {TENSION_TEMPLATE_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </FormField>

      <FormField
        label="写作风格模板"
        error={errors.style_template?.message}
        htmlFor="nb-style-template"
        hint="选定后据模板生成 style_guide.md,全书写作风格锚点(可不选)"
      >
        <select
          id="nb-style-template"
          data-testid="nb-style-template"
          disabled={disabled}
          aria-disabled={selectionLocked}
          tabIndex={selectionLocked ? -1 : undefined}
          className={cn(
            "flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            "disabled:cursor-not-allowed disabled:opacity-50",
            selectionLocked && "pointer-events-none",
          )}
          {...form.register("style_template")}
        >
          {STYLE_TEMPLATE_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
        {selectedStyleMeta && (
          <div
            data-testid="nb-style-template-preview"
            className="mt-2 space-y-2 rounded border border-border bg-muted/40 p-3 text-xs"
          >
            <p className="text-foreground">{selectedStyleMeta.description}</p>
            <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-muted-foreground">
              <StyleDimItem label="题材调性" value={selectedStyleMeta.dimensions.genre_tone} />
              <StyleDimItem label="叙事密度" value={selectedStyleMeta.dimensions.narrative_density} />
              <StyleDimItem label="修辞密度" value={selectedStyleMeta.dimensions.rhetoric_density} />
              <StyleDimItem label="对白比例" value={selectedStyleMeta.dimensions.dialogue_ratio} />
              <StyleDimItem label="POV 切换" value={selectedStyleMeta.dimensions.pov_switching} />
              <StyleDimItem label="金手指节奏" value={selectedStyleMeta.dimensions.power_pacing} />
              <StyleDimItem label="世界观密度" value={selectedStyleMeta.dimensions.worldbuilding_density} />
              <StyleDimItem label="情感色调" value={selectedStyleMeta.dimensions.emotional_tone} />
              <StyleDimItem label="章节长度" value={selectedStyleMeta.dimensions.chapter_length} />
              <StyleDimItem label="事件密度" value={selectedStyleMeta.dimensions.event_density} />
            </dl>
            <details className="text-muted-foreground">
              <summary className="cursor-pointer select-none text-foreground">
                查看风格指南正文
              </summary>
              <pre className="mt-1.5 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-background p-2 text-[11px] leading-relaxed">
                {selectedStyleMeta.preview}
              </pre>
            </details>
          </div>
        )}
      </FormField>

      <FormField
        label="默认人称视角"
        error={errors.default_voice?.message}
        htmlFor="nb-default-voice"
        required
      >
        <div
          role="radiogroup"
          aria-label="默认人称视角"
          data-testid="nb-default-voice"
          className="space-y-1.5"
        >
          {DEFAULT_VOICE_OPTIONS.map((opt) => (
            <label
              key={opt.value}
              className="flex items-center gap-2 text-sm text-foreground"
            >
              <input
                type="radio"
                value={opt.value}
                disabled={disabled}
                aria-disabled={selectionLocked}
                tabIndex={selectionLocked ? -1 : undefined}
                data-testid={`nb-default-voice-${opt.value}`}
                className={cn(
                  "h-4 w-4 disabled:cursor-not-allowed disabled:opacity-50",
                  selectionLocked && "pointer-events-none",
                )}
                {...form.register("default_voice")}
              />
              <span>{opt.label}</span>
            </label>
          ))}
        </div>
        {selectedVoice === "free" && (
          <p
            role="alert"
            data-testid="nb-default-voice-free-warn"
            className="mt-1.5 rounded border border-amber-300 bg-amber-50 px-2 py-1.5 text-xs text-amber-700"
          >
            ⚠️ 自由视角会<strong>关闭跨章节视角一致性检查</strong>,可能出现章节间
            人称混乱。仅在确需跨章节切换视角(如多 POV / 倒叙)时选择。
          </p>
        )}
      </FormField>

      {!selectionLocked && (
        <div className="space-y-2 rounded border border-dashed border-border p-3">
          <p className="text-sm font-medium text-foreground">
            静态资产(可选)
          </p>
          <p className="text-xs text-muted-foreground">
            上传 markdown 文件,后端会解析入库:world.md → 世界设定 / characters.md
            → 角色档案。创建工作区后,资产可在角色页 / 大纲页继续维护。
          </p>
          <FormField label="world.md(世界设定)" htmlFor="nb-world-md">
            <input
              ref={worldFileRef}
              id="nb-world-md"
              data-testid="nb-world-md"
              type="file"
              accept=".md,.markdown,text/markdown"
              disabled={disabled}
              className="block w-full text-sm file:mr-3 file:rounded file:border-0 file:bg-secondary file:px-3 file:py-1.5 file:text-sm file:text-secondary-foreground hover:file:bg-secondary/80 disabled:opacity-50"
            />
          </FormField>
          <FormField label="characters.md(角色档案)" htmlFor="nb-characters-md">
            <input
              ref={charactersFileRef}
              id="nb-characters-md"
              data-testid="nb-characters-md"
              type="file"
              accept=".md,.markdown,text/markdown"
              disabled={disabled}
              className="block w-full text-sm file:mr-3 file:rounded file:border-0 file:bg-secondary file:px-3 file:py-1.5 file:text-sm file:text-secondary-foreground hover:file:bg-secondary/80 disabled:opacity-50"
            />
          </FormField>
        </div>
      )}

      <Button
        type="submit"
        className="w-full"
        disabled={disabled}
        data-testid="nb-submit"
      >
        {submitButtonLabel}
      </Button>
    </form>
  );
}

/** I1:风格模板预览中的单个维度项(标签 + 值)。 */
function StyleDimItem(props: { label: string; value: string }) {
  return (
    <div className="flex gap-1">
      <dt className="shrink-0 text-muted-foreground/80">{props.label}:</dt>
      <dd className="text-foreground">{props.value}</dd>
    </div>
  );
}

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
