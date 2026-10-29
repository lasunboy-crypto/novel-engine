"""合并测试:Workspace + ChapterCard + Chapter + Review 全部 P1 范围内 Pydantic 模型。

按模型分组(每个 class 一个模型),便于文件长起来后定位。
每模型覆盖:合法构造 / 缺字段抛错 / 字段越界抛错(最少 3 条)。
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.core.models.chapter import Chapter, ChapterVersion
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.character import Character, VoiceProfile
from src.core.models.plot_thread import PlotThread
from src.core.models.review import ReviewIssue, ReviewResult
from src.core.models.timeline_entry import TimelineEntry
from src.core.models.workspace import Workspace


# ---------- 测试构造器 ----------
# 用最小合法 payload 工厂避免每个用例重复样板。

UTC_NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


def _workspace_payload(**overrides) -> dict:
    base = {
        "workspace_id": "ws_demo",
        "display_name": "示例工作区",
        "genre": "scifi",
        "premise": "一句话设定",
        "created_at": UTC_NOW,
        "last_active_at": UTC_NOW,
        "engine_version": "0.1.0",
        "status": "active",
        "total_chapters": 100,
        "completed_chapters": 0,
        "config_overrides": {},
    }
    base.update(overrides)
    return base


def _continuity_payload(**overrides) -> dict:
    base = {
        "prev_chapter_tail": "上一章末尾示例文本……",
        "unfinished_actions": ["剑还悬在半空"],
        "time_gap": "立即续接",
        "scene_continuation": "same",
        "pov_change": False,
        "pov_from": "李白",
        "pov_to": "李白",
    }
    base.update(overrides)
    return base


def _chapter_card_payload(**overrides) -> dict:
    base = {
        "chapter_id": "ch_001",
        "workspace_id": "ws_demo",
        "volume_id": "v1",
        "chapter_number": 1,
        "title": "开局",
        "tension_level": 5,
        "must_achieve": ["介绍主角"],
        "must_unresolved": ["主角身世"],
        "forbidden": ["金手指外挂"],
        "pov_character": "李白",
        "setting": "长安",
        "continuity_context": ContinuityContext(**_continuity_payload()),
        "next_chapter_seed": "下一章引子",
        "arc_phase": "opening",
        "is_arc_finale": False,
        "related_plot_threads": ["pt_main"],
        "related_characters": ["李白"],
    }
    base.update(overrides)
    return base


def _chapter_version_payload(**overrides) -> dict:
    base = {
        "version_id": 1,
        "content": "正文示例",
        "word_count": 4,
        "created_at": UTC_NOW,
        "creation_type": "draft",
        "model_used": "claude-sonnet-4-6",
        "review_score": None,
        "notes": "",
    }
    base.update(overrides)
    return base


def _chapter_payload(**overrides) -> dict:
    card = ChapterCard(**_chapter_card_payload())
    version = ChapterVersion(**_chapter_version_payload())
    base = {
        "chapter_id": "ch_001",
        "workspace_id": "ws_demo",
        "card": card,
        "current_version": 1,
        "versions": [version],
        "status": "drafting",
        "review_history": [],
        "created_at": UTC_NOW,
        "approved_at": None,
        "total_token_usage": {},
        "total_cost_usd": 0.0,
    }
    base.update(overrides)
    return base


def _voice_profile_payload(**overrides) -> dict:
    base = {
        "tone": "冷峻",
        "sentence_style": "短句直白,鲜用修饰",
        "self_address": "我",
        "catchphrases": ["此乃天意"],
        "addresses_others": {"char_002": "兄台"},
        "forbidden_phrases": ["哎呀"],
    }
    base.update(overrides)
    return base


def _character_payload(**overrides) -> dict:
    base = {
        "character_id": "char_001",
        "workspace_id": "ws_demo",
        "name": "李白",
        "aliases": ["青莲居士"],
        "archetype": "落魄诗仙",
        "appearance": "白衣胜雪,鬓边一缕银丝",
        "personality": "豪放洒脱,内里孤傲",
        "background": "本是仙界谪仙,堕入凡尘",
        "voice_profile": VoiceProfile(**_voice_profile_payload()),
        "goals_long_term": ["重返仙界"],
        "goals_short_term": ["在长安站稳脚跟"],
        "current_state": {"mood": "平静", "location": "长安"},
        "relationships": {"char_002": "故友"},
        "abilities": ["剑术", "诗赋"],
        "secrets": ["真实身份是谪仙"],
        "first_appearance_chapter": "ch_001",
        "last_updated_chapter": "ch_001",
    }
    base.update(overrides)
    return base


def _plot_thread_payload(**overrides) -> dict:
    base = {
        "thread_id": "pt_001",
        "workspace_id": "ws_demo",
        "title": "李白真实身份",
        "status": "planted",
        "importance": 4,
        "planted_chapter": "ch_001",
        "planned_payoff": "ch_050",
        "actual_payoff": None,
        "description": "李白是谪仙的暗示从首章酒馆出现",
        "related_characters": ["char_001"],
        "created_at": UTC_NOW,
        "last_updated_chapter": "ch_001",
    }
    base.update(overrides)
    return base


def _timeline_entry_payload(**overrides) -> dict:
    base = {
        "entry_id": "tl_001",
        "workspace_id": "ws_demo",
        "chapter_id": "ch_001",
        "description": "李白入长安城",
        "time_label": "建元三年春",
        "time_order": 100,
        "involved_characters": ["char_001"],
    }
    base.update(overrides)
    return base


def _review_issue_payload(**overrides) -> dict:
    base = {
        "paragraph_anchor": "李白持剑站在长安城门前的雨中思",
        "issue_type": "voice",
        "description": "口吻不像李白",
        "suggestion": "改成更洒脱的语气",
    }
    base.update(overrides)
    return base


def _review_result_payload(**overrides) -> dict:
    base = {
        "review_id": "rv_001",
        "chapter_id": "ch_001",
        "version_reviewed": 1,
        "reviewed_at": UTC_NOW,
        "reviewer_model": "claude-sonnet-4-6",
        "score_plot_progression": 7.5,
        "score_tension_match": 7.0,
        "score_prose_quality": 8.0,
        "score_consistency": 7.5,
        "score_hook_management": 7.0,
        "score_voice_consistency": 7.0,
        "score_overall": 7.3,
        "hard_constraints_passed": True,
        "failed_constraints": [],
        "issues": [],
        "suggestions": [],
        "verdict": "pass",
        "revise_targets": [],
    }
    base.update(overrides)
    return base


# ---------- Workspace ----------


class TestWorkspace:
    def test_legal_construction(self):
        ws = Workspace(**_workspace_payload())
        assert ws.workspace_id == "ws_demo"
        assert ws.status == "active"
        assert ws.config_overrides == {}

    def test_missing_required_field_raises(self):
        payload = _workspace_payload()
        del payload["workspace_id"]
        with pytest.raises(ValidationError):
            Workspace(**payload)

    def test_status_out_of_range_raises(self):
        with pytest.raises(ValidationError):
            Workspace(**_workspace_payload(status="deleted"))

    @pytest.mark.parametrize(
        "bad_id",
        [
            "../foo",  # path traversal: 父目录跳出
            "a/b",  # 包含路径分隔符
            "..",  # 纯父目录
            ".hidden",  # 含点号(非英数下划线连字符)
            "ws.demo",  # 任意 dotted workspace 仍不获准
            "ws demo",  # 含空格
            "ws@demo",  # 含特殊符号
            "",  # 空串
            "x" * 65,  # 超长(>64)
        ],
    )
    def test_workspace_id_invalid_charset_raises(self, bad_id):
        """P2-B-05:workspace_id 不在 ``[a-zA-Z0-9_-]{1,64}`` 内必须 422 ValidationError。"""

        with pytest.raises(ValidationError):
            Workspace(**_workspace_payload(workspace_id=bad_id))

    @pytest.mark.parametrize(
        "good_id",
        [
            "ws_demo",  # 历史最常用形态(下划线)
            "scifi_book_1",  # 路由集成测试沿用形态
            "fantasy-book",  # 连字符也允许
            "WS123",  # 大小写混合
            "x",  # 单字符(下限 1)
            "x" * 64,  # 64 字符(上限 64)
        ],
    )
    def test_workspace_id_valid_charset_passes(self, good_id):
        """P2-B-05:历史合法 workspace_id 不退化。"""

        ws = Workspace(**_workspace_payload(workspace_id=good_id))
        assert ws.workspace_id == good_id

    def test_reserved_qwen38_selection_workspace_id_passes(self):
        workspace_id = "post-v8-abc-qwen3.8-27b-selection-v1"

        ws = Workspace(**_workspace_payload(workspace_id=workspace_id))

        assert ws.workspace_id == workspace_id


# ---------- ContinuityContext ----------


class TestContinuityContext:
    def test_legal_construction(self):
        cc = ContinuityContext(**_continuity_payload())
        assert cc.scene_continuation == "same"
        assert cc.pov_change is False

    def test_missing_pov_to_raises(self):
        payload = _continuity_payload()
        del payload["pov_to"]
        with pytest.raises(ValidationError):
            ContinuityContext(**payload)

    def test_scene_continuation_out_of_range_raises(self):
        with pytest.raises(ValidationError):
            ContinuityContext(**_continuity_payload(scene_continuation="rewind"))


# ---------- ChapterCard ----------


class TestChapterCard:
    def test_legal_construction_with_defaults(self):
        card = ChapterCard(**_chapter_card_payload())
        # v1.0.x wave 6 s2.6d(2026-05-26)起默认 2500/4000(原 2800/4500)
        assert card.word_count_min == 2500
        assert card.word_count_max == 4000
        assert "draft_strategy" not in card.model_dump()
        assert card.max_revises == 2

    @pytest.mark.parametrize("legacy", ["single", "parallel_2", "parallel_3"])
    def test_legacy_storage_strategy_is_read_and_folded(self, legacy: str):
        payload = _chapter_card_payload()
        payload["draft_strategy"] = legacy
        card = ChapterCard.model_validate_storage(payload)
        assert "draft_strategy" not in card.model_dump()

    @pytest.mark.parametrize("retired", ["single", "parallel_2", "parallel_3"])
    def test_new_input_rejects_retired_strategy_key(self, retired: str):
        payload = _chapter_card_payload()
        payload["draft_strategy"] = retired
        with pytest.raises(ValidationError, match="已退休"):
            ChapterCard.model_validate(payload)

    def test_legacy_storage_rejects_unknown_strategy(self):
        payload = _chapter_card_payload()
        payload["draft_strategy"] = "parallel_4"
        with pytest.raises(ValidationError, match="历史 ChapterCard"):
            ChapterCard.model_validate_storage(payload)

    def test_tension_level_above_range_raises(self):
        with pytest.raises(ValidationError):
            ChapterCard(**_chapter_card_payload(tension_level=11))

    def test_missing_chapter_id_raises(self):
        payload = _chapter_card_payload()
        del payload["chapter_id"]
        with pytest.raises(ValidationError):
            ChapterCard(**payload)

    def test_arc_phase_out_of_range_raises(self):
        with pytest.raises(ValidationError):
            ChapterCard(**_chapter_card_payload(arc_phase="prologue"))

    @pytest.mark.parametrize(
        "bad_id",
        [
            "../ch_001",
            "ch/001",
            "..",
            "ch 001",
            "",
            "x" * 65,
        ],
    )
    def test_chapter_id_invalid_charset_raises(self, bad_id):
        """P2-B-05:chapter_id 与 workspace_id 同字符集白名单(同样会落盘)。"""

        with pytest.raises(ValidationError):
            ChapterCard(**_chapter_card_payload(chapter_id=bad_id))

    @pytest.mark.parametrize(
        "good_id",
        [
            "ch_001",  # 历史最常用形态
            "ch-vol1-001",  # 含连字符
            "CH123",  # 大小写
            "x",  # 单字符
        ],
    )
    def test_chapter_id_valid_charset_passes(self, good_id):
        """P2-B-05:历史合法 chapter_id 不退化。"""

        card = ChapterCard(**_chapter_card_payload(chapter_id=good_id))
        assert card.chapter_id == good_id


# ---------- ChapterVersion ----------


class TestChapterVersion:
    def test_legal_construction(self):
        v = ChapterVersion(**_chapter_version_payload())
        assert v.version_id == 1
        assert v.creation_type == "draft"
        assert v.review_score is None

    def test_creation_type_out_of_range_raises(self):
        with pytest.raises(ValidationError):
            ChapterVersion(**_chapter_version_payload(creation_type="auto_polish"))

    def test_missing_version_id_raises(self):
        payload = _chapter_version_payload()
        del payload["version_id"]
        with pytest.raises(ValidationError):
            ChapterVersion(**payload)


# ---------- Chapter ----------


class TestChapter:
    def test_legal_construction(self):
        ch = Chapter(**_chapter_payload())
        assert ch.chapter_id == "ch_001"
        assert ch.status == "drafting"
        assert ch.rewrite_count == 0
        assert ch.revise_count == 0
        assert len(ch.versions) == 1

    def test_status_out_of_range_raises(self):
        with pytest.raises(ValidationError):
            Chapter(**_chapter_payload(status="abandoned"))

    def test_missing_card_raises(self):
        payload = _chapter_payload()
        del payload["card"]
        with pytest.raises(ValidationError):
            Chapter(**payload)


# ---------- VoiceProfile ----------


class TestVoiceProfile:
    def test_legal_construction_with_defaults(self):
        vp = VoiceProfile(
            tone="冷峻",
            sentence_style="短句直白",
            self_address="我",
        )
        assert vp.catchphrases == []
        assert vp.addresses_others == {}
        assert vp.forbidden_phrases == []

    def test_legal_construction_full(self):
        vp = VoiceProfile(**_voice_profile_payload())
        assert vp.catchphrases == ["此乃天意"]
        assert vp.addresses_others == {"char_002": "兄台"}
        assert vp.forbidden_phrases == ["哎呀"]

    def test_missing_tone_raises(self):
        payload = _voice_profile_payload()
        del payload["tone"]
        with pytest.raises(ValidationError):
            VoiceProfile(**payload)


# ---------- Character ----------


class TestCharacter:
    def test_legal_construction(self):
        ch = Character(**_character_payload())
        assert ch.character_id == "char_001"
        assert ch.workspace_id == "ws_demo"
        assert ch.aliases == ["青莲居士"]
        assert ch.voice_profile.tone == "冷峻"
        assert ch.first_appearance_chapter == "ch_001"

    def test_optional_fields_default_when_absent(self):
        payload = _character_payload()
        for key in (
            "aliases",
            "current_state",
            "relationships",
            "abilities",
            "secrets",
            "first_appearance_chapter",
            "last_updated_chapter",
        ):
            payload.pop(key, None)
        ch = Character(**payload)
        assert ch.aliases == []
        assert ch.current_state == {}
        assert ch.relationships == {}
        assert ch.abilities == []
        assert ch.secrets == []
        assert ch.first_appearance_chapter is None
        assert ch.last_updated_chapter is None

    def test_missing_workspace_id_raises(self):
        payload = _character_payload()
        del payload["workspace_id"]
        with pytest.raises(ValidationError):
            Character(**payload)

    def test_missing_voice_profile_raises(self):
        payload = _character_payload()
        del payload["voice_profile"]
        with pytest.raises(ValidationError):
            Character(**payload)

    def test_missing_goals_long_term_raises(self):
        payload = _character_payload()
        del payload["goals_long_term"]
        with pytest.raises(ValidationError):
            Character(**payload)


# ---------- PlotThread ----------


class TestPlotThread:
    def test_legal_construction(self):
        pt = PlotThread(**_plot_thread_payload())
        assert pt.thread_id == "pt_001"
        assert pt.workspace_id == "ws_demo"
        assert pt.status == "planted"
        assert pt.importance == 4
        assert pt.actual_payoff is None
        assert pt.related_characters == ["char_001"]

    def test_optional_fields_default_when_absent(self):
        payload = _plot_thread_payload()
        for key in (
            "planned_payoff",
            "actual_payoff",
            "related_characters",
            "last_updated_chapter",
        ):
            payload.pop(key, None)
        pt = PlotThread(**payload)
        assert pt.planned_payoff is None
        assert pt.actual_payoff is None
        assert pt.related_characters == []
        assert pt.last_updated_chapter is None

    def test_status_out_of_range_raises(self):
        with pytest.raises(ValidationError):
            PlotThread(**_plot_thread_payload(status="closed"))

    def test_importance_below_range_raises(self):
        with pytest.raises(ValidationError):
            PlotThread(**_plot_thread_payload(importance=0))

    def test_importance_above_range_raises(self):
        with pytest.raises(ValidationError):
            PlotThread(**_plot_thread_payload(importance=6))

    def test_missing_workspace_id_raises(self):
        payload = _plot_thread_payload()
        del payload["workspace_id"]
        with pytest.raises(ValidationError):
            PlotThread(**payload)


# ---------- TimelineEntry ----------


class TestTimelineEntry:
    def test_legal_construction(self):
        tl = TimelineEntry(**_timeline_entry_payload())
        assert tl.entry_id == "tl_001"
        assert tl.workspace_id == "ws_demo"
        assert tl.chapter_id == "ch_001"
        assert tl.time_label == "建元三年春"
        assert tl.time_order == 100
        assert tl.involved_characters == ["char_001"]

    def test_involved_characters_default_when_absent(self):
        payload = _timeline_entry_payload()
        del payload["involved_characters"]
        tl = TimelineEntry(**payload)
        assert tl.involved_characters == []

    def test_missing_time_order_raises(self):
        payload = _timeline_entry_payload()
        del payload["time_order"]
        with pytest.raises(ValidationError):
            TimelineEntry(**payload)

    def test_missing_chapter_id_raises(self):
        payload = _timeline_entry_payload()
        del payload["chapter_id"]
        with pytest.raises(ValidationError):
            TimelineEntry(**payload)

    def test_time_order_must_be_int(self):
        with pytest.raises(ValidationError):
            TimelineEntry(**_timeline_entry_payload(time_order="一百"))


# ---------- ReviewIssue ----------


class TestReviewIssue:
    def test_legal_construction(self):
        issue = ReviewIssue(**_review_issue_payload())
        assert issue.issue_type == "voice"

    def test_missing_paragraph_anchor_raises(self):
        payload = _review_issue_payload()
        del payload["paragraph_anchor"]
        with pytest.raises(ValidationError):
            ReviewIssue(**payload)


# ---------- ReviewResult ----------


class TestReviewResult:
    def test_legal_construction(self):
        rr = ReviewResult(**_review_result_payload())
        assert rr.verdict == "pass"
        assert rr.hard_constraints_passed is True
        assert rr.issues == []

    def test_verdict_out_of_range_raises(self):
        with pytest.raises(ValidationError):
            ReviewResult(**_review_result_payload(verdict="approve"))

    def test_missing_score_overall_raises(self):
        payload = _review_result_payload()
        del payload["score_overall"]
        with pytest.raises(ValidationError):
            ReviewResult(**payload)
