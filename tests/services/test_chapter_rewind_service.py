"""ChapterRewindService 单测(P6-B-01)。

覆盖维度(对齐 plan §10 测试约定 5 类):

- **happy**:rewind / force-pass / unblock / unblock-all 走通 + ORM 状态翻转 +
  WS event publish + Chroma soft-delete 调用 + extracted_* revoked_at 写入
- **error**:rewind 不允许源 → ChapterRewindInvalidStateError;force-pass
  ``invalidated`` 源 → ChapterInvalidatedNeedsUnblockError;unblock 非
  ``invalidated`` 源 → ChapterUnblockInvalidStateError;章节不存在 → 通用
  ChapterRewindServiceError
- **gate**:无 session_factory 注入 → ChapterRewindServiceError 抛
- **rollback**:Chroma soft-delete 失败时,DB 已 commit 不撤回(best-effort
  + structlog),vector_revoked_count=0 但 invalidated_chapter_ids 仍正确
- **WS event**:每 happy path 配套 ws_manager.publish 调用断言

不依赖真 LLM / 真 Chroma(``FakeVectorStore`` + ``FakeWSManager`` 注入);
不依赖物理 fixture(``tmp_path``)。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.core.db import Base
from src.core.agents.chapter_summary_extractor import (
    ContinuityLedgerValidationError,
)
from src.core.db.models import (
    AttemptLedgerORM,
    CharacterORM,
    ChapterORM,
    ChapterVersionORM,
    CoreStateRevisionORM,
    PlotThreadORM,
    TimelineEntryORM,
    WorkspaceORM,
)
from src.core.llm.mock_client import MockLLMClient
from src.core.llm.attempt_context import run_observed_provider_attempt
from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage
from src.core.memory.vector import VectorChapterSnapshot
from src.core.models.outline import CharacterOutlineItem, PlotLongThread
from src.core.models.writer_profile import WriterProfile
from src.core.orchestrator.chapter_orchestrator import MemoryExtractionOutcome
from src.core.routing.router import RoutedTarget
from src.services.attempt_ledger import wrap_router_with_attempt_tracking
from src.services.chapter_operation_lease import ChapterOperationLeaseStore
from src.services.chapter_rewind_service import (
    ChapterInvalidatedNeedsUnblockError,
    ChapterRewindInvalidStateError,
    ChapterRewindService,
    ChapterRewindServiceError,
    ChapterUnblockInvalidStateError,
    ForcePassContinuityLedgerNotReadyError,
    REWIND_REWRITE_REQUIRED_REASON,
)
from src.services.memory_enrichment_checkpoint import (
    memory_enrichment_requires_manual_recovery,
    validate_memory_enrichment_checkpoint_shape,
)
from src.services.outline_entity_recovery import OutlineEntityBaselines
from src.services.workspace_service import WorkspaceService


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeVectorStore:
    """单测用 fake;记录 soft_delete_by_chapter_id 调用 + 返参数化数量。"""

    def __init__(
        self,
        *,
        revoked_count: int = 2,
        raise_exc: Exception | None = None,
        chapter_vector_count: int = 0,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.count_calls: list[tuple[str, str]] = []
        self._revoked_count = revoked_count
        self._raise_exc = raise_exc
        self.chapter_vector_count = chapter_vector_count
        self.snapshot_revision = 0

    def soft_delete_by_chapter_id(
        self, collection: str, *, chapter_id: str
    ) -> int:
        self.calls.append((collection, chapter_id))
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._revoked_count

    def count_by_chapter_id(
        self, collection: str, *, chapter_id: str
    ) -> int:
        self.count_calls.append((collection, chapter_id))
        return self.chapter_vector_count

    def snapshot_by_chapter_id(
        self,
        collection: str,
        *,
        chapter_id: str,
    ) -> VectorChapterSnapshot:
        self.count_calls.append((collection, chapter_id))
        digest = hashlib.sha256(
            f"{self.chapter_vector_count}:{self.snapshot_revision}".encode()
        ).hexdigest()
        return VectorChapterSnapshot(
            count=self.chapter_vector_count,
            digest=digest,
        )


class _MeteredProviderClient(LLMClient):
    """本地计量假 client；触发 attempt observer，但绝不访问网络。"""

    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self, messages: list[Message], **gen_params: Any
    ) -> LLMResponse:
        del messages, gen_params
        self.calls += 1
        return run_observed_provider_attempt(
            lambda: LLMResponse(
                content="{}",
                model_used="priced-memory",
                usage=TokenUsage(input_tokens=120, output_tokens=30),
                finish_reason="end_turn",
            ),
            technical_attempt=1,
        )


class _MeteredRawRouter:
    """模拟 API factory 返回的 attempt-tracking 之前的原始 router。"""

    def __init__(self, client: LLMClient) -> None:
        self._client = client
        self._profile = WriterProfile(
            profile_id="priced-memory",
            provider="mock",
            model="priced-memory",
            context_window=16_000,
            cost_per_1k_input=0.01,
            cost_per_1k_output=0.02,
        )

    def route(
        self,
        agent_name: str,
        *,
        chapter_card: Any = None,
        override_profile_id: str | None = None,
    ) -> RoutedTarget:
        del agent_name, chapter_card, override_profile_id
        return RoutedTarget(
            profile=self._profile,
            client=self._client,
            complete_kwargs={},
        )


class _FakeWSManager:
    """记录所有 publish 调用,供断言。"""

    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self._raise_exc = raise_exc

    def publish(self, *, channel: str, event: str, data: dict[str, Any]) -> None:
        if self._raise_exc is not None:
            raise self._raise_exc
        self.events.append({"channel": channel, "event": event, "data": data})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace_service(tmp_path: Path) -> WorkspaceService:
    workspaces_root = tmp_path / "workspaces"
    archives_root = tmp_path / "archives"
    workspaces_root.mkdir()
    archives_root.mkdir()
    return WorkspaceService(
        workspaces_root=workspaces_root, archives_root=archives_root
    )


@pytest.fixture
def workspace_id(workspace_service: WorkspaceService) -> str:
    workspace_service.create(
        "ws-rewind-test",
        display_name="P6-B-01 rewind service test",
        genre="xianxia",
        premise="test premise",
        activate=False,
    )
    return "ws-rewind-test"


@pytest.fixture
def session_factory() -> Callable[[str], Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    def factory(workspace_id: str) -> Session:
        return Session(engine)

    return factory


def _seed_workspace_orm(
    session_factory: Callable[[str], Session], workspace_id: str
) -> None:
    """在新 session 内 seed WorkspaceORM 满足 chapters/extracted_* FK。"""
    session = session_factory(workspace_id)
    try:
        now = datetime.now(timezone.utc)
        session.merge(
            WorkspaceORM(
                workspace_id=workspace_id,
                display_name="x",
                genre="xianxia",
                premise="p",
                created_at=now,
                last_active_at=now,
                engine_version="0.1.0",
                status="active",
                total_chapters=10,
                completed_chapters=0,
                config_overrides={},
            )
        )
        session.commit()
    finally:
        session.close()


def _seed_chapter(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    chapter_id: str,
    status: str = "approved",
) -> None:
    """seed ChapterORM 行。"""
    session = session_factory(workspace_id)
    try:
        now = datetime.now(timezone.utc)
        session.merge(
            ChapterORM(
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                current_version=1,
                status=status,
                rewrite_count=0,
                revise_count=0,
                created_at=now,
                approved_at=now if status == "approved" else None,
                total_token_usage={},
                total_cost_usd=0.0,
            )
        )
        session.commit()
    finally:
        session.close()


def _seed_chapter_version(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    chapter_id: str,
    *,
    content: str,
    version_id: int = 1,
) -> None:
    """seed 产品 force-pass 已存在的不可变正文版本真相源。"""

    session = session_factory(workspace_id)
    try:
        session.add(
            ChapterVersionORM(
                chapter_id=chapter_id,
                version_id=version_id,
                content=content,
                word_count=len(content),
                created_at=datetime.now(timezone.utc),
                creation_type="draft",
                model_used="mock",
                review_score=None,
                notes="",
                style_fingerprint=None,
            )
        )
        session.commit()
    finally:
        session.close()


def _force_pass_service(
    *,
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    **kwargs: Any,
) -> ChapterRewindService:
    """构造带离线 Mock ledger 抽取能力的 force-pass service。"""

    return ChapterRewindService(
        workspace_service=workspace_service,
        session_factory=session_factory,
        llm_client=MockLLMClient(),
        llm_model="mock-default",
        **kwargs,
    )


def _seed_character(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    character_id: str,
    first_appearance_chapter: str,
) -> None:
    session = session_factory(workspace_id)
    try:
        session.merge(
            CharacterORM(
                character_id=character_id,
                workspace_id=workspace_id,
                name=character_id,
                aliases=[],
                archetype="stub",
                appearance="",
                personality="",
                background="",
                voice_profile={"tone": "neutral"},
                goals_long_term=[],
                goals_short_term=[],
                current_state={},
                relationships={},
                abilities=[],
                secrets=[],
                first_appearance_chapter=first_appearance_chapter,
                last_updated_chapter=None,
            )
        )
        session.commit()
    finally:
        session.close()


def _seed_plot_thread(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    thread_id: str,
    planted_chapter: str,
) -> None:
    session = session_factory(workspace_id)
    try:
        now = datetime.now(timezone.utc)
        session.merge(
            PlotThreadORM(
                thread_id=thread_id,
                workspace_id=workspace_id,
                title=thread_id,
                status="planted",
                importance=3,
                planted_chapter=planted_chapter,
                planned_payoff=None,
                actual_payoff=None,
                description="",
                related_characters=[],
                created_at=now,
                last_updated_chapter=None,
            )
        )
        session.commit()
    finally:
        session.close()


def _seed_timeline_entry(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    entry_id: str,
    chapter_id: str,
) -> None:
    session = session_factory(workspace_id)
    try:
        session.merge(
            TimelineEntryORM(
                entry_id=entry_id,
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                description="",
                time_label="day 1",
                time_order=1,
                involved_characters=[],
            )
        )
        session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


class TestRewindHappyPath:
    def test_rewind_retracts_later_core_revision_and_reopens_predecessor(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """Rewind 与章节状态同事务撤回核心真值，不留幽灵新值。"""

        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "approved")
        _seed_chapter(session_factory, workspace_id, "ch-0002", "approved")
        now = datetime.now(timezone.utc)
        state_key = "object:青铜镜:surface"
        session = session_factory(workspace_id)
        try:
            ch1 = session.get(ChapterORM, "ch-0001")
            ch2 = session.get(ChapterORM, "ch-0002")
            assert ch1 is not None and ch2 is not None
            ch1.core_state_commit_hash = "1" * 64
            ch1.core_state_commit_status = "committed"
            ch2.core_state_commit_hash = "2" * 64
            ch2.core_state_commit_status = "committed"
            first = CoreStateRevisionORM(
                revision_id="csr-clean",
                workspace_id=workspace_id,
                state_key=state_key,
                entity_type="object",
                entity_id="青铜镜",
                attribute="surface",
                value_json={"value": "无锈无氧化"},
                value_text="无锈无氧化",
                source_kind="approved_text",
                source_chapter_id="ch-0001",
                source_version_id=1,
                source_path="chapter_summaries/ch-0001.core",
                source_text_hash="a" * 64,
                evidence_quote="镜面无锈无氧化。",
                valid_from_chapter=1,
                valid_to_chapter=1,
                supersedes_revision_id=None,
                status="superseded",
                change_reason="首次建立",
                ledger_hash="c" * 64,
                created_at=now,
                retracted_at=None,
            )
            second = CoreStateRevisionORM(
                revision_id="csr-rust",
                workspace_id=workspace_id,
                state_key=state_key,
                entity_type="object",
                entity_id="青铜镜",
                attribute="surface",
                value_json={"value": "暗绿色铜锈"},
                value_text="暗绿色铜锈",
                source_kind="approved_text",
                source_chapter_id="ch-0002",
                source_version_id=1,
                source_path="chapter_summaries/ch-0002.core",
                source_text_hash="b" * 64,
                evidence_quote="镜面覆上暗绿色铜锈。",
                valid_from_chapter=2,
                valid_to_chapter=None,
                supersedes_revision_id="csr-clean",
                status="active",
                change_reason="获准变化",
                ledger_hash="d" * 64,
                created_at=now,
                retracted_at=None,
            )
            session.add_all([first, second])
            session.commit()
        finally:
            session.close()

        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda _workspace_id: _FakeVectorStore(),
        )
        service.rewind(workspace_id, "ch-0002")

        session = session_factory(workspace_id)
        try:
            first = session.get(CoreStateRevisionORM, "csr-clean")
            second = session.get(CoreStateRevisionORM, "csr-rust")
            ch2 = session.get(ChapterORM, "ch-0002")
            assert first is not None and second is not None and ch2 is not None
            assert (first.status, first.valid_to_chapter) == ("active", None)
            assert second.status == "retracted"
            assert second.retracted_at is not None
            assert ch2.core_state_commit_status == "retracted"
            assert ch2.core_state_commit_hash == ""
        finally:
            session.close()

    def test_rewind_target_to_drafting_subsequent_to_invalidated(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        for cid, status in [
            ("ch-0001", "approved"),
            ("ch-0002", "approved"),
            ("ch-0003", "approved"),
            ("ch-0004", "reviewing"),
            ("ch-0005", "drafting"),
        ]:
            _seed_chapter(session_factory, workspace_id, cid, status)

        vs = _FakeVectorStore(revoked_count=3)
        ws = _FakeWSManager()
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda ws_id: vs,
            ws_manager=ws,
        )

        result = service.rewind(workspace_id, "ch-0003")

        assert result.chapter_id == "ch-0003"
        assert result.from_status == "approved"
        assert result.to_status == "drafting"
        assert result.invalidated_chapter_ids == ["ch-0004", "ch-0005"]
        assert result.vector_revoked_count == 3
        assert result.extracted_revoked_count == 0  # 无 extracted 行

        # ORM 验证
        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterORM, "ch-0003")
            assert row is not None
            assert row.status == "drafting"
            assert row.approved_at is None
            assert row.escalation_details is not None
            assert (
                row.escalation_details["reason"]
                == REWIND_REWRITE_REQUIRED_REASON
            )
            assert row.escalation_details["source_version_id"] == 1
            assert row.escalation_details["source_status"] == "approved"
            for cid in ("ch-0004", "ch-0005"):
                r = session.get(ChapterORM, cid)
                assert r is not None
                assert r.status == "invalidated"
                assert r.escalation_details is not None
                assert (
                    r.escalation_details["reason"]
                    == REWIND_REWRITE_REQUIRED_REASON
                )
                assert r.escalation_details["source_version_id"] == 1
            # 上游(ch-0001 / ch-0002)不受影响
            for cid in ("ch-0001", "ch-0002"):
                r = session.get(ChapterORM, cid)
                assert r is not None
                assert r.status == "approved"
        finally:
            session.close()

        # Chroma soft-delete 调用
        assert vs.calls == [("memories", "ch-0003")]

        # WS publish 3 事件:1 chapter_rewound + 2 chapter_invalidated
        events = [e["event"] for e in ws.events]
        assert events == ["chapter_rewound", "chapter_invalidated", "chapter_invalidated"]
        rewound = ws.events[0]
        assert rewound["data"]["chapter_id"] == "ch-0003"
        assert rewound["data"]["invalidated_chapter_ids"] == ["ch-0004", "ch-0005"]
        assert rewound["data"]["vector_revoked_count"] == 3

    def test_rewind_writes_extracted_revoked_at(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0003", "approved")
        _seed_character(
            session_factory, workspace_id, "ling-yan", "ch-0003"
        )
        _seed_character(
            session_factory, workspace_id, "su-li", "ch-0002"
        )  # 上游章节不受影响
        _seed_plot_thread(
            session_factory, workspace_id, "thread-a", "ch-0003"
        )
        _seed_timeline_entry(
            session_factory, workspace_id, "entry-1", "ch-0003"
        )

        vs = _FakeVectorStore()
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda ws_id: vs,
        )

        result = service.rewind(workspace_id, "ch-0003")
        assert result.extracted_revoked_count == 3  # ling-yan + thread-a + entry-1

        # ORM 实证
        session = session_factory(workspace_id)
        try:
            ling = session.get(CharacterORM, "ling-yan")
            assert ling.revoked_at is not None
            suli = session.get(CharacterORM, "su-li")
            assert suli.revoked_at is None  # 上游不影响
            thread = session.get(PlotThreadORM, "thread-a")
            assert thread.revoked_at is not None
            entry = session.get(TimelineEntryORM, "entry-1")
            assert entry.revoked_at is not None
        finally:
            session.close()

    def test_rewind_restores_outline_entities_and_revokes_only_extracted(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """首次于目标章入正文的书级实体恢复基线，不被当作正文新实体软删。"""

        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0003", "approved")
        _seed_character(
            session_factory, workspace_id, "outline-support", "ch-0003"
        )
        _seed_character(
            session_factory, workspace_id, "emergent-support", "ch-0003"
        )
        _seed_plot_thread(
            session_factory, workspace_id, "outline-thread", "ch-0003"
        )
        _seed_plot_thread(
            session_factory, workspace_id, "emergent-thread", "ch-0003"
        )
        _seed_timeline_entry(
            session_factory, workspace_id, "entry-target", "ch-0003"
        )
        session = session_factory(workspace_id)
        try:
            outline_character = session.get(CharacterORM, "outline-support")
            outline_thread = session.get(PlotThreadORM, "outline-thread")
            emergent_thread = session.get(PlotThreadORM, "emergent-thread")
            assert outline_character is not None
            assert outline_thread is not None
            assert emergent_thread is not None
            outline_character.name = "正文污染名"
            outline_character.last_updated_chapter = "ch-0003"
            outline_thread.description = "正文污染描述"
            outline_thread.last_updated_chapter = "ch-0003"
            outline_thread.origin = "outline"
            emergent_thread.origin = "emergent"
            session.commit()
        finally:
            session.close()

        baselines = OutlineEntityBaselines(
            characters={
                "outline-support": CharacterOutlineItem(
                    character_id="outline-support",
                    name="书级配角",
                    role="supporting",
                    archetype="向导",
                    appearance="红围巾",
                    personality="寡言",
                    background="书级背景",
                    motivation="寻找亲人",
                    voice_hint="短句",
                    relationships_brief={},
                )
            },
            plot_threads={
                "outline-thread": PlotLongThread(
                    thread_id="outline-thread",
                    title="书级伏笔",
                    description="大纲基线描述",
                )
            },
        )
        monkeypatch.setattr(
            "src.services.chapter_rewind_service.load_outline_entity_baselines",
            lambda **_kwargs: baselines,
        )
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda _workspace_id: _FakeVectorStore(),
        )

        result = service.rewind(workspace_id, "ch-0003")

        # 两个正文新实体 + timeline 被撤销；两个书级实体只恢复基线。
        assert result.extracted_revoked_count == 3
        session = session_factory(workspace_id)
        try:
            outline_character = session.get(CharacterORM, "outline-support")
            emergent_character = session.get(CharacterORM, "emergent-support")
            outline_thread = session.get(PlotThreadORM, "outline-thread")
            emergent_thread = session.get(PlotThreadORM, "emergent-thread")
            entry = session.get(TimelineEntryORM, "entry-target")
            assert outline_character is not None
            assert outline_character.name == "书级配角"
            assert outline_character.first_appearance_chapter is None
            assert outline_character.last_updated_chapter is None
            assert outline_character.revoked_at is None
            assert emergent_character is not None
            assert emergent_character.revoked_at is not None
            assert outline_thread is not None
            assert outline_thread.description == "大纲基线描述"
            assert outline_thread.planted_chapter is None
            assert outline_thread.last_updated_chapter is None
            assert outline_thread.revoked_at is None
            assert emergent_thread is not None
            assert emergent_thread.revoked_at is not None
            assert entry is not None
            assert entry.revoked_at is not None
        finally:
            session.close()

    def test_rewind_with_no_subsequent_chapters(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """rewind 最后一章:invalidated_chapter_ids 空。"""
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "approved")

        vs = _FakeVectorStore()
        ws = _FakeWSManager()
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda ws_id: vs,
            ws_manager=ws,
        )
        result = service.rewind(workspace_id, "ch-0001")
        assert result.invalidated_chapter_ids == []
        events = [e["event"] for e in ws.events]
        assert events == ["chapter_rewound"]


class TestForcePassHappyPath:
    def test_force_pass_writes_force_pass_fields(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, "ch-0001", "human_intervention"
        )
        _write_chapter_files(
            workspace_service, workspace_id, "ch-0001", with_card=True
        )

        ws = _FakeWSManager()
        service = _force_pass_service(
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=ws,
        )

        result = service.force_pass(
            workspace_id, "ch-0001", reason="rewrite 配额耗尽,手动强制通过"
        )
        assert result.chapter_id == "ch-0001"
        assert result.from_status == "human_intervention"
        assert result.to_status == "approved"
        assert isinstance(result.force_pass_at, datetime)
        assert result.force_pass_reason == "rewrite 配额耗尽,手动强制通过"

        # ORM 验证
        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row.status == "approved"
            assert row.approved_at is not None
            assert row.force_pass_at is not None
            assert row.force_pass_reason == "rewrite 配额耗尽,手动强制通过"
            assert row.ready_for_next_chapter_at is not None
        finally:
            session.close()

        # WS event
        assert len(ws.events) == 1
        e = ws.events[0]
        assert e["event"] == "chapter_force_passed"
        assert e["data"]["from_status"] == "human_intervention"
        assert e["data"]["force_pass_reason"] == "rewrite 配额耗尽,手动强制通过"


class TestCompletedChaptersCounterOnRewindAndForcePass:
    """A-03 hot-fix:rewind / force-pass 同步翻 ``workspace.completed_chapters``。

    - rewind:target_approved + subsequent_approved 数 → 计数 -N(floor 0)
    - force-pass:from_status 必非 approved/published(白名单),计数 +1
    """

    def test_rewind_decrements_workspace_counter(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """rewind ch-0003(approved)+ ch-0004/0005(approved/approved) →
        target -1 + subsequents -2 = 总 -3。"""
        _seed_workspace_orm(session_factory, workspace_id)
        # seed 完成数 = 3(模拟前置 +1×3)
        session = session_factory(workspace_id)
        try:
            ws_row = session.get(WorkspaceORM, workspace_id)
            ws_row.completed_chapters = 3
            session.commit()
        finally:
            session.close()
        for cid, status in [
            ("ch-0003", "approved"),
            ("ch-0004", "approved"),
            ("ch-0005", "approved"),
        ]:
            _seed_chapter(session_factory, workspace_id, cid, status)

        vs = _FakeVectorStore()
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda ws_id: vs,
        )
        service.rewind(workspace_id, "ch-0003")

        session = session_factory(workspace_id)
        try:
            ws_row = session.get(WorkspaceORM, workspace_id)
            assert ws_row.completed_chapters == 0  # 3 - 3 = 0
        finally:
            session.close()

    def test_rewind_decrement_floors_at_zero(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """完成数已 0 时 rewind 不下溢到负数。"""
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "approved")
        vs = _FakeVectorStore()
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda ws_id: vs,
        )
        service.rewind(workspace_id, "ch-0001")
        session = session_factory(workspace_id)
        try:
            ws_row = session.get(WorkspaceORM, workspace_id)
            assert ws_row.completed_chapters == 0  # max(0, 0-1)
        finally:
            session.close()

    def test_rewind_from_reviewing_does_not_decrement_target(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """target 是 reviewing(非 approved)→ target 不减;subsequent 仍按 approved 减。"""
        _seed_workspace_orm(session_factory, workspace_id)
        session = session_factory(workspace_id)
        try:
            ws_row = session.get(WorkspaceORM, workspace_id)
            ws_row.completed_chapters = 1
            session.commit()
        finally:
            session.close()
        _seed_chapter(session_factory, workspace_id, "ch-0001", "reviewing")
        _seed_chapter(session_factory, workspace_id, "ch-0002", "approved")
        vs = _FakeVectorStore()
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda ws_id: vs,
        )
        service.rewind(workspace_id, "ch-0001")
        session = session_factory(workspace_id)
        try:
            ws_row = session.get(WorkspaceORM, workspace_id)
            assert ws_row.completed_chapters == 0  # 1 - 0 (target) - 1 (subseq) = 0
        finally:
            session.close()

    def test_force_pass_increments_workspace_counter(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """force-pass 源状态非 approved → 计数 +1。"""
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, "ch-0001", "human_intervention"
        )
        _write_chapter_files(
            workspace_service, workspace_id, "ch-0001", with_card=True
        )
        service = _force_pass_service(
            workspace_service=workspace_service,
            session_factory=session_factory,
        )
        service.force_pass(workspace_id, "ch-0001", reason="manual force")
        session = session_factory(workspace_id)
        try:
            ws_row = session.get(WorkspaceORM, workspace_id)
            assert ws_row.completed_chapters == 1
        finally:
            session.close()

    def test_force_pass_syncs_meta_json(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """v1.0.x A2-03 续修:force-pass 也同步 meta.json completed_chapters +1。"""
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, "ch-0001", "human_intervention"
        )
        _write_chapter_files(
            workspace_service, workspace_id, "ch-0001", with_card=True
        )
        service = _force_pass_service(
            workspace_service=workspace_service,
            session_factory=session_factory,
        )
        service.force_pass(workspace_id, "ch-0001", reason="manual force")
        # meta.json 应同步 +1
        assert workspace_service.info(workspace_id).completed_chapters == 1

    def test_force_pass_marks_final_md(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """v1.0.x reviewer-resilience(2026-05-20):问题4 修法 — force_pass 后
        把 v{current_version}.md 复制成 final.md(让 ExportService 能读到正文)。
        """
        from src.core.workspace.workspace_paths import WorkspacePaths

        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, "ch-0001", "human_intervention"
        )
        _write_chapter_files(
            workspace_service,
            workspace_id,
            "ch-0001",
            content="第一章正文真实内容",
            with_card=True,
        )
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )

        service = _force_pass_service(
            workspace_service=workspace_service,
            session_factory=session_factory,
        )
        service.force_pass(workspace_id, "ch-0001", reason="manual force")

        # final.md 应被生成且内容等同 v1.md
        final_path = paths.chapter_version_file("ch-0001", "final.md")
        assert final_path.is_file()
        assert final_path.read_text(encoding="utf-8") == "第一章正文真实内容"

    def test_force_pass_missing_source_version_keeps_ready_closed(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """R4:v{current_version}.md 缺失时批准状态可留痕，但 ready 必须为空。"""
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, "ch-0001", "human_intervention"
        )
        # 故意不 seed v1.md 物理文件 — mark_final 将失败

        service = _force_pass_service(
            workspace_service=workspace_service,
            session_factory=session_factory,
        )
        with pytest.raises(
            ForcePassContinuityLedgerNotReadyError,
            match="final 版本标定失败",
        ):
            service.force_pass(
                workspace_id, "ch-0001", reason="manual force"
            )
        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row.status == "human_intervention"
            assert row.ready_for_next_chapter_at is None
        finally:
            session.close()

    def test_rewind_syncs_meta_json(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """v1.0.x A2-03 续修:rewind 也同步 meta.json completed_chapters -N。"""
        _seed_workspace_orm(session_factory, workspace_id)
        # 预先把 ORM + meta 都设到 3,模拟"已写 3 章 approved"
        workspace_service.sync_completed_chapters_to_meta(workspace_id, delta=3)
        session = session_factory(workspace_id)
        try:
            ws_row = session.get(WorkspaceORM, workspace_id)
            ws_row.completed_chapters = 3
            session.commit()
        finally:
            session.close()
        for cid in ("ch-0001", "ch-0002", "ch-0003"):
            _seed_chapter(session_factory, workspace_id, cid, "approved")

        vs = _FakeVectorStore()
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda ws_id: vs,
        )
        # rewind ch-0001 → target -1 + subsequent ch-0002/0003 -2 = -3
        service.rewind(workspace_id, "ch-0001")

        # meta.json 应同步从 3 → 0
        assert workspace_service.info(workspace_id).completed_chapters == 0

    def test_rewind_meta_floor_zero(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """v1.0.x A2-03 续修:meta.json 已是 0 时 rewind 不下溢。"""
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "approved")
        # meta 维持 0(create 默认),ORM 也是 0
        vs = _FakeVectorStore()
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda ws_id: vs,
        )
        service.rewind(workspace_id, "ch-0001")
        assert workspace_service.info(workspace_id).completed_chapters == 0

    def test_rewind_from_reviewing_no_meta_change(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """v1.0.x A2-03 续修:target=reviewing 时 target_loss=0,subsequent 都没,
        total_loss=0 → meta 不动。"""
        _seed_workspace_orm(session_factory, workspace_id)
        workspace_service.sync_completed_chapters_to_meta(workspace_id, delta=2)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "reviewing")

        vs = _FakeVectorStore()
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda ws_id: vs,
        )
        service.rewind(workspace_id, "ch-0001")
        # meta 不变(target 是 reviewing 非 approved,无 subsequent)
        assert workspace_service.info(workspace_id).completed_chapters == 2


class TestUnblockHappyPath:
    def test_unblock_invalidated_to_drafting(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "invalidated")

        ws = _FakeWSManager()
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=ws,
        )

        result = service.unblock(workspace_id, "ch-0001")
        assert result.from_status == "invalidated"
        assert result.to_status == "drafting"

        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row.status == "drafting"
        finally:
            session.close()

        assert len(ws.events) == 1
        assert ws.events[0]["event"] == "chapter_unblocked"

    def test_unblock_all_empty_workspace_is_200_idempotent(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """自决项 2:空状态调用 200 + count=0 幂等(对齐 P3-B-12 batch 模式)。"""
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "approved")  # 非 invalidated

        ws = _FakeWSManager()
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=ws,
        )

        result = service.unblock_all(workspace_id)
        assert result.unblocked_count == 0
        assert result.unblocked_chapter_ids == []
        assert len(ws.events) == 0  # 没有事件推送

    def test_unblock_all_batch_unblocks_all_invalidated(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "approved")
        _seed_chapter(session_factory, workspace_id, "ch-0002", "invalidated")
        _seed_chapter(session_factory, workspace_id, "ch-0003", "invalidated")

        ws = _FakeWSManager()
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=ws,
        )

        result = service.unblock_all(workspace_id)
        assert result.unblocked_count == 2
        assert result.unblocked_chapter_ids == ["ch-0002", "ch-0003"]
        events = [e["event"] for e in ws.events]
        assert events == ["chapter_unblocked", "chapter_unblocked"]


# ---------------------------------------------------------------------------
# error path
# ---------------------------------------------------------------------------


class TestRewindErrorCases:
    def test_rewind_range_blocks_when_any_subsequent_memory_worker_running(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """后续任一章 Worker 先赢时，整笔 rewind 在 DB/Chroma 前回滚。"""

        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "approved")
        _seed_chapter(session_factory, workspace_id, "ch-0002", "approved")
        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterORM, "ch-0002")
            assert row is not None
            row.memory_enrichment_status = "running"
            row.memory_enrichment_details = {
                "started_at": "2026-08-02T12:30:11+00:00",
                "tasks": [],
                "lease_id": "worker-won",
            }
            session.commit()
        finally:
            session.close()

        vector = _FakeVectorStore()
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda _workspace_id: vector,
        )
        with pytest.raises(ChapterRewindServiceError, match="running"):
            service.rewind(workspace_id, "ch-0001")

        session = session_factory(workspace_id)
        try:
            target = session.get(ChapterORM, "ch-0001")
            subsequent = session.get(ChapterORM, "ch-0002")
            assert target is not None
            assert subsequent is not None
            assert target.status == "approved"
            assert subsequent.status == "approved"
            assert subsequent.memory_enrichment_status == "running"
        finally:
            session.close()
        assert vector.calls == []

    def test_rewind_from_drafting_raises_invalid_state(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "drafting")

        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
        )
        with pytest.raises(ChapterRewindInvalidStateError) as exc_info:
            service.rewind(workspace_id, "ch-0001")
        assert exc_info.value.chapter_id == "ch-0001"
        assert exc_info.value.current_status == "drafting"
        assert "drafting" not in exc_info.value.allowed_sources

    def test_rewind_from_invalidated_raises_invalid_state(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, "ch-0001", "invalidated"
        )

        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
        )
        with pytest.raises(ChapterRewindInvalidStateError):
            service.rewind(workspace_id, "ch-0001")

    def test_rewind_chapter_not_found_raises_generic(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
        )
        with pytest.raises(ChapterRewindServiceError, match="不存在"):
            service.rewind(workspace_id, "ch-nonexistent")


class TestForcePassErrorCases:
    def test_force_pass_invalidated_raises_needs_unblock(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, "ch-0001", "invalidated"
        )
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
        )
        with pytest.raises(ChapterInvalidatedNeedsUnblockError):
            service.force_pass(
                workspace_id, "ch-0001", reason="test reason"
            )

    def test_force_pass_already_approved_raises_invalid_state(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "approved")
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
        )
        with pytest.raises(ChapterRewindServiceError, match="不允许 force-pass"):
            service.force_pass(workspace_id, "ch-0001", reason="test")


class TestUnblockErrorCases:
    def test_unblock_non_invalidated_raises_invalid_state(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "drafting")

        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
        )
        with pytest.raises(ChapterUnblockInvalidStateError) as exc_info:
            service.unblock(workspace_id, "ch-0001")
        assert exc_info.value.current_status == "drafting"


# ---------------------------------------------------------------------------
# gate(无 session_factory 注入)
# ---------------------------------------------------------------------------


class TestGate:
    def test_rewind_without_session_factory_raises(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        service = ChapterRewindService(
            workspace_service=workspace_service,
        )
        with pytest.raises(ChapterRewindServiceError, match="session_factory"):
            service.rewind(workspace_id, "ch-0001")

    def test_force_pass_without_session_factory_raises(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        service = ChapterRewindService(
            workspace_service=workspace_service,
        )
        with pytest.raises(ChapterRewindServiceError, match="session_factory"):
            service.force_pass(workspace_id, "ch-0001", reason="r")

    def test_unblock_without_session_factory_raises(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        service = ChapterRewindService(
            workspace_service=workspace_service,
        )
        with pytest.raises(ChapterRewindServiceError, match="session_factory"):
            service.unblock(workspace_id, "ch-0001")


# ---------------------------------------------------------------------------
# rollback / best-effort
# ---------------------------------------------------------------------------


class TestRollbackAndBestEffort:
    def test_rewind_continues_when_chroma_fails(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """Chroma soft-delete 失败 → DB commit 已生效不撤回(best-effort)。
        result.vector_revoked_count=0,WS event 仍正常 publish。"""
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "approved")
        _seed_chapter(session_factory, workspace_id, "ch-0002", "approved")

        vs = _FakeVectorStore(raise_exc=RuntimeError("chroma down"))
        ws = _FakeWSManager()
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda ws_id: vs,
            ws_manager=ws,
        )

        result = service.rewind(workspace_id, "ch-0001")
        assert result.vector_revoked_count == 0  # Chroma 失败 → 返 0
        assert result.invalidated_chapter_ids == ["ch-0002"]  # DB 已生效

        # ORM 验证 DB commit 完整
        session = session_factory(workspace_id)
        try:
            assert session.get(ChapterORM, "ch-0001").status == "drafting"
            assert session.get(ChapterORM, "ch-0002").status == "invalidated"
        finally:
            session.close()

        # WS event 仍 publish
        events = [e["event"] for e in ws.events]
        assert "chapter_rewound" in events

    def test_rewind_continues_when_ws_publish_fails(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        """WS publish 失败 → swallow + structlog,不影响主流程返回值。"""
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "approved")

        ws = _FakeWSManager(raise_exc=RuntimeError("ws disconnect"))
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=ws,
        )

        # 不应抛
        result = service.rewind(workspace_id, "ch-0001")
        assert result.to_status == "drafting"

    def test_rewind_no_vector_store_factory_returns_zero(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", "approved")

        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            # 无 vector_store_factory
        )
        result = service.rewind(workspace_id, "ch-0001")
        assert result.vector_revoked_count == 0


# ---------------------------------------------------------------------------
# WS event payload
# ---------------------------------------------------------------------------


class TestWSEventPayload:
    def test_rewind_event_payload_complete(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0003", "approved")
        _seed_chapter(session_factory, workspace_id, "ch-0004", "approved")

        vs = _FakeVectorStore(revoked_count=5)
        ws = _FakeWSManager()
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda ws_id: vs,
            ws_manager=ws,
        )
        service.rewind(workspace_id, "ch-0003")

        rewound = ws.events[0]
        assert rewound["channel"] == "chapter.ch-0003.events"
        assert rewound["event"] == "chapter_rewound"
        data = rewound["data"]
        assert data["chapter_id"] == "ch-0003"
        assert data["workspace_id"] == workspace_id
        assert data["from_status"] == "approved"
        assert data["to_status"] == "drafting"
        assert data["invalidated_chapter_ids"] == ["ch-0004"]
        assert data["vector_revoked_count"] == 5

        invalidated = ws.events[1]
        assert invalidated["channel"] == "chapter.ch-0004.events"
        assert invalidated["event"] == "chapter_invalidated"
        assert invalidated["data"]["trigger_chapter_id"] == "ch-0003"
        assert invalidated["data"]["to_status"] == "invalidated"


# ---------------------------------------------------------------------------
# v1.1 acceptance 修复2:孤儿章人工解决流程
# ---------------------------------------------------------------------------


def _write_chapter_files(
    workspace_service: WorkspaceService,
    workspace_id: str,
    chapter_id: str,
    *,
    content: str = "原 LLM 第一版正文。" * 30,
    with_card: bool = False,
) -> Path:
    """在工作区文件系统落 v1.md(+ 可选章节卡 JSON),返回章节目录。"""
    from src.core.workspace.workspace_paths import WorkspacePaths

    paths = WorkspacePaths(
        workspace_id, workspaces_root=workspace_service.workspaces_root
    )
    chapter_dir = paths.chapter_dir(chapter_id)
    chapter_dir.mkdir(parents=True, exist_ok=True)
    (chapter_dir / "v1.md").write_text(content, encoding="utf-8")
    if with_card:
        from src.core.models.chapter_card import ChapterCard, ContinuityContext

        card = ChapterCard(
            chapter_id=chapter_id,
            workspace_id=workspace_id,
            volume_id="v01",
            chapter_number=int(chapter_id.rsplit("-", 1)[-1]),
            title="测试章",
            tension_level=5,
            word_count_min=1,
            word_count_max=10**8,
            plot_node_budget=3,
            must_achieve=["测试 must"],
            must_unresolved=[],
            forbidden=[],
            pov_character="主角",
            setting="测试场景",
            continuity_context=ContinuityContext(
                prev_chapter_tail="上一章结尾。",
                unfinished_actions=[],
                time_gap="立即续接",
                scene_continuation="switch",
                pov_change=False,
                pov_from="主角",
                pov_to="主角",
            ),
            next_chapter_seed="下一章种子",
            arc_phase="rising",
            is_arc_finale=False,
            related_plot_threads=[],
            related_characters=["主角"],
            review_threshold=7.5,
            revise_threshold=6.5,
            max_rewrites=3,
            max_revises=2,
        )
        paths.cards_dir.mkdir(parents=True, exist_ok=True)
        (paths.cards_dir / f"{chapter_id}.json").write_text(
            card.model_dump_json(), encoding="utf-8"
        )
    return chapter_dir


class TestUpdateContentManual:
    """update_content_manual(孤儿章人工编辑正文)。"""

    def test_happy_human_intervention_writes_new_version_and_final(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, "ch-0007", status="human_intervention"
        )
        chapter_dir = _write_chapter_files(
            workspace_service, workspace_id, "ch-0007"
        )
        ws = _FakeWSManager()
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            ws_manager=ws,
        )

        edited = "人工修复后的正文。" * 40
        result = service.update_content_manual(
            workspace_id, "ch-0007", content=edited
        )

        # 新版本文件 + final 指针
        assert result.chapter_id == "ch-0007"
        assert result.version_id == 2
        assert (chapter_dir / "v2.md").read_text(encoding="utf-8") == edited
        assert (chapter_dir / "final.md").read_text(encoding="utf-8") == edited
        # LLM 旧版保留
        assert (chapter_dir / "v1.md").exists()
        # current_version 同步(force_pass 按它标 final,不同步会覆盖人工编辑)
        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterORM, "ch-0007")
            assert row is not None
            assert row.current_version == 2
            # 状态不变 — 批准是 force_pass 的职责
            assert row.status == "human_intervention"
        finally:
            session.close()
        # WS 事件
        assert any(
            e["event"] == "chapter_content_updated" for e in ws.events
        )

    def test_rejects_non_human_intervention_status(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(session_factory, workspace_id, "ch-0001", status="approved")
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
        )
        with pytest.raises(ChapterRewindServiceError) as exc_info:
            service.update_content_manual(
                workspace_id, "ch-0001", content="不应被写入"
            )
        assert "不允许人工编辑" in str(exc_info.value)

    def test_rejects_missing_chapter_row(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _seed_workspace_orm(session_factory, workspace_id)
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
        )
        with pytest.raises(ChapterRewindServiceError) as exc_info:
            service.update_content_manual(
                workspace_id, "ch-0099", content="x"
            )
        assert "不存在" in str(exc_info.value)

    def test_requires_session_factory(
        self, workspace_service: WorkspaceService, workspace_id: str
    ) -> None:
        service = ChapterRewindService(workspace_service=workspace_service)
        with pytest.raises(ChapterRewindServiceError):
            service.update_content_manual(workspace_id, "ch-0001", content="x")


class TestForcePassFullExtract:
    """force_pass 复用 reviewer-pass 的持久富记忆 Worker。"""

    def test_force_pass_invokes_full_extract_when_deps_injected(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """成功：pending→running→succeeded，来源和 Worker 终态均持久化。"""
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, "ch-0007", status="human_intervention"
        )
        content = "人工裁决后的最终正文。" * 30
        _write_chapter_files(
            workspace_service,
            workspace_id,
            "ch-0007",
            content=content,
            with_card=True,
        )
        _seed_chapter_version(
            session_factory,
            workspace_id,
            "ch-0007",
            content=content,
        )
        captured: dict[str, Any] = {}

        def _spy_extract(**kwargs: Any) -> MemoryExtractionOutcome:
            captured.update(kwargs)
            return MemoryExtractionOutcome(
                succeeded=True,
                failure_stage=None,
                vector_write_started=False,
                sqlite_rollback_succeeded=True,
                result_unknown=False,
            )

        monkeypatch.setattr(
            "src.services.chapter_service._extract_memories_safely",
            _spy_extract,
        )
        vs = _FakeVectorStore()
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda ws_id: vs,
            llm_client=MockLLMClient(),
            llm_model="mock-default",
        )

        result = service.force_pass(
            workspace_id, "ch-0007", reason="人工裁决批准"
        )

        assert result.to_status == "approved"
        assert captured, "完整 extract 未被调用"
        assert "人工裁决后的最终正文" in captured[
            "final_path"
        ].read_text(encoding="utf-8")
        assert captured["chapter_card"].chapter_id == "ch-0007"
        assert captured["workspace_id"] == workspace_id
        with session_factory(workspace_id) as session:
            row = session.get(ChapterORM, "ch-0007")
            assert row is not None
            assert row.memory_enrichment_status == "succeeded"
            validate_memory_enrichment_checkpoint_shape(
                status=row.memory_enrichment_status,
                details=row.memory_enrichment_details,
                enriched_at=row.memory_enriched_at,
            )
            assert isinstance(row.memory_enrichment_details, dict)
            assert row.memory_enrichment_details["trigger"] == "force_pass"
            assert any(
                task.get("name") == "memory_extractor"
                and task.get("status") == "succeeded"
                for task in row.memory_enrichment_details["tasks"]
            )

    def test_force_pass_records_known_memory_failure_as_safe_degraded(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """已知零副作用失败：明确 failed，但不误写成人工隔离现场。"""

        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, "ch-0008", status="human_intervention"
        )
        content = "人工裁决后的已知失败样本。" * 30
        _write_chapter_files(
            workspace_service,
            workspace_id,
            "ch-0008",
            content=content,
            with_card=True,
        )
        _seed_chapter_version(
            session_factory,
            workspace_id,
            "ch-0008",
            content=content,
        )
        monkeypatch.setattr(
            "src.services.chapter_service._extract_memories_safely",
            lambda **_kwargs: MemoryExtractionOutcome(
                succeeded=False,
                failure_stage="extract",
                vector_write_started=False,
                sqlite_rollback_succeeded=True,
                result_unknown=False,
            ),
        )
        service = _force_pass_service(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda _workspace_id: _FakeVectorStore(),
        )

        result = service.force_pass(
            workspace_id, "ch-0008", reason="人工裁决批准"
        )

        assert result.to_status == "approved"
        with session_factory(workspace_id) as session:
            row = session.get(ChapterORM, "ch-0008")
            assert row is not None
            assert row.memory_enrichment_status == "failed"
            validate_memory_enrichment_checkpoint_shape(
                status=row.memory_enrichment_status,
                details=row.memory_enrichment_details,
                enriched_at=row.memory_enriched_at,
            )
            details = row.memory_enrichment_details
            assert isinstance(details, dict)
            assert details["result_unknown"] is False
            assert details["manual_recovery_required"] is False
            assert details["safe_to_continue"] is True
            assert details["failure_disposition"] == "known_failure_degraded"
            assert not memory_enrichment_requires_manual_recovery(
                status=row.memory_enrichment_status,
                details=details,
            )

    def test_force_pass_memory_tracks_usage_cost_once_and_uses_named_cap(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """attempt raw router 外包 cost；一次响应只累计一次且 cap 非 magic。"""

        chapter_id = "ch-0006"
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, chapter_id, status="human_intervention"
        )
        content = "人工裁决后的计量样本。" * 30
        _write_chapter_files(
            workspace_service,
            workspace_id,
            chapter_id,
            content=content,
            with_card=True,
        )
        _seed_chapter_version(
            session_factory,
            workspace_id,
            chapter_id,
            content=content,
        )
        ledger = type(
            "ValidatedLedger",
            (),
            {
                "ledger_hash": "a" * 64,
                "source_text_hash": "b" * 64,
            },
        )()
        monkeypatch.setattr(
            "src.services.chapter_rewind_service.load_validated_continuity_ledger",
            lambda **_kwargs: ledger,
        )
        monkeypatch.setattr(
            "src.services.chapter_service.load_validated_continuity_ledger",
            lambda **_kwargs: ledger,
        )
        monkeypatch.setattr(
            "src.services.chapter_service.persist_continuity_ledger_vector_safely",
            lambda **_kwargs: True,
        )
        client = _MeteredProviderClient()

        def _router_factory(requested_workspace_id: str) -> Any:
            return wrap_router_with_attempt_tracking(
                _MeteredRawRouter(client),  # type: ignore[arg-type]
                workspace_id=requested_workspace_id,
                session_factory=session_factory,
            )

        def _metered_extract(**kwargs: Any) -> MemoryExtractionOutcome:
            router = kwargs["router"]
            assert router is not None
            target = router.route("memory_extractor")
            response = target.client.complete(
                [Message(role="user", content="本地计量假请求")],
                **target.complete_kwargs,
            )
            assert response.usage == TokenUsage(
                input_tokens=120,
                output_tokens=30,
            )
            return MemoryExtractionOutcome(
                succeeded=True,
                failure_stage=None,
                vector_write_started=False,
                sqlite_rollback_succeeded=True,
                result_unknown=False,
            )

        monkeypatch.setattr(
            "src.services.chapter_service._extract_memories_safely",
            _metered_extract,
        )
        monkeypatch.setattr(
            "src.services.chapter_rewind_service.DEFAULT_PROVIDER_CALL_HARD_CAP",
            7,
        )
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda _workspace_id: _FakeVectorStore(),
            router_factory=_router_factory,
            operation_lease_store=ChapterOperationLeaseStore(session_factory),
        )

        result = service.force_pass(
            workspace_id, chapter_id, reason="人工裁决批准"
        )

        assert result.to_status == "approved"
        assert client.calls == 1
        with session_factory(workspace_id) as session:
            row = session.get(ChapterORM, chapter_id)
            assert row is not None
            assert row.total_token_usage == {
                "priced-memory": {
                    "input_tokens": 120,
                    "output_tokens": 30,
                }
            }
            assert row.total_cost_usd == pytest.approx(0.0018)
            attempts = list(
                session.scalars(
                    select(AttemptLedgerORM).where(
                        AttemptLedgerORM.workspace_id == workspace_id,
                        AttemptLedgerORM.chapter_id == chapter_id,
                        AttemptLedgerORM.attempt_kind == "provider",
                    )
                )
            )
            assert len(attempts) == 1
            assert attempts[0].status == "succeeded"
            assert attempts[0].attempt_cap == 7

    def test_force_pass_first_ledger_tracks_usage_and_cost_once(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """首次 ledger 抽取：raw attempt router 同样外包 cost，且不双计。"""

        chapter_id = "ch-0005"
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, chapter_id, status="human_intervention"
        )
        content = "人工裁决后的首次小账本样本。" * 30
        _write_chapter_files(
            workspace_service,
            workspace_id,
            chapter_id,
            content=content,
            with_card=True,
        )
        _seed_chapter_version(
            session_factory,
            workspace_id,
            chapter_id,
            content=content,
        )
        ledger = type(
            "ValidatedLedger",
            (),
            {
                "ledger_hash": "c" * 64,
                "source_text_hash": "d" * 64,
            },
        )()

        def _missing_ledger(**_kwargs: Any) -> None:
            raise ContinuityLedgerValidationError("尚无连续性小账本")

        client = _MeteredProviderClient()

        def _router_factory(requested_workspace_id: str) -> Any:
            return wrap_router_with_attempt_tracking(
                _MeteredRawRouter(client),  # type: ignore[arg-type]
                workspace_id=requested_workspace_id,
                session_factory=session_factory,
            )

        def _extract_ledger_once(**kwargs: Any) -> Any:
            router = kwargs["router"]
            assert router is not None
            target = router.route("chapter_summary_extractor")
            response = target.client.complete(
                [Message(role="user", content="本地首次小账本计量假请求")],
                **target.complete_kwargs,
            )
            assert response.usage == TokenUsage(
                input_tokens=120,
                output_tokens=30,
            )
            return ledger

        monkeypatch.setattr(
            "src.services.chapter_rewind_service.load_validated_continuity_ledger",
            _missing_ledger,
        )
        monkeypatch.setattr(
            "src.services.chapter_rewind_service.extract_and_persist_continuity_ledger",
            _extract_ledger_once,
        )
        monkeypatch.setattr(
            "src.services.chapter_service.load_validated_continuity_ledger",
            lambda **_kwargs: ledger,
        )
        monkeypatch.setattr(
            "src.services.chapter_service.persist_continuity_ledger_vector_safely",
            lambda **_kwargs: True,
        )
        monkeypatch.setattr(
            "src.services.chapter_service._extract_memories_safely",
            lambda **_kwargs: MemoryExtractionOutcome(
                succeeded=True,
                failure_stage=None,
                vector_write_started=False,
                sqlite_rollback_succeeded=True,
                result_unknown=False,
            ),
        )
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda _workspace_id: _FakeVectorStore(),
            router_factory=_router_factory,
            operation_lease_store=ChapterOperationLeaseStore(session_factory),
        )

        result = service.force_pass(
            workspace_id, chapter_id, reason="人工裁决批准"
        )

        assert result.to_status == "approved"
        assert client.calls == 1
        with session_factory(workspace_id) as session:
            row = session.get(ChapterORM, chapter_id)
            assert row is not None
            assert row.total_token_usage == {
                "priced-memory": {
                    "input_tokens": 120,
                    "output_tokens": 30,
                }
            }
            assert row.total_cost_usd == pytest.approx(0.0018)
            attempts = list(
                session.scalars(
                    select(AttemptLedgerORM).where(
                        AttemptLedgerORM.workspace_id == workspace_id,
                        AttemptLedgerORM.chapter_id == chapter_id,
                        AttemptLedgerORM.attempt_kind == "provider",
                    )
                )
            )
            assert len(attempts) == 1
            assert attempts[0].agent_name == "chapter_summary_extractor"
            assert attempts[0].status == "succeeded"

    def test_force_pass_started_attempt_survives_crash_and_degrades_without_replay(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """started 请求结果未知：保留 attempt，重启不自动重放 Provider。"""

        chapter_id = "ch-0009"
        succeeded_attempt_id = "att-force-pass-memory-response"
        attempt_id = "att-force-pass-memory-crash"
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, chapter_id, status="human_intervention"
        )
        content = "人工裁决后的进程中断样本。" * 30
        _write_chapter_files(
            workspace_service,
            workspace_id,
            chapter_id,
            content=content,
            with_card=True,
        )
        _seed_chapter_version(
            session_factory,
            workspace_id,
            chapter_id,
            content=content,
        )

        def _crash_with_started_attempt(**_kwargs: Any) -> None:
            with session_factory(workspace_id) as session:
                now = datetime.now(timezone.utc)
                session.add_all(
                    [
                        AttemptLedgerORM(
                            attempt_id=succeeded_attempt_id,
                            workspace_id=workspace_id,
                            run_id=None,
                            task_id="different-task-still-same-chapter",
                            chapter_id=chapter_id,
                            attempt_kind="provider",
                            operation_kind="agent_call",
                            logical_call_id="logical-force-pass-memory-response",
                            agent_name="memory_extractor",
                            provider="different-provider-still-must-preserve",
                            model="different-model-still-must-preserve",
                            attempt_number=1,
                            attempt_cap=3,
                            status="succeeded",
                            version_before=1,
                            version_after=None,
                            error_type=None,
                            created_at=now,
                            completed_at=now,
                        ),
                        AttemptLedgerORM(
                        attempt_id=attempt_id,
                        workspace_id=workspace_id,
                        run_id=None,
                        task_id="task-force-pass-memory-crash",
                        chapter_id=chapter_id,
                        attempt_kind="provider",
                        operation_kind="agent_call",
                        logical_call_id="logical-force-pass-memory-crash",
                        agent_name="plot_thread_merger",
                        provider="mock",
                        model="mock-default",
                        attempt_number=1,
                        attempt_cap=3,
                        status="started",
                        version_before=1,
                        version_after=None,
                        error_type=None,
                        created_at=now + timedelta(microseconds=1),
                        completed_at=None,
                        ),
                    ]
                )
                session.commit()
            raise SystemExit("simulated process crash after request start")

        monkeypatch.setattr(
            "src.services.chapter_service._extract_memories_safely",
            _crash_with_started_attempt,
        )
        vector_store = _FakeVectorStore(chapter_vector_count=0)
        service = _force_pass_service(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda _workspace_id: vector_store,
        )

        with pytest.raises(SystemExit, match="simulated process crash"):
            service.force_pass(
                workspace_id, chapter_id, reason="人工裁决批准"
            )

        with session_factory(workspace_id) as session:
            row = session.get(ChapterORM, chapter_id)
            assert row is not None
            assert row.status == "approved"
            assert row.ready_for_next_chapter_at is not None
            assert row.memory_enrichment_status == "running"
            # live R8 ch3 在 baseline 合同引入前已经进入 running；只用旧版
            # exact 两字段形状覆盖兼容口径，不能从新形状删一个字段来伪装。
            started_at = row.memory_enrichment_details["started_at"]
            row.memory_enrichment_details = {
                "started_at": started_at,
                "tasks": [],
            }
            session.commit()
            validate_memory_enrichment_checkpoint_shape(
                status=row.memory_enrichment_status,
                details=row.memory_enrichment_details,
                enriched_at=row.memory_enriched_at,
            )
            attempt = session.get(AttemptLedgerORM, attempt_id)
            assert attempt is not None
            assert attempt.status == "started"

        with session_factory(workspace_id) as session:
            before = session.get(ChapterORM, chapter_id)
            assert before is not None
            details_before = dict(before.memory_enrichment_details)

        assert service.mark_running_memory_enrichment_as_failed_on_startup(
            workspace_id,
            apply=False,
        ) == [chapter_id]
        with session_factory(workspace_id) as session:
            preflight_row = session.get(ChapterORM, chapter_id)
            assert preflight_row is not None
            assert preflight_row.memory_enrichment_status == "running"
            assert preflight_row.memory_enrichment_details == details_before

        assert service.mark_running_memory_enrichment_as_failed_on_startup(
            workspace_id
        ) == [chapter_id]

        with session_factory(workspace_id) as session:
            row = session.get(ChapterORM, chapter_id)
            assert row is not None
            assert row.memory_enrichment_status == "failed"
            validate_memory_enrichment_checkpoint_shape(
                status=row.memory_enrichment_status,
                details=row.memory_enrichment_details,
                enriched_at=row.memory_enriched_at,
            )
            details = row.memory_enrichment_details
            assert isinstance(details, dict)
            recovery = details["startup_recovery"]
            assert recovery["result_unknown"] is True
            assert recovery["automatic_provider_replay"] is False
            assert recovery["started_provider_attempt_ids"] == [attempt_id]
            assert recovery["unconsumed_provider_attempts"] == [
                {
                    "attempt_id": succeeded_attempt_id,
                    "status": "succeeded",
                    "agent_name": "memory_extractor",
                },
                {
                    "attempt_id": attempt_id,
                    "status": "started",
                    "agent_name": "plot_thread_merger",
                }
            ]
            assert recovery["provider_result_unknown"] is True
            assert recovery["vector_count"] == 0
            assert recovery["vector_quarantined_count"] == 0
            assert recovery["vector_quarantine_succeeded"] is True
            assert recovery["safe_to_continue"] is True
            assert recovery["quarantine_required"] is False
            assert recovery["recovery_disposition"] == (
                "safe_degraded_no_sqlite_or_vector_persist"
            )
            assert not memory_enrichment_requires_manual_recovery(
                status=row.memory_enrichment_status,
                details=details,
            )
            attempt = session.get(AttemptLedgerORM, attempt_id)
            assert attempt is not None
            assert attempt.status == "started"
            succeeded_attempt = session.get(
                AttemptLedgerORM,
                succeeded_attempt_id,
            )
            assert succeeded_attempt is not None
            assert succeeded_attempt.status == "succeeded"

    def test_force_pass_chroma_partial_write_blocks_after_restart(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Chroma 部分写后进程退出：重启保留向量现场并落 blocked_unknown。"""

        chapter_id = "ch-0010"
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, chapter_id, status="human_intervention"
        )
        content = "人工裁决后的向量部分写样本。" * 30
        _write_chapter_files(
            workspace_service,
            workspace_id,
            chapter_id,
            content=content,
            with_card=True,
        )
        _seed_chapter_version(
            session_factory,
            workspace_id,
            chapter_id,
            content=content,
        )
        vector_store = _FakeVectorStore(chapter_vector_count=0)

        def _crash_after_vector_write(**_kwargs: Any) -> None:
            vector_store.chapter_vector_count = 1
            vector_store.snapshot_revision += 1
            raise SystemExit("simulated crash after Chroma partial write")

        monkeypatch.setattr(
            "src.services.chapter_service._extract_memories_safely",
            _crash_after_vector_write,
        )
        service = _force_pass_service(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda _workspace_id: vector_store,
        )

        with pytest.raises(SystemExit, match="Chroma partial write"):
            service.force_pass(
                workspace_id, chapter_id, reason="人工裁决批准"
            )
        assert service.mark_running_memory_enrichment_as_failed_on_startup(
            workspace_id
        ) == [chapter_id]

        with session_factory(workspace_id) as session:
            row = session.get(ChapterORM, chapter_id)
            assert row is not None
            assert row.memory_enrichment_status == "failed"
            validate_memory_enrichment_checkpoint_shape(
                status=row.memory_enrichment_status,
                details=row.memory_enrichment_details,
                enriched_at=row.memory_enriched_at,
            )
            details = row.memory_enrichment_details
            assert isinstance(details, dict)
            recovery = details["startup_recovery"]
            assert recovery["vector_count"] == 1
            assert recovery["vector_baseline_count"] == 0
            assert recovery["vector_side_effect_count"] > 0
            assert recovery["vector_quarantined_count"] == 0
            assert recovery["vector_quarantine_succeeded"] is True
            assert recovery["vector_quarantine_error"] is None
            assert recovery["safe_to_continue"] is False
            assert recovery["quarantine_required"] is True
            assert recovery["recovery_disposition"] == "blocked_uncertain"
            assert memory_enrichment_requires_manual_recovery(
                status=row.memory_enrichment_status,
                details=details,
            )

    def test_running_baseline_allows_preexisting_local_memory_when_unchanged(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """已有本章记忆/向量不等于本轮副作用；内容摘要相等即可降级。"""

        chapter_id = "ch-0011"
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, chapter_id, status="human_intervention"
        )
        _seed_character(
            session_factory,
            workspace_id,
            "legacy-character",
            chapter_id,
        )
        with session_factory(workspace_id) as session:
            row = session.get(ChapterORM, chapter_id)
            assert row is not None
            row.scene_anchor = {
                "location": "旧驿站",
                "time": "黄昏",
                "present_characters": ["legacy-character"],
            }
            session.commit()
        content = "已有合法记忆的中断样本。" * 30
        _write_chapter_files(
            workspace_service,
            workspace_id,
            chapter_id,
            content=content,
            with_card=True,
        )
        _seed_chapter_version(
            session_factory,
            workspace_id,
            chapter_id,
            content=content,
        )
        vector_store = _FakeVectorStore(chapter_vector_count=1)
        monkeypatch.setattr(
            "src.services.chapter_service._extract_memories_safely",
            lambda **_kwargs: (_ for _ in ()).throw(
                SystemExit("simulated crash before local persist")
            ),
        )
        service = _force_pass_service(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda _workspace_id: vector_store,
        )

        with pytest.raises(SystemExit, match="before local persist"):
            service.force_pass(workspace_id, chapter_id, reason="人工裁决批准")
        assert service.mark_running_memory_enrichment_as_failed_on_startup(
            workspace_id
        ) == [chapter_id]

        with session_factory(workspace_id) as session:
            row = session.get(ChapterORM, chapter_id)
            assert row is not None
            recovery = row.memory_enrichment_details["startup_recovery"]
            assert recovery["sqlite_baseline"]["character_first_appearance"] == 1
            assert recovery["sqlite_baseline"]["scene_anchor"] == 1
            assert recovery["vector_baseline_count"] == 1
            assert recovery["sqlite_side_effect_count"] == 0
            assert recovery["vector_side_effect_count"] == 0
            assert recovery["safe_to_continue"] is True
            assert recovery["recovery_disposition"] == (
                "safe_degraded_no_new_sqlite_or_vector_persist"
            )
            validate_memory_enrichment_checkpoint_shape(
                status=row.memory_enrichment_status,
                details=row.memory_enrichment_details,
                enriched_at=row.memory_enriched_at,
            )
            assert not memory_enrichment_requires_manual_recovery(
                status=row.memory_enrichment_status,
                details=row.memory_enrichment_details,
            )

    @pytest.mark.parametrize("damage", ["sqlite_content", "vector_content"])
    def test_running_baseline_blocks_same_count_content_change(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        monkeypatch: pytest.MonkeyPatch,
        damage: str,
    ) -> None:
        """计数不变但 SQLite/Chroma 内容被覆盖，仍必须 blocked_uncertain。"""

        chapter_id = "ch-0012"
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, chapter_id, status="human_intervention"
        )
        _seed_character(
            session_factory,
            workspace_id,
            "same-count-character",
            chapter_id,
        )
        content = "同数量内容覆盖的中断样本。" * 30
        _write_chapter_files(
            workspace_service,
            workspace_id,
            chapter_id,
            content=content,
            with_card=True,
        )
        _seed_chapter_version(
            session_factory,
            workspace_id,
            chapter_id,
            content=content,
        )
        vector_store = _FakeVectorStore(chapter_vector_count=1)

        def _crash_after_same_count_change(**_kwargs: Any) -> None:
            if damage == "sqlite_content":
                with session_factory(workspace_id) as session:
                    character = session.get(CharacterORM, "same-count-character")
                    assert character is not None
                    character.personality = "本轮中断前已被覆盖"
                    session.commit()
            else:
                vector_store.snapshot_revision += 1
            raise SystemExit("simulated same-count content change")

        monkeypatch.setattr(
            "src.services.chapter_service._extract_memories_safely",
            _crash_after_same_count_change,
        )
        service = _force_pass_service(
            workspace_service=workspace_service,
            session_factory=session_factory,
            vector_store_factory=lambda _workspace_id: vector_store,
        )

        with pytest.raises(SystemExit, match="same-count content change"):
            service.force_pass(workspace_id, chapter_id, reason="人工裁决批准")
        assert service.mark_running_memory_enrichment_as_failed_on_startup(
            workspace_id
        ) == [chapter_id]

        with session_factory(workspace_id) as session:
            row = session.get(ChapterORM, chapter_id)
            assert row is not None
            recovery = row.memory_enrichment_details["startup_recovery"]
            assert recovery["safe_to_continue"] is False
            assert recovery["recovery_disposition"] == "blocked_uncertain"
            assert memory_enrichment_requires_manual_recovery(
                status=row.memory_enrichment_status,
                details=row.memory_enrichment_details,
            )
            if damage == "sqlite_content":
                assert recovery["sqlite_side_effect_count"] > 0
                assert recovery["vector_side_effect_count"] == 0
            else:
                assert recovery["sqlite_side_effect_count"] == 0
                assert recovery["vector_side_effect_count"] > 0

    def test_force_pass_without_llm_deps_fails_before_ready(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R4:缺少 ledger 抽取依赖不能再以旧 fail-open 语义放行 ready。"""
        _seed_workspace_orm(session_factory, workspace_id)
        _seed_chapter(
            session_factory, workspace_id, "ch-0003", status="human_intervention"
        )
        _write_chapter_files(workspace_service, workspace_id, "ch-0003")
        called: list[bool] = []
        monkeypatch.setattr(
            "src.core.agents.memory_extractor.extract",
            lambda **kwargs: called.append(True),
        )
        service = ChapterRewindService(
            workspace_service=workspace_service,
            session_factory=session_factory,
        )

        with pytest.raises(ForcePassContinuityLedgerNotReadyError):
            service.force_pass(workspace_id, "ch-0003", reason="放行")
        assert called == []
        with session_factory(workspace_id) as session:
            row = session.get(ChapterORM, "ch-0003")
            assert row is not None
            assert row.status == "human_intervention"
            assert row.ready_for_next_chapter_at is None
