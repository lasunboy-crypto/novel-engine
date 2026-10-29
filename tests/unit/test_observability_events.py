"""OBSERVABILITY §2.4 关键事件埋点覆盖测试(P1-B-24)。

按"新模块 + 事件契约"分组:

1. ``TestMakeTraceId``:trace_id 生成器格式 + 边界
2. ``TestHashPrompt``:prompt 哈希稳定性 + 消息序列敏感性
3. ``TestArchivePrompt``:provider 路由 + 幂等 + 落盘正确性
4. ``TestBindChapterContext``:contextvars 绑定 + 退出清理 + 异常路径清理
5. ``TestMockClientEvents``:``llm_request`` / ``llm_response`` 字段对齐
6. ``TestClaudeClientEvents``:同上 + ``archive_prompt`` 真落盘(monkeypatch SDK)
7. ``TestAgentEvents``:writer / reviewer / reviser 三个完成事件
8. ``TestOrchestratorEvents``:``state_transition`` + ``human_intervention_required``
9. ``TestWorkspaceLifecycleEvents``:6 个 action 各 1 条事件

事件捕获走 ``structlog.testing.capture_logs``。**注意 capture_logs 的工作方式**:
它把 structlog 整条 processor 链替换为单个 ``LogCapture`` processor,所以
``merge_contextvars`` processor 不会跑——contextvars 字段(``workspace_id`` /
``chapter_id`` / ``trace_id``)**不会**自动出现在捕获的 event_dict 里。

要验证 contextvars 绑定本身是否正确,直接读 ``structlog.contextvars.
get_contextvars()`` 断言;要验证业务事件本身是否发出与字段对齐,断言捕获
event_dict 的业务字段(不含 contextvars 字段)。生产环境(``configure_logging``
配置的 processor 链)``merge_contextvars`` 正常跑,JSON 输出会带这三字段。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog
from structlog.testing import capture_logs

from src.api.logging import bind_chapter_context
from src.config import settings
from src.core.agents import _prompt as prompt_module
from src.core.agents.reviewer import review_chapter
from src.core.agents.reviser import revise_chapter
from src.core.agents.writer import write_chapter
from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage
from src.core.llm.mock_client import MockLLMClient
from src.core.llm.prompt_hash import (
    archive_prompt,
    hash_prompt,
    render_prompt_text,
)
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.review import ReviewIssue
from src.core.orchestrator.chapter_orchestrator import orchestrate_chapter
from src.core.orchestrator.trace import make_trace_id
from src.core.state_machine.states import ChapterState
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.workspace_service import WorkspaceService


# ---------------------------------------------------------------------------
# 公共工厂
# ---------------------------------------------------------------------------


def _msgs(*pairs: tuple[str, str]) -> list[Message]:
    return [Message(role=role, content=content) for role, content in pairs]


def _make_continuity(**overrides: Any) -> ContinuityContext:
    payload: dict[str, Any] = dict(
        prev_chapter_tail="他握紧剑柄。",
        unfinished_actions=[],  # P4-B-07:衔接桥默认通过
        time_gap="立即续接",
        scene_continuation="switch",  # P4-B-07:跳过 scene_continuation_location
        pov_change=False,
        pov_from="林衍",
        pov_to="林衍",
    )
    payload.update(overrides)
    return ContinuityContext(**payload)


def _make_card(chapter_id: str = "ch-0001", **overrides: Any) -> ChapterCard:
    payload: dict[str, Any] = dict(
        chapter_id=chapter_id,
        workspace_id="ws-demo",
        volume_id="v01",
        chapter_number=1,
        title="第一章",
        tension_level=6,
        # P4-B-06:validator 默认放宽,本文件测的是 observability 事件埋点。
        word_count_min=1,
        word_count_max=10**8,
        plot_node_budget=3,
        must_achieve=[],
        must_unresolved=[],
        forbidden=[],
        pov_character="林衍",
        setting="青崖山",
        continuity_context=_make_continuity(),
        next_chapter_seed="伏笔 X",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=["thread-main"],
        related_characters=["林衍"],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=1,
        max_revises=1,
    )
    payload.update(overrides)
    return ChapterCard(**payload)


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "writer.md").write_text("WRITER", encoding="utf-8")
    (prompts_dir / "reviewer.md").write_text("REVIEWER", encoding="utf-8")
    (prompts_dir / "reviser.md").write_text("REVISER", encoding="utf-8")
    return prompts_dir


@pytest.fixture(autouse=True)
def _patch_prompts_dir(
    monkeypatch: pytest.MonkeyPatch, fake_prompts_dir: Path
) -> None:
    monkeypatch.setattr(settings, "prompts_dir", fake_prompts_dir)


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> Any:
    prompt_module.load_prompt_template.cache_clear()
    yield
    prompt_module.load_prompt_template.cache_clear()


# ---------------------------------------------------------------------------
# 1. make_trace_id
# ---------------------------------------------------------------------------


class TestMakeTraceId:
    def test_basic_format(self) -> None:
        assert make_trace_id("ch-0012", "rewrite", 0) == "ch-0012-rewrite0"
        assert make_trace_id("ch-0012", "rewrite", 1) == "ch-0012-rewrite1"
        assert make_trace_id("ch-0012", "revise", 0) == "ch-0012-revise0"
        assert make_trace_id("ch-0001", "write", 0) == "ch-0001-write0"

    def test_negative_n_raises(self) -> None:
        with pytest.raises(ValueError, match="n 不能为负"):
            make_trace_id("ch-0001", "write", -1)


# ---------------------------------------------------------------------------
# 2. hash_prompt
# ---------------------------------------------------------------------------


class TestHashPrompt:
    def test_hash_is_12_hex_chars(self) -> None:
        h = hash_prompt(_msgs(("user", "hello")))
        assert len(h) == 12
        assert all(c in "0123456789abcdef" for c in h)

    def test_same_messages_same_hash(self) -> None:
        h1 = hash_prompt(_msgs(("system", "S"), ("user", "U")))
        h2 = hash_prompt(_msgs(("system", "S"), ("user", "U")))
        assert h1 == h2

    def test_different_content_different_hash(self) -> None:
        h1 = hash_prompt(_msgs(("user", "hello")))
        h2 = hash_prompt(_msgs(("user", "world")))
        assert h1 != h2

    def test_role_swap_changes_hash(self) -> None:
        h1 = hash_prompt(_msgs(("system", "X"), ("user", "Y")))
        h2 = hash_prompt(_msgs(("user", "X"), ("system", "Y")))
        assert h1 != h2


# ---------------------------------------------------------------------------
# 3. archive_prompt
# ---------------------------------------------------------------------------


class TestArchivePrompt:
    def test_mock_provider_returns_none_no_file(self, tmp_path: Path) -> None:
        result = archive_prompt(
            workspaces_root=tmp_path,
            workspace_id="ws-demo",
            prompt_hash="abc123def456",
            content="should not be written",
            provider="mock",
        )
        assert result is None
        # 不创建任何目录
        assert not (tmp_path / "ws-demo" / "archives").exists()

    def test_claude_provider_writes_file(self, tmp_path: Path) -> None:
        # v1.0.x wave 6 s2.6a:预创建 workspace 根目录,对齐生产语义
        # (workspace_service.create 已建好骨架),让 archive_prompt 新加的
        # "workspace 销毁防御"check 在测试路径下正常 fallthrough。
        (tmp_path / "ws-demo").mkdir(parents=True, exist_ok=True)
        result = archive_prompt(
            workspaces_root=tmp_path,
            workspace_id="ws-demo",
            prompt_hash="abc123def456",
            content="prompt text",
            provider="claude",
        )
        assert result is not None
        expected = tmp_path / "ws-demo" / "archives" / "prompts" / "abc123def456.txt"
        assert result == expected
        assert expected.read_text(encoding="utf-8") == "prompt text"

    def test_idempotent_no_overwrite(self, tmp_path: Path) -> None:
        # v1.0.x wave 6 s2.6a:预创建 workspace 根目录(同 test_claude_provider_writes_file)
        (tmp_path / "ws-demo").mkdir(parents=True, exist_ok=True)
        first = archive_prompt(
            workspaces_root=tmp_path,
            workspace_id="ws-demo",
            prompt_hash="hashx",
            content="first content",
            provider="claude",
        )
        # 第二次 hash 相同;函数不重写,返回同路径
        second = archive_prompt(
            workspaces_root=tmp_path,
            workspace_id="ws-demo",
            prompt_hash="hashx",
            content="second content (would overwrite if not idempotent)",
            provider="claude",
        )
        assert second == first
        assert first is not None
        # 内容仍是首次写入的
        assert first.read_text(encoding="utf-8") == "first content"

    def test_render_prompt_text_human_readable(self) -> None:
        text = render_prompt_text(_msgs(("system", "S body"), ("user", "U body")))
        assert "## role: system" in text
        assert "## role: user" in text
        assert "S body" in text
        assert "U body" in text


# ---------------------------------------------------------------------------
# 4. bind_chapter_context
# ---------------------------------------------------------------------------


class TestBindChapterContext:
    """直接读 ``get_contextvars()`` 验证绑定本身;生产环境的 ``merge_contextvars``
    processor 把这三字段合到每条 structlog 调用的 event_dict 里(capture_logs
    的局限见模块 docstring)。"""

    def test_fields_present_inside(self) -> None:
        with bind_chapter_context(
            workspace_id="ws-1",
            chapter_id="ch-0001",
            trace_id="ch-0001-write0",
        ):
            ctx = structlog.contextvars.get_contextvars()
            assert ctx["workspace_id"] == "ws-1"
            assert ctx["chapter_id"] == "ch-0001"
            assert ctx["trace_id"] == "ch-0001-write0"

    def test_fields_cleared_after_exit(self) -> None:
        with bind_chapter_context(
            workspace_id="ws-1", chapter_id="ch-0001", trace_id="t1"
        ):
            pass
        ctx = structlog.contextvars.get_contextvars()
        assert "workspace_id" not in ctx
        assert "chapter_id" not in ctx
        assert "trace_id" not in ctx

    def test_exception_path_still_clears(self) -> None:
        with pytest.raises(RuntimeError):
            with bind_chapter_context(
                workspace_id="ws-1",
                chapter_id="ch-0001",
                trace_id="t1",
            ):
                raise RuntimeError("boom")
        ctx = structlog.contextvars.get_contextvars()
        assert "workspace_id" not in ctx
        assert "chapter_id" not in ctx
        assert "trace_id" not in ctx


# ---------------------------------------------------------------------------
# 5. MockLLMClient events
# ---------------------------------------------------------------------------


def _writer_messages() -> list[Message]:
    return _msgs(
        ("system", "S"),
        ("user", "ch-0001 chapter\n\n<!-- agent_role: writer -->"),
    )


def _reviewer_messages() -> list[Message]:
    return _msgs(
        ("system", "S"),
        (
            "user",
            "ch-0001 review\n\n<!-- agent_role: reviewer chapter_id: ch-0001 -->",
        ),
    )


class TestMockClientEvents:
    def test_llm_request_and_response_emitted(self) -> None:
        client = MockLLMClient()
        with capture_logs() as logs:
            client.complete(_reviewer_messages(), model="claude-sonnet-4-6")

        events = {e["event"] for e in logs if "event" in e}
        assert "llm_request" in events
        assert "llm_response" in events

        req = next(e for e in logs if e["event"] == "llm_request")
        assert req["provider"] == "mock"
        assert req["model"] == "claude-sonnet-4-6"
        assert req["agent"] == "reviewer"
        assert req["input_tokens"] >= 1
        assert len(req["prompt_hash"]) == 12

        resp = next(e for e in logs if e["event"] == "llm_response")
        assert resp["provider"] == "mock"
        assert resp["model"] == "mock-claude-sonnet-4-6"
        assert resp["agent"] == "reviewer"
        assert resp["output_tokens"] >= 1
        assert resp["cost_usd"] == 0.0
        assert resp["latency_ms"] >= 0
        assert resp["finish_reason"] == "end_turn"
        assert resp["prompt_hash"] == req["prompt_hash"]


# ---------------------------------------------------------------------------
# 6. ClaudeClient events(monkeypatch anthropic SDK)
# ---------------------------------------------------------------------------


class TestClaudeClientEvents:
    def test_emits_events_and_archives_prompt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 直接构造 ClaudeClient,绕开真 anthropic SDK 的 API key 校验
        from src.core.llm import claude_client as cc

        # 让 settings.data_dir 指向 tmp_path,archive_prompt 才会写到此处
        monkeypatch.setattr(settings, "data_dir", tmp_path)

        # v1.0.x wave 6 s2.6a:预创建 ws-demo workspace 根目录,对齐生产语义,
        # 让 archive_prompt 新加的"workspace 销毁防御"check 正常 fallthrough。
        (tmp_path / "ws-demo").mkdir(parents=True, exist_ok=True)

        # 构造 fake anthropic 响应对象
        class FakeUsage:
            input_tokens = 123
            output_tokens = 456

        class FakeBlock:
            type = "text"
            text = "fake response body"

        class FakeResponse:
            content = [FakeBlock()]
            model = "claude-sonnet-4-6-20251001"
            usage = FakeUsage()
            stop_reason = "end_turn"

        class FakeMessages:
            def create(self, **kwargs: Any) -> FakeResponse:
                return FakeResponse()

        class FakeSDK:
            def __init__(self) -> None:
                self.messages = FakeMessages()

        # 跳过 ClaudeClient.__init__ 的 SDK 实例化(需要真 API key);
        # 直接 __new__ 然后注入字段。
        client = cc.ClaudeClient.__new__(cc.ClaudeClient)
        client._model = "claude-sonnet-4-6"  # type: ignore[attr-defined]
        client._sdk = FakeSDK()  # type: ignore[attr-defined]

        messages = _writer_messages()

        with capture_logs() as logs:
            with bind_chapter_context(
                workspace_id="ws-demo",
                chapter_id="ch-0001",
                trace_id="ch-0001-write0",
            ):
                response = client.complete(messages)

        # 响应正确返回
        assert response.content == "fake response body"
        assert response.usage.input_tokens == 123

        # 事件发了
        events = [e["event"] for e in logs if "event" in e]
        assert "llm_request" in events
        assert "llm_response" in events

        req = next(e for e in logs if e["event"] == "llm_request")
        assert req["provider"] == "claude"
        assert req["agent"] == "writer"
        assert req["model"] == "claude-sonnet-4-6"
        # contextvars 字段(workspace_id/chapter_id/trace_id)在 capture_logs
        # 内不出现(processor 链被替换),由生产 merge_contextvars 注入。

        resp = next(e for e in logs if e["event"] == "llm_response")
        assert resp["provider"] == "claude"
        assert resp["model"] == "claude-sonnet-4-6-20251001"
        assert resp["input_tokens"] == 123
        assert resp["output_tokens"] == 456
        assert resp["cost_usd"] == 0.0
        assert resp["finish_reason"] == "end_turn"

        # prompt 原文真落盘了(ClaudeClient 直接读 get_contextvars()
        # 不依赖 processor 链,故 archive_prompt 正常调用)
        archive_dir = tmp_path / "ws-demo" / "archives" / "prompts"
        archive_files = list(archive_dir.glob("*.txt"))
        assert len(archive_files) == 1
        archived = archive_files[0].read_text(encoding="utf-8")
        # 原文含 system + user 拼好后的字面
        assert "## role: system" in archived
        assert "## role: user" in archived
        assert "agent_role: writer" in archived

    def test_no_archive_without_workspace_context(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """contextvars 没绑 workspace_id 时不写归档(单测兼容)。"""
        from src.core.llm import claude_client as cc

        monkeypatch.setattr(settings, "data_dir", tmp_path)

        class FakeUsage:
            input_tokens = 1
            output_tokens = 1

        class FakeBlock:
            type = "text"
            text = "x"

        class FakeResponse:
            content = [FakeBlock()]
            model = "m"
            usage = FakeUsage()
            stop_reason = "end_turn"

        class FakeMessages:
            def create(self, **kwargs: Any) -> FakeResponse:
                return FakeResponse()

        class FakeSDK:
            def __init__(self) -> None:
                self.messages = FakeMessages()

        client = cc.ClaudeClient.__new__(cc.ClaudeClient)
        client._model = "claude-sonnet-4-6"  # type: ignore[attr-defined]
        client._sdk = FakeSDK()  # type: ignore[attr-defined]

        # 不绑 contextvars
        client.complete(_writer_messages())

        # 没有任何 archives 目录被创建
        assert not (tmp_path / "ws-demo").exists()


# ---------------------------------------------------------------------------
# 7. Agent completion events
# ---------------------------------------------------------------------------


def _make_llm_response_for(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        model_used="claude-sonnet-4-6-20251001",
        usage=TokenUsage(input_tokens=100, output_tokens=200),
        finish_reason="end_turn",
    )


class TestAgentEvents:
    def test_writer_emits_chapter_draft_completed(self) -> None:
        client = MagicMock(spec=LLMClient)
        client.complete.return_value = _make_llm_response_for("正文若干")
        card = _make_card(chapter_id="ch-0007")

        with capture_logs() as logs:
            write_chapter(
                client=client,
                chapter_card=card,
                memory_section="",
                model="claude-sonnet-4-6",
            )

        evt = next(e for e in logs if e.get("event") == "chapter_draft_completed")
        assert evt["chapter_id"] == "ch-0007"
        assert evt["word_count"] == len("正文若干")
        assert evt["model_used"] == "claude-sonnet-4-6-20251001"
        assert evt["finish_reason"] == "end_turn"

    def test_reviewer_emits_review_completed_with_verdict(self) -> None:
        client = MagicMock(spec=LLMClient)
        payload = {
            "score_plot_progression": 9.0,
            "score_tension_match": 9.0,
            "score_prose_quality": 9.0,
            "score_consistency": 9.0,
            "score_hook_management": 9.0,
            "score_voice_consistency": 9.0,
            "hard_constraints_passed": True,
            "failed_constraints": [],
            "issues": [],
            "suggestions": ["good"],
        }
        client.complete.return_value = _make_llm_response_for(
            json.dumps(payload, ensure_ascii=False)
        )
        card = _make_card(chapter_id="ch-0008")

        with capture_logs() as logs:
            review_chapter(
                client=client,
                chapter_card=card,
                chapter_text="正文",
                chapter_id="ch-0008",
                version_reviewed=1,
                model="claude-sonnet-4-6",
            )

        evt = next(e for e in logs if e.get("event") == "review_completed")
        assert evt["chapter_id"] == "ch-0008"
        assert evt["version_reviewed"] == 1
        assert evt["verdict"] == "pass"
        # P7-B-06:``hard_constraints_passed`` 字段从 ``review_completed`` 事件
        # 删除(reviewer 不再判此项;hard_constraints 综合在 orchestrator 自己的
        # ``must_achieve_validator_overrode_verdict`` / pipeline ``validator_failed``
        # 事件落)
        assert "hard_constraints_passed" not in evt
        assert evt["issues_count"] == 0
        assert evt["score_overall"] == pytest.approx(9.0)

    def test_reviser_emits_revision_completed(self) -> None:
        client = MagicMock(spec=LLMClient)
        client.complete.return_value = _make_llm_response_for("修订后正文")
        issues = [
            ReviewIssue(
                paragraph_anchor="x",
                issue_type="pacing",
                description="d",
                suggestion="s",
            ),
            ReviewIssue(
                paragraph_anchor="y",
                issue_type="continuity",
                description="d2",
                suggestion="s2",
            ),
        ]

        with capture_logs() as logs:
            revise_chapter(
                client=client,
                chapter_text="原文",
                issues=issues,
                model="claude-sonnet-4-6",
            )

        evt = next(e for e in logs if e.get("event") == "revision_completed")
        assert evt["revised_paragraphs_count"] == 2
        assert evt["model_used"] == "claude-sonnet-4-6-20251001"


# ---------------------------------------------------------------------------
# 8. Orchestrator events
# ---------------------------------------------------------------------------


class TestOrchestratorEvents:
    """state_transition + human_intervention_required 字段对齐。

    走 happy path:write → review (verdict=pass) → APPROVED,期待两条
    state_transition(drafting→reviewing、reviewing→approved)无
    human_intervention_required。
    """

    def _build_paths(self, tmp_path: Path) -> WorkspacePaths:
        workspaces_root = tmp_path / "workspaces"
        workspaces_root.mkdir()
        (workspaces_root / "ws-demo").mkdir()
        (workspaces_root / "ws-demo" / "data").mkdir()
        return WorkspacePaths(
            "ws-demo", workspaces_root=workspaces_root
        )

    def test_pass_path_emits_two_state_transitions_no_escalation(
        self, tmp_path: Path
    ) -> None:
        paths = self._build_paths(tmp_path)
        client = MagicMock(spec=LLMClient)

        # writer 先返回正文,reviewer 返回 verdict=pass
        review_payload = {
            "score_plot_progression": 9.0,
            "score_tension_match": 9.0,
            "score_prose_quality": 9.0,
            "score_consistency": 9.0,
            "score_hook_management": 9.0,
            "score_voice_consistency": 9.0,
            "hard_constraints_passed": True,
            "failed_constraints": [],
            "issues": [],
            "suggestions": ["ok"],
        }
        client.complete.side_effect = [
            _make_llm_response_for("正文"),
            _make_llm_response_for(json.dumps(review_payload)),
        ]

        card = _make_card(chapter_id="ch-0001")

        with capture_logs() as logs:
            result = orchestrate_chapter(
                client=client,
                chapter_card=card,
                paths=paths,
                memory_section="",
                workspace_id="ws-demo",
                model="claude-sonnet-4-6",
            )

        assert result.final_state == ChapterState.APPROVED
        transitions = [
            (e["from_state"], e["to_state"])
            for e in logs
            if e.get("event") == "state_transition"
        ]
        assert ("drafting", "reviewing") in transitions
        assert ("reviewing", "approved") in transitions
        # 没有升人工
        assert not any(
            e.get("event") == "human_intervention_required" for e in logs
        )

    def test_rewrite_budget_exhausted_emits_human_intervention(
        self, tmp_path: Path
    ) -> None:
        paths = self._build_paths(tmp_path)
        client = MagicMock(spec=LLMClient)

        # max_rewrites=1:writer 返正文,reviewer 返 verdict=rewrite,
        # 第二次 rewrite 后 reviewer 再返 rewrite,触发预算耗尽升人工
        rewrite_payload = {
            "score_plot_progression": 5.0,
            "score_tension_match": 5.0,
            "score_prose_quality": 5.0,
            "score_consistency": 5.0,
            "score_hook_management": 5.0,
            "score_voice_consistency": 5.0,
            "hard_constraints_passed": False,
            "failed_constraints": ["fc"],
            "issues": [],
            "suggestions": [],
        }
        # write → review(rewrite) → rewrite → review(rewrite)→升人工
        client.complete.side_effect = [
            _make_llm_response_for("正文 v1"),
            _make_llm_response_for(json.dumps(rewrite_payload)),
            _make_llm_response_for("正文 v2"),
            _make_llm_response_for(json.dumps(rewrite_payload)),
        ]

        card = _make_card(chapter_id="ch-0001", max_rewrites=1, max_revises=0)

        with capture_logs() as logs:
            result = orchestrate_chapter(
                client=client,
                chapter_card=card,
                paths=paths,
                memory_section="",
                workspace_id="ws-demo",
                model="claude-sonnet-4-6",
            )

        assert result.final_state == ChapterState.HUMAN_INTERVENTION
        assert result.escalation_reason == "rewrite_budget_exhausted"

        evt = next(
            e for e in logs if e.get("event") == "human_intervention_required"
        )
        assert evt["reason"] == "rewrite_budget_exhausted"
        assert evt["rewrite_count"] == 1
        assert evt["revise_count"] == 0


# ---------------------------------------------------------------------------
# 9. WorkspaceService lifecycle events(6 actions)
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace_service(tmp_path: Path) -> WorkspaceService:
    workspaces_root = tmp_path / "workspaces"
    archives_root = tmp_path / "archives"
    workspaces_root.mkdir()
    archives_root.mkdir()
    return WorkspaceService(
        workspaces_root=workspaces_root, archives_root=archives_root
    )


class TestWorkspaceLifecycleEvents:
    def test_create_emits_event(self, workspace_service: WorkspaceService) -> None:
        with capture_logs() as logs:
            workspace_service.create(
                "ws-1", display_name="dn", genre="g", premise="p"
            )
        evt = next(
            e
            for e in logs
            if e.get("event") == "workspace_lifecycle" and e.get("action") == "create"
        )
        assert evt["workspace_id"] == "ws-1"
        assert evt["activated"] is True
        assert "engine_version" in evt

    def test_clean_emits_event(self, workspace_service: WorkspaceService) -> None:
        workspace_service.create(
            "ws-1", display_name="dn", genre="g", premise="p"
        )
        with capture_logs() as logs:
            workspace_service.clean("ws-1")
        evt = next(
            e
            for e in logs
            if e.get("event") == "workspace_lifecycle" and e.get("action") == "clean"
        )
        assert evt["workspace_id"] == "ws-1"

    def test_archive_emits_event(self, workspace_service: WorkspaceService) -> None:
        workspace_service.create(
            "ws-1", display_name="dn", genre="g", premise="p"
        )
        with capture_logs() as logs:
            workspace_service.archive("ws-1")
        evt = next(
            e
            for e in logs
            if e.get("event") == "workspace_lifecycle" and e.get("action") == "archive"
        )
        assert evt["workspace_id"] == "ws-1"
        assert evt["archive_file"].endswith("ws-1.tar.gz")

    def test_purge_emits_warning_event(
        self, workspace_service: WorkspaceService
    ) -> None:
        workspace_service.create(
            "ws-1", display_name="dn", genre="g", premise="p"
        )
        with capture_logs() as logs:
            workspace_service.purge("ws-1", confirm=True)
        evt = next(
            e
            for e in logs
            if e.get("event") == "workspace_lifecycle" and e.get("action") == "purge"
        )
        assert evt["workspace_id"] == "ws-1"
        # purge 是危险操作,应走 warning 级别
        assert evt["log_level"] == "warning"

    def test_backup_emits_event(self, workspace_service: WorkspaceService) -> None:
        workspace_service.create(
            "ws-1", display_name="dn", genre="g", premise="p"
        )
        with capture_logs() as logs:
            workspace_service.backup("ws-1")
        evt = next(
            e
            for e in logs
            if e.get("event") == "workspace_lifecycle" and e.get("action") == "backup"
        )
        assert evt["workspace_id"] == "ws-1"
        assert "backup_file" in evt

    def test_restore_emits_event(
        self, workspace_service: WorkspaceService
    ) -> None:
        workspace_service.create(
            "ws-1", display_name="dn", genre="g", premise="p"
        )
        workspace_service.archive("ws-1")
        with capture_logs() as logs:
            workspace_service.restore("ws-1")
        evt = next(
            e
            for e in logs
            if e.get("event") == "workspace_lifecycle" and e.get("action") == "restore"
        )
        assert evt["workspace_id"] == "ws-1"
        assert evt["archive_file"].endswith("ws-1.tar.gz")
