"""编排的可确定检查，不把风格、心理、情绪或字数变成硬配额。"""
from __future__ import annotations

from src.core.models.authoring_contract import AuthoringContract
from src.core.models.outline import PlotOutput, VolumePlanItem, VolumePlansOutput


def validate_authoring_layout(plans: VolumePlansOutput, contract: AuthoringContract) -> None:
    if len(plans.volumes) != contract.preferences.total_volumes:
        raise ValueError("卷数与已选规划不符")
    expected = {m.milestone_id: m for m in contract.milestones}
    found: dict[str, tuple[int, int]] = {}
    offset = 0
    ids: set[str] = set()
    for volume, count in zip(plans.volumes, contract.preferences.counts(), strict=True):
        if contract.preferences.fixed_chapter_counts and volume.chapter_count != count:
            raise ValueError("用户固定的各卷章数不得改变")
        cursor = 1
        for arc in volume.arcs:
            for beat in arc.beats:
                if beat.beat_id in ids:
                    raise ValueError("编排后的beat ID重复")
                ids.add(beat.beat_id)
                if beat.chapter_range[0] != cursor:
                    raise ValueError("每章须恰好归属一个剧情段，不能漏章或重叠")
                cursor = beat.chapter_range[1] + 1
                for mid in beat.planning_milestones:
                    if mid not in expected or mid in found:
                        raise ValueError("规划节点引用不存在或重复发生")
                    found[mid] = (offset + beat.chapter_range[0], offset + beat.chapter_range[1])
        if cursor != volume.chapter_count + 1:
            raise ValueError("编排没有覆盖全卷章节")
        offset += volume.chapter_count
    if set(found) != set(expected):
        raise ValueError("已确认的开局节点必须分配到实际剧情段")
    deadline = contract.preferences.entry_deadline()
    entry = next(mid for mid, item in expected.items() if item.kind == "core_entry")
    if deadline is not None and found[entry][1] > deadline:
        raise ValueError(f"已选开局要求第{deadline}章前进入核心经历，不能延后到{found[entry]}")


def validate_rearranged_volume(original: VolumePlanItem, refined: VolumePlanItem, plot: PlotOutput | None) -> None:
    if refined.volume_id != original.volume_id:
        raise ValueError("细化不能替换卷的身份")
    for key in ("linked_phases", "linked_turning_points", "linked_threads"):
        if set(getattr(original, key)) != set(getattr(refined, key)):
            raise ValueError("跨卷主线分工需在全书规划调整，单卷不得静默改变")
    # 承重物件不能借重排改机制；普通准备场景与原章号不受此冻结。
    objects = {o.object_id: o for arc in original.arcs for beat in arc.beats
               if beat.dramatic_contract for o in beat.dramatic_contract.load_bearing_objects}
    actual = {o.object_id: o for arc in refined.arcs for beat in arc.beats
              if beat.dramatic_contract for o in beat.dramatic_contract.load_bearing_objects}
    if actual != objects:
        raise ValueError("重排不能增删或改写已确认承重物件机制")
    before = {m for arc in original.arcs for beat in arc.beats for m in beat.planning_milestones}
    after = {m for arc in refined.arcs for beat in arc.beats for m in beat.planning_milestones}
    if before != after:
        raise ValueError("重排不能丢失或移走本卷承诺的开局节点")
    if plot is not None:
        allowed = {t.turning_id for t in plot.major_turning_points}
        for arc in refined.arcs:
            for beat in arc.beats:
                drama = beat.dramatic_contract
                if drama and drama.turning_id and drama.turning_id not in allowed:
                    raise ValueError("重排引用了不存在的主线转折")
