import * as React from "react";

import {
  useUpdateWorkspaceCostGuardMutation,
  type CostGuardConfigIn,
  type CostGuardConfigOut,
} from "@/api/config";
import { showApiErrorToast, showSuccessToast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

interface CostGuardOverridePanelProps {
  workspaceId: string;
  config: CostGuardConfigOut;
}

/** 工作区成本守卫的独立编辑器；保存只改 cost_guard 段。 */
export function CostGuardOverridePanel({
  workspaceId,
  config,
}: CostGuardOverridePanelProps) {
  const updateMutation = useUpdateWorkspaceCostGuardMutation(workspaceId);
  const [currency, setCurrency] = React.useState(config.cost_currency);
  const [dailyLimit, setDailyLimit] = React.useState(
    String(config.per_workspace_daily_limit),
  );

  React.useEffect(() => {
    setCurrency(config.cost_currency);
    setDailyLimit(String(config.per_workspace_daily_limit));
  }, [config.cost_currency, config.per_workspace_daily_limit]);

  const parsedLimit = Number(dailyLimit);
  const valid =
    dailyLimit.trim().length > 0 &&
    Number.isFinite(parsedLimit) &&
    parsedLimit >= 0;
  const limitDirty = parsedLimit !== config.per_workspace_daily_limit;
  const currencyDirty = currency !== config.cost_currency;
  const dirty = valid && (limitDirty || currencyDirty);

  function handleSave() {
    if (!valid) return;
    const update: CostGuardConfigIn = {};
    if (limitDirty) update.per_workspace_daily_limit = parsedLimit;
    if (currencyDirty) update.cost_currency = currency;
    updateMutation.mutate(
      update,
      {
        onSuccess: () => {
          showSuccessToast(
            "已更新工作区成本上限",
            `单工作区单日上限已设为 ${currency}${parsedLimit.toFixed(2)}，下次继续立即生效`,
          );
        },
        onError: (err) => showApiErrorToast(err),
      },
    );
  }

  return (
    <section
      className="space-y-3 rounded border border-border p-4"
      data-testid="config-cost-guard-panel"
    >
      <div>
        <h2 className="text-lg font-semibold">成本守卫</h2>
        <p className="text-xs text-muted-foreground">
          调整当前工作区的阈值币种和数字；切换币种不会换算既有金额。
          保存不会改动 Writer、审核、检索或生成参数。
        </p>
      </div>

      <label className="block max-w-xs space-y-1 text-sm">
        <span className="text-muted-foreground">阈值币种</span>
        <select
          value={currency}
          onChange={(event) => setCurrency(event.target.value)}
          data-testid="config-cost-guard-currency"
          className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm"
        >
          <option value="USD">USD</option>
          <option value="RMB">RMB</option>
        </select>
      </label>

      <label className="block max-w-xs space-y-1 text-sm">
        <span className="text-muted-foreground">
          单工作区单日上限（{currency}）
        </span>
        <Input
          type="number"
          min={0}
          step="0.01"
          value={dailyLimit}
          onChange={(event) => setDailyLimit(event.target.value)}
          data-testid="config-cost-guard-daily-limit"
        />
      </label>

      {!valid && (
        <p className="text-xs text-destructive" data-testid="config-cost-guard-error">
          请输入大于或等于 0 的有效金额。
        </p>
      )}

      <div className="flex items-center gap-3">
        <Button
          type="button"
          onClick={handleSave}
          disabled={!dirty || updateMutation.isPending}
          data-testid="config-cost-guard-save"
        >
          {updateMutation.isPending ? "保存中…" : "保存成本上限"}
        </Button>
        <p className="text-xs text-muted-foreground">
          当前生效：
          <span>{`${config.cost_currency}${config.per_workspace_daily_limit.toFixed(2)} / 日`}</span>
        </p>
      </div>
    </section>
  );
}
