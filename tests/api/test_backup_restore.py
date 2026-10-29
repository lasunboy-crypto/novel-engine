"""备份 / 恢复 API 端点集成用例(P6-B-07)。

补 ``test_workspaces_routes.py`` 已覆盖的 happy / confirm-mismatch / not-found
之外的 2 个错误码 + GET ``/backups`` 列表 + 自动备份 hook 集成。

错误码覆盖
==========

| 端点 | 错误码 | 抛出方 |
|------|--------|--------|
| ``:restore`` 备份不存在 | ``BACKUP_NOT_FOUND`` (404) | service 异常翻译 |
| ``:restore`` 二次确认错 | ``RESTORE_CONFIRM_MISMATCH`` (422) | 路由前置校验 |
| ``:restore`` 版本不兼容 | ``RESTORE_VERSION_INCOMPATIBLE`` (422) | service 异常翻译 |
| ``:restore`` zip 损坏 | ``BACKUP_INVALID_FORMAT`` (422) | service 异常翻译 |
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.deps import get_backup_service, get_workspace_service
from src.api.main import create_app
from src.services.backup_service import BackupService
from src.services.workspace_service import WorkspaceService


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
def workspace_service(
    workspaces_root: Path, archives_root: Path
) -> WorkspaceService:
    return WorkspaceService(
        workspaces_root=workspaces_root, archives_root=archives_root
    )


@pytest.fixture()
def backup_service(workspace_service: WorkspaceService) -> BackupService:
    # ws_manager=None — 测试断言不依赖 WS,publish noop;BackupService 单测
    # 已覆盖 publish 行为
    return BackupService(workspace_service=workspace_service, ws_manager=None)


@pytest.fixture()
def client(
    workspace_service: WorkspaceService, backup_service: BackupService
) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_workspace_service] = lambda: workspace_service
    app.dependency_overrides[get_backup_service] = lambda: backup_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _create_workspace(client: TestClient) -> str:
    workspace_id = "ws_backup_api"
    client.post(
        "/api/v1/workspaces",
        json={
            "workspace_id": workspace_id,
            "display_name": "P6-B-07 backup api test",
            "genre": "scifi",
            "premise": "test premise",
            "total_chapters": 10,
        },
    )
    return workspace_id


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestBackupApi:
    def test_manual_backup_returns_backup_info(self, client: TestClient) -> None:
        workspace_id = _create_workspace(client)
        resp = client.post(f"/api/v1/workspaces/{workspace_id}:backup")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["backup_type"] == "manual"
        assert data["workspace_id"] == workspace_id
        assert data["backup_id"].startswith("manual_")
        assert data["file_path"].endswith(".zip")
        assert data["chapter_id"] is None
        assert "schema_version" in data
        assert data["size_bytes"] > 0


class TestListBackupsApi:
    def test_list_returns_empty_for_fresh_workspace(
        self, client: TestClient
    ) -> None:
        workspace_id = _create_workspace(client)
        resp = client.get(f"/api/v1/workspaces/{workspace_id}/backups")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["workspace_id"] == workspace_id
        assert data["backups"] == []

    def test_list_returns_backups_after_create(self, client: TestClient) -> None:
        """list 返手动备份(单份,避开同秒 backup_id 覆盖 — backup_id 紧凑
        时间戳粒度为秒,生产中调用方需节流;详 P1-B-14 backup 同秒注释)。"""
        workspace_id = _create_workspace(client)
        client.post(f"/api/v1/workspaces/{workspace_id}:backup")
        resp = client.get(f"/api/v1/workspaces/{workspace_id}/backups")
        assert resp.status_code == 200
        backups = resp.json()["data"]["backups"]
        assert len(backups) == 1
        assert backups[0]["backup_type"] == "manual"
        assert backups[0]["workspace_id"] == workspace_id


class TestPurgeBackupsApi:
    """``POST /{id}:purge-backups`` 集成(v1.1 w3-s8 F1)。"""

    def test_purge_deletes_backups_and_returns_count(
        self, client: TestClient
    ) -> None:
        """建一份手动备份 → purge 确认 → 删除 1 份 + list 回空。"""
        workspace_id = _create_workspace(client)
        client.post(f"/api/v1/workspaces/{workspace_id}:backup")

        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}:purge-backups",
            json={"confirm": workspace_id},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["workspace_id"] == workspace_id
        assert data["deleted_count"] == 1
        assert data["freed_bytes"] > 0
        # list 回空
        backups = client.get(
            f"/api/v1/workspaces/{workspace_id}/backups"
        ).json()["data"]["backups"]
        assert backups == []

    def test_purge_confirm_mismatch_returns_422(
        self, client: TestClient
    ) -> None:
        """confirm != workspace_id → 422 PURGE_BACKUPS_CONFIRM_MISMATCH,不删。"""
        workspace_id = _create_workspace(client)
        client.post(f"/api/v1/workspaces/{workspace_id}:backup")

        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}:purge-backups",
            json={"confirm": "wrong"},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "PURGE_BACKUPS_CONFIRM_MISMATCH"
        # 备份仍在
        backups = client.get(
            f"/api/v1/workspaces/{workspace_id}/backups"
        ).json()["data"]["backups"]
        assert len(backups) == 1

    def test_purge_empty_workspace_returns_zero(
        self, client: TestClient
    ) -> None:
        """无备份工作区 purge → 200 + deleted_count=0。"""
        workspace_id = _create_workspace(client)
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}:purge-backups",
            json={"confirm": workspace_id},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["deleted_count"] == 0


# ---------------------------------------------------------------------------
# Error paths(3 新错误码 + restore confirm mismatch)
# ---------------------------------------------------------------------------


class TestErrorCodes:
    def test_restore_backup_not_found_returns_404(
        self, client: TestClient
    ) -> None:
        workspace_id = _create_workspace(client)
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}:restore",
            json={
                "backup_id": "manual_19990101T000000Z",
                "confirm": workspace_id,
            },
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "BACKUP_NOT_FOUND"
        assert body["error"]["details"]["workspace_id"] == workspace_id

    def test_restore_invalid_zip_format_returns_422(
        self,
        client: TestClient,
        workspaces_root: Path,
    ) -> None:
        workspace_id = _create_workspace(client)
        # 投放一个非合法 zip 到 backups/
        backups_dir = workspaces_root / workspace_id / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        backup_id = "manual_20260101T000000Z"
        (backups_dir / f"{backup_id}.zip").write_bytes(b"corrupted content")

        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}:restore",
            json={"backup_id": backup_id, "confirm": workspace_id},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "BACKUP_INVALID_FORMAT"

    def test_restore_version_incompatible_returns_422(
        self,
        client: TestClient,
        workspaces_root: Path,
    ) -> None:
        workspace_id = _create_workspace(client)
        backups_dir = workspaces_root / workspace_id / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        backup_id = "manual_20260101T000000Z"
        with zipfile.ZipFile(backups_dir / f"{backup_id}.zip", "w") as zf:
            zf.writestr(
                "backup_meta.json",
                json.dumps(
                    {
                        "backup_id": backup_id,
                        "backup_type": "manual",
                        "workspace_id": workspace_id,
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "chapter_id": None,
                        "schema_version": "9999_future_schema",
                        "engine_version": "0.1.0",
                        "contents": [],
                    }
                ),
            )

        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}:restore",
            json={"backup_id": backup_id, "confirm": workspace_id},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["code"] == "RESTORE_VERSION_INCOMPATIBLE"
        details = body["error"]["details"]
        assert details["reason"] == "backup_newer"
        assert details["needs_engine_upgrade"] is True

    def test_restore_confirm_missing_returns_validation_failed(
        self, client: TestClient
    ) -> None:
        workspace_id = _create_workspace(client)
        resp = client.post(
            f"/api/v1/workspaces/{workspace_id}:restore",
            json={"backup_id": "manual_x"},  # confirm 缺失
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"
