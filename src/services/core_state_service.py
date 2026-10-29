"""核心状态/书内时钟唯一写入服务。

正常路径只有本服务能把 ChapterSummary 候选提升为当前权威；Timeline、
MemoryExtractor 与 rich-memory merger 没有写入入口。所有写入都绑定批准
``final.md`` 的 chapter/version/hash/evidence，并由 ChapterCard 预授权变化。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from src.core.db.models import (
    ChapterORM,
    ChapterCardORM,
    ChapterOperationLeaseORM,
    ChapterSummaryORM,
    ChapterVersionORM,
    CharacterORM,
    CoreStateRevisionORM,
    ReviewResultORM,
)
from src.core.models.chapter_card import ChapterCard, chapter_seq_from_id
from src.core.models.character import (
    CharacterCoreFact,
    CharacterStateSnapshot,
)
from src.core.models.continuity_ledger import (
    ContinuityEvidenceRecord,
    compute_continuity_ledger_hash,
    sha256_text,
)
from src.core.models.core_state import (
    CORE_STATE_RELATION_ATTRIBUTES,
    BookClockConstraint,
    BookClockInterval,
    BookTimeAbsoluteAnchor,
    BookTimeNode,
    BookTimeProjection,
    CoreStateCandidate,
    CoreStateContractError,
    CoreStateFact,
    CoreStateProjection,
    CoreStateTransition,
    advance_book_time_projection,
    build_book_clock_constraint,
    build_typed_book_clock_constraint,
    canonical_core_state_value,
    compute_book_time_relation_chain_hash,
    make_core_state_projection,
    render_core_state_capsule,
    validate_core_state_transition_sources,
)
from src.core.models.outline import CharacterOutlineItem, CharactersOutput


_log = structlog.get_logger(__name__)

SessionFactory = Callable[[str], Session]
_BOOK_CLOCK_STATE_KEY = "book_clock:main:elapsed_minutes"
_MAX_NEW_CORE_FACTS_PER_CHAPTER = 2
_MISSING_TRANSITION_EVIDENCE_PREFIX = (
    "章节卡获准变化缺少批准正文核心候选证据: "
)
_CHARACTER_IRREVERSIBLE_ATTRIBUTES = frozenset(
    {"life_status", "permanent_injury", "amnesia"}
)
_AMBIGUOUS_EVIDENCE_MARKERS = (
    "似乎",
    "仿佛",
    "也许",
    "可能",
    "猜",
    "传闻",
    "听说",
    "梦中",
    "梦里",
    "回忆中",
    "声称",
)
_RETAINED_STATE_DELTA_SEPARATORS = frozenset("，,；;。.:：、")
_STABLE_CODE_OBJECT_ID_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+$"
)
_LEGACY_OBJECT_ALIAS_SUFFIX_RE = re.compile(r"^[\u3400-\u9fff]{1,8}$")
_OBJECT_ATTRIBUTE_EVIDENCE_MARKERS: dict[str, tuple[str, ...]] = {
    "physical_form": ("形态", "形状", "结构"),
    "surface": ("表面", "纹理", "覆盖"),
    "holder": ("持有", "保管", "掌握", "交给"),
    "possession": ("持有", "保管", "收下", "交给", "递给"),
    "control": ("授权", "控制", "决定", "移交"),
    "access": ("访问", "操作", "打开", "进入", "授权"),
    "location": ("位于", "转移", "搬入", "送往"),
    "integrity": ("完整", "破损", "裂", "断"),
    "seal_status": ("封存", "封箱", "封条", "封锁"),
    "open_state": ("开启", "打开", "闭合", "封闭"),
}


class CoreStateServiceError(CoreStateContractError):
    """核心状态来源、合同、准入或事务不满足闭环。"""


class CoreStateConflictError(CoreStateServiceError):
    """批准正文候选与当前权威冲突，且没有匹配的章节卡迁移许可。"""

    def __init__(self, *, chapter_id: str, conflicts: list[str]) -> None:
        self.chapter_id = chapter_id
        self.conflicts = list(conflicts)
        super().__init__(
            f"核心状态提交冲突: chapter={chapter_id}: " + "；".join(conflicts)
        )


class CoreStateCommitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chapter_id: str
    source_version_id: int = Field(ge=1)
    status: str
    commit_hash: str
    projection_before_hash: str
    projection_after_hash: str
    admitted_count: int = Field(ge=0)
    reaffirmed_count: int = Field(ge=0)
    soft_count: int = Field(ge=0)


class CoreStateAuditItem(BaseModel):
    """API/UI 使用的来源、有效期和冲突统一视图。"""

    model_config = ConfigDict(extra="forbid")

    item_type: str
    revision_id: str | None = None
    state_key: str
    entity_type: str
    entity_id: str
    attribute: str
    value: str
    status: str
    source_chapter_id: str
    source_version_id: int
    source_path: str
    source_text_hash: str
    evidence_quote: str
    valid_from_chapter: int
    valid_to_chapter: int | None = None
    reason: str = ""


def _canonical_value(value: str) -> str:
    return canonical_core_state_value(value)


def _chapter_card_hash(card: ChapterCard) -> str:
    payload = card.model_dump(mode="json", exclude={"card_approved_at"})
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return sha256_text(canonical)


def _commit_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _card_structured_identity_text(card: ChapterCard) -> str:
    return "\n".join(
        [
            card.title,
            card.setting,
            *card.must_achieve,
            *card.must_unresolved,
            *card.forbidden,
            card.next_chapter_seed,
            card.continuity_context.continuity_bridge,
        ]
    )


def _core_candidates_for_ledger_hash(candidates: list[dict[str, Any]]) -> list[dict]:
    return [
        CoreStateCandidate.model_validate(item).model_dump(
            mode="json", exclude={"admission_status", "admission_reason"}
        )
        for item in candidates
    ]


def _longest_shared_attribute_phrase(
    *,
    candidate_value: str,
    evidence_quote: str,
    markers: tuple[str, ...],
) -> str:
    """取两段逐字共有且含属性信号的最长片段；不做同义词或模糊归并。"""

    left = candidate_value.strip()
    right = evidence_quote.strip()
    best = ""
    for start in range(len(left)):
        for end in range(start + 4, len(left) + 1):
            part = left[start:end].strip(" ，。；：、\t\r\n")
            if len(part) <= len(best) or not any(marker in part for marker in markers):
                continue
            if part in right:
                best = part
    return best


class CoreStateService:
    """构建投影、冻结卡片、提交批准事实、撤回和审计。"""

    def __init__(self, *, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _begin_immediate(session: Session) -> None:
        if not session.in_transaction():
            session.execute(text("BEGIN IMMEDIATE"))

    @staticmethod
    def _active_object_rows(
        session: Session, *, workspace_id: str
    ) -> list[CoreStateRevisionORM]:
        return list(
            session.scalars(
                select(CoreStateRevisionORM)
                .where(CoreStateRevisionORM.workspace_id == workspace_id)
                .where(CoreStateRevisionORM.status == "active")
                .order_by(
                    CoreStateRevisionORM.state_key,
                    CoreStateRevisionORM.valid_from_chapter,
                )
            )
        )

    @staticmethod
    def _projection_object_rows(
        session: Session,
        *,
        workspace_id: str,
        before_chapter: int | None,
    ) -> list[CoreStateRevisionORM]:
        if before_chapter is None:
            return CoreStateService._active_object_rows(
                session, workspace_id=workspace_id
            )
        as_of_chapter = before_chapter - 1
        return list(
            session.scalars(
                select(CoreStateRevisionORM)
                .where(CoreStateRevisionORM.workspace_id == workspace_id)
                .where(CoreStateRevisionORM.status != "retracted")
                .where(CoreStateRevisionORM.valid_from_chapter <= as_of_chapter)
                .where(
                    (CoreStateRevisionORM.valid_to_chapter.is_(None))
                    | (CoreStateRevisionORM.valid_to_chapter >= as_of_chapter)
                )
                .order_by(
                    CoreStateRevisionORM.state_key,
                    CoreStateRevisionORM.valid_from_chapter,
                )
            )
        )

    @staticmethod
    def _book_clock_history_rows(
        session: Session,
        *,
        workspace_id: str,
        before_chapter: int | None,
    ) -> list[CoreStateRevisionORM]:
        statement = (
            select(CoreStateRevisionORM)
            .where(CoreStateRevisionORM.workspace_id == workspace_id)
            .where(CoreStateRevisionORM.entity_type == "book_clock")
            .where(CoreStateRevisionORM.status != "retracted")
        )
        if before_chapter is not None:
            statement = statement.where(
                CoreStateRevisionORM.valid_from_chapter < before_chapter
            )
        return list(
            session.scalars(
                statement.order_by(CoreStateRevisionORM.valid_from_chapter)
            )
        )

    @classmethod
    def _decode_book_time_projection(
        cls,
        session: Session,
        *,
        workspace_id: str,
        before_chapter: int | None,
        current_row: CoreStateRevisionORM | None,
    ) -> tuple[BookTimeProjection | None, Literal[0, 1]]:
        """读取 v1 snapshot；v0 只恢复已知锚，不猜任何历史关系。"""

        if current_row is None:
            return None, 0
        current_value = dict(current_row.value_json or {})
        if current_value.get("clock_contract_version") == 1:
            raw_projection = current_value.get("book_time")
            try:
                return BookTimeProjection.model_validate(raw_projection), 1
            except ValidationError as exc:
                raise CoreStateServiceError(
                    f"book clock v1 持久化 snapshot 损坏: {exc}"
                ) from exc

        history = cls._book_clock_history_rows(
            session,
            workspace_id=workspace_id,
            before_chapter=before_chapter,
        )
        latest_known: CoreStateRevisionORM | None = None
        for row in history:
            if dict(row.value_json or {}).get("known") is True:
                latest_known = row

        latest_anchor: BookTimeAbsoluteAnchor | None = None
        latest_mainline_node_id: str | None = None
        known_node: BookTimeNode | None = None
        if latest_known is not None:
            value = dict(latest_known.value_json or {})
            interval = BookClockInterval(
                minimum_minutes=int(value["minimum_minutes"]),
                maximum_minutes=int(value["maximum_minutes"]),
            )
            node_id = hashlib.sha256(
                f"legacy-v0:{latest_known.revision_id}".encode("utf-8")
            ).hexdigest()
            known_node = BookTimeNode(
                node_id=node_id,
                chapter_id=latest_known.source_chapter_id,
                kind=(
                    "exact"
                    if interval.minimum_minutes == interval.maximum_minutes
                    else "bounded_interval"
                ),
                raw_time_gap=latest_known.evidence_quote or "legacy v0 numeric clock",
                gap_interval=interval,
                absolute_interval=interval,
            )
            latest_anchor = BookTimeAbsoluteAnchor(
                node_id=node_id,
                interval=interval,
            )
            latest_mainline_node_id = node_id

        if (
            current_value.get("known") is True
            and latest_known is not None
            and latest_known.revision_id == current_row.revision_id
        ):
            current_node = known_node
        else:
            current_node = BookTimeNode(
                node_id=hashlib.sha256(
                    f"legacy-v0-unknown:{current_row.revision_id}".encode("utf-8")
                ).hexdigest(),
                chapter_id=current_row.source_chapter_id,
                kind="genuinely_unanchored",
                raw_time_gap=current_row.evidence_quote or "legacy v0 unknown clock",
                unanchored_reason="legacy_unknown",
            )
        chain: tuple[BookTimeNode, ...] = ()
        return (
            BookTimeProjection(
                contract_version=0,
                current_node=current_node,
                latest_mainline_node_id=latest_mainline_node_id,
                latest_absolute_anchor=latest_anchor,
                active_relation_chain=chain,
                relation_chain_hash=compute_book_time_relation_chain_hash(
                    latest_absolute_anchor=latest_anchor,
                    active_relation_chain=chain,
                ),
            ),
            0,
        )

    @staticmethod
    def _character_projection_facts(
        session: Session,
        *,
        workspace_id: str,
        before_chapter: int | None,
    ) -> list[CoreStateFact]:
        facts: list[CoreStateFact] = []
        rows = list(
            session.scalars(
                select(CharacterORM)
                .where(CharacterORM.workspace_id == workspace_id)
                .where(CharacterORM.revoked_at.is_(None))
                .order_by(CharacterORM.character_id)
            )
        )
        for row in rows:
            snapshots: list[CharacterStateSnapshot] = []
            for raw in row.state_timeline or []:
                try:
                    snapshot = CharacterStateSnapshot.model_validate(raw)
                except ValidationError as exc:
                    raise CoreStateServiceError(
                        f"角色 {row.character_id} state_timeline 损坏: {exc}"
                    ) from exc
                if before_chapter is None or snapshot.as_of_chapter < before_chapter:
                    snapshots.append(snapshot)
            if not snapshots:
                continue
            latest = max(snapshots, key=lambda item: item.as_of_chapter)
            as_of_chapter = (
                latest.as_of_chapter if before_chapter is None else before_chapter - 1
            )
            for fact in latest.core_facts:
                if fact.valid_from_chapter > as_of_chapter or (
                    fact.valid_to_chapter is not None
                    and fact.valid_to_chapter < as_of_chapter
                ):
                    continue
                facts.append(
                    CoreStateFact(
                        revision_id=(
                            f"character:{row.character_id}:"
                            f"{fact.source_chapter_id}:{fact.state_key}"
                        ),
                        state_key=fact.state_key,
                        entity_type="character",
                        entity_id=row.character_id,
                        attribute=fact.attribute,
                        value=fact.value,
                        source_kind=fact.source_kind,
                        source_chapter_id=fact.source_chapter_id,
                        source_version_id=fact.source_version_id,
                        source_path=fact.source_path,
                        source_text_hash=fact.source_text_hash,
                        evidence_quote=fact.evidence_quote,
                        valid_from_chapter=fact.valid_from_chapter,
                        valid_to_chapter=fact.valid_to_chapter,
                    )
                )
        return facts

    @classmethod
    def _projection_from_session(
        cls,
        session: Session,
        *,
        workspace_id: str,
        before_chapter: int | None = None,
    ) -> CoreStateProjection:
        facts: list[CoreStateFact] = cls._character_projection_facts(
            session,
            workspace_id=workspace_id,
            before_chapter=before_chapter,
        )
        book_clock: BookClockInterval | None = None
        current_clock_row: CoreStateRevisionORM | None = None
        for row in cls._projection_object_rows(
            session,
            workspace_id=workspace_id,
            before_chapter=before_chapter,
        ):
            if row.entity_type == "book_clock":
                current_clock_row = row
                value = dict(row.value_json or {})
                if value.get("known") is True:
                    book_clock = BookClockInterval(
                        minimum_minutes=int(value["minimum_minutes"]),
                        maximum_minutes=int(value["maximum_minutes"]),
                    )
                else:
                    book_clock = None
                continue
            facts.append(
                CoreStateFact(
                    revision_id=row.revision_id,
                    state_key=row.state_key,
                    entity_type="object",
                    entity_id=row.entity_id,
                    attribute=row.attribute,  # type: ignore[arg-type]
                    value=row.value_text,
                    source_kind=row.source_kind,  # type: ignore[arg-type]
                    source_chapter_id=row.source_chapter_id,
                    source_version_id=row.source_version_id,
                    source_path=row.source_path,
                    source_text_hash=row.source_text_hash,
                    evidence_quote=row.evidence_quote,
                    valid_from_chapter=row.valid_from_chapter,
                    valid_to_chapter=row.valid_to_chapter,
                )
            )
        book_time, book_time_contract_version = cls._decode_book_time_projection(
            session,
            workspace_id=workspace_id,
            before_chapter=before_chapter,
            current_row=current_clock_row,
        )
        return make_core_state_projection(
            facts=facts,
            book_clock=book_clock,
            book_time=book_time,
            book_time_contract_version=book_time_contract_version,
        )

    def load_projection(
        self,
        workspace_id: str,
        *,
        before_chapter: int | None = None,
    ) -> CoreStateProjection:
        session = self._session_factory(workspace_id)
        try:
            return self._projection_from_session(
                session,
                workspace_id=workspace_id,
                before_chapter=before_chapter,
            )
        finally:
            session.close()

    @staticmethod
    def _validate_transition_sources(
        *,
        transitions: list[CoreStateTransition],
        projection: CoreStateProjection,
        card: ChapterCard | None = None,
        source_outline_dir: Path | None = None,
    ) -> None:
        from src.core.rules.drama_contracts import (
            DramaticSourceContractError, load_and_validate_card_dramatic_source,
        )

        try:
            validate_core_state_transition_sources(
                transitions=transitions,
                projection=projection,
                dramatic_source=card.dramatic_source if card is not None else None,
                related_character_ids=card.related_characters if card is not None else (),
            )
            current_ids = {fact.entity_id for fact in projection.facts if fact.entity_type == "object"}
            new_ids = {item.entity_id for item in transitions if item.entity_type == "object"
                       and item.attribute in {"possession", "control", "access"}
                       and item.entity_id not in current_ids}
            if len(new_ids) > 1 and card is not None and source_outline_dir is not None:
                # 恢复/提交不能只相信卡内自洽 hash，还要复核当前上游 SSOT。
                load_and_validate_card_dramatic_source(card=card, outline_dir=source_outline_dir)
        except (CoreStateContractError, DramaticSourceContractError) as exc:
            raise CoreStateServiceError(str(exc)) from exc

    @staticmethod
    def _expected_book_clock(
        *,
        card: ChapterCard,
        projection: CoreStateProjection,
        chapter_seq: int,
    ) -> BookClockConstraint:
        existing = card.book_clock_constraint
        if (
            card.core_state_contract_version == 1
            and existing is not None
            and existing.contract_version == 0
        ):
            return build_book_clock_constraint(
                time_gap=card.continuity_context.time_gap,
                previous_interval=projection.book_clock,
                is_first_chapter=chapter_seq == 1,
            )
        return build_typed_book_clock_constraint(
            chapter_id=card.chapter_id,
            time_gap=card.continuity_context.time_gap,
            previous_projection=projection.book_time,
            previous_projection_hash=projection.projection_hash,
            is_first_chapter=chapter_seq == 1,
        )

    def freeze_card_contract(
        self,
        *,
        card: ChapterCard,
        enabled: bool,
    ) -> ChapterCard:
        """在卡生成/人工保存时冻结当前投影与代码时钟。"""

        if not enabled:
            return card.model_copy(
                update={
                    "core_state_contract_version": 0,
                    "core_state_projection_hash": "",
                    "core_state_transitions": [],
                    "book_clock_constraint": None,
                }
            )
        seq = chapter_seq_from_id(card.chapter_id)
        if seq is None:
            raise CoreStateServiceError(
                f"核心状态合同要求规范 chapter_id: {card.chapter_id!r}"
            )
        projection = self.load_projection(card.workspace_id, before_chapter=seq)
        transitions = list(card.core_state_transitions)
        self._validate_transition_sources(
            card=card,
            transitions=transitions,
            projection=projection,
        )
        clock = self._expected_book_clock(
            card=card,
            projection=projection,
            chapter_seq=seq,
        )
        render_core_state_capsule(
            projection=projection,
            transitions=transitions,
            book_clock_constraint=clock,
        )
        return card.model_copy(
            update={
                "core_state_contract_version": 1,
                "core_state_projection_hash": projection.projection_hash,
                "book_clock_constraint": clock,
            }
        )

    def validate_card_contract(
        self,
        *,
        workspace_id: str,
        card: ChapterCard,
    ) -> CoreStateProjection | None:
        """approve/Writer 前验证卡片没有拿旧状态写作。"""

        if card.core_state_contract_version == 0:
            return None
        seq = chapter_seq_from_id(card.chapter_id)
        if seq is None:
            raise CoreStateServiceError("核心状态卡片 chapter_id 无法解析")
        projection = self.load_projection(workspace_id, before_chapter=seq)
        if projection.projection_hash != card.core_state_projection_hash:
            raise CoreStateServiceError(
                "章节卡核心状态投影已陈旧: "
                f"frozen={card.core_state_projection_hash} "
                f"current={projection.projection_hash}"
            )
        self._validate_transition_sources(
            card=card,
            transitions=list(card.core_state_transitions),
            projection=projection,
        )
        expected_clock = self._expected_book_clock(
            card=card,
            projection=projection,
            chapter_seq=seq,
        )
        if expected_clock != card.book_clock_constraint:
            raise CoreStateServiceError("章节卡 book_clock_constraint 已陈旧或被改写")
        render_core_state_capsule(
            projection=projection,
            transitions=card.core_state_transitions,
            book_clock_constraint=expected_clock,
        )
        return projection

    def prepare_legacy_initial_baseline_recovery(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        paths: Any,
    ) -> int:
        """为严格匹配的首章旧现场补建正文前角色基线，零 Provider。

        该入口只供用户把发生过核心状态冲突的**已拒绝首章卡**重新批准时调用。
        它不从一个关键词猜状态：同一候选值必须逐字出现在该角色至少两个相互
        独立的稳定 outline 字段中，且 DB、卡片、终稿、版本、摘要与此前冲突
        provenance 全部一致。普通 rejected 卡、后续章节或已有状态现场均 no-op /
        fail closed。
        """

        if chapter_id != "ch-0001":
            return 0
        outline_items = self._load_outline_character_items(paths)
        session = self._session_factory(workspace_id)
        try:
            self._begin_immediate(session)
            candidates, source_text_hash = self._pending_initial_conflict_candidates(
                session,
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                paths=paths,
                required_card_status="rejected",
            )
            if not candidates:
                session.rollback()
                return 0
            if len(candidates) > _MAX_NEW_CORE_FACTS_PER_CHAPTER:
                raise CoreStateServiceError("首章旧基线恢复候选超过每章核心事实上限")

            by_character: dict[str, list[CoreStateCandidate]] = {}
            for candidate in candidates:
                by_character.setdefault(candidate.entity_id, []).append(candidate)

            seeded = 0
            for character_id, items in by_character.items():
                outline_item = outline_items.get(character_id)
                if outline_item is None:
                    raise CoreStateServiceError(
                        f"首章旧基线恢复找不到 outline 角色: {character_id}"
                    )
                row = session.get(CharacterORM, character_id)
                if row is None or row.workspace_id != workspace_id:
                    raise CoreStateServiceError(
                        f"首章旧基线恢复找不到 DB 角色: {character_id}"
                    )
                if (
                    row.archetype != outline_item.archetype
                    or row.background != outline_item.background
                    or list(row.identity_facets or [])
                    != [
                        facet.model_dump(mode="json")
                        for facet in outline_item.identity_facets
                    ]
                ):
                    raise CoreStateServiceError(
                        f"首章旧基线恢复检测到 outline/DB 角色来源漂移: {character_id}"
                    )

                expected_facts: list[CharacterCoreFact] = []
                supporting_quotes: list[str] = []
                for candidate in items:
                    sources = self._legacy_candidate_outline_sources(
                        item=outline_item,
                        value=candidate.value,
                    )
                    if len(sources) < 2:
                        raise CoreStateServiceError(
                            "首章旧基线恢复缺少两个独立上游逐字证据: "
                            f"state_key={candidate.state_key} value={candidate.value!r}"
                        )
                    source_payload = {
                        field: text for field, text in sorted(sources.items())
                    }
                    source_hash = sha256_text(
                        json.dumps(
                            source_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                    expected_facts.append(
                        CharacterCoreFact(
                            state_key=candidate.state_key,
                            attribute=candidate.attribute,  # type: ignore[arg-type]
                            value=candidate.value,
                            source_kind="outline_characters",
                            source_chapter_id="outline:characters",
                            source_version_id=0,
                            source_path=(
                                "outline/characters.json.characters"
                                f"[{character_id}].{{"
                                + ",".join(sorted(sources))
                                + "}"
                            ),
                            source_text_hash=source_hash,
                            evidence_quote=candidate.value,
                            valid_from_chapter=0,
                        )
                    )
                    supporting_quotes.extend(source_payload.values())

                existing_snapshots = [
                    CharacterStateSnapshot.model_validate(raw)
                    for raw in (row.state_timeline or [])
                ]
                expected_facts.sort(key=lambda fact: fact.state_key)
                if existing_snapshots:
                    if len(existing_snapshots) != 1:
                        raise CoreStateServiceError(
                            f"首章旧基线恢复检测到已有角色时序现场: {character_id}"
                        )
                    existing = existing_snapshots[0]
                    if (
                        existing.as_of_chapter != 0
                        or existing.authority != "outline_characters"
                        or existing.core_facts != expected_facts
                    ):
                        raise CoreStateServiceError(
                            f"首章旧基线恢复检测到非同源已有基线: {character_id}"
                        )
                    for fact in expected_facts:
                        if (row.current_state or {}).get(
                            f"core.{fact.attribute}"
                        ) != fact.value:
                            raise CoreStateServiceError(
                                f"首章旧基线恢复 current_state 与既有基线不一致: "
                                f"{fact.state_key}"
                            )
                    continue

                current_state = dict(row.current_state or {})
                if any(str(key).startswith("core.") for key in current_state):
                    raise CoreStateServiceError(
                        f"首章旧基线恢复检测到无时序来源的 core.* 状态: {character_id}"
                    )
                item_hash = sha256_text(
                    json.dumps(
                        outline_item.model_dump(mode="json"),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                snapshot = CharacterStateSnapshot(
                    as_of_chapter=0,
                    authority="outline_characters",
                    source_chapter_id="outline:characters",
                    source_version_id=0,
                    source_text_hash=item_hash,
                    evidence_quotes=list(dict.fromkeys(supporting_quotes)),
                    core_facts=expected_facts,
                )
                row.state_timeline = [snapshot.model_dump(mode="json")]
                for fact in expected_facts:
                    current_state[f"core.{fact.attribute}"] = fact.value
                row.current_state = current_state
                seeded += len(expected_facts)

            _log.info(
                "legacy_initial_core_baseline_recovered",
                workspace_id=workspace_id,
                chapter_id=chapter_id,
                seeded_count=seeded,
                source_text_hash=source_text_hash,
                state_keys=[item.state_key for item in candidates],
            )
            session.commit()
            return seeded
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def require_pending_conflict_recovery_safe(
        self,
        *,
        session: Session,
        workspace_id: str,
        card: ChapterCard,
        paths: Any,
    ) -> None:
        """确认旧冲突已由同源 baseline 解决，且重批卡冻结了当前投影。"""

        candidates, _source_text_hash = self._pending_initial_conflict_candidates(
            session,
            workspace_id=workspace_id,
            chapter_id=card.chapter_id,
            paths=paths,
            required_card_status="approved",
        )
        if not candidates:
            raise CoreStateServiceError("核心状态恢复找不到原始首章冲突候选")
        projection = self._projection_from_session(
            session,
            workspace_id=workspace_id,
            before_chapter=1,
        )
        current = {fact.state_key: fact for fact in projection.facts}
        for candidate in candidates:
            active = current.get(candidate.state_key)
            if (
                active is None
                or active.source_kind != "outline_characters"
                or _canonical_value(active.value)
                != _canonical_value(candidate.value)
                or active.valid_from_chapter != 0
            ):
                raise CoreStateServiceError(
                    "原始首章冲突尚未由正文前同源基线解决: "
                    f"{candidate.state_key}"
                )
        if card.core_state_contract_version != 1:
            raise CoreStateServiceError("核心状态恢复要求 v1 章节卡合同")
        if card.core_state_projection_hash != projection.projection_hash:
            raise CoreStateServiceError("重批章节卡没有冻结恢复后的当前投影")
        self._validate_transition_sources(
            card=card,
            source_outline_dir=paths.outline_dir,
            transitions=list(card.core_state_transitions), projection=projection
        )
        expected_clock = self._expected_book_clock(
            card=card,
            projection=projection,
            chapter_seq=1,
        )
        if card.book_clock_constraint != expected_clock:
            raise CoreStateServiceError("重批章节卡 book clock 合同不匹配")
        render_core_state_capsule(
            projection=projection,
            transitions=card.core_state_transitions,
            book_clock_constraint=expected_clock,
        )

    def require_pending_transition_candidate_recovery_safe(
        self,
        *,
        session: Session,
        workspace_id: str,
        card: ChapterCard,
        paths: Any,
    ) -> bool:
        """核验同源摘要恢复：已登记角色换词冲突、旧候选误降级或遗漏关系。

        返回 True 仅允许一次摘要重提取；False 仍只做原有本地候选恢复。
        两条路径都要求正文、卡、ledger 和冲突来源完全同源。

        该入口只读。它不把任意 ``CoreStateConflictError`` 变成可恢复：必须是
        approved/final/vN/ledger 同源的 pending checkpoint，唯一冲突必须精确等于
        卡片尚未提交的迁移键；每个键都只能有一个旧 ``soft_not_core`` 候选。候选
        必须与批准 transition 的完整目标值逐字一致，或严格等于该目标值在
        ``当前值 + 分隔符`` 后的末尾增量；正文 exact evidence 仍须完整验真。
        """

        seq = chapter_seq_from_id(card.chapter_id)
        if seq is None or seq < 1:
            raise CoreStateServiceError("核心状态候选恢复要求有效章节序号")

        card_row = session.get(ChapterCardORM, card.chapter_id)
        chapter_row = session.get(ChapterORM, card.chapter_id)
        summary_row = session.get(ChapterSummaryORM, card.chapter_id)
        if (
            card_row is None
            or card_row.workspace_id != workspace_id
            or card_row.card_status != "approved"
            or card_row.card_approved_at is None
            or card.workspace_id != workspace_id
            or card.card_status != "approved"
            or chapter_row is None
            or chapter_row.workspace_id != workspace_id
            or chapter_row.status != "approved"
            or chapter_row.ready_for_next_chapter_at is not None
            or chapter_row.memory_enrichment_status != "pending"
            or chapter_row.core_state_commit_status != "pending"
            or not isinstance(chapter_row.current_version, int)
            or isinstance(chapter_row.current_version, bool)
            or chapter_row.current_version < 1
            or summary_row is None
            or summary_row.workspace_id != workspace_id
            or summary_row.validated_at is None
            or summary_row.source_version_id != chapter_row.current_version
        ):
            raise CoreStateServiceError(
                "非首章 transition 候选恢复 checkpoint 身份或状态不满足"
            )

        version_row = session.execute(
            select(ChapterVersionORM)
            .where(ChapterVersionORM.chapter_id == card.chapter_id)
            .order_by(ChapterVersionORM.version_id.desc())
            .limit(1)
        ).scalar_one_or_none()
        final_path = paths.chapter_version_file(card.chapter_id, "final.md")
        version_path = paths.chapter_version_file(
            card.chapter_id, f"v{chapter_row.current_version}.md"
        )
        try:
            final_text = final_path.read_text(encoding="utf-8")
            version_text = version_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise CoreStateServiceError(
                f"非首章 transition 候选恢复正文文件不可读: {exc}"
            ) from exc
        source_text_hash = sha256_text(final_text)
        if (
            version_row is None
            or version_row.version_id != chapter_row.current_version
            or version_row.content != final_text
            or version_text != final_text
            or summary_row.source_text_hash != source_text_hash
        ):
            raise CoreStateServiceError(
                "非首章 transition 候选恢复正文/版本/摘要来源漂移"
            )

        from src.core.agents.chapter_summary_extractor import (
            ContinuityLedgerValidationError,
            load_validated_continuity_ledger,
        )

        try:
            validated = load_validated_continuity_ledger(
                workspace_id=workspace_id,
                chapter_id=card.chapter_id,
                paths=paths,
                session_factory=self._session_factory,
                expected_version_id=chapter_row.current_version,
            )
        except ContinuityLedgerValidationError as exc:
            raise CoreStateServiceError(
                f"非首章 transition 候选恢复 ledger 无法验真: {exc}"
            ) from exc
        if (
            validated.source_text_hash != source_text_hash
            or validated.ledger_hash != summary_row.ledger_hash
        ):
            raise CoreStateServiceError(
                "非首章 transition 候选恢复 continuity ledger 来源漂移"
            )

        details = dict(chapter_row.core_state_commit_details or {})
        conflicts = details.get("conflicts")
        if (
            details.get("source_version_id") != chapter_row.current_version
            or details.get("source_text_hash") != source_text_hash
            or details.get("card_projection_hash")
            != card.core_state_projection_hash
            or not isinstance(conflicts, list)
            or not all(isinstance(item, str) for item in conflicts)
        ):
            raise CoreStateServiceError(
                "非首章 transition 候选恢复缺少原始冲突 provenance"
            )

        projection = self._projection_from_session(
            session,
            workspace_id=workspace_id,
            before_chapter=seq,
        )
        if card.core_state_contract_version != 1:
            raise CoreStateServiceError("非首章 transition 候选恢复要求 v1 卡片合同")
        if card.core_state_projection_hash != projection.projection_hash:
            raise CoreStateServiceError(
                "非首章 transition 候选恢复的卡片投影已漂移"
            )
        self._validate_transition_sources(
            card=card,
            source_outline_dir=paths.outline_dir,
            transitions=list(card.core_state_transitions), projection=projection
        )
        expected_clock = self._expected_book_clock(
            card=card,
            projection=projection,
            chapter_seq=seq,
        )
        if card.book_clock_constraint != expected_clock:
            raise CoreStateServiceError(
                "非首章 transition 候选恢复的 book clock 合同已漂移"
            )
        render_core_state_capsule(
            projection=projection,
            transitions=card.core_state_transitions,
            book_clock_constraint=expected_clock,
        )

        current = {fact.state_key: fact for fact in projection.facts}
        candidates = self._parse_candidates(summary_row)
        rejected = [item for item in candidates if item.admission_status == "rejected_conflict"]
        if rejected:
            # 只重提取摘要，不把近义描述自动归一、不修改已登记事实。
            # 当前值缺席的旧输入可能把症状重述当成变化；新输入重提取后仍走原硬检查。
            expected_conflicts = []
            for candidate in rejected:
                active = current.get(candidate.state_key)
                if (
                    candidate.entity_type != "character"
                    or active is None
                    or candidate.entity_id not in {card.pov_character, *card.related_characters}
                    or sum(item.state_key == candidate.state_key for item in candidates) != 1
                    or any(item.state_key == candidate.state_key for item in card.core_state_transitions)
                    or candidate.evidence_quote not in final_text
                    or candidate.evidence_fingerprint != sha256_text(candidate.evidence_quote)
                    or _canonical_value(candidate.value) == _canonical_value(active.value)
                ):
                    raise CoreStateServiceError("核心状态摘要恢复不满足已登记角色同源候选边界")
                expected_conflicts.append(
                    f"{candidate.state_key}: 当前={active.value!r}, "
                    f"候选={candidate.value!r}, 卡片无匹配 from→to 许可"
                )
            if conflicts != expected_conflicts or any(
                paths.chapter_dir(card.chapter_id).glob("summary-recovery-*.json")
            ):
                raise CoreStateServiceError("核心状态摘要恢复有其他冲突或已经尝试")
            return True
        missing_transitions = [
            transition
            for transition in card.core_state_transitions
            if not (
                transition.state_key in current
                and _canonical_value(current[transition.state_key].value)
                == _canonical_value(transition.to_value)
            )
        ]
        if not missing_transitions:
            raise CoreStateServiceError(
                "非首章 transition 候选恢复没有尚待提交的批准迁移"
            )
        candidates = self._parse_candidates(summary_row)
        if seq == 1 and not candidates:
            raise CoreStateServiceError("首章不能把空关系摘要作为含混登记纠正")
        if not candidates and all(
            transition.entity_type == "object"
            and transition.attribute in CORE_STATE_RELATION_ATTRIBUTES
            for transition in missing_transitions
        ):
            expected = _MISSING_TRANSITION_EVIDENCE_PREFIX + ", ".join(
                transition.state_key for transition in missing_transitions
            )
            archive_path = paths.chapter_version_file(
                card.chapter_id, f"summary-recovery-{summary_row.ledger_hash}.json"
            )
            if conflicts != [expected] or archive_path.exists():
                raise CoreStateServiceError(
                    "关系摘要恢复包含其他冲突或同源恢复已经尝试"
                )
            return True
        from src.services.relation_registration_recovery import (
            recovery_payload, decision_path, validated_registration_keys,
            registration_request_not_dispatched,
        )
        registration_payload = recovery_payload(card=card, candidates=candidates,
            projection=projection, paths=paths, text=final_text,
            ledger_hash=summary_row.ledger_hash,
            source_version_id=chapter_row.current_version)
        if registration_payload is not None:
            expected = _MISSING_TRANSITION_EVIDENCE_PREFIX + ", ".join(registration_payload["proposal_keys"])
            if conflicts != [expected]:
                raise CoreStateServiceError("关系登记纠正包含其他冲突")
            record_path = decision_path(paths=paths, payload=registration_payload)
            if record_path.exists():
                try:
                    record = json.loads(record_path.read_text())
                    if record.get("source") != registration_payload or not registration_request_not_dispatched(
                        session=session, workspace_id=workspace_id, chapter_id=card.chapter_id,
                        path=record_path, record=record):
                        validated_registration_keys(payload=registration_payload, record=record)
                except (ValueError, OSError) as exc:
                    raise CoreStateServiceError(str(exc)) from exc
            return False  # 只纠正关系提案，禁止重提已知摘要。

        if seq == 1:
            raise CoreStateServiceError("首章摘要恢复只允许已登记角色候选或同源含混关系登记纠正")
        candidate_counts = {
            state_key: sum(
                candidate.state_key == state_key for candidate in candidates
            )
            for state_key in {candidate.state_key for candidate in candidates}
        }
        recovery_keys: list[str] = []
        for transition in missing_transitions:
            matches = [
                candidate
                for candidate in candidates
                if candidate.state_key == transition.state_key
            ]
            if len(matches) != 1:
                raise CoreStateServiceError(
                    "非首章 transition 候选恢复缺少唯一同键候选: "
                    f"{transition.state_key}"
                )
            candidate = matches[0]
            if (
                candidate.evidence_quote not in final_text
                or candidate.evidence_fingerprint
                != sha256_text(candidate.evidence_quote)
            ):
                raise CoreStateServiceError(
                    "非首章 transition 候选恢复的 exact evidence 不匹配: "
                    f"{transition.state_key}"
                )
            if (
                candidate.admission_status == "soft_not_core"
                and "上游稳定身份" in candidate.admission_reason
            ):
                stabilized = self._stabilize_approved_transition_candidate(
                    candidate=candidate,
                    card=card,
                    current=current,
                    same_key_candidate_count=(
                        candidate_counts[candidate.state_key]
                    ),
                )
                recovery_keys.append(transition.state_key)
            elif (
                candidate.admission_status == "admitted"
                and _canonical_value(candidate.value)
                == _canonical_value(transition.to_value)
            ):
                # 原提交把同章另一迁移候选分类为 admitted 后，因任一兄弟
                # transition 缺证据而整体未落 revision。恢复时它仍须重新提交，
                # 但不能被误算成原始冲突键。
                stabilized = candidate
            else:
                raise CoreStateServiceError(
                    "非首章 transition 候选恢复的旧降级/已准入身份不匹配: "
                    f"{transition.state_key}"
                )
            if _canonical_value(stabilized.value) != _canonical_value(
                transition.to_value
            ):
                raise CoreStateServiceError(
                    "非首章 transition 候选恢复既非完整目标值，也非严格末尾增量: "
                    f"{transition.state_key}"
                )
            status, _reason = self._classify_candidate(
                candidate=stabilized, card=card
            )
            if status != "proposed":
                raise CoreStateServiceError(
                    "修复后候选仍不能通过严格静态准入: "
                    f"{transition.state_key} status={status}"
                )
        if not recovery_keys:
            raise CoreStateServiceError(
                "非首章 transition 候选恢复没有旧中心性误降级候选"
            )
        expected_conflict = _MISSING_TRANSITION_EVIDENCE_PREFIX + ", ".join(
            recovery_keys
        )
        if conflicts != [expected_conflict]:
            raise CoreStateServiceError(
                "非首章 transition 候选恢复包含计划外核心状态冲突"
            )

        return False

    @staticmethod
    def _load_outline_character_items(paths: Any) -> dict[str, CharacterOutlineItem]:
        outline_path = paths.outline_dir / "characters.json"
        try:
            raw = json.loads(outline_path.read_text(encoding="utf-8"))
            output = CharactersOutput.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise CoreStateServiceError(
                f"首章旧基线恢复无法验证 outline/characters.json: {exc}"
            ) from exc
        return {item.character_id: item for item in output.characters}

    @staticmethod
    def _legacy_candidate_outline_sources(
        *, item: CharacterOutlineItem, value: str
    ) -> dict[str, str]:
        source_texts = {
            "archetype": item.archetype,
            "background": item.background,
            "identity_facets": "\n".join(
                f"{facet.context}:{facet.identity}" for facet in item.identity_facets
            ),
        }
        return {
            field: text
            for field, text in source_texts.items()
            if value in text
        }

    def _pending_initial_conflict_candidates(
        self,
        session: Session,
        *,
        workspace_id: str,
        chapter_id: str,
        paths: Any,
        required_card_status: str,
    ) -> tuple[list[CoreStateCandidate], str]:
        """复核原冲突的 DB/正文/摘要来源，并返回唯一可恢复候选。"""

        if chapter_id != "ch-0001":
            return [], ""
        card_row = session.get(ChapterCardORM, chapter_id)
        chapter_row = session.get(ChapterORM, chapter_id)
        summary_row = session.get(ChapterSummaryORM, chapter_id)
        if (
            card_row is None
            or card_row.workspace_id != workspace_id
            or card_row.card_status != required_card_status
            or chapter_row is None
            or chapter_row.workspace_id != workspace_id
            or chapter_row.status != "approved"
            or chapter_row.ready_for_next_chapter_at is not None
            or chapter_row.core_state_commit_status != "pending"
            or not isinstance(chapter_row.current_version, int)
            or chapter_row.current_version < 1
            or summary_row is None
            or summary_row.workspace_id != workspace_id
            or summary_row.validated_at is None
            or summary_row.source_version_id != chapter_row.current_version
        ):
            raise CoreStateServiceError("首章旧基线恢复 checkpoint 身份或状态不满足")

        version_row = session.execute(
            select(ChapterVersionORM)
            .where(ChapterVersionORM.chapter_id == chapter_id)
            .order_by(ChapterVersionORM.version_id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if (
            version_row is None
            or version_row.version_id != chapter_row.current_version
        ):
            raise CoreStateServiceError("首章旧基线恢复找不到同一 DB 权威正文版本")
        final_path = paths.chapter_version_file(chapter_id, "final.md")
        version_path = paths.chapter_version_file(
            chapter_id, f"v{chapter_row.current_version}.md"
        )
        try:
            final_text = final_path.read_text(encoding="utf-8")
            version_text = version_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise CoreStateServiceError(
                f"首章旧基线恢复正文文件不可读: {exc}"
            ) from exc
        source_text_hash = sha256_text(final_text)
        if (
            final_text != version_text
            or final_text != version_row.content
            or summary_row.source_text_hash != source_text_hash
        ):
            raise CoreStateServiceError("首章旧基线恢复正文/版本/摘要来源漂移")

        details = dict(chapter_row.core_state_commit_details or {})
        conflicts = details.get("conflicts")
        if (
            details.get("source_version_id") != chapter_row.current_version
            or details.get("source_text_hash") != source_text_hash
            or not isinstance(conflicts, list)
            or not conflicts
            or not all(isinstance(item, str) for item in conflicts)
        ):
            raise CoreStateServiceError("首章旧基线恢复缺少原始冲突 provenance")

        candidates = self._parse_candidates(summary_row)
        recoverable = [
            candidate
            for candidate in candidates
            if candidate.admission_status == "rejected_conflict"
            and candidate.entity_type == "character"
            and candidate.attribute in _CHARACTER_IRREVERSIBLE_ATTRIBUTES
            and candidate.evidence_quote in final_text
            and candidate.evidence_fingerprint
            == sha256_text(candidate.evidence_quote)
        ]
        expected_conflicts = {
            candidate.state_key: (
                f"{candidate.state_key}: 不可逆角色状态首次出现也必须由"
                "章节卡显式声明 from=(未建立)→to 许可"
            )
            for candidate in recoverable
        }
        if (
            not recoverable
            or len(recoverable) != len(conflicts)
            or set(conflicts) != set(expected_conflicts.values())
        ):
            raise CoreStateServiceError("首章旧基线恢复包含非初始状态类冲突")
        return recoverable, source_text_hash

    @staticmethod
    def _candidate_is_central(
        *, candidate: CoreStateCandidate, card: ChapterCard
    ) -> bool:
        if candidate.entity_type == "character":
            return candidate.entity_id in {
                card.pov_character,
                *card.related_characters,
            }
        if any(
            transition.state_key == candidate.state_key
            and _canonical_value(transition.to_value)
            == _canonical_value(candidate.value)
            for transition in card.core_state_transitions
        ):
            # 章节卡冻结的精确 state_key/to_value 本身就是比自然语言提及更强的
            # 上游稳定身份。这里只放行同一批准迁移的正文候选；正文自荐的新 ID、
            # 错属性或错目标值仍走下方文本准入/冲突合同，不能借此升 HARD。
            return True
        haystack = "\n".join(
            [
                card.title,
                card.setting,
                *card.must_achieve,
                *card.must_unresolved,
                *card.forbidden,
                card.next_chapter_seed,
            ]
        )
        return candidate.entity_id in haystack

    @classmethod
    def _stabilize_approved_transition_candidate(
        cls,
        *,
        candidate: CoreStateCandidate,
        card: ChapterCard,
        current: dict[str, CoreStateFact],
        same_key_candidate_count: int,
    ) -> CoreStateCandidate:
        """把严格的“保留旧值 + 摘要仅给增量”收敛到批准目标值。

        ChapterCard 的 ``to_value`` 是正文前冻结的唯一目标。摘要若只返回其
        末尾增量，只有在目标的保留段是当前值的逐字长前缀、随后是明确分隔符，
        且候选逐字等于目标末尾时，才能机械补回目标。允许卡片删除当前值末尾的
        短旧状态（例如“且未启用”），但保留段至少占当前值三分之二、被删除末尾
        最多 16 字。这里不做同义词、任意子串、编辑距离或模型判断。
        """

        if same_key_candidate_count != 1:
            return candidate
        transitions = [
            transition
            for transition in card.core_state_transitions
            if transition.state_key == candidate.state_key
        ]
        if len(transitions) != 1:
            return candidate
        transition = transitions[0]
        candidate_value = _canonical_value(candidate.value)
        target_value = _canonical_value(transition.to_value)
        if candidate_value == target_value:
            return candidate
        if (
            candidate.entity_type != "object"
            or candidate.admission_status not in {"proposed", "soft_not_core"}
            or (
                candidate.admission_status == "soft_not_core"
                and "上游稳定身份" not in candidate.admission_reason
            )
            or candidate.ambiguous
            or not (
                candidate.objective
                and candidate.long_lived
                and candidate.book_bearing
            )
            or any(
                marker in candidate.evidence_quote
                for marker in _AMBIGUOUS_EVIDENCE_MARKERS
            )
            or transition.from_value is None
        ):
            return candidate
        active = current.get(candidate.state_key)
        if active is None:
            return candidate
        current_value = _canonical_value(active.value)
        if _canonical_value(transition.from_value) != current_value:
            return candidate
        if not candidate_value or not target_value.endswith(candidate_value):
            return candidate
        target_prefix = target_value[: -len(candidate_value)]
        retained_value = target_prefix.rstrip(
            "".join(_RETAINED_STATE_DELTA_SEPARATORS) + " \t\r\n"
        )
        separator = target_prefix[len(retained_value) :]
        if not current_value.startswith(retained_value):
            return candidate
        omitted_current_suffix = current_value[len(retained_value) :]
        if (
            len(retained_value) < 8
            or len(retained_value) * 3 < len(current_value) * 2
            or len(omitted_current_suffix) > 16
            or not separator
            or not any(
                character in _RETAINED_STATE_DELTA_SEPARATORS
                for character in separator
            )
            or any(
                not (
                    character.isspace()
                    or character in _RETAINED_STATE_DELTA_SEPARATORS
                )
                for character in separator
            )
        ):
            return candidate
        return candidate.model_copy(update={"value": transition.to_value})

    @classmethod
    def _classify_candidate(
        cls, *, candidate: CoreStateCandidate, card: ChapterCard
    ) -> tuple[str, str]:
        if candidate.attribute == "holder":
            return "soft_not_core", "legacy holder 只读，不再创建新 revision"
        if candidate.ambiguous:
            return "soft_ambiguous", "模型明确标记含混，不自动 HARD"
        if any(
            marker in candidate.evidence_quote for marker in _AMBIGUOUS_EVIDENCE_MARKERS
        ):
            return "soft_ambiguous", "逐字证据含猜测、梦境、回忆或转述信号"
        if not (
            candidate.objective and candidate.long_lived and candidate.book_bearing
        ):
            return "soft_not_core", "未同时满足客观、长期、全书承重三项准入"
        if not cls._candidate_is_central(candidate=candidate, card=card):
            return (
                "soft_not_core",
                "实体未在标题/场景/must/forbidden/下一章种子或出场角色中形成"
                "上游稳定身份；正文自荐不能自行升 HARD",
            )
        return "proposed", "通过静态准入，待与当前值/迁移许可比对"

    @staticmethod
    def _parse_candidates(row: ChapterSummaryORM) -> list[CoreStateCandidate]:
        candidates: list[CoreStateCandidate] = []
        for index, raw in enumerate(row.core_state_candidates or []):
            try:
                candidates.append(CoreStateCandidate.model_validate(raw))
            except ValidationError as exc:
                raise CoreStateServiceError(
                    f"ChapterSummary core_state_candidates[{index}] 损坏: {exc}"
                ) from exc
        return candidates

    @staticmethod
    def _transition_for_candidate(
        *, candidate: CoreStateCandidate, card: ChapterCard
    ) -> CoreStateTransition | None:
        return next(
            (
                transition
                for transition in card.core_state_transitions
                if transition.state_key == candidate.state_key
            ),
            None,
        )

    @staticmethod
    def _apply_character_candidates(
        session: Session,
        *,
        workspace_id: str,
        chapter_id: str,
        source_version_id: int,
        source_text_hash: str,
        seq: int,
        candidates: list[CoreStateCandidate],
        transition_by_key: dict[str, CoreStateTransition],
    ) -> None:
        by_character: dict[str, list[CoreStateCandidate]] = {}
        for candidate in candidates:
            if candidate.entity_type == "character":
                by_character.setdefault(candidate.entity_id, []).append(candidate)
        for character_id, items in by_character.items():
            row = session.get(CharacterORM, character_id)
            if row is None or row.workspace_id != workspace_id:
                raise CoreStateServiceError(f"角色核心候选引用未知角色: {character_id}")
            snapshots = [
                CharacterStateSnapshot.model_validate(raw)
                for raw in (row.state_timeline or [])
            ]
            prior = max(
                (item for item in snapshots if item.as_of_chapter < seq),
                key=lambda item: item.as_of_chapter,
                default=None,
            )
            current_facts = list(prior.core_facts) if prior is not None else []
            changed_keys = {item.state_key for item in items}
            for index, snapshot in enumerate(snapshots):
                updated_facts: list[CharacterCoreFact] = []
                changed = False
                for fact in snapshot.core_facts:
                    if fact.state_key in changed_keys and fact.valid_to_chapter is None:
                        fact = fact.model_copy(update={"valid_to_chapter": seq - 1})
                        changed = True
                    updated_facts.append(fact)
                if changed:
                    replacement = snapshot.model_copy(
                        update={"core_facts": updated_facts}
                    )
                    snapshots[index] = replacement

            active_facts = [
                fact for fact in current_facts if fact.state_key not in changed_keys
            ]
            for candidate in items:
                transition = transition_by_key.get(candidate.state_key)
                active_facts.append(
                    CharacterCoreFact(
                        state_key=candidate.state_key,
                        attribute=candidate.attribute,  # type: ignore[arg-type]
                        value=candidate.value,
                        source_kind="approved_text",
                        source_chapter_id=chapter_id,
                        source_version_id=source_version_id,
                        source_path=(
                            "chapter_summaries/"
                            f"{chapter_id}.core_state_candidates[{candidate.state_key}]"
                        ),
                        source_text_hash=source_text_hash,
                        evidence_quote=candidate.evidence_quote,
                        valid_from_chapter=seq,
                        supersedes_state_key=(
                            transition.state_key if transition is not None else None
                        ),
                    )
                )
            base = prior or CharacterStateSnapshot(as_of_chapter=seq)
            snapshot = base.model_copy(
                update={
                    "as_of_chapter": seq,
                    "authority": "approved_text",
                    "source_chapter_id": chapter_id,
                    "source_version_id": source_version_id,
                    "source_text_hash": source_text_hash,
                    "evidence_quotes": [item.evidence_quote for item in items],
                    "core_facts": sorted(active_facts, key=lambda fact: fact.state_key),
                    "state_changes": list(base.state_changes),
                }
            )
            snapshots = [item for item in snapshots if item.as_of_chapter != seq]
            snapshots.append(snapshot)
            snapshots.sort(key=lambda item: item.as_of_chapter)
            row.state_timeline = [item.model_dump(mode="json") for item in snapshots]
            current_state = dict(row.current_state or {})
            for candidate in items:
                current_state[f"core.{candidate.attribute}"] = candidate.value
            row.current_state = current_state

    @staticmethod
    def _supersede_row(row: CoreStateRevisionORM, *, seq: int) -> None:
        row.status = "superseded"
        row.valid_to_chapter = seq - 1

    @staticmethod
    def _new_revision(
        *,
        workspace_id: str,
        state_key: str,
        entity_type: str,
        entity_id: str,
        attribute: str,
        value_json: dict[str, Any],
        value_text: str,
        source_kind: str,
        chapter_id: str,
        source_version_id: int,
        source_path: str,
        source_text_hash: str,
        evidence_quote: str,
        seq: int,
        supersedes_revision_id: str | None,
        change_reason: str,
    ) -> CoreStateRevisionORM:
        return CoreStateRevisionORM(
            revision_id=f"csr_{uuid4().hex}",
            workspace_id=workspace_id,
            state_key=state_key,
            entity_type=entity_type,
            entity_id=entity_id,
            attribute=attribute,
            value_json=value_json,
            value_text=value_text,
            source_kind=source_kind,
            source_chapter_id=chapter_id,
            source_version_id=source_version_id,
            source_path=source_path,
            source_text_hash=source_text_hash,
            evidence_quote=evidence_quote,
            valid_from_chapter=seq,
            valid_to_chapter=None,
            supersedes_revision_id=supersedes_revision_id,
            status="active",
            change_reason=change_reason,
            ledger_hash="0" * 64,
            created_at=datetime.now(UTC),
            retracted_at=None,
        )

    def repair_previous_soft_object_identity_for_rejected_card(
        self,
        *,
        workspace_id: str,
        target_card: ChapterCard,
        previous_card: ChapterCard,
        paths: Any,
    ) -> bool:
        """零 Provider 修复紧邻前章被摘要别名降成 SOFT 的代码型物件。

        这是严格历史兼容入口，不是通用同义词合并：只接受 rejected 空白目标章
        暴露出的一个 ``有 from、无当前事实`` 迁移；前章批准卡、final、已验证
        小账本和 exact evidence 必须共同证明同一个代码型 ID。任一条件不唯一
        就不修；已命中特征但现场不再干净则 fail closed。
        """

        if target_card.card_status != "rejected":
            return False
        target_seq = chapter_seq_from_id(target_card.chapter_id)
        previous_seq = chapter_seq_from_id(previous_card.chapter_id)
        if (
            target_seq is None
            or previous_seq is None
            or previous_seq != target_seq - 1
            or previous_card.card_status != "approved"
            or previous_card.core_state_contract_version != 1
        ):
            return False

        projection = self.load_projection(
            workspace_id,
            before_chapter=target_seq,
        )
        current = {fact.state_key: fact for fact in projection.facts}
        missing = [
            transition
            for transition in target_card.core_state_transitions
            if transition.entity_type == "object"
            and transition.from_value is not None
            and transition.state_key not in current
        ]
        if len(missing) != 1:
            return False
        transition = missing[0]
        transition_from = transition.from_value
        assert transition_from is not None
        stable_id = transition.entity_id
        if not _STABLE_CODE_OBJECT_ID_RE.fullmatch(stable_id):
            return False
        if stable_id not in _card_structured_identity_text(previous_card):
            return False

        # 先用现有严格 loader 证明旧小账本本身与 approved final/version/card 同源。
        from src.core.agents.chapter_summary_extractor import (
            load_validated_continuity_ledger,
        )

        validated = load_validated_continuity_ledger(
            workspace_id=workspace_id,
            chapter_id=previous_card.chapter_id,
            paths=paths,
            session_factory=self._session_factory,
            expected_version_id=None,
        )
        parsed_candidates = [
            CoreStateCandidate.model_validate(item)
            for item in validated.core_state_candidates
        ]
        repairable: list[tuple[int, CoreStateCandidate]] = []
        for index, candidate in enumerate(parsed_candidates):
            suffix = (
                candidate.entity_id[len(stable_id) :]
                if candidate.entity_id.startswith(stable_id)
                else ""
            )
            if (
                candidate.entity_type == "object"
                and candidate.attribute == transition.attribute
                and candidate.entity_id != stable_id
                and _LEGACY_OBJECT_ALIAS_SUFFIX_RE.fullmatch(suffix) is not None
                and candidate.admission_status == "soft_not_core"
                and "上游稳定身份" in candidate.admission_reason
            ):
                repairable.append((index, candidate))
        if len(repairable) != 1:
            return False
        candidate_index, candidate = repairable[0]

        final_path = paths.chapter_version_file(previous_card.chapter_id, "final.md")
        try:
            final_text = final_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CoreStateServiceError(f"旧物件身份修复无法读取前章 final: {exc}") from exc
        records = [
            ContinuityEvidenceRecord.model_validate(item)
            for item in validated.evidence_records
        ]
        candidate_records = [
            item
            for item in records
            if item.field == "core_state_candidates" and item.index == candidate_index
        ]
        if len(candidate_records) != 1:
            return False
        candidate_record = candidate_records[0]
        if (
            candidate_record.fact_text
            != f"{candidate.state_key}={candidate.value}"
            or candidate_record.evidence_quote != candidate.evidence_quote
            or candidate_record.evidence_fingerprint
            != candidate.evidence_fingerprint
        ):
            return False

        markers = _OBJECT_ATTRIBUTE_EVIDENCE_MARKERS.get(transition.attribute, ())
        supporting = [
            item
            for item in records
            if item.field in {"key_events", "established_facts"}
            and stable_id in item.fact_text
            and stable_id in item.evidence_quote
            and item.evidence_quote in final_text
            and item.evidence_fingerprint == sha256_text(item.evidence_quote)
            and any(
                marker in item.fact_text or marker in item.evidence_quote
                for marker in markers
            )
        ]
        if len(supporting) != 1:
            return False
        support = supporting[0]
        repaired_value = _longest_shared_attribute_phrase(
            candidate_value=candidate.value,
            evidence_quote=support.evidence_quote,
            markers=markers,
        )
        if not repaired_value or not any(
            marker in repaired_value and marker in transition_from
            for marker in markers
        ):
            return False

        session = self._session_factory(workspace_id)
        try:
            self._begin_immediate(session)
            target_card_row = session.get(ChapterCardORM, target_card.chapter_id)
            if (
                target_card_row is None
                or target_card_row.workspace_id != workspace_id
                or target_card_row.card_status != "rejected"
            ):
                raise CoreStateServiceError("旧物件身份修复要求目标卡仍为 rejected")
            if session.get(ChapterOperationLeaseORM, (workspace_id, target_card.chapter_id)):
                raise CoreStateServiceError("旧物件身份修复时目标章仍有操作租约")
            if session.get(ChapterSummaryORM, target_card.chapter_id) is not None:
                raise CoreStateServiceError("旧物件身份修复要求目标章尚无小账本")
            if session.scalar(
                select(ChapterVersionORM).where(
                    ChapterVersionORM.chapter_id == target_card.chapter_id
                ).limit(1)
            ) is not None:
                raise CoreStateServiceError("旧物件身份修复要求目标章尚无正文版本")
            if session.scalar(
                select(ReviewResultORM).where(
                    ReviewResultORM.chapter_id == target_card.chapter_id
                ).limit(1)
            ) is not None:
                raise CoreStateServiceError("旧物件身份修复要求目标章尚无审稿结果")
            target_chapter_row = session.get(ChapterORM, target_card.chapter_id)
            if target_chapter_row is not None and not (
                target_chapter_row.workspace_id == workspace_id
                and target_chapter_row.current_version == 0
                and target_chapter_row.status == "drafting"
                and target_chapter_row.rewrite_count == 0
                and target_chapter_row.revise_count == 0
                and target_chapter_row.ready_for_next_chapter_at is None
            ):
                raise CoreStateServiceError("旧物件身份修复要求目标章没有正文运行现场")
            target_dir = paths.chapter_dir(target_card.chapter_id)
            if target_dir.is_dir() and any(target_dir.glob("*.md")):
                raise CoreStateServiceError("旧物件身份修复要求目标章磁盘正文为空")

            chapter_row = session.get(ChapterORM, previous_card.chapter_id)
            summary_row = session.get(ChapterSummaryORM, previous_card.chapter_id)
            if (
                chapter_row is None
                or chapter_row.workspace_id != workspace_id
                or chapter_row.status != "approved"
                or chapter_row.current_version != validated.source_version_id
                or chapter_row.ready_for_next_chapter_at is None
                or chapter_row.memory_enrichment_status != "succeeded"
                or chapter_row.core_state_commit_status != "committed"
                or len(chapter_row.core_state_commit_hash) != 64
                or summary_row is None
                or summary_row.workspace_id != workspace_id
                or summary_row.ledger_hash != validated.ledger_hash
                or summary_row.source_text_hash != validated.source_text_hash
                or summary_row.source_version_id != validated.source_version_id
                or summary_row.validated_at is None
            ):
                raise CoreStateServiceError("旧物件身份修复时前章硬水位已漂移")
            memory_details = dict(chapter_row.memory_enrichment_details or {})
            if (
                memory_details.get("continuity_ledger_hash") != validated.ledger_hash
                or memory_details.get("core_state_commit_hash")
                != chapter_row.core_state_commit_hash
            ):
                raise CoreStateServiceError("旧物件身份修复时前章富记忆来源已漂移")

            projection_before = self._projection_from_session(
                session,
                workspace_id=workspace_id,
                before_chapter=previous_seq,
            )
            if projection_before.projection_hash != previous_card.core_state_projection_hash:
                raise CoreStateServiceError("旧物件身份修复时前章卡片投影已漂移")
            live_projection = self._projection_from_session(
                session,
                workspace_id=workspace_id,
                before_chapter=target_seq,
            )
            if transition.state_key in {fact.state_key for fact in live_projection.facts}:
                session.rollback()
                return False

            live_candidates = [
                CoreStateCandidate.model_validate(item)
                for item in (summary_row.core_state_candidates or [])
            ]
            if live_candidates != parsed_candidates:
                raise CoreStateServiceError("旧物件身份修复时前章核心候选已漂移")
            live_records = [
                ContinuityEvidenceRecord.model_validate(item)
                for item in (summary_row.evidence_records or [])
            ]
            if live_records != records:
                raise CoreStateServiceError("旧物件身份修复时前章证据记录已漂移")

            repaired_candidate = CoreStateCandidate(
                entity_type="object",
                entity_id=stable_id,
                attribute=candidate.attribute,
                value=repaired_value,
                objective=candidate.objective,
                long_lived=candidate.long_lived,
                book_bearing=candidate.book_bearing,
                ambiguous=candidate.ambiguous,
                evidence_quote=support.evidence_quote,
                evidence_fingerprint=support.evidence_fingerprint,
                admission_status="admitted",
                admission_reason=(
                    "严格历史兼容修复：批准卡代码型 ID + final exact evidence "
                    f"从别名 {candidate.entity_id!r} 收敛为 {stable_id!r}"
                ),
            )
            classified = list(live_candidates)
            classified[candidate_index] = repaired_candidate
            updated_records = list(live_records)
            updated_records[updated_records.index(candidate_record)] = (
                candidate_record.model_copy(
                    update={
                        "fact_text": (
                            f"{repaired_candidate.state_key}={repaired_candidate.value}"
                        ),
                        "evidence_quote": repaired_candidate.evidence_quote,
                        "evidence_fingerprint": (
                            repaired_candidate.evidence_fingerprint
                        ),
                    }
                )
            )
            classified_json = [
                item.model_dump(mode="json") for item in classified
            ]
            new_ledger_hash = compute_continuity_ledger_hash(
                chapter_id=summary_row.chapter_id,
                workspace_id=summary_row.workspace_id,
                summary_text=summary_row.summary_text,
                pov_character=summary_row.pov_character,
                scene_setting=summary_row.scene_setting,
                key_events=list(summary_row.key_events or []),
                character_interactions=list(summary_row.character_interactions or []),
                unfinished_actions=list(summary_row.unfinished_actions or []),
                character_states=list(summary_row.character_states or []),
                established_facts=list(summary_row.established_facts or []),
                revealed_mechanics=list(summary_row.revealed_mechanics or []),
                core_state_candidates=_core_candidates_for_ledger_hash(
                    classified_json
                ),
                raw_tail=summary_row.raw_final_tail,
                source_version_id=summary_row.source_version_id,
                source_text_hash=summary_row.source_text_hash,
                evidence_records=updated_records,
            )
            summary_row.core_state_candidates = classified_json
            summary_row.evidence_records = [
                item.model_dump(mode="json") for item in updated_records
            ]
            summary_row.ledger_hash = new_ledger_hash
            summary_row.updated_at = datetime.now(UTC)

            new_row = self._new_revision(
                workspace_id=workspace_id,
                state_key=repaired_candidate.state_key,
                entity_type="object",
                entity_id=repaired_candidate.entity_id,
                attribute=repaired_candidate.attribute,
                value_json={"value": repaired_candidate.value},
                value_text=repaired_candidate.value,
                source_kind="approved_text",
                chapter_id=previous_card.chapter_id,
                source_version_id=validated.source_version_id,
                source_path=(
                    f"chapter_summaries/{previous_card.chapter_id}."
                    f"core_state_candidates[{repaired_candidate.state_key}]"
                ),
                source_text_hash=validated.source_text_hash,
                evidence_quote=repaired_candidate.evidence_quote,
                seq=previous_seq,
                supersedes_revision_id=None,
                change_reason="批准卡稳定 ID 的严格历史兼容准入",
            )
            session.add(new_row)
            session.flush()
            projection_after = self._projection_from_session(
                session,
                workspace_id=workspace_id,
                before_chapter=target_seq,
            )
            render_core_state_capsule(
                projection=projection_after,
                transitions=[],
                book_clock_constraint=None,
            )
            source_rows = session.scalars(
                select(CoreStateRevisionORM)
                .where(CoreStateRevisionORM.workspace_id == workspace_id)
                .where(
                    CoreStateRevisionORM.source_chapter_id
                    == previous_card.chapter_id
                )
                .where(CoreStateRevisionORM.status == "active")
            ).all()
            for row in source_rows:
                row.ledger_hash = projection_after.projection_hash

            clock = previous_card.book_clock_constraint
            if clock is None:
                raise CoreStateServiceError("旧物件身份修复缺少前章书内时钟合同")
            admitted_count = sum(
                item.admission_status == "admitted" for item in classified
            )
            reaffirmed_count = sum(
                item.admission_status == "reaffirmed" for item in classified
            )
            soft_count = sum(
                item.admission_status in {"soft_ambiguous", "soft_not_core"}
                for item in classified
            )
            commit_hash = _commit_hash(
                {
                    "chapter_id": previous_card.chapter_id,
                    "source_version_id": validated.source_version_id,
                    "source_text_hash": validated.source_text_hash,
                    "card_projection_hash": previous_card.core_state_projection_hash,
                    "projection_before_hash": projection_before.projection_hash,
                    "projection_after_hash": projection_after.projection_hash,
                    "candidates": classified_json,
                    "book_clock": clock.model_dump(mode="json"),
                }
            )
            old_ledger_hash = validated.ledger_hash
            old_commit_hash = chapter_row.core_state_commit_hash
            chapter_row.core_state_commit_hash = commit_hash
            chapter_row.core_state_commit_details = {
                "schema_version": 1,
                "source_version_id": validated.source_version_id,
                "source_text_hash": validated.source_text_hash,
                "card_projection_hash": previous_card.core_state_projection_hash,
                "projection_before_hash": projection_before.projection_hash,
                "projection_after_hash": projection_after.projection_hash,
                "admitted_count": admitted_count,
                "reaffirmed_count": reaffirmed_count,
                "soft_count": soft_count,
                "book_clock_hard": clock.is_hard,
                "repair_kind": "legacy_soft_object_identity",
                "repair_target_chapter_id": target_card.chapter_id,
                "repair_old_ledger_hash": old_ledger_hash,
                "repair_old_commit_hash": old_commit_hash,
            }
            memory_details["continuity_ledger_hash"] = new_ledger_hash
            memory_details["core_state_commit_hash"] = commit_hash
            chapter_row.memory_enrichment_details = memory_details
            session.commit()
            _log.warning(
                "legacy_soft_object_identity_repaired",
                workspace_id=workspace_id,
                source_chapter_id=previous_card.chapter_id,
                target_chapter_id=target_card.chapter_id,
                old_entity_id=candidate.entity_id,
                entity_id=stable_id,
                attribute=transition.attribute,
                value=repaired_value,
                old_ledger_hash=old_ledger_hash,
                new_ledger_hash=new_ledger_hash,
                old_commit_hash=old_commit_hash,
                new_commit_hash=commit_hash,
            )
            return True
        except Exception:
            if session.in_transaction():
                session.rollback()
            raise
        finally:
            session.close()

    def commit_approved_chapter(
        self,
        *,
        workspace_id: str,
        card: ChapterCard,
        source_version_id: int,
        paths: Any,
    ) -> CoreStateCommitResult:
        """从已验证 ChapterSummary 原子提交当前状态；幂等且不调用 Provider。"""

        seq = chapter_seq_from_id(card.chapter_id)
        if seq is None:
            raise CoreStateServiceError("核心状态提交要求规范 chapter_id")
        final_path = paths.chapter_version_file(card.chapter_id, "final.md")
        version_path = paths.chapter_version_file(
            card.chapter_id, f"v{source_version_id}.md"
        )
        try:
            final_text = final_path.read_text(encoding="utf-8")
            version_text = version_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CoreStateServiceError(f"核心状态来源正文不可读: {exc}") from exc
        if final_text != version_text:
            raise CoreStateServiceError("核心状态提交前 final.md 与来源版本不一致")
        source_text_hash = sha256_text(final_text)

        session = self._session_factory(workspace_id)
        try:
            self._begin_immediate(session)
            chapter_row = session.get(ChapterORM, card.chapter_id)
            if (
                chapter_row is None
                or chapter_row.workspace_id != workspace_id
                or chapter_row.status != "approved"
                or chapter_row.current_version != source_version_id
            ):
                raise CoreStateServiceError(
                    "核心状态提交要求章节处于同一 approved/current_version"
                )
            summary_row = session.get(ChapterSummaryORM, card.chapter_id)
            if (
                summary_row is None
                or summary_row.workspace_id != workspace_id
                or summary_row.validated_at is None
                or summary_row.source_version_id != source_version_id
                or summary_row.source_text_hash != source_text_hash
            ):
                raise CoreStateServiceError(
                    "核心状态提交要求同一来源版本的已验证 ChapterSummary"
                )

            if card.core_state_contract_version == 0:
                chapter_row.core_state_commit_hash = ""
                chapter_row.core_state_commit_status = "disabled"
                chapter_row.core_state_commit_details = {
                    "schema_version": 1,
                    "reason": "legacy_or_kill_switch",
                    "source_version_id": source_version_id,
                }
                session.commit()
                empty = self._projection_from_session(
                    session,
                    workspace_id=workspace_id,
                    before_chapter=seq + 1,
                )
                return CoreStateCommitResult(
                    chapter_id=card.chapter_id,
                    source_version_id=source_version_id,
                    status="disabled",
                    commit_hash="",
                    projection_before_hash=empty.projection_hash,
                    projection_after_hash=empty.projection_hash,
                    admitted_count=0,
                    reaffirmed_count=0,
                    soft_count=0,
                )

            if chapter_row.core_state_commit_status == "committed":
                details = dict(chapter_row.core_state_commit_details or {})
                clock = card.book_clock_constraint
                clock_details_match = bool(
                    clock is not None
                    and (
                        clock.contract_version == 0
                        or (
                            details.get("schema_version") == 2
                            and details.get("book_clock_contract_version") == 1
                            and details.get("book_time_node_id") == clock.node_id
                            and details.get("book_time_relation_chain_hash")
                            == clock.relation_chain_hash
                        )
                    )
                )
                if (
                    details.get("source_version_id") == source_version_id
                    and details.get("source_text_hash") == source_text_hash
                    and details.get("card_projection_hash")
                    == card.core_state_projection_hash
                    and clock_details_match
                ):
                    return CoreStateCommitResult(
                        chapter_id=card.chapter_id,
                        source_version_id=source_version_id,
                        status="committed",
                        commit_hash=chapter_row.core_state_commit_hash,
                        projection_before_hash=str(
                            details.get("projection_before_hash", "")
                        ),
                        projection_after_hash=str(
                            details.get("projection_after_hash", "")
                        ),
                        admitted_count=int(details.get("admitted_count", 0)),
                        reaffirmed_count=int(details.get("reaffirmed_count", 0)),
                        soft_count=int(details.get("soft_count", 0)),
                    )
                raise CoreStateServiceError(
                    "已有核心状态提交与当前 source/card provenance 不一致"
                )

            projection_before = self._projection_from_session(
                session,
                workspace_id=workspace_id,
                before_chapter=seq,
            )
            if projection_before.projection_hash != card.core_state_projection_hash:
                raise CoreStateServiceError(
                    "核心状态提交时卡片投影已陈旧，禁止把新正文接到旧状态"
                )
            self._validate_transition_sources(
                card=card,
                source_outline_dir=paths.outline_dir,
                transitions=card.core_state_transitions,
                projection=projection_before,
            )
            candidates = self._parse_candidates(summary_row)
            for index, candidate in enumerate(candidates):
                if candidate.evidence_quote not in final_text:
                    raise CoreStateServiceError(
                        "核心状态候选逐字证据已不在批准正文: "
                        f"candidate={index} state_key={candidate.state_key}"
                    )
                if candidate.evidence_fingerprint != sha256_text(
                    candidate.evidence_quote
                ):
                    raise CoreStateServiceError(
                        "核心状态候选 evidence_fingerprint 不匹配: "
                        f"candidate={index} state_key={candidate.state_key}"
                    )
            current = {fact.state_key: fact for fact in projection_before.facts}
            candidate_counts = {
                state_key: sum(
                    candidate.state_key == state_key for candidate in candidates
                )
                for state_key in {candidate.state_key for candidate in candidates}
            }
            stabilized_candidates: list[CoreStateCandidate] = []
            stabilizations: list[dict[str, str]] = []
            for candidate in candidates:
                stabilized = self._stabilize_approved_transition_candidate(
                    candidate=candidate,
                    card=card,
                    current=current,
                    same_key_candidate_count=candidate_counts[candidate.state_key],
                )
                stabilized_candidates.append(stabilized)
                if _canonical_value(stabilized.value) != _canonical_value(
                    candidate.value
                ):
                    stabilizations.append(
                        {
                            "state_key": candidate.state_key,
                            "summary_delta_value": candidate.value,
                            "approved_to_value": stabilized.value,
                            "kind": "retained_current_prefix_exact_delta_suffix",
                        }
                    )

            if stabilizations:
                try:
                    evidence_records = [
                        ContinuityEvidenceRecord.model_validate(item)
                        for item in (summary_row.evidence_records or [])
                    ]
                except ValidationError as exc:
                    raise CoreStateServiceError(
                        f"核心状态增量候选的 evidence_records 损坏: {exc}"
                    ) from exc
                original_candidate_json = [
                    item.model_dump(mode="json") for item in candidates
                ]
                old_ledger_hash = compute_continuity_ledger_hash(
                    chapter_id=summary_row.chapter_id,
                    workspace_id=summary_row.workspace_id,
                    summary_text=summary_row.summary_text,
                    pov_character=summary_row.pov_character,
                    scene_setting=summary_row.scene_setting,
                    key_events=list(summary_row.key_events or []),
                    character_interactions=list(
                        summary_row.character_interactions or []
                    ),
                    unfinished_actions=list(summary_row.unfinished_actions or []),
                    character_states=list(summary_row.character_states or []),
                    established_facts=list(summary_row.established_facts or []),
                    revealed_mechanics=list(summary_row.revealed_mechanics or []),
                    core_state_candidates=_core_candidates_for_ledger_hash(
                        original_candidate_json
                    ),
                    raw_tail=summary_row.raw_final_tail,
                    source_version_id=summary_row.source_version_id,
                    source_text_hash=summary_row.source_text_hash,
                    evidence_records=evidence_records,
                )
                if old_ledger_hash != summary_row.ledger_hash:
                    raise CoreStateServiceError(
                        "核心状态增量候选修复前 continuity ledger hash 不可复算"
                    )

                updated_records = list(evidence_records)
                for index, (candidate, stabilized) in enumerate(
                    zip(candidates, stabilized_candidates, strict=True)
                ):
                    if _canonical_value(candidate.value) == _canonical_value(
                        stabilized.value
                    ):
                        continue
                    matches = [
                        record
                        for record in evidence_records
                        if record.field == "core_state_candidates"
                        and record.index == index
                        and record.fact_text
                        == f"{candidate.state_key}={candidate.value}"
                        and record.evidence_quote == candidate.evidence_quote
                        and record.evidence_fingerprint
                        == candidate.evidence_fingerprint
                    ]
                    if len(matches) != 1:
                        raise CoreStateServiceError(
                            "核心状态增量候选缺少唯一同源 evidence record: "
                            f"{candidate.state_key}"
                        )
                    record = matches[0]
                    updated_records[updated_records.index(record)] = (
                        record.model_copy(
                            update={
                                "fact_text": (
                                    f"{stabilized.state_key}={stabilized.value}"
                                )
                            }
                        )
                    )

                stabilized_candidate_json = [
                    item.model_dump(mode="json")
                    for item in stabilized_candidates
                ]
                summary_row.evidence_records = [
                    item.model_dump(mode="json") for item in updated_records
                ]
                summary_row.ledger_hash = compute_continuity_ledger_hash(
                    chapter_id=summary_row.chapter_id,
                    workspace_id=summary_row.workspace_id,
                    summary_text=summary_row.summary_text,
                    pov_character=summary_row.pov_character,
                    scene_setting=summary_row.scene_setting,
                    key_events=list(summary_row.key_events or []),
                    character_interactions=list(
                        summary_row.character_interactions or []
                    ),
                    unfinished_actions=list(summary_row.unfinished_actions or []),
                    character_states=list(summary_row.character_states or []),
                    established_facts=list(summary_row.established_facts or []),
                    revealed_mechanics=list(summary_row.revealed_mechanics or []),
                    core_state_candidates=_core_candidates_for_ledger_hash(
                        stabilized_candidate_json
                    ),
                    raw_tail=summary_row.raw_final_tail,
                    source_version_id=summary_row.source_version_id,
                    source_text_hash=summary_row.source_text_hash,
                    evidence_records=updated_records,
                )
                summary_row.updated_at = datetime.now(UTC)
                candidates = stabilized_candidates

            admitted: list[CoreStateCandidate] = []
            reaffirmed = 0
            soft = 0
            conflicts: list[str] = []
            classified: list[CoreStateCandidate] = []

            for candidate in candidates:
                status, reason = self._classify_candidate(
                    candidate=candidate, card=card
                )
                if status != "proposed":
                    soft += 1
                    classified.append(
                        candidate.model_copy(
                            update={
                                "admission_status": status,
                                "admission_reason": reason,
                            }
                        )
                    )
                    continue
                active = current.get(candidate.state_key)
                if active is not None and _canonical_value(
                    active.value
                ) == _canonical_value(candidate.value):
                    reaffirmed += 1
                    classified.append(
                        candidate.model_copy(
                            update={
                                "admission_status": "reaffirmed",
                                "admission_reason": "与当前有效值一致，不重复创建 revision",
                            }
                        )
                    )
                    continue
                transition = self._transition_for_candidate(
                    candidate=candidate, card=card
                )
                if (
                    active is None
                    and seq > 1
                    and candidate.entity_type == "object"
                    and transition is None
                ):
                    soft += 1
                    classified.append(
                        candidate.model_copy(
                            update={
                                "admission_status": "soft_not_core",
                                "admission_reason": (
                                    "非首章首次物件核心事实缺少章节卡 from=null 许可；"
                                    "正文摘要不能自行扩充 HARD 物件名单"
                                ),
                            }
                        )
                    )
                    continue
                if active is not None:
                    valid_transition = (
                        transition is not None
                        and transition.from_value is not None
                        and _canonical_value(transition.from_value)
                        == _canonical_value(active.value)
                        and _canonical_value(transition.to_value)
                        == _canonical_value(candidate.value)
                    )
                    if not valid_transition:
                        reason = (
                            f"{candidate.state_key}: 当前={active.value!r}, "
                            f"候选={candidate.value!r}, 卡片无匹配 from→to 许可"
                        )
                        conflicts.append(reason)
                        classified.append(
                            candidate.model_copy(
                                update={
                                    "admission_status": "rejected_conflict",
                                    "admission_reason": reason,
                                }
                            )
                        )
                        continue
                elif transition is not None and transition.from_value is not None:
                    reason = f"{candidate.state_key}: 当前未建立但迁移声明了 from_value"
                    conflicts.append(reason)
                    classified.append(
                        candidate.model_copy(
                            update={
                                "admission_status": "rejected_conflict",
                                "admission_reason": reason,
                            }
                        )
                    )
                    continue
                elif (
                    transition is not None
                    and _canonical_value(transition.to_value)
                    != _canonical_value(candidate.value)
                ):
                    reason = (
                        f"{candidate.state_key}: 首次建立候选={candidate.value!r}, "
                        f"与卡片 to_value={transition.to_value!r} 不一致"
                    )
                    conflicts.append(reason)
                    classified.append(
                        candidate.model_copy(
                            update={
                                "admission_status": "rejected_conflict",
                                "admission_reason": reason,
                            }
                        )
                    )
                    continue
                elif (
                    candidate.entity_type == "character"
                    and candidate.attribute in _CHARACTER_IRREVERSIBLE_ATTRIBUTES
                    and transition is None
                ):
                    reason = (
                        f"{candidate.state_key}: 不可逆角色状态首次出现也必须由"
                        "章节卡显式声明 from=(未建立)→to 许可"
                    )
                    conflicts.append(reason)
                    classified.append(
                        candidate.model_copy(
                            update={
                                "admission_status": "rejected_conflict",
                                "admission_reason": reason,
                            }
                        )
                    )
                    continue
                if len(admitted) >= _MAX_NEW_CORE_FACTS_PER_CHAPTER:
                    soft += 1
                    classified.append(
                        candidate.model_copy(
                            update={
                                "admission_status": "soft_not_core",
                                "admission_reason": (
                                    "超过每章 2 条新核心事实的注意力准入上限，保留 SOFT"
                                ),
                            }
                        )
                    )
                    continue
                admitted.append(candidate)
                classified.append(
                    candidate.model_copy(
                        update={
                            "admission_status": "admitted",
                            "admission_reason": "确定性准入并绑定批准正文 exact evidence",
                        }
                    )
                )

            from src.services.relation_registration_recovery import (
                recovery_payload, decision_path, validated_unconfirmed_keys, validated_transient_keys,
            )
            unconfirmed_registrations: list[str] = []
            completed_transient_relations: list[str] = []
            registration_payload = recovery_payload(card=card, candidates=candidates,
                projection=projection_before, paths=paths, text=final_text,
                ledger_hash=summary_row.ledger_hash, source_version_id=source_version_id)
            if registration_payload is not None:
                record_path = decision_path(paths=paths, payload=registration_payload)
                if record_path.exists():
                    unconfirmed_registrations = validated_unconfirmed_keys(
                        payload=registration_payload, record=json.loads(record_path.read_text()))
                    completed_transient_relations = validated_transient_keys(
                        payload=registration_payload, record=json.loads(record_path.read_text()))
            admitted_keys = {item.state_key for item in admitted}
            missing_transitions = [
                transition.state_key
                for transition in card.core_state_transitions
                if transition.state_key not in admitted_keys
                and transition.state_key not in unconfirmed_registrations
                and transition.state_key not in completed_transient_relations
                and not (
                    transition.state_key in current
                    and _canonical_value(current[transition.state_key].value)
                    == _canonical_value(transition.to_value)
                )
            ]
            if missing_transitions:
                conflicts.append(
                    _MISSING_TRANSITION_EVIDENCE_PREFIX
                    + ", ".join(missing_transitions)
                )

            summary_row.core_state_candidates = [
                item.model_dump(mode="json") for item in classified
            ]
            if conflicts:
                chapter_row.core_state_commit_hash = ""
                chapter_row.core_state_commit_status = "pending"
                chapter_row.core_state_commit_details = {
                    "schema_version": (
                        2
                        if card.book_clock_constraint is not None
                        and card.book_clock_constraint.contract_version == 1
                        else 1
                    ),
                    "source_version_id": source_version_id,
                    "source_text_hash": source_text_hash,
                    "card_projection_hash": card.core_state_projection_hash,
                    "conflicts": conflicts,
                    "candidate_stabilizations": stabilizations,
                }
                session.commit()
                raise CoreStateConflictError(
                    chapter_id=card.chapter_id, conflicts=conflicts
                )

            active_rows = {
                row.state_key: row
                for row in self._active_object_rows(session, workspace_id=workspace_id)
            }
            new_rows: list[CoreStateRevisionORM] = []
            transition_by_key = {
                item.state_key: item for item in card.core_state_transitions
            }
            for candidate in admitted:
                if candidate.entity_type != "object":
                    continue
                prior_row = active_rows.get(candidate.state_key)
                if prior_row is not None:
                    self._supersede_row(prior_row, seq=seq)
                transition = transition_by_key.get(candidate.state_key)
                row = self._new_revision(
                    workspace_id=workspace_id,
                    state_key=candidate.state_key,
                    entity_type="object",
                    entity_id=candidate.entity_id,
                    attribute=candidate.attribute,
                    value_json={"value": candidate.value},
                    value_text=candidate.value,
                    source_kind="approved_text",
                    chapter_id=card.chapter_id,
                    source_version_id=source_version_id,
                    source_path=(
                        f"chapter_summaries/{card.chapter_id}."
                        f"core_state_candidates[{candidate.state_key}]"
                    ),
                    source_text_hash=source_text_hash,
                    evidence_quote=candidate.evidence_quote,
                    seq=seq,
                    supersedes_revision_id=(
                        prior_row.revision_id if prior_row is not None else None
                    ),
                    change_reason=(
                        transition.reason
                        if transition is not None
                        else "首次建立核心状态"
                    ),
                )
                session.add(row)
                new_rows.append(row)

            self._apply_character_candidates(
                session,
                workspace_id=workspace_id,
                chapter_id=card.chapter_id,
                source_version_id=source_version_id,
                source_text_hash=source_text_hash,
                seq=seq,
                candidates=[
                    item for item in admitted if item.entity_type == "character"
                ],
                transition_by_key=transition_by_key,
            )

            prior_clock = active_rows.get(_BOOK_CLOCK_STATE_KEY)
            if prior_clock is not None:
                self._supersede_row(prior_clock, seq=seq)
            clock = card.book_clock_constraint
            assert clock is not None
            clock_value: dict[str, Any]
            projected_book_time: BookTimeProjection | None = None
            if clock.contract_version == 1:
                projected_book_time = advance_book_time_projection(
                    previous_projection=projection_before.book_time,
                    constraint=clock,
                )
                current_absolute = (
                    projected_book_time.current_node.absolute_interval
                    if projected_book_time.current_node is not None
                    else None
                )
                clock_value = {
                    "clock_contract_version": 1,
                    "known": current_absolute is not None,
                    "book_time": projected_book_time.model_dump(mode="json"),
                }
                if current_absolute is not None:
                    clock_value.update(current_absolute.model_dump(mode="json"))
                clock_text = f"{clock.kind}: {clock.time_gap}"
            elif clock.is_hard:
                assert clock.current_interval is not None
                clock_value = {
                    "known": True,
                    **clock.current_interval.model_dump(mode="json"),
                }
                clock_text = (
                    f"{clock.current_interval.minimum_minutes}-"
                    f"{clock.current_interval.maximum_minutes} minutes"
                )
            else:
                clock_value = {"known": False, "reason": clock.soft_reason}
                clock_text = "unknown"
            clock_row = self._new_revision(
                workspace_id=workspace_id,
                state_key=_BOOK_CLOCK_STATE_KEY,
                entity_type="book_clock",
                entity_id="main",
                attribute="elapsed_minutes",
                value_json=clock_value,
                value_text=clock_text,
                source_kind="chapter_card",
                chapter_id=card.chapter_id,
                source_version_id=source_version_id,
                source_path=(
                    f"chapter_cards/{card.chapter_id}.continuity_context.time_gap"
                ),
                source_text_hash=_chapter_card_hash(card),
                evidence_quote=card.continuity_context.time_gap,
                seq=seq,
                supersedes_revision_id=(
                    prior_clock.revision_id if prior_clock is not None else None
                ),
                change_reason=(
                    "typed 书内时间节点与关系链推进"
                    if clock.contract_version == 1
                    else "主时间线间隔确定性累计"
                    if clock.is_hard
                    else "legacy 含混或非主时间线间隔形成未知屏障"
                ),
            )
            session.add(clock_row)
            new_rows.append(clock_row)
            session.flush()

            projection_after = self._projection_from_session(
                session,
                workspace_id=workspace_id,
                before_chapter=seq + 1,
            )
            render_core_state_capsule(
                projection=projection_after,
                transitions=[],
                book_clock_constraint=None,
            )
            for row in new_rows:
                row.ledger_hash = projection_after.projection_hash
            commit_hash = _commit_hash(
                {
                    "chapter_id": card.chapter_id,
                    "source_version_id": source_version_id,
                    "source_text_hash": source_text_hash,
                    "card_projection_hash": card.core_state_projection_hash,
                    "projection_before_hash": projection_before.projection_hash,
                    "projection_after_hash": projection_after.projection_hash,
                    "candidates": [item.model_dump(mode="json") for item in classified],
                    "book_clock": clock.model_dump(mode="json"),
                    "unconfirmed_registrations": unconfirmed_registrations,
                    **({"completed_transient_relations": completed_transient_relations}
                       if completed_transient_relations else {}),
                }
            )
            chapter_row.core_state_commit_hash = commit_hash
            chapter_row.core_state_commit_status = "committed"
            chapter_row.core_state_commit_details = {
                "schema_version": 2 if clock.contract_version == 1 else 1,
                "source_version_id": source_version_id,
                "source_text_hash": source_text_hash,
                "card_projection_hash": card.core_state_projection_hash,
                "projection_before_hash": projection_before.projection_hash,
                "projection_after_hash": projection_after.projection_hash,
                "admitted_count": len(admitted),
                "reaffirmed_count": reaffirmed,
                "soft_count": soft,
                "book_clock_hard": clock.is_hard,
                "book_clock_contract_version": clock.contract_version,
                "book_time_kind": clock.kind,
                "book_time_node_id": clock.node_id or None,
                "book_time_relation_chain_hash": (
                    projected_book_time.relation_chain_hash
                    if projected_book_time is not None
                    else None
                ),
                "candidate_stabilizations": stabilizations,
                "unconfirmed_registrations": unconfirmed_registrations,
                    **({"completed_transient_relations": completed_transient_relations}
                       if completed_transient_relations else {}),
            }
            session.commit()
            _log.info(
                "core_state_committed",
                workspace_id=workspace_id,
                chapter_id=card.chapter_id,
                source_version_id=source_version_id,
                commit_hash=commit_hash,
                projection_after_hash=projection_after.projection_hash,
                admitted_count=len(admitted),
                reaffirmed_count=reaffirmed,
                soft_count=soft,
                book_time_kind=clock.kind,
                book_time_node_id=clock.node_id or None,
                book_time_relation_chain_hash=(
                    projected_book_time.relation_chain_hash
                    if projected_book_time is not None
                    else None
                ),
            )
            return CoreStateCommitResult(
                chapter_id=card.chapter_id,
                source_version_id=source_version_id,
                status="committed",
                commit_hash=commit_hash,
                projection_before_hash=projection_before.projection_hash,
                projection_after_hash=projection_after.projection_hash,
                admitted_count=len(admitted),
                reaffirmed_count=reaffirmed,
                soft_count=soft,
            )
        except Exception:
            if session.in_transaction():
                session.rollback()
            raise
        finally:
            session.close()

    def require_committed(
        self,
        *,
        workspace_id: str,
        card: ChapterCard,
        source_version_id: int,
    ) -> str:
        if card.core_state_contract_version == 0:
            return ""
        session = self._session_factory(workspace_id)
        try:
            row = session.get(ChapterORM, card.chapter_id)
            details = dict(row.core_state_commit_details or {}) if row else {}
            clock = card.book_clock_constraint
            clock_details_match = bool(
                clock is not None
                and (
                    clock.contract_version == 0
                    or (
                        details.get("schema_version") == 2
                        and details.get("book_clock_contract_version") == 1
                        and details.get("book_time_node_id") == clock.node_id
                        and details.get("book_time_relation_chain_hash")
                        == clock.relation_chain_hash
                    )
                )
            )
            if (
                row is None
                or row.core_state_commit_status != "committed"
                or len(row.core_state_commit_hash) != 64
                or details.get("source_version_id") != source_version_id
                or details.get("card_projection_hash")
                != card.core_state_projection_hash
                or not clock_details_match
            ):
                raise CoreStateServiceError(
                    f"章节核心状态尚未完成同源提交: {card.chapter_id}"
                )
            return row.core_state_commit_hash
        finally:
            session.close()

    @classmethod
    def retract_from_chapter_in_session(
        cls,
        session: Session,
        *,
        workspace_id: str,
        target_chapter_seq: int,
        affected_chapter_ids: list[str],
        now: datetime,
    ) -> int:
        """Rewind 同事务撤回来源 revision、角色快照和提交水位并重建 active。"""

        rows = list(
            session.scalars(
                select(CoreStateRevisionORM)
                .where(CoreStateRevisionORM.workspace_id == workspace_id)
                .order_by(
                    CoreStateRevisionORM.state_key,
                    CoreStateRevisionORM.valid_from_chapter,
                )
            )
        )
        retracted = 0
        for row in rows:
            if row.valid_from_chapter >= target_chapter_seq:
                if row.status != "retracted":
                    retracted += 1
                row.status = "retracted"
                row.retracted_at = now
                row.valid_to_chapter = row.valid_from_chapter
        # partial unique index 只允许每个 state_key 一条 active；先把后续
        # revision 的 retracted 状态落进当前事务，再重新开放前驱。
        session.flush()

        by_key: dict[str, list[CoreStateRevisionORM]] = {}
        for row in rows:
            if row.status != "retracted":
                by_key.setdefault(row.state_key, []).append(row)
        for chain in by_key.values():
            chain.sort(key=lambda item: item.valid_from_chapter)
            for index, row in enumerate(chain):
                is_latest = index == len(chain) - 1
                row.status = "active" if is_latest else "superseded"
                row.valid_to_chapter = (
                    None if is_latest else chain[index + 1].valid_from_chapter - 1
                )

        characters = list(
            session.scalars(
                select(CharacterORM).where(CharacterORM.workspace_id == workspace_id)
            )
        )
        for character in characters:
            snapshots = [
                CharacterStateSnapshot.model_validate(raw)
                for raw in (character.state_timeline or [])
            ]
            kept = [
                snapshot
                for snapshot in snapshots
                if snapshot.as_of_chapter < target_chapter_seq
            ]
            if len(kept) == len(snapshots):
                continue
            if kept:
                latest = max(kept, key=lambda item: item.as_of_chapter)
                reopened = [
                    fact.model_copy(update={"valid_to_chapter": None})
                    if fact.valid_to_chapter is not None
                    and fact.valid_to_chapter >= target_chapter_seq - 1
                    else fact
                    for fact in latest.core_facts
                ]
                kept = [
                    item
                    if item.as_of_chapter != latest.as_of_chapter
                    else latest.model_copy(update={"core_facts": reopened})
                    for item in kept
                ]
            character.state_timeline = [
                snapshot.model_dump(mode="json") for snapshot in kept
            ]
            # rich-memory current_state 与核心账本共享旧角色表，但只有
            # core.* 命名空间受本服务管辖。Rewind 先清核心镜像，再从保留的
            # 最新 approved_text 快照重建，避免后续 Writer 看到幽灵值。
            current_state = {
                key: value
                for key, value in dict(character.current_state or {}).items()
                if not str(key).startswith("core.")
            }
            if kept:
                latest_kept = max(kept, key=lambda item: item.as_of_chapter)
                for fact in latest_kept.core_facts:
                    if fact.valid_to_chapter is None:
                        current_state[f"core.{fact.attribute}"] = fact.value
            character.current_state = current_state

        rebuilt_projection = cls._projection_from_session(
            session,
            workspace_id=workspace_id,
            before_chapter=target_chapter_seq,
        )
        rebuilt_book_time = rebuilt_projection.book_time
        if rebuilt_book_time is not None:
            retained_node_ids: set[str] = set()
            for row in rows:
                if row.status == "retracted" or row.entity_type != "book_clock":
                    continue
                value = dict(row.value_json or {})
                if value.get("clock_contract_version") == 1:
                    try:
                        snapshot = BookTimeProjection.model_validate(value.get("book_time"))
                    except ValidationError as exc:
                        raise CoreStateServiceError(
                            f"Rewind 发现损坏的 typed book-time snapshot: {exc}"
                        ) from exc
                    if snapshot.current_node is not None:
                        retained_node_ids.add(snapshot.current_node.node_id)
                elif value.get("known") is True:
                    retained_node_ids.add(
                        hashlib.sha256(
                            f"legacy-v0:{row.revision_id}".encode("utf-8")
                        ).hexdigest()
                    )
            referenced_node_ids = {
                node.node_id for node in rebuilt_book_time.active_relation_chain
            }
            referenced_node_ids.update(
                node.anchor_node_id
                for node in rebuilt_book_time.active_relation_chain
                if node.anchor_node_id is not None
            )
            if rebuilt_book_time.latest_absolute_anchor is not None:
                referenced_node_ids.add(
                    rebuilt_book_time.latest_absolute_anchor.node_id
                )
            missing = sorted(referenced_node_ids - retained_node_ids)
            if missing:
                raise CoreStateServiceError(
                    "Rewind 后 book-time relation 指向 missing/retracted node: "
                    + ", ".join(missing)
                )

        for chapter_id in affected_chapter_ids:
            chapter = session.get(ChapterORM, chapter_id)
            if chapter is None:
                continue
            chapter.core_state_commit_hash = ""
            chapter.core_state_commit_status = "retracted"
            previous_schema_version = dict(
                chapter.core_state_commit_details or {}
            ).get("schema_version")
            chapter.core_state_commit_details = {
                "schema_version": 2 if previous_schema_version == 2 else 1,
                "rewound_at": now.isoformat(),
                "target_chapter_seq": target_chapter_seq,
                "rewind_projection_hash": rebuilt_projection.projection_hash,
                "book_time_relation_chain_hash": (
                    rebuilt_book_time.relation_chain_hash
                    if rebuilt_book_time is not None
                    else None
                ),
            }
        _log.info(
            "core_state_book_time_rewind_rebuilt",
            workspace_id=workspace_id,
            target_chapter_seq=target_chapter_seq,
            projection_hash=rebuilt_projection.projection_hash,
            book_time_contract_version=rebuilt_projection.book_time_contract_version,
            book_time_relation_chain_hash=(
                rebuilt_book_time.relation_chain_hash
                if rebuilt_book_time is not None
                else None
            ),
        )
        return retracted

    def list_audit_items(self, workspace_id: str) -> list[CoreStateAuditItem]:
        """返回 revision 历史、角色当前核心事实和未采纳候选。"""

        session = self._session_factory(workspace_id)
        try:
            items: list[CoreStateAuditItem] = []
            rows = list(
                session.scalars(
                    select(CoreStateRevisionORM)
                    .where(CoreStateRevisionORM.workspace_id == workspace_id)
                    .order_by(
                        CoreStateRevisionORM.valid_from_chapter,
                        CoreStateRevisionORM.state_key,
                    )
                )
            )
            for row in rows:
                items.append(
                    CoreStateAuditItem(
                        item_type="revision",
                        revision_id=row.revision_id,
                        state_key=row.state_key,
                        entity_type=row.entity_type,
                        entity_id=row.entity_id,
                        attribute=row.attribute,
                        value=row.value_text,
                        status=row.status,
                        source_chapter_id=row.source_chapter_id,
                        source_version_id=row.source_version_id,
                        source_path=row.source_path,
                        source_text_hash=row.source_text_hash,
                        evidence_quote=row.evidence_quote,
                        valid_from_chapter=row.valid_from_chapter,
                        valid_to_chapter=row.valid_to_chapter,
                        reason=row.change_reason,
                    )
                )
            projection = self._projection_from_session(
                session, workspace_id=workspace_id
            )
            for fact in projection.facts:
                if fact.entity_type != "character":
                    continue
                items.append(
                    CoreStateAuditItem(
                        item_type="character_state",
                        revision_id=fact.revision_id,
                        state_key=fact.state_key,
                        entity_type="character",
                        entity_id=fact.entity_id,
                        attribute=fact.attribute,
                        value=fact.value,
                        status="active",
                        source_chapter_id=fact.source_chapter_id,
                        source_version_id=fact.source_version_id,
                        source_path=fact.source_path,
                        source_text_hash=fact.source_text_hash,
                        evidence_quote=fact.evidence_quote,
                        valid_from_chapter=fact.valid_from_chapter,
                        valid_to_chapter=fact.valid_to_chapter,
                    )
                )
            summaries = list(
                session.scalars(
                    select(ChapterSummaryORM)
                    .where(ChapterSummaryORM.workspace_id == workspace_id)
                    .order_by(ChapterSummaryORM.chapter_id)
                )
            )
            for summary in summaries:
                for candidate in self._parse_candidates(summary):
                    if candidate.admission_status in {"admitted", "reaffirmed"}:
                        continue
                    items.append(
                        CoreStateAuditItem(
                            item_type="candidate",
                            state_key=candidate.state_key,
                            entity_type=candidate.entity_type,
                            entity_id=candidate.entity_id,
                            attribute=candidate.attribute,
                            value=candidate.value,
                            status=candidate.admission_status,
                            source_chapter_id=summary.chapter_id,
                            source_version_id=summary.source_version_id,
                            source_path=(
                                f"chapter_summaries/{summary.chapter_id}."
                                "core_state_candidates"
                            ),
                            source_text_hash=summary.source_text_hash,
                            evidence_quote=candidate.evidence_quote,
                            valid_from_chapter=(
                                chapter_seq_from_id(summary.chapter_id) or 1
                            ),
                            reason=candidate.admission_reason,
                        )
                    )
            return items
        finally:
            session.close()


__all__ = [
    "CoreStateAuditItem",
    "CoreStateCommitResult",
    "CoreStateConflictError",
    "CoreStateService",
    "CoreStateServiceError",
]
