"""``src/core/agents/memory_extractor.py`` 单测(P2-B-09)。

完全 mock ``LLMClient``,不真调 API。SQLite 用 ``tmp_path`` + ``Base.metadata.
create_all``(沿 P2-B-04 ``test_memory_store`` 模式);Chroma 用 ``VectorStore``
+ ``tmp_path / 'chroma'``(沿 P2-B-06 ``test_vector_store`` 模式)。

覆盖维度:

- 正常路径:LLM 返合规 JSON → SQLite 三类落库 + Chroma 四类落库 +
  ``ExtractionResult`` 字段对齐
- 空抽取产物:四类都返 ``[]`` → 不调 ``vector_store.add``
- 解析失败:非法 JSON / schema 缺字段 → ``MemoryExtractorError``,SQLite /
  Chroma 一字未写
- get-or-update-or-create 语义:同 ``character_id`` 二次抽取走 update,
  ``first_appearance_chapter`` 保留;新 ``character_id`` 走 create
- Chroma 字段对齐:``id = f"{entity_type}-{entity_id}"``、metadata 五字段、
  document 文本拼装规则
- ``fact`` 类型只入 Chroma 不入 SQLite
- LLM 入参:system + user 两条 + user 末尾含 extractor marker + 默认温度
  / max_tokens / 模型名
- 代码块包裹兜底(```json ... ```)
- 默认 prompts_dir 走 ``settings.prompts_dir``(可被参数覆盖)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from src.core.agents import _prompt as prompt_module
from src.core.agents.memory_extractor import (
    DEFAULT_COLLECTION,
    ExtractedSceneAnchor,
    ExtractionResult,
    FactEntry,
    MemoryExtractorError,
    extract,
)
from src.core.db import Base, PlotThreadORM, WorkspaceORM
from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage
from src.core.memory.store import (
    create_character,
    get_character,
    get_timeline_entry,
    list_characters_by_workspace,
    list_plot_threads_by_workspace,
    list_timeline_entries_by_workspace,
)
from src.core.memory.vector import VectorStore
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.character import (
    Character,
    CharacterStateSnapshot,
    VoiceProfile,
)

# ---- LLM 抽取产物 fixture ---------------------------------------------------

_FULL_PAYLOAD: dict[str, Any] = {
    "characters": [
        {
            "character_id": "lin-yan",
            "name": "林衍",
            "archetype": "萌新剑修",
            "appearance": "白衣青年,腰间一柄朴素长剑",
            "personality": "沉静内敛,关键时刻果决",
            "background": "出身寒门,刚入山门",
            "voice_profile": {
                "tone": "冷峻",
                "sentence_style": "短句直白",
                "self_address": "我",
                "addresses_others": {"shi-jie": "师姐"},
                "catchphrases": [],
                "forbidden_phrases": ["亲爱的"],
            },
            "goals_long_term": ["问鼎剑道"],
            "goals_short_term": ["通过试炼"],
            "aliases": [],
            "current_state": {"位置": "山门"},
            "relationships": {},
            "abilities": [],
            "secrets": [],
        }
    ],
    "plot_threads": [
        {
            "thread_id": "yu-pei-zhi-yue",
            "title": "玉佩之约",
            "status": "planted",
            "importance": 4,
            "planted_chapter": "ch-0001",
            "description": "师姐赠玉佩,约他日相见",
            "planned_payoff": None,
            "actual_payoff": None,
            "related_characters": ["lin-yan", "shi-jie"],
        }
    ],
    "timeline": [
        {
            "entry_id": "lin-yan-shang-shan-ch-0001",
            "description": "林衍上山门拜师",
            "time_label": "本章",
            "time_order": 100,
            "involved_characters": ["lin-yan"],
        }
    ],
    "facts": [
        {
            "fact_id": "yu-pei-shou-zeng-ch-0001",
            "description": "师姐在山门初遇林衍时,赠予他一枚刻有云纹的玉佩,约定他日凭佩相见",
            "involved_characters": ["lin-yan", "shi-jie"],
        }
    ],
}

_EMPTY_PAYLOAD: dict[str, Any] = {
    "characters": [],
    "plot_threads": [],
    "timeline": [],
    "facts": [],
}


# ---- ChapterCard / fake LLM client fixture ---------------------------------


def _make_continuity(**overrides: Any) -> ContinuityContext:
    payload: dict[str, Any] = dict(
        prev_chapter_tail="(本章为首章)",
        unfinished_actions=[],
        time_gap="立即续接",
        scene_continuation="same",
        pov_change=False,
        pov_from=None,
        pov_to="林衍",
    )
    payload.update(overrides)
    return ContinuityContext(**payload)


def _make_card(**overrides: Any) -> ChapterCard:
    payload: dict[str, Any] = dict(
        chapter_id="ch-0001",
        workspace_id="ws-demo",
        volume_id="v01",
        chapter_number=1,
        title="第一章·上山门",
        tension_level=4,
        word_count_min=2800,
        word_count_max=4500,
        plot_node_budget=3,
        must_achieve=["林衍上山门"],
        must_unresolved=["山门规矩之谜"],
        forbidden=["现代科技元素"],
        pov_character="林衍",
        setting="青崖山下",
        continuity_context=_make_continuity(),
        next_chapter_seed="师姐留下玉佩",
        arc_phase="opening",
        is_arc_finale=False,
        # 这条是章节卡上游已经规划的伏笔，MemoryExtractor 只更新它；正文中
        # 真正新长出的临时伏笔另有高门槛专项测试。
        related_plot_threads=["yu-pei-zhi-yue"],
        related_characters=[],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=3,
        max_revises=2,
    )
    payload.update(overrides)
    return ChapterCard(**payload)


def _make_response(
    *,
    content: str,
    model_used: str = "claude-sonnet-4-6-20250101",
) -> LLMResponse:
    return LLMResponse(
        content=content,
        model_used=model_used,
        usage=TokenUsage(input_tokens=100, output_tokens=200),
        finish_reason="end_turn",
    )


def _make_fake_llm(content: str) -> MagicMock:
    """构造伪 LLMClient——``complete()`` 返预设 ``LLMResponse(content=...)``。

    用 ``spec=LLMClient`` 让 mock 拒绝意外属性访问,提前发现错误。
    """
    fake = MagicMock(spec=LLMClient)
    fake.complete.return_value = _make_response(content=content)
    return fake


# ---- SQLite + Chroma fixtures ----------------------------------------------


def _seed_workspace(session: Session, workspace_id: str) -> None:
    now = datetime.now(timezone.utc)
    session.add(
        WorkspaceORM(
            workspace_id=workspace_id,
            display_name=f"工作区-{workspace_id}",
            genre="xianxia",
            premise="一句话",
            created_at=now,
            last_active_at=now,
            engine_version="0.1.0",
            status="active",
            total_chapters=0,
            completed_chapters=0,
            config_overrides={},
        )
    )
    session.commit()


@pytest.fixture
def session(tmp_path: Path) -> Session:
    """临时 SQLite + ``Base.metadata.create_all`` + seed workspace。"""
    db_path = tmp_path / "novel.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _seed_workspace(s, "ws-demo")
        yield s


def _mock_embedding_fn(texts: list[str]) -> list[list[float]]:
    """4 维兜底 embedding;P2-B-09 单测不验距离语义,只验落库。"""
    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


@pytest.fixture
def vector_store(tmp_path: Path) -> VectorStore:
    return VectorStore(
        "ws-demo",
        embedding_fn=_mock_embedding_fn,
        persist_dir=tmp_path / "chroma",
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    """临时目录里建 fake ``memory_extractor.md`` 让 system_text 可断言。"""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "memory_extractor.md").write_text(
        "FAKE_EXTRACTOR_SYSTEM_PROMPT\n用于单测断言。",
        encoding="utf-8",
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _clear_prompt_cache():
    """清 ``load_prompt_template`` lru_cache;与 P1-B-10 / P1-B-11 单测同款"""
    prompt_module.load_prompt_template.cache_clear()
    yield
    prompt_module.load_prompt_template.cache_clear()


# ---- 正常路径 ---------------------------------------------------------------


class TestExtractFullPayload:
    def test_returns_extraction_result(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        fake_llm = _make_fake_llm(json.dumps(_FULL_PAYLOAD))
        result = extract(
            content="林衍上山门……(略)",
            card=_make_card(),
            llm=fake_llm,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        assert isinstance(result, ExtractionResult)
        assert result.chapter_id == "ch-0001"
        assert result.workspace_id == "ws-demo"
        assert result.model_used == "claude-sonnet-4-6-20250101"
        assert result.count_by_type == {
            "character": 1,
            "plot_thread": 1,
            "timeline": 1,
            "fact": 1,
            "state_snapshot": 0,
        }

    def test_persists_three_types_to_sqlite(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        fake_llm = _make_fake_llm(json.dumps(_FULL_PAYLOAD))
        extract(
            content="略",
            card=_make_card(),
            llm=fake_llm,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()

        characters = list_characters_by_workspace(session, "ws-demo")
        assert len(characters) == 1
        assert characters[0].character_id == "lin-yan"
        assert characters[0].name == "林衍"
        assert characters[0].voice_profile.tone == "冷峻"
        assert characters[0].voice_profile.self_address == "我"
        assert characters[0].first_appearance_chapter == "ch-0001"
        assert characters[0].last_updated_chapter == "ch-0001"

        threads = list_plot_threads_by_workspace(session, "ws-demo")
        assert len(threads) == 1
        assert threads[0].thread_id == "yu-pei-zhi-yue"
        assert threads[0].status == "planted"
        assert threads[0].importance == 4
        assert threads[0].last_updated_chapter == "ch-0001"

        timeline = list_timeline_entries_by_workspace(session, "ws-demo")
        assert len(timeline) == 1
        assert timeline[0].entry_id == "lin-yan-shang-shan-ch-0001"
        # S3(v1.1 w4 b1):time_order 由 _persist_timeline 硬注入 = 全局章序*100 + 章内rank。
        # ch-0001(全局序1)单条 entry → 100 + 1 = 101(不再用 LLM payload 的 100)。
        assert timeline[0].time_order == 101
        assert timeline[0].chapter_id == "ch-0001"

    def test_facts_only_in_chroma(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """fact 不入 SQLite(P2 不建 facts 表),但出现在 ExtractionResult 与 Chroma。"""
        fake_llm = _make_fake_llm(json.dumps(_FULL_PAYLOAD))
        result = extract(
            content="略",
            card=_make_card(),
            llm=fake_llm,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()
        assert len(result.facts) == 1
        assert isinstance(result.facts[0], FactEntry)
        assert result.facts[0].fact_id == "yu-pei-shou-zeng-ch-0001"
        # Chroma 里 fact 条目存在(总条目 4 条:character + thread + timeline + fact)
        assert vector_store.count(DEFAULT_COLLECTION) == 4

    def test_chroma_metadata_fields(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """metadata 字段:P2-B-09 五字段 + G1(v1.1 wave 4 s1)加 chapter_seq 第六字段。

        chapter_seq = 从 chapter_id ``ch-0001`` 解析的全局序(=1),供 retriever
        ``before_chapter_seq`` 时序过滤用。"""
        fake_llm = _make_fake_llm(json.dumps(_FULL_PAYLOAD))
        extract(
            content="略",
            card=_make_card(),
            llm=fake_llm,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        # 用宽 query 拿全部 4 条命中(top_k=10 > 实际 4)
        hits = vector_store.query(
            DEFAULT_COLLECTION,
            query_text="任意文本",
            top_k=10,
        )
        assert len(hits) == 4
        for hit in hits:
            assert set(hit.metadata.keys()) == {
                "entity_type",
                "entity_id",
                "chapter_id",
                "workspace_id",
                "created_at",
                "chapter_seq",
            }
            assert hit.metadata["chapter_id"] == "ch-0001"
            assert hit.metadata["workspace_id"] == "ws-demo"
            assert hit.metadata["chapter_seq"] == 1
        # 四类 entity_type 都齐
        types_seen = {hit.metadata["entity_type"] for hit in hits}
        assert types_seen == {"character", "plot_thread", "timeline", "fact"}

    def test_chroma_id_convention(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """Chroma id 形态 ``f"{entity_type}-{entity_id}"``——retriever 之外约定。"""
        fake_llm = _make_fake_llm(json.dumps(_FULL_PAYLOAD))
        extract(
            content="略",
            card=_make_card(),
            llm=fake_llm,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        hits = vector_store.query(
            DEFAULT_COLLECTION, query_text="任意", top_k=10
        )
        ids = {hit.id for hit in hits}
        assert ids == {
            "character-lin-yan",
            "plot_thread-yu-pei-zhi-yue",
            "timeline-lin-yan-shang-shan-ch-0001",
            "fact-yu-pei-shou-zeng-ch-0001",
        }

    def test_character_doc_includes_name_and_personality(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """角色 document 拼装:让 retriever 能按"姓名 / 性格 / 背景"查询命中。"""
        fake_llm = _make_fake_llm(json.dumps(_FULL_PAYLOAD))
        extract(
            content="略",
            card=_make_card(),
            llm=fake_llm,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        hits = vector_store.query(
            DEFAULT_COLLECTION, query_text="任意", top_k=10
        )
        char_hit = next(h for h in hits if h.id == "character-lin-yan")
        assert "林衍" in char_hit.document
        assert "萌新剑修" in char_hit.document
        assert "沉静内敛" in char_hit.document


# ---- B2 双时空场景锚点抽取(v1.1 wave 6 session 3)-----------------------------


class TestExtractSceneAnchor:
    def test_scene_anchor_parsed_and_carried(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """LLM 输出含 scene_anchor → 解析并透传到 ExtractionResult。"""
        payload = dict(_EMPTY_PAYLOAD)
        payload["scene_anchor"] = {
            "enter_dream_location": "市医院病房病床",
            "wake_location": "",
            "has_transition_explanation": False,
        }
        result = extract(
            content="主角握信物入梦……",
            card=_make_card(),
            llm=_make_fake_llm(json.dumps(payload)),
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        assert isinstance(result.scene_anchor, ExtractedSceneAnchor)
        assert result.scene_anchor.enter_dream_location == "市医院病房病床"
        assert result.scene_anchor.wake_location == ""
        assert result.scene_anchor.is_empty is False

    def test_missing_scene_anchor_is_none(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """旧 LLM 输出无 scene_anchor 字段 → ExtractionResult.scene_anchor=None(向后兼容)。"""
        result = extract(
            content="普通过场章……",
            card=_make_card(),
            llm=_make_fake_llm(json.dumps(_EMPTY_PAYLOAD)),
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        assert result.scene_anchor is None


# ---- 空抽取产物 -------------------------------------------------------------


class TestExtractEmptyPayload:
    def test_empty_payload_no_chroma_writes(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """LLM 返四类全空 → SQLite 不写、Chroma 不写;ExtractionResult 字段全空。"""
        fake_llm = _make_fake_llm(json.dumps(_EMPTY_PAYLOAD))
        result = extract(
            content="纯心理章……",
            card=_make_card(),
            llm=fake_llm,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()
        assert result.count_by_type == {
            "character": 0,
            "plot_thread": 0,
            "timeline": 0,
            "fact": 0,
            "state_snapshot": 0,
        }
        assert list_characters_by_workspace(session, "ws-demo") == []
        assert list_plot_threads_by_workspace(session, "ws-demo") == []
        assert list_timeline_entries_by_workspace(session, "ws-demo") == []
        assert vector_store.count(DEFAULT_COLLECTION) == 0


# ---- 解析失败 ---------------------------------------------------------------


class TestParseFailures:
    def test_invalid_json_raises_memory_extractor_error(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        fake_llm = _make_fake_llm("这不是 JSON,只是一段散文")
        with pytest.raises(MemoryExtractorError) as exc_info:
            extract(
                content="略",
                card=_make_card(),
                llm=fake_llm,
                workspace_id="ws-demo",
                db_session=session,
                vector_store=vector_store,
                prompts_dir=fake_prompts_dir,
            )
        assert exc_info.value.raw_output == "这不是 JSON,只是一段散文"
        assert exc_info.value.model_used == "claude-sonnet-4-6-20250101"
        # SQLite / Chroma 一字未写
        assert list_characters_by_workspace(session, "ws-demo") == []
        assert vector_store.count(DEFAULT_COLLECTION) == 0

    def test_schema_mismatch_raises(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """缺 ``facts`` 字段 → 校验错;SQLite / Chroma 一字未写。"""
        broken_payload = {
            "characters": [],
            "plot_threads": [],
            "timeline": [],
            # facts 缺失
        }
        fake_llm = _make_fake_llm(json.dumps(broken_payload))
        with pytest.raises(MemoryExtractorError):
            extract(
                content="略",
                card=_make_card(),
                llm=fake_llm,
                workspace_id="ws-demo",
                db_session=session,
                vector_store=vector_store,
                prompts_dir=fake_prompts_dir,
            )
        assert list_characters_by_workspace(session, "ws-demo") == []
        assert vector_store.count(DEFAULT_COLLECTION) == 0

    def test_voice_profile_missing_field_raises(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """``voice_profile`` 缺 ``self_address`` 必填字段 → 解析阶段就抛错,
        不延迟到 SQLite 写入。"""
        broken = {
            "characters": [
                {
                    "character_id": "lin-yan",
                    "name": "林衍",
                    "archetype": "萌新",
                    "appearance": "略",
                    "personality": "略",
                    "background": "略",
                    "voice_profile": {
                        "tone": "冷峻",
                        "sentence_style": "短句",
                        # self_address 缺失
                    },
                }
            ],
            "plot_threads": [],
            "timeline": [],
            "facts": [],
        }
        fake_llm = _make_fake_llm(json.dumps(broken))
        with pytest.raises(MemoryExtractorError):
            extract(
                content="略",
                card=_make_card(),
                llm=fake_llm,
                workspace_id="ws-demo",
                db_session=session,
                vector_store=vector_store,
                prompts_dir=fake_prompts_dir,
            )

    def test_strip_code_fence_json(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """LLM 偶发把 JSON 套进 ```json ... ``` 包裹(即使 prompt 禁止)→ 兜底剥离。"""
        fenced = "```json\n" + json.dumps(_EMPTY_PAYLOAD) + "\n```"
        fake_llm = _make_fake_llm(fenced)
        result = extract(
            content="略",
            card=_make_card(),
            llm=fake_llm,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        assert result.count_by_type == {
            "character": 0,
            "plot_thread": 0,
            "timeline": 0,
            "fact": 0,
            "state_snapshot": 0,
        }


# ---- get-or-update-or-create 语义 -----------------------------------------


def _emergent_thread_payload(
    *,
    thread_id: str,
    evidence_quote: str,
    importance: int = 4,
    expiry_policy: str = "volume_end",
) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "title": f"临时悬念-{thread_id}",
        "status": "planted",
        "importance": importance,
        "planted_chapter": "ch-0001",
        "description": "正文明确留下需要跨章回答的承诺，读者会等待后续给出结果。",
        "planned_payoff": None,
        "actual_payoff": None,
        "related_characters": ["lin-yan"],
        "evidence_quote": evidence_quote,
        "emergent_kind": "explicit_promise",
        "expiry_policy": expiry_policy,
    }


class TestEmergentPlotThreadAdmission:
    def test_low_threshold_new_thread_is_dropped_without_losing_facts(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        payload = dict(_EMPTY_PAYLOAD)
        payload["plot_threads"] = [
            {
                "thread_id": "mere-atmosphere",
                "title": "雨夜气氛",
                "status": "planted",
                "importance": 5,
                "planted_chapter": "ch-0001",
                "description": "窗外下雨",
                "planned_payoff": None,
                "actual_payoff": None,
                "related_characters": [],
            }
        ]
        payload["facts"] = [
            {
                "fact_id": "rain-ch-0001",
                "description": "本章窗外持续下雨",
                "involved_characters": [],
            }
        ]
        result = extract(
            content="窗外下雨，屋檐滴水，只是普通环境描写。",
            card=_make_card(related_plot_threads=[]),
            llm=_make_fake_llm(json.dumps(payload)),
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        assert result.plot_threads == []
        assert len(result.facts) == 1
        assert list_plot_threads_by_workspace(session, "ws-demo") == []

    def test_high_threshold_new_thread_persists_internal_metadata(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        evidence = "师姐把黑匣递给林衍：三日后若我没回来，你必须打开它。"
        payload = dict(_EMPTY_PAYLOAD)
        payload["plot_threads"] = [
            _emergent_thread_payload(
                thread_id="black-box-promise",
                evidence_quote=evidence,
            )
        ]
        result = extract(
            content=f"夜色沉下来。{evidence}林衍没有追问。",
            card=_make_card(related_plot_threads=[]),
            llm=_make_fake_llm(json.dumps(payload)),
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        assert [thread.thread_id for thread in result.plot_threads] == [
            "black-box-promise"
        ]
        row = session.get(PlotThreadORM, "black-box-promise")
        assert row is not None
        assert row.origin == "emergent"
        assert row.expiry_policy == "volume_end"
        assert row.source_evidence == evidence

    def test_existing_outline_thread_bypasses_emergent_gate_and_keeps_origin(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        now = datetime.now(UTC)
        session.add(
            PlotThreadORM(
                thread_id="planned-main",
                workspace_id="ws-demo",
                title="大纲主伏笔",
                status="planted",
                importance=5,
                planted_chapter=None,
                planned_payoff="ch-0012",
                actual_payoff=None,
                description="大纲阶段已安排的主伏笔",
                origin="outline",
                expiry_policy="never",
                source_evidence=None,
                related_characters=[],
                created_at=now,
                last_updated_chapter=None,
            )
        )
        session.commit()
        payload = dict(_EMPTY_PAYLOAD)
        payload["plot_threads"] = [
            {
                "thread_id": "planned-main",
                "title": "大纲主伏笔",
                "status": "developing",
                "importance": 5,
                "planted_chapter": "ch-0001",
                "description": "本章让大纲主伏笔继续推进，但无需临时准入字段。",
                "planned_payoff": None,
                "actual_payoff": None,
                "related_characters": ["lin-yan"],
                # 正式 Provider 会按“既有伏笔无需过期策略”输出 JSON null；
                # 这与字段省略语义相同，且不得覆盖 DB 的 outline/never。
                "expiry_policy": None,
            }
        ]
        extract(
            content="本章按大纲继续推进主伏笔。",
            card=_make_card(related_plot_threads=["planned-main"]),
            llm=_make_fake_llm(json.dumps(payload)),
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        row = session.get(PlotThreadORM, "planned-main")
        assert row is not None
        assert row.status == "developing"
        assert row.origin == "outline"
        assert row.expiry_policy == "never"
        assert row.planned_payoff == "ch-0012"

    def test_new_emergent_thread_with_null_expiry_is_dropped_not_parse_failed(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """null 对既有更新合法；新临时伏笔仍须通过非空 expiry 硬门。"""

        evidence = "他把名单锁进抽屉：三天后必须公开最后一个名字。"
        candidate = _emergent_thread_payload(
            thread_id="null-expiry-promise",
            evidence_quote=evidence,
        )
        candidate["expiry_policy"] = None
        payload = dict(_EMPTY_PAYLOAD)
        payload["plot_threads"] = [candidate]

        result = extract(
            content=evidence,
            card=_make_card(related_plot_threads=[]),
            llm=_make_fake_llm(json.dumps(payload)),
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )

        assert result.plot_threads == []
        assert list_plot_threads_by_workspace(session, "ws-demo") == []

    def test_at_most_one_new_emergent_thread_per_chapter(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        evidence_a = "老人指着北门说：钟响第三次时，井底的人会回来索命。"
        evidence_b = "林衍收起血书：若印记再亮一次，他就必须去废塔赴约。"
        payload = dict(_EMPTY_PAYLOAD)
        payload["plot_threads"] = [
            _emergent_thread_payload(
                thread_id="well-bell",
                evidence_quote=evidence_a,
                importance=4,
            ),
            _emergent_thread_payload(
                thread_id="tower-oath",
                evidence_quote=evidence_b,
                importance=5,
                expiry_policy="never",
            ),
        ]
        result = extract(
            content=f"{evidence_a}\n{evidence_b}",
            card=_make_card(related_plot_threads=[]),
            llm=_make_fake_llm(json.dumps(payload)),
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        assert [thread.thread_id for thread in result.plot_threads] == [
            "tower-oath"
        ]

    def test_selected_emergent_thread_can_resolve_by_exact_existing_id(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        evidence = "他留下密信：若三日未归，就把真相告诉所有人。"
        session.add(
            PlotThreadORM(
                thread_id="temp-letter",
                workspace_id="ws-demo",
                title="临时密信",
                status="planted",
                importance=4,
                planted_chapter="ch-0001",
                planned_payoff=None,
                actual_payoff=None,
                description="三日未归后必须打开密信并公布真相。",
                origin="emergent",
                expiry_policy="volume_end",
                source_evidence=evidence,
                related_characters=["lin-yan"],
                created_at=datetime.now(UTC),
                last_updated_chapter="ch-0001",
            )
        )
        session.commit()
        payload = dict(_EMPTY_PAYLOAD)
        payload["plot_threads"] = [
            {
                "thread_id": "temp-letter",
                "title": "临时密信",
                "status": "resolved",
                "importance": 4,
                "planted_chapter": "ch-0001",
                "description": "林衍已拆开密信并向众人公布真相。",
                "planned_payoff": None,
                "actual_payoff": "ch-0002",
                "related_characters": ["lin-yan"],
            }
        ]
        extract(
            content="林衍当众拆开密信，念出失踪者留下的证词，众人终于知道真相。",
            card=_make_card(
                chapter_id="ch-0002",
                related_plot_threads=["temp-letter"],
                must_achieve=[
                    "[临时伏笔收口:temp-letter]林衍拆开密信并向众人公布真相"
                ],
            ),
            llm=_make_fake_llm(json.dumps(payload)),
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        row = session.get(PlotThreadORM, "temp-letter")
        assert row is not None
        assert row.status == "resolved"
        assert row.actual_payoff == "ch-0002"
        assert row.origin == "emergent"
        assert row.source_evidence == evidence


class TestUpsertSemantics:
    def test_existing_character_updates_preserves_first_appearance(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """SQLite 已存在角色 → update 走 first_appearance_chapter 保留路径。"""
        # 先种入已存在角色,first_appearance="ch-0000"
        original = Character(
            character_id="lin-yan",
            workspace_id="ws-demo",
            name="林衍",
            archetype="新人",
            appearance="略",
            personality="略",
            background="略",
            voice_profile=VoiceProfile(
                tone="平和", sentence_style="平", self_address="我",
                addresses_others={}, catchphrases=[], forbidden_phrases=[]
            ),
            goals_long_term=[], goals_short_term=[],
            first_appearance_chapter="ch-0000",
            last_updated_chapter="ch-0000",
        )
        create_character(session, original)
        session.commit()

        # 用 LLM 抽取相同 character_id 的"新"属性
        fake_llm = _make_fake_llm(json.dumps(_FULL_PAYLOAD))
        extract(
            content="略",
            card=_make_card(chapter_id="ch-0001"),
            llm=fake_llm,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()

        loaded = get_character(session, "lin-yan")
        assert loaded is not None
        # 新属性已应用
        assert loaded.archetype == "萌新剑修"
        assert loaded.voice_profile.tone == "冷峻"
        # first_appearance 保留(不被本次 ch-0001 覆盖)
        assert loaded.first_appearance_chapter == "ch-0000"
        # last_updated 已推进
        assert loaded.last_updated_chapter == "ch-0001"

    def test_chroma_id_upsert_avoids_duplicates(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """同 character_id 二次抽取 → Chroma id 同 → upsert 替换,不重复。"""
        fake_llm_1 = _make_fake_llm(json.dumps(_FULL_PAYLOAD))
        extract(
            content="第一次抽取",
            card=_make_card(chapter_id="ch-0001"),
            llm=fake_llm_1,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()

        fake_llm_2 = _make_fake_llm(json.dumps(_FULL_PAYLOAD))
        extract(
            content="第二次抽取(同 ID)",
            card=_make_card(chapter_id="ch-0001"),
            llm=fake_llm_2,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()

        # 仍是 4 条(不重复)
        assert vector_store.count(DEFAULT_COLLECTION) == 4

    def test_existing_plot_thread_updates(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """plot_thread 同 ID 二次抽取 → 走 update,字段已被 LLM 抽取产物覆盖。"""
        fake_llm_1 = _make_fake_llm(json.dumps(_FULL_PAYLOAD))
        extract(
            content="第一次",
            card=_make_card(chapter_id="ch-0001"),
            llm=fake_llm_1,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()

        # 第二次:同 thread_id,LLM 给 status=resolved
        payload2 = json.loads(json.dumps(_FULL_PAYLOAD))
        payload2["plot_threads"][0]["status"] = "resolved"
        payload2["plot_threads"][0]["actual_payoff"] = "ch-0050"
        fake_llm_2 = _make_fake_llm(json.dumps(payload2))
        extract(
            content="第二次",
            card=_make_card(chapter_id="ch-0050"),
            llm=fake_llm_2,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()

        threads = list_plot_threads_by_workspace(session, "ws-demo")
        assert len(threads) == 1  # 同 ID 没新增
        assert threads[0].status == "resolved"
        assert threads[0].actual_payoff == "ch-0050"
        assert threads[0].last_updated_chapter == "ch-0050"

    def test_existing_timeline_entry_updates(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        fake_llm_1 = _make_fake_llm(json.dumps(_FULL_PAYLOAD))
        extract(
            content="第一次",
            card=_make_card(chapter_id="ch-0001"),
            llm=fake_llm_1,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()

        # 改 description / time_order
        payload2 = json.loads(json.dumps(_FULL_PAYLOAD))
        payload2["timeline"][0]["description"] = "林衍上山门拜师(改写)"
        payload2["timeline"][0]["time_order"] = 200
        fake_llm_2 = _make_fake_llm(json.dumps(payload2))
        extract(
            content="第二次",
            card=_make_card(chapter_id="ch-0001"),
            llm=fake_llm_2,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()

        loaded = get_timeline_entry(session, "lin-yan-shang-shan-ch-0001")
        assert loaded is not None
        # update 语义由 description 覆盖验证(第二次落库更新同 entry_id)。
        assert loaded.description == "林衍上山门拜师(改写)"
        # S3:time_order 由 _persist_timeline 硬注入(全局章序*100+rank),不再反映
        # payload 的 200;ch-0001 单条 → 100 + 1 = 101。
        assert loaded.time_order == 101

    def test_new_character_is_created(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """新 character_id(SQLite 不存在)→ create 路径 + first_appearance 填本章。"""
        fake_llm = _make_fake_llm(json.dumps(_FULL_PAYLOAD))
        # 抽取前 SQLite 无该角色
        assert get_character(session, "lin-yan") is None

        extract(
            content="略",
            card=_make_card(chapter_id="ch-0007"),
            llm=fake_llm,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()

        loaded = get_character(session, "lin-yan")
        assert loaded is not None
        assert loaded.first_appearance_chapter == "ch-0007"
        assert loaded.last_updated_chapter == "ch-0007"


# ---- LLM 入参契约 -----------------------------------------------------------


class TestLLMRequestContract:
    def test_messages_have_system_and_user(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        fake_llm = _make_fake_llm(json.dumps(_EMPTY_PAYLOAD))
        extract(
            content="正文略",
            card=_make_card(),
            llm=fake_llm,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        # 取最近一次 complete 的 messages 入参
        kwargs = fake_llm.complete.call_args.kwargs
        messages: list[Message] = kwargs["messages"]
        assert len(messages) == 2
        assert messages[0].role == "system"
        assert "FAKE_EXTRACTOR_SYSTEM_PROMPT" in messages[0].content
        assert messages[1].role == "user"

    def test_user_message_contains_card_context_and_content(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        fake_llm = _make_fake_llm(json.dumps(_EMPTY_PAYLOAD))
        extract(
            content="林衍上山门……(正文)",
            card=_make_card(
                chapter_id="ch-0001",
                related_characters=["lin-yan", "shi-jie"],
            ),
            llm=fake_llm,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        user_text = fake_llm.complete.call_args.kwargs["messages"][1].content
        assert "ch-0001" in user_text
        assert "ws-demo" in user_text
        assert "林衍" in user_text
        assert "lin-yan" in user_text
        assert "shi-jie" in user_text
        assert "林衍上山门……(正文)" in user_text

    def test_user_message_ends_with_extractor_marker(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        fake_llm = _make_fake_llm(json.dumps(_EMPTY_PAYLOAD))
        extract(
            content="略",
            card=_make_card(),
            llm=fake_llm,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        user_text = fake_llm.complete.call_args.kwargs["messages"][1].content
        assert user_text.rstrip().endswith("<!-- agent_role: extractor -->")

    def test_default_temperature_and_max_tokens(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        fake_llm = _make_fake_llm(json.dumps(_EMPTY_PAYLOAD))
        extract(
            content="略",
            card=_make_card(),
            llm=fake_llm,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        kwargs = fake_llm.complete.call_args.kwargs
        # 抽取求稳:温度 0.2,与 reviewer 0.3 同档低温度区间
        assert kwargs["temperature"] == 0.2
        assert kwargs["max_tokens"] == 4000
        assert kwargs["model"] == "claude-sonnet-4-6"

    def test_custom_model_passed_through(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        fake_llm = _make_fake_llm(json.dumps(_EMPTY_PAYLOAD))
        extract(
            content="略",
            card=_make_card(),
            llm=fake_llm,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
            model="claude-opus-4-7",
            temperature=0.5,
            max_tokens=8000,
        )
        kwargs = fake_llm.complete.call_args.kwargs
        assert kwargs["model"] == "claude-opus-4-7"
        assert kwargs["temperature"] == 0.5
        assert kwargs["max_tokens"] == 8000


# ---- 边界 -------------------------------------------------------------------


class TestEdgeCases:
    def test_card_with_no_related_characters_renders_placeholder(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """``related_characters=[]`` 时 user message 有占位文本而不是空白行。"""
        fake_llm = _make_fake_llm(json.dumps(_EMPTY_PAYLOAD))
        extract(
            content="略",
            card=_make_card(related_characters=[], related_plot_threads=[]),
            llm=fake_llm,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        user_text = fake_llm.complete.call_args.kwargs["messages"][1].content
        assert "(无,本章可能新登场角色)" in user_text
        assert "(无)" in user_text

    def test_only_facts_payload(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """LLM 仅抽 facts(无角色 / 无伏笔 / 无时间线)→ Chroma 只 1 条。"""
        only_facts = {
            "characters": [],
            "plot_threads": [],
            "timeline": [],
            "facts": [
                {
                    "fact_id": "single-fact",
                    "description": "只有一条 fact 的章节",
                    "involved_characters": [],
                }
            ],
        }
        fake_llm = _make_fake_llm(json.dumps(only_facts))
        extract(
            content="略",
            card=_make_card(),
            llm=fake_llm,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()
        assert vector_store.count(DEFAULT_COLLECTION) == 1
        assert list_characters_by_workspace(session, "ws-demo") == []

    def test_default_prompts_dir_uses_settings(
        self,
        session: Session,
        vector_store: VectorStore,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """``prompts_dir=None`` 时取 ``settings.prompts_dir``,与 P1 writer / reviewer 一致。"""
        # 临时改 settings.prompts_dir → 临时目录(避免依赖仓库根真模板)
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "memory_extractor.md").write_text(
            "DEFAULT_PROMPTS_DIR_TEMPLATE", encoding="utf-8"
        )
        from src.config import settings as settings_module
        monkeypatch.setattr(settings_module, "prompts_dir", prompts_dir)

        fake_llm = _make_fake_llm(json.dumps(_EMPTY_PAYLOAD))
        extract(
            content="略",
            card=_make_card(),
            llm=fake_llm,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            # 不传 prompts_dir,走 settings.prompts_dir
        )
        system_text = fake_llm.complete.call_args.kwargs["messages"][0].content
        assert "DEFAULT_PROMPTS_DIR_TEMPLATE" in system_text


# ---- DEFAULT_COLLECTION 常量与 retriever 对齐 ---------------------------


def test_default_collection_aligns_with_retriever() -> None:
    """``MemoryExtractor.DEFAULT_COLLECTION`` 必须与 retriever 同源,否则 Writer
    检索拿不到 Extractor 写入的记忆。"""
    from src.core.memory.retriever import (
        DEFAULT_COLLECTION as RETRIEVER_DEFAULT_COLLECTION,
    )
    assert DEFAULT_COLLECTION == RETRIEVER_DEFAULT_COLLECTION


def test_module_keeps_utc_iso_for_created_at(
    session: Session,
    vector_store: VectorStore,
    fake_prompts_dir: Path,
) -> None:
    """``metadata['created_at']`` 必须能 ``fromisoformat`` 解回 tz-aware UTC,
    便于 Writer / 调用方按时间窗过滤。"""
    fake_llm = _make_fake_llm(json.dumps(_FULL_PAYLOAD))
    extract(
        content="略",
        card=_make_card(),
        llm=fake_llm,
        workspace_id="ws-demo",
        db_session=session,
        vector_store=vector_store,
        prompts_dir=fake_prompts_dir,
    )
    hits = vector_store.query(DEFAULT_COLLECTION, query_text="略", top_k=10)
    iso_str = hits[0].metadata["created_at"]
    parsed = datetime.fromisoformat(iso_str)
    assert parsed.tzinfo is not None
    assert parsed.tzinfo.utcoffset(parsed) == UTC.utcoffset(parsed)


# ---- P3-B-14:workspace_id 参数 vs card.workspace_id 漂移防护 -----------


class TestWorkspaceIdParamOverridesCardField:
    """P3-acceptance 观察 1 修:`workspace_id` 入参显式注入,**不再**读
    ``card.workspace_id``——卡片是模板化资产,跨工作区复制时其
    ``workspace_id`` 字段可能与"操作工作区"漂移(P3-acceptance 实测
    fixture 卡片字面 ``workspace_id="p2e2e-demo"``,但 p3e2e-demo 工作区下
    复用时会让 ``memory_extracted`` 事件 / ORM 行 / Chroma metadata 全部串号
    成 p2e2e-demo)。

    本测试套构造**漂移场景**:``card.workspace_id="ws-stale-card"``(模拟
    历史 fixture 字段)+ ``workspace_id="ws-actual"``(操作上下文)→ 断言
    四处落位全部用 ws-actual:
    1. ``memory_extracted`` structlog 事件
    2. ``ExtractionResult.workspace_id`` 返回字段
    3. SQLite ORM 行(Character / PlotThread / TimelineEntry).workspace_id
    4. Chroma metadata.workspace_id
    """

    def _seed_two_workspaces(self, session: Session) -> None:
        # P2-B-19 起 ORM 引入 WorkspaceORM 外键,_persist_characters 写
        # Character.workspace_id 时需要工作区行存在(否则 FK 违反)。
        # 复用模块内 _seed_workspace 助手(已对齐 ORM 必填字段);
        # session fixture 默认 seed 了 "ws-demo",这里再 seed 两个漂移测试用。
        _seed_workspace(session, "ws-stale-card")
        _seed_workspace(session, "ws-actual")

    def test_event_field_uses_param_workspace_id_not_card_field(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """``memory_extracted`` event 的 ``workspace_id`` = 参数 ws-actual,
        即使卡片字段是 ws-stale-card。"""
        self._seed_two_workspaces(session)
        fake_llm = _make_fake_llm(json.dumps(_FULL_PAYLOAD))
        with capture_logs() as logs:
            extract(
                content="略",
                card=_make_card(workspace_id="ws-stale-card"),
                llm=fake_llm,
                workspace_id="ws-actual",
                db_session=session,
                vector_store=vector_store,
                prompts_dir=fake_prompts_dir,
            )
        evts = [e for e in logs if e.get("event") == "memory_extracted"]
        assert len(evts) == 1
        assert evts[0]["workspace_id"] == "ws-actual"

    def test_extraction_result_workspace_id_uses_param(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """``ExtractionResult.workspace_id`` = 参数 ws-actual。"""
        self._seed_two_workspaces(session)
        fake_llm = _make_fake_llm(json.dumps(_FULL_PAYLOAD))
        result = extract(
            content="略",
            card=_make_card(workspace_id="ws-stale-card"),
            llm=fake_llm,
            workspace_id="ws-actual",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        assert result.workspace_id == "ws-actual"

    def test_sqlite_orm_rows_use_param_workspace_id(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """Character / PlotThread / TimelineEntry ORM 行 ``workspace_id``
        字段 = 参数 ws-actual,**不**串号到 ws-stale-card。"""
        self._seed_two_workspaces(session)
        fake_llm = _make_fake_llm(json.dumps(_FULL_PAYLOAD))
        extract(
            content="略",
            card=_make_card(workspace_id="ws-stale-card"),
            llm=fake_llm,
            workspace_id="ws-actual",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()
        # ws-actual 拿到 1 character + 1 plot_thread + 1 timeline
        assert len(list_characters_by_workspace(session, "ws-actual")) == 1
        assert len(list_plot_threads_by_workspace(session, "ws-actual")) == 1
        assert len(list_timeline_entries_by_workspace(session, "ws-actual")) == 1
        # ws-stale-card 一字未写
        assert list_characters_by_workspace(session, "ws-stale-card") == []
        assert list_plot_threads_by_workspace(session, "ws-stale-card") == []
        assert list_timeline_entries_by_workspace(session, "ws-stale-card") == []

    def test_chroma_metadata_uses_param_workspace_id(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """Chroma metadata.workspace_id 五字段中 = 参数 ws-actual。"""
        self._seed_two_workspaces(session)
        fake_llm = _make_fake_llm(json.dumps(_FULL_PAYLOAD))
        extract(
            content="略",
            card=_make_card(workspace_id="ws-stale-card"),
            llm=fake_llm,
            workspace_id="ws-actual",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        hits = vector_store.query(DEFAULT_COLLECTION, query_text="略", top_k=10)
        assert len(hits) >= 1
        # 全部命中 metadata.workspace_id == "ws-actual"
        for hit in hits:
            assert hit.metadata["workspace_id"] == "ws-actual"

    def test_user_prompt_workspace_id_block_uses_param(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """user message 的"工作区 ID(workspace_id):"段也用参数 ws-actual,
        让 LLM 抽取上下文反映真实工作区。"""
        self._seed_two_workspaces(session)
        fake_llm = _make_fake_llm(json.dumps(_FULL_PAYLOAD))
        extract(
            content="略",
            card=_make_card(workspace_id="ws-stale-card"),
            llm=fake_llm,
            workspace_id="ws-actual",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        user_text = fake_llm.complete.call_args.kwargs["messages"][1].content
        assert "工作区 ID(workspace_id):ws-actual" in user_text
        assert "ws-stale-card" not in user_text


# ===========================================================================
# G1 角色时序状态追踪(v1.1 wave 4 session 1)
# ===========================================================================


def _state_payload(states: list[dict]) -> dict:
    """_FULL_PAYLOAD(含 lin-yan 角色)+ 指定 character_states。"""
    payload = dict(_FULL_PAYLOAD)
    payload["character_states"] = states
    return payload


def _seed_character_with_state(
    session: Session,
    *,
    character_id: str = "lin-yan",
    timeline: list[CharacterStateSnapshot] | None = None,
) -> None:
    create_character(
        session,
        Character(
            character_id=character_id,
            workspace_id="ws-demo",
            name="林衍",
            archetype="剑修",
            appearance="白衣",
            personality="沉静",
            background="寒门",
            voice_profile=VoiceProfile(
                tone="冷峻", sentence_style="短句", self_address="我"
            ),
            goals_long_term=["剑道"],
            goals_short_term=["试炼"],
            state_timeline=timeline or [],
        ),
    )
    session.commit()


class TestG1CharacterStates:
    """extract 抽 character_states → 落 state_timeline 端到端。"""

    def test_character_states_persisted_to_timeline(
        self, session: Session, vector_store: VectorStore, fake_prompts_dir: Path
    ) -> None:
        """character_states 折叠落角色 state_timeline,as_of_chapter = 全局序。"""
        payload = _state_payload(
            [
                {
                    "character_id": "lin-yan",
                    "physical_state": "右臂截断",
                    "injuries": ["右臂截肢"],
                    "acquired_items": ["断魂刀"],
                    "current_relationships": {"shi-jie": "生死之交"},
                    "state_changes": [
                        {"event_type": "injury", "description": "右臂被斩断"}
                    ],
                }
            ]
        )
        result = extract(
            content="林衍断臂",
            card=_make_card(chapter_id="ch-0010", related_characters=["lin-yan"]),
            llm=_make_fake_llm(json.dumps(payload)),
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()

        character = get_character(session, "lin-yan")
        assert character is not None
        assert len(character.state_timeline) == 1
        snap = character.state_timeline[0]
        # chapter_id ch-0010 → 全局序 10
        assert snap.as_of_chapter == 10
        assert snap.injuries == ["右臂截肢"]
        assert snap.acquired_items == ["断魂刀"]
        assert snap.state_changes[0].event_type == "injury"
        # ExtractionResult 观测字段
        assert len(result.state_snapshots) == 1
        assert result.count_by_type["state_snapshot"] == 1

    def test_prior_state_injected_into_prompt(
        self, session: Session, vector_store: VectorStore, fake_prompts_dir: Path
    ) -> None:
        """出场角色有历史状态时,extract 把'截止上一章'状态注入 LLM user message。"""
        _seed_character_with_state(
            session,
            timeline=[
                CharacterStateSnapshot(
                    as_of_chapter=9,
                    physical_state="右臂截断",
                    injuries=["右臂截肢"],
                )
            ],
        )
        fake_llm = _make_fake_llm(json.dumps(_state_payload([])))
        extract(
            content="第十章正文",
            card=_make_card(chapter_id="ch-0010", related_characters=["lin-yan"]),
            llm=fake_llm,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        user_text = fake_llm.complete.call_args.kwargs["messages"][1].content
        assert "角色当前状态(截止上一章" in user_text
        assert "右臂截肢" in user_text

    def test_no_prior_state_block_when_first_chapter(
        self, session: Session, vector_store: VectorStore, fake_prompts_dir: Path
    ) -> None:
        """首章 / 角色无历史状态 → 不注入'截止上一章'段。"""
        _seed_character_with_state(session, timeline=[])
        fake_llm = _make_fake_llm(json.dumps(_state_payload([])))
        extract(
            content="首章正文",
            card=_make_card(chapter_id="ch-0001", related_characters=["lin-yan"]),
            llm=fake_llm,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        user_text = fake_llm.complete.call_args.kwargs["messages"][1].content
        assert "角色当前状态(截止上一章" not in user_text

    def test_idempotent_replace_same_chapter(
        self, session: Session, vector_store: VectorStore, fake_prompts_dir: Path
    ) -> None:
        """同一章重抽 → 同 as_of_chapter 快照被替换,不重复累积。"""
        card = _make_card(chapter_id="ch-0010", related_characters=["lin-yan"])
        first = _state_payload(
            [{"character_id": "lin-yan", "injuries": ["右臂截肢"]}]
        )
        extract(
            content="v1", card=card, llm=_make_fake_llm(json.dumps(first)),
            workspace_id="ws-demo", db_session=session,
            vector_store=vector_store, prompts_dir=fake_prompts_dir,
        )
        session.commit()
        # 重抽同章,状态改了
        second = _state_payload(
            [{"character_id": "lin-yan", "injuries": ["右臂截肢", "左肩刀伤"]}]
        )
        extract(
            content="v2", card=card, llm=_make_fake_llm(json.dumps(second)),
            workspace_id="ws-demo", db_session=session,
            vector_store=vector_store, prompts_dir=fake_prompts_dir,
        )
        session.commit()

        character = get_character(session, "lin-yan")
        assert character is not None
        # 只有一条 as_of_chapter=10 的快照(替换非累积)
        snaps_at_10 = [s for s in character.state_timeline if s.as_of_chapter == 10]
        assert len(snaps_at_10) == 1
        assert snaps_at_10[0].injuries == ["右臂截肢", "左肩刀伤"]

    def test_unknown_character_state_skipped(
        self, session: Session, vector_store: VectorStore, fake_prompts_dir: Path
    ) -> None:
        """character_states 引用未落库角色 → 跳过不崩,不影响其他抽取。"""
        payload = dict(_EMPTY_PAYLOAD)
        payload["character_states"] = [
            {"character_id": "ghost-char", "injuries": ["不存在"]}
        ]
        result = extract(
            content="略",
            card=_make_card(chapter_id="ch-0005"),
            llm=_make_fake_llm(json.dumps(payload)),
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()
        # 未知角色被跳过 → 0 snapshot 落库
        assert result.count_by_type["state_snapshot"] == 0
        assert get_character(session, "ghost-char") is None

    def test_backward_compat_payload_without_character_states(
        self, session: Session, vector_store: VectorStore, fake_prompts_dir: Path
    ) -> None:
        """旧 fixture / 旧缓存响应不带 character_states 字段 → 默认空,不报错。"""
        result = extract(
            content="略",
            card=_make_card(chapter_id="ch-0001"),
            llm=_make_fake_llm(json.dumps(_FULL_PAYLOAD)),  # 不含 character_states
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()
        assert result.count_by_type["state_snapshot"] == 0


class TestProgressionConsistencyS10:
    """模块②(v1.1 wave 5 s10):进阶等级抽取(锚定增量 + 结转 + OFF 零回归)防回退。"""

    def test_progression_off_zero_regression(
        self, session: Session, vector_store: VectorStore, fake_prompts_dir: Path
    ) -> None:
        """OFF(默认):user message 无进阶指令 + progression_rank 恒 None(零回归)。"""
        fake = _make_fake_llm(
            json.dumps(_state_payload([{"character_id": "lin-yan",
                                        "progression_rank": "金丹"}]))
        )
        _seed_character_with_state(session, character_id="lin-yan")
        extract(
            content="略", card=_make_card(chapter_id="ch-0010",
                                          related_characters=["lin-yan"]),
            llm=fake, workspace_id="ws-demo", db_session=session,
            vector_store=vector_store, prompts_dir=fake_prompts_dir,
            # progression_consistency_enabled 默认 False
        )
        session.commit()
        user_text = fake.complete.call_args_list[0].kwargs["messages"][1].content
        assert "额外抽取:角色进阶" not in user_text
        # OFF → 即便 LLM 误给 progression_rank,持久化端也不写(恒 None)
        snap = get_character(session, "lin-yan").state_timeline[0]
        assert snap.progression_rank is None

    def test_progression_on_explicit_rank(
        self, session: Session, vector_store: VectorStore, fake_prompts_dir: Path
    ) -> None:
        """ON + LLM 明写进阶 → 落库 + prompt 含锚定增量指令 + 题材标签。"""
        fake = _make_fake_llm(
            json.dumps(_state_payload([{"character_id": "lin-yan",
                                        "progression_rank": "金丹"}]))
        )
        _seed_character_with_state(session, character_id="lin-yan")
        extract(
            content="林衍突破金丹",
            card=_make_card(chapter_id="ch-0010", related_characters=["lin-yan"]),
            llm=fake, workspace_id="ws-demo", db_session=session,
            vector_store=vector_store, prompts_dir=fake_prompts_dir,
            progression_consistency_enabled=True, progression_label="境界",
        )
        session.commit()
        user_text = fake.complete.call_args_list[0].kwargs["messages"][1].content
        assert "额外抽取:角色进阶境界" in user_text
        assert "只认明写" in user_text
        snap = get_character(session, "lin-yan").state_timeline[0]
        assert snap.progression_rank == "金丹"

    def test_progression_carry_forward(
        self, session: Session, vector_store: VectorStore, fake_prompts_dir: Path
    ) -> None:
        """结转:LLM 没明写变化(null)→ 沿用上一章 progression_rank + 锚定 prior 段。"""
        _seed_character_with_state(
            session, character_id="lin-yan",
            timeline=[CharacterStateSnapshot(as_of_chapter=5,
                                             progression_rank="炼气六层巅峰")],
        )
        fake = _make_fake_llm(
            json.dumps(_state_payload([{"character_id": "lin-yan",
                                        "physical_state": "平稳"}]))  # 无 progression_rank
        )
        extract(
            content="林衍打坐,无突破",
            card=_make_card(chapter_id="ch-0010", related_characters=["lin-yan"]),
            llm=fake, workspace_id="ws-demo", db_session=session,
            vector_store=vector_store, prompts_dir=fake_prompts_dir,
            progression_consistency_enabled=True, progression_label="境界",
        )
        session.commit()
        user_text = fake.complete.call_args_list[0].kwargs["messages"][1].content
        assert "当前境界:炼气六层巅峰" in user_text  # 锚定增量:prior 注入
        char = get_character(session, "lin-yan")
        snap10 = next(s for s in char.state_timeline if s.as_of_chapter == 10)
        assert snap10.progression_rank == "炼气六层巅峰"  # 结转,没明写就不丢
