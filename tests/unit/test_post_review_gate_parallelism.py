"""写后独立 LLM 闸并行化防回退测(v2 间隙 A1,2026-06-13)。

A1 把写后 6 个 LLM 闸(must_achieve / continuity / forbidden_crossing / setting /
dedup / ending;forbidden_crossing 为 W3.5 补强3 2026-06-26 加的 HARD 闸)
从串行链改为"判定并发 + 合并串行原序"。**零质量风险**的核心保证 = verdict
合并语义与串行逐字段相同。本文件锁死该契约:

1. **并发 == 串行(逐字段)**:同一组受控 judge 结果,
   ``post_review_gate_max_concurrency=1``(串行回退)与 ``=4``(并发)产出的
   review 在 verdict / hard_constraints_passed / failed_constraints /
   suggestions / constraint_results 五字段上完全相等。
2. **合并保序不受完成顺序影响**:judge 带反序 sleep(ending 先完成、setting
   后完成),并发模式 suggestions 顺序仍是原链序(continuity→setting→dedup→
   ending)—— 合并由 ``future.result()`` 调用顺序(固定原链序)决定,非完成序。
3. **任一 HARD 闸失败仍正确覆盖 verdict**:must_achieve / continuity 任一
   fail → verdict=rewrite + hard_constraints_passed=False,并发下成立。
4. **回退开关**:``=1`` 走串行路径,**不创建 ThreadPoolExecutor**(monkeypatch
   抛错验证池未被实例化)。
5. **judge 真 bug 异常冒泡**:judge 内未捕获异常经 ``future.result()`` 冒泡
   (与串行链"agent 异常向上传播"语义一致,非静默吞)。

谁可能破坏:任一改 ``_run_post_review_llm_gates`` 调度 / 改 6 个 ``_merge_*``
合并逻辑 / 改 ``_judge_*`` 拆分边界的 session。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

import src.core.orchestrator.chapter_orchestrator as orch
from src.core.agents.continuity_validator import ContinuityValidationResult
from src.core.agents.cross_chapter_dedup_validator import DedupResult
from src.core.agents.ending_tension_validator import EndingTensionResult
from src.core.agents.forbidden_crossing_validator import ForbiddenCrossingResult
from src.core.agents.must_achieve_validator import ValidationResult
from src.core.agents.setting_consistency_validator import (
    SettingConsistencyResult,
)
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.db.models import AttemptLedgerORM, Base
from src.core.llm.mock_client import MockLLMClient
from src.core.llm.cancel import LLMCancelledError
from src.core.models.routing import RoutingConfig, WriterProfilesConfig
from src.core.models.review import (
    ConstraintResult,
    ConstraintViolation,
    ReviewResult,
    stable_constraint_id,
)
from src.core.models.writer_profile import WriterProfile
from src.core.routing.router import ModelRouter, ProviderAttemptCapacity
from src.services.attempt_ledger import (
    bind_attempt_scope,
    wrap_router_with_attempt_tracking,
)
from src.services.cost_tracker import _CostTrackingRouter
from src.services.profile_override import _ProfileOverrideRouter


class _CapacityRouter:
    """不实际路由，只证明并发测试有足够的 Provider physical headroom。"""

    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.required_attempts: list[int] = []

    def can_afford_provider_attempts(self, required_attempts: int) -> bool:
        self.required_attempts.append(required_attempts)
        return self.allowed


@pytest.fixture
def ledger_session_factory(
    tmp_path: Path,
) -> Iterator[Callable[[], Session]]:
    """F3 使用真实临时 SQLite；不打开任何生产 workspace。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'post-review-cap.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        engine.dispose()


def _full_production_wrapper_stack(
    session_factory: Callable[[], Session],
) -> _CostTrackingRouter:
    """Cost → ProfileOverride → AttemptTracking → ModelRouter。"""

    profile = WriterProfile(
        profile_id="post-review-cap",
        provider="mock",
        model="mock-default",
        context_window=128_000,
    )
    config = WriterProfilesConfig(
        profiles={profile.profile_id: profile},
        routing=RoutingConfig(default=profile.profile_id),
    )
    raw = ModelRouter(config, client_factory=lambda **_kwargs: MockLLMClient())
    tracked = wrap_router_with_attempt_tracking(
        raw,
        workspace_id="ws-post-review-cap",
        session_factory=session_factory,
    )
    overridden = _ProfileOverrideRouter(  # type: ignore[arg-type]
        tracked,
        profile.profile_id,
    )
    return _CostTrackingRouter(  # type: ignore[arg-type]
        overridden,
        lambda _profile, _response, _cost: None,
    )


def _make_review() -> ReviewResult:
    """中性 reviewer 结论(verdict=pass / hard passed / 无 constraints / 无 suggestions)。"""
    score = 8.0
    return ReviewResult(
        review_id="rev-1",
        chapter_id="ch-0005",
        version_reviewed=1,
        reviewed_at=datetime.now(UTC),
        reviewer_model="mock",
        score_plot_progression=score,
        score_tension_match=score,
        score_prose_quality=score,
        score_consistency=score,
        score_hook_management=score,
        score_voice_consistency=score,
        score_overall=score,
        hard_constraints_passed=True,
        failed_constraints=[],
        issues=[],
        suggestions=[],
        verdict="pass",
        revise_targets=[],
    )


def _make_card() -> ChapterCard:
    return ChapterCard(
        chapter_id="ch-0005",
        workspace_id="ws-a1",
        volume_id="v01",
        chapter_number=5,
        title="第 5 章",
        tension_level=6,
        word_count_min=1,
        word_count_max=10**8,
        plot_node_budget=2,
        must_achieve=["事件A"],
        must_unresolved=[],
        forbidden=[],
        pov_character="林衍",
        setting="测试场景",
        continuity_context=ContinuityContext(
            prev_chapter_tail="上一章结尾",
            unfinished_actions=["未完动作"],
            time_gap="次日",
            scene_continuation="switch",
            pov_change=False,
            pov_from=None,
            pov_to="林衍",
        ),
        next_chapter_seed="待续",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=[],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=2,
        max_revises=2,
    )


def test_same_constraint_id_uses_fail_wins_without_duplicate_result() -> None:
    """字面 forbidden 失败不能被后到的语义 PASS 覆盖。"""
    literal_fail = _constraint_result(
        domain="forbidden",
        source_path="chapter_cards/ch-0005.forbidden",
        rule_text="不得提前揭示真相",
        status="fail",
        details="字面命中",
    )
    semantic_pass = literal_fail.model_copy(
        update={"status": "pass", "details": ""}
    )

    merged = orch._merge_constraint_results(_make_review(), [literal_fail])
    merged = orch._merge_constraint_results(merged, [semantic_pass])

    assert len(merged.constraint_results) == 1
    assert merged.constraint_results[0].status == "fail"
    assert merged.constraint_results[0].details == "字面命中"


def _constraint_result(
    *,
    domain: str,
    source_path: str,
    rule_text: str,
    status: str,
    details: str,
) -> ConstraintResult:
    """构造只由权威路径与规则语义定 ID 的受控 HARD 结果。"""
    return ConstraintResult(
        constraint_id=stable_constraint_id(
            source_path=source_path,
            domain=domain,
            rule_text=rule_text,
        ),
        domain=domain,  # type: ignore[arg-type]
        source_path=source_path,
        rule_text=rule_text,
        status=status,  # type: ignore[arg-type]
        details=details,
    )


def test_forbidden_judge_receives_card_must_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """所有写后入口共用的 judge 必须把 must 身份上下文传给 forbidden Agent。"""
    captured: dict[str, object] = {}

    def fake_validate(**kwargs: object) -> ForbiddenCrossingResult:
        captured.update(kwargs)
        return ForbiddenCrossingResult(passed=True)

    monkeypatch.setattr(orch, "validate_forbidden_crossing", fake_validate)
    card = _make_card().model_copy(
        update={
            "must_achieve": ["苏岚确认兄长的死亡方式"],
            "forbidden": ["不得揭示失踪队长的死亡真相"],
        }
    )

    result = orch._judge_forbidden_crossing(
        chapter_text="她认出兄长旧疤。",
        chapter_card=card,
        workspace_id="ws-a1",
        router=None,
        client=None,
        model=None,
    )

    assert result.passed is True
    assert captured["must_achieve"] == ["苏岚确认兄长的死亡方式"]
    assert captured["forbidden"] == ["不得揭示失踪队长的死亡真相"]


def _run_gates(
    monkeypatch: pytest.MonkeyPatch,
    concurrency: int,
    *,
    router: object | None = None,
) -> ReviewResult:
    """以指定并发档调 ``_run_post_review_llm_gates``(judge 已 monkeypatch)。"""
    monkeypatch.setattr(orch.settings, "post_review_gate_max_concurrency", concurrency)
    return orch._run_post_review_llm_gates(
        review=_make_review(),
        chapter_text="正文" * 500,
        chapter_card=_make_card(),
        workspace_id="ws-a1",
        router=router or _CapacityRouter(),  # type: ignore[arg-type]
        client=None,
        model=None,
        prev_chapter_card=None,
        world_contract=None,
        character_records=None,
        session_factory=None,
        versioning=None,  # type: ignore[arg-type]  # dedup/ending judge 被 monkeypatch,不触 versioning
    )


def _patch_judges_with_overrides(
    monkeypatch: pytest.MonkeyPatch, *, sleeps: dict[str, float] | None = None
) -> None:
    """monkeypatch 5 个 ``_judge_*`` 返回会触发覆盖/合并的受控结果。

    ``sleeps`` 给每个闸名注入 sleep 模拟判定耗时(测完成顺序无关)。
    """
    s = sleeps or {}

    def judge_ma(**kwargs: object) -> ValidationResult:
        time.sleep(s.get("ma", 0.0))
        return ValidationResult(
            passed=False,
            missing_items=["事件A"],
            llm_calls_count=1,
            constraint_results=[
                _constraint_result(
                    domain="must_achieve",
                    source_path="chapter_cards/ch-0005.must_achieve",
                    rule_text="事件A",
                    status="fail",
                    details="正文未完成事件A",
                )
            ],
        )

    def judge_cont(**kwargs: object) -> ContinuityValidationResult:
        time.sleep(s.get("cont", 0.0))
        return ContinuityValidationResult(
            passed=False,
            failed_hard_checks=["unfinished_actions"],
            soft_warnings=["时间警告X"],
            llm_calls_count=2,
            constraint_results=[
                _constraint_result(
                    domain="continuity",
                    source_path=(
                        "chapter_cards/ch-0005.continuity_context.unfinished_actions"
                    ),
                    rule_text="必须承接上一章未完成动作：未完动作",
                    status="fail",
                    details="正文未承接未完动作",
                )
            ],
        )

    def judge_setting(**kwargs: object) -> SettingConsistencyResult:
        time.sleep(s.get("setting", 0.0))
        return SettingConsistencyResult(
            passed=False,
            violations=[
                ConstraintViolation(
                    constraint_id="d" * 64,
                    domain="character.identity",
                    source_path="runtime/character_records",
                    source_hash="",
                    rule_quote="设定规则Y",
                    evidence_quote="设定证据Y",
                    severity="soft",
                    ambiguity="身份语义推断",
                )
            ],
            llm_calls_count=1,
        )

    def judge_dedup(**kwargs: object) -> DedupResult:
        time.sleep(s.get("dedup", 0.0))
        return DedupResult(
            passed=False,
            violations=["注水Z"],
            candidates_count=3,
            llm_calls_count=1,
        )

    def judge_ending(**kwargs: object) -> EndingTensionResult:
        time.sleep(s.get("ending", 0.0))
        return EndingTensionResult(
            passed=False, verdict="abrupt", evidence="断裂W", llm_calls_count=1
        )

    def judge_fc(**kwargs: object) -> ForbiddenCrossingResult:
        time.sleep(s.get("fc", 0.0))
        return ForbiddenCrossingResult(
            passed=False,
            violations=["越界示例V"],
            llm_calls_count=1,
            constraint_results=[
                _constraint_result(
                    domain="forbidden",
                    source_path="chapter_cards/ch-0005.forbidden",
                    rule_text="不得提前揭示真相",
                    status="fail",
                    details="正文提前揭示真相",
                )
            ],
        )

    monkeypatch.setattr(orch, "_judge_must_achieve", judge_ma)
    monkeypatch.setattr(orch, "_judge_continuity", judge_cont)
    monkeypatch.setattr(orch, "_judge_forbidden_crossing", judge_fc)
    monkeypatch.setattr(orch, "_judge_setting_consistency", judge_setting)
    monkeypatch.setattr(orch, "_judge_cross_chapter_dedup", judge_dedup)
    monkeypatch.setattr(orch, "_judge_ending_tension", judge_ending)


# 预期合并结果(串行 = 并发,逐字段)。
_EXPECTED_FAILED = [
    "must_achieve: 事件A",
    "continuity: unfinished_actions",
    "forbidden",  # 字面/语义共用同一规则身份；具体证据在 constraint_results
]
_EXPECTED_SUGGESTIONS = [
    "时间警告X",  # continuity soft(链上 must_achieve 之后、setting 之前)
    "设定一致性[SOFT:dddddddddddd]:规则「设定规则Y」；正文「设定证据Y」",
    "注水Z",  # dedup
    "终局张力:结局与前文张力断裂(强行岁月静好)——断裂W",  # ending
]


class TestR2SettingMerge:
    """R2:结构化 HARD 阻止 pass；SOFT 只进 suggestions。"""

    def test_hard_setting_violation_overrides_pass(self) -> None:
        violation = ConstraintViolation(
            constraint_id="e" * 64,
            domain="world.unique_rules",
            source_path="data/outline/world.json",
            source_hash="f" * 64,
            rule_quote="记忆抹除不可逆。",
            evidence_quote="被抹除的记忆全部恢复。",
            severity="hard",
            ambiguity=None,
        )
        merged = orch._merge_setting_consistency(
            review=_make_review(),
            result=SettingConsistencyResult(
                passed=False,
                violations=[violation],
                llm_calls_count=1,
                validation_source="authoritative_world_contract",
            ),
            chapter_card=_make_card(),
            workspace_id="ws-a1",
        )
        assert merged.verdict == "rewrite"
        assert merged.hard_constraints_passed is False
        assert merged.failed_constraints == [
            (
                f"setting:{violation.constraint_id}:"
                "规则「记忆抹除不可逆。」；正文「被抹除的记忆全部恢复。」"
            )
        ]
        assert merged.suggestions == []
        assert merged.constraint_violations == [violation]


class TestParallelEqualsSerial:
    """契约 1+3:并发与串行逐字段相等,HARD 覆盖正确。"""

    def test_all_pass_both_modes_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """全闸 pass → 两模式 review 都不变且相等。"""
        passing = {
            "_judge_must_achieve": ValidationResult(passed=True),
            "_judge_continuity": ContinuityValidationResult(passed=True, skipped=True),
            "_judge_forbidden_crossing": ForbiddenCrossingResult(passed=True),
            "_judge_setting_consistency": SettingConsistencyResult(passed=True),
            "_judge_cross_chapter_dedup": None,
            "_judge_ending_tension": None,
        }
        for name, val in passing.items():
            monkeypatch.setattr(orch, name, lambda val=val, **k: val)

        serial = _run_gates(monkeypatch, 1)
        parallel = _run_gates(monkeypatch, 4)
        assert serial.verdict == "pass"
        assert parallel.verdict == "pass"
        assert serial.suggestions == [] == parallel.suggestions
        assert serial.failed_constraints == [] == parallel.failed_constraints
        assert serial.constraint_results == parallel.constraint_results

    def test_overrides_field_by_field_equal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """6 闸全触发覆盖/合并 → 并发与串行四字段逐字相等(forbidden_crossing 第 6 闸 HARD)。"""
        _patch_judges_with_overrides(monkeypatch)
        serial = _run_gates(monkeypatch, 1)
        _patch_judges_with_overrides(monkeypatch)
        parallel = _run_gates(monkeypatch, 4)

        for r in (serial, parallel):
            assert r.verdict == "rewrite"
            assert r.hard_constraints_passed is False
            assert r.failed_constraints == _EXPECTED_FAILED
            assert r.suggestions == _EXPECTED_SUGGESTIONS
            assert [item.domain for item in r.constraint_results] == [
                "must_achieve",
                "continuity",
                "forbidden",
            ]
            assert all(item.status == "fail" for item in r.constraint_results)

        assert serial.verdict == parallel.verdict
        assert serial.hard_constraints_passed == parallel.hard_constraints_passed
        assert serial.failed_constraints == parallel.failed_constraints
        assert serial.suggestions == parallel.suggestions
        assert serial.constraint_results == parallel.constraint_results


class TestMergeOrderIndependentOfCompletion:
    """契约 2:合并顺序 = 原链序,不受 judge 完成顺序影响。"""

    def test_reverse_completion_still_chain_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ending 最快完成、setting 最慢 → suggestions 仍按原链序排列。"""
        _patch_judges_with_overrides(
            monkeypatch,
            sleeps={"setting": 0.15, "dedup": 0.05, "ending": 0.0},
        )
        parallel = _run_gates(monkeypatch, 4)
        # 即使 ending 先算完,suggestions 顺序仍是 continuity→setting→dedup→ending
        assert parallel.suggestions == _EXPECTED_SUGGESTIONS


class TestConcurrencyOneTakesSerialPath:
    """契约 4:max_concurrency=1 走串行,不实例化 ThreadPoolExecutor。"""

    def test_no_thread_pool_created_when_serial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_judges_with_overrides(monkeypatch)

        def boom(*a: object, **k: object) -> object:
            raise AssertionError("串行回退路径不应创建 ThreadPoolExecutor")

        monkeypatch.setattr(orch, "ThreadPoolExecutor", boom)
        result = _run_gates(monkeypatch, 1)
        # 串行路径仍正确合并(验证回退路径行为完整)
        assert result.verdict == "rewrite"
        assert result.failed_constraints == _EXPECTED_FAILED
        assert result.suggestions == _EXPECTED_SUGGESTIONS


class TestJudgeExceptionPropagates:
    """契约 5:judge 真 bug 异常经 future.result() 冒泡(不静默吞)。"""

    def test_parallel_judge_exception_bubbles(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_judges_with_overrides(monkeypatch)

        def judge_boom(**kwargs: object) -> ValidationResult:
            raise RuntimeError("judge 真 bug")

        monkeypatch.setattr(orch, "_judge_must_achieve", judge_boom)
        with pytest.raises(RuntimeError, match="judge 真 bug"):
            _run_gates(monkeypatch, 4)


class TestProviderCapHeadroom:
    """F3：完整生产 wrapper 栈必须保留 checker，低余量在建池前串行。"""

    def test_low_cap_full_wrapper_stack_falls_back_before_executor(
        self,
        monkeypatch: pytest.MonkeyPatch,
        ledger_session_factory: Callable[[], Session],
    ) -> None:
        router = _full_production_wrapper_stack(ledger_session_factory)
        assert isinstance(router, ProviderAttemptCapacity)
        _patch_judges_with_overrides(monkeypatch)

        def boom(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("低 cap 回退必须发生在创建 executor 之前")

        monkeypatch.setattr(orch, "ThreadPoolExecutor", boom)
        with bind_attempt_scope(
            workspace_id="ws-post-review-cap",
            task_id="task-low-cap",
            chapter_id="ch-0005",
            provider_call_cap=(
                orch._POST_REVIEW_GATE_REQUIRED_PROVIDER_ATTEMPTS - 1
            ),
        ):
            result = _run_gates(monkeypatch, 4, router=router)

        assert result.failed_constraints == _EXPECTED_FAILED
        assert orch._POST_REVIEW_GATE_REQUIRED_PROVIDER_ATTEMPTS == 36
        with ledger_session_factory() as session:
            assert session.scalar(select(func.count()).select_from(AttemptLedgerORM)) == 0

    def test_exact_headroom_keeps_parallel_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        ledger_session_factory: Callable[[], Session],
    ) -> None:
        router = _full_production_wrapper_stack(ledger_session_factory)
        _patch_judges_with_overrides(monkeypatch)
        with bind_attempt_scope(
            workspace_id="ws-post-review-cap",
            task_id="task-exact-cap",
            chapter_id="ch-0005",
            provider_call_cap=orch._POST_REVIEW_GATE_REQUIRED_PROVIDER_ATTEMPTS,
        ):
            result = _run_gates(monkeypatch, 4, router=router)

        assert result.failed_constraints == _EXPECTED_FAILED

    def test_missing_capability_is_unproven_and_falls_back(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_judges_with_overrides(monkeypatch)

        def boom(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("不可证明余量时不得创建 executor")

        monkeypatch.setattr(orch, "ThreadPoolExecutor", boom)
        result = _run_gates(monkeypatch, 4, router=object())
        assert result.failed_constraints == _EXPECTED_FAILED


class TestParallelFaultDrain:
    """F3：partial submit、迟到结果、多失败与停止优先级。"""

    def test_partial_submit_drains_started_worker(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        completed = Event()
        _patch_judges_with_overrides(monkeypatch)

        def late_ma(**_kwargs: object) -> ValidationResult:
            time.sleep(0.03)
            completed.set()
            return ValidationResult(passed=True)

        monkeypatch.setattr(orch, "_judge_must_achieve", late_ma)

        class PartialSubmitExecutor:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self._inner = RealThreadPoolExecutor(*args, **kwargs)
                self._submits = 0

            def __enter__(self) -> PartialSubmitExecutor:
                return self

            def __exit__(self, *args: object) -> None:
                self._inner.shutdown(wait=True)

            def submit(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
                self._submits += 1
                if self._submits == 3:
                    raise RuntimeError("partial submit failed")
                return self._inner.submit(*args, **kwargs)

        factory_calls = 0

        def executor_factory(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            nonlocal factory_calls
            factory_calls += 1
            if factory_calls == 1:
                return PartialSubmitExecutor(*args, **kwargs)
            return RealThreadPoolExecutor(*args, **kwargs)

        monkeypatch.setattr(orch, "ThreadPoolExecutor", executor_factory)
        with pytest.raises(RuntimeError, match="partial submit failed"):
            _run_gates(monkeypatch, 4)
        assert completed.is_set()

    def test_first_chain_failure_wins_after_late_success_is_drained(self) -> None:
        late_success = Event()

        def fail_first() -> None:
            raise RuntimeError("first-chain-error")

        def succeed_late() -> str:
            time.sleep(0.03)
            late_success.set()
            return "late-ok"

        with RealThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(fail_first), executor.submit(succeed_late)]
            with pytest.raises(RuntimeError, match="first-chain-error"):
                orch._drain_post_review_gate_futures(futures)
        assert late_success.is_set()

    def test_multiple_business_failures_use_fixed_chain_order(self) -> None:
        def fail(message: str, delay: float) -> None:
            time.sleep(delay)
            raise RuntimeError(message)

        with RealThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(fail, "chain-first", 0.03),
                executor.submit(fail, "completed-first", 0.0),
            ]
            with pytest.raises(RuntimeError, match="chain-first"):
                orch._drain_post_review_gate_futures(futures)

    @pytest.mark.parametrize(
        ("stop_error", "expected_type"),
        [
            (LLMCancelledError("cancelled"), LLMCancelledError),
            (KeyboardInterrupt("keyboard"), KeyboardInterrupt),
        ],
    )
    def test_stop_dominates_ordinary_business_failure(
        self,
        stop_error: BaseException,
        expected_type: type[BaseException],
    ) -> None:
        def fail_business() -> None:
            raise RuntimeError("business failed first")

        def stop() -> None:
            raise stop_error

        with RealThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(fail_business), executor.submit(stop)]
            with pytest.raises(expected_type):
                orch._drain_post_review_gate_futures(futures)
