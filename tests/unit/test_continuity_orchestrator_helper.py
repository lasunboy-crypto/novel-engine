"""orchestrator ``_apply_continuity_validator`` helper 集成测(v1.0.x wave 3
Commit D)。

完全 mock ``validate_continuity`` 返回值,直接测 helper 把
``ContinuityValidationResult`` 翻成 ``ReviewResult`` 更新的语义(对齐
must_achieve_validator helper 同型测试范式 — 单测 + integration 解耦):

1. **HARD fail 覆盖 verdict=rewrite**:Q1 fail → verdict 翻 rewrite +
   hard_constraints_passed=False + failed_constraints 含 ``"continuity: <name>"`` prefix
2. **HARD 双 fail**:Q1+Q2 双挂 → 2 个 prefixed marker 进 failed_constraints
3. **SOFT warning 合并 suggestions**:Q3 warning → review.suggestions 加 short string,
   verdict 不动
4. **HARD + SOFT 混合**:verdict=rewrite + suggestions 同时加
5. **skipped 不动 review**:首章 / 空 ctx skip → 原 review 返回
6. **fail_open 不动 review**:LLM 异常 → 原 review 返回
7. **passed 不动 review**:3 Q 全 yes → 原 review 返回
8. **failed_constraints 去重合并**:既有约束 + 本 validator 约束并集去重

"调度顺序 must_achieve → continuity"由 ``chapter_orchestrator.py`` L574-582 主循环
+ 唯一稿静态代码顺序保证(已 grep 实证 + ruff 通过),不另开 fixture
跑 ``orchestrate_chapter`` 完整路径。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from src.core.agents.continuity_validator import ContinuityValidationResult
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.review import ReviewResult
from src.core.orchestrator import chapter_orchestrator as orch_module


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def _make_continuity(**overrides: Any) -> ContinuityContext:
    payload: dict[str, Any] = dict(
        prev_chapter_tail="他握紧剑柄。" + "字" * 100,
        unfinished_actions=["剑势悬而未发"],
        time_gap="三日后",
        scene_continuation="same",
        pov_change=False,
        pov_from="林衍",
        pov_to="林衍",
    )
    payload.update(overrides)
    return ContinuityContext(**payload)


def _make_card(**overrides: Any) -> ChapterCard:
    payload: dict[str, Any] = dict(
        chapter_id="ch-0002",
        workspace_id="ws-demo",
        volume_id="v01",
        chapter_number=2,
        title="第二章·破阵",
        tension_level=8,
        word_count_min=1,
        word_count_max=10**8,
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


def _make_review(
    *,
    verdict: str = "pass",
    failed_constraints: list[str] | None = None,
    suggestions: list[str] | None = None,
    hard_constraints_passed: bool = True,
) -> ReviewResult:
    return ReviewResult(
        review_id="rev-001",
        chapter_id="ch-0002",
        version_reviewed=1,
        reviewed_at=datetime.now(UTC),
        reviewer_model="qwen-novel:latest",
        score_plot_progression=8.0,
        score_tension_match=8.0,
        score_prose_quality=8.0,
        score_consistency=8.0,
        score_hook_management=8.0,
        score_voice_consistency=8.0,
        score_overall=8.0,
        hard_constraints_passed=hard_constraints_passed,
        failed_constraints=failed_constraints or [],
        issues=[],
        suggestions=suggestions or [],
        verdict=verdict,  # type: ignore[arg-type]
        revise_targets=[],
    )


# ---------------------------------------------------------------------------
# 1. HARD fail 覆盖 verdict=rewrite
# ---------------------------------------------------------------------------


class TestHardFailOverridesVerdict:
    def test_q1_fail_overrides_to_rewrite(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Q1 unfinished_actions HARD fail → verdict=rewrite + failed_constraints
        # 含 "continuity: unfinished_actions" prefix
        monkeypatch.setattr(
            orch_module,
            "validate_continuity",
            lambda **kwargs: ContinuityValidationResult(
                passed=False,
                failed_hard_checks=["unfinished_actions"],
                soft_warnings=[],
                llm_calls_count=1,
            ),
        )
        review = _make_review(verdict="pass")
        result = orch_module._apply_continuity_validator(
            review=review,
            chapter_text="正文",
            chapter_card=_make_card(),
            workspace_id="ws-demo",
            router=None,
            client=None,
            model=None,
        )

        assert result.verdict == "rewrite"
        assert result.hard_constraints_passed is False
        assert result.failed_constraints == ["continuity: unfinished_actions"]
        assert result.suggestions == []  # 不动 suggestions

    def test_q1_q2_both_fail_appends_two_markers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            orch_module,
            "validate_continuity",
            lambda **kwargs: ContinuityValidationResult(
                passed=False,
                failed_hard_checks=["unfinished_actions", "scene_same"],
                soft_warnings=[],
                llm_calls_count=2,
            ),
        )
        review = _make_review(verdict="pass")
        result = orch_module._apply_continuity_validator(
            review=review,
            chapter_text="正文",
            chapter_card=_make_card(),
            workspace_id="ws-demo",
            router=None,
            client=None,
            model=None,
        )

        assert result.verdict == "rewrite"
        assert result.failed_constraints == [
            "continuity: unfinished_actions",
            "continuity: scene_same",
        ]

    def test_failed_constraints_merge_dedup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 既有 review.failed_constraints 与 validator 失败约束并集去重
        monkeypatch.setattr(
            orch_module,
            "validate_continuity",
            lambda **kwargs: ContinuityValidationResult(
                passed=False,
                failed_hard_checks=["unfinished_actions"],
                soft_warnings=[],
                llm_calls_count=1,
            ),
        )
        review = _make_review(
            verdict="rewrite",
            failed_constraints=["word_count", "forbidden"],
            hard_constraints_passed=False,
        )
        result = orch_module._apply_continuity_validator(
            review=review,
            chapter_text="正文",
            chapter_card=_make_card(),
            workspace_id="ws-demo",
            router=None,
            client=None,
            model=None,
        )

        # 既有 word_count / forbidden 保留,本 validator 失败 marker 追加
        assert result.failed_constraints == [
            "word_count",
            "forbidden",
            "continuity: unfinished_actions",
        ]


# ---------------------------------------------------------------------------
# 2. SOFT warning 合并 suggestions(不动 verdict)
# ---------------------------------------------------------------------------


class TestSoftWarningMergeSuggestions:
    def test_soft_warning_appends_to_suggestions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warning = "时间表述与 time_gap 不一致(time_gap=三日后):正文写'次日清晨'"
        monkeypatch.setattr(
            orch_module,
            "validate_continuity",
            lambda **kwargs: ContinuityValidationResult(
                passed=True,  # SOFT 不影响 passed
                failed_hard_checks=[],
                soft_warnings=[warning],
                llm_calls_count=1,
            ),
        )
        review = _make_review(verdict="pass", suggestions=["既有建议"])
        result = orch_module._apply_continuity_validator(
            review=review,
            chapter_text="正文",
            chapter_card=_make_card(),
            workspace_id="ws-demo",
            router=None,
            client=None,
            model=None,
        )

        # verdict / hard_constraints_passed / failed_constraints 不动
        assert result.verdict == "pass"
        assert result.hard_constraints_passed is True
        assert result.failed_constraints == []
        # suggestions 追加
        assert result.suggestions == ["既有建议", warning]


# ---------------------------------------------------------------------------
# 3. HARD + SOFT 混合
# ---------------------------------------------------------------------------


class TestHardAndSoftMixed:
    def test_hard_and_soft_both_applied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warning = "时间表述与 time_gap 不一致(time_gap=三日后):xxx"
        monkeypatch.setattr(
            orch_module,
            "validate_continuity",
            lambda **kwargs: ContinuityValidationResult(
                passed=False,
                failed_hard_checks=["unfinished_actions"],
                soft_warnings=[warning],
                llm_calls_count=2,
            ),
        )
        review = _make_review(verdict="revise", suggestions=["既有"])
        result = orch_module._apply_continuity_validator(
            review=review,
            chapter_text="正文",
            chapter_card=_make_card(),
            workspace_id="ws-demo",
            router=None,
            client=None,
            model=None,
        )

        # HARD 覆盖 verdict + failed_constraints
        assert result.verdict == "rewrite"
        assert result.hard_constraints_passed is False
        assert result.failed_constraints == ["continuity: unfinished_actions"]
        # SOFT 合并 suggestions
        assert result.suggestions == ["既有", warning]


# ---------------------------------------------------------------------------
# 4. skipped / fail_open / passed 都不动 review
# ---------------------------------------------------------------------------


class TestNoChangeBranches:
    def test_skipped_returns_review_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 首章 / 空 ctx → skip → 不动 review
        monkeypatch.setattr(
            orch_module,
            "validate_continuity",
            lambda **kwargs: ContinuityValidationResult(
                passed=True, skipped=True, llm_calls_count=0
            ),
        )
        review = _make_review(verdict="pass", suggestions=["既有"])
        result = orch_module._apply_continuity_validator(
            review=review,
            chapter_text="正文",
            chapter_card=_make_card(),
            workspace_id="ws-demo",
            router=None,
            client=None,
            model=None,
        )

        # 同一对象引用语义(model_copy 未触发)
        assert result is review

    def test_fail_open_returns_review_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # LLM 异常 → validator 内 fail-open → caller 不动 review
        monkeypatch.setattr(
            orch_module,
            "validate_continuity",
            lambda **kwargs: ContinuityValidationResult(
                passed=True, fail_open=True, llm_calls_count=1
            ),
        )
        review = _make_review(verdict="revise")
        result = orch_module._apply_continuity_validator(
            review=review,
            chapter_text="正文",
            chapter_card=_make_card(),
            workspace_id="ws-demo",
            router=None,
            client=None,
            model=None,
        )

        assert result is review

    def test_passed_no_warnings_returns_review_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 3 Q 全 yes → passed=True / 无 fail / 无 warn → 不动 review
        monkeypatch.setattr(
            orch_module,
            "validate_continuity",
            lambda **kwargs: ContinuityValidationResult(
                passed=True,
                failed_hard_checks=[],
                soft_warnings=[],
                llm_calls_count=3,
            ),
        )
        review = _make_review(verdict="pass")
        result = orch_module._apply_continuity_validator(
            review=review,
            chapter_text="正文",
            chapter_card=_make_card(),
            workspace_id="ws-demo",
            router=None,
            client=None,
            model=None,
        )

        assert result is review


# ---------------------------------------------------------------------------
# 9. v1.0.x wave 4 session 5:_load_prev_chapter_card_safe helper + Q4 caller
# ---------------------------------------------------------------------------


class TestLoadPrevChapterCardSafeWave4Session5:
    """``_load_prev_chapter_card_safe`` helper(本会话新增)4 路径覆盖。"""

    def test_first_chapter_returns_none(self, tmp_path) -> None:
        # 全书首章 ch-0001 → 直接返 None(无上一章,不走 IO)
        from src.core.workspace.workspace_paths import WorkspacePaths

        paths = WorkspacePaths(workspace_id="ws-demo", workspaces_root=tmp_path)
        result = orch_module._load_prev_chapter_card_safe(
            paths=paths, chapter_id="ch-0001"
        )
        assert result is None

    def test_loads_card_when_file_exists(self, tmp_path) -> None:
        # 上一章卡 SSOT 文件存在 + JSON 合法 → 返 ChapterCard 实例
        from src.core.workspace.workspace_paths import WorkspacePaths

        paths = WorkspacePaths(workspace_id="ws-demo", workspaces_root=tmp_path)
        paths.cards_dir.mkdir(parents=True, exist_ok=True)
        prev_card = _make_card(chapter_id="ch-0002", chapter_number=2)
        (paths.cards_dir / "ch-0002.json").write_text(
            prev_card.model_dump_json(), encoding="utf-8"
        )
        result = orch_module._load_prev_chapter_card_safe(
            paths=paths, chapter_id="ch-0003"
        )
        assert result is not None
        assert result.chapter_id == "ch-0002"
        assert result.chapter_number == 2

    def test_multi_volume_prev_card_uses_global_id(self, tmp_path) -> None:
        # v2 W3.5-S1 回归:多卷书前一章按**全局** chapter_id 派生,不按卷内章号。
        # ch-0010 = 卷2 第 3 章(chapter_number=3)→ 上一章必须是 ch-0009(全局前一章),
        # 绝非 ch-0002(老 bug 用 chapter_number-1=2 算出的卷1 早期章)。
        from src.core.workspace.workspace_paths import WorkspacePaths

        paths = WorkspacePaths(workspace_id="ws-demo", workspaces_root=tmp_path)
        paths.cards_dir.mkdir(parents=True, exist_ok=True)
        # 同时落 ch-0002(bug 会错误命中)和 ch-0009(正确目标)两张卡
        (paths.cards_dir / "ch-0002.json").write_text(
            _make_card(chapter_id="ch-0002", chapter_number=2).model_dump_json(),
            encoding="utf-8",
        )
        (paths.cards_dir / "ch-0009.json").write_text(
            _make_card(chapter_id="ch-0009", chapter_number=2).model_dump_json(),
            encoding="utf-8",
        )
        # 用卷2 第 3 章的卡(chapter_number=3 = 卷内号)调用
        result = orch_module._load_prev_chapter_card_safe(
            paths=paths, chapter_id="ch-0010"
        )
        assert result is not None
        assert result.chapter_id == "ch-0009"  # 全局前一章,非 ch-0002

    def test_file_missing_returns_none(self, tmp_path) -> None:
        # 上一章卡文件不存在 → swallow + 返 None(fail-open)
        from src.core.workspace.workspace_paths import WorkspacePaths

        paths = WorkspacePaths(workspace_id="ws-demo", workspaces_root=tmp_path)
        result = orch_module._load_prev_chapter_card_safe(
            paths=paths, chapter_id="ch-0003"
        )
        assert result is None

    def test_corrupt_json_returns_none(self, tmp_path) -> None:
        # 上一章卡文件存在但 JSON 损坏 → swallow + 返 None(fail-open)
        from src.core.workspace.workspace_paths import WorkspacePaths

        paths = WorkspacePaths(workspace_id="ws-demo", workspaces_root=tmp_path)
        paths.cards_dir.mkdir(parents=True, exist_ok=True)
        (paths.cards_dir / "ch-0001.json").write_text(
            "{这不是合法 JSON", encoding="utf-8"
        )
        result = orch_module._load_prev_chapter_card_safe(
            paths=paths, chapter_id="ch-0002"
        )
        assert result is None


class TestApplyContinuityValidatorForwardsPrevCardWave4Session5:
    """``_apply_continuity_validator`` 透传 ``prev_chapter_card`` 入参的 caller
    contract(本会话新增)。"""

    def test_prev_card_forwarded_to_validate_continuity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 用 spy 验证 prev_chapter_card 字面透传给 validate_continuity 入参
        captured: dict[str, Any] = {}

        def _spy_validate(**kwargs: Any) -> ContinuityValidationResult:
            captured.update(kwargs)
            return ContinuityValidationResult(
                passed=True,
                failed_hard_checks=[],
                soft_warnings=[],
                llm_calls_count=0,
                skipped=True,
            )

        monkeypatch.setattr(
            orch_module, "validate_continuity", _spy_validate
        )
        prev = _make_card(chapter_id="ch-0001", chapter_number=1)
        review = _make_review(verdict="pass")
        orch_module._apply_continuity_validator(
            review=review,
            chapter_text="正文",
            chapter_card=_make_card(),
            workspace_id="ws-demo",
            router=None,
            client=None,
            model=None,
            prev_chapter_card=prev,
        )
        assert captured.get("prev_chapter_card") is prev

    def test_prev_card_defaults_to_none_when_omitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 不显式传 prev_chapter_card → 透 None(既有 caller 零回归)
        captured: dict[str, Any] = {}

        def _spy_validate(**kwargs: Any) -> ContinuityValidationResult:
            captured.update(kwargs)
            return ContinuityValidationResult(
                passed=True,
                failed_hard_checks=[],
                soft_warnings=[],
                llm_calls_count=0,
                skipped=True,
            )

        monkeypatch.setattr(
            orch_module, "validate_continuity", _spy_validate
        )
        review = _make_review(verdict="pass")
        orch_module._apply_continuity_validator(
            review=review,
            chapter_text="正文",
            chapter_card=_make_card(),
            workspace_id="ws-demo",
            router=None,
            client=None,
            model=None,
        )
        assert "prev_chapter_card" in captured
        assert captured["prev_chapter_card"] is None
