"""风格漂移审计服务(P4-B-10;P7 完整实现升级位)。

对齐 OBSERVABILITY §3.1 ``style_audit_history`` 表 + plan §P4-B-10 字面
"StyleAuditor agent 接口预留 stub + 落 style_audit_history 表(severity=stub
行,P7 接 LLM 时复用 schema)"。

职责
====

1. **caller 预读编排**:校验工作区 + 校验 chapter_range_start / _end 章节
   存在性(若有给定)→ 调 ``audit_style`` stub → 转 ``StyleAuditEntry``
   Pydantic → 落 ``style_audit_history`` 表(``persist_style_audit_safely``
   helper,``*_safely`` 模式第 7 处)。
2. **返回 ``StyleAuditEntry`` Pydantic**:含 audit_id / audited_at /
   chapter_range_start / _end / drift_score / severity / report_json,API 路
   由直接序列化为响应。

设计要点
========

- **session 由工厂注入**:与 ``MemoryHealthService`` / ``CharacterService``
  / ``ChapterService`` 模板对齐 — ``session_factory: Callable[[str], Session]``
  接 ``workspace_id`` 返一个新 ``Session``。预读 chapter 校验单独 session,
  持久化单独 session(两 session 关闭点清晰)。
- **不嵌入 LLM client**:本 service P4 阶段不调任何 LLM,**不接 ``router`` /
  ``llm`` / ``model``**;P7 完整实现时再扩(对齐 ``StyleAuditEntry.severity``
  CHECK 约束升级时点)。
- **workspace_id 透传**(P3-B-14):caller 把 ``run_audit(workspace_id, ...)``
  入参 ``workspace_id`` 显式透传 agent + persist helper。
- **``*_safely`` helper 模式第 7 处判定**(P4-B-10 启动指令 ④):本任务
  ``persist_style_audit_safely`` 与 P4-B-02 / P4-B-03 / P4-B-04 / P4-B-08 /
  P4-B-09 六处 DB session swallow 共用 try/except/log + rollback + close
  骨架,但**各处情境略有差异**(P4-B-02 idempotent insert + FK / P4-B-03
  反查多表 / P4-B-04 聚合重算 / P4-B-08 WS publish / P4-B-09 simple insert
  / 本任务 simple insert + 无 FK)— **仍不抽公共模块**,理由对齐 P4-B-09
  自决项 4:抽出 ``with_session_safely`` 通用 helper 需要 callable 签名
  ``(session, *, workspace_id)`` 加 ``op_name`` + ``event_name`` 等多个
  额外入参,**反复传 event_name 比内联 try/except 多 2 行**,且各处独立性
  更利于阅读单条路径完整闭包。P4 后端收官 marker — P5+ 第 8 处再判一次。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.agents.style_auditor import StyleAuditReport, audit_style
from src.core.chapter_versioning import ChapterVersioning, ChapterVersioningError
from src.core.db.models import ChapterORM, StyleAuditEntryORM
from src.core.models.metrics import StyleAuditEntry
from src.core.routing.router import ModelRouter
from src.core.workspace.meta import read_meta
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.workspace_service import WorkspaceService

_log = structlog.get_logger(__name__)

SessionFactory = Callable[[str], Session]
"""按 ``workspace_id`` 开新 SQLite ``Session`` 工厂(与
``MemoryHealthService`` / ``ChapterService`` 同形)。"""

RouterFactory = Callable[[str], ModelRouter]
"""按 ``workspace_id`` 构造 ``ModelRouter`` 工厂(与 ``ChapterService`` 同形,
deps 注入 ``_make_router_factory()``);``None`` 时 StyleAuditor 无 router →
有模板 + 有正文时 resolve_routing 失败走 fail-open(返 ok)。"""

# v1.1 wave 4 s5 / I2.2:单次审计预读章节数上限(控 token + 时延;手动低频
# 端点,取范围内 / 全工作区前 N 章正文片段送审即可代表整体风格)。
_MAX_AUDIT_CHAPTERS = 8


class StyleAuditServiceError(Exception):
    """风格漂移审计业务异常。

    当前 P4-B-10 stub 阶段只用一档:``chapter_range_start`` /
    ``chapter_range_end`` 任一指向不存在的章节 → 抛此异常,由路由层
    翻译为 404 ``CHAPTER_NOT_FOUND``。
    """


class StyleAuditService:
    """风格漂移审计业务层(P4-B-10 stub;P7 完整实现升级位)。

    依赖 ``WorkspaceService``(校验 meta.json + 取 ``Workspace``)+
    ``session_factory``(每次调用 new 一个 Session 给本工作区的 SQLite)。
    """

    def __init__(
        self,
        *,
        workspace_service: WorkspaceService,
        session_factory: SessionFactory,
        router_factory: RouterFactory | None = None,
    ) -> None:
        self._workspace_service = workspace_service
        self._session_factory = session_factory
        self._router_factory = router_factory

    def run_audit(
        self,
        workspace_id: str,
        *,
        chapter_range_start: str | None = None,
        chapter_range_end: str | None = None,
    ) -> StyleAuditEntry:
        """触发一次风格漂移审计(P4 stub)。

        流程:
        1. 校验工作区 meta.json 存在(``WorkspaceService.info``)。
        2. 校验 ``chapter_range_start`` / ``_end`` 章节存在(给定时);任一
           不存在 → 抛 ``StyleAuditServiceError``。
        3. 调 ``audit_style`` stub agent → 得 ``StyleAuditReport``
           (severity="stub" / drift_score=None)。
        4. ``persist_style_audit_safely`` 落 ``style_audit_history`` 表
           (失败 swallow)。
        5. 返回 ``StyleAuditEntry`` Pydantic(API 序列化)。

        :param workspace_id: 当前操作工作区 ID;透传 agent + persist helper。
        :param chapter_range_start: 审计起点 chapter_id(可选);P4 stub 不
            消费内容,仅做存在性校验 + 透传记录。
        :param chapter_range_end: 审计终点 chapter_id(可选);同上。
        :return: ``StyleAuditEntry`` Pydantic 完整记录。
        :raises WorkspaceServiceError / MetaFileError: 工作区不存在或
            meta.json 不合法。
        :raises StyleAuditServiceError: chapter_range_start / _end 任一不存在。
        """
        # 1. 校验工作区。
        self._workspace_service.info(workspace_id)

        # 2. 校验 chapter 范围存在性(给定时)。
        self._verify_chapter_ids(
            workspace_id,
            chapter_range_start=chapter_range_start,
            chapter_range_end=chapter_range_end,
        )

        # 3. 解析工作区写作风格模板(无模板 → agent 短路返 ok)。
        style_template = self._resolve_style_template(workspace_id)

        # 4. 预读范围内章节 final 正文片段(无可审章节 → agent 短路返 ok)。
        excerpts = self._read_chapter_excerpts(
            workspace_id,
            chapter_range_start=chapter_range_start,
            chapter_range_end=chapter_range_end,
        )

        # 5. 调真 StyleAuditor agent(有模板 + 有正文时调 LLM 判偏移度;
        #    router_factory 缺失时 router=None → agent fail-open 返 ok)。
        router = (
            self._router_factory(workspace_id)
            if self._router_factory is not None
            else None
        )
        report = audit_style(
            workspace_id=workspace_id,
            style_template=style_template,
            chapter_excerpts=excerpts,
            chapter_range_start=chapter_range_start,
            chapter_range_end=chapter_range_end,
            router=router,
        )

        # 4. 转 Pydantic + 落表(swallow on failure)。
        entry = _build_style_audit_entry(
            report,
            chapter_range_start=chapter_range_start,
            chapter_range_end=chapter_range_end,
        )
        persist_style_audit_safely(
            self._session_factory,
            workspace_id=workspace_id,
            entry=entry,
        )

        return entry

    # ---- 内部 helper(预读 / 校验)-----------------------------------------

    def _verify_chapter_ids(
        self,
        workspace_id: str,
        *,
        chapter_range_start: str | None,
        chapter_range_end: str | None,
    ) -> None:
        """校验给定 chapter_id 存在于本工作区。

        两个 ID 都为 None → no-op(允许"整体审计无范围"语义,P7 可能扩入
        参覆盖)。任一指向不存在或跨工作区的 chapter → 抛
        ``StyleAuditServiceError`` 由路由层翻译为 404。
        """
        candidates = [
            c for c in (chapter_range_start, chapter_range_end) if c is not None
        ]
        if not candidates:
            return
        session = self._session_factory(workspace_id)
        try:
            rows = session.execute(
                select(ChapterORM.chapter_id).where(
                    ChapterORM.workspace_id == workspace_id,
                    ChapterORM.chapter_id.in_(candidates),
                )
            ).scalars().all()
            existing = set(rows)
            missing = [c for c in candidates if c not in existing]
            if missing:
                raise StyleAuditServiceError(
                    f"章节不存在: {', '.join(missing)}"
                )
        finally:
            session.close()

    def _resolve_style_template(self, workspace_id: str) -> str | None:
        """读 ``meta.json`` 的 ``Workspace.style_template``(v1.1 w4-s5 / I2.2)。

        ``None`` / 读失败 → 返 ``None``(StyleAuditor 短路返 ok)。
        """
        try:
            paths = WorkspacePaths(
                workspace_id,
                workspaces_root=self._workspace_service.workspaces_root,
            )
            return read_meta(paths.meta_json).style_template
        except Exception:  # noqa: BLE001 — swallow per "辅助任务失败兜底"先例
            return None

    def _read_chapter_excerpts(
        self,
        workspace_id: str,
        *,
        chapter_range_start: str | None,
        chapter_range_end: str | None,
    ) -> list[str]:
        """预读范围内章节 final 正文片段(供 StyleAuditor 判模板偏移)。

        范围语义(``chapter_id`` 为零填充 ``ch-NNNN``,字典序 = 章序):
        - 给定 ``chapter_range_start`` / ``_end`` → 取 ``chapter_id`` 落在
          ``[start, end]`` 闭区间的章节(任一缺失则该端不设界);
        - 都为 None → 取全工作区章节(整体审计)。

        一律按 ``chapter_id`` 升序、上限 ``_MAX_AUDIT_CHAPTERS`` 章(控 token);
        只取有版本正文(``current_version >= 1``)的章节。读正文失败的章节静默
        跳过(辅助任务,不阻断审计)。
        """
        paths = WorkspacePaths(
            workspace_id,
            workspaces_root=self._workspace_service.workspaces_root,
        )
        session = self._session_factory(workspace_id)
        try:
            rows = session.execute(
                select(
                    ChapterORM.chapter_id, ChapterORM.current_version
                ).where(
                    ChapterORM.workspace_id == workspace_id,
                    ChapterORM.current_version >= 1,
                ).order_by(ChapterORM.chapter_id)
            ).all()
        finally:
            session.close()

        selected: list[tuple[str, int]] = []
        for chapter_id, current_version in rows:
            if chapter_range_start is not None and chapter_id < chapter_range_start:
                continue
            if chapter_range_end is not None and chapter_id > chapter_range_end:
                continue
            selected.append((chapter_id, current_version))
            if len(selected) >= _MAX_AUDIT_CHAPTERS:
                break

        versioning = ChapterVersioning(paths)
        excerpts: list[str] = []
        for chapter_id, current_version in selected:
            try:
                content = versioning.read_version_content(
                    chapter_id, current_version
                )
            except ChapterVersioningError:
                continue
            if content and content.strip():
                excerpts.append(content)
        return excerpts


# =============================================================================
# 内部工具函数 + persist_*_safely(模块级,便于单测直接调用)
# =============================================================================


def _build_style_audit_entry(
    report: StyleAuditReport,
    *,
    chapter_range_start: str | None,
    chapter_range_end: str | None,
) -> StyleAuditEntry:
    """``StyleAuditReport`` → ``StyleAuditEntry`` Pydantic 转换。

    生成 ``audit_id``(``sty-<uuid12>``,对齐 ``MemoryHealthCheck.check_id``
    ``mhc-<uuid12>`` 风格)+ ``audited_at`` UTC now + ``report_json``
    序列化(stub 阶段含 summary + model_used 占位)。
    """
    return StyleAuditEntry(
        audit_id=f"sty-{uuid.uuid4().hex[:12]}",
        audited_at=datetime.now(UTC),
        chapter_range_start=chapter_range_start,
        chapter_range_end=chapter_range_end,
        drift_score=report.drift_score,
        severity=report.severity,
        report_json={
            "summary": report.summary,
            "model_used": report.model_used,
            "dimension_notes": list(report.dimension_notes),
        },
    )


def persist_style_audit_safely(
    session_factory: SessionFactory | None,
    *,
    workspace_id: str,
    entry: StyleAuditEntry,
) -> None:
    """落 ``style_audit_history`` 表,失败 swallow + structlog log。

    ``session_factory=None`` → no-op(测试无 db 路径)。给定时打开短 session、
    插入一行 ``StyleAuditEntryORM``、commit、close。事务边界全在本函数内,
    **不冒泡**——审计结果是辅助任务,落库失败不应阻断 API 响应(对齐 P2-B-19
    / P3-B-07 / P3-B-13 / P4-B-02 / P4-B-03 / P4-B-04 / P4-B-09 七先例,
    本任务是第 7 处 DB session swallow)。

    ``audit_id`` UUID 唯一生成,**不做幂等去重**(每次 ``run_audit`` 触发
    都落一行新的审计快照;``style_audit_history`` 表本就是历史轨迹,重复
    触发应有多行)。

    **``*_safely`` 第 7 处仍不抽公共模块**(P4-B-09 第 6 处判定接力):各
    caller event_name + payload schema 差异显著,抽 helper 需多传
    op_callable + event_name + workspace_id 等多入参,负面收益。P4 后端
    收官 marker;P5+ 第 8 处再判一次。
    """
    if session_factory is None:
        return
    try:
        session = session_factory(workspace_id)
    except Exception as exc:  # noqa: BLE001 — swallow per *_safely 七先例
        _log.error(
            "style_audit_persist_failed",
            workspace_id=workspace_id,
            audit_id=entry.audit_id,
            stage="open_session",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return
    try:
        row = StyleAuditEntryORM(
            audit_id=entry.audit_id,
            audited_at=entry.audited_at,
            chapter_range_start=entry.chapter_range_start,
            chapter_range_end=entry.chapter_range_end,
            drift_score=entry.drift_score,
            severity=entry.severity,
            report_json=dict(entry.report_json),
        )
        session.add(row)
        session.commit()
        _log.info(
            "style_audit_persisted",
            workspace_id=workspace_id,
            audit_id=entry.audit_id,
            severity=entry.severity,
            chapter_range_start=entry.chapter_range_start,
            chapter_range_end=entry.chapter_range_end,
        )
    except Exception as exc:  # noqa: BLE001 — swallow per *_safely 七先例
        session.rollback()
        _log.error(
            "style_audit_persist_failed",
            workspace_id=workspace_id,
            audit_id=entry.audit_id,
            stage="commit",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
    finally:
        session.close()


__all__ = [
    "RouterFactory",
    "SessionFactory",
    "StyleAuditService",
    "StyleAuditServiceError",
    "persist_style_audit_safely",
]
