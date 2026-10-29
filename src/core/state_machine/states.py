"""章节状态枚举。

8 个状态值与 ``Chapter.status`` 的 Literal 字面量完全一致(PROJECT_SPEC §3.5
+ P6-B-01 扩 ``invalidated`` 档)。
"""

from enum import StrEnum


class ChapterState(StrEnum):
    """章节生命周期状态机节点。

    与 ``src.core.models.chapter.Chapter.status`` 的 Literal 一一对应。
    用 StrEnum 是为了让 ``ChapterState.DRAFTING == "drafting"`` 直接成立,
    后续与 Pydantic 模型的字面量字段互操作时不需要 ``.value`` 转换。

    **P6-B-01 扩 ``INVALIDATED`` 档**:rewind 触发后,目标章节回退到 ``drafting``,
    后续章节翻 ``invalidated`` 表"上游被改写,本章 stale,需 user 显式 unblock
    才能再 write/rewrite/revise"。状态机正向流转(``orchestrate_chapter``)
    不会主动产出 ``invalidated``——只有 ``ChapterRewindService.rewind`` 端点
    强制写入;``ChapterRewindService.unblock`` 端点把 ``invalidated`` 翻回
    ``drafting``。
    """

    DRAFTING = "drafting"
    REVIEWING = "reviewing"
    REWRITING = "rewriting"
    REVISING = "revising"
    APPROVED = "approved"
    PUBLISHED = "published"
    HUMAN_INTERVENTION = "human_intervention"
    INVALIDATED = "invalidated"
