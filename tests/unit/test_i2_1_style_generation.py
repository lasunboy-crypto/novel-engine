"""I2.1 写作风格模板影响 generation_params + ChapterCard 默认值 单测。

v1.1 wave 4 session 3(I2.1)。覆盖:

1. ``STYLE_GENERATION_DEFAULTS`` 注册表自洽 + 与 ``STYLE_TEMPLATES`` key 对齐
   + 数值对齐 I1 第⑨⑩维定性文案 + candidates §I2 temperature 字面;
2. ``GenerationConfig.plot_node_budget`` 字段默认 + 合并;
3. ``ConfigService.load_generation_config`` ``engine_base_override`` 风格基线注入
   + project.yaml L1 覆盖字段级优先;
4. ``_assemble_chapter_card`` plot_node_budget 兜底优先级(LLM > 工作区默认 > 模型默认);
5. chapter_card_generator user message must_achieve_max 软提示注入(有/无模板);
6. Writer ``_resolve_style_writer_temperature``(T-A)+ write_chapter router 路径覆盖。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.agents import _prompt as prompt_module
from src.core.agents.chapter_card_generator import (
    ChapterCardGenerationContext,
    _assemble_chapter_card,
    _ChapterCardDecisionPayload,
    _build_user_message,
)
from src.core.agents.writer import (
    _resolve_style_writer_temperature,
    write_chapter,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.generation_config import GenerationConfig
from src.core.models.routing import RoutingConfig, WriterProfilesConfig
from src.core.models.setting_contract import SettingContract
from src.core.models.style_template import (
    STYLE_GENERATION_DEFAULTS,
    STYLE_TEMPLATES,
)
from src.core.models.writer_profile import WriterProfile
from src.core.routing.router import ModelRouter
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.config_service import ConfigService
from src.services.workspace_service import WorkspaceService


# ---------------------------------------------------------------------------
# 1. STYLE_GENERATION_DEFAULTS 注册表
# ---------------------------------------------------------------------------


class TestStyleGenerationRegistry:
    def test_keys_align_with_style_templates(self) -> None:
        """生成默认注册表 key 与 I1 STYLE_TEMPLATES 完全一致(6 模板)。"""
        assert set(STYLE_GENERATION_DEFAULTS) == set(STYLE_TEMPLATES)
        assert len(STYLE_GENERATION_DEFAULTS) == 7

    def test_word_count_ordering_valid(self) -> None:
        """每模板 word_count_min <= word_count_max(模型 validator 已校验,再断言)。"""
        for name, gen in STYLE_GENERATION_DEFAULTS.items():
            assert gen.word_count_min <= gen.word_count_max, name
            assert gen.plot_node_budget >= 1
            assert gen.must_achieve_max >= 1
            assert 0.0 <= gen.writer_temperature <= 2.0

    def test_values_materialize_i1_dimensions(self) -> None:
        """数值落实 I1 第⑨⑩维文案 + candidates temperature 字面(关键模板抽查)。"""
        comedic = STYLE_GENERATION_DEFAULTS["comedic_burst"]
        # I1 文案"偏短 2800-3500 字 / 多事件 4-5 件" + candidates"搞笑 0.9 灵活"
        assert (comedic.word_count_min, comedic.word_count_max) == (2800, 3500)
        assert comedic.plot_node_budget == 5
        assert comedic.must_achieve_max == 5
        assert comedic.writer_temperature == 0.9

        grand = STYLE_GENERATION_DEFAULTS["grand_epic"]
        # I1 文案"偏长 4500-6000 / 少事件 2-3" + candidates"宏大 0.65 稳重"
        assert (grand.word_count_min, grand.word_count_max) == (4500, 6000)
        assert grand.plot_node_budget == 3
        assert grand.must_achieve_max == 3
        assert grand.writer_temperature == 0.65

    def test_multi_event_templates_higher_budget(self) -> None:
        """多事件模板(搞笑/热血)节点预算 > 少事件模板(宏大/悬疑/治愈/古典)。"""
        multi = ("comedic_burst", "heroic_battle")
        few = ("grand_epic", "dark_mystery", "healing_daily", "classical_xianxia")
        for n in multi:
            assert STYLE_GENERATION_DEFAULTS[n].plot_node_budget == 5
        for n in few:
            assert STYLE_GENERATION_DEFAULTS[n].plot_node_budget == 3


# ---------------------------------------------------------------------------
# 2. GenerationConfig.plot_node_budget
# ---------------------------------------------------------------------------


class TestGenerationConfigPlotNodeBudget:
    def test_default_aligns_chapter_card(self) -> None:
        assert GenerationConfig().plot_node_budget == 3

    def test_explicit_value(self) -> None:
        cfg = GenerationConfig(plot_node_budget=5)
        assert cfg.plot_node_budget == 5

    def test_ge_1(self) -> None:
        with pytest.raises(ValueError):
            GenerationConfig(plot_node_budget=0)


# ---------------------------------------------------------------------------
# 3. ConfigService.load_generation_config engine_base_override
# ---------------------------------------------------------------------------


@pytest.fixture
def config_service(tmp_path: Path) -> ConfigService:
    workspaces_root = tmp_path / "workspaces"
    workspaces_root.mkdir()
    # 引擎 config.yaml 不提供 generation 段 → 走 GenerationConfig() 默认。
    engine_cfg = tmp_path / "config.yaml"
    engine_cfg.write_text("writer_profiles: {}\n", encoding="utf-8")
    return ConfigService(
        workspaces_root=workspaces_root, engine_config_path=engine_cfg
    )


def _make_ws_dir(workspaces_root: Path, ws_id: str) -> Path:
    ws = workspaces_root / ws_id
    ws.mkdir(parents=True, exist_ok=True)
    return ws


class TestEngineBaseOverride:
    def test_base_override_used_when_no_project_yaml(
        self, config_service: ConfigService
    ) -> None:
        """无 project.yaml → 直接返回风格基线(替代 config.yaml 引擎默认)。"""
        _make_ws_dir(config_service._workspaces_root, "ws_no_proj")
        base = GenerationConfig(
            word_count_min=2800, word_count_max=3500, plot_node_budget=5
        )
        cfg = config_service.load_generation_config(
            "ws_no_proj", engine_base_override=base
        )
        assert cfg.word_count_min == 2800
        assert cfg.word_count_max == 3500
        assert cfg.plot_node_budget == 5

    def test_project_yaml_field_overrides_style_base(
        self, config_service: ConfigService
    ) -> None:
        """project.yaml L1 用户覆盖字段级优先于风格基线;未填字段保留基线。"""
        ws = _make_ws_dir(config_service._workspaces_root, "ws_l1")
        (ws / "project.yaml").write_text(
            "generation:\n  word_count_min: 3000\n", encoding="utf-8"
        )
        base = GenerationConfig(
            word_count_min=2800, word_count_max=3500, plot_node_budget=5
        )
        cfg = config_service.load_generation_config(
            "ws_l1", engine_base_override=base
        )
        # word_count_min 被 L1 覆盖,其余字段保留风格基线。
        assert cfg.word_count_min == 3000
        assert cfg.word_count_max == 3500
        assert cfg.plot_node_budget == 5

    def test_no_base_override_falls_back_engine_default(
        self, config_service: ConfigService
    ) -> None:
        """不传 engine_base_override → 沿用 config.yaml 引擎默认(零回归)。"""
        _make_ws_dir(config_service._workspaces_root, "ws_default")
        cfg = config_service.load_generation_config("ws_default")
        assert cfg.word_count_min == 2500
        assert cfg.word_count_max == 4000
        assert cfg.plot_node_budget == 3


# ---------------------------------------------------------------------------
# 4. _assemble_chapter_card plot_node_budget 兜底优先级
# ---------------------------------------------------------------------------


def _make_context(**overrides) -> ChapterCardGenerationContext:
    from src.core.agents.chapter_card_generator import CharacterVoiceHint

    payload: dict = dict(
        workspace_id="ws-i2",
        chapter_id="ch-0001",
        volume_id="vol-1",
        chapter_number=1,
        arc_id="arc-1",
        arc_type="主线",
        arc_phase="opening",
        arc_summary="开篇",
        is_arc_finale=False,
        tension_level=5,
        setting_contract=SettingContract(
            setting="仙侠",
            source_path="data/outline/world.json",
            source_hash="0" * 64,
        ),
        core_conflict="复仇",
        volume_summary="重生卷",
        character_voices=[
            CharacterVoiceHint(
                character_id="char-a",
                name="李清玄",
                role="protagonist",
                voice_hint="铿锵",
            )
        ],
    )
    payload.update(overrides)
    return ChapterCardGenerationContext(**payload)


def _make_payload(**overrides) -> _ChapterCardDecisionPayload:
    payload: dict = dict(
        title="第一章",
        pov_character="char-a",
        setting="青崖山",
        must_achieve=["突破剑阵"],
        next_chapter_seed="玉佩颤动",
        related_characters=["char-a"],
        continuity_context=ContinuityContext(
            prev_chapter_tail="(首章)",
            unfinished_actions=[],
            time_gap="立即续接",
            scene_continuation="same",
            pov_change=False,
            pov_from=None,
            pov_to="char-a",
        ),
    )
    payload.update(overrides)
    return _ChapterCardDecisionPayload(**payload)


class TestAssemblePlotNodeBudget:
    def test_llm_payload_wins(self) -> None:
        card = _assemble_chapter_card(
            payload=_make_payload(plot_node_budget=7),
            context=_make_context(default_plot_node_budget=5),
        )
        assert card.plot_node_budget == 7

    def test_workspace_default_when_llm_absent(self) -> None:
        card = _assemble_chapter_card(
            payload=_make_payload(),  # 不产 plot_node_budget
            context=_make_context(default_plot_node_budget=5),
        )
        assert card.plot_node_budget == 5

    def test_model_default_when_both_absent(self) -> None:
        card = _assemble_chapter_card(
            payload=_make_payload(),
            context=_make_context(),  # default_plot_node_budget=None
        )
        assert card.plot_node_budget == 3  # ChapterCard 模型默认


# ---------------------------------------------------------------------------
# 5. chapter_card_generator must_achieve_max 软提示
# ---------------------------------------------------------------------------


class TestMustAchieveMaxPromptHint:
    def test_hint_present_when_set(self) -> None:
        msg = _build_user_message(_make_context(default_must_achieve_max=3))
        assert "事件密度引导" in msg
        assert "3 项以内" in msg

    def test_hint_absent_when_none(self) -> None:
        msg = _build_user_message(_make_context())
        assert "事件密度引导" not in msg


# ---------------------------------------------------------------------------
# 6. Writer T-A 温度 override
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_prompt_cache():
    prompt_module.load_prompt_template.cache_clear()
    yield
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "writer.md").write_text("FAKE_WRITER_PROMPT", encoding="utf-8")
    return prompts_dir


def _make_writer_card() -> ChapterCard:
    return ChapterCard(
        chapter_id="ch-0001",
        workspace_id="ws-style",
        volume_id="v01",
        chapter_number=1,
        title="第一章",
        tension_level=5,
        must_achieve=["事件 A"],
        must_unresolved=["悬念"],
        forbidden=["现代元素"],
        pov_character="李清玄",
        setting="青崖山",
        continuity_context=ContinuityContext(
            prev_chapter_tail="(首章)",
            unfinished_actions=[],
            time_gap="立即续接",
            scene_continuation="same",
            pov_change=False,
            pov_from=None,
            pov_to="李清玄",
        ),
        next_chapter_seed="玉佩颤动",
        arc_phase="opening",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=["李清玄"],
    )


def _make_workspace_with_style(
    tmp_path: Path, style_template: str | None
) -> WorkspacePaths:
    """用 WorkspaceService 建工作区写 meta.json(含 style_template),返 paths。"""
    workspaces_root = tmp_path / "ws_root"
    archives_root = tmp_path / "arch_root"
    workspaces_root.mkdir(exist_ok=True)
    archives_root.mkdir(exist_ok=True)
    svc = WorkspaceService(
        workspaces_root=workspaces_root, archives_root=archives_root
    )
    ws_id = f"ws-{style_template or 'none'}"
    svc.create(
        ws_id,
        display_name="I2.1 style test",
        genre="xianxia",
        premise="test",
        style_template=style_template,
        activate=False,
    )
    return WorkspacePaths(ws_id, workspaces_root=workspaces_root)


class TestResolveStyleWriterTemperature:
    def test_none_paths_returns_none(self) -> None:
        assert _resolve_style_writer_temperature(None) is None

    def test_no_style_template_returns_none(self, tmp_path: Path) -> None:
        paths = _make_workspace_with_style(tmp_path, None)
        assert _resolve_style_writer_temperature(paths) is None

    def test_style_template_returns_temperature(self, tmp_path: Path) -> None:
        paths = _make_workspace_with_style(tmp_path, "comedic_burst")
        assert _resolve_style_writer_temperature(paths) == 0.9

    def test_grand_epic_low_temperature(self, tmp_path: Path) -> None:
        paths = _make_workspace_with_style(tmp_path, "grand_epic")
        assert _resolve_style_writer_temperature(paths) == 0.65

    def test_missing_meta_returns_none(self, tmp_path: Path) -> None:
        """meta.json 缺失 → None(容错,不抛)。"""
        paths = WorkspacePaths("ws-ghost", workspaces_root=tmp_path / "empty")
        assert _resolve_style_writer_temperature(paths) is None


def _make_writer_router(profile_temp: float = 0.5) -> ModelRouter:
    """单 mock profile 路由 writer;profile 自带 temperature(被风格 override 替换)。"""
    profile = WriterProfile(
        profile_id="p_writer",
        provider="mock",
        model="mock-writer",
        context_window=16000,
        generation_params={"temperature": profile_temp},
    )
    config = WriterProfilesConfig(
        profiles={"p_writer": profile},
        routing=RoutingConfig(
            default="p_writer", rules=[], agent_routing={"writer": "p_writer"}
        ),
    )
    return ModelRouter(config, client_factory=lambda **kw: MagicMock(spec=LLMClient))


class TestWriteChapterStyleTemperatureOverride:
    def test_router_path_overrides_temperature(
        self, tmp_path: Path, fake_prompts_dir: Path
    ) -> None:
        """router 路径 + 工作区选 comedic_burst → complete 用 temperature=0.9。"""
        spy = MagicMock(spec=LLMClient)
        spy.complete.return_value = LLMResponse(
            content="正文",
            model_used="mock-writer",
            usage=TokenUsage(input_tokens=10, output_tokens=20),
            finish_reason="end_turn",
        )
        profile = WriterProfile(
            profile_id="p_writer",
            provider="mock",
            model="mock-writer",
            context_window=16000,
            generation_params={"temperature": 0.5},
        )
        config = WriterProfilesConfig(
            profiles={"p_writer": profile},
            routing=RoutingConfig(
                default="p_writer", rules=[], agent_routing={"writer": "p_writer"}
            ),
        )
        router = ModelRouter(config, client_factory=lambda **kw: spy)
        paths = _make_workspace_with_style(tmp_path, "comedic_burst")

        write_chapter(
            router=router,
            chapter_card=_make_writer_card(),
            memory_section="",
            workspace_paths=paths,
            workspace_id="ws-comedic_burst",
            prompts_dir=fake_prompts_dir,
        )
        assert spy.complete.call_args.kwargs["temperature"] == 0.9

    def test_router_path_no_style_keeps_profile_temperature(
        self, tmp_path: Path, fake_prompts_dir: Path
    ) -> None:
        """工作区未选模板 → 不覆盖,沿用 profile temperature 0.5。"""
        spy = MagicMock(spec=LLMClient)
        spy.complete.return_value = LLMResponse(
            content="正文",
            model_used="mock-writer",
            usage=TokenUsage(input_tokens=10, output_tokens=20),
            finish_reason="end_turn",
        )
        profile = WriterProfile(
            profile_id="p_writer",
            provider="mock",
            model="mock-writer",
            context_window=16000,
            generation_params={"temperature": 0.5},
        )
        config = WriterProfilesConfig(
            profiles={"p_writer": profile},
            routing=RoutingConfig(
                default="p_writer", rules=[], agent_routing={"writer": "p_writer"}
            ),
        )
        router = ModelRouter(config, client_factory=lambda **kw: spy)
        paths = _make_workspace_with_style(tmp_path, None)

        write_chapter(
            router=router,
            chapter_card=_make_writer_card(),
            memory_section="",
            workspace_paths=paths,
            workspace_id="ws-none",
            prompts_dir=fake_prompts_dir,
        )
        assert spy.complete.call_args.kwargs["temperature"] == 0.5

    def test_legacy_path_not_overridden(
        self, tmp_path: Path, fake_prompts_dir: Path
    ) -> None:
        """legacy 路径(client= + model=)用 caller 显式 temperature,不被风格扰动。"""
        spy = MagicMock(spec=LLMClient)
        spy.complete.return_value = LLMResponse(
            content="正文",
            model_used="claude-sonnet-4-6",
            usage=TokenUsage(input_tokens=10, output_tokens=20),
            finish_reason="end_turn",
        )
        paths = _make_workspace_with_style(tmp_path, "comedic_burst")

        write_chapter(
            client=spy,
            model="claude-sonnet-4-6",
            temperature=0.6,
            chapter_card=_make_writer_card(),
            memory_section="",
            workspace_paths=paths,
            workspace_id="ws-comedic_burst",
            prompts_dir=fake_prompts_dir,
        )
        # legacy 路径 router=None → 不进 override 分支,沿用 caller 0.6。
        assert spy.complete.call_args.kwargs["temperature"] == 0.6
