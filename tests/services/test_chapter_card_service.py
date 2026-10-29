"""ChapterCardService 单测(P5-B-06)。

覆盖范围:
- ``derive_context``:从 outline 4 段文件 + volumes 表派生
  ``ChapterCardGenerationContext`` 字段完整 + arc 命中 + tension 命中 + 首章 None
- ``generate_single_chapter_card``:wire agent + SSOT 落盘 + ORM upsert
- ``generate_first_card_after_commit``:固定只生成 ch-0001 并进入审核
- ``generate_next_card``:正式 Run 只生成当前唯一目标章
- 派生器边缘:outline 缺段 / chapter_number 越界 / arc 未覆盖

不依赖真 LLM(mock 路径 + ``MockLLMClient``);不依赖物理 fixture(``tmp_path``)。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from src.api import task_registry
from src.api.errors import CardNotApproved
from src.core.agents.chapter_summary_extractor import (
    ChapterSummary,
    continuity_ledger_provenance,
    extract_and_persist_continuity_ledger,
)
from src.core.agents.chapter_card_generator import (
    ChapterCardGenerationContext,
    ChapterCardGeneratorError,
)
from src.core.agents.setting_consistency_validator import SettingConsistencyResult
from src.core.db import Base
from src.core.db.models import (
    AttemptLedgerORM,
    CharacterORM,
    ChapterCardORM,
    ChapterORM,
    ChapterSummaryORM,
    ChapterVersionORM,
    PlotThreadORM,
    VolumeORM,
    WorkspaceORM,
)
from src.core.llm.attempt_context import run_observed_provider_attempt
from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage
from src.core.llm.mock_client import MockLLMClient
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.review import ConstraintViolation
from src.core.models.setting_contract import load_setting_contract
from src.core.models.writer_profile import WriterProfile
from src.core.routing.router import RoutedTarget
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.card_generator_config import CardGeneratorConfig
from src.services.attempt_ledger import wrap_router_with_attempt_tracking
from src.services.chapter_card_service import (
    CardInvalidStateTransitionError,
    CardNotFoundError,
    ChapterCardContextDerivationError,
    ChapterCardService,
    ChapterCardServiceError,
)
from src.services.chapter_service import ChapterService
from src.services.core_state_service import CoreStateService
from src.services.workspace_service import WorkspaceService


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
        "ws-card-svc-test",
        display_name="P5-B-06 card service test",
        genre="xianxia",
        premise="test premise",
        activate=False,
    )
    return "ws-card-svc-test"


@pytest.fixture
def session_factory() -> Callable[[str], Session]:
    """单 in-memory engine 多 session 共享数据。"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    def factory(workspace_id: str) -> Session:
        return Session(engine)

    return factory


@pytest.fixture
def mock_llm() -> MockLLMClient:
    return MockLLMClient()


@pytest.fixture(autouse=True)
def _clear_task_registry() -> Iterator[None]:
    """每个 test 隔离 task_registry,避免 LRU 残留干扰。"""
    yield
    task_registry.clear()


def _write_outline_fixtures(
    workspace_service: WorkspaceService, workspace_id: str
) -> None:
    """落 4 段 outline JSON 文件 + characters / plot 派生用占位数据。"""
    outline_dir = (
        workspace_service.workspaces_root / workspace_id / "data" / "outline"
    )
    outline_dir.mkdir(parents=True, exist_ok=True)

    world = {
        "setting": "末法时代仙侠世界",
        "power_system": "灵气稀薄,需依靠丹药修炼",
        "factions": "天剑宗、丹道阁、散修联盟",
        "culture": "重门第与传承,经济以灵石为本",
        "unique_rules": "无丹不可成仙,炼丹失败必失寿元",
    }
    (outline_dir / "world.json").write_text(
        json.dumps(world, ensure_ascii=False), encoding="utf-8"
    )

    characters = {
        "characters": [
            {
                "character_id": "char-protagonist",
                "name": "李清玄",
                "role": "protagonist",
                "archetype": "废柴炼丹师",
                "appearance": "玉冠白衣",
                "personality": "坚韧果敢",
                "background": "丹道阁弃徒",
                "motivation": "复仇兼证道",
                "voice_hint": "言辞铿锵",
                "relationships_brief": {},
            },
            {
                "character_id": "char-antagonist",
                "name": "王沧海",
                "role": "antagonist",
                "archetype": "宗门大长老",
                "appearance": "白须长髯",
                "personality": "阴险狡诈",
                "background": "天剑宗叛徒",
                "motivation": "夺取丹方",
                "voice_hint": "冷峻威压",
                "relationships_brief": {},
            },
            {
                "character_id": "char-deuter",
                "name": "苏婉清",
                "role": "deuteragonist",
                "archetype": "炼丹圣女",
                "appearance": "翠裙青衫",
                "personality": "温柔聪慧",
                "background": "丹道阁圣女",
                "motivation": "守护道统",
                "voice_hint": "温言细语",
                "relationships_brief": {},
            },
            {
                "character_id": "char-support",
                "name": "玄玑子",
                "role": "supporting",
                "archetype": "老丹师",
                "appearance": "鹤发童颜",
                "personality": "睿智沉稳",
                "background": "丹道阁前长老",
                "motivation": "传承衣钵",
                "voice_hint": "字字珠玑",
                "relationships_brief": {},
            },
        ]
    }
    (outline_dir / "characters.json").write_text(
        json.dumps(characters, ensure_ascii=False), encoding="utf-8"
    )

    plot = {
        "core_conflict": "李清玄重生后复仇王沧海并证仙",
        "main_phases": [
            {"phase_id": "ph-1", "title": "重生之始", "summary": "回到 7 岁开篇"},
            {"phase_id": "ph-2", "title": "入门崭露", "summary": "丹道天才"},
            {"phase_id": "ph-3", "title": "宗门动荡", "summary": "卷入争斗"},
            {"phase_id": "ph-4", "title": "登仙之路", "summary": "证道大成"},
        ],
        "major_turning_points": [
            {"turning_id": "tp-1", "title": "重生", "description": "回溯"},
            {"turning_id": "tp-2", "title": "亮相", "description": "首战告捷"},
            {"turning_id": "tp-3", "title": "证仙", "description": "终局升仙"},
        ],
        "long_threads": [
            {"thread_id": "th-1", "title": "失传丹方", "description": "失传的逆天丹方"},
            {"thread_id": "th-2", "title": "宗门内奸", "description": "王沧海背后的势力"},
            {"thread_id": "th-3", "title": "前世遗物", "description": "重生时携带遗物"},
        ],
        "character_arcs": {
            "char-protagonist": "从废柴到仙帝",
            "char-antagonist": "从光鲜到陨落",
        },
    }
    (outline_dir / "plot.json").write_text(
        json.dumps(plot, ensure_ascii=False), encoding="utf-8"
    )

    volume_plans = {
        "volumes": [
            {
                "volume_id": "vol-1",
                "title": "重生之卷",
                "summary": "重生主线开篇",
                "chapter_count": 6,
                "tension_template": "heros_journey",
                "linked_phases": ["ph-1", "ph-2"],
                "linked_turning_points": ["tp-1", "tp-2"],
                "linked_threads": ["th-1"],
                "arcs": [
                    {
                        "arc_id": "vol1-arc1",
                        "arc_type": "主线",
                        "chapter_range": [1, 3],
                        "phase": "opening",
                        "summary": "重生开篇,亮相",
                    },
                    {
                        "arc_id": "vol1-arc2",
                        "arc_type": "副本",
                        "chapter_range": [4, 6],
                        "phase": "rising",
                        "summary": "宗门试炼副本",
                    },
                ],
                "tension_curve": [3, 4, 5, 6, 7, 8],
            },
            {
                "volume_id": "vol-2",
                "title": "崭露之卷",
                "summary": "宗门崛起",
                "chapter_count": 6,
                "tension_template": "steady_daily",
                "linked_phases": ["ph-3"],
                "linked_turning_points": ["tp-3"],
                "linked_threads": ["th-2", "th-3"],
                "arcs": [
                    {
                        "arc_id": "vol2-arc1",
                        "arc_type": "主线",
                        "chapter_range": [1, 3],
                        "phase": "climax",
                        "summary": "宗门对决",
                    },
                    {
                        "arc_id": "vol2-arc2",
                        "arc_type": "副本",
                        "chapter_range": [4, 6],
                        "phase": "falling",
                        "summary": "余波副本",
                    },
                ],
                "tension_curve": [6, 7, 9, 7, 5, 4],
            },
        ]
    }
    (outline_dir / "volume_plans.json").write_text(
        json.dumps(volume_plans, ensure_ascii=False), encoding="utf-8"
    )


def _insert_workspace_orm(
    session_factory: Callable[[str], Session], workspace_id: str
) -> None:
    """落 WorkspaceORM 行(FK 兜底)。"""
    session = session_factory(workspace_id)
    try:
        now = datetime.now(timezone.utc)
        session.merge(
            WorkspaceORM(
                workspace_id=workspace_id,
                display_name="P5-B-06 card service test",
                genre="xianxia",
                premise="test",
                created_at=now,
                last_active_at=now,
                engine_version="0.1.0",
                status="active",
                total_chapters=12,
                completed_chapters=0,
                config_overrides={},
            )
        )
        session.commit()
    finally:
        session.close()


def _insert_volume_orms(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    *,
    vol1_chapter_count: int = 6,
    vol2_chapter_count: int = 6,
) -> None:
    """落 2 卷 VolumeORM 行(对齐 outline fixture 数据 + 可调首卷章数测短卷场景)。"""
    session = session_factory(workspace_id)
    try:
        now = datetime.now(timezone.utc)
        session.merge(
            VolumeORM(
                volume_id="vol-1",
                workspace_id=workspace_id,
                title="重生之卷",
                summary="重生主线",
                chapter_count=vol1_chapter_count,
                tension_template="heros_journey",
                arcs_json=[],
                tension_curve_json=[],
                created_at=now,
                last_updated_at=now,
            )
        )
        session.merge(
            VolumeORM(
                volume_id="vol-2",
                workspace_id=workspace_id,
                title="崭露之卷",
                summary="宗门崛起",
                chapter_count=vol2_chapter_count,
                tension_template="steady_daily",
                arcs_json=[],
                tension_curve_json=[],
                created_at=now,
                last_updated_at=now,
            )
        )
        session.commit()
    finally:
        session.close()


def _insert_character_orms(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    character_ids: list[str],
) -> None:
    """落 CharacterORM 行,用于 derive_context 走 DB 优先路径。"""
    session = session_factory(workspace_id)
    try:
        for cid in character_ids:
            session.merge(
                CharacterORM(
                    character_id=cid,
                    workspace_id=workspace_id,
                    name=f"角色{cid}",
                    aliases=[],
                    archetype="supporting",
                    appearance="外貌占位",
                    personality="性格占位",
                    background="背景占位",
                    identity_facets=[],
                    voice_profile={"description": f"{cid} 的说话方式"},
                    goals_long_term=[],
                    goals_short_term=[],
                    current_state={},
                    state_timeline=[],
                    relationships={},
                    abilities=[],
                    secrets=[],
                    first_appearance_chapter=None,
                    last_updated_chapter=None,
                    revoked_at=None,
                )
            )
        session.commit()
    finally:
        session.close()


def _make_service(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    mock_llm: MockLLMClient,
    *,
    card_config: CardGeneratorConfig | None = None,
    vector_store_factory: Callable[[str], object] | None = None,
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
        vector_store_factory=vector_store_factory,  # type: ignore[arg-type]
        ws_manager=None,
        card_config=card_config,
    )


# ---------------------------------------------------------------------------
# derive_context
# ---------------------------------------------------------------------------


class TestDeriveContext:
    def test_a1_derives_typed_drama_source_without_new_provider_path(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _write_outline_fixtures(workspace_service, workspace_id)
        _write_mini_plan_fixture(
            workspace_service,
            workspace_id,
            linked_characters=["char-protagonist", "char-antagonist"],
            next_one_line_event="王沧海隔空撤权，李清玄公开丹方来源并承担暴露代价",
        )
        outline_dir = (
            workspace_service.workspaces_root / workspace_id / "data" / "outline"
        )
        drama = {
            "function": "turning",
            "actor_id": "char-protagonist",
            "counterforce_id": "char-antagonist",
            "interaction_mode": "indirect",
            "thread_id": "th-1",
            "turning_id": "tp-1",
            "actor_goal": "李清玄公开丹方来源",
            "counteraction": "王沧海隔空撤销丹阁查阅权限",
            "choice": "李清玄把丹方交给苏婉清共同核验",
            "consequence": "王沧海失去独占丹方的筹码，李清玄暴露重生线索",
        }
        volume_payload = json.loads(
            (outline_dir / "volume_plans.json").read_text(encoding="utf-8")
        )
        volume = volume_payload["volumes"][0]
        volume["dramatic_promise"] = {
            "primary_thread_id": "th-1",
            "primary_turning_id": "tp-1",
            "actor_id": "char-protagonist",
            "counterforce_id": "char-antagonist",
            "outcome": "resolved",
            "deferred_to_volume_id": None,
            "deferred_to_thread_id": None,
        }
        volume["arcs"][0]["beats"] = [
            {
                "beat_id": "vol1-a1-turning",
                "chapter_range": [1, 2],
                "summary": "丹方归属首次正面转折",
                "must_advance": ["丹方来源进入公开核验"],
                "forbidden_advance": ["提前完成终局"],
                "dramatic_contract": drama,
            }
        ]
        (outline_dir / "volume_plans.json").write_text(
            json.dumps(volume_payload, ensure_ascii=False), encoding="utf-8"
        )
        mini_payload = json.loads(
            (outline_dir / "mini_plan.json").read_text(encoding="utf-8")
        )
        mini_payload["items"][1]["linked_characters"] = [
            "char-protagonist",
            "char-antagonist",
        ]
        mini_payload["items"][1]["linked_threads"] = ["th-1"]
        mini_payload["items"][1]["dramatic_contract"] = drama
        (outline_dir / "mini_plan.json").write_text(
            json.dumps(mini_payload, ensure_ascii=False), encoding="utf-8"
        )

        context = _make_service(
            workspace_service, session_factory, mock_llm
        ).derive_context(
            workspace_id,
            "ch-0002",
            chapter_number=2,
            volume_id="vol-1",
        )

        assert context.volume_dramatic_promise is not None
        assert context.beat_dramatic_contract is not None
        assert context.mini_plan_dramatic_contract == context.beat_dramatic_contract
        assert context.beat_id == "vol1-a1-turning"

        ordinary = _make_service(
            workspace_service, session_factory, mock_llm
        ).derive_context(
            workspace_id,
            "ch-0001",
            chapter_number=1,
            volume_id="vol-1",
        )
        assert ordinary.volume_dramatic_promise is not None
        assert ordinary.beat_id == "vol1-a1-turning"
        assert ordinary.beat_dramatic_contract is None
        assert ordinary.mini_plan_dramatic_contract is None

    def test_first_chapter_no_prev_fields(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _write_outline_fixtures(workspace_service, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        ctx = service.derive_context(
            workspace_id,
            "ch-0001",
            chapter_number=1,
            volume_id="vol-1",
        )
        assert ctx.chapter_number == 1
        assert ctx.arc_id == "vol1-arc1"
        assert ctx.arc_phase == "opening"
        assert ctx.arc_type == "主线"
        assert ctx.is_arc_finale is False  # ch1 不是 arc 末位 (1-3 末位是 3)
        assert ctx.tension_level == 3
        assert ctx.prev_chapter_summary is None
        assert ctx.prev_chapter_tail is None
        assert len(ctx.character_voices) == 4
        assert len(ctx.plot_threads) == 3
        assert ctx.core_state_enabled is True
        assert ctx.core_state_projection is not None
        assert ctx.core_state_projection.facts == ()
        assert ctx.setting_contract.setting == "末法时代仙侠世界"
        assert ctx.setting_contract.source_path == "data/outline/world.json"
        assert len(ctx.setting_contract.source_hash) == 64
        assert ctx.setting_contract == load_setting_contract(
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "outline"
            / "world.json"
        )
        assert ctx.core_conflict.startswith("李清玄重生后复仇")

    def test_workspace_kill_switch_keeps_new_card_context_legacy_v0(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """关闭总开关后不构建并行事实来源，只让后续卡走既有 v0。"""

        _write_outline_fixtures(workspace_service, workspace_id)
        project_yaml = (
            workspace_service.workspaces_root / workspace_id / "project.yaml"
        )
        project_yaml.write_text(
            "generation:\n  core_state_hard_enabled: false\n",
            encoding="utf-8",
        )

        ctx = _make_service(
            workspace_service, session_factory, mock_llm
        ).derive_context(
            workspace_id,
            "ch-0001",
            chapter_number=1,
            volume_id="vol-1",
        )

        assert ctx.core_state_enabled is False
        assert ctx.core_state_projection is None

    def test_database_partial_rows_cannot_hide_outline_main_threads(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """临时层接入后，DB 只落一条也必须与 outline 主伏笔取并集。"""
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        session = session_factory(workspace_id)
        try:
            session.add(
                PlotThreadORM(
                    thread_id="th-1",
                    workspace_id=workspace_id,
                    title="失传丹方",
                    status="developing",
                    importance=5,
                    planted_chapter="ch-0001",
                    planned_payoff=None,
                    actual_payoff=None,
                    description="数据库已更新的失传丹方进度",
                    origin="outline",
                    expiry_policy="never",
                    source_evidence=None,
                    related_characters=[],
                    created_at=datetime.now(timezone.utc),
                    last_updated_chapter="ch-0001",
                )
            )
            session.commit()
        finally:
            session.close()

        ctx = _make_service(
            workspace_service, session_factory, mock_llm
        ).derive_context(
            workspace_id,
            "ch-0001",
            chapter_number=1,
            volume_id="vol-1",
        )
        assert [thread.thread_id for thread in ctx.plot_threads] == [
            "th-1",
            "th-2",
            "th-3",
        ]
        assert ctx.plot_threads[0].description == "数据库已更新的失传丹方进度"
        assert all(thread.origin == "outline" for thread in ctx.plot_threads)

    def test_emergent_threads_expire_by_structured_arc_not_free_text_map(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        outline_dir = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "outline"
        )
        (outline_dir / "mini_plan.json").write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "chapter_id": "ch-0001",
                            "chapter_number": 1,
                            "volume_id": "vol-1",
                            "arc_id": "vol1-arc1",
                            "one_line_event": "李清玄发现临时线索",
                        },
                        {
                            "chapter_id": "ch-0004",
                            "chapter_number": 4,
                            "volume_id": "vol-1",
                            "arc_id": "vol1-arc2",
                            "one_line_event": "李清玄进入宗门试炼",
                        },
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        session = session_factory(workspace_id)
        try:
            now = datetime.now(timezone.utc)
            for thread_id, policy in (
                ("temp-arc", "arc_end"),
                ("temp-volume", "volume_end"),
                ("temp-never", "never"),
            ):
                session.add(
                    PlotThreadORM(
                        thread_id=thread_id,
                        workspace_id=workspace_id,
                        title=thread_id,
                        status="planted",
                        importance=5,
                        planted_chapter="ch-0001",
                        planned_payoff=None,
                        actual_payoff=None,
                        description=f"{thread_id} 的临时悬念",
                        origin="emergent",
                        expiry_policy=policy,
                        source_evidence="正文逐字证据足够长且唯一",
                        related_characters=[],
                        created_at=now,
                        last_updated_chapter="ch-0001",
                    )
                )
            session.commit()
        finally:
            session.close()

        ctx = _make_service(
            workspace_service, session_factory, mock_llm
        ).derive_context(
            workspace_id,
            "ch-0004",
            chapter_number=4,
            volume_id="vol-1",
        )
        ids = {thread.thread_id for thread in ctx.plot_threads}
        assert "temp-arc" not in ids
        assert {"temp-volume", "temp-never"}.issubset(ids)
        assert {"th-1", "th-2", "th-3"}.issubset(ids)

    def test_only_semantic_top_three_emergent_candidates_reach_card_context(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        session = session_factory(workspace_id)
        try:
            now = datetime.now(timezone.utc)
            for index in range(1, 5):
                session.add(
                    PlotThreadORM(
                        thread_id=f"temp-{index}",
                        workspace_id=workspace_id,
                        title=f"临时伏笔 {index}",
                        status="planted",
                        importance=4,
                        planted_chapter="ch-0001",
                        planned_payoff=None,
                        actual_payoff=None,
                        description=f"临时伏笔 {index} 的待回答承诺",
                        origin="emergent",
                        expiry_policy="never",
                        source_evidence=f"第 {index} 条正文唯一逐字证据",
                        related_characters=[],
                        created_at=now,
                        last_updated_chapter="ch-0001",
                    )
                )
            session.commit()
        finally:
            session.close()

        vector_store = MagicMock()
        vector_store.query.return_value = [
            MagicMock(metadata={"entity_id": thread_id})
            for thread_id in ("temp-3", "temp-1", "temp-4")
        ]
        service = _make_service(
            workspace_service,
            session_factory,
            mock_llm,
            vector_store_factory=lambda _workspace_id: vector_store,
        )
        ctx = service.derive_context(
            workspace_id,
            "ch-0001",
            chapter_number=1,
            volume_id="vol-1",
        )
        emergent_ids = [
            thread.thread_id
            for thread in ctx.plot_threads
            if thread.origin == "emergent"
        ]
        assert emergent_ids == ["temp-3", "temp-1", "temp-4"]
        assert len([t for t in ctx.plot_threads if t.origin == "outline"]) == 3
        vector_store.query.assert_called_once()
        call = vector_store.query.call_args.kwargs
        assert call["top_k"] == 3
        assert call["where"]["$and"][1]["entity_id"]["$in"] == [
            "temp-1",
            "temp-2",
            "temp-3",
            "temp-4",
        ]

    def test_beat_contract_is_derived_for_all_single_card_paths(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """普通单卡、引用重试与失效卡恢复都经 context 收到所属 beat 原文。"""

        _write_outline_fixtures(workspace_service, workspace_id)
        volume_path = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "outline"
            / "volume_plans.json"
        )
        volume_payload = json.loads(volume_path.read_text(encoding="utf-8"))
        volume_payload["volumes"][0]["arcs"][0]["beats"] = [
            {
                "beat_id": "vol1-arc1-bt1",
                "chapter_range": [1, 3],
                "summary": "在裂隙入口确认开启规律",
                "must_advance": ["三章合计确认裂隙的开启规律"],
                "forbidden_advance": ["林衍不得提前进入裂隙"],
            }
        ]
        volume_path.write_text(
            json.dumps(volume_payload, ensure_ascii=False), encoding="utf-8"
        )
        service = _make_service(workspace_service, session_factory, mock_llm)

        ctx = service.derive_context(
            workspace_id,
            "ch-0002",
            chapter_number=2,
            volume_id="vol-1",
        )

        assert ctx.beat_id == "vol1-arc1-bt1"
        assert ctx.beat_summary == "在裂隙入口确认开启规律"
        assert ctx.beat_must_advance == ["三章合计确认裂隙的开启规律"]
        assert ctx.beat_forbidden_advance == ["林衍不得提前进入裂隙"]

    def test_arc_finale_detected(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """ch=3 == vol1-arc1.chapter_range[1] (3) → is_arc_finale=True。"""
        _write_outline_fixtures(workspace_service, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        ctx = service.derive_context(
            workspace_id,
            "ch-0003",
            chapter_number=3,
            volume_id="vol-1",
        )
        assert ctx.is_arc_finale is True
        assert ctx.arc_id == "vol1-arc1"
        assert ctx.tension_level == 5  # tension_curve[2]

    def test_second_arc_phase(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """ch=4 落 vol1-arc2 (chapter_range [4,6], phase rising)。"""
        _write_outline_fixtures(workspace_service, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        ctx = service.derive_context(
            workspace_id,
            "ch-0004",
            chapter_number=4,
            volume_id="vol-1",
        )
        assert ctx.arc_id == "vol1-arc2"
        assert ctx.arc_phase == "rising"
        assert ctx.arc_type == "副本"

    def test_ready_previous_ledger_overrides_previous_card_plan(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """R4:下一卡直接前章上下文只采用已 ready 的实际小账本。"""

        _write_outline_fixtures(workspace_service, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        chapter_service = service._chapter_service
        previous_card = ChapterCard(
            chapter_id="ch-0001",
            workspace_id=workspace_id,
            volume_id="vol-1",
            chapter_number=1,
            title="计划标题",
            tension_level=3,
            word_count_min=2500,
            word_count_max=4500,
            must_achieve=[],
            must_unresolved=["计划中的未决项，不代表实际发生"],
            forbidden=[],
            pov_character="char-protagonist",
            setting="山门",
            continuity_context=ContinuityContext(
                prev_chapter_tail="",
                time_gap="开篇",
                scene_continuation="switch",
                pov_change=False,
                pov_from=None,
                pov_to="char-protagonist",
            ),
            next_chapter_seed="旧计划 seed，不得冒充已发生事实",
            arc_phase="opening",
            is_arc_finale=False,
            related_plot_threads=[],
            related_characters=["char-protagonist"],
        )
        chapter_service.save_chapter_card(workspace_id, previous_card)
        paths = WorkspacePaths(
            workspace_id,
            workspaces_root=workspace_service.workspaces_root,
        )
        final_text = "李清玄踏入丹道阁，亲手封住药炉。炉火仍未熄灭。"
        final_path = paths.chapter_version_file("ch-0001", "final.md")
        version_path = paths.chapter_version_file("ch-0001", "v1.md")
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_text(final_text, encoding="utf-8")
        version_path.write_text(final_text, encoding="utf-8")
        chapter_service._upsert_chapter_state(
            workspace_id,
            "ch-0001",
            state="approved",
        )
        with session_factory(workspace_id) as session:
            chapter_row = session.get(ChapterORM, "ch-0001")
            assert chapter_row is not None
            chapter_row.current_version = 1
            session.add(
                ChapterVersionORM(
                    chapter_id="ch-0001",
                    version_id=1,
                    content=final_text,
                    word_count=len(final_text),
                    created_at=datetime.now(timezone.utc),
                    creation_type="draft",
                    model_used="mock-default",
                    review_score=None,
                    notes="",
                    style_fingerprint=None,
                )
            )
            session.commit()
        ledger = extract_and_persist_continuity_ledger(
            workspace_id=workspace_id,
            chapter_id="ch-0001",
            source_version_id=1,
            paths=paths,
            session_factory=session_factory,
            llm_client=mock_llm,
            llm_model="mock-default",
            prompts_dir=Path(__file__).resolve().parents[2] / "prompts",
        )
        chapter_service._mark_ready_for_next_chapter(
            workspace_id,
            "ch-0001",
            paths=paths,
            final_path=final_path,
            final_version_id=1,
        )

        context = service.derive_context(
            workspace_id,
            "ch-0002",
            chapter_number=2,
            volume_id="vol-1",
        )

        assert context.prev_chapter_summary == ledger.summary_text
        assert context.prev_chapter_tail == ledger.raw_final_tail
        assert context.prev_pov_character == ledger.pov_character
        assert context.prev_unfinished_actions == ledger.unfinished_actions
        assert context.prev_chapter_summary != previous_card.next_chapter_seed
        assert context.prev_unfinished_actions != previous_card.must_unresolved
        assert context.continuity_ledger_provenance == (
            continuity_ledger_provenance(ledger)
        )
        direct = [
            summary
            for summary in context.prev_chapter_summaries
            if summary.chapter_id == "ch-0001"
        ]
        assert len(direct) == 1
        assert direct[0].evidence_records
        assert direct[0].character_states
        assert direct[0].established_facts

    def test_outline_missing_raises(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 不写 outline 文件
        service = _make_service(workspace_service, session_factory, mock_llm)
        complete = MagicMock(side_effect=AssertionError("缺合同后不得调用 LLM"))
        monkeypatch.setattr(mock_llm, "complete", complete)
        with pytest.raises(ChapterCardContextDerivationError) as exc_info:
            service.generate_single_chapter_card(
                workspace_id,
                "ch-0001",
                chapter_number=1,
                volume_id="vol-1",
            )
        assert str(exc_info.value) == (
            "权威世界契约缺失:data/outline/world.json"
        )
        complete.assert_not_called()

    def test_world_contract_corrupt_raises_before_llm(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_outline_fixtures(workspace_service, workspace_id)
        world_path = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "outline"
            / "world.json"
        )
        world_path.write_text("{bad-json", encoding="utf-8")
        service = _make_service(workspace_service, session_factory, mock_llm)
        complete = MagicMock(side_effect=AssertionError("坏合同后不得调用 LLM"))
        monkeypatch.setattr(mock_llm, "complete", complete)

        with pytest.raises(ChapterCardContextDerivationError) as exc_info:
            service.generate_single_chapter_card(
                workspace_id,
                "ch-0001",
                chapter_number=1,
                volume_id="vol-1",
            )

        assert str(exc_info.value) == (
            "权威世界契约损坏:data/outline/world.json"
        )
        complete.assert_not_called()

    def test_unknown_volume_id_raises(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _write_outline_fixtures(workspace_service, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        with pytest.raises(ChapterCardContextDerivationError):
            service.derive_context(
                workspace_id,
                "ch-0001",
                chapter_number=1,
                volume_id="vol-999",
            )

    def test_chapter_number_out_of_range_raises(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _write_outline_fixtures(workspace_service, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        with pytest.raises(ChapterCardContextDerivationError):
            service.derive_context(
                workspace_id,
                "ch-0099",
                chapter_number=99,
                volume_id="vol-1",
            )


# ---------------------------------------------------------------------------
# v1.0.x wave 4 session 2:mini_plan + 上一章卡 SSOT 注入
# ---------------------------------------------------------------------------


def _write_mini_plan_fixture(
    workspace_service: WorkspaceService,
    workspace_id: str,
    *,
    chapter_id: str = "ch-0001",
    one_line_event: str = "李清玄在丹道阁后山察觉宗门内奸的踪迹,埋下复仇伏笔",
    linked_characters: list[str] | None = None,
    chapter_focus: str = "",
    arc_beats: dict[str, str] | None = None,
    interaction_focus: str = "",
    breakthrough_payoff: str = "",
    next_one_line_event: str | None = None,
) -> None:
    """写一份最小合法 mini_plan.json fixture,含 ``chapter_id`` 一项。

    wave5 字段(chapter_focus/arc_beats/interaction_focus/breakthrough_payoff)
    **仅在传非默认值时写入** → 不传时 payload 字节与旧 fixture 完全一致,既有测试零影响。
    """
    outline_dir = (
        workspace_service.workspaces_root / workspace_id / "data" / "outline"
    )
    outline_dir.mkdir(parents=True, exist_ok=True)
    item: dict[str, object] = {
        "chapter_id": chapter_id,
        "chapter_number": 1,
        "volume_id": "vol-1",
        "arc_id": "vol1-arc1",
        "one_line_event": one_line_event,
        "linked_characters": linked_characters or ["char-protagonist"],
        "linked_threads": ["th-2"],
    }
    if chapter_focus:
        item["chapter_focus"] = chapter_focus
    if arc_beats:
        item["arc_beats"] = arc_beats
    if interaction_focus:
        item["interaction_focus"] = interaction_focus
    if breakthrough_payoff:
        item["breakthrough_payoff"] = breakthrough_payoff
    items = [item]
    if next_one_line_event is not None:
        items.append(
            {
                "chapter_id": "ch-0002",
                "chapter_number": 2,
                "volume_id": "vol-1",
                "arc_id": "vol1-arc1",
                "one_line_event": next_one_line_event,
                "linked_characters": ["char-protagonist"],
                "linked_threads": ["th-2"],
            }
        )
    payload = {"items": items}
    (outline_dir / "mini_plan.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _write_prev_chapter_card_ssot(
    workspace_service: WorkspaceService,
    workspace_id: str,
    *,
    chapter_id: str,
    next_chapter_seed: str,
    pov_character: str,
    must_unresolved: list[str],
) -> None:
    """写一份最小合法上一章 ChapterCard SSOT 文件(``data/cards/<id>.json``)。"""
    cards_dir = (
        workspace_service.workspaces_root / workspace_id / "data" / "cards"
    )
    cards_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "chapter_id": chapter_id,
        "workspace_id": workspace_id,
        "volume_id": "vol-1",
        "chapter_number": 1,
        "title": "前序章占位",
        "pov_character": pov_character,
        "setting": "fixture 场景占位",
        "arc_id": "vol1-arc1",
        "arc_type": "主线",
        "arc_phase": "opening",
        "is_arc_finale": False,
        "tension_level": 3,
        "must_achieve": ["上一章占位事件 A", "上一章占位事件 B"],
        "must_unresolved": must_unresolved,
        "forbidden": ["现代用语"],
        "continuity_context": {
            "prev_chapter_tail": "(开篇,无上一章)",
            "unfinished_actions": [],
            "time_gap": "开篇",
            "scene_continuation": "switch",
            "pov_change": False,
            "pov_from": None,
            "pov_to": pov_character,
        },
        "next_chapter_seed": next_chapter_seed,
        "related_plot_threads": ["th-1"],
        "related_characters": [pov_character],
        "card_status": "approved",
        "card_approved_at": None,
    }
    (cards_dir / f"{chapter_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


class TestDeriveContextWave4MiniPlan:
    """v1.0.x wave 4 session 2:mini_plan.json + 上一章卡 SSOT 注入,4 路径覆盖。

    - 路径 ①:有 mini_plan + 有上一章卡(ch-0002 完整路径)
    - 路径 ②:有 mini_plan + 无上一章卡(ch-0002,batch 第 1 张已生但 SSOT 缺)
    - 路径 ③:无 mini_plan + 有上一章卡(旧工作区 + chapter_card 流程已跑过)
    - 路径 ④:无 mini_plan + 无上一章卡(wave 4 前老工作区首章)
    """

    def test_mini_plan_event_injected_when_file_exists(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """路径 ④ 退化对照:首章 ch-0001 写 mini_plan 注入 mini_plan_event 字段。"""
        _write_outline_fixtures(workspace_service, workspace_id)
        _write_mini_plan_fixture(
            workspace_service,
            workspace_id,
            chapter_id="ch-0001",
            one_line_event="李清玄踏入山门初遇苏婉清,埋下契约伏笔",
        )
        service = _make_service(workspace_service, session_factory, mock_llm)
        ctx = service.derive_context(
            workspace_id, "ch-0001", chapter_number=1, volume_id="vol-1"
        )
        assert ctx.mini_plan_event == "李清玄踏入山门初遇苏婉清,埋下契约伏笔"
        # 首章其余 prev_* 字段仍为 None(chapter_number==1 分支)
        assert ctx.prev_chapter_summary is None
        assert ctx.prev_pov_character is None
        assert ctx.prev_unfinished_actions == []

    def test_next_mini_plan_event_is_injected_as_chapter_boundary(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _write_outline_fixtures(workspace_service, workspace_id)
        next_event = "名单出现空行，众人首次确认记忆抹除已经发生"
        _write_mini_plan_fixture(
            workspace_service,
            workspace_id,
            chapter_id="ch-0001",
            next_one_line_event=next_event,
        )
        service = _make_service(workspace_service, session_factory, mock_llm)

        ctx = service.derive_context(
            workspace_id, "ch-0001", chapter_number=1, volume_id="vol-1"
        )

        assert ctx.next_mini_plan_event == next_event

    def test_wave5_fields_passthrough_to_ctx(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """v1.1 wave5 接线防回退(透传中点环):derive_context 把 mini_plan 的
        chapter_focus(s6/§2.18)/arc_beats(s7/§2.19)/interaction_focus(s8/§2.20)/
        breakthrough_payoff(s10①/§2.23)读进 ctx(下游 _assemble_chapter_card 再
        确定性透传 ChapterCard)。防未来改 derive_context 漏读某字段 → 机制静默失效
        (writer 收不到 → 不注入),而 §3 启动 audit 与全量都照不出该环。"""
        _write_outline_fixtures(workspace_service, workspace_id)
        _write_mini_plan_fixture(
            workspace_service,
            workspace_id,
            chapter_id="ch-0001",
            linked_characters=["char-protagonist", "char-deuter"],
            chapter_focus="情感单焦:聚焦李清玄与苏婉清的诀别",
            arc_beats={"char-protagonist": "从犹豫到决断的转折"},
            interaction_focus="师徒对话中显露隔阂",
            breakthrough_payoff="突破筑基期——灵识外放新能力,当场碾压旧敌",
        )
        service = _make_service(workspace_service, session_factory, mock_llm)
        ctx = service.derive_context(
            workspace_id, "ch-0001", chapter_number=1, volume_id="vol-1"
        )
        assert ctx.mini_plan_linked_characters == ["char-protagonist", "char-deuter"]
        assert ctx.mini_plan_linked_threads == ["th-2"]
        assert ctx.chapter_focus == "情感单焦:聚焦李清玄与苏婉清的诀别"
        assert ctx.arc_beats == {"char-protagonist": "从犹豫到决断的转折"}
        assert ctx.interaction_focus == "师徒对话中显露隔阂"
        assert ctx.breakthrough_payoff == "突破筑基期——灵识外放新能力,当场碾压旧敌"

    def test_character_voices_prioritize_linked_and_cap_pool_c7(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """C-7:长跑角色表膨胀时,每章 character_voices 相关性筛选 + 封顶。

        DB 中只有涌现 NPC 行时,outline 核心角色仍保留;本章 mini_plan linked 的
        NPC 即便排序靠后也优先进入;额外角色池最多 12 个,避免全表无界塞进出卡上下文。
        """
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_character_orms(
            session_factory,
            workspace_id,
            [f"npc-{i:02d}" for i in range(15)],
        )
        _write_mini_plan_fixture(
            workspace_service,
            workspace_id,
            chapter_id="ch-0001",
            linked_characters=["npc-14", "char-deuter"],
        )
        service = _make_service(workspace_service, session_factory, mock_llm)

        ctx = service.derive_context(
            workspace_id, "ch-0001", chapter_number=1, volume_id="vol-1"
        )

        ids = [voice.character_id for voice in ctx.character_voices]
        assert len(ids) == 12
        assert ids[:2] == ["npc-14", "char-deuter"]
        assert {"char-protagonist", "char-antagonist", "char-support"}.issubset(ids)
        assert "npc-13" not in ids

    def test_wave5_fields_empty_when_mini_plan_omits(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """零回归:旧工作区 mini_plan 不含 wave5 字段 → ctx 取 MiniPlanItem default
        (空串/空 dict),不报错(向后兼容,护留白)。"""
        _write_outline_fixtures(workspace_service, workspace_id)
        _write_mini_plan_fixture(
            workspace_service, workspace_id, chapter_id="ch-0001"
        )
        service = _make_service(workspace_service, session_factory, mock_llm)
        ctx = service.derive_context(
            workspace_id, "ch-0001", chapter_number=1, volume_id="vol-1"
        )
        assert ctx.chapter_focus == ""
        assert ctx.arc_beats == {}
        assert ctx.interaction_focus == ""
        assert ctx.breakthrough_payoff == ""

    def test_mini_plan_event_none_when_file_missing(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """路径 ④:wave 4 前老工作区 mini_plan.json 缺失 → ctx.mini_plan_event=None + warn(Q1=A)。"""
        _write_outline_fixtures(workspace_service, workspace_id)
        # 不写 mini_plan.json
        service = _make_service(workspace_service, session_factory, mock_llm)
        with capture_logs() as logs:
            ctx = service.derive_context(
                workspace_id, "ch-0001", chapter_number=1, volume_id="vol-1"
            )
        assert ctx.mini_plan_event is None
        # structlog warning 落事件键 chapter_card_mini_plan_missing(Q1=A 拍板)
        assert any(
            log.get("event") == "chapter_card_mini_plan_missing"
            and log.get("reason") == "mini_plan_file_not_found"
            for log in logs
        )
        assert ctx.mini_plan_linked_characters == []
        assert ctx.mini_plan_linked_threads == []

    def test_prev_chapter_card_fields_injected_from_ssot(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """路径 ①:ch-0002 上一章卡 SSOT 存在 → 兑现 P5-B-06 注释承诺(Q4=A)。

        - prev_chapter_summary ← 上一章卡 next_chapter_seed
        - prev_pov_character ← 上一章卡 pov_character
        - prev_unfinished_actions ← 上一章卡 must_unresolved
        """
        _write_outline_fixtures(workspace_service, workspace_id)
        _write_mini_plan_fixture(
            workspace_service,
            workspace_id,
            chapter_id="ch-0002",
            one_line_event="苏婉清深夜传讯,揭露宗门内奸的关键信物",
        )
        _write_prev_chapter_card_ssot(
            workspace_service,
            workspace_id,
            chapter_id="ch-0001",
            next_chapter_seed="李清玄入门后夜逢苏婉清传讯,引出内奸悬念",
            pov_character="char-protagonist",
            must_unresolved=[
                "玉佩纹路所代表的真正身份未揭",
                "前世记忆未向任何人透露",
            ],
        )
        service = _make_service(workspace_service, session_factory, mock_llm)
        ctx = service.derive_context(
            workspace_id, "ch-0002", chapter_number=2, volume_id="vol-1"
        )
        assert ctx.mini_plan_event == "苏婉清深夜传讯,揭露宗门内奸的关键信物"
        assert ctx.prev_chapter_summary == "李清玄入门后夜逢苏婉清传讯,引出内奸悬念"
        assert ctx.prev_pov_character == "char-protagonist"
        assert ctx.prev_unfinished_actions == [
            "玉佩纹路所代表的真正身份未揭",
            "前世记忆未向任何人透露",
        ]

    def test_prev_chapter_card_none_when_card_missing(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """路径 ②:ch-0002 上一章卡 SSOT 不存在(batch 第 1 张 fail) → prev_* 留 None。

        wave 4 前老语义保留 — 上一章卡缺失不阻塞当前章 derive。
        """
        _write_outline_fixtures(workspace_service, workspace_id)
        _write_mini_plan_fixture(
            workspace_service,
            workspace_id,
            chapter_id="ch-0002",
            one_line_event="苏婉清深夜传讯,揭露宗门内奸的关键信物",
        )
        # 不写上一章卡 SSOT(模拟 batch 第 1 张 fail / 跳过场景)
        service = _make_service(workspace_service, session_factory, mock_llm)
        ctx = service.derive_context(
            workspace_id, "ch-0002", chapter_number=2, volume_id="vol-1"
        )
        assert ctx.mini_plan_event == "苏婉清深夜传讯,揭露宗门内奸的关键信物"
        assert ctx.prev_chapter_summary is None
        assert ctx.prev_pov_character is None
        assert ctx.prev_unfinished_actions == []

    def test_mini_plan_chapter_id_not_in_items_returns_none(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """边界:mini_plan.json 存在但 items 内无当前 chapter_id → None + warn。

        触发场景:user 改了 outline volume_plans 章数(总章数变多)但忘了 regenerate
        mini_plan 段。
        """
        _write_outline_fixtures(workspace_service, workspace_id)
        _write_mini_plan_fixture(
            workspace_service,
            workspace_id,
            chapter_id="ch-0001",
            one_line_event="占位事件",
        )
        # 查 ch-0002(outline 在范围内但 mini_plan items 没有该章)
        service = _make_service(workspace_service, session_factory, mock_llm)
        with capture_logs() as logs:
            ctx = service.derive_context(
                workspace_id, "ch-0002", chapter_number=2, volume_id="vol-1"
            )
        assert ctx.mini_plan_event is None
        assert any(
            log.get("event") == "chapter_card_mini_plan_missing"
            and log.get("reason") == "chapter_id_not_in_items"
            for log in logs
        )


def _insert_chapter_summary_orm_rows(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    *,
    chapter_summaries: list[dict],
) -> None:
    """直接 INSERT 多行 ``chapter_summaries`` 表(v1.0.x wave 4 session 3b 测试用)。

    每条 dict 至少含 ``chapter_id`` / ``summary_text``;其余字段按合理默认补全。
    """
    session = session_factory(workspace_id)
    try:
        now = datetime.now(timezone.utc)
        for entry in chapter_summaries:
            row = ChapterSummaryORM(
                chapter_id=entry["chapter_id"],
                workspace_id=workspace_id,
                summary_text=entry.get("summary_text", "fixture 摘要"),
                pov_character=entry.get("pov_character"),
                scene_setting=entry.get("scene_setting"),
                key_events=entry.get("key_events", []),
                character_interactions=entry.get("character_interactions", []),
                unfinished_actions=entry.get("unfinished_actions", []),
                extraction_trigger=entry.get("extraction_trigger", "reviewer_pass"),
                model_used=entry.get("model_used", "mock-model"),
                created_at=now,
                updated_at=now,
            )
            session.merge(row)
        session.commit()
    finally:
        session.close()


class TestDeriveContextWave4Session3bPrevSummaries:
    """v1.0.x wave 4 session 3b:``prev_chapter_summaries`` 注入,7 路径覆盖。

    - 路径 ①:有前置摘要表行 → 返按 chapter_id 升序的列表(happy path)
    - 路径 ②:首章 ch-0001 → 表无前置 → 空 list(自然边界)
    - 路径 ③:老工作区 chapter_summaries 表无任何行 → 空 list(向后兼容)
    - 路径 ④:超过 N=10 的前置章 → 倒序取最近 10 条，再升序交给模型
    - 路径 ⑤:session_factory is None → 空 list(legacy 单测兜底)
    - 路径 ⑥:reviewer_pass + force_pass 都返(不过滤 — 决策见 impl 文档)
    - 路径 ⑦:乱序 INSERT 仍按 chapter_id 升序返(查询级 ORDER BY 验证)
    """

    def test_prev_chapter_summaries_injected_from_table_rows(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """路径 ①:ch-0004 前有 3 章摘要 → 返 3 条按 chapter_id 升序。"""
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_chapter_summary_orm_rows(
            session_factory,
            workspace_id,
            chapter_summaries=[
                {
                    "chapter_id": "ch-0001",
                    "summary_text": "首章 — 李清玄踏入丹道阁",
                    "pov_character": "char-protagonist",
                    "scene_setting": "晨雾未散的山门",
                    "unfinished_actions": ["前世记忆悬而未发"],
                },
                {
                    "chapter_id": "ch-0002",
                    "summary_text": "次章 — 苏婉清传讯揭露线索",
                    "pov_character": "char-deuter",
                    "scene_setting": "深夜阁楼",
                    "unfinished_actions": ["内奸身份未明"],
                },
                {
                    "chapter_id": "ch-0003",
                    "summary_text": "三章 — 王沧海登场试探",
                    "pov_character": "char-protagonist",
                    "scene_setting": "丹房暗角",
                    "character_interactions": [
                        {
                            "char_a": "char-protagonist",
                            "char_b": "char-antagonist",
                            "dynamic": "从陌生到敌对的初次交锋",
                        }
                    ],
                },
            ],
        )
        service = _make_service(workspace_service, session_factory, mock_llm)
        ctx = service.derive_context(
            workspace_id, "ch-0004", chapter_number=4, volume_id="vol-1"
        )
        assert len(ctx.prev_chapter_summaries) == 3
        assert [s.chapter_id for s in ctx.prev_chapter_summaries] == [
            "ch-0001",
            "ch-0002",
            "ch-0003",
        ]
        # 字段透传完整(随机抽 ch-0003 的 character_interactions)
        assert ctx.prev_chapter_summaries[2].character_interactions == [
            {
                "char_a": "char-protagonist",
                "char_b": "char-antagonist",
                "dynamic": "从陌生到敌对的初次交锋",
            }
        ]
        # pov_character / scene_setting 透传
        assert ctx.prev_chapter_summaries[0].pov_character == "char-protagonist"
        assert ctx.prev_chapter_summaries[0].scene_setting == "晨雾未散的山门"

    def test_prev_chapter_summaries_empty_for_first_chapter(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """路径 ②:ch-0001 首章 → 表中无 chapter_id < 'ch-0001' 行 → 空 list。"""
        _write_outline_fixtures(workspace_service, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        ctx = service.derive_context(
            workspace_id, "ch-0001", chapter_number=1, volume_id="vol-1"
        )
        assert ctx.prev_chapter_summaries == []
        assert ctx.core_state_enabled is True
        assert ctx.core_state_projection is not None

    def test_prev_chapter_summaries_empty_when_table_empty(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """路径 ③:老工作区(wave 4 session 3a 前)→ 表存在但无任何行 → 空 list。"""
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        # 不 insert 任何 chapter_summary 行
        service = _make_service(workspace_service, session_factory, mock_llm)
        ctx = service.derive_context(
            workspace_id, "ch-0005", chapter_number=5, volume_id="vol-1"
        )
        assert ctx.prev_chapter_summaries == []

    def test_prev_chapter_summaries_truncated_to_default_n(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """路径 ④:INSERT 12 行前置 → ch-0013 返回最近 10 条且阅读顺序升序。"""
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        # 调整 outline volume_plans 让 ch-0013 在 vol-2(ch-0007 起 / 卷内 7)
        _insert_chapter_summary_orm_rows(
            session_factory,
            workspace_id,
            chapter_summaries=[
                {
                    "chapter_id": f"ch-{i:04d}",
                    "summary_text": f"第 {i} 章 fixture 摘要",
                }
                for i in range(1, 13)
            ],
        )
        service = _make_service(workspace_service, session_factory, mock_llm)
        # 用 ch-0007 卷 vol-2 内的章测(volume_plans fixture 内 vol-2 chapter_range 1-6)
        # 取 ch-0006 之前的 5 章本卷内 + 同时本测的重心是 limit n=10 行为
        # 简化:用 vol-1 ch-0006 测(ch-0001..ch-0005 都是 vol-1 内前置)
        # 但要求 12 行 + ch-0013 才能触发 truncate;改用 mock chapter_id 直读 helper
        # 直接调 _read_prev_chapter_summaries 测 truncate(避开 outline 章数约束)
        results = service._read_prev_chapter_summaries(
            workspace_id=workspace_id,
            chapter_id="ch-0013",  # 大于 ch-0012,前置 12 行全 match
            n=10,
        )
        assert len(results) == 10
        # 查询先 DESC LIMIT 10，再反转供模型按时间升序阅读。
        assert [s.chapter_id for s in results] == [
            f"ch-{i:04d}" for i in range(3, 13)
        ]

    def test_prev_chapter_summaries_empty_when_session_factory_none(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        mock_llm: MockLLMClient,
    ) -> None:
        """路径 ⑤:session_factory=None(legacy) → 空 list,不抛(对齐
        ``_derive_character_voices`` 同型 fallback)。"""
        _write_outline_fixtures(workspace_service, workspace_id)
        chapter_service = ChapterService(
            workspace_service=workspace_service,
            client=mock_llm,
            model="claude-sonnet-4-6",
            session_factory=None,
        )
        service = ChapterCardService(
            workspace_service=workspace_service,
            chapter_service=chapter_service,
            session_factory=None,
            client=mock_llm,
            model="claude-sonnet-4-6",
            ws_manager=None,
        )
        ctx = service.derive_context(
            workspace_id, "ch-0001", chapter_number=1, volume_id="vol-1"
        )
        assert ctx.prev_chapter_summaries == []
        assert ctx.core_state_enabled is False
        assert ctx.core_state_projection is None

    def test_authoritative_previous_ledger_replaces_within_latest_ten(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """摘要缺上一章行时，补权威 ledger 后仍最多 10 条且保留最近窗口。"""
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_chapter_summary_orm_rows(
            session_factory,
            workspace_id,
            chapter_summaries=[
                {
                    "chapter_id": f"ch-{idx:04d}",
                    "summary_text": f"第 {idx} 章摘要",
                }
                for idx in range(1, 11)
            ],
        )
        service = _make_service(workspace_service, session_factory, mock_llm)
        previous = ChapterSummary(
            chapter_id="ch-0011",
            workspace_id=workspace_id,
            summary_text="第 11 章权威小账本",
            raw_final_tail="第 11 章尾文",
            source_version_id=1,
            source_text_hash="a" * 64,
            ledger_hash="b" * 64,
            validated_at=datetime.now(timezone.utc),
            extraction_trigger="reviewer_pass",
            model_used="mock-model",
            extracted_at=datetime.now(timezone.utc),
        )
        monkeypatch.setattr(
            service._chapter_service,  # noqa: SLF001
            "require_ready_for_next_chapter",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(
            "src.services.chapter_card_service.load_validated_continuity_ledger",
            lambda **_kwargs: previous,
        )

        context = service.derive_context(
            workspace_id,
            "ch-0012",
            chapter_number=6,
            volume_id="vol-2",
        )

        assert len(context.prev_chapter_summaries) == 10
        assert [item.chapter_id for item in context.prev_chapter_summaries] == [
            *[f"ch-{idx:04d}" for idx in range(2, 11)],
            "ch-0011",
        ]

    def test_prev_chapter_summaries_includes_both_triggers(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """路径 ⑥:reviewer_pass + force_pass 行都返(本会话自决:不过滤,LLM 自判;
        v1.1 加 flag 时再过滤)。"""
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_chapter_summary_orm_rows(
            session_factory,
            workspace_id,
            chapter_summaries=[
                {
                    "chapter_id": "ch-0001",
                    "summary_text": "reviewer_pass 章摘要",
                    "extraction_trigger": "reviewer_pass",
                },
                {
                    "chapter_id": "ch-0002",
                    "summary_text": "force_pass 章摘要(用户跳过 reviewer)",
                    "extraction_trigger": "force_pass",
                },
            ],
        )
        service = _make_service(workspace_service, session_factory, mock_llm)
        ctx = service.derive_context(
            workspace_id, "ch-0003", chapter_number=3, volume_id="vol-1"
        )
        triggers = sorted(s.extraction_trigger for s in ctx.prev_chapter_summaries)
        assert triggers == ["force_pass", "reviewer_pass"]

    def test_prev_chapter_summaries_ordered_by_chapter_id_asc(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """路径 ⑦:INSERT 乱序 → 查询级 ORDER BY chapter_id ASC 仍升序返。"""
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        # 故意乱序 insert
        _insert_chapter_summary_orm_rows(
            session_factory,
            workspace_id,
            chapter_summaries=[
                {"chapter_id": "ch-0003", "summary_text": "3 章"},
                {"chapter_id": "ch-0001", "summary_text": "1 章"},
                {"chapter_id": "ch-0002", "summary_text": "2 章"},
            ],
        )
        service = _make_service(workspace_service, session_factory, mock_llm)
        ctx = service.derive_context(
            workspace_id, "ch-0004", chapter_number=4, volume_id="vol-1"
        )
        assert [s.chapter_id for s in ctx.prev_chapter_summaries] == [
            "ch-0001",
            "ch-0002",
            "ch-0003",
        ]


# ---------------------------------------------------------------------------
# generate_single_chapter_card
# ---------------------------------------------------------------------------


class TestGenerateSingleChapterCard:
    @pytest.mark.parametrize("severity", ["hard", "soft"])
    def test_setting_blocker_gets_one_evidence_bound_retry(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
        severity: str,
    ) -> None:
        import src.core.agents.chapter_card_generator as generator_module
        import src.services.chapter_card_service as service_module

        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        contract = load_setting_contract(
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "outline"
            / "world.json"
        )
        violation = ConstraintViolation(
            constraint_id="8" * 64,
            domain="world.unique_rules",
            source_path=contract.source_path,
            source_hash=contract.source_hash,
            rule_quote=contract.unique_rules,
            evidence_quote="候选卡同时要求启用又禁止必要状态变化",
            severity=severity,  # type: ignore[arg-type]
            ambiguity=None,
        )
        original_generate = service_module.generate_chapter_card
        seen_contexts = []

        def fail_once_then_generate(context, **kwargs):  # type: ignore[no-untyped-def]
            seen_contexts.append(context)
            if len(seen_contexts) == 1:
                raise ChapterCardGeneratorError(
                    "setting conflict",
                    raw_output="first candidate",
                    model_used="mock",
                    error_code="CHAPTER_CARD_SETTING_CONFLICT",
                    details={
                        "failure_layer": "chapter_card_setting",
                        "contract_hash": contract.source_hash,
                        "constraint_violations": [violation.model_dump(mode="json")],
                    },
                )
            return original_generate(context, **kwargs)

        monkeypatch.setattr(
            service_module, "generate_chapter_card", fail_once_then_generate
        )
        monkeypatch.setattr(
            generator_module,
            "validate_setting_consistency",
            lambda **_kwargs: SettingConsistencyResult(
                passed=True,
                resolved_recheck_constraint_ids=(violation.constraint_id,),
                validation_source="authoritative_world_contract",
            ),
        )
        with capture_logs() as logs:
            card = service.generate_single_chapter_card(
                workspace_id,
                "ch-0001",
                chapter_number=1,
                volume_id="vol-1",
                card_status="draft",
            )

        assert card.chapter_id == "ch-0001"
        assert len(seen_contexts) == 2
        assert seen_contexts[0].setting_conflict_retry_violations == []
        assert seen_contexts[1].setting_conflict_retry_violations == [violation]
        events = [row["event"] for row in logs]
        assert "chapter_card_setting_conflict_retry_started" in events
        assert "chapter_card_setting_conflict_retry_succeeded" in events

    def test_core_state_repair_reuses_same_single_contract_retry_budget(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.services.chapter_card_service as service_module

        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        original_generate = service_module.generate_chapter_card
        seen_contexts: list[ChapterCardGenerationContext] = []

        def fail_once_then_generate(context, **kwargs):  # type: ignore[no-untyped-def]
            seen_contexts.append(context)
            if len(seen_contexts) == 1:
                raise ChapterCardGeneratorError(
                    "missing current state",
                    raw_output="first candidate",
                    model_used="mock",
                    error_code="CHAPTER_CARD_CORE_STATE_INVALID",
                    details={"errors": ["迁移 object:TQ-07:seal_status 当前没有该事实"]},
                )
            return original_generate(context, **kwargs)

        monkeypatch.setattr(
            service_module,
            "generate_chapter_card",
            fail_once_then_generate,
        )

        with capture_logs() as logs:
            card = service.generate_single_chapter_card(
                workspace_id,
                "ch-0001",
                chapter_number=1,
                volume_id="vol-1",
            )

        assert card.chapter_id == "ch-0001"
        assert len(seen_contexts) == 2
        assert seen_contexts[1].core_state_retry_errors == [
            "迁移 object:TQ-07:seal_status 当前没有该事实"
        ]
        events = [row["event"] for row in logs]
        assert "chapter_card_core_state_retry_started" in events
        assert "chapter_card_core_state_retry_succeeded" in events

    def test_single_chapter_card_writes_file_and_orm(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)

        card = service.generate_single_chapter_card(
            workspace_id,
            "ch-0001",
            chapter_number=1,
            volume_id="vol-1",
            card_status="draft",
        )

        assert card.chapter_id == "ch-0001"
        assert card.volume_id == "vol-1"
        assert card.chapter_number == 1
        assert card.card_status == "draft"
        contract = load_setting_contract(
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "outline"
            / "world.json"
        )
        assert card.setting_contract_hash == contract.source_hash
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

    def test_active_prompt_nonempty_core_state_smoke_through_attempt_wrapper(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """真实产品形状：活动 prompt→strict schema→attempt wrap→SSOT/ORM。"""

        import src.core.agents.chapter_card_generator as generator_module

        class StrictTransitionClient(LLMClient):
            def __init__(self) -> None:
                self.prepared_schema: dict[str, Any] | None = None
                self.complete_params: dict[str, Any] | None = None
                self.system_prompt = ""

            def prepare_strict_json_schema(
                self,
                *,
                model: str,
                schema_name: str,
                schema: dict[str, Any],
                gen_params: dict[str, Any],
            ) -> dict[str, Any]:
                assert model == "qwen3.8-max"
                assert schema_name == "chapter_card"
                self.prepared_schema = schema
                prepared = dict(gen_params)
                prepared["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "strict": True,
                        "schema": schema,
                    },
                }
                return prepared

            def complete(
                self,
                messages: list[Message],
                **gen_params: Any,
            ) -> LLMResponse:
                self.complete_params = gen_params
                self.system_prompt = messages[0].content
                payload = {
                    "title": "身份从记忆中消失",
                    "pov_character": "char-protagonist",
                    "setting": "山门档案室，身份名册仍完好",
                    "craft_focus": "用旁人的陌生目光表现身份断裂，不增加新背景。",
                    "must_achieve": [
                        "李清玄核对名册并确认自己的署名仍客观存在",
                        "同伴面对李清玄时无法辨认其身份",
                    ],
                    "must_unresolved": ["身份抹除的范围仍未查清"],
                    "forbidden": ["本章绝不恢复任何人对李清玄的辨认"],
                    "continuity_context": {
                        "prev_chapter_tail": ("(系统将在解析后注入上一章真实尾文)"),
                        "unfinished_actions": [],
                        "time_gap": "开篇",
                        "scene_continuation": "switch",
                        "pov_change": False,
                        "pov_from": None,
                        "pov_to": "char-protagonist",
                    },
                    "core_state_transitions": [
                        {
                            "entity_type": "character",
                            "entity_id": "char-protagonist",
                            "attribute": "identity",
                            "from_value": None,
                            "to_value": ("公开记录仍在，但所有在世者都无法辨认其身份"),
                            "reason": "本章上游规划明确安排持续身份识别变化",
                        }
                    ],
                    "next_chapter_seed": "名册署名成为追索身份的唯一线索。",
                    "related_plot_threads": ["th-1"],
                    "related_characters": ["char-protagonist"],
                }
                response = LLMResponse(
                    content=json.dumps(payload, ensure_ascii=False),
                    model_used="qwen3.8-max",
                    usage=TokenUsage(input_tokens=120, output_tokens=80),
                    finish_reason="stop",
                )
                return run_observed_provider_attempt(
                    lambda: response,
                    technical_attempt=1,
                )

        class SingleClientRouter:
            def __init__(
                self,
                client: LLMClient,
                profile: WriterProfile,
            ) -> None:
                self.client = client
                self.profile = profile

            def route(
                self,
                _agent_name: str,
                *,
                chapter_card: Any = None,
                override_profile_id: str | None = None,
            ) -> RoutedTarget:
                del chapter_card, override_profile_id
                return RoutedTarget(self.profile, self.client, {})

        _write_outline_fixtures(workspace_service, workspace_id)
        _write_mini_plan_fixture(
            workspace_service,
            workspace_id,
            chapter_id="ch-0001",
        )
        _insert_workspace_orm(session_factory, workspace_id)
        client = StrictTransitionClient()
        profile = WriterProfile(
            profile_id="qwen-schema-smoke",
            provider="openai_compatible",
            model="qwen3.8-max",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            context_window=1_000_000,
        )
        tracked_router = wrap_router_with_attempt_tracking(
            SingleClientRouter(client, profile),  # type: ignore[arg-type]
            workspace_id=workspace_id,
            session_factory=session_factory,
        )
        chapter_service = ChapterService(
            workspace_service=workspace_service,
            client=client,
            model=profile.model,
            session_factory=session_factory,
        )
        service = ChapterCardService(
            workspace_service=workspace_service,
            chapter_service=chapter_service,
            session_factory=session_factory,
            router_factory=lambda _workspace_id: tracked_router,  # type: ignore[arg-type,return-value]
        )
        monkeypatch.setattr(
            generator_module,
            "_validate_chapter_card_semantics",
            lambda **_kwargs: None,
        )

        card = service.generate_single_chapter_card(
            workspace_id,
            "ch-0001",
            chapter_number=1,
            volume_id="vol-1",
            card_status="draft",
        )

        transition = card.core_state_transitions[0]
        assert transition.state_key == "character:char-protagonist:identity"
        assert transition.entity_type == "character"
        # fake Provider 只选 th-1；mini-plan 明确分配 th-2，最终卡必须确定性补齐。
        assert card.related_plot_threads == ["th-1", "th-2"]
        assert client.prepared_schema is not None
        assert client.complete_params is not None
        assert client.complete_params["response_format"]["type"] == "json_schema"  # type: ignore[index]
        assert "ChapterCardGenerator 提示词 v18" in client.system_prompt
        assert "不要输出 `state_key`" in client.system_prompt
        card_path = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "cards"
            / "ch-0001.json"
        )
        persisted = json.loads(card_path.read_text(encoding="utf-8"))
        assert persisted["core_state_transitions"][0]["state_key"] == (
            "character:char-protagonist:identity"
        )
        assert persisted["related_plot_threads"] == ["th-1", "th-2"]
        with session_factory(workspace_id) as session:
            attempts = session.query(AttemptLedgerORM).all()
        assert len(attempts) == 1
        assert attempts[0].status == "succeeded"
        assert attempts[0].agent_name == "chapter_card_generator"

    def test_setting_hard_conflict_leaves_no_card_ssot_or_orm(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.core.agents.chapter_card_generator as generator_module

        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        contract = load_setting_contract(
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "outline"
            / "world.json"
        )
        violation = ConstraintViolation(
            constraint_id="7" * 64,
            domain="world.unique_rules",
            source_path=contract.source_path,
            source_hash=contract.source_hash,
            rule_quote=contract.unique_rules,
            evidence_quote="山门初遇",
            severity="hard",
            ambiguity=None,
        )
        setting_judge_calls = {"count": 0}

        def always_conflicts(**_kwargs):  # type: ignore[no-untyped-def]
            setting_judge_calls["count"] += 1
            return SettingConsistencyResult(
                passed=False,
                violations=[violation],
                validation_source="authoritative_world_contract",
            )

        monkeypatch.setattr(
            generator_module,
            "validate_setting_consistency",
            always_conflicts,
        )
        service = _make_service(workspace_service, session_factory, mock_llm)

        with pytest.raises(ChapterCardGeneratorError) as exc_info:
            service.generate_single_chapter_card(
                workspace_id,
                "ch-0001",
                chapter_number=1,
                volume_id="vol-1",
                card_status="draft",
            )

        assert exc_info.value.error_code == "CHAPTER_CARD_SETTING_CONFLICT"
        assert setting_judge_calls["count"] == 2
        card_file = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "cards"
            / "ch-0001.json"
        )
        assert not card_file.exists()
        session = session_factory(workspace_id)
        try:
            assert session.get(ChapterCardORM, "ch-0001") is None
        finally:
            session.close()

        cards_generated: list[str] = []
        cards_failed: list[dict[str, str]] = []
        service._generate_one_card_for_task(  # noqa: SLF001 — 单卡任务错误码纵切
            workspace_id=workspace_id,
            task_id="task-r3-setting-conflict",
            chapter_id="ch-0001",
            chapter_number=1,
            volume_id="vol-1",
            initial_status="draft",
            cards_generated=cards_generated,
            cards_failed=cards_failed,
        )
        assert cards_generated == []
        assert cards_failed == [
            {
                "chapter_id": "ch-0001",
                "reason": "CHAPTER_CARD_SETTING_CONFLICT",
            }
        ]

        writer_calls = {"count": 0}

        def must_not_call_writer(*_args: object, **_kwargs: object) -> None:
            writer_calls["count"] += 1
            raise AssertionError("坏卡被 gate 拦截后不得调用 Writer")

        monkeypatch.setattr(
            service._chapter_service._client,  # noqa: SLF001 — 产品纵切断言
            "complete",
            must_not_call_writer,
        )
        with pytest.raises(CardNotApproved):
            service._chapter_service.write(workspace_id, "ch-0001")  # noqa: SLF001
        assert writer_calls["count"] == 0

    def test_single_chapter_card_with_pending_review(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        card = service.generate_single_chapter_card(
            workspace_id,
            "ch-0001",
            chapter_number=1,
            volume_id="vol-1",
            card_status="pending_review",
        )
        assert card.card_status == "pending_review"
        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterCardORM, "ch-0001")
        finally:
            session.close()
        assert row is not None
        assert row.card_status == "pending_review"


# ---------------------------------------------------------------------------
# 线性章节卡生成：commit 首卡 + Run 当前卡
# ---------------------------------------------------------------------------


class TestLinearChapterCardGeneration:
    def test_commit_generates_only_first_card_for_review(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        task_id = "task_first_card"
        task_registry.record_pending(
            task_id, type="chapter_card_first_after_commit"
        )

        result = service.generate_first_card_after_commit(
            workspace_id, task_id=task_id
        )

        assert result["chapter_id"] == "ch-0001"
        assert result["cards_generated"] == ["ch-0001"]
        assert result["cards_failed"] == []
        session = session_factory(workspace_id)
        try:
            statuses = {
                row.chapter_id: row.card_status
                for row in session.query(ChapterCardORM)
                .filter(ChapterCardORM.workspace_id == workspace_id)
                .all()
            }
        finally:
            session.close()
        assert statuses == {"ch-0001": "pending_review"}
        task = task_registry.get(task_id)
        assert task is not None and task.status == "done"

    def test_card_task_publishes_full_two_judge_snapshots(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        ws_manager = MagicMock()
        service = _make_service_with_ws(
            workspace_service,
            session_factory,
            mock_llm,
            ws_manager,
        )
        task_id = "task_first_card_parallel_status"
        task_registry.record_pending(
            task_id,
            type="chapter_card_first_after_commit",
        )

        service.generate_first_card_after_commit(workspace_id, task_id=task_id)

        parallel_frames = [
            call.kwargs["data"]
            for call in ws_manager.publish.call_args_list
            if call.kwargs["data"].get("parallel_group")
            == "chapter_card_semantic_gates"
        ]
        assert parallel_frames
        assert all(
            frame["stage"] == "chapter_card_semantic_gates" for frame in parallel_frames
        )
        final = parallel_frames[-1]
        assert [task["judge"] for task in final["tasks"]] == [
            "setting",
            "must_vs_forbidden",
        ]
        assert all(task["status"] == "succeeded" for task in final["tasks"])

    def test_run_generates_exactly_requested_current_card(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        task_id = "task_next_card"
        task_registry.record_pending(task_id, type="chapter_card_generate_next")

        result = service.generate_next_card(
            workspace_id, chapter_id="ch-0002", task_id=task_id
        )

        assert result["chapter_id"] == "ch-0002"
        assert result["cards_generated"] == ["ch-0002"]
        session = session_factory(workspace_id)
        try:
            rows = session.query(ChapterCardORM).all()
        finally:
            session.close()
        assert [(row.chapter_id, row.card_status) for row in rows] == [
            ("ch-0002", "draft")
        ]

    def test_commit_without_volumes_fails_task_without_raising(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _insert_workspace_orm(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        task_id = "task_first_card_no_volumes"
        task_registry.record_pending(
            task_id, type="chapter_card_first_after_commit"
        )

        result = service.generate_first_card_after_commit(
            workspace_id, task_id=task_id
        )

        assert "setup_error" in result
        task = task_registry.get(task_id)
        assert task is not None and task.status == "failed"

    def test_run_target_out_of_range_raises(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        with pytest.raises(ChapterCardServiceError, match="超出全书章节范围"):
            service.generate_next_card(
                workspace_id,
                chapter_id="ch-9999",
                task_id="task_out_of_range",
            )


# ---------------------------------------------------------------------------
# v1.0.x wave 4 session 4:后置同质化检测器 + 自动 retry
# ---------------------------------------------------------------------------


class TestComputeMustAchieveOverlapRatioWave4Session4:
    """``_compute_must_achieve_overlap_ratio`` 纯函数 Jaccard 重合率(模块级 helper)。"""

    def test_identical_lists_returns_1(self) -> None:
        from src.services.chapter_card_service import (
            _compute_must_achieve_overlap_ratio,
        )

        a = ["林衍踏入山门初遇沈雪", "沈雪赠玉佩"]
        b = ["林衍踏入山门初遇沈雪", "沈雪赠玉佩"]
        assert _compute_must_achieve_overlap_ratio(a, b) == 1.0

    def test_disjoint_chars_returns_0(self) -> None:
        from src.services.chapter_card_service import (
            _compute_must_achieve_overlap_ratio,
        )

        a = ["ABC", "DEF"]
        b = ["XYZ", "uvw"]
        assert _compute_must_achieve_overlap_ratio(a, b) == 0.0

    def test_both_empty_returns_0(self) -> None:
        from src.services.chapter_card_service import (
            _compute_must_achieve_overlap_ratio,
        )

        assert _compute_must_achieve_overlap_ratio([], []) == 0.0

    def test_partial_overlap_value_between_0_and_1(self) -> None:
        """两侧共享部分字符 — Jaccard 介于 0 和 1 之间(典型实战 5-25% 区间)。"""
        from src.services.chapter_card_service import (
            _compute_must_achieve_overlap_ratio,
        )

        a = ["林衍踏入山门"]
        b = ["林衍突破炼丹瓶颈"]
        ratio = _compute_must_achieve_overlap_ratio(a, b)
        # 共享:林衍(2 字符);union = 林衍踏入山门 + 突破炼丹瓶颈 ≈ 11-12 unique
        assert 0.0 < ratio < 0.5


def _make_diversification_card(
    *, chapter_number: int, must_achieve: list[str]
):
    """v1.0.x wave 4 session 4 helper:构造 must_achieve 已定制的 ChapterCard。"""
    from src.core.models.chapter_card import ChapterCard

    return ChapterCard(
        chapter_id=f"ch-{chapter_number:04d}",
        workspace_id="ws-test",
        volume_id="vol-1",
        chapter_number=chapter_number,
        arc_id="vol-1-arc-1",
        arc_type="主线",
        arc_phase="opening",
        arc_summary="测试 arc",
        is_arc_finale=False,
        tension_level=5,
        title=f"测试章 {chapter_number}",
        pov_character="lin-yan",
        setting="测试场景",
        must_achieve=must_achieve,
        must_unresolved=[],
        forbidden=[],
        continuity_context={
            "prev_chapter_tail": "",
            "unfinished_actions": [],
            "time_gap": "立即续接",
            "scene_continuation": "switch",
            "pov_change": False,
            "pov_from": None,
            "pov_to": "lin-yan",
        },
        next_chapter_seed="测试种子",
        related_plot_threads=[],
        related_characters=["lin-yan"],
    )


class TestDetectMustAchieveDiversificationWave4Session4:
    """``_detect_must_achieve_diversification`` 配对检测器(模块级 helper)。"""

    def test_pairs_above_threshold_flagged(self) -> None:
        from src.services.chapter_card_service import (
            _detect_must_achieve_diversification,
        )

        cards = [
            _make_diversification_card(
                chapter_number=1, must_achieve=["林衍踏入山门"]
            ),
            _make_diversification_card(
                chapter_number=2, must_achieve=["林衍踏入山门"]
            ),
            _make_diversification_card(
                chapter_number=3, must_achieve=["完全不同的角色登场"]
            ),
        ]
        # ch1 / ch2 字面完全相同 → Jaccard=1.0,超 0.5;ch3 与他们字符集差异大
        flagged = _detect_must_achieve_diversification(cards, threshold=0.5)
        assert flagged == {0, 1}

    def test_below_threshold_returns_empty(self) -> None:
        from src.services.chapter_card_service import (
            _detect_must_achieve_diversification,
        )

        cards = [
            _make_diversification_card(chapter_number=1, must_achieve=["林衍踏入山门"]),
            _make_diversification_card(chapter_number=2, must_achieve=["王沧海潜入丹道阁"]),
            _make_diversification_card(chapter_number=3, must_achieve=["玄玑子展示丹方"]),
        ]
        # 三章字符集差异大,Jaccard 远低于 0.5
        flagged = _detect_must_achieve_diversification(cards, threshold=0.5)
        assert flagged == set()

    def test_single_card_returns_empty(self) -> None:
        from src.services.chapter_card_service import (
            _detect_must_achieve_diversification,
        )

        cards = [_make_diversification_card(chapter_number=1, must_achieve=["孤章"])]
        flagged = _detect_must_achieve_diversification(cards, threshold=0.5)
        assert flagged == set()

    def test_threshold_zero_returns_empty(self) -> None:
        """阈值 ≤ 0 短路返空 set(避免 Jaccard=0 误命中等价配对)。"""
        from src.services.chapter_card_service import (
            _detect_must_achieve_diversification,
        )

        cards = [
            _make_diversification_card(chapter_number=1, must_achieve=["A"]),
            _make_diversification_card(chapter_number=2, must_achieve=["B"]),
        ]
        flagged = _detect_must_achieve_diversification(cards, threshold=0.0)
        assert flagged == set()


class TestCascadeDiversificationRecovery:
    """仅级联重生仍允许对已失效卡集合做同质化恢复。"""

    def test_cascade_path_triggers_auto_retry(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from structlog.testing import capture_logs

        from src.services import chapter_card_service as ccs_module

        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        monkeypatch.setattr(
            ccs_module,
            "_detect_must_achieve_diversification",
            lambda cards, *, threshold: {0, 1},
        )
        monkeypatch.setattr(
            ccs_module,
            "_compute_must_achieve_overlap_ratio",
            lambda _a, _b: 1.0,
        )
        task_id = "task_cascade_auto_retry"
        task_registry.record_pending(
            task_id, type="chapter_card_cascade_regenerate"
        )

        with capture_logs() as logs:
            result = service.regenerate_invalidated_cards_batch(
                workspace_id,
                chapter_ids=["ch-0004", "ch-0005"],
                task_id=task_id,
            )

        stats = result["auto_retry_stats"]
        for chapter_id in ["ch-0004", "ch-0005"]:
            assert stats[chapter_id] == {"attempts": 3, "succeeded": False}
        exhausted_events = [
            event
            for event in logs
            if event.get("event")
            == "chapter_card_batch_generation_auto_retry_exhausted_kept_original"
        ]
        assert len(exhausted_events) == 2


# ---------------------------------------------------------------------------
# 模块导出 sanity
# ---------------------------------------------------------------------------


def test_module_exports() -> None:
    """``__all__`` 完整包含核心类型 — 防漏导出。"""
    from src.services import chapter_card_service as mod

    assert "ChapterCardService" in mod.__all__
    assert "ChapterCardServiceError" in mod.__all__
    assert "ChapterCardContextDerivationError" in mod.__all__
    # P5-B-07 新加
    assert "CardAction" in mod.__all__
    assert "CardInvalidStateTransitionError" in mod.__all__
    assert "CardNotFoundError" in mod.__all__


# ---------------------------------------------------------------------------
# transition_card_status(P5-B-07)
# ---------------------------------------------------------------------------


class _FakeWsManager:
    """记录所有 publish 调用,验 WS event 推送。"""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def publish(
        self, *, channel: str, event: str, data: dict[str, object]
    ) -> None:
        self.events.append({"channel": channel, "event": event, "data": data})


def _seed_card_orm(
    session_factory: Callable[[str], Session],
    workspace_id: str,
    *,
    chapter_id: str = "ch-0001",
    card_status: str = "pending_review",
    rejection_notes: str | None = None,
) -> None:
    session = session_factory(workspace_id)
    try:
        now = datetime.now(timezone.utc)
        session.merge(
            ChapterCardORM(
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                card_status=card_status,
                card_approved_at=None,
                created_at=now,
                rejection_notes=rejection_notes,
            )
        )
        session.commit()
    finally:
        session.close()


def _seed_ssot_card(
    workspace_service: WorkspaceService,
    workspace_id: str,
    chapter_id: str,
    card_status: str,
) -> Path:
    """落最小可读 SSOT card json,验 patch 行为(只读 card_status / card_approved_at)。"""
    card_dir = (
        workspace_service.workspaces_root / workspace_id / "data" / "cards"
    )
    card_dir.mkdir(parents=True, exist_ok=True)
    card_path = card_dir / f"{chapter_id}.json"
    card_path.write_text(
        json.dumps(
            {
                "chapter_id": chapter_id,
                "card_status": card_status,
                "card_approved_at": None,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return card_path


def _make_service_with_ws(
    workspace_service: WorkspaceService,
    session_factory: Callable[[str], Session],
    mock_llm: MockLLMClient,
    ws_manager: object | None,
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
        ws_manager=ws_manager,  # type: ignore[arg-type]
    )


class TestTransitionCardStatus:
    """``transition_card_status`` 状态机(P5-B-07,决策 6=C)。

    覆盖:6 合法转换 + 2 非法(approve→approved / reject→rejected) + ORM 缺失
    + SSOT patch + WS publish + rejection_notes 持久化 + approve 清除 notes。
    """

    def test_a1_approve_runs_dramatic_source_preflight(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _insert_workspace_orm(session_factory, workspace_id)
        _seed_card_orm(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        gate = MagicMock()
        monkeypatch.setattr(
            service,
            "_validate_card_dramatic_source_for_approval",
            gate,
        )

        service.transition_card_status(
            workspace_id,
            "ch-0001",
            action="approve",
        )

        gate.assert_called_once_with(
            workspace_id=workspace_id,
            chapter_id="ch-0001",
        )

    @pytest.mark.parametrize("from_status", ["draft", "pending_review", "rejected"])
    def test_approve_from_legal_sources(
        self,
        from_status: str,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _insert_workspace_orm(session_factory, workspace_id)
        _seed_card_orm(
            session_factory, workspace_id, card_status=from_status
        )
        service = _make_service(workspace_service, session_factory, mock_llm)
        result = service.transition_card_status(
            workspace_id, "ch-0001", action="approve"
        )
        assert result["from_status"] == from_status
        assert result["to_status"] == "approved"
        assert result["card_approved_at"] is not None
        # ORM 写入
        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterCardORM, "ch-0001")
        finally:
            session.close()
        assert row is not None
        assert row.card_status == "approved"
        assert row.card_approved_at is not None
        assert row.rejection_notes is None  # approve 清除 notes

    @pytest.mark.parametrize(
        "from_status", ["draft", "pending_review", "approved"]
    )
    def test_reject_from_legal_sources(
        self,
        from_status: str,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _insert_workspace_orm(session_factory, workspace_id)
        _seed_card_orm(
            session_factory, workspace_id, card_status=from_status
        )
        service = _make_service(workspace_service, session_factory, mock_llm)
        result = service.transition_card_status(
            workspace_id,
            "ch-0001",
            action="reject",
            rejection_notes="人物动机不合理",
        )
        assert result["from_status"] == from_status
        assert result["to_status"] == "rejected"
        assert result["rejection_notes"] == "人物动机不合理"
        # ORM 写入
        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterCardORM, "ch-0001")
        finally:
            session.close()
        assert row is not None
        assert row.card_status == "rejected"
        assert row.rejection_notes == "人物动机不合理"

    def test_approve_from_approved_raises_invalid_transition(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _insert_workspace_orm(session_factory, workspace_id)
        _seed_card_orm(
            session_factory, workspace_id, card_status="approved"
        )
        service = _make_service(workspace_service, session_factory, mock_llm)
        with pytest.raises(CardInvalidStateTransitionError) as exc_info:
            service.transition_card_status(
                workspace_id, "ch-0001", action="approve"
            )
        assert exc_info.value.current_status == "approved"
        assert exc_info.value.action == "approve"
        assert "approved" not in exc_info.value.allowed_sources

    def test_reject_from_rejected_raises_invalid_transition(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _insert_workspace_orm(session_factory, workspace_id)
        _seed_card_orm(
            session_factory, workspace_id, card_status="rejected"
        )
        service = _make_service(workspace_service, session_factory, mock_llm)
        with pytest.raises(CardInvalidStateTransitionError) as exc_info:
            service.transition_card_status(
                workspace_id, "ch-0001", action="reject"
            )
        assert exc_info.value.current_status == "rejected"
        assert exc_info.value.action == "reject"

    def test_orm_row_missing_raises_card_not_found(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _insert_workspace_orm(session_factory, workspace_id)
        # 不 seed card row
        service = _make_service(workspace_service, session_factory, mock_llm)
        with pytest.raises(CardNotFoundError):
            service.transition_card_status(
                workspace_id, "ch-0001", action="approve"
            )

    def test_ssot_card_status_patched_on_approve(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _insert_workspace_orm(session_factory, workspace_id)
        _seed_card_orm(
            session_factory, workspace_id, card_status="pending_review"
        )
        card_path = _seed_ssot_card(
            workspace_service,
            workspace_id,
            "ch-0001",
            card_status="pending_review",
        )
        service = _make_service(workspace_service, session_factory, mock_llm)
        service.transition_card_status(
            workspace_id, "ch-0001", action="approve"
        )
        data = json.loads(card_path.read_text(encoding="utf-8"))
        assert data["card_status"] == "approved"
        assert data["card_approved_at"] is not None
        assert "core_state_contract_version" not in data

    def test_explicit_v1_card_is_revalidated_before_approval(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """新卡携带版本字段时，批准前必须重冻并验证同一权威投影。"""

        _insert_workspace_orm(session_factory, workspace_id)
        _seed_card_orm(
            session_factory, workspace_id, card_status="pending_review"
        )
        service = _make_service(workspace_service, session_factory, mock_llm)
        legacy = _make_card_for_volume(
            chapter_id="ch-0001",
            chapter_number=1,
            volume_id="vol-1",
            workspace_id=workspace_id,
            must_achieve=["建立开篇事件"],
        )
        legacy = legacy.model_copy(
            update={
                "continuity_context": legacy.continuity_context.model_copy(
                    update={"time_gap": "开篇"}
                )
            }
        )
        frozen = CoreStateService(
            session_factory=session_factory
        ).freeze_card_contract(card=legacy, enabled=True)
        service._chapter_service.save_chapter_card(workspace_id, frozen)

        service.transition_card_status(
            workspace_id, "ch-0001", action="approve"
        )

        approved = service._chapter_service.read_chapter_card(
            workspace_id, "ch-0001"
        )
        assert approved.card_status == "approved"
        assert approved.core_state_contract_version == 1
        assert len(approved.core_state_projection_hash) == 64
        assert approved.book_clock_constraint is not None
        assert approved.book_clock_constraint.is_hard is True

    def test_ssot_missing_skipped_silently(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """SSOT 文件不存在时 _patch 静默跳过(不报错,只 log)。"""
        _insert_workspace_orm(session_factory, workspace_id)
        _seed_card_orm(
            session_factory, workspace_id, card_status="pending_review"
        )
        service = _make_service(workspace_service, session_factory, mock_llm)
        # 不 seed SSOT
        result = service.transition_card_status(
            workspace_id, "ch-0001", action="approve"
        )
        assert result["to_status"] == "approved"

    def test_ws_publishes_2_events_on_approve(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _insert_workspace_orm(session_factory, workspace_id)
        _seed_card_orm(
            session_factory, workspace_id, card_status="pending_review"
        )
        fake_ws = _FakeWsManager()
        service = _make_service_with_ws(
            workspace_service, session_factory, mock_llm, fake_ws
        )
        service.transition_card_status(
            workspace_id, "ch-0001", action="approve"
        )
        # 2 个事件:card_status_changed + card_approved
        channels = {e["channel"] for e in fake_ws.events}
        events = [e["event"] for e in fake_ws.events]
        assert channels == {"chapter.ch-0001.card.events"}
        assert events == ["card_status_changed", "card_approved"]
        # Payload 字段
        for e in fake_ws.events:
            data = e["data"]
            assert data["chapter_id"] == "ch-0001"
            assert data["from_status"] == "pending_review"
            assert data["to_status"] == "approved"
            assert "card_approved_at" in data

    def test_ws_publishes_2_events_on_reject_with_notes(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _insert_workspace_orm(session_factory, workspace_id)
        _seed_card_orm(
            session_factory, workspace_id, card_status="pending_review"
        )
        fake_ws = _FakeWsManager()
        service = _make_service_with_ws(
            workspace_service, session_factory, mock_llm, fake_ws
        )
        service.transition_card_status(
            workspace_id,
            "ch-0001",
            action="reject",
            rejection_notes="情节松散",
        )
        events = [e["event"] for e in fake_ws.events]
        assert events == ["card_status_changed", "card_rejected"]
        for e in fake_ws.events:
            data = e["data"]
            assert data["rejection_notes"] == "情节松散"

    def test_approve_clears_previous_rejection_notes(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """reject → approve 序列后,rejection_notes 应被清除。"""
        _insert_workspace_orm(session_factory, workspace_id)
        _seed_card_orm(
            session_factory,
            workspace_id,
            card_status="rejected",
            rejection_notes="前次原因",
        )
        service = _make_service(workspace_service, session_factory, mock_llm)
        service.transition_card_status(
            workspace_id, "ch-0001", action="approve"
        )
        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterCardORM, "ch-0001")
        finally:
            session.close()
        assert row is not None
        assert row.card_status == "approved"
        assert row.rejection_notes is None

    def test_no_session_factory_raises(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        mock_llm: MockLLMClient,
    ) -> None:
        chapter_service = ChapterService(
            workspace_service=workspace_service,
            client=mock_llm,
            model="claude-sonnet-4-6",
        )
        service = ChapterCardService(
            workspace_service=workspace_service,
            chapter_service=chapter_service,
            session_factory=None,
            client=mock_llm,
            model="claude-sonnet-4-6",
        )
        with pytest.raises(ChapterCardServiceError):
            service.transition_card_status(
                workspace_id, "ch-0001", action="approve"
            )


def test_no_session_factory_fails_first_card_task(
    workspace_service: WorkspaceService,
    workspace_id: str,
    mock_llm: MockLLMClient,
) -> None:
    """``session_factory=None`` → 首卡 preflight 失败并由任务兜底。"""
    _write_outline_fixtures(workspace_service, workspace_id)
    from src.services.chapter_service import ChapterService

    chapter_service = ChapterService(
        workspace_service=workspace_service,
        client=mock_llm,
        model="claude-sonnet-4-6",
    )
    service = ChapterCardService(
        workspace_service=workspace_service,
        chapter_service=chapter_service,
        session_factory=None,
        client=mock_llm,
        model="claude-sonnet-4-6",
    )
    task_id = "task_no_session"
    task_registry.record_pending(task_id, type="chapter_card_first_after_commit")
    result = service.generate_first_card_after_commit(
        workspace_id, task_id=task_id
    )
    assert "setup_error" in result


# ---------------------------------------------------------------------------
# v1.0.x wave 5 session 4:retry_references 接 wave 4 forbidden + cascade
# ---------------------------------------------------------------------------


def _make_card_for_volume(
    *,
    chapter_id: str,
    chapter_number: int,
    volume_id: str,
    workspace_id: str,
    must_achieve: list[str],
):
    """v1.0.x wave 5 session 4 helper:构造带卷归属的最小 ChapterCard
    (供 _collect_forbidden / cascade / post-retry check 测试用)。"""
    from src.core.models.chapter_card import ChapterCard

    return ChapterCard(
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        volume_id=volume_id,
        chapter_number=chapter_number,
        arc_id=f"{volume_id}-arc-1",
        arc_type="主线",
        arc_phase="opening",
        arc_summary="测试 arc",
        is_arc_finale=False,
        tension_level=5,
        title=f"测试章 {chapter_number}",
        pov_character="char-protagonist",
        setting="测试场景",
        must_achieve=must_achieve,
        must_unresolved=[],
        forbidden=[],
        continuity_context={
            "prev_chapter_tail": "",
            "unfinished_actions": [],
            "time_gap": "立即续接",
            "scene_continuation": "switch",
            "pov_change": False,
            "pov_from": None,
            "pov_to": "char-protagonist",
        },
        next_chapter_seed="测试种子",
        related_plot_threads=[],
        related_characters=["char-protagonist"],
    )


def _seed_full_card(
    chapter_service: ChapterService,
    session_factory: Callable[[str], Session],
    workspace_id: str,
    *,
    chapter_id: str,
    chapter_number: int,
    volume_id: str,
    must_achieve: list[str],
    card_status: str = "approved",
) -> None:
    """SSOT 文件 + ORM 同时落一张完整章节卡(供 retry / cascade 测试用)。"""
    card = _make_card_for_volume(
        chapter_id=chapter_id,
        chapter_number=chapter_number,
        volume_id=volume_id,
        workspace_id=workspace_id,
        must_achieve=must_achieve,
    ).model_copy(update={"card_status": card_status})
    chapter_service.save_chapter_card(workspace_id, card)
    _seed_card_orm(
        session_factory,
        workspace_id,
        chapter_id=chapter_id,
        card_status=card_status,
    )


class TestCollectForbiddenMustAchieveFromVolumeWave5Session4:
    """``_collect_forbidden_must_achieve_from_volume`` 同卷其他 approved 收集。"""

    def test_collects_only_same_volume_approved_excluding_target(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        # 同卷 approved(应被收集):ch-0001 / ch-0002
        _seed_full_card(
            service._chapter_service, session_factory, workspace_id,
            chapter_id="ch-0001", chapter_number=1, volume_id="vol-1",
            must_achieve=["林衍踏入山门", "拜师沈雪"], card_status="approved",
        )
        _seed_full_card(
            service._chapter_service, session_factory, workspace_id,
            chapter_id="ch-0002", chapter_number=2, volume_id="vol-1",
            must_achieve=["林衍突破炼丹瓶颈"], card_status="approved",
        )
        # 同卷但 draft / rejected(不应被收集)
        _seed_full_card(
            service._chapter_service, session_factory, workspace_id,
            chapter_id="ch-0003", chapter_number=3, volume_id="vol-1",
            must_achieve=["不应被收集 - draft"], card_status="draft",
        )
        # 不同卷 approved(不应被收集)
        _seed_full_card(
            service._chapter_service, session_factory, workspace_id,
            chapter_id="ch-0007", chapter_number=1, volume_id="vol-2",
            must_achieve=["不应被收集 - 其他卷"], card_status="approved",
        )
        # exclude 自身:ch-0004 也 approved 但是 target
        _seed_full_card(
            service._chapter_service, session_factory, workspace_id,
            chapter_id="ch-0004", chapter_number=4, volume_id="vol-1",
            must_achieve=["target 自身不应收集"], card_status="approved",
        )

        forbidden = service._collect_forbidden_must_achieve_from_volume(
            workspace_id,
            volume_id="vol-1",
            exclude_chapter_id="ch-0004",
        )

        assert "林衍踏入山门" in forbidden
        assert "拜师沈雪" in forbidden
        assert "林衍突破炼丹瓶颈" in forbidden
        assert "不应被收集 - draft" not in forbidden
        assert "不应被收集 - 其他卷" not in forbidden
        assert "target 自身不应收集" not in forbidden

    def test_no_session_factory_returns_empty(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        mock_llm: MockLLMClient,
    ) -> None:
        """``session_factory=None``(P1/legacy)→ 空 list(零回归)。"""
        chapter_service = ChapterService(
            workspace_service=workspace_service,
            client=mock_llm,
            model="claude-sonnet-4-6",
        )
        service = ChapterCardService(
            workspace_service=workspace_service,
            chapter_service=chapter_service,
            session_factory=None,
            client=mock_llm,
            model="claude-sonnet-4-6",
        )
        assert service._collect_forbidden_must_achieve_from_volume(
            workspace_id, volume_id="vol-1", exclude_chapter_id="ch-0001"
        ) == []


class TestCollectSubsequentChapterCardIdsWave5Session4:
    """``_collect_subsequent_chapter_card_ids`` cascade 收集。"""

    def test_returns_sorted_ids_greater_than_target(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        # 落 5 张卡 ORM(ch-0001 至 ch-0005,跨卷亦可,只关心字典序)
        for n in range(1, 6):
            _seed_card_orm(
                session_factory, workspace_id,
                chapter_id=f"ch-{n:04d}", card_status="draft",
            )
        cascade = service._collect_subsequent_chapter_card_ids(
            workspace_id=workspace_id, target_chapter_id="ch-0002"
        )
        assert cascade == ["ch-0003", "ch-0004", "ch-0005"]

    def test_last_chapter_returns_empty(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        for n in range(1, 4):
            _seed_card_orm(
                session_factory, workspace_id,
                chapter_id=f"ch-{n:04d}", card_status="draft",
            )
        # target 是最后一章 → 后续空
        assert service._collect_subsequent_chapter_card_ids(
            workspace_id=workspace_id, target_chapter_id="ch-0003"
        ) == []

    def test_no_session_factory_returns_empty(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        mock_llm: MockLLMClient,
    ) -> None:
        chapter_service = ChapterService(
            workspace_service=workspace_service,
            client=mock_llm,
            model="claude-sonnet-4-6",
        )
        service = ChapterCardService(
            workspace_service=workspace_service,
            chapter_service=chapter_service,
            session_factory=None,
            client=mock_llm,
            model="claude-sonnet-4-6",
        )
        assert service._collect_subsequent_chapter_card_ids(
            workspace_id=workspace_id, target_chapter_id="ch-0001"
        ) == []


class TestRetryReferencesCascadeWave5Session4:
    """``retry_references`` cascade 触发 + post-retry diversification log。"""

    def test_rejected_card_retry_uses_setting_evidence_bound_retry(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """正式 UI rejected-card 重生不能绕过一次 Setting 定向修复。"""

        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        _seed_card_orm(
            session_factory,
            workspace_id,
            chapter_id="ch-0002",
            card_status="rejected",
        )
        service = _make_service(workspace_service, session_factory, mock_llm)
        contract = load_setting_contract(
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "outline"
            / "world.json"
        )
        violation = ConstraintViolation(
            constraint_id="9" * 64,
            domain="world.unique_rules",
            source_path=contract.source_path,
            source_hash=contract.source_hash,
            rule_quote=contract.unique_rules,
            evidence_quote="候选卡同时要求启用又禁止必要状态变化",
            severity="soft",
            ambiguity="模型严重度尚未消歧",
        )
        seen_contexts: list[ChapterCardGenerationContext] = []

        def fail_once_then_repair(context, **_kwargs):  # type: ignore[no-untyped-def]
            seen_contexts.append(context)
            if len(seen_contexts) == 1:
                raise ChapterCardGeneratorError(
                    "setting conflict",
                    raw_output="first candidate",
                    model_used="mock",
                    error_code="CHAPTER_CARD_SETTING_CONFLICT",
                    details={
                        "failure_layer": "chapter_card_setting",
                        "contract_hash": contract.source_hash,
                        "blocking_policy": (
                            "authoritative_unique_rule_exact_quotes"
                        ),
                        "constraint_violations": [
                            violation.model_dump(mode="json")
                        ],
                    },
                )
            return _make_card_for_volume(
                chapter_id=context.chapter_id,
                chapter_number=context.chapter_number,
                volume_id=context.volume_id,
                workspace_id=workspace_id,
                must_achieve=["定向修复后的可执行内容"],
            )

        monkeypatch.setattr(
            "src.services.chapter_card_service.generate_chapter_card",
            fail_once_then_repair,
        )

        with capture_logs() as logs:
            result = service.retry_references(
                workspace_id,
                "ch-0002",
                chapter_number=2,
                volume_id="vol-1",
            )

        assert len(seen_contexts) == 2
        assert seen_contexts[0].setting_conflict_retry_violations == []
        assert seen_contexts[1].setting_conflict_retry_violations == [violation]
        assert result.card.card_status == "draft"
        events = [row["event"] for row in logs]
        assert "chapter_card_setting_conflict_retry_started" in events
        assert "chapter_card_setting_conflict_retry_succeeded" in events

    def test_rejected_card_full_path_never_persists_false_passed_repair(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """本地全流程冒烟：第二轮 Judge 漏掉正向复核时旧卡/ORM 原样保留。"""

        import src.core.agents.chapter_card_generator as generator_module

        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        _seed_card_orm(
            session_factory,
            workspace_id,
            chapter_id="ch-0002",
            card_status="rejected",
            rejection_notes="权威规则冲突",
        )
        card_path = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "cards"
            / "ch-0002.json"
        )
        card_path.parent.mkdir(parents=True, exist_ok=True)
        old_card = _make_card_for_volume(
            chapter_id="ch-0002",
            chapter_number=2,
            volume_id="vol-1",
            workspace_id=workspace_id,
            must_achieve=["旧冲突卡保留证据"],
        ).model_copy(
            update={
                "card_status": "rejected",
                "rejection_notes": "权威规则冲突",
            }
        )
        old_bytes = old_card.model_dump_json(indent=2).encode()
        card_path.write_bytes(old_bytes)

        service = _make_service(workspace_service, session_factory, mock_llm)
        contract = load_setting_contract(
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "outline"
            / "world.json"
        )
        violation = ConstraintViolation(
            constraint_id="7" * 64,
            domain="world.unique_rules",
            source_path=contract.source_path,
            source_hash=contract.source_hash,
            rule_quote=contract.unique_rules,
            evidence_quote="候选卡再次启用且禁止必要状态变化",
            severity="hard",
            ambiguity="缺少批准连续性前态",
        )
        setting_calls = 0

        def conflict_then_false_pass(**_kwargs):  # type: ignore[no-untyped-def]
            """两轮都走真实 generator；只替换既有 Setting Provider 响应。"""

            nonlocal setting_calls
            setting_calls += 1
            if setting_calls == 1:
                return SettingConsistencyResult(
                    passed=False,
                    violations=[violation],
                    validation_source="authoritative_world_contract",
                )
            return SettingConsistencyResult(
                passed=True,
                validation_source="authoritative_world_contract",
            )

        monkeypatch.setattr(
            generator_module,
            "validate_setting_consistency",
            conflict_then_false_pass,
        )

        with capture_logs() as logs, pytest.raises(
            ChapterCardGeneratorError
        ) as exc_info:
            service.retry_references(
                workspace_id,
                "ch-0002",
                chapter_number=2,
                volume_id="vol-1",
            )

        assert setting_calls == 2
        assert exc_info.value.error_code == "CHAPTER_CARD_SETTING_CONFLICT"
        assert exc_info.value.details is not None
        assert exc_info.value.details["blocking_policy"] == (
            "required_positive_recheck"
        )
        assert card_path.read_bytes() == old_bytes
        session = session_factory(workspace_id)
        try:
            row = session.get(ChapterCardORM, "ch-0002")
            assert row is not None
            assert row.card_status == "rejected"
            assert row.rejection_notes == "权威规则冲突"
        finally:
            session.close()
        events = [row["event"] for row in logs]
        assert "chapter_card_setting_conflict_retry_started" in events
        assert "chapter_card_setting_conflict_retry_exhausted" in events
        assert "chapter_card_setting_conflict_retry_succeeded" not in events
        assert not any(row.get("agent") == "writer" for row in logs)

    def test_retry_returns_invalidated_ids_when_subsequent_cards_exist(
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
        # CharacterORM 表空 → reference_validator 走"DB 空跳过 unknown 校验"
        # 短路(本测聚焦 cascade 行为,不验 reference 校验主路径)
        service = _make_service(workspace_service, session_factory, mock_llm)
        # 落 5 张卡 ORM(模拟 outline:commit 已生成 batch 前 5 章)
        for n in range(1, 6):
            _seed_card_orm(
                session_factory, workspace_id,
                chapter_id=f"ch-{n:04d}", card_status="draft",
            )

        def _mock_card(ctx, **kwargs):  # type: ignore[no-untyped-def]
            return _make_card_for_volume(
                chapter_id=ctx.chapter_id,
                chapter_number=ctx.chapter_number,
                volume_id=ctx.volume_id,
                workspace_id=workspace_id,
                must_achieve=["retry 后新内容"],
            )

        monkeypatch.setattr(
            "src.services.chapter_card_service.generate_chapter_card",
            _mock_card,
        )

        result = service.retry_references(
            workspace_id, "ch-0003",
            chapter_number=3, volume_id="vol-1",
        )

        # cascade 含 ch-0004 / ch-0005(同 workspace 字典序 > ch-0003)
        assert result.invalidated_chapter_ids == ["ch-0004", "ch-0005"]
        assert result.card.chapter_id == "ch-0003"

    def test_retry_restores_legacy_rewound_outline_character_before_provider(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """旧 rewind 误撤销的书级角色在卡生成前恢复，引用闸不放宽。"""

        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        _insert_character_orms(
            session_factory,
            workspace_id,
            ["char-protagonist", "char-deuter"],
        )
        session = session_factory(workspace_id)
        try:
            row = session.get(CharacterORM, "char-deuter")
            assert row is not None
            row.name = "旧正文污染名"
            row.first_appearance_chapter = "ch-0003"
            row.last_updated_chapter = "ch-0003"
            row.revoked_at = datetime.now(timezone.utc)
            session.commit()
        finally:
            session.close()

        service = _make_service(workspace_service, session_factory, mock_llm)

        def _mock_card(ctx, **kwargs):  # type: ignore[no-untyped-def]
            return _make_card_for_volume(
                chapter_id=ctx.chapter_id,
                chapter_number=ctx.chapter_number,
                volume_id=ctx.volume_id,
                workspace_id=workspace_id,
                must_achieve=["retry 后新内容"],
            )

        monkeypatch.setattr(
            "src.services.chapter_card_service.generate_chapter_card",
            _mock_card,
        )

        service.retry_references(
            workspace_id,
            "ch-0003",
            chapter_number=3,
            volume_id="vol-1",
        )

        session = session_factory(workspace_id)
        try:
            row = session.get(CharacterORM, "char-deuter")
            assert row is not None
            assert row.name == "苏婉清"
            assert row.first_appearance_chapter is None
            assert row.last_updated_chapter is None
            assert row.revoked_at is None
        finally:
            session.close()

    def test_retry_post_check_logs_warning_when_overlap_high(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """retry 后单卡 vs 同卷其他章 must_achieve 高重合 → log warning + 接受。"""
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        # 同卷已有 ch-0001 卡 must_achieve = ["林衍踏入山门初遇沈雪"]
        _seed_full_card(
            service._chapter_service, session_factory, workspace_id,
            chapter_id="ch-0001", chapter_number=1, volume_id="vol-1",
            must_achieve=["林衍踏入山门初遇沈雪"], card_status="approved",
        )

        # retry mock 让 ch-0002 重生后 must_achieve 与 ch-0001 字面完全相同
        # → overlap = 1.0 >= 0.5,触发 warning(单测断言)
        def _mock_dup(ctx, **kwargs):  # type: ignore[no-untyped-def]
            return _make_card_for_volume(
                chapter_id=ctx.chapter_id,
                chapter_number=ctx.chapter_number,
                volume_id=ctx.volume_id,
                workspace_id=workspace_id,
                must_achieve=["林衍踏入山门初遇沈雪"],
            )

        monkeypatch.setattr(
            "src.services.chapter_card_service.generate_chapter_card",
            _mock_dup,
        )

        with capture_logs() as logs:
            service.retry_references(
                workspace_id, "ch-0002",
                chapter_number=2, volume_id="vol-1",
            )

        events = [log["event"] for log in logs]
        assert (
            "chapter_card_retry_references_still_diversification_flagged"
            in events
        ), f"warning 未触发 — events={events}"


class TestRegenerateInvalidatedCardsBatchWave5Session4:
    """``regenerate_invalidated_cards_batch`` cascade 批量重生。"""

    def test_regenerates_specified_chapter_ids(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """聚焦 cascade 流程:指定章被 regenerate + result 字段齐全。

        wave 6 s2 升级:cascade 也接入自动 retry(Q4=A),本测试 monkeypatch
        ``_detect_must_achieve_diversification`` 返空 set 跳过 retry 检测,
        保持测试聚焦原意图;retry 行为另由 wave 6 s2 新测覆盖。"""
        from src.services import chapter_card_service as ccs_module

        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        generated_ids: list[str] = []

        def _mock_card(ctx, **kwargs):  # type: ignore[no-untyped-def]
            generated_ids.append(ctx.chapter_id)
            return _make_card_for_volume(
                chapter_id=ctx.chapter_id,
                chapter_number=ctx.chapter_number,
                volume_id=ctx.volume_id,
                workspace_id=workspace_id,
                must_achieve=[f"重生 {ctx.chapter_id}"],
            )

        monkeypatch.setattr(
            "src.services.chapter_card_service.generate_chapter_card",
            _mock_card,
        )
        # wave 6 s2:cascade 现接入 retry,本测试跳过检测保持原意图聚焦
        monkeypatch.setattr(
            ccs_module,
            "_detect_must_achieve_diversification",
            lambda cards, *, threshold: set(),
        )

        task_id = "cascade-task-1"
        task_registry.record_pending(
            task_id, type="chapter_card_cascade_regenerate"
        )
        result = service.regenerate_invalidated_cards_batch(
            workspace_id,
            chapter_ids=["ch-0004", "ch-0005"],
            task_id=task_id,
        )

        assert sorted(generated_ids) == ["ch-0004", "ch-0005"]
        assert result["cards_generated"] == ["ch-0004", "ch-0005"]
        assert result["cards_failed"] == []
        assert result["cascade_count"] == 2
        # wave 6 s2:auto_retry_stats 字段必含(空 dict 即未触发任何 retry)
        assert result["auto_retry_stats"] == {}
        # task_registry 已更新 done
        info = task_registry.get(task_id)
        assert info is not None and info.status == "done"

    def test_no_volumes_records_setup_failed(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """volumes 表空 → setup 失败 swallow + task update_to_failed。"""
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        # 不调 _insert_volume_orms — volumes 空
        service = _make_service(workspace_service, session_factory, mock_llm)
        task_id = "cascade-task-no-vol"
        task_registry.record_pending(
            task_id, type="chapter_card_cascade_regenerate"
        )
        result = service.regenerate_invalidated_cards_batch(
            workspace_id, chapter_ids=["ch-0001"], task_id=task_id,
        )
        assert "setup_error" in result
        assert result["cards_generated"] == []
        info = task_registry.get(task_id)
        assert info is not None and info.status == "failed"


# ---------------------------------------------------------------------------
# v1.0.x wave 6 s3:prev_chapter_tail 占位字面代码层强制覆盖
# ---------------------------------------------------------------------------


def _build_card_with_continuity_tail(
    chapter_number: int,
    prev_chapter_tail_literal: str,
    *,
    chapter_id: str | None = None,
):
    """构造 ChapterCard 实例供 ``_override_prev_chapter_tail`` helper unit 测试。

    ``prev_chapter_tail_literal`` 模拟 LLM 自由生成的字面(可以是规范 / 旧
    占位 / hallucinated 等任意字面),helper 覆盖后 schema 一致性保持。
    """
    from src.core.models.chapter_card import ChapterCard as ChapterCardModel

    return ChapterCardModel(
        chapter_id=chapter_id or f"ch-{chapter_number:04d}",
        workspace_id="ws-test-wave6-s3",
        volume_id="vol-1",
        chapter_number=chapter_number,
        title="测试章占位",
        tension_level=5,
        must_achieve=["占位事件 A"],
        must_unresolved=["占位悬念"],
        forbidden=["现代用语"],
        pov_character="mock-protagonist",
        setting="测试场景占位",
        continuity_context={
            "prev_chapter_tail": prev_chapter_tail_literal,
            "unfinished_actions": [],
            "time_gap": "立即续接" if chapter_number > 1 else "开篇",
            "scene_continuation": "switch",
            "pov_change": False,
            "pov_from": None if chapter_number == 1 else "mock-protagonist",
            "pov_to": "mock-protagonist",
        },
        next_chapter_seed="测试 seed",
        arc_phase="opening",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=["mock-protagonist"],
    )


class TestOverridePrevChapterTailWave6S3:
    """v1.0.x wave 6 s3:``_override_prev_chapter_tail`` helper + service 层覆盖路径。

    根因:批量章节卡生成阶段 LLM 自由生成 ``prev_chapter_tail`` 字段,prompt
    字面"占位规则"对 LLM 不可强约束 — 实证 ch-2 LLM 写"(上一章正文待生成 — 参
    考 prev_chapter_summary 推论)"/ ch-3 错误 hallucinate"(第一章,无上一章)"。
    service 层 post-LLM 强制覆盖三档分支保证字面规范。
    """

    # ---- helper unit 三档分支 + hallucinate 覆盖 ----

    def test_helper_first_chapter_overrides_to_first_chapter_literal(self) -> None:
        """首章(``chapter_number == 1``):无论 LLM 输出何字面,均覆盖为"(首章,无上一章)"。"""
        from src.services.chapter_card_service import (
            _PREV_CHAPTER_TAIL_FIRST_CHAPTER,
            _override_prev_chapter_tail,
        )

        # LLM 输出旧字面(prompt v4 默认)
        card = _build_card_with_continuity_tail(
            chapter_number=1, prev_chapter_tail_literal="(开篇,无上一章)"
        )
        result = _override_prev_chapter_tail(
            card, chapter_number=1, prev_chapter_tail_input=None
        )
        assert result.continuity_context.prev_chapter_tail == "(首章,无上一章)"
        assert _PREV_CHAPTER_TAIL_FIRST_CHAPTER == "(首章,无上一章)"

    def test_helper_non_first_chapter_with_none_input_overrides_to_not_approved_literal(
        self,
    ) -> None:
        """非首章 + 入参 None(上一章 final.md 不存在,批量阶段常态):覆盖为长字面。"""
        from src.services.chapter_card_service import (
            _PREV_CHAPTER_TAIL_PREV_NOT_APPROVED,
            _override_prev_chapter_tail,
        )

        card = _build_card_with_continuity_tail(
            chapter_number=2,
            prev_chapter_tail_literal="(上一章正文待生成 — 参考 prev_chapter_summary 推断衔接)",
        )
        result = _override_prev_chapter_tail(
            card, chapter_number=2, prev_chapter_tail_input=None
        )
        assert (
            result.continuity_context.prev_chapter_tail
            == _PREV_CHAPTER_TAIL_PREV_NOT_APPROVED
        )
        assert "上一章未 approved" in result.continuity_context.prev_chapter_tail
        assert "从 final.md 重抽真字面" in result.continuity_context.prev_chapter_tail

    def test_helper_non_first_chapter_hallucinated_first_chapter_literal_overridden(
        self,
    ) -> None:
        """**关键场景**:LLM 把非首章 hallucinate 写成"(第一章,无上一章)"
        (luo-cloud-test ch-3 实证字面)→ 必须被代码层覆盖为长字面。"""
        from src.services.chapter_card_service import (
            _PREV_CHAPTER_TAIL_PREV_NOT_APPROVED,
            _override_prev_chapter_tail,
        )

        # ch-3 hallucinate:LLM 错把非首章写成"(第一章,无上一章)"
        card = _build_card_with_continuity_tail(
            chapter_number=3, prev_chapter_tail_literal="(第一章,无上一章)"
        )
        result = _override_prev_chapter_tail(
            card, chapter_number=3, prev_chapter_tail_input=None
        )
        # 必须覆盖 — 否则用户在 UI 上看到非首章却显示"(第一章...)" 体验崩坏
        assert (
            result.continuity_context.prev_chapter_tail
            == _PREV_CHAPTER_TAIL_PREV_NOT_APPROVED
        )

    def test_helper_non_first_chapter_with_real_tail_overrides_to_input_literal(
        self,
    ) -> None:
        """非首章 + 入参非 None(上一章 final.md 已 approved):用入参真字面覆盖 LLM 输出。"""
        from src.services.chapter_card_service import _override_prev_chapter_tail

        real_tail = "他望向远方,身影渐隐入夜色之中。" * 20  # ~480 字模拟末尾段
        # LLM 自由发挥可能写出别的内容,即使是合理段落也应被入参真字面覆盖
        card = _build_card_with_continuity_tail(
            chapter_number=4,
            prev_chapter_tail_literal="LLM 自由发挥的不一致内容占位",
        )
        result = _override_prev_chapter_tail(
            card, chapter_number=4, prev_chapter_tail_input=real_tail
        )
        # 入参真字面优先 — 防 LLM 篡改 ``derive_context`` 已读的 final.md 真字面
        assert result.continuity_context.prev_chapter_tail == real_tail

    def test_helper_cross_volume_first_uses_global_previous_tail(self) -> None:
        """卷内第一章不是全书首章时，必须保留全局上一章 ledger 尾部。"""
        from src.services.chapter_card_service import _override_prev_chapter_tail

        real_tail = "第六章最终段落，林澈仍在解析陈泊留下的逻辑漏洞。"
        card = _build_card_with_continuity_tail(
            chapter_number=1,
            chapter_id="ch-0007",
            prev_chapter_tail_literal="(首章,无上一章)",
        )

        result = _override_prev_chapter_tail(
            card,
            chapter_number=1,
            prev_chapter_tail_input=real_tail,
        )

        assert result.continuity_context.prev_chapter_tail == real_tail

    def test_helper_cross_volume_first_without_final_is_not_book_first(self) -> None:
        """卷内第一章缺前章 final 时走待就绪占位，不能冒充全书首章。"""
        from src.services.chapter_card_service import (
            _PREV_CHAPTER_TAIL_PREV_NOT_APPROVED,
            _override_prev_chapter_tail,
        )

        card = _build_card_with_continuity_tail(
            chapter_number=1,
            chapter_id="ch-0007",
            prev_chapter_tail_literal="(首章,无上一章)",
        )

        result = _override_prev_chapter_tail(
            card,
            chapter_number=1,
            prev_chapter_tail_input=None,
        )

        assert (
            result.continuity_context.prev_chapter_tail
            == _PREV_CHAPTER_TAIL_PREV_NOT_APPROVED
        )

    def test_helper_idempotent_when_card_already_correct(self) -> None:
        """已是规范字面:原样返(避免无意义的 ``model_copy`` 开销)。"""
        from src.services.chapter_card_service import (
            _PREV_CHAPTER_TAIL_FIRST_CHAPTER,
            _override_prev_chapter_tail,
        )

        card = _build_card_with_continuity_tail(
            chapter_number=1,
            prev_chapter_tail_literal=_PREV_CHAPTER_TAIL_FIRST_CHAPTER,
        )
        result = _override_prev_chapter_tail(
            card, chapter_number=1, prev_chapter_tail_input=None
        )
        # 同一对象引用(model_copy 短路)
        assert result is card

    # ---- service 层集成 ----

    def test_service_generate_single_chapter_card_first_chapter_overrides(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """首章 ``ch-0001``:覆盖 mock LLM 旧字面"(开篇,无上一章)" → "(首章,无上一章)"。

        mock fixture 仍输出 prompt v4 旧字面(``mock_client.py:1237``),service
        层覆盖后落 SSOT/ORM 的字面统一规范。
        """
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)

        card = service.generate_single_chapter_card(
            workspace_id,
            "ch-0001",
            chapter_number=1,
            volume_id="vol-1",
            card_status="draft",
        )

        assert card.continuity_context.prev_chapter_tail == "(首章,无上一章)"

        # SSOT 文件落盘字面也已覆盖(下次读卡仍是规范字面)
        card_file = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "cards"
            / "ch-0001.json"
        )
        payload = json.loads(card_file.read_text(encoding="utf-8"))
        assert (
            payload["continuity_context"]["prev_chapter_tail"]
            == "(首章,无上一章)"
        )

    def test_service_generate_single_chapter_card_non_first_chapter_prev_not_approved(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """非首章 ``ch-0002`` + 上一章 ``final.md`` 不存在:覆盖为长字面。

        mock fixture 输出 "(上一章正文待生成 — 参考 prev_chapter_summary 推断
        衔接)" 旧字面 → service 层覆盖为 wave 6 s3 规范长字面。
        """
        from src.services.chapter_card_service import (
            _PREV_CHAPTER_TAIL_PREV_NOT_APPROVED,
        )

        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)

        card = service.generate_single_chapter_card(
            workspace_id,
            "ch-0002",
            chapter_number=2,
            volume_id="vol-1",
            card_status="draft",
        )

        assert (
            card.continuity_context.prev_chapter_tail
            == _PREV_CHAPTER_TAIL_PREV_NOT_APPROVED
        )

    def test_service_generate_single_chapter_card_non_first_chapter_real_final_md(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """非首章 ``ch-0002`` + 上一章 ``ch-0001/final.md`` 存在:覆盖为真字面 800 字。

        模拟 wave 5 s3 真实生产路径 — 上一章已 approved,``_read_prev_chapter_tail``
        返真字面 → service 层用入参真字面覆盖 mock LLM 输出。
        """
        _write_outline_fixtures(workspace_service, workspace_id)
        _insert_workspace_orm(session_factory, workspace_id)

        # 写 ch-0001/final.md 模拟"上一章已 approved 有正文"
        real_final_content = (
            "前夜风雨过后,清晨山门石阶湿滑;松针落了一地,鸟鸣稀疏。" * 30
        )  # 远超 800 字阈值,确保截尾路径走 _TAIL_CHARS=800
        chapter_dir = (
            workspace_service.workspaces_root
            / workspace_id
            / "chapters"
            / "ch-0001"
        )
        chapter_dir.mkdir(parents=True, exist_ok=True)
        (chapter_dir / "final.md").write_text(
            real_final_content, encoding="utf-8"
        )

        service = _make_service(workspace_service, session_factory, mock_llm)
        card = service.generate_single_chapter_card(
            workspace_id,
            "ch-0002",
            chapter_number=2,
            volume_id="vol-1",
            card_status="draft",
        )

        # 真字面末尾 800 字应原样落卡(覆盖 mock LLM 任意输出)
        assert card.continuity_context.prev_chapter_tail == real_final_content[-800:]


# ---------------------------------------------------------------------------
# v1.1 wave 1 session 2 (G6.2):reset_word_count_for_workspace 单测
# ---------------------------------------------------------------------------


def _seed_card_with_word_count(
    chapter_service: ChapterService,
    session_factory: Callable[[str], Session],
    workspace_id: str,
    *,
    chapter_id: str,
    chapter_number: int,
    volume_id: str,
    card_status: str,
    word_count_min: int,
    word_count_max: int,
) -> None:
    """v1.1 wave 1 session 2 helper:落带显式 word_count_min/max 的章节卡
    (SSOT + ORM 同步)。模拟 wave 6 s2.6d-prep 之前旧 default (2800/4500)
    生成的章节卡。"""
    card = _make_card_for_volume(
        chapter_id=chapter_id,
        chapter_number=chapter_number,
        volume_id=volume_id,
        workspace_id=workspace_id,
        must_achieve=["测试 must_achieve"],
    ).model_copy(
        update={
            "card_status": card_status,
            "word_count_min": word_count_min,
            "word_count_max": word_count_max,
        }
    )
    chapter_service.save_chapter_card(workspace_id, card)
    _seed_card_orm(
        session_factory,
        workspace_id,
        chapter_id=chapter_id,
        card_status=card_status,
    )


class TestResetWordCountForWorkspaceWave1Session2:
    """v1.1 wave 1 session 2 (G6.2):工作区级 word_count_min/max backfill 单测。

    覆盖:
    - 默认 scope='unapproved' 跳过 approved,只重置 draft / pending_review / rejected
    - scope='all' 重置全部含 approved
    - 入参 word_count_min/max 覆盖 ChapterCard model default
    - 入参 None 时回退到 model default(2500 / 4000)
    - 校验异常:min > max / min < 1 / max < 1
    - 空工作区返空 list
    - 读盘失败 swallow 计入 failed_chapter_ids(robust)
    """

    def test_explicitly_adopt_new_policy_preserves_card_approval(self, workspace_service,
        workspace_id, session_factory, mock_llm):
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)
        _seed_card_with_word_count(service._chapter_service, session_factory, workspace_id,
            chapter_id="ch-0001", chapter_number=1, volume_id="vol-1", card_status="approved",
            word_count_min=2800, word_count_max=4500)
        original = service._chapter_service.read_chapter_card(workspace_id, "ch-0001")
        result = service.reset_word_count_for_workspace(workspace_id, scope="all",
            word_count_min=2500, word_count_max=3500, word_count_policy_version=1)
        adopted = service._chapter_service.read_chapter_card(workspace_id, "ch-0001")
        assert result["applied_word_count_policy_version"] == 1
        assert adopted.word_count_policy_version == 1
        assert adopted.card_status == original.card_status == "approved"
        assert adopted.must_achieve == original.must_achieve
        assert adopted.continuity_context == original.continuity_context
        service.reset_word_count_for_workspace(workspace_id, scope="all", word_count_min=2600, word_count_max=3600)
        assert service._chapter_service.read_chapter_card(workspace_id, "ch-0001").word_count_policy_version == 1

    def test_default_scope_unapproved_resets_non_approved_only(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)

        # 落 4 张卡:1 draft + 1 pending_review + 1 rejected + 1 approved
        # 模拟 wave 6 s2.6d-prep 之前的旧 default(2800 / 4500)
        _seed_card_with_word_count(
            service._chapter_service, session_factory, workspace_id,
            chapter_id="ch-0001", chapter_number=1, volume_id="vol-1",
            card_status="draft", word_count_min=2800, word_count_max=4500,
        )
        _seed_card_with_word_count(
            service._chapter_service, session_factory, workspace_id,
            chapter_id="ch-0002", chapter_number=2, volume_id="vol-1",
            card_status="pending_review", word_count_min=2800, word_count_max=4500,
        )
        _seed_card_with_word_count(
            service._chapter_service, session_factory, workspace_id,
            chapter_id="ch-0003", chapter_number=3, volume_id="vol-1",
            card_status="rejected", word_count_min=2800, word_count_max=4500,
        )
        _seed_card_with_word_count(
            service._chapter_service, session_factory, workspace_id,
            chapter_id="ch-0004", chapter_number=4, volume_id="vol-1",
            card_status="approved", word_count_min=2800, word_count_max=4500,
        )

        result = service.reset_word_count_for_workspace(workspace_id)

        # 默认 scope='unapproved' + 默认入参(None → model default 2500/4000)
        assert result["scope"] == "unapproved"
        assert result["applied_word_count_min"] == 2500
        assert result["applied_word_count_max"] == 4000
        assert result["reset_chapter_ids"] == ["ch-0001", "ch-0002", "ch-0003"]
        assert result["skipped_chapter_ids"] == ["ch-0004"]
        assert result["failed_chapter_ids"] == []

        # SSOT 文件 4 张卡:前 3 张 word_count 重置;approved 一张不动
        for cid in ("ch-0001", "ch-0002", "ch-0003"):
            card = service._chapter_service.read_chapter_card(workspace_id, cid)
            assert card.word_count_min == 2500
            assert card.word_count_max == 4000
        approved_card = service._chapter_service.read_chapter_card(
            workspace_id, "ch-0004"
        )
        assert approved_card.word_count_min == 2800
        assert approved_card.word_count_max == 4500

    def test_scope_all_resets_all_including_approved(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)

        _seed_card_with_word_count(
            service._chapter_service, session_factory, workspace_id,
            chapter_id="ch-0001", chapter_number=1, volume_id="vol-1",
            card_status="draft", word_count_min=2800, word_count_max=4500,
        )
        _seed_card_with_word_count(
            service._chapter_service, session_factory, workspace_id,
            chapter_id="ch-0002", chapter_number=2, volume_id="vol-1",
            card_status="approved", word_count_min=2800, word_count_max=4500,
        )

        result = service.reset_word_count_for_workspace(
            workspace_id, scope="all"
        )

        assert result["scope"] == "all"
        assert result["reset_chapter_ids"] == ["ch-0001", "ch-0002"]
        assert result["skipped_chapter_ids"] == []
        assert result["failed_chapter_ids"] == []

        # 即使 approved 也被重置
        approved_card = service._chapter_service.read_chapter_card(
            workspace_id, "ch-0002"
        )
        assert approved_card.word_count_min == 2500
        assert approved_card.word_count_max == 4000

    def test_explicit_word_count_overrides_model_default(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)

        _seed_card_with_word_count(
            service._chapter_service, session_factory, workspace_id,
            chapter_id="ch-0001", chapter_number=1, volume_id="vol-1",
            card_status="draft", word_count_min=2800, word_count_max=4500,
        )

        result = service.reset_word_count_for_workspace(
            workspace_id,
            scope="all",
            word_count_min=2000,
            word_count_max=3500,
        )

        assert result["applied_word_count_min"] == 2000
        assert result["applied_word_count_max"] == 3500

        card = service._chapter_service.read_chapter_card(
            workspace_id, "ch-0001"
        )
        assert card.word_count_min == 2000
        assert card.word_count_max == 3500

    def test_none_min_max_fallback_to_chapter_card_model_default(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """word_count_min/max=None → 用 ChapterCard model SSOT default。

        防漂移网:此测试同时校验"ChapterCard model default 未被偷偷改",改 default
        时本测试会同步失败,避免 G6 三件套 default 被悄悄改后行为对不上。
        """
        from src.core.models.chapter_card import ChapterCard as ChapterCardModel
        assert (
            ChapterCardModel.model_fields["word_count_min"].default == 2500
        ), "ChapterCard.word_count_min default 已偏离 2500(G6 三件套 default 锚)"
        assert (
            ChapterCardModel.model_fields["word_count_max"].default == 4000
        ), "ChapterCard.word_count_max default 已偏离 4000(G6 三件套 default 锚)"

        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)

        _seed_card_with_word_count(
            service._chapter_service, session_factory, workspace_id,
            chapter_id="ch-0001", chapter_number=1, volume_id="vol-1",
            card_status="draft", word_count_min=2800, word_count_max=4500,
        )

        result = service.reset_word_count_for_workspace(
            workspace_id,
            scope="all",
            word_count_min=None,
            word_count_max=None,
        )

        assert result["applied_word_count_min"] == 2500
        assert result["applied_word_count_max"] == 4000

    def test_min_greater_than_max_raises(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        _insert_workspace_orm(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)

        with pytest.raises(ChapterCardServiceError) as excinfo:
            service.reset_word_count_for_workspace(
                workspace_id, word_count_min=5000, word_count_max=3000
            )
        assert "不能大于" in str(excinfo.value)

    def test_empty_workspace_returns_empty_lists(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """无章节卡的工作区:所有 list 空 + applied 字段填模型 default。"""
        _insert_workspace_orm(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)

        result = service.reset_word_count_for_workspace(workspace_id)

        assert result["workspace_id"] == workspace_id
        assert result["reset_chapter_ids"] == []
        assert result["skipped_chapter_ids"] == []
        assert result["failed_chapter_ids"] == []
        assert result["applied_word_count_min"] == 2500
        assert result["applied_word_count_max"] == 4000

    def test_failed_chapter_id_when_ssot_read_fails(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """ORM 有行但 SSOT 文件缺失 → 该章计入 failed_chapter_ids,
        其他章节继续重置(swallow + log + 不阻断)。"""
        _insert_workspace_orm(session_factory, workspace_id)
        _insert_volume_orms(session_factory, workspace_id)
        service = _make_service(workspace_service, session_factory, mock_llm)

        # ch-0001 完整 SSOT + ORM(正常)
        _seed_card_with_word_count(
            service._chapter_service, session_factory, workspace_id,
            chapter_id="ch-0001", chapter_number=1, volume_id="vol-1",
            card_status="draft", word_count_min=2800, word_count_max=4500,
        )
        # ch-0002 ORM 有行 + SSOT 文件不存在(模拟人工删 / race destroy)
        _seed_card_orm(
            session_factory, workspace_id,
            chapter_id="ch-0002", card_status="draft",
        )

        result = service.reset_word_count_for_workspace(workspace_id)

        # ch-0001 成功;ch-0002 SSOT 读失败计入 failed
        assert result["reset_chapter_ids"] == ["ch-0001"]
        assert result["failed_chapter_ids"] == ["ch-0002"]
        assert result["skipped_chapter_ids"] == []


# ---------------------------------------------------------------------------
# I2.1:风格模板影响 derive_context 生成默认(word_count / plot_node_budget /
# must_achieve_max)。Approach A:注册表 SSOT,derive 时实时读 style_template。
# ---------------------------------------------------------------------------


class TestDeriveContextI2_1StyleGeneration:
    def _create_styled_ws(
        self,
        workspace_service: WorkspaceService,
        ws_id: str,
        style_template: str | None,
    ) -> None:
        workspace_service.create(
            ws_id,
            display_name="I2.1 derive test",
            genre="xianxia",
            premise="test",
            style_template=style_template,
            activate=False,
        )
        _write_outline_fixtures(workspace_service, ws_id)

    def test_comedic_burst_preserves_approximate_book_length(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """约数篇幅遵循本书，剧情预算仍来自模板。"""
        self._create_styled_ws(workspace_service, "ws-comedic", "comedic_burst")
        service = _make_service(workspace_service, session_factory, mock_llm)
        ctx = service.derive_context(
            "ws-comedic", "ch-0001", chapter_number=1, volume_id="vol-1"
        )
        assert ctx.default_word_count_min == 2000
        assert ctx.default_word_count_max == 2000
        assert ctx.word_count_policy_version == 2
        assert ctx.default_plot_node_budget == 5
        assert ctx.default_must_achieve_max == 5

    def test_grand_epic_preserves_approximate_book_length(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        self._create_styled_ws(workspace_service, "ws-grand", "grand_epic")
        service = _make_service(workspace_service, session_factory, mock_llm)
        ctx = service.derive_context(
            "ws-grand", "ch-0001", chapter_number=1, volume_id="vol-1"
        )
        assert ctx.default_word_count_min == 2000
        assert ctx.default_word_count_max == 2000
        assert ctx.default_plot_node_budget == 3
        assert ctx.default_must_achieve_max == 3

    def test_no_style_template_falls_back_engine_default(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """未选模板 → word_count/budget 走引擎默认,must_achieve_max 为 None(零回归)。"""
        self._create_styled_ws(workspace_service, "ws-plain", None)
        service = _make_service(workspace_service, session_factory, mock_llm)
        ctx = service.derive_context(
            "ws-plain", "ch-0001", chapter_number=1, volume_id="vol-1"
        )
        assert ctx.default_word_count_min == 2000
        assert ctx.default_word_count_max == 2000
        assert ctx.word_count_policy_version == 2
        assert ctx.default_plot_node_budget == 3
        assert ctx.default_must_achieve_max is None

    def test_project_yaml_l1_override_wins_over_style(
        self,
        workspace_service: WorkspaceService,
        session_factory: Callable[[str], Session],
        mock_llm: MockLLMClient,
    ) -> None:
        """project.yaml L1 用户覆盖字段级优先于风格基线;未填字段保留风格基线。"""
        self._create_styled_ws(workspace_service, "ws-l1", "comedic_burst")
        proj = workspace_service.workspaces_root / "ws-l1" / "project.yaml"
        proj.write_text("generation:\n  word_count_min: 3200\n", encoding="utf-8")
        service = _make_service(workspace_service, session_factory, mock_llm)
        ctx = service.derive_context(
            "ws-l1", "ch-0001", chapter_number=1, volume_id="vol-1"
        )
        # word_count_min 被 L1 覆盖;其余保留 comedic_burst 风格基线。
        assert ctx.default_word_count_min == 3200
        assert ctx.default_word_count_max == 3500
        assert ctx.default_plot_node_budget == 5
        # must_achieve_max 不走 GenerationConfig(无 L1 覆盖),仍是风格注册表值。
        assert ctx.default_must_achieve_max == 5
