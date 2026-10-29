"""ChapterSummary 路由请求 / 响应 schema(v1.0.x wave 4 session 5)。

供前端 OutlinePage 第 6 tab(``ChapterSummaryTabContent``)展示 + 编辑用。

端点 schema
============

- ``GET /workspaces/{id}/chapter-summaries`` — response ``list[ChapterSummaryOut]``
- ``PUT /workspaces/{id}/chapter-summaries/{chapter_id}`` — body ``ChapterSummaryUpdateIn``
  → response ``ChapterSummaryOut``
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ChapterSummaryOut(BaseModel):
    """单条 ChapterSummary 响应模型(对齐 ``ChapterSummaryORM`` 字段集)。"""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(..., description="章节 ID")
    workspace_id: str = Field(..., description="工作区 ID")
    summary_text: str = Field(..., description="本章核心摘要(LLM 抽取)")
    pov_character: str | None = Field(
        default=None, description="本章主视角角色 ID(可空)"
    )
    scene_setting: str | None = Field(
        default=None, description="本章主场景(可空)"
    )
    key_events: list[dict] = Field(
        default_factory=list, description="关键事件 list[{description, actors, location}]"
    )
    character_interactions: list[dict] = Field(
        default_factory=list,
        description="角色互动 list[{char_a, char_b, dynamic}]",
    )
    unfinished_actions: list[str] = Field(
        default_factory=list, description="未完动作短句列表"
    )
    character_states: list[dict] = Field(
        default_factory=list, description="角色章末状态及原文证据"
    )
    established_facts: list[str] = Field(
        default_factory=list, description="正文已确认、后文不得无故推翻的事实"
    )
    revealed_mechanics: list[str] = Field(
        default_factory=list, description="正文已揭示的机制边界"
    )
    core_state_candidates: list[dict] = Field(
        default_factory=list,
        description="核心状态候选及确定性准入结论；只读，不接受人工摘要 PATCH",
    )
    raw_final_tail: str = Field(
        default="", description="由代码截取的 final.md 末尾确定性上下文"
    )
    source_version_id: int = Field(
        default=0, description="小账本对应的最终正文版本"
    )
    source_text_hash: str = Field(
        default="", description="小账本来源 final.md 的 SHA-256"
    )
    evidence_records: list[dict] = Field(
        default_factory=list,
        description="结构事实到 evidence_quote/fingerprint 的逐条映射",
    )
    ledger_hash: str = Field(default="", description="可复算的连续性小账本 hash")
    validated_at: datetime | None = Field(
        default=None, description="最近一次通过 final/card/version/quote 校验的时间"
    )
    extraction_trigger: str = Field(
        ..., description="抽取触发(reviewer_pass / force_pass)"
    )
    model_used: str = Field(..., description="LLM 模型名(审计追溯)")
    extracted_at: datetime = Field(..., description="最后抽取 / 更新时间")


class ChapterSummaryUpdateIn(BaseModel):
    """``PUT /chapter-summaries/{chapter_id}`` 请求体。

    PATCH 语义:**所有字段全 optional**;只更新非 None 字段。``extraction_trigger``
    / ``model_used`` / ``created_at`` 不允许用户改(审计字段)。
    """

    model_config = ConfigDict(extra="forbid")

    summary_text: str | None = Field(
        default=None, min_length=1, description="摘要文本(非空)"
    )
    pov_character: str | None = Field(
        default=None,
        description="视角角色 ID;传空字符串视作清空(翻 None)",
    )
    scene_setting: str | None = Field(
        default=None,
        description="主场景;传空字符串视作清空(翻 None)",
    )
    key_events: list[dict] | None = Field(
        default=None, description="关键事件列表(整段替换)"
    )
    character_interactions: list[dict] | None = Field(
        default=None, description="角色互动列表(整段替换)"
    )
    unfinished_actions: list[str] | None = Field(
        default=None, description="未完动作列表(整段替换)"
    )


__all__ = [
    "ChapterSummaryOut",
    "ChapterSummaryUpdateIn",
]
