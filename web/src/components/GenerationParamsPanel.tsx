/**
 * 工作区级生成参数调整面板(v1.1 wave 3 session 3 / L1 Q16)。
 *
 * 入口:章节卡页面顶部"生成参数"按钮 → 右侧侧边浮层(非 modal,背景章节卡数据
 * 保持可见,对齐维护人"看着字数数据边调下限"的工作流)。3 项参数:
 * - 字数下限 / 上限:影响后续**新生成**章节卡(写 project.yaml generation 段)。
 * - max_rewrites(整章重写次数上限):同上。
 * - "应用到已有章节"按钮:复用 G6.2 backfill 端点,把当前字数区间推给已生成卡。
 *
 * 温度参数**不在本面板** —— 归 v1.1 I 类写作风格模板系统(I2)按风格分工种智能
 * 设置;L1 一刀切覆盖会与 I2 重复 / 冲突(详 impl 文档 §关键决策 + candidates §I2)。
 */
import * as React from "react";
import { Settings2 } from "lucide-react";

import {
  useBackfillWordCountMutation,
  useGenerationConfigQuery,
  useUpdateGenerationConfigMutation,
} from "@/api/generation_params";
import { useWorkspaceDetailQuery } from "@/api/workspaces";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";

interface GenerationParamsPanelProps {
  workspaceId: string;
}

type BackfillScope = "unapproved" | "all";
type GenreMechanismMode = "auto" | "on" | "off";
type TrimMode = "follow" | "on" | "off";

/** trim 三态:override(bool|null)→ UI 模式。null=跟随当前 writer 模型校准 / true=强制开 / false=强制关。 */
function overrideToMode(override: boolean | null | undefined): TrimMode {
  if (override === true) return "on";
  if (override === false) return "off";
  return "follow";
}

/** UI 模式 → override(bool|null);follow→null(PUT 整段替换 + 后端 exclude_none → 不落 project.yaml → 解析时跟随校准)。 */
function modeToOverride(mode: TrimMode): boolean | null {
  if (mode === "on") return true;
  if (mode === "off") return false;
  return null;
}

/**
 * 模块①②(v1.1 w5-s10)两开关仅对这 3 题材生效;其余题材两开关隐藏
 * (后端 genre 门控亦保证 no-op,前端隐藏纯为 UX 不给无关题材添困惑)。
 * 与 `src/core/models/genre_mechanism.py::PROGRESSION_GENRES` 对齐。
 */
const PROGRESSION_GENRES = new Set([
  "classical_xianxia",
  "xianxia_growth",
  "comedic_burst",
  "heroic_battle",
]);

export default function GenerationParamsPanel({
  workspaceId,
}: GenerationParamsPanelProps) {
  const [open, setOpen] = React.useState(false);

  return (
    <Sheet open={open} onOpenChange={setOpen} modal={false}>
      <SheetTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="sm"
          data-testid="generation-params-trigger"
          className="gap-1.5"
        >
          <Settings2 className="h-3.5 w-3.5" />
          生成参数
        </Button>
      </SheetTrigger>
      <SheetContent data-testid="generation-params-panel">
        {open && <PanelBody workspaceId={workspaceId} />}
      </SheetContent>
    </Sheet>
  );
}

function PanelBody({ workspaceId }: { workspaceId: string }) {
  const configQuery = useGenerationConfigQuery(workspaceId);
  const workspaceQuery = useWorkspaceDetailQuery(workspaceId);
  const updateMutation = useUpdateGenerationConfigMutation(workspaceId);
  const backfillMutation = useBackfillWordCountMutation(workspaceId);

  const [approximate, setApproximate] = React.useState(false);
  const [wordCountMin, setWordCountMin] = React.useState<string>("");
  const [wordCountMax, setWordCountMax] = React.useState<string>("");
  const [maxRewrites, setMaxRewrites] = React.useState<string>("");
  const [progressionConsistency, setProgressionConsistency] =
    React.useState<GenreMechanismMode>("auto");
  const [breakthroughPayoff, setBreakthroughPayoff] =
    React.useState<GenreMechanismMode>("auto");
  const [antiTicTrim, setAntiTicTrim] = React.useState<TrimMode>("follow");
  const [coreStateHardEnabled, setCoreStateHardEnabled] =
    React.useState<boolean>(true);
  const [backfillScope, setBackfillScope] =
    React.useState<BackfillScope>("unapproved");

  // 模块①②(v1.1 w5-s10)开关仅 3 题材显示;workspace 未加载 → styleTemplate=null
  // → 暂不显示(数据到位后自动渲染)。后端门控对非 3 题材亦 no-op,双保险。
  const styleTemplate = workspaceQuery.data?.style_template ?? null;
  const showGenreMechanisms =
    styleTemplate !== null && PROGRESSION_GENRES.has(styleTemplate);

  // GET 到配置后预填表单(只在数据首次到达时同步,之后由用户编辑驱动)。
  const loadedKey = configQuery.data
    ? `${configQuery.data.word_count_policy_version}-${configQuery.data.word_count_min}-${configQuery.data.word_count_max}-${configQuery.data.max_rewrites}-${configQuery.data.progression_consistency}-${configQuery.data.breakthrough_payoff}-${configQuery.data.anti_tic_trim_override}-${configQuery.data.anti_tic_trim_calibration_default}-${configQuery.data.word_count_trim_override}-${configQuery.data.word_count_trim_calibration_default}-${configQuery.data.core_state_hard_enabled}`
    : null;
  const [syncedKey, setSyncedKey] = React.useState<string | null>(null);
  React.useEffect(() => {
    if (configQuery.data && loadedKey !== syncedKey) {
      setApproximate(configQuery.data.word_count_policy_version === 2);
      setWordCountMin(String(configQuery.data.word_count_min));
      setWordCountMax(String(configQuery.data.word_count_max));
      setMaxRewrites(String(configQuery.data.max_rewrites));
      setProgressionConsistency(configQuery.data.progression_consistency);
      setBreakthroughPayoff(configQuery.data.breakthrough_payoff);
      setAntiTicTrim(overrideToMode(configQuery.data.anti_tic_trim_override));
      setCoreStateHardEnabled(configQuery.data.core_state_hard_enabled);
      setSyncedKey(loadedKey);
    }
  }, [configQuery.data, loadedKey, syncedKey]);

  const minNum = Number(wordCountMin);
  const maxNum = approximate ? minNum : Number(wordCountMax);
  const lengthLabel = approximate ? `约 ${minNum} 字` : `${minNum}–${maxNum} 字`;
  const rewritesNum = Number(maxRewrites);
  const fieldsValid =
    Number.isInteger(minNum) &&
    minNum >= 1 &&
    Number.isInteger(maxNum) &&
    maxNum >= 1 &&
    Number.isInteger(rewritesNum) &&
    rewritesNum >= 0;
  const rangeValid = fieldsValid && minNum <= maxNum;

  function handleSave() {
    if (!rangeValid) return;
    updateMutation.mutate(
      {
        word_count_min: minNum,
        word_count_max: maxNum,
        word_count_policy_version: approximate ? 2 : 1,
        max_rewrites: rewritesNum,
        // W3.5 校准联动三态:follow→null(后端不落盘 → 跟随当前 writer 模型校准)/ on→true / off→false。
        anti_tic_trim_enabled: modeToOverride(antiTicTrim),
        word_count_trim_enabled: false,
        core_state_hard_enabled: coreStateHardEnabled,
        // 模块①②(w5-s10):仅 3 题材纳入(非 3 题材开关不显示 → 不覆盖后端默认)。
        ...(showGenreMechanisms
          ? {
              progression_consistency: progressionConsistency,
              breakthrough_payoff: breakthroughPayoff,
            }
          : {}),
      },
      {
        onSuccess: () => {
          showSuccessToast(
            "已保存生成参数",
            `篇幅 ${lengthLabel} · 重写上限 ${rewritesNum}(影响后续新生成章节卡)`,
          );
        },
        onError: (err) => showApiErrorToast(err),
      },
    );
  }

  function handleBackfill() {
    if (!rangeValid) return;
    backfillMutation.mutate(
      {
        scope: backfillScope,
        word_count_min: minNum,
        word_count_max: maxNum,
        word_count_policy_version: approximate ? 2 : 1,
      },
      {
        onSuccess: (result) => {
          showSuccessToast(
            "已应用到已有章节",
            `${result.reset_chapter_ids.length} 章篇幅已更新为 ${lengthLabel}`,
          );
        },
        onError: (err) => showApiErrorToast(err),
      },
    );
  }

  return (
    <>
      <SheetHeader>
        <SheetTitle>生成参数</SheetTitle>
        <SheetDescription>
          工作区级默认,影响后续新生成章节卡。已生成的章节卡用下方"应用到已有章节"。
        </SheetDescription>
      </SheetHeader>

      {configQuery.isPending ? (
        <p className="text-sm text-muted-foreground">加载中…</p>
      ) : configQuery.isError ? (
        <p
          data-testid="generation-params-load-error"
          className="rounded border border-destructive bg-destructive/10 p-3 text-sm text-destructive"
        >
          加载生成参数失败,请重试。
        </p>
      ) : (
        <div className="flex flex-1 flex-col gap-4 overflow-y-auto">
          <label className="space-y-1 text-sm">
            <span>篇幅要求</span>
            <select aria-label="篇幅要求" data-testid="generation-params-length-mode"
              value={approximate ? "approximate" : "range"}
              onChange={(e) => setApproximate(e.target.value === "approximate")}
              className="w-full rounded border border-input bg-background px-2 py-1 text-sm">
              <option value="approximate">约数目标（不设下限）</option>
              <option value="range">范围要求</option>
            </select>
          </label>
          <div className="flex gap-3">
            <label className="flex-1 space-y-1 text-sm">
              <span className="text-muted-foreground">{approximate ? "目标字数（约）" : "字数下限"}</span>
              <Input
                type="number"
                min={1}
                value={wordCountMin}
                onChange={(e) => setWordCountMin(e.target.value)}
                data-testid="generation-params-word-count-min"
              />
            </label>
            {!approximate && <label className="flex-1 space-y-1 text-sm">
              <span className="text-muted-foreground">字数上限</span>
              <Input
                type="number"
                min={1}
                value={wordCountMax}
                onChange={(e) => setWordCountMax(e.target.value)}
                data-testid="generation-params-word-count-max"
              />
            </label>}
          </div>

          {fieldsValid && !rangeValid && (
            <p
              data-testid="generation-params-range-error"
              className="text-xs text-destructive"
            >
              字数下限不能大于上限。
            </p>
          )}

          <label className="space-y-1 text-sm">
            <span className="text-muted-foreground">
              整章重写次数上限(审稿未过时最多重写几次)
            </span>
            <Input
              type="number"
              min={0}
              value={maxRewrites}
              onChange={(e) => setMaxRewrites(e.target.value)}
              data-testid="generation-params-max-rewrites"
              className="w-28"
            />
          </label>

          <label className="flex items-start gap-2 rounded border border-border p-3 text-sm">
            <input
              type="checkbox"
              checked={coreStateHardEnabled}
              onChange={(event) =>
                setCoreStateHardEnabled(event.target.checked)
              }
              data-testid="generation-params-core-state-hard"
              className="mt-1"
            />
            <span>
              <span className="block font-medium">核心状态与书内时钟 HARD</span>
              <span className="block text-xs text-muted-foreground">
                默认开启。新章节卡会冻结少量全书级客观状态和主时间线，
                Writer 与现有审核共用同一判尺；关闭只影响后续新卡，不删除既有审计历史。
              </span>
            </span>
          </label>

          <p className="text-xs text-muted-foreground" data-testid="generation-params-length-policy">
            篇幅是正常写作要求，与其他任务同等重视。约数目标允许自然长短，不要求凑满。保存或应用后，字数不会导致拒稿、扣分或自动改写。
            特别长的章节仅附人工分章提示。
          </p>

          {/* W3.5 校准联动:anti-tic trim 三态(原布尔 checkbox → 三态 select)*/}
          <label className="space-y-1 text-sm">
            <span className="text-muted-foreground">
              句式去口头禅(写后润色:清「不是A是B」句式 + 演后盖章金句)
            </span>
            <select
              value={antiTicTrim}
              onChange={(e) => setAntiTicTrim(e.target.value as TrimMode)}
              data-testid="generation-params-anti-tic-trim"
              className="w-full rounded border border-input bg-background px-2 py-1 text-sm"
            >
              <option value="follow">
                跟随模型(当前:
                {configQuery.data?.anti_tic_trim_calibration_default
                  ? "开"
                  : "关"}
                )
              </option>
              <option value="on">强制开</option>
              <option value="off">强制关</option>
            </select>
            <span className="block text-xs text-muted-foreground">
              跟随模型:按当前写作模型校准自动决定(tell 盖章重的 deepseek 自动开)。
              开启后每章写完加一道润色,只清固定句式 + 多余金句,绝不动剧情 / 对白 / 细节,带护栏。
              每章多一次模型调用(约 +40~90 秒、成本极小)。
            </span>
          </label>

          {showGenreMechanisms && (
            <div
              className="space-y-3 rounded border border-border p-3"
              data-testid="generation-params-genre-mechanisms"
            >
              <p className="text-sm font-medium">网文/修仙机制(本题材专属)</p>
              <label className="space-y-1 text-sm">
                <span className="text-muted-foreground">
                  境界一致性(防角色境界莫名倒退 / 写串,长篇连载用)
                </span>
                <select
                  value={progressionConsistency}
                  onChange={(e) =>
                    setProgressionConsistency(
                      e.target.value as GenreMechanismMode,
                    )
                  }
                  data-testid="generation-params-progression-consistency"
                  className="w-full rounded border border-input bg-background px-2 py-1 text-sm"
                >
                  <option value="auto">
                    自动(古典仙侠默认开 / 爽文+热血默认关)
                  </option>
                  <option value="on">开启</option>
                  <option value="off">关闭</option>
                </select>
              </label>
              <label className="space-y-1 text-sm">
                <span className="text-muted-foreground">
                  升级可感化(突破章写出实力跃迁,不只丢一个境界名词)
                </span>
                <select
                  value={breakthroughPayoff}
                  onChange={(e) =>
                    setBreakthroughPayoff(e.target.value as GenreMechanismMode)
                  }
                  data-testid="generation-params-breakthrough-payoff"
                  className="w-full rounded border border-input bg-background px-2 py-1 text-sm"
                >
                  <option value="auto">自动(3 题材默认开)</option>
                  <option value="on">开启</option>
                  <option value="off">关闭</option>
                </select>
              </label>
              <span className="block text-xs text-muted-foreground">
                仅古典仙侠 / 搞笑爽文 / 热血战斗题材显示。影响后续新生成章节。
              </span>
            </div>
          )}

          <Button
            type="button"
            onClick={handleSave}
            disabled={!rangeValid || updateMutation.isPending}
            data-testid="generation-params-save"
          >
            {updateMutation.isPending ? "保存中…" : "保存默认"}
          </Button>

          <div className="mt-2 space-y-2 rounded border border-border p-3">
            <p className="text-sm font-medium">应用到已有章节</p>
            <p className="text-xs text-muted-foreground">
              把上方字数区间批量推给已生成的章节卡(不影响重写次数 / 不改默认)。
            </p>
            <label className="flex items-center gap-2 text-sm">
              <span className="text-muted-foreground">范围</span>
              <select
                value={backfillScope}
                onChange={(e) =>
                  setBackfillScope(e.target.value as BackfillScope)
                }
                data-testid="generation-params-backfill-scope"
                className="rounded border border-input bg-background px-2 py-1 text-sm"
              >
                <option value="unapproved">仅未审核(保护已审卡)</option>
                <option value="all">全部(含已审核)</option>
              </select>
            </label>
            <Button
              type="button"
              variant="secondary"
              onClick={handleBackfill}
              disabled={!rangeValid || backfillMutation.isPending}
              data-testid="generation-params-backfill"
            >
              {backfillMutation.isPending ? "应用中…" : "应用到已有章节"}
            </Button>
          </div>
        </div>
      )}

      <SheetFooter />
    </>
  );
}
