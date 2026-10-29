"""OutlineService 单测(P5-B-04)。

覆盖 5 个公共方法 + gate 集中点 + WS 5 stage 推送 + 文件 SSOT + 落库:

- ``generate_outline``:mock LLM 4 段串行 → 落 4 文件 + WS publish 5 事件
- ``regenerate_section``:单段重生,上游从落盘读(不重跑上游)
- ``read_outline`` / ``write_outline``:文件 SSOT 读写
- ``commit_outline``:gate 通过时落 VolumeORM + 返列表
- ``_run_commit_gates``:副本横跨 < 3 章 → ``OutlineGateViolation``
- ``_publish_stage``:WS publish swallow on failure
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from src.api.websocket.manager import WebSocketManager, manager
from src.core.db import Base
from src.core.db.models import (
    CharacterORM,
    PlotThreadORM,
    VolumeORM,
    WorkspaceORM,
)
from src.core.agents.outline_builder import OutlineBuilderError
from src.core.models.drama import DramaticBeatContract, VolumeDramaticPromise
from src.core.models.outline import (
    CharactersOutput, MiniPlanOutput, PlotOutput, VolumePlansOutput, WorldOutput,
)
from src.core.llm.mock_client import MockLLMClient
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.outline_service import (
    OutlineGateViolation,
    OutlineLengthCalibrationRequired,
    OutlineNotFound,
    OutlineReadinessError,
    OutlineService,
)
from src.services.workspace_service import WorkspaceService


@pytest.fixture(autouse=True)
def _reset_ws_manager() -> Iterator[None]:
    manager.reset()
    yield
    manager.reset()


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
        "ws-outline",
        display_name="P5-B-04 outline service test",
        genre="test",
        premise="x",
        activate=False,
    )
    return "ws-outline"


def _make_session_factory(
    workspace_service: WorkspaceService,
) -> Callable[[str], Session]:
    def _factory(workspace_id: str) -> Session:
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        engine = create_engine(f"sqlite:///{paths.db_path}")
        Base.metadata.create_all(engine)
        return Session(engine)

    return _factory


def _make_service(
    workspace_service: WorkspaceService,
    *,
    session_factory: Callable[[str], Session] | None = None,
    ws_manager: WebSocketManager | None = None,
    pre_mini_plan_gates_enabled: bool = True,
) -> OutlineService:
    return OutlineService(
        workspace_service=workspace_service,
        client=MockLLMClient(),
        model="claude-sonnet-4-6",
        router_factory=None,
        session_factory=session_factory,
        ws_manager=ws_manager,
        pre_mini_plan_gates_enabled=pre_mini_plan_gates_enabled,
    )


def _install_typed_drama(
    service: OutlineService,
    workspace_id: str,
) -> tuple[VolumePlansOutput, MiniPlanOutput]:
    """在 mock outline 上安装一条完整 typed source chain，模拟导入资产。"""

    outline = service.read_outline(workspace_id)
    volume_plans = VolumePlansOutput.model_validate(outline["volume_plans"])
    mini_plan = MiniPlanOutput.model_validate(outline["mini_plan"])
    characters = outline["characters"]["characters"]
    actor_id = characters[0]["character_id"]
    counterforce_id = characters[1]["character_id"]
    volume = volume_plans.volumes[0]
    thread_id = volume.linked_threads[0]
    turning_id = volume.linked_turning_points[0]
    promise = VolumeDramaticPromise(
        primary_thread_id=thread_id,
        primary_turning_id=turning_id,
        actor_id=actor_id,
        counterforce_id=counterforce_id,
        outcome="resolved",
    )
    drama = DramaticBeatContract(
        function="finale",
        actor_id=actor_id,
        counterforce_id=counterforce_id,
        interaction_mode="indirect",
        thread_id=thread_id,
        turning_id=turning_id,
        actor_goal="主角公开关键事实并终止对手的控制方案",
        counteraction="对手隔空冻结公开权限并切断见证路径",
        choice="主角放弃独占证据并邀请同伴共同见证",
        consequence="控制方案失败，关键事实进入公开记录",
    )
    finale_arc = volume.arcs[-1]
    finale_beat = finale_arc.beats[-1]
    updated_arc = finale_arc.model_copy(
        update={
            "beats": [
                *finale_arc.beats[:-1],
                finale_beat.model_copy(update={"dramatic_contract": drama}),
            ]
        }
    )
    updated_volume = volume.model_copy(
        update={
            "dramatic_promise": promise,
            "arcs": [*volume.arcs[:-1], updated_arc],
        }
    )
    volume_plans = VolumePlansOutput(
        volumes=[updated_volume, *volume_plans.volumes[1:]]
    )

    designated_number = finale_beat.chapter_range[1]
    designated_index = next(
        idx
        for idx, item in enumerate(mini_plan.items)
        if item.volume_id == volume.volume_id
        and item.chapter_number == designated_number
    )
    designated = mini_plan.items[designated_index]
    updated_designated = designated.model_copy(
        update={
            "linked_characters": list(
                dict.fromkeys(
                    [*designated.linked_characters, actor_id, counterforce_id]
                )
            ),
            "linked_threads": list(
                dict.fromkeys([*designated.linked_threads, thread_id])
            ),
            "dramatic_contract": drama,
        }
    )
    mini_items = list(mini_plan.items)
    mini_items[designated_index] = updated_designated
    mini_plan = MiniPlanOutput(items=mini_items)
    service.write_outline(
        workspace_id,
        volume_plans=volume_plans.model_dump(mode="json"),
        mini_plan=mini_plan.model_dump(mode="json"),
    )
    return volume_plans, mini_plan


def test_mini_plan_resume_missing_five_review_receipt_never_reaudits(
    workspace_service: WorkspaceService, workspace_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(workspace_service)
    full = service.generate_outline(
        workspace_id, premise="premise", total_volumes=1,
        chapters_per_volume=12, tension_template="heros_journey",
    )
    from src.services import outline_service

    monkeypatch.setattr(outline_service, "uses_experimental_outline", lambda *_: True)
    def unexpected_review(**_kwargs: Any) -> None:
        raise AssertionError("不得重跑五项审核")
    monkeypatch.setattr(outline_service, "validate_outline_consistency_partitioned", unexpected_review)
    with pytest.raises(OutlineReadinessError, match="不重复调用已通过审核"):
        service._run_pre_mini_plan_gates(
            workspace_id, premise="premise", genre="",
            world=WorldOutput.model_validate(full.world),
            characters=CharactersOutput.model_validate(full.characters),
            plot=PlotOutput.model_validate(full.plot),
            volume_plans=VolumePlansOutput.model_validate(full.volume_plans),
            router=None, require_prior_receipt=True,
        )


# =============================================================================
# generate_outline
# =============================================================================


class TestBreakthroughPayoffWiringS10:
    """v1.1 wave 5 s10① 接线防回退:outline_service 解析题材门控 + 透传 build_outline。

    防未来改 generate_outline 断了 ``_resolve_breakthrough_payoff_enabled`` 调用 /
    不把结果透传 build_outline → 模块①(升级可感化)在生产中静默失效(mini_plan 约束 12
    不注入、breakthrough_payoff 不填),而 §3 启动 audit 的机制层 mock 测照不出。
    """

    def test_resolve_breakthrough_gating_by_genre(
        self, workspace_service: WorkspaceService
    ) -> None:
        """门控:3 题材(heroic_battle)+ 默认 auto → True;非 3 题材(healing_daily)→ False。"""
        workspace_service.create(
            "ws-hb", display_name="x", genre="x", premise="x",
            style_template="heroic_battle", activate=False,
        )
        workspace_service.create(
            "ws-heal", display_name="x", genre="x", premise="x",
            style_template="healing_daily", activate=False,
        )
        service = _make_service(workspace_service)
        assert service._resolve_breakthrough_payoff_enabled("ws-hb") is True
        assert service._resolve_breakthrough_payoff_enabled("ws-heal") is False

    def test_generate_outline_passes_breakthrough_enabled_to_build_outline(
        self,
        workspace_service: WorkspaceService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """generate_outline 把 ``_resolve`` 的真值透传给 build_outline(breakthrough_payoff_enabled=)。"""
        workspace_service.create(
            "ws-hb", display_name="x", genre="x", premise="x",
            style_template="heroic_battle", activate=False,
        )
        service = _make_service(workspace_service)
        captured: dict[str, object] = {}

        class _StopBuild(Exception):
            pass

        def _fake_build_outline(**kwargs: object) -> object:
            captured.update(kwargs)
            raise _StopBuild

        monkeypatch.setattr(
            "src.services.outline_service.build_outline", _fake_build_outline
        )
        with pytest.raises(_StopBuild):
            service.generate_outline(
                "ws-hb", premise="x", total_volumes=2,
                chapters_per_volume=5, tension_template="heros_journey",
            )
        # heroic_battle ∈ 3 题材 + 默认 auto → 接线应把 True 透传给 build_outline
        assert captured.get("breakthrough_payoff_enabled") is True


class TestGenerateOutline:
    def test_generates_all_four_files(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        tmp_path: Path,
    ) -> None:
        service = _make_service(workspace_service)
        service.generate_outline(
            workspace_id,
            premise="一个废柴炼丹师在末法时代重生",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        outline_dir = (
            workspace_service.workspaces_root / workspace_id / "data" / "outline"
        )
        assert (outline_dir / "world.json").is_file()
        assert (outline_dir / "characters.json").is_file()
        assert (outline_dir / "plot.json").is_file()
        assert (outline_dir / "volume_plans.json").is_file()
        # JSON 内容可解析
        with open(outline_dir / "world.json", encoding="utf-8") as f:
            world = json.load(f)
        assert "setting" in world
        assert "power_system" in world

    def test_returns_full_outline_object(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        service = _make_service(workspace_service)
        full = service.generate_outline(
            workspace_id,
            premise="premise",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        assert len(full.volume_plans.volumes) == 3
        for vol in full.volume_plans.volumes:
            assert vol.chapter_count == 9
            assert vol.tension_template == "heros_journey"

    def test_generate_outline_emits_stage_timing_logs(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """E:大纲主链路产出阶段级耗时,含 LLM stage 与 pre-mini-plan gates。"""
        service = _make_service(workspace_service)

        with capture_logs() as logs:
            service.generate_outline(
                workspace_id,
                premise="premise",
                total_volumes=3,
                chapters_per_volume=9,
                tension_template="heros_journey",
                task_id="task_timing",
            )

        timings = [
            log for log in logs if log.get("event") == "outline_stage_timing"
        ]
        assert timings
        stages = {log["stage"] for log in timings}
        assert {"world", "characters", "plot", "volume_plans", "mini_plan"} <= stages
        substages = {log["substage"] for log in timings}
        assert "outline_consistency_gate" in substages
        assert "arc_span_gate" in substages
        assert "mini_plan_volume" in substages
        for log in timings:
            assert log["workspace_id"] == workspace_id
            assert log["task_id"] == "task_timing"
            assert log["entrypoint"] == "generate_outline"
            assert log["status"] in {"succeeded", "skipped"}
            assert log["duration_ms"] >= 0

    def test_publishes_5_ws_stage_events(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        ws = WebSocketManager()
        service = _make_service(workspace_service, ws_manager=ws)

        # 模拟订阅
        published: list[dict[str, Any]] = []
        orig = ws.publish

        def _spy(channel: str, event: str, data: dict[str, Any]) -> None:
            published.append({"channel": channel, "event": event, "data": data})
            orig(channel, event, data)

        ws.publish = _spy  # type: ignore[method-assign]

        service.generate_outline(
            workspace_id,
            premise="premise",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
            task_id="task_abc123",
        )
        # v1.0.x wave 3 Commit B follow-up(2026-05-21):service 只发 N 段
        # stage event,**不再 emit "done" stage** — 整体完成 done event 由
        # 路由层 wrapper publish event="done"(对齐 chapters.py 同型,修
        # NewBookPage 永远卡"生成中"bug)。
        # v1.0.x wave 4 session 1(2026-05-21):OutlineBuilder 从 4 段扩到 5 段,
        # 新增 ``mini_plan`` 章级事件分配 — 5 stage event 全部 publish。
        stage_frames = [m for m in published if "parallel_group" not in m["data"]]
        parallel_frames = [
            m
            for m in published
            if m["data"].get("parallel_group") == "mini_plan_beat_alignment"
        ]
        stages = [m["data"]["stage"] for m in stage_frames]
        assert stages == [
            "world",
            "characters",
            "plot",
            "volume_plans",
            "mini_plan",
        ]
        assert parallel_frames
        assert all(m["data"]["stage"] == "mini_plan" for m in parallel_frames)
        assert all(
            task["status"] in {"queued", "running", "succeeded", "failed"}
            for m in parallel_frames
            for task in m["data"]["tasks"]
        )
        for m in published:
            assert m["channel"] == "task.task_abc123.progress"
            assert m["event"] == "stage"
            assert 0.0 < m["data"]["progress"] <= 1.0
            # service 内部 publish 的 stage 严格属于 5 段名,不含 "done"
            assert m["data"]["stage"] != "done"

    def test_no_ws_when_task_id_none(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        ws = WebSocketManager()
        spy = MagicMock(wraps=ws.publish)
        ws.publish = spy  # type: ignore[method-assign]
        service = _make_service(workspace_service, ws_manager=ws)
        service.generate_outline(
            workspace_id,
            premise="premise",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
            task_id=None,
        )
        spy.assert_not_called()

    def test_ws_publish_failure_swallowed(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        bad_ws = MagicMock(spec=WebSocketManager)
        bad_ws.publish.side_effect = RuntimeError("WS down")
        service = _make_service(workspace_service, ws_manager=bad_ws)
        # 不应冒泡 RuntimeError;outline 生成主流程必须完成
        full = service.generate_outline(
            workspace_id,
            premise="premise",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
            task_id="task_x",
        )
        assert len(full.volume_plans.volumes) == 3


# =============================================================================
# regenerate_section
# =============================================================================


class TestRegenerateSection:
    def test_world_section_no_upstream_needed(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        service = _make_service(workspace_service)
        result = service.regenerate_section(
            workspace_id,
            section="world",
            premise="premise",
        )
        outline_dir = (
            workspace_service.workspaces_root / workspace_id / "data" / "outline"
        )
        assert (outline_dir / "world.json").is_file()
        assert result.setting

    def test_characters_needs_world_upstream(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        service = _make_service(workspace_service)
        with pytest.raises(OutlineNotFound, match="world.json 缺失"):
            service.regenerate_section(
                workspace_id, section="characters", premise="x"
            )

    def test_volume_plans_needs_params(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        service = _make_service(workspace_service)
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        with pytest.raises(Exception, match="total_volumes"):
            service.regenerate_section(
                workspace_id, section="volume_plans", premise="x"
            )

    def test_regenerate_volume_plans_blocks_arc_span_before_persist(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """D1:单段重生 volume_plans 也先跑 arc span gate,不把坏卷规划写成 SSOT。"""
        service = _make_service(workspace_service)
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        original = service.read_outline(workspace_id)["volume_plans"]

        with pytest.raises(OutlineBuilderError) as exc:
            service.regenerate_section(
                workspace_id,
                section="volume_plans",
                premise="x",
                total_volumes=2,
                chapters_per_volume=6,
                tension_template="heros_journey",
            )

        assert exc.value.model_used == "outline_arc_span_gate"
        assert exc.value.violations
        assert service.read_outline(workspace_id)["volume_plans"] == original

    def test_regenerate_volume_plans_blocks_consistency_before_persist(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """D1:单段重生 volume_plans 后立即跑 consistency gate,不等 mini_plan/commit。"""
        from src.core.agents.outline_consistency_validator import (
            OutlineConsistencyResult,
        )
        import src.services.outline_service as outline_service_module

        service = _make_service(workspace_service)
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        original = service.read_outline(workspace_id)["volume_plans"]

        monkeypatch.setattr(
            outline_service_module,
            "validate_outline_consistency",
            lambda **_: OutlineConsistencyResult(
                passed=False,
                contradictions=["volume_plans 与 plot 终局地点互斥"],
                has_non_fixable_contradiction=True,
                evidence="plot 写古墓,卷规划写博物馆",
            ),
        )

        with pytest.raises(OutlineBuilderError) as exc:
            service.regenerate_section(
                workspace_id,
                section="volume_plans",
                premise="x",
                total_volumes=3,
                chapters_per_volume=9,
                tension_template="heros_journey",
            )

        assert exc.value.model_used == "outline_consistency_validator"
        assert exc.value.contradictions == ["volume_plans 与 plot 终局地点互斥"]
        assert service.read_outline(workspace_id)["volume_plans"] == original

    def test_regenerate_volume_plans_setting_conflict_preserves_previous_ssot(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R9 v2:卷规划 HARD gate 失败时不覆盖上一份可回查 checkpoint。"""
        import src.services.outline_service as outline_service_module

        service = _make_service(workspace_service)
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        original = service.read_outline(workspace_id)["volume_plans"]

        def conflict(**_: Any) -> None:
            raise OutlineBuilderError(
                "VOLUME_PLANS_SETTING_CONFLICT",
                stage="volume_plans",
                raw_output="",
                model_used="mock-setting-gate",
                violations=[{"constraint_id": "9" * 64}],
                error_code="VOLUME_PLANS_SETTING_CONFLICT",
                contract_hash="a" * 64,
                attempts=3,
            )

        monkeypatch.setattr(
            outline_service_module, "build_volume_plans", conflict
        )
        with pytest.raises(OutlineBuilderError) as exc_info:
            service.regenerate_section(
                workspace_id,
                section="volume_plans",
                premise="x",
                total_volumes=3,
                chapters_per_volume=9,
                tension_template="heros_journey",
            )

        assert exc_info.value.error_code == "VOLUME_PLANS_SETTING_CONFLICT"
        assert service.read_outline(workspace_id)["volume_plans"] == original

    def test_regenerate_plot_after_full_outline(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        service = _make_service(workspace_service)
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        # 上游 world + characters 已落盘
        new_plot = service.regenerate_section(
            workspace_id, section="plot", premise="x"
        )
        assert new_plot.core_conflict

    def test_regenerate_plot_setting_conflict_preserves_previous_ssot(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R9:plot 写盘前 HARD gate 失败时不得覆盖上一份可回查 checkpoint。"""
        import src.services.outline_service as outline_service_module

        service = _make_service(workspace_service)
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        original = service.read_outline(workspace_id)["plot"]

        def conflict(**_: Any) -> None:
            raise OutlineBuilderError(
                "PLOT_SETTING_CONFLICT",
                stage="plot",
                raw_output="",
                model_used="mock-setting-gate",
                violations=[{"constraint_id": "7" * 64}],
                error_code="PLOT_SETTING_CONFLICT",
                contract_hash="8" * 64,
                attempts=3,
            )

        monkeypatch.setattr(outline_service_module, "build_plot", conflict)
        with pytest.raises(OutlineBuilderError) as exc_info:
            service.regenerate_section(
                workspace_id, section="plot", premise="x"
            )

        assert exc_info.value.error_code == "PLOT_SETTING_CONFLICT"
        assert service.read_outline(workspace_id)["plot"] == original

    def test_regenerate_mini_plan_after_full_outline(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """v1.0.x wave 4 session 1:regenerate_section('mini_plan') 单段重生。

        要求上游 volume_plans 已落盘 → mini_plan 单段重生 → 落盘
        mini_plan.json + 返 MiniPlanOutput。
        """
        from src.core.models.outline import MiniPlanOutput

        service = _make_service(workspace_service)
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        # 删 mini_plan.json 模拟旧工作区(wave 4 前 outline 生成,无 mini_plan)
        mini_plan_path = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "outline"
            / "mini_plan.json"
        )
        assert mini_plan_path.is_file()
        mini_plan_path.unlink()
        assert not mini_plan_path.is_file()

        # 单段重生 mini_plan
        result = service.regenerate_section(
            workspace_id, section="mini_plan", premise="x"
        )
        assert isinstance(result, MiniPlanOutput)
        assert mini_plan_path.is_file()
        # 3 卷 × 9 章 = 27 items
        assert len(result.items) == 27

    def test_regenerate_mini_plan_blocks_bad_volume_plans_before_llm(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """D1:重生 mini_plan 前先跑 volume_plans preflight,避免昂贵阶段才发现。"""
        import src.services.outline_service as outline_service_module

        legacy_service = _make_service(
            workspace_service, pre_mini_plan_gates_enabled=False
        )
        legacy_service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        legacy_service.regenerate_section(
            workspace_id,
            section="volume_plans",
            premise="x",
            total_volumes=2,
            chapters_per_volume=6,
            tension_template="heros_journey",
        )

        def _must_not_generate_mini_plan(**_: Any) -> None:
            raise AssertionError("preflight gate 应在 mini_plan LLM 前阻断")

        monkeypatch.setattr(
            outline_service_module, "generate_mini_plan", _must_not_generate_mini_plan
        )
        service = _make_service(workspace_service)
        with pytest.raises(OutlineBuilderError) as exc:
            service.regenerate_section(
                workspace_id, section="mini_plan", premise="x"
            )

        assert exc.value.model_used == "outline_arc_span_gate"

    def test_regenerate_mini_plan_setting_conflict_leaves_no_bad_ssot(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.services.outline_service as outline_service_module

        service = _make_service(workspace_service)
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        mini_path = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "outline"
            / "mini_plan.json"
        )
        mini_path.unlink()

        def conflict(**_: Any) -> None:
            raise OutlineBuilderError(
                "MINI_PLAN_SETTING_CONFLICT",
                stage="mini_plan",
                raw_output="",
                model_used="mock-setting-gate",
                violations=[{"constraint_id": "8" * 64}],
                error_code="MINI_PLAN_SETTING_CONFLICT",
                contract_hash="9" * 64,
                attempts=3,
            )

        monkeypatch.setattr(
            outline_service_module, "generate_mini_plan", conflict
        )
        with pytest.raises(OutlineBuilderError) as exc_info:
            service.regenerate_section(
                workspace_id, section="mini_plan", premise="x"
            )

        assert exc_info.value.error_code == "MINI_PLAN_SETTING_CONFLICT"
        assert not mini_path.exists()

    def test_pre_mini_plan_gate_disabled_preserves_commit_failure_path(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """D1:关闭 preflight 开关时保留旧路径,坏卷规划仍由 commit gate 诊断。"""
        service = _make_service(
            workspace_service, pre_mini_plan_gates_enabled=False
        )
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        with capture_logs() as logs:
            service.regenerate_section(
                workspace_id,
                section="volume_plans",
                premise="x",
                total_volumes=2,
                chapters_per_volume=6,
                tension_template="heros_journey",
            )

        skipped = [
            log
            for log in logs
            if log.get("event") == "outline_stage_timing"
            and log.get("substage") == "pre_mini_plan_gates"
        ]
        assert skipped
        assert skipped[0]["status"] == "skipped"
        assert skipped[0]["reason"] == "disabled"

        with pytest.raises(OutlineGateViolation) as exc:
            service.commit_outline(workspace_id)

        assert exc.value.failures[0].rule == "arc_too_short"

    def test_regenerate_mini_plan_needs_volume_plans_upstream(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """mini_plan 段需要上游 volume_plans 已落盘,否则抛 OutlineNotFound。"""
        service = _make_service(workspace_service)
        # 只落 world + characters + plot,删 volume_plans
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        vp_path = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "outline"
            / "volume_plans.json"
        )
        vp_path.unlink()
        with pytest.raises(OutlineNotFound, match="volume_plans.json 缺失"):
            service.regenerate_section(
                workspace_id, section="mini_plan", premise="x"
            )


# =============================================================================
# read / write outline
# =============================================================================


class TestReadWriteOutline:
    def test_read_returns_none_when_not_generated(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        service = _make_service(workspace_service)
        data = service.read_outline(workspace_id)
        # v1.0.x wave 4 session 1:read_outline 5 段(原 4 段 + mini_plan)
        assert data == {
            "world": None,
            "characters": None,
            "plot": None,
            "volume_plans": None,
            "mini_plan": None,
        }

    def test_read_after_generate(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        service = _make_service(workspace_service)
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        data = service.read_outline(workspace_id)
        for stage in ("world", "characters", "plot", "volume_plans"):
            assert data[stage] is not None

    def test_write_partial(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        service = _make_service(workspace_service)
        service.write_outline(
            workspace_id,
            world={
                "setting": "x",
                "power_system": "y",
                "factions": "z",
                "culture": "a",
                "unique_rules": "b",
            },
        )
        data = service.read_outline(workspace_id)
        assert data["world"]["setting"] == "x"
        assert data["characters"] is None


# =============================================================================
# commit_outline + gate
# =============================================================================


class TestCommitOutline:
    def test_commit_accepts_and_roundtrips_complete_typed_drama_source(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        service = _make_service(workspace_service, session_factory=None)
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        expected_volumes, expected_mini = _install_typed_drama(
            service,
            workspace_id,
        )

        assert service.commit_outline(workspace_id) == []
        persisted = service.read_outline(workspace_id)
        assert (
            VolumePlansOutput.model_validate(persisted["volume_plans"])
            == expected_volumes
        )
        assert MiniPlanOutput.model_validate(persisted["mini_plan"]) == expected_mini

    def test_commit_rejects_imported_typed_drama_source_drift(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        service = _make_service(workspace_service, session_factory=None)
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        _, mini_plan = _install_typed_drama(service, workspace_id)
        designated_index = next(
            idx
            for idx, item in enumerate(mini_plan.items)
            if item.dramatic_contract is not None
        )
        designated = mini_plan.items[designated_index]
        assert designated.dramatic_contract is not None
        drifted_contract = designated.dramatic_contract.model_copy(
            update={"consequence": "控制方案仍然有效，公开记录没有建立"}
        )
        drifted_items = list(mini_plan.items)
        drifted_items[designated_index] = designated.model_copy(
            update={"dramatic_contract": drifted_contract}
        )
        service.write_outline(
            workspace_id,
            mini_plan=MiniPlanOutput(items=drifted_items).model_dump(mode="json"),
        )

        with pytest.raises(OutlineBuilderError, match="typed drama source") as exc:
            service.commit_outline(workspace_id)

        assert exc.value.model_used == "<deterministic-a1>"

    def test_commit_rejects_typed_volume_when_mini_plan_missing(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        service = _make_service(workspace_service, session_factory=None)
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        _install_typed_drama(service, workspace_id)
        mini_plan_path = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "outline"
            / "mini_plan.json"
        )
        mini_plan_path.unlink()

        with pytest.raises(OutlineBuilderError, match="mini_plan source"):
            service.commit_outline(workspace_id)

    def test_commit_passes_with_valid_outline(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        session_factory = _make_session_factory(workspace_service)
        service = _make_service(
            workspace_service, session_factory=session_factory
        )
        # 9 章 / 3 卷 mock 产 [1,3]/[4,6]/[7,9],副本中段 = 3 章 ✅
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        volumes = service.commit_outline(workspace_id)
        assert len(volumes) == 3
        for vol in volumes:
            assert vol.workspace_id == workspace_id
            assert vol.chapter_count == 9

        # DB 内验证 ORM 行实际落盘
        sess = session_factory(workspace_id)
        try:
            rows = sess.execute(select(VolumeORM)).scalars().all()
            assert len(rows) == 3
        finally:
            sess.close()

    def test_commit_succeeds_when_mini_plan_missing(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """v1.0.x wave 4 session 1:mini_plan 段对 commit 是软依赖 —
        缺 mini_plan.json 时 commit 仍能成功(兼容 wave 4 前生成的旧工作区
        outline)。日志 warn 已由 structlog 自动落,本测试不断言 caplog
        (structlog 走自定义 logger 不一定能被 caplog 捕到)。"""
        session_factory = _make_session_factory(workspace_service)
        service = _make_service(
            workspace_service, session_factory=session_factory
        )
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        # 模拟 wave 4 前生成的旧工作区:删 mini_plan.json
        mini_plan_path = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "outline"
            / "mini_plan.json"
        )
        mini_plan_path.unlink()
        # commit 应仍成功(软依赖)— 缺 mini_plan 不抛 OutlineNotFound
        volumes = service.commit_outline(workspace_id)
        assert len(volumes) == 3  # commit 没被阻塞

    def test_generate_blocks_on_arc_too_short(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        session_factory = _make_session_factory(workspace_service)
        service = _make_service(
            workspace_service, session_factory=session_factory
        )
        # 6 章 → mock arcs [1,2]/[3,4]/[5,6],副本中段 = 2 章 → 违规
        # v2 W3.5 起该 gate 前移到 mini_plan 前,不再等到 commit 才拦截。
        with pytest.raises(OutlineBuilderError) as exc:
            service.generate_outline(
                workspace_id,
                premise="x",
                total_volumes=2,
                chapters_per_volume=6,
                tension_template="heros_journey",
            )
        assert exc.value.model_used == "outline_arc_span_gate"
        assert exc.value.raw_output.count("span=2") == 2

        # gate 失败时 volumes 表不应写入(事务边界)
        sess = session_factory(workspace_id)
        try:
            rows = sess.execute(select(VolumeORM)).scalars().all()
            assert rows == []
        finally:
            sess.close()

    def test_commit_missing_outline_raises_not_found(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        service = _make_service(workspace_service)
        with pytest.raises(OutlineNotFound):
            service.commit_outline(workspace_id)

    def test_commit_upsert_existing_volume(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """二次 commit 时同 volume_id 应 update 而非冲突。"""
        session_factory = _make_session_factory(workspace_service)
        service = _make_service(
            workspace_service, session_factory=session_factory
        )
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        service.commit_outline(workspace_id)
        # 再次 commit
        volumes2 = service.commit_outline(workspace_id)
        assert len(volumes2) == 3

    def test_commit_without_session_factory_returns_empty(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        service = _make_service(workspace_service, session_factory=None)
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        volumes = service.commit_outline(workspace_id)
        assert volumes == []  # gate 通过 + 无 DB 落库


# =============================================================================
# A-02 hot-fix:commit_outline 批落 characters 表
# =============================================================================


class TestCommitPersistsCharacters:
    """A-02 hot-fix:commit_outline 把 outline/characters.json 批落 characters 表。

    - happy:5 mock 角色全部落 SQLite,字段映射正确(name / archetype /
      voice_profile / relationships)
    - 幂等:重 commit 已存在 character_id 跳过(无 IntegrityError)
    - session_factory=None:跳过批落,commit 仍能跑通
    """

    def test_commit_persists_outline_characters_to_table(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        session_factory = _make_session_factory(workspace_service)
        service = _make_service(
            workspace_service, session_factory=session_factory
        )
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        service.commit_outline(workspace_id)
        sess = session_factory(workspace_id)
        try:
            rows = sess.execute(
                select(CharacterORM)
                .where(CharacterORM.workspace_id == workspace_id)
                .order_by(CharacterORM.character_id)
            ).scalars().all()
        finally:
            sess.close()
        # mock 5 角色全部落表
        assert len(rows) == 5
        ids = {r.character_id for r in rows}
        assert ids == {"lin-yan", "shi-jie", "wei-xiao", "mo-zun", "zhu-zhang-lao"}
        # 字段映射断言(lin-yan)
        lin_yan = next(r for r in rows if r.character_id == "lin-yan")
        assert lin_yan.workspace_id == workspace_id
        assert lin_yan.name == "林衍"
        assert lin_yan.archetype == "废柴重生剑修"
        assert lin_yan.voice_profile["tone"] == "[mock] 短句直白,冷峻"
        assert lin_yan.voice_profile["self_address"] == "我"
        assert lin_yan.voice_profile["addresses_others"] == {}
        assert lin_yan.relationships == {
            "shi-jie": "前世师姐,今生重逢",
            "wei-xiao": "前世挚友",
        }
        assert lin_yan.goals_long_term == [
            "[mock] 改变末法时代,问鼎修真巅峰,守护今生所爱"
        ]
        assert lin_yan.first_appearance_chapter is None

    def test_commit_materializes_structured_initial_core_baseline(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        session_factory = _make_session_factory(workspace_service)
        service = _make_service(
            workspace_service, session_factory=session_factory
        )
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        characters = service.read_outline(workspace_id)["characters"]
        assert characters is not None
        characters["characters"][0]["initial_core_facts"] = [
            {
                "attribute": "identity",
                "value": "炼丹师",
                "source_field": "background",
                "evidence_quote": "废柴炼丹师",
            }
        ]
        service.write_outline(workspace_id, characters=characters)

        service.commit_outline(workspace_id)

        with session_factory(workspace_id) as session:
            row = session.get(CharacterORM, "lin-yan")
            assert row is not None
            assert row.current_state["core.identity"] == "炼丹师"
            assert len(row.state_timeline) == 1
            snapshot = row.state_timeline[0]
            assert snapshot["as_of_chapter"] == 0
            assert snapshot["authority"] == "outline_characters"
            assert snapshot["core_facts"][0]["source_kind"] == "outline_characters"
            assert snapshot["core_facts"][0]["valid_from_chapter"] == 0

    def test_commit_idempotent_skips_existing_characters(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """重 commit 已落库的 character_id 跳过,不撞 IntegrityError。"""
        session_factory = _make_session_factory(workspace_service)
        service = _make_service(
            workspace_service, session_factory=session_factory
        )
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        service.commit_outline(workspace_id)
        # 再 commit:已存在 character_id 跳过
        service.commit_outline(workspace_id)
        sess = session_factory(workspace_id)
        try:
            rows = sess.execute(
                select(CharacterORM).where(
                    CharacterORM.workspace_id == workspace_id
                )
            ).scalars().all()
        finally:
            sess.close()
        # 数量仍为 5,无重复
        assert len(rows) == 5

    def test_commit_without_session_factory_skips_character_persist(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """session_factory=None 时批落 noop,commit 仍跑通返空列表。"""
        service = _make_service(workspace_service, session_factory=None)
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        # 不抛 + 返回空列表(volume 也没落库 — 同模式)
        volumes = service.commit_outline(workspace_id)
        assert volumes == []


class TestCommitPersistsPlotThreads:
    """B2-01 hot-fix:commit_outline 把 outline/plot.long_threads 批落
    plot_threads 表(类比 A-02 characters)。

    - happy:4 mock long_threads 全部落 SQLite,planted_chapter=None,
      字段映射正确(thread_id / title / description / status / importance)
    - 幂等:重 commit 已存在 thread_id 跳过(无 IntegrityError)
    - session_factory=None:批落 noop,commit 仍跑通
    - 单 thread 派生失败 swallow + 不阻断 commit + 其他 thread 仍落表
    - plot 段无效 swallow + 不阻断 commit(volumes / characters 已落)
    """

    def test_commit_persists_outline_plot_threads_to_table(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        session_factory = _make_session_factory(workspace_service)
        service = _make_service(
            workspace_service, session_factory=session_factory
        )
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        service.commit_outline(workspace_id)
        sess = session_factory(workspace_id)
        try:
            rows = sess.execute(
                select(PlotThreadORM)
                .where(PlotThreadORM.workspace_id == workspace_id)
                .order_by(PlotThreadORM.thread_id)
            ).scalars().all()
        finally:
            sess.close()
        # mock 4 long_threads 全部落表
        assert len(rows) == 4
        ids = {r.thread_id for r in rows}
        assert ids == {
            "yu-pei-zhi-yue",
            "shi-fu-fu-bi",
            "zhen-xian-zhi-wei",
            "wei-xiao-jia-zu",
        }
        # 字段映射断言(yu-pei-zhi-yue)
        yu_pei = next(r for r in rows if r.thread_id == "yu-pei-zhi-yue")
        assert yu_pei.workspace_id == workspace_id
        assert yu_pei.title == "[mock] 玉佩之约"
        assert yu_pei.description.startswith("[mock] 沈雪玉佩")
        assert yu_pei.status == "planted"
        assert yu_pei.importance == 3
        assert yu_pei.origin == "outline"
        assert yu_pei.expiry_policy == "never"
        assert yu_pei.source_evidence is None
        # B2-01 关键:outline 阶段无章节,planted_chapter=None
        assert yu_pei.planted_chapter is None
        assert yu_pei.planned_payoff is None
        assert yu_pei.actual_payoff is None
        assert yu_pei.last_updated_chapter is None
        assert yu_pei.related_characters == []
        assert yu_pei.revoked_at is None

    def test_commit_idempotent_skips_existing_plot_threads(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """重 commit 已落库的 thread_id 跳过,不撞 IntegrityError(幂等)。"""
        session_factory = _make_session_factory(workspace_service)
        service = _make_service(
            workspace_service, session_factory=session_factory
        )
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        service.commit_outline(workspace_id)
        # 再 commit:已存在 thread_id 跳过
        service.commit_outline(workspace_id)
        sess = session_factory(workspace_id)
        try:
            rows = sess.execute(
                select(PlotThreadORM).where(
                    PlotThreadORM.workspace_id == workspace_id
                )
            ).scalars().all()
        finally:
            sess.close()
        # 数量仍为 4,无重复
        assert len(rows) == 4

    def test_commit_without_session_factory_skips_plot_thread_persist(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """session_factory=None 时批落 noop,commit 仍跑通返空列表。"""
        service = _make_service(workspace_service, session_factory=None)
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        # 不抛 + 返回空列表(volume 也没落库 — 同模式)
        volumes = service.commit_outline(workspace_id)
        assert volumes == []

    def test_commit_persists_other_threads_when_one_fails(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """单 thread 落库失败 swallow + 不阻断其他 thread 落表。

        预先 INSERT thread_id="yu-pei-zhi-yue" 占位(workspace_id 字段不同 → upsert
        路径不会 hit;但 thread_id 是主键,会撞 IntegrityError → savepoint
        rollback)→ 其他 3 thread 应正常落表。
        """
        session_factory = _make_session_factory(workspace_service)
        service = _make_service(
            workspace_service, session_factory=session_factory
        )
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        # 预先植入"别的工作区"占同 thread_id 主键(撞主键冲突)
        # 用一个新 workspace_id "ws-other" 占同样的 yu-pei-zhi-yue 主键
        from datetime import UTC, datetime
        sess = session_factory(workspace_id)
        try:
            sess.add(
                PlotThreadORM(
                    thread_id="yu-pei-zhi-yue",
                    workspace_id="ws-other",
                    title="占位",
                    status="planted",
                    importance=1,
                    planted_chapter="ch-zzzz",
                    description="占位",
                    related_characters=[],
                    created_at=datetime.now(UTC),
                )
            )
            sess.commit()
        finally:
            sess.close()
        # commit:get_plot_thread 看到 yu-pei-zhi-yue 已存在跳过(走幂等而非
        # 撞键)→ 其他 3 thread 落表
        service.commit_outline(workspace_id)
        sess = session_factory(workspace_id)
        try:
            rows = sess.execute(
                select(PlotThreadORM).where(
                    PlotThreadORM.workspace_id == workspace_id
                )
            ).scalars().all()
        finally:
            sess.close()
        # workspace_id == workspace_id 的应该有 3(yu-pei-zhi-yue 被幂等跳过,
        # 仍属于 ws-other)
        assert len(rows) == 3
        ids = {r.thread_id for r in rows}
        assert ids == {
            "shi-fu-fu-bi",
            "zhen-xian-zhi-wei",
            "wei-xiao-jia-zu",
        }

    def test_commit_swallow_when_plot_section_invalid(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """plot.json 不符合 PlotOutput schema 时 swallow + commit 主流程仍跑通
        (类比 A-02 outline_characters_validate_failed 同模式)。"""
        session_factory = _make_session_factory(workspace_service)
        service = _make_service(
            workspace_service, session_factory=session_factory
        )
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        # 篡改 plot.json 让 PlotOutput.model_validate 失败
        from src.core.workspace.workspace_paths import WorkspacePaths
        paths = WorkspacePaths(
            workspace_id, workspaces_root=workspace_service.workspaces_root
        )
        plot_path = paths.root / "data" / "outline" / "plot.json"
        plot_path.write_text(
            json.dumps({"core_conflict": "x", "main_phases": []}),
            encoding="utf-8",
        )
        # commit 不抛(validate 失败被 swallow,主流程继续)
        volumes = service.commit_outline(workspace_id)
        assert len(volumes) == 3  # volumes 仍正常落
        # plot_threads 表空(plot 验证失败 → 没机会调 _persist)
        sess = session_factory(workspace_id)
        try:
            rows = sess.execute(
                select(PlotThreadORM).where(
                    PlotThreadORM.workspace_id == workspace_id
                )
            ).scalars().all()
        finally:
            sess.close()
        assert rows == []

    def test_persist_helper_planted_chapter_is_none_by_design(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """直接构造 PlotLongThread 走 _outline_plot_thread_to_thread helper,
        断言 planted_chapter=None(B2-01 schema 改可空的关键契约)。"""
        from datetime import UTC, datetime

        from src.core.models.outline import PlotLongThread
        from src.services.outline_service import _outline_plot_thread_to_thread

        item = PlotLongThread(
            thread_id="test-thread-id",
            title="测试伏笔",
            description="测试描述,埋于卷 1 收于卷 3",
        )
        now = datetime.now(UTC)
        thread = _outline_plot_thread_to_thread(
            workspace_id="ws-test", item=item, now=now
        )
        assert thread.thread_id == "test-thread-id"
        assert thread.workspace_id == "ws-test"
        assert thread.title == "测试伏笔"
        assert thread.description == "测试描述,埋于卷 1 收于卷 3"
        assert thread.status == "planted"
        assert thread.importance == 3
        assert thread.planted_chapter is None  # 关键
        assert thread.planned_payoff is None
        assert thread.actual_payoff is None
        assert thread.related_characters == []
        assert thread.created_at == now
        assert thread.last_updated_chapter is None


# =============================================================================
# outline_status 流转(P5-B-07,启动指令字面)
# =============================================================================


class TestOutlineStatusTransitions:
    """``WorkspaceORM.outline_status`` 在 ``generate_outline`` / ``commit_outline``
    端点的流转(P5-B-07,启动指令待审 ② 拍 A 3 档)。

    - ``generate_outline`` 起始翻 ``generating``
    - ``commit_outline`` gate 全过 + 落库后翻 ``committed``
    - ``session_factory=None`` 时 noop(CLI / 单测纯文件路径)
    """

    def test_generate_sets_status_to_generating(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        session_factory = _make_session_factory(workspace_service)
        service = _make_service(
            workspace_service, session_factory=session_factory
        )
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        sess = session_factory(workspace_id)
        try:
            row = sess.get(WorkspaceORM, workspace_id)
        finally:
            sess.close()
        assert row is not None
        assert row.outline_status == "generating"

    def test_commit_sets_status_to_committed(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        session_factory = _make_session_factory(workspace_service)
        service = _make_service(
            workspace_service, session_factory=session_factory
        )
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        service.commit_outline(workspace_id)
        sess = session_factory(workspace_id)
        try:
            row = sess.get(WorkspaceORM, workspace_id)
        finally:
            sess.close()
        assert row is not None
        assert row.outline_status == "committed"

    def test_commit_gate_failure_does_not_advance_status(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """生成期 arc gate 失败时 outline_status 留在 ``generating``。"""
        session_factory = _make_session_factory(workspace_service)
        service = _make_service(
            workspace_service, session_factory=session_factory
        )
        # v2 W3.5:arc_too_short 已从 commit 前移到 mini_plan 前。
        with pytest.raises(OutlineBuilderError):
            service.generate_outline(
                workspace_id,
                premise="x",
                total_volumes=2,
                chapters_per_volume=6,
                tension_template="heros_journey",
            )
        sess = session_factory(workspace_id)
        try:
            row = sess.get(WorkspaceORM, workspace_id)
        finally:
            sess.close()
        assert row is not None
        assert row.outline_status == "generating"  # gate 失败不翻 committed

    def test_no_session_factory_skips_status_update(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """``session_factory=None`` 时 _set_outline_status 静默 noop,
        generate / commit 仍能跑通。"""
        service = _make_service(workspace_service, session_factory=None)
        # 不抛
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        volumes = service.commit_outline(workspace_id)
        assert volumes == []


# =============================================================================
# 原子写
# =============================================================================


class TestAtomicWrite:
    def test_no_tmp_file_left_on_success(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        service = _make_service(workspace_service)
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        outline_dir = (
            workspace_service.workspaces_root / workspace_id / "data" / "outline"
        )
        tmp_files = list(outline_dir.glob("*.tmp"))
        assert tmp_files == []


class TestVolumeSeamReplayGate:
    """acceptance 修复1 gate 接线防回退(2026-06-10):commit → 卷界重演判定。

    [[feedback_test_full_wiring_not_just_mechanism_ends]]:validator 机制单测在
    test_volume_seam_replay_validator.py,本类锁 **commit_outline → _run_commit_gates
    → validate_volume_seam_replay 的接线**(monkeypatch 假 validator,不调 LLM)—
    接线被删 = 卷界重演在生产静默失效而机制单测照不出。
    """

    @staticmethod
    def _ready_service(
        workspace_service: WorkspaceService, workspace_id: str
    ) -> OutlineService:
        session_factory = _make_session_factory(workspace_service)
        service = _make_service(
            workspace_service, session_factory=session_factory
        )
        service.generate_outline(
            workspace_id,
            premise="x",
            total_volumes=3,
            chapters_per_volume=9,
            tension_template="heros_journey",
        )
        return service

    def test_commit_blocked_when_seam_replay_detected(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.core.agents.volume_seam_replay_validator import (
            SeamReplayFinding,
            SeamReplayResult,
        )
        import src.services.outline_service as outline_service_module

        service = self._ready_service(workspace_service, workspace_id)

        def _fake_validate(**kwargs: Any) -> SeamReplayResult:
            return SeamReplayResult(
                passed=False,
                findings=[
                    SeamReplayFinding(
                        prev_chapter_id="ch-0006",
                        next_chapter_id="ch-0007",
                        confidence=0.92,
                        evidence="新卷首章重新解决了前卷已完结的掐人冲突。",
                    )
                ],
                seams_total=2,
                seams_prescreen_fired=1,
                llm_calls_count=1,
            )

        monkeypatch.setattr(
            outline_service_module,
            "validate_volume_seam_replay",
            _fake_validate,
        )
        with pytest.raises(OutlineGateViolation) as excinfo:
            service.commit_outline(workspace_id)
        failure = excinfo.value.failures[0]
        assert failure.rule == "volume_seam_replay"
        assert failure.prev_chapter_id == "ch-0006"
        assert failure.next_chapter_id == "ch-0007"
        assert "重演" in str(excinfo.value) or "seam" in str(excinfo.value)

    def test_commit_passes_and_gate_receives_mini_plan_items(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """干净卷界 → commit 照常落库;且断言 gate 真拿到了 mini_plan items
        (commit → gate 的数据通路,不只是函数被调)。"""
        from src.core.agents.volume_seam_replay_validator import SeamReplayResult
        import src.services.outline_service as outline_service_module

        service = self._ready_service(workspace_service, workspace_id)
        captured: dict[str, Any] = {}

        def _fake_validate(**kwargs: Any) -> SeamReplayResult:
            captured.update(kwargs)
            return SeamReplayResult(passed=True, seams_total=2)

        monkeypatch.setattr(
            outline_service_module,
            "validate_volume_seam_replay",
            _fake_validate,
        )
        volumes = service.commit_outline(workspace_id)
        assert len(volumes) == 3
        items = captured.get("mini_plan_items")
        assert items, "gate 未收到 mini_plan_items — commit→gate 数据通路断了"
        assert all(hasattr(it, "one_line_event") for it in items)

    def test_gate_skipped_when_mini_plan_missing(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """旧工作区缺 mini_plan → 软依赖语义保持:gate 不触发、commit 照常成功。"""
        import src.services.outline_service as outline_service_module

        service = self._ready_service(workspace_service, workspace_id)
        mini_plan_path = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "outline"
            / "mini_plan.json"
        )
        mini_plan_path.unlink()

        def _must_not_be_called(**kwargs: Any) -> None:
            raise AssertionError(
                "mini_plan 缺失时不应调用 volume_seam_replay gate"
            )

        monkeypatch.setattr(
            outline_service_module,
            "validate_volume_seam_replay",
            _must_not_be_called,
        )
        volumes = service.commit_outline(workspace_id)
        assert len(volumes) == 3


class TestLengthCalibrationResolve:
    """W3.5-s3 OutlineService._resolve_length_calibration:resolve (模型,题材) 校准 + 上限。

    全链路接线起点(按 feedback_test_full_wiring):router→model 名→stored 校准→题材原型。
    依赖真 config.yaml 的 deepseek-v4-pro 首基线与 qwen3.8-max §2.143 新实测。
    正式路由下缺模型/题材校准必须 fail closed；只有 router=None 的 legacy/mock
    路径允许退化为不细化。
    """

    def test_deepseek_action_resolves_calibration(
        self, workspace_service: WorkspaceService, workspace_id: str
    ) -> None:
        svc = _make_service(workspace_service)
        router = MagicMock()
        router.select_profile.return_value = MagicMock(model="deepseek-v4-pro")
        cal, wcm = svc._resolve_length_calibration(workspace_id, "都市悬疑", router)
        assert cal is not None
        assert cal.chars_per_beat == 1788  # config.yaml deepseek action 首基线
        assert isinstance(wcm, int)  # workspace generation_config 上限可读

    def test_qwen38_action_resolves_calibration(
        self, workspace_service: WorkspaceService, workspace_id: str
    ) -> None:
        """qwen3.8 §2.143 实测已落库，动作型拆章读取 1088 chars/unit。"""
        svc = _make_service(workspace_service)
        router = MagicMock()
        router.select_profile.return_value = MagicMock(model="qwen3.8-max")
        cal, wcm = svc._resolve_length_calibration(workspace_id, "都市悬疑", router)
        assert cal is not None
        assert cal.chars_per_beat == 1088
        assert cal.chars_per_chapter == 3265
        assert cal.length_compliance == 1.0
        assert isinstance(wcm, int)

    def test_qwen38_new_book_mystery_resolves_action_calibration(
        self, workspace_service: WorkspaceService, workspace_id: str
    ) -> None:
        """正式 NewBookPage 保存的 mystery 必须复用悬疑 action 实测校准。"""
        svc = _make_service(workspace_service)
        router = MagicMock()
        router.select_profile.return_value = MagicMock(model="qwen3.8-max")
        cal, wcm = svc._resolve_length_calibration(workspace_id, "mystery", router)
        assert cal is not None
        assert cal.chars_per_beat == 1088
        assert cal.chars_per_chapter == 3265
        assert cal.length_compliance == 1.0
        assert isinstance(wcm, int)

    def test_unmapped_genre_blocks(
        self, workspace_service: WorkspaceService, workspace_id: str
    ) -> None:
        """正式路由题材未映射原型 → 阻断；既不猜原型，也不跳过细化。"""
        svc = _make_service(workspace_service)
        router = MagicMock()
        router.select_profile.return_value = MagicMock(model="deepseek-v4-pro")
        with pytest.raises(OutlineLengthCalibrationRequired) as exc_info:
            svc._resolve_length_calibration(workspace_id, "赛博朋克", router)
        assert exc_info.value.reason == "genre_archetype_unmapped"

    def test_router_none_returns_none(
        self, workspace_service: WorkspaceService, workspace_id: str
    ) -> None:
        """legacy client+model 路径(router None)→ (None, 上限);不拆。"""
        svc = _make_service(workspace_service)
        cal, _wcm = svc._resolve_length_calibration(workspace_id, "都市悬疑", None)
        assert cal is None

    def test_missing_model_gate_precedes_status_files_and_provider(
        self,
        workspace_service: WorkspaceService,
        workspace_id: str,
    ) -> None:
        """正式全量大纲在 world 请求前阻断，且不留下 generating/半成品。"""
        session_factory = _make_session_factory(workspace_service)
        router = MagicMock()
        router.select_profile.return_value = MagicMock(model="uncalibrated-test-model")
        client = MagicMock()
        service = OutlineService(
            workspace_service=workspace_service,
            client=client,
            model="uncalibrated-test-model",
            router_factory=lambda _workspace_id: router,
            session_factory=session_factory,
        )

        with pytest.raises(OutlineLengthCalibrationRequired):
            service.generate_outline(
                workspace_id,
                premise="premise",
                total_volumes=1,
                chapters_per_volume=12,
                tension_template="heros_journey",
                genre="都市悬疑",
            )

        client.complete.assert_not_called()
        outline_dir = (
            workspace_service.workspaces_root
            / workspace_id
            / "data"
            / "outline"
        )
        assert not any(outline_dir.glob("*.json"))
        session = session_factory(workspace_id)
        try:
            assert session.get(WorkspaceORM, workspace_id) is None
        finally:
            session.close()


class TestOutlineAutofixWiringS4Step1:
    """v2 W3.5-s4-step1 接线防回退:outline_service 解析 outline_autofix 门控 +
    透传 build_outline。

    防未来改 generate_outline 断了 ``_resolve_outline_autofix_enabled`` 调用 / 不把
    结果透传 build_outline → 大纲 name_typo 自动修在生产中失效(工作区级 kill-switch
    无效 / 恒走某固定值),而 §3 启动 audit 的机制层 mock 测照不出(同 breakthrough/
    trim 接线坑,[[feedback_test_full_wiring_not_just_mechanism_ends]])。
    """

    def test_resolve_default_true(
        self, workspace_service: WorkspaceService
    ) -> None:
        workspace_service.create(
            "ws-af", display_name="x", genre="x", premise="x", activate=False,
        )
        service = _make_service(workspace_service)
        assert service._resolve_outline_autofix_enabled("ws-af") is True

    def test_resolve_read_failure_returns_true(
        self, workspace_service: WorkspaceService
    ) -> None:
        # 不存在的工作区 → 读失败 → 退默认 True(机制零 fail-open,开着也不误放行)。
        service = _make_service(workspace_service)
        assert service._resolve_outline_autofix_enabled("nonexistent-ws") is True

    def test_generate_outline_passes_autofix_to_build_outline(
        self,
        workspace_service: WorkspaceService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """generate_outline 把 ``_resolve`` 真值透传 build_outline(outline_autofix_enabled=)。"""
        workspace_service.create(
            "ws-af", display_name="x", genre="x", premise="x", activate=False,
        )
        service = _make_service(workspace_service)
        captured: dict[str, object] = {}

        class _StopBuild(Exception):
            pass

        def _fake_build_outline(**kwargs: object) -> object:
            captured.update(kwargs)
            raise _StopBuild

        monkeypatch.setattr(
            "src.services.outline_service.build_outline", _fake_build_outline
        )
        with pytest.raises(_StopBuild):
            service.generate_outline(
                "ws-af", premise="x", total_volumes=2,
                chapters_per_volume=5, tension_template="heros_journey",
            )
        # 默认 True 必须透传(漏传 = 工作区级 kill-switch 失效)
        assert captured.get("outline_autofix_enabled") is True


class TestCharacterBackfillServiceWiringS4Step1:
    """v2 W3.5-s4-step1 机制1 接线防回退:outline_service 解析 character_backfill
    门控 + 透传 build_outline。

    防未来改 generate_outline 断了 ``_resolve_character_backfill_enabled`` 调用 / 不把
    结果透传 build_outline → 角色完整性回填在生产中失效(工作区级 kill-switch 无效),
    而 §3 启动 audit 的机制层 mock 测照不出([[feedback_test_full_wiring_not_just_mechanism_ends]])。
    """

    def test_resolve_default_true(
        self, workspace_service: WorkspaceService
    ) -> None:
        workspace_service.create(
            "ws-bf", display_name="x", genre="x", premise="x", activate=False,
        )
        service = _make_service(workspace_service)
        assert service._resolve_character_backfill_enabled("ws-bf") is True

    def test_resolve_read_failure_returns_true(
        self, workspace_service: WorkspaceService
    ) -> None:
        # 不存在的工作区 → 读失败 → 退默认 True(回填 fail-open,开着不误伤大纲)。
        service = _make_service(workspace_service)
        assert service._resolve_character_backfill_enabled("nonexistent-ws") is True

    def test_generate_outline_passes_backfill_to_build_outline(
        self,
        workspace_service: WorkspaceService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """generate_outline 把 ``_resolve`` 真值透传 build_outline(character_backfill_enabled=)。"""
        workspace_service.create(
            "ws-bf", display_name="x", genre="x", premise="x", activate=False,
        )
        service = _make_service(workspace_service)
        captured: dict[str, object] = {}

        class _StopBuild(Exception):
            pass

        def _fake_build_outline(**kwargs: object) -> object:
            captured.update(kwargs)
            raise _StopBuild

        monkeypatch.setattr(
            "src.services.outline_service.build_outline", _fake_build_outline
        )
        with pytest.raises(_StopBuild):
            service.generate_outline(
                "ws-bf", premise="x", total_volumes=2,
                chapters_per_volume=5, tension_template="heros_journey",
            )
        # 默认 True 必须透传(漏传 = 工作区级 kill-switch 失效)
        assert captured.get("character_backfill_enabled") is True
