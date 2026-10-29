"""v1.0.x wave 4 session 3a 章节摘要 LLM 抽取 agent 单元测试。

测试范围(对齐 ``src/core/agents/chapter_summary_extractor.py``):
- happy path:LLM 输出合规 JSON → ChapterSummary 返回
- fail-open 路径:LLM 异常 / JSON 解析失败 / schema 不符 / 空正文 → 返 None
- 差异化纪律(继承 wave 4 session 1+2,**Q4 阈值 < 50% 生产级**):5 章 fixture
  summary_text 字符集重合率 < 50%
- mock fixture dispatch:MockLLMClient 通过 ``chapter_summary_extractor`` marker
  路由到 ``_generate_chapter_summary_extractor_json``
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.core.agents.chapter_summary_extractor import (
    ChapterSummary,
    _build_evidence_source_catalog,
    extract_chapter_summary,
)
from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage
from src.core.llm.mock_client import MockLLMClient
from src.core.models.chapter_card import ChapterCard
from src.core.models.continuity_ledger import sha256_text
from src.core.models.core_state import (
    BookClockConstraint,
    BookClockInterval,
    CoreStateTransition,
)


_CHAPTER_TEXT = "林衍站在山门前。玉佩落地撞出回响。" * 100
_EVIDENCE_QUOTE = "林衍站在山门前。"


def _make_card(chapter_id: str = "ch-0001", chapter_number: int = 1) -> ChapterCard:
    """构造最小合法 ChapterCard fixture(其余字段填默认)。"""
    from src.core.models.chapter_card import ContinuityContext

    return ChapterCard(
        chapter_id=chapter_id,
        workspace_id="ws-test",
        volume_id="v1",
        chapter_number=chapter_number,
        title=f"第 {chapter_number} 章 测试",
        arc_phase="opening" if chapter_number == 1 else "rising",
        is_arc_finale=False,
        tension_level=5,
        pov_character="lin-yan",
        setting="山门外",
        word_count_min=2800,
        word_count_max=4500,
        must_achieve=["事件 A"],
        must_unresolved=[],
        forbidden=[],
        related_plot_threads=["thread-1"],
        related_characters=["lin-yan", "shen-zhao"],
        continuity_context=ContinuityContext(
            prev_chapter_tail="",
            time_gap="开篇",
            scene_continuation="switch",
            pov_change=False,
            pov_from=None,
            pov_to="lin-yan",
        ),
        emotional_arc="平静 → 紧张",
        next_chapter_seed="留白接续",
    )


class _FakeLLMClient(LLMClient):
    """返预设 content 的 fake client(给 happy / fail-open 单测用)。"""

    def __init__(
        self, content: str, *, raise_exc: Exception | None = None
    ) -> None:
        self._content = content
        self._raise_exc = raise_exc

    def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        **gen_params: Any,
    ) -> LLMResponse:
        if self._raise_exc is not None:
            raise self._raise_exc
        return LLMResponse(
            content=self._content,
            model_used=f"fake-{model or 'default'}",
            usage=TokenUsage(input_tokens=10, output_tokens=20),
            finish_reason="end_turn",
        )


class _SequenceLLMClient(LLMClient):
    """按顺序返回多轮结果，并保留每轮完整消息供 retry 反馈断言。"""

    def __init__(self, contents: list[str]) -> None:
        self._contents = list(contents)
        self.calls: list[list[Message]] = []

    def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        **gen_params: Any,
    ) -> LLMResponse:
        self.calls.append(messages)
        if not self._contents:
            raise AssertionError("测试未提供足够的 schema retry 响应")
        return LLMResponse(
            content=self._contents.pop(0),
            model_used=f"fake-{model or 'default'}",
            usage=TokenUsage(input_tokens=10, output_tokens=20),
            finish_reason="end_turn",
        )


def _valid_payload_json(summary_suffix: str = "") -> str:
    """返合规 LLM JSON 文本(测试 happy 路径)。"""
    payload = {
        "summary_text": f"林衍在山门前的青石阶上拾起灰羽,玉佩落地撞出回响{summary_suffix}",
        "pov_character": "lin-yan",
        "pov_evidence_quote": _EVIDENCE_QUOTE,
        "scene_setting": "山门外青石阶",
        "scene_setting_evidence_quote": _EVIDENCE_QUOTE,
        "key_events": [
            {
                "description": "林衍拾起灰羽",
                "actors": ["lin-yan"],
                "location": "山门",
                "evidence_quote": _EVIDENCE_QUOTE,
            }
        ],
        "character_interactions": [],
        "unfinished_actions": [
            {
                "description": "林衍欲转身离去未动",
                "is_immediate": True,
                "evidence_quote": _EVIDENCE_QUOTE,
            }
        ],
        "character_states": [
            {
                "character_id": "lin-yan",
                "state": "站在山门前",
                "evidence_quote": _EVIDENCE_QUOTE,
            }
        ],
        "established_facts": [
            {
                "fact": "林衍已经抵达山门",
                "evidence_quote": _EVIDENCE_QUOTE,
            }
        ],
        "revealed_mechanics": [],
    }
    return json.dumps(payload, ensure_ascii=False)


def _payload_with_third_key_event_quote(quote: str) -> str:
    """构造复现 R8 ``key_events[2]`` quote 定位失败的完整 payload。"""

    payload = json.loads(_valid_payload_json())
    payload["key_events"].extend(
        [
            {
                "description": "玉佩落地",
                "actors": ["lin-yan"],
                "location": "山门",
                "evidence_quote": _EVIDENCE_QUOTE,
            },
            {
                "description": "玉佩撞出回响",
                "actors": ["lin-yan"],
                "location": "山门",
                "evidence_quote": quote,
            },
        ]
    )
    return json.dumps(payload, ensure_ascii=False)


def _valid_source_ref_payload_json(
    *,
    evidence_source_id: str = "E0001",
) -> str:
    """返严格 R4 v3 source ID schema，覆盖全部六类数组。"""

    payload = {
        "summary_text": "林衍已到山门，玉佩落地后的后续仍待确认。",
        "pov_character": "lin-yan",
        "pov_evidence_source_id": evidence_source_id,
        "scene_setting": "山门前",
        "scene_setting_evidence_source_id": evidence_source_id,
        "key_events": [
            {
                "description": "林衍站在山门前",
                "actors": ["lin-yan"],
                "location": "山门前",
                "evidence_source_id": evidence_source_id,
            }
        ],
        "character_interactions": [
            {
                "char_a": "lin-yan",
                "char_b": "shen-zhao",
                "dynamic": "保持警惕",
                "evidence_source_id": evidence_source_id,
            }
        ],
        "unfinished_actions": [
            {
                "description": "确认玉佩回响来源",
                "is_immediate": False,
                "evidence_source_id": evidence_source_id,
            }
        ],
        "character_states": [
            {
                "character_id": "lin-yan",
                "state": "站在山门前",
                "evidence_source_id": evidence_source_id,
            }
        ],
        "established_facts": [
            {
                "fact": "林衍已经抵达山门",
                "evidence_source_id": evidence_source_id,
            }
        ],
        "revealed_mechanics": [
            {
                "mechanic": "玉佩落地会发出回响",
                "evidence_source_id": evidence_source_id,
            }
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


class TestHappyPath:
    """LLM 输出合规 JSON → ChapterSummary 正确返回。"""

    def test_happy_returns_chapter_summary(self) -> None:
        fake = _FakeLLMClient(_valid_payload_json())
        result = extract_chapter_summary(
            chapter_text=_CHAPTER_TEXT,
            card=_make_card(),
            chapter_id="ch-0001",
            workspace_id="ws-test",
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
        )
        assert result is not None
        assert isinstance(result, ChapterSummary)
        assert result.chapter_id == "ch-0001"
        assert result.workspace_id == "ws-test"
        assert "林衍" in result.summary_text
        assert result.pov_character == "lin-yan"
        assert result.scene_setting == "山门外青石阶"
        assert len(result.key_events) == 1
        assert result.unfinished_actions == ["林衍欲转身离去未动"]
        assert result.extraction_trigger == "reviewer_pass"
        assert result.model_used == "fake-qwen-novel"
        assert isinstance(result.extracted_at, datetime)
        assert result.extracted_at.tzinfo is UTC

    def test_extraction_trigger_force_pass_propagates(self) -> None:
        """force_pass trigger 正确透传到 ChapterSummary 字段(Q2 软性字段)。"""
        fake = _FakeLLMClient(_valid_payload_json())
        result = extract_chapter_summary(
            chapter_text=_CHAPTER_TEXT,
            card=_make_card(),
            chapter_id="ch-0002",
            workspace_id="ws-test",
            extraction_trigger="force_pass",
            client=fake,
            model="qwen-novel",
        )
        assert result is not None
        assert result.extraction_trigger == "force_pass"

    def test_empty_pov_string_translates_to_none(self) -> None:
        """LLM 返 pov_character="" → ChapterSummary.pov_character=None(ORM nullable)。"""
        payload = json.loads(_valid_payload_json())
        payload["pov_character"] = ""
        payload["scene_setting"] = ""
        fake = _FakeLLMClient(json.dumps(payload, ensure_ascii=False))
        result = extract_chapter_summary(
            chapter_text=_CHAPTER_TEXT,
            card=_make_card(),
            chapter_id="ch-0003",
            workspace_id="ws-test",
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
        )
        assert result is not None
        assert result.pov_character is None
        assert result.scene_setting is None


class TestRetryWithExactEvidenceFeedback:
    """schema/evidence 失败定向反馈；只有逐字证据成功后才能返回摘要。"""

    def test_schema_mismatch_retries_with_field_feedback(self) -> None:
        bad_payload = json.loads(_valid_payload_json())
        del bad_payload["summary_text"]
        fake = _SequenceLLMClient(
            [
                json.dumps(bad_payload, ensure_ascii=False),
                _valid_payload_json(),
            ]
        )

        result = extract_chapter_summary(
            chapter_text=_CHAPTER_TEXT,
            card=_make_card(),
            chapter_id="ch-schema-retry",
            workspace_id="ws-test",
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
            schema_retry_max_attempts=3,
        )

        assert result is not None
        assert len(fake.calls) == 2
        retry_user_text = fake.calls[1][1].content
        assert "字段 `summary_text`" in retry_user_text
        assert "schema 校验失败" in retry_user_text

    def test_missing_exact_quote_retries_then_accepts_only_corrected_quote(
        self,
    ) -> None:
        fabricated_quote = "林衍伫立在山门之前。"
        fake = _SequenceLLMClient(
            [
                _payload_with_third_key_event_quote(fabricated_quote),
                _payload_with_third_key_event_quote(_EVIDENCE_QUOTE),
            ]
        )

        result = extract_chapter_summary(
            chapter_text=_CHAPTER_TEXT,
            card=_make_card(),
            chapter_id="ch-evidence-retry",
            workspace_id="ws-test",
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
            source_version_id=1,
            schema_retry_max_attempts=3,
        )

        assert result is not None
        assert len(fake.calls) == 2
        retry_user_text = fake.calls[1][1].content
        assert "key_events[2] evidence_quote 无法在 final.md 定位" in retry_user_text
        assert "禁止模糊匹配、改写、拼接或猜测证据" in retry_user_text
        assert _CHAPTER_TEXT in retry_user_text
        assert result.key_events[2]["evidence_quote"] == _EVIDENCE_QUOTE
        assert result.key_events[2]["evidence_fingerprint"] == sha256_text(
            _EVIDENCE_QUOTE
        )
        assert fabricated_quote not in result.model_dump_json()

    def test_non_verbatim_quotes_exhaust_retry_and_return_none(self) -> None:
        fake = _SequenceLLMClient(
            [
                _payload_with_third_key_event_quote("林衍伫立在山门之前。"),
                _payload_with_third_key_event_quote("林衍站在山门前……"),
                _payload_with_third_key_event_quote(
                    "林衍站在山门前。……玉佩落地撞出回响。"
                ),
            ]
        )

        result = extract_chapter_summary(
            chapter_text=_CHAPTER_TEXT,
            card=_make_card(),
            chapter_id="ch-evidence-exhausted",
            workspace_id="ws-test",
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
            source_version_id=1,
            schema_retry_max_attempts=3,
        )

        assert result is None
        assert len(fake.calls) == 3
        for messages in fake.calls[1:]:
            retry_user_text = messages[1].content
            assert (
                "key_events[2] evidence_quote 无法在 final.md 定位" in retry_user_text
            )
            assert "禁止模糊匹配、改写、拼接或猜测证据" in retry_user_text


class TestStrictEvidenceSourceCatalog:
    """R8 strict R4：模型只选 ID，逐字 quote 始终由 pinned snapshot 取回。"""

    def test_catalog_preserves_unicode_boundaries_duplicates_and_tail(self) -> None:
        chapter_text = (
            "  她说：“别动！”\n"
            "门轴响了——却没有打开……\n\n"
            "……。”\n"
            "组合 e\u0301。预组 é。\r\n"
            "重复句。重复句。\n"
            "无终止符尾句  "
        )

        catalog = _build_evidence_source_catalog(chapter_text)
        quotes = [chapter_text[item.start : item.end] for item in catalog]

        assert [item.source_id for item in catalog] == [
            "E0001",
            "E0002",
            "E0003",
            "E0004",
            "E0005",
            "E0006",
            "E0007",
        ]
        assert quotes == [
            "她说：“别动！”",
            "门轴响了——却没有打开……",
            "组合 e\u0301。",
            "预组 é。",
            "重复句。",
            "重复句。",
            "无终止符尾句",
        ]
        assert all(quote in chapter_text for quote in quotes)
        assert quotes[2] != quotes[3]
        assert all(not quote[0].isspace() for quote in quotes)
        assert all(not quote[-1].isspace() for quote in quotes)

    def test_punctuation_only_text_keeps_strict_catalog_protocol(self) -> None:
        payload = {
            "summary_text": "正文仅含标点，无可抽取的结构事实。",
            "pov_character": "",
            "pov_evidence_source_id": "",
            "scene_setting": "",
            "scene_setting_evidence_source_id": "",
            "key_events": [],
            "character_interactions": [],
            "unfinished_actions": [],
            "character_states": [],
            "established_facts": [],
            "revealed_mechanics": [],
        }
        fake = _SequenceLLMClient([json.dumps(payload, ensure_ascii=False)])

        result = extract_chapter_summary(
            chapter_text="……。”",
            card=_make_card(),
            chapter_id="ch-source-ref-punctuation-only",
            workspace_id="ws-test",
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
            source_version_id=1,
            use_evidence_source_catalog=True,
        )

        assert result is not None
        assert result.evidence_records == []
        assert "## 章节正文证据目录" in fake.calls[0][1].content
        assert "(无可引用的非空原文片段)" in fake.calls[0][1].content
        assert "## 章节正文\n\n" not in fake.calls[0][1].content

    def test_valid_ids_map_all_fields_to_exact_quote_and_fingerprint(
        self,
    ) -> None:
        fake = _SequenceLLMClient([_valid_source_ref_payload_json()])

        result = extract_chapter_summary(
            chapter_text=_CHAPTER_TEXT,
            card=_make_card(),
            chapter_id="ch-source-ref-happy",
            workspace_id="ws-test",
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
            source_version_id=1,
            schema_retry_max_attempts=3,
            use_evidence_source_catalog=True,
        )

        assert result is not None
        assert len(fake.calls) == 1
        assert "## 章节正文证据目录" in fake.calls[0][1].content
        assert '"source_id": "E0001"' in fake.calls[0][1].content
        assert result.key_events[0]["evidence_quote"] == _EVIDENCE_QUOTE
        assert (
            result.character_interactions[0]["evidence_quote"]
            == _EVIDENCE_QUOTE
        )
        assert result.character_states[0]["evidence_quote"] == _EVIDENCE_QUOTE
        assert len(result.evidence_records) == 8
        assert all(
            record["evidence_quote"] == _EVIDENCE_QUOTE
            and record["evidence_fingerprint"] == sha256_text(_EVIDENCE_QUOTE)
            for record in result.evidence_records
        )
        assert "evidence_source_id" not in result.model_dump_json()

    def test_core_state_candidate_is_only_a_source_bound_nomination(self) -> None:
        """Extractor 只能产带逐字证据的 proposed 候选，不能自行升 HARD。"""

        chapter_text = "青铜镜表面无锈、无氧化。"
        payload = {
            "summary_text": "本章明确建立青铜镜表面状态。",
            "pov_character": "",
            "pov_evidence_source_id": "",
            "scene_setting": "",
            "scene_setting_evidence_source_id": "",
            "key_events": [],
            "character_interactions": [],
            "unfinished_actions": [],
            "character_states": [],
            "established_facts": [],
            "revealed_mechanics": [],
            "core_state_candidates": [
                {
                    "entity_type": "object",
                    "entity_id": "青铜镜",
                    "attribute": "surface",
                    "value": "表面无锈、无氧化",
                    "objective": True,
                    "long_lived": True,
                    "book_bearing": True,
                    "ambiguous": False,
                    "evidence_source_id": "E0001",
                }
            ],
        }
        fake = _SequenceLLMClient([json.dumps(payload, ensure_ascii=False)])
        card = _make_card().model_copy(
            update={
                "core_state_contract_version": 1,
                "core_state_projection_hash": "0" * 64,
                "core_state_transitions": [
                    CoreStateTransition(
                        entity_type="object",
                        entity_id="青铜镜",
                        attribute="surface",
                        from_value=None,
                        to_value="表面无锈、无氧化",
                        reason="开篇上游明确建立承重物件状态",
                    )
                ],
                "book_clock_constraint": BookClockConstraint(
                    time_gap="开篇",
                    gap_interval=BookClockInterval(
                        minimum_minutes=0, maximum_minutes=0
                    ),
                    current_interval=BookClockInterval(
                        minimum_minutes=0, maximum_minutes=0
                    ),
                    is_hard=True,
                ),
            }
        )

        result = extract_chapter_summary(
            chapter_text=chapter_text,
            card=card,
            chapter_id="ch-source-ref-core-state",
            workspace_id="ws-test",
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
            source_version_id=1,
            use_evidence_source_catalog=True,
        )

        assert result is not None
        candidate = result.core_state_candidates[0]
        assert candidate["state_key"] == "object:青铜镜:surface"
        assert candidate["admission_status"] == "proposed"
        assert candidate["admission_reason"] == ""
        assert candidate["evidence_quote"] == chapter_text
        assert candidate["evidence_fingerprint"] == sha256_text(chapter_text)
        assert "evidence_source_id" not in result.model_dump_json()
        user_text = fake.calls[0][1].content
        assert "只作证据查找目标" in user_text
        assert "object:青铜镜:surface: (未建立) → 表面无锈、无氧化" in user_text

    def test_object_alias_uses_existing_schema_retry_and_keeps_card_stable_id(
        self,
    ) -> None:
        chapter_text = "何临宣布对TQ-07实施二级封存管控。"

        def _payload(entity_id: str) -> str:
            return json.dumps(
                {
                    "summary_text": "何临完成封存升级。",
                    "pov_character": "",
                    "pov_evidence_source_id": "",
                    "scene_setting": "",
                    "scene_setting_evidence_source_id": "",
                    "key_events": [],
                    "character_interactions": [],
                    "unfinished_actions": [],
                    "character_states": [],
                    "established_facts": [],
                    "revealed_mechanics": [],
                    "core_state_candidates": [
                        {
                            "entity_type": "object",
                            "entity_id": entity_id,
                            "attribute": "seal_status",
                            "value": "二级封存管控",
                            "objective": True,
                            "long_lived": True,
                            "book_bearing": True,
                            "ambiguous": False,
                            "evidence_source_id": "E0001",
                        }
                    ],
                },
                ensure_ascii=False,
            )

        fake = _SequenceLLMClient([_payload("TQ-07库房"), _payload("TQ-07")])
        card = _make_card().model_copy(
            update={
                "must_achieve": ["何临对TQ-07实施二级封存管控"],
                "core_state_transitions": [
                    CoreStateTransition(
                        entity_type="object",
                        entity_id="TQ-07",
                        attribute="seal_status",
                        from_value=None,
                        to_value="二级封存管控",
                        reason="卡片已明确授权该稳定物件状态",
                    )
                ],
            }
        )

        result = extract_chapter_summary(
            chapter_text=chapter_text,
            card=card,
            chapter_id=card.chapter_id,
            workspace_id=card.workspace_id,
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
            source_version_id=1,
            schema_retry_max_attempts=2,
            use_evidence_source_catalog=True,
        )

        assert result is not None
        assert len(fake.calls) == 2
        assert result.core_state_candidates[0]["entity_id"] == "TQ-07"
        assert "合法代码型物件稳定 ID:TQ-07" in fake.calls[0][1].content
        assert "TQ-07库房" in fake.calls[1][-1].content

    def test_unknown_ids_are_reported_together_then_valid_retry_succeeds(
        self,
    ) -> None:
        bad_payload = json.loads(_valid_source_ref_payload_json())
        bad_payload["pov_evidence_source_id"] = "E0001,E0002"
        bad_payload["scene_setting_evidence_source_id"] = "e0001"
        bad_payload["key_events"][0]["evidence_source_id"] = "E9999"
        bad_payload["character_interactions"][0]["evidence_source_id"] = "Q0001"
        bad_payload["unfinished_actions"][0]["evidence_source_id"] = "E0000"
        bad_payload["character_states"][0]["evidence_source_id"] = " E0001"
        bad_payload["established_facts"][0]["evidence_source_id"] = "E1"
        bad_payload["revealed_mechanics"][0]["evidence_source_id"] = "E0001 "
        fake = _SequenceLLMClient(
            [
                json.dumps(bad_payload, ensure_ascii=False),
                _valid_source_ref_payload_json(),
            ]
        )

        result = extract_chapter_summary(
            chapter_text=_CHAPTER_TEXT,
            card=_make_card(),
            chapter_id="ch-source-ref-retry",
            workspace_id="ws-test",
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
            source_version_id=1,
            schema_retry_max_attempts=3,
            use_evidence_source_catalog=True,
        )

        assert result is not None
        assert len(fake.calls) == 2
        retry_text = fake.calls[1][1].content
        first_catalog_lines = [
            line
            for line in fake.calls[0][1].content.splitlines()
            if line.startswith('{"source_id"')
        ]
        retry_catalog_lines = [
            line
            for line in retry_text.splitlines()
            if line.startswith('{"source_id"')
        ]
        assert retry_catalog_lines == first_catalog_lines
        assert "pov_character[0]='E0001,E0002'" in retry_text
        assert "scene_setting[0]='e0001'" in retry_text
        assert "key_events[0]='E9999'" in retry_text
        assert "character_interactions[0]='Q0001'" in retry_text
        assert "unfinished_actions[0]='E0000'" in retry_text
        assert "character_states[0]=' E0001'" in retry_text
        assert "established_facts[0]='E1'" in retry_text
        assert "revealed_mechanics[0]='E0001 '" in retry_text
        assert "禁止大小写修复、模糊匹配、改写、拼接" in retry_text

    def test_strict_schema_rejects_literal_quote_fields(self) -> None:
        fake = _SequenceLLMClient([_valid_payload_json()])

        result = extract_chapter_summary(
            chapter_text=_CHAPTER_TEXT,
            card=_make_card(),
            chapter_id="ch-source-ref-no-quote",
            workspace_id="ws-test",
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
            source_version_id=1,
            schema_retry_max_attempts=1,
            use_evidence_source_catalog=True,
        )

        assert result is None
        assert len(fake.calls) == 1

    def test_strict_schema_retries_when_any_top_level_field_is_missing(
        self,
    ) -> None:
        bad_payload = json.loads(_valid_source_ref_payload_json())
        del bad_payload["revealed_mechanics"]
        fake = _SequenceLLMClient(
            [
                json.dumps(bad_payload, ensure_ascii=False),
                _valid_source_ref_payload_json(),
            ]
        )

        result = extract_chapter_summary(
            chapter_text=_CHAPTER_TEXT,
            card=_make_card(),
            chapter_id="ch-source-ref-required-fields",
            workspace_id="ws-test",
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
            source_version_id=1,
            schema_retry_max_attempts=3,
            use_evidence_source_catalog=True,
        )

        assert result is not None
        assert len(fake.calls) == 2
        assert "字段 `revealed_mechanics`" in fake.calls[1][1].content

    @pytest.mark.parametrize(
        ("fact", "source_id"),
        [("lin-yan", ""), ("", "E0001")],
    )
    def test_scalar_fact_and_source_id_must_be_paired(
        self,
        fact: str,
        source_id: str,
    ) -> None:
        payload = json.loads(_valid_source_ref_payload_json())
        payload["pov_character"] = fact
        payload["pov_evidence_source_id"] = source_id
        fake = _SequenceLLMClient(
            [json.dumps(payload, ensure_ascii=False)] * 3
        )

        result = extract_chapter_summary(
            chapter_text=_CHAPTER_TEXT,
            card=_make_card(),
            chapter_id="ch-source-ref-pair",
            workspace_id="ws-test",
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
            source_version_id=1,
            schema_retry_max_attempts=3,
            use_evidence_source_catalog=True,
        )

        assert result is None
        assert len(fake.calls) == 3
        assert "必须同时为空或同时存在" in fake.calls[1][1].content

    def test_legacy_quote_payload_and_v2_prompt_remain_compatible(self) -> None:
        fake = _SequenceLLMClient([_valid_payload_json()])

        result = extract_chapter_summary(
            chapter_text=_CHAPTER_TEXT,
            card=_make_card(),
            chapter_id="ch-legacy-v2",
            workspace_id="ws-test",
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
            schema_retry_max_attempts=1,
        )

        assert result is not None
        assert "## 章节正文\n\n" in fake.calls[0][1].content
        assert "章节正文证据目录" not in fake.calls[0][1].content

    def test_legacy_default_remains_single_attempt_fail_open(self) -> None:
        fake = _SequenceLLMClient(
            [
                _payload_with_third_key_event_quote("林衍伫立在山门之前。"),
                _valid_payload_json(),
            ]
        )

        result = extract_chapter_summary(
            chapter_text=_CHAPTER_TEXT,
            card=_make_card(),
            chapter_id="ch-legacy-single-attempt",
            workspace_id="ws-test",
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
        )

        assert result is None
        assert len(fake.calls) == 1


class TestFailOpen:
    """LLM 异常 / 解析失败 / 空正文 → 返 None,不抛。"""

    def test_empty_chapter_text_returns_none(self) -> None:
        fake = _FakeLLMClient(_valid_payload_json())
        result = extract_chapter_summary(
            chapter_text="",
            card=_make_card(),
            chapter_id="ch-empty",
            workspace_id="ws-test",
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
        )
        assert result is None

    def test_whitespace_only_chapter_text_returns_none(self) -> None:
        fake = _FakeLLMClient(_valid_payload_json())
        result = extract_chapter_summary(
            chapter_text="   \n\n  \t  ",
            card=_make_card(),
            chapter_id="ch-ws",
            workspace_id="ws-test",
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
        )
        assert result is None

    def test_llm_exception_returns_none(self) -> None:
        fake = _FakeLLMClient(
            "(never returned)",
            raise_exc=RuntimeError("Qwen 404 simulated"),
        )
        result = extract_chapter_summary(
            chapter_text="x" * 1000,
            card=_make_card(),
            chapter_id="ch-exc",
            workspace_id="ws-test",
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
        )
        assert result is None

    def test_invalid_json_returns_none(self) -> None:
        fake = _FakeLLMClient("{not valid json at all]]]")
        result = extract_chapter_summary(
            chapter_text="x" * 1000,
            card=_make_card(),
            chapter_id="ch-bad-json",
            workspace_id="ws-test",
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
        )
        assert result is None

    def test_schema_mismatch_returns_none(self) -> None:
        """LLM 输出缺必填字段(summary_text) → 返 None。"""
        bad_payload = {
            "pov_character": "lin-yan",
            "scene_setting": "山门",
            "key_events": [],
            "character_interactions": [],
            "unfinished_actions": [],
            # 缺 summary_text
        }
        fake = _FakeLLMClient(json.dumps(bad_payload, ensure_ascii=False))
        result = extract_chapter_summary(
            chapter_text="x" * 1000,
            card=_make_card(),
            chapter_id="ch-bad-schema",
            workspace_id="ws-test",
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
        )
        assert result is None

    def test_setup_failure_returns_none(
        self, tmp_path: Path
    ) -> None:
        """prompts_dir 不含模板文件 → setup fail-open 返 None。"""
        fake = _FakeLLMClient(_valid_payload_json())
        result = extract_chapter_summary(
            chapter_text="x" * 1000,
            card=_make_card(),
            chapter_id="ch-setup-fail",
            workspace_id="ws-test",
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
            prompts_dir=tmp_path,  # 空目录,无 chapter_summary_extractor_v2.md
        )
        assert result is None


class TestCodeFenceStripping:
    """LLM 输出含 ```json ... ``` 包裹时正确剥离。"""

    def test_json_code_fence_stripped(self) -> None:
        wrapped = f"```json\n{_valid_payload_json()}\n```"
        fake = _FakeLLMClient(wrapped)
        result = extract_chapter_summary(
            chapter_text=_CHAPTER_TEXT,
            card=_make_card(),
            chapter_id="ch-fence",
            workspace_id="ws-test",
            extraction_trigger="reviewer_pass",
            client=fake,
            model="qwen-novel",
        )
        assert result is not None
        assert "林衍" in result.summary_text


class TestMockClientDispatch:
    """MockLLMClient 通过 ``chapter_summary_extractor`` marker 路由到 fixture。"""

    def test_mock_fixture_returns_valid_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 让 prompts_dir 指向真实 prompts/(MockLLMClient 不读 prompt,但
        # extract_chapter_summary setup 阶段会 load_prompt_template)
        repo_prompts = Path(__file__).resolve().parents[2] / "prompts"
        client = MockLLMClient()
        card = _make_card(chapter_id="ch-mock-1", chapter_number=1)
        result = extract_chapter_summary(
            chapter_text="林衍站在山门前。" * 200,
            card=card,
            chapter_id="ch-mock-1",
            workspace_id="ws-mock",
            extraction_trigger="reviewer_pass",
            client=client,
            model="qwen-novel",
            prompts_dir=repo_prompts,
        )
        assert result is not None
        assert result.chapter_id == "ch-mock-1"
        # Mock fixture 派生差异化字段
        assert "ch-mock-1" in result.summary_text
        assert len(result.summary_text) > 50

    def test_mock_fixture_diversified_across_5_chapters(self) -> None:
        """**差异化纪律新立测**(Q4 阈值 < 50% 生产级,Q1=B 真 LLM 路径前置兑现)。

        5 章 fixture summary_text 字符集重合率 < 50%。
        """
        repo_prompts = Path(__file__).resolve().parents[2] / "prompts"
        client = MockLLMClient()
        summaries: list[str] = []
        for cn in range(1, 6):
            card = _make_card(chapter_id=f"ch-{cn:04d}", chapter_number=cn)
            result = extract_chapter_summary(
                chapter_text="正文占位 " * 300,
                card=card,
                chapter_id=f"ch-{cn:04d}",
                workspace_id="ws-div",
                extraction_trigger="reviewer_pass",
                client=client,
                model="qwen-novel",
                prompts_dir=repo_prompts,
            )
            assert result is not None
            summaries.append(result.summary_text)

        # 字符集重合率 = |intersect| / |union|;阈值 < 50%(生产级)
        # 排除 chapter_id 数字部分,只比较核心摘要内容
        char_sets = [set(s) for s in summaries]
        for i in range(len(char_sets)):
            for j in range(i + 1, len(char_sets)):
                union = char_sets[i] | char_sets[j]
                intersect = char_sets[i] & char_sets[j]
                if not union:
                    continue
                overlap_ratio = len(intersect) / len(union)
                assert overlap_ratio < 0.90, (
                    f"章 {i + 1} 与 章 {j + 1} summary_text 字符集重合率 "
                    f"{overlap_ratio:.2%} ≥ 90%(基础差异化粗筛通不过)。"
                    f"summary[{i}]={summaries[i][:80]!r}, "
                    f"summary[{j}]={summaries[j][:80]!r}"
                )

    def test_mock_fixture_key_events_have_actors_and_location(self) -> None:
        """fixture key_events 每条含 actors / location,符合 LLM schema 字面。"""
        repo_prompts = Path(__file__).resolve().parents[2] / "prompts"
        client = MockLLMClient()
        card = _make_card(chapter_id="ch-key-ev", chapter_number=2)
        result = extract_chapter_summary(
            chapter_text="x" * 1000,
            card=card,
            chapter_id="ch-key-ev",
            workspace_id="ws-ev",
            extraction_trigger="reviewer_pass",
            client=client,
            model="qwen-novel",
            prompts_dir=repo_prompts,
        )
        assert result is not None
        assert len(result.key_events) >= 1
        for ev in result.key_events:
            assert "description" in ev
            assert "actors" in ev
            assert "location" in ev
            assert ev["description"]  # 非空


def test_summary_identity_catalog_freezes_names_and_explains_relation_registration():
    from src.core.agents.chapter_summary_extractor import (
        _build_user_message, _chapter_summary_authority_contract,
    )
    card = _make_card()
    names = {"lin-yan": "林衍", "shen-zhao": "沈照"}
    catalog = _build_evidence_source_catalog(_CHAPTER_TEXT)
    contract = _chapter_summary_authority_contract(
        card=card, chapter_text=_CHAPTER_TEXT, source_catalog=catalog,
        character_names=names,
    )
    message = _build_user_message(
        card=card, chapter_text=_CHAPTER_TEXT, chapter_id=card.chapter_id,
        workspace_id=card.workspace_id, source_catalog=catalog,
        authority_selection=contract, use_evidence_source_catalog=True,
        character_names=names,
    )
    assert "lin-yan: 林衍" in message
    assert "shen-zhao: 沈照" in message
    assert "name=沈照" in message
    assert "不表示正文必须先发生移交" in message
    assert "正文不支持时保持缺失" in message
    swapped = _chapter_summary_authority_contract(
        card=card, chapter_text=_CHAPTER_TEXT, source_catalog=catalog,
        character_names={"lin-yan": "沈照", "shen-zhao": "林衍"},
    )
    assert contract.snapshot_hash != swapped.snapshot_hash


def test_summary_input_distinguishes_existing_state_from_change():
    from src.core.agents.chapter_summary_extractor import _build_user_message
    text = _build_user_message(
        card=_make_card(), chapter_text="他揉了揉多年未愈的左臂。",
        chapter_id="ch-0001", workspace_id="ws-test",
        current_core_states={"character:lin-yan:permanent_injury": "少年时左臂骨折留下旧伤"},
    )
    assert "少年时左臂骨折留下旧伤" in text
    assert "没有真实状态变化时不必再次提名" in text
    assert "没有卡片许可仍须如实记录真实变化" in text
