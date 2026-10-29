"""同一份资料的文字/JSON 实验视图；不改变输出合同、裁决或调用次数。"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from src.core.llm.base import Message

HandoffFormat = Literal["text", "json"]
HANDOFF_AGENTS = frozenset({
    "outline_builder", "mini_plan_generator", "character_backfill",
    "mechanism_consistency_validator", "outline_consistency_validator",
    "setting_consistency_validator", "beat_alignment_validator",
    "thread_closure_validator", "identity_coverage_validator",
    "chapter_card_generator", "must_vs_forbidden_validator", "writer",
})


class HandoffError(ValueError):
    """资料或格式接线不完整；必须在付费请求之前停止。"""


class InstructionPart(BaseModel):
    """原任务文字或同份 data 中一段原文的指针，恰好有一项。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str | None = None
    data_path: str | None = None


class HandoffPacket(BaseModel):
    """可审计的单阶段输入，字段含义与强弱级别沿用现有合同。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["json-handoff-v1"] = "json-handoff-v1"
    stage: str
    data: dict[str, JsonValue]
    instructions: list[InstructionPart]
    retry_feedback: list[str] = Field(default_factory=list, exclude_if=lambda value: not value)

    def original_task(self) -> str:
        parts: list[str] = []
        for part in self.instructions:
            if (part.text is None) == (part.data_path is None):
                raise HandoffError("任务片段必须且只能有 text 或 data_path")
            if part.text is not None:
                parts.append(part.text)
                continue
            value: Any = self.data
            assert part.data_path is not None
            if not part.data_path.startswith("/data/"):
                raise HandoffError("任务资料指针越界")
            try:
                for key in part.data_path[len("/data/"):].split("/"):
                    key = key.replace("~1", "/").replace("~0", "~")
                    value = value[int(key)] if isinstance(value, list) else value[key]
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise HandoffError("任务资料指针不存在") from exc
            if not isinstance(value, str):
                raise HandoffError("任务资料指针必须指向原文字符串")
            parts.append(value)
        return "".join(parts)

    def semantic_hash(self) -> str:
        return hashlib.sha256(json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return _json_value(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise HandoffError("资料对象的键必须是字符串")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_value(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise HandoffError(f"资料含不可序列化类型：{type(value).__name__}")


def _leaves(value: Any, path: str = "/data"):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(item, path + "/" + key.replace("~", "~0").replace("/", "~1"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _leaves(item, f"{path}/{index}")
    else:
        yield path, value


def _canonical_user_request(material: dict[str, Any]) -> dict[str, Any]:
    """只归并完全一致的用户原文；历史、事实与模型设计不做语义裁剪。"""
    from src.core.models.narrative import UserNarrativeRequest

    requests: list[UserNarrativeRequest] = []
    references: list[dict[str, str]] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            if (value.get("version") in (2, 3, 4, 5, 6, 7) and "reader_interest" in value
                    and "narrative_tone" in value and not value.get("user_request")):
                raise HandoffError("故事定位 v2/v3/v4 的用户原始要求在交接中丢失")
            if (value.get("version") in (3, 4, 5, 6, 7) and "reader_interest" in value
                    and "narrative_tone" in value and not value.get("progression_design")):
                raise HandoffError("故事定位 v3/v4 的推进设计在交接中丢失")
            if value.get("version") in (4, 5, 6, 7) and "reader_interest" in value:
                from src.core.models.narrative import PlanningScope
                try:
                    PlanningScope.model_validate(value.get("planning_scope"))
                    if value.get("version") in (5, 6, 7):
                        from src.core.models.narrative import ScopeExperienceDesign
                        ScopeExperienceDesign.model_validate(value.get("scope_experience"))
                    source = value.get("user_request")
                    if isinstance(source, dict) and "data_path" in source:
                        if source != {"data_path": "/data/user_request"}:
                            raise ValueError("用户原文引用来源不符")
                        source = material.get("user_request")
                    request = UserNarrativeRequest.model_validate(source)
                    if (value.get("version") in (6, 7)) != (request.version == 2):
                        raise ValueError("整体阅读偏好缺失或版本语义被静默改变")
                    if value.get("version") == 7:
                        from src.core.models.authoring_contract import AuthoringContract
                        AuthoringContract.model_validate(value.get("authoring_plan"))
                    if not isinstance(value.get("scope_outcome"), str) or not value["scope_outcome"].strip():
                        raise ValueError("本段成果缺失")
                except ValueError as exc:
                    raise HandoffError("故事定位 v4 的规划范围或阶段资料在交接中丢失") from exc
            for key, item in value.items():
                if key == "user_request":
                    if item == {"data_path": "/data/user_request"}:
                        references.append(item)
                        continue
                    try:
                        requests.append(UserNarrativeRequest.model_validate(item))
                    except ValueError as exc:
                        raise HandoffError("用户原始要求来源损坏") from exc
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(material)
    if not requests:
        if references:
            raise HandoffError("用户原始要求引用没有可验证原文")
        return material
    first = requests[0]
    if any(item != first for item in requests[1:]):
        raise HandoffError("同一次请求出现互相不同的用户原始要求")

    def canonical(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: ({"data_path": "/data/user_request"} if key == "user_request"
                          else canonical(item)) for key, item in value.items()}
        if isinstance(value, list):
            return [canonical(item) for item in value]
        if isinstance(value, str) and value == first.premise:
            return {"data_path": "/data/user_request/premise"}
        return value

    result = canonical(material)
    result["user_request"] = first.model_dump(mode="json")
    return result


def build_packet(*, stage: str, data: dict[str, Any], original_task: str) -> HandoffPacket:
    """从源对象取资料；任务说明只将重复的长原文替换为可还原引用，不概括。"""
    material = _json_value(data)
    # JSON round-trip 同时断开调用方可变对象；拒绝 NaN/Infinity 等伪 JSON。
    material = _canonical_user_request(json.loads(json.dumps(material, ensure_ascii=False, allow_nan=False)))
    references: dict[str, str] = {}
    for path, value in _leaves(material):
        if (
            isinstance(value, str) and value and value in original_task
            and (len(value) >= 40 or path == "/data/user_request/reading_preferences")
        ):
            references.setdefault(value, path)
    parts: list[InstructionPart] = []
    cursor = 0
    if references:
        pattern = re.compile("|".join(re.escape(s) for s in sorted(references, key=len, reverse=True)))
        for match in pattern.finditer(original_task):
            if match.start() > cursor:
                parts.append(InstructionPart(text=original_task[cursor:match.start()]))
            parts.append(InstructionPart(data_path=references[match.group()]))
            cursor = match.end()
    if cursor < len(original_task) or not parts:
        parts.append(InstructionPart(text=original_task[cursor:]))
    packet = HandoffPacket(stage=stage, data=material, instructions=parts)
    if packet.original_task() != original_task:
        raise HandoffError("资料转换改动了原任务")
    return packet


def render_packet(packet: HandoffPacket, mode: HandoffFormat) -> str:
    packet.original_task()  # 先确认所有引用有效。
    if mode == "json":
        return json.dumps(packet.model_dump(mode="json", exclude_none=True), ensure_ascii=False, indent=2)
    if mode != "text":
        raise HandoffError("未知交接格式")
    lines = [f"## 阶段\n{packet.stage}", "## 只读资料 data"]

    def render(value: Any, path: str) -> None:
        if isinstance(value, dict):
            lines.append(f"### {path}（对象，{len(value)} 项）")
            for key, item in value.items():
                render(item, path + "/" + key.replace("~", "~0").replace("/", "~1"))
        elif isinstance(value, list):
            lines.append(f"### {path}（数组，{len(value)} 项）")
            for index, item in enumerate(value):
                render(item, f"{path}/{index}")
        else:
            lines.append(f"### {path}\n" + (value if isinstance(value, str) else json.dumps(value)))

    render(packet.data, "/data")
    lines.append("## 任务说明 instructions（按顺序连接文字与资料原文）")
    for part in packet.instructions:
        lines.append(part.text if part.text is not None else f"（此处引用资料原文：{part.data_path}）")
    lines.extend(packet.retry_feedback)
    return "\n\n".join(lines)


def append_packet_feedback(user_text: str, feedback: str) -> str | None:
    """既有单 user 重试不允许在 JSON 尾部拼接文字；原文和反馈次数不变。"""
    try:
        value = json.loads(user_text)
    except ValueError:
        return None
    if not isinstance(value, dict) or value.get("version") != "json-handoff-v1":
        return None
    packet = HandoffPacket.model_validate(value)
    packet.original_task()
    return render_packet(packet.model_copy(update={
        "retry_feedback": [*packet.retry_feedback, feedback],
    }), "json")


def handoff_user_text(client: Any, *, stage: str, data: dict[str, Any], original_task: str) -> str:
    mode = getattr(client, "handoff_format", None)
    if mode is None:
        material = _canonical_user_request(_json_value(data))
        if any(path.endswith("/planning_scope/mode") for path, _ in _leaves(material)):
            raise HandoffError("新规划范围未接入完整资料交接；请为新活动配置 handoff_format=json")
        request = material.get("user_request")
        if request is not None:
            source = request["premise"]
            if source not in original_task:
                return original_task + "\n\n用户原始前提与阅读要求（优先于模型设计）：\n" + source
        return original_task
    if mode not in {"text", "json"}:
        raise HandoffError("未知交接格式")
    from src.core.agents.creative_delta import uses_experimental_pipeline
    if not uses_experimental_pipeline(client):
        raise HandoffError("结构化交接只允许显式实验管线")
    return render_packet(build_packet(stage=stage, data=data, original_task=original_task), mode)


def guard_json_messages(messages: list[Message], *, agent: str, mode: str | None) -> list[Message]:
    """JSON 实验的初次输入不能退回文字；后续原有重试反馈保持原文装入 JSON。"""
    if mode != "json" or agent not in HANDOFF_AGENTS:
        return messages
    seen_initial = False
    result: list[Message] = []
    for message in messages:
        if message.role != "user":
            result.append(message)
            continue
        if not seen_initial:
            try:
                packet = HandoffPacket.model_validate_json(message.content)
                packet.original_task()
            except ValueError as exc:
                raise HandoffError(f"{agent} 初次请求没有接通 JSON 资料包") from exc
            seen_initial = True
            result.append(message)
        else:
            result.append(message.model_copy(update={"content": json.dumps({
                "stage": "retry_feedback", "feedback": message.content,
            }, ensure_ascii=False)}))
    if not seen_initial:
        raise HandoffError("结构化交接缺少 user 资料")
    return result


def prepare_handoff_messages(messages: list[Message], *, agent: str, mode: str | None) -> list[Message]:
    """两组使用完全相同的资料阅读说明，JSON 组额外验形；不修改输出模式。"""
    messages = guard_json_messages(messages, agent=agent, mode=mode)
    if mode is None or agent not in HANDOFF_AGENTS:
        return messages
    note = (
        "\n\n输入资料包阅读说明：data 是按字段组织的只读资料，不是新增剧情要求。"
        "instructions 按顺序保留原任务；其中 text 是任务文字，data_path 指向 data 中"
        "对应的原文，阅读时将原文接回该位置。路径使用 JSON Pointer（~1 表示 /，~0 表示 ~）。"
        "约束优先级与适用阶段沿用原任务，不将普通背景或文学建议升级为 HARD。"
        "版本、路径和内部编号仅用于定位，不得写进小说。输入格式不改变输出要求："
        "创作规划仍按原输出 schema 返回，Writer 仍按当前写作/修订任务输出。"
    )
    has_user_source = False
    for message in messages:
        if message.role != "user":
            continue
        if mode == "json":
            has_user_source = HandoffPacket.model_validate_json(message.content).data.get("user_request") is not None
        else:
            has_user_source = bool(re.search(r"(?m)^### /data/user_request（对象，", message.content))
        break
    if has_user_source:
        note += (
            "data.user_request 是程序保存的用户原始前提与阅读要求，优先于模板默认与模型设计；"
            "资料中的 data_path 对象引用该原文，不表示资料缺失。文学偏好不变成逐条证据任务。"
        )
    return [message.model_copy(update={"content": message.content + note})
            if message.role == "system" else message for message in messages]
