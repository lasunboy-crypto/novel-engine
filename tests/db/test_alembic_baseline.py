"""验证 P1-B-16 alembic 基线迁移与 ORM 模型 schema 的正确性。

通过 alembic 的 Python API 程序化驱动 upgrade / downgrade,避免依赖 shell;
通过 ``monkeypatch.setenv("NOVEL_DB_PATH", ...)`` 注入临时 db,无需污染全局。

覆盖维度:
- env.py 缺失 NOVEL_DB_PATH 时抛 RuntimeError(防止误连)
- upgrade head 后 5 张业务表 + 3 个索引 + alembic_version 都存在
- 各表的列名、类型、可空性、PK/FK 约束符合 ORM 声明
- downgrade base 后业务表全部删除,只剩 alembic_version
- upgrade → downgrade → upgrade 幂等
- ORM 可对 schema 做最小读写(workspace 插入 / chapter / version / review / issue 端到端)
- chapter_versions 复合主键唯一性强制
- review_issues 用 role 字段同表区分 issue / revise_target
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from src.core.db import (
    ChapterORM,
    ChapterVersionORM,
    ReviewIssueORM,
    ReviewResultORM,
    WorkspaceORM,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config() -> Config:
    """返回指向仓库 alembic.ini 的 alembic Config 实例。"""
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    return cfg


@pytest.fixture
def tmp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """提供临时 db 路径并注入到 NOVEL_DB_PATH 供 alembic env.py 读取。"""
    db_path = tmp_path / "novel.db"
    monkeypatch.setenv("NOVEL_DB_PATH", str(db_path))
    return db_path


def _table_names(db_path: Path) -> list[str]:
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def _index_names(db_path: Path) -> list[str]:
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


class TestEnvConfig:
    def test_missing_db_path_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """env.py 没有 NOVEL_DB_PATH 时必须抛错,防止误连默认 db。"""
        monkeypatch.delenv("NOVEL_DB_PATH", raising=False)
        with pytest.raises(RuntimeError, match="NOVEL_DB_PATH"):
            command.upgrade(_alembic_config(), "head")

    def test_blank_db_path_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """空白字符串视同未设置(防止 .env 误设 NOVEL_DB_PATH= 留空)。"""
        monkeypatch.setenv("NOVEL_DB_PATH", "   ")
        with pytest.raises(RuntimeError, match="NOVEL_DB_PATH"):
            command.upgrade(_alembic_config(), "head")


class TestUpgradeBaseline:
    """P1 baseline 测试。**显式 upgrade 到 ``0001_baseline``**(非 ``head``),
    本类只验证 P1 5 表 + 3 索引的范围;P2+ 表 / 索引由各阶段自己的测试覆盖
    (例:``test_p2_models.py::TestUpgradeP2``)。"""

    def test_upgrade_creates_all_business_tables(self, tmp_db_path: Path) -> None:
        command.upgrade(_alembic_config(), "0001_baseline")
        tables = _table_names(tmp_db_path)
        assert "alembic_version" in tables
        assert sorted(set(tables) - {"alembic_version"}) == [
            "chapter_versions",
            "chapters",
            "review_issues",
            "review_results",
            "workspaces",
        ]

    def test_upgrade_creates_all_indexes(self, tmp_db_path: Path) -> None:
        command.upgrade(_alembic_config(), "0001_baseline")
        indexes = _index_names(tmp_db_path)
        assert sorted(indexes) == [
            "ix_chapters_workspace_id",
            "ix_review_issues_review_id",
            "ix_review_results_chapter_id",
        ]


class TestSchemaShape:
    """以 ORM metadata 为真相源,反向校验落库 schema 的形状一致。"""

    @pytest.fixture(autouse=True)
    def _upgrade(self, tmp_db_path: Path) -> None:
        command.upgrade(_alembic_config(), "head")
        self.engine = create_engine(f"sqlite:///{tmp_db_path}")
        self.inspector = inspect(self.engine)

    def test_workspaces_columns(self) -> None:
        cols = {c["name"] for c in self.inspector.get_columns("workspaces")}
        assert cols == {
            "workspace_id",
            "display_name",
            "genre",
            "premise",
            "created_at",
            "last_active_at",
            "engine_version",
            "status",
            "total_chapters",
            "completed_chapters",
            "config_overrides",
            # P5-B-07(0007 加列):outline_status 3 档(not_started /
            # generating / committed),CHECK ``ck_workspaces_outline_status``。
            "outline_status",
            # v1.1 wave 1 session 6(0015 加列 / G10):default_voice 3 档
            # (third_person / first_person / free),CHECK
            # ``ck_workspaces_default_voice``,server_default='third_person'。
            "default_voice",
        }
        pk = self.inspector.get_pk_constraint("workspaces")
        assert pk["constrained_columns"] == ["workspace_id"]

    def test_chapters_columns_and_fk(self) -> None:
        cols = {c["name"] for c in self.inspector.get_columns("chapters")}
        # 注意:不含 card / versions / review_history(P1 决策)。
        assert cols == {
            "chapter_id",
            "workspace_id",
            "current_version",
            "status",
            "rewrite_count",
            "revise_count",
            "created_at",
            "approved_at",
            "total_token_usage",
            "total_cost_usd",
            # P6-B-01(0008 加列):force-pass 端点持久化字段
            "force_pass_at",
            "force_pass_reason",
            # v1.1 wave 6 session 3(0020 加列,B2):双时空场景锚点
            "scene_anchor",
            # v1.1 acceptance 修复2(0022 加列,Fix D):升人工结构化详情
            "escalation_details",
            # W3.5 任务 C(0025 加列):写作 ready / memory_enriched 双水位
            "ready_for_next_chapter_at",
            "memory_enrichment_status",
            "memory_enriched_at",
            "memory_enrichment_details",
            # post-v6 Session 3/C(0032):核心状态 ready 前提交水位
            "core_state_commit_hash",
            "core_state_commit_status",
            "core_state_commit_details",
        }
        pk = self.inspector.get_pk_constraint("chapters")
        assert pk["constrained_columns"] == ["chapter_id"]
        fks = self.inspector.get_foreign_keys("chapters")
        assert len(fks) == 1
        fk = fks[0]
        assert fk["referred_table"] == "workspaces"
        assert fk["constrained_columns"] == ["workspace_id"]
        assert fk["referred_columns"] == ["workspace_id"]

    def test_chapter_versions_compound_pk(self) -> None:
        cols = {c["name"] for c in self.inspector.get_columns("chapter_versions")}
        assert cols == {
            "chapter_id",
            "version_id",
            "content",
            "word_count",
            "created_at",
            "creation_type",
            "model_used",
            "review_score",
            "notes",
            # v1.1 wave 1 session 4(G5)新加(Alembic 0014)
            "style_fingerprint",
        }
        pk = self.inspector.get_pk_constraint("chapter_versions")
        assert sorted(pk["constrained_columns"]) == ["chapter_id", "version_id"]

    def test_review_results_columns_and_fk(self) -> None:
        cols = {c["name"] for c in self.inspector.get_columns("review_results")}
        assert cols == {
            "review_id",
            "chapter_id",
            "version_reviewed",
            "reviewed_at",
            "reviewer_model",
            "score_plot_progression",
            "score_tension_match",
            "score_prose_quality",
            "score_consistency",
            "score_hook_management",
            "score_voice_consistency",
            "score_overall",
            # wave 5 卓越性纯度量(Alembic 0021,v1.1 wave 7 持久化):nullable,
            # 永不进 score_overall / verdict(纯存储,与判定正交)。
            "score_imagery",
            "score_interiority",
            "score_formulaic",
            "score_immersion",
            "hard_constraints_passed",
            "failed_constraints",
            # W3.5-R2:结构化 constraint provenance / HARD-SOFT 证据 JSON。
            "constraint_violations",
            # W3.5-R6:四域逐约束 pass/fail 与 pass→fail 稳定 ID。
            "constraint_results",
            "hard_constraint_regressions",
            "suggestions",
            "verdict",
        }
        fks = self.inspector.get_foreign_keys("review_results")
        assert len(fks) == 1
        assert fks[0]["referred_table"] == "chapters"

    def test_review_issues_columns_and_fk(self) -> None:
        cols = {c["name"] for c in self.inspector.get_columns("review_issues")}
        assert cols == {
            "id",
            "review_id",
            "role",
            "position",
            "paragraph_anchor",
            "issue_type",
            "description",
            "suggestion",
        }
        pk = self.inspector.get_pk_constraint("review_issues")
        assert pk["constrained_columns"] == ["id"]
        fks = self.inspector.get_foreign_keys("review_issues")
        assert len(fks) == 1
        assert fks[0]["referred_table"] == "review_results"

    def test_approved_at_is_nullable(self) -> None:
        approved_at = next(
            c
            for c in self.inspector.get_columns("chapters")
            if c["name"] == "approved_at"
        )
        assert approved_at["nullable"] is True

    def test_review_score_is_nullable(self) -> None:
        review_score = next(
            c
            for c in self.inspector.get_columns("chapter_versions")
            if c["name"] == "review_score"
        )
        assert review_score["nullable"] is True


class TestDowngrade:
    def test_downgrade_drops_all_business_tables(self, tmp_db_path: Path) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        tables = _table_names(tmp_db_path)
        assert tables == ["alembic_version"]

    def test_upgrade_downgrade_upgrade_idempotent(self, tmp_db_path: Path) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
        tables = _table_names(tmp_db_path)
        assert "workspaces" in tables
        assert "chapter_versions" in tables


class TestOrmReadWrite:
    """端到端:upgrade 后用 ORM 写入并回读,确认 schema 真实可用。"""

    @pytest.fixture
    def session(self, tmp_db_path: Path) -> Session:
        command.upgrade(_alembic_config(), "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            yield session

    def test_workspace_round_trip(self, session: Session) -> None:
        now = datetime.now(timezone.utc)
        ws = WorkspaceORM(
            workspace_id="scifi_book_1",
            display_name="科幻 1 号",
            genre="scifi",
            premise="一句话设定",
            created_at=now,
            last_active_at=now,
            engine_version="0.1.0",
            status="active",
            total_chapters=10,
            completed_chapters=0,
            config_overrides={"foo": "bar"},
        )
        session.add(ws)
        session.commit()

        loaded = session.get(WorkspaceORM, "scifi_book_1")
        assert loaded is not None
        assert loaded.config_overrides == {"foo": "bar"}
        assert loaded.status == "active"

    def test_chapter_version_review_issue_full_chain(self, session: Session) -> None:
        now = datetime.now(timezone.utc)
        session.add(
            WorkspaceORM(
                workspace_id="ws1",
                display_name="ws1",
                genre="scifi",
                premise="x",
                created_at=now,
                last_active_at=now,
                engine_version="0.1.0",
                status="active",
                total_chapters=1,
                completed_chapters=0,
                config_overrides={},
            )
        )
        session.add(
            ChapterORM(
                chapter_id="v1c001",
                workspace_id="ws1",
                current_version=1,
                status="drafting",
                rewrite_count=0,
                revise_count=0,
                created_at=now,
                approved_at=None,
                total_token_usage={"in": 100, "out": 200},
                total_cost_usd=0.0123,
            )
        )
        session.add(
            ChapterVersionORM(
                chapter_id="v1c001",
                version_id=1,
                content="正文内容",
                word_count=4,
                created_at=now,
                creation_type="draft",
                model_used="claude-sonnet-4-6",
                review_score=None,
                notes="",
            )
        )
        session.add(
            ReviewResultORM(
                review_id="rev-abcd1234",
                chapter_id="v1c001",
                version_reviewed=1,
                reviewed_at=now,
                reviewer_model="claude-sonnet-4-6",
                score_plot_progression=8.0,
                score_tension_match=7.5,
                score_prose_quality=7.0,
                score_consistency=8.5,
                score_hook_management=7.8,
                score_voice_consistency=7.2,
                score_overall=7.66,
                hard_constraints_passed=True,
                failed_constraints=[],
                suggestions=["优化开头", "强化钩子"],
                verdict="revise",
            )
        )
        session.add_all(
            [
                ReviewIssueORM(
                    review_id="rev-abcd1234",
                    role="issue",
                    position=0,
                    paragraph_anchor="第一段前 30 字……",
                    issue_type="pacing",
                    description="开头过缓",
                    suggestion="删去前两段",
                ),
                ReviewIssueORM(
                    review_id="rev-abcd1234",
                    role="revise_target",
                    position=0,
                    paragraph_anchor="第一段前 30 字……",
                    issue_type="pacing",
                    description="开头过缓",
                    suggestion="删去前两段",
                ),
            ]
        )
        session.commit()

        chapter = session.get(ChapterORM, "v1c001")
        assert chapter is not None
        assert chapter.total_token_usage == {"in": 100, "out": 200}
        assert len(chapter.versions) == 1
        assert len(chapter.reviews) == 1

        review = chapter.reviews[0]
        assert review.suggestions == ["优化开头", "强化钩子"]
        assert review.verdict == "revise"
        roles = sorted(i.role for i in review.issues)
        assert roles == ["issue", "revise_target"]

    def test_chapter_versions_compound_pk_blocks_duplicate(
        self, session: Session
    ) -> None:
        from sqlalchemy.exc import IntegrityError

        now = datetime.now(timezone.utc)
        session.add(
            WorkspaceORM(
                workspace_id="ws1",
                display_name="x",
                genre="g",
                premise="p",
                created_at=now,
                last_active_at=now,
                engine_version="0.1.0",
                status="active",
                total_chapters=0,
                completed_chapters=0,
                config_overrides={},
            )
        )
        session.add(
            ChapterORM(
                chapter_id="c1",
                workspace_id="ws1",
                current_version=1,
                status="drafting",
                rewrite_count=0,
                revise_count=0,
                created_at=now,
                approved_at=None,
                total_token_usage={},
                total_cost_usd=0.0,
            )
        )
        session.add(
            ChapterVersionORM(
                chapter_id="c1",
                version_id=1,
                content="a",
                word_count=1,
                created_at=now,
                creation_type="draft",
                model_used="m",
                review_score=None,
                notes="",
            )
        )
        session.commit()

        session.add(
            ChapterVersionORM(
                chapter_id="c1",
                version_id=1,
                content="b",
                word_count=1,
                created_at=now,
                creation_type="rewrite",
                model_used="m",
                review_score=None,
                notes="",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
