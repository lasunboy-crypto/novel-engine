"""``src.core.rules.validator_pipeline`` 集成测(P4-B-06)。

两层验证:

1. **pipeline 层**:``run_validator_pipeline`` 全过 / 失败时的报告字段 +
   structlog ``validator_passed`` / ``validator_failed`` 事件;
2. **Orchestrator 集成层**:mock 路径构造违反 must_achieve 的 Writer 产出,
   Reviewer pass 但 validator 检出 → ``review.verdict`` 覆盖为 ``rewrite`` →
   Orchestrator 进 REWRITING 分支 +1 → ``validator_failed`` 事件落日志。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import structlog
from structlog.testing import capture_logs

from src.config import settings
from src.core.agents import _prompt as prompt_module
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.orchestrator.chapter_orchestrator import (
    PrewriteAdmissionDataError,
    orchestrate_chapter,
)
from src.core.rules.prose_hard_contract import compile_executable_constraint_plan
from src.core.rules.style_drift_validator import (
    resolve_rewrite_style_constraint_spec,
)
from src.core.rules.validator_pipeline import run_validator_pipeline
from src.core.state_machine.states import ChapterState
from src.core.workspace.workspace_paths import WorkspacePaths


# ---------------------------------------------------------------------------
# fixture / 工厂(对齐 test_orchestrator.py)
# ---------------------------------------------------------------------------


def _make_continuity(**overrides) -> ContinuityContext:
    payload = dict(
        prev_chapter_tail="他握紧剑柄,目光不离对面那道身影。",
        unfinished_actions=[],  # P4-B-07:衔接桥 unfinished_actions_continuation 默认通过
        time_gap="立即续接",
        scene_continuation="switch",  # P4-B-07:scene_continuation_location 默认跳过(只有 same 时检查)
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
        word_count_min=100,  # 测试构造的 mock 正文较短,放宽下限
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


def _writer_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        model_used="claude-sonnet-4-6-20250101",
        usage=TokenUsage(input_tokens=1000, output_tokens=2000),
        finish_reason="end_turn",
    )


def _reviewer_payload(*, score: float = 8.0) -> dict:
    return {
        "score_plot_progression": score,
        "score_tension_match": score,
        "score_prose_quality": score,
        "score_consistency": score,
        "score_hook_management": score,
        "score_voice_consistency": score,
        "hard_constraints_passed": True,
        "failed_constraints": [],
        "issues": [],
        "suggestions": [],
    }


def _reviewer_response(*, score: float = 8.0) -> LLMResponse:
    return LLMResponse(
        content=json.dumps(_reviewer_payload(score=score), ensure_ascii=False),
        model_used="claude-sonnet-4-6-20250101",
        usage=TokenUsage(input_tokens=1500, output_tokens=400),
        finish_reason="end_turn",
    )


def _contract_sequence(*responses):
    """让指定响应依据当次动态 revision contract 生成。"""

    iterator = iter(responses)

    def _next(*args, **kwargs):
        del args
        response = next(iterator)
        if callable(response):
            return response(kwargs["messages"])
        return response

    return _next


def _localized_writer_response(messages, *, replacement: str) -> LLMResponse:
    """从 Writer user message 读取冻结身份并返回合法单段 patch。"""

    user_text = messages[1].content
    identity_tail = user_text.split("### 冻结身份与允许范围", 1)[1]
    identity_json = identity_tail.split("```json\n", 1)[1].split("\n```", 1)[0]
    identity = json.loads(identity_json)
    target = identity["targets"][0]
    payload = {
        "contract_version": "1",
        "mode": "localized",
        "source_version_id": identity["source_version_id"],
        "source_sha256": identity["source_sha256"],
        "plan_hash": identity["plan_hash"],
        "patches": [
            {
                "target_ids": [target["target_id"]],
                "anchor_id": target["allowed_anchor_ids"][0],
                "operation": target["allowed_operations"][0],
                "replacement": replacement,
            }
        ],
    }
    return _writer_response(json.dumps(payload, ensure_ascii=False))


def _contract_aware_writer_response(messages, *, global_content: str) -> LLMResponse:
    """GLOBAL 返整章；LOCALIZED 依据动态合同返单段 patch。"""

    user_text = messages[1].content
    if "LOCALIZED PATCH" in user_text:
        return _localized_writer_response(messages, replacement=global_content)
    return _writer_response(global_content)


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts_root"
    prompts_dir.mkdir()
    (prompts_dir / "writer.md").write_text("FAKE_WRITER", encoding="utf-8")
    (prompts_dir / "reviewer.md").write_text("FAKE_REVIEWER", encoding="utf-8")
    (prompts_dir / "reviser.md").write_text("FAKE_REVISER", encoding="utf-8")
    (prompts_dir / "word_count_trim.md").write_text(
        "FAKE_WORD_COUNT_TRIM", encoding="utf-8"
    )
    return prompts_dir


@pytest.fixture
def workspace_paths(tmp_path: Path) -> WorkspacePaths:
    # v1.0.x wave 6 s2.6a:预创建 paths.root 对齐生产语义,
    # 让 ChapterVersioning.write_version 新加的"workspace 销毁防御"正常 fallthrough。
    paths = WorkspacePaths("ws-demo", workspaces_root=tmp_path / "ws_root")
    paths.root.mkdir(parents=True, exist_ok=True)
    return paths


@pytest.fixture(autouse=True)
def _patch_settings_prompts_dir(
    monkeypatch: pytest.MonkeyPatch, fake_prompts_dir: Path
) -> None:
    monkeypatch.setattr(settings, "prompts_dir", fake_prompts_dir)


@pytest.fixture(autouse=True)
def _clear_prompt_cache():
    prompt_module.load_prompt_template.cache_clear()
    yield
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture(autouse=True)
def _ensure_debug_level_for_capture_logs() -> None:
    """``validator_passed`` 是 DEBUG level(OBSERVABILITY §2.4 字面)。
    其他测试可能调过 ``configure_logging`` 把 ``wrapper_class`` 设到 INFO,
    污染全局后 capture_logs 抓不到 DEBUG。每用例前重置 wrapper_class 到 DEBUG。
    """
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        cache_logger_on_first_use=False,
    )


@pytest.fixture
def mock_client() -> MagicMock:
    return MagicMock(spec=LLMClient)


# ---------------------------------------------------------------------------
# 1. pipeline 层:报告字段 + structlog 事件
# ---------------------------------------------------------------------------


class TestRunValidatorPipeline:
    """``run_validator_pipeline`` 全过 / 失败时的报告字段 + structlog 事件。"""

    def test_pipeline_passed_emits_validator_passed_event(self) -> None:
        card = _make_card()
        # 长度卡 100-4500;must_unresolved / forbidden 字段都满足
        # (v1.0.x A2-02 起 must_achieve 移交 LLM validator,本 pipeline 不再检)
        text = "他举步走向阵心,阵眼真凶身份依然成谜。" + "字" * 200

        with capture_logs() as logs:
            report = run_validator_pipeline(
                text, chapter_card=card, workspace_id="ws-demo"
            )

        assert report.passed
        assert report.failed_constraint_names == []
        assert all(r.passed for r in report.results)
        # validator_passed (DEBUG) — 单事件汇总
        passed_events = [
            e for e in logs if e.get("event") == "validator_passed"
        ]
        assert len(passed_events) == 1
        assert passed_events[0]["log_level"] == "debug"
        assert passed_events[0]["workspace_id"] == "ws-demo"
        assert passed_events[0]["chapter_id"] == "ch-0002"
        # v1.0.x wave 3 Commit D:衔接桥 §4.3 两条字面 substring 比对器从 pipeline
        # 移除调用(改由 LLM-based continuity_validator agent 接管),pipeline 节奏
        # 类 3 项(v1.0.x A2-02 起 must_achieve 也移交 LLM)是 caller 不传 ORM
        # 预读数据时的全部 validator 集合。
        assert set(passed_events[0]["validators"]) == {
            "word_count",
            "must_unresolved",
            "forbidden",
        }

    def test_tolerated_word_count_is_maintainer_log_not_writer_warning(self) -> None:
        card = _make_card(word_count_min=2500, word_count_max=4500)
        prefix = "阵眼真凶身份依然成谜。"
        text = prefix + "字" * (4737 - len(prefix))

        with capture_logs() as logs:
            report = run_validator_pipeline(
                text, chapter_card=card, workspace_id="ws-demo"
            )

        assert report.passed
        assert report.warning_signals == []
        tolerance_events = [
            event
            for event in logs
            if event.get("event") == "word_count_tolerance_accepted"
        ]
        assert len(tolerance_events) == 1
        assert tolerance_events[0]["actual_word_count"] == 4737
        assert tolerance_events[0]["target_max"] == 4500
        assert tolerance_events[0]["acceptance_max"] == 4950
        assert tolerance_events[0]["audience"] == "maintainer"

    def test_pipeline_failed_emits_one_event_per_failed_validator(self) -> None:
        # 字数过短(13 字 < 100)+ forbidden "手机" 命中 → 2 个 validator 失败,
        # emit 2 次。must_unresolved 因为 item 未在文中出现 → 默认通过。
        # (v1.0.x A2-02 起 must_achieve 移交 LLM validator,本 pipeline 不再检
        # 故不再触发 must_achieve 失败)
        card = _make_card(forbidden=["手机"])
        text = "他从背包里掏出手机查看消息。"

        with capture_logs() as logs:
            report = run_validator_pipeline(
                text, chapter_card=card, workspace_id="ws-demo"
            )

        assert not report.passed
        assert set(report.failed_constraint_names) == {
            "word_count",
            "forbidden",
        }
        # v1.0.x wave 3 Commit D:衔接桥 §4.3 两条从 pipeline 移除(LLM agent
        # 接管)— pipeline 现只剩节奏类 3 项。must_unresolved 因 item 未出现也通过。
        # 失败 2(word_count + forbidden) + 通过 1(must_unresolved) = 3,失败排前面。
        passed_flags = [r.passed for r in report.results]
        assert passed_flags[:2] == [False, False]
        assert all(p for p in passed_flags[2:])
        assert len(report.results) == 3

        failed_events = [e for e in logs if e.get("event") == "validator_failed"]
        assert len(failed_events) == 2
        assert {e["validator_name"] for e in failed_events} == {
            "word_count",
            "forbidden",
        }
        for e in failed_events:
            assert e["workspace_id"] == "ws-demo"
            assert e["chapter_id"] == "ch-0002"
            assert "details" in e
            assert e["log_level"] == "info"

    def test_pipeline_workspace_id_param_overrides_card_field(self) -> None:
        # P3-B-14 透传纪律:workspace_id 字段读 caller 透传值,不读
        # chapter_card.workspace_id(模板复用时字段可能漂移)。
        card = _make_card(workspace_id="ws-stale-card-field")
        text = "林衍突破第一道剑阵,阵眼真凶身份依然成谜。" + "字" * 200

        with capture_logs() as logs:
            run_validator_pipeline(
                text, chapter_card=card, workspace_id="ws-actual-runtime"
            )

        passed_events = [e for e in logs if e.get("event") == "validator_passed"]
        assert len(passed_events) == 1
        assert passed_events[0]["workspace_id"] == "ws-actual-runtime"
        # ws-stale-card-field 不应出现在任何事件字段
        for e in logs:
            for v in e.values():
                assert v != "ws-stale-card-field"


# ---------------------------------------------------------------------------
# 2. Orchestrator 集成:违反 must_achieve → 触发 rewrite + validator_failed
# ---------------------------------------------------------------------------


class TestOrchestratorValidatorIntegration:
    """mock 路径构造违反 forbidden 的 draft → validator 触发 rewrite 流程。

    v1.0.x A2-02 起 must_achieve 字面比对器从 pipeline 移除(交 LLM validator),
    本类测试改用 ``forbidden`` 触发 pipeline-driven rewrite —— 同样验证
    pipeline 失败覆盖 reviewer verdict 的核心契约。
    """

    def test_forbidden_violation_triggers_rewrite(
        self, mock_client: MagicMock, workspace_paths: WorkspacePaths
    ) -> None:
        # v1 草稿正文含 forbidden "手机" → validator 检出
        # v1 Reviewer 自报 hard_constraints_passed=True + score=8.0(伪正分)
        #   → Reviewer 自己判 pass
        #   但 validator 覆盖 verdict = rewrite → 进 REWRITING +1
        # v2 重写正文不含 forbidden → validator pass + Reviewer pass → APPROVED
        v1_text = "他握紧剑柄,掏出手机查看,迟迟未发动剑势。" + "字" * 150
        v2_text = "他举步走向阵心,推开石门。" + "字" * 200
        mock_client.complete.side_effect = _contract_sequence(
            _writer_response(content=v1_text),
            _reviewer_response(score=8.0),  # 自报 pass(LLM 自报硬约束 True)
            lambda messages: _contract_aware_writer_response(
                messages, global_content=v2_text
            ),
            _reviewer_response(score=8.5),  # v2 真 pass
        )

        with capture_logs() as logs:
            result = orchestrate_chapter(
                client=mock_client,
                chapter_card=_make_card(forbidden=["手机"]),
                paths=workspace_paths,
                memory_section="",
                workspace_id="ws-demo",
                model="claude-sonnet-4-6",
            )

        assert result.final_state == ChapterState.APPROVED
        assert result.rewrite_count == 1  # validator 触发 1 次 rewrite
        assert result.revise_count == 0
        assert [v.creation_type for v in result.versions] == ["draft", "rewrite"]
        # 第一次 review 经 validator 覆盖 → verdict=rewrite + failed_constraints
        # 含 forbidden;第二次 review 通过。
        assert result.reviews[0].verdict == "rewrite"
        assert "forbidden" in result.reviews[0].failed_constraints
        assert not result.reviews[0].hard_constraints_passed
        assert result.reviews[1].verdict == "pass"

        # validator_failed 事件至少一条 + validator_name=forbidden
        failed_events = [
            e
            for e in logs
            if e.get("event") == "validator_failed"
            and e.get("validator_name") == "forbidden"
        ]
        assert len(failed_events) >= 1
        assert failed_events[0]["chapter_id"] == "ch-0002"
        assert failed_events[0]["workspace_id"] == "ws-demo"

    def test_validator_pass_keeps_reviewer_verdict(
        self, mock_client: MagicMock, workspace_paths: WorkspacePaths
    ) -> None:
        # validator 全过 → review.verdict 不被覆盖,Reviewer pass 直接 APPROVED。
        good_text = "他举步走向阵心,阵眼真凶身份依然成谜。" + "字" * 200
        mock_client.complete.side_effect = [
            _writer_response(content=good_text),
            _reviewer_response(score=8.0),
        ]

        with capture_logs() as logs:
            result = orchestrate_chapter(
                client=mock_client,
                chapter_card=_make_card(),
                paths=workspace_paths,
                memory_section="",
                workspace_id="ws-demo",
                model="claude-sonnet-4-6",
            )

        assert result.final_state == ChapterState.APPROVED
        assert result.rewrite_count == 0
        assert result.reviews[0].verdict == "pass"
        # 不应有 validator_failed
        assert not any(e.get("event") == "validator_failed" for e in logs)
        # 应有一条 validator_passed
        assert any(e.get("event") == "validator_passed" for e in logs)

    def test_repeated_word_count_evidence_stops_before_third_writer(
        self, mock_client: MagicMock, workspace_paths: WorkspacePaths
    ) -> None:
        """rewrite 隔离：确定性 HARD 同证据连续失败时不再生成 v3。

        ``max_revises=0`` 有意绕开 §2.126 的专用 above-max trim，只验证
        §2.125 的 Writer rewrite 结构化无进展闸门。
        """

        # max=10 的隐藏验收上限为 11；12 才是真正的 deterministic HARD 失败。
        first_draft = "甲" * 12
        changed_rewrite = "乙" * 12
        mock_client.complete.side_effect = [
            _writer_response(content=first_draft),
            _reviewer_response(score=8.0),
            _writer_response(content=changed_rewrite),
            _reviewer_response(score=8.0),
        ]

        result = orchestrate_chapter(
            client=mock_client,
            chapter_card=_make_card(
                word_count_min=1,
                word_count_max=10,
                must_achieve=[],
                must_unresolved=[],
                forbidden=[],
                max_revises=0,
            ),
            paths=workspace_paths,
            memory_section="",
            workspace_id="ws-demo",
            model="claude-sonnet-4-6",
        )

        assert result.final_state == ChapterState.HUMAN_INTERVENTION
        assert result.escalation_reason == "repeated_hard_constraint_no_progress"
        assert result.rewrite_count == 1
        assert [version.creation_type for version in result.versions] == [
            "draft",
            "rewrite",
        ]
        assert mock_client.complete.call_count == 4

        first_hard = [
            item
            for item in result.reviews[0].constraint_violations
            if item.severity == "hard"
        ]
        second_hard = [
            item
            for item in result.reviews[1].constraint_violations
            if item.severity == "hard"
        ]
        assert len(first_hard) == len(second_hard) == 1
        assert first_hard[0].constraint_id == second_hard[0].constraint_id
        assert first_hard[0].evidence_quote == second_hard[0].evidence_quote
        assert result.escalation_details is not None
        assert result.escalation_details["constraint_ids"] == [
            first_hard[0].constraint_id
        ]

        rewrite_prompt = mock_client.complete.call_args_list[2].kwargs["messages"][
            1
        ].content
        assert first_hard[0].constraint_id in rewrite_prompt
        assert "| chapter.internal | fail |" in rewrite_prompt

    def test_validator_overrides_low_score_reviewer_verdict_p7_b_06(
        self, mock_client: MagicMock, workspace_paths: WorkspacePaths
    ) -> None:
        # P7-B-06:Reviewer 不再自报 ``failed_constraints``(LLM 输出该字段被
        # Pydantic 默认 ``extra=ignore`` 丢弃);本测试聚焦"低分 reviewer
        # 自然 verdict=rewrite + pipeline 也失败" 两路径合并 — final
        # failed_constraints 只剩 pipeline 检出的 forbidden(reviewer
        # 自报已无路径)。
        # (v1.0.x A2-02 起 must_achieve 移交 LLM validator,本测试 reframe 改
        # 用 forbidden 触发 pipeline 失败,保留两路径合并契约验证。)
        v1_text = "他握紧剑柄,掏出手机查看,迟迟未发动剑势。" + "字" * 150
        v2_text = "他举步走向阵心,推开石门。" + "字" * 200

        # 低分 reviewer payload:score=5.0 → reviewer 自然 verdict=rewrite
        # (不依赖 hard_constraints,P7-B-06 已删该路径)。pipeline 再叠加
        # forbidden 失败 → 最终 failed_constraints=['forbidden']。
        bad_resp = _reviewer_response(score=5.0)
        mock_client.complete.side_effect = _contract_sequence(
            _writer_response(content=v1_text),
            bad_resp,
            lambda messages: _contract_aware_writer_response(
                messages, global_content=v2_text
            ),
            _reviewer_response(score=8.5),
        )

        result = orchestrate_chapter(
            client=mock_client,
            chapter_card=_make_card(forbidden=["手机"]),
            paths=workspace_paths,
            memory_section="",
            workspace_id="ws-demo",
            model="claude-sonnet-4-6",
        )

        assert result.final_state == ChapterState.APPROVED
        assert result.rewrite_count == 1
        # Reviewer 自然给 rewrite + pipeline 也叠加 forbidden 失败
        assert result.reviews[0].verdict == "rewrite"
        # P7-B-06:reviewer 自报 failed_constraints 已删,只剩 pipeline 检出项
        assert result.reviews[0].failed_constraints == ["forbidden"]
        assert not result.reviews[0].hard_constraints_passed
        forbidden_results = [
            item
            for item in result.reviews[0].constraint_results
            if item.domain == "forbidden"
        ]
        assert len(forbidden_results) == 1
        assert forbidden_results[0].status == "fail"
        assert forbidden_results[0].source_path == "chapter_cards/ch-0002.forbidden"


# ---------------------------------------------------------------------------
# v1.1 G8:`classify_validator_severity` 集成路径 — light 派 revise,heavy 派 rewrite
# ---------------------------------------------------------------------------


class TestValidatorPipelineG8Severity:
    """v1.1 G8 reviewer/validator 互动分级覆盖集成测。

    覆盖 `_apply_validator_pipeline` 接 `classify_validator_severity` 后:
    - **light**(单条 word_count 失败 + 偏差 < 5%)→ verdict=revise,Reviser 真被调
    - **heavy**(其他)→ verdict=rewrite(沿用 P4-B-06 原行为)
    - 两条路径 review.validator_failure_details 字段都被写入(metrics / 历史可见)
    - structlog `validator_pipeline_verdict_dispatched` 事件 emitted(severity 字段)

    luo-cloud-test 21 章实证场景重演:71 次 reviewer verdict 0 次 revise → reviser
    路径 dead code in practice;G8 修法让 light 失败激活 reviser。
    """

    def test_word_count_within_hidden_tolerance_does_not_dispatch_revise(
        self, mock_client: MagicMock, workspace_paths: WorkspacePaths
    ) -> None:
        """96 位于 100 下限的隐藏 10% 容差内，Reviewer 后直接批准。"""
        v1_text = "字" * 96
        mock_client.complete.side_effect = [
            _writer_response(content=v1_text),
            _reviewer_response(score=8.0),
        ]

        with capture_logs() as logs:
            result = orchestrate_chapter(
                client=mock_client,
                chapter_card=_make_card(),  # word_count_min=100 默认
                paths=workspace_paths,
                memory_section="",
                workspace_id="ws-demo",
                model="claude-sonnet-4-6",
            )

        assert result.final_state == ChapterState.APPROVED
        assert result.revise_count == 0
        assert result.rewrite_count == 0
        assert result.reviews[0].verdict == "pass"
        assert result.reviews[0].failed_constraints == []
        assert [v.creation_type for v in result.versions] == ["draft"]
        assert mock_client.complete.call_count == 2
        dispatch_events = [
            e
            for e in logs
            if e.get("event") == "validator_pipeline_verdict_dispatched"
        ]
        assert dispatch_events == []
        assert any(
            event.get("event") == "word_count_tolerance_accepted"
            for event in logs
        )

    def test_above_max_uses_bounded_trim_and_counts_each_output(
        self, mock_client: MagicMock, workspace_paths: WorkspacePaths
    ) -> None:
        """R9 v4 根因回归：5000 超隐藏验收上限后走专用 trim。

        专用 trim 第一刀只压到 4970，代码量出仍超 4950，再把 4970 作为
        第二刀输入；第二刀 4200 达标后才落一个 revise 版本并重进完整审稿。
        """

        prefix = "他举步走向阵心,阵眼真凶身份依然成谜。"

        def sized_text(size: int) -> str:
            return prefix + "字" * (size - len(prefix))

        mock_client.complete.side_effect = [
            _writer_response(content=sized_text(5000)),
            _reviewer_response(score=8.0),
            _writer_response(content=sized_text(4970)),
            _writer_response(content=sized_text(4200)),
            _reviewer_response(score=8.5),
        ]

        result = orchestrate_chapter(
            client=mock_client,
            chapter_card=_make_card(word_count_min=2500, word_count_max=4500),
            paths=workspace_paths,
            memory_section="",
            workspace_id="ws-demo",
            model="claude-sonnet-4-6",
        )

        assert result.final_state == ChapterState.APPROVED
        assert result.revise_count == 1
        assert result.rewrite_count == 0
        assert [version.creation_type for version in result.versions] == [
            "draft",
            "revise",
        ]
        assert result.versions[-1].word_count == 4200
        assert mock_client.complete.call_count == 5

        first_trim = mock_client.complete.call_args_list[2].kwargs["messages"]
        second_trim = mock_client.complete.call_args_list[3].kwargs["messages"]
        assert first_trim[0].content == "FAKE_WORD_COUNT_TRIM"
        assert "当前 5000 字" in first_trim[1].content
        assert "至少要砍掉 500 字" in first_trim[1].content
        assert "当前 4970 字" in second_trim[1].content
        assert "至少要砍掉 470 字" in second_trim[1].content
        assert all(
            call.kwargs["messages"][0].content != "FAKE_REVISER"
            for call in mock_client.complete.call_args_list
        )

    def test_above_max_trim_not_converged_auto_rewrites_before_human(
        self, mock_client: MagicMock, workspace_paths: WorkspacePaths
    ) -> None:
        """两刀仍超上限不是人工边界：不落重复 revise、不复审，自动 rewrite。"""

        prefix = "他举步走向阵心,阵眼真凶身份依然成谜。"

        def sized_text(size: int) -> str:
            return prefix + "字" * (size - len(prefix))

        mock_client.complete.side_effect = _contract_sequence(
            _writer_response(content=sized_text(5000)),
            _reviewer_response(score=8.0),
            _writer_response(content=sized_text(4970)),
            _writer_response(content=sized_text(4970)),
            lambda messages: _contract_aware_writer_response(
                messages, global_content=sized_text(4200)
            ),
            _reviewer_response(score=8.5),
        )

        result = orchestrate_chapter(
            client=mock_client,
            chapter_card=_make_card(word_count_min=2500, word_count_max=4500),
            paths=workspace_paths,
            memory_section="",
            workspace_id="ws-demo",
            model="claude-sonnet-4-6",
        )

        assert result.final_state == ChapterState.APPROVED
        assert result.escalation_reason is None
        assert result.revise_count == 1
        assert result.rewrite_count == 1
        assert [version.creation_type for version in result.versions] == [
            "draft",
            "rewrite",
        ]
        assert result.versions[-1].word_count == 4200
        assert [review.version_reviewed for review in result.reviews] == [1, 2]
        assert mock_client.complete.call_count == 6
        rewrite_system = mock_client.complete.call_args_list[4].kwargs["messages"][0]
        assert rewrite_system.content == "FAKE_WRITER"

    def test_reactive_trim_does_not_repeat_failed_proactive_trim(
        self, mock_client: MagicMock, workspace_paths: WorkspacePaths
    ) -> None:
        """主动 trim 已用满两刀后，validator 不得再盲发第三、第四刀。"""

        prefix = "他举步走向阵心,阵眼真凶身份依然成谜。"

        def sized_text(size: int) -> str:
            return prefix + "字" * (size - len(prefix))

        mock_client.complete.side_effect = _contract_sequence(
            _writer_response(content=sized_text(5000)),
            # Writer 后主动 trim 两刀都没有收敛，最终仍保留 4600 原稿。
            _writer_response(content=sized_text(4970)),
            _writer_response(content=sized_text(4970)),
            _reviewer_response(score=8.0),
            # validator 确认 above-max 后应直接进 Writer rewrite，而不是再 trim。
            lambda messages: _contract_aware_writer_response(
                messages, global_content=sized_text(4200)
            ),
            _reviewer_response(score=8.5),
        )

        result = orchestrate_chapter(
            client=mock_client,
            chapter_card=_make_card(word_count_min=2500, word_count_max=4500),
            paths=workspace_paths,
            memory_section="",
            workspace_id="ws-demo",
            model="claude-sonnet-4-6",
            word_count_trim_enabled=True,
        )

        assert result.final_state == ChapterState.APPROVED
        assert result.escalation_reason is None
        assert result.revise_count == 1
        assert result.rewrite_count == 1
        assert [version.creation_type for version in result.versions] == [
            "draft",
            "rewrite",
        ]
        assert mock_client.complete.call_count == 6
        assert [
            call.kwargs["messages"][0].content
            for call in mock_client.complete.call_args_list
        ].count("FAKE_WORD_COUNT_TRIM") == 2
        assert mock_client.complete.call_args_list[4].kwargs["messages"][0].content == (
            "FAKE_WRITER"
        )

    def test_above_max_trim_below_card_min_is_not_persisted_as_success(
        self, mock_client: MagicMock, workspace_paths: WorkspacePaths
    ) -> None:
        """≤cap 但低于卡片下限不是达标 revise；不落瘦稿，自动 rewrite。"""

        prefix = "他举步走向阵心,阵眼真凶身份依然成谜。"

        def sized_text(size: int) -> str:
            return prefix + "字" * (size - len(prefix))

        mock_client.complete.side_effect = _contract_sequence(
            _writer_response(content=sized_text(5000)),
            _reviewer_response(score=8.0),
            _writer_response(content=sized_text(2000)),
            _writer_response(content=sized_text(1900)),
            lambda messages: _contract_aware_writer_response(
                messages, global_content=sized_text(4200)
            ),
            _reviewer_response(score=8.5),
        )

        result = orchestrate_chapter(
            client=mock_client,
            chapter_card=_make_card(word_count_min=2500, word_count_max=4500),
            paths=workspace_paths,
            memory_section="",
            workspace_id="ws-demo",
            model="claude-sonnet-4-6",
        )

        assert result.final_state == ChapterState.APPROVED
        assert result.revise_count == 1
        assert result.rewrite_count == 1
        assert [version.creation_type for version in result.versions] == [
            "draft",
            "rewrite",
        ]
        assert all(version.word_count >= 2500 for version in result.versions)

    def test_word_count_below_hidden_tolerance_dispatches_rewrite(
        self, mock_client: MagicMock, workspace_paths: WorkspacePaths
    ) -> None:
        """v1 writer 出 89 字(min=100,低于 90 容差边界)→ heavy → rewrite →
        进 REWRITING → v2 writer 出 200 字 → APPROVED(原 P4-B-06 行为不回归)。
        """
        v1_text = "字" * 89
        v2_text = "他举步走向阵心,阵眼真凶身份依然成谜。" + "字" * 200
        mock_client.complete.side_effect = _contract_sequence(
            _writer_response(content=v1_text),
            _reviewer_response(score=8.0),
            lambda messages: _contract_aware_writer_response(
                messages, global_content=v2_text
            ),
            _reviewer_response(score=8.5),
        )

        with capture_logs() as logs:
            result = orchestrate_chapter(
                client=mock_client,
                chapter_card=_make_card(),
                paths=workspace_paths,
                memory_section="",
                workspace_id="ws-demo",
                model="claude-sonnet-4-6",
            )

        assert result.final_state == ChapterState.APPROVED
        assert result.revise_count == 0
        assert result.rewrite_count == 1  # heavy 路径走 rewrite(P4-B-06 原行为)
        assert result.reviews[0].verdict == "rewrite"
        assert "word_count" in result.reviews[0].failed_constraints
        assert [v.creation_type for v in result.versions] == ["draft", "rewrite"]

        dispatch_events = [
            e
            for e in logs
            if e.get("event") == "validator_pipeline_verdict_dispatched"
        ]
        assert len(dispatch_events) == 1
        assert dispatch_events[0]["severity"] == "heavy"
        assert dispatch_events[0]["verdict"] == "rewrite"

    def test_multiple_validators_failed_dispatches_rewrite_heavy(
        self, mock_client: MagicMock, workspace_paths: WorkspacePaths
    ) -> None:
        """多 validator 同时失败 → heavy → rewrite。

        v1 writer 出 89 字(低于容差)+ forbidden 命中"手机" → 2 条
        validator 失败 → classify_validator_severity 严格判 heavy → rewrite。
        """
        v1_text = "他握紧手机。" + "字" * 83  # 89 字 + forbidden "手机" 命中
        v2_text = "他举步走向阵心,阵眼真凶身份依然成谜。" + "字" * 200
        mock_client.complete.side_effect = _contract_sequence(
            _writer_response(content=v1_text),
            _reviewer_response(score=8.0),
            lambda messages: _contract_aware_writer_response(
                messages, global_content=v2_text
            ),
            _reviewer_response(score=8.5),
        )

        result = orchestrate_chapter(
            client=mock_client,
            chapter_card=_make_card(forbidden=["手机"]),
            paths=workspace_paths,
            memory_section="",
            workspace_id="ws-demo",
            model="claude-sonnet-4-6",
        )

        assert result.final_state == ChapterState.APPROVED
        # 多 validator 失败 → heavy → rewrite 路径
        assert result.rewrite_count == 1
        assert result.revise_count == 0
        assert result.reviews[0].verdict == "rewrite"
        assert "forbidden" in result.reviews[0].failed_constraints
        assert "word_count" in result.reviews[0].failed_constraints

    def test_validator_failure_details_populated_in_review(
        self, mock_client: MagicMock, workspace_paths: WorkspacePaths
    ) -> None:
        """review.validator_failure_details 字段在 light + heavy 路径均被写入,
        含 name + details 完整结构(reviser / metrics / 历史 LogsPage 可消费)。
        """
        v1_text = "字" * 89  # 隐藏容差外，heavy
        v2_text = "他举步走向阵心,阵眼真凶身份依然成谜。" + "字" * 200
        mock_client.complete.side_effect = _contract_sequence(
            _writer_response(content=v1_text),
            _reviewer_response(score=8.0),
            lambda messages: _contract_aware_writer_response(
                messages, global_content=v2_text
            ),
            _reviewer_response(score=8.5),
        )

        result = orchestrate_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            paths=workspace_paths,
            memory_section="",
            workspace_id="ws-demo",
            model="claude-sonnet-4-6",
        )

        assert result.reviews[0].validator_failure_details is not None
        details = result.reviews[0].validator_failure_details
        assert len(details) >= 1
        # 至少一条是 word_count 失败(name + details 字段集对齐 ValidatorResult)
        word_count_failures = [d for d in details if d["name"] == "word_count"]
        assert len(word_count_failures) == 1
        wc_details = word_count_failures[0]["details"]
        assert wc_details["actual_word_count"] == 89
        assert wc_details["word_count_min"] == 100
        assert wc_details["reason"] == "below_min"

        # v2 review pass — validator_failure_details 应保持 None 默认
        assert result.reviews[1].validator_failure_details is None

    def test_tolerance_details_are_not_injected_into_reviser_prompt(
        self,
        mock_client: MagicMock,
        workspace_paths: WorkspacePaths,
        fake_prompts_dir: Path,
    ) -> None:
        """容差通过不是失败/WARN，不调用 Reviser，也不向任何上游泄露边界。"""
        v1_text = "字" * 96
        mock_client.complete.side_effect = [
            _writer_response(content=v1_text),
            _reviewer_response(score=8.0),
        ]

        result = orchestrate_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            paths=workspace_paths,
            memory_section="",
            workspace_id="ws-demo",
            model="claude-sonnet-4-6",
        )

        assert result.final_state == ChapterState.APPROVED

        assert mock_client.complete.call_count == 2
        all_prompt_text = "\n".join(
            message.content
            for call in mock_client.complete.call_args_list
            for message in call.kwargs["messages"]
        )
        assert "acceptance_min" not in all_prompt_text
        assert "acceptance_max" not in all_prompt_text


# ---------------------------------------------------------------------------
# 3. P4-acceptance-followup wire P4-B-07 第二批 dispatch 集成测
# ---------------------------------------------------------------------------


class TestWireSecondBatchValidatorDispatch:
    """角色 admission fail-closed；写后 timeline / voice 观察按新分级执行。"""

    @staticmethod
    def _make_wiring_factory(tmp_path: Path):
        """构造真实 in-memory SQLite session_factory(Writer 内 get_character
        走真实空表返回 None,不破 voice_section 路径)。返回零参 factory。"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from src.core.db.models import Base, ChapterORM, WorkspaceORM

        db_path = tmp_path / "wire_test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        now = datetime.now(UTC)
        with Session(engine) as session:
            session.add(
                WorkspaceORM(
                    workspace_id="ws-demo",
                    display_name="validator wiring",
                    genre="test",
                    premise="validator wiring",
                    created_at=now,
                    last_active_at=now,
                    engine_version="0.1.0",
                    status="active",
                    total_chapters=1,
                    completed_chapters=0,
                    config_overrides={},
                )
            )
            session.add(
                ChapterORM(
                    chapter_id="ch-0002",
                    workspace_id="ws-demo",
                    current_version=0,
                    status="drafting",
                    rewrite_count=0,
                    revise_count=0,
                    created_at=now,
                    approved_at=None,
                    total_token_usage={},
                    total_cost_usd=0.0,
                )
            )
            session.commit()

        def _factory() -> Session:
            return Session(engine)

        return _factory

    def test_timeline_inversion_warn_only_does_not_trigger_rewrite(
        self,
        mock_client: MagicMock,
        workspace_paths: WorkspacePaths,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """S3(v1.1 w4 b1):预读 TimelineEntry 时间倒流 → timeline_monotonic **降级
        warn-only** → 不触发 rewrite → 章节正常 APPROVED。

        time_order 是已落库记忆数据,正文 rewrite 改不动它;让它触发 rewrite 是错配
        (旧行为:倒流 → rewrite 空转 → human_intervention)。S3 把 timeline_monotonic
        在正文流程降级为瞭望哨(仅 warn 留痕),time_order 正确性改由生产端
        ``memory_extractor._persist_timeline`` 全局基数硬注入保证。"""
        from src.core.models.timeline_entry import TimelineEntry
        from src.core.orchestrator import chapter_orchestrator as orch_module

        # APPROVED 后 extract_memories 会调 LLM;本测试不关心 extract 结果,
        # monkeypatch 成 no-op 避免 side_effect 列表耗尽 StopIteration。
        monkeypatch.setattr(
            orch_module, "extract_memories", lambda **_: None
        )

        # 时间线倒流:entry1.time_order=200 > entry2.time_order=100 → 触发
        inverted_timeline = [
            TimelineEntry(
                entry_id="t1",
                workspace_id="ws-demo",
                chapter_id="ch-0001",
                description="第一事件",
                time_label="第一日",
                time_order=200,
            ),
            TimelineEntry(
                entry_id="t2",
                workspace_id="ws-demo",
                chapter_id="ch-0002",
                description="第二事件",
                time_label="第二日",
                time_order=100,  # 倒流
            ),
        ]

        def _fake_list_chars(_session, _workspace_id):
            character = MagicMock()
            character.character_id = "林衍"
            character.name = "林衍"
            character.current_state = {}
            character.voice_profile.forbidden_phrases = []
            character.identity_facets = []
            return [character]

        def _fake_list_timeline(_session, _workspace_id):
            return inverted_timeline

        monkeypatch.setattr(
            orch_module, "list_characters_by_workspace", _fake_list_chars
        )
        monkeypatch.setattr(
            orch_module,
            "list_timeline_entries_by_workspace",
            _fake_list_timeline,
        )

        # S3 降级后:倒流仅 warn-only 不触发 rewrite → 一次过,只需 1 writer + 1 reviewer。
        good_text = "林衍突破第一道剑阵,阵眼真凶身份依然成谜。" + "字" * 200
        mock_client.complete.side_effect = [
            _writer_response(content=good_text),
            _reviewer_response(score=8.0),
        ]

        factory = self._make_wiring_factory(tmp_path)
        fake_vector_store = MagicMock()

        with capture_logs() as logs:
            result = orchestrate_chapter(
                client=mock_client,
                chapter_card=_make_card(related_characters=[]),
                paths=workspace_paths,
                memory_section="",
                workspace_id="ws-demo",
                model="claude-sonnet-4-6",
                session_factory=factory,
                vector_store=fake_vector_store,
            )

        # S3:倒流降级 warn-only → 不再触发 rewrite → 章节正常 APPROVED
        assert result.final_state == ChapterState.APPROVED
        assert result.rewrite_count == 0
        # timeline_monotonic 失败转为 warn-only 日志(留痕),不再 emit validator_failed
        warn_only = [
            e for e in logs if e.get("event") == "timeline_monotonic_warn_only"
        ]
        assert len(warn_only) >= 1
        assert warn_only[0]["workspace_id"] == "ws-demo"
        timeline_failed = [
            e
            for e in logs
            if e.get("event") == "validator_failed"
            and e.get("validator_name") == "timeline_monotonic"
        ]
        assert timeline_failed == []  # 降级后不再作为 hard 失败

    def test_missing_card_character_escalates_without_writer_call(
        self,
        mock_client: MagicMock,
        workspace_paths: WorkspacePaths,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """空角色权威表 + 卡内 POV 引用 → 写前升人工，零正文版本、零调用。"""
        from src.core.orchestrator import chapter_orchestrator as orch_module

        monkeypatch.setattr(orch_module, "extract_memories", lambda **_: None)
        factory = self._make_wiring_factory(tmp_path)

        result = orchestrate_chapter(
            client=mock_client,
            chapter_card=_make_card(),
            paths=workspace_paths,
            memory_section="",
            workspace_id="ws-demo",
            model="claude-sonnet-4-6",
            session_factory=factory,
            vector_store=MagicMock(),
        )

        assert result.final_state == ChapterState.HUMAN_INTERVENTION
        assert result.escalation_reason == "chapter_card_admission_failed"
        assert result.versions == []
        assert result.rewrite_count == 0
        assert result.revise_count == 0
        mock_client.complete.assert_not_called()

    def test_character_admission_read_failure_stops_before_writer(
        self,
        mock_client: MagicMock,
        workspace_paths: WorkspacePaths,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """角色权威数据读失败必须 fail-closed，且 Writer 调用数保持 0。"""
        from src.core.orchestrator import chapter_orchestrator as orch_module

        # 同上,APPROVED 后 extract no-op。
        monkeypatch.setattr(
            orch_module, "extract_memories", lambda **_: None
        )

        def _raising_list(_session, _workspace_id):
            raise RuntimeError("simulated SQLite OperationalError")

        monkeypatch.setattr(
            orch_module, "list_characters_by_workspace", _raising_list
        )

        good_text = "林衍突破第一道剑阵,阵眼真凶身份依然成谜。" + "字" * 200
        mock_client.complete.side_effect = [
            _writer_response(content=good_text),
            _reviewer_response(score=8.0),
        ]

        factory = self._make_wiring_factory(tmp_path)
        fake_vector_store = MagicMock()

        with capture_logs() as logs, pytest.raises(
            PrewriteAdmissionDataError,
            match="Writer 前无法读取角色权威数据",
        ):
            orchestrate_chapter(
                client=mock_client,
                chapter_card=_make_card(),
                paths=workspace_paths,
                memory_section="",
                workspace_id="ws-demo",
                model="claude-sonnet-4-6",
                session_factory=factory,
                vector_store=fake_vector_store,
            )

        mock_client.complete.assert_not_called()
        admission_failed = [
            e
            for e in logs
            if e.get("event") == "prewrite_admission_character_data_failed"
        ]
        assert len(admission_failed) == 1
        assert admission_failed[0]["workspace_id"] == "ws-demo"


class TestValidatorEndToEndJsonl:
    """P4-acceptance-followup 启动指令 §6 ④ 字面要求:mock 路径
    ``validator_failed`` 事件经 ``WorkspaceAwareJSONLHandler`` 落工作区
    jsonl 文件。

    覆盖**端到端契约关键转换点**:orchestrate emit 的真实 event payload →
    JSONRenderer 序列化 → LogRecord → handler.emit → 工作区目录分流落盘。

    实现注:不依赖 pytest 测试运行期的全局 logger state(实测 P4-acceptance-followup
    跨全量回归,pytest caplog 在用例运行期会重置 root + 模块 logger handlers,
    把测试 setup 期添加的 handler 挤掉,导致 emit 不可达)。本测试用 capture_logs
    抓真实 orchestrate emit 的 event payload(已被 P4-B-06~B-10 实证可靠),
    然后用与生产 ``configure_logging`` 同款的 JSONRenderer 序列化为 message
    交给 handler.emit,验落盘。

    区别于其它两层覆盖:
    - ``TestRunValidatorPipeline`` + Item 1 wire 测试:event 流转(capture_logs);
    - ``test_logging_jsonl_handler.py``:handler 单测(make_record + emit)。
    """

    def test_forbidden_violation_writes_validator_failed_line_to_jsonl(
        self,
        mock_client: MagicMock,
        workspace_paths: WorkspacePaths,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # v1.0.x A2-02:must_achieve 移交 LLM validator,本端到端测试 reframe
        # 改用 forbidden 触发 pipeline-driven validator_failed event 落 jsonl。
        import logging as stdlogging

        from src.core.orchestrator import chapter_orchestrator as orch_module
        from src.api.logging import WorkspaceAwareJSONLHandler

        # APPROVED 后 extract_memories 会调 LLM;本测试不关心 extract 结果。
        monkeypatch.setattr(
            orch_module, "extract_memories", lambda **_: None
        )

        v1_text = "他握紧剑柄,掏出手机查看,迟迟未发动剑势。" + "字" * 150
        v2_text = "他举步走向阵心,推开石门。" + "字" * 200
        mock_client.complete.side_effect = _contract_sequence(
            _writer_response(content=v1_text),
            _reviewer_response(score=8.0),
            lambda messages: _contract_aware_writer_response(
                messages, global_content=v2_text
            ),
            _reviewer_response(score=8.5),
        )

        # 用 capture_logs 抓 orchestrate 期间 emit 的所有 event(P4-B-06 ~ B-10
        # + Item 1 wire 测试均用此模式,可靠)。
        with capture_logs() as captured:
            result = orchestrate_chapter(
                client=mock_client,
                chapter_card=_make_card(
                    workspace_id="ws-e2e-jsonl",
                    forbidden=["手机"],
                ),
                paths=workspace_paths,
                memory_section="",
                workspace_id="ws-e2e-jsonl",
                model="claude-sonnet-4-6",
            )
        assert result.final_state == ChapterState.APPROVED

        # 找真实 validator_failed event(P4-B-06 字面埋点字段)
        validator_failed_event = next(
            (
                e
                for e in captured
                if e.get("event") == "validator_failed"
                and e.get("validator_name") == "forbidden"
            ),
            None,
        )
        assert validator_failed_event is not None
        assert validator_failed_event["workspace_id"] == "ws-e2e-jsonl"
        assert validator_failed_event["chapter_id"] == "ch-0002"
        assert "details" in validator_failed_event

        # 真实 event payload → JSONRenderer 序列化(与生产 configure_logging 同款)
        # → LogRecord → handler.emit → 验工作区目录分流落盘 + 解析回 JSON 行
        # 字段(workspace_id / chapter_id / event / validator_name / details)。
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        handler = WorkspaceAwareJSONLHandler(data_dir=data_dir)
        json_renderer = structlog.processors.JSONRenderer()
        message = json_renderer(None, "info", dict(validator_failed_event))
        record = stdlogging.LogRecord(
            name="src.core.rules.validator_pipeline",
            level=stdlogging.INFO,
            pathname=__file__,
            lineno=1,
            msg=message,
            args=(),
            exc_info=None,
        )
        handler.emit(record)
        handler.flush()

        ws_logs = data_dir / "ws-e2e-jsonl" / "logs"
        jsonl_files = list(ws_logs.glob("*.jsonl"))
        assert jsonl_files, f"no jsonl in {ws_logs}"
        content = jsonl_files[0].read_text(encoding="utf-8")
        lines = [
            json.loads(line)
            for line in content.strip().split("\n")
            if line.strip()
        ]
        validator_failed_jsonl = [
            line
            for line in lines
            if line.get("event") == "validator_failed"
            and line.get("validator_name") == "forbidden"
        ]
        assert len(validator_failed_jsonl) == 1
        assert validator_failed_jsonl[0]["workspace_id"] == "ws-e2e-jsonl"
        assert validator_failed_jsonl[0]["chapter_id"] == "ch-0002"
        assert "details" in validator_failed_jsonl[0]


# ---------------------------------------------------------------------------
# 3. v1.1 wave 1 session 4(G5):style_drift_validator 集成
# ---------------------------------------------------------------------------


class TestStyleDriftValidatorG5Integration:
    """v1.1 wave 1 session 4(G5):rewrite 路径下 v1 vs vN 风格漂移检测。

    覆盖:
    - v1 自审:style_drift 不触发(自身就是 baseline)
    - rewrite vN >= 2:v1 vs vN 长篇 fingerprint 漂移触发 verdict=rewrite
    - 短文本(< MIN_CHARS_FOR_DRIFT_CHECK)兜底:既有所有 e2e 测不回归
    """

    def test_v1_self_review_does_not_trigger_style_drift(
        self, mock_client: MagicMock, workspace_paths: WorkspacePaths
    ) -> None:
        """v1 draft → reviewer pass → APPROVED。style_drift 因 v1 自审跳过。"""
        v1_text = "他举步走向阵心,阵眼真凶身份依然成谜。" + "字" * 200
        mock_client.complete.side_effect = [
            _writer_response(content=v1_text),
            _reviewer_response(score=8.0),
        ]

        with capture_logs() as logs:
            result = orchestrate_chapter(
                client=mock_client,
                chapter_card=_make_card(),
                paths=workspace_paths,
                memory_section="",
                workspace_id="ws-demo",
                model="claude-sonnet-4-6",
            )

        assert result.final_state == ChapterState.APPROVED
        assert result.rewrite_count == 0
        # 不应有 style_drift 失败事件(v1 自审跳过)
        style_drift_failed = [
            e
            for e in logs
            if e.get("event") == "validator_failed"
            and e.get("validator_name") == "style_drift"
        ]
        assert len(style_drift_failed) == 0

    def test_existing_short_text_e2e_not_regressed_by_style_drift(
        self, mock_client: MagicMock, workspace_paths: WorkspacePaths
    ) -> None:
        """rewrite vN >= 2 但 v1 / v2 都是短文本 → style_drift 短文本兜底 passed。

        既有 e2e 测都用 < 500 字 fixture,本测试守护它们不被 style_drift 反向回归。
        """
        v1_text = "他握紧剑柄,掏出手机查看,迟迟未发动剑势。" + "字" * 150
        v2_text = "他举步走向阵心,推开石门。" + "字" * 200
        mock_client.complete.side_effect = _contract_sequence(
            _writer_response(content=v1_text),
            _reviewer_response(score=8.0),
            lambda messages: _contract_aware_writer_response(
                messages, global_content=v2_text
            ),
            _reviewer_response(score=8.5),
        )

        result = orchestrate_chapter(
            client=mock_client,
            chapter_card=_make_card(forbidden=["手机"]),
            paths=workspace_paths,
            memory_section="",
            workspace_id="ws-demo",
            model="claude-sonnet-4-6",
        )

        # v1 forbidden 失败 → rewrite v2;v2 通过 → APPROVED
        # style_drift 不应额外触发(两版均 < MIN_CHARS_FOR_DRIFT_CHECK)
        assert result.final_state == ChapterState.APPROVED
        assert result.rewrite_count == 1
        # v2 review 通过(没有 style_drift 反向阻止)
        assert result.reviews[1].verdict == "pass"


# ---------------------------------------------------------------------------
# v2 W3.5-s7:trim×style_drift 句均豁免透传(中间环防回退)
# ---------------------------------------------------------------------------


def _drift_fp(sentence_length_mean: float) -> dict[str, float]:
    """合成长篇章节级 fingerprint,仅句均可变(过 MIN_CHARS / STD floor)。"""
    fp: dict[str, float] = {
        "paragraph_length_mean": 200.0,
        "paragraph_length_std": 80.0,
        "sentence_length_mean": sentence_length_mean,
        "sentence_length_std": 15.0,
        "simile_density": 0.1,
        "exclamation_density": 0.05,
        "total_chars": 3000.0,
        "total_sentences": 100.0,
    }
    for p in ("我", "你", "他", "她", "它", "我们", "你们", "他们", "她们"):
        fp[f"pronoun_freq_{p}"] = 0.02
    return fp


class TestStyleDriftTrimExemptionWiringW35S7:
    """``run_validator_pipeline`` 把 ``current_word_count_trimmed`` 透传给
    ``check_style_drift``(中间环防回退 — 这环断了则 trim 版本句均豁免在生产
    静默失效,ch-0010 死循环复发)。两版本句均 47.1 → 29.9(偏移 0.365 > 0.3)。"""

    _TEXT = "他举步走向阵心,阵眼真凶身份依然成谜。" + "字" * 200

    def test_trim_flag_propagates_sentence_mean_exempted(self) -> None:
        card = _make_card()
        report = run_validator_pipeline(
            self._TEXT,
            chapter_card=card,
            workspace_id="ws-demo",
            baseline_style_fingerprint=_drift_fp(47.1),
            current_style_fingerprint=_drift_fp(29.9),
            baseline_version_id=1,
            current_version_id=2,
            current_word_count_trimmed=True,
        )
        sd = next(r for r in report.results if r.name == "style_drift")
        assert sd.passed is True  # trim 版本句均豁免 → 不阻塞
        assert sd.details["sentence_mean_warn_only_due_to_trim"] is True

    def test_no_trim_flag_sentence_mean_still_hard(self) -> None:
        card = _make_card()
        report = run_validator_pipeline(
            self._TEXT,
            chapter_card=card,
            workspace_id="ws-demo",
            baseline_style_fingerprint=_drift_fp(47.1),
            current_style_fingerprint=_drift_fp(29.9),
            baseline_version_id=1,
            current_version_id=2,
            # current_word_count_trimmed 默认 False
        )
        sd = next(r for r in report.results if r.name == "style_drift")
        assert sd.passed is False  # 未 trim 句均仍硬约束(防真漂移漏放)
        assert sd.details["sentence_mean_warn_only_due_to_trim"] is False

    def test_style_failure_uses_frozen_plan_id_and_canonical_evidence(self) -> None:
        """G5 失败必须进入结构化 HARD 观察，不能只剩展示层字符串。"""

        card = _make_card()
        baseline = _drift_fp(47.1)
        current = _drift_fp(29.9)
        style_spec = resolve_rewrite_style_constraint_spec(
            v1_fingerprint=baseline,
            cross_chapter_baseline_fingerprint=None,
        )
        plan = compile_executable_constraint_plan(
            chapter_card=card,
            stage="rewrite",
            rewrite_style=style_spec,
        )

        report = run_validator_pipeline(
            self._TEXT,
            chapter_card=card,
            workspace_id="ws-demo",
            baseline_style_fingerprint=baseline,
            current_style_fingerprint=current,
            baseline_version_id=1,
            current_version_id=2,
            executable_constraint_plan=plan,
        )

        style_violations = [
            item
            for item in report.hard_violations
            if plan.constraint(item.constraint_id).kind == "style_drift"
        ]
        assert len(style_violations) == 1
        violation = style_violations[0]
        planned = plan.hard_violation_constraint(violation)
        assert planned.parameter("dimension") == "sentence_length_mean"
        assert violation.domain == "chapter.style"
        evidence = json.loads(violation.evidence_quote)
        assert evidence["dimension"] == "sentence_length_mean"
        assert evidence["current_value"] == 29.9
