"""[HARD] validator 报告 service(P6-B-11 引入,类 C 顺延 P6-F-01)。

P4-B-06 / P4-B-07 ``ValidatorPipelineReport`` 此前只走 structlog +
``ReviewResultORM.failed_constraints``(只存失败 validator 名),不暴露逐项
``ValidatorResult.details`` 给前端;P6-F-01 ChapterCardReviewPage "关键字段
达成对比"块字面需要 ``must_achieve`` 逐项打勾 / ``forbidden`` 命中红色,故
新建本 service + ``GET /chapters/{id}/validators/latest`` 端点。

设计要点
========

1. **on-the-fly 重跑 pipeline,不读历史 review_results**:validator 是纯函数
   (子串检索 + ORM 查询 + 启发式;无 LLM 调用),~ms 级开销,前端 review
   页打开时随用随调可接受。优点:
   - 不动 Orchestrator 写入路径(P4-B-06 既有逻辑零变更,零回归面)
   - 不开 Alembic head(无 schema 变更,对齐 CLAUDE.md §Alembic head 纪律)
   - 跑出来的结果与最新章节正文 / 章节卡 / 角色 / 时间线对齐(若用户改了卡
     再开 review 页,看到的就是新数据下的结果)

   缺点:若 P7+ 需要"历史 review 时刻"的 validator 报告快照,本端点拿不到
   (需扩 ``ReviewResultORM`` 加 ``validator_report_json`` 列 + Orchestrator
   写入路径接入)。本任务范围内不做。

2. **读输入复用既有 service helper**:
   - ``final.md``:走 ``WorkspacePaths.chapter_version_file(id, "final.md")``
     直接读文件;不复用 ``ChapterService.read_version_content``(后者只读
     ``v{N}.md`` 版本文件,不接 ``final.md``)。
   - ``ChapterCard``:走 ``ChapterService.read_chapter_card`` 复用既有错误
     语义(``ChapterServiceError`` 翻 422 ``CHAPTER_CARD_INVALID`` /
     ``ChapterCardNotFound`` 翻 404 ``CHAPTER_CARD_NOT_FOUND``)。
   - ``Character`` / ``TimelineEntry``:自己 ``select`` ORM + 过滤
     ``revoked_at IS NULL``(P6-B-01 soft-delete 联动 — rewind 后该角色 /
     时间线条目从校验视野排除);**不复用 store ``list_*_by_workspace``**
     (它们不过滤 revoked_at,与 P6-B-10 reference_validator 同型)。

3. **写前准入只读快照 + 写后 pipeline**:R9 起角色存在/死亡引用已从写后
   rewrite pipeline 移到 Provider 前。本查询仍用同一纯函数把两项当前数据状态
   展示给前端，但只作报告，不会派 rewrite。DB 表为空时
   ``character_existence`` 明确失败，前端可引导用户先导入角色或修章节卡。

4. **不发 WS event**:本 service 是查询性 GET,无副作用,无需 publish
   (对齐 ``ChapterCardService.read_*`` 模式)。

5. **不持久化结果**:每次调用都重跑(纯函数 + ms 级),不缓存(无 invalidate
   时机的复杂度)。前端按需求拉,不轮询。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.db import CharacterORM, TimelineEntryORM
from src.core.memory.store import (
    _character_orm_to_pydantic,
    _timeline_entry_orm_to_pydantic,
)
from src.core.models.character import Character
from src.core.models.timeline_entry import TimelineEntry
from src.core.rules import run_validator_pipeline
from src.core.rules.validators import (
    check_character_existence,
    check_dead_character_references,
)
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.chapter_service import ChapterService
from src.services.workspace_service import WorkspaceService

SessionFactory = Callable[[str], Session]


class ValidatorReportServiceError(Exception):
    """validator 报告 service 基类异常。"""


class ValidatorReportNotFoundError(ValidatorReportServiceError):
    """章节无 ``final.md`` —— validator 无正文可校(P6-B-11)。

    抛出条件:章节存在 + 章节卡存在,但 ``data/chapters/<chapter_id>/final.md``
    缺失(章节尚未进入 ``approved`` 状态)。翻译为 ``VALIDATOR_REPORT_NOT_FOUND
    (404)``。

    与 ``ChapterCardNotFound`` 区分:后者是"章节卡本身缺失"(走 422 ``CHAPTER_
    CARD_INVALID`` 或 404 ``CHAPTER_CARD_NOT_FOUND``),本错误码是"卡有 + 正文
    没有",前端可据此提示"先 ``:write`` 写出正文再来跑校验"。
    """


@dataclass
class ValidatorResultPayload:
    """单个 validator 结果的 service 层载体。

    字段与 ``src/api/schemas/validators.py:ValidatorResultOut`` 完全对齐 ——
    路由层 ``ValidatorResultOut.model_validate`` 自动转换。
    """

    name: str
    passed: bool
    details: dict[str, object] = field(default_factory=dict)


@dataclass
class ValidatorReportPayload:
    """validator 报告的 service 层载体。

    字段与 ``src/api/schemas/validators.py:ValidatorReportOut`` 完全对齐 ——
    路由层 ``ValidatorReportOut.model_validate`` 自动转换。**service 层不
    import Pydantic schema**,保持业务层与 API 表面层解耦(对齐
    ``ChapterCardService.generate_*`` 返 ``ChapterCard`` Pydantic 而非
    ``ChapterCardOut``)。
    """

    chapter_id: str
    workspace_id: str
    passed: bool
    failed_constraint_names: list[str]
    validators: list[ValidatorResultPayload]
    chapter_text_length: int


class ValidatorReportService:
    """[HARD] validator 报告查询服务(P6-B-11)。

    依赖 ``WorkspaceService``(校验 workspace 存在 + 读路径根)+ ``ChapterService``
    (复用 ``read_chapter_card``)+ ``session_factory``(读 ``CharacterORM`` /
    ``TimelineEntryORM``,过滤 ``revoked_at IS NULL``)。

    无 LLM client / WS manager(纯查询服务,无副作用)。
    """

    def __init__(
        self,
        *,
        workspace_service: WorkspaceService,
        chapter_service: ChapterService,
        session_factory: SessionFactory,
    ) -> None:
        self._workspace_service = workspace_service
        self._chapter_service = chapter_service
        self._session_factory = session_factory

    def get_latest_report(
        self, workspace_id: str, chapter_id: str
    ) -> ValidatorReportPayload:
        """跑一次完整 validator pipeline,返序列化后的报告。

        前置校验:
        1. workspace 存在(``WorkspaceService.info`` 缺失抛 ``MetaFileError``
           → 翻 422 ``WORKSPACE_INVALID``);
        2. 章节卡存在(``ChapterService.read_chapter_card`` 缺失抛
           ``ChapterServiceError`` → 翻 422 ``CHAPTER_CARD_INVALID``);
           **路由层先调 ``_ensure_card_exists`` 守卫 → 404 文件不存在**;
        3. ``final.md`` 存在 → 不存在抛 ``ValidatorReportNotFoundError``。

        :returns: ``ValidatorReportPayload`` dataclass — 路由层
            ``ValidatorReportOut.model_validate`` 转 Pydantic 响应。
        """
        # 1. workspace 校验(冒泡 MetaFileError → 422)。
        self._workspace_service.info(workspace_id)

        # 2. 章节卡(冒泡 ChapterServiceError → 422 / 404 路由守卫已前置)。
        card = self._chapter_service.read_chapter_card(workspace_id, chapter_id)

        # 3. final.md(走 WorkspacePaths 直读避免触发 DB 查询)。
        paths = WorkspacePaths(
            workspace_id,
            workspaces_root=self._workspace_service.workspaces_root,
        )
        final_path = paths.chapter_version_file(chapter_id, "final.md")
        if not final_path.is_file():
            raise ValidatorReportNotFoundError(
                f"章节正文不存在: workspace={workspace_id} chapter={chapter_id} "
                f"(final.md 缺失,需先 :write 写出正文)"
            )
        chapter_text = final_path.read_text(encoding="utf-8")

        # 4. 读 ORM 数据 — 过滤 revoked_at IS NULL(P6-B-01 soft-delete 联动)。
        characters: list[Character]
        timeline_entries: list[TimelineEntry]
        session = self._session_factory(workspace_id)
        try:
            char_rows = session.scalars(
                select(CharacterORM)
                .where(CharacterORM.workspace_id == workspace_id)
                .where(CharacterORM.revoked_at.is_(None))
                .order_by(CharacterORM.character_id)
            ).all()
            characters = [_character_orm_to_pydantic(r) for r in char_rows]

            tl_rows = session.scalars(
                select(TimelineEntryORM)
                .where(TimelineEntryORM.workspace_id == workspace_id)
                .where(TimelineEntryORM.revoked_at.is_(None))
                .order_by(
                    TimelineEntryORM.time_order, TimelineEntryORM.entry_id
                )
            ).all()
            timeline_entries = [_timeline_entry_orm_to_pydantic(r) for r in tl_rows]
        finally:
            session.close()

        # 5. 用与 Writer 前 gate 相同的纯函数生成只读准入快照；它们不重新进入
        # 写后 pipeline，避免把数据问题错误路由成正文 rewrite。
        admission_results = [
            check_character_existence(
                chapter_card=card,
                known_character_ids={c.character_id for c in characters},
            ),
            check_dead_character_references(
                chapter_card=card,
                character_records=characters,
            ),
        ]

        # 6. 跑写后 pipeline(纯函数,无 LLM)。
        report = run_validator_pipeline(
            chapter_text,
            chapter_card=card,
            workspace_id=workspace_id,
            timeline_entries=timeline_entries,
            character_records=characters,
            allow_word_count_tolerance=self._chapter_service.allows_word_count_tolerance(workspace_id),
        )

        combined_results = [*admission_results, *report.results]
        failed_results = [result for result in combined_results if not result.passed]
        passed_results = [result for result in combined_results if result.passed]
        ordered_results = [*failed_results, *passed_results]

        return ValidatorReportPayload(
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            passed=not failed_results,
            failed_constraint_names=[result.name for result in failed_results],
            validators=[
                ValidatorResultPayload(
                    name=r.name,
                    passed=r.passed,
                    details=dict(r.details),
                )
                for r in ordered_results
            ],
            chapter_text_length=len(chapter_text),
        )
