"""SQLAlchemy 2.x 声明式 ORM 模型(对应 PROJECT_SPEC §3)。

P1 引入 5 张表:``workspaces`` / ``chapters`` / ``chapter_versions`` /
``review_results`` / ``review_issues``;P2-B-03 追加 3 张记忆表
``characters`` / ``plot_threads`` / ``timeline_entries``；W3.5-R5 追加
``attempt_ledger``，统一保存跨进程的 pipeline 与真实 provider 尝试。

设计要点:

1. **每工作区独立 ``novel.db``**,故所有表都在同一个 SQLite 文件内,跨表外键
   不需要跨库;``workspaces`` 表在每个 db 内通常只有一行(本工作区元数据)。
2. **章节卡 P1 走文件**(``data/cards/<chapter_id>.json``),不进 ``chapters``
   表;``chapters`` 表只存运行态字段(状态、计数、累计 token / 成本)。
3. **``chapter_versions`` 用复合主键** ``(chapter_id, version_id)``:版本号在
   章节内 1 起递增,复合 PK 自然且免去额外 UNIQUE 约束。
4. **``review_issues`` 共表** + ``role`` 字段区分 ``issue`` / ``revise_target``
   (revise_targets 是 issues 的子集语义);``position`` 字段保证 round-trip
   顺序稳定。
5. **复杂字段(dict / list[str])用 SQLAlchemy 通用 ``JSON`` 类型**,SQLite
   实际存 TEXT,SQLAlchemy 自动序列化/反序列化。
6. **时间字段** ``DateTime(timezone=True)``,UTC 入库;SQLite 存字符串,
   SQLAlchemy 透明处理。
7. **ORM 字段名严格对齐 Pydantic 模型字段名**(便于 Pydantic ↔ ORM 互转)。
8. **VoiceProfile 嵌入 ``characters`` 表 JSON 列**(P2-B-03 决策):VoiceProfile
   与 Character 1:1,无独立查询需求,P3 voice forbidden 校验直接读 character row。
9. **``timeline_entries.chapter_id`` 不加 FK**(P2-B-03 决策):章节卡 P2 仍走
   文件,ChapterORM row 仅 drafting 期才创建;P5 OutlineBuilder 一次性产出章节
   卡 + 时间线时,引用的 chapter_id 可能尚未在 ``chapters`` 表存在,弱字符串
   引用更稳妥(等 P2-B-04 store.py CRUD 时按业务校验)。

ORM 类后缀 ``ORM`` 与同名 Pydantic 模型(``Workspace`` / ``Chapter`` / ...)
区分,避免 import 时混淆。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    event,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection: Any, connection_record: Any) -> None:
    """每个 SQLite 连接开 WAL + busy_timeout(v1.1 wave 4 B1 acceptance 修 Y1)。

    - ``journal_mode=WAL``:读写并发(一写多读),``:memory:`` 库 no-op
      (返回 ``memory``),不报错。
    - ``busy_timeout=5000``:普通写锁占用时退避重试至多 5s。

    **Y1 治本的真正手段在 ``cost_tracker`` 的进程内串行锁**(见
    ``cost_tracker._COST_WRITE_LOCK``),不在此处:WAL + busy_timeout 只解决
    "读不阻塞写"和普通 ``SQLITE_BUSY`` 排队,但解决不了多连接并发对**同一
    chapter row** ``SELECT…UPDATE``(DEFERRED 读快照升级写)撞的
    ``SQLITE_BUSY_SNAPSHOT`` —— 此错 ``busy_timeout`` 不重试、立即抛 locked
    (复现实测等待 0.00s 即抛)。曾试过全局 ``BEGIN IMMEDIATE`` 根治,但它
    改变所有事务的 PRAGMA 时机(``PRAGMA foreign_keys`` 在事务内 no-op)→
    破坏 3 个 FK 测试,副作用过大;改为在 cost_tracker 应用层串行写,零
    全局副作用。

    全局 ``Engine`` connect 监听;非 SQLite 连接(本工程不出现)早 return 防御。
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


class Base(DeclarativeBase):
    """SQLAlchemy 2.x 声明式基类,所有 ORM 模型继承。

    单例 ``metadata`` 由 alembic ``env.py`` 设置 ``target_metadata`` 时使用。
    """


OUTLINE_STATUS_VALUES: tuple[str, ...] = (
    "not_started",
    "generating",
    "committed",
)
"""``WorkspaceORM.outline_status`` 允许值集合(P5-B-07 起 3 档,对齐 p5_plan
§"P5-B-07"字面 + 启动指令待审 ② 拍 A 3 档)。

3 档语义:

- ``not_started``:工作区刚创建,outline 尚未触发生成(P1-P4 既有工作区
  默认值,Alembic 0007 加列 DEFAULT)。
- ``generating``:``POST outline:generate`` 端点触发,4 段串行进行中。
- ``committed``:``POST outline:commit`` gate 全过 + volumes 表落库成功;
  工作区进入"章节可写"状态(``chapter:write`` gate 不再因 outline 缺失
  阻塞,本字段不直接 gate write — gate 仍由 ``card_status="approved"``
  控制,本字段仅作 NewBookPage UI 进度展示)。

**与 ``card_status`` 解耦**:本字段是 workspace 级 outline 流转状态,
``card_status`` 是 chapter 级章节卡审核状态,生命周期独立。

CHECK 约束 SQL 字符串由 ``_outline_status_check_sql`` 构造,迁移脚本
``alembic/versions/0007_outline_status_rejection_notes.py`` 直接复用同一份
枚举,确保 ORM 与迁移单一真相源(对齐 P2-B-17 ``CHAPTER_STATUS_VALUES`` /
P4-B-01 ``MEMORY_HEALTH_SEVERITY_VALUES`` / P5-B-01 ``TENSION_TEMPLATE_VALUES``
/ P5-B-02 ``CARD_STATUS_VALUES`` 同型模式)。
"""


def _outline_status_check_sql() -> str:
    """``WorkspaceORM.outline_status`` CHECK 约束 SQL 谓词(3 档 IN 列表)。"""
    quoted = ", ".join(f"'{v}'" for v in OUTLINE_STATUS_VALUES)
    return f"outline_status IN ({quoted})"


DEFAULT_VOICE_VALUES: tuple[str, ...] = ("third_person", "first_person", "free")
"""``WorkspaceORM.default_voice`` 允许值集合(v1.1 wave 1 session 6 / G10 引入)。

对齐 CLAUDE.md §核心硬约束第 11 条"长篇默认第三人称视角"+ Pydantic
``Workspace.default_voice`` Literal 同源,3 档语义:

- ``third_person``:项目级默认(中文长篇优先,2026-05-27 wave 1 末 B1 实证后立)
- ``first_person``:第一人称(用户显式选,L2 NewBookPage UI wave 3-s4)
- ``free``:关闭跨章节视角硬约束,只 warn log(用户选 / 实验)
"""


def _default_voice_check_sql() -> str:
    """``WorkspaceORM.default_voice`` CHECK 约束 SQL 谓词(3 档 IN 列表)。"""
    quoted = ", ".join(f"'{v}'" for v in DEFAULT_VOICE_VALUES)
    return f"default_voice IN ({quoted})"


class WorkspaceORM(Base):
    """工作区元数据(对应 ``Workspace`` Pydantic 模型,PROJECT_SPEC §3.1)。

    P1 阶段每个工作区独立 db,因此本表通常只有一行;落表是为后续查询 / 跨
    工作区聚合(P5)做准备,P1 暂不强制依赖此表(meta.json 仍是事实源)。

    **P5-B-07 起新增 ``outline_status``** 字段(DB-only 运行态,**不入** Pydantic
    ``Workspace`` 与 meta.json):workspace 级 outline 流转状态 3 档,由
    ``OutlineService`` 在 generate / commit 端点直接 update ORM 行
    (对齐 P5-B-04 ``OutlineService`` 落 ``VolumeORM`` 同源 — service 拿
    session_factory 直接写 ORM)。CHECK 约束确保枚举内合法值。
    """

    __tablename__ = "workspaces"

    workspace_id: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    genre: Mapped[str] = mapped_column(String, nullable=False)
    premise: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    engine_version: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    total_chapters: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_chapters: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    config_overrides: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    outline_status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default="not_started",
    )
    """outline 流转状态(P5-B-07,3 档 ``OUTLINE_STATUS_VALUES``)。

    **纯 ``server_default`` 不带 Python ``default``**:SQLAlchemy ORM ``INSERT``
    时若 attribute 未在对象上赋值,**不**发出本列(让 DB 的 ``DEFAULT
    'not_started'`` 填),避免 P5-B-07 加列后旧 fixture 测试
    (``test_alembic_baseline`` / P2-B-17 ``downgrade -1`` 等显式 upgrade 到
    历史 revision 的测试)在 ORM INSERT 时撞"table workspaces has no column
    named outline_status"。若 caller 显式给 attribute 赋值(``OutlineService
    ._set_outline_status`` 写 'generating' / 'committed'),则 ORM 会发出
    UPDATE / INSERT 列。
    """
    default_voice: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default="third_person",
    )
    """工作区默认人称视角(v1.1 wave 1 session 6 / G10,3 档 ``DEFAULT_VOICE_VALUES``)。

    对齐 CLAUDE.md §核心硬约束第 11 条"长篇默认第三人称视角"+ Pydantic
    ``Workspace.default_voice`` Literal 同源。
    G10 ``cross_chapter_voice_validator`` 在首章(无 baseline)时读此字段
    决定锁定哪个人称;L2 ``NewBookPage`` UI(wave 3-s4)让用户在创建工作区
    时显式选 3 档之一。

    **纯 ``server_default`` 不带 Python ``default``** — 同 ``outline_status``
    模式:既有 workspace 行 Alembic 0015 upgrade 后 backfill 为 ``third_person``
    (项目级硬约束默认);新工作区 INSERT 时若 caller 不赋值则走 DB DEFAULT,
    若 ``WorkspaceService.create`` 显式传 ``default_voice`` 参数则走 INSERT 列。
    """

    __table_args__ = (
        CheckConstraint(
            _outline_status_check_sql(),
            name="ck_workspaces_outline_status",
        ),
        CheckConstraint(
            _default_voice_check_sql(),
            name="ck_workspaces_default_voice",
        ),
    )


CHAPTER_STATUS_VALUES: tuple[str, ...] = (
    "drafting",
    "reviewing",
    "rewriting",
    "revising",
    "approved",
    "published",
    "human_intervention",
    "invalidated",
)
"""``ChapterORM.status`` 允许值集合(P2-B-17 起 7 档,P6-B-01 扩 ``invalidated``
第 8 档)。对齐 PROJECT_SPEC §3.5 ``Chapter.status`` Literal 与 ``ChapterState``
StrEnum。

CHECK 约束 SQL 字符串由 ``_chapter_status_check_sql`` 构造,迁移脚本
``alembic/versions/0003_chapters_state_7_levels.py``(7 档 baseline)+
``alembic/versions/0008_chapter_invalidated_force_pass.py``(扩 ``invalidated``
档 + ``force_pass_at`` + ``force_pass_reason`` 字段)直接复用同一份枚举,
确保 ORM 与迁移单一真相源。

**``invalidated`` 语义**:rewind 触发后,目标章节回退到 ``drafting``,
``chapter_id > target_id`` 的后续章节翻 ``invalidated`` 表"上游被改写,本章
stale";``ChapterService._run_gates`` 检测 ``invalidated`` 抛
``ChapterInvalidatedNeedsUnblockError`` (路由翻译为 409
``CHAPTER_INVALIDATED_NEEDS_UNBLOCK``,要求 user 先 ``:unblock`` 才能
write/rewrite/revise)。
"""


def _chapter_status_check_sql() -> str:
    """``CHECK`` 约束 SQL 谓词(P6-B-01 起 8 档 IN 列表;P2-B-17 baseline 7 档)。

    单独函数让迁移脚本与 ORM 共用。值列表来源 ``CHAPTER_STATUS_VALUES``。
    """
    quoted = ", ".join(f"'{v}'" for v in CHAPTER_STATUS_VALUES)
    return f"status IN ({quoted})"


class ChapterORM(Base):
    """章节运行态(对应 ``Chapter`` Pydantic 模型,PROJECT_SPEC §3.5 子集)。

    **不存** ``card`` / ``versions`` / ``review_history``:
    - ``card`` P1 走文件 ``data/cards/<chapter_id>.json``
    - ``versions`` 通过 ``chapter_versions.chapter_id`` 反查
    - ``review_history`` 通过 ``review_results.chapter_id`` 反查

    **``status`` 字段 7 档 CHECK**(P2-B-17 起,对齐 PROJECT_SPEC §3.5 Pydantic
    ``Chapter.status`` 7 档 Literal):取值必须落在
    ``CHAPTER_STATUS_VALUES`` 内(``drafting`` / ``reviewing`` / ``rewriting``
    / ``revising`` / ``approved`` / ``published`` / ``human_intervention``)。
    P1 baseline 没有 CHECK 约束,P2-B-17 0003 迁移补加。
    """

    __tablename__ = "chapters"

    chapter_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
    )
    current_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    rewrite_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revise_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    total_token_usage: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    total_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    force_pass_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """``POST /chapters/{id}:force-pass`` 触发时间(P6-B-01 引入,Alembic 0008
    加列);常态 ``None``。force-pass 跳过 Reviewer 是显著决策点,留时间戳
    便于事后追溯。"""

    force_pass_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    """``POST /chapters/{id}:force-pass`` 触发理由(P6-B-01 引入,Alembic 0008
    加列);常态 ``None``。schema 字面强制非空(``min_length=1``,自决项 1
    对齐 PROJECT_SPEC §5.4 字面"破坏性操作要 user 显式留痕"),DB 列允许
    NULL 兼容 P5- 之前历史行。"""

    escalation_details: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    """升 ``human_intervention`` 时的结构化详情(v1.1 acceptance 修复2 / Fix D,
    Alembic 0022 加列);常态 ``None``。目前唯一写入方 = ``ChapterService``
    在 ``_orchestrate`` 返回 ``HUMAN_INTERVENTION`` 且 ``OrchestratorResult.
    escalation_details`` 非空时落
    ``{"reason", "checked_at_state", "findings": [{"must_text", "confidence",
    "evidence", "suggestion"}]}``;``GET /chapters/{id}/status`` 透出供孤儿章
    人工裁决 UI 展示"哪条 must 不可达 + 处置建议"。nullable 无 default(对齐
    ``force_pass_reason`` 0008 同型:ORM INSERT 未赋值不发列,旧 revision
    fixture 测试不撞 no such column)。"""

    scene_anchor: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, server_default="{}"
    )
    """B2 双时空场景锚点(v1.1 wave 6 session 3,Alembic 0020 加列);常态空 dict
    ``{}``。双时空入梦 / 醒来章由 ``_extract_memories_safely`` 在 extract 后写入
    ``{enter_dream_location, wake_location, has_transition_explanation}``。跨章
    比对"醒来点≠入梦点且无位移交代"时按 ``chapter_id`` 升序查最近一次入梦点。
    既有行 upgrade 后 backfill 为 ``{}``(无锚点,B2 比对自动 skip)。

    **纯 ``server_default`` 不带 Python ``default``**(对齐 ``outline_status`` /
    ``default_voice`` 同型):ORM ``INSERT`` 未显式赋值时不发本列,让 DB ``DEFAULT
    '{}'`` 填,避免 0020 加列后旧 fixture 测试(``test_alembic_baseline`` / P2-B-17
    ``downgrade -1`` 等显式 upgrade 到历史 revision 的测试)在 ORM INSERT 撞
    "no such column scene_anchor"。caller(``_persist_scene_anchor_to_chapter``)
    显式赋值时 ORM 才发 UPDATE 列。"""

    ready_for_next_chapter_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """W3.5-C 写作硬水位:终稿已落盘、章节状态已 approved、下一章可安全读取
    上章 final.md + chapter card 继续推进时写入。常态 ``None`` 表示尚未达到
    该水位;该列是"下一章必须等"的确定性账本,不依赖 LLM 摘要/向量加工。"""

    core_state_commit_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=""
    )
    """Session 3/C 核心状态原子提交 hash；空串表示 legacy/尚未提交。"""

    core_state_commit_status: Mapped[str] = mapped_column(
        String, nullable=False, server_default="disabled"
    )
    """``disabled/pending/committed/retracted``；ready 前合同 v1 必须 committed。"""

    core_state_commit_details: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, server_default="{}"
    )
    """来源版本、投影 hash、准入/拒绝计数与恢复审计；不进入 Writer prompt。"""

    memory_enrichment_status: Mapped[str] = mapped_column(
        String, nullable=False, server_default="pending"
    )
    """W3.5-C 富记忆后台加工状态:``pending`` / ``running`` / ``succeeded`` /
    ``failed`` / ``skipped``。纯 ``server_default`` 不带 Python ``default``,
    对齐 ``scene_anchor`` 的历史 revision 兼容策略。"""

    memory_enriched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """富记忆加工成功完成时间。失败或尚未运行时为 ``None``。"""

    memory_enrichment_details: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    """富记忆后台任务的结构化详情。失败时记录失败 stage / error_message,
    成功时记录实际执行或跳过的子任务,避免检索为空被静默误解为"没有记忆"。"""

    versions: Mapped[list[ChapterVersionORM]] = relationship(
        back_populates="chapter",
        cascade="all, delete-orphan",
        order_by="ChapterVersionORM.version_id",
    )
    reviews: Mapped[list[ReviewResultORM]] = relationship(
        back_populates="chapter",
        cascade="all, delete-orphan",
        order_by="ReviewResultORM.reviewed_at",
    )

    __table_args__ = (
        Index("ix_chapters_workspace_id", "workspace_id"),
        CheckConstraint(
            _chapter_status_check_sql(),
            # P6-B-01 起 8 档 CHECK;P2-B-17 baseline 约束名 ``ck_chapters_status_7_levels``
            # 在 Alembic 0008 内 drop 后重新创建为 ``ck_chapters_status_8_levels``,
            # ORM 字面 name 与新约束对齐(对齐 P5-B-02 / P5-B-07 batch_alter_table
            # 重建 CHECK 名同模式)。
            name="ck_chapters_status_8_levels",
        ),
    )


class ChapterOperationLeaseORM(Base):
    """R8 章节有副作用操作的持久唯一租约。

    租约与 chapters、attempt_ledger 位于同一工作区 SQLite。进程崩溃时不
    自动删除；后续入口必须依据 attempt/版本/审稿/final/ledger/ready 现场
    分类，绝不能因内存 Worker 消失就重复发出付费请求。
    """

    __tablename__ = "chapter_operation_leases"

    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        primary_key=True,
    )
    chapter_id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
    )
    lease_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    operation_kind: Mapped[str] = mapped_column(String, nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    phase: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("ix_chapter_operation_leases_task_id", "task_id"),
    )


class ChapterVersionORM(Base):
    """章节版本快照(对应 ``ChapterVersion`` Pydantic 模型)。

    **复合主键** ``(chapter_id, version_id)``:version_id 在章节内 1 起单调
    递增(P1-B-05 ``ChapterVersioning.next_version_id`` 保证),复合 PK 是
    最自然且免索引的方案。
    """

    __tablename__ = "chapter_versions"

    chapter_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("chapters.chapter_id", ondelete="CASCADE"),
        primary_key=True,
    )
    version_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    creation_type: Mapped[str] = mapped_column(String, nullable=False)
    model_used: Mapped[str] = mapped_column(String, nullable=False)
    review_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # v1.1 wave 1 session 4(G5):风格指纹 JSON nullable 列,首版作 baseline,
    # 后续版本对比偏移度检测风格漂移。详 Alembic 0014_chapter_versions_style_fingerprint。
    style_fingerprint: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    chapter: Mapped[ChapterORM] = relationship(back_populates="versions")


class ReviewResultORM(Base):
    """审稿结果(对应 ``ReviewResult`` Pydantic 模型,PROJECT_SPEC §3.6)。

    ``failed_constraints`` / ``suggestions`` 是 ``list[str]``；
    ``constraint_violations`` 保存 R2 结构化约束证据；R6 的
    ``constraint_results`` / ``hard_constraint_regressions`` 保存逐约束状态与
    pass→fail 的稳定 ID。上述集合均用 ``JSON`` 列。
    ``issues`` 与 ``revise_targets`` 关系类列表落 ``review_issues`` 表
    (用 ``role`` 字段区分),不内嵌 JSON,便于 SQL 聚合。
    """

    __tablename__ = "review_results"

    review_id: Mapped[str] = mapped_column(String, primary_key=True)
    chapter_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("chapters.chapter_id", ondelete="CASCADE"),
        nullable=False,
    )
    version_reviewed: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    reviewer_model: Mapped[str] = mapped_column(String, nullable=False)

    score_plot_progression: Mapped[float] = mapped_column(Float, nullable=False)
    score_tension_match: Mapped[float] = mapped_column(Float, nullable=False)
    score_prose_quality: Mapped[float] = mapped_column(Float, nullable=False)
    score_consistency: Mapped[float] = mapped_column(Float, nullable=False)
    score_hook_management: Mapped[float] = mapped_column(Float, nullable=False)
    score_voice_consistency: Mapped[float] = mapped_column(Float, nullable=False)
    score_overall: Mapped[float] = mapped_column(Float, nullable=False)

    # wave 5 卓越性纯度量(M2,Alembic 0021,v1.1 wave 7 持久化):画面感 / 内心戏 /
    # 套路化(反极性)/ 沉浸感。**nullable=True**:LLM 漏产 / 旧审稿(0021 前)→ NULL。
    # **永不进 score_overall 加权、永不影响 verdict**(verdict 由 reviewer
    # `_compute_score_overall` 的 6 基础维加权在写库前算定,本 4 列纯存储,与 verdict
    # 计算正交)。仅供唯一稿质量观察 + 前端质感读数展示。
    score_imagery: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_interiority: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_formulaic: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_immersion: Mapped[float | None] = mapped_column(Float, nullable=True)

    hard_constraints_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failed_constraints: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    constraint_violations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    constraint_results: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    hard_constraint_regressions: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    suggestions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    verdict: Mapped[str] = mapped_column(String, nullable=False)

    chapter: Mapped[ChapterORM] = relationship(back_populates="reviews")
    issues: Mapped[list[ReviewIssueORM]] = relationship(
        back_populates="review",
        cascade="all, delete-orphan",
        order_by="ReviewIssueORM.id",
    )

    __table_args__ = (Index("ix_review_results_chapter_id", "chapter_id"),)


class ReviewIssueORM(Base):
    """审稿问题条目(对应 ``ReviewIssue`` Pydantic 模型)。

    ``ReviewIssue`` 在 Pydantic 层没有自身 ID,这里用自增 ``id`` 做代理 PK。

    ``role`` 字段区分两种来源:
    - ``"issue"`` —— 来自 ``ReviewResult.issues`` 列表
    - ``"revise_target"`` —— 来自 ``ReviewResult.revise_targets`` 列表
      (verdict=revise 时才有,通常是 issues 的子集)

    ``position`` 字段保留列表内顺序(0 起),保证 ORM ↔ Pydantic round-trip
    时列表顺序稳定。
    """

    __tablename__ = "review_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    review_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("review_results.review_id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    paragraph_anchor: Mapped[str] = mapped_column(Text, nullable=False)
    issue_type: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    suggestion: Mapped[str] = mapped_column(Text, nullable=False)

    review: Mapped[ReviewResultORM] = relationship(back_populates="issues")

    __table_args__ = (Index("ix_review_issues_review_id", "review_id"),)


class CharacterORM(Base):
    """角色卡(对应 ``Character`` Pydantic 模型,PROJECT_SPEC §3.3)。

    ``voice_profile`` 嵌入 JSON 列(P2-B-03 决策):VoiceProfile 与 Character
    1:1,无独立查询需求,P3 voice forbidden 校验直接读 character row。

    复杂字段(``aliases`` / ``goals_*`` / ``current_state`` / ``relationships``
    / ``abilities`` / ``secrets`` / ``voice_profile``)统一用 ``JSON`` 列。
    """

    __tablename__ = "characters"

    character_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    archetype: Mapped[str] = mapped_column(String, nullable=False)
    appearance: Mapped[str] = mapped_column(Text, nullable=False)
    personality: Mapped[str] = mapped_column(Text, nullable=False)
    background: Mapped[str] = mapped_column(Text, nullable=False)
    identity_facets: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    """角色多情境身份侧写(v1.1 wave 6 session 2 / A3,Alembic 0019 加列)。

    ``IdentityFacet`` 列表的 JSON 序列化:每条 {context, identity}。空列表默认
    (旧角色行 backfill 为 ``[]``;无多重身份的简单角色也是空)。Writer 注入 +
    mini_plan 覆盖自检 + 职业黑名单粗筛三处消费。"""
    voice_profile: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    goals_long_term: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    goals_short_term: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    current_state: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    state_timeline: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    """G1 角色时序状态追踪(v1.1 wave 4 session 1,Alembic 0016 加列)。

    ``CharacterStateSnapshot`` 列表的 JSON 序列化:每章一条,按 ``as_of_chapter``
    升序。Writer 写第 N 章时取截止 N-1 章的最新一条注入 prompt(绕开 embedding),
    防"断臂复活"。空列表默认(旧角色行 backfill 为 ``[]``)。"""
    relationships: Mapped[dict[str, str]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    abilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    secrets: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    first_appearance_chapter: Mapped[str | None] = mapped_column(String, nullable=True)
    last_updated_chapter: Mapped[str | None] = mapped_column(String, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """``ChapterRewindService.rewind`` 软删除时间戳(P6-B-01 引入,Alembic 0008
    加列);常态 ``None``。``first_appearance_chapter == target_id`` 的角色行
    在 rewind 时翻成 ``datetime.now(UTC)``,逻辑上从检索结果排除(对齐
    plan §"P6-B-01"字面 + Chroma soft-delete revoked metadata 同源)。
    P6+ 服务层是否过滤本字段由各 service 自行决定(P6-B-01 仅落字段,
    不强制改 ``character_service`` 等查询路径)。"""

    __table_args__ = (Index("ix_characters_workspace_id", "workspace_id"),)


class PlotThreadORM(Base):
    """伏笔(对应 ``PlotThread`` Pydantic 模型,PROJECT_SPEC §3.4)。

    ``status`` / ``importance`` 加索引(任务模板要求):前端按状态 4 档 +
    重要性 1-5 筛选(P2-B-15 路由),独立索引覆盖两种主筛维度。
    """

    __tablename__ = "plot_threads"

    thread_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    thread_type: Mapped[str] = mapped_column(
        String, nullable=False, server_default="plot_event"
    )
    """伏笔类型(v1.1 wave 4 session 2 / G2,Alembic 0017 加列)。

    ``plot_event`` / ``foreshadowing`` / ``metaphor`` / ``callback`` 四档,对应
    ``PlotThread.thread_type`` Literal。既有行 upgrade 后 ``server_default``
    backfill 为 ``plot_event``(向后兼容)。辅助 UI 分类;G2 callback 链路按
    thread_id 精确 lookup,不依赖本列。"""
    status: Mapped[str] = mapped_column(String, nullable=False)
    importance: Mapped[int] = mapped_column(Integer, nullable=False)
    planted_chapter: Mapped[str | None] = mapped_column(String, nullable=True)
    planned_payoff: Mapped[str | None] = mapped_column(String, nullable=True)
    actual_payoff: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(
        String, nullable=False, server_default="outline"
    )
    """伏笔来源(R9 临时伏笔分层):

    ``outline`` = 大纲阶段预先规划的全书/卷级主伏笔；``emergent`` = 正文写完后
    由既有 MemoryExtractor 高门槛收录的临时伏笔；``manual`` = 用户/API/资产
    导入创建。字段只供内部管线分流，不进入公开 PlotThread API，避免把临时层
    叠成第二套 Canon 产品模型。历史行迁移时保守回填 ``outline``，因此不会被
    临时伏笔的候选上限或过期规则误过滤。
    """
    expiry_policy: Mapped[str] = mapped_column(
        String, nullable=False, server_default="never"
    )
    """临时伏笔的常规检索休眠策略:``arc_end`` / ``volume_end`` / ``never``。

    仅 ``origin='emergent'`` 时生效；大纲/人工伏笔固定按 ``never`` 处理。
    “过期”只是不再进入日常章节卡候选，不改 status、不删除，也不冒充已收口。
    """
    source_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    """正文中新建临时伏笔时的逐字证据；大纲/人工伏笔通常为空。"""
    related_characters: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_updated_chapter: Mapped[str | None] = mapped_column(String, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """``ChapterRewindService.rewind`` 软删除时间戳(P6-B-01 引入,Alembic 0008
    加列);常态 ``None``。``planted_chapter == target_id`` 的伏笔行在 rewind
    时翻成 ``datetime.now(UTC)``,逻辑上从检索结果排除(``planted_chapter``
    为 ``None`` 的 outline 落表行不会被该 query 命中 — 安全)。"""

    __table_args__ = (
        Index("ix_plot_threads_workspace_id", "workspace_id"),
        Index("ix_plot_threads_status", "status"),
        Index("ix_plot_threads_importance", "importance"),
        CheckConstraint(
            "origin IN ('outline', 'emergent', 'manual')",
            name="ck_plot_threads_origin",
        ),
        CheckConstraint(
            "expiry_policy IN ('arc_end', 'volume_end', 'never')",
            name="ck_plot_threads_expiry_policy",
        ),
    )


class TimelineEntryORM(Base):
    """时间线条目(对应 ``TimelineEntry`` Pydantic 模型,PROJECT_SPEC §3.4)。

    ``time_order`` 加索引(任务模板要求):落地 §4.2 [HARD] 时间线单调校验,
    P2-B-16 路由按 ``time_order`` 升序输出。

    **``chapter_id`` 不加 FK**(P2-B-03 决策):章节卡 P2 仍走文件,ChapterORM
    row 仅 drafting 期才创建;P5 OutlineBuilder 一次性产出章节卡 + 时间线时
    引用的 chapter_id 可能尚未在 ``chapters`` 表存在,弱字符串引用更稳妥。
    """

    __tablename__ = "timeline_entries"

    entry_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
    )
    chapter_id: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    time_label: Mapped[str] = mapped_column(String, nullable=False)
    time_order: Mapped[int] = mapped_column(Integer, nullable=False)
    involved_characters: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """``ChapterRewindService.rewind`` 软删除时间戳(P6-B-01 引入,Alembic 0008
    加列);常态 ``None``。``chapter_id == target_id`` 的时间线行在 rewind
    时翻成 ``datetime.now(UTC)``,逻辑上从检索结果排除。"""
    is_flashback: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    """v1.1 E2 引入(Alembic 0013):flashback / 回忆 / 穿越回过去 / 双时空
    穿梭的另一时空标记。``check_timeline_monotonic`` 校验时跳过
    ``is_flashback=True`` 的 entry,只在主时间线内做单调校验。
    ``MemoryExtractor`` prompt 引导 LLM 标此字段。旧行回填 False。"""

    __table_args__ = (
        Index("ix_timeline_entries_workspace_id", "workspace_id"),
        Index("ix_timeline_entries_time_order", "time_order"),
    )


MEMORY_HEALTH_SEVERITY_VALUES: tuple[str, ...] = (
    "ok",
    "minor",
    "warning",
    "critical",
)
"""``MemoryHealthCheckORM.severity`` 允许值集合(对齐 OBSERVABILITY §3.1
``memory_health_history.severity`` TEXT 字面 + Pydantic ``MemoryHealthCheck``
``Literal``)。

CHECK 约束 SQL 由 ``_memory_health_severity_check_sql`` 构造,迁移脚本
``alembic/versions/0004_p4_metrics_tables.py`` 复用同一份枚举,确保 ORM 与
迁移单一真相源(对齐 P2-B-17 ``CHAPTER_STATUS_VALUES`` 模式)。
"""


def _memory_health_severity_check_sql() -> str:
    """``MemoryHealthCheckORM.severity`` CHECK 约束 SQL 谓词(4 档 IN 列表)。"""
    quoted = ", ".join(f"'{v}'" for v in MEMORY_HEALTH_SEVERITY_VALUES)
    return f"severity IN ({quoted})"


class MetricsDailyORM(Base):
    """工作区每日聚合指标(对应 ``MetricsDaily`` Pydantic 模型,OBSERVABILITY
    §3.1 ``metrics_daily``)。

    P4-B-04 metrics_aggregator 在每章 APPROVED 后增量更新当日 row;同一
    工作区每天一行,``date`` 主键避免重复。**计数字段** ``NOT NULL DEFAULT 0``
    (无样本时 0 比 NULL 更合理);**平均值字段** ``NULL``(无样本时确实未
    定义)。
    """

    __tablename__ = "metrics_daily"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    chapters_drafted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chapters_approved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chapters_rewritten: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chapters_revised: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chapters_escalated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_review_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_rewrites_per_chapter: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_revises_per_chapter: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_chapter_latency_seconds: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    qwen_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claude_sonnet_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claude_opus_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class MetricsPerChapterORM(Base):
    """单章详细指标(对应 ``MetricsPerChapter`` Pydantic 模型,OBSERVABILITY
    §3.1 ``metrics_per_chapter``)。

    P4-B-03 ``_on_state_transition(to_state=APPROVED)`` 钩子一次性落库;
    ``cache_input_tokens`` / ``cache_hit_ratio`` 是 P4-plan-kickoff 决策 5
    拍板的 prompt cache 2 字段,默认 ``NULL``(disable)。

    **不加 FK** 到 ``chapters.chapter_id``:虽 P4 阶段 chapter 落库已通,
    但 metrics_per_chapter 的语义是"章节最终态指标",一旦落库即作为只读
    历史样本,即使章节后续被删(P5 OutlineBuilder 重建)也应保留;弱字符串
    引用更稳妥(对齐 P2-B-03 timeline_entries.chapter_id 决策)。
    """

    __tablename__ = "metrics_per_chapter"

    chapter_id: Mapped[str] = mapped_column(String, primary_key=True)
    drafted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    total_latency_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    rewrite_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revise_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # post-v6 Session A:物理列只为旧 SQLite / 历史报表可读而保留；新生产
    # 不再写入或输出多稿 metrics，故 Python 名称明确标成 legacy read-only。
    legacy_parallel_drafts_used: Mapped[bool] = mapped_column(
        "parallel_drafts_used", Boolean, nullable=False, default=False
    )
    final_score_overall: Mapped[float] = mapped_column(Float, nullable=False)
    final_score_plot_progression: Mapped[float] = mapped_column(Float, nullable=False)
    final_score_tension_match: Mapped[float] = mapped_column(Float, nullable=False)
    final_score_prose_quality: Mapped[float] = mapped_column(Float, nullable=False)
    final_score_consistency: Mapped[float] = mapped_column(Float, nullable=False)
    final_score_voice_consistency: Mapped[float] = mapped_column(Float, nullable=False)
    final_score_hook_management: Mapped[float] = mapped_column(Float, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False)
    target_word_count_min: Mapped[int] = mapped_column(Integer, nullable=False)
    target_word_count_max: Mapped[int] = mapped_column(Integer, nullable=False)
    target_tension_level: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_tension_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    writer_profile: Mapped[str] = mapped_column(String, nullable=False)
    writer_model: Mapped[str] = mapped_column(String, nullable=False)
    writer_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reviewer_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reviser_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_hit_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)


class SystemMetricsDailyORM(Base):
    """系统级每日聚合(对应 ``SystemMetricsDaily`` Pydantic 模型,
    OBSERVABILITY §3.1 ``system_metrics_daily``)。

    跨工作区聚合;``date`` 主键。P4-B-04 由 API 中间件级别聚合,与 §2.4
    ``api_request`` / ``api_error`` / ``ws_*`` / ``frontend_error`` /
    ``pipeline_*`` 事件耦合。
    """

    __tablename__ = "system_metrics_daily"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    api_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    api_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_api_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    ws_connections: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    frontend_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pipeline_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class MemoryHealthCheckORM(Base):
    """记忆健康度检查历史(对应 ``MemoryHealthCheck`` Pydantic 模型,
    OBSERVABILITY §3.1 ``memory_health_history``)。

    P4-B-09 MemoryHealthChecker agent 触发后落一行;``severity=critical``
    时流水线自动暂停 + WS 推送(P4-B-08 阈值告警联动)。

    ``severity`` 字段 4 档 CHECK 约束(``ok|minor|warning|critical``)。
    ``report_json`` spec 字面 TEXT 但项目模式以 ``sa.JSON`` 列存 ``dict``
    (SQLAlchemy 自动序列化);名字保留 ``_json`` 后缀以保持与 spec 字面
    一致。
    """

    __tablename__ = "memory_health_history"

    check_id: Mapped[str] = mapped_column(String, primary_key=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    severity: Mapped[str] = mapped_column(String, nullable=False)
    stale_threads_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inactive_characters_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    conflicts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    report_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )

    __table_args__ = (
        Index("ix_memory_health_history_checked_at", "checked_at"),
        CheckConstraint(
            _memory_health_severity_check_sql(),
            name="ck_memory_health_history_severity",
        ),
    )


STYLE_AUDIT_SEVERITY_VALUES: tuple[str, ...] = (
    "ok",
    "minor",
    "warning",
    "critical",
)
"""``StyleAuditEntryORM.severity`` 允许值集合(v1.1 wave 4 s5 / I2.2 StyleAuditor
落地时启用,对齐 Pydantic ``StyleAuditEntry.severity`` ``Literal``)。

P4-B-10 stub 阶段 severity 固定 ``"stub"`` 且无 CHECK 约束;v1.1 w4-s5 StyleAuditor
真 agent 落地后 severity 升正式 4 档,CHECK 约束 SQL 由
``_style_audit_severity_check_sql`` 构造,迁移脚本
``alembic/versions/0018_style_audit_severity_check.py`` 复用同一份枚举(fold
旧 ``stub`` → ``ok`` 后加约束),确保 ORM 与迁移单一真相源(对齐 P4-B-01
``MEMORY_HEALTH_SEVERITY_VALUES`` 模式)。
"""


def _style_audit_severity_check_sql() -> str:
    """``StyleAuditEntryORM.severity`` CHECK 约束 SQL 谓词(4 档 IN 列表)。"""
    quoted = ", ".join(f"'{v}'" for v in STYLE_AUDIT_SEVERITY_VALUES)
    return f"severity IN ({quoted})"


class StyleAuditEntryORM(Base):
    """风格漂移审计历史(对应 ``StyleAuditEntry`` Pydantic 模型,
    OBSERVABILITY §3.1 ``style_audit_history``)。

    P4-B-10 StyleAuditor agent 接口预留 stub(``severity="stub"``);v1.1 wave 4
    s5 / I2.2 StyleAuditor 真 agent 落地后复用同表 schema,severity 升级为正式
    4 档枚举 ``ok|minor|warning|critical``。

    **``severity`` CHECK 约束**(v1.1 w4-s5 / I2.2 起):维护人拍板 DB 层加锁
    (2026-06-01),由 ``_style_audit_severity_check_sql`` 构造,迁移
    ``0018_style_audit_severity_check`` 先 fold 旧 ``stub`` 行 → ``ok`` 再加
    约束。SSOT = ``STYLE_AUDIT_SEVERITY_VALUES``。
    """

    __tablename__ = "style_audit_history"

    audit_id: Mapped[str] = mapped_column(String, primary_key=True)
    audited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    chapter_range_start: Mapped[str | None] = mapped_column(String, nullable=True)
    chapter_range_end: Mapped[str | None] = mapped_column(String, nullable=True)
    drift_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    report_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )

    __table_args__ = (
        Index("ix_style_audit_history_audited_at", "audited_at"),
        CheckConstraint(
            _style_audit_severity_check_sql(),
            name="ck_style_audit_history_severity",
        ),
    )


TENSION_TEMPLATE_VALUES: tuple[str, ...] = (
    "heros_journey",
    "steady_daily",
    "strong_stimulation",
    "free",
)
"""``VolumeORM.tension_template`` 允许值集合(对齐 FRONTEND_SPEC §4.2 字面
4 模板"英雄之旅 / 平稳日常 / 强刺激爽文 / 自由")。

CHECK 约束 SQL 由 ``_tension_template_check_sql`` 构造,迁移脚本
``alembic/versions/0005_volumes_table.py`` 复用同一份枚举,确保 ORM 与
迁移单一真相源(对齐 P4-B-01 ``MEMORY_HEALTH_SEVERITY_VALUES`` / P2-B-17
``CHAPTER_STATUS_VALUES`` 模式)。
"""


def _tension_template_check_sql() -> str:
    """``VolumeORM.tension_template`` CHECK 约束 SQL 谓词(4 档 IN 列表)。"""
    quoted = ", ".join(f"'{v}'" for v in TENSION_TEMPLATE_VALUES)
    return f"tension_template IN ({quoted})"


class VolumeORM(Base):
    """卷(对应 ``Volume`` Pydantic 模型,PROJECT_SPEC §3 + ROADMAP §P5)。

    P5-B-01 引入。决策 2=C 处置:Volume 主表 + Arc / TensionCurvePoint
    走 JSON 嵌套(``arcs_json`` / ``tension_curve_json`` 用 ``sa.JSON``,
    对齐 P4-B-01 ``report_json`` 先例);outline 整体 SSOT 仍在
    ``workspaces/<id>/data/outline/volume_plans.json`` 文件,DB 是查询缓存。

    **CHECK 约束**:``tension_template IN (4 档)``(对齐 FRONTEND_SPEC §4.2
    字面);枚举值由 ``TENSION_TEMPLATE_VALUES`` 单一真相源提供。

    **复合主键 / 关系**:``volume_id`` 单列 PK + ``workspace_id`` FK CASCADE
    到 ``workspaces``(对齐 P1 ``chapters.workspace_id`` 模式)。
    """

    __tablename__ = "volumes"

    volume_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    chapter_count: Mapped[int] = mapped_column(Integer, nullable=False)
    tension_template: Mapped[str] = mapped_column(String, nullable=False)
    arcs_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    tension_curve_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("ix_volumes_workspace_id", "workspace_id"),
        CheckConstraint(
            _tension_template_check_sql(),
            name="ck_volumes_tension_template",
        ),
    )


CARD_STATUS_VALUES: tuple[str, ...] = (
    "draft",
    "pending_review",
    "approved",
    "rejected",
)
"""``ChapterCardORM.card_status`` 允许值集合(P5-B-02 起 4 档,对齐
PROJECT_SPEC §3.2 + plan-kickoff 决策 6=C ``CardStatus`` Literal)。

与 ``Chapter.status`` 7 档完全解耦:章节卡审核生命周期独立于章节正文生命
周期(决策 6=C),UI 上 ``ChapterStateBadge`` 与 ``CardStatusBadge`` 双显。

CHECK 约束 SQL 字符串由 ``_card_status_check_sql`` 构造,迁移脚本
``alembic/versions/0006_chapter_card_status.py`` 直接复用同一份枚举,确保
ORM 与迁移单一真相源(对齐 P2-B-17 ``CHAPTER_STATUS_VALUES`` + P4-B-01
``MEMORY_HEALTH_SEVERITY_VALUES`` + P5-B-01 ``TENSION_TEMPLATE_VALUES`` 模式)。
"""


def _card_status_check_sql() -> str:
    """``ChapterCardORM.card_status`` CHECK 约束 SQL 谓词(4 档 IN 列表)。"""
    quoted = ", ".join(f"'{v}'" for v in CARD_STATUS_VALUES)
    return f"card_status IN ({quoted})"


class ChapterCardORM(Base):
    """章节卡审核状态(对应 ``ChapterCard.card_status`` + ``card_approved_at``,
    PROJECT_SPEC §3.2 + ROADMAP §P5 验收 "前 3 章进入审核")。

    **P5-B-02 引入,最小列设计**(类 C 处置):章节卡完整内容仍走文件
    ``workspaces/<id>/data/cards/<chapter_id>.json``(SSOT),本表只缓存
    审核状态相关 2 字段 + chapter_id PK + workspace_id 索引,支持 P5-B-06
    ``chapter:write`` gate 高效检索 ``card_status != 'approved'``。

    设计动机(plan-kickoff 决策 2=C 先例):outline / volume 整体 SSOT 在
    文件,DB 是查询缓存;章节卡 P1 起就走文件 SSOT,P5-B-02 不改变 SSOT
    位置,只把审核状态字段同步落 DB 索引层(避免 P5-B-06 gate 检查时扫
    N 个 JSON 文件)。

    **CHECK 约束**:``card_status IN (4 档)``(对齐 PROJECT_SPEC §3.2 字面);
    枚举值由 ``CARD_STATUS_VALUES`` 单一真相源提供。

    **关系**:``chapter_id`` 单列 PK + ``workspace_id`` FK CASCADE 到
    ``workspaces``(对齐 P1 ``chapters.workspace_id`` 模式);**不**对
    ``chapters.chapter_id`` 加 FK — 章节卡先于章节正文创建(P5-B-06
    outline:commit 后批量生成卡片时 ``chapters`` 行尚未存在),弱字符串
    引用避免插入顺序耦合(对齐 P2-B-03 ``timeline_entries.chapter_id``
    决策)。
    """

    __tablename__ = "chapter_cards"

    chapter_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
    )
    card_status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="draft",
        server_default="draft",
    )
    card_approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    rejection_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    """``POST /chapters/{id}/card:reject`` 可选 body 字段持久化(P5-B-07 起,
    Alembic 0007 加列);未审核退回时为 ``None``。P6+ 若引入审核历史表,
    本字段保留作为最近一次退回理由的快照;短期(P5)够前端 ChapterCardReviewPage
    展示"上一次为什么被退回"用。"""

    force_profile: Mapped[str | None] = mapped_column(Text, nullable=True)
    """force_profile 字段(P6-B-01 引入,Alembic 0008 加列;**P6-B-09 真接入**):
    强制使用某 Writer profile(对齐 FRONTEND_SPEC §4.8 字面),P6-B-01 仅落
    列预留位,字段语义 + service 联通 留 P6-B-09 实施(对齐 P5-B-02 加列
    + service 联通分步先例)。常态 ``None``。"""

    __table_args__ = (
        Index("ix_chapter_cards_workspace_id", "workspace_id"),
        CheckConstraint(
            _card_status_check_sql(),
            name="ck_chapter_cards_status",
        ),
    )


PIPELINE_STATE_VALUES: tuple[str, ...] = (
    "idle",
    "running",
    "paused",
    "interrupted",
    "stopped",
    "error",
)
"""``PipelineRunORM.state`` 允许值集合(v2 w1-s2 起 6 档,对齐 P6-B-02
``PipelineState`` Literal 字面 + ``PipelineRunSnapshot.state`` 字段)。

6 档语义:

- ``idle``:占位,实际入库行不会用此值(``GET /status`` 无 active 行时虚构
  idle snapshot 返回)。**列入 CHECK 允许集合是为对齐 Literal 一致**,持久化
  侧实际只写 ``running`` / ``paused`` / ``interrupted`` / ``stopped`` /
  ``error``。
- ``running``:active 运行中(execute 循环正在跑章节)。
- ``paused``:user 暂停 / invalidated gate 自动暂停。
- ``interrupted``:v2 w1-s2 加(Alembic 0024,对齐 ``task_records.status``
  同名档语义)— 进程重启时 run 行残留 ``running`` 且数据完整可恢复
  (``chapter_ids`` 非空)→ lifespan 启动 hook 翻本档;**可 ``:resume``
  断点续跑**(区别于 ``error`` 死路只能 stop)。
- ``stopped``:user 终止 / 全章自然完成。
- ``error``:execute 循环未捕获异常 / 启动扫描发现行数据不完整(``chapter_ids``
  空,理论不可达兜底)。

CHECK 约束 SQL 由 ``_pipeline_state_check_sql`` 构造,迁移脚本
``alembic/versions/0009_pipeline_runs_table.py``(建表 5 档)+
``alembic/versions/0024_pipeline_runs_state_interrupted.py``(扩 6 档)复用
同一份枚举,确保 ORM 与迁移单一真相源(对齐 P2-B-17 ``CHAPTER_STATUS_VALUES``
+ P4-B-01 ``MEMORY_HEALTH_SEVERITY_VALUES`` + P5-B-01
``TENSION_TEMPLATE_VALUES`` 模式)。
"""


def _pipeline_state_check_sql() -> str:
    """``PipelineRunORM.state`` CHECK 约束 SQL 谓词(6 档 IN 列表)。"""
    quoted = ", ".join(f"'{v}'" for v in PIPELINE_STATE_VALUES)
    return f"state IN ({quoted})"


PIPELINE_SCOPE_VALUES: tuple[str, ...] = (
    "single",
    "range",
    "volume",
    "until",
)
"""``PipelineRunORM.scope`` 允许值集合(P6-B-03 起 4 档,对齐 P6-B-02
``PipelineScope`` Literal 字面 + plan §"P6-B-02" scope 4 档语义)。"""


def _pipeline_scope_check_sql() -> str:
    """``PipelineRunORM.scope`` CHECK 约束 SQL 谓词(4 档 IN 列表)。"""
    quoted = ", ".join(f"'{v}'" for v in PIPELINE_SCOPE_VALUES)
    return f"scope IN ({quoted})"


PIPELINE_FAILURE_POLICY_VALUES: tuple[str, ...] = (
    "stop",
    "skip",
    "retry",
)
"""``PipelineRunORM.failure_policy`` 允许值集合(v2 w1-s1 起 3 档,对齐
``PipelineFailurePolicy`` Literal 字面 + v2_plan §4 w1-s1 设计要点)。

3 档语义(章级失败 = 孤儿章升 human_intervention / 写章异常 / 卡生成失败):

- ``stop``(默认,推荐):孤儿章 → auto_pause reason=``chapter_escalated``
  (可人工解决后 resume);异常 → state=error。孤儿章需人工,跳过会让
  衔接接计划非实际。
- ``skip``:失败章记入 ``skipped_chapter_ids`` 继续写下一章(不 append
  completed;选章时跳过防死循环)。
- ``retry``:重试 1 次后仍失败 → 按 ``stop`` 处置。

CHECK 约束 SQL 由 ``_pipeline_failure_policy_check_sql`` 构造,迁移脚本
``alembic/versions/0023_pipeline_runs_failure_policy.py`` 复用同一份枚举,
确保 ORM 与迁移单一真相源(对齐 ``PIPELINE_STATE_VALUES`` 模式)。
"""


def _pipeline_failure_policy_check_sql() -> str:
    """``PipelineRunORM.failure_policy`` CHECK 约束 SQL 谓词(3 档 IN 列表)。"""
    quoted = ", ".join(f"'{v}'" for v in PIPELINE_FAILURE_POLICY_VALUES)
    return f"failure_policy IN ({quoted})"


class PipelineRunORM(Base):
    """流水线运行历史(对应 P6-B-02 ``PipelineRunSnapshot`` Pydantic 字段,
    PROJECT_SPEC §11 字面"长流程中断丢数据 → 状态持久化 + 自动备份"持久化半)。

    P6-B-03 引入:把 P6-B-02 内存版 ``_active_runs`` / ``_last_runs`` 字典
    迁到 SQLite 持久化,进程重启不丢 run state。启动时 lifespan hook 扫
    ``state='running'`` 行:数据完整可恢复(``chapter_ids`` 非空)→ 翻
    ``interrupted``(v2 w1-s2,可 ``:resume`` 断点续跑);不完整(理论
    不可达兜底)→ 翻 ``error``。两者均带 ``error_details={"reason":
    "engine_restarted_mid_run"}``(不自动续跑 — user 手动 resume)。

    **字段命名严格对齐 P6-B-02 ``PipelineRunSnapshot`` Pydantic**(避免
    重命名漂移):``run_id`` / ``workspace_id`` / ``state`` / ``scope`` /
    ``start_chapter_id`` / ``end_chapter_id`` / ``volume_id`` /
    ``profile_override`` / ``chapter_ids`` / ``current_chapter`` /
    ``completed_chapter_ids`` / ``pause_reason`` / ``error_details`` /
    ``failure_policy`` / ``skipped_chapter_ids``(v2 w1-s1 加)/
    ``created_at`` / ``updated_at`` / ``completed_at``。

    **CHECK 约束**:① ``state IN (6 档)`` 由 ``PIPELINE_STATE_VALUES`` 单一
    真相源提供(v2 w1-s2 扩 ``interrupted``,Alembic 0024);② ``scope IN
    (4 档)`` 由 ``PIPELINE_SCOPE_VALUES`` 单一真相源提供;③ ``failure_policy
    IN (3 档)`` 由 ``PIPELINE_FAILURE_POLICY_VALUES`` 单一真相源提供
    (v2 w1-s1,Alembic 0023)。

    **关系**:``run_id`` 单列 PK + ``workspace_id`` FK CASCADE 到
    ``workspaces``(对齐 P5-B-01 ``VolumeORM`` 同模式)。多列 JSON
    (``chapter_ids`` / ``completed_chapter_ids`` / ``error_details``)走
    ``sa.JSON``(对齐 P4-B-01 ``report_json`` + P5-B-01 ``arcs_json`` 同源)。
    """

    __tablename__ = "pipeline_runs"

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(String, nullable=False)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    start_chapter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    end_chapter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    volume_id: Mapped[str | None] = mapped_column(String, nullable=True)
    profile_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    chapter_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    current_chapter: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_chapter_ids: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    pause_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # v2 w1-s1:失败策略 3 档(stop/skip/retry,见 PIPELINE_FAILURE_POLICY_VALUES)
    # + 跳过章列表(skip 策略下失败章记录;选章时 completed ∪ skipped 都不再选)。
    # server_default 让 0023 迁移对旧行 backfill 为 stop / []。
    failure_policy: Mapped[str] = mapped_column(
        String, nullable=False, default="stop", server_default="stop"
    )
    skipped_chapter_ids: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    provider_call_cap: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1000"
    )
    """本 run 使用的真实 provider 物理调用 hard cap。

    计数按本 ``run_id`` 的 ``attempt_ledger`` 累计，因此 pause/resume 与
    进程重启不会获得免费调用；新批准的 run 持久化自己的活动上限。
    """

    provider_call_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    transport_retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    pipeline_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    upstream_generation_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    """R5 查询投影；由 ``attempt_ledger`` 插入事务内重算，不能独立累加。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_pipeline_runs_workspace_id", "workspace_id"),
        Index("ix_pipeline_runs_state", "state"),
        CheckConstraint(
            _pipeline_state_check_sql(),
            name="ck_pipeline_runs_state_6_levels",
        ),
        CheckConstraint(
            _pipeline_scope_check_sql(),
            name="ck_pipeline_runs_scope_4_levels",
        ),
        CheckConstraint(
            _pipeline_failure_policy_check_sql(),
            name="ck_pipeline_runs_failure_policy_3_levels",
        ),
    )


ATTEMPT_KIND_VALUES: tuple[str, ...] = ("pipeline", "provider")
ATTEMPT_STATUS_VALUES: tuple[str, ...] = ("started", "succeeded", "failed")
PROVIDER_USAGE_STATUS_VALUES: tuple[str, ...] = ("known", "unknown")
PROVIDER_RESPONSE_STATUS_VALUES: tuple[str, ...] = (
    "request_started",
    "completed",
    "failed",
    "stream_incomplete",
    "usage_missing",
)
CONFIGURED_COST_BASIS_VALUES: tuple[str, ...] = (
    "conservative_reserve",
    "metered_usage",
)


def _attempt_kind_check_sql() -> str:
    quoted = ", ".join(f"'{value}'" for value in ATTEMPT_KIND_VALUES)
    return f"attempt_kind IN ({quoted})"


def _attempt_status_check_sql() -> str:
    quoted = ", ".join(f"'{value}'" for value in ATTEMPT_STATUS_VALUES)
    return f"status IN ({quoted})"


class AttemptLedgerORM(Base):
    """W3.5-R5 不可刷新 attempt 账本。

    一行表示一次 pipeline 章处理尝试，或一次真实 provider 的物理 HTTP/SDK
    请求。provider client 内部 retry 会写多行，并用同一 ``logical_call_id``
    关联；``attempt_number`` 是该逻辑调用内的 1-based 物理序号。pipeline 行
    则按工作区+章节跨 run 单调编号。

    ``workspace_id`` / ``chapter_id`` / ``run_id`` 故意使用弱字符串引用：
    大纲 provider 调用可能早于 ``workspaces``/``chapters`` ORM 行，非 pipeline
    任务也没有 run；物理隔离由每工作区独立 SQLite 保证。
    """

    __tablename__ = "attempt_ledger"

    attempt_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    task_id: Mapped[str] = mapped_column(String, nullable=False)
    chapter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt_kind: Mapped[str] = mapped_column(String, nullable=False)
    operation_kind: Mapped[str] = mapped_column(String, nullable=False)
    logical_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    agent_name: Mapped[str | None] = mapped_column(String, nullable=True)
    provider: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_cap: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    version_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    version_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String, nullable=True)
    # 0033：provider 物理请求的权威计量结果。全部 nullable 是刻意的：迁移
    # 不猜历史行；历史 NULL 与新请求的显式 unknown 是两种不同状态。
    usage_status: Mapped[str | None] = mapped_column(String, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reserved_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reserved_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    configured_cost_currency: Mapped[str | None] = mapped_column(
        String(3), nullable=True
    )
    configured_input_cost_per_1k: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    configured_output_cost_per_1k: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    configured_cost_amount: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    configured_cost_basis: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    response_status: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index(
            "ix_attempt_ledger_workspace_kind_created",
            "workspace_id",
            "attempt_kind",
            "created_at",
        ),
        Index(
            "ix_attempt_ledger_run_kind",
            "run_id",
            "attempt_kind",
        ),
        Index(
            "ix_attempt_ledger_chapter_kind",
            "chapter_id",
            "attempt_kind",
        ),
        Index(
            "ix_attempt_ledger_task_agent_model",
            "task_id",
            "agent_name",
            "model",
        ),
        CheckConstraint(
            _attempt_kind_check_sql(),
            name="ck_attempt_ledger_kind_2_levels",
        ),
        CheckConstraint(
            _attempt_status_check_sql(),
            name="ck_attempt_ledger_status_3_levels",
        ),
        CheckConstraint(
            "attempt_number >= 1",
            name="ck_attempt_ledger_attempt_number_positive",
        ),
        CheckConstraint(
            "attempt_cap >= 1",
            name="ck_attempt_ledger_attempt_cap_positive",
        ),
        CheckConstraint(
            "usage_status IS NULL OR usage_status IN ('known', 'unknown')",
            name="ck_attempt_ledger_usage_status_2_levels",
        ),
        CheckConstraint(
            "response_status IS NULL OR response_status IN "
            "('request_started', 'completed', 'failed', 'stream_incomplete', "
            "'usage_missing')",
            name="ck_attempt_ledger_response_status_5_levels",
        ),
        CheckConstraint(
            "configured_cost_basis IS NULL OR configured_cost_basis IN "
            "('conservative_reserve', 'metered_usage')",
            name="ck_attempt_ledger_cost_basis_2_levels",
        ),
        CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name="ck_attempt_ledger_input_tokens_nonnegative",
        ),
        CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name="ck_attempt_ledger_output_tokens_nonnegative",
        ),
        CheckConstraint(
            "reserved_input_tokens IS NULL OR reserved_input_tokens >= 0",
            name="ck_attempt_ledger_reserved_input_tokens_nonnegative",
        ),
        CheckConstraint(
            "reserved_output_tokens IS NULL OR reserved_output_tokens >= 0",
            name="ck_attempt_ledger_reserved_output_tokens_nonnegative",
        ),
        CheckConstraint(
            "configured_input_cost_per_1k IS NULL OR "
            "configured_input_cost_per_1k >= 0",
            name="ck_attempt_ledger_input_rate_nonnegative",
        ),
        CheckConstraint(
            "configured_output_cost_per_1k IS NULL OR "
            "configured_output_cost_per_1k >= 0",
            name="ck_attempt_ledger_output_rate_nonnegative",
        ),
        CheckConstraint(
            "configured_cost_amount IS NULL OR configured_cost_amount >= 0",
            name="ck_attempt_ledger_cost_amount_nonnegative",
        ),
        CheckConstraint(
            "usage_status != 'known' OR "
            "(input_tokens IS NOT NULL AND output_tokens IS NOT NULL)",
            name="ck_attempt_ledger_known_usage_has_tokens",
        ),
    )


CHAPTER_SUMMARY_TRIGGER_VALUES: tuple[str, ...] = (
    "reviewer_pass",
    "force_pass",
)
"""``ChapterSummaryORM.extraction_trigger`` 允许值集合(wave 4 session 3a 起 2 档)。

2 档语义:

- ``reviewer_pass``:章节走完正常 reviewer 审稿流程,verdict=pass 后 approved
  时触发抽取。常态高质量来源。
- ``force_pass``:用户主动跳过 reviewer 通过(``POST /chapters/{id}:force-pass``),
  正文可能未达 reviewer 阈值。给 v1.1 / 会话 3b "只用高质量摘要作下游章节卡
  上下文"留筛选基础。

CHECK 约束 SQL 由 ``_chapter_summary_trigger_check_sql`` 构造,迁移脚本
``alembic/versions/0012_chapter_summary_table.py`` 复用同一份枚举,确保 ORM
与迁移单一真相源(对齐 P2-B-17 ``CHAPTER_STATUS_VALUES`` / P6-B-03
``PIPELINE_STATE_VALUES`` 同模式)。
"""


def _chapter_summary_trigger_check_sql() -> str:
    """``ChapterSummaryORM.extraction_trigger`` CHECK 约束 SQL 谓词(2 档 IN 列表)。"""
    quoted = ", ".join(f"'{v}'" for v in CHAPTER_SUMMARY_TRIGGER_VALUES)
    return f"extraction_trigger IN ({quoted})"


class ChapterSummaryORM(Base):
    """章节摘要(v1.0.x wave 4 session 3a 引入,长程记忆基础设施)。

    职责:保存章节事实摘要；W3.5-R4 起同时承担“下一章可继续”所需的同步
    连续性小账本。批准入口必须在 final/version/card/逐条 quote/hash 校验通过
    后写 ``validated_at``，章节卡端再读取该行并冻结 provenance。

    **与 ``ChapterCardORM`` 解耦**:章节卡是写作前的"计划"(must_achieve / 目标
    场景),章节摘要是写作后的"事实"(实际发生事件 / 视角 / 场景)。生命周期
    完全独立(对齐 P5-B-02 ``ChapterCardORM`` 与 ``ChapterORM`` 解耦先例)。

    **与 ``MemoryExtractor.extract()``(P2-B-09)解耦**:既有 extract() 是跨章
    实体抽取(character / plot_thread / timeline / fact 四类,SQLite + Chroma
    双写),本表是单章摘要(LLM 一次性产出本章 ground truth)。语义并列、不
    冲突,各自独立 LLM 调用。

    **关系**:``chapter_id`` 单列 PK + ``workspace_id`` FK CASCADE 到
    ``workspaces``(对齐 P5-B-02 ``ChapterCardORM`` 同模式);**不**对
    ``chapters.chapter_id`` 加 FK — 章节摘要在 approved 时落库,此时 chapters
    行已存在,但弱字符串引用避免插入顺序耦合 + 与 ``ChapterCardORM`` 同模式
    (对齐 P2-B-03 ``timeline_entries.chapter_id`` 决策)。

    **CHECK 约束**:``extraction_trigger IN (2 档)``(对齐 wave 4 session 3a
    Q2 软性 push back 接受);枚举值由 ``CHAPTER_SUMMARY_TRIGGER_VALUES`` 单一
    真相源提供。

    字段集设计(对齐 P5-B-06 ``chapter_card_service.derive_context`` 注释承诺 +
    给会话 3b 完整衔接素材):

    - ``summary_text``:本章核心摘要(~200-500 字 LLM 输出,会话 3b 注入下一
      章卡 LLM 上下文用)
    - ``pov_character``:本章主视角角色 ID(LLM 推断,可空 — 多视角 / 旁观时
      为 None)
    - ``scene_setting``:本章主场景描述(LLM 输出,可空)
    - ``key_events``:本章关键事件列表(LLM 输出,JSON list[dict],每条
      ``{description, actors, location}`` 三元组)
    - ``character_interactions``:本章角色互动(LLM 输出,JSON list[dict])
    - ``unfinished_actions``:本章未完动作(LLM 输出,JSON list[str];与
      ``continuity_extractor.unfinished_actions`` 字段名对齐,会话 3b 直接
      作 ``prev_unfinished_actions`` 注入)
    - ``character_states / established_facts / revealed_mechanics``:R4 同步
      小账本新增的跨章硬事实
    - ``raw_final_tail``:由代码从同一 ``final.md`` 确定性截取的末尾 800 字
    - ``source_version_id / source_text_hash / ledger_hash``:R4 来源版本、
      正文 hash 与可复算账本 hash
    - ``evidence_records``:每条结构事实到原文 quote / fingerprint 的逐条映射
    - ``validated_at``:最近一次通过 final/card/version/quote 校验的时间
    - ``extraction_trigger``:抽取触发来源(reviewer_pass / force_pass,Q2 软性
      字段)
    - ``model_used``:LLM 模型名(qwen-novel 等,审计追溯)
    - ``created_at`` / ``updated_at``:时间戳
    """

    __tablename__ = "chapter_summaries"

    chapter_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
    )
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    pov_character: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_setting: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_events: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    character_interactions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    unfinished_actions: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    character_states: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, server_default="[]"
    )
    established_facts: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, server_default="[]"
    )
    revealed_mechanics: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, server_default="[]"
    )
    core_state_candidates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, server_default="[]"
    )
    """现有 ChapterSummaryExtractor 同次提出的带 exact evidence 核心候选。

    模型只提名；``CoreStateService`` 以纯代码写回 admission_status/reason。
    SOFT/冲突候选保留审计，但不进入当前权威投影。
    """
    raw_final_tail: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    source_version_id: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    source_text_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=""
    )
    evidence_records: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, server_default="[]"
    )
    ledger_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=""
    )
    validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    extraction_trigger: Mapped[str] = mapped_column(String, nullable=False)
    model_used: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("ix_chapter_summaries_workspace_id", "workspace_id"),
        CheckConstraint(
            _chapter_summary_trigger_check_sql(),
            name="ck_chapter_summaries_extraction_trigger",
        ),
    )


CORE_STATE_REVISION_STATUS_VALUES: tuple[str, ...] = (
    "active",
    "superseded",
    "retracted",
)


def _core_state_revision_status_check_sql() -> str:
    quoted = ", ".join(f"'{value}'" for value in CORE_STATE_REVISION_STATUS_VALUES)
    return f"status IN ({quoted})"


class CoreStateRevisionORM(Base):
    """承重物件状态与主时间线书内时钟的可撤回 revision 账。

    角色不可逆状态继续以 ``CharacterORM.state_timeline`` 为权威，避免再造
    第二套角色真值；本表只保存 object 与 book_clock。唯一写入方是
    ``CoreStateService``，富记忆和 Timeline merger 没有写权限。
    """

    __tablename__ = "core_state_revisions"

    revision_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
    )
    state_key: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[str] = mapped_column(String, nullable=False)
    attribute: Mapped[str] = mapped_column(String, nullable=False)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    value_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(String, nullable=False)
    source_chapter_id: Mapped[str] = mapped_column(String, nullable=False)
    source_version_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_quote: Mapped[str] = mapped_column(Text, nullable=False)
    valid_from_chapter: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_to_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    supersedes_revision_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    change_reason: Mapped[str] = mapped_column(Text, nullable=False)
    ledger_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    retracted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index(
            "ix_core_state_revisions_workspace_state",
            "workspace_id",
            "state_key",
            "status",
        ),
        Index(
            "ix_core_state_revisions_workspace_source_chapter",
            "workspace_id",
            "source_chapter_id",
        ),
        Index(
            "uq_core_state_revisions_one_active",
            "workspace_id",
            "state_key",
            unique=True,
            sqlite_where=text("status = 'active'"),
        ),
        CheckConstraint(
            _core_state_revision_status_check_sql(),
            name="ck_core_state_revisions_status",
        ),
        CheckConstraint(
            "entity_type IN ('object', 'book_clock')",
            name="ck_core_state_revisions_entity_type",
        ),
        CheckConstraint(
            "source_version_id >= 1",
            name="ck_core_state_revisions_source_version_positive",
        ),
        CheckConstraint(
            "valid_from_chapter >= 1",
            name="ck_core_state_revisions_valid_from_positive",
        ),
    )
