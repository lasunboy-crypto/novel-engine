"""OutlineBuilder 生成期结构契约(v2 W3.5 D2)。

D1 已把 pre-mini-plan gate 放回正确位置;本模块把一部分可验证结构约束
再前移到 ``volume_plans`` 生成 / 卷内细化阶段:

- 首轮 ``volume_plans``:在 LLM 输出后第一时间校验卷数、骨架章数范围、
  linked_* 来源、arc/beat 覆盖。
- 卷内细化:逐卷校验 identity 保留,尤其 arc/beat ID 与 must / forbidden
  字面不被改写。

注意:这些约束只接入**新生成路径**。Pydantic 模型仍保留旧 outline 数据
的空 beats 兼容,避免把历史资产和诊断路径误伤。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Literal

from src.core.models.drama import LoadBearingObjectContract
from src.core.models.outline import (
    CharactersOutput,
    PlotOutput,
    VolumeArcPlan,
    VolumePlanItem,
    VolumePlansOutput,
)
from src.core.models.model_calibration import canonical_genre_identity

VolumePlansContractRule = Literal[
    "volume_count_mismatch",
    "chapter_count_out_of_range",
    "unknown_linked_phase",
    "unknown_linked_turning_point",
    "unknown_linked_thread",
    "dramatic_promise_missing",
    "dramatic_finale_missing",
    "dramatic_unknown_ref",
    "dramatic_identity_mismatch",
    "dramatic_object_unassigned",
    "dramatic_deferred_target_invalid",
    "dramatic_beat_missing_must",
    "beat_count_out_of_range",
    "beat_coverage_gap",
    "selection_short_arc_mismatch",
    "refine_arc_identity_changed",
    "refine_volume_semantics_changed",
    "refine_arc_summary_changed",
    "refine_beat_identity_changed",
    "refine_beat_summary_changed",
    "refine_beat_must_changed",
    "refine_beat_forbidden_changed",
    "refine_dramatic_semantics_changed",
]


@dataclass(frozen=True)
class VolumePlansContract:
    """``volume_plans`` 生成前可显式化的结构契约。"""

    total_volumes: int
    chapter_count_min: int
    chapter_count_max: int
    chapters_per_volume: int
    allowed_phase_ids: frozenset[str]
    allowed_turning_ids: frozenset[str]
    allowed_thread_ids: frozenset[str]
    allowed_character_ids: frozenset[str]
    load_bearing_objects: tuple[LoadBearingObjectContract, ...]
    require_dramatic_promise: bool
    # 仅由私有 selection runtime 传入；普通 production 永远保持 False。
    selection_short_arc: bool = False


@dataclass(frozen=True)
class VolumePlansContractFailure:
    """单条 D2 契约失败,供 retry feedback 和测试断言复用。"""

    rule: VolumePlansContractRule
    message: str
    volume_id: str | None = None
    arc_id: str | None = None
    beat_id: str | None = None
    field: str | None = None
    object_id: str | None = None
    actual_turning_id: str | None = None
    actual_thread_id: str | None = None
    allowed_turning_id: str | None = None
    allowed_thread_id: str | None = None


def build_volume_plans_contract(
    *,
    characters: CharactersOutput,
    plot: PlotOutput,
    total_volumes: int,
    chapters_per_volume: int,
    genre: str = "",
    selection_short_arc: bool = False,
) -> VolumePlansContract:
    """根据上游只读事实构造 ``volume_plans`` 首轮生成契约。

    ``chapter_count`` 是骨架层约束,允许围绕输入每卷章数 ±20% 波动。后续
    长度感知拆章可能扩大卷内章数,由 ``refine_volume_plans_length_aware``
    负责身份保留,不复用本范围。
    """

    delta = max(1, round(chapters_per_volume * 0.2))
    chapter_count_min = (
        chapters_per_volume
        if selection_short_arc
        else max(1, chapters_per_volume - delta)
    )
    chapter_count_max = (
        chapters_per_volume if selection_short_arc else chapters_per_volume + delta
    )
    return VolumePlansContract(
        total_volumes=total_volumes,
        chapter_count_min=chapter_count_min,
        chapter_count_max=chapter_count_max,
        chapters_per_volume=chapters_per_volume,
        allowed_phase_ids=frozenset(p.phase_id for p in plot.main_phases),
        allowed_turning_ids=frozenset(t.turning_id for t in plot.major_turning_points),
        allowed_thread_ids=frozenset(th.thread_id for th in plot.long_threads),
        allowed_character_ids=frozenset(c.character_id for c in characters.characters),
        load_bearing_objects=tuple(plot.load_bearing_objects),
        require_dramatic_promise=canonical_genre_identity(genre) == "mystery",
        selection_short_arc=selection_short_arc,
    )


def format_volume_plans_contract_for_prompt(
    contract: VolumePlansContract,
    *,
    include_promise_outcome_contract: bool = True,
    required_slot_delta: bool = False,
    range_feasibility_delta: bool = False,
) -> str:
    """把生成期契约渲染成简短 prompt 段。"""

    phase_ids = _format_id_set(contract.allowed_phase_ids)
    turning_ids = _format_id_set(contract.allowed_turning_ids)
    thread_ids = _format_id_set(contract.allowed_thread_ids)
    character_ids = _format_id_set(contract.allowed_character_ids)
    object_catalog = (
        json.dumps(
            [item.model_dump(mode="json") for item in contract.load_bearing_objects],
            ensure_ascii=False,
            sort_keys=True,
        )
        if contract.load_bearing_objects
        else "[]"
    )
    if required_slot_delta:
        drama_line = (
            "- 本题材每卷必须给出唯一 dramatic_promise 与唯一 finale_contract；"
            "程序把 finale 创意附到唯一卷末 beat，并从 promise 复制 "
            "actor/counterforce/thread/turning。\n"
            if contract.require_dramatic_promise
            else "- 没有 typed drama 时 dramatic_promise/finale_contract 可为 null；"
            "一旦使用 dramatic_moments，两者必须成对出现。\n"
        )
        object_contract = (
            "- 根级 object_placements 必须按 catalog 顺序逐对象各给一行；"
            "只选择 volume/finale 或 dramatic_moment 局部 slot，不复制对象内容。\n"
            "- 每个 placement 必须满足精确同源规则："
            "object.linked_turning_id == target.turning_id OR "
            "object.linked_thread_id == target.thread_id；一个目标最多 2 个对象。\n"
            "- 下列 plot catalog 中每个对象都必须出现且只出现一次；"
            "不得默认集中进 finale。\n"
            f"  plot_load_bearing_object_catalog={object_catalog}\n"
        )
        if range_feasibility_delta:
            chapter_count = contract.chapters_per_volume
            if contract.selection_short_arc:
                range_contract = (
                    "- 程序已冻结范围骨架：3 个单章 arc，chapter_range 依次为 "
                    "[1,1]/[2,2]/[3,3]，每 arc 恰好一个同范围 beat；"
                    "只能逐字填写 Schema 给定的范围。\n"
                )
            else:
                feasible_arc_max = min(4, chapter_count // 2)
                if feasible_arc_max < 2:
                    raise ValueError(
                        "chapter count cannot fit the normal two-arc contract"
                    )
                if chapter_count == 4:
                    range_contract = (
                        "- 程序已冻结范围骨架：精确 2 个 arc，范围为 [1,2]/[3,4]；"
                        "每 arc 精确 2 个单章 beat，范围依次为 "
                        "[1,1]/[2,2] 与 [3,3]/[4,4]。不要新增、复制、重叠或改写范围。\n"
                    )
                else:
                    range_contract = (
                        f"- 程序已按冻结 {chapter_count} 章移除不可能的 arc 数量；"
                        f"本卷只允许 2—{feasible_arc_max} 个 arc。Schema 未固定的"
                        "具体连续范围仍由你按剧情选择。\n"
                    )
        else:
            range_contract = ""
    else:
        drama_line = (
            "- 本题材每卷必须给出唯一 dramatic_promise，并在卷末 finale beat 用同一 "
            "actor/counterforce/thread/turning 对账 resolved 或明确延期。\n"
            if contract.require_dramatic_promise
            else "- dramatic_promise 只在本题材确有承重主对抗时使用；普通留白不凑合同。\n"
        )
        object_contract = (
            "- 每个 dramatic beat 的 load_bearing_objects 必须为 0—2 个；普通 beat 留 []。\n"
            "- 每个对象必须满足精确同源规则："
            "object.linked_turning_id == beat.turning_id OR "
            "object.linked_thread_id == beat.thread_id。\n"
            "- 下列 plot catalog 中每个对象的两个 linked_* 就是允许来源；"
            "只能逐对象、逐字段复制到匹配 source 的 dramatic beat，不得默认集中进 finale。"
            "catalog 中每个对象至少由一个 typed 高价值 beat 承担。\n"
            f"  plot_load_bearing_object_catalog={object_catalog}\n"
        )
        range_contract = ""
    promise_outcome_line = (
        "- 本次只有 1 卷，不存在合法后续卷：dramatic_promise.outcome 只能为 "
        "resolved，两个 deferred_to_* 必须为 null。\n"
        if contract.total_volumes == 1
        else (
            "- outcome=resolved 时两个 deferred_to_* 必须为 null；只有非末卷可用 "
            "explicitly_deferred，且必须同时选择 schema 中给出的后续 volume_id 与"
            "已有 thread_id；末卷只能 resolved。\n"
        )
    )
    return (
        "\n\n## 本轮 volume_plans 结构契约(生成前硬约束)\n\n"
        f"- volumes 长度必须 == {contract.total_volumes}。\n"
        f"- 每卷骨架 chapter_count 必须落在 "
        f"[{contract.chapter_count_min},{contract.chapter_count_max}] 内"
        f"(输入基准 {contract.chapters_per_volume};长度感知拆章另在单卷细化层做)。\n"
        f"- linked_phases 只能引用:{phase_ids}。\n"
        f"- linked_turning_points 只能引用:{turning_ids}。\n"
        f"- linked_threads 只能引用:{thread_ids}。\n"
        f"- 角色引用必须来自 characters/backfill 后集合:{character_ids};"
        "不要新造未登记姓名或 ID。\n"
        f"{object_contract}{drama_line}{range_contract}"
        + (promise_outcome_line if include_promise_outcome_contract else "")
        + (
            "- 本次 selection_test 短活动必须恰好 3 个非副本 arc，依次只覆盖 "
            "[1,1]/[2,2]/[3,3]，phase 依次为 opening/rising/climax；每个 arc "
            "恰好 1 个同范围 beat。\n"
            if contract.selection_short_arc
            else "- 普通产品每个 arc 必须严格拆 2-4 个 beats；"
            "beats 必须连续不重叠覆盖该 arc。\n"
        )
        + "- forbidden_advance 是防剧透契约,不得把后续 arc/beat 的核心事件提前穿越。\n"
    )


def validate_volume_plans_contract(
    volume_plans: VolumePlansOutput,
    contract: VolumePlansContract,
) -> list[VolumePlansContractFailure]:
    """校验首轮 ``volume_plans`` 是否满足生成期契约。"""

    failures: list[VolumePlansContractFailure] = []
    if len(volume_plans.volumes) != contract.total_volumes:
        failures.append(
            VolumePlansContractFailure(
                rule="volume_count_mismatch",
                message=(
                    f"volumes 长度 {len(volume_plans.volumes)} 必须 == "
                    f"{contract.total_volumes}"
                ),
            )
        )

    volume_ids = [volume.volume_id for volume in volume_plans.volumes]
    volume_index = {volume_id: idx for idx, volume_id in enumerate(volume_ids)}
    volume_by_id = {volume.volume_id: volume for volume in volume_plans.volumes}
    used_load_bearing_object_ids: set[str] = set()
    for volume in volume_plans.volumes:
        if not (
            contract.chapter_count_min
            <= volume.chapter_count
            <= contract.chapter_count_max
        ):
            failures.append(
                VolumePlansContractFailure(
                    rule="chapter_count_out_of_range",
                    volume_id=volume.volume_id,
                    message=(
                        f"chapter_count={volume.chapter_count} 不在骨架范围 "
                        f"[{contract.chapter_count_min},{contract.chapter_count_max}]"
                    ),
                )
            )
        failures.extend(_validate_linked_ids(volume, contract))
        if contract.selection_short_arc:
            expected_ranges = [(1, 1), (2, 2), (3, 3)]
            expected_phases = ["opening", "rising", "climax"]
            expected_arc_types = ["起因", "升级", "高潮/交代"]
            ordered_arcs = sorted(volume.arcs, key=lambda arc: arc.chapter_range[0])
            shape_ok = (
                volume.chapter_count == 3
                and len(ordered_arcs) == 3
                and [arc.chapter_range for arc in ordered_arcs] == expected_ranges
                and [arc.phase for arc in ordered_arcs] == expected_phases
                and [arc.arc_type for arc in ordered_arcs] == expected_arc_types
                and all(
                    len(arc.beats) == 1
                    and arc.beats[0].chapter_range == arc.chapter_range
                    for arc in ordered_arcs
                )
            )
            if not shape_ok:
                failures.append(
                    VolumePlansContractFailure(
                        rule="selection_short_arc_mismatch",
                        volume_id=volume.volume_id,
                        message=(
                            "selection_test 3 章合同必须为单章 arc_type="
                            "起因/升级/高潮/交代，phase=opening/rising/climax，"
                            "每 arc 恰好 1 个同范围 beat"
                        ),
                    )
                )
        failures.extend(
            _validate_volume_drama(
                volume=volume,
                contract=contract,
                volume_index=volume_index,
                volume_by_id=volume_by_id,
            )
        )
        for arc in volume.arcs:
            failures.extend(
                _validate_arc_beats_cover_arc(volume, arc, contract=contract)
            )
            for beat in arc.beats:
                if beat.dramatic_contract is not None:
                    used_load_bearing_object_ids.update(
                        item.object_id
                        for item in beat.dramatic_contract.load_bearing_objects
                    )
    for item in contract.load_bearing_objects:
        if item.object_id not in used_load_bearing_object_ids:
            failures.append(
                VolumePlansContractFailure(
                    rule="dramatic_object_unassigned",
                    message=(
                        "plot 承重物件必须至少由一个 typed 高价值 beat 承担:"
                        f"{item.object_id}"
                    ),
                )
            )
    return failures


def validate_refined_volume_contract(
    *,
    original: VolumePlanItem,
    refined: VolumePlanItem,
) -> list[VolumePlansContractFailure]:
    """校验单卷细化只重分配章数，不改任何剧情语义字段。"""

    failures: list[VolumePlansContractFailure] = []
    if (
        refined.title != original.title
        or refined.summary != original.summary
        or refined.tension_template != original.tension_template
        or refined.linked_phases != original.linked_phases
        or refined.linked_turning_points != original.linked_turning_points
        or refined.linked_threads != original.linked_threads
        or refined.dramatic_promise != original.dramatic_promise
    ):
        failures.append(
            VolumePlansContractFailure(
                rule="refine_volume_semantics_changed",
                volume_id=original.volume_id,
                message=(
                    "卷标题、概要、张力模板、linked_* 与 dramatic_promise 必须原样保留；"
                    "细化只改章数、区间与张力曲线"
                ),
            )
        )
    original_arcs = list(original.arcs)
    refined_arcs = list(refined.arcs)

    if len(refined_arcs) != len(original_arcs):
        failures.append(
            VolumePlansContractFailure(
                rule="refine_arc_identity_changed",
                volume_id=original.volume_id,
                message=(
                    f"arc 数量 {len(refined_arcs)} 必须 == 输入 {len(original_arcs)}"
                ),
            )
        )
        return failures

    for idx, (src_arc, dst_arc) in enumerate(
        zip(original_arcs, refined_arcs, strict=True),
        start=1,
    ):
        if (
            dst_arc.arc_id != src_arc.arc_id
            or dst_arc.arc_type != src_arc.arc_type
            or dst_arc.phase != src_arc.phase
        ):
            failures.append(
                VolumePlansContractFailure(
                    rule="refine_arc_identity_changed",
                    volume_id=original.volume_id,
                    arc_id=dst_arc.arc_id,
                    message=(
                        f"第 {idx} 个 arc 身份必须保持:arc_id={src_arc.arc_id}, "
                        f"arc_type={src_arc.arc_type}, phase={src_arc.phase}"
                    ),
                )
            )
            continue
        if dst_arc.summary != src_arc.summary:
            failures.append(
                VolumePlansContractFailure(
                    rule="refine_arc_summary_changed",
                    volume_id=original.volume_id,
                    arc_id=src_arc.arc_id,
                    message="arc.summary 必须原样保留；细化不能改剧情语义",
                )
            )
        failures.extend(
            _validate_refined_beats(
                volume_id=original.volume_id,
                source_arc=src_arc,
                refined_arc=dst_arc,
            )
        )
    return failures


def format_volume_plans_contract_failures_for_retry(
    failures: list[VolumePlansContractFailure],
) -> str:
    """把契约失败转成有界、结构化 retry feedback。"""

    lines = [
        "volume_plans 结构契约失败。请只修复下列结构项,不要改写 world / characters / plot 上游事实。",
        "失败明细:",
    ]
    for failure in failures:
        location = _format_failure_location(failure)
        lines.append(f"- [{failure.rule}]{location}{failure.message}")
        source_detail = _format_object_source_detail(failure)
        if source_detail:
            lines.append(f"  {source_detail}")
    return "\n".join(lines)


def collect_volume_object_source_failures(
    raw_data: Any,
    contract: VolumePlansContract,
) -> list[VolumePlansContractFailure]:
    """从尚未通过 Pydantic 的原始 volume JSON 提取可证 object-source 失败。

    这里只读取候选已经显式给出的 stable ID 和 catalog 权威来源，不补字段、
    不改内容，也不尝试把无 ID 的自由文本猜成承重物件。它专供 schema retry
    把 Pydantic 跨字段错误翻成可执行反馈；最终准入仍由原模型与 business
    validator 决定。
    """

    if not isinstance(raw_data, dict):
        return []
    volumes = raw_data.get("volumes")
    if not isinstance(volumes, list):
        return []
    catalog = {item.object_id: item for item in contract.load_bearing_objects}
    failures: list[VolumePlansContractFailure] = []
    for volume_index, volume in enumerate(volumes):
        if not isinstance(volume, dict):
            continue
        arcs = volume.get("arcs")
        if not isinstance(arcs, list):
            continue
        for arc_index, arc in enumerate(arcs):
            if not isinstance(arc, dict):
                continue
            beats = arc.get("beats")
            if not isinstance(beats, list):
                continue
            for beat_index, beat in enumerate(beats):
                if not isinstance(beat, dict):
                    continue
                drama = beat.get("dramatic_contract")
                if not isinstance(drama, dict):
                    continue
                actual_turning = _optional_string(drama.get("turning_id"))
                actual_thread = _optional_string(drama.get("thread_id"))
                objects = drama.get("load_bearing_objects")
                if not isinstance(objects, list):
                    continue
                for object_index, candidate in enumerate(objects):
                    if not isinstance(candidate, dict):
                        continue
                    object_id = candidate.get("object_id")
                    if not isinstance(object_id, str):
                        continue
                    expected = catalog.get(object_id)
                    if expected is None:
                        continue
                    same_source = (
                        expected.linked_turning_id is not None
                        and expected.linked_turning_id == actual_turning
                    ) or (
                        expected.linked_thread_id is not None
                        and expected.linked_thread_id == actual_thread
                    )
                    if same_source:
                        continue
                    field = (
                        f"volumes[{volume_index}].arcs[{arc_index}].beats[{beat_index}]"
                        f".dramatic_contract.load_bearing_objects[{object_index}]"
                    )
                    failures.append(
                        VolumePlansContractFailure(
                            rule="dramatic_identity_mismatch",
                            message=(
                                "承重物件未满足 catalog allowed source 与当前 beat "
                                "source 的精确 OR 同源规则"
                            ),
                            volume_id=_optional_string(volume.get("volume_id")),
                            arc_id=_optional_string(arc.get("arc_id")),
                            beat_id=_optional_string(beat.get("beat_id")),
                            field=field,
                            object_id=object_id,
                            actual_turning_id=actual_turning,
                            actual_thread_id=actual_thread,
                            allowed_turning_id=expected.linked_turning_id,
                            allowed_thread_id=expected.linked_thread_id,
                        )
                    )
    return failures


def format_volume_object_source_failures_for_retry(
    failures: list[VolumePlansContractFailure],
) -> str:
    """只渲染可证 object-source 明细，供 Pydantic schema feedback 追加。"""

    details: list[str] = []
    for failure in failures:
        detail = _format_object_source_detail(failure)
        if detail:
            details.append(detail)
    if not details:
        return ""
    return (
        "\n\n### 承重物件同源精确反馈\n\n"
        + "\n".join(f"- {detail}" for detail in details)
        + "\n- 只把对象移到匹配 allowed source 的 beat；不得改写 catalog、"
        "不得猜故事或把对象默认集中进 finale。"
    )


def _validate_linked_ids(
    volume: VolumePlanItem,
    contract: VolumePlansContract,
) -> list[VolumePlansContractFailure]:
    failures: list[VolumePlansContractFailure] = []
    failures.extend(
        _unknown_id_failures(
            rule="unknown_linked_phase",
            volume_id=volume.volume_id,
            field_name="linked_phases",
            ids=volume.linked_phases,
            allowed=contract.allowed_phase_ids,
        )
    )
    failures.extend(
        _unknown_id_failures(
            rule="unknown_linked_turning_point",
            volume_id=volume.volume_id,
            field_name="linked_turning_points",
            ids=volume.linked_turning_points,
            allowed=contract.allowed_turning_ids,
        )
    )
    failures.extend(
        _unknown_id_failures(
            rule="unknown_linked_thread",
            volume_id=volume.volume_id,
            field_name="linked_threads",
            ids=volume.linked_threads,
            allowed=contract.allowed_thread_ids,
        )
    )
    return failures


def _unknown_id_failures(
    *,
    rule: VolumePlansContractRule,
    volume_id: str,
    field_name: str,
    ids: list[str],
    allowed: frozenset[str],
) -> list[VolumePlansContractFailure]:
    failures: list[VolumePlansContractFailure] = []
    for value in ids:
        if value in allowed:
            continue
        failures.append(
            VolumePlansContractFailure(
                rule=rule,
                volume_id=volume_id,
                message=f"{field_name} 引用了未知 ID {value!r}",
            )
        )
    return failures


def _validate_arc_beats_cover_arc(
    volume: VolumePlanItem,
    arc: VolumeArcPlan,
    *,
    contract: VolumePlansContract,
) -> list[VolumePlansContractFailure]:
    arc_start, arc_end = arc.chapter_range
    arc_span = arc_end - arc_start + 1
    min_beats = 1 if contract.selection_short_arc else 2
    max_beats = 1 if contract.selection_short_arc else 4
    failures: list[VolumePlansContractFailure] = []

    if not (min_beats <= len(arc.beats) <= max_beats):
        failures.append(
            VolumePlansContractFailure(
                rule="beat_count_out_of_range",
                volume_id=volume.volume_id,
                arc_id=arc.arc_id,
                message=(
                    f"arc 跨度 {arc_span} 章时 beats 数量应为 "
                    f"{min_beats}-{max_beats},实际 {len(arc.beats)}"
                ),
            )
        )
        return failures

    expected_start = arc_start
    for beat in sorted(arc.beats, key=lambda item: item.chapter_range[0]):
        beat_start, beat_end = beat.chapter_range
        if beat_start != expected_start:
            failures.append(
                VolumePlansContractFailure(
                    rule="beat_coverage_gap",
                    volume_id=volume.volume_id,
                    arc_id=arc.arc_id,
                    beat_id=beat.beat_id,
                    message=(
                        f"beats 未连续覆盖 arc;期望下一段从 {expected_start} 开始,"
                        f"实际 {beat_start}"
                    ),
                )
            )
            return failures
        expected_start = beat_end + 1

    if expected_start != arc_end + 1:
        failures.append(
            VolumePlansContractFailure(
                rule="beat_coverage_gap",
                volume_id=volume.volume_id,
                arc_id=arc.arc_id,
                message=(
                    f"beats 未覆盖到 arc 末尾 {arc_end};当前覆盖到 {expected_start - 1}"
                ),
            )
        )
    return failures


def _validate_refined_beats(
    *,
    volume_id: str,
    source_arc: VolumeArcPlan,
    refined_arc: VolumeArcPlan,
) -> list[VolumePlansContractFailure]:
    failures: list[VolumePlansContractFailure] = []
    if not source_arc.beats:
        return failures
    if len(refined_arc.beats) != len(source_arc.beats):
        return [
            VolumePlansContractFailure(
                rule="refine_beat_identity_changed",
                volume_id=volume_id,
                arc_id=source_arc.arc_id,
                message=(
                    f"arc={source_arc.arc_id} beat 数量 {len(refined_arc.beats)} "
                    f"必须 == 输入 {len(source_arc.beats)}"
                ),
            )
        ]

    for idx, (src_beat, dst_beat) in enumerate(
        zip(source_arc.beats, refined_arc.beats, strict=True),
        start=1,
    ):
        if dst_beat.beat_id != src_beat.beat_id:
            failures.append(
                VolumePlansContractFailure(
                    rule="refine_beat_identity_changed",
                    volume_id=volume_id,
                    arc_id=source_arc.arc_id,
                    beat_id=dst_beat.beat_id,
                    message=(
                        f"第 {idx} 个 beat_id 必须保持 {src_beat.beat_id},"
                        f"实际 {dst_beat.beat_id}"
                    ),
                )
            )
            continue
        if dst_beat.summary != src_beat.summary:
            failures.append(
                VolumePlansContractFailure(
                    rule="refine_beat_summary_changed",
                    volume_id=volume_id,
                    arc_id=source_arc.arc_id,
                    beat_id=src_beat.beat_id,
                    message="beat.summary 必须原样保留；细化不能改剧情语义",
                )
            )
        if list(dst_beat.must_advance) != list(src_beat.must_advance):
            failures.append(
                VolumePlansContractFailure(
                    rule="refine_beat_must_changed",
                    volume_id=volume_id,
                    arc_id=source_arc.arc_id,
                    beat_id=src_beat.beat_id,
                    message="must_advance 必须原样保留;细化只改章数区间",
                )
            )
        if list(dst_beat.forbidden_advance) != list(src_beat.forbidden_advance):
            failures.append(
                VolumePlansContractFailure(
                    rule="refine_beat_forbidden_changed",
                    volume_id=volume_id,
                    arc_id=source_arc.arc_id,
                    beat_id=src_beat.beat_id,
                    message="forbidden_advance 必须原样保留,不得穿越后续剧情",
                )
            )
        if dst_beat.dramatic_contract != src_beat.dramatic_contract:
            failures.append(
                VolumePlansContractFailure(
                    rule="refine_dramatic_semantics_changed",
                    volume_id=volume_id,
                    arc_id=source_arc.arc_id,
                    beat_id=src_beat.beat_id,
                    message="dramatic_contract 必须逐字段原样保留",
                )
            )
    return failures


def _validate_volume_drama(
    *,
    volume: VolumePlanItem,
    contract: VolumePlansContract,
    volume_index: dict[str, int],
    volume_by_id: dict[str, VolumePlanItem],
) -> list[VolumePlansContractFailure]:
    """校验卷承诺、卷末 payoff 与所有 typed ID 的同源性。"""

    failures: list[VolumePlansContractFailure] = []
    promise = volume.dramatic_promise
    all_beats = [beat for arc in volume.arcs for beat in arc.beats]
    all_finale_beats = [
        beat
        for beat in all_beats
        if beat.dramatic_contract is not None
        and beat.dramatic_contract.function == "finale"
    ]
    finale_beats = [
        beat
        for beat in all_finale_beats
        if beat.chapter_range[1] == volume.chapter_count
    ]

    has_dramatic_beat = any(beat.dramatic_contract is not None for beat in all_beats)
    if promise is None and (contract.require_dramatic_promise or has_dramatic_beat):
        failures.append(
            VolumePlansContractFailure(
                rule="dramatic_promise_missing",
                volume_id=volume.volume_id,
                message="悬疑卷或含 typed 高价值 beat 的卷缺少 dramatic_promise",
            )
        )
    if promise is not None or contract.require_dramatic_promise or has_dramatic_beat:
        if len(all_finale_beats) != 1 or len(finale_beats) != 1:
            failures.append(
                VolumePlansContractFailure(
                    rule="dramatic_finale_missing",
                    volume_id=volume.volume_id,
                    message=(
                        "卷末必须且只能有一个 function=finale 的 dramatic_contract，"
                        f"全卷实际 {len(all_finale_beats)}，位于卷末 {len(finale_beats)}"
                    ),
                )
            )

    for beat in all_beats:
        drama = beat.dramatic_contract
        if drama is None:
            continue
        if not beat.must_advance:
            failures.append(
                VolumePlansContractFailure(
                    rule="dramatic_beat_missing_must",
                    volume_id=volume.volume_id,
                    beat_id=beat.beat_id,
                    message=(
                        "typed 高价值 beat 必须复用既有非空 must_advance，"
                        "不能靠 A1 新增 beat-alignment 请求"
                    ),
                )
            )
        unknown: list[str] = []
        if drama.actor_id not in contract.allowed_character_ids:
            unknown.append(f"actor_id={drama.actor_id}")
        if drama.counterforce_id not in contract.allowed_character_ids:
            unknown.append(f"counterforce_id={drama.counterforce_id}")
        if drama.thread_id and drama.thread_id not in contract.allowed_thread_ids:
            unknown.append(f"thread_id={drama.thread_id}")
        if drama.turning_id and drama.turning_id not in contract.allowed_turning_ids:
            unknown.append(f"turning_id={drama.turning_id}")
        if unknown:
            failures.append(
                VolumePlansContractFailure(
                    rule="dramatic_unknown_ref",
                    volume_id=volume.volume_id,
                    beat_id=beat.beat_id,
                    message="dramatic_contract 引用未知 ID: " + ", ".join(unknown),
                )
            )
        object_catalog = {
            item.object_id: item for item in contract.load_bearing_objects
        }
        for object_index, item in enumerate(drama.load_bearing_objects):
            expected = object_catalog.get(item.object_id)
            if expected is None:
                failures.append(
                    VolumePlansContractFailure(
                        rule="dramatic_unknown_ref",
                        volume_id=volume.volume_id,
                        beat_id=beat.beat_id,
                        message=(
                            "dramatic_contract 引用未知承重物件 "
                            f"object_id={item.object_id}"
                        ),
                    )
                )
            elif item != expected:
                field = _stable_object_field(
                    volume=volume,
                    beat_id=beat.beat_id,
                    object_index=object_index,
                )
                failures.append(
                    VolumePlansContractFailure(
                        rule="dramatic_identity_mismatch",
                        volume_id=volume.volume_id,
                        beat_id=beat.beat_id,
                        message=(
                            "dramatic_contract.load_bearing_objects 必须从 plot "
                            f"catalog 逐字段复制 object_id={item.object_id}"
                        ),
                        field=field,
                        object_id=item.object_id,
                        actual_turning_id=drama.turning_id,
                        actual_thread_id=drama.thread_id,
                        allowed_turning_id=expected.linked_turning_id,
                        allowed_thread_id=expected.linked_thread_id,
                    )
                )
        if (
            drama.thread_id is not None and drama.thread_id not in volume.linked_threads
        ) or (
            drama.turning_id is not None
            and drama.turning_id not in volume.linked_turning_points
        ):
            failures.append(
                VolumePlansContractFailure(
                    rule="dramatic_identity_mismatch",
                    volume_id=volume.volume_id,
                    beat_id=beat.beat_id,
                    message=(
                        "dramatic_contract 的 thread/turning 必须同时进入本卷 linked_*"
                    ),
                )
            )

    if promise is None:
        return failures

    unknown_promise: list[str] = []
    if (
        promise.primary_thread_id is not None
        and promise.primary_thread_id not in contract.allowed_thread_ids
    ):
        unknown_promise.append(f"primary_thread_id={promise.primary_thread_id}")
    if (
        promise.primary_turning_id
        and promise.primary_turning_id not in contract.allowed_turning_ids
    ):
        unknown_promise.append(f"primary_turning_id={promise.primary_turning_id}")
    if promise.actor_id not in contract.allowed_character_ids:
        unknown_promise.append(f"actor_id={promise.actor_id}")
    if promise.counterforce_id not in contract.allowed_character_ids:
        unknown_promise.append(f"counterforce_id={promise.counterforce_id}")
    if unknown_promise:
        failures.append(
            VolumePlansContractFailure(
                rule="dramatic_unknown_ref",
                volume_id=volume.volume_id,
                message="dramatic_promise 引用未知 ID: " + ", ".join(unknown_promise),
            )
        )
    if (
        promise.primary_thread_id is not None
        and promise.primary_thread_id not in volume.linked_threads
    ) or (
        promise.primary_turning_id is not None
        and promise.primary_turning_id not in volume.linked_turning_points
    ):
        failures.append(
            VolumePlansContractFailure(
                rule="dramatic_identity_mismatch",
                volume_id=volume.volume_id,
                message="dramatic_promise 主 thread/turning 必须同时进入本卷 linked_*",
            )
        )

    if len(finale_beats) == 1:
        finale = finale_beats[0].dramatic_contract
        assert finale is not None
        if (
            finale.actor_id != promise.actor_id
            or finale.counterforce_id != promise.counterforce_id
            or finale.thread_id != promise.primary_thread_id
            or finale.turning_id != promise.primary_turning_id
        ):
            failures.append(
                VolumePlansContractFailure(
                    rule="dramatic_identity_mismatch",
                    volume_id=volume.volume_id,
                    beat_id=finale_beats[0].beat_id,
                    message="finale dramatic_contract 未与本卷主承诺逐字段对齐",
                )
            )

    if promise.outcome == "explicitly_deferred":
        target_volume = promise.deferred_to_volume_id
        target_thread = promise.deferred_to_thread_id
        current_idx = volume_index.get(volume.volume_id, -1)
        target_idx = volume_index.get(target_volume or "", -1)
        target_volume_plan = volume_by_id.get(target_volume or "")
        if (
            target_idx <= current_idx
            or target_thread not in contract.allowed_thread_ids
            or target_volume_plan is None
            or target_thread not in target_volume_plan.linked_threads
        ):
            failures.append(
                VolumePlansContractFailure(
                    rule="dramatic_deferred_target_invalid",
                    volume_id=volume.volume_id,
                    message=(
                        "explicitly_deferred 必须指向计划中后续 volume_id 与已存在 thread_id"
                    ),
                )
            )
    return failures


def _format_id_set(values: frozenset[str]) -> str:
    if not values:
        return "(无)"
    return ", ".join(sorted(values))


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _stable_object_field(
    *,
    volume: VolumePlanItem,
    beat_id: str,
    object_index: int,
) -> str:
    for arc in volume.arcs:
        if any(beat.beat_id == beat_id for beat in arc.beats):
            return (
                f"volumes[volume_id={volume.volume_id}].arcs[arc_id={arc.arc_id}]"
                f".beats[beat_id={beat_id}].dramatic_contract"
                f".load_bearing_objects[{object_index}]"
            )
    return (
        f"volumes[volume_id={volume.volume_id}].beats[beat_id={beat_id}]"
        f".dramatic_contract.load_bearing_objects[{object_index}]"
    )


def _display_optional_id(value: str | None) -> str:
    return value if value is not None else "null"


def _format_object_source_detail(
    failure: VolumePlansContractFailure,
) -> str:
    if not failure.field or not failure.object_id:
        return ""
    return (
        f"field={failure.field} object_id={failure.object_id} "
        "candidate_beat_source("
        f"turning_id={_display_optional_id(failure.actual_turning_id)},"
        f"thread_id={_display_optional_id(failure.actual_thread_id)}) "
        "object_allowed_source("
        f"linked_turning_id={_display_optional_id(failure.allowed_turning_id)},"
        f"linked_thread_id={_display_optional_id(failure.allowed_thread_id)})"
    )


def _format_failure_location(failure: VolumePlansContractFailure) -> str:
    parts: list[str] = []
    if failure.volume_id:
        parts.append(f"volume_id={failure.volume_id}")
    if failure.arc_id:
        parts.append(f"arc_id={failure.arc_id}")
    if failure.beat_id:
        parts.append(f"beat_id={failure.beat_id}")
    if not parts:
        return " "
    return " " + ", ".join(parts) + ": "


__all__ = [
    "build_volume_plans_contract",
    "collect_volume_object_source_failures",
    "format_volume_object_source_failures_for_retry",
    "format_volume_plans_contract_failures_for_retry",
    "format_volume_plans_contract_for_prompt",
    "validate_refined_volume_contract",
    "validate_volume_plans_contract",
    "VolumePlansContract",
    "VolumePlansContractFailure",
]
