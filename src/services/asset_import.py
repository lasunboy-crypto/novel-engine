"""静态资产导入服务(P2-B-13)。

读取 ``md_dir`` 下 4 个可选 Markdown 文件,按"## 小节标题 + 字段表"约定
解析,落 SQLite + Chroma:

- ``characters.md``:每节一个 ``Character``,入 SQLite + Chroma。
- ``plot_threads.md``:每节一个 ``PlotThread``,入 SQLite + Chroma。
- ``timeline.md``:每节一个 ``TimelineEntry``,入 SQLite + Chroma。
- ``world.md``:每节一个语义 ``fact``,**只入 Chroma**(对齐 P2-B-09 决策
  2:P2 阶段无 facts 表)。

同名条目幂等覆盖:SQLite 走 ``get-or-update-or-create``,Chroma 同 ID
``add`` 行为是 upsert。

设计要点
========

1. **入参与 ``MemoryExtractor`` 对偶**:``import_assets(md_dir, *,
   workspace_id, db_session, vector_store)``。``Session`` / ``VectorStore``
   由 caller 注入,事务边界归 caller(本服务 ``flush`` 不 ``commit``)。
2. **文件全可选**:任何一个文件不存在则静默跳过该类型。``md_dir`` 自身
   不存在 / 非目录抛 ``AssetImportError``。
3. **解析失败抛 ``AssetImportError``**:缺必填字段 / int 字段非整数 / 字段
   值不合 Pydantic schema 都抛(对齐 ``MemoryExtractorError`` 形态),
   caller 看到错误回滚事务即可,无残留。
4. **Chroma 五字段 metadata**(对齐 P2-B-09 拍板):``entity_type`` /
   ``entity_id`` / ``chapter_id`` / ``workspace_id`` / ``created_at``。静
   态资产无章节归属,``chapter_id`` 一律填 ``"__static__"`` 占位(下游
   retriever 调用方按 ``where`` 子句区分;静态资产可作为持久背景始终参与
   检索)。
5. **写入顺序:先 SQLite ``flush`` 再 Chroma ``add``**——任一类型 SQLite
   失败时 Chroma 还没动,caller rollback 干净;Chroma ``add`` 失败时
   SQLite 还在 caller 事务里没 commit。
6. **WorkspaceORM 必须已存在**:本服务不 sync ``meta.json`` ↔ ``WorkspaceORM``,
   caller 责任(CLI handler 在调本服务前调 ``ensure_workspace_orm``)。

边界
====

- ``md_dir`` 下 4 个文件全缺失 → 返回 ``ImportAssetsResult`` ``counts`` 全 0,
  ``files_processed`` 空列表。不抛错(等同"无资产可导")。
- 文件存在但无 ``## ...`` 节(只有文件级 ``# 标题`` 或纯文本)→ 该文件
  ``files_processed`` 登记,贡献 0 条目。
- LLM **不参与**:纯文件解析 + 入库,符合 CLAUDE.md §维护人环境约束
  (P1-P2 默认 mock,本任务无需 LLM)。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from src.core.db import WorkspaceORM
from src.core.memory.store import (
    create_character,
    create_plot_thread,
    create_timeline_entry,
    get_character,
    get_plot_thread,
    get_timeline_entry,
    update_character,
    update_plot_thread,
    update_timeline_entry,
)
from src.core.memory.vector import VectorStore
from src.core.models.character import Character, VoiceProfile
from src.core.models.plot_thread import PlotThread
from src.core.models.timeline_entry import TimelineEntry
from src.core.models.workspace import Workspace

_log = structlog.get_logger(__name__)

# Chroma collection 名,对齐 P2-B-08 retriever ``DEFAULT_COLLECTION``。
DEFAULT_COLLECTION = "memories"

# 静态资产 metadata 中 ``chapter_id`` 占位值——静态资产无章节归属。
STATIC_CHAPTER_ID = "__static__"

# 文件名约定。
CHARACTERS_FILENAME = "characters.md"
PLOT_THREADS_FILENAME = "plot_threads.md"
TIMELINE_FILENAME = "timeline.md"
WORLD_FILENAME = "world.md"


class AssetImportError(ValueError):
    """资产导入失败时抛出。

    主要触发场景:``md_dir`` 不存在 / 必填字段缺失 / int 字段格式错 / 字段值
    不合 Pydantic schema(``ValidationError`` 翻译)。caller 看到错误回滚
    事务即可,无残留。
    """


class ImportedFact(BaseModel):
    """``world.md`` 抽出的语义记忆(只入 Chroma,沿用 P2-B-09 ``FactEntry`` 形态)。"""

    fact_id: str
    description: str
    involved_characters: list[str] = Field(default_factory=list)


class ImportAssetsResult(BaseModel):
    """``import_assets`` 出参。

    汇总四类导入条目数 + 处理过的文件名;调用方(CLI / 未来 API)按需展示。
    """

    workspace_id: str
    files_processed: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


# ---- Markdown 解析 helpers --------------------------------------------------

_HEADING_LINE_RE = re.compile(r"^##\s+(.+?)\s*$")
"""``## <heading>``,heading 可含空格 / 中文标点;锚点 ``{#id}`` 不剥(显示
名段允许带,parser 不依赖 heading 取 ID,ID 由表内 ``*_id`` 字段提供)。"""

_TABLE_ROW_RE = re.compile(r"^\s*\|(.*)\|\s*$")
"""标准 markdown 管道表行;两端 ``|`` 必有,中间内容由 ``|`` 分隔。"""

_TABLE_HEADER_PAIRS: set[tuple[str, str]] = {
    ("字段", "值"),
    ("field", "value"),
    ("key", "value"),
    ("name", "value"),
}
"""表头行的 ``(key, value)`` 字面集合。**两端**都匹配才视为表头(避免把
形如 ``| field | actual_data |`` 这样合法但 key 是字面 "field" 的数据行
误判)。"""


def _parse_markdown_sections(text: str) -> list[dict[str, str]]:
    """把 markdown 文件解析成 ``[{字段: 值, ..., "_heading": "..."}]``。

    每个 ``## <heading>`` 起一个 section,后续紧跟管道表的 ``| key | value |``
    行被收集到该 section 的 dict 里;表头行(``| 字段 | 值 |``)与分隔行
    (``|---|---|``)被过滤。

    无 ``##`` 节(只有文件级 ``# 标题`` 或纯文本)→ 返回 ``[]``。
    """
    sections: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    for line in text.splitlines():
        heading_match = _HEADING_LINE_RE.match(line)
        if heading_match is not None:
            if current is not None:
                sections.append(current)
            current = {"_heading": heading_match.group(1).strip()}
            continue

        if current is None:
            continue

        row_match = _TABLE_ROW_RE.match(line)
        if row_match is None:
            continue

        cells = [c.strip() for c in row_match.group(1).split("|")]
        if len(cells) != 2:
            continue
        key, value = cells
        if not key:
            continue
        # 表分隔符行:``| --- | --- |`` 被切成 ``["---", "---"]``。
        if all(ch in {"-", ":", " "} for ch in key):
            continue
        # 表头行:``| 字段 | 值 |`` 等;**key + value 同时匹配**才认。
        if (key.lower(), value.lower()) in _TABLE_HEADER_PAIRS:
            continue
        current[key] = value

    if current is not None:
        sections.append(current)
    return sections


def _split_list(value: str, delim: str = ",") -> list[str]:
    """逗号分隔 → 列表;空值 / 占位符(``(无)`` / ``(空)`` / ``-``)→ ``[]``。"""
    stripped = value.strip()
    if not stripped or stripped in {"(无)", "(空)", "-"}:
        return []
    return [item.strip() for item in stripped.split(delim) if item.strip()]


def _split_kv_dict(value: str) -> dict[str, str]:
    """``k1:v1;k2:v2`` → ``{"k1": "v1", "k2": "v2"}``;空值返 ``{}``。"""
    stripped = value.strip()
    if not stripped or stripped in {"(无)", "(空)", "-"}:
        return {}
    result: dict[str, str] = {}
    for pair in stripped.split(";"):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        key, val = pair.split(":", 1)
        if key.strip():
            result[key.strip()] = val.strip()
    return result


def _require(
    section: dict[str, str], key: str, *, file: str, heading: str
) -> str:
    """取必填字段;缺失 / 空白抛 ``AssetImportError`` 带文件名 + heading 定位。"""
    raw = section.get(key, "")
    val = raw.strip()
    if not val:
        raise AssetImportError(
            f"{file} '## {heading}' 段缺必填字段 '{key}'"
        )
    return val


def _parse_int(
    value: str, *, file: str, heading: str, key: str
) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise AssetImportError(
            f"{file} '## {heading}' 段字段 '{key}' 必须是整数,实际 {value!r}"
        ) from exc


def _validation_error_to_asset_import_error(
    exc: ValidationError, *, file: str, heading: str
) -> AssetImportError:
    """``ValidationError`` 翻译成 ``AssetImportError``,保留 schema 错误信息。"""
    return AssetImportError(
        f"{file} '## {heading}' 段不符合 Pydantic schema: {exc.errors()}"
    )


# ---- 实体构造 ----------------------------------------------------------------


def _build_character(
    section: dict[str, str], *, workspace_id: str, file: str
) -> Character:
    """``characters.md`` 一节 → ``Character``。

    ``voice_profile`` 由表内扁平的 ``tone`` / ``sentence_style`` /
    ``self_address`` / ``catchphrases`` / ``addresses_others`` /
    ``forbidden_phrases`` 字段组装(P2-B-13 简版,无嵌套表);``current_state``
    导入阶段一律置空(运行时态,P2-B-09 ``MemoryExtractor`` 后续维护)。
    """
    heading = section.get("_heading", "(unnamed)")
    try:
        voice = VoiceProfile(
            catchphrases=_split_list(section.get("catchphrases", "")),
            tone=_require(section, "tone", file=file, heading=heading),
            sentence_style=_require(
                section, "sentence_style", file=file, heading=heading
            ),
            self_address=_require(
                section, "self_address", file=file, heading=heading
            ),
            addresses_others=_split_kv_dict(section.get("addresses_others", "")),
            forbidden_phrases=_split_list(section.get("forbidden_phrases", "")),
        )
        return Character(
            character_id=_require(
                section, "character_id", file=file, heading=heading
            ),
            workspace_id=workspace_id,
            name=section.get("name", "").strip() or heading,
            aliases=_split_list(section.get("aliases", "")),
            archetype=_require(
                section, "archetype", file=file, heading=heading
            ),
            appearance=_require(
                section, "appearance", file=file, heading=heading
            ),
            personality=_require(
                section, "personality", file=file, heading=heading
            ),
            background=_require(
                section, "background", file=file, heading=heading
            ),
            voice_profile=voice,
            goals_long_term=_split_list(section.get("goals_long_term", "")),
            goals_short_term=_split_list(section.get("goals_short_term", "")),
            current_state={},
            relationships=_split_kv_dict(section.get("relationships", "")),
            abilities=_split_list(section.get("abilities", "")),
            secrets=_split_list(section.get("secrets", "")),
            first_appearance_chapter=section.get(
                "first_appearance_chapter", ""
            ).strip()
            or None,
            last_updated_chapter=section.get(
                "last_updated_chapter", ""
            ).strip()
            or None,
        )
    except ValidationError as exc:
        raise _validation_error_to_asset_import_error(
            exc, file=file, heading=heading
        ) from exc


def _build_plot_thread(
    section: dict[str, str], *, workspace_id: str, file: str
) -> PlotThread:
    """``plot_threads.md`` 一节 → ``PlotThread``。

    ``created_at`` 由本服务自动填 ``datetime.now(UTC)``——用户文件不需手填;
    update 路径下 store 层会保留原值不覆盖(走 ``model_copy``)。
    """
    heading = section.get("_heading", "(unnamed)")
    try:
        return PlotThread(
            thread_id=_require(
                section, "thread_id", file=file, heading=heading
            ),
            workspace_id=workspace_id,
            title=section.get("title", "").strip() or heading,
            status=_require(
                section, "status", file=file, heading=heading
            ),  # type: ignore[arg-type]
            importance=_parse_int(
                _require(section, "importance", file=file, heading=heading),
                file=file,
                heading=heading,
                key="importance",
            ),
            planted_chapter=_require(
                section, "planted_chapter", file=file, heading=heading
            ),
            planned_payoff=section.get("planned_payoff", "").strip() or None,
            actual_payoff=section.get("actual_payoff", "").strip() or None,
            description=_require(
                section, "description", file=file, heading=heading
            ),
            related_characters=_split_list(
                section.get("related_characters", "")
            ),
            created_at=datetime.now(UTC),
            last_updated_chapter=section.get(
                "last_updated_chapter", ""
            ).strip()
            or None,
        )
    except ValidationError as exc:
        raise _validation_error_to_asset_import_error(
            exc, file=file, heading=heading
        ) from exc


def _build_timeline_entry(
    section: dict[str, str], *, workspace_id: str, file: str
) -> TimelineEntry:
    """``timeline.md`` 一节 → ``TimelineEntry``。"""
    heading = section.get("_heading", "(unnamed)")
    try:
        return TimelineEntry(
            entry_id=_require(
                section, "entry_id", file=file, heading=heading
            ),
            workspace_id=workspace_id,
            chapter_id=_require(
                section, "chapter_id", file=file, heading=heading
            ),
            description=_require(
                section, "description", file=file, heading=heading
            ),
            time_label=_require(
                section, "time_label", file=file, heading=heading
            ),
            time_order=_parse_int(
                _require(
                    section, "time_order", file=file, heading=heading
                ),
                file=file,
                heading=heading,
                key="time_order",
            ),
            involved_characters=_split_list(
                section.get("involved_characters", "")
            ),
        )
    except ValidationError as exc:
        raise _validation_error_to_asset_import_error(
            exc, file=file, heading=heading
        ) from exc


def _build_fact(section: dict[str, str], *, file: str) -> ImportedFact:
    """``world.md`` 一节 → ``ImportedFact``(只入 Chroma,P2 不建 facts 表)。"""
    heading = section.get("_heading", "(unnamed)")
    try:
        return ImportedFact(
            fact_id=_require(
                section, "fact_id", file=file, heading=heading
            ),
            description=_require(
                section, "description", file=file, heading=heading
            ),
            involved_characters=_split_list(
                section.get("involved_characters", "")
            ),
        )
    except ValidationError as exc:
        raise _validation_error_to_asset_import_error(
            exc, file=file, heading=heading
        ) from exc


# ---- Chroma 写入 ------------------------------------------------------------


def _compose_character_doc(ch: Character) -> str:
    """角色向量 document 文本(对齐 P2-B-09 ``_compose_character_doc`` 同款)。"""
    parts = [ch.name]
    if ch.archetype:
        parts.append(ch.archetype)
    if ch.personality:
        parts.append(ch.personality)
    if ch.background:
        parts.append(ch.background)
    return " | ".join(parts)


def _compose_plot_thread_doc(th: PlotThread) -> str:
    return f"{th.title} | {th.description}"


def _compose_timeline_doc(tl: TimelineEntry) -> str:
    return f"{tl.description} | {tl.time_label}"


def _make_metadata(
    *,
    entity_type: str,
    entity_id: str,
    workspace_id: str,
    created_at: str,
) -> dict[str, Any]:
    """五字段 metadata(对齐 P2-B-09 拍板)。

    静态资产无章节归属,``chapter_id`` 用 ``__static__`` 占位;调用方按
    ``where={"chapter_id": "__static__"}`` 子句过滤纯静态资产记忆。
    """
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "chapter_id": STATIC_CHAPTER_ID,
        "workspace_id": workspace_id,
        "created_at": created_at,
    }


def _persist_to_vector_store(
    *,
    vector_store: VectorStore,
    workspace_id: str,
    characters: list[Character],
    plot_threads: list[PlotThread],
    timeline: list[TimelineEntry],
    facts: list[ImportedFact],
) -> None:
    """四类合并批量写 Chroma(对齐 P2-B-09 ``_persist_to_vector_store`` 形态)。

    Chroma id = ``f"{entity_type}-{entity_id}"``;同 ID 行为是 upsert,
    天然实现"同名条目幂等覆盖"。
    """
    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict[str, Any]] = []
    created_at_iso = datetime.now(UTC).isoformat()

    for ch in characters:
        ids.append(f"character-{ch.character_id}")
        documents.append(_compose_character_doc(ch))
        metadatas.append(
            _make_metadata(
                entity_type="character",
                entity_id=ch.character_id,
                workspace_id=workspace_id,
                created_at=created_at_iso,
            )
        )
    for th in plot_threads:
        ids.append(f"plot_thread-{th.thread_id}")
        documents.append(_compose_plot_thread_doc(th))
        metadatas.append(
            _make_metadata(
                entity_type="plot_thread",
                entity_id=th.thread_id,
                workspace_id=workspace_id,
                created_at=created_at_iso,
            )
        )
    for tl in timeline:
        ids.append(f"timeline-{tl.entry_id}")
        documents.append(_compose_timeline_doc(tl))
        metadatas.append(
            _make_metadata(
                entity_type="timeline",
                entity_id=tl.entry_id,
                workspace_id=workspace_id,
                created_at=created_at_iso,
            )
        )
    for fa in facts:
        ids.append(f"fact-{fa.fact_id}")
        documents.append(fa.description)
        metadatas.append(
            _make_metadata(
                entity_type="fact",
                entity_id=fa.fact_id,
                workspace_id=workspace_id,
                created_at=created_at_iso,
            )
        )

    if not ids:
        return
    vector_store.add(
        DEFAULT_COLLECTION,
        ids=ids,
        documents=documents,
        metadatas=metadatas,
    )


# ---- 工作区 ORM bootstrap ---------------------------------------------------


def ensure_workspace_orm(session: Session, workspace: Workspace) -> None:
    """把 ``meta.json`` 反映的工作区元数据 sync 到 ``WorkspaceORM`` 行
    (idempotent upsert)。

    P2 阶段 ``WorkspaceService.create`` 只写 ``meta.json``,未触达 SQLite;
    ``Character`` / ``PlotThread`` / ``TimelineEntry`` 三表 FK 指向
    ``workspaces`` 表,SQLite 默认 ``PRAGMA foreign_keys=OFF`` 时空 FK 不
    报错,但语义上需要 WorkspaceORM 行存在。本 helper 在 ``import-assets``
    bootstrap 时填补这条 sync(P2-B-14 Characters CRUD 起 API 路由层会复用)。

    本函数 ``flush`` 不 ``commit``,事务边界归 caller。
    """
    existing = session.get(WorkspaceORM, workspace.workspace_id)
    if existing is None:
        session.add(
            WorkspaceORM(
                workspace_id=workspace.workspace_id,
                display_name=workspace.display_name,
                genre=workspace.genre,
                premise=workspace.premise,
                created_at=workspace.created_at,
                last_active_at=workspace.last_active_at,
                engine_version=workspace.engine_version,
                status=workspace.status,
                total_chapters=workspace.total_chapters,
                completed_chapters=workspace.completed_chapters,
                config_overrides=dict(workspace.config_overrides),
                default_voice=workspace.default_voice,
            )
        )
    else:
        existing.display_name = workspace.display_name
        existing.genre = workspace.genre
        existing.premise = workspace.premise
        existing.last_active_at = workspace.last_active_at
        existing.engine_version = workspace.engine_version
        existing.status = workspace.status
        existing.total_chapters = workspace.total_chapters
        existing.completed_chapters = workspace.completed_chapters
        existing.config_overrides = dict(workspace.config_overrides)
        existing.default_voice = workspace.default_voice
    session.flush()


# ---- 主入口 ----------------------------------------------------------------


def import_assets(
    md_dir: Path,
    *,
    workspace_id: str,
    db_session: Session,
    vector_store: VectorStore,
) -> ImportAssetsResult:
    """从 ``md_dir`` 解析 4 个静态资产文件并入库。

    流程:
    1. ``md_dir`` 不存在 / 非目录 → 抛 ``AssetImportError``。
    2. 依次尝试 ``characters.md`` / ``plot_threads.md`` / ``timeline.md`` /
       ``world.md``;存在则解析 + 构造实体 + 写 SQLite(三类硬记忆走
       ``get-or-update-or-create``)。
    3. 四类合并批量写 Chroma(``DEFAULT_COLLECTION`` 默认 ``"memories"``,
       id 约定 ``f"{entity_type}-{entity_id}"``,五字段 metadata)。
    4. 落 ``asset_import_completed`` structlog 事件,返回
       ``ImportAssetsResult``。

    :param md_dir: 资产文件目录;4 个文件全可选,缺失静默跳过。
    :param workspace_id: 目标工作区 ID(三类硬记忆的 ``workspace_id`` 字段
        + Chroma metadata ``workspace_id`` 字段全同此值)。
    :param db_session: SQLAlchemy ``Session``;事务边界归 caller(本服务
        ``flush`` 不 ``commit``)。
    :param vector_store: 注入的 ``VectorStore``(已绑定 workspace_id +
        embedding_fn + persist_dir)。
    :raises AssetImportError: ``md_dir`` 不存在 / 必填字段缺失 / 类型错。
    :returns: ``ImportAssetsResult``——四类导入条目数 + 处理的文件名。
    """
    if not md_dir.is_dir():
        raise AssetImportError(f"md_dir 不是目录或不存在: {md_dir}")

    files_processed: list[str] = []
    saved_characters: list[Character] = []
    saved_threads: list[PlotThread] = []
    saved_timeline: list[TimelineEntry] = []
    facts: list[ImportedFact] = []

    # 1) characters.md
    char_path = md_dir / CHARACTERS_FILENAME
    if char_path.is_file():
        files_processed.append(CHARACTERS_FILENAME)
        sections = _parse_markdown_sections(
            char_path.read_text(encoding="utf-8")
        )
        for section in sections:
            char = _build_character(
                section, workspace_id=workspace_id, file=CHARACTERS_FILENAME
            )
            existing = get_character(db_session, char.character_id)
            if existing is None:
                saved_characters.append(create_character(db_session, char))
            else:
                saved_characters.append(update_character(db_session, char))

    # 2) plot_threads.md
    threads_path = md_dir / PLOT_THREADS_FILENAME
    if threads_path.is_file():
        files_processed.append(PLOT_THREADS_FILENAME)
        sections = _parse_markdown_sections(
            threads_path.read_text(encoding="utf-8")
        )
        for section in sections:
            thread = _build_plot_thread(
                section, workspace_id=workspace_id, file=PLOT_THREADS_FILENAME
            )
            existing_thread = get_plot_thread(db_session, thread.thread_id)
            if existing_thread is None:
                saved_threads.append(create_plot_thread(db_session, thread))
            else:
                # update 路径保留原 ``created_at``(用户文件不维护)。
                thread = thread.model_copy(
                    update={"created_at": existing_thread.created_at}
                )
                saved_threads.append(update_plot_thread(db_session, thread))

    # 3) timeline.md
    timeline_path = md_dir / TIMELINE_FILENAME
    if timeline_path.is_file():
        files_processed.append(TIMELINE_FILENAME)
        sections = _parse_markdown_sections(
            timeline_path.read_text(encoding="utf-8")
        )
        for section in sections:
            entry = _build_timeline_entry(
                section, workspace_id=workspace_id, file=TIMELINE_FILENAME
            )
            existing_entry = get_timeline_entry(db_session, entry.entry_id)
            if existing_entry is None:
                saved_timeline.append(
                    create_timeline_entry(db_session, entry)
                )
            else:
                saved_timeline.append(
                    update_timeline_entry(db_session, entry)
                )

    # 4) world.md(只入 Chroma)
    world_path = md_dir / WORLD_FILENAME
    if world_path.is_file():
        files_processed.append(WORLD_FILENAME)
        sections = _parse_markdown_sections(
            world_path.read_text(encoding="utf-8")
        )
        for section in sections:
            facts.append(_build_fact(section, file=WORLD_FILENAME))

    # 5) Chroma 合并批量写。
    _persist_to_vector_store(
        vector_store=vector_store,
        workspace_id=workspace_id,
        characters=saved_characters,
        plot_threads=saved_threads,
        timeline=saved_timeline,
        facts=facts,
    )

    counts = {
        "character": len(saved_characters),
        "plot_thread": len(saved_threads),
        "timeline": len(saved_timeline),
        "fact": len(facts),
    }

    _log.info(
        "asset_import_completed",
        workspace_id=workspace_id,
        md_dir=str(md_dir),
        files_processed=files_processed,
        counts=counts,
    )

    return ImportAssetsResult(
        workspace_id=workspace_id,
        files_processed=files_processed,
        counts=counts,
    )


def import_assets_from_content(
    *,
    workspace_id: str,
    assets_dir: Path,
    world_md_content: str | None,
    characters_md_content: str | None,
    db_session: Session,
    vector_store: VectorStore,
) -> ImportAssetsResult:
    """``POST /api/v1/workspaces/{id}/assets:import`` 端点的服务入口(P5-B-08)。

    与 ``import_assets`` 的区别:本函数接 **markdown 文本内容**(API body 上
    传场景),先把内容原子写到 ``assets_dir`` 下的 ``world.md`` /
    ``characters.md``,再调既有 ``import_assets`` 完成解析 + 入库。两个字段
    都 ``None`` / 空字符串时,``assets_dir`` 仍会 mkdir 但本次不写新文件 +
    调 ``import_assets`` 只处理目录里已存在的文件(返 counts 全 0)。

    设计选择(P5-B-08 决策 ③=A 落盘):落盘后源文件保留,后续 debug / 浏览
    器走查 / re-import 时可直接看 / 复用,与 CLI ``import-assets`` 接外部
    目录互补;并把 in-memory text 转成既有 service 的 ``md_dir`` 入参,**不
    改 ``import_assets`` 签名**。

    :param assets_dir: 工作区 ``data/assets/`` 目录(由 caller 从
        ``WorkspacePaths.assets_dir`` 取);不存在自动 ``mkdir``。
    :param world_md_content: ``world.md`` 文件内容(None / 空字符串 → 跳过本
        次落盘,既有文件不动)。
    :param characters_md_content: ``characters.md`` 文件内容(None / 空字符串
        → 跳过本次落盘,既有文件不动)。
    :raises AssetImportError: 与 ``import_assets`` 同(必填字段缺失 / 字段
        值不合 schema 等)。
    :returns: 与 ``import_assets`` 同。``counts`` 字段名沿用既有命名
        (``character`` / ``plot_thread`` / ``timeline`` / ``fact``),不裁字段。
    """
    # v1.0.x wave 6 s2.6a:race 防御 — assets_dir 形如 <workspaces>/<wid>/data/assets/,
    # workspace 根目录 = assets_dir.parent.parent;已被 rmtree → swallow + log + raise
    # AssetImportError 让 caller 显式知道写失败(asset import 是用户主动操作,silent skip
    # 会让 UI 显示"导入成功"误导 user)。
    workspace_root = assets_dir.parent.parent
    if not workspace_root.is_dir():
        _log.warning(
            "workspace_writer_skipped",
            writer="import_assets_with_md_files",
            workspace_id=workspace_id,
            assets_dir=str(assets_dir),
            reason="workspace_destroyed_during_async_task",
        )
        raise AssetImportError(
            f"工作区已被销毁,拒绝写入资产: {workspace_root}"
        )
    assets_dir.mkdir(parents=True, exist_ok=True)

    if world_md_content:
        _write_text_atomic(assets_dir / WORLD_FILENAME, world_md_content)
    if characters_md_content:
        _write_text_atomic(assets_dir / CHARACTERS_FILENAME, characters_md_content)

    return import_assets(
        assets_dir,
        workspace_id=workspace_id,
        db_session=db_session,
        vector_store=vector_store,
    )


def _write_text_atomic(target: Path, content: str) -> None:
    """tmp → replace 原子写,与 ``ChapterService.save_chapter_card`` 同模式。

    避免半截文件:写 ``<target>.tmp`` 再 ``rename`` 到 ``<target>``;POSIX
    ``rename`` 保证原子性,中断时只剩 ``<target>`` 旧内容或不存在,绝不出
    现内容写一半的状态。
    """
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(target)
