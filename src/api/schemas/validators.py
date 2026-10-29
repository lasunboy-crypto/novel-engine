"""[HARD] validator 报告响应 schema(P6-B-11 引入)。

P4-B-06 / P4-B-07 落地的 ``ValidatorPipelineReport`` 此前只走 structlog
``validator_failed`` / ``validator_passed`` 事件 + 失败约束名落 ``ReviewResultORM.
failed_constraints``,**未暴露完整 ``ValidatorResult.details`` 给 API**。

P6-B-11 新建 ``GET /chapters/{chapter_id}/validators/latest`` 端点(plan §"P6-B-11"
字面,类 C 顺延 — P6-F-01 字面引用"P4-B-06 validator result_json"但工程实情
未暴露,故新建端点 + schema 补足规格源),走 on-the-fly 跑 validator pipeline
(读 ``final.md`` + 章节卡 + Character / TimelineEntry → 调
``run_validator_pipeline`` → 序列化为本 schema),供前端
``ChapterCardReviewPage`` "关键字段达成对比"块消费(FRONTEND_SPEC §4.9
字面 must_achieve 逐项打勾)。

字段对齐 ``src/core/rules/validator_pipeline.py:ValidatorPipelineReport`` +
``src/core/rules/validators.py:ValidatorResult``:**前者是 dataclass(frozen=True)
不可直接做 Pydantic 响应模型,后者 ``details`` 是 ``dict[str, Any]`` —— 本 schema
显式定义对应 BaseModel 子类**,前端 OpenAPI 类型生成可拿到稳定 schema。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ValidatorResultOut(BaseModel):
    """单个 validator 的输出(对齐 ``src/core/rules/validators.py:ValidatorResult``)。

    ``details`` 字段为字典,key / value 因 validator 类型而异:

    - ``word_count``:``actual_word_count`` / ``word_count_min`` / ``word_count_max``
      / ``reason``(``below_min`` / ``above_max``)。
    - ``must_achieve``:``items_total`` / ``items_missing`` /
      可选 ``llm_fallback_used``。
    - ``must_unresolved``:``items_total`` / ``items_resolved`` /
      可选 ``llm_fallback_used``。
    - ``forbidden``:``items_total`` / ``forbidden_hits``。
    - ``character_existence``:``related_characters_total`` /
      ``missing_character_ids``。
    - ``timeline_monotonic``:``entries_total`` / ``violations``。
    - ``death_foreshadowing``:``dead_character_ids`` / ``related_chars_total``。
    - ``voice_forbidden``:``character_violations`` 列表。
    - ``unfinished_actions_continuation``:``hit_actions`` /
      ``unfinished_actions_total`` / ``window_chars``。
    - ``scene_continuation_location``:``location_keywords_found`` / 等。

    前端按 ``name`` 分流渲染 details。
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    name: str = Field(..., description="validator 名称(枚举值同 ValidatorResult.name)")
    passed: bool = Field(..., description="是否通过")
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="失败 / 通过详情;字段集合因 validator 类型而异(见 docstring)",
    )


class ValidatorReportOut(BaseModel):
    """章节最近一次 validator 跑批的完整报告(P6-B-11)。

    服务层 ``ValidatorReportService.get_latest_report`` 现跑现产:读 ``final.md``
    + 章节卡 + Character / TimelineEntry → 调 ``run_validator_pipeline`` →
    本 schema。**纯函数 + 子串检索 + ORM 查询无 LLM 调用**,~ms 级,前端
    review 页随用随调可接受;不持久化(若 P7+ 需要历史快照,加 review_result
    扩列即可,本端点契约不变)。

    与 ``ReviewSummaryEntry.hard_constraints_passed`` 整体 boolean 互补 —— 后者
    汇总结果存于 review_results 表,本端点提供逐项 details(前端"关键字段
    达成对比"块需要逐项 must_achieve 打勾 / forbidden 命中红色等)。
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    chapter_id: str = Field(..., description="章节 ID")
    workspace_id: str = Field(..., description="工作区 ID")
    passed: bool = Field(
        ..., description="所有 validator 全过 = True;任一失败 = False"
    )
    failed_constraint_names: list[str] = Field(
        default_factory=list,
        description=(
            "失败 validator 名称列表(同 ValidatorPipelineReport.failed_constraint_names);"
            "进 ReviewResult.failed_constraints 的同一份数据,前端可作为"
            "快速 chip 显示"
        ),
    )
    validators: list[ValidatorResultOut] = Field(
        ...,
        description=(
            "每个 validator 的逐项结果;**失败的排在前面**(对齐 "
            "ValidatorPipelineReport.results 排序),便于前端取前 N 条做"
            "概览面板"
        ),
    )
    chapter_text_length: int = Field(
        ...,
        ge=0,
        description=(
            "本次跑批基于的章节正文长度(``len(final.md)``,中文字符数);"
            "前端可用于核对 word_count validator 结果一致性"
        ),
    )
