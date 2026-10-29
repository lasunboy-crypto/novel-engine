"""静态资产 import 路由(P5-B-08)。

对齐 ROADMAP v1.1 §P1 验收清单 1(浏览器创建工作区,导入预制 world /
characters md)+ p5_plan §"P5-B-08" 字面 + 决策 7=C(摊到 NewBookPage UI 流程)。

1 个端点
========

| 方法 | 路径 | 作用 |
|------|------|------|
| POST | ``/workspaces/{workspace_id}/assets:import`` | 上传 world.md / characters.md 落盘 + 解析入库 |

职责边界
========

路由层只做:参数解析 + 工厂构造 Session / VectorStore / WorkspacePaths +
调 service + 包装响应。"落盘 + 调既有 ``import_assets``" 这串协调级业务
集中在 ``services.asset_import.import_assets_from_content`` 顶层函数。

事务与回滚
==========

事务边界本路由内自管(对齐 CLI ``cmd_workspace_import_assets`` 模式):
service 失败时 ``rollback`` + ``raise`` 让全局错误中间件翻译;成功时
``commit`` + 关 session。VectorStore ``add`` 是 IDEMPOTENT(同 ID upsert),
中段 SQLite rollback 后 Chroma 行残留不影响下次重 import(同 ID 覆盖)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import get_workspace_service
from src.api.schemas.assets import AssetsImportIn, AssetsImportOut
from src.api.schemas.common import SuccessEnvelope
from src.cli.commands import make_db_session, make_vector_store
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.asset_import import (
    ensure_workspace_orm,
    import_assets_from_content,
)
from src.services.post_v8_abc_selection_activity import (
    assert_selection_assets_forbidden,
)
from src.services.workspace_service import WorkspaceService

router = APIRouter(prefix="/workspaces", tags=["assets"])


@router.post(
    "/{workspace_id}/assets:import",
    response_model=SuccessEnvelope[AssetsImportOut],
    summary="导入静态资产 markdown",
    description=(
        "上传 world.md / characters.md 内容(任一可选,默认 None),落盘到工作"
        "区 ``data/assets/`` 目录后调既有 ``import_assets`` service 解析入库 "
        "(Characters → SQLite、World facts → Chroma)。事务由路由自管:失败"
        "回滚不留残;成功 commit 后返 ``AssetsImportOut``(``workspace_id`` / "
        "``files_processed`` / ``counts`` 4 类条目数)。工作区不存在 → "
        "``WORKSPACE_INVALID (422)``(``WorkspaceService.info`` 抛 "
        "``MetaFileError`` 翻译)。"
    ),
)
async def import_workspace_assets(
    workspace_id: str,
    body: AssetsImportIn,
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> SuccessEnvelope[AssetsImportOut]:
    assert_selection_assets_forbidden(workspace_id)
    workspace = workspace_service.info(workspace_id)
    paths = WorkspacePaths(
        workspace_id, workspaces_root=workspace_service.workspaces_root
    )

    session = make_db_session(paths)
    try:
        vector_store = make_vector_store(workspace_id, paths)
        ensure_workspace_orm(session, workspace)
        try:
            result = import_assets_from_content(
                workspace_id=workspace_id,
                assets_dir=paths.assets_dir,
                world_md_content=body.world_md_content,
                characters_md_content=body.characters_md_content,
                db_session=session,
                vector_store=vector_store,
            )
        except Exception:
            session.rollback()
            raise
        else:
            session.commit()
    finally:
        session.close()

    return SuccessEnvelope(
        data=AssetsImportOut(
            workspace_id=result.workspace_id,
            files_processed=list(result.files_processed),
            counts=dict(result.counts),
        )
    )
