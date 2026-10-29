"""Reviser agent 简版单测(P1-B-12)。

完全 mock ``LLMClient``,不真调 API;用临时目录注入 fake ``reviser.md``,
避免依赖仓库根真实提示词文件做断言。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.agents import _prompt as prompt_module
from src.core.agents import reviser as reviser_module
from src.core.agents.reviser import (
    ReviserOutput,
    _build_user_message,
    _format_reviser_hard_gate_contract,
    revise_chapter,
)
from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.review import (
    REVIEWER_ISSUES_MAX_ITEMS,
    REVIEW_ISSUE_DESCRIPTION_MAX_CHARS,
    REVIEW_ISSUE_PARAGRAPH_ANCHOR_MAX_CHARS,
    REVIEW_ISSUE_SUGGESTION_MAX_CHARS,
    REVIEW_ISSUE_TYPE_MAX_CHARS,
    ConstraintResult,
    ReviewIssue,
    stable_constraint_id,
)
from src.core.rules.prose_hard_contract import compile_executable_constraint_plan
from src.core.rules.prose_revision_contract import build_reviser_revision_contract
from tests.revision_contract_helpers import single_target_localized_response


def _make_issue(**overrides) -> ReviewIssue:
    payload = dict(
        paragraph_anchor="他握紧剑柄,目光不离对面那道身影",
        issue_type="pacing",
        description="此段铺陈过长,延滞了进入打斗的节奏",
        suggestion="压缩为 2-3 句,直接切入剑势",
    )
    payload.update(overrides)
    return ReviewIssue(**payload)


def _make_response(
    *,
    content: str = "修订后正文(略)",
    model_used: str = "claude-sonnet-4-6-20250101",
    input_tokens: int = 1234,
    output_tokens: int = 5678,
    finish_reason: str | None = "end_turn",
) -> LLMResponse:
    return LLMResponse(
        content=content,
        model_used=model_used,
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        finish_reason=finish_reason,
    )


_SAMPLE_TEXT = (
    "第一段:林衍走到山门前,长剑出鞘三寸。\n\n"
    "第二段:他握紧剑柄,目光不离对面那道身影,心中翻涌着旧日恩怨,"
    "脑海里千头万绪一时无从抓起,只能屏息以待对方先发难。\n\n"
    "第三段:风过青崖,残阵嗡鸣。"
)


def _make_contract_card() -> ChapterCard:
    return ChapterCard(
        chapter_id="ch-0002",
        workspace_id="ws-demo",
        volume_id="v01",
        chapter_number=2,
        title="密室门外",
        tension_level=7,
        word_count_min=2500,
        word_count_max=4000,
        plot_node_budget=3,
        must_achieve=["守住密室入口"],
        must_unresolved=["钥匙来源"],
        forbidden=["本章进入密室"],
        pov_character="守门人",
        setting="密室门外走廊",
        continuity_context=ContinuityContext(
            prev_chapter_tail="钥匙刚插入锁孔。",
            unfinished_actions=["继续转动钥匙"],
            time_gap="立即续接",
            scene_continuation="same",
            pov_change=False,
            pov_from="守门人",
            pov_to="守门人",
        ),
        next_chapter_seed="门后传来脚步声",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=["守门人"],
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    """在临时目录里建一个 reviser.md,内容可断言。"""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "reviser.md").write_text(
        "FAKE_REVISER_SYSTEM_PROMPT\n\n用于单测断言的 system 标记。",
        encoding="utf-8",
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _clear_prompt_cache():
    """每个用例清一次 lru_cache,避免不同 fake 模板互相污染。

    Rule of Three 抽取后,缓存对象在 ``src.core.agents._prompt`` 里,
    Writer/Reviewer/Reviser 共享。各自单测都按本 fixture 清一次,顺序
    无关。
    """
    prompt_module.load_prompt_template.cache_clear()
    yield
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=LLMClient)
    client.complete.return_value = _make_response()
    return client


class TestPromptAssembly:
    """user 消息含原文 + 审稿意见列表;system 来自模板文件。"""

    def test_user_message_contains_chapter_text(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        revise_chapter(
            client=mock_client,
            chapter_text=_SAMPLE_TEXT,
            issues=[_make_issue()],
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "林衍走到山门前" in user_text
        assert "他握紧剑柄,目光不离对面那道身影" in user_text
        assert "风过青崖" in user_text

    def test_user_message_ends_with_reviser_role_marker(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # P1-B-22:user message 末尾必须含 agent 身份 marker。
        revise_chapter(
            client=mock_client,
            chapter_text=_SAMPLE_TEXT,
            issues=[_make_issue()],
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert user_text.rstrip().endswith("<!-- agent_role: reviser -->")

    def test_user_message_contains_all_issue_fields(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        issue = _make_issue(
            paragraph_anchor="他握紧剑柄,目光不离对面那道身影",
            issue_type="pacing",
            description="此段铺陈过长,延滞了进入打斗的节奏",
            suggestion="压缩为 2-3 句,直接切入剑势",
        )
        revise_chapter(
            client=mock_client,
            chapter_text=_SAMPLE_TEXT,
            issues=[issue],
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        # 锚点、类型、描述、建议四字段都进 user 消息
        assert issue.paragraph_anchor in user_text
        assert issue.issue_type in user_text
        assert issue.description in user_text
        assert issue.suggestion in user_text

    def test_user_message_renders_multiple_issues(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        issues = [
            _make_issue(
                paragraph_anchor="第一段:林衍走到山门前",
                issue_type="prose",
                description="开篇过于直白",
                suggestion="加一句环境暗示",
            ),
            _make_issue(
                paragraph_anchor="第三段:风过青崖",
                issue_type="hook",
                description="结尾钩子太弱",
                suggestion="把残阵嗡鸣具象化",
            ),
        ]
        revise_chapter(
            client=mock_client,
            chapter_text=_SAMPLE_TEXT,
            issues=issues,
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        # 两条意见的核心字段都进 user 消息;能区分顺序(意见 1 / 意见 2)
        assert "意见 1" in user_text and "意见 2" in user_text
        assert "开篇过于直白" in user_text
        assert "结尾钩子太弱" in user_text
        # 意见 1 应早于意见 2 出现
        assert user_text.index("意见 1") < user_text.index("意见 2")

    def test_f5_max_reviewer_issues_add_at_most_4500_chars(self) -> None:
        """下一轮 Reviser 的 Reviewer 派生部分有界；正文自身不计入该预算。"""
        issues = [
            ReviewIssue(
                paragraph_anchor="锚" * REVIEW_ISSUE_PARAGRAPH_ANCHOR_MAX_CHARS,
                issue_type="类" * REVIEW_ISSUE_TYPE_MAX_CHARS,
                description="描" * REVIEW_ISSUE_DESCRIPTION_MAX_CHARS,
                suggestion="改" * REVIEW_ISSUE_SUGGESTION_MAX_CHARS,
            )
            for _ in range(REVIEWER_ISSUES_MAX_ITEMS)
        ]
        chapter_text = "正文" * 3000

        user_text = _build_user_message(chapter_text=chapter_text, issues=issues)

        assert user_text.count("### 意见 ") == REVIEWER_ISSUES_MAX_ITEMS
        assert len(user_text) - len(chapter_text) <= 4500

    def test_system_message_loaded_from_template_file(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        revise_chapter(
            client=mock_client,
            chapter_text=_SAMPLE_TEXT,
            issues=[_make_issue()],
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        messages = mock_client.complete.call_args.kwargs["messages"]
        assert messages[0].role == "system"
        assert "FAKE_REVISER_SYSTEM_PROMPT" in messages[0].content


class TestClientInvocation:
    """``client.complete`` 调用形态:一次、两条消息、参数透传。"""

    def test_complete_called_once_with_two_messages(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        revise_chapter(
            client=mock_client,
            chapter_text=_SAMPLE_TEXT,
            issues=[_make_issue()],
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        assert mock_client.complete.call_count == 1
        messages = mock_client.complete.call_args.kwargs["messages"]
        assert len(messages) == 2
        assert messages[0].role == "system"
        assert messages[1].role == "user"
        assert all(isinstance(m, Message) for m in messages)

    def test_default_generation_params(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        revise_chapter(
            client=mock_client,
            chapter_text=_SAMPLE_TEXT,
            issues=[_make_issue()],
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        kwargs = mock_client.complete.call_args.kwargs
        assert kwargs["model"] == "claude-sonnet-4-6"
        assert kwargs["max_tokens"] == 8000
        assert kwargs["temperature"] == 0.5

    def test_caller_can_override_generation_params(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        revise_chapter(
            client=mock_client,
            chapter_text=_SAMPLE_TEXT,
            issues=[_make_issue()],
            model="claude-opus-4-7",
            max_tokens=12000,
            temperature=0.4,
            prompts_dir=fake_prompts_dir,
        )
        kwargs = mock_client.complete.call_args.kwargs
        assert kwargs["model"] == "claude-opus-4-7"
        assert kwargs["max_tokens"] == 12000
        assert kwargs["temperature"] == 0.4


class TestReturnValue:
    """``ReviserOutput`` 字段透传 ``LLMResponse``。"""

    def test_returns_reviser_output_with_response_fields(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            content="修订后正文实际内容",
            model_used="claude-sonnet-4-6-20260101",
            input_tokens=999,
            output_tokens=8888,
            finish_reason="end_turn",
        )
        result = revise_chapter(
            client=mock_client,
            chapter_text=_SAMPLE_TEXT,
            issues=[_make_issue()],
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        assert isinstance(result, ReviserOutput)
        assert result.content == "修订后正文实际内容"
        assert result.model_used == "claude-sonnet-4-6-20260101"
        assert result.token_usage.input_tokens == 999
        assert result.token_usage.output_tokens == 8888
        assert result.token_usage.total_tokens == 999 + 8888
        assert result.finish_reason == "end_turn"

    def test_finish_reason_passthrough_none(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(finish_reason=None)
        result = revise_chapter(
            client=mock_client,
            chapter_text=_SAMPLE_TEXT,
            issues=[_make_issue()],
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        assert result.finish_reason is None


class TestEdgeCases:
    """边界:空 issues 列表(理论上不该走到 Reviser,但仍要兜)。"""

    def test_empty_issues_list_renders_as_none_marker(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        revise_chapter(
            client=mock_client,
            chapter_text=_SAMPLE_TEXT,
            issues=[],
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        # 空列表不抛错,user 消息里"审稿意见"段下应有"无"占位
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "审稿意见" in user_text
        assert "无" in user_text
        # 但绝不能出现"意见 1"之类的虚假序号
        assert "意见 1" not in user_text

    def test_empty_issues_does_not_break_call(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        result = revise_chapter(
            client=mock_client,
            chapter_text=_SAMPLE_TEXT,
            issues=[],
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        assert isinstance(result, ReviserOutput)
        assert mock_client.complete.call_count == 1


class TestValidatorFailuresInput:
    """v1.1 G8:`validator_failures` 入参渲染到 user message + LLM 真被调断言。

    覆盖 light 路径(validator pipeline 单条 word_count 失败 + 偏差 < 5%)派
    verdict=revise 时,Orchestrator 透传 `ReviewResult.validator_failure_details`
    给 reviser 的 prompt 渲染行为。
    """

    def test_validator_failures_word_count_renders_in_user_message(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        revise_chapter(
            client=mock_client,
            chapter_text=_SAMPLE_TEXT,
            issues=[],
            validator_failures=[
                {
                    "name": "word_count",
                    "details": {
                        "actual_word_count": 2450,
                        "word_count_min": 2500,
                        "word_count_max": 4000,
                        "reason": "below_min",
                    },
                }
            ],
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "validator 硬约束失败明细" in user_text
        assert "word_count" in user_text
        # 关键 details 字段必须可见(让 LLM 知道差多少字)
        assert "actual_word_count" in user_text
        assert "2450" in user_text
        assert "word_count_min" in user_text
        assert "2500" in user_text
        assert "below_min" in user_text

    def test_validator_failures_none_renders_as_none_marker(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 不传 validator_failures(默认 None)→ 段下应渲染"无"占位,
        # 配合 prompt"validator_failures 为空时走原 ReviewIssue patch 路径"指令
        revise_chapter(
            client=mock_client,
            chapter_text=_SAMPLE_TEXT,
            issues=[_make_issue()],
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "validator 硬约束失败明细" in user_text
        # 段下应有"无"占位
        assert "失败 1" not in user_text  # 不应出现虚假序号

    def test_validator_failures_empty_list_renders_as_none_marker(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 显式传 [] 等价 None — 都走 "无" 占位
        revise_chapter(
            client=mock_client,
            chapter_text=_SAMPLE_TEXT,
            issues=[_make_issue()],
            validator_failures=[],
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "validator 硬约束失败明细" in user_text
        assert "失败 1" not in user_text

    def test_both_issues_and_validator_failures_present(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 两类来源都存在 — reviser prompt 应能同时渲染
        revise_chapter(
            client=mock_client,
            chapter_text=_SAMPLE_TEXT,
            issues=[_make_issue(description="此段铺陈过长 X9")],
            validator_failures=[
                {
                    "name": "word_count",
                    "details": {
                        "actual_word_count": 4080,
                        "word_count_min": 2500,
                        "word_count_max": 4000,
                        "reason": "above_max",
                    },
                }
            ],
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        # ReviewIssue 段
        assert "意见 1" in user_text
        assert "此段铺陈过长 X9" in user_text
        # validator_failures 段
        assert "失败 1" in user_text
        assert "above_max" in user_text
        assert "4080" in user_text

    def test_revision_completed_event_includes_validator_failures_count(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # `revision_completed` 事件应新增 ``validator_failures_count`` 维度
        # (LogsPage / metrics 便于区分 reviser revise 触发源)
        import logging

        import structlog
        from structlog.testing import capture_logs

        # 确保 structlog wrapper 不过滤 INFO
        structlog.configure(
            wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
            cache_logger_on_first_use=False,
        )

        with capture_logs() as logs:
            revise_chapter(
                client=mock_client,
                chapter_text=_SAMPLE_TEXT,
                issues=[_make_issue()],
                validator_failures=[
                    {
                        "name": "word_count",
                        "details": {
                            "actual_word_count": 2450,
                            "word_count_min": 2500,
                            "word_count_max": 4000,
                            "reason": "below_min",
                        },
                    }
                ],
                model="claude-sonnet-4-6",
                prompts_dir=fake_prompts_dir,
            )

        events = [e for e in logs if e.get("event") == "revision_completed"]
        assert len(events) == 1
        assert events[0]["validator_failures_count"] == 1
        assert events[0]["revised_paragraphs_count"] == 1

    def test_validator_failures_with_none_details_safe(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        # 防御:details 字段缺失 / 为 None 时不抛(_render_validator_failures
        # 内部用 `failure.get("details") or {}`)
        revise_chapter(
            client=mock_client,
            chapter_text=_SAMPLE_TEXT,
            issues=[],
            validator_failures=[{"name": "word_count"}],  # details 字段缺失
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        user_text = mock_client.complete.call_args.kwargs["messages"][1].content
        assert "失败 1" in user_text
        assert "word_count" in user_text


class TestR9ReviserConstraintContract:
    """Reviser 看见同一 ChapterCard、共享 paragraph floor 与 PASS 保序项。"""

    def test_contract_renders_card_sources_and_structured_pass(self) -> None:
        source_path = "chapter_cards/ch-0002.must_achieve"
        rule_text = "守住密室入口"
        result = ConstraintResult(
            constraint_id=stable_constraint_id(
                source_path=source_path,
                domain="must_achieve",
                rule_text=rule_text,
            ),
            domain="must_achieve",
            source_path=source_path,
            rule_text=rule_text,
            status="pass",
        )
        text = _format_reviser_hard_gate_contract(
            chapter_card=_make_contract_card(),
            constraint_results=[result],
        )

        assert "局部修订约束合同(REVISER/VALIDATOR SSOT)" in text
        assert "2500 <= actual_word_count <= 4000" in text
        assert "`paragraph_mean_floor`" in text
        assert "paragraph_length_mean >= 30" in text
        assert "守住密室入口" in text
        assert "本章进入密室" in text
        assert "[PASS][must_achieve]" in text
        assert "WARN/SOFT 不得升级为 HARD" in text

    def test_legacy_without_card_does_not_guess_thresholds(self) -> None:
        text = _format_reviser_hard_gate_contract(
            chapter_card=None,
            constraint_results=None,
        )
        assert "ChapterCard 未透传（legacy 路径）" in text
        assert "不得猜字数或内容阈值" in text
        assert "paragraph_length_mean >= 30" in text


class TestPromptsDirInjection:
    """``prompts_dir`` 注入路径:显式传 / 默认走 settings.prompts_dir。"""

    def test_explicit_prompts_dir_overrides_settings(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # 把 settings.prompts_dir 指向另一个不存在的目录;显式参数应覆盖。
        monkeypatch.setattr(
            reviser_module.settings, "prompts_dir", tmp_path / "ignored"
        )
        revise_chapter(
            client=mock_client,
            chapter_text=_SAMPLE_TEXT,
            issues=[_make_issue()],
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        system_text = mock_client.complete.call_args.kwargs["messages"][0].content
        assert "FAKE_REVISER_SYSTEM_PROMPT" in system_text

    def test_default_prompts_dir_uses_settings(
        self,
        mock_client: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 把 settings.prompts_dir 指向可控的临时目录,放一份 marker 模板。
        custom_dir = tmp_path / "settings-prompts"
        custom_dir.mkdir()
        (custom_dir / "reviser.md").write_text(
            "FROM_SETTINGS_DEFAULT", encoding="utf-8"
        )
        monkeypatch.setattr(reviser_module.settings, "prompts_dir", custom_dir)

        revise_chapter(
            client=mock_client,
            chapter_text=_SAMPLE_TEXT,
            issues=[_make_issue()],
            model="claude-sonnet-4-6",
            # 不传 prompts_dir,走 settings 默认
        )
        system_text = mock_client.complete.call_args.kwargs["messages"][0].content
        assert "FROM_SETTINGS_DEFAULT" in system_text


class TestReviserOutputModel:
    """``ReviserOutput`` Pydantic 模型自身校验。"""

    def test_total_tokens_property_via_token_usage(self) -> None:
        out = ReviserOutput(
            content="x",
            model_used="m",
            token_usage=TokenUsage(input_tokens=3, output_tokens=4),
        )
        assert out.token_usage.total_tokens == 7

    def test_finish_reason_optional(self) -> None:
        out = ReviserOutput(
            content="x",
            model_used="m",
            token_usage=TokenUsage(input_tokens=0, output_tokens=0),
        )
        assert out.finish_reason is None


class TestLocalizedStrictOutput:
    """F4：只有 LOCALIZED Reviser 请求 strict schema；GLOBAL 保持纯正文。"""

    def test_localized_contract_prepares_strict_schema(
        self,
        fake_prompts_dir: Path,
    ) -> None:
        card = _make_contract_card()
        plan = compile_executable_constraint_plan(
            chapter_card=card,
            stage="revise",
        )
        issue = _make_issue()
        contract = build_reviser_revision_contract(
            source_content=_SAMPLE_TEXT,
            source_version_id=3,
            executable_constraint_plan=plan,
            issues=[issue],
            validator_failures=[],
            constraint_results=[],
        )
        client = MagicMock(spec=LLMClient)
        captured: dict[str, object] = {}

        def prepare_schema(**kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            gen_params = kwargs["gen_params"]
            assert isinstance(gen_params, dict)
            return {**gen_params, "strict_schema_sentinel": True}

        def complete(**kwargs: object) -> LLMResponse:
            messages = kwargs["messages"]
            assert isinstance(messages, list)
            assert kwargs["strict_schema_sentinel"] is True
            return single_target_localized_response(
                messages,
                replacement="第二段：他立即拔剑迎敌。",
            )

        client.prepare_strict_json_schema.side_effect = prepare_schema
        client.complete.side_effect = complete

        output = revise_chapter(
            client=client,
            model="qwen3.8-max",
            chapter_text=_SAMPLE_TEXT,
            issues=[issue],
            chapter_card=card,
            executable_constraint_plan=plan,
            revision_contract=contract,
            prompts_dir=fake_prompts_dir,
        )

        assert output.content != _SAMPLE_TEXT
        assert captured["schema_name"] == "localized_revision"
        schema = captured["schema"]
        assert isinstance(schema, dict)
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == {
            "contract_version",
            "mode",
            "source_version_id",
            "source_sha256",
            "plan_hash",
            "patches",
        }

    def test_global_contract_does_not_prepare_json_schema(
        self,
        fake_prompts_dir: Path,
    ) -> None:
        card = _make_contract_card()
        plan = compile_executable_constraint_plan(
            chapter_card=card,
            stage="revise",
        )
        validator_failures = [{"name": "word_count", "details": {}}]
        contract = build_reviser_revision_contract(
            source_content=_SAMPLE_TEXT,
            source_version_id=3,
            executable_constraint_plan=plan,
            issues=[],
            validator_failures=validator_failures,
            constraint_results=[],
        )
        assert contract.mode == "global"
        client = MagicMock(spec=LLMClient)
        client.complete.return_value = _make_response(
            content=_SAMPLE_TEXT + "\n\n新增有效段落。"
        )

        revise_chapter(
            client=client,
            model="qwen3.8-max",
            chapter_text=_SAMPLE_TEXT,
            issues=[],
            validator_failures=validator_failures,
            chapter_card=card,
            executable_constraint_plan=plan,
            revision_contract=contract,
            prompts_dir=fake_prompts_dir,
        )

        client.prepare_strict_json_schema.assert_not_called()
