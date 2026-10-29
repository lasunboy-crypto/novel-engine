"""P1 离线审核分包：来源导航与可检查输入，不执行 LLM 或产品裁决。"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from src.core.llm.prompt_audit import estimate_local_tokens

VERSION = "outline-review-packets-v1"
ID_FIELDS = frozenset(
    {
        "character_id",
        "phase_id",
        "turning_id",
        "thread_id",
        "object_id",
        "volume_id",
        "arc_id",
        "beat_id",
    }
)
DEFINITION_RECORDS = {
    r"/characters/characters/\d+": "character_id",
    r"/plot/main_phases/\d+": "phase_id",
    r"/plot/major_turning_points/\d+": "turning_id",
    r"/plot/long_threads/\d+": "thread_id",
    r"/plot/load_bearing_objects/\d+": "object_id",
    r"/volume_plans/volumes/\d+": "volume_id",
    r"/volume_plans/volumes/\d+/arcs/\d+": "arc_id",
    r"/volume_plans/volumes/\d+/arcs/\d+/beats/\d+": "beat_id",
}

ATOMIC_IDS = ID_FIELDS - {"character_id", "volume_id", "arc_id"}
HISTORY_FIELDS = frozenset(
    {
        "background",
        "motivation",
        "relationships_brief",
        "identity_facets",
        "initial_core_facts",
        "role",
        "archetype",
        "name",
        "character_id",
        "aliases",
    }
)
CATEGORIES = {
    "history": "核对人物身份、开篇前史与后续首次事件；区分发生时间、回忆及揭晓时间。",
    "mechanism": "核对规则与相关用例的对象、条件、效果和代价；普通规则不升级为绝对规则。",
    "seam": "核对相邻故事段的动作状态与时间交接；计划和禁令不等于实际完成。",
    "residual": "检查未归类资料中的跨段关联；发现缺少对照来源时请求补资料。",
    "global": "检查完整主线骨架、跨组遗漏和审核责任覆盖；局部通过不能推出全书通过。",
}
TASK_TEMPLATE = """离线拆分审核候选任务 v1（尚未接入产品）。
沿用下列原终审的证据尺度，只判断 task_scope 指定的关系。
source 是完整字段原文，路径与编号仅定位；global_frame_refs 指向共同前提。
同一数字/动作或相似字句不能证明同一事件；必须保留年份、条件、首次/再次及说话者。
区分有证据的互斥、无该类冲突、可兼容留白、材料不足。不得用缺说明代替矛盾证据。
材料不足可提交需要的来源路径或实体编号，不将未决当作通过或剧情错误。
所有引用必须绑定路径和逐字原文；没有事件发生时间时保持未知，不从章节顺序推断前史。
source_snapshot 绑定版本；仅在本次范围内判断。revision_scope 为空，不改写故事。
输出合同与补资料调度须在 P2 正式入口实现后冻结，本导出不是可直接外发的产品请求。
"""


class PacketError(ValueError):
    """来源、版本或选择非法，离线导出失败。"""


def canonical(value: Any) -> str:
    """稳定 JSON，用于版本和完整性；不转换原文字串。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def source_id(pointer: str) -> str:
    """短引用只定位字段；全长路径与快照 hash 仍保留。"""
    return "s_" + hashlib.sha256(pointer.encode()).hexdigest()[:12]


def _escape(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def leaves(value: Any, path: str = "") -> dict[str, Any]:
    """空容器也入册，区分缺失、空值、空串；采用标准 JSON Pointer。"""
    result: dict[str, Any] = {}
    if isinstance(value, dict) and value:
        for key, item in value.items():
            if not isinstance(key, str):
                raise PacketError("对象键必须为字符串")
            result.update(leaves(item, path + "/" + _escape(key)))
    elif isinstance(value, list) and value:
        for i, item in enumerate(value):
            result.update(leaves(item, f"{path}/{i}"))
    else:
        result[path] = value
    return result


def resolve(value: Any, pointer: str) -> Any:
    """只接受已有位置；禁止负索引、前导零和越界。"""
    if pointer == "":
        return value
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise PacketError("非法 JSON Pointer")
    try:
        for part in pointer[1:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not re.fullmatch(r"0|[1-9]\d*", part):
                    raise PacketError("非法数组位置")
                value = value[int(part)]
            else:
                value = value[part]
    except (KeyError, IndexError, TypeError) as exc:
        raise PacketError(f"来源不存在：{pointer}") from exc
    return value


def _units(value: Any, path: str = "") -> dict[str, Any]:
    # beat/turn/thread 等记录不切句；人物字段及关系字典也保持完整。
    if isinstance(value, dict) and any(key in value for key in ATOMIC_IDS):
        return {path: value}
    if re.fullmatch(r"/characters/characters/\d+/[^/]+", path):
        return {path: value}
    if path.startswith("/world/") or re.fullmatch(r"/plot/character_arcs/[^/]+", path):
        return {path: value}
    if isinstance(value, dict) and value:
        return {
            p: v
            for key, item in value.items()
            for p, v in _units(item, path + "/" + _escape(key)).items()
        }
    if (
        isinstance(value, list)
        and value
        and all(not isinstance(v, (dict, list)) for v in value)
    ):
        return {path: value}
    if isinstance(value, list) and value:
        return {
            p: v
            for i, item in enumerate(value)
            for p, v in _units(item, f"{path}/{i}").items()
        }
    return {path: value}


def _records(value: Any, path: str = "") -> list[dict[str, Any]]:
    records = []
    if isinstance(value, dict):
        for field in sorted(ID_FIELDS & value.keys()):
            if not any(
                field == expected and re.fullmatch(pattern, path)
                for pattern, expected in DEFINITION_RECORDS.items()
            ):
                continue
            identifier = value[field]
            if isinstance(identifier, str) and identifier:
                aliases = value.get("aliases", [])
                terms = {identifier}
                terms.update(
                    v
                    for k in ("name", "label", "title")
                    if isinstance(v := value.get(k), str) and v
                )
                if isinstance(aliases, list):
                    terms.update(v for v in aliases if isinstance(v, str) and v)
                records.append(
                    {
                        "id": identifier,
                        "kind": field,
                        "path": path,
                        "terms": sorted(terms),
                    }
                )
        for key, item in value.items():
            records.extend(_records(item, path + "/" + _escape(key)))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            records.extend(_records(item, f"{path}/{i}"))
    return records


def _mentions(text: str, term: str) -> bool:
    if re.fullmatch(r"[\w-]+", term, flags=re.ASCII):
        return bool(re.search(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])", text))
    return len(term) >= 2 and term in text


@dataclass(frozen=True)
class SourceIndex:
    """断开调用方引用的源快照；下游每次渲染重新验 hash 防止可变字段被改。"""

    data: dict[str, Any]
    source_hash: str
    units: dict[str, Any]
    records: list[dict[str, Any]]
    candidates: dict[str, list[str]]
    diagnostics: dict[str, Any]
    navigation_hash: str

    def verify(self) -> None:
        if digest(self.data) != self.source_hash or self.units != _units(self.data):
            raise PacketError("来源或索引已改变，请从新版本重新分包")
        if digest([self.candidates, self.diagnostics]) != self.navigation_hash:
            raise PacketError("候选依赖索引已改变")
        if self.records != _records(self.data):
            raise PacketError("实体索引已改变")

    def manifest(self) -> dict[str, Any]:
        self.verify()
        return {
            "version": VERSION,
            "source_hash": self.source_hash,
            "leaves": {
                p: {"sha256": digest(v), "value": deepcopy(v)}
                for p, v in leaves(self.data).items()
            },
            "units": {
                p: {
                    "source_id": source_id(p),
                    "sha256": digest(v),
                    "leaf_paths": list(leaves(v, p)),
                    "candidate_entities": self.candidates[p],
                }
                for p, v in self.units.items()
            },
            "entities": deepcopy(self.records),
            "diagnostics": deepcopy(self.diagnostics),
            "semantic_relationship_coverage_proven": False,
        }


def build_index(data: dict[str, Any]) -> SourceIndex:
    """建立候选导航；不归并事件，不给自由文字增加事实/时间标签。"""
    leaves(data)  # 在 JSON 转换前拒绝非字符串键。
    source = json.loads(canonical(data))
    if (
        not isinstance(source, dict)
        or not {"world", "characters", "plot", "volume_plans"} <= source.keys()
    ):
        raise PacketError("需要完整四段大纲 data")
    units = _units(source)
    records = _records(source)
    by_id: dict[str, list[str]] = defaultdict(list)
    by_term: dict[str, set[str]] = defaultdict(set)
    for record in records:
        by_id[record["id"]].append(record["path"])
        for term in record["terms"]:
            by_term[term].add(record["id"])
    candidates = {}
    unresolved = []
    for path, value in units.items():
        text = path + " " + canonical(value)
        found = set()
        for record in records:
            if (
                path == record["path"]
                or path.startswith(record["path"] + "/")
                or any(_mentions(text, term) for term in record["terms"])
            ):
                found.add(record["id"])
        candidates[path] = sorted(found)
        for ptr, item in leaves(value, path).items():
            for token in re.findall(
                r"(?<![\w-])[a-z][a-z0-9_]*-\d+(?:-\d+)*(?![\w-])",
                ptr + " " + str(item),
            ):
                if token not in by_id:
                    unresolved.append({"path": ptr, "candidate_id": token})
    # 仅记录直接依赖；不递归展开，互引不会导致无限循环或全书洪泛。
    graph: dict[str, set[str]] = {identifier: set() for identifier in by_id}
    for record in records:
        for path, refs in candidates.items():
            if path == record["path"] or path.startswith(record["path"] + "/"):
                graph[record["id"]].update(set(refs) - {record["id"]})
    mutual = sorted(
        {tuple(sorted((a, b))) for a, bs in graph.items() for b in bs if a in graph[b]}
    )
    unidentified = set()
    for pattern, field in DEFINITION_RECORDS.items():
        for leaf in leaves(source):
            match = re.match("(" + pattern + r")(?=/|$)", leaf)
            if match:
                item = resolve(source, match[1])
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get(field), str)
                    or not item[field]
                ):
                    unidentified.add(match[1])
    definition_fields = {r["path"] + "/" + r["kind"] for r in records}
    references: dict[str, list[str]] = defaultdict(list)
    for pointer, value in leaves(source).items():
        if (
            isinstance(value, str)
            and value in by_id
            and pointer not in definition_fields
        ):
            references[value].append(pointer)
    diagnostics = {
        "known_id_reference_occurrences": dict(references),
        "unidentified_records": sorted(unidentified),
        "duplicate_ids": {k: v for k, v in by_id.items() if len(v) > 1},
        "ambiguous_terms": {k: sorted(v) for k, v in by_term.items() if len(v) > 1},
        "unresolved_id_candidates": unresolved,
        "mutual_candidate_pairs_including_containment": mutual,
        "dependency_expansion": "direct_only_no_recursive_closure",
        "semantic_event_identity": "unknown_not_inferred",
        "unknown_unregistered_aliases": "not_detectable_by_exact_lookup; residual_and_global_review_required",
    }
    return SourceIndex(
        source,
        digest(source),
        units,
        records,
        candidates,
        diagnostics,
        digest([candidates, diagnostics]),
    )


@dataclass(frozen=True)
class ReviewPacket:
    packet_id: str
    category: str
    source_hash: str
    scope_entities: tuple[str, ...]
    primary_refs: tuple[str, ...]
    context_refs: tuple[str, ...]
    global_frame_refs: tuple[str, ...]
    # 依赖是检索候选，不足以证明只改其他字段时可安全缓存。
    dependency_completeness: str = "unknown"

    def payload(self, index: SourceIndex) -> dict[str, Any]:
        index.verify()
        if self.source_hash != index.source_hash:
            raise PacketError("任务包不是当前来源版本")
        refs = sorted(
            set(self.primary_refs + self.context_refs + self.global_frame_refs)
        )
        if any(ref not in index.units for ref in refs):
            raise PacketError("任务包引用不存在的来源单元")
        if len({source_id(p) for p in refs}) != len(refs):
            raise PacketError("短来源引用发生碰撞")
        return {
            "version": VERSION,
            "source_snapshot": self.source_hash,
            "packet_id": self.packet_id,
            "task_scope": {
                "category": self.category,
                "instruction": CATEGORIES[self.category],
                "entities": self.scope_entities,
                "primary_refs": [source_id(p) for p in self.primary_refs],
            },
            "global_frame_refs": [source_id(p) for p in self.global_frame_refs],
            "boundary_context_refs": [source_id(p) for p in self.context_refs],
            "source": {
                source_id(ref): {"path": ref, "value": deepcopy(index.units[ref])}
                for ref in refs
            },
            "dependency_refs": {
                "candidate_entities": sorted(
                    {entity for ref in refs for entity in index.candidates[ref]}
                ),
                "completeness": self.dependency_completeness,
                "full_index_available_offline": True,
            },
            "coverage_manifest": {
                "full_book_verdict": False,
                "relationship_coverage": "unproven",
                "can_request_more_source": True,
            },
            "revision_scope": [],
        }


def _history(path: str) -> bool:
    match = re.fullmatch(r"/characters/characters/\d+/([^/]+)", path)
    return bool(match and match[1] in HISTORY_FIELDS)


def _frame(index: SourceIndex) -> set[str]:
    return {
        p
        for p in index.units
        if p
        in {
            "/premise",
            "/genre",
            "/plot/core_conflict",
            "/world/setting",
            "/world/unique_rules",
            "/world/power_system",
        }
        or re.fullmatch(r"/characters/characters/\d+/(name|role|character_id)", p)
    }


def build_packets(
    index: SourceIndex, *, max_source_chars: int = 18000
) -> list[ReviewPacket]:
    """按结构及共同实体选包；从未读取人工冲突标签。超大关联包保留并标记。"""
    index.verify()
    if max_source_chars <= 0:
        raise PacketError("包规模预算必须为正")
    frame = _frame(index)
    packets: list[ReviewPacket] = []

    def add(
        category: str,
        primary: set[str],
        context: set[str] | None = None,
        entities: tuple[str, ...] = (),
    ) -> None:
        if primary:
            packets.append(
                ReviewPacket(
                    f"{category}-{1 + sum(p.category == category for p in packets):03d}",
                    category,
                    index.source_hash,
                    entities,
                    tuple(sorted(primary)),
                    tuple(sorted(context or set())),
                    tuple(sorted(frame)),
                )
            )

    arcs_by_volume: dict[str, list[str]] = defaultdict(list)
    for record in index.records:
        if record["kind"] == "arc_id":
            arcs_by_volume[record["path"].rsplit("/arcs/", 1)[0]].append(record["path"])
    history = {
        p
        for p in index.units
        if _history(p)
        and p.rsplit("/", 1)[-1]
        in {
            "background",
            "relationships_brief",
            "initial_core_facts",
            "identity_facets",
        }
    }
    turning_context = {
        p
        for p in index.units
        if p.startswith(
            (
                "/plot/main_phases/",
                "/plot/major_turning_points/",
                "/plot/character_arcs/",
            )
        )
    }
    # 以已有故事段限定未来动作；共同前史保留全体人物，避免人名关联洪泛。
    # 某段包含何种事件只来自源结构，完全不读取预先标好的冲突位置。
    for paths in arcs_by_volume.values():
        beats = [
            r["path"]
            for r in index.records
            if r["kind"] == "beat_id"
            and any(r["path"].startswith(p + "/") for p in paths)
        ]

        def add_history(path: str, selected: list[str]) -> None:
            metadata = {
                p
                for p in index.units
                if p.startswith(path + "/") and "/beats/" not in p
            }
            primary = metadata | set(selected)
            boundary = set()
            if selected:
                lo, hi = beats.index(selected[0]), beats.index(selected[-1])
                boundary.update(beats[max(0, lo - 1) : lo])
                boundary.update(beats[hi + 1 : hi + 2])
            context = history | turning_context | boundary
            size = sum(
                len(canonical(index.units[p])) for p in primary | context | frame
            )
            if size > max_source_chars and len(selected) > 1:
                midpoint = len(selected) // 2
                add_history(path, selected[:midpoint])
                add_history(path, selected[midpoint:])
            else:
                # 单一不可再分事件仍超大时保留全部，measure 明确报告，不切原文。
                add(
                    "history",
                    primary,
                    context,
                    tuple(index.units[p]["beat_id"] for p in selected),
                )

        for path in paths:
            add_history(path, [p for p in beats if p.startswith(path + "/")])
    if not arcs_by_volume:
        add("history", history | turning_context)
    # 世界规则的用途可能藏在人物或长线字段，不能只检查 beat 中的显式物件。
    world_refs = {p for p in index.units if p.startswith("/world/")}
    for section in ("characters", "plot", "volume_plans"):
        uses = {p for p in index.units if p.startswith("/" + section + "/")}
        add("mechanism", world_refs | uses, entities=(section,))
    semantic_ids = {
        r["id"]
        for r in index.records
        if r["kind"]
        in {
            "phase_id",
            "turning_id",
            "thread_id",
            "object_id",
        }
    }
    all_arcs = [path for paths in arcs_by_volume.values() for path in paths]
    for left, right in zip(all_arcs, all_arcs[1:]):
        primary = {p for p in index.units if p.startswith((left + "/", right + "/"))}
        related = {ref for p in primary for ref in index.candidates[p]} & semantic_ids
        context = {
            p
            for p in index.units
            if p.startswith("/plot/") and related & set(index.candidates[p])
        }
        context.update(
            p
            for p in index.units
            if p
            in {
                left.rsplit("/arcs/", 1)[0] + "/summary",
                right.rsplit("/arcs/", 1)[0] + "/summary",
            }
        )
        add("seam", primary, context)
    global_refs = {
        p
        for p in index.units
        if p.startswith(
            (
                "/plot/core_conflict",
                "/plot/main_phases/",
                "/plot/major_turning_points/",
                "/plot/long_threads/",
                "/plot/character_arcs/",
            )
        )
        or (p.startswith("/volume_plans/") and "/beats/" not in p)
    }
    add("global", global_refs)
    covered = frame | {
        p for packet in packets for p in packet.primary_refs + packet.context_refs
    }
    pending: set[str] = set()
    size = 0
    residual_scope = (set(index.units) - covered) | {
        p for p in index.units if p.startswith("/characters/") and not _history(p)
    }
    for path in sorted(residual_scope):
        cost = len(canonical(index.units[path]))
        if pending and size + cost > max_source_chars:
            add("residual", pending)
            pending, size = set(), 0
        pending.add(path)
        size += cost
    add("residual", pending)
    return packets


def render_task(
    index: SourceIndex,
    packet: ReviewPacket,
    packets: list[ReviewPacket],
    task_context: str = "",
) -> dict[str, Any]:
    """为全局包带上分工目录；局部结果未运行，不伪装成汇总裁决。"""
    payload = packet.payload(index)
    payload["shared_field_definitions"] = task_context
    if packet.category == "global":
        payload["coverage_manifest"]["task_assignments"] = [
            {
                "packet_id": p.packet_id,
                "category": p.category,
                "scope_entities": p.scope_entities,
                "primary_refs": [source_id(ref) for ref in p.primary_refs],
            }
            for p in packets
            if p.packet_id != packet.packet_id
        ]
        payload["coverage_manifest"]["prior_results_status"] = (
            "not_run; P2 must supply validated local results"
        )
        payload["coverage_manifest"]["complete_source_directory"] = [
            {"source_id": source_id(p), "path": p} for p in index.units
        ]
    return payload


def request_sources(
    index: SourceIndex,
    packet: ReviewPacket,
    *,
    source_hash: str,
    paths: tuple[str, ...] = (),
    entity_ids: tuple[str, ...] = (),
) -> ReviewPacket:
    """离线演练补原文；版本/引用不对即拒绝，不会调用模型或标通过。"""
    packet.payload(index)
    if source_hash != index.source_hash:
        raise PacketError("补资料请求来源版本已过期")
    refs = set(packet.context_refs)
    for pointer in paths:
        resolve(index.data, pointer)
        matches = {
            p
            for p in index.units
            if p == pointer
            or p.startswith(pointer + "/")
            or pointer.startswith(p + "/")
        }
        if not matches:
            raise PacketError("没有相应来源单元")
        refs.update(matches)
    for identifier in entity_ids:
        if identifier not in {r["id"] for r in index.records}:
            raise PacketError(f"未知实体：{identifier}")
        refs.update(p for p, ids in index.candidates.items() if identifier in ids)
    return ReviewPacket(
        packet.packet_id,
        packet.category,
        packet.source_hash,
        packet.scope_entities,
        packet.primary_refs,
        tuple(sorted(refs)),
        packet.global_frame_refs,
    )


def validate_quote(
    index: SourceIndex, packet: ReviewPacket, *, path: str, quote: str
) -> None:
    """核对包内原文引用；有效引用仍不等于理由成立。"""
    payload = packet.payload(index)
    if not any(
        path == item["path"] or path.startswith(item["path"] + "/")
        for item in payload["source"].values()
    ):
        raise PacketError("引用资料不在当前包内，请先补取")
    value = resolve(index.data, path)
    if not isinstance(value, str) or not quote.strip() or quote not in value:
        raise PacketError("引用不是该字段的逐字原文")


def invalidated_packets(
    old: SourceIndex, new: SourceIndex, packets: list[ReviewPacket]
) -> list[str]:
    """依赖完备性未知时，任何原材料变化都保守失效；不伪称局部缓存已安全。"""
    old.verify()
    new.verify()
    for packet in packets:
        packet.payload(old)
    return [p.packet_id for p in packets] if old.source_hash != new.source_hash else []


def measure(
    index: SourceIndex,
    packets: list[ReviewPacket],
    *,
    original_messages: list[dict[str, str]],
    max_source_chars: int = 18000,
    task_context: str = "",
) -> dict[str, Any]:
    """用相同本地计量器比较；启发式 token 非供应商计费。"""
    index.verify()
    systems = [m["content"] for m in original_messages if m["role"] == "system"]
    system_chars = sum(len(text) for text in systems)
    system_tokens = sum(estimate_local_tokens(text) for text in systems)
    baseline_chars = sum(len(m["content"]) for m in original_messages)
    baseline_tokens = sum(
        estimate_local_tokens(m["content"]) for m in original_messages
    )
    counters: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    for packet in packets:
        payload = render_task(index, packet, packets, task_context)
        source = payload["source"]
        counters.update(item["path"] for item in source.values())
        text = canonical(payload)
        source_chars = sum(len(canonical(v["value"])) for v in source.values())
        row = {
            "packet_id": packet.packet_id,
            "category": packet.category,
            "source_chars": source_chars,
            "source_units": len(source),
            "render_chars": system_chars + len(TASK_TEMPLATE) + len(text),
            "local_input_tokens": system_tokens
            + estimate_local_tokens(TASK_TEMPLATE)
            + estimate_local_tokens(text),
            "source_budget_exceeded": source_chars > max_source_chars,
            "render_ratio_to_original": (system_chars + len(TASK_TEMPLATE) + len(text))
            / max(1, baseline_chars),
        }
        rows.append(row)
    all_leaves = set(leaves(index.data))
    covered_leaves = {
        leaf for path in counters for leaf in leaves(index.units[path], path)
    }
    total = sum(row["local_input_tokens"] for row in rows)
    return {
        "version": VERSION,
        "source_hash": index.source_hash,
        "provider_calls": 0,
        "status": "offline_prototype_not_product_ready",
        "semantic_verdict": "not_run",
        "meter": "existing estimate_local_tokens: CJK=1, other nonspace/4; NOT provider usage",
        "baseline": {
            "render_chars": baseline_chars,
            "local_input_tokens": baseline_tokens,
            "data_chars": len(canonical(index.data)),
            "unique_source_chars": sum(len(canonical(v)) for v in index.units.values()),
            "source_units": len(index.units),
            "source_leaves": len(all_leaves),
        },
        "packets": rows,
        "initial_review_calls_estimate": len(rows),
        "total_local_input_tokens": total,
        "total_input_ratio": total / max(1, baseline_tokens),
        "duplicated_source_chars": sum(
            (count - 1) * len(canonical(index.units[path]))
            for path, count in counters.items()
        ),
        "uncovered_leaf_paths": sorted(all_leaves - covered_leaves),
        "field_coverage_complete": all_leaves == covered_leaves,
        "relationship_coverage_proven": False,
        "oversized_packets": [
            row["packet_id"] for row in rows if row["source_budget_exceeded"]
        ],
        "additional_calls": "context refill/global follow-up/retry not executed; must budget separately",
        "cost": "input estimate only; output/thinking unknown; no claim of cost savings",
        "diagnostics": index.diagnostics,
    }
