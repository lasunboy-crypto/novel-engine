"""同源批准正文的关系登记提案纠正；复用原出卡模型，不改卡或正文。

只处理首次、含混的关系登记，并要求同一对象已有另一项明确关系证据。
真实转移、没有唯一候选、未知响应或来源漂移仍停止。纠正记录不产生事实。
"""
from __future__ import annotations

import json
from pathlib import Path
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.core.models.chapter_card import ChapterCard
from src.core.models.continuity_ledger import sha256_text
from src.core.models.core_state import (
    CORE_STATE_RELATION_ATTRIBUTES, CORE_STATE_RELATION_EVIDENCE_GUIDANCE,
    CoreStateCandidate, CoreStateProjection,
)


class RelationRegistrationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state_key: str
    disposition: Literal["unconfirmed_registration", "required_change", "unresolved"]
    source_quote: str = Field(min_length=1)
    evidence_quote: str = Field(min_length=1)
    explanation: str = Field(min_length=1)


class RelationRegistrationDecisions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[RelationRegistrationDecision] = Field(min_length=1)


class EndpointRegistrationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state_key: str
    source_quote: str = Field(min_length=1)
    evidence_quote: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    disposition: Literal[
        "unconfirmed_registration", "required_change", "unresolved",
        "completed_transient_change",
    ]
    ending_evidence_quote: str = ""


class EndpointRegistrationDecisions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[EndpointRegistrationDecision] = Field(min_length=1)


def registration_recovery_keys(*, card: ChapterCard,
    candidates: list[CoreStateCandidate], projection: CoreStateProjection,
) -> list[str]:
    """不推断中文语义，只限定允许原出卡模型重新裁决的提案集合。"""
    current = {item.state_key: item.value for item in projection.facts}
    by_key = {item.state_key: item for item in candidates}
    if len(by_key) != len(candidates):
        return []
    keys = []
    for transition in card.core_state_transitions:
        if current.get(transition.state_key) == transition.to_value:
            continue
        candidate = by_key.get(transition.state_key)
        if candidate is not None and candidate.admission_status == "admitted" and not candidate.ambiguous and candidate.value == transition.to_value:
            continue
        if (transition.entity_type != "object" or transition.attribute not in CORE_STATE_RELATION_ATTRIBUTES
            or transition.from_value is not None or transition.state_key in current
            or candidate is None or not candidate.ambiguous
            or candidate.admission_status != "soft_ambiguous"
            or candidate.value != transition.to_value):
            return []
        # 不能把本章唯一的承重关系全部撤掉；另一义必须有明确本章证据。
        sibling = any(other.entity_type == "object" and other.entity_id == transition.entity_id
            and other.attribute in CORE_STATE_RELATION_ATTRIBUTES and other.state_key != transition.state_key
            and other.state_key in by_key and not by_key[other.state_key].ambiguous
            and by_key[other.state_key].admission_status == "admitted"
            and by_key[other.state_key].value == other.to_value
            for other in card.core_state_transitions)
        if not sibling:
            return []
        keys.append(transition.state_key)
    return keys


def recovery_payload(*, card: ChapterCard, candidates: list[CoreStateCandidate],
    projection: CoreStateProjection, paths: Any, text: str, ledger_hash: str,
    source_version_id: int,
) -> dict[str, Any] | None:
    keys = registration_recovery_keys(card=card, candidates=candidates, projection=projection)
    if not keys:
        return None
    from src.core.rules.drama_contracts import load_and_validate_card_dramatic_source
    from src.core.models.outline import MiniPlanOutput
    load_and_validate_card_dramatic_source(card=card, outline_dir=paths.outline_dir)
    mini_plan = MiniPlanOutput.model_validate_json((paths.outline_dir / "mini_plan.json").read_text())
    mini = next(item for item in mini_plan.items if item.chapter_id == card.chapter_id)
    source_parts = [mini.one_line_event, *card.must_achieve]
    if mini.dramatic_contract is not None:
        source_parts.extend(mini.dramatic_contract.natural_requirements())
    for item in candidates:
        if item.evidence_quote not in text or item.evidence_fingerprint != sha256_text(item.evidence_quote):
            raise ValueError("关系登记纠正的正文证据来源漂移")
    payload = {"schema_version": 1, "workspace_id": card.workspace_id, "chapter_id": card.chapter_id,
        "source_version_id": source_version_id, "source_text_hash": sha256_text(text),
        "ledger_hash": ledger_hash, "card": card.model_dump(mode="json"),
        "mini_plan": mini.model_dump(mode="json"), "projection": projection.model_dump(mode="json"),
        "candidates": [item.model_dump(mode="json") for item in candidates],
        "proposal_keys": keys, "source_text": "\n".join(source_parts), "approved_text": text}
    return endpoint_recovery_payload(payload=payload, paths=paths)


def decision_path(*, paths: Any, payload: dict[str, Any]) -> Path:
    digest = sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return paths.chapter_version_file(payload["chapter_id"], f"relation-registration-{digest}.json")


def _validated_decisions(*, payload: dict[str, Any], record: dict[str, Any]) -> Any:
    if record.get("source") != payload or record.get("status") != "responded":
        raise ValueError("关系登记纠正响应未知或来源漂移，禁止重发")
    schema = EndpointRegistrationDecisions if payload.get("schema_version") == 2 else RelationRegistrationDecisions
    result = schema.model_validate_json(record["raw_response"])
    keys = [item.state_key for item in result.decisions]
    if len(set(keys)) != len(keys) or set(keys) != set(payload["proposal_keys"]):
        raise ValueError("关系登记纠正必须逐项返回全部提案，不得漏项/增加/重复")
    candidates = {item["state_key"]: item for item in payload["candidates"]}
    for decision in result.decisions:
        if (decision.source_quote not in payload["source_text"]
            or decision.evidence_quote != candidates[decision.state_key]["evidence_quote"]):
            raise ValueError("关系登记纠正必须有同源规划与原含混证据的逐字引文")
    return result.decisions


def endpoint_recovery_payload(*, payload: dict[str, Any], paths: Any) -> dict[str, Any]:
    """只对非长期候选增加章末裁决；旧未知请求永不借版本升级重发。"""
    eligible = [item["state_key"] for item in payload["candidates"]
                if item["state_key"] in payload["proposal_keys"] and item["long_lived"] is False
                and item["objective"] and item["book_bearing"]]
    if not eligible:
        return payload
    legacy_path = decision_path(paths=paths, payload=payload)
    if legacy_path.exists():
        legacy = json.loads(legacy_path.read_text())
        if legacy.get("status") != "responded":
            return payload  # 原入口继续检查未发出证明；版本升级不绕过未知请求。
        decisions = _validated_decisions(payload=payload, record=legacy)
        # 已知旧回答明确承认事件必须发生，但旧合同无“当章已结束”一档。
        # 未决、无效或未知响应仍按旧合同停止；已通过的旧裁决直接复用。
        required = [item.state_key for item in decisions if item.disposition == "required_change"]
        if (not required or not set(required).issubset(eligible)
            or any(item.disposition == "unresolved" for item in decisions)):
            return payload
    return {**payload, "schema_version": 2, "transient_eligible_keys": eligible}


def validated_registration_keys(*, payload: dict[str, Any], record: dict[str, Any]) -> list[str]:
    decisions = _validated_decisions(payload=payload, record=record)
    candidates = {item["state_key"]: item for item in payload["candidates"]}
    for decision in decisions:
        if decision.disposition == "completed_transient_change":
            quote = decision.ending_evidence_quote
            text = payload["approved_text"]
            if (decision.state_key not in payload.get("transient_eligible_keys", [])
                or candidates[decision.state_key]["long_lived"] is not False
                or not candidates[decision.state_key]["objective"]
                or not candidates[decision.state_key]["book_bearing"]
                or not quote.strip() or text.count(quote) != 1
                or text.count(decision.evidence_quote) != 1
                or text.index(quote) < text.index(decision.evidence_quote) + len(decision.evidence_quote)):
                raise ValueError("当章结束关系必须有唯一、顺序正确的取得与结束正文证据")
        elif decision.disposition != "unconfirmed_registration":
            raise ValueError("关系提案仍属必需变化或未决，禁止自动放行: " + decision.state_key)
    return [item.state_key for item in decisions]


def validated_unconfirmed_keys(*, payload: dict[str, Any], record: dict[str, Any]) -> list[str]:
    """保留旧调用的严格含义；完成过的瞬时关系另记审计，不能冒充未知。"""
    validated_registration_keys(payload=payload, record=record)
    return [item.state_key for item in _validated_decisions(payload=payload, record=record)
            if item.disposition == "unconfirmed_registration"]


def validated_transient_keys(*, payload: dict[str, Any], record: dict[str, Any]) -> list[str]:
    validated_registration_keys(payload=payload, record=record)
    return [item.state_key for item in _validated_decisions(payload=payload, record=record)
            if item.disposition == "completed_transient_change"]


def registration_request_not_dispatched(*, session: Any, workspace_id: str,
    chapter_id: str, path: Path, record: dict[str, Any],
) -> bool:
    """未知响应不能重发；仅请求准备后账本完全没有 Provider attempt 才能续发。"""
    from sqlalchemy import select, func
    from src.core.db.models import AttemptLedgerORM
    if record.get("status") != "request_prepared" or "raw_response" in record:
        return False
    at = datetime.fromisoformat(record["prepared_at"]) if record.get("prepared_at") else datetime.fromtimestamp(path.stat().st_mtime, UTC)
    count = session.scalar(select(func.count()).select_from(AttemptLedgerORM).where(
        AttemptLedgerORM.workspace_id == workspace_id,
        AttemptLedgerORM.chapter_id == chapter_id,
        AttemptLedgerORM.attempt_kind == "provider",
        AttemptLedgerORM.created_at >= at))
    return count == 0


def reconcile_registration(*, payload: dict[str, Any], paths: Any,
    router: Any, client: Any, model: str | None,
    no_dispatch_proof: Callable[[Path, dict[str, Any]], bool] | None = None,
) -> list[str]:
    """原章节卡生成器的一次条件纠正；已知响应只解析复用，不重发。"""
    from src.core.agents._prompt import resolve_routing, append_role_marker
    from src.core.agents._structured_output import prepare_strict_json_output
    from src.core.llm.base import Message
    from src.core.llm.local_json_contract import LocalJsonResponseViolation
    path = decision_path(paths=paths, payload=payload)
    prior_record = None
    if path.exists():
        prior_record = json.loads(path.read_text())
        if (prior_record.get("source") != payload or no_dispatch_proof is None
            or not no_dispatch_proof(path, prior_record)):
            return validated_registration_keys(payload=payload, record=prior_record)
    resolved_client, resolved_model, kwargs = resolve_routing(agent_name="chapter_card_generator",
        router=router, client=client, model=model,
        legacy_kwargs={"max_tokens": 4000, "temperature": 0.2})
    kwargs = prepare_strict_json_output(client=resolved_client, model=resolved_model,
        output_model=(EndpointRegistrationDecisions if payload.get("schema_version") == 2 else RelationRegistrationDecisions), schema_name="relation_registration_decisions",
        complete_kwargs=kwargs)
    # 发出前先冻结来源；崩溃而响应未知时同一记录禁止盲重发。
    record = {"source": payload, "status": "request_prepared", "prepared_at": datetime.now(UTC).isoformat()}
    if prior_record is not None:
        record["prior_local_preflight"] = prior_record
    with path.open("w" if prior_record is not None else "x", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
    system = ("你是原章节卡生成器，现在仅纠正已批准正文之后的首次关系登记提案。"
        + CORE_STATE_RELATION_EVIDENCE_GUIDANCE
        + "全书关系目录只定义允许追踪哪些属性，不证明本章发生了它们。"
        "逐项判断 proposal_keys：若本章规划必须实际获得、失去、转移或授权，返回 required_change；"
        "只有本章并不要求该变化、原卡把照料/操作误当成另一义首次登记，且正文仍含混时，"
        "返回 unconfirmed_registration，含义仅为保留未知，不是关系不存在。"
        "无法排除必需变化返回 unresolved。不得补写正文、推定所有权或把后章结果提前。"
        "每项 source_quote 必须逐字出自 source_text，evidence_quote 必须原样复制对应含混候选。"
        "只输出 decisions JSON，不修改其他字段。")
    if payload.get("schema_version") == 2:
        system += (
            "本次额外核对事件与章末状态：上述 required_change 指必须发生但尚未完成的变化。"
            "仅 transient_eligible_keys 中的关系，若正文明确完成取得/授权，又明确在本章"
            "消耗、归还或终止，且章末不再成立，返回 completed_transient_change。"
            "evidence_quote 仍复制原候选的取得证据，ending_evidence_quote 必须引用随后"
            "明确结束同一具体物件关系的唯一连续正文，不能只引用暂时放下或人物猜测。"
            "必须核对人物、物件、关系和先后；仍持续、缺证或存在其他可能则 required_change"
            "或 unresolved。不得把消耗品的取得经过登记为章末仍持有，不得补写事实。"
            "其他 disposition 的 ending_evidence_quote 留空。"
        )
    from src.core.agents.structured_handoff import handoff_user_text
    user_text = handoff_user_text(resolved_client, stage="chapter_card", data={"context": payload},
        original_task=append_role_marker(json.dumps(payload, ensure_ascii=False, sort_keys=True), role="chapter_card_generator"))
    try:
        response = resolved_client.complete(model=resolved_model, **kwargs,
            messages=[Message(role="system", content=system), Message(role="user", content=user_text)])
    except LocalJsonResponseViolation as exc:
        response = exc.response  # 响应已知但格式有错也保留，不能再付费重提摘要。
    record.update(status="responded", raw_response=response.content, model_used=response.model_used)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    temporary.replace(path)
    return validated_registration_keys(payload=payload, record=record)
