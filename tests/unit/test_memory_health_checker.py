"""``src/core/agents/memory_health_checker.py`` 单测(P4-B-09)。

完全 mock ``LLMClient``,不真调 API;不开 SQLite——agent 函数走 P4-B-07
"预读数据接口"纪律,接 caller 预读的 Character / PlotThread / TimelineEntry
列表,内部无数据库依赖。

覆盖维度:

- 正常路径 4 档 severity:LLM 返合规 JSON(ok / minor / warning / critical)
  → ``MemoryHealthReport`` 字段对齐 + ``issues_count`` 派生属性正确
- 空输入(三类列表 + recent_chapter_ids 全空)→ agent 仍调 LLM(LLM 应
  返 ``severity=ok`` + 三空 issue 列表)
- 解析失败:非法 JSON → ``MemoryHealthCheckerError``,structlog 落
  ``memory_health_checker_parse_failed`` 事件
- 解析失败:schema 缺字段 / 字段类型错 → ``MemoryHealthCheckerError``
- 解析失败:LLM 返非法 ``severity`` 值(``unknown``)→
  ``MemoryHealthCheckerError``
- 代码块包裹兜底(```json ... ```)
- LLM 入参:system + user 两条消息,user 含 workspace_id / characters /
  plot_threads / timeline / recent_chapter_ids 各 section
- structlog ``memory_health_check`` 事件字段(workspace_id / severity /
  issues_count / 三类 count / model_used)
- legacy 路径:``router=None`` + 缺 ``llm`` / ``model`` → ``TypeError``
- ``workspace_id`` 透传(P3-B-14):agent 入参 ``workspace_id`` 反映在
  event 字段,**不读** Character.workspace_id 字段
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from src.core.agents.memory_health_checker import (
    MemoryHealthCheckerError,
    MemoryHealthIssue,
    MemoryHealthReport,
    check_memory_health,
)
from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage
from src.core.models.character import Character, VoiceProfile
from src.core.models.plot_thread import PlotThread
from src.core.models.timeline_entry import TimelineEntry

# ---- LLM 输出 fixture --------------------------------------------------------

_PAYLOAD_OK: dict[str, Any] = {
    "severity": "ok",
    "summary": "工作区共 1 个角色 / 1 条伏笔 / 1 条时间线,无显著问题。",
    "stale_threads": [],
    "inactive_characters": [],
    "conflicts": [],
}

_PAYLOAD_MINOR: dict[str, Any] = {
    "severity": "minor",
    "summary": "少量陈旧伏笔。",
    "stale_threads": [
        {"id": "thread-old", "reason": "ch-0001 埋下后 5 章未推进"}
    ],
    "inactive_characters": [],
    "conflicts": [],
}

_PAYLOAD_WARNING: dict[str, Any] = {
    "severity": "warning",
    "summary": "显著陈旧伏笔 + 部分角色不活跃。",
    "stale_threads": [
        {"id": "t1", "reason": "stale"},
        {"id": "t2", "reason": "stale"},
        {"id": "t3", "reason": "stale"},
    ],
    "inactive_characters": [
        {"id": "c1", "reason": "5 章未出场"},
        {"id": "c2", "reason": "5 章未出场"},
    ],
    "conflicts": [],
}

_PAYLOAD_CRITICAL: dict[str, Any] = {
    "severity": "critical",
    "summary": "检测到角色字段矛盾 + 多条陈旧伏笔。",
    "stale_threads": [
        {"id": "t1", "reason": "stale"},
        {"id": "t2", "reason": "stale"},
        {"id": "t3", "reason": "stale"},
        {"id": "t4", "reason": "stale"},
        {"id": "t5", "reason": "stale"},
    ],
    "inactive_characters": [
        {"id": "c1", "reason": "inactive"},
        {"id": "c2", "reason": "inactive"},
        {"id": "c3", "reason": "inactive"},
    ],
    "conflicts": [
        {
            "id": "c1",
            "reason": "ch-0003 标记 status=dead,ch-0005 仍以活着出场",
        },
    ],
}


def _make_response(
    *,
    content: str,
    model_used: str = "claude-sonnet-4-6-20250101",
) -> LLMResponse:
    return LLMResponse(
        content=content,
        model_used=model_used,
        usage=TokenUsage(input_tokens=80, output_tokens=160),
        finish_reason="end_turn",
    )


def _make_fake_llm(content: str) -> MagicMock:
    """构造伪 LLMClient——``complete()`` 返预设 ``LLMResponse(content=...)``。"""
    fake = MagicMock(spec=LLMClient)
    fake.complete.return_value = _make_response(content=content)
    return fake


# ---- 三类 Pydantic 实体 helper ----------------------------------------------


def _make_character(
    *,
    character_id: str = "lin-yan",
    workspace_id: str = "ws-demo",
    current_state: dict | None = None,
    last_updated_chapter: str | None = "ch-0001",
) -> Character:
    return Character(
        character_id=character_id,
        workspace_id=workspace_id,
        name="林衍",
        aliases=[],
        archetype="萌新剑修",
        appearance="白衣青年",
        personality="沉静内敛",
        background="出身寒门",
        voice_profile=VoiceProfile(
            tone="冷峻",
            sentence_style="短句直白",
            self_address="我",
            addresses_others={},
            catchphrases=[],
            forbidden_phrases=[],
        ),
        goals_long_term=[],
        goals_short_term=[],
        current_state=current_state if current_state is not None else {},
        relationships={},
        abilities=[],
        secrets=[],
        first_appearance_chapter="ch-0001",
        last_updated_chapter=last_updated_chapter,
    )


def _make_plot_thread(
    *,
    thread_id: str = "yu-pei",
    workspace_id: str = "ws-demo",
    status: str = "planted",
    importance: int = 4,
    last_updated_chapter: str | None = "ch-0001",
) -> PlotThread:
    return PlotThread(
        thread_id=thread_id,
        workspace_id=workspace_id,
        title="玉佩之约",
        status=status,  # type: ignore[arg-type]
        importance=importance,
        planted_chapter="ch-0001",
        planned_payoff=None,
        actual_payoff=None,
        description="师姐赠玉佩",
        related_characters=["lin-yan"],
        created_at=datetime.now(UTC),
        last_updated_chapter=last_updated_chapter,
    )


def _make_timeline_entry(
    *,
    entry_id: str = "evt-1",
    workspace_id: str = "ws-demo",
    chapter_id: str = "ch-0001",
    time_order: int = 100,
) -> TimelineEntry:
    return TimelineEntry(
        entry_id=entry_id,
        workspace_id=workspace_id,
        chapter_id=chapter_id,
        description="林衍上山门拜师",
        time_label="本章",
        time_order=time_order,
        involved_characters=["lin-yan"],
    )


# =============================================================================
# 1. 4 档 severity 正常路径
# =============================================================================


class TestSeverityHappyPaths:
    @pytest.mark.parametrize(
        "payload, expected_severity, expected_issues_count",
        [
            (_PAYLOAD_OK, "ok", 0),
            (_PAYLOAD_MINOR, "minor", 1),
            (_PAYLOAD_WARNING, "warning", 5),
            (_PAYLOAD_CRITICAL, "critical", 9),
        ],
    )
    def test_severity_round_trip(
        self,
        payload: dict[str, Any],
        expected_severity: str,
        expected_issues_count: int,
    ) -> None:
        fake_llm = _make_fake_llm(json.dumps(payload))

        report = check_memory_health(
            workspace_id="ws-demo",
            characters=[_make_character()],
            plot_threads=[_make_plot_thread()],
            timeline=[_make_timeline_entry()],
            recent_chapter_ids=["ch-0001"],
            llm=fake_llm,
            model="claude-sonnet-4-6",
        )

        assert isinstance(report, MemoryHealthReport)
        assert report.severity == expected_severity
        assert report.issues_count == expected_issues_count
        assert report.model_used == "claude-sonnet-4-6-20250101"
        # 三类 list 元素是 MemoryHealthIssue 实例
        for issues in (
            report.stale_threads,
            report.inactive_characters,
            report.conflicts,
        ):
            for issue in issues:
                assert isinstance(issue, MemoryHealthIssue)
                assert issue.id
                assert issue.reason


# =============================================================================
# 2. 空输入 + 代码块包裹兜底
# =============================================================================


class TestEdgeInputs:
    def test_empty_inputs_still_call_llm(self) -> None:
        fake_llm = _make_fake_llm(json.dumps(_PAYLOAD_OK))

        report = check_memory_health(
            workspace_id="ws-empty",
            characters=[],
            plot_threads=[],
            timeline=[],
            recent_chapter_ids=[],
            llm=fake_llm,
            model="claude-sonnet-4-6",
        )

        # LLM 被调用一次 + 解析无误
        assert fake_llm.complete.call_count == 1
        assert report.severity == "ok"
        # user message 应明确表达三类空列表 + 无 recent_chapter_ids
        user_msg = _extract_user_msg(fake_llm)
        assert "(无)" in user_msg  # 空 characters / plot_threads / timeline 段
        assert "(无,工作区尚无写章)" in user_msg

    def test_code_fence_wrap_is_stripped(self) -> None:
        fenced = f"```json\n{json.dumps(_PAYLOAD_OK)}\n```"
        fake_llm = _make_fake_llm(fenced)
        report = check_memory_health(
            workspace_id="ws-demo",
            characters=[_make_character()],
            plot_threads=[],
            timeline=[],
            recent_chapter_ids=["ch-0001"],
            llm=fake_llm,
            model="claude-sonnet-4-6",
        )
        assert report.severity == "ok"

    def test_trailing_html_comment_stripped(self) -> None:
        with_comment = (
            json.dumps(_PAYLOAD_OK)
            + "\n<!-- agent_role: extractor -->"
        )
        fake_llm = _make_fake_llm(with_comment)
        report = check_memory_health(
            workspace_id="ws-demo",
            characters=[],
            plot_threads=[],
            timeline=[],
            recent_chapter_ids=[],
            llm=fake_llm,
            model="claude-sonnet-4-6",
        )
        assert report.severity == "ok"


# =============================================================================
# 3. 解析失败
# =============================================================================


class TestParseFailures:
    def test_invalid_json(self) -> None:
        fake_llm = _make_fake_llm("not a json string")
        with pytest.raises(MemoryHealthCheckerError) as exc_info:
            check_memory_health(
                workspace_id="ws-demo",
                characters=[],
                plot_threads=[],
                timeline=[],
                recent_chapter_ids=[],
                llm=fake_llm,
                model="claude-sonnet-4-6",
            )
        assert exc_info.value.raw_output == "not a json string"
        assert exc_info.value.model_used == "claude-sonnet-4-6-20250101"

    def test_schema_missing_field(self) -> None:
        bad = {
            # 缺 severity
            "summary": "x",
            "stale_threads": [],
            "inactive_characters": [],
            "conflicts": [],
        }
        fake_llm = _make_fake_llm(json.dumps(bad))
        with pytest.raises(MemoryHealthCheckerError):
            check_memory_health(
                workspace_id="ws-demo",
                characters=[],
                plot_threads=[],
                timeline=[],
                recent_chapter_ids=[],
                llm=fake_llm,
                model="claude-sonnet-4-6",
            )

    def test_unknown_severity_value(self) -> None:
        bad = dict(_PAYLOAD_OK)
        bad["severity"] = "unknown"
        fake_llm = _make_fake_llm(json.dumps(bad))
        with pytest.raises(MemoryHealthCheckerError):
            check_memory_health(
                workspace_id="ws-demo",
                characters=[],
                plot_threads=[],
                timeline=[],
                recent_chapter_ids=[],
                llm=fake_llm,
                model="claude-sonnet-4-6",
            )

    def test_parse_failure_emits_structlog_event(self) -> None:
        fake_llm = _make_fake_llm("garbage")
        with capture_logs() as logs:
            with pytest.raises(MemoryHealthCheckerError):
                check_memory_health(
                    workspace_id="ws-demo",
                    characters=[],
                    plot_threads=[],
                    timeline=[],
                    recent_chapter_ids=[],
                    llm=fake_llm,
                    model="claude-sonnet-4-6",
                )
        failed_events = [
            r for r in logs
            if r.get("event") == "memory_health_checker_parse_failed"
        ]
        assert len(failed_events) == 1
        assert failed_events[0]["reason"] == "invalid_json"
        assert failed_events[0]["workspace_id"] == "ws-demo"


# =============================================================================
# 4. structlog memory_health_check 事件 + workspace_id 透传
# =============================================================================


class TestStructlogEvent:
    def test_structlog_event_fields(self) -> None:
        fake_llm = _make_fake_llm(json.dumps(_PAYLOAD_WARNING))
        with capture_logs() as logs:
            report = check_memory_health(
                workspace_id="ws-emit",
                characters=[_make_character()],
                plot_threads=[_make_plot_thread()],
                timeline=[_make_timeline_entry()],
                recent_chapter_ids=["ch-0001", "ch-0002"],
                llm=fake_llm,
                model="claude-sonnet-4-6",
            )

        check_events = [r for r in logs if r.get("event") == "memory_health_check"]
        assert len(check_events) == 1
        evt = check_events[0]
        assert evt["workspace_id"] == "ws-emit"
        assert evt["severity"] == "warning"
        assert evt["issues_count"] == report.issues_count == 5
        assert evt["stale_threads_count"] == 3
        assert evt["inactive_characters_count"] == 2
        assert evt["conflicts_count"] == 0
        assert evt["model_used"] == "claude-sonnet-4-6-20250101"

    def test_workspace_id_from_caller_not_from_character_field(self) -> None:
        """P3-B-14 透传纪律:agent 入参 ``workspace_id`` 反映在事件字段,
        即使 Character.workspace_id 字段是另一个值(模板复用场景)。"""
        fake_llm = _make_fake_llm(json.dumps(_PAYLOAD_OK))
        # Character 字段 workspace_id=ws-fixture(漂移源),agent 入参
        # workspace_id=ws-operating(操作工作区)— 事件应取后者。
        drifted_char = _make_character(workspace_id="ws-fixture")
        with capture_logs() as logs:
            check_memory_health(
                workspace_id="ws-operating",
                characters=[drifted_char],
                plot_threads=[],
                timeline=[],
                recent_chapter_ids=[],
                llm=fake_llm,
                model="claude-sonnet-4-6",
            )
        evt = next(r for r in logs if r.get("event") == "memory_health_check")
        assert evt["workspace_id"] == "ws-operating"


# =============================================================================
# 5. LLM 入参契约
# =============================================================================


class TestLLMInvocation:
    def test_messages_shape(self) -> None:
        fake_llm = _make_fake_llm(json.dumps(_PAYLOAD_OK))
        check_memory_health(
            workspace_id="ws-x",
            characters=[_make_character()],
            plot_threads=[_make_plot_thread(thread_id="t-1")],
            timeline=[_make_timeline_entry(entry_id="e-1")],
            recent_chapter_ids=["ch-0001", "ch-0002"],
            llm=fake_llm,
            model="claude-sonnet-4-6",
        )
        call = fake_llm.complete.call_args
        messages: list[Message] = call.kwargs["messages"]
        # system + user 共 2 条
        assert len(messages) == 2
        assert messages[0].role == "system"
        assert messages[1].role == "user"
        user_text = messages[1].content
        # workspace_id / 三类列表 ID / recent chapter_id 都在 user 文本里
        assert "ws-x" in user_text
        assert "lin-yan" in user_text
        assert "t-1" in user_text
        assert "e-1" in user_text
        assert "ch-0001" in user_text
        assert "ch-0002" in user_text
        # 默认 temperature / max_tokens 传给 client
        assert call.kwargs["model"] == "claude-sonnet-4-6"
        assert call.kwargs["temperature"] == 0.2
        assert call.kwargs["max_tokens"] == 2000

    def test_legacy_router_none_missing_llm_raises(self) -> None:
        with pytest.raises(TypeError):
            check_memory_health(
                workspace_id="ws-demo",
                characters=[],
                plot_threads=[],
                timeline=[],
                recent_chapter_ids=[],
                llm=None,
                model=None,
            )


# =============================================================================
# helper
# =============================================================================


def _extract_user_msg(fake_llm: MagicMock) -> str:
    messages = fake_llm.complete.call_args.kwargs["messages"]
    return next(m.content for m in messages if m.role == "user")
