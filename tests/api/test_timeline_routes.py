"""时间线路由集成用例(P2-B-16)。

覆盖 5 个 CRUD 端点的"主路径 + 错误路径",并验证:

- 路由前置守卫 vs service 异常翻译双轨划分
- ``X-Workspace-Id`` header 强制纪律
- 跨工作区视为不存在的安全语义(404)
- ``GET /timeline`` 按 ``time_order`` 升序的单调断言
  (PROJECT_SPEC §4.2 [HARD] 单调校验由 Reviewer commit 前批量执行,
  本 service 层允许 ``time_order`` 重复 / 乱序写入,只验证 list 返回顺序
  按 store 既定排序方向。)

复用 P2-B-14 / P2-B-15 fixture 模板(``WorkspaceService`` + in-memory
``shared_session`` + ``_SessionProxy`` close-no-op)。

错误码覆盖矩阵
==============

| 错误码 | 来源 |
|--------|------|
| ``MISSING_WORKSPACE_HEADER`` (422) | 路由前置依赖 |
| ``TIMELINE_ENTRY_WORKSPACE_MISMATCH`` (422) | 路由前置守卫 |
| ``TIMELINE_ENTRY_ID_MISMATCH`` (422) | 路由前置守卫(PUT) |
| ``TIMELINE_ENTRY_NOT_FOUND`` (404) | service 翻译 |
| ``TIMELINE_ENTRY_ALREADY_EXISTS`` (409) | service 翻译 |
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

from src.api.deps import get_timeline_service
from src.api.main import create_app
from src.core.db import Base, WorkspaceORM
from src.core.models.timeline_entry import TimelineEntry
from src.services.timeline_service import TimelineEntryService
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
    """转发 ``Session`` 调用但 ``close`` 不真关。沿用 P2-B-14 / P2-B-15 模板。"""

    def __init__(self, inner: Session) -> None:
        self._inner = inner

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)

    def close(self) -> None:
        pass


@pytest.fixture()
def timeline_service(
    workspace_service: WorkspaceService, shared_session: Session
) -> TimelineEntryService:
    def factory(_workspace_id: str) -> Session:
        return _SessionProxy(shared_session)  # type: ignore[return-value]

    return TimelineEntryService(
        workspace_service=workspace_service, session_factory=factory
    )


@pytest.fixture()
def client(timeline_service: TimelineEntryService) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_timeline_service] = lambda: timeline_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---- 工厂 -------------------------------------------------------------------


def _entry(
    *,
    entry_id: str = "tl_001",
    workspace_id: str = "ws-demo",
    chapter_id: str = "v1c003",
    description: str = "魔尊之子血脉觉醒",
    time_label: str = "建元三年春",
    time_order: int = 100,
    involved_characters: list[str] | None = None,
    **overrides: Any,
) -> TimelineEntry:
    payload: dict[str, Any] = {
        "entry_id": entry_id,
        "workspace_id": workspace_id,
        "chapter_id": chapter_id,
        "description": description,
        "time_label": time_label,
        "time_order": time_order,
        "involved_characters": involved_characters or ["villain_001"],
    }
    payload.update(overrides)
    return TimelineEntry(**payload)


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
# 1. GET /timeline(列表)
# =============================================================================


class TestList:
    def test_list_empty_returns_empty_array(
        self, client: TestClient, prepared: str
    ) -> None:
        resp = client.get("/api/v1/timeline", headers=_hdr())
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"] == []
        assert "error" not in body

    def test_list_returns_workspace_entries_sorted_by_time_order(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        # 先插入 time_order 较大的(tl_late, 300),再插入更小的(tl_mid, 200)
        # 和最小的(tl_early, 100),验证 list 严格按 time_order 升序返回。
        for tid, order in [("tl_late", 300), ("tl_mid", 200), ("tl_early", 100)]:
            client.post(
                "/api/v1/timeline",
                headers=_hdr(),
                json=_entry(entry_id=tid, time_order=order).model_dump(
                    mode="json"
                ),
            )
        resp = client.get("/api/v1/timeline", headers=_hdr())
        assert resp.status_code == 200
        rows = resp.json()["data"]
        ids = [r["entry_id"] for r in rows]
        orders = [r["time_order"] for r in rows]
        assert ids == ["tl_early", "tl_mid", "tl_late"]
        # 单调断言:严格升序
        assert all(orders[i] < orders[i + 1] for i in range(len(orders) - 1))

    def test_list_time_order_ties_broken_by_entry_id(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        """``time_order`` 相同的两条记录,按 ``entry_id`` 二次排序。

        Reviewer 单调校验(P2-B-02)只关心 commit 前的最终状态;允许中间态
        ``time_order`` 重复,本测试验证 store 排序稳定性沿用到 API 层。
        """
        for tid in ["tl_b", "tl_a", "tl_c"]:
            client.post(
                "/api/v1/timeline",
                headers=_hdr(),
                json=_entry(entry_id=tid, time_order=500).model_dump(
                    mode="json"
                ),
            )
        resp = client.get("/api/v1/timeline", headers=_hdr())
        assert resp.status_code == 200
        ids = [r["entry_id"] for r in resp.json()["data"]]
        assert ids == ["tl_a", "tl_b", "tl_c"]

    def test_list_isolates_by_workspace(
        self,
        client: TestClient,
        prepared: str,
        prepared_other: str,
    ) -> None:
        client.post(
            "/api/v1/timeline",
            headers=_hdr("ws-demo"),
            json=_entry(entry_id="tl_demo").model_dump(mode="json"),
        )
        client.post(
            "/api/v1/timeline",
            headers=_hdr("ws-other"),
            json=_entry(
                entry_id="tl_other", workspace_id="ws-other"
            ).model_dump(mode="json"),
        )
        demo = client.get("/api/v1/timeline", headers=_hdr("ws-demo")).json()
        other = client.get(
            "/api/v1/timeline", headers=_hdr("ws-other")
        ).json()
        assert [r["entry_id"] for r in demo["data"]] == ["tl_demo"]
        assert [r["entry_id"] for r in other["data"]] == ["tl_other"]

    def test_list_missing_workspace_header_returns_422(
        self, client: TestClient
    ) -> None:
        resp = client.get("/api/v1/timeline")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"

    def test_list_unknown_workspace_returns_422(
        self, client: TestClient
    ) -> None:
        resp = client.get("/api/v1/timeline", headers=_hdr("ws-nope"))
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "WORKSPACE_INVALID"


# =============================================================================
# 2. GET /timeline/{id}
# =============================================================================


class TestGet:
    def test_get_round_trip(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        client.post(
            "/api/v1/timeline",
            headers=_hdr(),
            json=_entry().model_dump(mode="json"),
        )
        resp = client.get("/api/v1/timeline/tl_001", headers=_hdr())
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["entry_id"] == "tl_001"
        assert data["chapter_id"] == "v1c003"
        assert data["time_label"] == "建元三年春"
        assert data["time_order"] == 100
        assert data["involved_characters"] == ["villain_001"]

    def test_get_missing_returns_404(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        resp = client.get("/api/v1/timeline/tl_999", headers=_hdr())
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "TIMELINE_ENTRY_NOT_FOUND"

    def test_get_cross_workspace_treated_as_not_found(
        self,
        client: TestClient,
        prepared: str,
        prepared_other: str,
    ) -> None:
        client.post(
            "/api/v1/timeline",
            headers=_hdr("ws-demo"),
            json=_entry().model_dump(mode="json"),
        )
        # ws-other 视图下 tl_001 不存在
        resp = client.get("/api/v1/timeline/tl_001", headers=_hdr("ws-other"))
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "TIMELINE_ENTRY_NOT_FOUND"

    def test_get_missing_workspace_header_returns_422(
        self, client: TestClient
    ) -> None:
        resp = client.get("/api/v1/timeline/tl_001")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"


# =============================================================================
# 3. POST /timeline
# =============================================================================


class TestCreate:
    def test_create_returns_201_and_persists(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        body = _entry().model_dump(mode="json")
        resp = client.post("/api/v1/timeline", headers=_hdr(), json=body)
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["entry_id"] == "tl_001"
        get_resp = client.get("/api/v1/timeline/tl_001", headers=_hdr())
        assert get_resp.status_code == 200

    def test_create_duplicate_returns_409(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        body = _entry().model_dump(mode="json")
        client.post("/api/v1/timeline", headers=_hdr(), json=body)
        resp = client.post("/api/v1/timeline", headers=_hdr(), json=body)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "TIMELINE_ENTRY_ALREADY_EXISTS"

    def test_create_workspace_mismatch_returns_422(
        self,
        client: TestClient,
        prepared: str,
        prepared_other: str,
    ) -> None:
        body = _entry(workspace_id="ws-other").model_dump(mode="json")
        resp = client.post(
            "/api/v1/timeline", headers=_hdr("ws-demo"), json=body
        )
        assert resp.status_code == 422
        assert (
            resp.json()["error"]["code"] == "TIMELINE_ENTRY_WORKSPACE_MISMATCH"
        )

    def test_create_missing_workspace_header_returns_422(
        self, client: TestClient
    ) -> None:
        body = _entry().model_dump(mode="json")
        resp = client.post("/api/v1/timeline", json=body)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"

    def test_create_invalid_body_returns_422(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        # 缺必填字段 description
        body = _entry().model_dump(mode="json")
        del body["description"]
        resp = client.post("/api/v1/timeline", headers=_hdr(), json=body)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"

    def test_create_global_id_collision_with_other_workspace_returns_409(
        self,
        client: TestClient,
        prepared: str,
        prepared_other: str,
    ) -> None:
        """``entry_id`` 是 SQLite 全局主键;另一工作区已占用 → 409。"""
        client.post(
            "/api/v1/timeline",
            headers=_hdr("ws-demo"),
            json=_entry(workspace_id="ws-demo").model_dump(mode="json"),
        )
        body = _entry(workspace_id="ws-other").model_dump(mode="json")
        resp = client.post(
            "/api/v1/timeline", headers=_hdr("ws-other"), json=body
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "TIMELINE_ENTRY_ALREADY_EXISTS"


# =============================================================================
# 4. PUT /timeline/{id}
# =============================================================================


class TestUpdate:
    def test_update_round_trip(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        client.post(
            "/api/v1/timeline",
            headers=_hdr(),
            json=_entry().model_dump(mode="json"),
        )
        updated = _entry(
            description="主角觉醒(改写后)",
            time_label="建元四年夏",
            time_order=150,
        ).model_dump(mode="json")
        resp = client.put(
            "/api/v1/timeline/tl_001", headers=_hdr(), json=updated
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["description"] == "主角觉醒(改写后)"
        assert data["time_label"] == "建元四年夏"
        assert data["time_order"] == 150
        # 二次 GET 验证持久化
        got = client.get("/api/v1/timeline/tl_001", headers=_hdr()).json()
        assert got["data"]["time_order"] == 150

    def test_update_missing_returns_404(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        body = _entry(entry_id="ghost").model_dump(mode="json")
        resp = client.put(
            "/api/v1/timeline/ghost", headers=_hdr(), json=body
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "TIMELINE_ENTRY_NOT_FOUND"

    def test_update_id_mismatch_returns_422(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        client.post(
            "/api/v1/timeline",
            headers=_hdr(),
            json=_entry().model_dump(mode="json"),
        )
        body = _entry(entry_id="other_id").model_dump(mode="json")
        resp = client.put(
            "/api/v1/timeline/tl_001", headers=_hdr(), json=body
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "TIMELINE_ENTRY_ID_MISMATCH"

    def test_update_workspace_mismatch_returns_422(
        self,
        client: TestClient,
        prepared: str,
        prepared_other: str,
    ) -> None:
        client.post(
            "/api/v1/timeline",
            headers=_hdr("ws-demo"),
            json=_entry().model_dump(mode="json"),
        )
        body = _entry(workspace_id="ws-other").model_dump(mode="json")
        resp = client.put(
            "/api/v1/timeline/tl_001", headers=_hdr("ws-demo"), json=body
        )
        assert resp.status_code == 422
        assert (
            resp.json()["error"]["code"] == "TIMELINE_ENTRY_WORKSPACE_MISMATCH"
        )

    def test_update_cross_workspace_target_returns_404(
        self,
        client: TestClient,
        prepared: str,
        prepared_other: str,
    ) -> None:
        """ws-demo 下创建 tl_001;ws-other 头 + body 改它 → 404。

        路由层 body.workspace_id=ws-other / header=ws-other 一致(无 422),
        但 store 内 tl_001.workspace_id=ws-demo → service 视为不存在 → 404。
        """
        client.post(
            "/api/v1/timeline",
            headers=_hdr("ws-demo"),
            json=_entry(workspace_id="ws-demo").model_dump(mode="json"),
        )
        body = _entry(workspace_id="ws-other").model_dump(mode="json")
        resp = client.put(
            "/api/v1/timeline/tl_001", headers=_hdr("ws-other"), json=body
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "TIMELINE_ENTRY_NOT_FOUND"

    def test_update_missing_workspace_header_returns_422(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        body = _entry().model_dump(mode="json")
        resp = client.put("/api/v1/timeline/tl_001", json=body)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"


# =============================================================================
# 5. DELETE /timeline/{id}
# =============================================================================


class TestDelete:
    def test_delete_round_trip(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        client.post(
            "/api/v1/timeline",
            headers=_hdr(),
            json=_entry().model_dump(mode="json"),
        )
        resp = client.delete("/api/v1/timeline/tl_001", headers=_hdr())
        assert resp.status_code == 200
        assert resp.json()["data"] == {"entry_id": "tl_001"}
        # 二次 GET 应 404
        got = client.get("/api/v1/timeline/tl_001", headers=_hdr())
        assert got.status_code == 404

    def test_delete_missing_returns_404(
        self,
        client: TestClient,
        prepared: str,
    ) -> None:
        resp = client.delete("/api/v1/timeline/ghost", headers=_hdr())
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "TIMELINE_ENTRY_NOT_FOUND"

    def test_delete_cross_workspace_returns_404(
        self,
        client: TestClient,
        prepared: str,
        prepared_other: str,
    ) -> None:
        client.post(
            "/api/v1/timeline",
            headers=_hdr("ws-demo"),
            json=_entry().model_dump(mode="json"),
        )
        resp = client.delete(
            "/api/v1/timeline/tl_001", headers=_hdr("ws-other")
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "TIMELINE_ENTRY_NOT_FOUND"
        # 原工作区数据未受影响
        got = client.get(
            "/api/v1/timeline/tl_001", headers=_hdr("ws-demo")
        )
        assert got.status_code == 200

    def test_delete_missing_workspace_header_returns_422(
        self, client: TestClient
    ) -> None:
        resp = client.delete("/api/v1/timeline/tl_001")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MISSING_WORKSPACE_HEADER"


# =============================================================================
# 6. OpenAPI
# =============================================================================


class TestOpenAPI:
    def test_openapi_includes_all_timeline_routes(
        self, client: TestClient
    ) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        assert "/api/v1/timeline" in paths
        assert "/api/v1/timeline/{entry_id}" in paths
        assert "get" in paths["/api/v1/timeline"]
        assert "post" in paths["/api/v1/timeline"]
        ops = paths["/api/v1/timeline/{entry_id}"]
        assert "get" in ops
        assert "put" in ops
        assert "delete" in ops
