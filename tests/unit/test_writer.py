"""Writer agent 单测(P1-B-10 简版 + P2-B-10 retriever / voice 扩展)。

完全 mock ``LLMClient``,不真调 API;用临时目录注入 fake ``writer.md``,
避免依赖仓库根的真实提示词文件做断言。

P2-B-10 新增 ``TestRetrievedMemorySection`` / ``TestVoiceProfileSection`` /
``TestRetrievalAndVoiceCombined`` 三组用例,验证 ``vector_store`` /
``db_session`` 注入路径;P1 默认路径(两参数都 ``None``)用例保持原状,
零退化。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from src.core.agents import _prompt as prompt_module
from src.core.agents import writer as writer_module
from src.core.agents.writer import (
    RewriteConstraintGuardrail,
    WriterOutput,
    write_chapter,
)
from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage
from src.core.memory.vector import QueryHit, VectorStore
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.character import (
    Character,
    CharacterStateSnapshot,
    IdentityFacet,
    VoiceProfile,
)
from src.core.models.core_state import (
    BookClockConstraint,
    BookClockInterval,
    CoreStateFact,
    make_core_state_projection,
)
from src.core.models.drama import (
    DramaticBeatContract,
    build_dramatic_source_binding,
    VolumeDramaticPromise,
)
from src.core.models.review import (
    ConstraintResult,
    ConstraintViolation,
    stable_constraint_id,
)
from src.core.models.setting_contract import SettingContract
from src.core.rules.prose_hard_contract import (
    RewriteStyleConstraintSpec,
    compile_executable_constraint_plan,
)


def _make_continuity(**overrides) -> ContinuityContext:
    payload = dict(
        prev_chapter_tail="他握紧剑柄,目光不离对面那道身影。",
        unfinished_actions=["剑势悬而未发"],
        time_gap="立即续接",
        scene_continuation="same",
        pov_change=False,
        pov_from="林衍",
        pov_to="林衍",
    )
    payload.update(overrides)
    return ContinuityContext(**payload)


def _make_card(**overrides) -> ChapterCard:
    payload: dict = dict(
        chapter_id="ch-0002",
        workspace_id="ws-demo",
        volume_id="v01",
        chapter_number=2,
        title="第二章·破阵",
        tension_level=8,
        word_count_min=2800,
        word_count_max=4500,
        plot_node_budget=3,
        must_achieve=["林衍突破第一道剑阵", "苏离表明立场"],
        must_unresolved=["阵眼真凶身份"],
        forbidden=["现代科技元素", "提及第三卷剧情"],
        pov_character="林衍",
        setting="青崖山顶残阵",
        continuity_context=_make_continuity(),
        next_chapter_seed="苏离消失前留下的玉佩在颤动",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=["thread-main", "thread-suli"],
        related_characters=["林衍", "苏离"],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=3,
        max_revises=2,
    )
    payload.update(overrides)
    return ChapterCard(**payload)


def test_a1_writer_renders_typed_dramatic_source_without_hidden_markers() -> None:
    promise = VolumeDramaticPromise(
        primary_thread_id="thread-main",
        primary_turning_id="turn-final",
        actor_id="lin-yan",
        counterforce_id="mo-zun",
        outcome="explicitly_deferred",
        deferred_to_volume_id="v02",
        deferred_to_thread_id="thread-main",
    )
    beat = DramaticBeatContract(
        function="finale",
        actor_id="lin-yan",
        counterforce_id="mo-zun",
        interaction_mode="indirect",
        thread_id="thread-main",
        turning_id="turn-final",
        actor_goal="林衍把证据交给盟友保管",
        counteraction="墨尊隔空切断山门通信",
        choice="林衍放弃当场追击并保护证人",
        consequence="本卷保住证据，但主冲突明确延至下一卷",
    )
    binding = build_dramatic_source_binding(
        volume_id="v01",
        beat_id="v1-final",
        promise=promise,
        beat_contract=beat,
        mini_plan_contract=beat,
    )

    rendered = writer_module._format_dramatic_source(
        _make_card(dramatic_source=binding)
    )

    assert "interaction_mode:indirect" in rendered
    assert "volume_final_outcome:explicitly_deferred" in rendered
    assert "deferred_to_volume_id:v02" in rendered
    assert beat.counteraction in rendered
    assert "DRAMA_" not in rendered


def _make_response(
    *,
    content: str = "正文内容(略)",
    model_used: str = "claude-sonnet-4-6-20250101",
    input_tokens: int = 1234,
    output_tokens: int = 5678,
    finish_reason: str | None = "end_turn",
) -> LLMResponse:
    return LLMResponse(
        content=content,
        model_used=model_used,
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        finish_reason=finish_reason,
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    """在临时目录里建一个 writer.md,内容可断言。"""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "writer.md").write_text(
        "FAKE_WRITER_SYSTEM_PROMPT\n\n用于单测断言的 system 标记。",
        encoding="utf-8",
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _clear_prompt_cache():
    """每个用例清一次 lru_cache,避免不同 fake 模板互相污染。

    Rule of Three 抽取后,缓存对象在 ``src.core.agents._prompt`` 里,
    Writer/Reviewer/Reviser 共享。各自单测都按本 fixture 清一次,顺序
    无关。
    """
    prompt_module.load_prompt_template.cache_clear()
    yield
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=LLMClient)
    client.complete.return_value = _make_response()
    return client


class TestPromptAssembly:
    """user 消息含章节卡核心字段、记忆段落、衔接桥;system 来自模板文件。"""

    def test_user_message_contains_card_core_fields(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        card = _make_card()
        write_chapter(
            client=mock_client,
            chapter_card=card,
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        messages = mock_client.complete.call_args.kwargs["messages"]
        user_text = messages[1].content

        assert card.title in user_text
        assert card.pov_character in user_text
        assert "word_count_min=2800" in user_text
        assert "word_count_max=4500" in user_text
        # 必达事件至少一条出现在 user 消息里
        assert "林衍突破第一道剑阵" in user_text
        # 禁止元素至少一条出现
        assert "现代科技元素" in user_text

    def test_writer_sees_same_core_facts_and_judge_ids_before_generation(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        projection = make_core_state_projection(
            facts=[
                CoreStateFact(
                    revision_id="csr-mirror",
                    state_key="object:青铜镜:surface",
                    entity_type="object",
                    entity_id="青铜镜",
                    attribute="surface",
                    value="无锈无氧化",
                    source_kind="approved_text",
                    source_chapter_id="ch-0001",
                    source_version_id=1,
                    source_path="chapter_summaries/ch-0001.core_state_candidates",
                    source_text_hash="e" * 64,
                    evidence_quote="镜面无锈无氧化。",
                    valid_from_chapter=1,
                )
            ],
            book_clock=BookClockInterval(
                minimum_minutes=7 * 24 * 60,
                maximum_minutes=10 * 24 * 60,
            ),
        )
        clock = BookClockConstraint(
            time_gap="一日后",
            previous_interval=projection.book_clock,
            gap_interval=BookClockInterval(
                minimum_minutes=24 * 60,
                maximum_minutes=24 * 60,
            ),
            current_interval=BookClockInterval(
                minimum_minutes=8 * 24 * 60,
                maximum_minutes=11 * 24 * 60,
            ),
            is_hard=True,
        )
        card = _make_card(
            continuity_context=_make_continuity(time_gap="一日后"),
            core_state_contract_version=1,
            core_state_projection_hash=projection.projection_hash,
            book_clock_constraint=clock,
        )
        plan = compile_executable_constraint_plan(
            chapter_card=card,
            stage="draft",
            core_state_projection=projection,
        )

        write_chapter(
            client=mock_client,
            chapter_card=card,
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            executable_constraint_plan=plan,
            core_state_projection=projection,
        )

        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "surface=无锈无氧化" in user_text
        assert "本章主时间线距开篇：8天—11天" in user_text
        assert "不要求正文逐条复述" in user_text
        assert plan.by_kind("core_state")[0].constraint_id in user_text
        assert plan.by_kind("book_clock")[0].constraint_id in user_text

    def test_user_message_ends_with_writer_role_marker(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # P1-B-22:user message 末尾必须含 agent 身份 marker。
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert user_text.rstrip().endswith("<!-- agent_role: writer -->")

    def test_user_message_contains_continuity_tail(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        card = _make_card()
        write_chapter(
            client=mock_client,
            chapter_card=card,
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "他握紧剑柄,目光不离对面那道身影。" in user_text
        assert "立即续接" in user_text

    @pytest.mark.parametrize("creation_type", ["draft", "rewrite"])
    def test_user_message_includes_approved_continuity_bridge_verbatim(
        self,
        creation_type: str,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
    ) -> None:
        bridge = (
            "游荡者收到低频指令后撤，林澈的悬停状态随即解除；陈远从岔口"
            "经过却没有发现他，林澈再沿信号进入支路并抵达祭坛。"
        )
        card = _make_card(
            continuity_context=_make_continuity(continuity_bridge=bridge)
        )

        write_chapter(
            client=mock_client,
            chapter_card=card,
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            creation_type=creation_type,  # type: ignore[arg-type]
        )

        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "已批准的具体过桥路线(continuity_bridge)" in user_text
        assert bridge in user_text

    def test_continuity_labels_pov_change_as_cross_chapter_only(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        card = _make_card(
            pov_character="苏离",
            continuity_context=_make_continuity(
                pov_change=True,
                pov_from="林衍",
                pov_to="苏离",
            ),
        )
        write_chapter(
            client=mock_client,
            chapter_card=card,
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "跨章主视角角色变化(pov_change):切换" in user_text
        assert "只描述章节边界" in user_text
        assert "不授权本章内部切换其他角色视角或改变叙述人称" in user_text

    def test_user_message_contains_memory_section(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        memory = "## 世界观\n\n青崖山位于东境最北。\n\n## 人物\n\n林衍:主角"
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section=memory,
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "青崖山位于东境最北" in user_text
        assert "## 背景信息" in user_text

    def test_system_message_loaded_from_template_file(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        messages = mock_client.complete.call_args.kwargs["messages"]
        assert messages[0].role == "system"
        assert "FAKE_WRITER_SYSTEM_PROMPT" in messages[0].content


class TestCraftSection:
    """M-craft(v1.1 wave 5 s2):引擎内置通用反套话写作手法纪律段。

    与其它条件段(identity / callback / style_guide)的根本区别 = **无条件每章
    注入**、**book-agnostic**(不读工作区配置 / 不依赖所选模板)。走范式 ②
    (writer 级注入,与 A3 同型),实证见 w5-s1 postmortem §5(降套话 67~91% +
    盲读 5→7)。
    """

    def test_craft_section_unconditionally_present(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 最朴素路径:无 db_session / 无 workspace_paths / 无风格模板 → craft 段
        # 仍必须出现。无条件注入是本段与其它条件段(identity / style_guide 可空)
        # 的根本差别,这条钉死它。
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 本章文学执行" in user_text

    def test_hard_contract_precedes_salient_literary_block(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """先给真实通过标准，再给唯一文学块；二者都在辅助记忆之前。"""
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="辅助记忆只应出现在权威层之后",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        hard_pos = user_text.index("## 本轮正文约束合同")
        literary_pos = user_text.index("## 本章文学执行")
        memory_pos = user_text.index("辅助记忆只应出现在权威层之后")
        assert hard_pos < literary_pos < memory_pos

    def test_craft_section_carries_core_craft_directions(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 近普世内核(2026-06-06 三层归位后):反复读机 + 关键戏别偷懒(写具体的
        # 活性成分,限定关键戏、非处处多写)+ 克制补刀。整体 showing 密度归
        # style_template,**不**在通用静态段(否则误伤爽文)。
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "不是/并非……而是……" in user_text
        assert "先注意什么" in user_text
        assert "办案路径" in user_text
        assert "让人物的反应自然展开" in user_text
        assert "演完即停" not in user_text
        # 题材相关的整体 showing 密度指令已移出(归 style_template,避免误伤爽文)
        assert "show, don't tell" not in user_text

    def test_craft_section_is_book_agnostic(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # book-agnostic 守门:craft 段**绝不含**任何特定书(B1 锦衣卫穿越本)的
        # 专属套话 —— 偏头痛比喻 / 锦衣卫黑话 / 跨界频段控制网等是工作区级
        # style_guide 的事;硬编码进引擎 = 产品功能对其它题材的书失效。这条钉死
        # postmortem §5 的"③ 本书专属清单已砍掉"。
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        for book_specific in ("偏头痛", "锦衣卫", "跨界", "频段", "控制网", "锚点"):
            assert book_specific not in user_text, (
                f"craft 段混入本书专属套话: {book_specific}"
            )

    def test_build_craft_section_returns_static_nonempty(self) -> None:
        # 单元级:``_build_craft_section()`` 永远返回固定非空串(无条件注入的
        # 前提 —— 永不返回空,故 _build_user_message 必 append)。
        from src.core.agents.writer import _build_craft_section

        section = _build_craft_section()
        assert section.strip()
        assert "## 本章文学执行" in section


class TestCraftFocusSection:
    """M2 纠正侧(v1.1 wave 5 s3):本章 per-scene 卓越性焦点段(card.craft_focus)。

    与无条件的 craft 段(TestCraftSection)区别 = **条件段**:card.craft_focus
    非空才注入(动态 per-chapter),空时跳过(旧卡 / 动作章留空 → 零回归)。
    """

    def test_craft_focus_section_present_when_card_has_focus(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # card.craft_focus 非空 → "## 本章卓越性焦点" banner + 焦点原文都进 user message。
        focus = "情感节点:破阵失败后林衍独处时深挖自我怀疑;关键戏:剑阵对峙给画面。"
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(craft_focus=focus),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 本章卓越性焦点" in user_text
        assert focus in user_text

    def test_craft_focus_section_absent_when_empty(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 护留白 + 零回归:craft_focus 默认 ""(动作章 / 旧卡)→ 段完全不出现。
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),  # craft_focus 默认 ""
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 本章卓越性焦点" not in user_text

    def test_format_craft_focus_unit(self) -> None:
        # 单元级:空 → "";非空 → 含 banner + 焦点原文。
        from src.core.agents.writer import _format_craft_focus

        assert _format_craft_focus(_make_card(craft_focus="")) == ""
        assert _format_craft_focus(_make_card(craft_focus="   ")) == ""  # 仅空白也跳过
        rendered = _format_craft_focus(_make_card(craft_focus="情感节点:深挖内心"))
        assert "## 本章卓越性焦点" in rendered
        assert "情感节点:深挖内心" in rendered


class TestChapterFocusSection:
    """M4'''(v1.1 wave 5 s6):本章结构焦点段(card.chapter_focus)。

    条件段:card.chapter_focus 非空(偏离题材默认结构的章)才注入,空时跳过
    (默认结构章 / 旧卡留空 → 逐字节零回归)。与 craft_focus 正交(结构形态 vs 笔墨投放)。
    """

    def test_chapter_focus_section_present_when_card_has_focus(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # card.chapter_focus 非空 → "## 本章结构焦点" banner + 焦点原文 + 反稀释指令都进。
        focus = "情感单焦-现实:全程在 ICU 守挚友咽气,梦境侧只在主角恍惚回闪一掠,不平行展开。"
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(chapter_focus=focus),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 本章结构焦点" in user_text
        assert focus in user_text
        assert "别按题材默认结构" in user_text  # 反稀释指令在位

    def test_chapter_focus_section_absent_when_empty(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 护留白 + 零回归:chapter_focus 默认 ""(默认结构章 / 旧卡)→ 段完全不出现。
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),  # chapter_focus 默认 ""
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 本章结构焦点" not in user_text

    def test_format_chapter_focus_unit(self) -> None:
        # 单元级:空 / 仅空白 → "";非空 → 含 banner + 焦点原文 + 反稀释 + 连贯指令。
        from src.core.agents.writer import _format_chapter_focus

        assert _format_chapter_focus(_make_card(chapter_focus="")) == ""
        assert _format_chapter_focus(_make_card(chapter_focus="   ")) == ""
        rendered = _format_chapter_focus(
            _make_card(chapter_focus="角色单焦-林疏影黑化")
        )
        assert "## 本章结构焦点" in rendered
        assert "角色单焦-林疏影黑化" in rendered
        assert "连贯优先" in rendered  # 连贯硬约束在位


class TestArcBeatsSection:
    """M5(v1.1 wave 5 s7):本章角色弧光台阶段(card.arc_beats)。

    条件段:card.arc_beats 非空(某角色弧光真推进的章)才注入,空 dict 时跳过
    (绝大多数章无弧光推进 / 旧卡 → 逐字节零回归)。命门=动作演出非旁白声明。
    """

    def test_arc_beats_section_present_when_card_has_beats(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # card.arc_beats 非空 → banner + 各台阶 value 原文 + 禁旁白指令都进。
        beat = "林疏影首次为查案默许伪造一条线索,亲手按下证物销毁键、删完盯着空屏幕没说话。"
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(arc_beats={"lin-shu-ying": beat}),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 本章角色弧光台阶" in user_text
        assert beat in user_text
        assert "用动作 / 抉择 / 代价演出" in user_text  # 动作演出指令在位
        assert "绝不允许" in user_text  # 禁旁白声明

    def test_arc_beats_section_absent_when_empty(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 护留白 + 零回归:arc_beats 默认 {}(无弧光推进章 / 旧卡)→ 段完全不出现。
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),  # arc_beats 默认 {}
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 本章角色弧光台阶" not in user_text

    def test_format_arc_beats_unit(self) -> None:
        # 单元级:空 dict / 全空白 value → "";非空 → banner + 各 value + 禁旁白。
        from src.core.agents.writer import _format_arc_beats

        assert _format_arc_beats(_make_card(arc_beats={})) == ""
        # 全空白 value 视同空(过滤)
        assert _format_arc_beats(_make_card(arc_beats={"x": "   "})) == ""
        rendered = _format_arc_beats(
            _make_card(
                arc_beats={
                    "chen-mo": "陈默第一次违抗沈舟的指令,独自留下断后,转身时没回头。",
                    "lin-shu-ying": "林疏影删掉了对自己有利的证词。",
                }
            )
        )
        assert "## 本章角色弧光台阶" in rendered
        assert "陈默第一次违抗" in rendered  # 多台阶都渲染
        assert "林疏影删掉了" in rendered
        assert "绝不允许" in rendered  # 禁旁白声明铁律在位
        assert "❌" in rendered  # ✅/❌ 对照在位


class TestClientInvocation:
    """``client.complete`` 调用形态:一次、两条消息、参数透传。"""

    def test_complete_called_once_with_two_messages(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        assert mock_client.complete.call_count == 1
        messages = mock_client.complete.call_args.kwargs["messages"]
        assert len(messages) == 2
        assert messages[0].role == "system"
        assert messages[1].role == "user"
        assert all(isinstance(m, Message) for m in messages)

    def test_default_generation_params(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        kwargs = mock_client.complete.call_args.kwargs
        assert kwargs["model"] == "claude-sonnet-4-6"
        assert kwargs["max_tokens"] == 8000
        assert kwargs["temperature"] == 0.85

    def test_caller_can_override_generation_params(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-opus-4-7",
            max_tokens=12000,
            temperature=0.6,
            prompts_dir=fake_prompts_dir,
        )
        kwargs = mock_client.complete.call_args.kwargs
        assert kwargs["model"] == "claude-opus-4-7"
        assert kwargs["max_tokens"] == 12000
        assert kwargs["temperature"] == 0.6


class TestReturnValue:
    """``WriterOutput`` 字段透传 ``LLMResponse``。"""

    def test_returns_writer_output_with_response_fields(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            content="实际正文",
            model_used="claude-sonnet-4-6-20260101",
            input_tokens=999,
            output_tokens=8888,
            finish_reason="end_turn",
        )
        result = write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        assert isinstance(result, WriterOutput)
        assert result.content == "实际正文"
        assert result.model_used == "claude-sonnet-4-6-20260101"
        assert result.token_usage.input_tokens == 999
        assert result.token_usage.output_tokens == 8888
        assert result.token_usage.total_tokens == 999 + 8888
        assert result.finish_reason == "end_turn"

    def test_finish_reason_passthrough_none(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(finish_reason=None)
        result = write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        assert result.finish_reason is None


class TestEdgeCases:
    """边界:空记忆、首章空衔接桥、空必达列表。"""

    def test_empty_memory_section_does_not_break(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        # 空记忆时不输出 ## 背景信息 段
        assert "## 背景信息" not in user_text

    def test_blank_memory_section_treated_as_empty(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="   \n\n  ",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 背景信息" not in user_text

    def test_first_chapter_empty_continuity_tail(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        card = _make_card(
            continuity_context=_make_continuity(
                prev_chapter_tail="",
                unfinished_actions=[],
                pov_from=None,
                scene_continuation="switch",
            )
        )
        write_chapter(
            client=mock_client,
            chapter_card=card,
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        # 首章兜底文案出现,代码不抛错
        assert "首章" in user_text or "无可衔接的前文" in user_text

    def test_empty_must_achieve_renders_as_none_marker(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        card = _make_card(must_achieve=[], must_unresolved=[], forbidden=[])
        write_chapter(
            client=mock_client,
            chapter_card=card,
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        # 三组列表都空时,各段下应有"无"占位,不裸露空标题
        assert (
            user_text.count("\n无") >= 3
            or user_text.count("无\n") >= 3
            or user_text.count("无") >= 3
        )


class TestPromptsDirInjection:
    """``prompts_dir`` 注入路径:显式传 / 默认走 settings.prompts_dir。"""

    def test_explicit_prompts_dir_overrides_settings(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # 把 settings.prompts_dir 指向另一个目录(不存在的);显式参数应覆盖。
        monkeypatch.setattr(writer_module.settings, "prompts_dir", tmp_path / "ignored")
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        system_text = mock_client.complete.call_args.kwargs["messages"][0].content
        assert "FAKE_WRITER_SYSTEM_PROMPT" in system_text

    def test_default_prompts_dir_uses_settings(
        self,
        mock_client: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 把 settings.prompts_dir 指向可控的临时目录,放一份 marker 模板。
        custom_dir = tmp_path / "settings-prompts"
        custom_dir.mkdir()
        (custom_dir / "writer.md").write_text("FROM_SETTINGS_DEFAULT", encoding="utf-8")
        monkeypatch.setattr(writer_module.settings, "prompts_dir", custom_dir)

        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            # 不传 prompts_dir,走 settings 默认
        )
        system_text = mock_client.complete.call_args.kwargs["messages"][0].content
        assert "FROM_SETTINGS_DEFAULT" in system_text


class TestWriterOutputModel:
    """``WriterOutput`` Pydantic 模型自身校验。"""

    def test_writer_output_total_tokens_property_via_token_usage(self) -> None:
        out = WriterOutput(
            content="x",
            model_used="m",
            token_usage=TokenUsage(input_tokens=3, output_tokens=4),
        )
        assert out.token_usage.total_tokens == 7

    def test_writer_output_finish_reason_optional(self) -> None:
        out = WriterOutput(
            content="x",
            model_used="m",
            token_usage=TokenUsage(input_tokens=0, output_tokens=0),
        )
        assert out.finish_reason is None


# ---- P2-B-10:retriever 注入路径 + voice_profile 注入路径 -------------------


def _make_voice_profile(**overrides) -> VoiceProfile:
    payload = dict(
        catchphrases=["剑势如山"],
        tone="冷峻克制",
        sentence_style="短句直白",
        self_address="我",
        addresses_others={"苏离": "苏小姐"},
        forbidden_phrases=["哎呀", "哈哈哈"],
    )
    payload.update(overrides)
    return VoiceProfile(**payload)


def _make_character(
    *, character_id: str = "林衍", name: str = "林衍", **overrides
) -> Character:
    payload: dict = dict(
        character_id=character_id,
        workspace_id="ws-demo",
        name=name,
        aliases=[],
        archetype="寒门主角",
        appearance="青衣配剑",
        personality="沉静内敛",
        background="山门弃徒",
        voice_profile=_make_voice_profile(),
        goals_long_term=["重夺剑宗"],
        goals_short_term=["破阵"],
        current_state={},
        relationships={},
        abilities=[],
        secrets=[],
        first_appearance_chapter="ch-0001",
        last_updated_chapter="ch-0002",
    )
    payload.update(overrides)
    return Character(**payload)


def _make_query_hit(
    *,
    id: str,
    document: str,
    metadata: dict | None = None,
    distance: float = 0.1,
) -> QueryHit:
    return QueryHit(
        id=id,
        document=document,
        metadata=metadata if metadata is not None else {},
        distance=distance,
    )


class TestRetrievedMemorySection:
    """P2-B-10:``vector_store`` 注入路径下的"## 检索记忆"段。"""

    def test_no_section_when_vector_store_none(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """``vector_store=None`` → user message 不附检索记忆段(P1 路径)。"""
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 检索记忆" not in user_text

    def test_section_appears_with_fact_and_character_hits(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """``vector_store`` 非空 + 两条 where 都有命中 → 段含两子标题 + 命中文本。"""
        vs = MagicMock(spec=VectorStore)
        vs.query.side_effect = [
            # fact where 命中
            [_make_query_hit(id="fact-FA01", document="青崖山阵眼以玉佩为核")],
            # character where 命中
            [
                _make_query_hit(
                    id="character-林衍",
                    document="林衍 | 寒门主角 | 沉静内敛 | 山门弃徒",
                )
            ],
            # v1.1 wave 3 s6(E1):chapter_summary where 命中
            [
                _make_query_hit(
                    id="chapter_summary-ch-0003",
                    document="林衍夺回玉佩,识破长老布局 | 玉佩归位",
                )
            ],
        ]
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            vector_store=vs,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 检索记忆" in user_text
        assert "### fact(关键事实)" in user_text
        assert "青崖山阵眼以玉佩为核" in user_text
        assert "### character(出场角色档案)" in user_text
        assert "林衍 | 寒门主角" in user_text
        # v1.1 wave 3 s6(E1):chapter_summary 维度块 + 命中文本
        assert "### chapter_summary(相关历史章节摘要)" in user_text
        assert "林衍夺回玉佩,识破长老布局" in user_text

    def test_section_passes_correct_where_clauses(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """两次 ``vs.query`` 分别用 fact / character + $in entity_id where 子句。

        P6-B-01 起 retriever 默认合并 ``{"revoked": {"$ne": True}}`` 过滤
        软删除条目;writer 透传 caller where 经 retriever ``_merge_revoked_filter``
        包裹成 ``{"$and": [caller_where, revoked_filter]}``。"""
        vs = MagicMock(spec=VectorStore)
        vs.query.return_value = []
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            vector_store=vs,
        )
        # v1.1 wave 3 s6(E1):三条 query 调用,where 子句分别针对 fact /
        # character + $in / chapter_summary,每条都包裹 revoked filter $and 子句。
        # v1.1 wave 4 s1(G1):fact + chapter_summary 额外合并 chapter_seq < 本章
        # 全局序(ch-0002 → 2)时序过滤;character 维度**不过滤**(保留最新档案)。
        assert vs.query.call_count == 3
        first_where = vs.query.call_args_list[0].kwargs["where"]
        second_where = vs.query.call_args_list[1].kwargs["where"]
        third_where = vs.query.call_args_list[2].kwargs["where"]
        # 第 1 条:fact + chapter_seq 时序过滤,再与 revoked filter 合并
        assert first_where == {
            "$and": [
                {
                    "$and": [
                        {"entity_type": "fact"},
                        {"chapter_seq": {"$lt": 2}},
                    ]
                },
                {"revoked": {"$ne": True}},
            ]
        }
        # 第 2 条:character(无 chapter_seq 过滤)— caller 已是 $and(character
        # + $in entity_id)→ 与 revoked 再嵌一层 $and
        assert "$and" in second_where
        outer_sub = second_where["$and"]
        # outer_sub[0] 是 caller 原 where(含内层 $and),outer_sub[1] 是 revoked
        assert outer_sub[1] == {"revoked": {"$ne": True}}
        caller_inner = outer_sub[0]
        assert "$and" in caller_inner
        inner_sub = caller_inner["$and"]
        assert {"entity_type": "character"} in inner_sub
        in_clause = next(c for c in inner_sub if "entity_id" in c)
        assert set(in_clause["entity_id"]["$in"]) == {"林衍", "苏离"}
        # character where 不含 chapter_seq(G1 刻意不时序过滤角色最新档案)
        assert "chapter_seq" not in str(second_where)
        # 第 3 条(E1+G1):chapter_summary + chapter_seq 时序过滤,再与 revoked 合并
        assert third_where == {
            "$and": [
                {
                    "$and": [
                        {"entity_type": "chapter_summary"},
                        {"chapter_seq": {"$lt": 2}},
                    ]
                },
                {"revoked": {"$ne": True}},
            ]
        }

    def test_section_skips_character_query_when_related_empty(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """``related_characters=[]`` → 跳过 character query;仍发 fact + chapter_summary。

        P6-B-01 起 fact where 与 revoked filter 以 ``$and`` 合并。v1.1 wave 3 s6
        (E1):chapter_summary 维度不依赖 related_characters,故 related 空时仍发
        (fact + chapter_summary)两条,只跳过 character 那条。"""
        vs = MagicMock(spec=VectorStore)
        vs.query.return_value = []
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(related_characters=[]),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            vector_store=vs,
        )
        # fact + chapter_summary 两次 query(跳过 character)
        assert vs.query.call_count == 2
        wheres = [c.kwargs["where"] for c in vs.query.call_args_list]
        # v1.1 wave 4 s1(G1):fact / chapter_summary 都合并 chapter_seq < 2 时序过滤
        assert {
            "$and": [
                {
                    "$and": [
                        {"entity_type": "fact"},
                        {"chapter_seq": {"$lt": 2}},
                    ]
                },
                {"revoked": {"$ne": True}},
            ]
        } in wheres
        assert {
            "$and": [
                {
                    "$and": [
                        {"entity_type": "chapter_summary"},
                        {"chapter_seq": {"$lt": 2}},
                    ]
                },
                {"revoked": {"$ne": True}},
            ]
        } in wheres
        # character 维度被跳过(无 entity_type=character 的 where)
        assert not any(
            "character" in str(w) and "chapter_summary" not in str(w) for w in wheres
        )

    def test_section_renders_placeholder_when_both_empty(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """两条 where 命中都为空 → 段落仍输出但写"暂无相关检索"(显式告诉 LLM)。"""
        vs = MagicMock(spec=VectorStore)
        vs.query.return_value = []
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            vector_store=vs,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 检索记忆" in user_text
        assert "暂无相关检索" in user_text

    def test_top_k_default_and_override(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """``retriever_top_k`` 默认 10;调用方可显式覆盖。"""
        vs = MagicMock(spec=VectorStore)
        vs.query.return_value = []
        # 默认
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            vector_store=vs,
        )
        assert vs.query.call_args_list[0].kwargs["top_k"] == 10
        # 覆盖
        vs.query.reset_mock()
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            vector_store=vs,
            retriever_top_k=5,
        )
        assert vs.query.call_args_list[0].kwargs["top_k"] == 5


class TestVoiceProfileSection:
    """P2-B-10:``db_session`` 注入路径下 system message 末尾的角色声音档案段。"""

    def test_no_section_when_db_session_none(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """``db_session=None`` → system 不附 voice 段(P1 路径)。"""
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        system_text = mock_client.complete.call_args.kwargs["messages"][0].content
        assert "## 出场角色声音档案" not in system_text

    def test_section_appended_to_system_with_voice_fields(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``db_session`` 非空 → system 末尾附 voice 段;六字段全展开。"""
        characters = {
            "林衍": _make_character(character_id="林衍", name="林衍"),
            "苏离": _make_character(
                character_id="苏离",
                name="苏离",
                voice_profile=_make_voice_profile(
                    catchphrases=[],
                    tone="清冷飘逸",
                    sentence_style="长句修饰",
                    self_address="本宫",
                    addresses_others={},
                    forbidden_phrases=["俗气"],
                ),
            ),
        }
        monkeypatch.setattr(
            writer_module,
            "get_character",
            lambda session, cid: characters.get(cid),
        )
        fake_session = MagicMock(spec=Session)
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            db_session=fake_session,
        )
        system_text = mock_client.complete.call_args.kwargs["messages"][0].content
        # 段标题与两个角色子标题
        assert "## 出场角色声音档案" in system_text
        assert "### 林衍(林衍)" in system_text
        assert "### 苏离(苏离)" in system_text
        # 林衍 6 字段
        assert "tone(语气):冷峻克制" in system_text
        assert "sentence_style(句式):短句直白" in system_text
        assert "self_address(自称):我" in system_text
        assert "addresses_others(对他人称呼):苏离→苏小姐" in system_text
        assert "catchphrases(口头禅):剑势如山" in system_text
        assert "forbidden_phrases(禁止用语):哎呀, 哈哈哈" in system_text
        # 苏离的空集合渲染成"无"
        assert "self_address(自称):本宫" in system_text
        # FAKE_WRITER_SYSTEM_PROMPT 模板内容仍在 system 起始处
        assert system_text.startswith("FAKE_WRITER_SYSTEM_PROMPT")

    def test_section_skipped_when_related_empty(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``related_characters=[]`` → voice 段不附(voice section 不查 db)。

        v1.1 wave 4 s1(G1):``related_characters=[]`` 时 voice section 仍跳过
        (本测试断言点);但 G1 角色截止状态段会按 ``pov_character`` 查 db,故
        get_character 不再是"完全不调用"——本测试聚焦 voice 段不附,不再断言
        get_character 零调用(G1 后 pov 状态查询是预期行为)。
        """
        get_character_mock = MagicMock(return_value=None)
        monkeypatch.setattr(writer_module, "get_character", get_character_mock)
        fake_session = MagicMock(spec=Session)
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(related_characters=[]),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            db_session=fake_session,
        )
        system_text = mock_client.complete.call_args.kwargs["messages"][0].content
        assert "## 出场角色声音档案" not in system_text

    def test_section_skipped_when_all_characters_missing(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """所有 ``related_characters`` 都查不到 → 不附 voice 段(等同 P1 路径)。"""
        monkeypatch.setattr(writer_module, "get_character", lambda session, cid: None)
        fake_session = MagicMock(spec=Session)
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            db_session=fake_session,
        )
        system_text = mock_client.complete.call_args.kwargs["messages"][0].content
        assert "## 出场角色声音档案" not in system_text

    def test_section_skips_missing_characters_individually(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """部分查到 / 部分缺失 → 跳过缺失,只渲染查到的角色。"""
        characters = {"林衍": _make_character(character_id="林衍", name="林衍")}
        monkeypatch.setattr(
            writer_module,
            "get_character",
            lambda session, cid: characters.get(cid),
        )
        fake_session = MagicMock(spec=Session)
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),  # related_characters=["林衍", "苏离"]
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            db_session=fake_session,
        )
        system_text = mock_client.complete.call_args.kwargs["messages"][0].content
        assert "### 林衍(林衍)" in system_text
        # 苏离查不到,不应出现 voice 子标题
        assert "### 苏离" not in system_text


class TestPersonalitySection:
    """v2 w2-s5:db_session 注入路径下 system message 的"性格底色"段(主角缺陷透传全章)。

    治 wiring 缺口:主角缺陷(characters.personality)此前只在 arc_beats 章经 arc_beats
    间接到 writer;本段每章注入出场角色 personality(含缺陷底色),普通章也立得住。
    """

    def test_no_section_when_db_session_none(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """db_session=None → system 不附性格底色段(零回归 P1 路径)。"""
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        system_text = mock_client.complete.call_args.kwargs["messages"][0].content
        assert "## 出场角色性格与可用动机" not in system_text

    def test_section_appended_with_flaw_personality(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """db_session 非空 + character 有 personality → 段附 + 缺陷底色逐字进 system。"""
        flaw = "用绝对理性掩饰对失控的病态恐惧,最该果断时因怕失控而退缩、误判"
        characters = {
            "林衍": _make_character(character_id="林衍", name="林衍", personality=flaw),
        }
        monkeypatch.setattr(
            writer_module, "get_character", lambda session, cid: characters.get(cid)
        )
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(related_characters=["林衍"]),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            db_session=MagicMock(spec=Session),
        )
        system_text = mock_client.complete.call_args.kwargs["messages"][0].content
        assert "## 出场角色性格与可用动机" in system_text
        assert "### 林衍" in system_text
        assert flaw in system_text  # 缺陷底色逐字进 writer system message(治 wiring)
        assert "当前短期目标:破阵" in system_text

    def test_section_skipped_when_related_empty(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """related_characters=[] → 性格底色段不附。"""
        monkeypatch.setattr(
            writer_module, "get_character", MagicMock(return_value=None)
        )
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(related_characters=[]),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            db_session=MagicMock(spec=Session),
        )
        system_text = mock_client.complete.call_args.kwargs["messages"][0].content
        assert "## 出场角色性格与可用动机" not in system_text

    def test_section_skipped_when_all_character_material_blank(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """外貌、背景、能力、性格、目标与关系全空 → 不附空材料段。"""
        characters = {
            "林衍": _make_character(
                character_id="林衍",
                name="林衍",
                appearance="   ",
                background="   ",
                abilities=[],
                personality="   ",
                goals_long_term=[],
                goals_short_term=[],
                relationships={},
            ),
        }
        monkeypatch.setattr(
            writer_module, "get_character", lambda session, cid: characters.get(cid)
        )
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(related_characters=["林衍"]),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            db_session=MagicMock(spec=Session),
        )
        system_text = mock_client.complete.call_args.kwargs["messages"][0].content
        assert "## 出场角色性格与可用动机" not in system_text

    def test_capsule_keeps_only_current_cast_relationships(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        characters = {
            "林衍": _make_character(
                character_id="林衍",
                name="林衍",
                goals_short_term=["保住苏离的退路", "拿到阵图", "无关第三目标"],
                goals_long_term=["偿还当年没有伸手救人的亏欠"],
                relationships={"苏离": "欠她一条命", "玄鸟": "旧友"},
            ),
            "苏离": _make_character(character_id="苏离", name="苏离"),
        }
        monkeypatch.setattr(
            writer_module, "get_character", lambda session, cid: characters.get(cid)
        )
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(related_characters=["苏离"]),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            db_session=MagicMock(spec=Session),
        )
        system_text = mock_client.complete.call_args.kwargs["messages"][0].content
        assert "当前短期目标:保住苏离的退路；拿到阵图" in system_text
        assert "本章出场关系:苏离:欠她一条命" in system_text
        # 相关人物已由上游选定，不再用目标位置替模型判断其是否有用。
        assert "无关第三目标" in system_text
        assert "长期动机（上游 motivation）:偿还当年没有伸手救人的亏欠" in system_text
        assert "玄鸟" not in system_text

    def test_selected_character_material_is_complete_before_request_budget(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """相关人物、第三项目标和句尾原因不能被局部长度预算静默丢掉。"""
        character_ids = [f"char-{index}" for index in range(5)]
        characters = {
            character_id: _make_character(
                character_id=character_id,
                name=f"角色{index}",
                personality="性格" * 300 + f"底色末尾{index}",
                goals_short_term=["目标" * 200, "顾虑" * 200, "第三目标"],
                goals_long_term=["动机" * 150 + f"原因末尾{index}"],
                relationships={
                    other_id: "关系" * 200
                    for other_id in character_ids
                    if other_id != character_id
                },
            )
            for index, character_id in enumerate(character_ids)
        }
        monkeypatch.setattr(
            writer_module, "get_character", lambda session, cid: characters.get(cid)
        )
        section = writer_module._build_personality_section(
            db_session=MagicMock(spec=Session),
            related_characters=character_ids,
        )
        assert len(section) > 3200
        assert "第三目标" in section
        assert "……" not in section
        for index in range(5):
            assert f"底色末尾{index}" in section
            assert f"原因末尾{index}" in section
        for character in characters.values():
            assert character.personality in section
            assert all(value in section for value in character.relationships.values())


class TestRelationshipLockSection:
    """W3.5:writer user message 注入本章相关人物关系硬锁。"""

    def test_user_message_includes_pov_relationship_chain(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """POV 的卡外强关系链要进入 writer prompt。"""
        characters = {
            "林衍": _make_character(
                character_id="林衍",
                name="林衍",
                relationships={"沈确": "直属上司,负责审阅他的调查报告"},
            ),
            "苏离": _make_character(character_id="苏离", name="苏离"),
            "沈确": _make_character(character_id="沈确", name="沈确"),
        }
        monkeypatch.setattr(
            writer_module,
            "get_character",
            lambda session, cid: characters.get(cid),
        )
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(related_characters=["苏离"]),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            db_session=MagicMock(spec=Session),
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 角色关系硬锁" in user_text
        assert "- 林衍 与 沈确:直属上司,负责审阅他的调查报告" in user_text

    def test_user_message_skips_empty_relationships(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """空关系不产生 writer 关系段。"""
        characters = {
            "林衍": _make_character(character_id="林衍", name="林衍"),
            "苏离": _make_character(character_id="苏离", name="苏离"),
        }
        monkeypatch.setattr(
            writer_module,
            "get_character",
            lambda session, cid: characters.get(cid),
        )
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(related_characters=["苏离"]),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            db_session=MagicMock(spec=Session),
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 角色关系硬锁" not in user_text

    def test_related_external_relationship_does_not_pollute_prompt(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """非 POV related 角色的卡外关系不污染正文上下文。"""
        characters = {
            "林衍": _make_character(
                character_id="林衍",
                name="林衍",
                relationships={"沈确": "直属上司"},
            ),
            "苏离": _make_character(
                character_id="苏离",
                name="苏离",
                relationships={"玄鸟": "幼年旧友,本章不相关"},
            ),
            "沈确": _make_character(character_id="沈确", name="沈确"),
            "玄鸟": _make_character(character_id="玄鸟", name="玄鸟"),
        }
        monkeypatch.setattr(
            writer_module,
            "get_character",
            lambda session, cid: characters.get(cid),
        )
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(related_characters=["苏离"]),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            db_session=MagicMock(spec=Session),
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "- 林衍 与 沈确:直属上司" in user_text
        assert "玄鸟" not in user_text
        assert "幼年旧友" not in user_text


class TestRetrievalAndVoiceCombined:
    """P2-B-10:同时注入 ``vector_store`` + ``db_session`` 时两段共存。"""

    def test_both_sections_present_when_both_injected(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 准备 retriever / db 双注入
        vs = MagicMock(spec=VectorStore)
        vs.query.side_effect = [
            [_make_query_hit(id="fact-1", document="阵眼以玉佩为核")],
            [_make_query_hit(id="character-林衍", document="林衍 | 寒门主角")],
            # v1.1 wave 3 s6(E1):chapter_summary 第三条 query(可空)
            [],
        ]
        characters = {"林衍": _make_character(character_id="林衍", name="林衍")}
        monkeypatch.setattr(
            writer_module,
            "get_character",
            lambda session, cid: characters.get(cid),
        )
        fake_session = MagicMock(spec=Session)
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="文件式记忆段",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            vector_store=vs,
            db_session=fake_session,
        )
        system_text = mock_client.complete.call_args.kwargs["messages"][0].content
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        # voice 段在 system,检索段在 user
        assert "## 出场角色声音档案" in system_text
        assert "### 林衍(林衍)" in system_text
        assert "## 检索记忆" in user_text
        assert "## 背景信息" in user_text  # 文件式记忆段仍存在
        # 顺序:user message 中 ## 背景信息 在 ## 检索记忆 之前
        assert user_text.index("## 背景信息") < user_text.index("## 检索记忆")
        # 顺序:user message 中 ## 检索记忆 在 ## 章末钩子 之前
        assert user_text.index("## 检索记忆") < user_text.index("## 章末钩子")


# =====================================================================
# v1.1 wave 4 session 1 / G1:角色截止状态段单测
# =====================================================================


class TestCharacterStateSection:
    """``## 角色截止状态`` 段(SQL state_timeline,绕开 embedding,防断臂复活)。"""

    def test_section_rendered_when_prior_state_exists(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """出场角色有截止上一章的状态 → user message 附'## 角色截止状态'段。"""
        character = _make_character(
            character_id="林衍",
            name="林衍",
            state_timeline=[
                CharacterStateSnapshot(
                    as_of_chapter=9,
                    physical_state="右臂截断",
                    injuries=["右臂截肢"],
                    acquired_items=["断魂刀"],
                )
            ],
        )
        monkeypatch.setattr(
            writer_module,
            "get_character",
            lambda session, cid: character if cid == "林衍" else None,
        )
        write_chapter(
            client=mock_client,
            # ch-0010 → 全局序 10,截止第 9 章状态命中
            chapter_card=_make_card(chapter_id="ch-0010", related_characters=["林衍"]),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            db_session=MagicMock(spec=Session),
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 角色截止状态" in user_text
        assert "右臂截肢" in user_text
        assert "断魂刀" in user_text
        assert "(截止第 9 章)" in user_text
        assert "派生记忆" in user_text
        assert "若本消息包含权威世界契约" in user_text
        assert "优先级低于该契约" in user_text
        assert "不自动证明其永久或不可逆" in user_text

    def test_section_skipped_when_no_prior_state(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """角色无历史状态(state_timeline 空)→ 不附该段。"""
        character = _make_character(character_id="林衍", name="林衍", state_timeline=[])
        monkeypatch.setattr(
            writer_module,
            "get_character",
            lambda session, cid: character if cid == "林衍" else None,
        )
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(chapter_id="ch-0010", related_characters=["林衍"]),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            db_session=MagicMock(spec=Session),
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 角色截止状态" not in user_text

    def test_section_skipped_first_chapter(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """首章(全局序 1)→ 即使角色有 as_of_chapter>=1 的快照也不渲染(无截止上一章)。"""
        character = _make_character(
            character_id="林衍",
            name="林衍",
            state_timeline=[CharacterStateSnapshot(as_of_chapter=1, injuries=["x"])],
        )
        monkeypatch.setattr(
            writer_module,
            "get_character",
            lambda session, cid: character if cid == "林衍" else None,
        )
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(chapter_id="ch-0001", related_characters=["林衍"]),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            db_session=MagicMock(spec=Session),
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 角色截止状态" not in user_text

    def test_section_skipped_when_no_db_session(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """``db_session=None``(P1 路径)→ 不附该段。"""
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(chapter_id="ch-0010"),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 角色截止状态" not in user_text


# =====================================================================
# v1.1 wave 1 session 7 / G11:rewrite mode 字数收敛指导段单测
# =====================================================================


def _make_v1_fingerprint(
    *,
    paragraph_length_mean: float = 56.0,
    sentence_length_mean: float = 26.0,
    simile_density: float = 0.100,
    third_dominant: bool = True,
) -> dict[str, float]:
    """构造一份典型的 v1 fingerprint dict(17 字段),用于 rewrite mode 测试。

    third_dominant=True → 第三人称代词主导;False → 第一人称代词主导。
    """
    fp: dict[str, float] = {
        "paragraph_length_mean": paragraph_length_mean,
        "paragraph_length_std": 12.0,
        "sentence_length_mean": sentence_length_mean,
        "sentence_length_std": 8.0,
        "simile_density": simile_density,
        "exclamation_density": 0.05,
        "total_chars": 2353.0,
        "total_sentences": 90.0,
    }
    if third_dominant:
        fp.update(
            {
                "pronoun_freq_他": 0.030,
                "pronoun_freq_她": 0.020,
                "pronoun_freq_它": 0.001,
                "pronoun_freq_他们": 0.002,
                "pronoun_freq_她们": 0.001,
                "pronoun_freq_它们": 0.000,
                "pronoun_freq_我": 0.001,
                "pronoun_freq_我们": 0.000,
            }
        )
    else:
        fp.update(
            {
                "pronoun_freq_他": 0.001,
                "pronoun_freq_她": 0.001,
                "pronoun_freq_它": 0.000,
                "pronoun_freq_他们": 0.000,
                "pronoun_freq_她们": 0.000,
                "pronoun_freq_它们": 0.000,
                "pronoun_freq_我": 0.040,
                "pronoun_freq_我们": 0.005,
            }
        )
    return fp


def _make_validator_failures_below_min() -> list[dict]:
    """构造 luo-cloud-test ch-21 / luo-clean-v1 ch-7 现场的 word_count below min 失败。"""
    return [
        {
            "name": "word_count",
            "details": {
                "actual_word_count": 2353,
                "word_count_min": 2500,
                "word_count_max": 3500,
                "reason": "below_min",
            },
        }
    ]


class TestIdentitySection:
    """A3(v1.1 w6-s2):``## 出场角色身份档案`` 段(SQL identity_facets 固定注入)。"""

    def test_section_rendered_for_multi_facet_character(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """出场角色有 identity_facets → user message 附'## 出场角色身份档案'段。"""
        character = _make_character(
            character_id="shen",
            name="沈舟",
            identity_facets=[
                IdentityFacet(context="现实", identity="心理医生"),
                IdentityFacet(context="梦境", identity="卧底锦衣卫"),
            ],
        )
        monkeypatch.setattr(
            writer_module,
            "get_character",
            lambda session, cid: character if cid == "shen" else None,
        )
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(chapter_id="ch-0010", related_characters=["shen"]),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            db_session=MagicMock(spec=Session),
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 出场角色身份档案" in user_text
        assert "沈舟" in user_text
        assert "现实=心理医生" in user_text
        assert "梦境=卧底锦衣卫" in user_text

    def test_section_skipped_for_simple_character(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """普通角色(无 identity_facets)→ 不附该段(普通故事不受影响)。"""
        character = _make_character(
            character_id="林衍", name="林衍", identity_facets=[]
        )
        monkeypatch.setattr(
            writer_module,
            "get_character",
            lambda session, cid: character if cid == "林衍" else None,
        )
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(chapter_id="ch-0010", related_characters=["林衍"]),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            db_session=MagicMock(spec=Session),
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 出场角色身份档案" not in user_text

    def test_section_skipped_when_db_session_none(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """db_session=None(P1 路径)→ 不附该段。"""
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(related_characters=["shen"]),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 出场角色身份档案" not in user_text


class TestRewriteModeBaselineSection:
    """v1.1 G11:creation_type='rewrite' 时附 SAME CHAPTER V1 BASELINE 段。"""

    def test_rewrite_mode_includes_baseline_section_header(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            creation_type="rewrite",
            prev_version_fingerprint=_make_v1_fingerprint(),
            validator_failures=_make_validator_failures_below_min(),
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## rewrite mode 字数收敛指导(SAME CHAPTER V1 BASELINE)" in user_text

    def test_rewrite_mode_includes_fingerprint_numbers(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """4 维数字必须精确出现(段落均值 56 / 句长均值 26 / 比喻密度 0.100)。"""
        fp = _make_v1_fingerprint(
            paragraph_length_mean=56.0,
            sentence_length_mean=26.0,
            simile_density=0.100,
        )
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            creation_type="rewrite",
            prev_version_fingerprint=fp,
            validator_failures=_make_validator_failures_below_min(),
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "约 56 字" in user_text
        assert "约 26 字" in user_text
        assert "约 0.100 次/句" in user_text
        assert "比喻密度仅作软性风格参考" in user_text
        assert "偏移只记 WARN，不单独打回" in user_text

    def test_rewrite_mode_includes_dominant_pov_third_person(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            creation_type="rewrite",
            prev_version_fingerprint=_make_v1_fingerprint(third_dominant=True),
            validator_failures=_make_validator_failures_below_min(),
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "third_person(他/她为主)" in user_text

    def test_rewrite_mode_includes_dominant_pov_first_person(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            creation_type="rewrite",
            prev_version_fingerprint=_make_v1_fingerprint(third_dominant=False),
            validator_failures=_make_validator_failures_below_min(),
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "first_person(我为主)" in user_text

    def test_rewrite_mode_renders_validator_failures(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """validator_failures 列表必须渲染 name + details 字面到 user message。"""
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            creation_type="rewrite",
            prev_version_fingerprint=_make_v1_fingerprint(),
            validator_failures=_make_validator_failures_below_min(),
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "失败 1" in user_text
        assert "word_count" in user_text
        assert "actual_word_count" in user_text
        assert "2353" in user_text
        assert "word_count_min" in user_text
        assert "2500" in user_text
        assert "below_min" in user_text

    def test_rewrite_mode_empty_validator_failures_renders_no_validator_text(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """validator_failures=None → 失败明细段渲染"无"+ 解释字面。"""
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            creation_type="rewrite",
            prev_version_fingerprint=_make_v1_fingerprint(),
            validator_failures=None,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "当前 checkpoint 没有可用的 validator 失败明细" in user_text
        assert "不要据此假定字数不足" in user_text

    def test_rewrite_mode_preserves_passed_must_when_forbidden_failed(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        must = "方砚说服阿依古丽取得巡查死角信息"
        forbidden = "直接出现鬼声"
        must_source = "chapter_cards/ch-0002.must_achieve"
        forbidden_source = "chapter_cards/ch-0002.forbidden"
        guardrail = RewriteConstraintGuardrail(
            must_fix=[
                ConstraintResult(
                    constraint_id=stable_constraint_id(
                        source_path=forbidden_source,
                        domain="forbidden",
                        rule_text=forbidden,
                    ),
                    domain="forbidden",
                    source_path=forbidden_source,
                    rule_text=forbidden,
                    status="fail",
                    details="正文证据",
                )
            ],
            must_preserve=[
                ConstraintResult(
                    constraint_id=stable_constraint_id(
                        source_path=must_source,
                        domain="must_achieve",
                        rule_text=must,
                    ),
                    domain="must_achieve",
                    source_path=must_source,
                    rule_text=must,
                    status="pass",
                )
            ],
        )

        write_chapter(
            client=mock_client,
            chapter_card=_make_card(must_achieve=[must], forbidden=[forbidden]),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            creation_type="rewrite",
            prev_version_fingerprint=_make_v1_fingerprint(),
            validator_failures=None,
            rewrite_constraint_guardrail=guardrail,
        )

        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "hard gate 保序表(W3.5-B" in user_text
        assert "本轮 HARD 失败（必须修复；结构化表）" in user_text
        assert "上一轮 HARD 已通过（本轮必须保留；结构化表）" in user_text
        assert "| forbidden | fail |" in user_text
        assert "| must_achieve | pass |" in user_text
        assert must in user_text

    def test_rewrite_mode_preserves_passed_forbidden_when_must_failed(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        must = "方砚说服阿依古丽取得巡查死角信息"
        forbidden = "直接出现鬼声"
        must_source = "chapter_cards/ch-0002.must_achieve"
        forbidden_source = "chapter_cards/ch-0002.forbidden"
        guardrail = RewriteConstraintGuardrail(
            must_fix=[
                ConstraintResult(
                    constraint_id=stable_constraint_id(
                        source_path=must_source,
                        domain="must_achieve",
                        rule_text=must,
                    ),
                    domain="must_achieve",
                    source_path=must_source,
                    rule_text=must,
                    status="fail",
                )
            ],
            must_preserve=[
                ConstraintResult(
                    constraint_id=stable_constraint_id(
                        source_path=forbidden_source,
                        domain="forbidden",
                        rule_text=forbidden,
                    ),
                    domain="forbidden",
                    source_path=forbidden_source,
                    rule_text=forbidden,
                    status="pass",
                )
            ],
        )

        write_chapter(
            client=mock_client,
            chapter_card=_make_card(must_achieve=[must], forbidden=[forbidden]),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            creation_type="rewrite",
            prev_version_fingerprint=_make_v1_fingerprint(),
            validator_failures=None,
            rewrite_constraint_guardrail=guardrail,
        )

        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "| must_achieve | fail |" in user_text
        assert "| forbidden | pass |" in user_text
        assert forbidden in user_text

    def test_world_rule_failure_overrides_conflicting_derived_state(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """权威规则失败必须给出可执行修法，禁止让角色状态反向覆盖世界规则。"""
        rule = "单次观看超时会导致短暂失明。"
        evidence = "视力损伤已固化为永久的黑暗"
        source_path = "data/outline/world.json"
        violation = ConstraintViolation(
            constraint_id=stable_constraint_id(
                source_path=source_path,
                domain="world.unique_rules",
                rule_text=rule,
            ),
            domain="world.unique_rules",
            source_path=source_path,
            source_hash="a" * 64,
            rule_quote=rule,
            evidence_quote=evidence,
            severity="hard",
            suggested_action=(
                "删除或改写该正文证据表达的冲突命题，使其语义与权威规则一致；"
                "不得只换同义词、补充因果或强化原冲突结论。"
            ),
            explanation=(
                "world.unique_rules 是最高优先级事实；章节卡、角色状态、检索记忆和"
                "源正文中的冲突派生事实均不得覆盖它。"
            ),
        )
        guardrail = RewriteConstraintGuardrail(
            constraint_violations=[violation],
        )

        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            creation_type="rewrite",
            prev_version_fingerprint=_make_v1_fingerprint(),
            validator_failures=None,
            rewrite_constraint_guardrail=guardrail,
        )

        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert evidence in user_text
        assert "必须修法" in user_text
        assert "不得只换同义词" in user_text
        assert "权威世界规则修复纪律" in user_text
        assert "冲突部分视为污染" in user_text
        assert "本章仍处于某状态" in user_text

    def test_legacy_world_rule_failure_without_action_still_gets_guardrail(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """旧 checkpoint 的 null action/explanation 也必须获得权威修复纪律。"""
        violation = ConstraintViolation(
            constraint_id=stable_constraint_id(
                source_path="data/outline/world.json",
                domain="world.unique_rules",
                rule_text="单次观看超时会导致短暂失明。",
            ),
            domain="world.unique_rules",
            source_path="data/outline/world.json",
            source_hash="a" * 64,
            rule_quote="单次观看超时会导致短暂失明。",
            evidence_quote="视力损伤已固化为永久的黑暗",
            severity="hard",
        )

        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            creation_type="rewrite",
            prev_version_fingerprint=_make_v1_fingerprint(),
            validator_failures=None,
            rewrite_constraint_guardrail=RewriteConstraintGuardrail(
                constraint_violations=[violation]
            ),
        )

        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "权威世界规则修复纪律" in user_text
        assert "必须删除或改写表中证据表达的冲突命题" in user_text
        assert "只换同义词、补充原因" in user_text

    def test_rewrite_mode_includes_extension_categories(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """扩展方式 4 维必须出现(对话细节 / 内心独白 / 场景描写 / 动作细节)。"""
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            creation_type="rewrite",
            prev_version_fingerprint=_make_v1_fingerprint(),
            validator_failures=_make_validator_failures_below_min(),
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "对话细节" in user_text
        assert "内心独白" in user_text
        assert "场景描写" in user_text
        assert "动作细节" in user_text

    def test_rewrite_mode_includes_counterexamples(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """反例 3 条必须出现(拉长段落 / 大量比喻 / 切人称视角)。"""
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            creation_type="rewrite",
            prev_version_fingerprint=_make_v1_fingerprint(),
            validator_failures=_make_validator_failures_below_min(),
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "不要拉长单个段落" in user_text
        assert "不要加大量新比喻" in user_text
        assert "不要切换人称视角" in user_text

    def test_draft_mode_no_baseline_section(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """creation_type='draft' → 不附 SAME CHAPTER V1 BASELINE 段。"""
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            creation_type="draft",
            prev_version_fingerprint=_make_v1_fingerprint(),  # 即便有 fp,draft 路径仍跳过
            validator_failures=_make_validator_failures_below_min(),
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "SAME CHAPTER V1 BASELINE" not in user_text

    def test_no_creation_type_no_baseline_section(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """creation_type=None(默认)→ 不附 BASELINE 段(零回归 P1/P2 路径)。"""
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "SAME CHAPTER V1 BASELINE" not in user_text

    def test_rewrite_mode_fingerprint_none_fallback(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """creation_type='rewrite' + prev_version_fingerprint=None → 段仍附但 4 维段渲染缺失提示。"""
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            creation_type="rewrite",
            prev_version_fingerprint=None,
            validator_failures=_make_validator_failures_below_min(),
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "SAME CHAPTER V1 BASELINE" in user_text
        assert "v1 fingerprint 缺失" in user_text

    def test_rewrite_mode_section_before_memory_section(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """段顺序:rewrite 收敛指导段在记忆段之前(对齐 _build_user_message docstring)。"""
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="工作区文件式记忆段内容",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            creation_type="rewrite",
            prev_version_fingerprint=_make_v1_fingerprint(),
            validator_failures=_make_validator_failures_below_min(),
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert user_text.index("SAME CHAPTER V1 BASELINE") < user_text.index(
            "## 背景信息"
        )

    def test_rewrite_mode_mixed_pov_label_when_pronouns_balanced(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """双方代词都不超 2.0x 阈值 → mixed 标签(对齐 POV_DOMINANCE_RATIO)。"""
        fp = _make_v1_fingerprint()
        # 让第一/第三人称频次接近(都很高但不超 2.0x 倍率)
        fp["pronoun_freq_他"] = 0.020
        fp["pronoun_freq_她"] = 0.010
        fp["pronoun_freq_他们"] = 0.000
        fp["pronoun_freq_它"] = 0.000
        fp["pronoun_freq_她们"] = 0.000
        fp["pronoun_freq_它们"] = 0.000
        fp["pronoun_freq_我"] = 0.018
        fp["pronoun_freq_我们"] = 0.000
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            creation_type="rewrite",
            prev_version_fingerprint=fp,
            validator_failures=_make_validator_failures_below_min(),
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "mixed(双方都不超阈值" in user_text

    def test_rewrite_mode_empty_fingerprint_pronouns_falls_back_third_person(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """代词频次全 0 → 项目级默认第三人称兜底(对齐 default_voice 硬约束)。"""
        fp = _make_v1_fingerprint()
        for key in list(fp.keys()):
            if key.startswith("pronoun_freq_"):
                fp[key] = 0.0
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            creation_type="rewrite",
            prev_version_fingerprint=fp,
            validator_failures=_make_validator_failures_below_min(),
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "third_person(默认,代词频次极低)" in user_text


class TestInteractionFocusSection:
    """M4''-轴A(v1.1 wave 5 s8):本章互动场景中心段(card.interaction_focus)。

    条件段:card.interaction_focus 非空(松 beat 题材互动中心章)才注入,空时跳过
    (独处留白章 / 紧 beat 题材 / 旧卡 → 逐字节零回归)。命门=配角当真人 + 情感从互动
    演出非旁白(治松 beat"独处内省扎堆")。"""

    def test_interaction_focus_section_present_when_card_has_focus(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # card.interaction_focus 非空 → banner + focus 原文 + 配角真人 + 情感从互动指令都进。
        focus = "本章核心是林晚与陈阿婆一起琢磨无字日记,陈阿婆凭老经验主张别强求,两人温和分歧。"
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(interaction_focus=focus),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 本章互动场景中心" in user_text
        assert focus in user_text
        assert "双向对手戏" in user_text  # 配角当真人指令
        assert "别用主角大段内心独白" in user_text  # 禁独白化(情感靠演非讲)
        # w5-s8 精修:三种"替读者盖章意义"的 tell 写法禁令(治盲读实测 more-tell 副作用)
        assert "替读者盖章意义" in user_text
        assert "别让叙述者跳出来贴标签" in user_text  # 禁主题盖章句
        assert "人生格言金句" in user_text  # 禁配角金句化(智慧藏家常里)
        assert "护治愈基调" in user_text  # 温和摩擦非尖锐冲突

    def test_interaction_focus_section_absent_when_empty(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 护留白 + 零回归:interaction_focus 默认 ""(独处留白章 / 紧 beat / 旧卡)→ 段完全不出现。
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),  # interaction_focus 默认 ""
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 本章互动场景中心" not in user_text

    def test_format_interaction_focus_unit(self) -> None:
        # 单元级:空 / 全空白 → "";非空 → banner + focus + 配角真人 + 护留白。
        from src.core.agents.writer import _format_interaction_focus

        assert _format_interaction_focus(_make_card(interaction_focus="")) == ""
        assert _format_interaction_focus(_make_card(interaction_focus="   ")) == ""
        rendered = _format_interaction_focus(
            _make_card(interaction_focus="林晚与陆知远在河边一起修一把旧椅子。")
        )
        assert "## 本章互动场景中心" in rendered
        assert "有主体性的真人" in rendered
        assert "护留白" in rendered


class TestBreakthroughPayoffSection:
    """模块①(v1.1 wave 5 s10):本章升级可感化段(card.breakthrough_payoff)。

    条件段:非空(境界突破章)才注入升级可感化指令;空时跳过(非突破章 / 非 3 题材 /
    开关 OFF / 旧卡 → 逐字节零回归)。"""

    def test_section_present_when_card_has_payoff(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        payoff = "突破炼气巅峰——展示灵识外放感知翻倍,当场碾压之前险胜的铁甲犀级对手。"
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(breakthrough_payoff=payoff),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 本章升级可感化" in user_text
        assert payoff in user_text
        assert "对比起点给出可感的实力跃迁" in user_text  # 起点→终点 delta
        assert "突破后能做到、之前做不到" in user_text  # 新能力展示
        assert "痛苦 / 异象只作铺垫" in user_text  # 破同质化痛苦模板

    def test_section_absent_when_empty(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 零回归:breakthrough_payoff 默认 ""(非突破章 / 非 3 题材 / OFF / 旧卡)→ 段不出现。
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(),  # breakthrough_payoff 默认 ""
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 本章升级可感化" not in user_text

    def test_format_breakthrough_payoff_unit(self) -> None:
        from src.core.agents.writer import _format_breakthrough_payoff

        assert _format_breakthrough_payoff(_make_card(breakthrough_payoff="")) == ""
        assert _format_breakthrough_payoff(_make_card(breakthrough_payoff="  ")) == ""
        rendered = _format_breakthrough_payoff(
            _make_card(breakthrough_payoff="突破金丹,新能力:御剑飞行。")
        )
        assert "## 本章升级可感化" in rendered
        assert "演出来" in rendered


class TestProgressionSection:
    """模块②(v1.1 wave 5 s10):角色进阶等级段(境界一致性,gated)。

    主力防漂移注入(注入↔写出闭环的'注入'端)。enabled=False(非 3 题材 / 开关 OFF)→
    跳过(逐字节零回归);enabled=True + 角色有 progression_rank → 注入'截止上一章境界 +
    单调规则'。"""

    def _seed_char(self, monkeypatch: pytest.MonkeyPatch, rank: str | None) -> None:
        character = _make_character(
            character_id="萧尘",
            name="萧尘",
            state_timeline=[
                CharacterStateSnapshot(as_of_chapter=8, progression_rank=rank)
            ],
        )
        monkeypatch.setattr(
            writer_module,
            "get_character",
            lambda session, cid: character if cid == "萧尘" else None,
        )

    def test_section_present_when_enabled_and_rank_exists(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._seed_char(monkeypatch, "炼气六层巅峰")
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(chapter_id="ch-0010", related_characters=["萧尘"]),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            db_session=MagicMock(spec=Session),
            progression_consistency_enabled=True,
            progression_label="境界",
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 角色进阶境界" in user_text
        assert "炼气六层巅峰" in user_text
        assert "单调不倒退" in user_text
        assert "倒退必须有交代" in user_text  # 合法倒退(突破失败/被打落等)
        assert "越阶可以、但顺设定" in user_text  # 越级战是爽点

    def test_section_skipped_when_disabled(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 零回归铁律:enabled=False(默认 / 非 3 题材 / 开关 OFF)→ 段完全不出现。
        self._seed_char(monkeypatch, "炼气六层巅峰")
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(chapter_id="ch-0010", related_characters=["萧尘"]),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            db_session=MagicMock(spec=Session),
            # progression_consistency_enabled 默认 False
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 角色进阶" not in user_text

    def test_section_skipped_when_rank_none(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # enabled 但角色无 progression_rank(首章 / 旧数据)→ 跳过(不强行注入空段)。
        self._seed_char(monkeypatch, None)
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(chapter_id="ch-0010", related_characters=["萧尘"]),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            db_session=MagicMock(spec=Session),
            progression_consistency_enabled=True,
            progression_label="境界",
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 角色进阶" not in user_text

    def test_label_genre_specific(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 题材标签:战斗题材 label='战力' → 注入'## 角色进阶战力'(中性字段题材化显示)。
        self._seed_char(monkeypatch, "战力8200")
        write_chapter(
            client=mock_client,
            chapter_card=_make_card(chapter_id="ch-0010", related_characters=["萧尘"]),
            memory_section="",
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
            db_session=MagicMock(spec=Session),
            progression_consistency_enabled=True,
            progression_label="战力",
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "## 角色进阶战力" in user_text
        assert "战力8200" in user_text


class TestVoiceBaselineAnchorSource:
    """v2 w2-s2 注入端:``_format_voice_baseline`` 按 ``anchor_source`` 渲染。

    防回退:① OFF 臂(prev_chapter,默认)字面逐字不变 — A/B 控制组零回归;
    ② ON 臂(book_anchor)段名/来源标注诚实(prompt 不撒谎)。
    """

    def _summary(self, **overrides) -> "writer_module.PovSummary":
        from src.core.rules.style_fingerprint import PovSummary

        payload = dict(
            dominant_pov="third_person",
            paragraph_length_mean=71.0,
            sentence_length_mean=31.0,
            simile_density=0.11,
        )
        payload.update(overrides)
        return PovSummary(**payload)

    def test_default_prev_chapter_renders_legacy_literal(self) -> None:
        text = writer_module._format_voice_baseline(self._summary())
        assert "PREV CHAPTER VOICE BASELINE" in text
        assert "BOOK VOICE ANCHOR" not in text
        assert "约 71 字" in text
        assert "段落均值用于跨章 WARN 观察" in text
        assert "SAME CHAPTER V1 BASELINE" in text
        assert "健康 v1 段均 / 句均" in text
        assert "异常维与比喻密度仍为 WARN-only" in text
        assert "pov_change` 只记录跨章主视角角色变化" in text
        assert "`pov_change=true` 显式切换时例外" not in text

    def test_book_anchor_renders_honest_source_labels(self) -> None:
        text = writer_module._format_voice_baseline(
            self._summary(anchor_source="book_anchor")
        )
        assert "BOOK VOICE ANCHOR" in text
        assert "PREV CHAPTER VOICE BASELINE" not in text
        # 数字来源逐项诚实标注:段均/句均=全书开篇锚,POV/比喻=前一章
        assert "全书开篇锚" in text
        assert "dominant_pov,来自前一章" in text
        assert "simile_density,来自前一章" in text
        # 拉回基线的核心指令字面(漂移治理语义)
        assert "锚定的是全书开篇基线" in text
        assert "跨章段均偏移只记 WARN" in text
        assert "健康 v1 段均 / 句均" in text
        assert "异常维与比喻密度仍为 WARN-only" in text
        assert "pov_change` 只记录跨章主视角角色变化" in text
        assert "`pov_change=true` 显式切换时例外" not in text

    def test_r9_ch2_initial_prompt_does_not_invent_rewrite_hard_ceiling(self) -> None:
        """R9 ch-0002:前章 62 是初稿参考，不得预造 rewrite 才适用的 93 HARD。"""
        text = writer_module._format_voice_baseline(
            self._summary(paragraph_length_mean=62.296875)
        )

        assert "paragraph_length_mean):约 62 字" in text
        assert "段落均值用于跨章 WARN 观察" in text
        assert "绝对不得高于 93.0 字" not in text
        assert "初稿段落节奏执行目标" not in text

    def test_user_message_carries_book_anchor_section(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """write_chapter 透传链:PovSummary(book_anchor) → user message 含
        BOOK VOICE ANCHOR 段(透传环防回退,防"机制两头好、中间环静默断")。"""
        write_chapter(
            client=mock_client,
            model="claude-sonnet-4-6",
            chapter_card=_make_card(),
            memory_section="",
            prompts_dir=fake_prompts_dir,
            prev_chapter_voice_summary=self._summary(
                anchor_source="book_anchor",
                paragraph_length_mean=70.8,
                sentence_length_mean=30.9,
            ),
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "BOOK VOICE ANCHOR" in user_text
        assert "约 71 字" in user_text  # 70.8 四舍五入,数字真来自全书锚
        assert "跨章段均偏移只记 WARN" in user_text
        assert "初稿段落节奏执行目标" not in user_text

    def test_structured_soft_watchlist_is_injected_before_rewrite_feedback(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        watchlist = (
            "## 已批准前文 WARN（SOFT WATCHLIST；绝不触发打回）\n\n"
            "这不是 failed_constraints，不得升级为 HARD。"
        )
        write_chapter(
            client=mock_client,
            model="claude-sonnet-4-6",
            chapter_card=_make_card(),
            memory_section="",
            prompts_dir=fake_prompts_dir,
            creation_type="rewrite",
            prev_version_fingerprint=_make_v1_fingerprint(),
            warning_watchlist_section=watchlist,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content

        assert watchlist in user_text
        assert user_text.index("SOFT WATCHLIST") < user_text.index(
            "## rewrite mode 字数收敛指导"
        )

    def test_draft_receives_all_applicable_hard_gates_from_one_contract(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        write_chapter(
            client=mock_client,
            model="claude-sonnet-4-6",
            chapter_card=_make_card(),
            memory_section="",
            prompts_dir=fake_prompts_dir,
            creation_type="draft",
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "本轮正文约束合同(WRITER/VALIDATOR SSOT)" in user_text
        assert "当前阶段:`draft`" in user_text
        assert "2800 <= actual_word_count <= 4500" in user_text
        assert "`paragraph_mean_floor`" in user_text
        assert "paragraph_length_mean >= 30" in user_text
        assert "初稿尚无 v1，不得预造 rewrite 保序尺" in user_text
        assert "跨章段均/句均参考" in user_text
        assert "不得把它们临时升级成 HARD" in user_text

    def test_every_planned_hard_is_explicit_before_rewrite_provider_call(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """11 类正文 HARD 都必须先逐条进入 Writer 请求，Judge 才能据此打回。"""

        card = _make_card()
        setting_contract = SettingContract(
            setting="架空大陆",
            power_system="阵法通常由多人协作。",
            unique_rules="青铜门只在铜印贴近锁面时开启，无任何豁免。",
            source_path="data/outline/world.json",
            source_hash="a" * 64,
        )
        plan = compile_executable_constraint_plan(
            chapter_card=card,
            stage="rewrite",
            setting_contract=setting_contract,
            character_records=[_make_character()],
            expected_voice="third_person",
            rewrite_style=RewriteStyleConstraintSpec(
                paragraph_baseline=60,
                paragraph_threshold=0.5,
                paragraph_hard=True,
                sentence_baseline=24,
                sentence_threshold=0.3,
                sentence_hard=True,
            ),
        )

        write_chapter(
            client=mock_client,
            model="claude-sonnet-4-6",
            chapter_card=card,
            memory_section="",
            setting_contract=setting_contract,
            prompts_dir=fake_prompts_dir,
            creation_type="rewrite",
            prev_version_fingerprint=_make_v1_fingerprint(
                paragraph_length_mean=60,
                sentence_length_mean=24,
            ),
            executable_constraint_plan=plan,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content

        assert {item.kind for item in plan.constraints} == {
            "word_count",
            "paragraph_mean_floor",
            "must_achieve",
            "must_unresolved",
            "forbidden",
            "continuity_unfinished_actions",
            "continuity_scene_same",
            "setting_unique_rules",
            "voice_forbidden",
            "cross_chapter_voice",
            "style_drift",
        }
        assert all(item.constraint_id in user_text for item in plan.constraints)
        assert all(item.rule_text in user_text for item in plan.constraints)
        assert plan.plan_hash in user_text

    def test_rewrite_uses_same_chapter_preservation_scale_without_initial_fake_gate(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """rewrite 只对健康 v1 使用保序尺，初稿段不伪造该硬闸。"""
        write_chapter(
            client=mock_client,
            model="claude-sonnet-4-6",
            chapter_card=_make_card(),
            memory_section="",
            prompts_dir=fake_prompts_dir,
            creation_type="rewrite",
            prev_chapter_voice_summary=self._summary(),
            prev_version_fingerprint=_make_v1_fingerprint(),
            validator_failures=None,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "PREV CHAPTER VOICE BASELINE" in user_text
        assert "初稿段落节奏执行目标" not in user_text
        assert "SAME CHAPTER V1 BASELINE" in user_text
        assert "段落均值 [HARD] 绝对允许区间" in user_text
        assert "当前阶段:`rewrite`" in user_text
        assert "只对动态 `SAME CHAPTER V1 BASELINE`" in user_text
