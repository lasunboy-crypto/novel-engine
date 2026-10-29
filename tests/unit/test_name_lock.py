"""命名锁定表防回退测(v2 W3.5 补强1)。

两层覆盖(feedback_test_full_wiring_not_just_mechanism_ends 纪律):
1. **机制**:``_name_lock.extract_locked_names`` / ``build_name_lock_header``
   —— 锚点提取 / 边界消歧 / 噪声拒绝 / canonical 排除 / header 构造。
2. **接线**:``_build_plot_user_message`` / ``_build_volume_plans_user_message``
   / ``_build_mini_plan_user_message_per_volume`` 三个 builder 真把 header 前置注入
   —— proactive 注入的透传环最易被后续编辑静默删掉,必须各有一条 wiring 断言。

根因 + 设计:docs/findings/v2_w3_5_deepseek_governability_eval_results.md §5b/§9 +
src/core/agents/_name_lock.py 模块 docstring。
"""

from __future__ import annotations

from src.core.agents._name_lock import (
    build_name_lock_header,
    build_relationship_lock_lines,
    extract_locked_names,
    extract_place_anchors,
    extract_time_anchors,
)
from src.core.agents.outline_builder import (
    _build_mini_plan_user_message_per_volume,
    _build_plot_user_message,
    _build_volume_plans_user_message,
)
from src.core.models.outline import (
    CharacterOutlineItem,
    CharactersOutput,
    PlotLongThread,
    PlotOutput,
    PlotPhase,
    PlotTurningPoint,
    VolumeArcPlan,
    VolumeBeatPlan,
    VolumePlanItem,
    WorldOutput,
)

_HEADER_MARK = "## ⚠️ 全书 canonical 事实锁定表"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _char(
    cid: str,
    name: str,
    *,
    background: str = "普通背景,没有任何特殊命名实体。",
    motivation: str = "想要变强。",
    appearance: str = "外貌平平。",
    personality: str = "性格温和。",
    relationships_brief: dict[str, str] | None = None,
) -> CharacterOutlineItem:
    return CharacterOutlineItem(
        character_id=cid,
        name=name,
        role="supporting",
        archetype="原型",
        appearance=appearance,
        personality=personality,
        background=background,
        motivation=motivation,
        voice_hint="冷峻",
        relationships_brief=relationships_brief or {},
    )


def _buried_father_chars() -> CharactersOutput:
    """镜像 s7 真实硬伤:父亲"顾崇山"只埋在 background/motivation、无独立 ID。"""
    return CharactersOutput(
        characters=[
            _char(
                "gu-yun",
                "顾云",
                background="北大考古讲师。其父顾崇山是二十年前在西域失踪的传奇考古学家。",
                motivation="追寻父亲顾崇山失踪的真相。",
            ),
            _char(
                "su-chang",
                "苏畅",
                background="他是顾云父亲顾崇山的学生和助手,也是失踪考古队唯一幸存者。",
                motivation="他想找到顾崇山的遗骸,给顾云一个交代。",
            ),
            _char("jiang-yun", "江昀"),
            _char("wei-ze", "魏泽"),
        ]
    )


def _clean_chars() -> CharactersOutput:
    """全角色都有独立 ID、无 buried 名、**无 relationships_brief**(纯 clean 收敛态)。"""
    return CharactersOutput(
        characters=[
            _char("lu-chen", "陆沉", background="孤儿出身,自学成才,常年穿黑衣。"),
            _char("su-xiao", "苏晓", background="成为团队向导,任何任务都全力以赴。"),
            _char("wei-yuan", "魏渊", background="文物修复专家,高维理论的提出者。"),
            _char("li-shuo", "黎朔", background="低调的幕后人物。"),
        ]
    )


def _rel_chars() -> CharactersOutput:
    """clean 名 + 无 buried/时间/地点,**仅** relationships_brief 有关系(关系硬锁触发)。

    镜像 (a) RCT 强制触发:黎苍是黎朔的「叔伯」,这一关系身份事实只埋在 relationships_brief
    (plot 不传)→ deepseek 写成父子/祖父。关系硬锁把它显式锁住。
    """
    return CharactersOutput(
        characters=[
            _char(
                "li-shuo",
                "黎朔",
                relationships_brief={
                    "li-cang": "血缘上的叔伯,理念与立场的死敌",
                    "zhou-ming": "并肩闯荡的生死搭档",
                },
            ),
            _char(
                "li-cang",
                "黎苍",
                relationships_brief={"li-shuo": "试图矫正或抹杀的叛逆侄子"},
            ),
            _char("zhou-ming", "周明"),
            _char("su-wan", "苏晚"),
        ]
    )


def _world() -> WorldOutput:
    return WorldOutput(
        setting="近未来都市。",
        power_system="无。",
        factions="两大阵营。",
        culture="科技文化。",
        unique_rules="独特硬规则。",
    )


def _plot() -> PlotOutput:
    return PlotOutput(
        core_conflict="主角 vs 幕后。",
        main_phases=[
            PlotPhase(phase_id="phase-1", title="开端", summary="起"),
            PlotPhase(phase_id="phase-2", title="发展", summary="承"),
            PlotPhase(phase_id="phase-3", title="高潮", summary="转"),
            PlotPhase(phase_id="phase-4", title="结局", summary="收"),
        ],
        major_turning_points=[
            PlotTurningPoint(turning_id="tp-1", title="转折一", description="变一"),
            PlotTurningPoint(turning_id="tp-2", title="转折二", description="变二"),
            PlotTurningPoint(turning_id="tp-3", title="转折三", description="变三"),
        ],
        long_threads=[
            PlotLongThread(thread_id="thread-1", title="身世", description="揭晓"),
            PlotLongThread(thread_id="thread-2", title="真相", description="收口"),
            PlotLongThread(thread_id="thread-3", title="宿敌", description="开放"),
        ],
        character_arcs={"gu-yun": "从迷茫到清醒"},
    )


def _volume() -> VolumePlanItem:
    return VolumePlanItem(
        volume_id="v1",
        title="卷一",
        summary="卷概要",
        chapter_count=2,
        tension_template="heros_journey",
        arcs=[
            VolumeArcPlan(
                arc_id="v1-arc-1",
                arc_type="主线",
                chapter_range=(1, 1),
                phase="opening",
                summary="前段",
                beats=[
                    VolumeBeatPlan(
                        beat_id="v1-arc-1-b1", chapter_range=(1, 1), summary="b"
                    )
                ],
            ),
            VolumeArcPlan(
                arc_id="v1-arc-2",
                arc_type="主线",
                chapter_range=(2, 2),
                phase="rising",
                summary="后段",
                beats=[
                    VolumeBeatPlan(
                        beat_id="v1-arc-2-b1", chapter_range=(2, 2), summary="b"
                    )
                ],
            ),
        ],
        tension_curve=[4, 6],
    )


# ---------------------------------------------------------------------------
# 机制:extract_locked_names
# ---------------------------------------------------------------------------


class TestExtractLockedNames:
    def test_extracts_buried_father_via_kinship_anchor(self) -> None:
        locked = extract_locked_names(_buried_father_chars())
        names = [ln.name for ln in locked]
        assert names == ["顾崇山"]
        assert locked[0].occurrences >= 2
        assert "顾崇山" in locked[0].snippet  # grounding 出处带原句

    def test_boundary_prefers_recurring_longer_name(self) -> None:
        # 顾崇山(3 字)复现 → 取它而非 顾崇(2 字)
        locked = extract_locked_names(_buried_father_chars())
        assert all(ln.name != "顾崇" for ln in locked)

    def test_clean_profiles_extract_nothing(self) -> None:
        assert extract_locked_names(_clean_chars()) == []

    def test_rejects_common_words_without_anchor(self) -> None:
        # 背景含"成为/任何/文物"(姓氏字开头常用词)但无亲属锚点 → 不抽
        chars = CharactersOutput(
            characters=[
                _char("a", "陆沉", background="他成为高手,任何文物都能鉴别,常年钻研。"),
                _char("b", "苏晓"),
                _char("c", "江昀"),
                _char("d", "魏泽"),
            ]
        )
        assert extract_locked_names(chars) == []

    def test_excludes_canonical_name_and_substring(self) -> None:
        # 锚点后跟的是 canonical 角色本人名(及其子串)→ 不重复锁定
        chars = CharactersOutput(
            characters=[
                _char("lin", "林远志", background="名叫林远志,本是孤儿。"),
                _char("a", "苏晓", background="她的师父林远志收养了她。"),
                _char("c", "江昀"),
                _char("d", "魏泽"),
            ]
        )
        # 林远志 是 canonical;"林远"是其子串 → 都排除 → 空
        assert extract_locked_names(chars) == []

    def test_scans_relationships_brief_values(self) -> None:
        chars = CharactersOutput(
            characters=[
                _char(
                    "a",
                    "陆沉",
                    relationships_brief={"mentor": "他的师父周怀安一手栽培了他。"},
                ),
                _char("b", "苏晓"),
                _char("c", "江昀"),
                _char("d", "魏泽"),
            ]
        )
        names = [ln.name for ln in extract_locked_names(chars)]
        assert "周怀安" in names


# ---------------------------------------------------------------------------
# 机制:extract_time_anchors(W3.5 补强1 泛化)
# ---------------------------------------------------------------------------


class TestExtractTimeAnchors:
    def test_extracts_year_ago_anchor(self) -> None:
        # _buried_father_chars 背景含"二十年前在西域失踪"
        times = [t.name for t in extract_time_anchors(_buried_father_chars())]
        assert "二十年前" in times

    def test_filters_trivial_durations(self) -> None:
        chars = CharactersOutput(
            characters=[
                _char("a", "陆沉", background="他工作了两年,休息一年。"),
                _char("b", "苏晓"),
                _char("c", "江昀"),
                _char("d", "魏泽"),
            ]
        )
        # 一年/两年(无"前"、无整十百千万)→ 不锁
        assert extract_time_anchors(chars) == []

    def test_locks_distinctive_distance(self) -> None:
        chars = CharactersOutput(
            characters=[
                _char("a", "陆沉", background="他守护了两千年的古老封印。"),
                _char("b", "苏晓"),
                _char("c", "江昀"),
                _char("d", "魏泽"),
            ]
        )
        assert "两千年" in [t.name for t in extract_time_anchors(chars)]


# ---------------------------------------------------------------------------
# 机制:extract_place_anchors(W3.5 补强1 泛化)
# ---------------------------------------------------------------------------


class TestExtractPlaceAnchors:
    def test_extracts_region_seed(self) -> None:
        places = [p.name for p in extract_place_anchors(_buried_father_chars())]
        assert "西域" in places  # 区域 seed 精确匹配

    def test_extracts_anchored_named_place(self) -> None:
        chars = CharactersOutput(
            characters=[
                _char("a", "陆沉", background="考古队在罗布泊腹地失踪。"),
                _char("b", "苏晓"),
                _char("c", "江昀"),
                _char("d", "魏泽"),
            ]
        )
        assert "罗布泊" in [p.name for p in extract_place_anchors(chars)]

    def test_no_name_collision(self) -> None:
        # 顾崇山(崇'山')/ 江昀(姓'江')不得被当地点(后缀撞名是地点抽取的致命坑)
        places = [p.name for p in extract_place_anchors(_buried_father_chars())]
        assert "顾崇山" not in places
        assert all("顾崇山" not in p and "江昀" not in p for p in places)


# ---------------------------------------------------------------------------
# 机制:build_relationship_lock_lines(v2 W3.5 (a) 关系硬锁)
# ---------------------------------------------------------------------------


class TestRelationshipLock:
    def test_renders_relationship_with_names(self) -> None:
        # id→name 映射 + 关系原文
        lines = build_relationship_lock_lines(_rel_chars())
        joined = "\n".join(lines)
        assert "黎朔 与 黎苍:血缘上的叔伯" in joined  # 叔伯关系被显式锁
        assert "黎朔 与 周明" in joined

    def test_dedupes_bidirectional_pair(self) -> None:
        # 黎朔→黎苍(叔伯)与 黎苍→黎朔(叛逆侄子)= 同一对 → 只列一次,取先遇到的一向
        lines = build_relationship_lock_lines(_rel_chars())
        pair_lines = [ln for ln in lines if "黎朔" in ln and "黎苍" in ln]
        assert len(pair_lines) == 1
        assert "叛逆侄子" not in "\n".join(lines)  # 反向被去重

    def test_empty_when_no_relationships(self) -> None:
        assert build_relationship_lock_lines(_clean_chars()) == []

    def test_skips_empty_relationship_text(self) -> None:
        chars = CharactersOutput(
            characters=[
                _char("a", "陆沉", relationships_brief={"b": "  "}),  # 空白 → 跳过
                _char("b", "苏晓"),
                _char("c", "江昀"),
                _char("d", "魏泽"),
            ]
        )
        assert build_relationship_lock_lines(chars) == []


# ---------------------------------------------------------------------------
# 机制:build_name_lock_header
# ---------------------------------------------------------------------------


class TestBuildNameLockHeader:
    def test_header_contains_name_and_lock_instruction(self) -> None:
        header = build_name_lock_header(_buried_father_chars())
        assert _HEADER_MARK in header
        assert "顾崇山" in header
        assert "不可改" in header or "严禁" in header

    def test_header_has_relationship_section_when_relationships(self) -> None:
        # (a) 关系硬锁:关系段 + 钉身份放开剧情框定
        header = build_name_lock_header(_rel_chars())
        assert "角色关系锁定" in header
        assert "黎朔 与 黎苍:血缘上的叔伯" in header
        assert "严禁改写关系本身" in header
        assert "自由发挥" in header  # 放开剧情

    def test_header_emitted_when_only_relationships(self) -> None:
        # 无 buried 名 / 时间 / 地点,仅 relationships_brief → 仍输出(锁关系 + 加固角色名)
        header = build_name_lock_header(_rel_chars())
        assert _HEADER_MARK in header
        assert "核心角色名" in header and "黎朔" in header

    def test_header_no_relationship_section_when_clean(self) -> None:
        # _buried_father_chars 无 relationships_brief → 无关系段(只有名/时间/地点)
        header = build_name_lock_header(_buried_father_chars())
        assert "角色关系锁定" not in header

    def test_header_lists_canonical_names(self) -> None:
        # 泛化:加固 canonical 角色名(苏畅→苏偿 实证需显式锁)
        header = build_name_lock_header(_buried_father_chars())
        assert "核心角色名" in header
        assert "顾云" in header and "苏畅" in header

    def test_header_has_time_and_place_sections(self) -> None:
        header = build_name_lock_header(_buried_father_chars())
        assert "时间锚点" in header and "二十年前" in header
        assert "地点" in header and "西域" in header

    def test_header_emitted_when_only_time_no_buried(self) -> None:
        # 无 buried 名但有时间锚点 → 仍输出(锁时间 + 加固角色名)
        chars = CharactersOutput(
            characters=[
                _char("a", "陆沉", background="他在三百年前立下誓言。"),
                _char("b", "苏晓"),
                _char("c", "江昀"),
                _char("d", "魏泽"),
            ]
        )
        header = build_name_lock_header(chars)
        assert _HEADER_MARK in header
        assert "三百年前" in header

    def test_header_empty_when_no_facts(self) -> None:
        # 无 buried 名 + 无时间 + 无地点 → 不扰动
        assert build_name_lock_header(_clean_chars()) == ""


# ---------------------------------------------------------------------------
# 接线:三个 builder 真注入 header(防回退核心)
# ---------------------------------------------------------------------------


class TestBuilderWiring:
    def test_plot_message_injects_header_when_buried(self) -> None:
        msg = _build_plot_user_message(
            premise="p", genre="悬疑", world=_world(), characters=_buried_father_chars()
        )
        assert msg.startswith(_HEADER_MARK)
        assert "顾崇山" in msg

    def test_plot_message_no_header_when_clean(self) -> None:
        msg = _build_plot_user_message(
            premise="p", genre="悬疑", world=_world(), characters=_clean_chars()
        )
        assert msg.startswith("## 用户输入")

    def test_volume_plans_message_injects_header(self) -> None:
        msg = _build_volume_plans_user_message(
            premise="p",
            genre="悬疑",
            world=_world(),
            characters=_buried_father_chars(),
            plot=_plot(),
            total_volumes=1,
            chapters_per_volume=2,
            tension_template="heros_journey",
        )
        assert msg.startswith(_HEADER_MARK)
        assert "顾崇山" in msg

    def test_volume_plans_message_no_header_when_clean(self) -> None:
        msg = _build_volume_plans_user_message(
            premise="p",
            genre="悬疑",
            world=_world(),
            characters=_clean_chars(),
            plot=_plot(),
            total_volumes=1,
            chapters_per_volume=2,
            tension_template="heros_journey",
        )
        assert msg.startswith("## 用户输入")

    def test_mini_plan_message_injects_header(self) -> None:
        chapter_id_map = {("v1", 1): "ch-0001", ("v1", 2): "ch-0002"}
        msg = _build_mini_plan_user_message_per_volume(
            world=_world(),
            characters=_buried_father_chars(),
            plot=_plot(),
            volume=_volume(),
            genre="悬疑",
            chapter_id_map=chapter_id_map,
            prev_volumes_tail=[],
        )
        assert msg.startswith(_HEADER_MARK)
        assert "顾崇山" in msg

    def test_mini_plan_message_no_header_when_clean(self) -> None:
        chapter_id_map = {("v1", 1): "ch-0001", ("v1", 2): "ch-0002"}
        msg = _build_mini_plan_user_message_per_volume(
            world=_world(),
            characters=_clean_chars(),
            plot=_plot(),
            volume=_volume(),
            genre="悬疑",
            chapter_id_map=chapter_id_map,
            prev_volumes_tail=[],
        )
        assert msg.startswith("## 任务背景")

    # (a) 关系硬锁接线:3 个 builder 真注入关系锁(proactive 透传环易被静默删,各一条断言)
    def test_plot_message_injects_relationship_lock(self) -> None:
        msg = _build_plot_user_message(
            premise="p", genre="悬疑", world=_world(), characters=_rel_chars()
        )
        assert msg.startswith(_HEADER_MARK)
        assert "黎朔 与 黎苍:血缘上的叔伯" in msg

    def test_volume_plans_message_injects_relationship_lock(self) -> None:
        msg = _build_volume_plans_user_message(
            premise="p",
            genre="悬疑",
            world=_world(),
            characters=_rel_chars(),
            plot=_plot(),
            total_volumes=1,
            chapters_per_volume=2,
            tension_template="heros_journey",
        )
        assert msg.startswith(_HEADER_MARK)
        assert "黎朔 与 黎苍:血缘上的叔伯" in msg

    def test_mini_plan_message_injects_relationship_lock(self) -> None:
        chapter_id_map = {("v1", 1): "ch-0001", ("v1", 2): "ch-0002"}
        msg = _build_mini_plan_user_message_per_volume(
            world=_world(),
            characters=_rel_chars(),
            plot=_plot(),
            volume=_volume(),
            genre="悬疑",
            chapter_id_map=chapter_id_map,
            prev_volumes_tail=[],
        )
        assert msg.startswith(_HEADER_MARK)
        assert "黎朔 与 黎苍:血缘上的叔伯" in msg
