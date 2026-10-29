"""Agent 身份 marker 注册表完备性防回退测(v2 间隙 A3①,2026-06-13)。

根因:``_prompt._AGENT_ROLE_MARKER_RE`` 的 alternation 曾是手写清单,与
``AgentRole`` Literal / ``_MARKER_BY_ROLE`` 不同步 —— 漏列
``volume_seam_replay_validator`` / ``must_feasibility_validator`` 两角色,
后果:

1. 真 LLM client(openai_compatible / claude / ollama)的 ``llm_request`` /
   ``llm_response`` 事件 ``agent=None``(逐工作区成本画像归属断裂,质量闸
   成本审计靠时间戳配对才归属);
2. mock 模式下这两个 fail-open validator 因 marker 探测 ValueError 被吞而
   **静默跳过**(维护人"默认 mock 跑全链路"约束下,Fix D 写前预检 +
   卷界重演 gate 两治理闸 mock 路径裸奔)。

修复:正则 alternation 从 ``get_args(AgentRole)`` 动态构建(Literal 成为
单一真相源)+ mock_client 补两角色分支。本文件断言:

- ``AgentRole`` 每个成员都能走通 ``append_role_marker → detect_agent_role``
  闭环(同时覆盖 ``_MARKER_BY_ROLE`` 与正则的完备性 —— 任一处漏列即红);
- mock 两新分支返回严格合规 JSON(条数自适应 / schema 可解析);
- 两 validator 用 MockLLMClient 端到端不再 fail-open。

谁可能破坏:任一往 ``AgentRole`` 加新角色但漏配 ``_MARKER_BY_ROLE`` 的
session(参数化测自动覆盖新成员,加了 Literal 没加 marker 表 → 立即红);
任一改 mock_client 分支链 / 两 validator 输出 schema 的 session。
"""

from __future__ import annotations

from typing import get_args

import pytest

from src.core.agents._prompt import (
    AgentRole,
    _MARKER_BY_ROLE,
    append_role_marker,
    detect_agent_role,
)
from src.core.llm.base import Message
from src.core.llm.mock_client import MockLLMClient

_ALL_ROLES: tuple[str, ...] = get_args(AgentRole)


class TestAgentRoleMarkerRegistryCompleteness:
    """三处清单(Literal / _MARKER_BY_ROLE / 探测正则)永不再漂。"""

    def test_marker_table_covers_every_agent_role(self) -> None:
        """_MARKER_BY_ROLE 的 key 集合与 AgentRole Literal 完全一致。"""
        assert set(_MARKER_BY_ROLE) == set(_ALL_ROLES)

    @pytest.mark.parametrize("role", _ALL_ROLES)
    def test_append_then_detect_roundtrip(self, role: str) -> None:
        """每个角色 append_role_marker → detect_agent_role 闭环返回原角色。

        曾经的回归形态:volume_seam_replay / must_feasibility 在 Literal 与
        marker 表都在册,但探测正则手写清单漏列 → detect 返 (None, None)。
        reviewer 的 marker 契约强制带 chapter_id(P1-B-22),单独适配。
        """
        chapter_id = "ch-0001" if role == "reviewer" else None
        text = append_role_marker(
            "占位 user 消息正文",
            role=role,  # type: ignore[arg-type]
            chapter_id=chapter_id,
        )
        detected, detected_chapter_id = detect_agent_role(
            [Message(role="user", content=text)]
        )
        assert detected == role
        assert detected_chapter_id == chapter_id

    def test_reviewer_chapter_id_still_captured(self) -> None:
        """chapter_id 可选捕获组不因动态构建回归(reviewer 带章号场景)。"""
        text = append_role_marker(
            "审稿正文", role="reviewer", chapter_id="ch-0007"
        )
        detected, chapter_id = detect_agent_role(
            [Message(role="user", content=text)]
        )
        assert detected == "reviewer"
        assert chapter_id == "ch-0007"


class TestMockMustFeasibilityBranch:
    """mock 新分支:must_feasibility(Fix D 写前预检)。"""

    def _complete(self, user_text: str) -> str:
        client = MockLLMClient()
        marked = append_role_marker(user_text, role="must_feasibility_validator")
        response = client.complete(
            messages=[
                Message(role="system", content="判定系统提示"),
                Message(role="user", content=marked),
            ],
            model="mock-model",
        )
        return response.content

    def test_items_count_adapts_to_numbered_musts(self) -> None:
        """``## 待判定``段 3 条编号 → items 恰好 3 条(条数不齐调用方判 fail-open)。"""
        import json

        user_text = (
            "## 前文已写事实\n- ch-0001:前文摘要\n"
            "\n## 待判定:本章(ch-0002)must_achieve\n"
            "1. 主角发现密室\n2. 配角受伤\n3. 凶手留下新线索\n"
            "\n请按系统提示词标准逐条判定可达成性,输出严格 JSON,"
            "items 恰好 3 条、顺序一致。"
        )
        payload = json.loads(self._complete(user_text))
        assert len(payload["items"]) == 3
        assert [it["verdict"] for it in payload["items"]] == ["feasible"] * 3
        assert payload["items"][0]["must"] == "主角发现密室"

    def test_payload_passes_validator_schema(self) -> None:
        """输出严格通过 _FeasibilityLLMPayload 校验(5 字段 + 枚举 + 区间)。"""
        import json

        from src.core.agents.must_feasibility_validator import (
            _FeasibilityLLMPayload,
        )

        user_text = (
            "## 待判定:本章(ch-0003)must_achieve\n1. 唯一事件\n"
            "\n请按系统提示词标准逐条判定可达成性,输出严格 JSON,"
            "items 恰好 1 条、顺序一致。"
        )
        parsed = _FeasibilityLLMPayload.model_validate(
            json.loads(self._complete(user_text))
        )
        assert len(parsed.items) == 1

    def test_validator_end_to_end_no_longer_fails_open(self) -> None:
        """validate_must_feasibility + MockLLMClient 端到端:正常判定非 fail-open。

        修复前形态:marker 探测 ValueError → except 吞 → fail_open=True。
        """
        from src.core.agents.must_feasibility_validator import (
            validate_must_feasibility,
        )

        result = validate_must_feasibility(
            must_achieve=["主角发现密室", "配角受伤"],
            chapter_id="ch-0002",
            recent_summaries=[("ch-0001", "前章摘要:主角抵达庄园。", [])],
            client=MockLLMClient(),
            model="mock-model",
        )
        assert result.fail_open is False
        assert result.passed is True
        assert result.skipped is False
        assert result.items_total == 2
        assert result.llm_calls_count == 1


class TestMockVolumeSeamReplayBranch:
    """mock 新分支:volume_seam_replay(卷界事件重演 gate)。"""

    def test_payload_passes_seam_schema_and_says_no_replay(self) -> None:
        """输出严格通过 _SeamLLMPayload 校验且 replay=false(护 commit happy)。"""
        import json

        from src.core.agents.volume_seam_replay_validator import _SeamLLMPayload

        client = MockLLMClient()
        marked = append_role_marker(
            "## 上卷结尾事件\n…\n## 次卷开篇事件\n…",
            role="volume_seam_replay_validator",
        )
        response = client.complete(
            messages=[
                Message(role="system", content="判定系统提示"),
                Message(role="user", content=marked),
            ],
            model="mock-model",
        )
        parsed = _SeamLLMPayload.model_validate(json.loads(response.content))
        assert parsed.replay is False
