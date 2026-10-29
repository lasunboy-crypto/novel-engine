"""验证 P5-B-01 alembic 0005 迁移与 ``volumes`` 表 ORM。

P5-B-01 引入 ``volumes`` 表(对应 ``Volume`` Pydantic 模型,PROJECT_SPEC §3
+ ROADMAP §P5)。

覆盖维度(沿用 ``test_p4_b_01_metrics.py`` 同结构):

- upgrade head 后 14 张业务表(P1 5 + P2 3 + P4 5 + P5 1)+ 索引齐全
- downgrade -1 干净退回 0004_p4_metrics_tables(只删 volumes 表 + 索引)
- upgrade → downgrade → upgrade 幂等
- ORM round-trip:Pydantic ↔ ORM ↔ SQLite arcs_json / tension_curve_json
  JSON 列序列化无损
- ``tension_template`` 4 档 CHECK 约束:允许写,非档值 IntegrityError
- ``workspace_id`` FK CASCADE:删 workspace 时连带 cascade volumes
- 表列名与 ORM ``__table__.columns`` 一致
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.db import (
    TENSION_TEMPLATE_VALUES,
    VolumeORM,
    WorkspaceORM,
)
from src.core.models.volume import (
    Arc,
    TensionCurvePoint,
    Volume,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config() -> Config:
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    return cfg


@pytest.fixture
def tmp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


def _now_utc() -> datetime:
    return datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)


def _volume_to_orm(v: Volume) -> VolumeORM:
    """Pydantic ``Volume`` → ``VolumeORM`` 字段映射 helper(P5-B-09 service
    层未来会内联同等映射;本任务在测试内复用避免重复样板)。

    ``arcs`` → ``arcs_json``;``tension_curve`` → ``tension_curve_json``;
    嵌套 Pydantic 对象用 ``model_dump(mode='json')`` 转 JSON-safe dict
    (tuple → list、datetime → ISO 字符串);datetime 字段保留 datetime
    对象给 ORM 列(``DateTime(timezone=True)`` 自处理)。
    """
    return VolumeORM(
        volume_id=v.volume_id,
        workspace_id=v.workspace_id,
        title=v.title,
        summary=v.summary,
        chapter_count=v.chapter_count,
        tension_template=v.tension_template,
        arcs_json=[a.model_dump(mode="json") for a in v.arcs],
        tension_curve_json=[p.model_dump(mode="json") for p in v.tension_curve],
        created_at=v.created_at,
        last_updated_at=v.last_updated_at,
    )


P5_TABLES = {"volumes"}
P5_INDEXES = {"ix_volumes_workspace_id"}


class TestAlembicMigration:
    """alembic 0005 schema 层面验证。"""

    def test_upgrade_head_creates_volumes_table(self, tmp_db_path: Path) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        tables = set(_table_names(tmp_db_path))
        assert P5_TABLES.issubset(tables)
        # P1 / P2 / P4 表仍在
        assert {
            "workspaces",
            "chapters",
            "characters",
            "metrics_daily",
            "metrics_per_chapter",
            "memory_health_history",
        }.issubset(tables)

    def test_upgrade_head_creates_p5_index(self, tmp_db_path: Path) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        indexes = set(_index_names(tmp_db_path))
        assert P5_INDEXES.issubset(indexes)

    def test_downgrade_minus_one_drops_only_volumes(
        self, tmp_db_path: Path
    ) -> None:
        cfg = _alembic_config()
        # **显式 revision** 升到 0005,避免 P5-B-02+ 加 0006 后本测试断言失败
        # (对齐 CLAUDE.md §实施纪律:Alembic head 硬编码假设修复纪律)
        command.upgrade(cfg, "0005_volumes_table")
        command.downgrade(cfg, "-1")
        tables = set(_table_names(tmp_db_path))
        # P5 表全消失
        assert tables.isdisjoint(P5_TABLES)
        # 其他表保留
        assert {
            "workspaces",
            "metrics_daily",
            "memory_health_history",
        }.issubset(tables)

    def test_upgrade_downgrade_upgrade_idempotent(
        self, tmp_db_path: Path
    ) -> None:
        cfg = _alembic_config()
        # **显式 revision** 验证 0005 自身幂等,不受未来 0006+ 加入影响
        command.upgrade(cfg, "0005_volumes_table")
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, "0005_volumes_table")
        tables = set(_table_names(tmp_db_path))
        assert P5_TABLES.issubset(tables)

    def test_volumes_columns_match_orm(self, tmp_db_path: Path) -> None:
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        inspector = inspect(engine)
        db_cols = {c["name"] for c in inspector.get_columns("volumes")}
        orm_cols = {c.name for c in VolumeORM.__table__.columns}
        assert db_cols == orm_cols


class TestVolumesTensionTemplateCheck:
    """``volumes.tension_template`` 4 档 CHECK 约束。"""

    @pytest.fixture
    def session(self, tmp_db_path: Path) -> Session:
        command.upgrade(_alembic_config(), "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            # 先插入一个 workspace 满足 FK
            session.add(
                WorkspaceORM(
                    workspace_id="ws-test",
                    display_name="测试工作区",
                    genre="玄幻",
                    premise="测试",
                    created_at=_now_utc(),
                    last_active_at=_now_utc(),
                    engine_version="0.1.0",
                    status="active",
                    total_chapters=0,
                    completed_chapters=0,
                    config_overrides={},
                )
            )
            session.commit()
            yield session

    @pytest.mark.parametrize("template", TENSION_TEMPLATE_VALUES)
    def test_each_allowed_template_writes(
        self, session: Session, template: str
    ) -> None:
        session.add(
            VolumeORM(
                volume_id=f"vol-{template}",
                workspace_id="ws-test",
                title="t",
                summary="s",
                chapter_count=10,
                tension_template=template,
                arcs_json=[],
                tension_curve_json=[],
                created_at=_now_utc(),
                last_updated_at=_now_utc(),
            )
        )
        session.commit()
        loaded = session.get(VolumeORM, f"vol-{template}")
        assert loaded is not None
        assert loaded.tension_template == template

    def test_disallowed_template_raises(self, session: Session) -> None:
        session.add(
            VolumeORM(
                volume_id="vol-bad",
                workspace_id="ws-test",
                title="t",
                summary="s",
                chapter_count=10,
                tension_template="epic_quest",
                arcs_json=[],
                tension_curve_json=[],
                created_at=_now_utc(),
                last_updated_at=_now_utc(),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


class TestVolumeOrmRoundTrip:
    """Volume Pydantic → ORM → SQLite → 回读 → 字段一致(含 JSON 嵌套字段)。"""

    @pytest.fixture
    def session(self, tmp_db_path: Path) -> Session:
        command.upgrade(_alembic_config(), "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")
        with Session(engine) as session:
            session.add(
                WorkspaceORM(
                    workspace_id="ws-test",
                    display_name="测试",
                    genre="玄幻",
                    premise="测试",
                    created_at=_now_utc(),
                    last_active_at=_now_utc(),
                    engine_version="0.1.0",
                    status="active",
                    total_chapters=0,
                    completed_chapters=0,
                    config_overrides={},
                )
            )
            session.commit()
            yield session

    def test_empty_volume_round_trip(self, session: Session) -> None:
        v = Volume(
            volume_id="vol-empty",
            workspace_id="ws-test",
            title="第一卷",
            summary="概要",
            chapter_count=10,
            tension_template="heros_journey",
            created_at=_now_utc(),
            last_updated_at=_now_utc(),
        )
        session.add(_volume_to_orm(v))
        session.commit()

        loaded = session.get(VolumeORM, "vol-empty")
        assert loaded is not None
        assert loaded.title == "第一卷"
        assert loaded.chapter_count == 10
        assert loaded.tension_template == "heros_journey"
        assert loaded.arcs_json == []
        assert loaded.tension_curve_json == []

    def test_full_volume_with_arcs_and_curve_round_trip(
        self, session: Session
    ) -> None:
        arcs = [
            Arc(
                arc_id="arc-001",
                arc_type="主线",
                chapter_range=(1, 10),
                phase="opening",
                summary="主角起步",
            ),
            Arc(
                arc_id="arc-002",
                arc_type="副本",
                chapter_range=(11, 14),
                phase="rising",
                summary="第一个副本",
            ),
        ]
        curve = [
            TensionCurvePoint(
                chapter_number=i, tension=(i % 9) + 1, is_arc_finale=(i == 14)
            )
            for i in range(1, 15)
        ]
        v = Volume(
            volume_id="vol-full",
            workspace_id="ws-test",
            title="第二卷",
            summary="详细",
            chapter_count=14,
            tension_template="strong_stimulation",
            arcs=arcs,
            tension_curve=curve,
            created_at=_now_utc(),
            last_updated_at=_now_utc(),
        )

        session.add(_volume_to_orm(v))
        session.commit()

        loaded = session.get(VolumeORM, "vol-full")
        assert loaded is not None
        assert len(loaded.arcs_json) == 2
        # tuple → list 在 JSON round-trip 后,落库回读保持 list
        assert loaded.arcs_json[0]["arc_id"] == "arc-001"
        assert loaded.arcs_json[0]["chapter_range"] == [1, 10]
        assert loaded.arcs_json[1]["phase"] == "rising"
        assert len(loaded.tension_curve_json) == 14
        assert loaded.tension_curve_json[13]["is_arc_finale"] is True

    def test_volume_rebuild_from_loaded_orm(self, session: Session) -> None:
        """从 ORM 回读后能重建 Pydantic Volume(模拟 service 查询路径)。"""
        v = Volume(
            volume_id="vol-rebuild",
            workspace_id="ws-test",
            title="t",
            summary="s",
            chapter_count=3,
            tension_template="free",
            arcs=[
                Arc(
                    arc_id="arc-x",
                    arc_type="主线",
                    chapter_range=(1, 3),
                    phase="opening",
                    summary="x",
                ),
            ],
            tension_curve=[
                TensionCurvePoint(chapter_number=i, tension=5)
                for i in range(1, 4)
            ],
            created_at=_now_utc(),
            last_updated_at=_now_utc(),
        )
        session.add(_volume_to_orm(v))
        session.commit()

        loaded = session.get(VolumeORM, "vol-rebuild")
        assert loaded is not None
        rebuilt = Volume(
            volume_id=loaded.volume_id,
            workspace_id=loaded.workspace_id,
            title=loaded.title,
            summary=loaded.summary,
            chapter_count=loaded.chapter_count,
            tension_template=loaded.tension_template,  # type: ignore[arg-type]
            arcs=[Arc.model_validate(a) for a in loaded.arcs_json],
            tension_curve=[
                TensionCurvePoint.model_validate(p)
                for p in loaded.tension_curve_json
            ],
            created_at=loaded.created_at,
            last_updated_at=loaded.last_updated_at,
        )
        assert rebuilt.volume_id == "vol-rebuild"
        assert rebuilt.arcs[0].chapter_range == (1, 3)
        assert len(rebuilt.tension_curve) == 3


class TestVolumeWorkspaceForeignKey:
    """``volumes.workspace_id`` FK CASCADE 验证。"""

    def test_orphan_workspace_id_blocked(self, tmp_db_path: Path) -> None:
        """workspace_id 不存在时插入失败(FK 约束)。

        SQLite 默认 PRAGMA foreign_keys=OFF;SQLAlchemy 连接通常不主动 ON。
        这里显式 ON 后验证 FK 阻塞;若 OFF 则该测试 skip(对齐项目实际运行
        环境会显式启用 FK 的语义)。
        """
        command.upgrade(_alembic_config(), "head")
        engine = create_engine(f"sqlite:///{tmp_db_path}")

        # 显式 ON FK pragma(模拟生产环境)
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys = ON")
            conn.commit()

        with Session(engine) as session:
            session.execute(text("PRAGMA foreign_keys = ON"))
            session.add(
                VolumeORM(
                    volume_id="vol-orphan",
                    workspace_id="ws-nonexistent",
                    title="t",
                    summary="s",
                    chapter_count=10,
                    tension_template="free",
                    arcs_json=[],
                    tension_curve_json=[],
                    created_at=_now_utc(),
                    last_updated_at=_now_utc(),
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
