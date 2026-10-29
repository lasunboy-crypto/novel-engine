"""ChapterCard / WriterOutput 引用合法性后处理(P6-B-10 引入,P5 候选 1 v1 内解决)。

职责:扫描已生成的 ``ChapterCard`` 4 个引用字段(``related_characters`` /
``pov_character`` / ``related_plot_threads`` / ``continuity_context.pov_to``)
是否在工作区 SQLite ``characters`` / ``plot_threads`` 表内有对应行;若发现
未知 ID,抛 :class:`ReferenceValidationFailedError`,details 含
``unknown_character_ids`` / ``unknown_thread_ids``。

与 ``chapter_card_generator._validate_cross_field_references`` 的区别
====================================================================

- ``_validate_cross_field_references`` 校验"LLM 输出 vs caller 在 prompt
  里传入的 ``character_voices`` / ``plot_threads`` 列表" — agent 层"自洽性"
  校验(对齐 P5-B-05 §"跨字段引用完整性后置校验"决策)。
- 本模块校验"ChapterCard vs **工作区 SQLite 表实际行**" — 持久化数据层
  "真实存在性"校验(对齐 P5-acceptance 留观 ① 处置 — Qwen 产 ch-0005 引用
  ``bai-ling`` 角色但工作区表中无该行)。两者职责互补,前者是"agent 不乱
  发明",后者是"agent 不引用过期/虚构 ID"。

设计要点
========

1. **DB 空表 = legacy fixture 兼容**:``characters`` / ``plot_threads`` 表
   对当前 workspace_id 无行时,**跳过对应类目的校验**(返回空 set 视为
   "未知集合",但 unknown_ids 必为空 → 不抛错)。保证 P1-P5 既有测试 +
   尚未 import 资产的工作区零回归。
2. **soft-delete 兼容(P6-B-01)**:``CharacterORM.revoked_at`` /
   ``PlotThreadORM.revoked_at`` 非空行视为"已撤销",**不计入已知集合**。
   rewind 后的章节引用已撤销角色 / 伏笔 → 走 ``:retry-references`` 修复。
3. **纯函数 + 注入式**:本模块不持有 session,接 ``Session`` 实例参数 + 把
   "读 DB" 与 "校验 card" 拆成两个公共函数,便于调用方在不同事务边界内
   组合使用(``ChapterCardService.generate_single_chapter_card`` retry 路径
   + ``ChapterService._run_gates`` writer 前置 gate 各自管理 session)。
4. **错误结构化 + details schema 稳定**:``ReferenceValidationFailedError``
   暴露 ``workspace_id`` / ``chapter_id`` / ``unknown_character_ids`` /
   ``unknown_thread_ids`` 属性,翻译表(``src/api/errors.py``)直接读取构造
   ``ApiError.details`` 字段,前端 ChapterCardReviewPage 可据此引导用户。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from src.core.models.chapter_card import ChapterCard

_log = structlog.get_logger(__name__)


class ReferenceValidationFailedError(ValueError):
    """章节卡 4 字段引用 ID 在工作区表中无对应行时抛出。

    路由层翻译为 ``REFERENCE_VALIDATION_FAILED (422)``;``details`` schema:

    - ``workspace_id``:发生校验失败的工作区 ID
    - ``chapter_id``:章节 ID
    - ``unknown_character_ids``:``related_characters`` / ``pov_character``
      / ``continuity_context.pov_to`` 引用但 SQLite 表无对应行的角色 ID 列表
    - ``unknown_thread_ids``:``related_plot_threads`` 引用但 SQLite 表无对
      应行的伏笔 ID 列表
    """

    def __init__(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        unknown_character_ids: list[str],
        unknown_thread_ids: list[str],
    ) -> None:
        self.workspace_id = workspace_id
        self.chapter_id = chapter_id
        self.unknown_character_ids = list(unknown_character_ids)
        self.unknown_thread_ids = list(unknown_thread_ids)
        super().__init__(
            f"workspace {workspace_id} chapter {chapter_id} 引用合法性校验失败: "
            f"unknown_character_ids={self.unknown_character_ids!r} "
            f"unknown_thread_ids={self.unknown_thread_ids!r}"
        )


def collect_unknown_ids_from_card(
    card: ChapterCard,
    *,
    known_character_ids: set[str],
    known_thread_ids: set[str],
) -> tuple[list[str], list[str]]:
    """扫 ChapterCard 4 字段,返回 ``(unknown_character_ids, unknown_thread_ids)``。

    扫描字段:

    1. ``card.pov_character``:本章视角(单字符串)
    2. ``card.related_characters``:本章出场角色 ID 列表
    3. ``card.continuity_context.pov_to``:衔接桥目标视角(应 == pov_character)
    4. ``card.related_plot_threads``:本章关联伏笔 ID 列表

    返回保持入参顺序(便于测试断言);相同 ID 多处引用仅记一次(``dict.fromkeys``
    保序去重)。

    **DB 空集语义**:``known_character_ids`` 为空 set 时**跳过角色校验**
    (返回空列表),避免 legacy fixture / 未 import 资产工作区误报。
    ``known_thread_ids`` 同理。
    """
    unknown_chars_raw: list[str] = []
    if known_character_ids:
        if card.pov_character not in known_character_ids:
            unknown_chars_raw.append(card.pov_character)
        for cid in card.related_characters:
            if cid not in known_character_ids:
                unknown_chars_raw.append(cid)
        # continuity_context.pov_to 通常 == pov_character;独立扫一次以防分裂
        pov_to = card.continuity_context.pov_to
        if pov_to not in known_character_ids:
            unknown_chars_raw.append(pov_to)

    unknown_threads_raw: list[str] = []
    if known_thread_ids:
        for tid in card.related_plot_threads:
            if tid not in known_thread_ids:
                unknown_threads_raw.append(tid)

    # 保序去重(dict.fromkeys 在 Python 3.7+ 保持插入顺序)
    unknown_chars = list(dict.fromkeys(unknown_chars_raw))
    unknown_threads = list(dict.fromkeys(unknown_threads_raw))
    return unknown_chars, unknown_threads


def load_known_ids_from_workspace(
    session: Session, workspace_id: str
) -> tuple[set[str], set[str]]:
    """读 ``characters`` + ``plot_threads`` 表的非 revoked 行 ID 集合。

    ``revoked_at`` 非空行视为"已撤销"(P6-B-01 rewind soft-delete),**不计入**
    已知集合(rewind 后再 :write 该章节会触发 :retry-references)。

    返回 ``(known_character_ids, known_thread_ids)`` 两个 ``set[str]``。
    """
    from src.core.db.models import CharacterORM, PlotThreadORM

    character_rows = (
        session.query(CharacterORM.character_id)
        .filter(
            CharacterORM.workspace_id == workspace_id,
            CharacterORM.revoked_at.is_(None),
        )
        .all()
    )
    thread_rows = (
        session.query(PlotThreadORM.thread_id)
        .filter(
            PlotThreadORM.workspace_id == workspace_id,
            PlotThreadORM.revoked_at.is_(None),
        )
        .all()
    )
    known_character_ids = {row[0] for row in character_rows}
    known_thread_ids = {row[0] for row in thread_rows}
    return known_character_ids, known_thread_ids


def validate_card_against_workspace(
    card: ChapterCard,
    *,
    session: Session,
    workspace_id: str,
) -> None:
    """高层入口:读 DB + 扫 card → 命中未知引用时抛 ``ReferenceValidationFailedError``。

    DB 表对当前 workspace 空时(``known_character_ids`` / ``known_thread_ids``
    任一为空 set)对应类目 noop(legacy 兼容,见 ``collect_unknown_ids_from_card``)。

    :raises ReferenceValidationFailedError: 命中未知引用,``details`` 见异常 docstring。
    """
    known_char_ids, known_thread_ids = load_known_ids_from_workspace(
        session, workspace_id
    )
    unknown_chars, unknown_threads = collect_unknown_ids_from_card(
        card,
        known_character_ids=known_char_ids,
        known_thread_ids=known_thread_ids,
    )
    if unknown_chars or unknown_threads:
        _log.warning(
            "reference_validation_failed",
            workspace_id=workspace_id,
            chapter_id=card.chapter_id,
            unknown_character_ids=unknown_chars,
            unknown_thread_ids=unknown_threads,
            known_character_count=len(known_char_ids),
            known_thread_count=len(known_thread_ids),
        )
        raise ReferenceValidationFailedError(
            workspace_id=workspace_id,
            chapter_id=card.chapter_id,
            unknown_character_ids=unknown_chars,
            unknown_thread_ids=unknown_threads,
        )


__all__ = [
    "ReferenceValidationFailedError",
    "collect_unknown_ids_from_card",
    "load_known_ids_from_workspace",
    "validate_card_against_workspace",
]
