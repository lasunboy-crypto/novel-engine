"""outline_consistency_validator agent 单测(v2 W3.5-S3,大纲跨段一致性校验)。

覆盖(对齐 mechanism_consistency_validator 同型测试范围):

1. **happy**:无矛盾 + 无覆盖缺口 → passed=True
2. **contradiction**:名字矛盾 / 角色混淆 → passed=False / contradictions 非空(HARD)
3. **coverage gap only**:卷规划覆盖缺口但无矛盾 → passed=True + coverage_gaps 非空(WARN 不阻断)
4. **LLM fail**:LLM 抛异常 → fail-open(passed=True + fail_open=True)
5. **setup fail**:prompt 文件缺失 → fail-open
6. **retry feedback**:``format_outline_consistency_failure_for_retry`` 输出含 contradictions 字面
7. **user message**:跨段字段(角色名 / plot / 卷规划)+ marker 都进 user message
8. **mock 端到端**:MockLLMClient 经 marker 返"无矛盾"非 fail-open(护 happy 路径)

完全 mock ``LLMClient``;用临时目录注入 fake ``outline_consistency_validator_v1.md``。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.agents import _prompt as prompt_module
from src.core.agents.outline_consistency_validator import (
    NameTypoFix,
    OutlineConsistencyResult,
    apply_name_typo_fixes,
    format_outline_consistency_failure_for_retry,
    validate_outline_consistency,
    validate_outline_consistency_partitioned,
    validate_split_review_payload,
    _SplitReviewPayload,
    validate_outline_stage_consistency,
)
from src.core.agents.structured_handoff import HandoffPacket
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.outline import (
    CharactersOutput,
    PlotOutput,
    VolumeArcPlan,
    VolumeBeatPlan,
    VolumePlanItem,
    VolumePlansOutput,
    WorldOutput,
)


# ---------------------------------------------------------------------------
# 4 段 outline fixture(满足各 schema 约束)
# ---------------------------------------------------------------------------


def _make_world() -> WorldOutput:
    return WorldOutput(
        setting="架空大陆九州,正值末法时代",
        power_system="修炼九境",
        factions="九大正道 + 三大魔域",
        culture="修真者高于俗世",
        unique_rules="渡劫失败必死",
    )


def _make_characters() -> CharactersOutput:
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


def _make_volume_plans() -> VolumePlansOutput:
    return VolumePlansOutput(
        volumes=[
            VolumePlanItem(
                volume_id="v1",
                title="卷一·觉醒",
                summary="本卷起势,林衍重生入门",
                chapter_count=2,
                tension_template="heros_journey",
                linked_phases=["phase-1"],
                linked_turning_points=["turn-1"],
                linked_threads=["yu-pei"],
                arcs=[
                    VolumeArcPlan(
                        arc_id="v1-a1",
                        arc_type="主线",
                        chapter_range=(1, 1),
                        phase="opening",
                        summary="开篇重生",
                    ),
                    VolumeArcPlan(
                        arc_id="v1-a2",
                        arc_type="主线",
                        chapter_range=(2, 2),
                        phase="rising",
                        summary="入门推进",
                    ),
                ],
                tension_curve=[5, 8],
            ),
        ],
    )


def _make_response(
    *,
    contradictions: list[str] | None = None,
    coverage_gaps: list[str] | None = None,
    evidence: str = "4 段跨段一致",
    model_used: str = "qwen-max",
    repair_scope: str | None = None,
) -> LLMResponse:
    payload = {
        "contradictions": contradictions or [],
        "coverage_gaps": coverage_gaps or [],
        "evidence": evidence,
    }
    if repair_scope is not None:
        payload["repair_scope"] = repair_scope
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        model_used=model_used,
        usage=TokenUsage(input_tokens=900, output_tokens=80),
        finish_reason="stop",
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "outline_consistency_validator_v2.md").write_text(
        "FAKE_OUTLINE_CONSISTENCY_VALIDATOR_PROMPT\n\n用于单测的 system 标记。",
        encoding="utf-8",
    )
    (prompts_dir / "outline_stage_consistency_validator_v3.md").write_text(
        "FAKE_STAGE_FACT_PROMPT\n\n用于单测的来源事实核对。", encoding="utf-8",
    )
    (prompts_dir / "outline_split_consistency_validator_v4.md").write_text(
        "FAKE_SPLIT_FACT_PROMPT\n\n只输出 JSON。", encoding="utf-8",
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> None:
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=LLMClient)
    client.complete.return_value = _make_response()
    return client


def _call(
    mock_client: MagicMock,
    fake_prompts_dir: Path,
) -> OutlineConsistencyResult:
    return validate_outline_consistency(
        world=_make_world(),
        characters=_make_characters(),
        plot=_make_plot(),
        volume_plans=_make_volume_plans(),
        premise="废柴少年重生改写末法时代,与魔域大尊的宿命对决。",
        genre="修仙",
        workspace_id="ws-demo",
        client=mock_client,
        model="qwen-max",
        prompts_dir=fake_prompts_dir,
    )


class TestHappy:
    def test_no_contradiction_no_gap_passes(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            contradictions=[], coverage_gaps=[], evidence="跨段一致,卷规划覆盖主线"
        )
        result = _call(mock_client, fake_prompts_dir)
        assert result.passed is True
        assert result.contradictions == []
        assert result.coverage_gaps == []
        assert result.llm_calls_count == 1
        assert result.fail_open is False
        assert mock_client.complete.call_count == 1


class TestContradiction:
    @pytest.mark.parametrize(
        ("repair_scope", "allowed"),
        [("volume_plans_only", True), ("upstream", False), ("unclear", False), (None, False)],
    )
    def test_repair_scope_does_not_turn_failure_into_pass(
        self, mock_client, fake_prompts_dir, repair_scope, allowed
    ) -> None:
        mock_client.complete.return_value = _make_response(
            contradictions=["人物背景与主线中的首次事件互斥"],
            repair_scope=repair_scope,
        )
        result = _call(mock_client, fake_prompts_dir)
        assert result.passed is False
        assert result.has_non_fixable_contradiction is True
        assert result.volume_plans_retry_allowed is allowed
        assert mock_client.complete.call_count == 1

    def test_no_conflict_does_not_authorize_rewrite(
        self, mock_client, fake_prompts_dir
    ) -> None:
        mock_client.complete.return_value = _make_response(
            repair_scope="volume_plans_only"
        )
        result = _call(mock_client, fake_prompts_dir)
        assert result.passed is True
        assert result.volume_plans_retry_allowed is False

    def test_name_contradiction_fails_hard(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            contradictions=[
                "主角父亲在 plot 叫'顾崇山',characters 同一人却叫'顾盛礼',名字互斥",
            ],
            coverage_gaps=[],
            evidence="同一实体跨段两个互斥本名",
        )
        result = _call(mock_client, fake_prompts_dir)
        assert result.passed is False
        assert len(result.contradictions) == 1
        assert "互斥" in result.contradictions[0]

    def test_character_confusion_fails_hard(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            contradictions=[
                "characters 列'内线小陆'与'反派女儿小纪'两个角色,plot 把两人混为一人",
            ],
            coverage_gaps=[],
            evidence="两个独立角色被混为一人",
        )
        result = _call(mock_client, fake_prompts_dir)
        assert result.passed is False
        assert "混为一人" in result.contradictions[0]


class TestCoverageGapOnly:
    def test_coverage_gap_does_not_block(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """卷规划覆盖缺口是 WARN — 不影响 passed(护留白:不阻断,仅提示)。"""
        mock_client.complete.return_value = _make_response(
            contradictions=[],
            coverage_gaps=[
                "plot.major_turning_points 的'终极决战'没有任何一卷承接",
            ],
            evidence="主骨架转折未被卷承接,WARN 标记非矛盾",
        )
        result = _call(mock_client, fake_prompts_dir)
        assert result.passed is True  # WARN 不阻断
        assert len(result.coverage_gaps) == 1
        assert result.contradictions == []


class TestFailOpen:
    def test_llm_exception_fail_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.side_effect = RuntimeError("LLM 瞬态故障")
        result = _call(mock_client, fake_prompts_dir)
        assert result.passed is True
        assert result.fail_open is True

    def test_invalid_json_fail_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = LLMResponse(
            content="这不是 JSON",
            model_used="qwen-max",
            usage=TokenUsage(input_tokens=900, output_tokens=10),
            finish_reason="stop",
        )
        result = _call(mock_client, fake_prompts_dir)
        assert result.passed is True
        assert result.fail_open is True

    def test_setup_missing_prompt_fail_open(
        self, mock_client: MagicMock, tmp_path: Path
    ) -> None:
        empty_dir = tmp_path / "empty_prompts"
        empty_dir.mkdir()
        result = validate_outline_consistency(
            world=_make_world(),
            characters=_make_characters(),
            plot=_make_plot(),
            volume_plans=_make_volume_plans(),
            client=mock_client,
            model="qwen-max",
            prompts_dir=empty_dir,
        )
        assert result.passed is True
        assert result.fail_open is True
        assert result.llm_calls_count == 0


class TestUserMessage:
    def test_user_message_includes_cross_stage_fields_and_marker(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """跨段字段(角色名 / plot 阶段 / 卷规划)+ marker 都进 user message。"""
        _call(mock_client, fake_prompts_dir)
        sent_messages = mock_client.complete.call_args.kwargs["messages"]
        user_text = sent_messages[1].content
        # 角色段(跨段一致性判定核心:名字 + id)。
        assert "林衍" in user_text
        assert "lin-yan" in user_text
        # plot 段(覆盖判定 + 角色一致性)。
        assert "core_conflict" in user_text
        assert "phase-1" in user_text
        # 卷规划段。
        assert "v1" in user_text
        # marker(MockLLMClient / 真 LLM client agent 字段识别)。
        assert "<!-- agent_role: outline_consistency_validator -->" in user_text


class TestRetryFeedback:
    def test_format_failure_contains_contradictions(self) -> None:
        result = OutlineConsistencyResult(
            passed=False,
            contradictions=["顾崇山/顾盛礼名字矛盾", "小陆/小纪混为一人"],
            coverage_gaps=["终极决战无卷承接"],
            evidence="两处跨段矛盾 + 一处覆盖缺口",
        )
        text = format_outline_consistency_failure_for_retry(result=result)
        assert "顾崇山/顾盛礼名字矛盾" in text
        assert "小陆/小纪混为一人" in text
        assert "终极决战无卷承接" in text  # coverage_gaps 也附上(非阻断提示)


class TestMockEndToEnd:
    """MockLLMClient 经 marker 返"无矛盾"非 fail-open(护 outline happy 路径)。"""

    def test_mock_client_returns_passed_not_fail_open(self) -> None:
        from src.core.llm.mock_client import MockLLMClient

        result = validate_outline_consistency(
            world=_make_world(),
            characters=_make_characters(),
            plot=_make_plot(),
            volume_plans=_make_volume_plans(),
            client=MockLLMClient(),
            model="mock-model",
        )
        # mock 返 contradictions=[] / coverage_gaps=[] → passed=True 且非 fail-open
        # (marker 探测成功走专属分支,不是异常吞掉的 fail-open)。
        assert result.passed is True
        assert result.fail_open is False
        assert result.contradictions == []
        assert result.llm_calls_count == 1


def _make_structured_response(
    *,
    contradictions: list[dict],
    coverage_gaps: list[str] | None = None,
    evidence: str = "见 contradictions",
    model_used: str = "qwen-max",
) -> LLMResponse:
    """v2:逐条结构化 contradictions(每条 {desc,type,wrong,correct,reason})。"""
    payload = {
        "contradictions": contradictions,
        "coverage_gaps": coverage_gaps or [],
        "evidence": evidence,
    }
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        model_used=model_used,
        usage=TokenUsage(input_tokens=900, output_tokens=120),
        finish_reason="stop",
    )


class TestV2Classification:
    """v2:逐条分类 → name_typo_fixes 子集 + has_non_fixable 标记。"""

    def test_name_typo_populates_fixes_not_non_fixable(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_structured_response(
            contradictions=[{
                "desc": "导师本名多处为'陆秉渊',骆云弧光误写'骆秉渊'",
                "type": "name_typo", "wrong": "骆秉渊", "correct": "陆秉渊",
                "reason": "同一实体手滑写岔,多数派陆秉渊",
            }],
        )
        r = _call(mock_client, fake_prompts_dir)
        assert r.passed is False  # 有冲突 → 未过(待 caller 自动修)
        assert len(r.name_typo_fixes) == 1
        assert r.name_typo_fixes[0].wrong == "骆秉渊"
        assert r.name_typo_fixes[0].correct == "陆秉渊"
        assert r.has_non_fixable_contradiction is False  # 全是可修 name_typo

    def test_substantive_sets_non_fixable_no_fix(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_structured_response(
            contradictions=[{
                "desc": "主角结局在 plot 与卷内弧生死互斥",
                "type": "substantive", "wrong": "", "correct": "",
                "reason": "无唯一正解,需重生",
            }],
        )
        r = _call(mock_client, fake_prompts_dir)
        assert r.passed is False
        assert r.name_typo_fixes == []
        assert r.has_non_fixable_contradiction is True

    def test_mixed_typo_and_substantive_is_non_fixable(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_structured_response(
            contradictions=[
                {"desc": "名字写岔", "type": "name_typo",
                 "wrong": "骆秉渊", "correct": "陆秉渊", "reason": "手滑"},
                {"desc": "生死互斥", "type": "substantive",
                 "wrong": "", "correct": "", "reason": "重生"},
            ],
        )
        r = _call(mock_client, fake_prompts_dir)
        # 有 1 个可修 + 1 个不可修 → has_non_fixable=True(caller 据此维持 HARD)
        assert len(r.name_typo_fixes) == 1
        assert r.has_non_fixable_contradiction is True

    def test_name_typo_missing_correct_treated_non_fixable(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # type=name_typo 但 correct 缺失 → 信息不全,不进 fixes + 标 non_fixable。
        mock_client.complete.return_value = _make_structured_response(
            contradictions=[{
                "desc": "名字疑似写岔但没给正解", "type": "name_typo",
                "wrong": "骆秉渊", "correct": "", "reason": "不确定正解",
            }],
        )
        r = _call(mock_client, fake_prompts_dir)
        assert r.name_typo_fixes == []
        assert r.has_non_fixable_contradiction is True

    def test_legacy_str_contradictions_coerced_substantive(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 向后兼容:老 v1 格式 list[str] → 当 substantive(不会被误自动修)。
        mock_client.complete.return_value = _make_response(
            contradictions=["主角父亲名字互斥"], evidence="老格式",
        )
        r = _call(mock_client, fake_prompts_dir)
        assert r.passed is False
        assert r.name_typo_fixes == []
        assert r.has_non_fixable_contradiction is True


class TestApplyNameTypoFixes:
    """apply_name_typo_fixes:确定性归一 + 多数票护栏 + 子串/空护栏。

    用 fixture 高频名「青云门」(沈雪/朱长老 background 各 1 = ≥2 次)作 correct;
    wrong「青云阁」用**追加注入**(不替换、不减 correct 计数)保证多数票稳定。
    """

    def _outline(self):
        return (_make_world(), _make_characters(), _make_plot(), _make_volume_plans())

    def _inject_typo_into_plot(self, plot: PlotOutput, wrong: str) -> PlotOutput:
        # 追加一处 wrong 到 core_conflict(不动既有 correct 计数)。
        return PlotOutput.model_validate_json(
            plot.model_dump_json().replace(
                plot.core_conflict, plot.core_conflict + f"（有人误称{wrong}）", 1
            )
        )

    def test_majority_correct_applied(self) -> None:
        w, c, p, v = self._outline()
        correct_n = sum(
            o.model_dump_json().count("青云门") for o in (w, c, p, v)
        )
        assert correct_n >= 2  # fixture 保证 correct 多数
        p_typo = self._inject_typo_into_plot(p, "青云阁")
        fixes = [NameTypoFix(wrong="青云阁", correct="青云门", desc="t")]
        (_, _, np_, _), applied, skipped = apply_name_typo_fixes(
            world=w, characters=c, plot=p_typo, volume_plans=v, fixes=fixes,
        )
        assert len(applied) == 1
        assert "青云阁" not in np_.model_dump_json()  # wrong 已归一为 correct

    def test_minority_correct_rejected_by_majority_guard(self) -> None:
        # 标反:wrong=青云门(多数≥2) correct=青云阁(0 次)→ 多数票护栏拦下。
        w, c, p, v = self._outline()
        fixes = [NameTypoFix(wrong="青云门", correct="青云阁", desc="backwards")]
        (_, _, _, _), applied, skipped = apply_name_typo_fixes(
            world=w, characters=c, plot=p, volume_plans=v, fixes=fixes,
        )
        assert applied == []
        assert len(skipped) == 1
        assert "多数票" in skipped[0]["reason"]

    def test_substring_overlap_rejected(self) -> None:
        w, c, p, v = self._outline()
        fixes = [NameTypoFix(wrong="青云", correct="青云门", desc="substr")]
        (_, _, _, _), applied, skipped = apply_name_typo_fixes(
            world=w, characters=c, plot=p, volume_plans=v, fixes=fixes,
        )
        assert applied == []
        assert "子串" in skipped[0]["reason"]

    def test_empty_fixes_noop_returns_same_objects(self) -> None:
        w, c, p, v = self._outline()
        (nw, nc, np_, nv), applied, skipped = apply_name_typo_fixes(
            world=w, characters=c, plot=p, volume_plans=v, fixes=[],
        )
        assert applied == [] and skipped == []
        assert nw is w and nc is c and np_ is p and nv is v  # 零开销原样返回


def test_experimental_consistency_receives_background_and_opening_facts(mock_client):
    mock_client.creative_delta_capability = "creative_delta_v6"
    _call(mock_client, Path(__file__).resolve().parents[2] / "prompts")
    messages = mock_client.complete.call_args.kwargs["messages"]
    message = messages[1].content
    for character in _make_characters().characters:
        assert character.background in message
    assert "initial_core_facts" in message
    assert _make_world().setting in message
    assert "情节逻辑 / 设定合理性不在 HARD 范围" not in messages[0].content
    assert "审核对象是这份原样输入" in message


def test_experimental_consistency_preserves_all_four_parts_once(mock_client, fake_prompts_dir):
    """普通 beat 与未被选进戏剧合同的物件不能在实际请求中消失。"""
    from src.core.models.outline import CharacterOutlineItem

    mock_client.creative_delta_capability = "creative_delta_v6"
    world, characters, plot, plans = (
        _make_world(), _make_characters(), _make_plot(), _make_volume_plans(),
    )
    raw_plot = plot.model_dump(mode="json")
    raw_plot["load_bearing_objects"] = [{
        "object_id": "sealed-note", "label": "师傅的封存手札",
        "causal_role": "close", "causal_requirement": "必须拿到师傅签名才能打开封条",
        "relation_attributes": ["access"], "linked_thread_id": "shi-fu",
    }]
    plot = PlotOutput.model_validate(raw_plot)
    plans.volumes[0].arcs[0].beats = [VolumeBeatPlan(
        beat_id="plain-opening", chapter_range=(1, 1),
        summary="普通开篇中先保管手札", must_advance=["确认封条仍完整"],
        forbidden_advance=["提前打开手札"], dramatic_contract=None,
    )]
    expected = {name: item.model_dump(mode="json") for name, item in (
        ("world", world), ("characters", characters), ("plot", plot),
        ("volume_plans", plans),
    )}
    result = validate_outline_consistency(
        world=world, characters=characters, plot=plot, volume_plans=plans,
        premise="先保管，再寻找开启手札的依据", genre="修仙", workspace_id="ws-demo",
        client=mock_client, model="test", prompts_dir=fake_prompts_dir,
    )
    assert result.passed
    mock_client.complete.assert_called_once()
    message = mock_client.complete.call_args.kwargs["messages"][1].content
    definitions = message.split("## 人物字段的开篇档案定义\n", 1)[1]
    for name in ("background", "motivation", "relationships_brief", "initial_core_facts"):
        assert CharacterOutlineItem.model_fields[name].description in definitions
    assert "明确尚未实现的愿望或计划不算已发生" in definitions
    body = message.split("## 四段大纲原文\n", 1)[1]
    received, _ = json.JSONDecoder().raw_decode(body)
    assert received == expected
    assert message.count(world.setting) == 1
    assert message.count(characters.characters[0].motivation) == 1
    assert "确认封条仍完整" in message
    assert "提前打开手札" in message
    assert "必须拿到师傅签名才能打开封条" in message


def test_shared_opening_context_keeps_existing_callers_unchanged():
    from src.core.agents.story_authority_context import render_story_authority_context

    world, characters = _make_world(), _make_characters()
    message = render_story_authority_context(world=world, characters=characters)
    body = message[message.index("{\n"):]
    assert json.loads(body) == {
        "world": world.model_dump(mode="json"),
        "characters": [c.model_dump(mode="json") for c in characters.characters],
    }
    assert "四段大纲原文" not in message
    assert "人物字段的开篇档案定义" not in message


@pytest.mark.parametrize("failure", ["empty", "transport", "missing_verdict"])
def test_experimental_consistency_cannot_treat_absent_verdict_as_pass(mock_client, fake_prompts_dir, failure):
    mock_client.creative_delta_capability = "creative_delta_v6"
    if failure == "transport":
        mock_client.complete.side_effect = RuntimeError("network")
    elif failure == "missing_verdict":
        mock_client.complete.return_value = LLMResponse(
            content=json.dumps({"evidence": "已阅读大纲"}), model_used="test",
            usage=TokenUsage(input_tokens=1, output_tokens=1), finish_reason="stop",
        )
    else:
        mock_client.complete.return_value = LLMResponse(content="", model_used="test",
            usage=TokenUsage(input_tokens=1, output_tokens=1), finish_reason="stop")
    with pytest.raises(RuntimeError, match="不能当作通过"):
        _call(mock_client, fake_prompts_dir)
    assert mock_client.complete.call_count == 1


def test_experimental_full_review_accepts_exact_premise_quote(mock_client, fake_prompts_dir):
    mock_client.creative_delta_capability = "creative_delta_v6"
    premise = "废柴少年重生改写末法时代,与魔域大尊的宿命对决。"
    quotes = [
        {"path": "premise", "quote": "废柴少年重生改写末法时代"},
        {"path": "characters.characters.0.background", "quote": "重生回入山门那年"},
    ]
    payload = {"contradictions": [{"desc": "开篇前史冲突", "type": "substantive",
                "reason": "同一主角的前史不能同时成立", "source_quotes": quotes}],
               "coverage_gaps": [], "evidence": "离线假审核", "repair_scope": "upstream"}
    mock_client.complete.return_value = LLMResponse(
        content=json.dumps(payload, ensure_ascii=False), model_used="fake",
        usage=TokenUsage(input_tokens=1, output_tokens=1), finish_reason="stop",
    )
    result = validate_outline_consistency(
        world=_make_world(), characters=_make_characters(), plot=_make_plot(),
        volume_plans=_make_volume_plans(), premise=premise, genre="修仙",
        workspace_id="ws-demo", client=mock_client, model="fake",
        prompts_dir=fake_prompts_dir,
    )
    assert not result.passed
    assert result.contradiction_details[0]["source_quotes"] == quotes


@pytest.mark.parametrize("stage", ["characters", "plot"])
def test_stage_fact_review_uses_exact_source_quotes_and_blocks_conflict(
    mock_client, fake_prompts_dir, stage,
):
    world, characters, plot = _make_world(), _make_characters(), _make_plot()
    if stage == "characters":
        quotes = [{"path": "world.factions", "quote": world.factions},
                  {"path": "characters.characters.0.background", "quote": characters.characters[0].background}]
    else:
        quotes = [{"path": "characters.characters.0.background", "quote": characters.characters[0].background},
                  {"path": "plot.core_conflict", "quote": plot.core_conflict}]
    payload = {"contradictions": [{"desc": "同一次事件方向相反", "type": "substantive",
                "reason": "两段声称同一事实却方向相反", "source_quotes": quotes}],
               "coverage_gaps": [], "evidence": "离线假审核", "repair_scope": "upstream"}
    mock_client.complete.return_value = LLMResponse(content=json.dumps(payload, ensure_ascii=False),
        model_used="fake", usage=TokenUsage(input_tokens=1, output_tokens=1), finish_reason="stop")
    result = validate_outline_stage_consistency(stage=stage, world=world, characters=characters,
        plot=plot if stage == "plot" else None, client=mock_client, model="fake", prompts_dir=fake_prompts_dir)
    assert not result.passed
    assert result.contradiction_details[0]["source_quotes"] == quotes
    assert mock_client.complete.call_count == 1
    assert "FAKE_STAGE_FACT_PROMPT" in mock_client.complete.call_args.kwargs["messages"][0].content


def test_stage_fact_review_rejects_invented_quote_without_pass(mock_client, fake_prompts_dir):
    payload = {"contradictions": [{"desc": "矛盾", "type": "substantive", "reason": "同一事件相反",
        "source_quotes": [{"path": "world.factions", "quote": "伪造原文"},
                          {"path": "characters.characters.0.background", "quote": "重生回入山门那年"}]}],
        "coverage_gaps": [], "evidence": "离线假审核", "repair_scope": "upstream"}
    mock_client.complete.return_value = LLMResponse(content=json.dumps(payload, ensure_ascii=False),
        model_used="fake", usage=TokenUsage(input_tokens=1, output_tokens=1), finish_reason="stop")
    with pytest.raises(ValueError, match="引文不在"):
        validate_outline_stage_consistency(stage="characters", world=_make_world(),
            characters=_make_characters(), client=mock_client, model="fake", prompts_dir=fake_prompts_dir)


def test_stage_fact_review_accepts_exact_premise_quote(mock_client, fake_prompts_dir):
    premise = "沈砚的师父因十五年前救灾失误而不愿再介入"
    characters = _make_characters()
    quotes = [{"path": "premise", "quote": premise},
              {"path": "characters.characters.0.background",
               "quote": characters.characters[0].background[:12]}]
    payload = {"contradictions": [{"desc": "师承前史冲突", "type": "substantive",
                "reason": "同一人的同一师父与救灾前史不能同时成立", "source_quotes": quotes}],
               "coverage_gaps": [], "evidence": "离线假审核", "repair_scope": "upstream"}
    mock_client.complete.return_value = LLMResponse(content=json.dumps(payload, ensure_ascii=False),
        model_used="fake", usage=TokenUsage(input_tokens=1, output_tokens=1), finish_reason="stop")
    result = validate_outline_stage_consistency(stage="characters", world=_make_world(),
        characters=characters, premise=premise, client=mock_client, model="fake",
        prompts_dir=fake_prompts_dir)
    assert not result.passed
    assert result.contradiction_details[0]["source_quotes"] == quotes


def test_stage_fact_review_json_handoff_carries_sources_once(mock_client, fake_prompts_dir):
    mock_client.creative_delta_capability = "creative_delta_v6"
    mock_client.handoff_format = "json"
    mock_client.complete.return_value = _make_response(repair_scope="none")
    world, characters = _make_world(), _make_characters()
    result = validate_outline_stage_consistency(stage="characters", world=world,
        characters=characters, client=mock_client, model="fake", prompts_dir=fake_prompts_dir)
    assert result.passed
    packet = HandoffPacket.model_validate_json(mock_client.complete.call_args.kwargs["messages"][1].content)
    assert packet.data["world"] == world.model_dump(mode="json")
    assert packet.data["characters"] == characters.model_dump(mode="json")
    assert world.setting not in packet.original_task()


def test_full_review_json_handoff_keeps_complete_source_without_duplicate_text(
    mock_client, fake_prompts_dir,
):
    from src.core.agents.outline_review_diagnostic import read_original

    mock_client.creative_delta_capability = "creative_delta_v6"
    mock_client.handoff_format = "json"
    mock_client.complete.return_value = _make_response(repair_scope="none")
    assert _call(mock_client, fake_prompts_dir).passed
    messages = mock_client.complete.call_args.kwargs["messages"]
    packet = HandoffPacket.model_validate_json(messages[1].content)
    expected = {
        "world": _make_world().model_dump(mode="json"),
        "characters": _make_characters().model_dump(mode="json"),
        "plot": _make_plot().model_dump(mode="json"),
        "volume_plans": _make_volume_plans().model_dump(mode="json"),
        "premise": "废柴少年重生改写末法时代,与魔域大尊的宿命对决。",
        "genre": "修仙",
    }
    assert packet.data == expected
    assert "## 四段大纲原文" in packet.original_task()
    assert "## 人物字段的开篇档案定义" in packet.original_task()
    assert _make_world().unique_rules not in packet.original_task()
    assert messages[1].content.count(_make_world().unique_rules) == 1
    index, _, _ = read_original(messages)
    assert index.data == expected


def test_partitioned_review_requires_five_valid_stage_verdicts(mock_client, fake_prompts_dir):
    seen = []

    def answer(*, messages, **_kwargs):
        data = HandoffPacket.model_validate_json(messages[1].content).data
        stage = data["review_stage"]
        seen.append((stage, set(data)))
        payload = {"review_stage": stage, "source_hash": data["source_hash"],
            "outcome": "compatible", "reviewed_sections":
                ["world", "characters", "plot", "volume_plans"] if stage == "cross" else [stage],
            "contradictions": [], "coverage_gaps": [], "requested_paths": [],
            "evidence": "本项原文可承接", "repair_scope": "none"}
        return LLMResponse(content=json.dumps(payload), model_used="fake",
            usage=TokenUsage(input_tokens=1, output_tokens=1), finish_reason="stop")

    mock_client.complete.side_effect = answer
    result = validate_outline_consistency_partitioned(
        world=_make_world(), characters=_make_characters(), plot=_make_plot(),
        volume_plans=_make_volume_plans(), client=mock_client, model="fake",
        prompts_dir=fake_prompts_dir)
    assert result.passed and result.llm_calls_count == 5
    assert [stage for stage, _ in seen] == ["world", "characters", "plot", "volume_plans", "cross"]
    assert "volume_plans" not in seen[2][1]
    assert "volume_plans" in seen[3][1]


def test_partitioned_review_unknown_stops_before_later_paid_calls(mock_client, fake_prompts_dir):
    def answer(*, messages, **_kwargs):
        data = HandoffPacket.model_validate_json(messages[1].content).data
        stage = data["review_stage"]
        payload = {"review_stage": stage, "source_hash": data["source_hash"],
            "outcome": "unknown" if stage == "characters" else "compatible",
            "reviewed_sections": [stage], "contradictions": [], "coverage_gaps": [],
            "requested_paths": ["plot.core_conflict"] if stage == "characters" else [],
            "evidence": "尚缺同一事件的时间", "repair_scope": "unclear" if stage == "characters" else "none"}
        return LLMResponse(content=json.dumps(payload), model_used="fake",
            usage=TokenUsage(input_tokens=1, output_tokens=1), finish_reason="stop")

    mock_client.complete.side_effect = answer
    with pytest.raises(RuntimeError, match="资料不足"):
        validate_outline_consistency_partitioned(
            world=_make_world(), characters=_make_characters(), plot=_make_plot(),
            volume_plans=_make_volume_plans(), client=mock_client, model="fake",
            prompts_dir=fake_prompts_dir)
    assert mock_client.complete.call_count == 2


def test_partitioned_review_rejects_missing_cross_section_evidence(mock_client, fake_prompts_dir):
    world = _make_world()

    def answer(*, messages, **_kwargs):
        data = HandoffPacket.model_validate_json(messages[1].content).data
        stage = data["review_stage"]
        conflict = {"desc": "虚构冲突", "reason": "同一行动相反", "source_quotes": [
            {"path": "world.setting", "quote": world.setting[:4]},
            {"path": "world.power_system", "quote": world.power_system[:4]}]}
        payload = {"review_stage": stage, "source_hash": data["source_hash"],
            "outcome": "conflict" if stage == "cross" else "compatible",
            "reviewed_sections": ["world", "characters", "plot", "volume_plans"] if stage == "cross" else [stage],
            "contradictions": [conflict] if stage == "cross" else [],
            "coverage_gaps": [], "requested_paths": [], "evidence": "检查来源",
            "repair_scope": "upstream" if stage == "cross" else "none"}
        return LLMResponse(content=json.dumps(payload, ensure_ascii=False), model_used="fake",
            usage=TokenUsage(input_tokens=1, output_tokens=1), finish_reason="stop")

    mock_client.complete.side_effect = answer
    with pytest.raises(RuntimeError, match="没有有效结论"):
        validate_outline_consistency_partitioned(
            world=world, characters=_make_characters(), plot=_make_plot(),
            volume_plans=_make_volume_plans(), client=mock_client, model="fake",
            prompts_dir=fake_prompts_dir)
    assert mock_client.complete.call_count == 5


def test_split_review_cannot_claim_unseen_sections_as_covered():
    from src.core.agents.outline_review_packets import digest

    source = {"world": _make_world().model_dump(mode="json"),
              "characters": _make_characters().model_dump(mode="json"),
              "plot": _make_plot().model_dump(mode="json"),
              "volume_plans": _make_volume_plans().model_dump(mode="json"),
              "premise": "", "genre": ""}
    visible = {"world": source["world"], "premise": "", "genre": ""}
    payload = _SplitReviewPayload(review_stage="world", source_hash=digest(visible),
        outcome="compatible", reviewed_sections=["world", "volume_plans"],
        contradictions=[], coverage_gaps=[], requested_paths=[], evidence="没有矛盾",
        repair_scope="none")
    with pytest.raises(ValueError, match="未提供"):
        validate_split_review_payload(payload, stage="world", source=source)


@pytest.mark.parametrize("path", ["user_request.premise", "premise", "world.story_intent.user_request.premise"])
def test_split_review_accepts_verified_user_source_visible_in_json_handoff(path):
    from src.core.models.narrative import UserNarrativeRequest
    from src.core.agents.outline_review_packets import digest
    from src.core.agents.structured_handoff import build_packet

    request = UserNarrativeRequest.from_source(premise="少年因资质平庸落选", genre="修仙").model_dump(mode="json")
    source = {"world": {"setting": "有灵根的孩子都能入门", "story_intent": {"user_request": request}},
              "premise": request["premise"], "genre": request["genre"]}
    visible = build_packet(stage="review", data=source, original_task="").data
    assert visible["user_request"] == request
    payload = _SplitReviewPayload(review_stage="world", source_hash=digest(source), outcome="conflict",
        reviewed_sections=["world"], contradictions=[{"desc": "录取条件矛盾", "reason": "同一录取与落选条件互斥",
            "source_quotes": [{"path": "world.setting", "quote": "有灵根的孩子都能入门"},
                              {"path": path, "quote": "因资质平庸落选"}]}],
        coverage_gaps=[], requested_paths=[], evidence="两端原文确有矛盾", repair_scope="upstream")
    assert validate_split_review_payload(payload, stage="world", source=source).outcome == "conflict"


@pytest.mark.parametrize("fault", ["forged_quote", "unbound_source", "bad_source_hash"])
def test_user_source_alias_still_rejects_invented_or_unbound_evidence(fault):
    from src.core.models.narrative import UserNarrativeRequest
    from src.core.agents.outline_review_packets import digest

    request = UserNarrativeRequest.from_source(premise="少年因资质平庸落选", genre="修仙").model_dump(mode="json")
    if fault == "bad_source_hash":
        request["source_sha256"] = "0" * 64
    source = {"world": {"setting": "有灵根的孩子都能入门", "story_intent": {"user_request": request}},
              "premise": request["premise"], "genre": request["genre"]}
    if fault == "unbound_source":
        source["world"]["story_intent"] = {}
    quote = "凭空编造的落选规则" if fault == "forged_quote" else "因资质平庸落选"
    payload = _SplitReviewPayload(review_stage="world", source_hash=digest(source), outcome="conflict",
        reviewed_sections=["world"], contradictions=[{"desc": "录取条件矛盾", "reason": "同条件互斥",
            "source_quotes": [{"path": "world.setting", "quote": "有灵根的孩子都能入门"},
                              {"path": "user_request.premise", "quote": quote}]}],
        coverage_gaps=[], requested_paths=[], evidence="检查原文", repair_scope="upstream")
    with pytest.raises(ValueError):
        validate_split_review_payload(payload, stage="world", source=source)


@pytest.mark.parametrize("design_path", [
    "scope_outcome", "protagonist_start", "narrative_tone",
    "opening_orientation", "progression_design.story_engine",
    "scope_experience.core_experience", "planning_scope.mode",
])
def test_generated_story_intent_cannot_be_hard_fact_evidence(design_path):
    from src.core.agents.outline_consistency_validator import (
        _OutlineConsistencyLLMPayload, _validate_source_quotes,
    )
    intent = {}
    cursor = intent
    parts = design_path.split(".")
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = "早期设想：原株随主角远行"
    source = {"world": {"story_intent": intent, "power_system": "草离原脉过久叶枯"},
              "plot": {"core_conflict": "原株留在原脉，主角远行"}}
    before = json.dumps(source, ensure_ascii=False, sort_keys=True)
    payload = _OutlineConsistencyLLMPayload(contradictions=[{
        "desc": "早期设计与后续实现不同", "reason": "原株去向不同",
        "source_quotes": [
            {"path": "world.story_intent." + design_path, "quote": cursor[parts[-1]]},
            {"path": "plot.core_conflict", "quote": source["plot"]["core_conflict"]},
            {"path": "world.power_system", "quote": source["world"]["power_system"]},
        ],
    }], coverage_gaps=[], evidence="模型试图将设想当事实", repair_scope="upstream")
    with pytest.raises(ValueError, match="模型设想不能作为硬矛盾"):
        _validate_source_quotes(payload, source)
    assert json.dumps(source, ensure_ascii=False, sort_keys=True) == before


def test_actual_world_fact_conflict_still_valid_with_generated_intent_present():
    from src.core.agents.outline_consistency_validator import (
        _OutlineConsistencyLLMPayload, _validate_source_quotes,
    )
    source = {"world": {"story_intent": {"scope_outcome": "早期设计"},
                        "power_system": "原株离脉过久会叶枯"},
              "plot": {"core_conflict": "原株离脉十年始终鲜活"}}
    payload = _OutlineConsistencyLLMPayload(contradictions=[{
        "desc": "正式事实互斥", "reason": "同一株同一离脉条件下会枯与始终鲜活相反",
        "source_quotes": [{"path": "world.power_system", "quote": source["world"]["power_system"]},
                          {"path": "plot.core_conflict", "quote": source["plot"]["core_conflict"]}],
    }], coverage_gaps=[], evidence="真实规则冲突", repair_scope="upstream")
    _validate_source_quotes(payload, source)
    assert payload.contradictions


def test_active_review_prompts_distinguish_generated_design_from_user_facts():
    repo = Path(__file__).resolve().parents[2]
    for name in ("outline_stage_consistency_validator_v3.md",
                 "outline_split_consistency_validator_v4.md"):
        text = (repo / "prompts" / name).read_text()
        assert "早期创作设想" in text
        assert "不能单独作硬矛盾" in text
        assert "用户原文" in text
