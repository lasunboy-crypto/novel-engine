"""角色路由集成用例(P2-B-14)。

覆盖 5 个 CRUD 端点的"主路径 + 错误路径",并验证 P1-B-17 全局异常处理 +
P1-B-18 双轨划分(路由前置守卫 vs service 异常翻译)在 P2 角色路由的
应用。

测试基础设施
============

复用 P1-B-19 章节路由测试套路(``tests/api/test_chapters_routes.py``):

- ``WorkspaceService`` 用 ``tmp_path`` 显式注入 ``workspaces_root`` /
  ``archives_root``。
- ``CharacterService`` 用 in-memory 工厂(每次返回同一个共享 Session)避开
  真实文件系统,事务在每个 request 自然 commit。
- ``app.dependency_overrides`` 覆盖 ``get_character_service`` 顶层 dep
  (FastAPI 解依赖一旦命中 override 就不继续解嵌套)。

错误码覆盖矩阵
==============

| 错误码 | 来源 |
|--------|------|
| ``MISSING_WORKSPACE_HEADER`` (422) | 路由前置依赖 |
| ``CHARACTER_WORKSPACE_MISMATCH`` (422) | 路由前置守卫 |
| ``CHARACTER_ID_MISMATCH`` (422) | 路由前置守卫(PUT) |
| ``CHARACTER_NOT_FOUND`` (404) | service 翻译(``CharacterNotFoundError``) |
| ``CHARACTER_ALREADY_EXISTS`` (409) | service 翻译(``CharacterAlreadyExistsError``) |
| ``WORKSPACE_INVALID`` (422) | service 翻译(``MetaFileError`` 冒泡) |
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.api.deps import get_character_service
from src.api.main import create_app
from src.core.db import Base, WorkspaceORM
from src.core.models.character import Character, VoiceProfile
from src.services.character_service import CharacterService
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
def shared_session(tmp_path: Path) -> Iterator[Session]:
    """全局共享的 SQLite Session(in-memory engine)。

    单测共享一个 Session 与 engine,工厂返回它的"代理 wrapper"——wrapper 转
    发所有调用到底层,但 ``close`` 是 no-op,避免 service 关闭后下一次请求
    没法读到刚 commit 的数据。
    """
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    yield session
    session.close()


class _SessionProxy:
    """转发 ``Session`` 调用但 ``close`` 不真关闭。"""

    def __init__(self, inner: Session) -> None:
        self._inner = inner

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)

    def close(self) -> None:  # service 调 close 时不真关
        pass


@pytest.fixture()
def character_service(
    workspace_service: WorkspaceService, shared_session: Session
) -> CharacterService:
    def factory(_workspace_id: str) -> Session:
        # 每次返回 wrapper(底层共享同一个 Session 与 engine)
        return _SessionProxy(shared_session)  # type: ignore[return-value]

    return CharacterService(
        workspace_service=workspace_service, session_factory=factory
    )


@pytest.fixture()
def client(character_service: CharacterService) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_character_service] = lambda: character_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---- 工厂 -------------------------------------------------------------------


def _voice_profile(**overrides: Any) -> VoiceProfile:
    payload: dict[str, Any] = {
        "catchphrases": ["剑动八方"],
        "tone": "冷峻",
        "sentence_style": "短句直白",
        "self_address": "本座",
        "addresses_others": {"hero_001": "小子"},
        "forbidden_phrases": ["亲爱的"],
    }
    payload.update(overrides)
    return VoiceProfile(**payload)


def _character(
    *,
    character_id: str = "villain_001",
    workspace_id: str = "ws-demo",
    **overrides: Any,
) -> Character:
    payload: dict[str, Any] = {
        "character_id": character_id,
        "workspace_id": workspace_id,
        "name": "魔尊",
        "aliases": ["魔头"],
        "archetype": "反派宗师",
        "appearance": "黑袍高冠",
        "personality": "孤傲",
        "background": "千年前坠魔",
        "voice_profile": _voice_profile(),
        "goals_long_term": ["重夺天命"],
        "goals_short_term": ["夺至宝"],
        "current_state": {"injury": "轻"},
        "relationships": {"hero_001": "宿敌"},
        "abilities": ["剑诀"],
        "secrets": ["生父"],
        "first_appearance_chapter": "v1c003",
        "last_updated_chapter": "v1c012",
    }
    payload.update(overrides)
    return Character(**payload)


@pytest.fixture()
def prepared(
    workspace_service: WorkspaceService, shared_session: Session
) -> str:
    """落 ``ws-demo`` 工作区(meta.json + WorkspaceORM 同步);返回 ws_id。"""
    workspace_service.create(
        "ws-demo",
        display_name="科幻测试本",
        genre="scifi",
        premise="人类在月球建殖民地",
        total_chapters=120,
    )
    now = datetime.now(timezone.utc)
    shared_session.add(
        WorkspaceORM(
            workspace_id="ws-demo",
            display_name="科幻测试本",
            genre="scifi",
            premise="人类在月球建殖民地",
            created_at=now,
            last_active_at=now,
            engine_version="0.1.0",
            status="active",
            total_chapters=120,
            completed_chapters=0,
            config_overrides={},
        )
    )
    shared_session.commit()
    return "ws-demo"


@pytest.fixture()
def prepared_other(
    workspace_service: WorkspaceService, shared_session: Session
) -> str:
    """落第二个工作区 ``ws-other`` 用于跨工作区隔离测试。"""
    workspace_service.create(
        "ws-other",
        display_name="另一本",
        genre="fantasy",
        premise="另一句话",
        total_chapters=50,
    )
    now = datetime.now(timezone.utc)
    shared_session.add(
        WorkspaceORM(
            workspace_id="ws-other",
            display_name="另一本",
            genre="fantasy",
            premise="另一句话",
            created_at=now,
            last_active_at=now,
            engine_version="0.1.0",
            status="active",
            total_chapters=50,
            completed_chapters=0,
            config_overrides={},
        )
    )
    shared_session.commit()
    return "ws-other"


def _hdr(workspace_id: str = "ws-demo") -> dict[str, str]:
    return {"X-Workspace-Id": workspace_id}


# =============================================================================
# 1. GET /characters(列表)
# =============================================================================


class TestList:
    def test_list_empty_returns_empty_array(
        self, client: TestClient, prepared: str
    ) -> None:
        resp = client.get("/api/v1/characters", headers=_hdr())
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"] == []
        assert "error" not in body

    def test_list_returns_workspace_characters_sorted(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        client.post(
            "/api/v1/characters",
            headers=_hdr(),
            json=_character(character_id="c_b").model_dump(mode="json"),
        )
        client.post(
            "/api/v1/characters",
            headers=_hdr(),
            json=_character(character_id="c_a").model_dump(mode="json"),
        )
        resp = client.get("/api/v1/characters", headers=_hdr())
        assert resp.status_code == 200
        ids = [c["character_id"] for c in resp.json()["data"]]
        assert ids == ["c_a", "c_b"]

    def test_list_isolates_by_workspace(
        self,
        client: TestClient,
        prepared: str,
        prepared_other: str,
    ) -> None:
        client.post(
            "/api/v1/characters",
            headers=_hdr("ws-demo"),
            json=_character(character_id="c_demo").model_dump(mode="json"),
        )
        client.post(
            "/api/v1/characters",
            headers=_hdr("ws-other"),
            json=_character(
                character_id="c_other", workspace_id="ws-other"
            ).model_dump(mode="json"),
        )
        demo = client.get("/api/v1/characters", headers=_hdr("ws-demo")).json()
        other = client.get("/api/v1/characters", headers=_hdr("ws-other")).json()
        assert [c["character_id"] for c in demo["data"]] == ["c_demo"]
        assert [c["character_id"] for c in other["data"]] == ["c_other"]

    def test_list_missing_workspace_header_returns_422(
        self, client: TestClient
    ) -> None:
        resp = client.get("/api/v1/characters")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"

    def test_list_unknown_workspace_returns_422(
        self, client: TestClient
    ) -> None:
        resp = client.get("/api/v1/characters", headers=_hdr("ws-nope"))
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "WORKSPACE_INVALID"


# =============================================================================
# 2. GET /characters/{id}(详情)
# =============================================================================


class TestGet:
    def test_get_round_trip(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        client.post(
            "/api/v1/characters",
            headers=_hdr(),
            json=_character().model_dump(mode="json"),
        )
        resp = client.get("/api/v1/characters/villain_001", headers=_hdr())
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["character_id"] == "villain_001"
        assert data["voice_profile"]["self_address"] == "本座"
        assert data["aliases"] == ["魔头"]

    def test_get_missing_returns_404(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        resp = client.get("/api/v1/characters/villain_999", headers=_hdr())
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "CHARACTER_NOT_FOUND"

    def test_get_cross_workspace_treated_as_not_found(
        self,
        client: TestClient,
        prepared: str,
        prepared_other: str,
    ) -> None:
        # ws-demo 创建 villain_001
        client.post(
            "/api/v1/characters",
            headers=_hdr("ws-demo"),
            json=_character().model_dump(mode="json"),
        )
        # ws-other 视图下 villain_001 不存在
        resp = client.get(
            "/api/v1/characters/villain_001", headers=_hdr("ws-other")
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "CHARACTER_NOT_FOUND"

    def test_get_missing_workspace_header_returns_422(
        self, client: TestClient
    ) -> None:
        resp = client.get("/api/v1/characters/villain_001")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"


# =============================================================================
# 3. POST /characters(新建)
# =============================================================================


class TestCreate:
    def test_create_returns_201_and_persists(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        body = _character().model_dump(mode="json")
        resp = client.post("/api/v1/characters", headers=_hdr(), json=body)
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["character_id"] == "villain_001"
        # GET 落库验证
        get_resp = client.get(
            "/api/v1/characters/villain_001", headers=_hdr()
        )
        assert get_resp.status_code == 200

    def test_create_duplicate_returns_409(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        body = _character().model_dump(mode="json")
        client.post("/api/v1/characters", headers=_hdr(), json=body)
        resp = client.post("/api/v1/characters", headers=_hdr(), json=body)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CHARACTER_ALREADY_EXISTS"

    def test_create_workspace_mismatch_returns_422(
        self,
        client: TestClient,
        prepared: str,
        prepared_other: str,
    ) -> None:
        # body.workspace_id=ws-other 但 header=ws-demo
        body = _character(workspace_id="ws-other").model_dump(mode="json")
        resp = client.post(
            "/api/v1/characters", headers=_hdr("ws-demo"), json=body
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "CHARACTER_WORKSPACE_MISMATCH"

    def test_create_missing_workspace_header_returns_422(
        self, client: TestClient
    ) -> None:
        body = _character().model_dump(mode="json")
        resp = client.post("/api/v1/characters", json=body)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"

    def test_create_invalid_body_returns_422(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        # 缺 voice_profile.tone 字段
        body = _character().model_dump(mode="json")
        del body["voice_profile"]["tone"]
        resp = client.post("/api/v1/characters", headers=_hdr(), json=body)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"

    def test_create_global_id_collision_with_other_workspace_returns_409(
        self,
        client: TestClient,
        prepared: str,
        prepared_other: str,
    ) -> None:
        """``character_id`` 是 SQLite 全局主键;另一工作区已占用 → 409。"""
        client.post(
            "/api/v1/characters",
            headers=_hdr("ws-demo"),
            json=_character(workspace_id="ws-demo").model_dump(mode="json"),
        )
        # ws-other 试图创建同名 character
        body = _character(workspace_id="ws-other").model_dump(mode="json")
        resp = client.post(
            "/api/v1/characters", headers=_hdr("ws-other"), json=body
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CHARACTER_ALREADY_EXISTS"


# =============================================================================
# 4. PUT /characters/{id}(更新)
# =============================================================================


class TestUpdate:
    def test_update_round_trip(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        client.post(
            "/api/v1/characters",
            headers=_hdr(),
            json=_character().model_dump(mode="json"),
        )
        updated = _character(personality="冷酷沉静").model_dump(mode="json")
        resp = client.put(
            "/api/v1/characters/villain_001", headers=_hdr(), json=updated
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["personality"] == "冷酷沉静"
        # 二次 GET 验证持久化
        got = client.get(
            "/api/v1/characters/villain_001", headers=_hdr()
        ).json()
        assert got["data"]["personality"] == "冷酷沉静"

    def test_update_missing_returns_404(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        body = _character(character_id="ghost").model_dump(mode="json")
        resp = client.put(
            "/api/v1/characters/ghost", headers=_hdr(), json=body
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "CHARACTER_NOT_FOUND"

    def test_update_id_mismatch_returns_422(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        client.post(
            "/api/v1/characters",
            headers=_hdr(),
            json=_character().model_dump(mode="json"),
        )
        body = _character(character_id="other_id").model_dump(mode="json")
        resp = client.put(
            "/api/v1/characters/villain_001", headers=_hdr(), json=body
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "CHARACTER_ID_MISMATCH"

    def test_update_workspace_mismatch_returns_422(
        self,
        client: TestClient,
        prepared: str,
        prepared_other: str,
    ) -> None:
        client.post(
            "/api/v1/characters",
            headers=_hdr("ws-demo"),
            json=_character().model_dump(mode="json"),
        )
        body = _character(workspace_id="ws-other").model_dump(mode="json")
        resp = client.put(
            "/api/v1/characters/villain_001",
            headers=_hdr("ws-demo"),
            json=body,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "CHARACTER_WORKSPACE_MISMATCH"

    def test_update_cross_workspace_target_returns_404(
        self,
        client: TestClient,
        prepared: str,
        prepared_other: str,
    ) -> None:
        """ws-demo 下创建 villain_001;ws-other 头 + body 改它 → 404。

        路由层 body.workspace_id=ws-other / header=ws-other 一致(无 422),
        但 store 内 villain_001.workspace_id=ws-demo → service 视为不存在 →
        404。
        """
        client.post(
            "/api/v1/characters",
            headers=_hdr("ws-demo"),
            json=_character(workspace_id="ws-demo").model_dump(mode="json"),
        )
        body = _character(workspace_id="ws-other").model_dump(mode="json")
        resp = client.put(
            "/api/v1/characters/villain_001",
            headers=_hdr("ws-other"),
            json=body,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "CHARACTER_NOT_FOUND"

    def test_update_missing_workspace_header_returns_422(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        body = _character().model_dump(mode="json")
        resp = client.put("/api/v1/characters/villain_001", json=body)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"


# =============================================================================
# 5. DELETE /characters/{id}
# =============================================================================


class TestDelete:
    def test_delete_round_trip(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        client.post(
            "/api/v1/characters",
            headers=_hdr(),
            json=_character().model_dump(mode="json"),
        )
        resp = client.delete(
            "/api/v1/characters/villain_001", headers=_hdr()
        )
        assert resp.status_code == 200
        assert resp.json()["data"] == {"character_id": "villain_001"}
        # 二次 GET 应 404
        got = client.get(
            "/api/v1/characters/villain_001", headers=_hdr()
        )
        assert got.status_code == 404

    def test_delete_missing_returns_404(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        resp = client.delete(
            "/api/v1/characters/ghost", headers=_hdr()
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "CHARACTER_NOT_FOUND"

    def test_delete_cross_workspace_returns_404(
        self,
        client: TestClient,
        prepared: str,
        prepared_other: str,
    ) -> None:
        client.post(
            "/api/v1/characters",
            headers=_hdr("ws-demo"),
            json=_character().model_dump(mode="json"),
        )
        resp = client.delete(
            "/api/v1/characters/villain_001", headers=_hdr("ws-other")
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "CHARACTER_NOT_FOUND"
        # 原工作区数据未受影响
        got = client.get(
            "/api/v1/characters/villain_001", headers=_hdr("ws-demo")
        )
        assert got.status_code == 200

    def test_delete_missing_workspace_header_returns_422(
        self, client: TestClient
    ) -> None:
        resp = client.delete("/api/v1/characters/villain_001")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"


# =============================================================================
# 6. OpenAPI
# =============================================================================


class TestOpenAPI:
    def test_openapi_includes_all_character_routes(
        self, client: TestClient
    ) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        assert "/api/v1/characters" in paths
        assert "/api/v1/characters/{character_id}" in paths
        # GET / POST 在 ``/api/v1/characters``
        assert "get" in paths["/api/v1/characters"]
        assert "post" in paths["/api/v1/characters"]
        # GET / PUT / DELETE 在 ``/api/v1/characters/{character_id}``
        ops = paths["/api/v1/characters/{character_id}"]
        assert "get" in ops
        assert "put" in ops
        assert "delete" in ops
