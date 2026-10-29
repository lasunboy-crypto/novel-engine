"""章节与版本模型(对应 PROJECT_SPEC §3.5)。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from src.core.models.chapter_card import ChapterCard
from src.core.models.review import ReviewResult


class ChapterVersion(BaseModel):
    """章节版本快照(A7)。

    每次 draft / rewrite / revise / manual_edit 都新增一个版本,落盘到工作区。
    """

    version_id: int = Field(..., ge=1, description="版本号(从 1 起)")
    content: str = Field(..., description="本版本正文")
    word_count: int = Field(..., ge=0, description="正文字数")
    created_at: datetime = Field(..., description="版本创建时间(UTC)")
    creation_type: Literal["draft", "rewrite", "revise", "manual_edit"] = Field(
        ..., description="本版本的创建方式"
    )
    model_used: str = Field(..., description="生成本版本所用的模型 ID")
    review_score: float | None = Field(..., description="本版本的综合评分,无则 None")
    notes: str = Field(..., description="备注信息")
    style_fingerprint: dict[str, float] | None = Field(
        default=None,
        description=(
            "v1.1 wave 1 session 4(G5)起加入的风格指纹。4 维度纯规则统计:"
            "段落长度均值/标准差、人称代词频次、句长均值/标准差、修辞密度。"
            "首版(v1)抽取后落该字段作 baseline;后续版本对比偏移度。"
            "None = 未抽取(向后兼容旧数据)。"
        ),
    )


class Chapter(BaseModel):
    """章节聚合根。

    持有 ChapterCard、所有版本、审稿历史、状态与累计成本。
    """

    chapter_id: str = Field(..., description="章节唯一 ID")
    workspace_id: str = Field(..., description="所属工作区 ID")
    card: ChapterCard = Field(..., description="本章的章节卡")
    current_version: int = Field(..., ge=1, description="当前激活的版本号")
    versions: list[ChapterVersion] = Field(..., description="本章所有版本快照")
    status: Literal[
        "drafting",
        "reviewing",
        "rewriting",
        "revising",
        "approved",
        "published",
        "human_intervention",
        "invalidated",
    ] = Field(
        ...,
        description=(
            "章节状态(状态机当前节点;P6-B-01 起 8 档,``invalidated`` 表"
            "rewind 触发后上游被改写、本章 stale 需 user 显式 unblock 才能"
            "再 write/rewrite/revise)"
        ),
    )
    rewrite_count: int = Field(0, ge=0, description="累计重写次数")
    revise_count: int = Field(0, ge=0, description="累计修订次数(A2)")
    review_history: list[ReviewResult] = Field(..., description="累计审稿历史")
    created_at: datetime = Field(..., description="章节创建时间(UTC)")
    approved_at: datetime | None = Field(..., description="通过审稿的时间,未通过为 None")
    total_token_usage: dict = Field(..., description="累计 token 使用统计")
    total_cost_usd: float = Field(..., ge=0, description="累计成本(美元)")
