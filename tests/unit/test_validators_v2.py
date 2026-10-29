"""``src.core.rules`` P4-B-07 第二批 [HARD] validator 单元测。

覆盖范围:

- ``check_character_existence``(§4.2 第 1 条)
- ``check_timeline_monotonic``(§4.2 第 3 条)
- ``check_death_foreshadowing``(§4.2 第 2 条 P4 简版)
- ``check_voice_forbidden``(§4.2 [新增 HARD])
- ``check_unfinished_actions_continuation``(§4.3 第 1 条)
- ``check_scene_continuation_location``(§4.3 第 2 条)

pipeline dispatch 与 structlog 事件层在 ``test_validators_v2_pipeline``
类中验证(同文件 + capture_logs);Orchestrator 集成 mock 路径已 P4-B-06
实证(``test_validator_pipeline.py``),P4-B-07 不重复。
"""

from __future__ import annotations

import logging

import structlog
from structlog.testing import capture_logs

from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.character import Character, IdentityFacet, VoiceProfile
from src.core.models.timeline_entry import TimelineEntry
from src.core.rules.validator_pipeline import run_validator_pipeline
from src.core.rules.validators import (
    check_character_existence,
    check_death_foreshadowing,
    check_profession_title_blacklist,
    check_scene_continuation_location,
    check_timeline_monotonic,
    check_unfinished_actions_continuation,
    check_voice_forbidden,
)


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def _make_continuity(**overrides) -> ContinuityContext:
    payload = dict(
        prev_chapter_tail="他握紧剑柄。",
        unfinished_actions=[],
        time_gap="立即续接",
        scene_continuation="switch",
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
        word_count_min=1,
        word_count_max=10_000_000,
        plot_node_budget=3,
        must_achieve=[],
        must_unresolved=[],
        forbidden=[],
        pov_character="林衍",
        setting="青崖山顶",
        continuity_context=_make_continuity(),
        next_chapter_seed="玉佩颤动",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=["thread-main"],
        related_characters=["char-lin-yan"],
    )
    payload.update(overrides)
    return ChapterCard(**payload)


def _make_voice(**overrides) -> VoiceProfile:
    payload: dict = dict(
        catchphrases=[],
        tone="冷峻",
        sentence_style="短句直白",
        self_address="我",
        addresses_others={},
        forbidden_phrases=[],
    )
    payload.update(overrides)
    return VoiceProfile(**payload)


def _make_character(
    character_id: str = "char-lin-yan",
    *,
    name: str = "林衍",
    current_state: dict | None = None,
    forbidden_phrases: list[str] | None = None,
    identity_facets: list[IdentityFacet] | None = None,
) -> Character:
    return Character(
        character_id=character_id,
        workspace_id="ws-demo",
        name=name,
        aliases=[],
        archetype="少年剑客",
        appearance="青衣",
        personality="冷静",
        background="出身寒门",
        identity_facets=identity_facets or [],
        voice_profile=_make_voice(forbidden_phrases=forbidden_phrases or []),
        goals_long_term=["重整宗门"],
        goals_short_term=["破阵"],
        current_state=current_state or {},
        relationships={},
        abilities=["剑法"],
        secrets=[],
    )


def _make_timeline_entry(
    entry_id: str,
    time_order: int,
    chapter_id: str = "ch-0001",
    is_flashback: bool = False,
) -> TimelineEntry:
    return TimelineEntry(
        entry_id=entry_id,
        workspace_id="ws-demo",
        chapter_id=chapter_id,
        description=f"event-{entry_id}",
        time_label=f"order-{time_order}",
        time_order=time_order,
        is_flashback=is_flashback,
    )


# ---------------------------------------------------------------------------
# 1. character_existence(§4.2 第 1 条)
# ---------------------------------------------------------------------------


class TestCheckCharacterExistence:
    def test_passes_when_all_related_characters_exist(self) -> None:
        card = _make_card(related_characters=["char-a", "char-b"])
        result = check_character_existence(
            chapter_card=card,
            known_character_ids={"林衍", "char-a", "char-b", "char-c"},
        )
        assert result.passed
        assert result.name == "character_existence"
        assert result.details["missing_character_ids"] == []
        assert result.details["related_characters_total"] == 3

    def test_fails_when_any_related_character_missing(self) -> None:
        card = _make_card(related_characters=["char-a", "char-missing"])
        result = check_character_existence(
            chapter_card=card,
            known_character_ids={"林衍", "char-a"},
        )
        assert not result.passed
        assert result.details["missing_character_ids"] == ["char-missing"]

    def test_passes_when_related_characters_empty_but_pov_exists(self) -> None:
        # 边界:无 related 角色时仍须验证 POV / continuity POV。
        card = _make_card(related_characters=[])
        result = check_character_existence(
            chapter_card=card,
            known_character_ids={"林衍"},
        )
        assert result.passed
        assert result.details["related_characters_total"] == 1

    def test_fails_when_pov_character_missing(self) -> None:
        card = _make_card(related_characters=[])
        result = check_character_existence(
            chapter_card=card,
            known_character_ids=set(),
        )
        assert not result.passed
        assert result.details["missing_character_ids"] == ["林衍"]


# ---------------------------------------------------------------------------
# 2. timeline_monotonic(§4.2 第 3 条)
# ---------------------------------------------------------------------------


class TestCheckTimelineMonotonic:
    def test_passes_for_strictly_increasing_sequence(self) -> None:
        entries = [
            _make_timeline_entry("e1", 10),
            _make_timeline_entry("e2", 20),
            _make_timeline_entry("e3", 30),
        ]
        result = check_timeline_monotonic(timeline_entries=entries)
        assert result.passed
        assert result.name == "timeline_monotonic"
        assert result.details == {"entries_total": 3}

    def test_fails_on_first_inversion(self) -> None:
        # 严格递增违反:e2.time_order (15) < e1.time_order (20)
        entries = [
            _make_timeline_entry("e1", 20),
            _make_timeline_entry("e2", 15),
            _make_timeline_entry("e3", 30),
        ]
        result = check_timeline_monotonic(timeline_entries=entries)
        assert not result.passed
        assert result.details["violation"] == {
            "prev_entry_id": "e1",
            "prev_time_order": 20,
            "next_entry_id": "e2",
            "next_time_order": 15,
        }

    def test_fails_on_equal_time_order_not_strict(self) -> None:
        # 严格递增:相等不算单调
        entries = [
            _make_timeline_entry("e1", 10),
            _make_timeline_entry("e2", 10),
        ]
        result = check_timeline_monotonic(timeline_entries=entries)
        assert not result.passed
        assert result.details["violation"]["prev_time_order"] == 10
        assert result.details["violation"]["next_time_order"] == 10

    def test_passes_on_empty_or_single_entry(self) -> None:
        assert check_timeline_monotonic(timeline_entries=[]).passed
        assert check_timeline_monotonic(
            timeline_entries=[_make_timeline_entry("e1", 1)]
        ).passed

    # -----------------------------------------------------------------------
    # v1.1 wave 1 session 1(E2):is_flashback 跳过校验
    # -----------------------------------------------------------------------

    def test_flashback_entry_skipped_in_monotonic_check(self) -> None:
        """E2 治本下游:is_flashback=True 的 entry 不参与单调校验。

        场景:luo-cloud-test ch-11+ 双时空叙事。主时间线 entry 严格递增,
        中间插入 flashback entry(time_order 较小)→ 旧版会因相邻对 50 vs
        30 误判违反单调,E2 后跳过 flashback → pass。
        """
        entries = [
            _make_timeline_entry("e1-main", 1001, is_flashback=False),
            _make_timeline_entry("e2-flashback-1", 30, is_flashback=True),
            _make_timeline_entry("e3-main", 1002, is_flashback=False),
            _make_timeline_entry("e4-flashback-2", 50, is_flashback=True),
            _make_timeline_entry("e5-main", 1003, is_flashback=False),
        ]
        result = check_timeline_monotonic(timeline_entries=entries)
        assert result.passed, (
            f"flashback entries should be skipped, but validator failed: {result.details}"
        )
        # 跳过 flashback 后只剩 3 个主时间线 entry
        assert result.details["entries_total"] == 3

    def test_multiple_flashback_segments_not_interfere(self) -> None:
        """多个 flashback 段不互相干扰:flashback 段 time_order 之间允许任意关系
        (包括相等 / 反向),只要主时间线单调即可。
        """
        entries = [
            _make_timeline_entry("e1-main", 100, is_flashback=False),
            _make_timeline_entry("e2-fb-day-3", 30, is_flashback=True),
            _make_timeline_entry("e3-fb-day-1", 10, is_flashback=True),
            # flashback 段:30 → 10 反向,但因 is_flashback=True 全跳过,不算违规
            _make_timeline_entry("e4-fb-day-5", 50, is_flashback=True),
            _make_timeline_entry("e5-main", 200, is_flashback=False),
        ]
        result = check_timeline_monotonic(timeline_entries=entries)
        assert result.passed

    def test_real_inversion_still_fails_with_flashback(self) -> None:
        """真乱序仍能 fail:主时间线 entry 反向时不被 flashback 掩盖。"""
        entries = [
            _make_timeline_entry("e1-main", 200, is_flashback=False),
            _make_timeline_entry("e2-fb", 30, is_flashback=True),
            _make_timeline_entry("e3-main-INVERTED", 150, is_flashback=False),
            # 主时间线 e1 (200) → e3 (150) 反向 → 应 fail
        ]
        result = check_timeline_monotonic(timeline_entries=entries)
        assert not result.passed
        # violation 应指向主时间线两 entry,跳过中间 flashback
        assert result.details["violation"]["prev_entry_id"] == "e1-main"
        assert result.details["violation"]["next_entry_id"] == "e3-main-INVERTED"
        assert result.details["violation"]["prev_time_order"] == 200
        assert result.details["violation"]["next_time_order"] == 150

    def test_all_flashback_entries_passes(self) -> None:
        """所有 entry 都是 flashback → 主时间线为空 → 通过(< 2 entries)。"""
        entries = [
            _make_timeline_entry("e1-fb", 100, is_flashback=True),
            _make_timeline_entry("e2-fb", 50, is_flashback=True),
            _make_timeline_entry("e3-fb", 30, is_flashback=True),
        ]
        result = check_timeline_monotonic(timeline_entries=entries)
        assert result.passed
        assert result.details["entries_total"] == 0

    def test_luo_cloud_test_ch11_scenario(self) -> None:
        """luo-cloud-test 2026-05-25 现场重演:ch-11+ 双时空叙事 timeline 不再误判。

        场景:ch-10 主时间线推进到 time_order=1000(arc-1 末);ch-11 进入双时空
        叙事,主时间线继续 time_order=1100,同时插入 2024 年地球时空 flashback
        entry(time_order=2024+小整数)。旧版会因 1100 vs 2024 失败,E2 后跳过
        flashback → pass。
        """
        entries = [
            _make_timeline_entry("ch10-arc1-end", 1000, chapter_id="ch-0010", is_flashback=False),
            _make_timeline_entry("ch11-main-1", 1100, chapter_id="ch-0011", is_flashback=False),
            # 双时空 flashback:2024 年地球时空 entry,time_order 高但是 flashback
            _make_timeline_entry("ch11-earth-2024", 2024_01, chapter_id="ch-0011", is_flashback=True),
            _make_timeline_entry("ch11-main-2", 1101, chapter_id="ch-0011", is_flashback=False),
            _make_timeline_entry("ch12-main", 1200, chapter_id="ch-0012", is_flashback=False),
        ]
        result = check_timeline_monotonic(timeline_entries=entries)
        assert result.passed, (
            "luo-cloud-test 现场:E2 治本后 ch-11+ 双时空叙事不该再触发误判"
        )


# ---------------------------------------------------------------------------
# 3. death_foreshadowing(§4.2 第 2 条 P4 简版)
# ---------------------------------------------------------------------------


class TestCheckDeathForeshadowing:
    def test_passes_when_no_dead_character_in_related(self) -> None:
        card = _make_card(related_characters=["char-lin-yan"])
        records = [
            _make_character("char-lin-yan", current_state={"status": "alive"}),
            _make_character("char-other", current_state={"status": "dead"}),  # 不在 related
        ]
        result = check_death_foreshadowing(chapter_card=card, character_records=records)
        assert result.passed
        assert result.details["dead_characters_in_related"] == []

    def test_fails_when_dead_character_marked_active(self) -> None:
        card = _make_card(related_characters=["char-su-li"])
        records = [
            _make_character(
                "char-su-li", name="苏离", current_state={"status": "dead"}
            )
        ]
        result = check_death_foreshadowing(chapter_card=card, character_records=records)
        assert not result.passed
        assert result.details["dead_characters_in_related"] == [
            {"character_id": "char-su-li", "name": "苏离"}
        ]

    def test_passes_when_current_state_missing_status_field(self) -> None:
        # 边界:current_state 无 status 字段 → 视为非 dead → 通过
        card = _make_card(related_characters=["char-lin-yan"])
        records = [_make_character("char-lin-yan", current_state={"mood": "calm"})]
        result = check_death_foreshadowing(chapter_card=card, character_records=records)
        assert result.passed


# ---------------------------------------------------------------------------
# 4. voice_forbidden(§4.2 [新增 HARD])
# ---------------------------------------------------------------------------


class TestCheckVoiceForbidden:
    def test_passes_when_no_phrase_hit(self) -> None:
        card = _make_card(related_characters=["char-lin-yan"])
        records = [
            _make_character(
                "char-lin-yan",
                forbidden_phrases=["我去", "卧槽"],
            )
        ]
        text = "他握紧剑柄,沉声道:阵眼真凶身份依然成谜。"
        result = check_voice_forbidden(
            text, chapter_card=card, character_records=records
        )
        assert result.passed
        assert result.details["hits"] == []

    def test_fails_when_phrase_appears_in_text(self) -> None:
        card = _make_card(related_characters=["char-lin-yan"])
        records = [
            _make_character(
                "char-lin-yan",
                forbidden_phrases=["卧槽"],
            )
        ]
        text = "林衍踏入阵中,心里暗骂:卧槽这阵也太复杂了。"
        result = check_voice_forbidden(
            text, chapter_card=card, character_records=records
        )
        assert not result.passed
        assert result.details["hits"] == [
            {"character_id": "char-lin-yan", "name": "林衍", "phrase": "卧槽"}
        ]
        assert result.details["attributable_hits"] == result.details["hits"]
        assert result.details["ambiguous_hits"] == []

    def test_unattributed_phrase_is_only_an_ambiguous_candidate(self) -> None:
        card = _make_card(related_characters=["char-lin-yan"])
        records = [
            _make_character("char-lin-yan", forbidden_phrases=["卧槽"])
        ]
        result = check_voice_forbidden(
            "远处有人喊了一声：卧槽。林衍没有回头。",
            chapter_card=card,
            character_records=records,
        )
        assert result.passed
        assert result.details["attributable_hits"] == []
        assert result.details["ambiguous_hits"] == result.details["hits"]

    def test_skips_characters_not_in_related(self) -> None:
        # 边界:角色不在 related_characters → 该角色的 forbidden_phrases 不查
        card = _make_card(related_characters=["char-lin-yan"])
        records = [
            _make_character(
                "char-lin-yan", forbidden_phrases=["uninteresting"]
            ),
            _make_character(
                "char-other-npc",
                name="路人",
                forbidden_phrases=["卧槽"],  # 但路人不在 related → 命中也不计
            ),
        ]
        text = "卧槽。"  # 命中路人 forbidden 但路人不在 related
        result = check_voice_forbidden(
            text, chapter_card=card, character_records=records
        )
        assert result.passed


# ---------------------------------------------------------------------------
# 4.5 profession_title_blacklist(A3,v1.1 w6-s2,WARN-only 粗筛)
# ---------------------------------------------------------------------------


class TestCheckProfessionTitleBlacklist:
    def _shen_lu_records(self) -> list[Character]:
        # 沈舟现实=心理医生 / 梦境=锦衣卫;陆星野=刑警(他角色专属职业词)
        return [
            _make_character(
                "char-shen-zhou",
                name="沈舟",
                identity_facets=[
                    IdentityFacet(context="现实", identity="心理医生"),
                    IdentityFacet(context="梦境", identity="锦衣卫"),
                ],
            ),
            _make_character(
                "char-lu",
                name="陆星野",
                identity_facets=[IdentityFacet(context="现实", identity="刑警")],
            ),
        ]

    def test_flags_wrong_title_adjacent_to_name(self) -> None:
        """B1 现场重演:"现代刑警沈舟" → 刑警(陆星野专属)邻接沈舟 → 标记 not passed。"""
        card = _make_card(
            pov_character="char-shen-zhou",
            related_characters=["char-shen-zhou"],
        )
        text = "现代刑警沈舟盯着病历,陷入沉思。"
        result = check_profession_title_blacklist(
            text, chapter_card=card, character_records=self._shen_lu_records()
        )
        assert result.passed is False
        hits = result.details["hits"]
        assert len(hits) == 1
        assert hits[0]["name"] == "沈舟"
        assert hits[0]["wrong_title"] == "刑警"
        assert "陆星野" in hits[0]["title_owner"]

    def test_own_title_not_flagged(self) -> None:
        """沈舟以自己的身份(心理医生)出场 → 不标记。"""
        card = _make_card(
            pov_character="char-shen-zhou",
            related_characters=["char-shen-zhou"],
        )
        text = "心理医生沈舟在诊所为患者做催眠。"
        result = check_profession_title_blacklist(
            text, chapter_card=card, character_records=self._shen_lu_records()
        )
        assert result.passed is True

    def test_wrong_title_far_from_name_not_flagged(self) -> None:
        """刑警远离沈舟名(指代真正的陆星野)→ 邻接窗口外 → 不标记(减少误报)。"""
        card = _make_card(
            pov_character="char-shen-zhou",
            related_characters=["char-shen-zhou", "char-lu"],
        )
        text = "沈舟与同伴并肩走着,远处一名刑警正在勘查另一处现场,与他们无关。"
        result = check_profession_title_blacklist(
            text, chapter_card=card, character_records=self._shen_lu_records()
        )
        assert result.passed is True

    def test_character_without_facets_skipped(self) -> None:
        """无 identity_facets 的普通角色不检(普通故事不受影响)。"""
        card = _make_card(
            pov_character="char-lin-yan",
            related_characters=["char-lin-yan"],
        )
        text = "刑警林衍出现了。"  # 林衍无 facets → 不检
        result = check_profession_title_blacklist(
            text,
            chapter_card=card,
            character_records=[_make_character()],  # 林衍,无 facets
        )
        assert result.passed is True

    def test_warn_only_in_pipeline_does_not_fail(self) -> None:
        """职业黑名单命中在 pipeline 里是 WARN-only:不计入 failed,pipeline 仍 passed。"""
        card = _make_card(
            pov_character="char-shen-zhou",
            related_characters=["char-shen-zhou"],
            word_count_min=1,
            word_count_max=10_000_000,
        )
        text = "现代刑警沈舟盯着病历。" * 5
        with capture_logs() as logs:
            report = run_validator_pipeline(
                text,
                chapter_card=card,
                workspace_id="ws-demo",
                known_character_ids={"char-shen-zhou", "char-lu"},
                character_records=self._shen_lu_records(),
            )
        # WARN-only:职业黑名单命中不进 failed_constraint_names
        assert "profession_title_blacklist" not in report.failed_constraint_names
        # 但有 warn log 留痕
        assert any(
            e.get("event") == "profession_title_blacklist_warn_only" for e in logs
        )


# ---------------------------------------------------------------------------
# 5. unfinished_actions_continuation(§4.3 第 1 条)
# ---------------------------------------------------------------------------


class TestCheckUnfinishedActionsContinuation:
    def test_passes_when_any_action_matched_in_head(self) -> None:
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=["剑势悬而未发", "对峙未决"]
            )
        )
        text = "他闭目片刻,终于让剑势悬而未发的紧张感落地。" + "字" * 200
        result = check_unfinished_actions_continuation(text, chapter_card=card)
        assert result.passed
        assert "剑势悬而未发" in result.details["matched_actions"]

    def test_fails_when_no_action_matched_in_head(self) -> None:
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=["剑势悬而未发"]
            )
        )
        text = "另起一段全新的场景描写,与上一章未完成动作完全无关。" + "字" * 200
        result = check_unfinished_actions_continuation(text, chapter_card=card)
        assert not result.passed
        assert result.details["matched_actions"] == []
        assert result.details["missing_actions"] == ["剑势悬而未发"]

    def test_passes_when_actions_empty(self) -> None:
        # 边界:unfinished_actions=[] → 直接通过
        card = _make_card(
            continuity_context=_make_continuity(unfinished_actions=[])
        )
        result = check_unfinished_actions_continuation(
            "正文随意。", chapter_card=card
        )
        assert result.passed
        assert result.details["actions_total"] == 0

    def test_respects_window_chars_boundary(self) -> None:
        # 边界:命中位置在 window_chars 之后 → 视为未命中
        card = _make_card(
            continuity_context=_make_continuity(
                unfinished_actions=["剑势悬而未发"]
            )
        )
        # 前 50 字无命中,后续才出现 → 失败
        text = "字" * 50 + "剑势悬而未发" + "字" * 100
        result = check_unfinished_actions_continuation(
            text, chapter_card=card, window_chars=30
        )
        assert not result.passed


# ---------------------------------------------------------------------------
# 6. scene_continuation_location(§4.3 第 2 条)
# ---------------------------------------------------------------------------


class TestCheckSceneContinuationLocation:
    def test_passes_when_scene_continuation_not_same(self) -> None:
        # scene_continuation in {"switch", "time_jump"} → 直接通过(跳过)
        for mode in ("switch", "time_jump"):
            card = _make_card(
                continuity_context=_make_continuity(scene_continuation=mode)
            )
            result = check_scene_continuation_location("他来到新的山门前。", chapter_card=card)
            assert result.passed
            assert result.details["skipped"] is True

    def test_passes_when_same_and_no_switch_keywords_in_head(self) -> None:
        card = _make_card(
            continuity_context=_make_continuity(scene_continuation="same")
        )
        text = "他依旧站在原地,目光不离对面那道身影。"
        result = check_scene_continuation_location(text, chapter_card=card)
        assert result.passed
        assert result.details["location_switch_keywords_hit"] == []

    def test_fails_when_same_and_head_contains_switch_keyword(self) -> None:
        card = _make_card(
            continuity_context=_make_continuity(scene_continuation="same")
        )
        text = "他来到山门前,目光不离对面那道身影。"  # "来到" 在前 100 字
        result = check_scene_continuation_location(text, chapter_card=card)
        assert not result.passed
        assert "来到" in result.details["location_switch_keywords_hit"]


# ---------------------------------------------------------------------------
# 7. pipeline dispatch:第二批可选数据缺失 → 跳过;structlog 事件
# ---------------------------------------------------------------------------


def _ensure_debug_log_level() -> None:
    """``validator_passed`` 是 DEBUG level — 测试用例前重置 wrapper_class 到 DEBUG
    保证 capture_logs 抓到。"""
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        cache_logger_on_first_use=False,
    )


class TestPipelineDispatchSecondBatch:
    """pipeline 调度第二批 6 个 check 的入参 gating + 事件名汇总。"""

    def test_dispatch_skips_orm_dependent_checks_when_params_none(self) -> None:
        # 仅传 chapter_text + chapter_card + workspace_id → 节奏类 3 个。
        # (v1.0.x A2-02 起 must_achieve 字面比对器移交 LLM validator;
        # v1.0.x wave 3 Commit D 起 §4.3 衔接桥两条也移交 LLM validator。)
        # 一致性类 4 个(character_existence / timeline_monotonic / death_foreshadowing /
        # voice_forbidden)因 ``known_character_ids`` / ``timeline_entries`` /
        # ``character_records`` 全 None 跳过。
        _ensure_debug_log_level()
        card = _make_card()
        text = "正常正文。" + "字" * 100

        with capture_logs() as logs:
            report = run_validator_pipeline(
                text, chapter_card=card, workspace_id="ws-demo"
            )

        assert report.passed
        validator_names = {r.name for r in report.results}
        assert validator_names == {
            "word_count",
            "must_unresolved",
            "forbidden",
        }
        passed_events = [e for e in logs if e.get("event") == "validator_passed"]
        assert len(passed_events) == 1
        assert set(passed_events[0]["validators"]) == validator_names

    def test_dispatch_runs_all_v2_checks_when_data_provided(self) -> None:
        _ensure_debug_log_level()
        card = _make_card(related_characters=["char-lin-yan"])
        records = [_make_character("char-lin-yan", forbidden_phrases=[])]
        timeline = [
            _make_timeline_entry("e1", 1),
            _make_timeline_entry("e2", 2),
        ]
        text = "正常正文。" + "字" * 100

        report = run_validator_pipeline(
            text,
            chapter_card=card,
            workspace_id="ws-demo",
            known_character_ids={"char-lin-yan"},
            timeline_entries=timeline,
            character_records=records,
        )

        assert report.passed
        validator_names = {r.name for r in report.results}
        # 写后只剩节奏 3 + timeline + voice + profession。角色存在/死亡引用
        # 已移到 Writer 前 admission，不得再让正文 rewrite 背数据问题。
        # (v1.0.x wave 3 Commit D 起 §4.3 衔接桥两条 unfinished_actions_continuation
        # / scene_continuation_location 也从 pipeline 移除,改由 LLM-based
        # continuity_validator agent 接管。)
        # A3(v1.1 w6-s2):character_records 提供时还跑 profession_title_blacklist
        # (本 fixture 角色无 identity_facets → passed → 计入 results)。
        assert validator_names == {
            "word_count",
            "must_unresolved",
            "forbidden",
            "timeline_monotonic",
            "voice_forbidden",
            "profession_title_blacklist",
        }

    def test_data_defects_removed_and_ambiguous_signals_remain_warn(self) -> None:
        # 未知/死亡角色属于写前 admission；timeline 与无法归属的禁语只留 WARN。
        _ensure_debug_log_level()
        card = _make_card(
            related_characters=["char-su-li", "char-missing"],
        )
        records = [
            _make_character(
                "char-su-li",
                name="苏离",
                current_state={"status": "dead"},
                forbidden_phrases=["卧槽"],
            ),
        ]
        timeline = [
            _make_timeline_entry("e1", 20),
            _make_timeline_entry("e2", 10),  # 倒流
        ]
        # 没有“苏离说/道/暗骂”等可归属证据，禁语候选不得 HARD。
        text = "苏离站在山门前,卧槽真复杂。" + "字" * 200

        with capture_logs() as logs:
            report = run_validator_pipeline(
                text,
                chapter_card=card,
                workspace_id="ws-demo",
                known_character_ids={"char-su-li"},  # char-missing 不在
                timeline_entries=timeline,
                character_records=records,
            )

        assert report.passed
        assert report.failed_constraint_names == []
        assert {warning.domain for warning in report.warning_signals} == {
            "chapter.timeline",
            "character.voice",
        }
        failed_events = [e for e in logs if e.get("event") == "validator_failed"]
        assert failed_events == []
        # timeline_monotonic 倒流转为 warn-only 日志(留痕),不计入 hard 失败。
        warn_only = [
            e for e in logs if e.get("event") == "timeline_monotonic_warn_only"
        ]
        assert len(warn_only) == 1
        assert warn_only[0]["workspace_id"] == "ws-demo"
        voice_warn = [
            event
            for event in logs
            if event.get("event")
            == "voice_forbidden_attribution_unknown_warn_only"
        ]
        assert len(voice_warn) == 1

    def test_attributable_voice_hit_remains_hard(self) -> None:
        card = _make_card(related_characters=["char-su-li"])
        records = [
            _make_character(
                "char-su-li", name="苏离", forbidden_phrases=["卧槽"]
            )
        ]
        report = run_validator_pipeline(
            "苏离皱眉暗骂：卧槽，这阵法太绕。" + "字" * 200,
            chapter_card=card,
            workspace_id="ws-demo",
            character_records=records,
        )
        assert not report.passed
        assert report.failed_constraint_names == ["voice_forbidden"]
        assert report.warning_signals == []
