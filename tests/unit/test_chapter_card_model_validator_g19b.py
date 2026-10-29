"""G19b ChapterCard model_validator 兜底测试(v1.1 wave 2 临时会话 2026-05-28 上提)。

G19b 治本 = forbidden 字段风格污染检测。原 src/core/agents/chapter_card_generator.py
内的早期检测路径只覆盖 generate_chapter_card 调用,wave 4 w4-s9 K2 批量章节卡推理
等绕过 generator 的 caller 不被截获。

本次上提到 ChapterCard.model_validator(mode='after'),任何创建 ChapterCard 实例
的路径(直接拼装 / model_validate dict / ORM 反序列化 / K2 批量 / cascade retry /
manual seed)都自动走 detect_forbidden_style_pollution 净化,**无法绕过**。

本测试为 wave 4 w4-s9 K2 批量路径防御:
- K2 实施期如果新建批量 caller 绕过 generate_chapter_card,本 invariant 仍截获
- 任何动 ChapterCard 模型 / forbidden 字段处理逻辑的 session 不能误删 model_validator
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.models.chapter_card import (
    ChapterCard,
    ContinuityContext,
    FORBIDDEN_STYLE_KEYWORDS,
    detect_forbidden_style_pollution,
)


def _make_continuity() -> ContinuityContext:
    return ContinuityContext(
        prev_chapter_tail="他握紧剑柄,目光不离对面那道身影。",
        unfinished_actions=["剑势悬而未发"],
        time_gap="立即续接",
        scene_continuation="same",
        pov_change=False,
        pov_from="林衍",
        pov_to="林衍",
    )


def _make_card_payload(**overrides) -> dict:
    """G19b model_validator 测试用 ChapterCard payload (forbidden 可由 overrides 改)。"""
    payload: dict = dict(
        chapter_id="ch-0005",
        workspace_id="ws-demo",
        volume_id="v01",
        chapter_number=5,
        title="第五章·迷局",
        tension_level=6,
        word_count_min=2500,
        word_count_max=4000,
        plot_node_budget=3,
        must_achieve=["林衍发现密信", "苏离透露关键线索"],
        must_unresolved=["密信背后主谋"],
        forbidden=["现代科技元素", "提及第三卷剧情"],  # 默认无污染
        pov_character="林衍",
        setting="残阵深处密室",
        continuity_context=_make_continuity(),
        next_chapter_seed="密室角落传出微弱呼吸声",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=["thread-main"],
        related_characters=["林衍", "苏离"],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=3,
        max_revises=2,
    )
    payload.update(overrides)
    return payload


# ===========================================================================
# G19b: helper function 直接调用(原 generator caller 路径)
# ===========================================================================


class TestG19bDetectFunction:
    """detect_forbidden_style_pollution helper 行为(public API,SSOT in chapter_card.py)。"""

    def test_empty_returns_empty(self) -> None:
        assert detect_forbidden_style_pollution([]) == []

    def test_clean_drama_elements_pass(self) -> None:
        result = detect_forbidden_style_pollution(
            ["现代用语", "现代科技产品", "提及结局剧透"]
        )
        assert result == []

    def test_style_pollution_detected(self) -> None:
        result = detect_forbidden_style_pollution(
            ["短句堆砌过密", "现代用语"]  # "短句" 命中
        )
        assert result == ["短句堆砌过密"]

    def test_constants_exposed(self) -> None:
        """FORBIDDEN_STYLE_KEYWORDS 是 public 常量,关键字数 ≥ 50(防误删)。

        历史水平 58 个(2026-05-28 上提时),阈值 50 留 ~15% 余地;低于 50 视为
        严重误删需要 push back review。
        """
        assert isinstance(FORBIDDEN_STYLE_KEYWORDS, tuple)
        assert len(FORBIDDEN_STYLE_KEYWORDS) >= 50, (
            "FORBIDDEN_STYLE_KEYWORDS 关键字数严重少于历史水平 (≥ 50), "
            "可能被误删"
        )


# ===========================================================================
# G19b model_validator: 直接拼装 ChapterCard 实例路径
# ===========================================================================


class TestG19bModelValidatorDirectConstruct:
    """直接 ChapterCard(...) 拼装的 model_validator 兜底契约。"""

    def test_clean_forbidden_passes(self) -> None:
        """无污染 forbidden → ChapterCard 实例创建成功。"""
        payload = _make_card_payload(forbidden=["现代科技元素", "提及第三卷剧情"])
        card = ChapterCard(**payload)
        assert card.forbidden == ["现代科技元素", "提及第三卷剧情"]

    def test_empty_forbidden_passes(self) -> None:
        """空 forbidden 列表 → 成功。"""
        payload = _make_card_payload(forbidden=[])
        card = ChapterCard(**payload)
        assert card.forbidden == []

    def test_style_pollution_raises_validation_error(self) -> None:
        """forbidden 含"短句"风格污染 → ValidationError(K2 批量绕过 generator 防御)。"""
        payload = _make_card_payload(
            forbidden=["现代用语", "短句堆砌过密"]
        )
        with pytest.raises(ValidationError) as exc_info:
            ChapterCard(**payload)
        # 错误信息必须含 G19b 标识 + 污染条目
        msg = str(exc_info.value)
        assert "G19b" in msg
        assert "短句堆砌过密" in msg

    def test_multiple_pollutions_all_reported(self) -> None:
        """多条污染都应被列出(便于 caller 排查)。"""
        payload = _make_card_payload(
            forbidden=["大量比喻", "节奏拖沓", "现代用语"]
        )
        with pytest.raises(ValidationError) as exc_info:
            ChapterCard(**payload)
        msg = str(exc_info.value)
        assert "大量比喻" in msg
        assert "节奏拖沓" in msg

    def test_error_message_guides_fix(self) -> None:
        """错误信息须包含 fix 引导(让 caller 知道如何改)。"""
        payload = _make_card_payload(forbidden=["散文诗式短段"])
        with pytest.raises(ValidationError) as exc_info:
            ChapterCard(**payload)
        msg = str(exc_info.value)
        # 引导含"剧情禁元素"或"writer.md"提示
        assert ("剧情禁元素" in msg) or ("writer.md" in msg) or ("voice_profile" in msg)


# ===========================================================================
# G19b model_validator: model_validate(dict) 路径(K2 批量场景模拟)
# ===========================================================================


class TestG19bModelValidatorDictPath:
    """model_validate(dict) 路径 — K2 批量章节卡推理典型场景。

    K2 实施期可能用 ChapterCard.model_validate(dict) 从 LLM batch 输出反序列化,
    本测验证此路径也走 model_validator(不只是 ChapterCard(**payload) 构造路径)。
    """

    def test_dict_validate_clean_passes(self) -> None:
        payload = _make_card_payload(forbidden=["现代用语"])
        card = ChapterCard.model_validate(payload)
        assert card.forbidden == ["现代用语"]

    def test_dict_validate_polluted_raises(self) -> None:
        """模拟 K2 批量 LLM 输出含风格污染 → model_validate 也拦截。"""
        payload = _make_card_payload(
            forbidden=["现代用语", "短句堆砌"]
        )
        with pytest.raises(ValidationError) as exc_info:
            ChapterCard.model_validate(payload)
        assert "G19b" in str(exc_info.value)

    def test_dict_validate_with_pollution_keyword_variants(self) -> None:
        """各类型关键字命中都触发(随机抽 5 类)。"""
        pollution_cases = [
            "字以上的长句",        # 句长类
            "段落过短",            # 段落类
            "散文诗",              # 排版风格
            "大段堆砌叙述",        # 信息密度
            "修辞密集",            # 修辞密度
        ]
        for polluted_entry in pollution_cases:
            payload = _make_card_payload(
                forbidden=["现代用语", polluted_entry]
            )
            with pytest.raises(ValidationError, match="G19b") as exc_info:
                ChapterCard.model_validate(payload)
            assert polluted_entry in str(exc_info.value), (
                f"污染条目 {polluted_entry!r} 应在错误信息中"
            )


# ===========================================================================
# G19b model_validator invariant: chapter_card_generator.py re-export 契约
# ===========================================================================


class TestG19bGeneratorReExport:
    """chapter_card_generator.py 内 _detect_forbidden_style_pollution / _FORBIDDEN_STYLE_KEYWORDS
    应仍可 import(私有别名 re-export 自 chapter_card.py SSOT)。

    防御:wave 2-5 重构 chapter_card_generator.py 时不能误删 re-export 别名,
    否则 21 个现有 test_forbidden_style_pollution_g19b.py 测试链路断裂。
    """

    def test_generator_private_aliases_resolve(self) -> None:
        from src.core.agents.chapter_card_generator import (
            _detect_forbidden_style_pollution,
            _FORBIDDEN_STYLE_KEYWORDS,
        )
        # 别名指向 chapter_card.py SSOT
        assert _detect_forbidden_style_pollution is detect_forbidden_style_pollution
        assert _FORBIDDEN_STYLE_KEYWORDS is FORBIDDEN_STYLE_KEYWORDS
