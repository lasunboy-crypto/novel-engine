"""ChapterService P6-B-09 ``ChapterCard.force_profile`` 集成测试。

P6-B-09 在章节卡 Pydantic 模型加 ``force_profile: Literal[...] | None`` 字段持久化覆盖,
与 P3-B-11 单次 ``profile_override`` 互补——章节卡级覆盖优先于单次 request body
覆盖。本测试覆盖 plan §10 字面 5 类:

- **happy**:``card.force_profile="cloud_premium"`` → write 时所有 agent 命中
  cloud_premium 模型;PUT card 后 GET 返同值字段。
- **error**:``card.force_profile`` 是 Literal 内的合法字面值但 ``writer_profiles``
  未注册 → write 抛 ``WriterProfileNotFoundError`` → API 422
  ``WRITER_PROFILE_NOT_FOUND`` + details 含 ``profile_id`` / ``known_profiles``。
- **gate**:``force_profile=None`` 走默认路由(回归 P3-B-11 既有行为);
  ``force_profile + profile_override`` 同时给定 → force_profile 优先(章节卡持久化
  优先于请求 transient)。
- **rollback**:``force_profile`` 越界时 ``chapters`` 表行未修改(校验在
  ``_upsert_chapter_state`` 之前)。
- **WS event**:force_profile 触发的 chapter:write 调用同既有 ``on_state_transition``
  回调(无新事件 — 章节卡级 wrap 不引入新 WS 频道)。

走真 ``MockLLMClient`` + 真 ``ChapterService`` + 真 ``ModelRouter``,mock 仅 LLM
provider 与 transition 回调。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.api import task_registry
from src.api.deps import get_chapter_service
from src.api.main import create_app
from src.config import settings
from src.core.agents import _prompt as prompt_module
from src.core.db import Base
from src.core.db.models import ChapterORM
from src.core.llm.mock_client import MockLLMClient
from src.core.memory.vector import VectorStore
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.routing import RoutingConfig, WriterProfilesConfig
from src.core.models.writer_profile import WriterProfile
from src.core.routing.router import ModelRouter
from src.core.state_machine.states import ChapterState
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.chapter_service import ChapterService
from src.services.profile_override import WriterProfileNotFoundError
from src.services.workspace_service import WorkspaceService

pytestmark = pytest.mark.usefixtures("valid_character_admission")


# ---- fixtures + helpers -----------------------------------------------------


def _mock_embedding_fn(texts: list[str]) -> list[list[float]]:
    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


def _make_continuity(**overrides: object) -> ContinuityContext:
    payload: dict = dict(
        prev_chapter_tail="",
        unfinished_actions=[],
        time_gap="新场景",
        scene_continuation="switch",
        pov_change=False,
        pov_from=None,
        pov_to="林衍",
    )
    payload.update(overrides)
    return ContinuityContext(**payload)


def _make_card(
    chapter_id: str = "ch-0001",
    chapter_number: int = 1,
    *,
    workspace_id: str = "ws-force",
    force_profile: str | None = None,
) -> ChapterCard:
    """构造测试章节卡。``force_profile`` 显式参数,默认 None(回归 baseline)。"""
    return ChapterCard(
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        volume_id="v01",
        chapter_number=chapter_number,
        title=f"第 {chapter_number} 章·force_profile 测试",
        tension_level=6,
        word_count_min=1,
        word_count_max=10**8,
        plot_node_budget=2,
        must_achieve=[],
        must_unresolved=[],
        forbidden=[],
        pov_character="林衍",
        setting="测试场景",
        continuity_context=_make_continuity(),
        next_chapter_seed="待续",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=["lin-yan"],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=1,
        max_revises=1,
        force_profile=force_profile,  # type: ignore[arg-type]
    )


def _build_config_with_4_profiles(
    *,
    default: str = "cloud_quality",
    agent_routing: dict[str, str] | None = None,
    only: tuple[str, ...] | None = None,
) -> WriterProfilesConfig:
    """4-profile 配置(对齐 ChapterCard.force_profile Literal 4 档真实命名)。

    ``only`` 给定时仅注册子集(``error`` 测试故意漏注册某 profile_id,触发
    ``WRITER_PROFILE_NOT_FOUND``);默认全注册 4 档。每 profile 模型名后缀
    profile_id 便于断言 client_factory 收到哪些模型。
    """
    all_profiles = {
        "cloud_quality": WriterProfile(
            profile_id="cloud_quality",
            provider="mock",
            model="mock-cloud_quality",
            context_window=16000,
        ),
        "cloud_premium": WriterProfile(
            profile_id="cloud_premium",
            provider="mock",
            model="mock-cloud_premium",
            context_window=32000,
        ),
        "local_qwen_32k": WriterProfile(
            profile_id="local_qwen_32k",
            provider="mock",
            model="mock-local_qwen_32k",
            context_window=32000,
        ),
        "local_qwen_safe": WriterProfile(
            profile_id="local_qwen_safe",
            provider="mock",
            model="mock-local_qwen_safe",
            context_window=16000,
        ),
    }
    if only is not None:
        profiles = {k: all_profiles[k] for k in only}
    else:
        profiles = all_profiles
    return WriterProfilesConfig(
        profiles=profiles,
        routing=RoutingConfig(
            default=default,
            rules=[],
            agent_routing=agent_routing or {},
        ),
    )


@pytest.fixture
def workspace_service(tmp_path: Path) -> WorkspaceService:
    workspaces_root = tmp_path / "workspaces"
    archives_root = tmp_path / "archives"
    workspaces_root.mkdir()
    archives_root.mkdir()
    return WorkspaceService(
        workspaces_root=workspaces_root, archives_root=archives_root
    )


def _make_session_factory(
    workspace_service: WorkspaceService,
) -> Callable[[str], Session]:
    def factory(workspace_id: str) -> Session:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        Base.metadata.create_all(engine)
        return Session(engine)

    return factory


def _make_vector_store_factory(
    workspace_service: WorkspaceService,
) -> Callable[[str], VectorStore]:
    def factory(workspace_id: str) -> VectorStore:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        return VectorStore(
            workspace_id,
            embedding_fn=_mock_embedding_fn,
            persist_dir=paths.chroma_dir,
        )

    return factory


@pytest.fixture
def prepared_workspace(workspace_service: WorkspaceService) -> str:
    workspace_service.create(
        "ws-force",
        display_name="Force Profile 集成",
        genre="test",
        premise="验证 P6-B-09 force_profile",
        total_chapters=3,
    )
    return "ws-force"


def _build_service_with_router(
    workspace_service: WorkspaceService,
    router: ModelRouter,
) -> ChapterService:
    return ChapterService(
        workspace_service=workspace_service,
        client=MockLLMClient(),
        model="legacy-model",
        session_factory=_make_session_factory(workspace_service),
        vector_store_factory=_make_vector_store_factory(workspace_service),
        router_factory=lambda ws_id: router,
    )


# ---- 1. happy:write 命中 force_profile -------------------------------------


class TestForceProfileHappyPath:
    """``card.force_profile`` 非空 → 所有 agent 命中该 profile,不论 agent_routing。"""

    def test_write_with_force_profile_routes_all_agents(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """章节卡 force_profile=cloud_premium → 即使 agent_routing 全部指向
        cloud_quality,所有 agent 仍命中 cloud_premium 模型。"""
        config = _build_config_with_4_profiles(
            default="cloud_quality",
            agent_routing={
                "writer": "cloud_quality",
                "reviewer": "cloud_quality",
                "memory_extractor": "cloud_quality",
            },
        )
        constructed_models: list[str] = []

        def _factory(**kw: object) -> MockLLMClient:
            constructed_models.append(str(kw["model"]))
            return MockLLMClient()

        router = ModelRouter(config, client_factory=_factory)
        service = _build_service_with_router(workspace_service, router)
        service.save_chapter_card(
            prepared_workspace,
            _make_card("ch-0001", 1, force_profile="cloud_premium"),
        )

        result = service.write(prepared_workspace, "ch-0001")

        assert result.final_state == ChapterState.APPROVED
        # 所有 agent 走 cloud_premium → 只构造一种 client
        assert constructed_models == ["mock-cloud_premium"]

    def test_rewrite_with_force_profile_routes_all_agents(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """rewrite 路径同走 force_profile(与 write 同 _orchestrate 入口)。"""
        config = _build_config_with_4_profiles(default="cloud_quality")
        constructed_models: list[str] = []
        routed_client = MockLLMClient()

        def _factory(**kw: object) -> MockLLMClient:
            constructed_models.append(str(kw["model"]))
            return routed_client

        router = ModelRouter(config, client_factory=_factory)
        service = _build_service_with_router(workspace_service, router)
        service.save_chapter_card(
            prepared_workspace,
            _make_card("ch-0001", 1, force_profile="local_qwen_32k"),
        )

        first = service.write(prepared_workspace, "ch-0001")
        calls_before_rewrite = routed_client._total_calls
        result = service.rewrite(
            prepared_workspace, "ch-0001", prior_rewrite_count=0
        )

        assert result.final_state == ChapterState.APPROVED
        assert constructed_models == ["mock-local_qwen_32k"]
        assert first.final_version_id == 1
        assert routed_client._total_calls > calls_before_rewrite


# ---- 2. gate ----------------------------------------------------------------


class TestForceProfileGate:
    """``force_profile=None`` 回归 agent_routing;``force_profile + profile_override``
    同时给定时 force_profile 优先。"""

    def test_none_force_profile_falls_back_to_agent_routing(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """``force_profile=None`` → 走 agent_routing(writer→cloud_quality,
        reviewer→cloud_premium 两 profile 都被构造)。"""
        config = _build_config_with_4_profiles(
            default="cloud_quality",
            agent_routing={
                "writer": "cloud_quality",
                "reviewer": "cloud_premium",
                "memory_extractor": "cloud_quality",
            },
        )
        router = ModelRouter(config)
        service = _build_service_with_router(workspace_service, router)
        service.save_chapter_card(
            prepared_workspace, _make_card("ch-0001", 1, force_profile=None)
        )

        result = service.write(prepared_workspace, "ch-0001")

        assert result.final_state == ChapterState.APPROVED
        # 2 profile 都被命中(writer/extractor=cloud_quality, reviewer=cloud_premium)
        assert len(router._client_cache) == 2

    def test_force_profile_wins_over_profile_override(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """章节卡 force_profile=cloud_premium + 请求 body profile_override=
        cloud_quality → force_profile 优先(章节卡持久化 > 单次请求 transient)。"""
        config = _build_config_with_4_profiles(default="cloud_quality")
        constructed_models: list[str] = []

        def _factory(**kw: object) -> MockLLMClient:
            constructed_models.append(str(kw["model"]))
            return MockLLMClient()

        router = ModelRouter(config, client_factory=_factory)
        service = _build_service_with_router(workspace_service, router)
        service.save_chapter_card(
            prepared_workspace,
            _make_card("ch-0001", 1, force_profile="cloud_premium"),
        )

        result = service.write(
            prepared_workspace, "ch-0001", profile_override="cloud_quality"
        )

        assert result.final_state == ChapterState.APPROVED
        # force_profile 优先 → 全部 cloud_premium,profile_override 被覆盖
        assert constructed_models == ["mock-cloud_premium"]


# ---- 3. error:越界 + 4. rollback -------------------------------------------


class TestForceProfileErrorAndRollback:
    """``force_profile`` 是 Literal 内合法字面值但 ``writer_profiles`` 未注册
    → 抛 ``WriterProfileNotFoundError`` + ``chapters`` 表未修改。"""

    def test_force_profile_unknown_raises_writer_profile_not_found(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """deployment 配置漂移:只注册 cloud_quality + cloud_premium,但卡
        force_profile=local_qwen_32k(Literal 合法,运行时未注册)→ 抛
        ``WriterProfileNotFoundError``,details 含 ``profile_id`` 与
        ``known_profiles`` 列表(便于前端引导改卡)。"""
        config = _build_config_with_4_profiles(
            default="cloud_quality",
            only=("cloud_quality", "cloud_premium"),
        )
        router = ModelRouter(config)
        service = _build_service_with_router(workspace_service, router)
        service.save_chapter_card(
            prepared_workspace,
            _make_card("ch-0001", 1, force_profile="local_qwen_32k"),
        )

        with pytest.raises(WriterProfileNotFoundError) as excinfo:
            service.write(prepared_workspace, "ch-0001")

        assert "local_qwen_32k" in str(excinfo.value)
        details = excinfo.value.details  # type: ignore[attr-defined]
        assert details["profile_id"] == "local_qwen_32k"
        assert sorted(details["known_profiles"]) == [
            "cloud_premium",
            "cloud_quality",
        ]

    def test_force_profile_unknown_does_not_advance_chapter_status(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """rollback:force_profile 越界时 ``chapters`` 表行**未被 upsert**
        (校验在 ``_upsert_chapter_state`` 之前)。"""
        config = _build_config_with_4_profiles(
            default="cloud_quality",
            only=("cloud_quality",),
        )
        router = ModelRouter(config)
        service = _build_service_with_router(workspace_service, router)
        service.save_chapter_card(
            prepared_workspace,
            _make_card("ch-0001", 1, force_profile="cloud_premium"),
        )

        with pytest.raises(WriterProfileNotFoundError):
            service.write(prepared_workspace, "ch-0001")

        # ``chapters`` 行未创建(_upsert_chapter_state 未被调用)
        session_factory = _make_session_factory(workspace_service)
        session = session_factory(prepared_workspace)
        try:
            row = session.get(ChapterORM, "ch-0001")
            assert row is None, (
                f"force_profile 越界时 chapters 行不应被 upsert,实际: "
                f"{row.status if row else None}"
            )
        finally:
            session.close()


# ---- 5. WS event:force_profile 触发 write 调用既有 on_state_transition ------


class TestForceProfileWSEvent:
    """force_profile 路径不引入新 WS 事件:既有 ``on_state_transition`` 回调
    照常被调用(由 API 路由 ``_make_state_transition_callback`` 推
    ``chapter.{id}.events``)。"""

    def test_force_profile_invokes_state_transition_callback(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """传入 mock callback,断言 force_profile 路径下 callback 被多次调用
        (与 baseline write 行为一致 — 至少包含 APPROVED 终态)。"""
        config = _build_config_with_4_profiles(default="cloud_quality")
        router = ModelRouter(config)
        service = _build_service_with_router(workspace_service, router)
        service.save_chapter_card(
            prepared_workspace,
            _make_card("ch-0001", 1, force_profile="cloud_premium"),
        )

        transitions: list[tuple[str, str]] = []

        def _cb(prev: ChapterState, nxt: ChapterState) -> None:
            transitions.append((prev.value, nxt.value))

        result = service.write(prepared_workspace, "ch-0001", on_state_transition=_cb)

        assert result.final_state == ChapterState.APPROVED
        # callback 被调用 ≥ 1 次,且终态 APPROVED 必在内
        assert any(nxt == "approved" for _, nxt in transitions)


# =============================================================================
# API 层 — PUT/GET 圆 trip + 422 翻译
# =============================================================================


@pytest.fixture()
def fake_prompts_dir(tmp_path: Path) -> Path:
    """tmp_path 下建假模板目录,Writer / Reviewer / Reviser agent 共享。"""
    p = tmp_path / "prompts_root"
    p.mkdir()
    (p / "writer.md").write_text("FAKE_WRITER", encoding="utf-8")
    (p / "reviewer.md").write_text("FAKE_REVIEWER", encoding="utf-8")
    (p / "reviser.md").write_text("FAKE_REVISER", encoding="utf-8")
    (p / "chapter_summary_extractor_v2.md").write_text(
        "FAKE_CHAPTER_SUMMARY_EXTRACTOR", encoding="utf-8"
    )
    (p / "chapter_summary_extractor_v3.md").write_text(
        "FAKE_CHAPTER_SUMMARY_EXTRACTOR_SOURCE_REFS", encoding="utf-8"
    )
    return p


@pytest.fixture(autouse=True)
def _patch_prompts_dir(
    monkeypatch: pytest.MonkeyPatch, fake_prompts_dir: Path
) -> None:
    monkeypatch.setattr(settings, "prompts_dir", fake_prompts_dir)


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> Iterator[None]:
    prompt_module.load_prompt_template.cache_clear()
    yield
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture(autouse=True)
def _clear_task_registry() -> Iterator[None]:
    task_registry.clear()
    yield
    task_registry.clear()


def _hdr(workspace_id: str = "ws-force") -> dict[str, str]:
    return {"X-Workspace-Id": workspace_id}


class TestForceProfileApi:
    """API 层 PUT/GET 圆 trip + 422 翻译(WRITER_PROFILE_NOT_FOUND)。"""

    def test_put_card_with_force_profile_round_trips_to_get(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """PUT /chapters/{id}/card body 含 force_profile=cloud_premium →
        GET 返同值字段(SSOT JSON 文件持久化)。"""
        chapter_service = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="legacy-model",
        )
        app = create_app()
        app.dependency_overrides[get_chapter_service] = lambda: chapter_service

        card = _make_card("ch-0001", 1, force_profile="cloud_premium")
        with TestClient(app) as client:
            put_resp = client.put(
                "/api/v1/chapters/ch-0001/card",
                headers=_hdr(),
                json=json.loads(card.model_dump_json()),
            )
            assert put_resp.status_code == 200
            assert put_resp.json()["data"]["force_profile"] == "cloud_premium"

            get_resp = client.get(
                "/api/v1/chapters/ch-0001/card", headers=_hdr()
            )
            assert get_resp.status_code == 200
            assert get_resp.json()["data"]["force_profile"] == "cloud_premium"
        app.dependency_overrides.clear()

    def test_write_with_unknown_force_profile_returns_422(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """章节卡 force_profile=local_qwen_safe 但 router 只注册 cloud_quality
        → POST /chapters/{id}:write 返 422 ``WRITER_PROFILE_NOT_FOUND`` +
        error.details 含 ``profile_id`` / ``known_profiles``。"""
        config = _build_config_with_4_profiles(
            default="cloud_quality", only=("cloud_quality",)
        )
        router = ModelRouter(config)
        chapter_service = _build_service_with_router(workspace_service, router)
        chapter_service.save_chapter_card(
            prepared_workspace,
            _make_card("ch-0001", 1, force_profile="local_qwen_safe"),
        )

        app = create_app()
        app.dependency_overrides[get_chapter_service] = lambda: chapter_service
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/chapters/ch-0001:write", headers=_hdr()
            )
        app.dependency_overrides.clear()

        # v1.0.x Commit B::write 改 BackgroundTasks 异步,WriterProfileNotFound
        # 在 service.write 内抛 → wrapper update_to_failed +
        # error.code=WRITER_PROFILE_NOT_FOUND。HTTP 仍 202(异步语义)。
        assert resp.status_code == 202
        info = resp.json()["data"]
        from src.api import task_registry as _tr

        final = _tr.get(info["task_id"])
        assert final is not None
        assert final.status == "failed"
        assert final.error["code"] == "WRITER_PROFILE_NOT_FOUND"
        details = final.error["details"]
        assert details["profile_id"] == "local_qwen_safe"
        assert details["known_profiles"] == ["cloud_quality"]

    def test_put_card_with_invalid_force_profile_literal_returns_422(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """PUT body 含 force_profile="totally_unknown_profile"(不在 Literal
        4 档列表)→ Pydantic 校验 422 ``VALIDATION_FAILED``(早于
        WRITER_PROFILE_NOT_FOUND 路径,前端两层错码都需要处理)。"""
        chapter_service = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="legacy-model",
        )
        app = create_app()
        app.dependency_overrides[get_chapter_service] = lambda: chapter_service

        card = _make_card("ch-0001", 1)
        # 绕 Pydantic 构造直接 dict 注入非法字面值(模拟前端 raw 报文)
        payload = json.loads(card.model_dump_json())
        payload["force_profile"] = "totally_unknown_profile"
        with TestClient(app) as client:
            resp = client.put(
                "/api/v1/chapters/ch-0001/card", headers=_hdr(), json=payload
            )
        app.dependency_overrides.clear()

        assert resp.status_code == 422
        # Pydantic 422 走 VALIDATION_FAILED;不是 WRITER_PROFILE_NOT_FOUND
        # (后者是运行时 force_profile 越界,前者是字面值不在 Literal 4 档)
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


# ---- backward compat:旧 JSON(无 force_profile 键)载入 ---------------------


class TestForceProfileBackwardCompat:
    """P1-P5 期已落盘的章节卡 JSON 文件**无 ``force_profile`` 键**,加新字段后
    必须仍能正常 load(默认 None)。"""

    def test_old_json_without_force_profile_loads_as_none(
        self,
        workspace_service: WorkspaceService,
        prepared_workspace: str,
    ) -> None:
        """模拟旧 SSOT 文件:写一份缺 ``force_profile`` 键的 JSON 到 cards 目录,
        ``ChapterService.read_chapter_card`` 应能解析为 force_profile=None。"""
        chapter_service = ChapterService(
            workspace_service=workspace_service,
            client=MockLLMClient(),
            model="legacy-model",
        )
        card = _make_card("ch-0001", 1)
        payload = json.loads(card.model_dump_json())
        # 模拟旧版本文件 — 显式删除 force_profile 键
        payload.pop("force_profile", None)

        paths = WorkspacePaths(
            "ws-force", workspaces_root=workspace_service.workspaces_root
        )
        paths.cards_dir.mkdir(parents=True, exist_ok=True)
        (paths.cards_dir / "ch-0001.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        loaded = chapter_service.read_chapter_card("ws-force", "ch-0001")
        assert loaded.force_profile is None
