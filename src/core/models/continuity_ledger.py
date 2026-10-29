"""R4 连续性小账本的纯数据合同与确定性校验辅助。

本模块不做数据库或文件 IO。调用方负责读取 ``final.md`` / version / card，
再用这里的 SHA-256、逐条证据覆盖和 ledger hash 规则完成硬水位校验。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
FIRST_CHAPTER_TIME_GAP_SENTINELS = frozenset(
    {"开篇", "首章", "无(本章为首章)", "无（本章为首章）"}
)
"""只有全书首章可用；已有上一章 ledger 时必须拒绝或先确定性归一。"""


class ContinuityEvidenceRecord(BaseModel):
    """一条结构化连续性事实到 ``final.md`` 原文的可回查映射。"""

    field: str = Field(..., min_length=1)
    index: int = Field(..., ge=0)
    fact_text: str = Field(..., min_length=1)
    evidence_quote: str = Field(..., min_length=1)
    evidence_fingerprint: str = Field(..., pattern=_SHA256_PATTERN)
    is_immediate: bool = False


class ContinuityLedgerProvenance(BaseModel):
    """章节卡冻结的上一章连续性小账本 provenance。"""

    chapter_id: str = Field(..., min_length=1)
    source_version_id: int = Field(..., ge=1)
    source_text_hash: str = Field(..., pattern=_SHA256_PATTERN)
    ledger_hash: str = Field(..., pattern=_SHA256_PATTERN)


@dataclass(frozen=True)
class ProvenanceBoundContinuityCapsule:
    """只在一次调用内共享的、来源已验真的连续性快照。

    这是纯 Python 瞬态合同，不进入 ChapterCard/SQLite/API JSON。结构化长程
    字段以 canonical JSON 冻结，避免 Card、Writer、Judge 各自从来源不明的
    自由文本重建同一事实。``provenance`` 仍是唯一来源身份；``capsule_hash``
    只供离线 render/coverage oracle 使用，不替代 ledger hash。
    """

    provenance: ContinuityLedgerProvenance
    summary_text: str
    pov_character: str | None
    scene_setting: str | None
    raw_final_tail: str
    unfinished_actions: tuple[str, ...]
    key_events_json: str
    character_interactions_json: str
    character_states_json: str
    established_facts_json: str
    revealed_mechanics_json: str
    core_state_candidates_json: str
    evidence_records: tuple[ContinuityEvidenceRecord, ...]
    capsule_hash: str

    @property
    def field_coverage(self) -> tuple[str, ...]:
        """返回固定 coverage 表；顺序参与离线 oracle，不参与业务语义。"""

        return (
            "chapter_id",
            "source_version_id",
            "source_text_hash",
            "ledger_hash",
            "summary_text",
            "pov_character",
            "scene_setting",
            "raw_final_tail",
            "unfinished_actions",
            "key_events",
            "character_interactions",
            "character_states",
            "established_facts",
            "revealed_mechanics",
            "core_state_candidates",
            "evidence_records",
        )

    def assert_matches_projection(
        self,
        *,
        frozen_provenance: ContinuityLedgerProvenance | None,
        prev_chapter_tail: str,
        unfinished_actions: list[str],
        pov_from: str | None,
    ) -> None:
        """证明消费方仍在使用同一来源的逐字段投影。"""

        validate_frozen_provenance(
            frozen=frozen_provenance,
            current=self.provenance,
        )
        validate_continuity_projection(
            ledger_raw_final_tail=self.raw_final_tail,
            ledger_unfinished_actions=list(self.unfinished_actions),
            ledger_pov_character=self.pov_character,
            card_prev_chapter_tail=prev_chapter_tail,
            card_unfinished_actions=unfinished_actions,
            card_pov_from=pov_from,
        )


def sha256_text(text: str) -> str:
    """返回 UTF-8 文本的完整 SHA-256。"""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def raw_final_tail(chapter_text: str, *, limit: int = 800) -> str:
    """返回确定性的 ``final.md`` 尾部上下文。"""

    return chapter_text[-limit:]


def make_evidence_record(
    *,
    field: str,
    index: int,
    fact_text: str,
    evidence_quote: str,
    chapter_text: str,
    is_immediate: bool = False,
) -> ContinuityEvidenceRecord:
    """构造并验证一条原文证据；quote 必须能在同一份 final 中逐字定位。"""

    quote = evidence_quote.strip()
    if not quote:
        raise ValueError(f"{field}[{index}] 缺少 evidence_quote")
    if quote not in chapter_text:
        raise ValueError(f"{field}[{index}] evidence_quote 无法在 final.md 定位")
    return ContinuityEvidenceRecord(
        field=field,
        index=index,
        fact_text=fact_text.strip(),
        evidence_quote=quote,
        evidence_fingerprint=sha256_text(quote),
        is_immediate=is_immediate,
    )


def validate_evidence_records(
    *,
    records: list[ContinuityEvidenceRecord],
    chapter_text: str,
    expected_facts: dict[tuple[str, int], str],
) -> None:
    """验证逐条覆盖、事实字面、quote 定位与 fingerprint。

    ``expected_facts`` 由已持久化结构字段确定，因此多证据、漏证据、错位证据
    都不能靠 LLM 调用成功蒙混过硬水位。
    """

    actual: dict[tuple[str, int], ContinuityEvidenceRecord] = {}
    for record in records:
        key = (record.field, record.index)
        if key in actual:
            raise ValueError(f"连续性证据重复: {record.field}[{record.index}]")
        actual[key] = record
        if record.evidence_quote not in chapter_text:
            raise ValueError(
                f"{record.field}[{record.index}] evidence_quote 已不在当前 final.md"
            )
        if record.evidence_fingerprint != sha256_text(record.evidence_quote):
            raise ValueError(
                f"{record.field}[{record.index}] evidence_fingerprint 不匹配"
            )

    if set(actual) != set(expected_facts):
        missing = sorted(set(expected_facts) - set(actual))
        extra = sorted(set(actual) - set(expected_facts))
        raise ValueError(f"连续性证据覆盖不完整: missing={missing} extra={extra}")

    for key, fact_text in expected_facts.items():
        if actual[key].fact_text != fact_text:
            raise ValueError(
                f"{key[0]}[{key[1]}] fact_text 与结构字段不一致"
            )


def immediate_action_indices(
    *, actions: list[str], records: list[ContinuityEvidenceRecord],
) -> list[int]:
    """从完整有源记录机械选择即时动作；漏项、错位不能降成空义务。"""
    indexed: dict[int, ContinuityEvidenceRecord] = {}
    for record in records:
        if record.field != "unfinished_actions":
            continue
        if record.index in indexed:
            raise ValueError("即时动作分类来源有重复索引")
        indexed[record.index] = record
    if set(indexed) != set(range(len(actions))):
        raise ValueError("即时动作分类来源未完整覆盖前章事项")
    for index, action in enumerate(actions):
        if indexed[index].fact_text != action:
            raise ValueError("即时动作分类来源文本不匹配")
    return [i for i in range(len(actions)) if indexed[i].is_immediate]


def validate_immediate_action_scope(
    *, actions: list[str], records: list[ContinuityEvidenceRecord],
    frozen_indices: list[int] | None,
) -> None:
    """新版索引须匹配已经通过 hash/逐字证据验真的账本；旧卡不改语义。"""
    if frozen_indices is not None and frozen_indices != immediate_action_indices(
        actions=actions, records=records,
    ):
        raise ValueError("章节卡即时动作范围与冻结账本漂移")


def compute_continuity_ledger_hash(
    *,
    chapter_id: str,
    workspace_id: str,
    summary_text: str,
    pov_character: str | None,
    scene_setting: str | None,
    key_events: list[dict[str, Any]],
    character_interactions: list[dict[str, Any]],
    unfinished_actions: list[str],
    character_states: list[dict[str, Any]],
    established_facts: list[str],
    revealed_mechanics: list[str],
    raw_tail: str,
    source_version_id: int,
    source_text_hash: str,
    evidence_records: list[ContinuityEvidenceRecord],
    core_state_candidates: list[dict[str, Any]] | None = None,
) -> str:
    """按稳定 JSON 计算小账本语义 hash；时间戳和模型名不参与。"""

    payload = {
        "chapter_id": chapter_id,
        "workspace_id": workspace_id,
        "summary_text": summary_text,
        "pov_character": pov_character,
        "scene_setting": scene_setting,
        "key_events": key_events,
        "character_interactions": character_interactions,
        "unfinished_actions": unfinished_actions,
        "character_states": character_states,
        "established_facts": established_facts,
        "revealed_mechanics": revealed_mechanics,
        "raw_final_tail": raw_tail,
        "source_version_id": source_version_id,
        "source_text_hash": source_text_hash,
        "evidence_records": [
            record.model_dump(mode="json") for record in evidence_records
        ],
    }
    # None 保持 0031 以前账本逐字 hash；新 extractor 显式传 list（含空列表）
    # 后才把候选维度纳入语义身份，legacy workspace 无需自动回填。
    if core_state_candidates is not None:
        payload["core_state_candidates"] = core_state_candidates
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256_text(canonical)


def validate_frozen_provenance(
    *,
    frozen: ContinuityLedgerProvenance | None,
    current: ContinuityLedgerProvenance,
) -> None:
    """验证章节卡冻结值与当前上一章小账本完全一致。"""

    if frozen is None:
        raise ValueError("章节卡缺少上一章 continuity ledger provenance")
    if frozen != current:
        raise ValueError(
            "章节卡 continuity ledger provenance 已陈旧: "
            f"frozen={frozen.model_dump(mode='json')} "
            f"current={current.model_dump(mode='json')}"
        )


def validate_continuity_projection(
    *,
    ledger_raw_final_tail: str,
    ledger_unfinished_actions: list[str],
    ledger_pov_character: str | None,
    card_prev_chapter_tail: str,
    card_unfinished_actions: list[str],
    card_pov_from: str | None,
) -> None:
    """验证卡片里的直接前章事实是当前小账本的确定性投影。

    provenance 只能证明卡片绑定了哪本账，不能证明 LLM 或上传方没有在
    ``continuity_context`` 内改写账本内容；因此批准与 Writer 前还要逐字段
    比对尾部、未完动作和前章 POV。
    """

    if card_prev_chapter_tail != ledger_raw_final_tail:
        raise ValueError("continuity_context.prev_chapter_tail 与当前小账本不一致")
    if card_unfinished_actions != ledger_unfinished_actions:
        raise ValueError("continuity_context.unfinished_actions 与当前小账本不一致")
    if card_pov_from != ledger_pov_character:
        raise ValueError("continuity_context.pov_from 与当前小账本不一致")


def validate_continuity_bridge(
    *,
    evidence_records: list[ContinuityEvidenceRecord],
    time_gap: str,
    scene_continuation: str,
    continuity_bridge: str,
) -> None:
    """在即时未完动作遇到跳时/换景时要求逐字可判定的承接说明。"""

    if time_gap.strip() in FIRST_CHAPTER_TIME_GAP_SENTINELS:
        raise ValueError("上一章 continuity ledger 已存在，time_gap 不得使用首章字面")

    immediate_actions = [
        record.fact_text
        for record in evidence_records
        if record.field == "unfinished_actions" and record.is_immediate
    ]
    if not immediate_actions:
        return
    normalized_gap = time_gap.strip()
    is_immediate_gap = any(
        marker in normalized_gap
        for marker in ("立即", "紧接", "同一时刻", "数秒", "片刻")
    )
    has_transition = scene_continuation != "same" or not is_immediate_gap
    if not has_transition:
        return
    bridge = continuity_bridge.strip()
    if not bridge:
        raise ValueError("即时未完动作遇到跳时/换景，但 continuity_bridge 为空")
    missing = [action for action in immediate_actions if action not in bridge]
    if missing:
        raise ValueError(
            "continuity_bridge 未逐字承接即时未完动作: "
            f"missing={missing}"
        )


__all__ = [
    "ContinuityEvidenceRecord",
    "ContinuityLedgerProvenance",
    "ProvenanceBoundContinuityCapsule",
    "FIRST_CHAPTER_TIME_GAP_SENTINELS",
    "compute_continuity_ledger_hash",
    "make_evidence_record",
    "raw_final_tail",
    "sha256_text",
    "validate_continuity_bridge",
    "validate_continuity_projection",
    "validate_evidence_records",
    "validate_frozen_provenance",
]
