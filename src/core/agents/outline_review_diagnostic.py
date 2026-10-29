"""P2 只读诊断合同：来源绑定、线上 JSON 消息与局部结论校验。"""
from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.agents.outline_review_packets import (
    PacketError, ReviewPacket, SourceIndex, build_index, build_packets,
    canonical, digest, render_task, request_sources, resolve, validate_quote,
)
from src.core.agents.structured_handoff import HandoffPacket, build_packet, render_packet
from src.core.llm.base import Message
from src.services.run_model_stage_registry import conservative_rendered_input_tokens


class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["conflict", "compatible", "unknown"]
    reason: str = Field(min_length=1, max_length=2000)
    source_quotes: list[Quote] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def two_sides(self) -> Finding:
        if self.outcome == "conflict" and len({q.path for q in self.source_quotes}) < 2:
            raise ValueError("冲突必须有两端不同原文")
        return self


class LocalReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    packet_id: str
    source_hash: str
    outcome: Literal["conflict", "compatible", "unknown"]
    findings: list[Finding] = Field(max_length=40)
    reviewed_paths: list[str] = Field(min_length=1)
    requested_paths: list[str] = Field(max_length=30)
    summary: str = Field(min_length=1, max_length=2000)


LOCAL_INSTRUCTION = """你是小说大纲编辑。本次只审核资料包 task_scope 中的范围。source 内是完整原文；source_id 是导航指针，不证明不同事件是同一件事。
按 shared_field_definitions 理解开篇档案。核对时间、事件身份、触发条件及另一端限定，再判断两端能否共存。不同年份、再次发生、相似后果不构成同一事件的证据。缺少中间经过可以留给 Writer，只要自然承接不否认既定事实、不改写明确事件、不发明绝对规则例外。不要替作者补剧情或增加 HARD。
conflict 只用于原文明示互斥，reason 必须说明同一时间/事件/条件的依据与为何无法共存；compatible 仅表示本包已检查范围可承接，不代表全书通过；必要资料不在包内或无法确定时返回 unknown，并填写 requested_paths（原始 JSON Pointer）。找不到路径则留空并解释。不得猜造引文。source_quotes 的 path 使用原始 JSON Pointer，quote 是该字符串字段中连续逐字引文。
reviewed_paths 列出实际核对的原始路径；findings 不凑数量。没有足够资料时不能用空 findings 宣布通过。只输出下述 schema 的一个 JSON 对象，不要代码块。packet_id 和 source_hash 必须照抄。
"""


def read_original(messages: list[Message]) -> tuple[SourceIndex, list[ReviewPacket], str]:
    if [m.role for m in messages] != ["system", "user"]:
        raise PacketError("只接受单次完整终审 system/user 消息")
    packet = HandoffPacket.model_validate_json(messages[1].content)
    if packet.stage != "outline_consistency_validator":
        raise PacketError("必须是大纲终审来源")
    prefix, marker, _ = packet.original_task().rpartition("\n## 四段大纲原文\n")
    _, opening, definitions = prefix.rpartition("## 开篇时间与人物既往权威原文\n")
    if not marker or not opening:
        raise PacketError("缺少共同开篇字段定义")
    index = build_index(packet.data)
    return index, build_packets(index), opening + definitions


def local_messages(index: SourceIndex, packet: ReviewPacket, packets: list[ReviewPacket],
                   definitions: str, *, prior_results: list[LocalReview] | None = None) -> list[Message]:
    data = render_task(index, packet, packets, definitions)
    data["source_hash"] = index.source_hash
    # 全局只接收经同源校验的局部结果；缺任务仍保持未决，不自动发补资料调用。
    if packet.category == "global":
        required = {p.packet_id for p in packets if p.category != "global"}
        results = prior_results or []
        received = {r.packet_id for r in results}
        if len(received) != len(results) or not received <= required:
            raise PacketError("全局结果有重复或计划外任务")
        by_id = {p.packet_id: p for p in packets}
        validated_results = [validate_local(index, by_id[result.packet_id], result) for result in results]
        data["validated_local_results"] = [r.model_dump(mode="json") for r in validated_results]
        data["missing_local_tasks"] = sorted(required - received)
        data["coverage_manifest"]["prior_results_status"] = "complete" if received == required else "incomplete"
    task = LOCAL_INSTRUCTION + canonical(LocalReview.model_json_schema())
    return [Message(role="system", content=task), Message(role="user", content=render_packet(
        build_packet(stage="outline_consistency_validator", data=data, original_task=task), "json"))]


def _quote_leaf_paths(value: Any, path: str, quote: str) -> list[str]:
    """仅在模型指向容器时，寻找唯一包含该逐字引文的原文字符串叶子。"""
    if isinstance(value, str):
        return [path] if quote in value else []
    if isinstance(value, dict):
        return [candidate for key, child in value.items()
            for candidate in _quote_leaf_paths(child, path + "/" + str(key).replace("~", "~0").replace("/", "~1"), quote)]
    if isinstance(value, list):
        return [candidate for i, child in enumerate(value)
            for candidate in _quote_leaf_paths(child, path + "/" + str(i), quote)]
    return []


def validate_local(index: SourceIndex, packet: ReviewPacket, result: LocalReview) -> LocalReview:
    packet.payload(index)
    if result.source_hash != index.source_hash or result.packet_id != packet.packet_id:
        raise PacketError("审核结果来源版本或任务不匹配")
    refs = (*packet.primary_refs, *packet.context_refs, *packet.global_frame_refs)
    for path in result.reviewed_paths:
        resolve(index.data, path)
        if not any(path == p or path.startswith(p + "/") for p in refs):
            raise PacketError("声称审核了包外资料")
    normalized = result.model_copy(deep=True)
    for finding in normalized.findings:
        for quote in finding.source_quotes:
            value = resolve(index.data, quote.path)
            if isinstance(value, (dict, list)):
                candidates = _quote_leaf_paths(value, quote.path, quote.quote)
                if len(candidates) != 1:
                    raise PacketError("容器引文无法唯一定位到原文字符串字段")
                quote.path = candidates[0]
            validate_quote(index, packet, path=quote.path, quote=quote.quote)
        if finding.outcome == "conflict" and len({q.path for q in finding.source_quotes}) < 2:
            raise PacketError("冲突两端归一后指向同一原文字段")
    for path in result.requested_paths:
        request_sources(index, packet, source_hash=result.source_hash, paths=(path,))
    outcomes = {f.outcome for f in result.findings}
    expected = "conflict" if "conflict" in outcomes else "unknown" if "unknown" in outcomes or result.requested_paths else result.outcome
    if result.outcome != expected or (result.outcome == "conflict" and "conflict" not in outcomes):
        raise PacketError("局部结论与发现/缺失资料不一致")
    if result.outcome == "compatible" and not all(
        any(path == primary or path.startswith(primary + "/") or primary.startswith(path + "/")
            for path in result.reviewed_paths) for primary in packet.primary_refs
    ):
        raise PacketError("局部通过缺少主审核范围覆盖；应报告 unknown")
    return normalized


def freeze_requests(original: list[Message], *, legacy: bool = False) -> dict:
    index, packets, definitions = read_original(original)
    selected = [p for p in packets if p.packet_id in {"history-001", "history-002"}]
    if len(selected) != 2:
        raise PacketError("本轮需要两个历史资料包；材料不适用，不能换包凑数")
    jobs: list[dict[str, Any]] = [{"job_id": "full", "messages": [m.model_dump() for m in original]}]
    for packet in selected:
        jobs.append({"job_id": packet.packet_id, "messages": [m.model_dump() for m in local_messages(index, packet, packets, definitions)]})
    for job in jobs:
        job["messages_hash"] = digest(job["messages"])
        job["input_upper_bound"] = conservative_rendered_input_tokens([Message(**m) for m in job["messages"]])
    # v1 only reconstructs historical evidence; new plans bind the full capacity
    # to their identity. Preparing an estimate never grants paid authorization.
    output_cap = 65536 if legacy else 393216
    if not legacy:
        for job in jobs:
            job["generation"] = {"output_cap": output_cap, "reasoning_effort": "max",
                                 "provider_input_guard_tokens": 655360}
    first = sum(j["input_upper_bound"] * 2 + output_cap * 8 for j in jobs) / 1_000_000
    return {"version": "outline-review-diagnostic-v1" if legacy else "outline-review-diagnostic-v2", "source_hash": index.source_hash,
            "jobs": jobs, "request_hash": digest(jobs), "first_attempt_reserve_cny": first,
            "all_attempts_reserve_cny": first * 3, "physical_request_cap": 9,
            "cost_cap_cny": 8 if legacy else math.ceil(first * 3 + .25),
            "output_cap": output_cap, "full_book_verdict": False,
            **({} if legacy else {"reasoning_effort": "max", "provider_input_guard_tokens": 655360})}
