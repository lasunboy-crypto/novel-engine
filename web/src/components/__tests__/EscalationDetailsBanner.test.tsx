/**
 * EscalationDetailsBanner 防回退测(v1.1 acceptance 修复2,孤儿章人工解决)。
 *
 * 钉住:
 * 1. 非 human_intervention 状态 → 不渲染(零垂直占用)
 * 2. human_intervention 但无 escalation_details → 不渲染
 * 3. Fix D findings → 列出 must / 置信 / 证据 / 处置建议
 * 4. reason 中文标签映射
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { EscalationDetailsBanner } from "@/components/EscalationDetailsBanner";
import type { ChapterStatusOut } from "@/api/chapters";

function _status(overrides: Partial<ChapterStatusOut>): ChapterStatusOut {
  return {
    chapter_id: "ch-0007",
    status: "human_intervention",
    latest_version_id: 1,
    has_final: false,
    version_count: 1,
    escalation_details: null,
    ...overrides,
  } as ChapterStatusOut;
}

const _fixDDetails = {
  reason: "must_infeasible_pre_write",
  checked_at_state: "pre_write",
  findings: [
    {
      must_text: "沈舟在防空洞强行压制副人格救下流浪汉",
      confidence: 0.95,
      evidence: "ch-0006 已完结收束写明该冲突已解决。",
      suggestion: "建议从卡中删除该 must,或改写为后果延续式措辞。",
    },
  ],
};

describe("EscalationDetailsBanner", () => {
  it("非 human_intervention 状态 → 不渲染", () => {
    render(
      <EscalationDetailsBanner
        status={_status({ status: "approved", escalation_details: _fixDDetails })}
      />,
    );
    expect(screen.queryByTestId("escalation-details-banner")).toBeNull();
  });

  it("human_intervention 但 escalation_details 为 null → 不渲染", () => {
    render(
      <EscalationDetailsBanner
        status={_status({ escalation_details: null })}
      />,
    );
    expect(screen.queryByTestId("escalation-details-banner")).toBeNull();
  });

  it("status undefined(加载中)→ 不渲染", () => {
    render(<EscalationDetailsBanner status={undefined} />);
    expect(screen.queryByTestId("escalation-details-banner")).toBeNull();
  });

  it("Fix D findings:渲染 must / 置信 / 证据 / 处置建议", () => {
    render(
      <EscalationDetailsBanner
        status={_status({ escalation_details: _fixDDetails })}
      />,
    );
    const banner = screen.getByTestId("escalation-details-banner");
    expect(banner).toBeInTheDocument();
    // reason 中文标签
    expect(banner).toHaveTextContent("必达事件已被前文消费");
    // finding 内容
    const finding = screen.getByTestId("escalation-finding");
    expect(finding).toHaveTextContent("沈舟在防空洞强行压制副人格救下流浪汉");
    expect(finding).toHaveTextContent("95%"); // 置信
    expect(finding).toHaveTextContent("ch-0006 已完结收束");
    expect(finding).toHaveTextContent("建议从卡中删除");
  });

  it("多条 findings 全部渲染", () => {
    render(
      <EscalationDetailsBanner
        status={_status({
          escalation_details: {
            reason: "must_infeasible_pre_write",
            findings: [
              { must_text: "must A", confidence: 0.9, evidence: "e", suggestion: "s" },
              { must_text: "must B", confidence: 0.85, evidence: "e2", suggestion: "s2" },
            ],
          },
        })}
      />,
    );
    expect(screen.getAllByTestId("escalation-finding")).toHaveLength(2);
  });

  it("无 findings 的升级原因(如 budget 耗尽)→ 渲染横幅但无 finding 列表", () => {
    render(
      <EscalationDetailsBanner
        status={_status({
          escalation_details: { reason: "rewrite_budget_exhausted" },
        })}
      />,
    );
    expect(screen.getByTestId("escalation-details-banner")).toHaveTextContent(
      "重写预算耗尽",
    );
    expect(screen.queryByTestId("escalation-finding")).toBeNull();
  });

  // v2 Fix 2:"确认误判 · 忽略预检并写作"按钮。
  it("Fix D 误判按钮:must_infeasible_pre_write + 有回调 → 渲染按钮并可点击", () => {
    const onIgnore = vi.fn();
    render(
      <EscalationDetailsBanner
        status={_status({ escalation_details: _fixDDetails })}
        onIgnoreFeasibility={onIgnore}
      />,
    );
    const btn = screen.getByTestId("ignore-feasibility-button");
    expect(btn).toHaveTextContent("确认误判");
    fireEvent.click(btn);
    expect(onIgnore).toHaveBeenCalledTimes(1);
  });

  it("isIgnoringFeasibility=true → 按钮 disabled + 显示进行中文案", () => {
    render(
      <EscalationDetailsBanner
        status={_status({ escalation_details: _fixDDetails })}
        onIgnoreFeasibility={vi.fn()}
        isIgnoringFeasibility
      />,
    );
    const btn = screen.getByTestId("ignore-feasibility-button");
    expect(btn).toBeDisabled();
    expect(btn).toHaveTextContent("正在重写");
  });

  it("非 must_infeasible_pre_write 原因(budget 耗尽)→ 不渲染误判按钮", () => {
    render(
      <EscalationDetailsBanner
        status={_status({
          escalation_details: { reason: "rewrite_budget_exhausted" },
        })}
        onIgnoreFeasibility={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("ignore-feasibility-button")).toBeNull();
  });

  it("无 onIgnoreFeasibility 回调 → 不渲染按钮(向后兼容)", () => {
    render(
      <EscalationDetailsBanner
        status={_status({ escalation_details: _fixDDetails })}
      />,
    );
    expect(screen.queryByTestId("ignore-feasibility-button")).toBeNull();
  });

  // ---- v2 W3.5 UI 缺口3:全原因详情 + 重审按钮 ----

  const _rewriteBudgetDetails = {
    reason: "rewrite_budget_exhausted",
    checked_at_state: "post_review",
    summary: "已重写 3/3 次,最后一次审校判决仍为 rewrite,重写预算耗尽自动升人工。",
    last_verdict: "rewrite",
    score_overall: 5.2,
    rewrite_count: 3,
    max_rewrites: 3,
    issues: [
      {
        issue_type: "consistency",
        description: "与前章设定冲突:乔远本应缺席",
        suggestion: "删除乔远出场段落",
        paragraph_anchor: "乔远缓步走入大厅",
      },
    ],
    suggestions: ["整体压缩冗余内心独白"],
  };

  it("rewrite_budget_exhausted:渲染 summary + issues + suggestions + 分数/预算", () => {
    render(
      <EscalationDetailsBanner
        status={_status({ escalation_details: _rewriteBudgetDetails })}
      />,
    );
    const banner = screen.getByTestId("escalation-details-banner");
    expect(banner).toHaveTextContent("重写预算耗尽");
    expect(banner).toHaveTextContent("综合分 5.2");
    expect(banner).toHaveTextContent("重写 3/3");
    expect(screen.getByTestId("escalation-summary")).toHaveTextContent(
      "重写预算耗尽自动升人工",
    );
    const issue = screen.getByTestId("escalation-issue");
    expect(issue).toHaveTextContent("consistency");
    expect(issue).toHaveTextContent("乔远本应缺席");
    expect(issue).toHaveTextContent("删除乔远出场段落");
    expect(screen.getByTestId("escalation-suggestions")).toHaveTextContent(
      "整体压缩冗余内心独白",
    );
    // 不应误渲染 must_infeasible 的 finding。
    expect(screen.queryByTestId("escalation-finding")).toBeNull();
  });

  it("reviewer_parse_failed:渲染技术细节(模型 + 原始输出预览)", () => {
    render(
      <EscalationDetailsBanner
        status={_status({
          escalation_details: {
            reason: "reviewer_parse_failed_after_retry",
            checked_at_state: "reviewing",
            summary: "自动审稿连续 5 次解析失败,升人工。",
            model_used: "deepseek-v4-pro",
            attempts: ["a1", "a2", "a3", "a4", "a5"],
            raw_output_preview: "这是损坏的 JSON 输出片段...",
          },
        })}
      />,
    );
    const banner = screen.getByTestId("escalation-details-banner");
    expect(banner).toHaveTextContent("审稿输出连续解析失败");
    const tech = screen.getByTestId("escalation-technical");
    expect(tech).toHaveTextContent("deepseek-v4-pro");
    expect(tech).toHaveTextContent("这是损坏的 JSON 输出片段");
  });

  it("re_review_failed:reason 中文标签 + issues 渲染", () => {
    render(
      <EscalationDetailsBanner
        status={_status({
          escalation_details: {
            reason: "re_review_failed",
            checked_at_state: "re_review",
            summary: "人工改稿后重审未通过。",
            issues: [
              { issue_type: "pacing", description: "节奏仍过缓", suggestion: "压缩" },
            ],
          },
        })}
      />,
    );
    expect(screen.getByTestId("escalation-details-banner")).toHaveTextContent(
      "人工改稿后重审未通过",
    );
    expect(screen.getByTestId("escalation-issue")).toHaveTextContent("节奏仍过缓");
  });

  it("提交后重审按钮:有 onReReview → 渲染并可点击(所有原因)", () => {
    const onReReview = vi.fn();
    render(
      <EscalationDetailsBanner
        status={_status({ escalation_details: _rewriteBudgetDetails })}
        onReReview={onReReview}
      />,
    );
    const btn = screen.getByTestId("re-review-button");
    expect(btn).toHaveTextContent("提交后重审");
    fireEvent.click(btn);
    expect(onReReview).toHaveBeenCalledTimes(1);
  });

  it("isReReviewing=true → 重审按钮 disabled + 进行中文案", () => {
    render(
      <EscalationDetailsBanner
        status={_status({ escalation_details: _rewriteBudgetDetails })}
        onReReview={vi.fn()}
        isReReviewing
      />,
    );
    const btn = screen.getByTestId("re-review-button");
    expect(btn).toBeDisabled();
    expect(btn).toHaveTextContent("正在重审");
  });

  it("无 onReReview 回调 → 不渲染重审按钮(向后兼容)", () => {
    render(
      <EscalationDetailsBanner
        status={_status({ escalation_details: _rewriteBudgetDetails })}
      />,
    );
    expect(screen.queryByTestId("re-review-button")).toBeNull();
  });
});
