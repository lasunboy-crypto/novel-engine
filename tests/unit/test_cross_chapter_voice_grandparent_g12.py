"""v1.1 wave 1 session 8 / G12:``_resolve_cross_chapter_voice_context`` 递归祖父
fallback 单元测试。

覆盖场景:
1. 直接前一章 approved 有 fingerprint → 返该章(=G10 旧行为)
2. 直接前一章 human_intervention + 祖父 approved → 跳过 + 返祖父
3. ch-N-1 + ch-N-2 都 human_intervention + ch-N-3 approved → 跳两层 + 返祖父
4. 全部上游章节都非 approved → 返 (None, None, default_voice)
5. 上游章节 approved 但无 fingerprint(旧数据)→ 继续向上找
6. ch-0001 自身 → 不向上找(prev_id=None)→ 返 (None, None, default_voice)
7. ORM 查询异常 → log + 继续向上找(不立即中断)
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.core.db import Base
from src.core.db.models import ChapterORM, ChapterVersionORM, WorkspaceORM
from src.core.models.chapter_card import ChapterCard, ContinuityContext
from src.core.orchestrator.chapter_orchestrator import (
    _resolve_cross_chapter_voice_context,
)

# 第三人称 fingerprint(他/她频次主导)
_THIRD_FP = {
    "paragraph_length_mean": 53.0,
    "paragraph_length_std": 18.0,
    "sentence_length_mean": 22.0,
    "sentence_length_std": 11.0,
    "simile_density": 0.02,
    "exclamation_density": 0.07,
    "total_chars": 2920.0,
    "total_sentences": 129.0,
    "pronoun_freq_我们": 0.0007,
    "pronoun_freq_你们": 0.0,
    "pronoun_freq_他们": 0.0017,
    "pronoun_freq_她们": 0.0,
    "pronoun_freq_我": 0.0038,
    "pronoun_freq_你": 0.0027,
    "pronoun_freq_他": 0.0065,  # 主导
    "pronoun_freq_她": 0.0031,
    "pronoun_freq_它": 0.0,
}

# 第一人称 fingerprint(我频次主导)
_FIRST_FP = {
    "paragraph_length_mean": 108.0,
    "paragraph_length_std": 69.0,
    "sentence_length_mean": 31.0,
    "sentence_length_std": 15.0,
    "simile_density": 0.05,
    "exclamation_density": 0.11,
    "total_chars": 3197.0,
    "total_sentences": 99.0,
    "pronoun_freq_我们": 0.0006,
    "pronoun_freq_你们": 0.0,
    "pronoun_freq_他们": 0.0,
    "pronoun_freq_她们": 0.0,
    "pronoun_freq_我": 0.0141,  # 主导
    "pronoun_freq_你": 0.0025,
    "pronoun_freq_他": 0.0081,
    "pronoun_freq_她": 0.0,
    "pronoun_freq_它": 0.0,
}


def _seed_workspace(session: Session, workspace_id: str = "luo-clean-v1") -> None:
    now = datetime.now(UTC)
    ws = WorkspaceORM(
        workspace_id=workspace_id,
        display_name="测试工作区",
        genre="都市悬疑",
        premise="一句话设定",
        created_at=now,
        last_active_at=now,
        engine_version="0.1.0",
        status="active",
        total_chapters=10,
        completed_chapters=0,
        config_overrides={},
        outline_status="committed",
        default_voice="third_person",
    )
    session.add(ws)


def _seed_chapter(
    session: Session,
    *,
    chapter_id: str,
    status: str,
    fingerprint: dict[str, float] | None,
    workspace_id: str = "luo-clean-v1",
    current_version: int = 1,
) -> None:
    now = datetime.now(UTC)
    ch = ChapterORM(
        chapter_id=chapter_id,
        workspace_id=workspace_id,
        status=status,
        current_version=current_version,
        rewrite_count=0,
        revise_count=0,
        created_at=now,
    )
    session.add(ch)
    if fingerprint is not None:
        cv = ChapterVersionORM(
            chapter_id=chapter_id,
            version_id=current_version,
            content="(略 — 测试用 fixture,fingerprint 已注入)",
            word_count=int(fingerprint.get("total_chars", 3000)),
            created_at=now,
            creation_type="draft",
            model_used="mock",
            notes="",
            review_score=None,
            style_fingerprint=fingerprint,
        )
        session.add(cv)


def _make_chapter_card(
    *,
    chapter_id: str,
    chapter_number: int,
) -> ChapterCard:
    return ChapterCard(
        chapter_id=chapter_id,
        workspace_id="luo-clean-v1",
        volume_id="vol-1",
        chapter_number=chapter_number,
        title="测试章",
        tension_level=5,
        word_count_min=2500,
        word_count_max=4000,
        plot_node_budget=3,
        must_achieve=["事件 A"],
        must_unresolved=["悬念 X"],
        forbidden=[],
        pov_character="主角",
        setting="现代心理诊所",
        continuity_context=ContinuityContext(
            prev_chapter_tail="(上一章末尾)",
            unfinished_actions=[],
            time_gap="数小时后",
            scene_continuation="same",
            pov_change=False,
            pov_from="主角",
            pov_to="主角",
        ),
        next_chapter_seed="(下一章引子)",
        arc_phase="rising",
        is_arc_finale=False,
        related_plot_threads=[],
        related_characters=["主角"],
    )


@pytest.fixture
def session(tmp_path: Path) -> Session:
    db_path = tmp_path / "novel.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _seed_workspace(s)
        yield s


class TestG12RecursiveAncestorBaseline:
    """G12 治本上游核心场景:直接前一章 baseline 不可用时递归向上找。"""

    def test_direct_prev_approved_returns_prev_fingerprint(
        self, session: Session
    ) -> None:
        """G10 旧行为兼容:直接前一章 approved + 有 fingerprint → 返该章 fingerprint。"""
        _seed_chapter(session, chapter_id="ch-0005", status="approved", fingerprint=_THIRD_FP)
        session.commit()

        card = _make_chapter_card(chapter_id="ch-0006", chapter_number=6)
        baseline_fp, baseline_id, default_voice = _resolve_cross_chapter_voice_context(
            preread_session=session,
            workspace_id="luo-clean-v1",
            chapter_card=card,
        )
        assert baseline_fp == _THIRD_FP
        assert baseline_id == "ch-0005"
        assert default_voice == "third_person"

    def test_prev_human_intervention_skips_to_grandparent(
        self, session: Session
    ) -> None:
        """G12 核心:ch-5 human_intervention + ch-4 approved → 跳过 ch-5,返 ch-4 fingerprint。

        luo-clean-v1 现场重演:ch-5 4 versions 全 fail 撞 budget → 进 human_intervention,
        ch-6 baseline 应该跳过 ch-5 走 ch-4(third_person)而非 None。
        """
        _seed_chapter(
            session, chapter_id="ch-0004", status="approved", fingerprint=_THIRD_FP
        )
        _seed_chapter(
            session, chapter_id="ch-0005", status="human_intervention", fingerprint=None
        )
        session.commit()

        card = _make_chapter_card(chapter_id="ch-0006", chapter_number=6)
        baseline_fp, baseline_id, _ = _resolve_cross_chapter_voice_context(
            preread_session=session,
            workspace_id="luo-clean-v1",
            chapter_card=card,
        )
        assert baseline_fp == _THIRD_FP
        assert baseline_id == "ch-0004"

    def test_two_levels_failed_skips_to_great_grandparent(
        self, session: Session
    ) -> None:
        """G12 鲁棒边界:ch-5 + ch-4 都 human_intervention,ch-3 approved → 跳两层返 ch-3。"""
        _seed_chapter(
            session, chapter_id="ch-0003", status="approved", fingerprint=_THIRD_FP
        )
        _seed_chapter(
            session, chapter_id="ch-0004", status="rewriting", fingerprint=None
        )
        _seed_chapter(
            session, chapter_id="ch-0005", status="human_intervention", fingerprint=None
        )
        session.commit()

        card = _make_chapter_card(chapter_id="ch-0006", chapter_number=6)
        baseline_fp, baseline_id, _ = _resolve_cross_chapter_voice_context(
            preread_session=session,
            workspace_id="luo-clean-v1",
            chapter_card=card,
        )
        assert baseline_fp == _THIRD_FP
        assert baseline_id == "ch-0003"

    def test_all_upstream_failed_returns_none(self, session: Session) -> None:
        """G12 C 兜底场景:全部上游都非 approved → 返 (None, None, default_voice),
        让 validator 走 workspace.default_voice 兜底校验路径。"""
        _seed_chapter(
            session, chapter_id="ch-0001", status="drafting", fingerprint=None
        )
        _seed_chapter(
            session, chapter_id="ch-0002", status="human_intervention", fingerprint=None
        )
        session.commit()

        card = _make_chapter_card(chapter_id="ch-0003", chapter_number=3)
        baseline_fp, baseline_id, default_voice = _resolve_cross_chapter_voice_context(
            preread_session=session,
            workspace_id="luo-clean-v1",
            chapter_card=card,
        )
        assert baseline_fp is None
        assert baseline_id is None
        assert default_voice == "third_person"

    def test_upstream_approved_but_no_fingerprint_skips_continues(
        self, session: Session
    ) -> None:
        """G12 向后兼容:approved 但无 fingerprint(G5 前旧数据)→ 继续向上找。"""
        _seed_chapter(
            session, chapter_id="ch-0003", status="approved", fingerprint=_THIRD_FP
        )
        # ch-0004 approved 但无 fingerprint(模拟 G5 之前的旧数据)
        _seed_chapter(
            session, chapter_id="ch-0004", status="approved", fingerprint=None
        )
        session.commit()

        card = _make_chapter_card(chapter_id="ch-0005", chapter_number=5)
        baseline_fp, baseline_id, _ = _resolve_cross_chapter_voice_context(
            preread_session=session,
            workspace_id="luo-clean-v1",
            chapter_card=card,
        )
        # 跳过 ch-4(无 fingerprint)→ ch-3(有 fingerprint)
        assert baseline_fp == _THIRD_FP
        assert baseline_id == "ch-0003"

    def test_first_chapter_returns_none(self, session: Session) -> None:
        """ch-0001 没有上游 → _infer_prev_chapter_id 返 None → helper 立即返 (None, None, default)。"""
        card = _make_chapter_card(chapter_id="ch-0001", chapter_number=1)
        baseline_fp, baseline_id, default_voice = _resolve_cross_chapter_voice_context(
            preread_session=session,
            workspace_id="luo-clean-v1",
            chapter_card=card,
        )
        assert baseline_fp is None
        assert baseline_id is None
        assert default_voice == "third_person"

    def test_missing_chapter_row_skips_continues(self, session: Session) -> None:
        """G12 鲁棒:上游 ChapterORM 行缺失(测试 fixture 残缺)→ 跳过 + 继续向上找。"""
        _seed_chapter(
            session, chapter_id="ch-0003", status="approved", fingerprint=_THIRD_FP
        )
        # 故意不创建 ch-0004 行(模拟数据缺失场景)
        session.commit()

        card = _make_chapter_card(chapter_id="ch-0005", chapter_number=5)
        baseline_fp, baseline_id, _ = _resolve_cross_chapter_voice_context(
            preread_session=session,
            workspace_id="luo-clean-v1",
            chapter_card=card,
        )
        # ch-4 行缺失 → 跳到 ch-3
        assert baseline_fp == _THIRD_FP
        assert baseline_id == "ch-0003"


class TestG12WorkspaceDefaultVoiceVariants:
    """G12 配套:workspace.default_voice 不同值正确透传。"""

    def test_first_person_default_voice(self, session: Session) -> None:
        """workspace.default_voice='first_person' 正确返第三返值。"""
        ws = session.get(WorkspaceORM, "luo-clean-v1")
        ws.default_voice = "first_person"
        session.commit()

        card = _make_chapter_card(chapter_id="ch-0002", chapter_number=2)
        _, _, default_voice = _resolve_cross_chapter_voice_context(
            preread_session=session,
            workspace_id="luo-clean-v1",
            chapter_card=card,
        )
        assert default_voice == "first_person"

    def test_free_default_voice(self, session: Session) -> None:
        """workspace.default_voice='free' 正确透传(validator Layer 0 用)。"""
        ws = session.get(WorkspaceORM, "luo-clean-v1")
        ws.default_voice = "free"
        session.commit()

        card = _make_chapter_card(chapter_id="ch-0002", chapter_number=2)
        _, _, default_voice = _resolve_cross_chapter_voice_context(
            preread_session=session,
            workspace_id="luo-clean-v1",
            chapter_card=card,
        )
        assert default_voice == "free"

    def test_workspace_missing_falls_back_to_third_person(self, session: Session) -> None:
        """workspace_id 不存在(异常路径)→ default_voice 兜底 third_person。"""
        card = _make_chapter_card(chapter_id="ch-0002", chapter_number=2)
        _, _, default_voice = _resolve_cross_chapter_voice_context(
            preread_session=session,
            workspace_id="nonexistent-workspace",
            chapter_card=card,
        )
        assert default_voice == "third_person"
