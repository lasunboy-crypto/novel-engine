"""forbidden_crossing_validator agent 单测(v2 W3.5 补强3,2026-06-26)。

覆盖:
1. **happy 无越界**:LLM 返 ``violations=[]`` → passed=True / 空 violations
2. **越界报警**(显式 + 隐式):LLM 返非空 violations → passed=False + 人类可读短句
3. **skip 空 forbidden**:forbidden 空 / 全空白 → skipped / 0 LLM call
4. **skip 空正文**:chapter_text 空 → skipped
5. **fail-open LLM 抛**:LLM 抛 → passed=True + fail_open=True(宁可漏报不误杀)
6. **fail-open 非法 JSON**:LLM 返非法 JSON → fail-open
7. **setup fail-open**:prompt 缺失 → fail-open + 0 LLM call
8. **user message 内容**:含 forbidden 清单 + marker
9. **code fence 剥除**:LLM 用 ```json 包裹 → 正常解析
10. **MockLLMClient 端到端**:经 marker 返"无越界"非 fail-open(护审稿 happy 路径)

完全 mock ``LLMClient``;临时目录注入 fake prompt 模板(沿 setting_consistency 同款 fixture)。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import structlog

from src.core.agents import _prompt as prompt_module
from src.core.agents.forbidden_crossing_validator import (
    ForbiddenCrossingResult,
    validate_forbidden_crossing,
)
from src.core.llm.base import LLMClient, LLMResponse, TokenUsage
from src.core.llm.mock_client import MockLLMClient


def _make_response(
    *,
    violations: list[dict] | None = None,
    model_used: str = "qwen-max",
) -> LLMResponse:
    payload = {"violations": violations or []}
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        model_used=model_used,
        usage=TokenUsage(input_tokens=900, output_tokens=40),
        finish_reason="stop",
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "forbidden_crossing_validator_v4.md").write_text(
        "FAKE_FORBIDDEN_CROSSING_VALIDATOR_SYSTEM_PROMPT\n\n单测断言标记。",
        encoding="utf-8",
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> None:
    prompt_module.load_prompt_template.cache_clear()


@pytest.fixture(autouse=True)
def _ensure_debug_level() -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(0))


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=LLMClient)
    client.complete.return_value = _make_response()
    return client


_FORBIDDEN = [
    "林晓本章绝不踏入、穿过或进入裂隙——无论是否到达临界点,进入留到后续章节",
    "陈远的真实身份本章绝不被当众揭穿",
]
_MUST_ACHIEVE = [
    "林晓必须在裂隙外辨认石壁刻痕",
    "陈远必须向同伴交出一份未署名的旧档案",
]
# 守界正文(站在临界点前、未跨过)——mock 路径下内容不影响 mock 返回值,仅供真实性。
_CHAPTER = "她的指尖贴上石壁,壁面泛起微光,最终却只是后退一步。" + "守界" * 200


def _call(
    mock_client: MagicMock,
    fake_prompts_dir: Path,
    *,
    chapter_text: str = _CHAPTER,
    forbidden: list[str] | None = None,
    must_achieve: list[str] | None = None,
    chapter_id: str = "ch-0007",
) -> ForbiddenCrossingResult:
    return validate_forbidden_crossing(
        chapter_text=chapter_text,
        forbidden=_FORBIDDEN if forbidden is None else forbidden,
        must_achieve=(
            _MUST_ACHIEVE if must_achieve is None else must_achieve
        ),
        chapter_id=chapter_id,
        workspace_id="ws-demo",
        client=mock_client,
        model="qwen-max",
        prompts_dir=fake_prompts_dir,
    )


class TestHappy:
    def test_no_violations_passes(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        result = _call(mock_client, fake_prompts_dir)
        assert result.passed is True
        assert result.violations == []
        assert result.llm_calls_count == 1
        assert result.skipped is False
        assert result.fail_open is False
        assert [item.status for item in result.constraint_results] == [
            "pass",
            "pass",
        ]


class TestViolations:
    def test_explicit_crossing_reported(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            violations=[
                {
                    "item_index": 1,
                    "subject": "林晓",
                    "evidence": "林晓一步踏进裂隙,身影没入幽蓝",
                }
            ]
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            chapter_text="林晓一步踏进裂隙,身影没入幽蓝。",
        )
        assert result.passed is False
        assert len(result.violations) == 1
        # 人类可读短句含被违反的 forbidden + evidence
        assert "forbidden 越界:「林晓本章绝不踏入" in result.violations[0]
        assert "踏进裂隙" in result.violations[0]
        assert result.llm_calls_count == 1
        assert result.fail_open is False
        assert [item.status for item in result.constraint_results] == [
            "fail",
            "pass",
        ]
        assert result.constraint_results[0].rule_text == _FORBIDDEN[0]

    def test_implicit_crossing_reported(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """隐式越界(被光吞没 / 身后合拢)= judge 抓到的关键弱点 → 同样报。"""
        mock_client.complete.return_value = _make_response(
            violations=[
                {
                    "item_index": 1,
                    "subject": "林晓",
                    "evidence": "林晓被蓝光吞没,再睁眼四周是光的甬道、身后裂口看不见",
                }
            ]
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            chapter_text="林晓被蓝光吞没,再睁眼四周是光的甬道、身后裂口看不见。",
        )
        assert result.passed is False
        assert "林晓被蓝光吞没" in result.violations[0]

    def test_multiple_violations_reported(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = _make_response(
            violations=[
                {"item_index": 1, "subject": "林晓", "evidence": "林晓踏进了裂隙"},
                {
                    "item_index": 2,
                    "subject": "陈远",
                    "evidence": "陈远的卧底身份被当众戳穿",
                },
            ]
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            chapter_text="林晓踏进了裂隙。陈远的卧底身份被当众戳穿。",
        )
        assert result.passed is False
        assert len(result.violations) == 2
        assert "陈远" in result.violations[1]


class TestSkip:
    def test_empty_forbidden_skips(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        result = _call(mock_client, fake_prompts_dir, forbidden=[])
        assert result.skipped is True
        assert result.passed is True
        assert result.llm_calls_count == 0
        assert mock_client.complete.call_count == 0

    def test_whitespace_only_forbidden_skips(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """forbidden 全为空白串 → 视作无禁元素 → skip(不发起 LLM 调用)。"""
        result = _call(mock_client, fake_prompts_dir, forbidden=["  ", ""])
        assert result.skipped is True
        assert result.llm_calls_count == 0
        assert mock_client.complete.call_count == 0

    def test_empty_chapter_text_skips(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        result = _call(mock_client, fake_prompts_dir, chapter_text="   ")
        assert result.skipped is True
        assert result.llm_calls_count == 0
        assert mock_client.complete.call_count == 0


class TestFailOpen:
    def test_llm_raises_fail_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """LLM 抛 → fail-open(passed=True):judge 故障绝不误触发 rewrite(零误杀同向)。"""
        mock_client.complete.side_effect = RuntimeError("qwen timeout")
        result = _call(mock_client, fake_prompts_dir)
        assert result.passed is True
        assert result.fail_open is True
        assert result.violations == []
        assert result.llm_calls_count == 1

    def test_invalid_json_fail_open(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        mock_client.complete.return_value = LLMResponse(
            content="这不是 JSON",
            model_used="qwen-max",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
            finish_reason="stop",
        )
        result = _call(mock_client, fake_prompts_dir)
        assert result.passed is True
        assert result.fail_open is True

    @pytest.mark.parametrize(
        "violations",
        [
            [
                {"item_index": 1, "subject": "林晓", "evidence": "证据 A"},
                {"item_index": 1, "subject": "林晓", "evidence": "重复编号"},
            ],
            [
                {
                    "item_index": 3,
                    "subject": "林晓",
                    "evidence": "越出两条权威清单",
                }
            ],
            [
                {
                    "item_index": 1,
                    "subject": "林晓",
                    "evidence": "证据 A",
                    "forbidden": "模型自行复述的非权威规则",
                }
            ],
        ],
    )
    def test_invalid_index_or_extra_rule_text_fails_open(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        violations: list[dict],
    ) -> None:
        """编号须唯一有效，且模型复述规则不能冒充权威 constraint 身份。"""
        mock_client.complete.return_value = _make_response(violations=violations)
        result = _call(mock_client, fake_prompts_dir)
        assert result.passed is True
        assert result.fail_open is True
        assert result.constraint_results == []

    def test_setup_fail_open_missing_prompt(
        self, mock_client: MagicMock, tmp_path: Path
    ) -> None:
        empty_dir = tmp_path / "empty_prompts"
        empty_dir.mkdir()
        result = _call(mock_client, empty_dir)
        assert result.passed is True
        assert result.fail_open is True
        assert result.llm_calls_count == 0
        assert mock_client.complete.call_count == 0


class TestCodeFence:
    def test_strips_json_code_fence(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """LLM 用 ```json 包裹输出 → 剥除后正常解析为越界。"""
        body = json.dumps(
            {
                "violations": [
                    {
                        "item_index": 1,
                        "subject": "林晓",
                        "evidence": "林晓踏进裂隙",
                    }
                ]
            },
            ensure_ascii=False,
        )
        mock_client.complete.return_value = LLMResponse(
            content=f"```json\n{body}\n```",
            model_used="qwen-max",
            usage=TokenUsage(input_tokens=10, output_tokens=20),
            finish_reason="stop",
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            chapter_text="林晓踏进裂隙。",
        )
        assert result.passed is False
        assert len(result.violations) == 1

    def test_unbound_subject_is_dropped_without_hard_failure(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        """证据虽为原文，主体却不在对应规则中 → 保守丢弃，不触发 rewrite。"""
        mock_client.complete.return_value = _make_response(
            violations=[
                {
                    "item_index": 1,
                    "subject": "陈远",
                    "evidence": "陈远踏进了裂隙",
                }
            ]
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            chapter_text="陈远踏进了裂隙。",
        )
        assert result.passed is True
        assert result.fail_open is False
        assert [item.status for item in result.constraint_results] == ["pass", "pass"]

    @pytest.mark.parametrize("subject", ["她", "角色", "本章"])
    def test_ambiguous_shared_subject_is_not_promoted_to_hard(
        self,
        mock_client: MagicMock,
        fake_prompts_dir: Path,
        subject: str,
    ) -> None:
        """代词/占位词虽是两边共有子串，也不能冒充同一具体主体。"""
        rule = f"{subject}绝不踏入裂隙"
        evidence = f"{subject}踏入了裂隙"
        mock_client.complete.return_value = _make_response(
            violations=[
                {"item_index": 1, "subject": subject, "evidence": evidence}
            ]
        )
        result = _call(
            mock_client,
            fake_prompts_dir,
            chapter_text=f"{evidence}。",
            forbidden=[rule],
        )
        assert result.passed is True
        assert result.fail_open is False
        assert result.constraint_results[0].status == "pass"


class TestUserMessage:
    def test_user_message_contains_identity_context_forbidden_and_marker(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        _call(mock_client, fake_prompts_dir)
        user_msg = next(
            m
            for m in mock_client.complete.call_args.kwargs["messages"]
            if m.role == "user"
        )
        content = user_msg.content
        assert "本章必须发生的情节" in content
        assert "仅作人物/对象/事件身份消歧" in content
        assert "林晓必须在裂隙外辨认石壁刻痕" in content
        assert "陈远必须向同伴交出一份未署名的旧档案" in content
        assert "本章禁止发生的情节" in content
        assert "绝不踏入" in content  # forbidden 条目
        assert "陈远的真实身份" in content
        assert "<!-- agent_role: forbidden_crossing_validator -->" in content

    def test_empty_must_context_is_explicit_and_keeps_forbidden_call(
        self, mock_client: MagicMock, fake_prompts_dir: Path
    ) -> None:
        result = _call(
            mock_client,
            fake_prompts_dir,
            must_achieve=[],
        )

        assert result.passed is True
        assert result.skipped is False
        user_msg = next(
            message
            for message in mock_client.complete.call_args.kwargs["messages"]
            if message.role == "user"
        )
        assert "must_achieve;仅作人物/对象/事件身份消歧" in user_msg.content
        assert "（无）" in user_msg.content


class TestPromptVersioning:
    def test_v3_is_archived_verbatim_and_v4_binds_subject(self) -> None:
        prompts_dir = Path(__file__).parents[2] / "prompts"
        archived_v3 = (
            prompts_dir / "archive" / "forbidden_crossing_validator_v3.md"
        ).read_bytes()
        active_v4 = (
            prompts_dir / "forbidden_crossing_validator_v4.md"
        ).read_text(encoding="utf-8")

        assert archived_v3.startswith(b"# forbidden ")
        assert "subject" in active_v4
        assert "规则与证据中逐字共同出现" in active_v4
        assert "无法绑定就不报" in active_v4
        assert "不能改选两边都出现的地点" in active_v4
        assert "代词或占位词不是明确主体" in active_v4
        assert "沈聿" not in active_v4
        assert "林澈" not in active_v4


class TestMockClientEndToEnd:
    def test_mock_returns_no_violation_not_fail_open(self) -> None:
        """真 MockLLMClient 经 marker 返 ``violations=[]`` → passed + 非 fail-open。

        护审稿 happy 路径:既有 mock 跑(无真 LLM)审稿不因新闸误触发 rewrite。
        用真 prompts_dir(settings.prompts_dir)= 验真 prompt 文件就位。
        """
        result = validate_forbidden_crossing(
            chapter_text=_CHAPTER,
            forbidden=_FORBIDDEN,
            chapter_id="ch-0007",
            workspace_id="ws-demo",
            client=MockLLMClient(),
            model="mock",
        )
        assert result.passed is True
        assert result.violations == []
        assert result.skipped is False
        assert result.fail_open is False
        assert result.llm_calls_count == 1
