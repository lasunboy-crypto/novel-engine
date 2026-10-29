"""核心状态只读审计 API。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.api.deps import get_core_state_service, get_workspace_service
from src.api.main import create_app
from src.config import settings
from src.core.db import Base
from src.core.db.models import CoreStateRevisionORM, WorkspaceORM
from src.core.models.core_state import (
    advance_book_time_projection,
    build_typed_book_clock_constraint,
)
from src.services.core_state_service import CoreStateService
from src.services.workspace_service import WorkspaceService


@pytest.fixture
def workspace_service(tmp_path: Path) -> WorkspaceService:
    service = WorkspaceService(
        workspaces_root=tmp_path / "workspaces",
        archives_root=tmp_path / "archives",
    )
    service.create(
        "ws-core-api",
        display_name="核心状态 API",
        genre="test",
        premise="核心状态审计",
        activate=False,
    )
    return service


@pytest.fixture
def session_factory() -> Callable[[str], Session]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add(
            WorkspaceORM(
                workspace_id="ws-core-api",
                display_name="核心状态 API",
                genre="test",
                premise="核心状态审计",
                created_at=now,
                last_active_at=now,
                engine_version="test",
                status="active",
                total_chapters=12,
                completed_chapters=1,
                config_overrides={},
            )
        )
        session.add(
            CoreStateRevisionORM(
                revision_id="csr-api-mirror",
                workspace_id="ws-core-api",
                state_key="object:青铜镜:surface",
                entity_type="object",
                entity_id="青铜镜",
                attribute="surface",
                value_json={"value": "无锈无氧化"},
                value_text="无锈无氧化",
                source_kind="approved_text",
                source_chapter_id="ch-0001",
                source_version_id=1,
                source_path="chapter_summaries/ch-0001.core_state_candidates",
                source_text_hash="a" * 64,
                evidence_quote="青铜镜镜面无锈无氧化。",
                valid_from_chapter=1,
                valid_to_chapter=None,
                supersedes_revision_id=None,
                status="active",
                change_reason="首次建立",
                ledger_hash="b" * 64,
                created_at=now,
                retracted_at=None,
            )
        )
        session.commit()

    def factory(_workspace_id: str) -> Session:
        return Session(engine)

    return factory


@pytest.fixture
def client(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setattr(settings, "data_dir", workspace_service.workspaces_root)
    app = create_app()
    core_service = CoreStateService(session_factory=session_factory)
    app.dependency_overrides[get_workspace_service] = lambda: workspace_service
    app.dependency_overrides[get_core_state_service] = lambda: core_service
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_get_core_state_audit_exposes_current_source_and_evidence(
    client: TestClient,
) -> None:
    response = client.get(
        "/api/v1/chapters/core-state",
        headers={"X-Workspace-Id": "ws-core-api"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["projection"]["facts"] == [
        {
            "revision_id": "csr-api-mirror",
            "state_key": "object:青铜镜:surface",
            "entity_type": "object",
            "entity_id": "青铜镜",
            "attribute": "surface",
            "value": "无锈无氧化",
            "source_kind": "approved_text",
            "source_chapter_id": "ch-0001",
            "source_version_id": 1,
            "source_path": "chapter_summaries/ch-0001.core_state_candidates",
            "source_text_hash": "a" * 64,
            "evidence_quote": "青铜镜镜面无锈无氧化。",
            "valid_from_chapter": 1,
            "valid_to_chapter": None,
        }
    ]
    assert data["items"][0]["status"] == "active"
    assert data["items"][0]["reason"] == "首次建立"


def test_get_core_state_audit_exposes_additive_typed_natural_book_time(
    client: TestClient,
    session_factory: Callable[[str], Session],
) -> None:
    opening = build_typed_book_clock_constraint(
        chapter_id="ch-0001",
        time_gap="开篇",
        previous_projection=None,
        previous_projection_hash="0" * 64,
        is_first_chapter=True,
    )
    opening_projection = advance_book_time_projection(
        previous_projection=None,
        constraint=opening,
    )
    approximate = build_typed_book_clock_constraint(
        chapter_id="ch-0002",
        time_gap="约一小时后",
        previous_projection=opening_projection,
        previous_projection_hash="1" * 64,
        is_first_chapter=False,
    )
    projection = advance_book_time_projection(
        previous_projection=opening_projection,
        constraint=approximate,
    )
    row = CoreStateService._new_revision(
        workspace_id="ws-core-api",
        state_key="book_clock:main:elapsed_minutes",
        entity_type="book_clock",
        entity_id="main",
        attribute="elapsed_minutes",
        value_json={
            "clock_contract_version": 1,
            "known": False,
            "book_time": projection.model_dump(mode="json"),
        },
        value_text="ordered_relative:约一小时后",
        source_kind="chapter_card",
        chapter_id="ch-0002",
        source_version_id=1,
        source_path="chapter_cards/ch-0002.book_clock_constraint",
        source_text_hash="c" * 64,
        evidence_quote="约一小时后",
        seq=2,
        supersedes_revision_id=None,
        change_reason="typed book time commit",
    )
    with session_factory("ws-core-api") as session:
        session.add(row)
        session.commit()

    response = client.get(
        "/api/v1/chapters/core-state",
        headers={"X-Workspace-Id": "ws-core-api"},
    )

    assert response.status_code == 200
    typed = response.json()["data"]["projection"]
    assert typed["book_time_contract_version"] == 1
    assert typed["book_clock"] is None
    assert typed["book_time"]["current_node"] == {
        "node_id": approximate.node_id,
        "chapter_id": "ch-0002",
        "kind": "ordered_relative",
        "raw_time_gap": "约一小时后",
        "anchor_node_id": opening.node_id,
        "relations": ["after"],
        "day_phase": None,
        "approximate_quantity": {"value": 1, "unit": "hour"},
        "gap_interval": None,
        "absolute_interval": None,
        "unanchored_reason": None,
    }
    assert typed["book_time"]["latest_absolute_anchor"]["node_id"] == opening.node_id
