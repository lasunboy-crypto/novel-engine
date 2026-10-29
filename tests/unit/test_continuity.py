"""衔接桥抽取测试(P1-B-09 简单规则版 + P2-B-12 LLM 路径 + 统一入口分发)。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core import continuity as continuity_module
from src.core.continuity import (
    TAIL_CHARS,
    ContinuityExtractorError,
    build_continuity_context,
    extract_continuity_with_llm,
    extract_from_prev_text,
)
from src.core.agents import _prompt as prompt_module
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.chapter_card import ChapterCard, ContinuityContext


class TestTailExtraction:
    """末尾抽取规则:取末尾 800 字符;短输入直接全文返回。"""

    def test_returns_full_text_when_short(self):
        text = "他握紧剑柄,目光不离对面那道身影。"
        result = extract_from_prev_text(text)
        assert result["prev_chapter_tail"] == text

    def test_returns_last_800_chars_when_long(self):
        # 构造一段超过 800 字符的中文正文,前缀做标记便于断言"被切掉"。
        prefix = "前缀段落。" * 100  # 600 字符
        suffix = "末尾段落。" * 200  # 1200 字符
        text = prefix + suffix
        assert len(text) > TAIL_CHARS

        result = extract_from_prev_text(text)
        assert len(result["prev_chapter_tail"]) == TAIL_CHARS
        assert result["prev_chapter_tail"] == text[-TAIL_CHARS:]
        assert "前缀段落" not in result["prev_chapter_tail"]

    def test_exactly_800_chars_returned_in_full(self):
        text = "甲" * TAIL_CHARS
        assert len(text) == TAIL_CHARS
        result = extract_from_prev_text(text)
        assert result["prev_chapter_tail"] == text
        assert len(result["prev_chapter_tail"]) == TAIL_CHARS

    def test_801_chars_drops_first_char(self):
        text = "X" + "甲" * TAIL_CHARS
        assert len(text) == TAIL_CHARS + 1
        result = extract_from_prev_text(text)
        assert len(result["prev_chapter_tail"]) == TAIL_CHARS
        assert result["prev_chapter_tail"].startswith("甲")
        assert "X" not in result["prev_chapter_tail"]


class TestUnfinishedActions:
    """未完成动作识别:省略号结尾 / 进行时副词。"""

    def test_chinese_ellipsis_suffix_matched(self):
        text = "他举起剑,刃尖在月光下泛出寒意。剑还悬在半空……"
        result = extract_from_prev_text(text)
        assert any("剑还悬在半空" in s for s in result["unfinished_actions"])

    def test_three_dots_suffix_matched(self):
        text = "她转过身,声音几乎听不见。门缓缓合上..."
        result = extract_from_prev_text(text)
        assert any("门缓缓合上" in s for s in result["unfinished_actions"])

    def test_progressive_adverb_zheng(self):
        # 用句号结尾,只靠"正"这个进行时副词命中。
        text = "夜色漫过窗台。他正盯着那张泛黄的信纸。"
        result = extract_from_prev_text(text)
        assert any("他正盯着" in s for s in result["unfinished_actions"])

    def test_progressive_adverb_reng(self):
        text = "鼓声渐止。她仍立在原地未动。"
        result = extract_from_prev_text(text)
        assert any("她仍立在原地" in s for s in result["unfinished_actions"])

    def test_no_match_returns_empty_list(self):
        # 全是已完成动作 + 句号收束,无任何省略号或进行时副词。
        text = "他喝下最后一口酒。把杯子放回桌上。然后离开了。"
        result = extract_from_prev_text(text)
        assert result["unfinished_actions"] == []

    def test_only_last_three_sentences_scanned(self):
        # 把含未完成信号的句子放在很靠前位置,期望被窗口忽略。
        text = (
            "他还盯着那扇门。"  # 进行时,但远离末尾
            "走廊很长。"
            "灯光昏黄。"
            "脚步声越来越近。"
            "门把手转动了一下,然后停住。"
        )
        result = extract_from_prev_text(text)
        assert not any("他还盯着那扇门" in s for s in result["unfinished_actions"])

    def test_multiple_candidates_in_window(self):
        text = "他握剑的手仍在颤。空气还凝着血腥味。月光下身影久久未动……"
        result = extract_from_prev_text(text)
        # 三句都靠末尾,且都带信号(仍 / 还 / 省略号)。
        assert len(result["unfinished_actions"]) >= 2


class TestBoundary:
    """边界情况:空输入、纯空白、首章场景。"""

    def test_empty_string_returns_empty_fields(self):
        result = extract_from_prev_text("")
        assert result == {"prev_chapter_tail": "", "unfinished_actions": []}

    def test_whitespace_only_returns_empty_fields(self):
        result = extract_from_prev_text("   \n\t  \n")
        assert result == {"prev_chapter_tail": "", "unfinished_actions": []}

    def test_single_short_sentence_no_terminator(self):
        # 没有任何句末标点,也没有信号词,期望 unfinished 为空但 tail 完整保留。
        text = "他站着"
        result = extract_from_prev_text(text)
        assert result["prev_chapter_tail"] == text
        assert result["unfinished_actions"] == []

    def test_return_type_is_dict_with_expected_keys(self):
        result = extract_from_prev_text("一段普通的话。")
        assert isinstance(result, dict)
        assert set(result.keys()) == {"prev_chapter_tail", "unfinished_actions"}


# ---- P2-B-12 LLM 抽取路径 + 统一入口 ----------------------------------------


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
        must_achieve=["林衍突破第一道剑阵"],
        must_unresolved=["阵眼真凶身份"],
        forbidden=["现代科技元素"],
        pov_character="林衍",
        setting="青崖山顶残阵",
        continuity_context=_make_continuity(),
        next_chapter_seed="苏离消失前留下的玉佩在颤动",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=["thread-main"],
        related_characters=["林衍"],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=3,
        max_revises=2,
    )
    payload.update(overrides)
    return ChapterCard(**payload)


def _llm_continuity_payload(**overrides) -> dict:
    payload: dict = dict(
        prev_chapter_tail="月光下他久久未动……",
        unfinished_actions=["剑势悬而未发", "对峙未决"],
        time_gap="立即续接",
        scene_continuation="same",
        pov_change=False,
        pov_from="林衍",
        pov_to="林衍",
    )
    payload.update(overrides)
    return payload


def _make_response(
    *,
    content: str | None = None,
    payload: dict | None = None,
    model_used: str = "claude-sonnet-4-6-20250101",
    finish_reason: str | None = "end_turn",
) -> LLMResponse:
    if content is None:
        content = json.dumps(
            payload if payload is not None else _llm_continuity_payload(),
            ensure_ascii=False,
        )
    return LLMResponse(
        content=content,
        model_used=model_used,
        usage=TokenUsage(input_tokens=1500, output_tokens=200),
        finish_reason=finish_reason,
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    """在临时目录里建一个 continuity_extractor.md,内容可断言。"""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "continuity_extractor.md").write_text(
        "FAKE_CONTINUITY_SYSTEM_PROMPT\n\n用于单测断言的 system 标记。",
        encoding="utf-8",
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _clear_prompt_cache():
    """每个用例清一次 lru_cache,避免不同 fake 模板互相污染。"""
    prompt_module.load_prompt_template.cache_clear()
    yield
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture(autouse=True)
def _reset_strategy_cache():
    """统一入口 ``_resolved_default_strategy`` 缓存 settings 值,
    每用例清一次,确保 monkeypatch 立即生效。"""
    continuity_module._resolved_default_strategy.cache_clear()
    yield
    continuity_module._resolved_default_strategy.cache_clear()


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=LLMClient)
    client.complete.return_value = _make_response()
    return client


class TestExtractContinuityWithLLM:
    """P2-B-12:LLM 抽取路径直接产出完整 ``ContinuityContext``(7 字段)。"""

    def test_returns_continuity_context_aligned_with_schema(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """LLM 给的 7 字段 JSON → Pydantic 校验为 ContinuityContext。"""
        result = extract_continuity_with_llm(
            client=mock_client,
            prev_content="林衍举起剑,刃尖在月光下泛出寒意。",
            current_card=_make_card(),
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        assert isinstance(result, ContinuityContext)
        assert result.prev_chapter_tail == "月光下他久久未动……"
        assert result.unfinished_actions == ["剑势悬而未发", "对峙未决"]
        assert result.time_gap == "立即续接"
        assert result.scene_continuation == "same"
        assert result.pov_change is False
        assert result.pov_from == "林衍"
        assert result.pov_to == "林衍"

    def test_user_message_carries_card_context_and_prev_content(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        prev = "他举起剑,刃尖在月光下泛出寒意。这是关键标记 PREV_TEXT_MARKER。"
        extract_continuity_with_llm(
            client=mock_client,
            prev_content=prev,
            current_card=_make_card(),
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        # 当前章节卡核心字段
        assert "ch-0002" in user_text
        assert "第二章·破阵" in user_text
        assert "林衍" in user_text
        assert "青崖山顶残阵" in user_text
        # 上一章正文 marker
        assert "PREV_TEXT_MARKER" in user_text
        # extractor marker(P1-B-22 协议)
        assert user_text.rstrip().endswith("<!-- agent_role: extractor -->")

    def test_first_chapter_uses_placeholder_for_prev_pov_and_content(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """首章场景:``card.continuity_context.pov_from=None`` + 空正文 →
        user message 用占位符,不让 LLM 误读 ``None`` 字面。"""
        first_card = _make_card(
            continuity_context=_make_continuity(
                pov_from=None, pov_to="林衍", scene_continuation="switch"
            )
        )
        extract_continuity_with_llm(
            client=mock_client,
            prev_content="",
            current_card=first_card,
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "(无,本章为首章)" in user_text
        assert "(本章为首章,无前章正文)" in user_text

    def test_system_message_loaded_from_template(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        extract_continuity_with_llm(
            client=mock_client,
            prev_content="正文",
            current_card=_make_card(),
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        messages = mock_client.complete.call_args.kwargs["messages"]
        assert messages[0].role == "system"
        assert "FAKE_CONTINUITY_SYSTEM_PROMPT" in messages[0].content

    def test_default_generation_params(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        extract_continuity_with_llm(
            client=mock_client,
            prev_content="正文",
            current_card=_make_card(),
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        kwargs = mock_client.complete.call_args.kwargs
        assert kwargs["model"] == "claude-sonnet-4-6"
        assert kwargs["max_tokens"] == 2000
        assert kwargs["temperature"] == 0.2

    def test_default_prompts_dir_uses_settings(
        self,
        mock_client: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        custom_dir = tmp_path / "settings-prompts"
        custom_dir.mkdir()
        (custom_dir / "continuity_extractor.md").write_text(
            "FROM_SETTINGS_DEFAULT", encoding="utf-8"
        )
        monkeypatch.setattr(
            continuity_module.settings, "prompts_dir", custom_dir
        )
        extract_continuity_with_llm(
            client=mock_client,
            prev_content="正文",
            current_card=_make_card(),
            model="claude-sonnet-4-6",
        )
        system_text = mock_client.complete.call_args.kwargs["messages"][0].content
        assert "FROM_SETTINGS_DEFAULT" in system_text


class TestExtractContinuityWithLLMParseFailure:
    """P2-B-12:LLM 输出非 JSON / 缺字段 / 类型错 → ``ContinuityExtractorError``。"""

    def test_non_json_output_raises_error_with_raw(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            content="这不是 JSON,只是闲聊。", model_used="claude-sonnet-fail"
        )
        with pytest.raises(ContinuityExtractorError) as exc_info:
            extract_continuity_with_llm(
                client=mock_client,
                prev_content="正文",
                current_card=_make_card(),
                model="claude-sonnet-4-6",
                prompts_dir=fake_prompts_dir,
            )
        assert exc_info.value.raw_output == "这不是 JSON,只是闲聊。"
        assert exc_info.value.model_used == "claude-sonnet-fail"

    def test_json_missing_field_raises_error(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 缺 pov_to(ContinuityContext 必填字段)
        partial = _llm_continuity_payload()
        del partial["pov_to"]
        mock_client.complete.return_value = _make_response(
            content=json.dumps(partial, ensure_ascii=False)
        )
        with pytest.raises(ContinuityExtractorError):
            extract_continuity_with_llm(
                client=mock_client,
                prev_content="正文",
                current_card=_make_card(),
                model="claude-sonnet-4-6",
                prompts_dir=fake_prompts_dir,
            )

    def test_json_invalid_enum_raises_error(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # scene_continuation 不在枚举集合中
        bad = _llm_continuity_payload(scene_continuation="invalid_mode")
        mock_client.complete.return_value = _make_response(
            content=json.dumps(bad, ensure_ascii=False)
        )
        with pytest.raises(ContinuityExtractorError):
            extract_continuity_with_llm(
                client=mock_client,
                prev_content="正文",
                current_card=_make_card(),
                model="claude-sonnet-4-6",
                prompts_dir=fake_prompts_dir,
            )

    def test_json_in_markdown_fence_is_tolerated(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        body = json.dumps(_llm_continuity_payload(), ensure_ascii=False)
        wrapped = f"```json\n{body}\n```"
        mock_client.complete.return_value = _make_response(content=wrapped)
        result = extract_continuity_with_llm(
            client=mock_client,
            prev_content="正文",
            current_card=_make_card(),
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        assert isinstance(result, ContinuityContext)

    def test_json_with_trailing_html_comment_is_tolerated(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        body = json.dumps(_llm_continuity_payload(), ensure_ascii=False)
        contaminated = f"{body}\n<!-- agent_role: extractor -->"
        mock_client.complete.return_value = _make_response(content=contaminated)
        result = extract_continuity_with_llm(
            client=mock_client,
            prev_content="正文",
            current_card=_make_card(),
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        assert isinstance(result, ContinuityContext)


class TestBuildContinuityContextRulePath:
    """P2-B-12:``strategy='rule'`` 走 P1 简单规则 + 章节卡基底字段融合。"""

    def test_rule_path_extracts_tail_and_overrides_card_fields(self) -> None:
        # 章节卡 continuity_context 已有手填 prev_chapter_tail / unfinished_actions
        # rule 路径应当用 prev_content 抽取覆盖这两个字段,其余字段沿用卡值。
        card = _make_card(
            continuity_context=_make_continuity(
                prev_chapter_tail="<旧手填值,应被覆盖>",
                unfinished_actions=["<旧>"],
                time_gap="数日后",
                scene_continuation="time_jump",
                pov_change=True,
                pov_from="苏离",
                pov_to="林衍",
            )
        )
        prev = "鼓声渐止。她仍立在原地未动……"
        result = build_continuity_context(
            prev_content=prev, current_card=card, strategy="rule"
        )
        # 抽取覆盖
        assert result.prev_chapter_tail == prev  # 短文本,原样返回
        assert any("仍立在原地" in s for s in result.unfinished_actions)
        # 卡值保留
        assert result.time_gap == "数日后"
        assert result.scene_continuation == "time_jump"
        assert result.pov_change is True
        assert result.pov_from == "苏离"
        assert result.pov_to == "林衍"

    def test_rule_path_first_chapter_empty_prev_content(self) -> None:
        card = _make_card(
            continuity_context=_make_continuity(
                prev_chapter_tail="<旧值>",
                unfinished_actions=["<旧>"],
                pov_from=None,
                pov_to="林衍",
                scene_continuation="switch",
                time_gap="无(本章为首章)",
                pov_change=False,
            )
        )
        result = build_continuity_context(
            prev_content="", current_card=card, strategy="rule"
        )
        assert result.prev_chapter_tail == ""
        assert result.unfinished_actions == []
        assert result.pov_from is None
        assert result.pov_to == "林衍"
        assert result.scene_continuation == "switch"

    def test_rule_path_does_not_invoke_client(
        self, mock_client: MagicMock
    ) -> None:
        """rule 路径忽略 client / model 参数,应当不调 ``complete``。"""
        build_continuity_context(
            prev_content="正文",
            current_card=_make_card(),
            strategy="rule",
            client=mock_client,
            model="claude-sonnet-4-6",
        )
        mock_client.complete.assert_not_called()


class TestBuildContinuityContextLLMPath:
    """P2-B-12:``strategy='llm'`` 走 LLM 抽取产出 7 字段。"""

    def test_llm_path_returns_llm_extracted_context(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        result = build_continuity_context(
            prev_content="正文",
            current_card=_make_card(),
            strategy="llm",
            client=mock_client,
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        assert isinstance(result, ContinuityContext)
        # 取 _llm_continuity_payload 默认值
        assert result.prev_chapter_tail == "月光下他久久未动……"
        assert result.unfinished_actions == ["剑势悬而未发", "对峙未决"]
        mock_client.complete.assert_called_once()

    def test_llm_path_missing_client_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="client \\+ model"):
            build_continuity_context(
                prev_content="正文",
                current_card=_make_card(),
                strategy="llm",
                model="claude-sonnet-4-6",
            )

    def test_llm_path_missing_model_raises_value_error(
        self, mock_client: MagicMock
    ) -> None:
        with pytest.raises(ValueError, match="client \\+ model"):
            build_continuity_context(
                prev_content="正文",
                current_card=_make_card(),
                strategy="llm",
                client=mock_client,
            )


class TestBuildContinuityContextDefaultStrategy:
    """P2-B-12:``strategy=None`` 时按 ``settings.continuity_strategy`` 分发。"""

    def test_default_dispatches_to_llm_when_settings_llm(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            continuity_module.settings, "continuity_strategy", "llm"
        )
        continuity_module._resolved_default_strategy.cache_clear()
        result = build_continuity_context(
            prev_content="正文",
            current_card=_make_card(),
            client=mock_client,
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        assert isinstance(result, ContinuityContext)
        mock_client.complete.assert_called_once()

    def test_default_dispatches_to_rule_when_settings_rule(
        self,
        mock_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            continuity_module.settings, "continuity_strategy", "rule"
        )
        continuity_module._resolved_default_strategy.cache_clear()
        result = build_continuity_context(
            prev_content="鼓声渐止。她仍立在原地未动。",
            current_card=_make_card(),
            client=mock_client,  # 应被忽略
            model="claude-sonnet-4-6",  # 应被忽略
        )
        assert result.prev_chapter_tail == "鼓声渐止。她仍立在原地未动。"
        mock_client.complete.assert_not_called()

    def test_explicit_strategy_overrides_settings_default(
        self,
        mock_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # monkeypatch settings 强制 llm,但显式传 strategy='rule' 仍应走 rule。
        monkeypatch.setattr(
            continuity_module.settings, "continuity_strategy", "llm"
        )
        continuity_module._resolved_default_strategy.cache_clear()
        result = build_continuity_context(
            prev_content="正文。",
            current_card=_make_card(),
            strategy="rule",
            client=mock_client,
        )
        assert isinstance(result, ContinuityContext)
        mock_client.complete.assert_not_called()


class TestBuildContinuityContextRulePlusLLMPath:
    """v1.1 K1:``strategy='rule_plus_llm'`` 叠加 —— rule 保字面 tail + LLM 增强软字段。"""

    def _card_with_distinct_base(self) -> ChapterCard:
        # 卡基底 time_gap / scene_continuation / pov_* 与 LLM payload 默认值
        # 故意取不同,便于断言"软字段来自 LLM、确定性字段来自 rule/卡"。
        return _make_card(
            continuity_context=_make_continuity(
                prev_chapter_tail="<旧手填值,应被 rule 覆盖>",
                unfinished_actions=["<旧手填,应被 rule 覆盖>"],
                time_gap="数日后",
                scene_continuation="time_jump",
                pov_change=True,
                pov_from="苏离",
                pov_to="林衍",
            )
        )

    def test_rule_keeps_literal_tail_llm_only_enhances_soft_fields(
        self, fake_prompts_dir: Path
    ) -> None:
        """核心稳定性保证:字面 tail / unfinished / pov_* 来自 rule+卡,
        time_gap / scene_continuation 来自 LLM(LLM 的 tail 被丢弃)。"""
        client = MagicMock(spec=LLMClient)
        # LLM payload 的 tail / pov / unfinished 与最终结果应**无关**,
        # 仅 time_gap='立即续接' + scene_continuation='same' 被采纳。
        client.complete.return_value = _make_response(
            payload=_llm_continuity_payload(
                prev_chapter_tail="月光下他久久未动……",  # 应被丢弃
                unfinished_actions=["LLM 自造动作"],  # 应被丢弃
                time_gap="立即续接",  # 应被采纳
                scene_continuation="same",  # 应被采纳
                pov_change=False,  # 应被丢弃(用卡值)
                pov_from="错误视角",  # 应被丢弃
                pov_to="错误视角",  # 应被丢弃
            )
        )
        prev = "鼓声渐止。她仍立在原地未动……"
        result = build_continuity_context(
            prev_content=prev,
            current_card=self._card_with_distinct_base(),
            strategy="rule_plus_llm",
            client=client,
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        # 字面 tail 来自 rule(短文本原样),**不是** LLM 的 paraphrase
        assert result.prev_chapter_tail == prev
        # unfinished_actions 来自 rule 信号检测,**不是** LLM 自造
        assert any("仍立在原地" in s for s in result.unfinished_actions)
        assert "LLM 自造动作" not in result.unfinished_actions
        # 两个软字段被 LLM 增强(覆盖卡占位)
        assert result.time_gap == "立即续接"
        assert result.scene_continuation == "same"
        # pov_* 来自卡基底,**不是** LLM
        assert result.pov_change is True
        assert result.pov_from == "苏离"
        assert result.pov_to == "林衍"
        client.complete.assert_called_once()

    def test_llm_failure_degrades_soft_fields_to_card_keeps_tail(
        self, fake_prompts_dir: Path
    ) -> None:
        """LLM 抽取失败(非法 JSON)→ time_gap / scene_continuation 退回卡值,
        字面 tail 仍来自 rule,**不冒泡异常**。"""
        bad_client = MagicMock(spec=LLMClient)
        bad_client.complete.return_value = _make_response(content="这不是 JSON")
        prev = "鼓声渐止。她仍立在原地未动……"
        result = build_continuity_context(
            prev_content=prev,
            current_card=self._card_with_distinct_base(),
            strategy="rule_plus_llm",
            client=bad_client,
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        # 字面 tail 保底
        assert result.prev_chapter_tail == prev
        assert any("仍立在原地" in s for s in result.unfinished_actions)
        # 软字段降级回卡基底手填值(非 LLM)
        assert result.time_gap == "数日后"
        assert result.scene_continuation == "time_jump"
        # pov_* 仍是卡值
        assert result.pov_from == "苏离"

    def test_llm_client_exception_degrades_not_bubble(
        self, fake_prompts_dir: Path
    ) -> None:
        """LLM client 抛运行时异常(如网络错)→ 降级保底,不冒泡。"""
        raising_client = MagicMock(spec=LLMClient)
        raising_client.complete.side_effect = RuntimeError("network down")
        prev = "正文末句。"
        result = build_continuity_context(
            prev_content=prev,
            current_card=self._card_with_distinct_base(),
            strategy="rule_plus_llm",
            client=raising_client,
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        assert result.prev_chapter_tail == prev
        assert result.time_gap == "数日后"  # 降级
        assert result.scene_continuation == "time_jump"  # 降级

    def test_missing_router_and_client_raises_value_error(self) -> None:
        """配置错误(router / client+model 均缺)fail loud,区别于运行时降级。"""
        with pytest.raises(ValueError, match="router 或 client \\+ model"):
            build_continuity_context(
                prev_content="正文",
                current_card=_make_card(),
                strategy="rule_plus_llm",
            )

    def test_missing_model_raises_value_error(
        self, mock_client: MagicMock
    ) -> None:
        with pytest.raises(ValueError, match="router 或 client \\+ model"):
            build_continuity_context(
                prev_content="正文",
                current_card=_make_card(),
                strategy="rule_plus_llm",
                client=mock_client,
            )

    def test_default_dispatches_to_rule_plus_llm_when_settings_set(
        self,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``strategy=None`` + settings='rule_plus_llm' → 走叠加路径。"""
        monkeypatch.setattr(
            continuity_module.settings, "continuity_strategy", "rule_plus_llm"
        )
        continuity_module._resolved_default_strategy.cache_clear()
        client = MagicMock(spec=LLMClient)
        client.complete.return_value = _make_response(
            payload=_llm_continuity_payload(
                time_gap="立即续接", scene_continuation="same"
            )
        )
        prev = "鼓声渐止。她仍立在原地未动……"
        result = build_continuity_context(
            prev_content=prev,
            current_card=self._card_with_distinct_base(),
            client=client,
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        assert result.prev_chapter_tail == prev  # rule 字面 tail
        assert result.time_gap == "立即续接"  # LLM 软字段
        client.complete.assert_called_once()


class TestContinuityExtractorErrorClass:
    """P2-B-12:``ContinuityExtractorError`` 自身字段。"""

    def test_exception_carries_raw_output_and_model(self) -> None:
        err = ContinuityExtractorError(
            "bad", raw_output="<原始输出>", model_used="claude-x"
        )
        assert str(err) == "bad"
        assert err.raw_output == "<原始输出>"
        assert err.model_used == "claude-x"
        # 仍是 ValueError 子类,caller 可按 ValueError 兜
        assert isinstance(err, ValueError)
