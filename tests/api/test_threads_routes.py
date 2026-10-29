"""伏笔路由集成用例(P2-B-15)。

覆盖 5 个 CRUD 端点的"主路径 + 错误路径",并验证:

- 路由前置守卫 vs service 异常翻译双轨划分
- ``X-Workspace-Id`` header 强制纪律
- 跨工作区视为不存在的安全语义(404)
- ``?status=`` / ``?importance=`` query 参数过滤组合

复用 P2-B-14 ``test_characters_routes.py`` 的 fixture 模板(``WorkspaceService``
+ in-memory ``shared_session`` + ``_SessionProxy`` close-no-op)。

错误码覆盖矩阵
==============

| 错误码 | 来源 |
|--------|------|
| ``MISSING_WORKSPACE_HEADER`` (422) | 路由前置依赖 |
| ``PLOT_THREAD_WORKSPACE_MISMATCH`` (422) | 路由前置守卫 |
| ``PLOT_THREAD_ID_MISMATCH`` (422) | 路由前置守卫(PUT) |
| ``PLOT_THREAD_NOT_FOUND`` (404) | service 翻译 |
| ``PLOT_THREAD_ALREADY_EXISTS`` (409) | service 翻译 |
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

from src.api.deps import get_thread_service
from src.api.main import create_app
from src.core.db import Base, WorkspaceORM
from src.core.models.plot_thread import PlotThread
from src.services.thread_service import PlotThreadService
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
    """全局共享 SQLite Session(每测试一个独立 db 文件)。"""
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    yield session
    session.close()


class _SessionProxy:
    """转发 ``Session`` 调用但 ``close`` 不真关。沿用 P2-B-14 模板。"""

    def __init__(self, inner: Session) -> None:
        self._inner = inner

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)

    def close(self) -> None:
        pass


@pytest.fixture()
def thread_service(
    workspace_service: WorkspaceService, shared_session: Session
) -> PlotThreadService:
    def factory(_workspace_id: str) -> Session:
        return _SessionProxy(shared_session)  # type: ignore[return-value]

    return PlotThreadService(
        workspace_service=workspace_service, session_factory=factory
    )


@pytest.fixture()
def client(thread_service: PlotThreadService) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_thread_service] = lambda: thread_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---- 工厂 -------------------------------------------------------------------


def _thread(
    *,
    thread_id: str = "thr_001",
    workspace_id: str = "ws-demo",
    status: str = "planted",
    importance: int = 3,
    created_at: datetime | None = None,
    **overrides: Any,
) -> PlotThread:
    payload: dict[str, Any] = {
        "thread_id": thread_id,
        "workspace_id": workspace_id,
        "title": "魔尊之子身世",
        "status": status,
        "importance": importance,
        "planted_chapter": "v1c003",
        "planned_payoff": "v1c050",
        "actual_payoff": None,
        "description": "魔尊之子被人类抚养,血脉觉醒后引发剧烈冲突",
        "related_characters": ["villain_001", "hero_001"],
        "created_at": created_at or datetime(2026, 5, 1, tzinfo=timezone.utc),
        "last_updated_chapter": None,
    }
    payload.update(overrides)
    return PlotThread(**payload)


@pytest.fixture()
def prepared(
    workspace_service: WorkspaceService, shared_session: Session
) -> str:
    """落 ``ws-demo`` 工作区(meta.json + WorkspaceORM 同步)。"""
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
# 1. GET /threads(列表 + 过滤)
# =============================================================================


class TestList:
    def test_list_empty_returns_empty_array(
        self, client: TestClient, prepared: str
    ) -> None:
        resp = client.get("/api/v1/threads", headers=_hdr())
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"] == []
        assert "error" not in body

    def test_list_returns_workspace_threads_sorted(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        # 先创建 created_at 较晚的(thr_late),再创建较早的(thr_early)
        client.post(
            "/api/v1/threads",
            headers=_hdr(),
            json=_thread(
                thread_id="thr_late",
                created_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
            ).model_dump(mode="json"),
        )
        client.post(
            "/api/v1/threads",
            headers=_hdr(),
            json=_thread(
                thread_id="thr_early",
                created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            ).model_dump(mode="json"),
        )
        resp = client.get("/api/v1/threads", headers=_hdr())
        assert resp.status_code == 200
        ids = [t["thread_id"] for t in resp.json()["data"]]
        # store 按 (created_at, thread_id) 升序 → thr_early 在前
        assert ids == ["thr_early", "thr_late"]

    def test_list_isolates_by_workspace(
        self,
        client: TestClient,
        prepared: str,
        prepared_other: str,
    ) -> None:
        client.post(
            "/api/v1/threads",
            headers=_hdr("ws-demo"),
            json=_thread(thread_id="thr_demo").model_dump(mode="json"),
        )
        client.post(
            "/api/v1/threads",
            headers=_hdr("ws-other"),
            json=_thread(
                thread_id="thr_other", workspace_id="ws-other"
            ).model_dump(mode="json"),
        )
        demo = client.get("/api/v1/threads", headers=_hdr("ws-demo")).json()
        other = client.get("/api/v1/threads", headers=_hdr("ws-other")).json()
        assert [t["thread_id"] for t in demo["data"]] == ["thr_demo"]
        assert [t["thread_id"] for t in other["data"]] == ["thr_other"]

    def test_list_filter_by_status(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        for tid, status in [
            ("thr_a", "planted"),
            ("thr_b", "developing"),
            ("thr_c", "developing"),
            ("thr_d", "resolved"),
        ]:
            client.post(
                "/api/v1/threads",
                headers=_hdr(),
                json=_thread(thread_id=tid, status=status).model_dump(
                    mode="json"
                ),
            )
        resp = client.get(
            "/api/v1/threads", headers=_hdr(), params={"status": "developing"}
        )
        assert resp.status_code == 200
        ids = sorted(t["thread_id"] for t in resp.json()["data"])
        assert ids == ["thr_b", "thr_c"]

    def test_list_filter_by_importance(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        for tid, imp in [
            ("thr_a", 1),
            ("thr_b", 5),
            ("thr_c", 5),
            ("thr_d", 3),
        ]:
            client.post(
                "/api/v1/threads",
                headers=_hdr(),
                json=_thread(thread_id=tid, importance=imp).model_dump(
                    mode="json"
                ),
            )
        resp = client.get(
            "/api/v1/threads", headers=_hdr(), params={"importance": 5}
        )
        assert resp.status_code == 200
        ids = sorted(t["thread_id"] for t in resp.json()["data"])
        assert ids == ["thr_b", "thr_c"]

    def test_list_filter_combined_status_importance(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        # 5 条数据,只有 thr_match (developing+5) 同时命中两个筛选
        rows = [
            ("thr_match", "developing", 5),
            ("thr_status_only", "developing", 3),
            ("thr_imp_only", "planted", 5),
            ("thr_neither", "abandoned", 1),
            ("thr_match2", "developing", 5),
        ]
        for tid, status, imp in rows:
            client.post(
                "/api/v1/threads",
                headers=_hdr(),
                json=_thread(
                    thread_id=tid, status=status, importance=imp
                ).model_dump(mode="json"),
            )
        resp = client.get(
            "/api/v1/threads",
            headers=_hdr(),
            params={"status": "developing", "importance": 5},
        )
        assert resp.status_code == 200
        ids = sorted(t["thread_id"] for t in resp.json()["data"])
        assert ids == ["thr_match", "thr_match2"]

    def test_list_filter_invalid_status_returns_422(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        resp = client.get(
            "/api/v1/threads", headers=_hdr(), params={"status": "bogus"}
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"

    def test_list_filter_invalid_importance_returns_422(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        resp = client.get(
            "/api/v1/threads", headers=_hdr(), params={"importance": 9}
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"

    def test_list_missing_workspace_header_returns_422(
        self, client: TestClient
    ) -> None:
        resp = client.get("/api/v1/threads")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"

    def test_list_unknown_workspace_returns_422(
        self, client: TestClient
    ) -> None:
        resp = client.get("/api/v1/threads", headers=_hdr("ws-nope"))
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "WORKSPACE_INVALID"


# =============================================================================
# 2. GET /threads/{id}
# =============================================================================


class TestGet:
    def test_get_round_trip(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        client.post(
            "/api/v1/threads",
            headers=_hdr(),
            json=_thread().model_dump(mode="json"),
        )
        resp = client.get("/api/v1/threads/thr_001", headers=_hdr())
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["thread_id"] == "thr_001"
        assert data["status"] == "planted"
        assert data["importance"] == 3
        assert data["related_characters"] == ["villain_001", "hero_001"]

    def test_get_missing_returns_404(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        resp = client.get("/api/v1/threads/thr_999", headers=_hdr())
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "PLOT_THREAD_NOT_FOUND"

    def test_get_cross_workspace_treated_as_not_found(
        self,
        client: TestClient,
        prepared: str,
        prepared_other: str,
    ) -> None:
        client.post(
            "/api/v1/threads",
            headers=_hdr("ws-demo"),
            json=_thread().model_dump(mode="json"),
        )
        # ws-other 视图下 thr_001 不存在
        resp = client.get("/api/v1/threads/thr_001", headers=_hdr("ws-other"))
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "PLOT_THREAD_NOT_FOUND"

    def test_get_missing_workspace_header_returns_422(
        self, client: TestClient
    ) -> None:
        resp = client.get("/api/v1/threads/thr_001")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"


# =============================================================================
# 3. POST /threads
# =============================================================================


class TestCreate:
    def test_create_returns_201_and_persists(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        body = _thread().model_dump(mode="json")
        resp = client.post("/api/v1/threads", headers=_hdr(), json=body)
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["thread_id"] == "thr_001"
        get_resp = client.get("/api/v1/threads/thr_001", headers=_hdr())
        assert get_resp.status_code == 200

    def test_create_duplicate_returns_409(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        body = _thread().model_dump(mode="json")
        client.post("/api/v1/threads", headers=_hdr(), json=body)
        resp = client.post("/api/v1/threads", headers=_hdr(), json=body)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PLOT_THREAD_ALREADY_EXISTS"

    def test_create_workspace_mismatch_returns_422(
        self,
        client: TestClient,
        prepared: str,
        prepared_other: str,
    ) -> None:
        body = _thread(workspace_id="ws-other").model_dump(mode="json")
        resp = client.post("/api/v1/threads", headers=_hdr("ws-demo"), json=body)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "PLOT_THREAD_WORKSPACE_MISMATCH"

    def test_create_missing_workspace_header_returns_422(
        self, client: TestClient
    ) -> None:
        body = _thread().model_dump(mode="json")
        resp = client.post("/api/v1/threads", json=body)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"

    def test_create_invalid_body_returns_422(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        # importance 越界
        body = _thread().model_dump(mode="json")
        body["importance"] = 99
        resp = client.post("/api/v1/threads", headers=_hdr(), json=body)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"

    def test_create_invalid_status_returns_422(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        body = _thread().model_dump(mode="json")
        body["status"] = "bogus"
        resp = client.post("/api/v1/threads", headers=_hdr(), json=body)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"

    def test_create_global_id_collision_with_other_workspace_returns_409(
        self,
        client: TestClient,
        prepared: str,
        prepared_other: str,
    ) -> None:
        """``thread_id`` 是 SQLite 全局主键;另一工作区已占用 → 409。"""
        client.post(
            "/api/v1/threads",
            headers=_hdr("ws-demo"),
            json=_thread(workspace_id="ws-demo").model_dump(mode="json"),
        )
        body = _thread(workspace_id="ws-other").model_dump(mode="json")
        resp = client.post("/api/v1/threads", headers=_hdr("ws-other"), json=body)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PLOT_THREAD_ALREADY_EXISTS"


# =============================================================================
# 4. PUT /threads/{id}
# =============================================================================


class TestUpdate:
    def test_update_round_trip(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        client.post(
            "/api/v1/threads",
            headers=_hdr(),
            json=_thread().model_dump(mode="json"),
        )
        updated = _thread(
            status="developing", importance=5, actual_payoff="v1c060"
        ).model_dump(mode="json")
        resp = client.put(
            "/api/v1/threads/thr_001", headers=_hdr(), json=updated
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "developing"
        assert data["importance"] == 5
        assert data["actual_payoff"] == "v1c060"
        # 二次 GET 验证持久化
        got = client.get("/api/v1/threads/thr_001", headers=_hdr()).json()
        assert got["data"]["status"] == "developing"

    def test_update_missing_returns_404(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        body = _thread(thread_id="ghost").model_dump(mode="json")
        resp = client.put("/api/v1/threads/ghost", headers=_hdr(), json=body)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "PLOT_THREAD_NOT_FOUND"

    def test_update_id_mismatch_returns_422(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        client.post(
            "/api/v1/threads",
            headers=_hdr(),
            json=_thread().model_dump(mode="json"),
        )
        body = _thread(thread_id="other_id").model_dump(mode="json")
        resp = client.put("/api/v1/threads/thr_001", headers=_hdr(), json=body)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "PLOT_THREAD_ID_MISMATCH"

    def test_update_workspace_mismatch_returns_422(
        self,
        client: TestClient,
        prepared: str,
        prepared_other: str,
    ) -> None:
        client.post(
            "/api/v1/threads",
            headers=_hdr("ws-demo"),
            json=_thread().model_dump(mode="json"),
        )
        body = _thread(workspace_id="ws-other").model_dump(mode="json")
        resp = client.put(
            "/api/v1/threads/thr_001", headers=_hdr("ws-demo"), json=body
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "PLOT_THREAD_WORKSPACE_MISMATCH"

    def test_update_cross_workspace_target_returns_404(
        self,
        client: TestClient,
        prepared: str,
        prepared_other: str,
    ) -> None:
        """ws-demo 下创建 thr_001;ws-other 头 + body 改它 → 404。

        路由层 body.workspace_id=ws-other / header=ws-other 一致(无 422),
        但 store 内 thr_001.workspace_id=ws-demo → service 视为不存在 →
        404。
        """
        client.post(
            "/api/v1/threads",
            headers=_hdr("ws-demo"),
            json=_thread(workspace_id="ws-demo").model_dump(mode="json"),
        )
        body = _thread(workspace_id="ws-other").model_dump(mode="json")
        resp = client.put(
            "/api/v1/threads/thr_001", headers=_hdr("ws-other"), json=body
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "PLOT_THREAD_NOT_FOUND"

    def test_update_missing_workspace_header_returns_422(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        body = _thread().model_dump(mode="json")
        resp = client.put("/api/v1/threads/thr_001", json=body)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"


# =============================================================================
# 5. DELETE /threads/{id}
# =============================================================================


class TestDelete:
    def test_delete_round_trip(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        client.post(
            "/api/v1/threads",
            headers=_hdr(),
            json=_thread().model_dump(mode="json"),
        )
        resp = client.delete("/api/v1/threads/thr_001", headers=_hdr())
        assert resp.status_code == 200
        assert resp.json()["data"] == {"thread_id": "thr_001"}
        # 二次 GET 应 404
        got = client.get("/api/v1/threads/thr_001", headers=_hdr())
        assert got.status_code == 404

    def test_delete_missing_returns_404(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        resp = client.delete("/api/v1/threads/ghost", headers=_hdr())
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "PLOT_THREAD_NOT_FOUND"

    def test_delete_cross_workspace_returns_404(
        self,
        client: TestClient,
        prepared: str,
        prepared_other: str,
    ) -> None:
        client.post(
            "/api/v1/threads",
            headers=_hdr("ws-demo"),
            json=_thread().model_dump(mode="json"),
        )
        resp = client.delete(
            "/api/v1/threads/thr_001", headers=_hdr("ws-other")
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "PLOT_THREAD_NOT_FOUND"
        # 原工作区数据未受影响
        got = client.get("/api/v1/threads/thr_001", headers=_hdr("ws-demo"))
        assert got.status_code == 200

    def test_delete_missing_workspace_header_returns_422(
        self, client: TestClient
    ) -> None:
        resp = client.delete("/api/v1/threads/thr_001")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"


# =============================================================================
# 6. OpenAPI
# =============================================================================


class TestOpenAPI:
    def test_openapi_includes_all_thread_routes(
        self, client: TestClient
    ) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        assert "/api/v1/threads" in paths
        assert "/api/v1/threads/{thread_id}" in paths
        assert "get" in paths["/api/v1/threads"]
        assert "post" in paths["/api/v1/threads"]
        ops = paths["/api/v1/threads/{thread_id}"]
        assert "get" in ops
        assert "put" in ops
        assert "delete" in ops

    def test_openapi_list_has_status_and_importance_query_params(
        self, client: TestClient
    ) -> None:
        spec = client.get("/openapi.json").json()
        list_op = spec["paths"]["/api/v1/threads"]["get"]
        param_names = {p["name"] for p in list_op.get("parameters", [])}
        assert "status" in param_names
        assert "importance" in param_names
