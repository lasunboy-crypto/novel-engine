"""``src/services/asset_import.py`` 单测(P2-B-13)。

覆盖维度:

- 4 文件混合 fixture:完整入库 + counts 正确 + files_processed 列表正确
- 文件全缺失:counts 全 0,无报错
- 单文件存在:仅该类型 +1
- world.md 只入 Chroma 不入 SQLite(对齐 P2-B-09 决策 2)
- 同名条目幂等覆盖:第二次导入更新已存条目,SQLite 记录数不增
- Chroma id / metadata 五字段约定(对齐 P2-B-09)
- 解析错误:缺必填字段 / int 字段非整数 / 字段值不合 schema
- md_dir 不存在 / 非目录抛 ``AssetImportError``
- ``ensure_workspace_orm``:upsert 行为(create + update 路径)

为避免引 alembic command(沿用 ``test_memory_store.py`` 风格),用
``Base.metadata.create_all`` 直建 schema;Chroma 用 ``tmp_path / "chroma"``
+ 4 维 mock embedding fn 避免触发真模型加载。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.core.db import Base, WorkspaceORM
from src.core.memory.store import (
    list_characters_by_workspace,
    list_plot_threads_by_workspace,
    list_timeline_entries_by_workspace,
)
from src.core.memory.vector import VectorStore
from src.core.models.workspace import Workspace
from src.services.asset_import import (
    DEFAULT_COLLECTION,
    STATIC_CHAPTER_ID,
    AssetImportError,
    ImportAssetsResult,
    _parse_markdown_sections,
    _split_kv_dict,
    _split_list,
    ensure_workspace_orm,
    import_assets,
)


# ---- Fixtures ---------------------------------------------------------------


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
    db_path = tmp_path / "novel.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _seed_workspace(s, "ws-demo")
        yield s


def _mock_embedding_fn(texts: list[str]) -> list[list[float]]:
    """4 维兜底 embedding;P2-B-13 单测不验距离语义,只验落库。"""
    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


@pytest.fixture
def vector_store(tmp_path: Path) -> VectorStore:
    return VectorStore(
        "ws-demo",
        embedding_fn=_mock_embedding_fn,
        persist_dir=tmp_path / "chroma",
    )


# ---- 测试数据(完整 4 文件 fixture) ---------------------------------------


_CHARACTERS_MD = """\
# 角色清单

## 林衍

| 字段 | 值 |
|------|----|
| character_id | lin-yan |
| name | 林衍 |
| archetype | 萌新剑修 |
| appearance | 黑衣黑发 |
| personality | 沉静内敛 |
| background | 寒门出身,机缘获剑 |
| tone | 冷静 |
| sentence_style | 短句直白 |
| self_address | 我 |
| aliases | 衍儿,小林 |
| catchphrases | 剑随心动 |
| forbidden_phrases | 现代俚语 |
| addresses_others | hero_001:小子;villain_001:阁下 |
| relationships | hero_001:同门;villain_001:宿敌 |
| goals_long_term | 修剑成圣 |
| goals_short_term | 突破筑基 |
| abilities | 御剑诀,基础剑法 |
| secrets | 玉佩中藏剑魂 |
| first_appearance_chapter | ch-0001 |

## 苏离

| 字段 | 值 |
|------|----|
| character_id | su-li |
| name | 苏离 |
| archetype | 神秘前辈 |
| appearance | 白衣胜雪 |
| personality | 高冷 |
| background | 来历不明 |
| tone | 淡漠 |
| sentence_style | 长句修饰 |
| self_address | 本座 |
"""

_PLOT_THREADS_MD = """\
# 伏笔

## 玉佩之约

| 字段 | 值 |
|------|----|
| thread_id | yu-pei-zhi-yue |
| title | 玉佩之约 |
| status | planted |
| importance | 4 |
| planted_chapter | ch-0001 |
| description | 林衍获赠玉佩;来历未明 |
| related_characters | lin-yan,su-li |
| planned_payoff | ch-0050 |

## 神秘前辈身份

| 字段 | 值 |
|------|----|
| thread_id | mysterious-senior |
| title | 神秘前辈身份 |
| status | developing |
| importance | 5 |
| planted_chapter | ch-0002 |
| description | 苏离的真实身份与立场 |
"""

_TIMELINE_MD = """\
# 时间线

## 林衍上山

| 字段 | 值 |
|------|----|
| entry_id | lin-yan-shang-shan |
| chapter_id | ch-0001 |
| description | 林衍登青崖山求道 |
| time_label | 建元三年春 |
| time_order | 100 |
| involved_characters | lin-yan |

## 玉佩获赠

| 字段 | 值 |
|------|----|
| entry_id | yu-pei-shou-zeng |
| chapter_id | ch-0001 |
| description | 苏离将玉佩赠予林衍 |
| time_label | 建元三年春,夜 |
| time_order | 110 |
| involved_characters | lin-yan,su-li |
"""

_WORLD_MD = """\
# 世界观

## 修真等级体系

| 字段 | 值 |
|------|----|
| fact_id | xiu-zhen-deng-ji |
| description | 修真分九境,每三境为一阶,境界突破需吸纳天地灵气并悟道 |
| involved_characters | (无) |

## 青崖山地理

| 字段 | 值 |
|------|----|
| fact_id | qing-ya-shan-di-li |
| description | 青崖山位于东海之滨,主峰常年云雾缭绕,为剑修圣地 |
"""


def _write_full_fixture(md_dir: Path) -> None:
    md_dir.mkdir(parents=True, exist_ok=True)
    (md_dir / "characters.md").write_text(_CHARACTERS_MD, encoding="utf-8")
    (md_dir / "plot_threads.md").write_text(_PLOT_THREADS_MD, encoding="utf-8")
    (md_dir / "timeline.md").write_text(_TIMELINE_MD, encoding="utf-8")
    (md_dir / "world.md").write_text(_WORLD_MD, encoding="utf-8")


# ---- helper 单测 ------------------------------------------------------------


class TestParseMarkdownSections:
    def test_empty_file_returns_empty_list(self) -> None:
        assert _parse_markdown_sections("") == []

    def test_only_file_title_returns_empty(self) -> None:
        assert _parse_markdown_sections("# 只有文件标题\n\n纯文本") == []

    def test_section_with_table_parses_kv(self) -> None:
        text = """## 林衍

| 字段 | 值 |
|------|----|
| character_id | lin-yan |
| name | 林衍 |
"""
        sections = _parse_markdown_sections(text)
        assert len(sections) == 1
        assert sections[0]["_heading"] == "林衍"
        assert sections[0]["character_id"] == "lin-yan"
        assert sections[0]["name"] == "林衍"

    def test_table_separator_and_header_filtered(self) -> None:
        text = """## A

| 字段 | 值 |
|:-----|---:|
| somekey | somevalue |
"""
        sections = _parse_markdown_sections(text)
        assert len(sections) == 1
        # 表头 "字段|值" + 分隔行 ":---|---:" 都被过滤,只留 somekey=somevalue。
        assert sections[0] == {"_heading": "A", "somekey": "somevalue"}

    def test_data_row_not_misfiltered_as_header(self) -> None:
        """key="field" 但 value 不是字面 "value" → 视为合法数据行。"""
        text = """## A

| field | actual_data |
"""
        sections = _parse_markdown_sections(text)
        assert sections[0] == {"_heading": "A", "field": "actual_data"}

    def test_multiple_sections_split_by_heading(self) -> None:
        text = """## 角色 A

| k | v |
|---|---|
| character_id | a |

## 角色 B

| k | v |
|---|---|
| character_id | b |
"""
        sections = _parse_markdown_sections(text)
        assert len(sections) == 2
        assert sections[0]["_heading"] == "角色 A"
        assert sections[0]["character_id"] == "a"
        assert sections[1]["_heading"] == "角色 B"
        assert sections[1]["character_id"] == "b"


class TestSplitList:
    def test_comma_separated(self) -> None:
        assert _split_list("a,b,c") == ["a", "b", "c"]

    def test_strips_whitespace(self) -> None:
        assert _split_list(" a , b , c ") == ["a", "b", "c"]

    def test_empty_returns_empty(self) -> None:
        assert _split_list("") == []
        assert _split_list("   ") == []

    def test_placeholder_returns_empty(self) -> None:
        assert _split_list("(无)") == []
        assert _split_list("(空)") == []
        assert _split_list("-") == []


class TestSplitKvDict:
    def test_semi_separated_pairs(self) -> None:
        assert _split_kv_dict("k1:v1;k2:v2") == {"k1": "v1", "k2": "v2"}

    def test_strips_whitespace(self) -> None:
        assert _split_kv_dict(" k1 : v1 ; k2 : v2 ") == {
            "k1": "v1",
            "k2": "v2",
        }

    def test_empty_returns_empty(self) -> None:
        assert _split_kv_dict("") == {}
        assert _split_kv_dict("(无)") == {}

    def test_skips_malformed_pairs(self) -> None:
        # 缺冒号的 pair 跳过
        assert _split_kv_dict("k1:v1;malformed;k2:v2") == {
            "k1": "v1",
            "k2": "v2",
        }


# ---- import_assets 完整路径 ------------------------------------------------


class TestImportFullFixture:
    def test_returns_result(
        self, tmp_path: Path, session: Session, vector_store: VectorStore
    ) -> None:
        md_dir = tmp_path / "assets"
        _write_full_fixture(md_dir)
        result = import_assets(
            md_dir,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
        )
        assert isinstance(result, ImportAssetsResult)
        assert result.workspace_id == "ws-demo"
        assert result.counts == {
            "character": 2,
            "plot_thread": 2,
            "timeline": 2,
            "fact": 2,
        }
        assert set(result.files_processed) == {
            "characters.md",
            "plot_threads.md",
            "timeline.md",
            "world.md",
        }

    def test_persists_three_types_to_sqlite(
        self, tmp_path: Path, session: Session, vector_store: VectorStore
    ) -> None:
        md_dir = tmp_path / "assets"
        _write_full_fixture(md_dir)
        import_assets(
            md_dir,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
        )
        session.commit()

        chars = list_characters_by_workspace(session, "ws-demo")
        assert [c.character_id for c in chars] == ["lin-yan", "su-li"]
        assert chars[0].name == "林衍"
        assert chars[0].voice_profile.tone == "冷静"
        assert chars[0].voice_profile.self_address == "我"
        assert chars[0].aliases == ["衍儿", "小林"]
        assert chars[0].voice_profile.addresses_others == {
            "hero_001": "小子",
            "villain_001": "阁下",
        }
        assert chars[0].relationships == {
            "hero_001": "同门",
            "villain_001": "宿敌",
        }
        assert chars[0].abilities == ["御剑诀", "基础剑法"]
        assert chars[0].first_appearance_chapter == "ch-0001"

        threads = list_plot_threads_by_workspace(session, "ws-demo")
        assert {t.thread_id for t in threads} == {
            "yu-pei-zhi-yue",
            "mysterious-senior",
        }
        yu_pei = next(t for t in threads if t.thread_id == "yu-pei-zhi-yue")
        assert yu_pei.title == "玉佩之约"
        assert yu_pei.status == "planted"
        assert yu_pei.importance == 4
        assert yu_pei.related_characters == ["lin-yan", "su-li"]
        assert yu_pei.planned_payoff == "ch-0050"
        # service 自动填 created_at(用户文件不维护)
        assert yu_pei.created_at is not None

        timeline = list_timeline_entries_by_workspace(session, "ws-demo")
        assert [t.entry_id for t in timeline] == [
            "lin-yan-shang-shan",
            "yu-pei-shou-zeng",
        ]
        assert timeline[0].time_order == 100
        assert timeline[1].time_order == 110

    def test_facts_only_in_chroma(
        self, tmp_path: Path, session: Session, vector_store: VectorStore
    ) -> None:
        """fact 不入 SQLite(P2 不建 facts 表),但出现在 Chroma。"""
        md_dir = tmp_path / "assets"
        _write_full_fixture(md_dir)
        import_assets(
            md_dir,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
        )
        session.commit()
        # Chroma 总条目 = 2 character + 2 plot_thread + 2 timeline + 2 fact = 8
        assert vector_store.count(DEFAULT_COLLECTION) == 8

    def test_chroma_metadata_fields(
        self, tmp_path: Path, session: Session, vector_store: VectorStore
    ) -> None:
        md_dir = tmp_path / "assets"
        _write_full_fixture(md_dir)
        import_assets(
            md_dir,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
        )
        session.commit()

        hits = vector_store.query(
            DEFAULT_COLLECTION,
            query_text="任意",
            top_k=20,
        )
        assert len(hits) == 8
        for hit in hits:
            assert set(hit.metadata.keys()) == {
                "entity_type",
                "entity_id",
                "chapter_id",
                "workspace_id",
                "created_at",
            }
            assert hit.metadata["chapter_id"] == STATIC_CHAPTER_ID
            assert hit.metadata["workspace_id"] == "ws-demo"
        types_seen = {hit.metadata["entity_type"] for hit in hits}
        assert types_seen == {"character", "plot_thread", "timeline", "fact"}

    def test_chroma_id_convention(
        self, tmp_path: Path, session: Session, vector_store: VectorStore
    ) -> None:
        """``f"{entity_type}-{entity_id}"`` 形态(对齐 P2-B-09)。"""
        md_dir = tmp_path / "assets"
        _write_full_fixture(md_dir)
        import_assets(
            md_dir,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
        )
        session.commit()

        hits = vector_store.query(
            DEFAULT_COLLECTION, query_text="任意", top_k=20
        )
        ids = {hit.id for hit in hits}
        assert ids == {
            "character-lin-yan",
            "character-su-li",
            "plot_thread-yu-pei-zhi-yue",
            "plot_thread-mysterious-senior",
            "timeline-lin-yan-shang-shan",
            "timeline-yu-pei-shou-zeng",
            "fact-xiu-zhen-deng-ji",
            "fact-qing-ya-shan-di-li",
        }


# ---- 部分文件 / 空目录 -----------------------------------------------------


class TestPartialFixture:
    def test_no_files_returns_empty_counts(
        self, tmp_path: Path, session: Session, vector_store: VectorStore
    ) -> None:
        md_dir = tmp_path / "empty"
        md_dir.mkdir()
        result = import_assets(
            md_dir,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
        )
        session.commit()
        assert result.counts == {
            "character": 0,
            "plot_thread": 0,
            "timeline": 0,
            "fact": 0,
        }
        assert result.files_processed == []
        assert vector_store.count(DEFAULT_COLLECTION) == 0

    def test_only_characters_md(
        self, tmp_path: Path, session: Session, vector_store: VectorStore
    ) -> None:
        md_dir = tmp_path / "assets"
        md_dir.mkdir()
        (md_dir / "characters.md").write_text(_CHARACTERS_MD, encoding="utf-8")
        result = import_assets(
            md_dir,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
        )
        session.commit()
        assert result.counts["character"] == 2
        assert result.counts["plot_thread"] == 0
        assert result.counts["timeline"] == 0
        assert result.counts["fact"] == 0
        assert result.files_processed == ["characters.md"]

    def test_only_world_md_facts_in_chroma(
        self, tmp_path: Path, session: Session, vector_store: VectorStore
    ) -> None:
        md_dir = tmp_path / "assets"
        md_dir.mkdir()
        (md_dir / "world.md").write_text(_WORLD_MD, encoding="utf-8")
        result = import_assets(
            md_dir,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
        )
        session.commit()
        assert result.counts == {
            "character": 0,
            "plot_thread": 0,
            "timeline": 0,
            "fact": 2,
        }
        assert vector_store.count(DEFAULT_COLLECTION) == 2

    def test_empty_md_file_processed_zero_entries(
        self, tmp_path: Path, session: Session, vector_store: VectorStore
    ) -> None:
        """文件存在但无 ``##`` 节(只有文件级 ``# 标题``)→ 登记为 processed,
        贡献 0 条目。"""
        md_dir = tmp_path / "assets"
        md_dir.mkdir()
        (md_dir / "characters.md").write_text(
            "# 角色清单\n\n暂无内容\n", encoding="utf-8"
        )
        result = import_assets(
            md_dir,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
        )
        session.commit()
        assert result.counts["character"] == 0
        assert result.files_processed == ["characters.md"]


# ---- 同名条目幂等覆盖 ------------------------------------------------------


class TestIdempotentOverwrite:
    def test_second_import_updates_no_duplicates(
        self, tmp_path: Path, session: Session, vector_store: VectorStore
    ) -> None:
        md_dir = tmp_path / "assets"
        _write_full_fixture(md_dir)

        # 第一次导入
        import_assets(
            md_dir,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
        )
        session.commit()
        assert len(list_characters_by_workspace(session, "ws-demo")) == 2

        # 改写 characters.md(同 ID 不同 name + tone)
        modified = _CHARACTERS_MD.replace(
            "| name | 林衍 |", "| name | 林衍·改 |"
        ).replace("| tone | 冷静 |", "| tone | 凌厉 |")
        (md_dir / "characters.md").write_text(modified, encoding="utf-8")

        result2 = import_assets(
            md_dir,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
        )
        session.commit()
        assert result2.counts["character"] == 2

        chars = list_characters_by_workspace(session, "ws-demo")
        # SQLite 不增条目,数据已更新
        assert len(chars) == 2
        lin_yan = next(c for c in chars if c.character_id == "lin-yan")
        assert lin_yan.name == "林衍·改"
        assert lin_yan.voice_profile.tone == "凌厉"

    def test_chroma_upsert_no_duplicates(
        self, tmp_path: Path, session: Session, vector_store: VectorStore
    ) -> None:
        """Chroma id 同 ID 是 upsert(对齐 P2-B-09 拍板)。"""
        md_dir = tmp_path / "assets"
        _write_full_fixture(md_dir)
        import_assets(
            md_dir,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
        )
        session.commit()
        assert vector_store.count(DEFAULT_COLLECTION) == 8

        # 第二次导入同样 ID,Chroma 条目数应保持 8
        import_assets(
            md_dir,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
        )
        session.commit()
        assert vector_store.count(DEFAULT_COLLECTION) == 8

    def test_plot_thread_update_preserves_created_at(
        self, tmp_path: Path, session: Session, vector_store: VectorStore
    ) -> None:
        """plot_thread update 路径保留原 ``created_at`` 不被覆盖。"""
        md_dir = tmp_path / "assets"
        md_dir.mkdir()
        (md_dir / "plot_threads.md").write_text(
            _PLOT_THREADS_MD, encoding="utf-8"
        )

        import_assets(
            md_dir,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
        )
        session.commit()
        threads_v1 = list_plot_threads_by_workspace(session, "ws-demo")
        original_created_at = next(
            t.created_at for t in threads_v1 if t.thread_id == "yu-pei-zhi-yue"
        )

        # 第二次导入(改 description)
        modified = _PLOT_THREADS_MD.replace(
            "| description | 林衍获赠玉佩;来历未明 |",
            "| description | 玉佩内藏剑魂,来自上古 |",
        )
        (md_dir / "plot_threads.md").write_text(modified, encoding="utf-8")

        import_assets(
            md_dir,
            workspace_id="ws-demo",
            db_session=session,
            vector_store=vector_store,
        )
        session.commit()
        threads_v2 = list_plot_threads_by_workspace(session, "ws-demo")
        yu_pei = next(
            t for t in threads_v2 if t.thread_id == "yu-pei-zhi-yue"
        )
        assert yu_pei.description == "玉佩内藏剑魂,来自上古"
        # created_at 保留原值
        assert yu_pei.created_at == original_created_at


# ---- 解析错误 -------------------------------------------------------------


class TestParseErrors:
    def test_md_dir_not_exists(
        self, tmp_path: Path, session: Session, vector_store: VectorStore
    ) -> None:
        with pytest.raises(AssetImportError, match="md_dir 不是目录"):
            import_assets(
                tmp_path / "nope",
                workspace_id="ws-demo",
                db_session=session,
                vector_store=vector_store,
            )

    def test_md_dir_is_file(
        self, tmp_path: Path, session: Session, vector_store: VectorStore
    ) -> None:
        not_dir = tmp_path / "is_a_file.md"
        not_dir.write_text("x", encoding="utf-8")
        with pytest.raises(AssetImportError, match="md_dir 不是目录"):
            import_assets(
                not_dir,
                workspace_id="ws-demo",
                db_session=session,
                vector_store=vector_store,
            )

    def test_missing_required_field(
        self, tmp_path: Path, session: Session, vector_store: VectorStore
    ) -> None:
        md_dir = tmp_path / "assets"
        md_dir.mkdir()
        # characters.md 缺 archetype
        bad = """## 林衍

| 字段 | 值 |
|------|----|
| character_id | lin-yan |
| name | 林衍 |
| appearance | 黑衣 |
| personality | 沉静 |
| background | 寒门 |
| tone | 冷静 |
| sentence_style | 短句 |
| self_address | 我 |
"""
        (md_dir / "characters.md").write_text(bad, encoding="utf-8")
        with pytest.raises(AssetImportError, match="archetype"):
            import_assets(
                md_dir,
                workspace_id="ws-demo",
                db_session=session,
                vector_store=vector_store,
            )

    def test_int_field_not_integer(
        self, tmp_path: Path, session: Session, vector_store: VectorStore
    ) -> None:
        md_dir = tmp_path / "assets"
        md_dir.mkdir()
        bad = """## A

| 字段 | 值 |
|------|----|
| thread_id | t1 |
| status | planted |
| importance | NaN |
| planted_chapter | ch-0001 |
| description | x |
"""
        (md_dir / "plot_threads.md").write_text(bad, encoding="utf-8")
        with pytest.raises(
            AssetImportError, match="importance.*整数"
        ):
            import_assets(
                md_dir,
                workspace_id="ws-demo",
                db_session=session,
                vector_store=vector_store,
            )

    def test_field_value_violates_pydantic_schema(
        self, tmp_path: Path, session: Session, vector_store: VectorStore
    ) -> None:
        """``importance`` 超 1-5 范围 → Pydantic ``ValidationError`` 翻译为
        ``AssetImportError``。"""
        md_dir = tmp_path / "assets"
        md_dir.mkdir()
        bad = """## A

| 字段 | 值 |
|------|----|
| thread_id | t1 |
| status | planted |
| importance | 99 |
| planted_chapter | ch-0001 |
| description | x |
"""
        (md_dir / "plot_threads.md").write_text(bad, encoding="utf-8")
        with pytest.raises(AssetImportError, match="schema"):
            import_assets(
                md_dir,
                workspace_id="ws-demo",
                db_session=session,
                vector_store=vector_store,
            )

    def test_invalid_status_value(
        self, tmp_path: Path, session: Session, vector_store: VectorStore
    ) -> None:
        """``status`` 不在 ``planted/developing/resolved/abandoned`` 集合内
        → schema 错。"""
        md_dir = tmp_path / "assets"
        md_dir.mkdir()
        bad = """## A

| 字段 | 值 |
|------|----|
| thread_id | t1 |
| status | bogus |
| importance | 3 |
| planted_chapter | ch-0001 |
| description | x |
"""
        (md_dir / "plot_threads.md").write_text(bad, encoding="utf-8")
        with pytest.raises(AssetImportError, match="schema"):
            import_assets(
                md_dir,
                workspace_id="ws-demo",
                db_session=session,
                vector_store=vector_store,
            )


# ---- ensure_workspace_orm --------------------------------------------------


class TestEnsureWorkspaceOrm:
    @pytest.fixture
    def empty_session(self, tmp_path: Path) -> Session:
        """不预先 seed workspace 的空 session,验证 upsert create 路径。"""
        db_path = tmp_path / "novel.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        with Session(engine) as s:
            yield s

    def _make_workspace(
        self, *, workspace_id: str = "ws-new", display_name: str = "新工作区"
    ) -> Workspace:
        now = datetime.now(timezone.utc)
        return Workspace(
            workspace_id=workspace_id,
            display_name=display_name,
            genre="scifi",
            premise="x",
            created_at=now,
            last_active_at=now,
            engine_version="0.1.0",
            status="active",
            total_chapters=10,
            completed_chapters=2,
            config_overrides={},
        )

    def test_create_when_missing(self, empty_session: Session) -> None:
        ws = self._make_workspace(workspace_id="ws-new")
        ensure_workspace_orm(empty_session, ws)
        empty_session.commit()
        row = empty_session.get(WorkspaceORM, "ws-new")
        assert row is not None
        assert row.display_name == "新工作区"
        assert row.total_chapters == 10

    def test_update_when_existing(self, session: Session) -> None:
        # session fixture 已 seed "ws-demo",改 display_name 后 ensure 应更新。
        ws = self._make_workspace(
            workspace_id="ws-demo", display_name="改名后"
        )
        ensure_workspace_orm(session, ws)
        session.commit()
        row = session.get(WorkspaceORM, "ws-demo")
        assert row is not None
        assert row.display_name == "改名后"

    def test_idempotent_double_call(self, empty_session: Session) -> None:
        ws = self._make_workspace(workspace_id="ws-new")
        ensure_workspace_orm(empty_session, ws)
        ensure_workspace_orm(empty_session, ws)
        empty_session.commit()
        # 仍然只有一行
        from sqlalchemy import select

        rows = empty_session.scalars(select(WorkspaceORM)).all()
        assert len(rows) == 1
