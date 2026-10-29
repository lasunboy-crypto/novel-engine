"""Reviewer 工作区级阈值配置(P6-B-13)。

引擎默认 ``config.yaml`` 的 ``reviewer:`` 段;工作区 ``project.yaml`` 可整段覆盖
(``ConfigService.load_reviewer_config``)。本模块只定义 Pydantic 模型 + 加载入口,
**不修改 reviewer agent runtime**(``src/core/agents/reviewer.py`` 沿用 ChapterCard
单章字段 ``review_threshold`` / ``revise_threshold`` + 模块级常量 ``ESCALATION_*_BOUND``,
工作区覆盖运行时接入留 P6-F-08+ — 启动指令字面接力 marker)。

字段语义
========

- ``pass_threshold``    : ``score_overall >= pass_threshold`` 即判 pass(对齐
  ChapterCard 单章字段 ``review_threshold`` 同语义,命名分歧详 impl §"关键决策")。
- ``revise_threshold``  : ``revise_threshold <= score_overall < pass_threshold``
  判 revise;低于该值判 rewrite。
- ``escalation_lower_bound`` / ``escalation_upper_bound`` : 主审 Qwen score_overall
  落入闭区间 ``[lower, upper]`` 且 ``agent_routing.reviewer_escalation`` 显式配置时
  触发升 Claude 复审(对齐 ``src/core/agents/reviewer.py`` 现 ``ESCALATION_LOWER_BOUND``
  / ``ESCALATION_UPPER_BOUND`` 模块级常量)。

字段默认值对齐当前 reviewer 实现
================================

- ``pass_threshold`` 7.5  ← ChapterCard ``review_threshold`` 默认 7.5
- ``revise_threshold`` 6.5 ← ChapterCard ``revise_threshold`` 默认 6.5
- ``escalation_lower_bound`` 6.0 ← reviewer.py ``ESCALATION_LOWER_BOUND``
- ``escalation_upper_bound`` 8.0 ← reviewer.py ``ESCALATION_UPPER_BOUND``

交叉校验
========

- ``revise_threshold < pass_threshold``  (verdict 三档非空区间)
- ``escalation_lower_bound < escalation_upper_bound``  (升级区间合理)
- 所有字段 ``0.0 <= x <= 10.0``  (对齐 score_overall 取值范围)
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReviewerConfig(BaseModel):
    """``reviewer:`` 配置段(P6-B-13 工作区级 reviewer 阈值默认)。

    Pydantic v2 model;字段默认值对齐当前 ChapterCard 单章字段 +
    ``src/core/agents/reviewer.py`` 模块级常量。

    与 ChapterCard 字段并列但层级不同
    --------------------------------
    本模型是**工作区级默认**(项目级,跨章节复用);ChapterCard
    ``review_threshold`` / ``revise_threshold`` 是**单章级覆盖**。运行时
    优先级 ``ChapterCard > Workspace > Engine`` 留 P6-F-08+ 接入(本任务
    只暴露端点 + service 方法,reviewer agent runtime 暂沿用 ChapterCard
    字段单一来源)。
    """

    model_config = ConfigDict(extra="forbid")

    pass_threshold: float = Field(
        default=7.5,
        ge=0.0,
        le=10.0,
        description=(
            "通过阈值;``score_overall >= pass_threshold`` 即判 pass。"
            "对齐 ChapterCard.review_threshold 默认 7.5。"
        ),
    )
    revise_threshold: float = Field(
        default=6.5,
        ge=0.0,
        le=10.0,
        description=(
            "revise 阈值;``revise_threshold <= score_overall < pass_threshold`` 判 revise,"
            "低于该值判 rewrite。对齐 ChapterCard.revise_threshold 默认 6.5。"
        ),
    )
    escalation_lower_bound: float = Field(
        default=6.0,
        ge=0.0,
        le=10.0,
        description=(
            "边界升 Claude 复审下界(闭区间);对齐 reviewer.py ESCALATION_LOWER_BOUND。"
        ),
    )
    escalation_upper_bound: float = Field(
        default=8.0,
        ge=0.0,
        le=10.0,
        description=(
            "边界升 Claude 复审上界(闭区间);对齐 reviewer.py ESCALATION_UPPER_BOUND。"
        ),
    )

    @model_validator(mode="after")
    def _validate_threshold_ordering(self) -> "ReviewerConfig":
        if self.revise_threshold >= self.pass_threshold:
            raise ValueError(
                "revise_threshold 必须严格小于 pass_threshold "
                f"(当前 revise={self.revise_threshold} >= pass={self.pass_threshold})"
            )
        if self.escalation_lower_bound >= self.escalation_upper_bound:
            raise ValueError(
                "escalation_lower_bound 必须严格小于 escalation_upper_bound "
                f"(当前 lower={self.escalation_lower_bound} >= upper={self.escalation_upper_bound})"
            )
        return self
