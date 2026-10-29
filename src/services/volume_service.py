"""Volume CRUD + apply-template service(P5-B-09)。

对齐 plan §"P5-B-09" 字面 + FRONTEND_SPEC §4.6 字面 + P5-B-01 ``Volume``
Pydantic 模型 + P5-B-07 ``WorkspaceORM.outline_status`` 字段。

职责
====

- ``list_volumes(workspace_id)``:列工作区所有卷,按 ``volume_id`` 升序。
- ``get_volume(workspace_id, volume_id)``:读单卷;不存在 → ``VolumeNotFoundError``。
- ``update_volume(workspace_id, volume_id, patch)``:patch 字段 + 用 ``Volume.model_validate``
  整体校验 → 落 ORM。**不变式**(``tension_curve`` 长度 == ``chapter_count`` /
  arc 区间合法 / arc_id 唯一)patch 后必须仍成立。
- ``apply_template(workspace_id, volume_id, template_name)``:根据模板字典
  填充 ``tension_curve_json`` + 同步 ``tension_template`` 字段;**不动
  ``arcs_json``**(对齐 plan §P5-B-09 字面"根据模板字典填充 tension_curve_json"
  + FRONTEND_SPEC §4.6 字面"曲线模板下拉,可一键应用模板")。
- ``get_outline_status(workspace_id)``:读 ``WorkspaceORM.outline_status``
  (P5-B-07 3 档);ORM 行不存在 → 返 ``"not_started"`` 字面默认。

事务模式
========

``update_volume`` / ``apply_template`` 走"开 session → 改 ORM → commit /
rollback → close" 标准模式(对齐 ``OutlineService.commit_outline``)。
``list_volumes`` / ``get_volume`` / ``get_outline_status`` 只读,不写
session。

异常
====

- ``VolumeNotFoundError``:卷不存在或不属于本工作区(跨工作区视为不存在;
  对齐 P2 ``CharacterNotFoundError`` 模式)。
- ``VolumeUpdateInvalidError``:``Volume.model_validate`` 抛 ``ValidationError``
  时 wrap;翻译为 ``WORKSPACE_INVALID``(422,通用规格源校验失败语义,
  暂复用既有错误码不再增 ``VolumeUpdateInvalid 422`` — 待 P6 收口时观察)。
  **本任务自决**:校验失败本身已带详细字段错误,沿用 ``WORKSPACE_INVALID``
  与 outline 模块同源。

  Code 自决项 — `_translate_service_error` 入口翻译时返 ``WorkspaceInvalid``
  (422) — 对齐既有 outline / asset_import 模式;不增独立错误码。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy.orm import Session

from src.core.db.models import VolumeORM, WorkspaceORM
from src.core.models.tension_curve import apply_template as _apply_template_values
from src.core.models.volume import (
    TensionCurvePoint,
    TensionTemplateName,
    Volume,
)
from src.services.workspace_service import WorkspaceService

SessionFactory = Callable[[str], Session]

_log = structlog.get_logger(__name__)


# =============================================================================
# 异常
# =============================================================================


class VolumeServiceError(Exception):
    """Volume service 通用异常基类。"""


class VolumeNotFoundError(VolumeServiceError):
    """卷不存在或不属于本工作区。

    跨工作区视为不存在(``row.workspace_id != workspace_id`` 等价 404,
    对齐 ``CharacterNotFoundError`` / ``PlotThreadNotFoundError`` 模式)。
    """

    def __init__(self, *, volume_id: str, workspace_id: str) -> None:
        super().__init__(
            f"Volume {volume_id!r} not found in workspace {workspace_id!r}"
        )
        self.volume_id = volume_id
        self.workspace_id = workspace_id


class VolumeUpdateInvalidError(VolumeServiceError):
    """``update_volume`` patch 后整体不变式校验失败(``Volume.model_validate``
    抛 ``ValidationError``)。

    例:patch 改 ``chapter_count = 12`` 但 ``tension_curve`` 长度仍 10 →
    Volume 模型 ``_validate_tension_curve_length`` 抛 → 翻译此异常。
    路由层翻译为 ``WORKSPACE_INVALID``(422)。
    """

    def __init__(self, message: str, *, errors: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.errors = errors


# =============================================================================
# Service
# =============================================================================


class VolumeService:
    """Volume CRUD + apply-template + outline_status 读取。

    依赖 ``WorkspaceService``(校验工作区存在 — ``info`` 抛 ``MetaFileError``
    被全局翻译为 ``WORKSPACE_INVALID 422``)+ 可选 ``session_factory``
    (``None`` 时所有方法走文件外语义 — list 返空 / get 抛 NotFound)。

    P5-B-09 暂未需要 LLM client / WS manager(纯 CRUD + 字典查表),
    P6+ 若加"卷历史版本"等异步任务可扩。
    """

    def __init__(
        self,
        *,
        workspace_service: WorkspaceService,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._workspace_service = workspace_service
        self._session_factory = session_factory

    # ---- 公共方法 -----------------------------------------------------------

    def list_volumes(self, workspace_id: str) -> list[Volume]:
        """列工作区所有卷,按 ``volume_id`` 升序(确定性顺序便于 UI 渲染)。

        工作区不存在 → ``MetaFileError``(全局翻译为 ``WORKSPACE_INVALID``)。
        ``session_factory=None`` → 返空列表(单测纯文件路径)。
        """
        self._workspace_service.info(workspace_id)
        if self._session_factory is None:
            return []
        session = self._session_factory(workspace_id)
        try:
            rows = (
                session.query(VolumeORM)
                .filter(VolumeORM.workspace_id == workspace_id)
                .order_by(VolumeORM.volume_id)
                .all()
            )
            return [_orm_to_pydantic(row) for row in rows]
        finally:
            session.close()

    def get_volume(self, workspace_id: str, volume_id: str) -> Volume:
        """读单卷;不存在或跨工作区 → ``VolumeNotFoundError``。"""
        self._workspace_service.info(workspace_id)
        if self._session_factory is None:
            raise VolumeNotFoundError(
                volume_id=volume_id, workspace_id=workspace_id
            )
        session = self._session_factory(workspace_id)
        try:
            row = session.get(VolumeORM, volume_id)
            if row is None or row.workspace_id != workspace_id:
                raise VolumeNotFoundError(
                    volume_id=volume_id, workspace_id=workspace_id
                )
            return _orm_to_pydantic(row)
        finally:
            session.close()

    def update_volume(
        self,
        workspace_id: str,
        volume_id: str,
        patch: dict[str, Any],
    ) -> Volume:
        """patch 字段 + ``Volume.model_validate`` 整体校验 → 落 ORM。

        ``patch`` 仅含非 None 字段(路由层 ``model_dump(exclude_unset=True)``
        过滤);校验失败 → ``VolumeUpdateInvalidError``(翻译 422)。

        **副作用**:成功时 ``last_updated_at`` 翻新 + structlog
        ``volume_updated`` 事件。
        """
        self._workspace_service.info(workspace_id)
        if self._session_factory is None:
            raise VolumeNotFoundError(
                volume_id=volume_id, workspace_id=workspace_id
            )
        session = self._session_factory(workspace_id)
        try:
            row = session.get(VolumeORM, volume_id)
            if row is None or row.workspace_id != workspace_id:
                raise VolumeNotFoundError(
                    volume_id=volume_id, workspace_id=workspace_id
                )

            now = datetime.now(UTC)
            merged = _orm_to_pydantic_dict(row)
            for key, value in patch.items():
                if value is None:
                    continue
                merged[key] = value
            merged["last_updated_at"] = now

            try:
                new_volume = Volume.model_validate(merged)
            except ValidationError as exc:
                # ``include_context=False`` 防 ctx 含 ValueError 实例不可
                # JSON 序列化(我们走 details JSON 输出);其他字段仍保留
                # (loc / msg / type / input)足够定位失败字段。
                raise VolumeUpdateInvalidError(
                    f"Volume update validation failed for {volume_id!r}: {exc}",
                    errors=exc.errors(include_context=False, include_url=False),
                ) from exc

            row.title = new_volume.title
            row.summary = new_volume.summary
            row.chapter_count = new_volume.chapter_count
            row.tension_template = new_volume.tension_template
            row.arcs_json = [arc.model_dump(mode="json") for arc in new_volume.arcs]
            row.tension_curve_json = [
                pt.model_dump(mode="json") for pt in new_volume.tension_curve
            ]
            row.last_updated_at = now
            session.commit()
            session.refresh(row)
            _log.info(
                "volume_updated",
                workspace_id=workspace_id,
                volume_id=volume_id,
                fields_patched=sorted(patch.keys()),
            )
            return _orm_to_pydantic(row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def apply_template(
        self,
        workspace_id: str,
        volume_id: str,
        template_name: TensionTemplateName,
    ) -> tuple[Volume, list[TensionCurvePoint]]:
        """填充 ``tension_curve_json`` + 同步 ``tension_template`` 字段。

        **不动 ``arcs_json``**(plan §P5-B-09 字面 + FRONTEND_SPEC §4.6
        字面)— ``is_arc_finale`` 标志位通过既有 ``arcs_json`` 末位章号
        计算,arcs 列表本身保持 patch 前原值。

        返回 ``(完整 Volume, 新 tension_curve)`` 二元组,路由层既可回显
        全卷也可只回显曲线(本任务路由层只回显 ``ApplyTemplateOut``
        含曲线 — 客户端按需另调 ``GET /volumes/{vid}`` 拿全卷)。
        """
        self._workspace_service.info(workspace_id)
        if self._session_factory is None:
            raise VolumeNotFoundError(
                volume_id=volume_id, workspace_id=workspace_id
            )
        session = self._session_factory(workspace_id)
        try:
            row = session.get(VolumeORM, volume_id)
            if row is None or row.workspace_id != workspace_id:
                raise VolumeNotFoundError(
                    volume_id=volume_id, workspace_id=workspace_id
                )

            tension_values = _apply_template_values(
                template_name, row.chapter_count
            )
            arc_finale_chapters = {
                tuple(arc.get("chapter_range", ()))[1]
                for arc in (row.arcs_json or [])
                if arc.get("chapter_range")
                and len(arc.get("chapter_range", ())) == 2
            }
            curve_payload = [
                {
                    "chapter_number": i + 1,
                    "tension": tension_values[i],
                    "is_arc_finale": (i + 1) in arc_finale_chapters,
                }
                for i in range(len(tension_values))
            ]
            row.tension_curve_json = curve_payload
            row.tension_template = template_name
            row.last_updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(row)
            _log.info(
                "volume_template_applied",
                workspace_id=workspace_id,
                volume_id=volume_id,
                template=template_name,
                chapter_count=row.chapter_count,
            )
            volume = _orm_to_pydantic(row)
            return volume, volume.tension_curve
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_outline_status(self, workspace_id: str) -> str:
        """读 ``WorkspaceORM.outline_status``(P5-B-07 3 档)。

        ORM 行不存在(P2/P4 老工作区 alembic 0007 升级前默认 ``not_started``;
        当前测试 fixture 也可能因未触发 ensure_workspace_orm 而无行)→ 返
        ``"not_started"`` 字面默认,语义对齐 alembic 0007 字面默认值。

        本端点为 P5-F-02 NewBookPage 量身定制(plan §P5-B-09 ④ + P5-B-07
        §6 marker + P5-B-08 §6 marker)。
        """
        self._workspace_service.info(workspace_id)
        if self._session_factory is None:
            return "not_started"
        session = self._session_factory(workspace_id)
        try:
            row = session.get(WorkspaceORM, workspace_id)
            if row is None:
                return "not_started"
            return row.outline_status
        finally:
            session.close()


# =============================================================================
# 内部辅助
# =============================================================================


def _orm_to_pydantic(row: VolumeORM) -> Volume:
    """``VolumeORM`` → ``Volume`` Pydantic(用 ``model_validate`` 走完整校验)。"""
    return Volume.model_validate(_orm_to_pydantic_dict(row))


def _orm_to_pydantic_dict(row: VolumeORM) -> dict[str, Any]:
    """``VolumeORM`` → 适合 ``Volume.model_validate`` 的 dict。

    SQLAlchemy ``JSON`` 列已是 Python list/dict;Pydantic 会自动把 inner
    list[dict] 校验成 list[Arc] / list[TensionCurvePoint]。
    """
    return {
        "volume_id": row.volume_id,
        "workspace_id": row.workspace_id,
        "title": row.title,
        "summary": row.summary,
        "chapter_count": row.chapter_count,
        "tension_template": row.tension_template,
        "arcs": list(row.arcs_json or []),
        "tension_curve": list(row.tension_curve_json or []),
        "created_at": row.created_at,
        "last_updated_at": row.last_updated_at,
    }


def volume_to_out_dict(volume: Volume) -> dict[str, Any]:
    """``Volume`` Pydantic → ``VolumeOut`` 兼容 dict(给路由层用 — 同字段)。

    本服务不依赖 ``api/schemas/volumes.py`` 避免反向依赖;路由层调本函数
    把 service 产出的 Volume dump 后构造 ``VolumeOut(**d)``。
    """
    return {
        "volume_id": volume.volume_id,
        "workspace_id": volume.workspace_id,
        "title": volume.title,
        "summary": volume.summary,
        "chapter_count": volume.chapter_count,
        "tension_template": volume.tension_template,
        "arcs": [arc.model_dump(mode="python") for arc in volume.arcs],
        "tension_curve": [
            pt.model_dump(mode="python") for pt in volume.tension_curve
        ],
        "created_at": volume.created_at,
        "last_updated_at": volume.last_updated_at,
    }
