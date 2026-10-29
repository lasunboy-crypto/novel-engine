"""OutlineBuilder agent 单测(P5-B-03)。

走 MockLLMClient 路径 + 临时 prompts 目录隔离。覆盖:

1. 4 段 happy path:``build_world`` / ``build_characters`` / ``build_plot`` /
   ``build_volume_plans`` 各自经 mock 路径返合规 Pydantic 实例。
2. user message 拼装:含 premise / 上游 context / marker 4 子角色。
3. JSON 解析失败抛 ``OutlineBuilderError`` + 不自动降级。
4. agent 出参后置校验(``volumes`` 长度 == ``total_volumes``)。
5. ``ModelRouter`` 注入路径:``agent_routing.outline_builder`` 命中 +
   ``cloud_quality`` 覆盖切 mock provider 分支正确(无 Claude API key 仍可验证)。
6. ``build_outline`` 4 段串行 + ``on_stage_complete`` 回调 + 早失败终止下游。
7. ``MockLLMClient`` 的 4 段 dispatch 分支各自命中正确 fixture。
"""

from __future__ import annotations

import json
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from src.core.agents import _prompt as prompt_module
from src.core.agents import outline_builder
from src.core.agents.beat_alignment_validator import (
    BeatAlignmentValidationResult,
)
from src.core.agents.outline_builder import (
    _normalize_volume_plans_explicit_trailing_field_pair,
    FullOutline,
    MiniPlanSettingConflictError,
    OutlineBuilderError,
    PlotSettingConflictError,
    VolumePlansSettingConflictError,
    _build_length_section,
    _build_plot_user_message,
    _build_volume_plans_user_message,
    _build_volume_refine_user_message,
    _density_anchored_target_chapters,
    _derive_default_chapter_ids,
    _run_stage,
    build_characters,
    build_outline,
    build_plot,
    build_volume_plans,
    build_world,
    generate_mini_plan,
    refine_volume_plans_length_aware,
)
from src.core.agents.outline_consistency_validator import (
    NameTypoFix,
    OutlineConsistencyResult,
)
from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage
from src.core.llm.cancel import (
    LLMCancelledError,
    bound_cancel_event,
    current_cancel_event,
    raise_if_cancelled,
)
from src.core.llm.mock_client import MockLLMClient
from src.core.agents.setting_consistency_validator import (
    SettingConsistencyResult,
    SettingConsistencyTechnicalError,
)
from src.core.models.model_calibration import GenreCalibration
from src.core.models.outline import (
    CharactersOutput,
    MiniPlanItem,
    MiniPlanOutput,
    PlotOutput,
    VolumeArcPlan,
    VolumeBeatPlan,
    VolumePlanItem,
    VolumePlansOutput,
    WorldOutput,
)
from src.core.models.routing import RoutingConfig, WriterProfilesConfig
from src.core.models.review import ConstraintViolation
from src.core.models.setting_contract import SettingContract
from src.core.models.writer_profile import WriterProfile
from src.core.routing.router import ModelRouter
from src.core.rules.outline_contracts import build_volume_plans_contract


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_PREMISE = "一个废柴炼丹师在末法时代重生为顶级仙帝"


def test_volume_plans_normalizer_moves_only_explicit_trailing_pair() -> None:
    source = {
        "volumes": [
            {
                "volume_id": "v1",
                "arcs": [
                    {"arc_id": "a1"},
                    {"arc_id": "a2"},
                    "tension_curve",
                    [4, 7, 9],
                ],
            }
        ]
    }

    with capture_logs() as logs:
        repaired = _normalize_volume_plans_explicit_trailing_field_pair(source)

    assert repaired is not source
    assert source["volumes"][0]["arcs"][-2:] == [
        "tension_curve",
        [4, 7, 9],
    ]
    assert repaired["volumes"][0]["arcs"] == [
        {"arc_id": "a1"},
        {"arc_id": "a2"},
    ]
    assert repaired["volumes"][0]["tension_curve"] == [4, 7, 9]
    assert [entry["event"] for entry in logs] == [
        "outline_volume_plans_explicit_field_pair_repaired"
    ]


def test_volume_plans_normalizer_moves_explicit_root_list_trailing_pair() -> None:
    source = {
        "volumes": [
            {"volume_id": "v1", "arcs": [{"arc_id": "a1"}]},
            "tension_curve",
            [4, 7, 9],
        ]
    }

    with capture_logs() as logs:
        repaired = _normalize_volume_plans_explicit_trailing_field_pair(source)

    assert repaired is not source
    assert source["volumes"][-2:] == ["tension_curve", [4, 7, 9]]
    assert repaired["volumes"] == [
        {
            "volume_id": "v1",
            "arcs": [{"arc_id": "a1"}],
            "tension_curve": [4, 7, 9],
        }
    ]
    assert logs[0]["repair_scopes"] == [
        "volumes_trailing_tension_curve_key_and_list_only"
    ]


@pytest.mark.parametrize(
    "source",
    [
        {"volumes": [{"arcs": [{"arc_id": "a1"}, "curve", [4, 7, 9]]}]},
        {
            "volumes": [
                {
                    "arcs": [{"arc_id": "a1"}, "tension_curve", "4,7,9"],
                }
            ]
        },
        {
            "volumes": [
                {"volume_id": "v1", "arcs": []},
                {"volume_id": "v2", "arcs": []},
                "tension_curve",
                [4, 7, 9],
            ]
        },
        {
            "volumes": [
                {
                    "arcs": [{"arc_id": "a1"}, "tension_curve", [4, 7, 9]],
                    "tension_curve": [1, 2, 3],
                }
            ]
        },
    ],
)
def test_volume_plans_normalizer_keeps_ambiguous_shapes_fail_closed(
    source: dict[str, Any],
) -> None:
    assert _normalize_volume_plans_explicit_trailing_field_pair(source) is source


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    """临时 prompts 目录:复制 4 段真实提示词到 ``outline_builder/`` 子目录。

    使用真实提示词内容(而非 stub),确保 system message 拼装在测试与真实链路
    完全一致;mock 不消费 system 内容,但 ``hash_prompt`` 走相同流程。
    """
    prompts_dir = tmp_path / "prompts"
    sub = prompts_dir / "outline_builder"
    sub.mkdir(parents=True)
    for filename in (
        "world.md",
        "characters.md",
        "plot.md",
        "volume_plans.md",
        "volume_refine.md",
        "mini_plan.md",
    ):
        (sub / filename).write_text(
            f"FAKE_OUTLINE_BUILDER_SYSTEM_PROMPT_{filename}\n\n仅供单测断言。",
            encoding="utf-8",
        )
    (prompts_dir / "prewriting_semantic_validator_v6.md").write_text(
        "FAKE_PREWRITING_SEMANTIC_VALIDATOR_SYSTEM_PROMPT\n\n仅供单测断言。",
        encoding="utf-8",
    )
    (prompts_dir / "beat_alignment_batch_validator_v2.md").write_text(
        "FAKE_BEAT_ALIGNMENT_BATCH_SYSTEM_PROMPT\n\n仅供单测断言。",
        encoding="utf-8",
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _clear_prompt_cache():
    """每个用例清一次 ``load_prompt_template`` lru_cache(对齐其他 agent 单测约定)。"""
    prompt_module.load_prompt_template.cache_clear()
    yield
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture
def mock_client() -> MockLLMClient:
    return MockLLMClient()


def _make_world() -> WorldOutput:
    return WorldOutput(
        setting="架空大陆九州,正值末法时代",
        power_system="修炼九境",
        factions="九大正道 + 三大魔域",
        culture="修真者高于俗世",
        unique_rules="渡劫失败必死",
    )


def _llm_response(content: str, *, model_used: str = "fake-model") -> LLMResponse:
    return LLMResponse(
        content=content,
        model_used=model_used,
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


class _QueuedTextClient(LLMClient):
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    def complete(self, messages: list[Message], **gen_params: Any) -> LLMResponse:
        if not self._responses:
            raise AssertionError("_QueuedTextClient 响应队列已空")
        return _llm_response(self._responses.pop(0))


def _make_characters() -> CharactersOutput:
    """4 个角色满足 min_length=4。"""
    return CharactersOutput(
        characters=[
            {
                "character_id": "lin-yan",
                "name": "林衍",
                "role": "protagonist",
                "archetype": "废柴重生剑修",
                "appearance": "白衣青年",
                "personality": "沉静内敛",
                "background": "重生回入山门那年",
                "motivation": "改变末法时代",
                "voice_hint": "短句直白",
                "relationships_brief": {},
            },
            {
                "character_id": "shi-jie",
                "name": "沈雪",
                "role": "deuteragonist",
                "archetype": "天才师姐",
                "appearance": "雪白长裙",
                "personality": "温柔坚毅",
                "background": "青云门首席",
                "motivation": "守护林衍",
                "voice_hint": "中长句优雅",
                "relationships_brief": {"lin-yan": "师弟"},
            },
            {
                "character_id": "mo-zun",
                "name": "墨尊",
                "role": "antagonist",
                "archetype": "魔域大尊",
                "appearance": "黑袍蒙面",
                "personality": "阴狠多谋",
                "background": "血煞域之主",
                "motivation": "吞噬人间",
                "voice_hint": "低沉冷峻",
                "relationships_brief": {},
            },
            {
                "character_id": "zhu-zhang-lao",
                "name": "朱长老",
                "role": "supporting",
                "archetype": "授业恩师",
                "appearance": "白须长袍",
                "personality": "严慈相济",
                "background": "青云门长老",
                "motivation": "守护九州",
                "voice_hint": "温和中长句",
                "relationships_brief": {},
            },
        ],
    )


def _make_plot() -> PlotOutput:
    return PlotOutput(
        core_conflict="重生林衍 vs 魔域大尊",
        main_phases=[
            {"phase_id": "phase-1", "title": "觉醒", "summary": "重生入门"},
            {"phase_id": "phase-2", "title": "试炼", "summary": "内门选拔"},
            {"phase_id": "phase-3", "title": "真相", "summary": "师傅遇害"},
            {"phase_id": "phase-4", "title": "大战", "summary": "正魔大战"},
        ],
        major_turning_points=[
            {"turning_id": "turn-1", "title": "师傅之死", "description": "线索"},
            {"turning_id": "turn-2", "title": "墨尊现身", "description": "破裂"},
            {"turning_id": "turn-3", "title": "终极决战", "description": "存亡"},
        ],
        long_threads=[
            {"thread_id": "yu-pei", "title": "玉佩之约", "description": "约定"},
            {"thread_id": "shi-fu", "title": "师傅遗物", "description": "名册"},
            {"thread_id": "zhen-xian", "title": "真仙之位", "description": "归属"},
        ],
        character_arcs={"lin-yan": "成长", "mo-zun": "暴露"},
    )


# ---------------------------------------------------------------------------
# 测试套 1:4 段 happy path
# ---------------------------------------------------------------------------


class TestStageBuildHappyPath:
    """走 MockLLMClient legacy 路径,4 段各自返合规 Pydantic 实例。"""

    def test_build_world_happy_path(
        self, fake_prompts_dir: Path, mock_client: MockLLMClient
    ) -> None:
        world = build_world(
            premise=_PREMISE,
            genre="仙侠",
            total_volumes=3,
            chapters_per_volume=15,
            client=mock_client,
            model="mock-default",
            prompts_dir=fake_prompts_dir,
        )
        assert isinstance(world, WorldOutput)
        assert world.setting.startswith("[mock]")
        assert world.power_system
        assert world.factions
        assert world.culture
        assert world.unique_rules

    def test_build_characters_requires_explicit_initial_core_fact_decision(
        self,
        fake_prompts_dir: Path,
        mock_client: MockLLMClient,
    ) -> None:
        response = mock_client.complete(
            [Message(role="user", content="<!-- agent_role: outline_characters -->")],
            model="mock-default",
        )
        payload = json.loads(response.content)
        for item in payload["characters"]:
            item.pop("initial_core_facts", None)
        raw = json.dumps(payload, ensure_ascii=False)
        client = _QueuedTextClient([raw, raw, raw])

        with pytest.raises(OutlineBuilderError) as exc_info:
            build_characters(
                premise=_PREMISE,
                world=_make_world(),
                client=client,
                model="fake-model",
                prompts_dir=fake_prompts_dir,
            )
        assert exc_info.value.__cause__ is not None
        assert "initial_core_facts" in str(exc_info.value.__cause__)

    def test_characters_output_accepts_legacy_null_initial_core_facts(
        self, mock_client: MockLLMClient
    ) -> None:
        response = mock_client.complete(
            [Message(role="user", content="<!-- agent_role: outline_characters -->")],
            model="mock-default",
        )
        payload = json.loads(response.content)
        payload["characters"][0]["initial_core_facts"] = None

        parsed = CharactersOutput.model_validate(payload)

        assert parsed.characters[0].initial_core_facts == []
        assert "initial_core_facts" in parsed.characters[0].model_fields_set

    def test_build_characters_happy_path(
        self, fake_prompts_dir: Path, mock_client: MockLLMClient
    ) -> None:
        world = _make_world()
        result = build_characters(
            premise=_PREMISE,
            world=world,
            client=mock_client,
            model="mock-default",
            prompts_dir=fake_prompts_dir,
        )
        assert isinstance(result, CharactersOutput)
        assert 4 <= len(result.characters) <= 8
        # mock fixture 含 protagonist
        roles = {c.role for c in result.characters}
        assert "protagonist" in roles
        assert "antagonist" in roles

    def test_build_plot_happy_path(
        self, fake_prompts_dir: Path, mock_client: MockLLMClient
    ) -> None:
        result = build_plot(
            premise=_PREMISE,
            world=_make_world(),
            characters=_make_characters(),
            client=mock_client,
            model="mock-default",
            prompts_dir=fake_prompts_dir,
        )
        assert isinstance(result, PlotOutput)
        assert result.core_conflict
        assert 4 <= len(result.main_phases) <= 7
        assert 3 <= len(result.major_turning_points) <= 6
        assert 3 <= len(result.long_threads) <= 6
        assert isinstance(result.character_arcs, dict)

    def test_build_volume_plans_happy_path(
        self, fake_prompts_dir: Path, mock_client: MockLLMClient
    ) -> None:
        result = build_volume_plans(
            premise=_PREMISE,
            world=_make_world(),
            characters=_make_characters(),
            plot=_make_plot(),
            total_volumes=3,
            chapters_per_volume=15,
            tension_template="heros_journey",
            client=mock_client,
            model="mock-default",
            prompts_dir=fake_prompts_dir,
        )
        assert isinstance(result, VolumePlansOutput)
        assert len(result.volumes) == 3
        # 每卷 chapter_count + arcs + tension_curve 严格校验
        for vol in result.volumes:
            assert vol.chapter_count == 15
            assert 2 <= len(vol.arcs) <= 4
            assert len(vol.tension_curve) == 15
            # arcs 覆盖 [1, chapter_count] 闭区间
            assert vol.arcs[0].chapter_range[0] == 1
            assert vol.arcs[-1].chapter_range[1] == 15


# ---------------------------------------------------------------------------
# 测试套 2:premise 边界 + 出参后置校验
# ---------------------------------------------------------------------------


class TestEdgeInputs:
    """边界入参 + agent 出参后置校验。"""

    @pytest.mark.parametrize("empty", ["", "   ", "\n\t"])
    def test_build_world_rejects_empty_premise(
        self, fake_prompts_dir: Path, mock_client: MockLLMClient, empty: str
    ) -> None:
        with pytest.raises(ValueError, match="premise 不能为空"):
            build_world(
                premise=empty,
                client=mock_client,
                model="mock-default",
                prompts_dir=fake_prompts_dir,
            )

    def test_build_characters_rejects_empty_premise(
        self, fake_prompts_dir: Path, mock_client: MockLLMClient
    ) -> None:
        with pytest.raises(ValueError, match="premise 不能为空"):
            build_characters(
                premise="",
                world=_make_world(),
                client=mock_client,
                model="mock-default",
                prompts_dir=fake_prompts_dir,
            )

    def test_build_volume_plans_rejects_zero_total_volumes(
        self, fake_prompts_dir: Path, mock_client: MockLLMClient
    ) -> None:
        with pytest.raises(ValueError, match="total_volumes must be >= 1"):
            build_volume_plans(
                premise=_PREMISE,
                world=_make_world(),
                characters=_make_characters(),
                plot=_make_plot(),
                total_volumes=0,
                chapters_per_volume=10,
                tension_template="heros_journey",
                client=mock_client,
                model="mock-default",
                prompts_dir=fake_prompts_dir,
            )

    def test_build_volume_plans_rejects_zero_chapters_per_volume(
        self, fake_prompts_dir: Path, mock_client: MockLLMClient
    ) -> None:
        with pytest.raises(ValueError, match="chapters_per_volume must be >= 1"):
            build_volume_plans(
                premise=_PREMISE,
                world=_make_world(),
                characters=_make_characters(),
                plot=_make_plot(),
                total_volumes=3,
                chapters_per_volume=0,
                tension_template="heros_journey",
                client=mock_client,
                model="mock-default",
                prompts_dir=fake_prompts_dir,
            )


class TestRouterOrLegacyRequired:
    """``router=None`` 且 legacy 缺 ``client`` / ``model`` 时抛 ``TypeError``。"""

    def test_world_requires_router_or_legacy(
        self, fake_prompts_dir: Path
    ) -> None:
        with pytest.raises(TypeError, match="必须传入 router= 或"):
            build_world(
                premise=_PREMISE,
                client=None,
                model=None,
                prompts_dir=fake_prompts_dir,
            )


# ---------------------------------------------------------------------------
# 测试套 3:Prompt assembly + agent role marker
# ---------------------------------------------------------------------------


class TestPromptAssembly:
    """验证 system 来自模板 + user message 含 premise / 上游 / marker。"""

    def _intercept_complete(self) -> tuple[MagicMock, list[list[Message]]]:
        """构造 MagicMock client,拦截 ``complete(messages=...)`` 入参以供断言。"""
        captured: list[list[Message]] = []

        def _complete(messages: list[Message], **kwargs):
            captured.append(messages)
            # 返回 world 段固定 JSON,让 Pydantic 校验通过。其他 stage 不会触
            # 发本辅助;若需扩展再加。
            payload = {
                "setting": "S",
                "power_system": "P",
                "factions": "F",
                "culture": "C",
                "unique_rules": "U",
            }
            return LLMResponse(
                content=json.dumps(payload),
                model_used="mock-default",
                usage=TokenUsage(input_tokens=1, output_tokens=1),
                finish_reason="end_turn",
            )

        client = MagicMock(spec=LLMClient)
        client.complete.side_effect = _complete
        return client, captured

    def test_world_user_message_contains_premise_and_marker(
        self, fake_prompts_dir: Path
    ) -> None:
        client, captured = self._intercept_complete()
        build_world(
            premise=_PREMISE,
            genre="仙侠",
            total_volumes=5,
            chapters_per_volume=30,
            client=client,
            model="mock-default",
            prompts_dir=fake_prompts_dir,
        )
        messages = captured[0]
        # system 来自 fake 模板
        assert messages[0].role == "system"
        assert "FAKE_OUTLINE_BUILDER_SYSTEM_PROMPT_world.md" in messages[0].content
        # user 含 premise + genre + 卷数 + marker
        user_text = messages[1].content
        assert messages[1].role == "user"
        assert _PREMISE in user_text
        assert "仙侠" in user_text
        assert "total_volumes" in user_text
        assert "<!-- agent_role: outline_world -->" in user_text

    def test_characters_user_message_carries_world_context(
        self, fake_prompts_dir: Path
    ) -> None:
        captured: list[list[Message]] = []

        def _complete(messages: list[Message], **kwargs):
            captured.append(messages)
            return LLMResponse(
                content=json.dumps(_make_characters().model_dump()),
                model_used="mock-default",
                usage=TokenUsage(input_tokens=1, output_tokens=1),
                finish_reason="end_turn",
            )

        client = MagicMock(spec=LLMClient)
        client.complete.side_effect = _complete
        world = _make_world()
        build_characters(
            premise=_PREMISE,
            world=world,
            client=client,
            model="mock-default",
            prompts_dir=fake_prompts_dir,
        )
        user_text = captured[0][1].content
        assert _PREMISE in user_text
        assert world.setting in user_text
        assert world.factions in user_text
        assert "<!-- agent_role: outline_characters -->" in user_text

    def test_plot_user_message_carries_character_ids(
        self, fake_prompts_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[list[Message]] = []

        def _complete(messages: list[Message], **kwargs):
            captured.append(messages)
            return LLMResponse(
                content=json.dumps(_make_plot().model_dump()),
                model_used="mock-default",
                usage=TokenUsage(input_tokens=1, output_tokens=1),
                finish_reason="end_turn",
            )

        client = MagicMock(spec=LLMClient)
        client.complete.side_effect = _complete
        monkeypatch.setattr(
            outline_builder,
            "validate_setting_consistency",
            lambda **_kwargs: SettingConsistencyResult(passed=True),
        )
        characters = _make_characters()
        build_plot(
            premise=_PREMISE,
            world=_make_world(),
            characters=characters,
            client=client,
            model="mock-default",
            prompts_dir=fake_prompts_dir,
        )
        user_text = captured[0][1].content
        for c in characters.characters:
            assert c.character_id in user_text
        assert "<!-- agent_role: outline_plot -->" in user_text


# ---------------------------------------------------------------------------
# 测试套 4:JSON 解析失败 / schema 不符抛清晰异常
# ---------------------------------------------------------------------------


class TestJsonAndSchemaFailures:
    """LLM 输出非 JSON / 缺字段 / 长度越界 → ``OutlineBuilderError`` 含 stage / raw_output。"""

    def _client_returning(self, raw: str) -> MagicMock:
        client = MagicMock(spec=LLMClient)
        client.complete.return_value = LLMResponse(
            content=raw,
            model_used="mock-default",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="end_turn",
        )
        return client

    def test_invalid_json_raises_with_stage(self, fake_prompts_dir: Path) -> None:
        client = self._client_returning("this is not json at all")
        with pytest.raises(OutlineBuilderError) as exc:
            build_world(
                premise=_PREMISE,
                client=client,
                model="mock-default",
                prompts_dir=fake_prompts_dir,
            )
        assert exc.value.stage == "world"
        assert "非合法 JSON" in str(exc.value)
        assert exc.value.raw_output == "this is not json at all"

    def test_schema_mismatch_raises_with_stage(self, fake_prompts_dir: Path) -> None:
        # 缺 ``factions`` 字段
        bad_payload = {
            "setting": "S",
            "power_system": "P",
            "culture": "C",
            "unique_rules": "U",
        }
        client = self._client_returning(json.dumps(bad_payload))
        with pytest.raises(OutlineBuilderError) as exc:
            build_world(
                premise=_PREMISE,
                client=client,
                model="mock-default",
                prompts_dir=fake_prompts_dir,
            )
        assert exc.value.stage == "world"
        assert "schema 不符" in str(exc.value)

    def test_characters_too_few_raises(self, fake_prompts_dir: Path) -> None:
        """``characters`` 长度 3 < 4 → Pydantic ValidationError → OutlineBuilderError。"""
        # 构造 3 个角色(min_length=4)
        bad_payload = {
            "characters": _make_characters().model_dump()["characters"][:3]
        }
        client = self._client_returning(json.dumps(bad_payload))
        with pytest.raises(OutlineBuilderError) as exc:
            build_characters(
                premise=_PREMISE,
                world=_make_world(),
                client=client,
                model="mock-default",
                prompts_dir=fake_prompts_dir,
            )
        assert exc.value.stage == "characters"

    def test_code_fence_wrapped_json_is_stripped(self, fake_prompts_dir: Path) -> None:
        """LLM 把 JSON 套 ``` ```json ... ``` ``` 包裹时,_strip_format_noise 剥后能解析。"""
        payload = {
            "setting": "S",
            "power_system": "P",
            "factions": "F",
            "culture": "C",
            "unique_rules": "U",
        }
        wrapped = f"```json\n{json.dumps(payload)}\n```"
        client = self._client_returning(wrapped)
        result = build_world(
            premise=_PREMISE,
            client=client,
            model="mock-default",
            prompts_dir=fake_prompts_dir,
        )
        assert isinstance(result, WorldOutput)


class TestOutlineStageTiming:
    """E:``_run_stage`` 汇合点记录 retry 成功与 retry 耗尽失败。"""

    def test_run_stage_timing_records_retry_success(self) -> None:
        client = _QueuedTextClient(
            [
                "not-json",
                json.dumps(_make_world().model_dump(), ensure_ascii=False),
            ]
        )

        with capture_logs() as logs:
            result = _run_stage(
                stage="world",
                system_text="system",
                user_text="user",
                output_model=WorldOutput,
                router=None,
                client=client,
                model="fake-model",
                max_tokens=100,
                temperature=0.1,
                timing_context={"workspace_id": "ws-timing"},
            )

        assert isinstance(result, WorldOutput)
        timings = [
            log for log in logs if log.get("event") == "outline_stage_timing"
        ]
        assert len(timings) == 1
        assert timings[0]["stage"] == "world"
        assert timings[0]["substage"] == "llm"
        assert timings[0]["status"] == "succeeded"
        assert timings[0]["attempts"] == 2
        assert timings[0]["retried"] is True
        assert timings[0]["duration_ms"] >= 0

    def test_run_stage_timing_records_retry_exhausted_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            outline_builder.settings, "outline_builder_max_retry_attempts", 2
        )
        client = _QueuedTextClient(["not-json", "still-not-json"])

        with capture_logs() as logs:
            with pytest.raises(OutlineBuilderError):
                _run_stage(
                    stage="world",
                    system_text="system",
                    user_text="user",
                    output_model=WorldOutput,
                    router=None,
                    client=client,
                    model="fake-model",
                    max_tokens=100,
                    temperature=0.1,
                    timing_context={"workspace_id": "ws-timing"},
                )

        timings = [
            log for log in logs if log.get("event") == "outline_stage_timing"
        ]
        assert len(timings) == 1
        assert timings[0]["stage"] == "world"
        assert timings[0]["status"] == "failed"
        assert timings[0]["attempts"] == 2
        assert timings[0]["reason"] == "invalid_json"
        assert timings[0]["retry_exhausted"] is True
        assert timings[0]["duration_ms"] >= 0


# ---------------------------------------------------------------------------
# 测试套 5:agent 出参后置校验(volumes 长度 == total_volumes)
# ---------------------------------------------------------------------------


class TestPostValidation:
    """``build_volume_plans`` 出参 ``volumes`` 长度严格 == ``total_volumes``。"""

    def test_volumes_length_mismatch_raises_outline_error(
        self, fake_prompts_dir: Path
    ) -> None:
        """mock fixture 用 total_volumes=2 拼 2 卷;agent 入参传 total_volumes=5
        → mock 返 2 卷,agent 出参后置校验抓到不一致 → OutlineBuilderError。

        其实 mock fixture 严格按 user_text 抓 total_volumes,所以 mock 返 5 卷;
        本测试改用 MagicMock 强制返 1 卷,验证 agent 后置校验生效。

        VCD-3 起,post-validation 错误仍走 retry helper,但同一稳定 failure
        fingerprint 连续两次无进展会在购买第三份同类响应前停止。
        """
        bad_volume_plans = {
            "volumes": [
                {
                    "volume_id": "v1",
                    "title": "卷一",
                    "summary": "summary",
                    "chapter_count": 10,
                    "tension_template": "heros_journey",
                    "linked_phases": [],
                    "linked_turning_points": [],
                    "linked_threads": [],
                    "arcs": [
                        {
                            "arc_id": "v1-a-1",
                            "arc_type": "主线",
                            "chapter_range": [1, 4],
                            "phase": "opening",
                            "summary": "开篇",
                        },
                        {
                            "arc_id": "v1-a-2",
                            "arc_type": "主线",
                            "chapter_range": [5, 10],
                            "phase": "climax",
                            "summary": "高潮",
                        },
                    ],
                    "tension_curve": [3, 4, 5, 5, 6, 7, 8, 9, 8, 7],
                }
            ]
        }
        client = MagicMock(spec=LLMClient)
        client.complete.return_value = LLMResponse(
            content=json.dumps(bad_volume_plans),
            model_used="mock-default",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="end_turn",
        )
        with pytest.raises(OutlineBuilderError) as exc:
            build_volume_plans(
                premise=_PREMISE,
                world=_make_world(),
                characters=_make_characters(),
                plot=_make_plot(),
                total_volumes=5,  # 入参 5,mock 返 1 → 不一致
                chapters_per_volume=10,
                tension_template="heros_journey",
                client=client,
                model="mock-default",
                prompts_dir=fake_prompts_dir,
            )
        assert exc.value.stage == "volume_plans"
        # v1.0.x wave 5 session 2:post-validation 错误走 retry helper,
        # 终态消息含"N 次 retry 后业务规则校验仍失败"。
        assert "业务规则" in str(exc.value)
        assert exc.value.retry_stop_reason == "no_progress"
        assert exc.value.failure_fingerprint
        # retry 真发生一次；同错连续出现后不会购买第三份同类响应。
        assert client.complete.call_count == 2


def _contract_volume_payload(
    *,
    volume_id: str = "v1",
    linked_thread: str = "yu-pei",
) -> dict[str, object]:
    payload = _make_volume_plan_item(volume_id, 10).model_dump(mode="json")
    payload["linked_phases"] = ["phase-1"]
    payload["linked_turning_points"] = ["turn-1"]
    payload["linked_threads"] = [linked_thread]
    return payload


class _SequentialVolumePlansClient(LLMClient):
    """按调用顺序返回 volume_plans JSON,用于验证 post_validate retry。"""

    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self._payloads = list(payloads)
        self.calls = 0

    def complete(self, *, messages, model, **kwargs):  # type: ignore[override]
        self.calls += 1
        if not self._payloads:
            raise AssertionError("_SequentialVolumePlansClient:响应已耗尽")
        payload = self._payloads.pop(0)
        return LLMResponse(
            content=json.dumps(payload, ensure_ascii=False),
            model_used=model or "fake-volume-plans",
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )


class TestVolumePlansContract:
    """W3.5 D2:``volume_plans`` 契约先行 + 局部 retry。"""

    def test_skeleton_message_injects_contract_before_llm(self) -> None:
        contract = build_volume_plans_contract(
            characters=_make_characters(),
            plot=_make_plot(),
            total_volumes=2,
            chapters_per_volume=10,
        )
        text = _build_volume_plans_user_message(
            premise=_PREMISE,
            genre="都市悬疑",
            world=_make_world(),
            characters=_make_characters(),
            plot=_make_plot(),
            total_volumes=2,
            chapters_per_volume=10,
            tension_template="heros_journey",
            contract=contract,
        )
        assert "本轮 volume_plans 结构契约" in text
        assert "volumes 长度必须 == 2" in text
        assert "linked_threads 只能引用:shi-fu, yu-pei, zhen-xian" in text
        assert "角色引用必须来自 characters/backfill 后集合" in text

    def test_contract_retry_catches_unknown_linked_id_before_mini_plan(
        self, fake_prompts_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad = {"volumes": [_contract_volume_payload(linked_thread="ghost-thread")]}
        good = {"volumes": [_contract_volume_payload(linked_thread="yu-pei")]}
        client = _SequentialVolumePlansClient([bad, good])
        monkeypatch.setattr(
            outline_builder,
            "validate_setting_consistency",
            lambda **_kwargs: SettingConsistencyResult(passed=True),
        )

        result = build_volume_plans(
            premise=_PREMISE,
            world=_make_world(),
            characters=_make_characters(),
            plot=_make_plot(),
            total_volumes=1,
            chapters_per_volume=10,
            tension_template="heros_journey",
            client=client,
            model="fake-volume-plans",
            prompts_dir=fake_prompts_dir,
        )

        assert client.calls == 2
        assert result.volumes[0].linked_threads == ["yu-pei"]


# ---------------------------------------------------------------------------
# 测试套 6:ModelRouter 路径 + agent_routing
# ---------------------------------------------------------------------------


def _build_router_config(
    *, outline_builder_profile: str = "p_local"
) -> WriterProfilesConfig:
    """两 profile:p_local(默认)+ p_premium(切走 cloud_quality 验证)。"""
    profiles: dict[str, WriterProfile] = {
        "p_local": WriterProfile(
            profile_id="p_local",
            provider="mock",
            model="mock-local",
            context_window=16000,
        ),
        "p_premium": WriterProfile(
            profile_id="p_premium",
            provider="mock",
            model="mock-premium",
            context_window=32000,
        ),
    }
    return WriterProfilesConfig(
        profiles=profiles,
        routing=RoutingConfig(
            default="p_local",
            rules=[],
            agent_routing={"outline_builder": outline_builder_profile},
        ),
    )


class TestRouterPath:
    """``router.route('outline_builder')`` 命中 ``agent_routing.outline_builder``。"""

    def test_router_dispatches_to_outline_builder_profile(
        self, fake_prompts_dir: Path
    ) -> None:
        constructed_clients: list[LLMClient] = []

        def _factory(**kw) -> LLMClient:
            c = MockLLMClient()
            constructed_clients.append(c)
            return c

        config = _build_router_config(outline_builder_profile="p_local")
        router = ModelRouter(config, client_factory=_factory)

        world = build_world(
            premise=_PREMISE,
            router=router,
            prompts_dir=fake_prompts_dir,
        )
        assert isinstance(world, WorldOutput)
        # 只构造了一个 client(p_local 一份)
        assert len(constructed_clients) == 1

    def test_router_with_different_profile_constructs_different_client(
        self, fake_prompts_dir: Path
    ) -> None:
        """``agent_routing.outline_builder=p_premium`` 时 router 取 p_premium client。

        缓存键 ``(provider, model, base_url)`` = (mock, mock-premium, None)。
        """
        constructed_clients: list[tuple[str, str | None]] = []

        def _factory(
            *,
            model: str,
            base_url: str | None = None,
            provider: str | None = None,
        ) -> LLMClient:
            constructed_clients.append((model, base_url))
            return MockLLMClient()

        config = _build_router_config(outline_builder_profile="p_premium")
        router = ModelRouter(config, client_factory=_factory)

        build_world(
            premise=_PREMISE,
            router=router,
            prompts_dir=fake_prompts_dir,
        )
        # world stage 走 outline_builder=p_premium → 首个构造的 client 是 mock-premium。
        # v1.1 wave 6 session 1 / A1:build_world post_validate 还会调
        # mechanism_consistency_validator(本测试 fake_prompts_dir 无该 prompt → 该
        # validator setup fail-open,但 resolve_routing 仍按默认 profile 构造一个
        # client)。故只断言"world stage 用了 premium",不锁定后续 validator client。
        assert constructed_clients[0] == ("mock-premium", None)


# ---------------------------------------------------------------------------
# 测试套 7:build_outline 4 段串行 + on_stage_complete 回调
# ---------------------------------------------------------------------------


class TestBuildOutlineSequence:
    """``build_outline`` 一键 5 段串行(v1.0.x wave 4 session 1 起加 mini_plan)+
    早失败终止下游。"""

    def test_full_outline_passes_5_stages(
        self, fake_prompts_dir: Path, mock_client: MockLLMClient
    ) -> None:
        from src.core.models.outline import MiniPlanOutput

        result = build_outline(
            premise=_PREMISE,
            total_volumes=3,
            chapters_per_volume=15,
            tension_template="heros_journey",
            genre="仙侠",
            client=mock_client,
            model="mock-default",
            prompts_dir=fake_prompts_dir,
        )
        assert isinstance(result, FullOutline)
        assert isinstance(result.world, WorldOutput)
        assert isinstance(result.characters, CharactersOutput)
        assert isinstance(result.plot, PlotOutput)
        assert isinstance(result.volume_plans, VolumePlansOutput)
        assert isinstance(result.mini_plan, MiniPlanOutput)
        assert len(result.volume_plans.volumes) == 3
        # mini_plan 全书 items 数 = sum(volume.chapter_count) = 3 * 15 = 45
        assert len(result.mini_plan.items) == 45

    def test_on_stage_complete_fires_5_times(
        self, fake_prompts_dir: Path, mock_client: MockLLMClient
    ) -> None:
        from src.core.models.outline import MiniPlanOutput

        events: list[tuple[str, type]] = []

        def callback(stage: str, output) -> None:
            events.append((stage, type(output)))

        build_outline(
            premise=_PREMISE,
            total_volumes=2,
            chapters_per_volume=15,
            tension_template="heros_journey",
            client=mock_client,
            model="mock-default",
            prompts_dir=fake_prompts_dir,
            on_stage_complete=callback,
        )
        assert [e[0] for e in events] == [
            "world",
            "characters",
            "plot",
            "volume_plans",
            "mini_plan",
        ]
        assert [e[1] for e in events] == [
            WorldOutput,
            CharactersOutput,
            PlotOutput,
            VolumePlansOutput,
            MiniPlanOutput,
        ]

    def test_mini_plan_chapter_ids_globally_unique(
        self, fake_prompts_dir: Path, mock_client: MockLLMClient
    ) -> None:
        """build_outline 跑完 5 段后,mini_plan items 的 chapter_id 全书唯一,
        且按 (volume_id, chapter_number) 顺序填(对齐 ``_derive_default_chapter_ids``)。"""
        result = build_outline(
            premise=_PREMISE,
            total_volumes=2,
            chapters_per_volume=10,
            tension_template="heros_journey",
            client=mock_client,
            model="mock-default",
            prompts_dir=fake_prompts_dir,
        )
        chapter_ids = [item.chapter_id for item in result.mini_plan.items]
        # 全书 20 章 chapter_id 唯一
        assert len(chapter_ids) == 20
        assert len(set(chapter_ids)) == 20
        # ch-0001 .. ch-0020 顺序
        assert chapter_ids[0] == "ch-0001"
        assert chapter_ids[-1] == "ch-0020"

    def test_early_stage_failure_stops_downstream(
        self, fake_prompts_dir: Path
    ) -> None:
        """world 段抛 OutlineBuilderError 时,characters / plot / volume_plans
        都不应被调用(对齐 p5_plan §6 '中间阶段失败不进入下游')。

        v1.0.x wave 5 session 2 起,_run_stage 接入 retry helper:world 段
        每次都返非法 JSON → 3 次 retry 都失败 → 最终 raise。client.complete
        被调 3 次(都在 world 段),下游 characters/plot/volume_plans/mini_plan
        段不触发。
        """
        client = MagicMock(spec=LLMClient)
        client.complete.return_value = LLMResponse(
            content="invalid json garbage",
            model_used="mock-default",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="end_turn",
        )

        events: list[str] = []

        def callback(stage: str, output) -> None:
            events.append(stage)

        with pytest.raises(OutlineBuilderError) as exc:
            build_outline(
                premise=_PREMISE,
                total_volumes=3,
                chapters_per_volume=15,
                tension_template="heros_journey",
                client=client,
                model="mock-default",
                prompts_dir=fake_prompts_dir,
                on_stage_complete=callback,
            )
        assert exc.value.stage == "world"
        # retry helper 调 3 次 complete(world 段全部失败),下游不调
        assert client.complete.call_count == 3
        assert events == []  # 回调一次也不触发(world 失败前未到 callback)


# ---------------------------------------------------------------------------
# 测试套 8:MockLLMClient 4 段 dispatch 分支
# ---------------------------------------------------------------------------


class TestMockClientOutlineDispatch:
    """MockLLMClient 对 4 个 ``outline_*`` marker 各自返合规 JSON。"""

    def _build_messages(self, marker: str, extra: str = "") -> list[Message]:
        return [
            Message(role="system", content="SYS"),
            Message(
                role="user",
                content=f"## test\n\n{extra}\n\n<!-- agent_role: {marker} -->",
            ),
        ]

    def test_outline_world_returns_world_schema(
        self, mock_client: MockLLMClient
    ) -> None:
        response = mock_client.complete(
            self._build_messages("outline_world"), model="mock-default"
        )
        data = json.loads(response.content)
        WorldOutput.model_validate(data)  # 抛即测试失败

    def test_outline_characters_returns_characters_schema(
        self, mock_client: MockLLMClient
    ) -> None:
        response = mock_client.complete(
            self._build_messages("outline_characters"), model="mock-default"
        )
        data = json.loads(response.content)
        CharactersOutput.model_validate(data)

    def test_outline_plot_returns_plot_schema(
        self, mock_client: MockLLMClient
    ) -> None:
        response = mock_client.complete(
            self._build_messages("outline_plot"), model="mock-default"
        )
        data = json.loads(response.content)
        PlotOutput.model_validate(data)

    def test_outline_volume_plans_uses_user_inputs(
        self, mock_client: MockLLMClient
    ) -> None:
        """user_text 含 ``total_volumes=4`` / ``chapters_per_volume=20`` 时,
        mock fixture 应裁剪 4 卷 + 每卷 20 章。"""
        extra = (
            "- total_volumes:4\n"
            "- chapters_per_volume:20\n"
            "- tension_template:steady_daily\n"
        )
        response = mock_client.complete(
            self._build_messages("outline_volume_plans", extra=extra),
            model="mock-default",
        )
        data = json.loads(response.content)
        result = VolumePlansOutput.model_validate(data)
        assert len(result.volumes) == 4
        for vol in result.volumes:
            assert vol.chapter_count == 20
            assert vol.tension_template == "steady_daily"
            assert len(vol.tension_curve) == 20


# ---------------------------------------------------------------------------
# 测试套 8b:MockLLMClient mini_plan_generator dispatch
# (v1.0.x wave 4 session 1)
# ---------------------------------------------------------------------------


class TestMockClientMiniPlanDispatch:
    """MockLLMClient 对 ``mini_plan_generator`` marker 返合规单卷 mini_plan JSON。"""

    def _build_messages(self, user_body: str) -> list[Message]:
        return [
            Message(role="system", content="SYS"),
            Message(
                role="user",
                content=user_body + "\n<!-- agent_role: mini_plan_generator -->",
            ),
        ]

    def test_mini_plan_returns_valid_items_for_single_volume(
        self, mock_client: MockLLMClient
    ) -> None:
        """单卷 user_text 含 chapter_id 映射 → mock 应返合规 items 列表。"""
        from src.core.agents.outline_builder import _SingleVolumeMiniPlanOutput

        user_body = (
            "## 当前卷规划(只读)\n\n"
            "- volume_id:v1\n"
            "- title:测试卷\n"
            "- summary:test\n"
            "- chapter_count:5\n"
            "- tension_curve(每章 1-10):[2, 4, 5, 7, 6]\n\n"
            "### 本卷 arcs(arc_id 必命中下列之一)\n"
            "  - v1-arc-1-opening | 主线 | chapter_range=[1, 5] | phase=opening\n"
            "    summary: opening arc\n\n"
            "## 当前可用角色清单\n\n"
            "  - char-protagonist | 主角(protagonist) | voice_hint: 沉稳\n"
            "  - char-mentor | 导师(mentor) | voice_hint: 温和\n\n"
            "## 当前活跃伏笔清单\n\n"
            "  - thread-mystery-sword | 神秘剑灵: 主角神剑之谜\n\n"
            "## 章节 ID 映射\n\n"
            "  - chapter_number=1 → chapter_id=ch-0001\n"
            "  - chapter_number=2 → chapter_id=ch-0002\n"
            "  - chapter_number=3 → chapter_id=ch-0003\n"
            "  - chapter_number=4 → chapter_id=ch-0004\n"
            "  - chapter_number=5 → chapter_id=ch-0005\n"
        )
        response = mock_client.complete(
            self._build_messages(user_body), model="mock-default"
        )
        data = json.loads(response.content)
        result = _SingleVolumeMiniPlanOutput.model_validate(data)
        assert len(result.items) == 5

        for idx, item in enumerate(result.items, start=1):
            assert item.chapter_id == f"ch-{idx:04d}"
            assert item.chapter_number == idx
            assert item.volume_id == "v1"
            assert item.arc_id == "v1-arc-1-opening"
            assert item.one_line_event  # 非空
            assert len(item.one_line_event) > 10  # 至少 10 字

    def test_mini_plan_items_are_diversified(
        self, mock_client: MockLLMClient
    ) -> None:
        """5 张样本 one_line_event **必须差异化**(主语 / 动词 / 宾语三元素轮换)。

        wave 4 session 1 立的新测试纪律:覆盖 mock fixture 假递进盲区。
        若 fixture 退化为按 chapter_number 简单派生占位文字,本测必失败。
        """
        user_body = (
            "## 当前卷规划(只读)\n\n"
            "- volume_id:v1\n"
            "- chapter_count:5\n"
            "- tension_curve(每章 1-10):[3, 5, 7, 6, 8]\n\n"
            "### 本卷 arcs(arc_id 必命中下列之一)\n"
            "  - v1-arc-1-opening | 主线 | chapter_range=[1, 5] | phase=opening\n"
            "    summary: opening arc\n\n"
            "  - char-a | 主角(protagonist) | voice_hint: x\n"
            "  - char-b | 导师(mentor) | voice_hint: y\n\n"
            "  - thread-1 | t: d\n\n"
            "  - chapter_number=1 → chapter_id=ch-0001\n"
            "  - chapter_number=2 → chapter_id=ch-0002\n"
            "  - chapter_number=3 → chapter_id=ch-0003\n"
            "  - chapter_number=4 → chapter_id=ch-0004\n"
            "  - chapter_number=5 → chapter_id=ch-0005\n"
        )
        response = mock_client.complete(
            self._build_messages(user_body), model="mock-default"
        )
        data = json.loads(response.content)
        items = data["items"]
        assert len(items) == 5
        events = [item["one_line_event"] for item in items]
        # 5 条 one_line_event 字面不重复(粗筛)
        assert len(set(events)) == 5, (
            "Mock fixture 退化为雷同输出:\n  " + "\n  ".join(events)
        )
        # 任意两条编辑距离不应 < 5(粗筛 — 完全雷同字面 = 失败)
        # 用更简单的"字符集差异 ≥ 10%"启发式(避免引入 Levenshtein 依赖)
        for i in range(len(events)):
            for j in range(i + 1, len(events)):
                set_a = set(events[i])
                set_b = set(events[j])
                shared = len(set_a & set_b)
                total = len(set_a | set_b)
                # 共享字符 / 总字符 < 90% — 即至少 10% 字符不同
                assert shared / total < 0.9, (
                    f"Mock fixture items {i} / {j} 字符集过度重叠:\n"
                    f"  ch-{i+1:04d}: {events[i]}\n"
                    f"  ch-{j+1:04d}: {events[j]}\n"
                    f"  字符集重合率 {shared}/{total} = {shared/total:.2%}"
                )


# ---------------------------------------------------------------------------
# 测试套 9:_prompt module 5 子角色 marker 支持(v1.0.x wave 4 加 mini_plan_generator)
# ---------------------------------------------------------------------------


class TestPromptMarkerExtension:
    """_prompt.py 扩 5 个 outline / mini_plan role 后,``append_role_marker`` 全部接受。"""

    @pytest.mark.parametrize(
        "role",
        [
            "outline_world",
            "outline_characters",
            "outline_plot",
            "outline_volume_plans",
            "mini_plan_generator",
        ],
    )
    def test_append_role_marker_accepts_outline_roles(self, role: str) -> None:
        out = prompt_module.append_role_marker("BODY", role=role)  # type: ignore[arg-type]
        assert out.endswith(f"<!-- agent_role: {role} -->")

    @pytest.mark.parametrize(
        "role,marker",
        [
            ("outline_world", "<!-- agent_role: outline_world -->"),
            ("outline_characters", "<!-- agent_role: outline_characters -->"),
            ("outline_plot", "<!-- agent_role: outline_plot -->"),
            ("outline_volume_plans", "<!-- agent_role: outline_volume_plans -->"),
            (
                "mini_plan_generator",
                "<!-- agent_role: mini_plan_generator -->",
            ),
        ],
    )
    def test_detect_agent_role_returns_outline_role(
        self, role: str, marker: str
    ) -> None:
        messages = [Message(role="user", content=f"body\n\n{marker}")]
        detected_role, chapter_id = prompt_module.detect_agent_role(messages)
        assert detected_role == role
        assert chapter_id is None


# ---------------------------------------------------------------------------
# 模块导出 sanity check
# ---------------------------------------------------------------------------


def test_module_exports_4_stage_builders() -> None:
    """OutlineBuilder agent 模块导出 4 个 stage 函数 + 1 串行入口 +
    错误类 + FullOutline。"""
    assert hasattr(outline_builder, "build_world")
    assert hasattr(outline_builder, "build_characters")
    assert hasattr(outline_builder, "build_plot")
    assert hasattr(outline_builder, "build_volume_plans")
    assert hasattr(outline_builder, "build_outline")
    assert hasattr(outline_builder, "FullOutline")
    assert hasattr(outline_builder, "OutlineBuilderError")


class TestMiniPlanStructuralProfileM4ppp:
    """M4'''(v1.1 wave 5 s6):mini_plan 题材结构 profile(Q2 题材感知)+ 戏剧功能信号
    注入(turning_points + character_arcs)+ chapter_focus function→focus 指令。"""

    def test_structural_profile_suspense_genre(self) -> None:
        # 悬疑 / 推理 题材 → 内化的 suspense profile(加分项 + 红线齐全)。
        from src.core.agents.outline_builder import _structural_profile_for_genre

        prof = _structural_profile_for_genre("都市悬疑")
        assert "悬疑 / 推理" in prof
        assert "情感单焦" in prof  # 加分项:情感峰别稀释
        assert "backstory 单焦" in prof  # 加分项:反派前史
        assert "POV 克制" in prof  # 红线:防泄底
        # genre 关键词命中(推理 / 侦探同走 suspense profile)
        assert "悬疑 / 推理" in _structural_profile_for_genre("推理")
        assert "悬疑 / 推理" in _structural_profile_for_genre("侦探小说")

    def test_structural_profile_generic_fallback(self) -> None:
        # 未内化 profile 的题材 / 空 genre → 通用兜底(守核心钩子 + 情感峰别稀释),
        # **不含**悬疑专属红线(防止把一题材红线误套到别题材)。
        from src.core.agents.outline_builder import _structural_profile_for_genre

        prof = _structural_profile_for_genre("仙侠")
        assert "通用兜底" in prof
        assert "情感单焦" in prof  # 通用也教情感峰别被多线稀释
        assert "POV 克制" not in prof  # 悬疑红线不外溢
        # 空 genre 也走兜底,不报错
        assert "通用兜底" in _structural_profile_for_genre("")

    def test_mini_plan_message_injects_profile_signals_and_instruction(self) -> None:
        # _build_mini_plan_user_message_per_volume 注入:题材 profile + 功能信号
        # (turning_points + character_arcs)+ chapter_focus function→focus 指令。
        from src.core.agents.outline_builder import (
            _build_mini_plan_user_message_per_volume,
        )
        from src.core.models.outline import VolumeArcPlan, VolumePlanItem

        volume = VolumePlanItem(
            volume_id="v1",
            title="卷一·觉醒",
            summary="本卷起势",
            chapter_count=2,
            tension_template="heros_journey",
            arcs=[
                VolumeArcPlan(
                    arc_id="v1-a1",
                    arc_type="主线",
                    chapter_range=(1, 1),
                    phase="opening",
                    summary="开篇",
                ),
                VolumeArcPlan(
                    arc_id="v1-a2",
                    arc_type="主线",
                    chapter_range=(2, 2),
                    phase="rising",
                    summary="推进",
                ),
            ],
            tension_curve=[5, 8],
        )
        msg = _build_mini_plan_user_message_per_volume(
            world=_make_world(),
            characters=_make_characters(),
            plot=_make_plot(),
            volume=volume,
            genre="悬疑",
            chapter_id_map={("v1", 1): "ch-0001", ("v1", 2): "ch-0002"},
            prev_volumes_tail=[],
        )
        # 题材 profile 注入(Q2)
        assert "题材结构 profile" in msg
        assert "情感单焦" in msg
        assert "POV 克制" in msg
        # 功能信号注入(治"看不到 turning_point / 弧光 → 无脑双线")
        assert "major_turning_points" in msg
        assert "character_arcs" in msg
        # _make_plot 的真实 turning point / arc 内容确实进了 message
        assert "lin-yan" in msg  # character_arcs key
        # function→focus 指令 + Q1 非配额铁律在位
        assert "结构跟功能走" in msg
        assert "chapter_focus" in msg
        assert "内容驱动非配额" in msg

    def test_mini_plan_message_generic_genre_no_suspense_redline(self) -> None:
        # 非悬疑题材 → message 走通用兜底,不含悬疑专属 POV 红线(题材感知不外溢)。
        from src.core.agents.outline_builder import (
            _build_mini_plan_user_message_per_volume,
        )
        from src.core.models.outline import VolumeArcPlan, VolumePlanItem

        volume = VolumePlanItem(
            volume_id="v1",
            title="卷一",
            summary="起",
            chapter_count=2,
            tension_template="heros_journey",
            arcs=[
                VolumeArcPlan(
                    arc_id="v1-a1",
                    arc_type="主线",
                    chapter_range=(1, 1),
                    phase="opening",
                    summary="开篇",
                ),
                VolumeArcPlan(
                    arc_id="v1-a2",
                    arc_type="主线",
                    chapter_range=(2, 2),
                    phase="rising",
                    summary="推进",
                ),
            ],
            tension_curve=[5, 8],
        )
        msg = _build_mini_plan_user_message_per_volume(
            world=_make_world(),
            characters=_make_characters(),
            plot=_make_plot(),
            volume=volume,
            genre="仙侠",
            chapter_id_map={("v1", 1): "ch-0001", ("v1", 2): "ch-0002"},
            prev_volumes_tail=[],
        )
        assert "通用兜底" in msg
        assert "POV 克制" not in msg  # 悬疑红线不外溢到仙侠
        # function→focus 通用机制仍在(题材无关)
        assert "结构跟功能走" in msg


class TestMiniPlanArcBeatsM5:
    """M5(v1.1 wave 5 s7):mini_plan user message 注入逐章弧光台阶(arc_beats)指令
    + character_arcs 复用作 arc_beats 信号源(题材无关,动作演出非旁白声明)。"""

    def _make_volume(self):
        from src.core.models.outline import VolumeArcPlan, VolumePlanItem

        return VolumePlanItem(
            volume_id="v1",
            title="卷一·觉醒",
            summary="本卷起势",
            chapter_count=2,
            tension_template="heros_journey",
            arcs=[
                VolumeArcPlan(
                    arc_id="v1-a1",
                    arc_type="主线",
                    chapter_range=(1, 1),
                    phase="opening",
                    summary="开篇",
                ),
                VolumeArcPlan(
                    arc_id="v1-a2",
                    arc_type="主线",
                    chapter_range=(2, 2),
                    phase="rising",
                    summary="推进",
                ),
            ],
            tension_curve=[5, 8],
        )

    def test_mini_plan_message_injects_arc_beats_instruction(self) -> None:
        # _build_mini_plan_user_message_per_volume 注入 M5 arc_beats 指令 +
        # 动作演出非旁白命门 + 内容驱动非配额 + 配角/反派优先。
        from src.core.agents.outline_builder import (
            _build_mini_plan_user_message_per_volume,
        )

        msg = _build_mini_plan_user_message_per_volume(
            world=_make_world(),
            characters=_make_characters(),
            plot=_make_plot(),
            volume=self._make_volume(),
            genre="悬疑",
            chapter_id_map={("v1", 1): "ch-0001", ("v1", 2): "ch-0002"},
            prev_volumes_tail=[],
        )
        # M5 arc_beats 指令在位
        assert "M5 角色弧光逐章落地" in msg
        assert "arc_beats" in msg
        # 命门:动作演出非旁白声明(带 ✅/❌ 对照)
        assert "动作演出非旁白声明" in msg
        # 内容驱动非配额 + 配角/反派优先
        assert "内容驱动非配额" in msg
        assert "配角/反派优先" in msg
        # character_arcs 标签已注明同时供 M5 用(arc_beats 信号源)
        assert "arc_beats" in msg and "character_arcs" in msg
        # _make_plot 真实弧光 key 进 message(arc_beats 判定素材)
        assert "lin-yan" in msg

    def test_mini_plan_message_arc_beats_genre_agnostic(self) -> None:
        # 弧光"动作演出"题材无关:非悬疑题材 message 同样含 M5 指令。
        from src.core.agents.outline_builder import (
            _build_mini_plan_user_message_per_volume,
        )

        msg = _build_mini_plan_user_message_per_volume(
            world=_make_world(),
            characters=_make_characters(),
            plot=_make_plot(),
            volume=self._make_volume(),
            genre="仙侠",
            chapter_id_map={("v1", 1): "ch-0001", ("v1", 2): "ch-0002"},
            prev_volumes_tail=[],
        )
        assert "M5 角色弧光逐章落地" in msg
        assert "动作演出非旁白声明" in msg


class TestMiniPlanInteractionFocusAxisA:
    """M4''-轴A(v1.1 wave 5 s8):mini_plan 题材 profile 加治愈日常(松 beat)分支
    + interaction_focus 互动驱动指令注入(治"独处内省扎堆")。守题材感知不外溢
    (悬疑 profile 不含互动驱动 / 治愈 profile 不含 POV 克制)。"""

    def test_structural_profile_healing_genre(self) -> None:
        # 治愈日常 / 慢生活 → 内化的 healing profile(松 beat 互动驱动 + 红线齐全)。
        from src.core.agents.outline_builder import _structural_profile_for_genre

        prof = _structural_profile_for_genre("治愈日常")
        assert "治愈日常 / 慢生活" in prof
        assert "互动驱动" in prof  # 松 beat 引擎类型
        assert "interaction_focus" in prof  # 启用约束 11
        assert "本书启用约束 11" in prof
        assert "护治愈基调" in prof  # 红线:别尖锐冲突
        assert "护留白" in prof  # 红线:别消灭留白
        # 悬疑红线不外溢到治愈日常
        assert "POV 克制" not in prof

    def test_structural_profile_healing_keyword_variants(self) -> None:
        # 关键词命中:慢生活 / 温情 / 田园 / 疗愈 同走 healing profile。
        from src.core.agents.outline_builder import _structural_profile_for_genre

        assert "治愈日常 / 慢生活" in _structural_profile_for_genre("慢生活")
        assert "治愈日常 / 慢生活" in _structural_profile_for_genre("田园温情")
        assert "治愈日常 / 慢生活" in _structural_profile_for_genre("疗愈系")

    def test_structural_profile_suspense_no_interaction_leak(self) -> None:
        # 反向:悬疑 profile **不含**松 beat 互动驱动(题材感知不外溢,避免 s8 误推悬疑)。
        from src.core.agents.outline_builder import _structural_profile_for_genre

        prof = _structural_profile_for_genre("都市悬疑")
        assert "互动驱动" not in prof
        assert "interaction_focus" not in prof
        # 通用兜底也不含松 beat 互动驱动
        assert "互动驱动" not in _structural_profile_for_genre("仙侠")

    def test_mini_plan_message_healing_injects_interaction_instruction(self) -> None:
        # genre=治愈日常 → message 注入 healing profile + 约束 11 互动驱动指令 + interaction_focus。
        from src.core.agents.outline_builder import (
            _build_mini_plan_user_message_per_volume,
        )
        from src.core.models.outline import VolumeArcPlan, VolumePlanItem

        volume = VolumePlanItem(
            volume_id="v1",
            title="卷一·归镇",
            summary="本卷起势",
            chapter_count=2,
            tension_template="steady_daily",
            arcs=[
                VolumeArcPlan(
                    arc_id="v1-a1",
                    arc_type="主线",
                    chapter_range=(1, 1),
                    phase="opening",
                    summary="开篇",
                ),
                VolumeArcPlan(
                    arc_id="v1-a2",
                    arc_type="主线",
                    chapter_range=(2, 2),
                    phase="rising",
                    summary="推进",
                ),
            ],
            tension_curve=[4, 5],
        )
        msg = _build_mini_plan_user_message_per_volume(
            world=_make_world(),
            characters=_make_characters(),
            plot=_make_plot(),
            volume=volume,
            genre="治愈日常",
            chapter_id_map={("v1", 1): "ch-0001", ("v1", 2): "ch-0002"},
            prev_volumes_tail=[],
        )
        # 题材 profile(松 beat 互动驱动)注入
        assert "治愈日常 / 慢生活" in msg
        assert "本书启用约束 11" in msg
        # 约束 11 互动驱动指令(静态)在位
        assert "M4''-轴A 互动驱动" in msg
        assert "interaction_focus" in msg
        assert "默认走向真实互动" in msg


class TestBreakthroughPayoffMiniPlanS10:
    """模块①(v1.1 wave 5 s10):mini_plan 约束 12 升级可感化 gated 注入防回退。

    gated 条件块:breakthrough_payoff_enabled=True(3 题材+开关 ON)才追加约束 12;
    False(默认 / 非 3 题材 / OFF)→ message 逐字节不含约束 12(零回归铁律)。"""

    def _volume(self):
        from src.core.models.outline import VolumePlanItem, VolumeArcPlan
        return VolumePlanItem(
            volume_id="v1",
            title="卷一·崛起",
            summary="废柴逆袭",
            chapter_count=2,
            tension_template="strong_stimulation",
            arcs=[
                VolumeArcPlan(
                    arc_id="v1-a1", arc_type="主线", chapter_range=(1, 1),
                    phase="opening", summary="蛰伏",
                ),
                VolumeArcPlan(
                    arc_id="v1-a2", arc_type="主线", chapter_range=(2, 2),
                    phase="rising", summary="突破",
                ),
            ],
            tension_curve=[7, 9],
        )

    def _msg(self, enabled: bool) -> str:
        from src.core.agents.outline_builder import (
            _build_mini_plan_user_message_per_volume,
        )
        return _build_mini_plan_user_message_per_volume(
            world=_make_world(),
            characters=_make_characters(),
            plot=_make_plot(),
            volume=self._volume(),
            genre="玄幻修真升级",
            chapter_id_map={("v1", 1): "ch-0001", ("v1", 2): "ch-0002"},
            prev_volumes_tail=[],
            breakthrough_payoff_enabled=enabled,
        )

    def test_constraint_12_injected_when_enabled(self) -> None:
        msg = self._msg(enabled=True)
        assert "升级可感化" in msg
        assert "breakthrough_payoff" in msg
        assert "判本章是不是境界突破/晋级章" in msg
        assert "内容驱动非配额" in msg  # 防注水

    def test_constraint_12_absent_when_disabled(self) -> None:
        # 零回归铁律:OFF → message 不含约束 12 任何字面。
        msg = self._msg(enabled=False)
        assert "升级可感化" not in msg
        assert "breakthrough_payoff" not in msg

    def test_off_message_byte_identical_to_no_param(self) -> None:
        # 逐字节零回归:enabled=False 与不传参(默认 False)产物完全一致。
        from src.core.agents.outline_builder import (
            _build_mini_plan_user_message_per_volume,
        )
        base = _build_mini_plan_user_message_per_volume(
            world=_make_world(), characters=_make_characters(), plot=_make_plot(),
            volume=self._volume(), genre="玄幻修真升级",
            chapter_id_map={("v1", 1): "ch-0001", ("v1", 2): "ch-0002"},
            prev_volumes_tail=[],
        )
        assert base == self._msg(enabled=False)


class TestSeamAntiReplayPrevTail:
    """卷界双极化(v1.1 acceptance 修复1,2026-06-10):滚动上下文渲染防回退。

    luo-accept-v1 ch-0007 现场:prev_tail 只渲染 one_line_event(悬念式措辞
    "险些掐死流浪汉",结局只在 arc_beats)+ 指令只防"重启"不防"重演" → 新卷
    LLM 把"解决前卷悬念"当开篇职责重演 → 与已写正文重叠 → must 永不可达成。
    本类锁定:① arc_beats"已完结的收束"进渲染 ② 反重演指令在位。
    """

    @staticmethod
    def _v2_volume_and_msg(prev_tail: list) -> str:
        from src.core.agents.outline_builder import (
            _build_mini_plan_user_message_per_volume,
        )
        from src.core.models.outline import VolumeArcPlan, VolumePlanItem

        volume = VolumePlanItem(
            volume_id="v2",
            title="卷二·失控",
            summary="侵蚀蔓延",
            chapter_count=2,
            tension_template="heros_journey",
            arcs=[
                VolumeArcPlan(
                    arc_id="v2-a1",
                    arc_type="主线",
                    chapter_range=(1, 1),
                    phase="rising",
                    summary="现实失控",
                ),
                VolumeArcPlan(
                    arc_id="v2-a2",
                    arc_type="主线",
                    chapter_range=(2, 2),
                    phase="rising",
                    summary="盘问交锋",
                ),
            ],
            tension_curve=[6, 7],
        )
        return _build_mini_plan_user_message_per_volume(
            world=_make_world(),
            characters=_make_characters(),
            plot=_make_plot(),
            volume=volume,
            genre="悬疑",
            chapter_id_map={("v2", 1): "ch-0007", ("v2", 2): "ch-0008"},
            prev_volumes_tail=prev_tail,
        )

    def test_prev_tail_renders_arc_beats_resolution_and_anti_replay(self) -> None:
        """ch-0006 真实字面重演:收束行 + 双极指令必须都进 message。"""
        from src.core.models.outline import MiniPlanItem

        tail_item = MiniPlanItem(
            chapter_id="ch-0006",
            chapter_number=6,
            volume_id="v1",
            arc_id="v1-arc-2-rising",
            one_line_event=(
                "沈舟实地勘察老街废弃防空洞,突遭锦衣卫人格轻微侵蚀,"
                "用审讯手法险些掐死一名无辜流浪汉。"
            ),
            info_increment="首次身份侵蚀失控让沈舟面临现实伤人风险。",
            linked_characters=["shen-zhou"],
            arc_beats={
                "shen-zhou": (
                    "在防空洞内死死掰开自己掐住流浪汉脖子的手指,"
                    "用随身携带的镇纸砸破额头换取清醒。"
                )
            },
        )
        msg = self._v2_volume_and_msg([tail_item])
        # ① 收束信息进渲染(下一卷 LLM 必须看到"悬念已在前卷内解决")
        assert "本章内已完结的收束" in msg
        assert "掰开自己掐住流浪汉脖子的手指" in msg
        # ② 反重演指令双极在位
        assert "不得在本卷重演、复述或重新解决" in msg
        assert "后果与新事态" in msg
        # 原"防重启"极不丢
        assert "不应「重启」主线" in msg

    def test_prev_tail_without_arc_beats_keeps_instruction(self) -> None:
        """无 arc_beats 的末章:不渲染收束行,但反重演指令仍在位。"""
        from src.core.models.outline import MiniPlanItem

        tail_item = MiniPlanItem(
            chapter_id="ch-0006",
            chapter_number=6,
            volume_id="v1",
            arc_id="v1-arc-2-rising",
            one_line_event="沈舟勘察防空洞确认双时空地理重合。",
            info_increment="地理重叠物理证实。",
            linked_characters=["shen-zhou"],
        )
        msg = self._v2_volume_and_msg([tail_item])
        assert "本章内已完结的收束" not in msg
        assert "不得在本卷重演、复述或重新解决" in msg


# ---------------------------------------------------------------------------
# 测试套:W3.5-s3 长度感知拆章(卷规划层密度参考注入 + 零回归)
# ---------------------------------------------------------------------------


def _action_calibration() -> GenreCalibration:
    """deepseek-v4-pro action 首基线(config.yaml,s2 实测):
    chars_per_beat 1788 / chars_per_chapter 5364 / 守闸 0.333 / 单元 3。"""
    return GenreCalibration(
        chars_per_beat=1788,
        chars_per_chapter=5364,
        length_compliance=0.333,
        content_units_per_chapter=3.0,
        sample_chapters=6,
    )


def _make_volume_plan_item(
    volume_id: str = "v1", chapter_count: int = 10
) -> VolumePlanItem:
    """合规单卷骨架(2 arc / 各 2 beat / tension 长度 == chapter_count),供细化测试。

    arc-2 标 '副本'(下游 ≥3 章 gate)+ climax,作"重头戏"靶子;chapter_count >= 8
    才保证 arc-2 段长 >= 3(本 helper 默认 10)。
    """
    half = chapter_count // 2
    return VolumePlanItem.model_validate(
        {
            "volume_id": volume_id,
            "title": f"{volume_id} 卷",
            "summary": "本卷在主线中的位置概述,占位满足 min_length。",
            "chapter_count": chapter_count,
            "tension_template": "heros_journey",
            "linked_phases": ["phase-1"],
            "linked_turning_points": ["turn-1"],
            "linked_threads": ["thread-1"],
            "arcs": [
                {
                    "arc_id": f"{volume_id}-arc-1",
                    "arc_type": "主线",
                    "chapter_range": [1, half],
                    "phase": "opening",
                    "summary": "开端弧概要",
                    "beats": [
                        {
                            "beat_id": f"{volume_id}-arc-1-beat-1",
                            "chapter_range": [1, max(1, half - 1)],
                            "summary": "开端 beat",
                            "must_advance": ["主角登场", "获得契机"],
                            "forbidden_advance": [
                                "携带残片返回地球",
                                "确立双时空穿梭",
                            ],
                        },
                        {
                            "beat_id": f"{volume_id}-arc-1-beat-2",
                            "chapter_range": [max(2, half), half],
                            "summary": "开端收束 beat",
                            "must_advance": ["立下目标"],
                            "forbidden_advance": ["终极反派现身"],
                        },
                    ],
                },
                {
                    "arc_id": f"{volume_id}-arc-2",
                    "arc_type": "副本",
                    "chapter_range": [half + 1, chapter_count],
                    "phase": "climax",
                    "summary": "高潮副本弧(重头戏)",
                    "beats": [
                        {
                            "beat_id": f"{volume_id}-arc-2-beat-1",
                            "chapter_range": [half + 1, chapter_count - 1],
                            "summary": "高潮推进 beat",
                            "must_advance": ["正面对决"],
                            "forbidden_advance": ["卷二核心剧情"],
                        },
                        {
                            "beat_id": f"{volume_id}-arc-2-beat-2",
                            "chapter_range": [chapter_count, chapter_count],
                            "summary": "终局 beat",
                            "must_advance": ["卷终局"],
                            "forbidden_advance": ["卷三核心剧情"],
                        },
                    ],
                },
            ],
            "tension_curve": [3 + (i % 7) for i in range(chapter_count)],
        }
    )


def _make_bad_arc_span_volume_plans() -> VolumePlansOutput:
    """副本 arc 仅 2 章,用于复现 outline:commit arc_too_short 阻断。"""
    return VolumePlansOutput.model_validate(
        {
            "volumes": [
                {
                    "volume_id": "v1",
                    "title": "v1 卷",
                    "summary": "本卷在主线中的位置概述,占位满足 min_length。",
                    "chapter_count": 7,
                    "tension_template": "heros_journey",
                    "linked_phases": ["phase-1"],
                    "linked_turning_points": ["turn-1"],
                    "linked_threads": ["thread-1"],
                    "arcs": [
                        {
                            "arc_id": "v1-arc-1",
                            "arc_type": "主线",
                            "chapter_range": [1, 2],
                            "phase": "opening",
                            "summary": "开端弧概要",
                            "beats": [
                                {
                                    "beat_id": "v1-arc-1-beat-1",
                                    "chapter_range": [1, 2],
                                    "summary": "开端 beat",
                                    "must_advance": ["主角登场"],
                                    "forbidden_advance": ["终局提前"],
                                }
                            ],
                        },
                        {
                            "arc_id": "v1-arc-2",
                            "arc_type": "主线",
                            "chapter_range": [3, 5],
                            "phase": "rising",
                            "summary": "推进弧概要",
                            "beats": [
                                {
                                    "beat_id": "v1-arc-2-beat-1",
                                    "chapter_range": [3, 5],
                                    "summary": "推进 beat",
                                    "must_advance": ["危机升级"],
                                    "forbidden_advance": ["终局提前"],
                                }
                            ],
                        },
                        {
                            "arc_id": "v1-arc-3",
                            "arc_type": "副本",
                            "chapter_range": [6, 7],
                            "phase": "climax",
                            "summary": "高潮副本弧但跨度不足",
                            "beats": [
                                {
                                    "beat_id": "v1-arc-3-beat-1",
                                    "chapter_range": [6, 7],
                                    "summary": "高潮 beat",
                                    "must_advance": ["正面对决"],
                                    "forbidden_advance": ["卷二核心剧情"],
                                }
                            ],
                        },
                    ],
                    "tension_curve": [3, 4, 5, 6, 7, 8, 6],
                }
            ]
        }
    )


# 细化后 v1(10→14 章):arc-2 副本高潮加宽 [7,14] + 宽 beat;保留 volume_id/arc_id/
# forbidden_advance 字面。供 _FakeRefineClient 返回。
_REFINED_V1_14 = json.dumps(
    {
        "volumes": [
            {
                "volume_id": "v1",
                "title": "v1 卷",
                "summary": "本卷在主线中的位置概述,占位满足 min_length。",
                "chapter_count": 14,
                "tension_template": "heros_journey",
                "linked_phases": ["phase-1"],
                "linked_turning_points": ["turn-1"],
                "linked_threads": ["thread-1"],
                "arcs": [
                    {
                        "arc_id": "v1-arc-1",
                        "arc_type": "主线",
                        "chapter_range": [1, 6],
                        "phase": "opening",
                        "summary": "开端弧概要",
                        "beats": [
                            {
                                "beat_id": "v1-arc-1-beat-1",
                                "chapter_range": [1, 4],
                                "summary": "开端 beat",
                                "must_advance": ["主角登场", "获得契机"],
                                "forbidden_advance": [
                                    "携带残片返回地球",
                                    "确立双时空穿梭",
                                ],
                            },
                            {
                                "beat_id": "v1-arc-1-beat-2",
                                "chapter_range": [5, 6],
                                "summary": "开端收束 beat",
                                "must_advance": ["立下目标"],
                                "forbidden_advance": ["终极反派现身"],
                            },
                        ],
                    },
                    {
                        "arc_id": "v1-arc-2",
                        "arc_type": "副本",
                        "chapter_range": [7, 14],
                        "phase": "climax",
                        "summary": "高潮副本弧(重头戏)",
                        "beats": [
                            {
                                "beat_id": "v1-arc-2-beat-1",
                                "chapter_range": [7, 11],
                                "summary": "高潮推进 beat",
                                "must_advance": ["正面对决"],
                                "forbidden_advance": ["卷二核心剧情"],
                            },
                            {
                                "beat_id": "v1-arc-2-beat-2",
                                "chapter_range": [12, 14],
                                "summary": "终局 beat",
                                "must_advance": ["卷终局"],
                                "forbidden_advance": ["卷三核心剧情"],
                            },
                        ],
                    },
                ],
                "tension_curve": [3, 4, 4, 5, 5, 6, 7, 7, 8, 8, 9, 9, 9, 6],
            }
        ]
    },
    ensure_ascii=False,
)


class _FakeRefineClient(LLMClient):
    """细化 e2e:complete 按 user message 里的 volume_id 返对应"细化后单卷"JSON。"""

    def __init__(self, response_by_volume: dict[str, str]) -> None:
        self._by_vol = response_by_volume
        self.calls = 0

    def complete(self, *, messages, model, **kwargs):  # type: ignore[override]
        self.calls += 1
        all_text = " ".join(m.content for m in messages)
        for vid, js in self._by_vol.items():
            if f"volume_id:{vid}" in all_text:
                return LLMResponse(
                    content=js,
                    model_used=model or "fake-refine",
                    usage=TokenUsage(input_tokens=10, output_tokens=10),
                )
        raise AssertionError("_FakeRefineClient:无匹配 volume_id 的 fake 响应")


class _QueuedRefineClient(LLMClient):
    """按 volume_id 队列返回响应,用于验证单卷 retry 不影响健康卷。"""

    def __init__(self, responses_by_volume: dict[str, list[str]]) -> None:
        self._by_vol = {
            volume_id: list(responses)
            for volume_id, responses in responses_by_volume.items()
        }
        self.calls_by_volume: list[str] = []

    def complete(self, *, messages, model, **kwargs):  # type: ignore[override]
        all_text = " ".join(m.content for m in messages)
        for vid, responses in self._by_vol.items():
            if f"volume_id:{vid}" not in all_text:
                continue
            if not responses:
                raise AssertionError(f"_QueuedRefineClient:{vid} 响应已耗尽")
            self.calls_by_volume.append(vid)
            return LLMResponse(
                content=responses.pop(0),
                model_used=model or "fake-refine",
                usage=TokenUsage(input_tokens=10, output_tokens=10),
            )
        raise AssertionError("_QueuedRefineClient:无匹配 volume_id 的 fake 响应")


class TestLengthSectionHelper:
    """W3.5-s3b:``_build_length_section`` 归一化密度段(s3a 注入逻辑抽出共用)。"""

    def test_length_section_contains_normalized_density(self) -> None:
        text = _build_length_section(_action_calibration(), 4500)
        assert "长度感知参考" in text
        assert "1788" in text  # 归一化 chars_per_beat(拆章后稳定)
        assert "2.5" in text  # 4500/1788 ≈ 2.5 单元/章到上限
        assert "4500" in text
        assert "5364" in text  # 未拆章对照(超闸)
        # 主导是戏剧分量,不是字数公式(防退化成"按字数算章数")
        assert "戏剧分量" in text


class TestVolumePlansSkeletonClean:
    """W3.5-s3b 零回归:全书骨架 ``_build_volume_plans_user_message`` 永不注入密度、
    永远「chapter_count ≈ N」钉子(长度感知拆章已移到卷内细化层)。"""

    def test_skeleton_message_never_injects_density(self) -> None:
        text = _build_volume_plans_user_message(
            premise=_PREMISE,
            genre="都市悬疑",
            world=_make_world(),
            characters=_make_characters(),
            plot=_make_plot(),
            total_volumes=3,
            chapters_per_volume=15,
            tension_template="heros_journey",
        )
        assert "长度感知参考" not in text
        assert _PREMISE in text
        assert "tension_curve 长度严格 == chapter_count" in text
        # 骨架保留「chapter_count ≈ N」钉子(≈N 干净,拆章交给细化层)
        assert "chapter_count ≈" in text

    def test_skeleton_message_injects_consistency_retry_feedback(self) -> None:
        text = _build_volume_plans_user_message(
            premise=_PREMISE,
            genre="都市悬疑",
            world=_make_world(),
            characters=_make_characters(),
            plot=_make_plot(),
            total_volumes=2,
            chapters_per_volume=6,
            tension_template="heros_journey",
            consistency_retry_feedback="终局地点与 plot 冲突:卷规划写古墓,plot 写博物馆地下。",
        )
        assert "上次大纲跨段一致性校验失败" in text
        assert "终局地点与 plot 冲突" in text
        assert "本次只能重出 volume_plans" in text
        assert "world / characters / plot 是上游事实" in text


class TestDensityAnchoredTarget:
    """W3.5-s3b 过度拆分修复核心:``_density_anchored_target_chapters`` 把骨架卷章数
    按演出密度折算成细化目标章数(deepseek action ≈ ×1.2),止住多卷过度拆分翻倍。"""

    def test_action_genre_anchors_to_1_2x(self) -> None:
        """deepseek action(units_cap = 4500/1788 ≈ 2.5,cupc 3.0)→ ×1.2 系数。"""
        cal = _action_calibration()
        # 6 章卷 → round(6 × 3.0 / 2.517) = 7(交接期望:小卷 6→7,不再翻倍到 10-20)
        assert _density_anchored_target_chapters(6, cal, 4500) == 7
        # 30 章单卷 → round(30 × 3.0 / 2.517) = 36(交接期望:单卷 30→36)
        assert _density_anchored_target_chapters(30, cal, 4500) == 36
        # 10 章卷 → round(10 × 3.0 / 2.517) = 12
        assert _density_anchored_target_chapters(10, cal, 4500) == 12

    def test_genre_adaptive_introspection_barely_splits(self) -> None:
        """introspection 题材 chars_per_beat 低(1534)→ units_cap 高 → 系数 ≈1.0,
        几乎不拆(deepseek introspection 写得短,无需多分章)。公式自适应、非硬编码 1.2。"""
        introspection = GenreCalibration(
            chars_per_beat=1534,
            chars_per_chapter=4602,
            length_compliance=0.5,
            content_units_per_chapter=3.0,
            sample_chapters=6,
        )
        # units_cap = 4500/1534 ≈ 2.93,factor = 3.0/2.93 ≈ 1.02 → round(6×1.02)=6
        assert _density_anchored_target_chapters(6, introspection, 4500) == 6

    def test_clamps_to_skeleton_never_shrinks(self) -> None:
        """若某模型写得短到 units_cap ≥ 实测密度(factor < 1)→ 目标钳到 skeleton_cc,
        细化只增不减(不缩卷,保骨架结构最小章数)。"""
        short_writer = GenreCalibration(
            chars_per_beat=1000,  # units_cap = 4500/1000 = 4.5 > cupc 3.0 → factor 0.67
            chars_per_chapter=3000,
            length_compliance=1.0,
            content_units_per_chapter=3.0,
            sample_chapters=6,
        )
        # round(10 × 3.0/4.5) = round(6.67) = 7 < 10 → 钳到 10
        assert _density_anchored_target_chapters(10, short_writer, 4500) == 10


class TestVolumeRefineUserMessage:
    """W3.5-s3b:卷内细化 user message —— 输入单卷结构 + 密度参考 + 细化框架。"""

    def test_refine_message_injects_density_and_structure(self) -> None:
        vol = _make_volume_plan_item("v1", 10)
        text = _build_volume_refine_user_message(
            volume=vol,
            genre="都市悬疑",
            length_calibration=_action_calibration(),
            target_word_count_max=4500,
        )
        # 密度参考(复用 _build_length_section)
        assert "长度感知参考" in text
        assert "1788" in text
        # 输入卷结构 + 身份 + 防剧透字面原样带入(保留骨架)
        assert "volume_id:v1" in text
        assert "v1-arc-2" in text
        assert "携带残片返回地球" in text  # forbidden_advance 字面保留
        # 细化框架:恰好 1 个卷 + 重头戏多分章 + 少而宽 beat
        assert "恰好 1 个卷" in text
        assert "少而宽 beat" in text
        assert "原 chapter_count:10" in text
        # 密度锚定目标章数(s3b 修复):正面给 N = round(10 × 3.0 / (4500/1788)) = 12
        assert "密度锚定目标章数" in text
        assert "瞄准约 12 章" in text

    def test_refine_message_density_anchor_is_positive_no_reverse_numbers(self) -> None:
        """防 guard 覆辙:密度锚定只正面给目标 N,绝不出现"别超过 / 翻倍回退"类反面数字
        措辞(deepseek 会锚定反面数字 → s3b guard 尝试就栽在这)。"""
        vol = _make_volume_plan_item("v1", 6)
        text = _build_volume_refine_user_message(
            volume=vol,
            genre="修仙",
            length_calibration=_action_calibration(),
            target_word_count_max=4500,
        )
        # 6 章卷 → round(6 × 3.0 / 2.517) = 7(止住翻倍)
        assert "瞄准约 7 章" in text
        # 绝无反面数字护栏措辞
        for forbidden in ("别超过", "不要超过", "若翻倍", "翻倍就", "上限不得"):
            assert forbidden not in text, f"密度锚定段不应含反面措辞 {forbidden!r}"

    def test_refine_message_injects_consistency_retry_feedback(self) -> None:
        vol = _make_volume_plan_item("v2", 6)
        text = _build_volume_refine_user_message(
            volume=vol,
            genre="都市悬疑",
            length_calibration=_action_calibration(),
            target_word_count_max=4500,
            consistency_retry_feedback="傅韵行动与 plot 冲突:卷规划写牺牲,plot 写存活。",
        )
        assert "上次大纲跨段一致性校验失败" in text
        assert "傅韵行动与 plot 冲突" in text
        assert "本次只细化当前单卷" in text


class TestRefineVolumePlansLengthAware:
    """W3.5-s3b:``refine_volume_plans_length_aware`` 逐卷聚焦细化 + 身份保留。"""

    def test_refine_grows_chapter_count_preserves_identity(
        self, fake_prompts_dir: Path
    ) -> None:
        skeleton = VolumePlansOutput(volumes=[_make_volume_plan_item("v1", 10)])
        client = _FakeRefineClient({"v1": _REFINED_V1_14})
        refined = refine_volume_plans_length_aware(
            volume_plans=skeleton,
            length_calibration=_action_calibration(),
            target_word_count_max=4500,
            genre="都市悬疑",
            client=client,
            model="fake-refine",
            prompts_dir=fake_prompts_dir,
        )
        assert client.calls == 1  # 单卷 → 1 次 LLM
        assert len(refined.volumes) == 1
        r = refined.volumes[0]
        assert r.volume_id == "v1"  # 身份保留
        assert r.chapter_count == 14  # 内容驱动多出(10→14)
        # 重头戏 arc-2(副本高潮)加宽:段长 8 章(> 原 5)
        arc2 = next(a for a in r.arcs if a.arc_id == "v1-arc-2")
        assert arc2.chapter_range[1] - arc2.chapter_range[0] + 1 == 8

    def test_refine_loops_each_volume(self, fake_prompts_dir: Path) -> None:
        """多卷 → 每卷各 1 次细化调用(聚焦单卷)。"""
        refined_v2 = _REFINED_V1_14.replace('"v1', '"v2')
        skeleton = VolumePlansOutput(
            volumes=[
                _make_volume_plan_item("v1", 10),
                _make_volume_plan_item("v2", 10),
            ]
        )
        client = _FakeRefineClient({"v1": _REFINED_V1_14, "v2": refined_v2})
        refined = refine_volume_plans_length_aware(
            volume_plans=skeleton,
            length_calibration=_action_calibration(),
            target_word_count_max=4500,
            client=client,
            model="fake-refine",
            prompts_dir=fake_prompts_dir,
        )
        assert client.calls == 2  # 两卷各一次
        assert [v.volume_id for v in refined.volumes] == ["v1", "v2"]
        assert all(v.chapter_count == 14 for v in refined.volumes)

    def test_refine_rejects_volume_id_mismatch(
        self, fake_prompts_dir: Path
    ) -> None:
        """细化输出 volume_id 与输入不符 → retry 耗尽抛 OutlineBuilderError。"""
        skeleton = VolumePlansOutput(volumes=[_make_volume_plan_item("vX", 10)])
        # fake 永远返 v1(与输入 vX 不符)→ post_validate 拒 → retry 耗尽
        client = _FakeRefineClient({"vX": _REFINED_V1_14})
        with pytest.raises(OutlineBuilderError):
            refine_volume_plans_length_aware(
                volume_plans=skeleton,
                length_calibration=_action_calibration(),
                target_word_count_max=4500,
                client=client,
                model="fake-refine",
                prompts_dir=fake_prompts_dir,
            )

    def test_refine_retries_only_failed_volume_on_contract_regression(
        self, fake_prompts_dir: Path
    ) -> None:
        """v2 首轮破坏 beat 身份 → 只重试 v2;v1 不重生。"""
        refined_v2 = _REFINED_V1_14.replace('"v1', '"v2')
        bad_v2 = refined_v2.replace("v2-arc-2-beat-2", "v2-arc-2-beat-X", 1)
        skeleton = VolumePlansOutput(
            volumes=[
                _make_volume_plan_item("v1", 10),
                _make_volume_plan_item("v2", 10),
            ]
        )
        client = _QueuedRefineClient(
            {
                "v1": [_REFINED_V1_14],
                "v2": [bad_v2, refined_v2],
            }
        )

        refined = refine_volume_plans_length_aware(
            volume_plans=skeleton,
            length_calibration=_action_calibration(),
            target_word_count_max=4500,
            client=client,
            model="fake-refine",
            prompts_dir=fake_prompts_dir,
        )

        assert client.calls_by_volume == ["v1", "v2", "v2"]
        assert [v.volume_id for v in refined.volumes] == ["v1", "v2"]

    def test_refine_retries_when_story_semantics_change(
        self, fake_prompts_dir: Path
    ) -> None:
        """卷内细化只能重排章数；改写卷概要会被确定性拒绝后局部重试。"""
        bad = _REFINED_V1_14.replace(
            "本卷在主线中的位置概述,占位满足 min_length。",
            "地宫的抹除规则随之失效。",
            1,
        )
        skeleton = VolumePlansOutput(volumes=[_make_volume_plan_item("v1", 10)])
        client = _QueuedRefineClient({"v1": [bad, _REFINED_V1_14]})

        refined = refine_volume_plans_length_aware(
            volume_plans=skeleton,
            length_calibration=_action_calibration(),
            target_word_count_max=4500,
            client=client,
            model="fake-refine",
            prompts_dir=fake_prompts_dir,
        )

        assert client.calls_by_volume == ["v1", "v1"]
        assert refined.volumes[0].summary == skeleton.volumes[0].summary


class TestChapterIdRenumberingAfterGrowth:
    """W3.5-s3b 下游自洽核心:卷内细化改 chapter_count → 全书 ch-NNNN 章号顺延。"""

    def test_derive_chapter_ids_renumbers_after_growth(self) -> None:
        # 中间卷 v2 细化后 10→14,后续卷 v3 章号应顺延(不是按原 10)
        vp = VolumePlansOutput(
            volumes=[
                _make_volume_plan_item("v1", 10),
                _make_volume_plan_item("v2", 14),
                _make_volume_plan_item("v3", 10),
            ]
        )
        mapping = _derive_default_chapter_ids(vp)
        assert mapping[("v1", 1)] == "ch-0001"
        assert mapping[("v1", 10)] == "ch-0010"
        # v2 顺延从 11 起,14 章 → ch-0011..ch-0024
        assert mapping[("v2", 1)] == "ch-0011"
        assert mapping[("v2", 14)] == "ch-0024"
        # v3 顺延从 25 起(跨过 v2 的 14 章,不是 10)
        assert mapping[("v3", 1)] == "ch-0025"
        assert mapping[("v3", 10)] == "ch-0034"
        assert len(mapping) == 34  # 全书总章数 = 10+14+10


class TestBuildOutlineRefineWiring:
    """W3.5-s3b:build_outline 在 volume_plans 后接卷内细化 + 重持久化(下游自洽)。"""

    def test_build_outline_calls_refine_and_repersists_when_calibrated(
        self, fake_prompts_dir: Path, mock_client: MockLLMClient
    ) -> None:
        from unittest.mock import patch

        stages: list[str] = []
        cal = _action_calibration()
        # 细化 spy:返回输入 volume_plans(identity),让 mini_plan 正常走 mock fixture
        with patch(
            "src.core.agents.outline_builder.refine_volume_plans_length_aware",
            side_effect=lambda *, volume_plans, **kw: volume_plans,
        ) as spy:
            build_outline(
                premise=_PREMISE,
                total_volumes=2,
                chapters_per_volume=15,
                tension_template="heros_journey",
                genre="都市悬疑",
                length_calibration=cal,
                target_word_count_max=4500,
                client=mock_client,
                model="mock-default",
                prompts_dir=fake_prompts_dir,
                on_stage_complete=lambda s, o: stages.append(s),
            )
        assert spy.call_count == 1  # 校准齐备 → 细化触发
        kwargs = spy.call_args.kwargs
        assert kwargs["length_calibration"] is cal
        assert kwargs["target_word_count_max"] == 4500
        # 卷规划先在内存中细化并通过审核，再一次性落盘。
        assert stages.count("volume_plans") == 1
        assert stages == [
            "world",
            "characters",
            "plot",
            "volume_plans",
            "mini_plan",
        ]

    def test_build_outline_skips_refine_when_no_calibration(
        self, fake_prompts_dir: Path, mock_client: MockLLMClient
    ) -> None:
        from unittest.mock import patch

        stages: list[str] = []
        with patch(
            "src.core.agents.outline_builder.refine_volume_plans_length_aware"
        ) as spy:
            build_outline(
                premise=_PREMISE,
                total_volumes=2,
                chapters_per_volume=15,
                tension_template="heros_journey",
                genre="都市悬疑",
                client=mock_client,
                model="mock-default",
                prompts_dir=fake_prompts_dir,
                on_stage_complete=lambda s, o: stages.append(s),
            )
        assert spy.call_count == 0  # 无校准(qwen 路径)→ 细化跳过(零回归)
        assert stages.count("volume_plans") == 1
        assert stages == ["world", "characters", "plot", "volume_plans", "mini_plan"]

    def test_arc_span_gate_retries_volume_plans_before_mini_plan(
        self, fake_prompts_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """commit 期副本跨度硬 gate 前移:失败后重出卷规划,通过才进 mini_plan。"""
        from src.core.agents import outline_builder as ob

        bad = _make_bad_arc_span_volume_plans()
        good = VolumePlansOutput(volumes=[_make_volume_plan_item("v1", 7)])
        stages: list[str] = []
        refine_feedbacks: list[str | None] = []
        mini_plan_inputs: list[VolumePlansOutput] = []

        monkeypatch.setattr(ob, "build_world", lambda **_: _make_world())
        monkeypatch.setattr(ob, "build_characters", lambda **_: _make_characters())
        monkeypatch.setattr(ob, "build_plot", lambda **_: _make_plot())
        monkeypatch.setattr(ob, "build_volume_plans", lambda **_: good)
        monkeypatch.setattr(
            ob,
            "validate_outline_consistency",
            lambda **_: OutlineConsistencyResult(passed=True),
        )

        def fake_refine(**kwargs) -> VolumePlansOutput:
            refine_feedbacks.append(kwargs.get("consistency_retry_feedback"))
            return bad if len(refine_feedbacks) == 1 else good

        def fake_generate_mini_plan(**kwargs) -> MiniPlanOutput:
            mini_plan_inputs.append(kwargs["volume_plans"])
            return MiniPlanOutput(
                items=[
                    MiniPlanItem(
                        chapter_id="ch-0001",
                        chapter_number=1,
                        volume_id="v1",
                        arc_id="v1-arc-1",
                        one_line_event="林牧进入遗址并发现第一件异常青铜器。",
                    )
                ]
            )

        monkeypatch.setattr(ob, "refine_volume_plans_length_aware", fake_refine)
        monkeypatch.setattr(ob, "generate_mini_plan", fake_generate_mini_plan)

        outline = build_outline(
            premise=_PREMISE,
            total_volumes=1,
            chapters_per_volume=6,
            tension_template="heros_journey",
            genre="都市悬疑",
            length_calibration=_action_calibration(),
            target_word_count_max=4500,
            prompts_dir=fake_prompts_dir,
            on_stage_complete=lambda s, _: stages.append(s),
            character_backfill_enabled=False,
        )

        assert refine_feedbacks[0] is None
        assert refine_feedbacks[1] is not None
        assert "outline:commit 硬规则失败" in refine_feedbacks[1]
        assert "span=2" in refine_feedbacks[1]
        assert "至少 3 章" in refine_feedbacks[1]
        assert mini_plan_inputs == [good]
        assert outline.volume_plans == good
        assert stages == [
            "world",
            "characters",
            "plot",
            "volume_plans",
            "mini_plan",
        ]


class TestBuildOutlineAutofix:
    """v2 W3.5-s4-step1:大纲 name_typo 确定性自动修循环编排。

    monkeypatch ``validate_outline_consistency``(控 passed/分类序列)+
    ``apply_name_typo_fixes``(控 applied),验 gating + 自动修 + 重验 + 零 fail-open。
    4 段 + mini_plan 走真 MockLLMClient。
    """

    def _result(
        self, passed: bool, *, name_typo_fixes=(), has_non_fixable: bool = False
    ) -> OutlineConsistencyResult:
        return OutlineConsistencyResult(
            passed=passed,
            contradictions=[] if passed else ["x 冲突"],
            name_typo_fixes=list(name_typo_fixes),
            has_non_fixable_contradiction=has_non_fixable,
        )

    def _patch(self, monkeypatch, *, seq, on_apply=None):
        from src.core.agents import outline_builder as ob

        def _fake_validate(**_kw):
            return seq.pop(0)

        def _fake_apply(*, world, characters, plot, volume_plans, fixes, **_kw):
            if on_apply is not None:
                on_apply(fixes)
            return (world, characters, plot, volume_plans), list(fixes), []

        monkeypatch.setattr(ob, "validate_outline_consistency", _fake_validate)
        monkeypatch.setattr(ob, "validate_outline_consistency_partitioned", _fake_validate)
        monkeypatch.setattr(ob, "apply_name_typo_fixes", _fake_apply)

    def _build(self, mock_client, fake_prompts_dir, **kw):
        return build_outline(
            premise=_PREMISE,
            total_volumes=2,
            chapters_per_volume=15,
            tension_template="heros_journey",
            genre="仙侠",
            client=mock_client,
            model="mock-default",
            prompts_dir=fake_prompts_dir,
            **kw,
        )

    def test_all_typo_autofixed_revalidate_clean_proceeds(
        self, fake_prompts_dir, mock_client, monkeypatch
    ) -> None:
        applied_calls: list = []
        # 第一次:全 name_typo(可修);重验:干净。
        seq = [
            self._result(False, name_typo_fixes=[NameTypoFix("骆秉渊", "陆秉渊")]),
            self._result(True),
        ]
        self._patch(monkeypatch, seq=seq, on_apply=applied_calls.append)
        result = self._build(mock_client, fake_prompts_dir)  # 不应 raise
        assert isinstance(result, FullOutline)
        assert len(applied_calls) == 1  # apply 调用 1 次
        assert seq == []  # validator 调用 2 次(detect + re-validate)

    def test_all_typo_but_revalidate_still_fails_raises(
        self, fake_prompts_dir, mock_client, monkeypatch
    ) -> None:
        # 修后重验仍不过(残留冲突)→ 零 fail-open 维持 HARD。
        seq = [
            self._result(False, name_typo_fixes=[NameTypoFix("骆秉渊", "陆秉渊")]),
            self._result(False, name_typo_fixes=[NameTypoFix("骆秉渊", "陆秉渊")]),
        ]
        self._patch(monkeypatch, seq=seq)
        with pytest.raises(OutlineBuilderError):
            self._build(mock_client, fake_prompts_dir)

    def test_substantive_skips_autofix_raises(
        self, fake_prompts_dir, mock_client, monkeypatch
    ) -> None:
        # 含不可修硬伤(has_non_fixable)且关闭新 retry → 不走自动修,立即 HARD。
        apply_calls: list = []
        seq = [
            self._result(
                False,
                name_typo_fixes=[NameTypoFix("骆秉渊", "陆秉渊")],
                has_non_fixable=True,
            )
        ]
        self._patch(monkeypatch, seq=seq, on_apply=apply_calls.append)
        with pytest.raises(OutlineBuilderError):
            self._build(
                mock_client,
                fake_prompts_dir,
                outline_consistency_retry_enabled=False,
            )
        assert apply_calls == []  # apply 绝不被调用

    def test_substantive_consistency_retries_volume_plans_once(
        self, fake_prompts_dir, mock_client, monkeypatch
    ) -> None:
        # 首轮实质冲突 → 带 feedback 重出一次 volume_plans → 重验干净 → 继续 mini_plan。
        seq = [self._result(False, has_non_fixable=True), self._result(True)]
        self._patch(monkeypatch, seq=seq)
        stages: list[str] = []
        result = self._build(
            mock_client,
            fake_prompts_dir,
            character_backfill_enabled=False,
            on_stage_complete=lambda stage, _output: stages.append(stage),
        )
        assert isinstance(result, FullOutline)
        assert seq == []  # validator 首轮失败 + retry 后重验成功
        assert stages.count("volume_plans") == 1
        assert stages[-1] == "mini_plan"

    @pytest.mark.parametrize("volume_plans_retry_allowed", [False, True])
    def test_experimental_consistency_retries_only_repairable_volume(
        self, fake_prompts_dir, mock_client, monkeypatch, volume_plans_retry_allowed
    ) -> None:
        from src.core.agents import outline_builder as ob

        monkeypatch.setattr(ob, "uses_experimental_outline", lambda *_: True)
        monkeypatch.setattr(ob, "validate_outline_stage_consistency",
                            lambda **_: OutlineConsistencyResult(passed=True))
        failure = OutlineConsistencyResult(
            passed=False,
            contradictions=["有两端原文支持的时间矛盾"],
            has_non_fixable_contradiction=True,
            volume_plans_retry_allowed=volume_plans_retry_allowed,
        )
        seq = [failure]
        if volume_plans_retry_allowed:
            seq.append(self._result(True))
        self._patch(monkeypatch, seq=seq)
        stages: list[str] = []
        kwargs = {
            "character_backfill_enabled": False,
            "on_stage_complete": lambda stage, _output: stages.append(stage),
        }
        if volume_plans_retry_allowed:
            self._build(mock_client, fake_prompts_dir, **kwargs)
            assert stages.count("volume_plans") == 1
            assert stages[-1] == "mini_plan"
        else:
            with pytest.raises(OutlineBuilderError, match="时间矛盾"):
                self._build(mock_client, fake_prompts_dir, **kwargs)
            assert stages.count("volume_plans") == 0
            assert "mini_plan" not in stages
        assert stages.count("world") == 1
        assert stages.count("characters") == 1
        assert stages.count("plot") == 1
        assert seq == []

    def test_failed_full_review_stages_candidate_without_publishing_volume(
        self, fake_prompts_dir, mock_client, monkeypatch,
    ) -> None:
        from src.core.agents import outline_builder as ob

        monkeypatch.setattr(ob, "uses_experimental_outline", lambda *_: True)
        monkeypatch.setattr(ob, "validate_outline_stage_consistency",
                            lambda **_: OutlineConsistencyResult(passed=True))
        failure = OutlineConsistencyResult(passed=False,
            contradictions=["同一次旧事写成两个互斥结果"],
            contradiction_details=[{"desc": "同一次旧事写成两个互斥结果",
                "reason": "两侧原文不能同时成立", "source_quotes": [
                    {"path": "characters.characters.0.background", "quote": "旧事第一端"},
                    {"path": "plot.core_conflict", "quote": "旧事第二端"}]}],
            repair_scope="upstream", has_non_fixable_contradiction=True)
        monkeypatch.setattr(ob, "validate_outline_consistency", lambda **_: failure)
        monkeypatch.setattr(ob, "validate_outline_consistency_partitioned", lambda **_: failure)
        stages: list[str] = []
        candidates: list[VolumePlansOutput] = []
        def stage_candidate(world, characters, plot, volume_plans, consistency):
            assert consistency is failure
            candidates.append(volume_plans)
            return "candidate_" + "a" * 64
        with pytest.raises(OutlineBuilderError) as caught:
            self._build(mock_client, fake_prompts_dir, character_backfill_enabled=False,
                on_stage_complete=lambda stage, _: stages.append(stage),
                on_review_failure=stage_candidate)
        assert caught.value.candidate_id == "candidate_" + "a" * 64
        assert len(candidates) == 1
        assert stages == ["world", "characters", "plot"]

    def test_new_character_fact_conflict_retries_only_character_generation(
        self, fake_prompts_dir, mock_client, monkeypatch,
    ) -> None:
        from src.core.agents import outline_builder as ob

        monkeypatch.setattr(ob, "uses_experimental_outline", lambda *_: True)
        verdicts = [OutlineConsistencyResult(passed=False,
            contradictions=["借贷方向写反"], has_non_fixable_contradiction=True),
            OutlineConsistencyResult(passed=True)]
        monkeypatch.setattr(ob, "validate_outline_stage_consistency",
            lambda **_: verdicts.pop(0))
        characters = ob.build_characters(premise=_PREMISE, world=_make_world(),
            genre="仙侠", client=mock_client, model="mock-default", prompts_dir=fake_prompts_dir)
        assert isinstance(characters, CharactersOutput)
        assert verdicts == []
        assert mock_client._total_calls == 2

    def test_autofix_disabled_skips_autofix_raises(
        self, fake_prompts_dir, mock_client, monkeypatch
    ) -> None:
        # kill-switch:即便全 name_typo,关掉则维持 v1 HARD 行为。
        apply_calls: list = []
        seq = [self._result(False, name_typo_fixes=[NameTypoFix("骆秉渊", "陆秉渊")])]
        self._patch(monkeypatch, seq=seq, on_apply=apply_calls.append)
        with pytest.raises(OutlineBuilderError):
            self._build(mock_client, fake_prompts_dir, outline_autofix_enabled=False)
        assert apply_calls == []


class TestCharacterBackfillWiringS4Step1:
    """v2 W3.5-s4-step1 机制1:角色完整性回填在 build_outline 的接线。

    monkeypatch ``backfill_plot_referenced_characters``,验:① 默认开时被调两次
    (plot 后、volume_plans 前 + volume_plans 后、mini_plan 前)② 任一轮补到卡 →
    重持久化 characters(on_stage_complete 再调一次)③ no-op(added 空)→ 不重
    持久化 ④ kill-switch 关 → 不调。
    4 段走真 MockLLMClient。
    """

    def _build(self, mock_client, fake_prompts_dir, *, on_stage=None, **kw):
        return build_outline(
            premise=_PREMISE,
            total_volumes=2,
            chapters_per_volume=15,
            tension_template="heros_journey",
            genre="仙侠",
            client=mock_client,
            model="mock-default",
            prompts_dir=fake_prompts_dir,
            on_stage_complete=on_stage,
            **kw,
        )

    def test_plot_backfill_runs_before_volume_and_repersists_when_card_added(
        self, fake_prompts_dir, mock_client, monkeypatch
    ) -> None:
        calls: list = []

        def _fake_backfill(*, characters, volume_plans=None, **_kw):
            calls.append(volume_plans is not None)
            if volume_plans is None:
                return characters, ["plot-added"]
            return characters, []

        monkeypatch.setattr(
            outline_builder, "backfill_plot_referenced_characters", _fake_backfill
        )
        stages: list = []
        self._build(
            mock_client,
            fake_prompts_dir,
            on_stage=lambda s, o: stages.append(s),
        )
        assert calls == [False, True]
        # characters 出现 2 次:build_characters 后 + plot 回填补卡重持久化后
        assert stages.count("characters") == 2
        first_char_idx = stages.index("characters")
        second_char_idx = stages.index("characters", first_char_idx + 1)
        assert second_char_idx < stages.index("volume_plans")

    def test_volume_backfill_runs_after_volume_and_repersists_when_card_added(
        self, fake_prompts_dir, mock_client, monkeypatch
    ) -> None:
        calls: list = []

        def _fake_backfill(*, characters, volume_plans=None, **_kw):
            calls.append(volume_plans is not None)
            if volume_plans is not None:
                changed = characters.model_copy(deep=True)
                changed.characters[0].background += "；卷规划补充了此人的新经历"
                return changed, ["volume-added"]
            return characters, []

        monkeypatch.setattr(
            outline_builder, "backfill_plot_referenced_characters", _fake_backfill
        )
        stages: list = []
        self._build(
            mock_client,
            fake_prompts_dir,
            on_stage=lambda s, o: stages.append(s),
        )
        assert calls == [False, True]
        assert stages.count("characters") == 2
        first_char_idx = stages.index("characters")
        second_char_idx = stages.index("characters", first_char_idx + 1)
        assert second_char_idx < stages.index("volume_plans")
        assert second_char_idx < stages.index("mini_plan")

    def test_backfill_noop_does_not_repersist(
        self, fake_prompts_dir, mock_client, monkeypatch
    ) -> None:
        calls: list = []

        def _fake_backfill(*, characters, volume_plans=None, **_kw):
            calls.append(volume_plans is not None)
            return characters, []  # added 空 = no-op

        monkeypatch.setattr(
            outline_builder, "backfill_plot_referenced_characters", _fake_backfill
        )
        stages: list = []
        self._build(
            mock_client,
            fake_prompts_dir,
            on_stage=lambda s, o: stages.append(s),
        )
        assert calls == [False, True]
        assert stages.count("characters") == 1  # 不重持久化

    def test_backfill_disabled_skips_call(
        self, fake_prompts_dir, mock_client, monkeypatch
    ) -> None:
        calls: list = []
        monkeypatch.setattr(
            outline_builder,
            "backfill_plot_referenced_characters",
            lambda **_kw: calls.append(1),
        )
        self._build(mock_client, fake_prompts_dir, character_backfill_enabled=False)
        assert calls == []  # kill-switch 关 → 回填绝不被调

    @pytest.mark.parametrize("after_volume", [False, True])
    def test_backfill_error_stops_following_stages_and_preserves_evidence(
        self, fake_prompts_dir, mock_client, monkeypatch, after_volume,
    ) -> None:
        from src.core.agents.character_backfill import CharacterBackfillError

        def fail_backfill(*, characters, volume_plans=None, **_kw):
            if (volume_plans is not None) == after_volume:
                raise CharacterBackfillError(
                    "人物补充无有效结果", raw_output="原始错误人物结果",
                    model_used="deepseek-flash", attempts=3,
                )
            return characters, []

        monkeypatch.setattr(outline_builder, "backfill_plot_referenced_characters", fail_backfill)
        stages: list[str] = []
        with pytest.raises(OutlineBuilderError, match="人物补充无有效结果") as caught:
            self._build(mock_client, fake_prompts_dir, on_stage=lambda s, o: stages.append(s))
        assert caught.value.stage == "characters"
        assert caught.value.raw_output == "原始错误人物结果"
        assert caught.value.attempts == 3
        assert caught.value.model_used == "deepseek-flash"
        assert stages.count("characters") == 1
        assert "mini_plan" not in stages
        assert "volume_plans" not in stages


class _R9PlotClient(LLMClient):
    """稳定返回同一合法 plot，并记录 HARD 反馈后的局部重试消息。"""

    def __init__(self) -> None:
        self.plot_calls = 0
        self.retry_user_text = ""

    def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        **kwargs: object,
    ) -> LLMResponse:
        del model, kwargs
        user_text = messages[-1].content
        assert "<!-- agent_role: outline_plot -->" in user_text
        self.plot_calls += 1
        if self.plot_calls > 1:
            self.retry_user_text = user_text
        return _llm_response(
            json.dumps(_make_plot().model_dump(mode="json"), ensure_ascii=False)
        )


class TestPlotSettingGateR9:
    _RULE = "记忆抹除一次只影响一个与死亡真相因果绑定最深的人。"
    _EVIDENCE = _make_plot().core_conflict

    @staticmethod
    def _hard_violation() -> ConstraintViolation:
        return ConstraintViolation(
            constraint_id="5" * 64,
            domain="world.unique_rules",
            source_path="data/outline/world.json",
            source_hash="6" * 64,
            rule_quote=TestPlotSettingGateR9._RULE,
            evidence_quote=TestPlotSettingGateR9._EVIDENCE,
            severity="hard",
            ambiguity=None,
        )

    def test_plot_user_message_injects_exact_rule_and_execution_contract(
        self,
    ) -> None:
        world = _make_world().model_copy(update={"unique_rules": self._RULE})

        message = _build_plot_user_message(
            premise=_PREMISE,
            genre="悬疑",
            world=world,
            characters=_make_characters(),
        )

        assert "## 主线写前世界规则合同(HARD)" in message
        assert f"权威规则原文:{self._RULE}" in message
        assert "角色不得选择自己、同伴、指定人物、群体、类别或概念" in message
        assert "原文允许群体效果时不得擅自缩成单对象" in message
        assert "误判、妄想或因规则不允许而无法启动" in message

    def test_hard_conflict_retries_plot_with_exact_contract_feedback(
        self,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        world = _make_world().model_copy(update={"unique_rules": self._RULE})
        client = _R9PlotClient()
        semantic_calls: list[str] = []

        def fake_setting_gate(**kwargs: object) -> SettingConsistencyResult:
            semantic_calls.append(str(kwargs["validation_scope"]))
            assert self._EVIDENCE in str(kwargs["chapter_text"])
            if len(semantic_calls) == 1:
                return SettingConsistencyResult(
                    passed=False,
                    violations=[self._hard_violation()],
                    validation_source="authoritative_world_contract",
                )
            return SettingConsistencyResult(
                passed=True,
                validation_source="authoritative_world_contract",
            )

        monkeypatch.setattr(
            outline_builder, "validate_setting_consistency", fake_setting_gate
        )
        result = build_plot(
            premise=_PREMISE,
            world=world,
            characters=_make_characters(),
            client=client,
            model="mock-default",
            prompts_dir=fake_prompts_dir,
        )

        assert isinstance(result, PlotOutput)
        assert client.plot_calls == 2
        assert semantic_calls == ["outline_plot", "outline_plot"]
        assert self._RULE in client.retry_user_text
        assert self._EVIDENCE in client.retry_user_text

    def test_hard_retry_exhaustion_keeps_plot_error_code(
        self,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        world = _make_world().model_copy(update={"unique_rules": self._RULE})
        client = _R9PlotClient()
        monkeypatch.setattr(
            outline_builder.settings, "outline_builder_max_retry_attempts", 2
        )
        monkeypatch.setattr(
            outline_builder,
            "validate_setting_consistency",
            lambda **_kwargs: SettingConsistencyResult(
                passed=False,
                violations=[self._hard_violation()],
                validation_source="authoritative_world_contract",
            ),
        )

        with pytest.raises(OutlineBuilderError) as exc_info:
            build_plot(
                premise=_PREMISE,
                world=world,
                characters=_make_characters(),
                client=client,
                model="mock-default",
                prompts_dir=fake_prompts_dir,
            )

        assert isinstance(exc_info.value.__cause__, PlotSettingConflictError)
        assert exc_info.value.error_code == "PLOT_SETTING_CONFLICT"
        assert exc_info.value.attempts == 2
        assert len(exc_info.value.violations or []) == 1
        assert client.plot_calls == 2

    def test_setting_judge_technical_failure_does_not_retry_plot(
        self,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        world = _make_world().model_copy(update={"unique_rules": self._RULE})
        client = _R9PlotClient()

        def technical_failure(**_kwargs: object) -> SettingConsistencyResult:
            raise SettingConsistencyTechnicalError(
                stage="transport_or_parse",
                attempts=2,
                cause=RuntimeError("validator timeout"),
            )

        monkeypatch.setattr(
            outline_builder, "validate_setting_consistency", technical_failure
        )

        with pytest.raises(SettingConsistencyTechnicalError):
            build_plot(
                premise=_PREMISE,
                world=world,
                characters=_make_characters(),
                client=client,
                model="mock-default",
                prompts_dir=fake_prompts_dir,
            )

        assert client.plot_calls == 1


class _R9VolumePlansClient(LLMClient):
    """返回结构合法但语义违规的卷规划，并记录 HARD 反馈后的局部重试。"""

    def __init__(self) -> None:
        self._delegate = MockLLMClient()
        self.volume_calls = 0
        self.retry_user_text = ""

    def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        **kwargs: object,
    ) -> LLMResponse:
        user_text = messages[-1].content
        assert "<!-- agent_role: outline_volume_plans -->" in user_text
        self.volume_calls += 1
        if self.volume_calls > 1:
            self.retry_user_text = user_text
        response = self._delegate.complete(messages=messages, model=model, **kwargs)
        payload = json.loads(response.content)
        payload["volumes"][-1]["summary"] = "地宫的抹除规则随之失效。"
        return LLMResponse(
            content=json.dumps(payload, ensure_ascii=False),
            model_used=response.model_used,
            usage=response.usage,
            finish_reason=response.finish_reason,
        )


class TestVolumePlansSettingGateR9:
    _RULE = "每次真相闭合后，核心镜自动抹除一名因果绑定最深的人，规则不可逆。"
    _EVIDENCE = "地宫的抹除规则随之失效。"

    @staticmethod
    def _hard_violation() -> ConstraintViolation:
        return ConstraintViolation(
            constraint_id="7" * 64,
            domain="world.unique_rules",
            source_path="data/outline/world.json",
            source_hash="8" * 64,
            rule_quote=TestVolumePlansSettingGateR9._RULE,
            evidence_quote=TestVolumePlansSettingGateR9._EVIDENCE,
            severity="hard",
            ambiguity=None,
        )

    @staticmethod
    def _build(
        *,
        world: WorldOutput,
        client: LLMClient,
        prompts_dir: Path,
    ) -> VolumePlansOutput:
        return build_volume_plans(
            premise=_PREMISE,
            world=world,
            characters=_make_characters(),
            plot=_make_plot(),
            total_volumes=2,
            chapters_per_volume=6,
            tension_template="heros_journey",
            genre="悬疑",
            client=client,
            model="mock-default",
            prompts_dir=prompts_dir,
        )

    def test_user_message_injects_exact_rule_and_full_plot_semantics(self) -> None:
        world = _make_world().model_copy(update={"unique_rules": self._RULE})
        plot = _make_plot()

        message = _build_volume_plans_user_message(
            premise=_PREMISE,
            genre="悬疑",
            world=world,
            characters=_make_characters(),
            plot=plot,
            total_volumes=2,
            chapters_per_volume=6,
            tension_template="heros_journey",
        )

        assert "## 卷规划写前世界规则合同(HARD)" in message
        assert f"权威规则原文:{self._RULE}" in message
        assert "规则本身失效" in message
        assert "改变、移除、逆转、暂停、重置或绕过" in message
        assert plot.main_phases[0].summary in message
        assert plot.major_turning_points[0].description in message
        assert plot.long_threads[0].description in message

    def test_hard_conflict_retries_volume_plans_before_return(
        self,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        world = _make_world().model_copy(update={"unique_rules": self._RULE})
        client = _R9VolumePlansClient()
        semantic_calls: list[str] = []

        def fake_setting_gate(**kwargs: object) -> SettingConsistencyResult:
            semantic_calls.append(str(kwargs["validation_scope"]))
            assert self._EVIDENCE in str(kwargs["chapter_text"])
            if len(semantic_calls) == 1:
                return SettingConsistencyResult(
                    passed=False,
                    violations=[self._hard_violation()],
                    validation_source="authoritative_world_contract",
                )
            return SettingConsistencyResult(
                passed=True,
                validation_source="authoritative_world_contract",
            )

        monkeypatch.setattr(
            outline_builder, "validate_setting_consistency", fake_setting_gate
        )
        result = self._build(
            world=world,
            client=client,
            prompts_dir=fake_prompts_dir,
        )

        assert isinstance(result, VolumePlansOutput)
        assert client.volume_calls == 2
        assert semantic_calls == ["volume_plans", "volume_plans"]
        assert self._RULE in client.retry_user_text
        assert self._EVIDENCE in client.retry_user_text

    def test_hard_retry_exhaustion_keeps_volume_error_code(
        self,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        world = _make_world().model_copy(update={"unique_rules": self._RULE})
        client = _R9VolumePlansClient()
        monkeypatch.setattr(
            outline_builder.settings, "outline_builder_max_retry_attempts", 2
        )
        monkeypatch.setattr(
            outline_builder,
            "validate_setting_consistency",
            lambda **_kwargs: SettingConsistencyResult(
                passed=False,
                violations=[self._hard_violation()],
                validation_source="authoritative_world_contract",
            ),
        )

        with pytest.raises(OutlineBuilderError) as exc_info:
            self._build(
                world=world,
                client=client,
                prompts_dir=fake_prompts_dir,
            )

        assert isinstance(exc_info.value.__cause__, VolumePlansSettingConflictError)
        assert exc_info.value.error_code == "VOLUME_PLANS_SETTING_CONFLICT"
        assert exc_info.value.attempts == 2
        assert len(exc_info.value.violations or []) == 1
        assert client.volume_calls == 2

    def test_setting_judge_technical_failure_does_not_retry_volume_plans(
        self,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        world = _make_world().model_copy(update={"unique_rules": self._RULE})
        client = _R9VolumePlansClient()

        def technical_failure(**_kwargs: object) -> SettingConsistencyResult:
            raise SettingConsistencyTechnicalError(
                stage="transport_or_parse",
                attempts=2,
                cause=RuntimeError("validator timeout"),
            )

        monkeypatch.setattr(
            outline_builder, "validate_setting_consistency", technical_failure
        )

        with pytest.raises(SettingConsistencyTechnicalError):
            self._build(
                world=world,
                client=client,
                prompts_dir=fake_prompts_dir,
            )

        assert client.volume_calls == 1


class _R3MiniPlanClient(LLMClient):
    """让 F ch-0002 冲突只出现在 v1 首次结果，验证单卷局部 retry。"""

    def __init__(self, *, conflict_every_time: bool = False) -> None:
        self._delegate = MockLLMClient()
        self.conflict_every_time = conflict_every_time
        self.mini_calls: dict[str, int] = {}
        self.retry_user_text: dict[str, str] = {}

    def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        **kwargs: object,
    ) -> LLMResponse:
        response = self._delegate.complete(messages, model=model, **kwargs)
        user_text = messages[-1].content
        if "<!-- agent_role: mini_plan_generator -->" not in user_text:
            return response
        volume_id = "v1" if "- volume_id:v1" in user_text else "v2"
        attempt = self.mini_calls.get(volume_id, 0) + 1
        self.mini_calls[volume_id] = attempt
        if attempt > 1:
            self.retry_user_text[volume_id] = user_text
        if volume_id != "v1" or (attempt > 1 and not self.conflict_every_time):
            return response
        payload = json.loads(response.content)
        payload["items"][1]["one_line_event"] = (
            "林澈在旅馆房间床底暗格中翻出导师孙启明藏匿的残缺笔记，"
            "通过比对地图坐标与涂抹痕迹，锁定无名地宫方位及核心文物青铜镜。"
        )
        return LLMResponse(
            content=json.dumps(payload, ensure_ascii=False),
            model_used=response.model_used,
            usage=response.usage,
            finish_reason=response.finish_reason,
        )


class TestMiniPlanSettingGateR3:
    _RULE = (
        "载有被抹除者信息的物理记录会衰变，"
        "只有地宫内部或距青铜镜一米内的物品豁免。"
    )
    _EVIDENCE = (
        "林澈在旅馆房间床底暗格中翻出导师孙启明藏匿的残缺笔记，"
        "通过比对地图坐标与涂抹痕迹，锁定无名地宫方位及核心文物青铜镜。"
    )

    @staticmethod
    def _hard_violation() -> ConstraintViolation:
        return ConstraintViolation(
            constraint_id="3" * 64,
            domain="world.unique_rules",
            source_path="data/outline/world.json",
            source_hash="4" * 64,
            rule_quote=TestMiniPlanSettingGateR3._RULE,
            evidence_quote=TestMiniPlanSettingGateR3._EVIDENCE,
            severity="hard",
            ambiguity=None,
        )

    def _volume_plans(
        self, mock_client: MockLLMClient, fake_prompts_dir: Path, world: WorldOutput
    ) -> VolumePlansOutput:
        return build_volume_plans(
            premise="戈壁地宫调查",
            world=world,
            characters=_make_characters(),
            plot=_make_plot(),
            total_volumes=2,
            chapters_per_volume=5,
            tension_template="heros_journey",
            client=mock_client,
            model="mock-default",
            prompts_dir=fake_prompts_dir,
        )

    def test_hard_retries_only_failed_volume_then_healthy_volume_once(
        self,
        mock_client: MockLLMClient,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        world = _make_world().model_copy(update={"unique_rules": self._RULE})
        volumes = self._volume_plans(mock_client, fake_prompts_dir, world)
        client = _R3MiniPlanClient()
        semantic_calls: list[str] = []

        def fake_setting_gate(**kwargs: object) -> SettingConsistencyResult:
            chapter_id = str(kwargs["chapter_id"])
            semantic_calls.append(chapter_id)
            if self._EVIDENCE in str(kwargs["chapter_text"]):
                return SettingConsistencyResult(
                    passed=False,
                    violations=[self._hard_violation()],
                    validation_source="authoritative_world_contract",
                )
            return SettingConsistencyResult(
                passed=True,
                validation_source="authoritative_world_contract",
            )

        monkeypatch.setattr(
            outline_builder, "validate_setting_consistency", fake_setting_gate
        )
        result = generate_mini_plan(
            world=world,
            characters=_make_characters(),
            plot=_make_plot(),
            volume_plans=volumes,
            client=client,
            model="mock-default",
            prompts_dir=fake_prompts_dir,
        )

        assert len(result.items) == 10
        assert client.mini_calls == {"v1": 2, "v2": 1}
        assert semantic_calls == ["mini-plan:v1", "mini-plan:v1", "mini-plan:v2"]
        assert self._RULE in client.retry_user_text["v1"]
        assert self._EVIDENCE in client.retry_user_text["v1"]
        assert all(self._EVIDENCE not in item.one_line_event for item in result.items)

    def test_hard_retry_exhaustion_keeps_structured_error(
        self,
        mock_client: MockLLMClient,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        world = _make_world().model_copy(update={"unique_rules": self._RULE})
        volumes = self._volume_plans(mock_client, fake_prompts_dir, world)
        client = _R3MiniPlanClient(conflict_every_time=True)
        monkeypatch.setattr(
            outline_builder.settings, "outline_builder_max_retry_attempts", 2
        )
        monkeypatch.setattr(
            outline_builder,
            "validate_setting_consistency",
            lambda **_kwargs: SettingConsistencyResult(
                passed=False,
                violations=[self._hard_violation()],
                validation_source="authoritative_world_contract",
            ),
        )

        with pytest.raises(OutlineBuilderError) as exc_info:
            generate_mini_plan(
                world=world,
                characters=_make_characters(),
                plot=_make_plot(),
                volume_plans=volumes,
                client=client,
                model="mock-default",
                prompts_dir=fake_prompts_dir,
            )

        assert isinstance(exc_info.value.__cause__, MiniPlanSettingConflictError)
        assert exc_info.value.error_code == "MINI_PLAN_SETTING_CONFLICT"
        assert exc_info.value.attempts == 2
        assert len(exc_info.value.violations or []) == 1
        assert client.mini_calls == {"v1": 2}

    def test_setting_judge_technical_failure_does_not_retry_mini_plan(
        self,
        mock_client: MockLLMClient,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        world = _make_world().model_copy(update={"unique_rules": self._RULE})
        volumes = self._volume_plans(mock_client, fake_prompts_dir, world)
        client = _R3MiniPlanClient()

        def technical_failure(**_kwargs: object) -> SettingConsistencyResult:
            raise SettingConsistencyTechnicalError(
                stage="transport_or_parse",
                attempts=2,
                cause=RuntimeError("validator timeout"),
            )

        monkeypatch.setattr(
            outline_builder, "validate_setting_consistency", technical_failure
        )

        with capture_logs() as logs:
            with pytest.raises(SettingConsistencyTechnicalError) as exc_info:
                generate_mini_plan(
                    world=world,
                    characters=_make_characters(),
                    plot=_make_plot(),
                    volume_plans=volumes,
                    client=client,
                    model="mock-default",
                    prompts_dir=fake_prompts_dir,
                    timing_context={"workspace_id": "ws-r3"},
                )

        assert exc_info.value.attempts == 2
        assert client.mini_calls == {"v1": 1}
        failed_timing = next(
            log
            for log in logs
            if log.get("event") == "outline_stage_timing"
            and log.get("status") == "failed"
        )
        assert failed_timing["stage"] == "mini_plan"
        assert failed_timing["attempts"] == 2
        assert (
            failed_timing["reason"] == "non_retryable_post_validate_failed"
        )


class TestR9V6HardRuleUpstreamVisibility:
    """会被世界规则 Judge 阻断的生成阶段必须先看到同一条 HARD 原文。"""

    _RULE = "任何活物触碰黑匣后都会失去最近一小时记忆，无任何豁免。"

    def _world(self) -> WorldOutput:
        return _make_world().model_copy(update={"unique_rules": self._RULE})

    def test_characters_stage_gets_exact_rule_and_no_loophole_boundary(self) -> None:
        message = outline_builder._build_characters_user_message(
            premise=_PREMISE,
            genre="悬疑",
            world=self._world(),
        )

        assert (
            f"unique_rules(HARD；只应包含真正不可破例的世界规则):{self._RULE}"
            in message
        )
        assert "## 核心人设写前世界规则合同(HARD)" in message
        assert "不得为 `unique_rules` 创造规则原文没有的例外" in message
        assert "只有 `unique_rules` 原文明示的例外才可使用" in message
        assert "`setting` / `power_system` 是普通机制上下文" in message

    def test_mini_plan_stage_gets_shared_contract_with_explicit_level(self) -> None:
        world = self._world()
        contract = SettingContract.from_world_output(world)
        volume = _make_volume_plan_item()
        chapter_id_map = {
            (volume.volume_id, number): f"ch-{number:04d}"
            for number in range(1, volume.chapter_count + 1)
        }

        message = outline_builder._build_mini_plan_user_message_per_volume(
            world=world,
            setting_contract=contract,
            characters=_make_characters(),
            plot=_make_plot(),
            volume=volume,
            genre="悬疑",
            chapter_id_map=chapter_id_map,
            prev_volumes_tail=[],
        )

        assert contract.render_for_prompt() in message
        assert self._RULE in message
        assert "`unique_rules` 才是不可破例的 HARD" in message
        assert "`setting` / `power_system` 是世界背景与普通机制上下文" in message


def _make_post_v6_parallel_volume(
    chapter_count: int,
) -> tuple[VolumePlanItem, list[MiniPlanItem]]:
    """构造任意卷长的完整 Beat 覆盖，不依赖 Provider fixture。"""

    split = chapter_count // 2
    first_arc = VolumeArcPlan(
        arc_id="v1-arc-a",
        arc_type="主线",
        chapter_range=(1, split),
        phase="opening",
        summary="前半卷",
        beats=[
            VolumeBeatPlan(
                beat_id="v1-beat-a",
                chapter_range=(1, split),
                summary="前半卷推进",
                must_advance=["推进前半卷"],
                forbidden_advance=["不得提前收束"],
            )
        ],
    )
    second_arc = VolumeArcPlan(
        arc_id="v1-arc-b",
        arc_type="主线",
        chapter_range=(split + 1, chapter_count),
        phase="climax",
        summary="后半卷",
        beats=[
            VolumeBeatPlan(
                beat_id="v1-beat-b",
                chapter_range=(split + 1, chapter_count),
                summary="后半卷推进",
                must_advance=["推进后半卷"],
                forbidden_advance=["不得跳过高潮"],
            )
        ],
    )
    volume = VolumePlanItem(
        volume_id="v1",
        title="并行测试卷",
        summary="验证任意卷长严格波次",
        chapter_count=chapter_count,
        tension_template="heros_journey",
        arcs=[first_arc, second_arc],
        tension_curve=[5] * chapter_count,
    )
    items = [
        MiniPlanItem(
            chapter_id=f"ch-{number:04d}",
            chapter_number=number,
            volume_id="v1",
            arc_id="v1-arc-a" if number <= split else "v1-arc-b",
            one_line_event=f"第 {number} 章推进本章所属 Beat",
            info_increment=f"新增信息 {number}",
        )
        for number in range(1, chapter_count + 1)
    ]
    return volume, items


class TestPostV6BeatSafeParallelism:
    def test_strict_waves_copy_context_and_deep_freeze_payload(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        volume, items = _make_post_v6_parallel_volume(9)
        monkeypatch.setattr(
            outline_builder.settings,
            "mini_plan_beat_alignment_max_concurrency",
            4,
        )
        identity: ContextVar[tuple[str, str, str]] = ContextVar("beat_identity")
        token = identity.set(("ws-safe", "ch-scope", "trace-safe"))
        lock = threading.Lock()
        first_wave_barrier = threading.Barrier(4)
        active = 0
        max_active = 0
        finished: set[int] = set()
        initial_lengths: list[int] = []
        snapshots: list[dict[str, Any]] = []

        def fake_validator(**kwargs: Any) -> BeatAlignmentValidationResult:
            nonlocal active, max_active
            number = int(str(kwargs["chapter_id"]).split("-")[-1])
            beat = kwargs["beat"]
            assert identity.get() == ("ws-safe", "ch-scope", "trace-safe")
            with lock:
                if 5 <= number <= 8:
                    assert {1, 2, 3, 4}.issubset(finished)
                if number == 9:
                    assert set(range(1, 9)).issubset(finished)
                active += 1
                max_active = max(max_active, active)
                initial_lengths.append(len(beat.must_advance))
            beat.must_advance.append(f"worker-{number}")
            if number <= 4:
                first_wave_barrier.wait(timeout=2)
            time.sleep(0.005)
            with lock:
                active -= 1
                finished.add(number)
            return BeatAlignmentValidationResult(passed=True, llm_calls_count=1)

        monkeypatch.setattr(
            outline_builder,
            "validate_beat_alignment",
            fake_validator,
        )
        try:
            outline_builder._validate_mini_plan_beat_alignment(
                items=items,
                volume=volume,
                router=None,
                client=None,
                model=None,
                prompts_dir=None,
                on_parallel_status=snapshots.append,
            )
        finally:
            identity.reset(token)

        assert max_active == 4
        assert finished == set(range(1, 10))
        assert initial_lengths == [1] * 9
        assert volume.arcs[0].beats[0].must_advance == ["推进前半卷"]
        assert volume.arcs[1].beats[0].must_advance == ["推进后半卷"]
        assert all(task["status"] == "queued" for task in snapshots[0]["tasks"])
        assert all(task["status"] == "succeeded" for task in snapshots[-1]["tasks"])

    def test_parallel_failure_uses_input_order_and_does_not_start_later_wave(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        volume, items = _make_post_v6_parallel_volume(9)
        monkeypatch.setattr(
            outline_builder.settings,
            "mini_plan_beat_alignment_max_concurrency",
            4,
        )
        barrier = threading.Barrier(4)
        calls: list[str] = []
        snapshots: list[dict[str, Any]] = []

        def fake_validator(**kwargs: Any) -> BeatAlignmentValidationResult:
            chapter_id = str(kwargs["chapter_id"])
            calls.append(chapter_id)
            barrier.wait(timeout=2)
            # ch-0004 先返回失败、ch-0002 后返回失败；合并仍必须报 ch-0002。
            if chapter_id == "ch-0002":
                time.sleep(0.02)
            if chapter_id in {"ch-0002", "ch-0004"}:
                return BeatAlignmentValidationResult(
                    passed=False,
                    must_advance_missing=["推进"],
                    evidence=f"{chapter_id} failed",
                    llm_calls_count=1,
                )
            return BeatAlignmentValidationResult(passed=True, llm_calls_count=1)

        monkeypatch.setattr(
            outline_builder,
            "validate_beat_alignment",
            fake_validator,
        )
        with pytest.raises(ValueError, match="ch-0002"):
            outline_builder._validate_mini_plan_beat_alignment(
                items=items,
                volume=volume,
                router=None,
                client=None,
                model=None,
                prompts_dir=None,
                on_parallel_status=snapshots.append,
            )

        assert set(calls) == {"ch-0001", "ch-0002", "ch-0003", "ch-0004"}
        later = snapshots[-1]["tasks"][4:]
        assert all(task["status"] == "failed" for task in later)
        assert all(task["reason"] == "not_run_after_earlier_failure" for task in later)

    def test_parallel_and_serial_failure_payloads_are_exactly_equal(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        volume, items = _make_post_v6_parallel_volume(5)

        def fake_validator(**kwargs: Any) -> BeatAlignmentValidationResult:
            chapter_id = str(kwargs["chapter_id"])
            if chapter_id == "ch-0002":
                return BeatAlignmentValidationResult(
                    passed=False,
                    forbidden_advance_violated=["不得提前收束"],
                    must_advance_missing=["推进前半卷"],
                    evidence="固定失败证据",
                    llm_calls_count=1,
                )
            return BeatAlignmentValidationResult(passed=True, llm_calls_count=1)

        monkeypatch.setattr(
            outline_builder,
            "validate_beat_alignment",
            fake_validator,
        )
        errors: list[str] = []
        for concurrency in (1, 4):
            monkeypatch.setattr(
                outline_builder.settings,
                "mini_plan_beat_alignment_max_concurrency",
                concurrency,
            )
            with pytest.raises(ValueError) as exc_info:
                outline_builder._validate_mini_plan_beat_alignment(
                    items=items,
                    volume=volume,
                    router=None,
                    client=None,
                    model=None,
                    prompts_dir=None,
                )
            errors.append(str(exc_info.value))

        assert errors[1] == errors[0]

    def test_cancel_context_reaches_all_started_workers_and_waits_for_them(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        volume, items = _make_post_v6_parallel_volume(8)
        monkeypatch.setattr(
            outline_builder.settings,
            "mini_plan_beat_alignment_max_concurrency",
            4,
        )
        cancel_event = threading.Event()
        barrier = threading.Barrier(4)
        finished: set[str] = set()
        started: list[str] = []
        lock = threading.Lock()

        def fake_validator(**kwargs: Any) -> BeatAlignmentValidationResult:
            chapter_id = str(kwargs["chapter_id"])
            assert current_cancel_event() is cancel_event
            with lock:
                started.append(chapter_id)
            barrier.wait(timeout=2)
            if chapter_id == "ch-0001":
                cancel_event.set()
            time.sleep(0.01)
            try:
                raise_if_cancelled()
            finally:
                with lock:
                    finished.add(chapter_id)
            raise AssertionError("cancel event 应在所有第一波 worker 中生效")

        monkeypatch.setattr(
            outline_builder,
            "validate_beat_alignment",
            fake_validator,
        )
        with bound_cancel_event(cancel_event):
            with pytest.raises(LLMCancelledError):
                outline_builder._validate_mini_plan_beat_alignment(
                    items=items,
                    volume=volume,
                    router=None,
                    client=None,
                    model=None,
                    prompts_dir=None,
                )

        assert set(started) == {"ch-0001", "ch-0002", "ch-0003", "ch-0004"}
        assert finished == set(started)

    def test_cancellation_dominates_same_wave_semantic_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        volume, items = _make_post_v6_parallel_volume(8)
        monkeypatch.setattr(
            outline_builder.settings,
            "mini_plan_beat_alignment_max_concurrency",
            4,
        )
        barrier = threading.Barrier(4)
        calls: list[str] = []

        def fake_validator(**kwargs: Any) -> BeatAlignmentValidationResult:
            chapter_id = str(kwargs["chapter_id"])
            calls.append(chapter_id)
            barrier.wait(timeout=2)
            if chapter_id == "ch-0001":
                return BeatAlignmentValidationResult(
                    passed=False,
                    evidence="semantic failure",
                    llm_calls_count=1,
                )
            if chapter_id == "ch-0002":
                raise LLMCancelledError("cancelled")
            return BeatAlignmentValidationResult(passed=True, llm_calls_count=1)

        monkeypatch.setattr(
            outline_builder,
            "validate_beat_alignment",
            fake_validator,
        )

        with pytest.raises(LLMCancelledError):
            outline_builder._validate_mini_plan_beat_alignment(
                items=items,
                volume=volume,
                router=None,
                client=None,
                model=None,
                prompts_dir=None,
            )

        assert set(calls) == {"ch-0001", "ch-0002", "ch-0003", "ch-0004"}

    def test_serial_fallback_restores_old_fail_fast_call_count(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        volume, items = _make_post_v6_parallel_volume(9)
        monkeypatch.setattr(
            outline_builder.settings,
            "mini_plan_beat_alignment_max_concurrency",
            1,
        )
        calls: list[str] = []

        def fake_validator(**kwargs: Any) -> BeatAlignmentValidationResult:
            chapter_id = str(kwargs["chapter_id"])
            calls.append(chapter_id)
            return BeatAlignmentValidationResult(
                passed=chapter_id != "ch-0002",
                evidence="serial",
                llm_calls_count=1,
            )

        monkeypatch.setattr(
            outline_builder,
            "validate_beat_alignment",
            fake_validator,
        )
        with pytest.raises(ValueError, match="ch-0002"):
            outline_builder._validate_mini_plan_beat_alignment(
                items=items,
                volume=volume,
                router=None,
                client=None,
                model=None,
                prompts_dir=None,
            )
        assert calls == ["ch-0001", "ch-0002"]

    @pytest.mark.parametrize(
        ("chapter_count", "expected_waves"),
        [(12, 3), (15, 4), (25, 7)],
    )
    def test_arbitrary_volume_lengths_use_ceil_n_over_four_waves(
        self,
        chapter_count: int,
        expected_waves: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        volume, items = _make_post_v6_parallel_volume(chapter_count)
        monkeypatch.setattr(
            outline_builder.settings,
            "mini_plan_beat_alignment_max_concurrency",
            4,
        )
        snapshots: list[dict[str, Any]] = []
        calls: list[str] = []
        lock = threading.Lock()
        waves_seen: set[int] = set()
        finished: set[int] = set()

        def fake_validator(**kwargs: Any) -> BeatAlignmentValidationResult:
            chapter_id = str(kwargs["chapter_id"])
            number = int(chapter_id.split("-")[-1])
            wave = (number - 1) // 4
            with lock:
                if wave > 0:
                    assert set(range(1, wave * 4 + 1)).issubset(finished)
                waves_seen.add(wave)
                calls.append(chapter_id)
                finished.add(number)
            return BeatAlignmentValidationResult(passed=True, llm_calls_count=1)

        monkeypatch.setattr(
            outline_builder,
            "validate_beat_alignment",
            fake_validator,
        )

        outline_builder._validate_mini_plan_beat_alignment(
            items=items,
            volume=volume,
            router=None,
            client=None,
            model=None,
            prompts_dir=None,
            on_parallel_status=snapshots.append,
        )

        assert len(calls) == chapter_count
        assert waves_seen == set(range(expected_waves))
        assert all(task["status"] == "succeeded" for task in snapshots[-1]["tasks"])

    def test_volumes_remain_strictly_serial(
        self,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first, _ = _make_post_v6_parallel_volume(4)
        second = first.model_copy(
            deep=True,
            update={"volume_id": "v2", "title": "第二卷"},
        )
        volume_plans = VolumePlansOutput(volumes=[first, second])
        chapter_ids = {
            ("v1", number): f"ch-{number:04d}" for number in range(1, 5)
        }
        chapter_ids.update(
            {("v2", number): f"ch-{number + 4:04d}" for number in range(1, 5)}
        )
        snapshots: list[dict[str, Any]] = []
        monkeypatch.setattr(
            outline_builder.settings,
            "mini_plan_beat_alignment_max_concurrency",
            4,
        )
        monkeypatch.setattr(
            outline_builder,
            "validate_beat_alignment",
            lambda **_kwargs: BeatAlignmentValidationResult(
                passed=True,
                llm_calls_count=1,
            ),
        )

        result = generate_mini_plan(
            world=_make_world(),
            characters=_make_characters(),
            plot=_make_plot(),
            volume_plans=volume_plans,
            chapter_id_by_volume_and_number=chapter_ids,
            client=MockLLMClient(),
            model="mock-default",
            prompts_dir=fake_prompts_dir,
            on_parallel_status=snapshots.append,
        )

        assert len(result.items) == 8
        first_second_volume_frame = next(
            index
            for index, snapshot in enumerate(snapshots)
            if snapshot["volume_id"] == "v2"
        )
        assert all(
            snapshot["volume_id"] == "v1"
            for snapshot in snapshots[:first_second_volume_frame]
        )
        assert all(
            task["status"] == "succeeded"
            for task in snapshots[first_second_volume_frame - 1]["tasks"]
        )


@pytest.mark.parametrize("experimental", [True, False])
def test_outline_stage_retries_include_source_only_for_experimental_pipeline(experimental):
    from pydantic import BaseModel, Field
    from src.core.agents.creative_delta import CREATIVE_DELTA_CAPABILITY

    class Candidate(BaseModel):
        name: str
        age: int = Field(ge=1)

    class Client(_QueuedTextClient):
        def __init__(self):
            super().__init__(["{\"name\":\"许禾\",\"age\":0}", "{\"name\":\"许禾\",\"age\":16}"])
            self.calls = []
            if experimental:
                self.creative_delta_capability = CREATIVE_DELTA_CAPABILITY

        def complete(self, messages, **params):
            self.calls.append(messages)
            return super().complete(messages, **params)

    client = Client()
    result = _run_stage(stage="characters", system_text="sys", user_text="只读原上游",
                        output_model=Candidate, router=None, client=client, model="fake-model",
                        max_tokens=100, temperature=0.5)
    assert result.name == "许禾" and result.age == 16
    assert len(client.calls) == 2 and len(client.calls[0]) == 2
    if experimental:
        assert len(client.calls[1]) == 4
        assert client.calls[1][2].content == "{\"name\":\"许禾\",\"age\":0}"
    else:
        assert len(client.calls[1]) == 2
