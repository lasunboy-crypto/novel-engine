"""章节卡相关请求/响应 schema(P1-B-19 + P5-B-06 + P5-B-07)。

P1-P4 阶段章节卡走文件(``data/cards/<chapter_id>.json``),由人工填入。
``ChapterCardOut`` 是 ``ChapterCard`` 的空子类——读 / 写共用同一份 Pydantic 模型,
PUT 请求体直接复用 ``core.models.chapter_card.ChapterCard``(请求体校验由
Pydantic 自动跑;chapter_id 一致性由路由层 ``ChapterIdMismatch`` 守卫,见
``src/api/errors.py``)。

P5-B-06 起,本模块扩 ``ChapterCardGenerateIn`` 单章自动生成端点请求体。
P5-B-07 起,本模块扩 ``ChapterCardRejectIn`` + ``ChapterCardStatusOut``;
``POST /chapters/{id}/card`` 直接 JSON 上传单章卡留 P5-B-08。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.core.models import ID_PATTERN
from src.core.models.chapter_card import CardStatus, ChapterCard


class ChapterCardOut(ChapterCard):
    """``GET /chapters/{chapter_id}/card`` 响应 data 段(空子类)。

    与 ``Workspace`` / ``WorkspaceOut`` 同套路:字段完全一致,保留 API 表面
    独立演进坐标(P2+ 可能加 ``_links`` / 审核状态等字段)。
    """


class ChapterCardGenerateIn(BaseModel):
    """``POST /chapters/{id}/card:generate`` 请求体(P5-B-06 引入)。

    单章触发 ``ChapterCardGenerator`` agent 生成章节卡(决策 5=C "其余懒生成");
    新生卡默认 ``card_status="draft"`` (单端点不进 pending_review,审核流入口
    走 ``card:approve`` / ``card:reject`` 端点 P5-B-07)。

    ``volume_id`` + ``chapter_number`` 由 caller 显式传入:章节卡 SQLite 表 PK
    是 ``chapter_id`` 单列,无法从 chapter_id 推断卷归属与卷内章号
    (派生器需要这两个字段决定 arc / arc_phase / tension_level)。
    """

    model_config = ConfigDict(extra="forbid")

    volume_id: str = Field(
        ...,
        pattern=ID_PATTERN,
        description="本章所属卷 ID(派生器需要查 volumes 表对应行)",
    )
    chapter_number: int = Field(
        ...,
        ge=1,
        description="本章在所属卷内的 1-based 章序号(派生器查 tension_curve / arcs 用)",
    )


class ChapterCardRetryReferencesIn(BaseModel):
    """``POST /chapters/{id}:retry-references`` 请求体(P6-B-10 引入)。

    供前端 ChapterCardReviewPage 在引用合法性校验失败(REFERENCE_VALIDATION_FAILED
    422)后用户点"重试引用"按钮触发。服务层走 ``retry_references`` 方法,显式
    把 ``is_retry_with_strict_ids=True`` 注入 agent context,严格只用工作区
    ``characters`` / ``plot_threads`` 表已有 ID。

    ``volume_id`` + ``chapter_number`` 与 ``ChapterCardGenerateIn`` 同语义
    (派生器需要的卷归属与卷内章号);单独 schema 是为前后端契约清晰 + 接口位
    保留(P6+ 可能扩 ``include_revoked_refs`` 等开关)。
    """

    model_config = ConfigDict(extra="forbid")

    volume_id: str = Field(
        ...,
        pattern=ID_PATTERN,
        description="本章所属卷 ID(派生器需要查 volumes 表对应行)",
    )
    chapter_number: int = Field(
        ...,
        ge=1,
        description="本章在所属卷内的 1-based 章序号(派生器查 tension_curve / arcs 用)",
    )


class ChapterCardRetryReferencesOut(BaseModel):
    """``POST /chapters/{id}:retry-references`` 响应 data 段
    (v1.0.x wave 5 session 4 引入,P6-B-10 起原本只返 ``ChapterCardOut``)。

    含本次重试后的章节卡 + cascade 级联失效信息 — wave 5 session 4 (k):
    用户改本章节卡后,后续章节卡基于旧上下文派生的内容已失效,需在后台批量
    重生。前端按 ``invalidated_chapter_ids`` 提示用户"修改本章节卡后续 N 章
    需要重生,确认继续?"二次确认对话框;``cascade_regenerate_task_id`` 非 null
    时订阅 ``task.{id}.progress`` WS 频道接 cascade 重生进度。

    ``cascade_regenerate_task_id`` 为 ``None`` 时:本章是末章 / 无后续章节卡,
    无 cascade 触发。
    """

    model_config = ConfigDict(extra="forbid")

    card: ChapterCardOut = Field(
        ..., description="本次重试后的章节卡(已落 SSOT + ORM upsert)"
    )
    invalidated_chapter_ids: list[str] = Field(
        ...,
        description=(
            "因本次卡修改 cascade 失效的后续章节 chapter_id 列表(字典序)。"
            "空 list = 本章是末章 / 无后续章节卡,无级联。**总是返显式 list,"
            "即使空 — OpenAPI required = 前端 types 字段非 optional**"
        ),
    )
    cascade_regenerate_task_id: str | None = Field(
        default=None,
        description=(
            "级联批量重生任务 ID(订阅 ``task.{id}.progress`` 接 stage / "
            "done / batch_partial 进度)。``invalidated_chapter_ids`` 为空时此项 ``None``"
        ),
    )


class ChapterCardApproveIn(BaseModel):
    """``POST /chapters/{id}/card:approve`` 请求体(P5-B-07 引入)。

    审核通过无需额外参数;本 schema 是空模型,保留接口位以备 P6+ 扩
    ``approver_id`` / ``approve_reason`` 等审计字段(对齐
    ``WorkspaceCleanResult`` 空响应模式)。
    """

    model_config = ConfigDict(extra="forbid")


class ChapterCardRejectIn(BaseModel):
    """``POST /chapters/{id}/card:reject`` 请求体(P5-B-07 引入)。

    ``rejection_notes`` 可选(默认 ``None``):由前端 ChapterCardReviewPage
    "退回修改"按钮的 textarea 收集,持久化到 ``ChapterCardORM.rejection_notes``
    + WS event payload。
    """

    model_config = ConfigDict(extra="forbid")

    rejection_notes: str | None = Field(
        default=None,
        description="审核退回原因(可选,前端 textarea 收集)",
    )


class ChapterCardsResetWordCountIn(BaseModel):
    """v1.1 wave 1 session 2 (G6.2):``POST /chapter-cards:reset-word-count``
    请求体。

    工作区级批量重置已生成章节卡的 ``word_count_min/max`` 到当前 ``ChapterCard``
    model default 或用户指定值。痛场景:wave 6 s2.6d-prep 调全局 default 后,
    老工作区章节卡仍用旧 default,反复撞 ``word_count|below_min``。
    """

    model_config = ConfigDict(extra="forbid")

    scope: Literal["unapproved", "all"] = Field(
        default="unapproved",
        description=(
            "重置范围:``unapproved``(默认,跳过 card_status='approved') / "
            "``all``(含 approved)。默认 unapproved 保护用户审过的字数预算"
        ),
    )
    word_count_min: int | None = Field(
        default=None,
        ge=1,
        description=(
            "应用的字数下限;留空(``null``)= 用 ``ChapterCard`` model default(2500)。"
            "需 <= word_count_max"
        ),
    )
    word_count_max: int | None = Field(
        default=None,
        ge=1,
        description=(
            "应用的字数上限;留空(``null``)= 用 ``ChapterCard`` model default(4000)。"
            "需 >= word_count_min"
        ),
    )


    word_count_policy_version: Literal[0, 1, 2] | None = Field(
        None, description="显式采用篇幅策略；2 为约数目标（min=max），1 为范围要求，均不按字数阻断；省略保留旧值"
    )


class ChapterCardsResetWordCountResult(BaseModel):
    """v1.1 wave 1 session 2 (G6.2):``POST /chapter-cards:reset-word-count``
    响应 data 段。

    字段集对齐 service 层 ``reset_word_count_for_workspace`` 返回 dict
    (workspace_id / scope / applied min/max 回显参数 + 三类章节 ID 分组列表)。
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(..., description="工作区 ID(回显请求 URL 参数)")
    scope: Literal["unapproved", "all"] = Field(
        ..., description="重置范围(回显请求体参数)"
    )
    applied_word_count_min: int = Field(
        ...,
        ge=1,
        description=(
            "实际应用的字数下限(入参 None 时回退到 ChapterCard model default)"
        ),
    )
    applied_word_count_max: int = Field(
        ...,
        ge=1,
        description=(
            "实际应用的字数上限(入参 None 时回退到 ChapterCard model default)"
        ),
    )
    applied_word_count_policy_version: Literal[0, 1, 2] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    reset_chapter_ids: list[str] = Field(
        ..., description="成功重置的章节 ID 列表(已字典序排序)"
    )
    skipped_chapter_ids: list[str] = Field(
        ...,
        description=(
            "因 scope='unapproved' 跳过的 approved 章节 ID 列表。"
            "scope='all' 时为空 list"
        ),
    )
    failed_chapter_ids: list[str] = Field(
        ...,
        description=(
            "读盘 / 写盘失败的章节 ID 列表(service 层 swallow + log,"
            "不阻断其他章节的重置)。空 list = 全部成功"
        ),
    )


class ChapterCardStatusOut(BaseModel):
    """``POST /chapters/{id}/card:approve`` / ``card:reject`` 响应 data 段
    (P5-B-07 引入)。

    与 ``ChapterCardOut`` 区分:本 schema 只携带审核状态相关字段
    (chapter_id / from_status / to_status / 时间戳 / 退回理由),不返完整
    ChapterCard JSON(前端 ChapterCardReviewPage 已有完整卡的本地 cache,
    状态变更后只需 patch 状态字段;减少响应体积)。

    与 WS event ``card_approved`` / ``card_rejected`` payload 字段同源
    (前端 mutation onSuccess 与 WS onMessage 走同一更新 reducer)。
    """

    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(..., description="章节 ID")
    from_status: CardStatus = Field(..., description="转换前的 card_status")
    to_status: CardStatus = Field(..., description="转换后的 card_status")
    card_approved_at: datetime | None = Field(
        default=None,
        description="``approved`` 时填,其他 to_status 为 None",
    )
    rejection_notes: str | None = Field(
        default=None,
        description="``rejected`` 时若 body 含 ``rejection_notes`` 则回显,其他为 None",
    )
