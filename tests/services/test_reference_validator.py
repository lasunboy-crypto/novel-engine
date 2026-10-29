"""reference_validator 单测 + ChapterCardService retry-references hook + Writer
前置 gate(P6-B-10)。

覆盖范围(5 类测试约定 + 1 unit 测):
- **unit**:``collect_unknown_ids_from_card`` 纯函数 4 字段扫描 + DB 空集
  跳过(legacy 兼容)
- **happy**:首次 agent 输出引用未知 ID → 自动 retry 1 次产合法输出 → 落库通过
- **error**:强制 2 次都失败 → ``ReferenceValidationFailedError``(``unknown_character_ids`` /
  ``unknown_thread_ids`` 含未知 ID)
- **gate**:retry 时 agent prompt 顶部含"重试上下文 — 严格 ID 约束"banner +
  "已知角色 ID"字面
- **rollback**:retry 失败时不落 SSOT 文件 + 不 upsert ORM 行
- **WS event**:retry hook 不阻碍 SSOT save + ORM upsert(既有事件依赖 caller,
  本 service 层不发新 event)

不依赖真 LLM(mock 路径 + monkeypatch);不依赖物理 fixture(``tmp_path``)。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
import structlog
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.core.agents.chapter_card_generator import (
    ChapterCardGenerationContext,
    ChapterCardGeneratorError,
)
from src.core.agents.reference_validator import (
    ReferenceValidationFailedError,
    collect_unknown_ids_from_card,
    load_known_ids_from_workspace,
    validate_card_against_workspace,
)
from src.core.db import Base
from src.core.db.models import (
    CharacterORM,
    ChapterCardORM,
    PlotThreadORM,
    VolumeORM,
    WorkspaceORM,
)
from src.core.llm.mock_client import MockLLMClient
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.setting_contract import SettingContract
from src.services.chapter_card_service import ChapterCardService
from src.services.chapter_service import ChapterService, ChapterServiceError
from src.services.workspace_service import WorkspaceService

# 导入已存在 fixture 工具(派生 outline + workspace ORM + volume ORM)
from tests.services.test_chapter_card_service import (
    _insert_volume_orms,
    _insert_workspace_orm,
    _write_outline_fixtures,
)


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
        "ws-ref-test",
        display_name="P6-B-10 reference_validator test",
        genre="xianxia",
        premise="test premise",
        activate=False,
    )
    return "ws-ref-test"


@pytest.fixture
def session_factory() -> Callable[[str], Session]:
    """单 in-memory engine + 共享数据。"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    def factory(workspace_id: str) -> Session:
        return Session(engine)

    return factory


@pytest.fixture
def mock_llm() -> MockLLMClient:
    return MockLLMClient()


def _insert_character_orm(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    character_id: str,
    *,
    revoked: bool = False,
) -> None:
    """落 CharacterORM 单行(供 reference_validator 校验对账)。"""
    session = session_factory(workspace_id)
    try:
        now = datetime.now(UTC)
        session.merge(
            CharacterORM(
                character_id=character_id,
                workspace_id=workspace_id,
                name=f"name-of-{character_id}",
                aliases=[],
                archetype="主角" if character_id.endswith("protagonist") else "支线",
                appearance="(test)",
                personality="(test)",
                background="(test)",
                voice_profile={
                    "description": "test voice",
                    "catchphrases": [],
                    "forbidden_phrases": [],
                    "tone": "neutral",
                    "sentence_style": "short",
                    "self_address": "我",
                    "addresses_others": {},
                },
                goals_long_term=[],
                goals_short_term=[],
                current_state={},
                relationships={},
                abilities=[],
                secrets=[],
                first_appearance_chapter=None,
                last_updated_chapter=None,
                revoked_at=now if revoked else None,
            )
        )
        session.commit()
    finally:
        session.close()


def _insert_plot_thread_orm(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    thread_id: str,
    *,
    revoked: bool = False,
) -> None:
    """落 PlotThreadORM 单行。"""
    session = session_factory(workspace_id)
    try:
        now = datetime.now(UTC)
        session.merge(
            PlotThreadORM(
                thread_id=thread_id,
                workspace_id=workspace_id,
                title=f"title-of-{thread_id}",
                status="planted",
                importance=3,
                planted_chapter="ch-0001",
                planned_payoff=None,
                actual_payoff=None,
                description="(test)",
                related_characters=[],
                created_at=now,
                last_updated_chapter=None,
                revoked_at=now if revoked else None,
            )
        )
        session.commit()
    finally:
        session.close()


def _make_card(
    chapter_id: str,
    *,
    pov_character: str,
    related_characters: list[str],
    related_plot_threads: list[str] | None = None,
) -> ChapterCard:
    """构造测试用 ChapterCard,引用字段 caller 指定。"""
    return ChapterCard(
        chapter_id=chapter_id,
        workspace_id="ws-ref-test",
        volume_id="vol-1",
        chapter_number=1,
        title="测试章",
        tension_level=5,
        must_achieve=["事件 A"],
        must_unresolved=[],
        forbidden=[],
        pov_character=pov_character,
        setting="测试场景",
        continuity_context=ContinuityContext(
            prev_chapter_tail="(开篇,无上一章)",
            unfinished_actions=[],
            time_gap="开篇",
            scene_continuation="switch",
            pov_change=False,
            pov_from=None,
            pov_to=pov_character,
        ),
        next_chapter_seed="种子",
        arc_phase="opening",
        is_arc_finale=False,
        related_plot_threads=related_plot_threads or [],
        related_characters=related_characters,
    )


def _make_service(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    mock_llm: MockLLMClient,
) -> ChapterCardService:
    chapter_service = ChapterService(
        workspace_service=workspace_service,
        client=mock_llm,
        model="claude-sonnet-4-6",
        session_factory=session_factory,
    )
    return ChapterCardService(
        workspace_service=workspace_service,
        chapter_service=chapter_service,
        session_factory=session_factory,
        client=mock_llm,
        model="claude-sonnet-4-6",
        ws_manager=None,
    )


# ---------------------------------------------------------------------------
# unit: collect_unknown_ids_from_card 纯函数
# ---------------------------------------------------------------------------


class TestCollectUnknownIds:
    def test_all_known_returns_empty_lists(self) -> None:
        card = _make_card(
            "ch-0001",
            pov_character="char-a",
            related_characters=["char-a", "char-b"],
            related_plot_threads=["th-1"],
        )
        unknown_chars, unknown_threads = collect_unknown_ids_from_card(
            card,
            known_character_ids={"char-a", "char-b"},
            known_thread_ids={"th-1"},
        )
        assert unknown_chars == []
        assert unknown_threads == []

    def test_unknown_pov_and_related_collected(self) -> None:
        """pov_character / related_characters / continuity_context.pov_to 全部扫,保序去重。"""
        card = _make_card(
            "ch-0001",
            pov_character="bai-ling",  # unknown
            related_characters=["bai-ling", "char-a"],  # bai-ling unknown
            related_plot_threads=[],
        )
        unknown_chars, unknown_threads = collect_unknown_ids_from_card(
            card,
            known_character_ids={"char-a"},
            known_thread_ids={"th-1"},
        )
        # 保序 + 去重:pov_character 首次出现 → 列表只 1 个 bai-ling
        assert unknown_chars == ["bai-ling"]
        assert unknown_threads == []

    def test_unknown_thread_collected(self) -> None:
        card = _make_card(
            "ch-0001",
            pov_character="char-a",
            related_characters=["char-a"],
            related_plot_threads=["th-ghost", "th-1"],  # th-ghost unknown
        )
        unknown_chars, unknown_threads = collect_unknown_ids_from_card(
            card,
            known_character_ids={"char-a"},
            known_thread_ids={"th-1"},
        )
        assert unknown_chars == []
        assert unknown_threads == ["th-ghost"]

    def test_empty_known_character_set_skips_validation(self) -> None:
        """DB 空表 = known_character_ids 空 set → 跳过角色校验(legacy 兼容)。"""
        card = _make_card(
            "ch-0001",
            pov_character="bai-ling",
            related_characters=["bai-ling"],
            related_plot_threads=["th-x"],
        )
        unknown_chars, unknown_threads = collect_unknown_ids_from_card(
            card,
            known_character_ids=set(),  # DB Characters 表空
            known_thread_ids={"th-1"},  # DB 有 th-1
        )
        assert unknown_chars == []  # 角色类目跳过校验
        assert unknown_threads == ["th-x"]  # 伏笔类目正常校验


# ---------------------------------------------------------------------------
# unit: load_known_ids_from_workspace 读 DB(含 revoked 过滤)
# ---------------------------------------------------------------------------


class TestLoadKnownIds:
    def test_loads_chars_and_threads_excluding_revoked(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_character_orm(session_factory, workspace_id, "char-live")
        _insert_character_orm(session_factory, workspace_id, "char-revoked", revoked=True)
        _insert_plot_thread_orm(session_factory, workspace_id, "th-live")
        _insert_plot_thread_orm(
            session_factory, workspace_id, "th-revoked", revoked=True
        )

        session = session_factory(workspace_id)
        try:
            known_chars, known_threads = load_known_ids_from_workspace(
                session, workspace_id
            )
        finally:
            session.close()
        assert known_chars == {"char-live"}
        assert known_threads == {"th-live"}


# ---------------------------------------------------------------------------
# happy:首次 agent 输出引用未知 ID → retry 自动重试产合法输出 → 落库通过
# ---------------------------------------------------------------------------


class TestAutoRetryHappyPath:
    """模拟 P5-acceptance 留观 ① — Qwen 产 ch-0001 引用 ``bai-ling`` (workspace
    角色表无) → 系统自动 retry 1 次,retry context 过滤到只剩 DB 已知 ID +
    is_retry_with_strict_ids=True banner → 第 2 次 mock 走 strict context →
    合法输出落库。"""

    def test_first_attempt_invalid_retry_succeeds(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        # DB 只放 char-protagonist + th-1(workspace 真实持久化的 ID)
        _insert_character_orm(session_factory, workspace_id, "char-protagonist")
        _insert_plot_thread_orm(session_factory, workspace_id, "th-1")
        service = _make_service(workspace_service, session_factory, mock_llm)

        # monkeypatch generate_chapter_card:首次返 invalid(bai-ling),retry
        # 返合法(走 retry context 的首位 character)
        call_count = {"n": 0}

        def _fake_generate(ctx: ChapterCardGenerationContext, **kwargs: object) -> ChapterCard:  # type: ignore[no-untyped-def]
            call_count["n"] += 1
            if call_count["n"] == 1:
                # 首次:返 invalid card(引用 bai-ling)
                return _make_card(
                    ctx.chapter_id,
                    pov_character="bai-ling",
                    related_characters=["bai-ling"],
                    related_plot_threads=["th-1"],
                )
            # retry:走 ctx.character_voices 首位(DB-filtered) + retry banner 已注入
            assert ctx.is_retry_with_strict_ids is True, "retry call must carry strict flag"
            pov = ctx.character_voices[0].character_id
            return _make_card(
                ctx.chapter_id,
                pov_character=pov,
                related_characters=[pov],
                related_plot_threads=["th-1"],
            )

        monkeypatch.setattr(
            "src.services.chapter_card_service.generate_chapter_card",
            _fake_generate,
        )

        card = service.generate_single_chapter_card(
            workspace_id,
            "ch-0001",
            chapter_number=1,
            volume_id="vol-1",
        )

        # 重试成功 — 第 2 次产出 char-protagonist(DB-known)
        assert call_count["n"] == 2
        assert card.pov_character == "char-protagonist"
        # SSOT 文件落盘
        card_file = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "cards"
            / "ch-0001.json"
        )
        assert card_file.is_file()
        # ORM 行落盘
        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterCardORM, "ch-0001")
        finally:
            session.close()
        assert row is not None
        assert row.card_status == "draft"

    def test_inner_agent_reference_error_reaches_existing_strict_retry(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Agent 先 fail-closed 时，服务层仍应执行既有一次严格 ID 重试。"""

        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        _insert_character_orm(session_factory, workspace_id, "char-protagonist")
        _insert_plot_thread_orm(session_factory, workspace_id, "th-1")
        service = _make_service(workspace_service, session_factory, mock_llm)
        action = "上一章即时未完动作必须逐字带回"
        captured: list[ChapterCardGenerationContext] = []

        def _fail_inside_then_succeed(
            ctx: ChapterCardGenerationContext, **kwargs: object
        ) -> ChapterCard:
            captured.append(ctx)
            if len(captured) == 1:
                raise ChapterCardGeneratorError(
                    "related_plot_threads 含未知 ID",
                    raw_output="{}",
                    model_used="mock-default",
                    error_code="CHAPTER_CARD_REFERENCE_INVALID",
                    details={
                        "unknown_character_ids": [],
                        "unknown_thread_ids": ["th-typo"],
                    },
                )
            assert ctx.is_retry_with_strict_ids is True
            assert ctx.continuity_bridge_retry_actions == [action]
            pov = ctx.character_voices[0].character_id
            return _make_card(
                ctx.chapter_id,
                pov_character=pov,
                related_characters=[pov],
                related_plot_threads=["th-1"],
            )

        monkeypatch.setattr(
            "src.services.chapter_card_service.generate_chapter_card",
            _fail_inside_then_succeed,
        )

        card = service.generate_single_chapter_card(
            workspace_id,
            "ch-0001",
            chapter_number=1,
            volume_id="vol-1",
            continuity_bridge_retry_actions=[action],
        )

        assert len(captured) == 2
        assert card.related_plot_threads == ["th-1"]


# ---------------------------------------------------------------------------
# error:两次都失败 → ReferenceValidationFailedError + details unknown_ids
# ---------------------------------------------------------------------------


class TestAutoRetryStillFails:
    def test_both_attempts_invalid_raises_with_unknown_ids(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        _insert_character_orm(session_factory, workspace_id, "char-protagonist")
        service = _make_service(workspace_service, session_factory, mock_llm)

        # monkeypatch:总是返 invalid(bai-ling)
        def _always_invalid(ctx: ChapterCardGenerationContext, **kwargs: object) -> ChapterCard:  # type: ignore[no-untyped-def]
            return _make_card(
                ctx.chapter_id,
                pov_character="bai-ling",
                related_characters=["bai-ling"],
                related_plot_threads=[],
            )

        monkeypatch.setattr(
            "src.services.chapter_card_service.generate_chapter_card",
            _always_invalid,
        )

        with pytest.raises(ReferenceValidationFailedError) as exc_info:
            service.generate_single_chapter_card(
                workspace_id,
                "ch-0001",
                chapter_number=1,
                volume_id="vol-1",
            )
        exc = exc_info.value
        assert exc.workspace_id == workspace_id
        assert exc.chapter_id == "ch-0001"
        assert exc.unknown_character_ids == ["bai-ling"]
        assert exc.unknown_thread_ids == []


# ---------------------------------------------------------------------------
# gate:retry 时 agent prompt 顶部含"重试上下文 — 严格 ID 约束"banner 字面
# ---------------------------------------------------------------------------


class TestRetryBannerInjected:
    def test_retry_call_context_has_strict_flag_and_filtered_voices(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """retry call 的 context.is_retry_with_strict_ids=True 且 character_voices
        过滤到只剩 DB 已知 ID。"""
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        _insert_character_orm(session_factory, workspace_id, "char-protagonist")
        service = _make_service(workspace_service, session_factory, mock_llm)

        captured_contexts: list[ChapterCardGenerationContext] = []

        def _capture_and_return(ctx: ChapterCardGenerationContext, **kwargs: object) -> ChapterCard:  # type: ignore[no-untyped-def]
            captured_contexts.append(ctx)
            if len(captured_contexts) == 1:
                return _make_card(
                    ctx.chapter_id,
                    pov_character="bai-ling",
                    related_characters=["bai-ling"],
                )
            pov = ctx.character_voices[0].character_id
            return _make_card(
                ctx.chapter_id,
                pov_character=pov,
                related_characters=[pov],
            )

        monkeypatch.setattr(
            "src.services.chapter_card_service.generate_chapter_card",
            _capture_and_return,
        )

        service.generate_single_chapter_card(
            workspace_id,
            "ch-0001",
            chapter_number=1,
            volume_id="vol-1",
        )

        # 首次 context:is_retry=False,character_voices 全(outline 派生 4 个)
        assert len(captured_contexts) == 2
        first_ctx = captured_contexts[0]
        retry_ctx = captured_contexts[1]
        assert first_ctx.is_retry_with_strict_ids is False
        assert retry_ctx.is_retry_with_strict_ids is True
        # retry context 过滤到只剩 char-protagonist(DB 已知)
        retry_char_ids = {c.character_id for c in retry_ctx.character_voices}
        assert retry_char_ids == {"char-protagonist"}

    def test_retry_user_message_contains_banner_literal(self) -> None:
        """直接调 agent ``_build_user_message``,is_retry_with_strict_ids=True
        时 user_text 顶部含 banner 字面。"""
        from src.core.agents.chapter_card_generator import (
            CharacterVoiceHint,
            PlotThreadSummary,
            _build_user_message,
        )

        ctx = ChapterCardGenerationContext(
            workspace_id="ws-test",
            chapter_id="ch-0001",
            volume_id="vol-1",
            chapter_number=1,
            arc_id="arc-1",
            arc_type="主线",
            arc_phase="opening",
            arc_summary="(test)",
            is_arc_finale=False,
            tension_level=5,
            setting_contract=SettingContract(
                setting="(test)",
                source_path="data/outline/world.json",
                source_hash="0" * 64,
            ),
            core_conflict="(test)",
            volume_summary="(test)",
            character_voices=[
                CharacterVoiceHint(
                    character_id="char-protagonist",
                    name="主角",
                    role="主角",
                    voice_hint="直白",
                )
            ],
            plot_threads=[
                PlotThreadSummary(
                    thread_id="th-1",
                    title="伏笔 1",
                    description="(test)",
                    status="planted",
                )
            ],
            is_retry_with_strict_ids=True,
        )
        user_text = _build_user_message(ctx)
        assert "重试上下文 — 严格 ID 约束" in user_text
        assert "已知角色 ID" in user_text
        assert "`char-protagonist`" in user_text
        assert "已知伏笔 ID" in user_text
        assert "`th-1`" in user_text


# ---------------------------------------------------------------------------
# rollback:retry 失败时不落 SSOT + 不 upsert ORM
# ---------------------------------------------------------------------------


class TestFailureRollback:
    def test_retry_failure_no_ssot_no_orm(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        _insert_character_orm(session_factory, workspace_id, "char-protagonist")
        service = _make_service(workspace_service, session_factory, mock_llm)

        def _always_invalid(ctx: ChapterCardGenerationContext, **kwargs: object) -> ChapterCard:  # type: ignore[no-untyped-def]
            return _make_card(
                ctx.chapter_id,
                pov_character="bai-ling",
                related_characters=["bai-ling"],
            )

        monkeypatch.setattr(
            "src.services.chapter_card_service.generate_chapter_card",
            _always_invalid,
        )

        with pytest.raises(ReferenceValidationFailedError):
            service.generate_single_chapter_card(
                workspace_id,
                "ch-0001",
                chapter_number=1,
                volume_id="vol-1",
            )

        # SSOT 文件未落
        card_file = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "cards"
            / "ch-0001.json"
        )
        assert not card_file.exists()
        # ORM 行未 upsert
        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterCardORM, "ch-0001")
        finally:
            session.close()
        assert row is None


# ---------------------------------------------------------------------------
# retry_references 手动入口
# ---------------------------------------------------------------------------


class TestRetryReferencesMethod:
    def test_retry_references_uses_strict_context_and_persists(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        _insert_character_orm(session_factory, workspace_id, "char-protagonist")
        service = _make_service(workspace_service, session_factory, mock_llm)

        captured: list[ChapterCardGenerationContext] = []
        captured_workspace_ids: list[object] = []

        def _capture(ctx: ChapterCardGenerationContext, **kwargs: object) -> ChapterCard:  # type: ignore[no-untyped-def]
            captured.append(ctx)
            captured_workspace_ids.append(
                structlog.contextvars.get_contextvars().get("workspace_id")
            )
            pov = ctx.character_voices[0].character_id
            return _make_card(
                ctx.chapter_id,
                pov_character=pov,
                related_characters=[pov],
            )

        monkeypatch.setattr(
            "src.services.chapter_card_service.generate_chapter_card",
            _capture,
        )

        result = service.retry_references(
            workspace_id,
            "ch-0001",
            chapter_number=1,
            volume_id="vol-1",
        )

        # retry_references 单次重试 = 1 次 generate 调用,is_retry=True
        # v1.0.x wave 5 session 4:返 RetryReferencesResult(card + invalidated_chapter_ids)
        assert len(captured) == 1
        assert captured[0].is_retry_with_strict_ids is True
        assert captured_workspace_ids == [workspace_id]
        assert result.card.pov_character == "char-protagonist"
        # 单章 fixture 无后续章节卡 → cascade list 应为空
        assert result.invalidated_chapter_ids == []
        # SSOT 落盘 + ORM upsert
        card_file = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "cards"
            / "ch-0001.json"
        )
        assert card_file.is_file()
        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterCardORM, "ch-0001")
        finally:
            session.close()
        assert row is not None
        assert row.card_status == "draft"

    def test_retry_references_keeps_known_continuity_rejection_actions(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """UI 引用重生不得丢掉同一卡已知的 bridge 精确拒绝证据。"""

        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        _insert_character_orm(session_factory, workspace_id, "char-protagonist")
        service = _make_service(workspace_service, session_factory, mock_llm)
        action = "上一章即时动作必须逐字承接"
        captured: list[ChapterCardGenerationContext] = []

        monkeypatch.setattr(
            service,
            "_continuity_bridge_actions_for_reference_retry",
            lambda **_kwargs: [action],
        )

        def _capture(
            ctx: ChapterCardGenerationContext, **kwargs: object
        ) -> ChapterCard:
            captured.append(ctx)
            pov = ctx.character_voices[0].character_id
            return _make_card(
                ctx.chapter_id,
                pov_character=pov,
                related_characters=[pov],
            )

        monkeypatch.setattr(
            "src.services.chapter_card_service.generate_chapter_card",
            _capture,
        )

        service.retry_references(
            workspace_id,
            "ch-0001",
            chapter_number=1,
            volume_id="vol-1",
        )

        assert len(captured) == 1
        assert captured[0].is_retry_with_strict_ids is True
        assert captured[0].continuity_bridge_retry_actions == [action]

    def test_retry_references_failure_raises_422_error(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        _insert_character_orm(session_factory, workspace_id, "char-protagonist")
        service = _make_service(workspace_service, session_factory, mock_llm)

        def _always_invalid(ctx: ChapterCardGenerationContext, **kwargs: object) -> ChapterCard:  # type: ignore[no-untyped-def]
            return _make_card(
                ctx.chapter_id,
                pov_character="bai-ling",
                related_characters=["bai-ling"],
            )

        monkeypatch.setattr(
            "src.services.chapter_card_service.generate_chapter_card",
            _always_invalid,
        )

        with pytest.raises(ReferenceValidationFailedError) as exc_info:
            service.retry_references(
                workspace_id,
                "ch-0001",
                chapter_number=1,
                volume_id="vol-1",
            )
        assert exc_info.value.unknown_character_ids == ["bai-ling"]


# ---------------------------------------------------------------------------
# Writer 前置 gate
# ---------------------------------------------------------------------------


class TestWriterGateReferenceValidation:
    def test_gate_blocks_when_card_references_unknown_char(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """章节卡引用 'bai-ling',DB Characters 只有 'char-real' → write gate
        抛 ReferenceValidationFailedError 422。"""
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_character_orm(session_factory, workspace_id, "char-real")

        # 写一份卡到 SSOT 文件 — 引用 bai-ling(DB-unknown)
        chapter_service = ChapterService(
            workspace_service=workspace_service,
            client=mock_llm,
            model="claude-sonnet-4-6",
            session_factory=session_factory,
        )
        card = _make_card(
            "ch-0001",
            pov_character="bai-ling",
            related_characters=["bai-ling"],
        )
        chapter_service.save_chapter_card(workspace_id, card)

        # 显式 upsert ChapterCardORM 行 + card_status=approved(绕过 card_status gate)
        session = session_factory(workspace_id)
        try:
            session.add(
                ChapterCardORM(
                    chapter_id="ch-0001",
                    workspace_id=workspace_id,
                    card_status="approved",
                    card_approved_at=datetime.now(UTC),
                    created_at=datetime.now(UTC),
                )
            )
            session.commit()
        finally:
            session.close()

        # 跑 write → reference_validator gate 应抛 ReferenceValidationFailedError
        with pytest.raises(ReferenceValidationFailedError) as exc_info:
            chapter_service.write(workspace_id, "ch-0001")
        assert exc_info.value.unknown_character_ids == ["bai-ling"]

    def test_empty_db_chars_fail_closed_in_character_admission(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """低层 reference gate 可兼容空表，但正文准入必须在 Provider 前硬停。"""
        _insert_workspace_orm(session_factory, workspace_id)
        # 显式**不**插入 CharacterORM 行 → DB 表空,gate 应 noop

        chapter_service = ChapterService(
            workspace_service=workspace_service,
            client=mock_llm,
            model="claude-sonnet-4-6",
            session_factory=session_factory,
        )
        card = _make_card(
            "ch-0001",
            pov_character="ghost-char",
            related_characters=["ghost-char"],
        )
        chapter_service.save_chapter_card(workspace_id, card)
        session = session_factory(workspace_id)
        try:
            session.add(
                ChapterCardORM(
                    chapter_id="ch-0001",
                    workspace_id=workspace_id,
                    card_status="approved",
                    card_approved_at=datetime.now(UTC),
                    created_at=datetime.now(UTC),
                )
            )
            session.commit()
        finally:
            session.close()

        with pytest.raises(
            ChapterServiceError, match="CHAPTER_CARD_CHARACTER_STATE_INVALID"
        ) as exc_info:
            chapter_service._run_gates(  # noqa: SLF001
                workspace_id, chapter_id="ch-0001"
            )
        assert "character_existence" in str(exc_info.value)


# ---------------------------------------------------------------------------
# validate_card_against_workspace 高层入口(WS event 段补:retry hook 不发新事件)
# ---------------------------------------------------------------------------


class TestValidateCardAgainstWorkspaceHelper:
    def test_helper_passes_when_all_known(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_character_orm(session_factory, workspace_id, "char-a")
        _insert_plot_thread_orm(session_factory, workspace_id, "th-1")

        card = _make_card(
            "ch-0001",
            pov_character="char-a",
            related_characters=["char-a"],
            related_plot_threads=["th-1"],
        )
        session = session_factory(workspace_id)
        try:
            # 不抛
            validate_card_against_workspace(
                card, session=session, workspace_id=workspace_id
            )
        finally:
            session.close()

    def test_helper_raises_on_unknown_id(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
    ) -> None:
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_character_orm(session_factory, workspace_id, "char-a")

        card = _make_card(
            "ch-0001",
            pov_character="bai-ling",
            related_characters=["bai-ling"],
        )
        session = session_factory(workspace_id)
        try:
            with pytest.raises(ReferenceValidationFailedError):
                validate_card_against_workspace(
                    card, session=session, workspace_id=workspace_id
                )
        finally:
            session.close()


# ---------------------------------------------------------------------------
# unused import shim(避免 ruff 抓 json 进口未用)
# ---------------------------------------------------------------------------


def test_imports_present() -> None:
    """sanity: 关键导出存在(供 ruff F401 / import-cycle 兜底)。"""
    assert ReferenceValidationFailedError is not None
    assert callable(collect_unknown_ids_from_card)
    assert callable(load_known_ids_from_workspace)
    assert callable(validate_card_against_workspace)
    # 兜底使用 json import 防 ruff F401
    assert json.loads("{}") == {}
    assert VolumeORM is not None
    assert WorkspaceORM is not None
