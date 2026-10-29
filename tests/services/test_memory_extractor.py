"""P7-B-05 MemoryExtractor 实体合并规则增强单测。

覆盖 ``_persist_characters`` 三档合并增强:

1. **名称相似度去重**:``character_id`` 找不到 → ``difflib.SequenceMatcher``
   ratio ≥ 0.85 → 视为同人 → 复用既有 ``character_id`` 走 update 路径(不创建
   新行)。
2. **阈值边界(0.84)不合并**:相似度 < 0.85 → 创建新行(分界线证据)。
3. **first_appearance_chapter 早优先**:字典序取小者保留(``ch-0003`` < ``ch-0006``)。
4. **voice_profile 字段级合并**:incoming list / dict 为空 → 保留 existing 非空值;
   incoming 非空 → 整体覆盖。

P1-P6 既有 4 测覆盖在 ``tests/unit/test_memory_extractor.py``(同 ID get/update/create
语义);本文件只 cover P7-B-05 新规则(合并规则增强后产生的"合并实体"语义)。

fixture / mock 全走 tmp_path,与 P2-B-09 unit 测同源(``_seed_workspace`` /
fake LLM client / mock embedding_fn);DATA_DIR 不需要(纯 SQLite + Chroma in-memory
临时目录,基线 2090 passed + 本任务新增 ≥4 测后上调)。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.core.agents import _prompt as prompt_module
from src.core.agents.memory_extractor import extract
from src.core.db import Base, WorkspaceORM
from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage  # noqa: F401
from src.core.memory.store import (
    create_character,
    get_character,
    list_characters_by_workspace,
    update_character,
)
from src.core.memory.vector import VectorStore
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.models.character import (
    Character,
    CharacterCoreFact,
    CharacterStateSnapshot,
    VoiceProfile,
)
from src.core.models.setting_contract import SettingContract

# ---- fixture(对齐 tests/unit/test_memory_extractor.py 模式)-----------------


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
        chapter_id="ch-0006",
        workspace_id="ws-p7b05",
        volume_id="v01",
        chapter_number=6,
        title="第六章",
        tension_level=4,
        word_count_min=2800,
        word_count_max=4500,
        plot_node_budget=3,
        must_achieve=["林衍下山"],
        must_unresolved=["山门秘辛"],
        forbidden=["现代科技"],
        pov_character="林衍",
        setting="青崖山下",
        continuity_context=_make_continuity(),
        next_chapter_seed="次回伏笔",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=[],
        review_threshold=7.5,
        revise_threshold=6.5,
        max_rewrites=3,
        max_revises=2,
    )
    payload.update(overrides)
    return ChapterCard(**payload)


def _make_response(
    *, content: str, model_used: str = "claude-sonnet-4-6"
) -> LLMResponse:
    return LLMResponse(
        content=content,
        model_used=model_used,
        usage=TokenUsage(input_tokens=100, output_tokens=200),
        finish_reason="end_turn",
    )


def _make_fake_llm(content: str) -> MagicMock:
    fake = MagicMock(spec=LLMClient)
    fake.complete.return_value = _make_response(content=content)
    return fake


def _make_extracted_character_payload(
    *,
    character_id: str,
    name: str,
    voice_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造单个 ``ExtractedCharacter`` LLM 输出 payload。

    ``voice_overrides`` 给定字段会覆盖 voice_profile 默认非空字段;list / dict
    类字段传 ``[]`` / ``{}`` 即可断言"LLM 重抽产空 → 触发字段级合并"。
    """
    voice = {
        "tone": "冷峻",
        "sentence_style": "短句直白",
        "self_address": "我",
        "addresses_others": {"shi-jie": "师姐"},
        "catchphrases": ["问鼎"],
        "forbidden_phrases": ["亲爱的"],
    }
    if voice_overrides:
        voice.update(voice_overrides)
    return {
        "character_id": character_id,
        "name": name,
        "archetype": "萌新剑修",
        "appearance": "白衣青年",
        "personality": "沉静内敛",
        "background": "出身寒门",
        "voice_profile": voice,
        "goals_long_term": [],
        "goals_short_term": [],
        "aliases": [],
        "current_state": {},
        "relationships": {},
        "abilities": [],
        "secrets": [],
    }


def _wrap_payload(character_payload: dict[str, Any]) -> str:
    return json.dumps(
        {
            "characters": [character_payload],
            "plot_threads": [],
            "timeline": [],
            "facts": [],
        }
    )


def _seed_workspace(session: Session, workspace_id: str) -> None:
    now = datetime.now(UTC)
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
    db_path = tmp_path / "novel.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _seed_workspace(s, "ws-p7b05")
        yield s


def _mock_embedding_fn(texts: list[str]) -> list[list[float]]:
    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


@pytest.fixture
def vector_store(tmp_path: Path) -> VectorStore:
    return VectorStore(
        "ws-p7b05",
        embedding_fn=_mock_embedding_fn,
        persist_dir=tmp_path / "chroma",
    )


@pytest.fixture
def fake_prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "memory_extractor.md").write_text(
        "FAKE_EXTRACTOR_SYSTEM_PROMPT",
        encoding="utf-8",
    )
    return prompts_dir


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> Any:
    prompt_module.load_prompt_template.cache_clear()
    yield
    prompt_module.load_prompt_template.cache_clear()


def _seed_existing_character(
    session: Session,
    *,
    character_id: str,
    name: str,
    first_appearance_chapter: str = "ch-0003",
    voice: VoiceProfile | None = None,
) -> Character:
    if voice is None:
        voice = VoiceProfile(
            tone="冷峻",
            sentence_style="短句直白",
            self_address="我",
            addresses_others={"shi-jie": "师姐"},
            catchphrases=["问鼎"],
            forbidden_phrases=["亲爱的", "宝贝"],
        )
    char = Character(
        character_id=character_id,
        workspace_id="ws-p7b05",
        name=name,
        archetype="萌新剑修",
        appearance="白衣青年",
        personality="沉静内敛",
        background="出身寒门",
        voice_profile=voice,
        goals_long_term=[],
        goals_short_term=[],
        first_appearance_chapter=first_appearance_chapter,
        last_updated_chapter=first_appearance_chapter,
    )
    saved = create_character(session, char)
    session.commit()
    return saved


def test_rich_memory_preserves_approved_core_namespace_and_same_chapter_snapshot(
    session: Session,
    vector_store: VectorStore,
    fake_prompts_dir: Path,
) -> None:
    """富记忆可以补普通状态，但不能把 approved_text 核心事实覆盖。"""

    seeded = _seed_existing_character(
        session,
        character_id="lin-yan",
        name="林衍",
        first_appearance_chapter="ch-0001",
    )
    source_hash = "a" * 64
    core_fact = CharacterCoreFact(
        state_key="character:lin-yan:life_status",
        attribute="life_status",
        value="存活",
        source_chapter_id="ch-0006",
        source_version_id=1,
        source_path="chapter_summaries/ch-0006.core_state_candidates",
        source_text_hash=source_hash,
        evidence_quote="林衍仍有清晰的呼吸和脉搏。",
        valid_from_chapter=6,
    )
    approved_snapshot = CharacterStateSnapshot(
        as_of_chapter=6,
        physical_state="清醒",
        authority="approved_text",
        source_chapter_id="ch-0006",
        source_version_id=1,
        source_text_hash=source_hash,
        evidence_quotes=[core_fact.evidence_quote],
        core_facts=[core_fact],
    )
    update_character(
        session,
        seeded.model_copy(
            update={
                "current_state": {
                    "core.life_status": "存活",
                    "位置": "井房",
                },
                "state_timeline": [approved_snapshot],
            }
        ),
    )
    session.commit()

    incoming = _make_extracted_character_payload(
        character_id="lin-yan", name="林衍"
    )
    incoming["current_state"] = {
        "core.life_status": "死亡",  # 故意模拟低权威错误覆盖
        "情绪": "疲惫",
    }
    payload = {
        "characters": [incoming],
        "plot_threads": [],
        "timeline": [],
        "facts": [],
        "character_states": [
            {
                "character_id": "lin-yan",
                "physical_state": "轻伤",
                "injuries": ["手掌擦伤"],
                "acquired_items": [],
                "current_relationships": {},
                "state_changes": [],
            }
        ],
    }

    extract(
        content="林衍手掌擦伤，但仍然活着。",
        card=_make_card(chapter_id="ch-0006"),
        llm=_make_fake_llm(json.dumps(payload, ensure_ascii=False)),
        workspace_id="ws-p7b05",
        db_session=session,
        vector_store=vector_store,
        prompts_dir=fake_prompts_dir,
    )
    session.commit()

    loaded = get_character(session, "lin-yan")
    assert loaded is not None
    assert loaded.current_state["core.life_status"] == "存活"
    assert loaded.current_state["情绪"] == "疲惫"
    snapshot = next(
        item for item in loaded.state_timeline if item.as_of_chapter == 6
    )
    assert snapshot.authority == "approved_text"
    assert snapshot.core_facts == [core_fact]
    assert snapshot.physical_state == "轻伤"
    assert snapshot.injuries == ["手掌擦伤"]


def test_rich_memory_carries_prior_core_facts_into_next_chapter_snapshot(
    session: Session,
    vector_store: VectorStore,
    fake_prompts_dir: Path,
) -> None:
    """本章无新核心候选时，富记忆新快照仍须继承上一章批准核心事实。"""
    seeded = _seed_existing_character(
        session,
        character_id="lin-yan",
        name="林衍",
        first_appearance_chapter="ch-0001",
    )
    core_fact = CharacterCoreFact(
        state_key="character:lin-yan:permanent_injury",
        attribute="permanent_injury",
        value="右臂齐肘截断",
        source_chapter_id="ch-0006",
        source_version_id=1,
        source_path="chapter_summaries/ch-0006.core_state_candidates",
        source_text_hash="b" * 64,
        evidence_quote="他的右臂已齐肘截断。",
        valid_from_chapter=6,
    )
    update_character(
        session,
        seeded.model_copy(
            update={
                "current_state": {"core.permanent_injury": core_fact.value},
                "state_timeline": [
                    CharacterStateSnapshot(
                        as_of_chapter=6,
                        authority="approved_text",
                        source_chapter_id="ch-0006",
                        source_version_id=1,
                        source_text_hash="b" * 64,
                        evidence_quotes=[core_fact.evidence_quote],
                        core_facts=[core_fact],
                    )
                ],
            }
        ),
    )
    session.commit()
    incoming = _make_extracted_character_payload(
        character_id="lin-yan", name="林衍"
    )
    payload = {
        "characters": [incoming],
        "plot_threads": [],
        "timeline": [],
        "facts": [],
        "character_states": [
            {
                "character_id": "lin-yan",
                "physical_state": "失血后虚弱",
                "injuries": [],
                "acquired_items": [],
                "current_relationships": {},
                "state_changes": [],
            }
        ],
    }

    extract(
        content="第七章继续推进，林衍因失血而虚弱。",
        card=_make_card(chapter_id="ch-0007"),
        llm=_make_fake_llm(json.dumps(payload, ensure_ascii=False)),
        workspace_id="ws-p7b05",
        db_session=session,
        vector_store=vector_store,
        prompts_dir=fake_prompts_dir,
    )
    session.commit()

    loaded = get_character(session, "lin-yan")
    assert loaded is not None
    snapshot = next(
        item for item in loaded.state_timeline if item.as_of_chapter == 7
    )
    assert snapshot.authority == "approved_text"
    assert snapshot.core_facts == [core_fact]
    assert snapshot.physical_state == "失血后虚弱"
    assert loaded.current_state["core.permanent_injury"] == core_fact.value


# =============================================================================
# 0. 权威设定优先级(R9 v5 派生状态污染热修)
# =============================================================================


def test_memory_extractor_receives_authoritative_setting_contract(
    session: Session,
    vector_store: VectorStore,
    fake_prompts_dir: Path,
) -> None:
    """同一既有 extractor 调用拿到 world 契约；不新增 Agent 或 Provider 请求。"""
    payload = json.dumps(
        {
            "characters": [],
            "plot_threads": [],
            "timeline": [],
            "facts": [],
            "character_states": [],
        },
        ensure_ascii=False,
    )
    fake_llm = _make_fake_llm(payload)
    contract = SettingContract(
        unique_rules="单次观看超时会导致短暂失明。",
        source_path="data/outline/world.json",
        source_hash="a" * 64,
    )

    extract(
        content="林衍本章末仍然看不见。",
        card=_make_card(chapter_id="ch-0006"),
        llm=fake_llm,
        workspace_id="ws-p7b05",
        db_session=session,
        vector_store=vector_store,
        prompts_dir=fake_prompts_dir,
        setting_contract=contract,
    )

    assert fake_llm.complete.call_count == 1
    user_text = fake_llm.complete.call_args.kwargs["messages"][1].content
    assert "权威世界契约" in user_text
    assert contract.unique_rules in user_text
    assert "富记忆权威优先级" in user_text
    assert "不得把‘本章尚未恢复’擅自升级为‘永久／不可逆’" in user_text


# =============================================================================
# 1. 名称相似度合并(P7-B-05 决策 5=B 第 ① 项)
# =============================================================================


class TestNameSimilarityMerge:
    """``_find_similar_character`` ratio ≥ 0.85 触发合并;< 0.85 创建新行。"""

    def test_high_similarity_merges_to_existing(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """name 完全相同(ratio = 1.0)但 character_id 漂移 → 合并到既有行,不创新行。"""
        # 种入既有 "林衍" / character_id="lin-yan"
        _seed_existing_character(
            session, character_id="lin-yan", name="林衍"
        )
        # LLM 抽出 character_id="lin-yan-2" 但 name 仍是 "林衍" → 应合并
        payload = _make_extracted_character_payload(
            character_id="lin-yan-2", name="林衍"
        )
        extract(
            content="略",
            card=_make_card(chapter_id="ch-0006"),
            llm=_make_fake_llm(_wrap_payload(payload)),
            workspace_id="ws-p7b05",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()

        chars = list_characters_by_workspace(session, "ws-p7b05")
        assert len(chars) == 1, "name 100% 相似不应创建新行"
        assert chars[0].character_id == "lin-yan"  # 复用既有 ID
        assert chars[0].name == "林衍"

    def test_threshold_boundary_below_does_not_merge(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """name 相似度 < 0.85 阈值 → 创建新行(分界线证据)。

        "Alice" vs "Bob" ratio 是 0(无公共子串),远低于 0.85 → 新行。
        阈值边界严格 < 0.85 不合并;= 0.85 合并(由实现 ``>=`` 判断)。
        """
        _seed_existing_character(
            session, character_id="alice", name="Alice"
        )
        payload = _make_extracted_character_payload(
            character_id="bob", name="Bob"
        )
        extract(
            content="略",
            card=_make_card(chapter_id="ch-0006"),
            llm=_make_fake_llm(_wrap_payload(payload)),
            workspace_id="ws-p7b05",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()

        chars = list_characters_by_workspace(session, "ws-p7b05")
        assert len(chars) == 2  # 不合并,两行并存
        ids = {c.character_id for c in chars}
        assert ids == {"alice", "bob"}


# =============================================================================
# 2. first_appearance_chapter 早优先(P7-B-05 决策 5=B 第 ② 项)
# =============================================================================


class TestFirstAppearanceEarliestWins:
    """``_earliest_chapter_id``:已有 + 本章字典序取小者。"""

    def test_existing_earlier_chapter_preserved(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """既有 first_appearance=ch-0003,本章=ch-0006 → 保留 ch-0003。"""
        _seed_existing_character(
            session,
            character_id="lin-yan",
            name="林衍",
            first_appearance_chapter="ch-0003",
        )
        payload = _make_extracted_character_payload(
            character_id="lin-yan", name="林衍"
        )
        extract(
            content="略",
            card=_make_card(chapter_id="ch-0006"),
            llm=_make_fake_llm(_wrap_payload(payload)),
            workspace_id="ws-p7b05",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()

        loaded = get_character(session, "lin-yan")
        assert loaded is not None
        assert loaded.first_appearance_chapter == "ch-0003"  # 早值保留
        assert loaded.last_updated_chapter == "ch-0006"  # last 推进

    def test_rewind_scenario_earliest_wins_via_similarity(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """rewind 场景:既有 first_appearance=ch-0008,LLM 重抽本章=ch-0003 →
        合并后 first_appearance=ch-0003(字典序更小,"早优先"语义)。
        """
        _seed_existing_character(
            session,
            character_id="lin-yan",
            name="林衍",
            first_appearance_chapter="ch-0008",
        )
        payload = _make_extracted_character_payload(
            character_id="lin-yan", name="林衍"
        )
        extract(
            content="略",
            card=_make_card(chapter_id="ch-0003"),
            llm=_make_fake_llm(_wrap_payload(payload)),
            workspace_id="ws-p7b05",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()

        loaded = get_character(session, "lin-yan")
        assert loaded is not None
        assert loaded.first_appearance_chapter == "ch-0003"  # 早优先生效


# =============================================================================
# 3. voice_profile 字段级合并(P7-B-05 决策 5=B 第 ③ 项)
# =============================================================================


class TestVoiceProfileFieldMerge:
    """``_merge_voice_profile``:incoming 空字段保留 existing 值,非空才覆盖。"""

    def test_empty_list_fields_preserve_existing(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """LLM 重抽时 ``forbidden_phrases`` / ``catchphrases`` / ``addresses_others``
        产为空 → 保留 existing 非空值(对齐 v2_wishlist mock LLM 优化项段修法 2)。
        """
        existing_voice = VoiceProfile(
            tone="冷峻",
            sentence_style="短句直白",
            self_address="我",
            addresses_others={"shi-jie": "师姐", "shi-fu": "师父"},
            catchphrases=["问鼎", "持剑"],
            forbidden_phrases=["亲爱的", "宝贝", "甜心"],
        )
        _seed_existing_character(
            session,
            character_id="lin-yan",
            name="林衍",
            voice=existing_voice,
        )
        # LLM 重抽时 3 个集合类字段全产空 + 改 tone 字符串
        payload = _make_extracted_character_payload(
            character_id="lin-yan",
            name="林衍",
            voice_overrides={
                "tone": "凌厉",  # 非空 → 应覆盖
                "addresses_others": {},  # 空 → 应保留
                "catchphrases": [],  # 空 → 应保留
                "forbidden_phrases": [],  # 空 → 应保留
            },
        )
        extract(
            content="略",
            card=_make_card(chapter_id="ch-0006"),
            llm=_make_fake_llm(_wrap_payload(payload)),
            workspace_id="ws-p7b05",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()

        loaded = get_character(session, "lin-yan")
        assert loaded is not None
        voice = loaded.voice_profile
        # 非空字段覆盖
        assert voice.tone == "凌厉"
        # 空字段保留 existing
        assert voice.addresses_others == {"shi-jie": "师姐", "shi-fu": "师父"}
        assert voice.catchphrases == ["问鼎", "持剑"]
        assert voice.forbidden_phrases == ["亲爱的", "宝贝", "甜心"]

    def test_non_empty_incoming_overwrites(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
    ) -> None:
        """LLM 给非空 list / dict → 整体覆盖 existing(字段级"非空整体替换"语义,
        不做 list 元素级 union 合并)。
        """
        existing_voice = VoiceProfile(
            tone="冷峻",
            sentence_style="短句直白",
            self_address="我",
            addresses_others={"old-key": "old-val"},
            catchphrases=["old-phrase"],
            forbidden_phrases=["old-forbidden"],
        )
        _seed_existing_character(
            session,
            character_id="lin-yan",
            name="林衍",
            voice=existing_voice,
        )
        payload = _make_extracted_character_payload(
            character_id="lin-yan",
            name="林衍",
            voice_overrides={
                "addresses_others": {"new-key": "new-val"},
                "catchphrases": ["new-phrase"],
                "forbidden_phrases": ["new-forbidden"],
            },
        )
        extract(
            content="略",
            card=_make_card(chapter_id="ch-0006"),
            llm=_make_fake_llm(_wrap_payload(payload)),
            workspace_id="ws-p7b05",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )
        session.commit()

        loaded = get_character(session, "lin-yan")
        assert loaded is not None
        voice = loaded.voice_profile
        # 非空 incoming 整体替换 existing(无 union 语义)
        assert voice.addresses_others == {"new-key": "new-val"}
        assert voice.catchphrases == ["new-phrase"]
        assert voice.forbidden_phrases == ["new-forbidden"]


# =============================================================================
# Y1 缺陷 C 治本回归(v1.1 w4 B1,2026-06-03):extract 写事务不跨 merger LLM
# =============================================================================


class TestExtractWriteNotAcrossMergeLLM:
    """锁住"两段式"不变量:全部 merge LLM 在全部 SQLite persist 之前,中间
    ``rollback`` 隔断读快照。

    **为何**:原"merge LLM 与 persist 交错"实现让 extract 长 session 首次 persist
    即获 WAL 写锁,之后 plot_thread_merger / timeline_entry_merger 的 thinking LLM
    (各 ~20s)在持锁状态下进行 → 写锁被占 40s+ → 并发 cost 写 retry 全失败(B1
    长跑 ch-15~18 实证)。治本 = 先全 merge(只读 + 改 payload 不写库)后全 persist。
    本测试用 spy 记录调用顺序,防未来回归退化回交错。
    """

    def test_all_merge_called_before_all_persist_with_rollback_gate(
        self,
        session: Session,
        vector_store: VectorStore,
        fake_prompts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.core.agents import memory_extractor as me

        order: list[str] = []

        def _spy(label: str, *, returns: Any = None) -> Any:
            def _wrapper(*_args: Any, **_kwargs: Any) -> Any:
                order.append(label)
                return returns

            return _wrapper

        # 三个 merge:in_place 改 payload,返回 None。
        monkeypatch.setattr(
            me, "_apply_character_disambiguation_in_place", _spy("merge:character")
        )
        monkeypatch.setattr(
            me, "_apply_plot_thread_merge_in_place", _spy("merge:plot_thread")
        )
        monkeypatch.setattr(
            me, "_apply_timeline_entry_merge_in_place", _spy("merge:timeline")
        )
        # 四个 persist:返回 [](下游 _persist_to_vector_store 接 saved list)。
        monkeypatch.setattr(
            me, "_persist_characters", _spy("persist:characters", returns=[])
        )
        monkeypatch.setattr(
            me,
            "_persist_character_states",
            _spy("persist:character_states", returns=[]),
        )
        monkeypatch.setattr(
            me, "_persist_plot_threads", _spy("persist:plot_threads", returns=[])
        )
        monkeypatch.setattr(
            me, "_persist_timeline", _spy("persist:timeline", returns=[])
        )
        # Chroma 写 spy 掉(避免真写 + 记录其在最后)。
        monkeypatch.setattr(me, "_persist_to_vector_store", _spy("chroma"))
        # rollback spy(记录隔断时机;纯记录不真 rollback,spy 后 persist 均不真写)。
        monkeypatch.setattr(session, "rollback", _spy("rollback"))

        extract(
            content="正文内容",
            card=_make_card(),
            workspace_id="ws-p7b05",
            llm=_make_fake_llm(
                _wrap_payload(
                    _make_extracted_character_payload(
                        character_id="lin-yan", name="林衍"
                    )
                )
            ),
            model="claude-sonnet-4-6",
            db_session=session,
            vector_store=vector_store,
            prompts_dir=fake_prompts_dir,
        )

        merge_idx = [i for i, n in enumerate(order) if n.startswith("merge:")]
        persist_idx = [i for i, n in enumerate(order) if n.startswith("persist:")]
        assert len(merge_idx) == 3, f"应调 3 个 merge;实际顺序={order}"
        assert len(persist_idx) == 4, f"应调 4 个 persist;实际顺序={order}"
        # 不变量 1:所有 merge LLM 在所有 persist 写之前(写事务不跨 LLM 的核心)。
        assert max(merge_idx) < min(persist_idx), (
            f"全部 merge 必须在全部 persist 之前;实际顺序={order}"
        )
        # 不变量 2:rollback 隔断在 merge 之后、persist 之前(防 SNAPSHOT 升级)。
        rollback_idx = order.index("rollback")
        assert max(merge_idx) < rollback_idx < min(persist_idx), (
            f"rollback 必须隔断 merge/persist;实际顺序={order}"
        )
        # 不变量 3:Chroma 写在所有 SQLite persist 之后(原子性顺序不变)。
        assert order[-1] == "chroma", f"Chroma 应最后写;实际顺序={order}"
