"""ChapterSummary CRUD service(v1.0.x wave 4 session 5 引入)。

职责:暴露 ``GET /workspaces/{id}/chapter-summaries``(列表)+ ``PUT
/workspaces/{id}/chapter-summaries/{chapter_id}``(单条编辑)给前端
OutlinePage 第 6 tab 用。**只做 CRUD,不调 LLM**(LLM 抽取在
``src/core/agents/chapter_summary_extractor.py``,经 ChapterService /
ChapterRewindService 的 reviewer_pass / force_pass hook 自动落 ORM;
本 service 是用户事后手动编辑入口；任何可编辑事实变化都会撤销
``validated_at``，防止旧 ready/provenance 继续被信任)。

设计要点:

1. **service 层不直连 SQL**(对齐 CLAUDE.md §"实施纪律"路由不直连 DB,
   service 层封装 SQLAlchemy session 边界)。
2. **workspace_id 强一致**:list / update 都通过 ``workspace_service.info``
   校验工作区存在(不存在 → ``WorkspaceServiceError``,API 层翻 404)。
3. **update_summary 不创建新行**(对齐 P5-B-05 ``card_service`` 单卡 PUT
   语义);LLM 抽取产生 ORM 行,用户只编辑,不手动 INSERT。
4. **updated_at 自动写**;``created_at`` 保留首次抽取时间不动(对齐
   ``chapter_summary_extractor.extract_and_persist_chapter_summary_safely``
   既有 upsert 模式)。
5. **fail-fast not found**:``ChapterSummaryNotFoundError`` 让 API 层翻
   404,而非创建空行(避免和 LLM 抽取竞态)。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from sqlalchemy.orm import Session

from src.core.agents.chapter_summary_extractor import ChapterSummary
from src.core.db.models import ChapterSummaryORM
from src.services.workspace_service import WorkspaceService

_log = structlog.get_logger(__name__)


class ChapterSummaryServiceError(Exception):
    """``ChapterSummaryService`` 业务异常基类(API 层翻 errors.py)。"""


class ChapterSummaryNotFoundError(ChapterSummaryServiceError):
    """指定 chapter_id 在工作区内无 ChapterSummary 行(LLM 尚未抽取 / 章节
    未 approved / chapter_id 错)。API 层翻 ``CHAPTER_SUMMARY_NOT_FOUND 404``。"""


# 工作区级 session_factory 签名(对齐 chapter_card_service.py / outline_service.py)
WorkspaceSessionFactory = Callable[[str], Session]


class ChapterSummaryService:
    """章节摘要 CRUD service(GET 列表 + PUT 单条编辑)。"""

    def __init__(
        self,
        *,
        workspace_service: WorkspaceService,
        session_factory: WorkspaceSessionFactory | None = None,
    ) -> None:
        self._workspace_service = workspace_service
        self._session_factory = session_factory

    def list_summaries(self, workspace_id: str) -> list[ChapterSummary]:
        """列出工作区下所有 ChapterSummary,按 ``chapter_id`` ASC 排序。

        无 session_factory(单测路径)→ 返空 list(对齐既有 session_factory
        None noop 同型)。
        """
        # 校验工作区存在(meta.json 损坏 / 不存在 → WorkspaceServiceError)
        self._workspace_service.info(workspace_id)

        if self._session_factory is None:
            return []
        session = self._session_factory(workspace_id)
        try:
            rows = (
                session.query(ChapterSummaryORM)
                .filter(ChapterSummaryORM.workspace_id == workspace_id)
                .order_by(ChapterSummaryORM.chapter_id.asc())
                .all()
            )
            return [self._orm_to_pydantic(r) for r in rows]
        finally:
            session.close()

    def update_summary(
        self,
        workspace_id: str,
        chapter_id: str,
        *,
        summary_text: str | None = None,
        pov_character: str | None = None,
        scene_setting: str | None = None,
        key_events: list[dict] | None = None,
        character_interactions: list[dict] | None = None,
        unfinished_actions: list[str] | None = None,
    ) -> ChapterSummary:
        """更新指定 chapter_id 摘要的部分字段。

        语义:**只更新非 None 字段**(PATCH 语义);``updated_at`` 自动写,
        ``created_at`` / ``extraction_trigger`` / ``model_used`` 保留不动
        (后两者是 LLM 抽取的审计痕迹,用户编辑不改)。

        :raises ChapterSummaryNotFoundError: chapter_id 在工作区内无对应行
        :raises WorkspaceServiceError: 工作区不存在 / meta 损坏
        :raises ChapterSummaryServiceError: session_factory 未注入(运行时
            配置错;单测路径 list 返空但 update 必须有 session — 不允许
            "假装更新"语义不一致)
        """
        self._workspace_service.info(workspace_id)

        if self._session_factory is None:
            raise ChapterSummaryServiceError(
                "ChapterSummaryService.update_summary 需要 session_factory"
                " 注入(运行时配置错)"
            )

        session = self._session_factory(workspace_id)
        try:
            row = session.get(ChapterSummaryORM, chapter_id)
            if row is None or row.workspace_id != workspace_id:
                raise ChapterSummaryNotFoundError(
                    f"chapter_summary not found: chapter_id={chapter_id}"
                    f" workspace_id={workspace_id}"
                )

            now = datetime.now(UTC)
            ledger_fields_changed = any(
                value is not None
                for value in (
                    summary_text,
                    pov_character,
                    scene_setting,
                    key_events,
                    character_interactions,
                    unfinished_actions,
                )
            )
            if summary_text is not None:
                row.summary_text = summary_text
            if pov_character is not None:
                # 空字符串视作清空(翻 None,对齐 ORM nullable=True 语义)
                row.pov_character = pov_character if pov_character else None
            if scene_setting is not None:
                row.scene_setting = scene_setting if scene_setting else None
            if key_events is not None:
                row.key_events = key_events
            if character_interactions is not None:
                row.character_interactions = character_interactions
            if unfinished_actions is not None:
                row.unfinished_actions = unfinished_actions
            if ledger_fields_changed:
                # R4:人工编辑会改变结构事实，却没有重新跑 quote/hash 校验。
                # 保留旧 hash 供审计，但撤销 validated 水位；后续 ready / card
                # provenance 检查会 fail closed，直到从 final 重新抽取小账本。
                row.validated_at = None
            row.updated_at = now

            try:
                session.commit()
            except Exception:
                session.rollback()
                raise
            session.refresh(row)
            _log.info(
                "chapter_summary_updated",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
            )
            return self._orm_to_pydantic(row)
        finally:
            session.close()

    @staticmethod
    def _orm_to_pydantic(row: ChapterSummaryORM) -> ChapterSummary:
        """ORM → Pydantic(``ChapterSummary``)字面转换。"""
        return ChapterSummary(
            chapter_id=row.chapter_id,
            workspace_id=row.workspace_id,
            summary_text=row.summary_text,
            pov_character=row.pov_character,
            scene_setting=row.scene_setting,
            key_events=list(row.key_events or []),
            character_interactions=list(row.character_interactions or []),
            unfinished_actions=list(row.unfinished_actions or []),
            character_states=list(row.character_states or []),
            established_facts=list(row.established_facts or []),
            revealed_mechanics=list(row.revealed_mechanics or []),
            raw_final_tail=row.raw_final_tail,
            source_version_id=row.source_version_id,
            source_text_hash=row.source_text_hash,
            evidence_records=list(row.evidence_records or []),
            ledger_hash=row.ledger_hash,
            validated_at=row.validated_at,
            extraction_trigger=row.extraction_trigger,  # type: ignore[arg-type]
            model_used=row.model_used,
            extracted_at=row.updated_at or row.created_at,
        )


__all__ = [
    "ChapterSummaryNotFoundError",
    "ChapterSummaryService",
    "ChapterSummaryServiceError",
]
