"""工作区路由集成用例(P1-B-18)。

覆盖 9 个端点的"主路径 + 错误路径",并验证 P1-B-17 全局异常处理器与本任务
新增的"路由前置校验"两条轨道协同工作。

测试基础设施
============

- 用 ``create_app()`` 工厂(P1-B-17 暴露)拿到独立 app,**不**新写一份
  app——保证中间件 / handler 栈与生产一致。
- 通过 ``app.dependency_overrides[get_workspace_service]`` 把 service 实例
  替换成指向 ``tmp_path`` 的版本,避免污染本机 ``settings.data_dir``。
- 每条用例独立 fixture(``tmp_path`` 自带隔离),不共享 service 状态。

错误码覆盖
==========

| 端点 | 错误码 | 抛出方 |
|------|--------|--------|
| ``:purge`` confirm 不匹配 | ``PURGE_CONFIRM_MISMATCH`` (422) | 路由前置校验 |
| ``:archive`` 已归档 | ``WORKSPACE_ALREADY_ARCHIVED`` (409) | 路由前置守卫 |
| ``:restore`` 归档不存在 | ``ARCHIVE_NOT_FOUND`` (404) | 路由前置守卫 |
| ``:clean`` 不存在的工作区 | ``WORKSPACE_CONFLICT`` (409) | service 异常翻译 |
| ``GET /{id}`` 不存在 | ``WORKSPACE_INVALID`` (422) | service 异常翻译 |
| ``POST`` 重复创建 | ``WORKSPACE_CONFLICT`` (409) | service 异常翻译 |
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.deps import get_workspace_service
from src.api.main import create_app
from src.core.workspace.meta import write_meta
from src.services.workspace_service import WorkspaceService


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture()
def workspaces_root(tmp_path: Path) -> Path:
    return tmp_path / "workspaces"


@pytest.fixture()
def archives_root(tmp_path: Path) -> Path:
    return tmp_path / "archives"


@pytest.fixture()
def workspace_service(
    workspaces_root: Path, archives_root: Path
) -> WorkspaceService:
    return WorkspaceService(
        workspaces_root=workspaces_root, archives_root=archives_root
    )


@pytest.fixture()
def client(workspace_service: WorkspaceService) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_workspace_service] = lambda: workspace_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _create_payload(**overrides: object) -> dict[str, object]:
    """构造一个最小合法 ``WorkspaceCreateIn`` payload。"""

    base: dict[str, object] = {
        "workspace_id": "scifi_book_1",
        "display_name": "科幻试验册",
        "genre": "scifi",
        "premise": "人类首次接触外星文明的副作用。",
        "total_chapters": 100,
    }
    base.update(overrides)
    return base


# =============================================================================
# CRUD
# =============================================================================


class TestCreate:
    def test_create_returns_201_and_workspace(self, client: TestClient) -> None:
        resp = client.post("/api/v1/workspaces", json=_create_payload())
        assert resp.status_code == 201
        body = resp.json()
        assert "data" in body and "error" not in body
        assert body["data"]["workspace_id"] == "scifi_book_1"
        assert body["data"]["status"] == "active"
        assert body["data"]["display_name"] == "科幻试验册"

    def test_create_then_get_allows_lifecycle_log_before_first_database(
        self,
        client: TestClient,
        workspaces_root: Path,
    ) -> None:
        workspace_id = "scifi_book_1"
        resp = client.post("/api/v1/workspaces", json=_create_payload())
        assert resp.status_code == 201
        workspace_root = workspaces_root / workspace_id
        (workspace_root / "logs" / "creation.jsonl").write_text(
            '{"event":"workspace_lifecycle"}\n',
            encoding="utf-8",
        )

        detail = client.get(f"/api/v1/workspaces/{workspace_id}")

        assert detail.status_code == 200
        assert detail.json()["data"]["workspace_id"] == workspace_id
        assert not (workspace_root / ".runtime_initialization_pending.json").exists()

    def test_create_default_voice_passthrough(self, client: TestClient) -> None:
        """L2(wave 3 s4):default_voice 经路由透传落 service → 响应回显。"""
        resp = client.post(
            "/api/v1/workspaces",
            json=_create_payload(default_voice="free"),
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["default_voice"] == "free"

    def test_create_default_voice_omitted_defaults_third_person(
        self, client: TestClient
    ) -> None:
        """L2:不传 default_voice → 默认 third_person(项目级硬约束)。"""
        resp = client.post("/api/v1/workspaces", json=_create_payload())
        assert resp.status_code == 201
        assert resp.json()["data"]["default_voice"] == "third_person"

    def test_create_default_voice_invalid_returns_422(
        self, client: TestClient
    ) -> None:
        """L2:非枚举值被 Pydantic Literal 拦下 → 422。"""
        resp = client.post(
            "/api/v1/workspaces",
            json=_create_payload(default_voice="omniscient"),
        )
        assert resp.status_code == 422

    def test_create_default_activates_workspace(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        client.post("/api/v1/workspaces", json=_create_payload())
        active_link = workspaces_root / "_active"
        assert active_link.is_symlink()

    def test_create_with_activate_false_no_symlink(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        resp = client.post(
            "/api/v1/workspaces", json=_create_payload(activate=False)
        )
        assert resp.status_code == 201
        active_link = workspaces_root / "_active"
        assert not active_link.exists() and not active_link.is_symlink()

    def test_create_duplicate_returns_workspace_conflict(
        self, client: TestClient
    ) -> None:
        client.post("/api/v1/workspaces", json=_create_payload())
        resp = client.post("/api/v1/workspaces", json=_create_payload())
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"]["code"] == "WORKSPACE_CONFLICT"
        assert "request_id" in body["error"]

    def test_create_missing_field_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/workspaces",
            json={"workspace_id": "x"},  # 缺 display_name 等
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"

    @pytest.mark.parametrize(
        "bad_id",
        [
            "../foo",  # path traversal
            "a/b",  # 路径分隔符
            "..",
            "ws demo",
            "ws@demo",
        ],
    )
    def test_create_invalid_workspace_id_charset_returns_422(
        self, client: TestClient, bad_id: str
    ) -> None:
        """P2-B-05:API 层拒绝包含路径分隔符 / 空格 / 特殊符号的 workspace_id。

        无需到达 service / 文件系统;Pydantic schema 层即触发 422。
        """

        resp = client.post(
            "/api/v1/workspaces", json=_create_payload(workspace_id=bad_id)
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


class TestList:
    def test_list_empty_returns_empty_array(self, client: TestClient) -> None:
        resp = client.get("/api/v1/workspaces")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    def test_list_returns_created_workspaces(self, client: TestClient) -> None:
        client.post("/api/v1/workspaces", json=_create_payload())
        client.post(
            "/api/v1/workspaces",
            json=_create_payload(workspace_id="fantasy_book", genre="fantasy"),
        )
        resp = client.get("/api/v1/workspaces")
        assert resp.status_code == 200
        ids = sorted(w["workspace_id"] for w in resp.json()["data"])
        assert ids == ["fantasy_book", "scifi_book_1"]


class TestInfo:
    def test_info_returns_workspace(self, client: TestClient) -> None:
        client.post("/api/v1/workspaces", json=_create_payload())
        resp = client.get("/api/v1/workspaces/scifi_book_1")
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert body["workspace_id"] == "scifi_book_1"
        # wave 6 s1+s4:GET /workspaces/{id} 响应含 outline_status API 表面字段,
        # 新建工作区无 WorkspaceORM 行 → VolumeService.get_outline_status 返
        # alembic 0007 默认 "not_started"。
        assert body["outline_status"] == "not_started"

    def test_info_missing_returns_workspace_invalid(self, client: TestClient) -> None:
        # MetaFileError(meta.json 不存在) → WorkspaceInvalid (422)
        resp = client.get("/api/v1/workspaces/no_such_workspace")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "WORKSPACE_INVALID"

    @pytest.mark.parametrize(
        "orm_value", ["not_started", "generating", "committed"]
    )
    def test_info_outline_status_reflects_volume_service(
        self, client: TestClient, orm_value: str
    ) -> None:
        """wave 6 s1+s4:GET /workspaces/{id} 响应 outline_status 字段透传
        ``VolumeService.get_outline_status`` 返值,前端 OutlinePage 据此渲染
        三档状态分支(未生成 / 已生成未 commit / 已 commit)。
        """
        from src.api.deps import get_volume_service

        class _StubVolumeService:
            def get_outline_status(self, workspace_id: str) -> str:
                return orm_value

        client.app.dependency_overrides[get_volume_service] = (
            lambda: _StubVolumeService()
        )
        client.post("/api/v1/workspaces", json=_create_payload())
        resp = client.get("/api/v1/workspaces/scifi_book_1")
        assert resp.status_code == 200
        assert resp.json()["data"]["outline_status"] == orm_value


# =============================================================================
# 生命周期动作
# =============================================================================


class TestSwitch:
    def test_switch_updates_active_link(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        client.post("/api/v1/workspaces", json=_create_payload(activate=False))
        resp = client.post("/api/v1/workspaces/scifi_book_1:switch")
        assert resp.status_code == 200
        assert (workspaces_root / "_active").is_symlink()

    def test_switch_already_active_is_idempotent(self, client: TestClient) -> None:
        client.post("/api/v1/workspaces", json=_create_payload())
        # 重复 switch 同一个工作区 → 200 幂等
        resp1 = client.post("/api/v1/workspaces/scifi_book_1:switch")
        resp2 = client.post("/api/v1/workspaces/scifi_book_1:switch")
        assert resp1.status_code == 200
        assert resp2.status_code == 200

    def test_switch_missing_returns_workspace_conflict(
        self, client: TestClient
    ) -> None:
        # service.switch(missing) 抛 WorkspaceServiceError → WORKSPACE_CONFLICT
        resp = client.post("/api/v1/workspaces/no_such:switch")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "WORKSPACE_CONFLICT"


class TestArchive:
    def test_archive_main_path(
        self, client: TestClient, archives_root: Path
    ) -> None:
        client.post("/api/v1/workspaces", json=_create_payload())
        resp = client.post("/api/v1/workspaces/scifi_book_1:archive")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["workspace_id"] == "scifi_book_1"
        assert body["data"]["archive_path"].endswith("scifi_book_1.tar.gz")
        assert (archives_root / "scifi_book_1.tar.gz").is_file()

    def test_archive_already_archived_via_status_guard(
        self,
        client: TestClient,
        workspace_service: WorkspaceService,
    ) -> None:
        """直接用 service 造一个 status=archived 的 workspace,验证前置守卫。

        路径(`POST /:archive` 后目录已删,无法在同一目录上重复触发)无法触
        发"已归档"前置守卫;改为手工把现有目录的 ``meta.status`` 写成
        ``archived``,再调路由,验证守卫拦截。
        """
        workspace_service.create(
            "archived_target",
            display_name="x",
            genre="x",
            premise="x",
            activate=False,
        )
        meta_path = (
            workspace_service.workspaces_root / "archived_target" / "meta.json"
        )
        ws = workspace_service.info("archived_target")
        write_meta(meta_path, ws.model_copy(update={"status": "archived"}))

        resp = client.post("/api/v1/workspaces/archived_target:archive")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "WORKSPACE_ALREADY_ARCHIVED"
        assert "archived_target" in resp.json()["error"]["message"]

    def test_archive_missing_workspace_returns_workspace_invalid(
        self, client: TestClient
    ) -> None:
        # 路由前置守卫 service.info(id) 抛 MetaFileError → WORKSPACE_INVALID (422)
        resp = client.post("/api/v1/workspaces/no_such:archive")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "WORKSPACE_INVALID"


class TestClean:
    def test_clean_main_path(self, client: TestClient) -> None:
        client.post("/api/v1/workspaces", json=_create_payload())
        resp = client.post("/api/v1/workspaces/scifi_book_1:clean")
        assert resp.status_code == 200
        assert resp.json()["data"]["workspace_id"] == "scifi_book_1"

    def test_clean_missing_returns_workspace_conflict(
        self, client: TestClient
    ) -> None:
        # service 抛 WorkspaceServiceError("工作区不存在") → WORKSPACE_CONFLICT
        resp = client.post("/api/v1/workspaces/no_such:clean")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "WORKSPACE_CONFLICT"


class TestPurge:
    def test_purge_main_path(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        client.post("/api/v1/workspaces", json=_create_payload())
        resp = client.post(
            "/api/v1/workspaces/scifi_book_1:purge",
            json={"confirm": "scifi_book_1"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["workspace_id"] == "scifi_book_1"
        assert not (workspaces_root / "scifi_book_1").exists()

    def test_purge_confirm_mismatch(self, client: TestClient) -> None:
        client.post("/api/v1/workspaces", json=_create_payload())
        resp = client.post(
            "/api/v1/workspaces/scifi_book_1:purge",
            json={"confirm": "wrong_id"},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["code"] == "PURGE_CONFIRM_MISMATCH"
        assert "scifi_book_1" in body["error"]["message"]

    def test_purge_missing_confirm_field_returns_validation_failed(
        self, client: TestClient
    ) -> None:
        client.post("/api/v1/workspaces", json=_create_payload())
        resp = client.post("/api/v1/workspaces/scifi_book_1:purge", json={})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


class TestDestroy:
    """P7-B-07:``:destroy`` 端点三方全删 + strict confirm + 404 not found。"""

    def test_destroy_main_path_three_targets(
        self, client: TestClient, workspaces_root: Path, archives_root: Path
    ) -> None:
        """workspace 目录 + 顶级 archive 都存在 → 两项全删。

        构造场景:先 create 走完一次 archive(产 tar.gz + 删原目录),再 create
        同 id 把目录建回来。此时 workspace 目录 + 顶级 archive 双双在场。
        """
        client.post("/api/v1/workspaces", json=_create_payload())
        client.post("/api/v1/workspaces/scifi_book_1:archive")
        # archive 后 workspace 目录已删,但顶级 archive 在;再 create 同 id
        # 把目录重建,模拟"已 archive 过但又新写过一段内容"场景
        client.post("/api/v1/workspaces", json=_create_payload())

        archive_file = archives_root / "scifi_book_1.tar.gz"
        workspace_dir = workspaces_root / "scifi_book_1"
        assert workspace_dir.is_dir()
        assert archive_file.is_file()

        resp = client.post(
            "/api/v1/workspaces/scifi_book_1:destroy",
            json={"confirm": "scifi_book_1"},
        )
        assert resp.status_code == 200, resp.json()
        data = resp.json()["data"]
        assert data["workspace_id"] == "scifi_book_1"
        assert len(data["deleted_paths"]) == 2
        assert any(p.endswith("scifi_book_1") for p in data["deleted_paths"])
        assert any(p.endswith(".tar.gz") for p in data["deleted_paths"])
        assert not workspace_dir.is_dir()
        assert not archive_file.is_file()

    def test_destroy_workspace_only_no_archive(
        self, client: TestClient, workspaces_root: Path, archives_root: Path
    ) -> None:
        client.post("/api/v1/workspaces", json=_create_payload())
        workspace_dir = workspaces_root / "scifi_book_1"
        archive_file = archives_root / "scifi_book_1.tar.gz"
        assert workspace_dir.is_dir()
        assert not archive_file.is_file()

        resp = client.post(
            "/api/v1/workspaces/scifi_book_1:destroy",
            json={"confirm": "scifi_book_1"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data["deleted_paths"]) == 1
        assert not workspace_dir.is_dir()

    def test_destroy_confirm_mismatch_returns_422(
        self, client: TestClient, workspaces_root: Path
    ) -> None:
        client.post("/api/v1/workspaces", json=_create_payload())
        resp = client.post(
            "/api/v1/workspaces/scifi_book_1:destroy",
            json={"confirm": "wrong_id"},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["code"] == "WORKSPACE_DESTROY_CONFIRM_INVALID"
        assert body["error"]["details"]["workspace_id"] == "scifi_book_1"
        assert body["error"]["details"]["got"] == "wrong_id"
        # 文件系统未改动
        assert (workspaces_root / "scifi_book_1").is_dir()

    def test_destroy_workspace_not_found_returns_404(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/api/v1/workspaces/ghost_book:destroy",
            json={"confirm": "ghost_book"},
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "WORKSPACE_NOT_FOUND"
        assert body["error"]["details"]["workspace_id"] == "ghost_book"

    def test_destroy_missing_confirm_field_returns_validation_failed(
        self, client: TestClient
    ) -> None:
        client.post("/api/v1/workspaces", json=_create_payload())
        resp = client.post("/api/v1/workspaces/scifi_book_1:destroy", json={})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


class TestBackup:
    """P6-B-07 起 ``:backup`` 走 BackupService(.zip 模式),响应 schema
    切到 ``BackupInfoOut``;P1 既有 .tar.gz 路径已弃用。"""

    def test_backup_main_path(self, client: TestClient) -> None:
        client.post("/api/v1/workspaces", json=_create_payload())
        resp = client.post("/api/v1/workspaces/scifi_book_1:backup")
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert body["file_path"].endswith(".zip")
        assert body["backup_type"] == "manual"
        assert body["workspace_id"] == "scifi_book_1"
        assert body["backup_id"].startswith("manual_")
        assert body["size_bytes"] > 0

    def test_backup_missing_returns_workspace_conflict(
        self, client: TestClient
    ) -> None:
        resp = client.post("/api/v1/workspaces/no_such:backup")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "WORKSPACE_CONFLICT"


class TestRestore:
    """P6-B-07 起 ``:restore`` 走 BackupService(.zip 模式),body 必传
    ``{backup_id, confirm}``,语义"从工作区内 backups/<backup_id>.zip 恢复"。
    P1 archive(.tar.gz)恢复路径在本任务范围内不再走 HTTP API(CLI 仍可)。"""

    def test_restore_main_path(self, client: TestClient) -> None:
        client.post("/api/v1/workspaces", json=_create_payload())
        # 普通读取会通过产品 session factory 初始化 fresh workspace 数据库；
        # 完整备份必须包含已绑定身份的合法 novel.db，才能进入原子恢复流程。
        info_resp = client.get("/api/v1/workspaces/scifi_book_1")
        assert info_resp.status_code == 200
        # 先手动备份产 backup_id
        backup_resp = client.post("/api/v1/workspaces/scifi_book_1:backup")
        backup_id = backup_resp.json()["data"]["backup_id"]
        # 二次确认 confirm 匹配
        resp = client.post(
            "/api/v1/workspaces/scifi_book_1:restore",
            json={"backup_id": backup_id, "confirm": "scifi_book_1"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["backup_id"] == backup_id
        assert data["workspace_id"] == "scifi_book_1"
        assert data["pre_restore_backup_id"].startswith("pre_restore_")

    def test_restore_backup_not_found(self, client: TestClient) -> None:
        client.post("/api/v1/workspaces", json=_create_payload())
        resp = client.post(
            "/api/v1/workspaces/scifi_book_1:restore",
            json={"backup_id": "manual_19990101T000000Z", "confirm": "scifi_book_1"},
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "BACKUP_NOT_FOUND"

    def test_restore_confirm_mismatch(self, client: TestClient) -> None:
        client.post("/api/v1/workspaces", json=_create_payload())
        backup_resp = client.post("/api/v1/workspaces/scifi_book_1:backup")
        backup_id = backup_resp.json()["data"]["backup_id"]
        resp = client.post(
            "/api/v1/workspaces/scifi_book_1:restore",
            json={"backup_id": backup_id, "confirm": "wrong_id"},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "RESTORE_CONFIRM_MISMATCH"


class TestOpenAPI:
    def test_openapi_includes_all_workspace_routes(self, client: TestClient) -> None:
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        paths = resp.json()["paths"]
        expected = [
            "/api/v1/workspaces",
            "/api/v1/workspaces/{workspace_id}",
            "/api/v1/workspaces/{workspace_id}:switch",
            "/api/v1/workspaces/{workspace_id}:archive",
            "/api/v1/workspaces/{workspace_id}:clean",
            "/api/v1/workspaces/{workspace_id}:purge",
            "/api/v1/workspaces/{workspace_id}:backup",
            "/api/v1/workspaces/{workspace_id}:restore",
            "/api/v1/workspaces/{workspace_id}/backups",
        ]
        for path in expected:
            assert path in paths, f"OpenAPI 缺路由 {path}"
