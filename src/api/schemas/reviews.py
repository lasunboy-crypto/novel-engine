"""审稿相关请求/响应 schema(P1-B-19)。

P1 阶段 ``review_results`` / ``review_issues`` 表已建(P1-B-16),但 service 层
尚未把审稿持久化(P2 才接上)。本模块只暴露:

- ``ReviewIssueIn`` / ``ReviewIssueOut``:复用 ``core.models.review.ReviewIssue``
  的空子类别名(对齐 P1-B-18 ``WorkspaceOut`` 模式),保留 API 表面演进余地。
- ``ReviewSummaryEntry``:用在 ``:write`` / ``:rewrite`` 完成后 result 段的
  ``review_summary`` 数组里,从 ``OrchestratorResult.reviews`` 摘要而来。

P1 不暴露完整 ``ReviewResult`` 输出(没持久化,无独立 GET 端点)。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.core.models.review import ConstraintResult, ConstraintViolation, ReviewIssue


class ReviewIssueIn(ReviewIssue):
    """``:revise`` 请求体里的 issue 列表元素;空子类。"""


class ReviewIssueOut(ReviewIssue):
    """响应中嵌入的 issue 元素(用于 ``review_summary[].revise_targets``);空子类。"""


class ReviewSummaryEntry(BaseModel):
    """单次审稿的摘要(``OrchestratorResult.reviews`` 的子集 + 命中目标)。

    决策 6 拍板:``revise_targets`` 必须带,前端拿到 ``verdict=revise`` 不必再
    回头调端点反查 reviser 应处理的目标(P1 阶段 review 没持久化,这是仅有
    的获取入口)。

    ``score_overall`` 来源:``OrchestratorResult.reviews[].score_overall``——
    P1 ReviewResult 模型自带该字段,Reviewer agent 写入。

    P2-B-18:把 6 档分项(对齐 PROJECT_SPEC §4.4 加权)+ ``hard_constraints_passed``
    一并暴露——为 P2-F-07 ``VoiceCheckPanel`` 解锁 ``score_voice_consistency``
    联动信息维度,同时让 review 详情页(P3+)直接消费 6 档分项,不再加端点。
    """

    model_config = ConfigDict(extra="forbid")

    version_reviewed: int = Field(..., ge=1, description="本次审稿对应的版本号")
    verdict: Literal["pass", "revise", "rewrite", "escalate_to_human"] = Field(
        ..., description="判决结果"
    )
    score_overall: float = Field(..., description="综合分")
    score_plot_progression: float = Field(..., description="情节推进分(A1)")
    score_tension_match: float = Field(..., description="张力匹配分(A1)")
    score_prose_quality: float = Field(..., description="文笔质量分")
    score_consistency: float = Field(..., description="一致性分")
    score_hook_management: float = Field(..., description="钩子管理分")
    score_voice_consistency: float = Field(
        ..., description="角色声音一致性分(A3,P2-B-11 实质化)"
    )
    score_imagery: float | None = Field(
        default=None,
        description=(
            "画面性分(M2,wave 5 s2;0-10):show-vs-tell 卓越性度量。"
            "**纯度量,永不进 score_overall 加权、永不影响 verdict** —— "
            "仅供唯一稿质量观察与前端质感仪表盘展示。LLM 漏产 → None。"
        ),
    )
    score_interiority: float | None = Field(
        default=None,
        description=(
            "内心戏分(M2,wave 5 s3;0-10):角色内在呈现深度卓越性度量。"
            "**纯度量,永不进 score_overall、永不影响 verdict**(同 score_imagery)。"
            "LLM 漏产 → None。"
        ),
    )
    score_formulaic: float | None = Field(
        default=None,
        description=(
            "套路化分(M2,wave 5 s9;0-10):机器流水线味程度。"
            "**⚠️ 唯一'越高越差'的反极性维度**(其余 score 越高越好)。"
            "**纯度量,永不进 score_overall、永不影响 verdict** —— 前端展示需用警示色"
            "标注高分。LLM 漏产 → None。"
        ),
    )
    score_immersion: float | None = Field(
        default=None,
        description=(
            "沉浸感分(M2,wave 5 s9;0-10):读者代入感水准卓越性度量。"
            "**纯度量,永不进 score_overall、永不影响 verdict**(同 score_imagery)。"
            "LLM 漏产 → None。"
        ),
    )
    hard_constraints_passed: bool = Field(..., description="本次审稿硬约束是否全部通过")
    constraint_violations: list[ConstraintViolation] = Field(
        default_factory=list,
        description=("可回查来源规则与正文证据的结构化约束违规；旧审稿记录为空列表。"),
    )
    constraint_results: list[ConstraintResult] = Field(
        default_factory=list,
        description="四个 HARD 域逐条 pass/fail 结果；展示文案不参与 ID 比较。",
    )
    hard_constraint_regressions: list[str] = Field(
        default_factory=list,
        description="本版本发生 pass→fail 的稳定 constraint ID。",
    )
    revise_targets: list[ReviewIssueOut] = Field(
        ...,
        description=(
            "若 verdict=revise,Reviser 应处理的目标列表;非 revise 判决通常为空。"
            "P1 阶段是前端能看到 issue 详情的唯一入口(review 未持久化)。"
        ),
    )
