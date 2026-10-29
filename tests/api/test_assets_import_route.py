"""``POST /api/v1/workspaces/{id}/assets:import`` 路由集成测试(P5-B-08)。

覆盖维度:

- 主路径:同时上传 world.md + characters.md → 200 + counts.character / fact 正确
- 单独上传 world.md:counts.character=0 + counts.fact 非 0
- 单独上传 characters.md:counts.character 非 0 + counts.fact=0
- 全为 None / 空字符串:200 + counts 全 0 + files_processed 空
- 文件落盘实证:``workspaces/{id}/data/assets/world.md`` / ``characters.md`` 存在且内容匹配
- `ensure_workspace_orm` 实证:WorkspaceORM 缺失(P1-P4 旧工作区)自动同步
- 工作区不存在:``WORKSPACE_INVALID (422)``(``MetaFileError`` 翻译)
- markdown 必填字段缺失:``ASSETS_IMPORT_INVALID (422)``(``AssetImportError`` 翻译)
- body extra="forbid":带未知字段 → 422 ``VALIDATION_FAILED``

测试基础设施
============

- ``WorkspaceService`` 用 ``tmp_path`` 显式注入 ``workspaces_root`` /
  ``archives_root``。
- monkeypatch ``src.api.routes.assets.make_vector_store`` 用 mock embedding
  fn 的 ``VectorStore``(避免触发真模型加载 ~1.2GB)。
- ``make_db_session`` 不替换 — 它直接绑工作区目录下 ``novel.db``,
  ``Base.metadata.create_all`` 在 tmp_path 内 idempotent 建表。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import routes as _api_routes  # noqa: F401  确保子模块可被访问
from src.api.deps import get_workspace_service
from src.api.main import create_app
from src.api.routes import assets as assets_route
from src.core.memory.store import list_characters_by_workspace
from src.core.memory.vector import VectorStore
from src.services.workspace_service import WorkspaceService


# =============================================================================
# Fixtures
# =============================================================================


_CHARACTERS_MD = """\
# 角色

## 林衍

| 字段 | 值 |
|------|----|
| character_id | lin-yan |
| name | 林衍 |
| archetype | 萌新剑修 |
| appearance | 黑衣黑发 |
| personality | 沉静内敛 |
| background | 寒门出身 |
| tone | 冷静 |
| sentence_style | 短句直白 |
| self_address | 我 |
| aliases | 衍儿 |
| catchphrases | 剑随心动 |
| forbidden_phrases | 现代俚语 |
| addresses_others | hero_001:小子 |
| relationships | hero_001:同门 |
| goals_long_term | 修剑成圣 |
| goals_short_term | 突破筑基 |
| abilities | 御剑诀 |
| secrets | 玉佩中藏剑魂 |
| first_appearance_chapter | ch-0001 |

## 苏离

| 字段 | 值 |
|------|----|
| character_id | su-li |
| name | 苏离 |
| archetype | 神秘前辈 |
| appearance | 白衣胜雪 |
| personality | 高冷 |
| background | 来历不明 |
| tone | 淡漠 |
| sentence_style | 长句修饰 |
| self_address | 本座 |
"""

_WORLD_MD = """\
# 世界观

## 修真等级体系

| 字段 | 值 |
|------|----|
| fact_id | xiu-zhen-deng-ji |
| description | 修真分九境 |
| involved_characters | (无) |

## 青崖山地理

| 字段 | 值 |
|------|----|
| fact_id | qing-ya-shan-di-li |
| description | 青崖山位于东海之滨 |
"""


def _mock_embedding_fn(texts: list[str]) -> list[list[float]]:
    """4 维兜底 embedding;单测不验距离语义,只验落库。"""
    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


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
def client(
    workspace_service: WorkspaceService,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """构造 TestClient,monkeypatch ``make_vector_store`` 避免触发真 embedding。"""

    def fake_make_vector_store(workspace_id: str, paths) -> VectorStore:  # type: ignore[no-untyped-def]
        return VectorStore(
            workspace_id,
            embedding_fn=_mock_embedding_fn,
            persist_dir=paths.chroma_dir,
        )

    monkeypatch.setattr(
        assets_route, "make_vector_store", fake_make_vector_store
    )

    app = create_app()
    app.dependency_overrides[get_workspace_service] = lambda: workspace_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def prepared(workspace_service: WorkspaceService) -> str:
    """落 ``ws-demo`` 工作区(只写 meta.json,不预填 WorkspaceORM —
    验证 ``ensure_workspace_orm`` 自动同步)。返回 ws_id。
    """
    workspace_service.create(
        "ws-demo",
        display_name="资产测试本",
        genre="xianxia",
        premise="一句话",
        total_chapters=120,
    )
    return "ws-demo"


# =============================================================================
# 主路径
# =============================================================================


class TestImportHappy:
    def test_world_and_characters_together(
        self,
        client: TestClient,
        prepared: str,
        workspaces_root: Path,
    ) -> None:
        resp = client.post(
            f"/api/v1/workspaces/{prepared}/assets:import",
            json={
                "world_md_content": _WORLD_MD,
                "characters_md_content": _CHARACTERS_MD,
            },
        )
        assert resp.status_code == 200, resp.json()
        body = resp.json()
        assert "error" not in body
        data = body["data"]
        assert data["workspace_id"] == prepared
        assert sorted(data["files_processed"]) == ["characters.md", "world.md"]
        assert data["counts"]["character"] == 2
        assert data["counts"]["fact"] == 2
        # 未上传的类型 counts = 0
        assert data["counts"]["plot_thread"] == 0
        assert data["counts"]["timeline"] == 0

    def test_files_landed_on_disk(
        self,
        client: TestClient,
        prepared: str,
        workspaces_root: Path,
    ) -> None:
        client.post(
            f"/api/v1/workspaces/{prepared}/assets:import",
            json={
                "world_md_content": _WORLD_MD,
                "characters_md_content": _CHARACTERS_MD,
            },
        )
        assets_dir = workspaces_root / prepared / "data" / "assets"
        assert (assets_dir / "world.md").is_file()
        assert (assets_dir / "characters.md").is_file()
        assert (
            assets_dir / "world.md"
        ).read_text(encoding="utf-8") == _WORLD_MD
        assert (
            assets_dir / "characters.md"
        ).read_text(encoding="utf-8") == _CHARACTERS_MD

    def test_characters_persisted_to_sqlite(
        self,
        client: TestClient,
        prepared: str,
        workspaces_root: Path,
    ) -> None:
        client.post(
            f"/api/v1/workspaces/{prepared}/assets:import",
            json={
                "world_md_content": _WORLD_MD,
                "characters_md_content": _CHARACTERS_MD,
            },
        )
        # 直接读工作区 novel.db 验证 SQLite 真落库(不走 GET API)。
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from src.core.db import Base

        db_path = workspaces_root / prepared / "data" / "novel.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            characters = list_characters_by_workspace(session, prepared)
        ids = sorted(c.character_id for c in characters)
        assert ids == ["lin-yan", "su-li"]

    def test_only_world(
        self, client: TestClient, prepared: str
    ) -> None:
        resp = client.post(
            f"/api/v1/workspaces/{prepared}/assets:import",
            json={"world_md_content": _WORLD_MD},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["files_processed"] == ["world.md"]
        assert data["counts"]["character"] == 0
        assert data["counts"]["fact"] == 2

    def test_only_characters(
        self, client: TestClient, prepared: str
    ) -> None:
        resp = client.post(
            f"/api/v1/workspaces/{prepared}/assets:import",
            json={"characters_md_content": _CHARACTERS_MD},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["files_processed"] == ["characters.md"]
        assert data["counts"]["character"] == 2
        assert data["counts"]["fact"] == 0

    def test_empty_body_returns_zero_counts(
        self, client: TestClient, prepared: str
    ) -> None:
        """两字段都为 None:assets_dir 仍会 mkdir,counts 全 0,files_processed 空。"""
        resp = client.post(
            f"/api/v1/workspaces/{prepared}/assets:import",
            json={},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["files_processed"] == []
        assert data["counts"] == {
            "character": 0,
            "plot_thread": 0,
            "timeline": 0,
            "fact": 0,
        }


# =============================================================================
# 幂等 + 复用历史
# =============================================================================


class TestIdempotency:
    def test_second_upload_overwrites_disk(
        self,
        client: TestClient,
        prepared: str,
        workspaces_root: Path,
    ) -> None:
        client.post(
            f"/api/v1/workspaces/{prepared}/assets:import",
            json={"world_md_content": _WORLD_MD},
        )
        # 第二次只传 characters_md;world.md 旧文件应保留(本次不上传 = 不删)。
        resp = client.post(
            f"/api/v1/workspaces/{prepared}/assets:import",
            json={"characters_md_content": _CHARACTERS_MD},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        # 第二次会扫到磁盘已有的 world.md + 本次新写的 characters.md
        assert sorted(data["files_processed"]) == ["characters.md", "world.md"]


# =============================================================================
# 错误路径
# =============================================================================


class TestErrors:
    def test_unknown_workspace_returns_422_workspace_invalid(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/api/v1/workspaces/ws-nope/assets:import",
            json={"world_md_content": _WORLD_MD},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "WORKSPACE_INVALID"

    def test_invalid_markdown_returns_422_assets_import_invalid(
        self, client: TestClient, prepared: str
    ) -> None:
        """``characters.md`` 缺必填字段(无 character_id)→
        ``ASSETS_IMPORT_INVALID``。"""
        bad_md = """\
# 角色

## 林衍

| 字段 | 值 |
|------|----|
| name | 林衍 |
"""
        resp = client.post(
            f"/api/v1/workspaces/{prepared}/assets:import",
            json={"characters_md_content": bad_md},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "ASSETS_IMPORT_INVALID"

    def test_extra_field_rejected_by_pydantic(
        self, client: TestClient, prepared: str
    ) -> None:
        """body extra="forbid":带未知字段 → 422 VALIDATION_FAILED。"""
        resp = client.post(
            f"/api/v1/workspaces/{prepared}/assets:import",
            json={"world_md_content": _WORLD_MD, "unknown": "x"},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"

    def test_rollback_on_failure_keeps_db_clean(
        self,
        client: TestClient,
        prepared: str,
        workspaces_root: Path,
    ) -> None:
        """解析失败时 session.rollback(),SQLite 不留半截行。"""
        bad_md = """\
# 角色

## 林衍

| 字段 | 值 |
|------|----|
| name | 林衍 |
"""
        client.post(
            f"/api/v1/workspaces/{prepared}/assets:import",
            json={"characters_md_content": bad_md},
        )
        # 直接读 novel.db 看 characters 表为空(rollback 生效)。
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from src.core.db import Base

        db_path = workspaces_root / prepared / "data" / "novel.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            characters = list_characters_by_workspace(session, prepared)
        assert characters == []
