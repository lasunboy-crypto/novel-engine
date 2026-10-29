"""outline 内容护栏 wiring 单测(v1.1 wave 6 session 1 / A1 + A4)。

验证两条护栏接进 outline_builder 的端到端行为:

A1(build_world post_validate):
- mechanism 自检判真矛盾 → build_world retry-with-feedback,N 次仍矛盾 → OutlineBuilderError
- mechanism 自检无矛盾 → build_world 正常返回 WorldOutput
- mechanism 先矛盾后干净 → build_world retry 后成功(recovery)

A4(generate_mini_plan 末尾 _check_thread_closure):
- 结局区已挂线的 thread 不进候选(closed-by-plan,省 token)
- 结局区未挂线的 thread(含孤儿)进候选 → 批量 judge
- dangling 进 dangling_titles;open_with_setup / closed 不进(护 C1 留白)

完全 mock LLMClient,用按 agent_role marker 路由的 stub。
"""

from __future__ import annotations

import json

import pytest

from src.core.agents import _prompt as prompt_module
from src.core.agents.outline_builder import (
    OutlineBuilderError,
    _check_identity_coverage,
    _check_thread_closure,
    build_world,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.character import IdentityFacet
from src.core.models.outline import (
    CharacterOutlineItem,
    CharactersOutput,
    MiniPlanItem,
    PlotLongThread,
    PlotOutput,
    PlotPhase,
    PlotTurningPoint,
    VolumeArcPlan,
    VolumeBeatPlan,
    VolumePlanItem,
    VolumePlansOutput,
)

_PREMISE = "心理医生进入患者梦境治疗,卷入跨梦境与现实的连环案。"

_WORLD_JSON = json.dumps(
    {
        "setting": "近未来都市,催眠治疗普及。",
        "power_system": "入梦技术:治疗师引导患者进入梦境。",
        "factions": "心理诊所 vs 地下黑客组织。",
        "culture": "科技与伦理张力下的都市文化。",
        "unique_rules": "入梦者纯意识穿梭,肉体留现实催眠舱;梦中所获随醒来消散。",
    },
    ensure_ascii=False,
)


def _resp(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        model_used="qwen-max",
        usage=TokenUsage(input_tokens=200, output_tokens=80),
        finish_reason="stop",
    )


def _mech_resp(*, contradictions: list[str], evidence: str = "判定") -> str:
    return json.dumps(
        {"contradictions": contradictions, "vague_mechanisms": [], "evidence": evidence},
        ensure_ascii=False,
    )


class _RoleRoutingClient(LLMClient):
    """按 user message 内 agent_role marker 路由的 stub。

    - 含 ``mechanism_consistency_validator`` marker → 返 mechanism 序列下一条
    - 含 ``thread_closure_validator`` marker → 返 thread_closure 序列下一条
    - 其余(world stage)→ 返 world JSON
    """

    def __init__(
        self,
        *,
        world_json: str = _WORLD_JSON,
        mechanism_responses: list[str] | None = None,
        thread_closure_responses: list[str] | None = None,
        identity_coverage_responses: list[str] | None = None,
    ) -> None:
        self._world = world_json
        self._mech = list(mechanism_responses or [])
        self._closure = list(thread_closure_responses or [])
        self._coverage = list(identity_coverage_responses or [])
        self.mech_calls = 0
        self.closure_calls = 0
        self.coverage_calls = 0

    def complete(self, *, messages, model, **kwargs):  # type: ignore[no-untyped-def]
        text = "\n".join(m.content for m in messages if m.role == "user")
        if "agent_role: mechanism_consistency_validator" in text:
            resp = self._mech[min(self.mech_calls, len(self._mech) - 1)]
            self.mech_calls += 1
            return _resp(resp)
        if "agent_role: thread_closure_validator" in text:
            resp = self._closure[min(self.closure_calls, len(self._closure) - 1)]
            self.closure_calls += 1
            return _resp(resp)
        if "agent_role: identity_coverage_validator" in text:
            resp = self._coverage[min(self.coverage_calls, len(self._coverage) - 1)]
            self.coverage_calls += 1
            return _resp(resp)
        return _resp(self._world)


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> None:
    prompt_module.load_prompt_template.cache_clear()


# ---------------------------------------------------------------------------
# A1:build_world post_validate 机制自检
# ---------------------------------------------------------------------------


class TestA1BuildWorldMechanism:
    def test_no_contradiction_returns_world(self) -> None:
        client = _RoleRoutingClient(
            mechanism_responses=[_mech_resp(contradictions=[])]
        )
        world = build_world(premise=_PREMISE, client=client, model="qwen-max")
        assert world.setting.startswith("近未来都市")
        assert client.mech_calls == 1  # 每次 world 生成自检一次

    def test_persistent_contradiction_raises(self) -> None:
        client = _RoleRoutingClient(
            mechanism_responses=[
                _mech_resp(contradictions=["肉体留现实 与 可带实物跨界 互斥"])
            ]
        )
        with pytest.raises(OutlineBuilderError) as exc:
            build_world(premise=_PREMISE, client=client, model="qwen-max")
        # retry 耗尽后报 post_validate 失败(stage=world)
        assert exc.value.stage == "world"
        assert client.mech_calls >= 2  # 至少重试一次

    def test_contradiction_then_clean_recovers(self) -> None:
        client = _RoleRoutingClient(
            mechanism_responses=[
                _mech_resp(contradictions=["首次判矛盾"]),
                _mech_resp(contradictions=[]),  # retry 后干净
            ]
        )
        world = build_world(premise=_PREMISE, client=client, model="qwen-max")
        assert world is not None
        assert client.mech_calls == 2  # 首次矛盾 + retry 干净


# ---------------------------------------------------------------------------
# A4:_check_thread_closure 全书伏笔收尾编排
# ---------------------------------------------------------------------------


def _make_plot_3_threads() -> PlotOutput:
    return PlotOutput(
        core_conflict="主角 vs 幕后操盘者,最终摧毁阴谋。",
        main_phases=[
            PlotPhase(phase_id="phase-1", title="开端", summary="入梦"),
            PlotPhase(phase_id="phase-2", title="发展", summary="追查"),
            PlotPhase(phase_id="phase-3", title="高潮", summary="决战"),
            PlotPhase(phase_id="phase-4", title="结局", summary="收束"),
        ],
        major_turning_points=[
            PlotTurningPoint(turning_id="tp-1", title="转折一", description="入门"),
            PlotTurningPoint(turning_id="tp-2", title="转折二", description="背叛"),
            PlotTurningPoint(turning_id="tp-3", title="转折三", description="觉悟"),
        ],
        long_threads=[
            PlotLongThread(thread_id="thread-1", title="身世", description="身世之谜,结局揭晓"),
            PlotLongThread(thread_id="thread-2", title="界外真局", description="反复暗示,需收口"),
            PlotLongThread(thread_id="thread-3", title="妹妹", description="能否救回,开放结局"),
        ],
        character_arcs={"char-hero": "从迷茫到清醒"},
    )


def _make_volume_plans_2vol() -> VolumePlansOutput:
    def _vol(vid: str, ch_start: int) -> VolumePlanItem:
        return VolumePlanItem(
            volume_id=vid,
            title=f"卷 {vid}",
            summary="卷概要",
            chapter_count=2,
            tension_template="heros_journey",
            arcs=[
                VolumeArcPlan(
                    arc_id=f"{vid}-arc-1",
                    arc_type="主线",
                    chapter_range=(1, 1),
                    phase="opening",
                    summary="前段",
                    beats=[
                        VolumeBeatPlan(
                            beat_id=f"{vid}-arc-1-b1",
                            chapter_range=(1, 1),
                            summary="b",
                        )
                    ],
                ),
                VolumeArcPlan(
                    arc_id=f"{vid}-arc-2",
                    arc_type="主线",
                    chapter_range=(2, 2),
                    phase="rising",
                    summary="后段",
                    beats=[
                        VolumeBeatPlan(
                            beat_id=f"{vid}-arc-2-b1",
                            chapter_range=(2, 2),
                            summary="b",
                        )
                    ],
                ),
            ],
            tension_curve=[4, 6],
        )

    return VolumePlansOutput(volumes=[_vol("v1", 1), _vol("v2", 3)])


def _make_items() -> list[MiniPlanItem]:
    def _it(cid, num, vid, arc, threads, callbacks=None):
        return MiniPlanItem(
            chapter_id=cid,
            chapter_number=num,
            volume_id=vid,
            arc_id=arc,
            one_line_event=f"{cid} 事件",
            linked_threads=threads,
            callback_anchors=callbacks or [],
        )

    # v1(ch 1-2):thread-1 + thread-2 早段挂线;v2(最终卷,ch 1-2):thread-1 在结局区
    # 再挂(→ closed-by-plan 跳过);thread-2 结局区不挂(→ 候选);thread-3 全书孤儿(→ 候选)
    return [
        _it("ch-0001", 1, "v1", "v1-arc-1", ["thread-1", "thread-2"]),
        _it("ch-0002", 2, "v1", "v1-arc-2", ["thread-2"]),
        _it("ch-0003", 1, "v2", "v2-arc-1", ["thread-1"]),
        _it("ch-0004", 2, "v2", "v2-arc-2", []),
    ]


class TestA4CheckThreadClosure:
    def test_closure_region_hook_skips_candidate_and_detects_dangling(self) -> None:
        # 候选顺序 = plot.long_threads 顺序跳过已收口者:thread-1 跳过(结局区挂线),
        # 候选 = [thread-2(界外真局), thread-3(妹妹)]
        # stub:index 1(界外真局)→ dangling;index 2(妹妹)→ open_with_setup(护 C1)
        closure_resp = json.dumps(
            {
                "results": [
                    {"index": 1, "verdict": "dangling", "evidence": "界外真局埋了不收"},
                    {
                        "index": 2,
                        "verdict": "open_with_setup",
                        "evidence": "妹妹线有交代的开放",
                    },
                ]
            },
            ensure_ascii=False,
        )
        client = _RoleRoutingClient(thread_closure_responses=[closure_resp])

        dangling = _check_thread_closure(
            plot=_make_plot_3_threads(),
            all_items=_make_items(),
            volume_plans=_make_volume_plans_2vol(),
            router=None,
            client=client,
            model="qwen-max",
            prompts_dir=None,
        )
        # thread-1 结局区挂线被跳过;thread-3 open_with_setup 护 C1 不计 dangling
        assert dangling == ["界外真局"]
        assert client.closure_calls == 1  # 批处理一次调用

    def test_all_threads_hooked_in_closure_skips_llm(self) -> None:
        # 全部 thread 都在结局区(v2)挂线 → 0 候选 → 0 次 LLM 调用
        items = [
            MiniPlanItem(
                chapter_id="ch-0003",
                chapter_number=1,
                volume_id="v2",
                arc_id="v2-arc-1",
                one_line_event="结局区收口全部伏笔",
                linked_threads=["thread-1", "thread-2", "thread-3"],
            ),
        ]
        client = _RoleRoutingClient(thread_closure_responses=["{}"])
        dangling = _check_thread_closure(
            plot=_make_plot_3_threads(),
            all_items=items,
            volume_plans=_make_volume_plans_2vol(),
            router=None,
            client=client,
            model="qwen-max",
            prompts_dir=None,
        )
        assert dangling == []
        assert client.closure_calls == 0  # 无候选 → 不调 LLM


# ---------------------------------------------------------------------------
# A3:_check_identity_coverage 全书角色身份覆盖度编排
# ---------------------------------------------------------------------------


def _char(cid: str, name: str, *facets: tuple[str, str]) -> CharacterOutlineItem:
    return CharacterOutlineItem(
        character_id=cid,
        name=name,
        role="supporting",
        archetype="原型",
        appearance="外貌",
        personality="性格",
        background="背景",
        motivation="动机",
        voice_hint="冷峻",
        identity_facets=[IdentityFacet(context=c, identity=i) for c, i in facets],
    )


def _cov_items() -> list[MiniPlanItem]:
    return [
        MiniPlanItem(
            chapter_id="ch-0001",
            chapter_number=1,
            volume_id="v1",
            arc_id="v1-arc-1",
            one_line_event="沈舟在诊所催眠患者",
        ),
        MiniPlanItem(
            chapter_id="ch-0002",
            chapter_number=2,
            volume_id="v1",
            arc_id="v1-arc-2",
            one_line_event="沈舟入梦以锦衣卫潜入",
        ),
    ]


class TestA3CheckIdentityCoverage:
    def test_no_multi_facet_character_skips_llm(self) -> None:
        """无多情境身份角色(facets < 2)→ 0 候选 → 不调 LLM。"""
        chars = CharactersOutput(
            characters=[
                _char("hero", "主角", ("现实", "学生")),  # 单 facet
                _char("a", "甲"),  # 空 facet
                _char("b", "乙"),
                _char("c", "丙"),
            ]
        )
        client = _RoleRoutingClient(identity_coverage_responses=["{}"])
        gaps = _check_identity_coverage(
            characters=chars,
            all_items=_cov_items(),
            router=None,
            client=client,
            model="qwen-max",
            prompts_dir=None,
        )
        assert gaps == []
        assert client.coverage_calls == 0  # 无候选 → 不调 LLM

    def test_multi_facet_gap_detected(self) -> None:
        """多情境身份角色某侧未覆盖 → gaps 含该角色 + 缺侧;批处理一次调用。"""
        chars = CharactersOutput(
            characters=[
                _char("shen", "沈舟", ("现实", "心理医生"), ("梦境", "卧底锦衣卫")),
                _char("bai", "白露", ("现实", "诊所助理"), ("梦境", "引路人")),
                _char("c", "丙"),
                _char("d", "丁"),
            ]
        )
        # 候选顺序 = characters 里 facets≥2 的:沈舟(idx1)/白露(idx2)
        coverage_resp = json.dumps(
            {
                "results": [
                    {"index": 1, "uncovered_contexts": [], "evidence": "两侧均覆盖"},
                    {
                        "index": 2,
                        "uncovered_contexts": ["现实"],
                        "evidence": "白露现实侧全书无交代",
                    },
                ]
            },
            ensure_ascii=False,
        )
        client = _RoleRoutingClient(identity_coverage_responses=[coverage_resp])
        gaps = _check_identity_coverage(
            characters=chars,
            all_items=_cov_items(),
            router=None,
            client=client,
            model="qwen-max",
            prompts_dir=None,
        )
        assert gaps == [("白露", ["现实"])]
        assert client.coverage_calls == 1  # 批处理一次调用
