"""MockLLMClient 单测(P1-B-22)。

覆盖:
- Agent 识别(marker 抓取):writer / reviewer / reviser 三分支
- Verdict env 控制:全局 / 章节级 / 默认 / 非法值
- Reviewer 输出可被 ``ReviewResult.model_validate_json`` 解析(配合 P1-B-11
  ``review_chapter`` 端到端跑通)
- Marker 缺失 / chapter_id 缺失 → ``ValueError`` fail loud
- 安全网:总调用次数 > ``_TOTAL_CALL_SAFETY_LIMIT``(W3.5-S3 起 64)抛 ``RuntimeError``
- complete 签名:``model: str | None = None`` 显式 / ``model_used`` 后缀
- TokenUsage 非负
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.core.agents._prompt import (
    AGENT_ROLE_MARKER_EXTRACTOR,
    AGENT_ROLE_MARKER_REVIEWER,
    AGENT_ROLE_MARKER_REVISER,
    AGENT_ROLE_MARKER_WRITER,
)
from src.core.agents.reviewer import review_chapter
from src.core.llm.base import Message
from src.core.llm.mock_client import MockLLMClient
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.review import ReviewResult


# ---- helpers ----------------------------------------------------------------


def _user_message(content: str) -> Message:
    return Message(role="user", content=content)


def _system_message(content: str = "fake system prompt") -> Message:
    return Message(role="system", content=content)


@pytest.mark.parametrize("upper,expected", [(4500, 4500), (100_000_000, 10_000)])
def test_synthetic_writer_bounds_placeholder_allocation(upper, expected):
    messages = [Message(role=m.role, content=m.content.replace("word_count_max=2000", f"word_count_max={upper}"))
                for m in _writer_messages()]
    result = MockLLMClient().complete(messages)
    assert expected - 2 <= len(result.content) <= expected


def _writer_messages(extra: str = "") -> list[Message]:
    return [
        _system_message(),
        _user_message(
            f"## 章节卡\n- 标题:测试章\nword_count_max=2000\nch-0001\n{extra}\n\n"
            f"{AGENT_ROLE_MARKER_WRITER}"
        ),
    ]


def _reviser_messages(chapter_id: str = "ch-0001") -> list[Message]:
    return [
        _system_message(),
        _user_message(
            f"## 审稿意见\n- 修订建议\n- chapter_id:{chapter_id}\n\n"
            f"## 本章原文\n占位文字\n\n{AGENT_ROLE_MARKER_REVISER}"
        ),
    ]


def _reviewer_messages(chapter_id: str = "ch-0001") -> list[Message]:
    """造一组合法 reviewer messages。

    P1-B-22 协议:reviewer marker 带 chapter_id,user message 主体不需要
    再写 chapter_id 字面(P1-B-11 _build_user_message 也没写)。
    """
    marker = f"<!-- agent_role: reviewer chapter_id: {chapter_id} -->"
    return [
        _system_message(),
        _user_message(
            f"## 章节卡硬约束(用于审稿对照)\n- 视角:林衍\n\n"
            f"## 待审正文\n占位\n\n{marker}"
        ),
    ]


def _localized_revision_messages(*, role: str) -> list[Message]:
    marker = (
        AGENT_ROLE_MARKER_WRITER
        if role == "writer"
        else AGENT_ROLE_MARKER_REVISER
    )
    anchor_id = "p0001-" + "d" * 16
    identity = {
        "contract_version": "1",
        "mode": "localized",
        "source_version_id": 2,
        "source_sha256": "c" * 64,
        "plan_hash": "b" * 64,
        "preserve_constraint_ids": [],
        "global_reasons": [],
        "targets": [
            {
                "target_id": "a" * 64,
                "source": "constraint_result",
                "instruction": "手机",
                "allowed_anchor_ids": [anchor_id],
                "allowed_operations": ["replace"],
            }
        ],
    }
    source = [{"anchor_id": anchor_id, "text": "他拿出手机查看消息。"}]
    return [
        _system_message(),
        _user_message(
            "ch-0001\n"
            "## 正文修复执行合同（LOCALIZED PATCH；代码机械合成）\n\n"
            "### 冻结身份与允许范围\n\n"
            f"```json\n{json.dumps(identity, ensure_ascii=False)}\n```\n\n"
            "### 带稳定锚点的源正文\n\n"
            f"```json\n{json.dumps(source, ensure_ascii=False)}\n```\n\n"
            f"{marker}"
        ),
    ]


# ---- TestRoleDispatch -------------------------------------------------------


class TestRoleDispatch:
    """marker 抓取 → 分发到三个分支。"""

    def test_writer_marker_returns_chapter_text(self) -> None:
        client = MockLLMClient()
        response = client.complete(_writer_messages())
        # 头部含 mock 标识 + 占位段落
        assert "[mock]" in response.content
        assert "ch-0001" in response.content
        # 字数靠近 word_count_max=2000
        assert 1500 < len(response.content) < 4000

    def test_global_rewrite_fixture_changes_mock_draft(self) -> None:
        """产品 mock 必须模拟一次真实 GLOBAL 改动，不能永久原样回显。"""
        client = MockLLMClient()
        draft = client.complete(_writer_messages()).content
        rewrite = client.complete(
            _writer_messages("## 正文修复执行合同（代码已显式授权 GLOBAL）")
        ).content

        assert rewrite != draft
        assert "[mock-rewrite]" in rewrite

    def test_reviewer_marker_returns_parseable_json(self) -> None:
        client = MockLLMClient()
        response = client.complete(_reviewer_messages())
        # mock 输出可被 P7-B-06 后的 LLM payload schema 解析(6 score + issues +
        # suggestions;``hard_constraints_passed`` / ``failed_constraints`` 已删,
        # 由 must_achieve_validator + validator_pipeline 综合判定)
        data = json.loads(response.content)
        assert "score_plot_progression" in data
        assert "issues" in data
        assert "suggestions" in data

    def test_reviser_marker_returns_revised_text(self) -> None:
        client = MockLLMClient()
        response = client.complete(_reviser_messages())
        assert "[mock-revised]" in response.content
        assert "ch-0001" in response.content

    @pytest.mark.parametrize("role", ["writer", "reviser"])
    def test_localized_revision_contract_returns_strict_patch(self, role: str) -> None:
        client = MockLLMClient()

        response = client.complete(_localized_revision_messages(role=role))
        payload = json.loads(response.content)

        assert payload["mode"] == "localized"
        assert payload["source_version_id"] == 2
        assert payload["source_sha256"] == "c" * 64
        assert payload["plan_hash"] == "b" * 64
        assert payload["patches"][0]["target_ids"] == ["a" * 64]
        assert "手机" not in payload["patches"][0]["replacement"]

    def test_marker_missing_raises_value_error(self) -> None:
        client = MockLLMClient()
        messages = [_system_message(), _user_message("无任何 marker 的 user 消息")]
        with pytest.raises(ValueError, match="marker"):
            client.complete(messages)

    def test_unknown_marker_value_raises_value_error(self) -> None:
        # marker 形态对但 role 不在白名单(re 不会匹配,等价于"没有 marker")
        client = MockLLMClient()
        messages = [
            _system_message(),
            _user_message("text\n\n<!-- agent_role: unknown_role -->"),
        ]
        with pytest.raises(ValueError, match="marker"):
            client.complete(messages)


# ---- TestVerdictResolution --------------------------------------------------


class TestVerdictResolution:
    """章节级 env > 全局 env > 默认 pass;非法值抛 ValueError。"""

    def test_default_verdict_is_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LLM_MOCK_VERDICT", raising=False)
        monkeypatch.delenv("LLM_MOCK_VERDICT_CH_0001", raising=False)
        client = MockLLMClient()
        response = client.complete(_reviewer_messages("ch-0001"))
        data = json.loads(response.content)
        # pass 路径:全 9.0 + 空 issues(P7-B-06 起 hard_constraints 已从
        # reviewer payload 移除)
        assert data["score_plot_progression"] == 9.0
        assert data["issues"] == []

    def test_global_verdict_revise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_MOCK_VERDICT", "revise")
        monkeypatch.delenv("LLM_MOCK_VERDICT_CH_0001", raising=False)
        client = MockLLMClient()
        response = client.complete(_reviewer_messages("ch-0001"))
        data = json.loads(response.content)
        # revise 路径:全 7.0 + 1 条 issue
        assert data["score_plot_progression"] == 7.0
        assert len(data["issues"]) == 1

    def test_global_verdict_rewrite(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_MOCK_VERDICT", "rewrite")
        monkeypatch.delenv("LLM_MOCK_VERDICT_CH_0001", raising=False)
        client = MockLLMClient()
        response = client.complete(_reviewer_messages("ch-0001"))
        data = json.loads(response.content)
        # P7-B-06:rewrite 路径改走 score 5.0 < revise_threshold 6.5 触发,
        # 不再依赖 hard_constraints_passed=False(后者已从 reviewer payload 删)
        assert data["score_plot_progression"] == 5.0

    def test_chapter_env_overrides_global(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_MOCK_VERDICT", "pass")
        monkeypatch.setenv("LLM_MOCK_VERDICT_CH_0001", "revise")
        client = MockLLMClient()
        response = client.complete(_reviewer_messages("ch-0001"))
        data = json.loads(response.content)
        # 章节级 revise 优先生效
        assert data["score_plot_progression"] == 7.0
        assert len(data["issues"]) == 1

    def test_chapter_env_normalization_uppercase_underscore(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ch-0042 应映射到 LLM_MOCK_VERDICT_CH_0042
        monkeypatch.setenv("LLM_MOCK_VERDICT", "pass")
        monkeypatch.setenv("LLM_MOCK_VERDICT_CH_0042", "rewrite")
        client = MockLLMClient()
        response = client.complete(_reviewer_messages("ch-0042"))
        data = json.loads(response.content)
        # P7-B-06:rewrite 走 score 5.0 路径,不再走 hard_constraints
        assert data["score_plot_progression"] == 5.0

    def test_invalid_verdict_value_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_MOCK_VERDICT", "approve")  # 非允许值
        client = MockLLMClient()
        with pytest.raises(ValueError, match="非法"):
            client.complete(_reviewer_messages("ch-0001"))

    def test_chapter_id_missing_in_reviewer_marker_raises(self) -> None:
        # reviewer marker 必须带 chapter_id(P1-B-22 协议);只用 bare
        # ``<!-- agent_role: reviewer -->`` marker 时 mock 应 fail loud。
        client = MockLLMClient()
        messages = [
            _system_message(),
            _user_message(
                f"## 待审正文\n占位\n\n{AGENT_ROLE_MARKER_REVIEWER}"
            ),
        ]
        with pytest.raises(ValueError, match="chapter_id"):
            client.complete(messages)


# ---- TestSafetyLimit --------------------------------------------------------


class TestSafetyLimit:
    """单实例累计调用 ≥ 上限抛 RuntimeError(安全网,非业务收敛)。

    用 ``_TOTAL_CALL_SAFETY_LIMIT`` 常量自适应,不硬编码具体值(W3.5-S3 由 50→64
    后本测不需改 — 防回退测随常量演进)。
    """

    def test_total_call_safety_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.core.llm.mock_client import _TOTAL_CALL_SAFETY_LIMIT

        monkeypatch.setenv("LLM_MOCK_VERDICT", "pass")
        client = MockLLMClient()
        # 前 _TOTAL_CALL_SAFETY_LIMIT 次正常
        for _ in range(_TOTAL_CALL_SAFETY_LIMIT):
            client.complete(_writer_messages())
        # 第 (上限+1) 次抛 RuntimeError
        with pytest.raises(RuntimeError, match="无限循环"):
            client.complete(_writer_messages())


# ---- TestCompleteContract ---------------------------------------------------


class TestCompleteContract:
    """complete 签名 + LLMResponse 字段契约。"""

    def test_model_param_reflected_in_model_used(self) -> None:
        client = MockLLMClient()
        response = client.complete(_writer_messages(), model="claude-opus-4-7")
        assert response.model_used == "mock-claude-opus-4-7"

    def test_model_default_is_mock_default(self) -> None:
        client = MockLLMClient()
        response = client.complete(_writer_messages())
        assert response.model_used == "mock-default"

    def test_token_usage_non_negative(self) -> None:
        client = MockLLMClient()
        response = client.complete(_writer_messages())
        assert response.usage.input_tokens >= 1
        assert response.usage.output_tokens >= 1

    def test_finish_reason_is_end_turn(self) -> None:
        client = MockLLMClient()
        response = client.complete(_writer_messages())
        assert response.finish_reason == "end_turn"

    def test_extra_gen_params_silently_ignored(self) -> None:
        client = MockLLMClient()
        # mock 不消费 temperature / max_tokens 等,但不应抛错
        response = client.complete(
            _writer_messages(),
            model="claude-sonnet-4-6",
            temperature=0.85,
            max_tokens=8000,
            top_p=0.95,
        )
        assert response.content


# ---- TestExtractorBranch (P2-B-19) ------------------------------------------


def _extractor_messages(chapter_id: str = "ch-0001") -> list[Message]:
    """造一组合法 extractor messages。"""
    return [
        _system_message("fake extractor system prompt"),
        _user_message(
            f"## 章节卡上下文\nchapter_id: {chapter_id}\n\n"
            f"## 待抽取正文\n占位\n\n{AGENT_ROLE_MARKER_EXTRACTOR}"
        ),
    ]


class TestExtractorBranch:
    """P2-B-19:MockLLMClient extractor 分支返合规 4 类 JSON。

    schema 同步预警:P2-B-09 改 ``_ExtractorLLMPayload`` schema 时,Mock 输出
    会立即在这里红;同时让 wiring 集成路径不再抛 NotImplementedError。
    """

    def test_extractor_returns_valid_json_payload(self) -> None:
        client = MockLLMClient()
        response = client.complete(_extractor_messages("ch-0001"))
        payload = json.loads(response.content)
        assert set(payload.keys()) == {
            "characters",
            "plot_threads",
            "timeline",
            "facts",
        }
        assert len(payload["characters"]) == 1
        assert len(payload["plot_threads"]) == 1
        assert len(payload["timeline"]) == 1
        assert len(payload["facts"]) == 2

    def test_extractor_payload_passes_extractor_pydantic_validation(self) -> None:
        """端到端:Mock 输出 → ``_ExtractorLLMPayload.model_validate`` 不抛。"""
        from src.core.agents.memory_extractor import _ExtractorLLMPayload

        client = MockLLMClient()
        response = client.complete(_extractor_messages("ch-0001"))
        payload = _ExtractorLLMPayload.model_validate_json(response.content)
        # P2-B-19 mock 设计:跨章共享 character_id / thread_id
        assert payload.characters[0].character_id == "lin-yan"
        assert payload.plot_threads[0].thread_id == "zhu-jue-cheng-zhang"
        # 每章独立 timeline / facts
        assert payload.timeline[0].entry_id == "mock-event-ch-0001"
        assert payload.facts[0].fact_id == "mock-fact-ch-0001-1"
        assert payload.facts[1].fact_id == "mock-fact-ch-0001-2"

    def test_extractor_time_order_derived_from_chapter_number(self) -> None:
        """``ch-0002`` → time_order=200(单调递增,验证 retriever 排序场景)。"""
        client = MockLLMClient()
        response = client.complete(_extractor_messages("ch-0002"))
        payload = json.loads(response.content)
        assert payload["timeline"][0]["entry_id"] == "mock-event-ch-0002"
        assert payload["timeline"][0]["time_order"] == 200

    def test_extractor_distinct_facts_per_chapter(self) -> None:
        """跨章 fact ID 不同,让 retriever 在第 N+1 章能拿到第 1..N 章的累积。"""
        client = MockLLMClient()
        a = json.loads(client.complete(_extractor_messages("ch-0001")).content)
        b = json.loads(client.complete(_extractor_messages("ch-0002")).content)
        a_ids = {f["fact_id"] for f in a["facts"]}
        b_ids = {f["fact_id"] for f in b["facts"]}
        assert a_ids.isdisjoint(b_ids)


# ---- TestEndToEndWithReviewer -----------------------------------------------


def _make_card_for_reviewer() -> ChapterCard:
    """造一个最小章节卡用于 review_chapter 端到端调用。"""
    return ChapterCard(
        chapter_id="ch-0001",
        workspace_id="ws-demo",
        volume_id="v01",
        chapter_number=1,
        title="Mock 测试章",
        tension_level=5,
        word_count_min=2800,
        word_count_max=4500,
        plot_node_budget=3,
        must_achieve=["占位必达"],
        must_unresolved=["占位未解"],
        forbidden=["占位禁止"],
        pov_character="林衍",
        setting="某地",
        continuity_context=ContinuityContext(
            prev_chapter_tail="",
            unfinished_actions=[],
            time_gap="未知",
            scene_continuation="switch",
            pov_change=False,
            pov_from=None,
            pov_to="林衍",
        ),
        next_chapter_seed="某事",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=["林衍"],
        review_threshold=8.0,
        revise_threshold=6.0,
        max_rewrites=2,
        max_revises=1,
    )


class TestEndToEndWithReviewer:
    """Mock + P1-B-11 review_chapter 端到端:三档 verdict 都跑通。

    这是关键的"调用方 schema 同步预警"——P2/P3 改 ReviewResult schema
    或 _ReviewerLLMPayload 字段时,Mock 输出会立即在这里红。
    """

    @pytest.fixture
    def fake_prompts_dir(self, tmp_path: Path) -> Path:
        (tmp_path / "reviewer.md").write_text(
            "FAKE_REVIEWER_SYSTEM_PROMPT", encoding="utf-8"
        )
        return tmp_path

    def test_pass_path(
        self, monkeypatch: pytest.MonkeyPatch, fake_prompts_dir: Path
    ) -> None:
        monkeypatch.setenv("LLM_MOCK_VERDICT", "pass")
        monkeypatch.delenv("LLM_MOCK_VERDICT_CH_0001", raising=False)
        result = review_chapter(
            client=MockLLMClient(),
            chapter_card=_make_card_for_reviewer(),
            chapter_text="占位正文",
            chapter_id="ch-0001",
            version_reviewed=1,
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        assert isinstance(result, ReviewResult)
        assert result.verdict == "pass"
        assert result.hard_constraints_passed is True
        # ReviewResult.model_validate_json 间接通过 review_chapter 已经覆盖
        # (review_chapter 内部 _ReviewerLLMPayload.model_validate +
        # ReviewResult 构造);此处再正向断言 schema 完整。
        assert isinstance(result.reviewed_at, datetime)
        assert result.reviewed_at.tzinfo is UTC

    def test_revise_path(
        self, monkeypatch: pytest.MonkeyPatch, fake_prompts_dir: Path
    ) -> None:
        monkeypatch.setenv("LLM_MOCK_VERDICT", "revise")
        monkeypatch.delenv("LLM_MOCK_VERDICT_CH_0001", raising=False)
        result = review_chapter(
            client=MockLLMClient(),
            chapter_card=_make_card_for_reviewer(),
            chapter_text="占位正文",
            chapter_id="ch-0001",
            version_reviewed=1,
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        assert result.verdict == "revise"
        assert result.revise_targets, "verdict=revise 时 revise_targets 不应为空"

    def test_rewrite_path(
        self, monkeypatch: pytest.MonkeyPatch, fake_prompts_dir: Path
    ) -> None:
        monkeypatch.setenv("LLM_MOCK_VERDICT", "rewrite")
        monkeypatch.delenv("LLM_MOCK_VERDICT_CH_0001", raising=False)
        result = review_chapter(
            client=MockLLMClient(),
            chapter_card=_make_card_for_reviewer(),
            chapter_text="占位正文",
            chapter_id="ch-0001",
            version_reviewed=1,
            model="claude-sonnet-4-6",
            prompts_dir=fake_prompts_dir,
        )
        assert result.verdict == "rewrite"
        # P7-B-06:reviewer 自身不再判 hard_constraints,默认 True;rewrite
        # 由 score 5.0 < revise_threshold 6.5 路径决定。caller 综合 validator
        # 后可独立覆盖 hard_constraints_passed=False(本测试只到 reviewer
        # 出口,不走 orchestrator,故仍是 True)。
        assert result.hard_constraints_passed is True


# ---- TestInstanceIsolation --------------------------------------------------


class TestInstanceIsolation:
    """每个测试用例新建 MockLLMClient 实例,无跨用例状态泄漏。

    P1-B-22 删掉 reviewer call count 后,实例只剩 ``_total_calls`` 安全网
    计数;两个独立实例间 _total_calls 完全独立,验证文档契约
    "MockLLMClient 实例不应跨测试用例共享"在代码层面成立。
    """

    def test_two_instances_have_independent_counters(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 用 _TOTAL_CALL_SAFETY_LIMIT 常量自适应(W3.5-S3 由 50→64,本测随常量演进)。
        from src.core.llm.mock_client import _TOTAL_CALL_SAFETY_LIMIT

        monkeypatch.setenv("LLM_MOCK_VERDICT", "pass")
        client_a = MockLLMClient()
        client_b = MockLLMClient()

        # client_a 跑满到上限边界
        for _ in range(_TOTAL_CALL_SAFETY_LIMIT):
            client_a.complete(_writer_messages())

        # client_b 全新,少量调用应仍在阈值内,不会被 client_a 的计数影响
        for _ in range(10):
            client_b.complete(_writer_messages())

        # client_a 第 (上限+1) 次仍触发阈值(证明 client_b 的调用没"借"给 client_a)
        with pytest.raises(RuntimeError):
            client_a.complete(_writer_messages())
