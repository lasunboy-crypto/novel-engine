"""工作区数据迁移 API 端点集成用例(P6-B-08)。

覆盖路由层 ``GET /workspaces/{id}/version-status`` + ``POST /workspaces/{id}:
migrate`` + OpenAPI 注册 + 3 错误码通过翻译表落地:

| 端点 | 错误码 | 抛出方 |
|------|--------|--------|
| ``:migrate`` 二次确认错 | ``WORKSPACE_MIGRATION_CONFIRM_INVALID`` (422) | service 异常翻译 |
| ``:migrate`` 主版本跨级 | ``WORKSPACE_VERSION_INCOMPATIBLE`` (422) | service 异常翻译 |
| ``:migrate`` 备份失败 | ``WORKSPACE_MIGRATION_BACKUP_FAILED`` (500) | service 异常翻译 |
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.api.deps import (
    get_backup_service,
    get_workspace_service,
    get_workspace_version_service,
)
from src.api.main import create_app
from src.core.db import Base, WorkspaceORM
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.backup_service import BackupService, BackupServiceError
from src.services.workspace_service import WorkspaceService
from src.services.workspace_version_service import (
    WorkspaceVersionService,
    get_current_schema_version,
)
from src.version import ENGINE_VERSION

_PREVIOUS_SCHEMA = "0029_structured_rewrite_constraint_results"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspaces_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir()
    return root


@pytest.fixture()
def archives_root(tmp_path: Path) -> Path:
    root = tmp_path / "archives"
    root.mkdir()
    return root


@pytest.fixture()
def workspace_service(workspaces_root: Path, archives_root: Path) -> WorkspaceService:
    return WorkspaceService(
        workspaces_root=workspaces_root, archives_root=archives_root
    )


@pytest.fixture()
def backup_service(workspace_service: WorkspaceService) -> BackupService:
    return BackupService(workspace_service=workspace_service, ws_manager=None)


@pytest.fixture()
def version_service(
    workspace_service: WorkspaceService, backup_service: BackupService
) -> WorkspaceVersionService:
    return WorkspaceVersionService(
        workspace_service=workspace_service,
        backup_service=backup_service,
        ws_manager=None,
    )


@pytest.fixture()
def client(
    workspace_service: WorkspaceService,
    backup_service: BackupService,
    version_service: WorkspaceVersionService,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.delenv("NOVEL_DB_PATH", raising=False)
    app = create_app()
    app.dependency_overrides[get_workspace_service] = lambda: workspace_service
    app.dependency_overrides[get_backup_service] = lambda: backup_service
    app.dependency_overrides[get_workspace_version_service] = lambda: version_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _create_workspace(client: TestClient) -> str:
    workspace_id = "ws-migrate-api"
    client.post(
        "/api/v1/workspaces",
        json={
            "workspace_id": workspace_id,
            "display_name": "P6-B-08 migrate api test",
            "genre": "scifi",
            "premise": "test premise",
            "total_chapters": 10,
        },
    )
    return workspace_id


def _rewrite_meta(
    workspaces_root: Path,
    workspace_id: str,
    *,
    schema_version: str | None,
    engine_version: str | None = None,
) -> None:
    meta_path = workspaces_root / workspace_id / "meta.json"
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["schema_version"] = schema_version
    if engine_version is not None:
        payload["engine_version"] = engine_version
    meta_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _prepare_previous_schema_database(
    workspace_service: WorkspaceService,
    workspace_id: str,
) -> None:
    """构造产品可迁移的真实 0029/no-stamp 工作区，而非伪造远古 meta。"""

    _rewrite_meta(
        workspace_service.workspaces_root,
        workspace_id,
        schema_version=_PREVIOUS_SCHEMA,
    )
    paths = WorkspacePaths(
        workspace_id,
        workspaces_root=workspace_service.workspaces_root,
    )
    (paths.root / ".runtime_initialization_pending.json").unlink()
    workspace = workspace_service.info(workspace_id)
    engine = create_engine(f"sqlite:///{paths.db_path}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            WorkspaceORM(
                workspace_id=workspace.workspace_id,
                display_name=workspace.display_name,
                genre=workspace.genre,
                premise=workspace.premise,
                created_at=workspace.created_at,
                last_active_at=workspace.last_active_at,
                engine_version=workspace.engine_version,
                status=workspace.status,
                total_chapters=workspace.total_chapters,
                completed_chapters=workspace.completed_chapters,
                config_overrides=workspace.config_overrides,
            )
        )
        session.commit()
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE chapter_operation_leases")
    engine.dispose()


# ---------------------------------------------------------------------------
# happy path:GET /version-status
# ---------------------------------------------------------------------------


class TestVersionStatusApi:
    def test_fresh_workspace_returns_match(self, client: TestClient) -> None:
        workspace_id = _create_workspace(client)
        resp = client.get(f"/api/v1/workspaces/{workspace_id}/version-status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["workspace_id"] == workspace_id
        assert data["engine_version_match"] is True
        assert data["schema_version_match"] is True
        assert data["migration_required"] is False
        assert data["current_engine"] == ENGINE_VERSION
        assert data["current_schema"] == get_current_schema_version()

    def test_old_schema_returns_migration_required(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        workspace_id = _create_workspace(client)
        _rewrite_meta(
            workspaces_root,
            workspace_id,
            schema_version="0008_chapter_invalidated_force_pass",
        )
        resp = client.get(f"/api/v1/workspaces/{workspace_id}/version-status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["schema_version_match"] is False
        assert data["migration_required"] is True
        assert data["workspace_schema"] == "0008_chapter_invalidated_force_pass"


# ---------------------------------------------------------------------------
# happy path:POST :migrate
# ---------------------------------------------------------------------------


class TestMigrateApi:
    def test_migrate_main_path(
        self,
        client: TestClient,
        workspace_service: WorkspaceService,
    ) -> None:
        workspace_id = _create_workspace(client)
        _prepare_previous_schema_database(workspace_service, workspace_id)

        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}:migrate",
            json={"confirm": workspace_id},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["workspace_id"] == workspace_id
        assert data["from_schema_version"] == _PREVIOUS_SCHEMA
        assert data["to_schema_version"] == get_current_schema_version()
        assert data["pre_migration_backup_id"].startswith("manual_")

        # 再查 version-status 已 match
        resp2 = client.get(f"/api/v1/workspaces/{workspace_id}/version-status")
        assert resp2.json()["data"]["migration_required"] is False


# ---------------------------------------------------------------------------
# error path:3 错误码
# ---------------------------------------------------------------------------


class TestMigrateErrorCodes:
    def test_migrate_confirm_invalid_returns_422(self, client: TestClient) -> None:
        workspace_id = _create_workspace(client)
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}:migrate",
            json={"confirm": "wrong-id"},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["code"] == "WORKSPACE_MIGRATION_CONFIRM_INVALID"
        assert body["error"]["details"]["workspace_id"] == workspace_id

    def test_migrate_version_incompatible_returns_422(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        workspace_id = _create_workspace(client)
        # 模拟跨大版本 workspace(engine_version 主版本号 != current)
        _rewrite_meta(
            workspaces_root,
            workspace_id,
            schema_version="0008_chapter_invalidated_force_pass",
            engine_version="2.0.0",
        )
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}:migrate",
            json={"confirm": workspace_id},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["code"] == "WORKSPACE_VERSION_INCOMPATIBLE"
        details = body["error"]["details"]
        assert details["workspace_engine_version"] == "2.0.0"
        assert details["current_engine_version"] == ENGINE_VERSION

    def test_migrate_backup_failure_returns_500(
        self,
        workspace_service: WorkspaceService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """直接用 app + 注入坏 BackupService,触发 WORKSPACE_MIGRATION_BACKUP_FAILED。"""
        monkeypatch.delenv("NOVEL_DB_PATH", raising=False)

        class _BrokenBackupService:
            def create_migration_backup(
                self,
                workspace_id: str,  # noqa: ARG002
                *,
                source_schema_version: str | None,  # noqa: ARG002
                source_engine_version: str,  # noqa: ARG002
            ) -> Any:
                raise BackupServiceError("disk full simulation")

        broken_version_service = WorkspaceVersionService(
            workspace_service=workspace_service,
            backup_service=_BrokenBackupService(),  # type: ignore[arg-type]
            ws_manager=None,
        )

        app = create_app()
        app.dependency_overrides[get_workspace_service] = lambda: workspace_service
        app.dependency_overrides[get_workspace_version_service] = lambda: (
            broken_version_service
        )

        with TestClient(app) as c:
            # 创建工作区
            c.post(
                "/api/v1/workspaces",
                json={
                    "workspace_id": "ws-backup-fail",
                    "display_name": "P6-B-08 backup fail test",
                    "genre": "scifi",
                    "premise": "test premise",
                    "total_chapters": 0,
                },
            )
            # 构造精确 0029/no-stamp 产品库，让 migrate 合法走到备份步骤。
            _prepare_previous_schema_database(
                workspace_service,
                "ws-backup-fail",
            )
            resp = c.post(
                "/api/v1/workspaces/ws-backup-fail:migrate",
                json={"confirm": "ws-backup-fail"},
            )

        app.dependency_overrides.clear()

        assert resp.status_code == 500
        body = resp.json()
        assert body["error"]["code"] == "WORKSPACE_MIGRATION_BACKUP_FAILED"
        assert body["error"]["details"]["workspace_id"] == "ws-backup-fail"
        assert "disk full" in body["error"]["details"]["reason"]

    def test_migrate_confirm_missing_returns_validation_failed(
        self, client: TestClient
    ) -> None:
        workspace_id = _create_workspace(client)
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}:migrate",
            json={},  # confirm 缺失
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


# ---------------------------------------------------------------------------
# OpenAPI 注册
# ---------------------------------------------------------------------------


class TestOpenAPI:
    def test_version_status_endpoint_in_openapi(self) -> None:
        app = create_app()
        spec = app.openapi()
        assert "/api/v1/workspaces/{workspace_id}/version-status" in spec["paths"]
        assert "VersionStatusOut" in spec["components"]["schemas"]

    def test_migrate_endpoint_in_openapi(self) -> None:
        app = create_app()
        spec = app.openapi()
        assert "/api/v1/workspaces/{workspace_id}:migrate" in spec["paths"]
        assert "MigrateIn" in spec["components"]["schemas"]
        assert "MigrateResultOut" in spec["components"]["schemas"]
