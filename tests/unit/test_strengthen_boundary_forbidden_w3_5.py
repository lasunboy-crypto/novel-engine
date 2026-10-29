"""strengthen_boundary_forbidden 单测(v2 W3.5 补强3,防回退)。

契约(根因实证 docs/impl/v2_w3_5_writer_boundary_supplement3.md §0.6):卷末 / 临界点章
forbidden 弱措辞"提前[进入类]"只禁"过早做"、不禁"章末跨过临界点做" → 护栏失效
(弱措辞 writer 越界 ~62%,强措辞 ~0)。B 规则确定性追加时间无条件强边界项。

谁可能破坏:改 chapter_card.py 的 _BOUNDARY_ENTRY_VERBS / 正则 / guard,或挪走
chapter_card_generator._assemble_chapter_card 内的接线。
"""

from __future__ import annotations

from src.core.models.chapter_card import (
    propagate_forbidden_advance_entry,
    strengthen_boundary_forbidden,
)


def _strong(forbidden: list[str]) -> bool:
    """是否含 B 追加的强边界项。"""
    return any(
        "绝不" in f and any(v in f for v in ("进入", "踏入", "穿过"))
        for f in forbidden
    )


# --- 应触发:弱"提前[进入类]" + must_achieve 不含进入 ---


def test_fires_on_premature_enter() -> None:
    out = strengthen_boundary_forbidden(["林晓提前进入裂隙"], ["将手按上裂隙壁,壁面发光"])
    assert _strong(out)
    assert "裂隙" in out[-1]  # 对象抽取
    # 去稀释:原"提前进入"被改为"进入"(无条件禁止),不再保留弱措辞
    assert "林晓进入裂隙" in out
    assert not any("提前进入" in f for f in out)


def test_fires_on_premature_step_in_and_extracts_object() -> None:
    out = strengthen_boundary_forbidden(["提前踏入通道", "赵怀瑾出场"], ["抵达入口"])
    assert _strong(out)
    assert "通道" in out[-1]
    assert not any("提前踏入" in f for f in out)  # 去稀释


def test_fires_on_compound_item_dedilutes_enter_keeps_open() -> None:
    """复合条目('提前开启或提前进入'):进入部分去稀释('提前进入'→'进入'),'提前开启'不动。"""
    out = strengthen_boundary_forbidden(
        ["裂隙壁在子时前提前开启或林晓提前进入"], ["按壁、壁发光"]
    )
    assert _strong(out)
    # '提前开启'保留(开启非进入类),'提前进入'去稀释为'进入'
    assert any("提前开启" in f for f in out)
    assert not any("提前进入" in f for f in out)
    assert any("或林晓进入" in f for f in out)


# --- guard:must_achieve 要求该进入动作 → 跳过(防 must/forbidden 矛盾)---


def test_skips_when_must_achieve_requires_enter() -> None:
    out = strengthen_boundary_forbidden(["林晓提前进入裂隙"], ["林晓进入密室取得档案"])
    assert out == ["林晓提前进入裂隙"]  # 原样,不强化
    assert not _strong(out)


def test_skips_when_must_achieve_has_step_in() -> None:
    out = strengthen_boundary_forbidden(["提前穿过结界"], ["踏入禁地见到师父"])
    assert not _strong(out)


# --- 不应触发:非进入类动词 / 无"提前" ---


def test_no_fire_on_premature_open() -> None:
    """'提前开启'(开启非进入类)不触发。"""
    out = strengthen_boundary_forbidden(["裂隙提前开启"], ["按壁"])
    assert out == ["裂隙提前开启"]


def test_no_fire_on_premature_reveal_or_appear() -> None:
    assert not _strong(strengthen_boundary_forbidden(["提前揭示父亲下落"], ["对峙"]))
    assert not _strong(strengthen_boundary_forbidden(["赵怀瑾提前登场"], ["对峙"]))


def test_no_fire_without_premature() -> None:
    out = strengthen_boundary_forbidden(["越界提及未登场角色", "出现现代器物"], ["对话"])
    assert out == ["越界提及未登场角色", "出现现代器物"]


# --- 幂等:已强化的卡再跑不重复追加 ---


def test_idempotent() -> None:
    once = strengthen_boundary_forbidden(["林晓提前进入裂隙"], ["按壁"])
    twice = strengthen_boundary_forbidden(once, ["按壁"])
    # 第二次:once 里仍含原弱条'提前进入' → 仍会算出同一强条,但去重不重复追加
    assert twice.count(once[-1]) == 1


# --- 空输入 / 边界 ---


def test_empty_forbidden() -> None:
    assert strengthen_boundary_forbidden([], ["按壁"]) == []


def test_returns_new_list_not_mutate() -> None:
    orig = ["越界提及未登场角色"]
    out = strengthen_boundary_forbidden(orig, ["对话"])
    assert out is not orig  # 返回新 list 副本


# ---------------------------------------------------------------------------
# propagate_forbidden_advance_entry(健壮主力:beat forbidden_advance → 卡 forbidden)
# ---------------------------------------------------------------------------


def _has_concrete_strong(forbidden: list[str], space: str) -> bool:
    return any("绝不" in f and space in f and ("进入" in f or "踏入" in f) for f in forbidden)


def test_fa_propagates_concrete_entry_boundary() -> None:
    """forbidden_advance '完全进入裂隙或异空间' → 注入含具体对象的强边界。"""
    fa = ["林晓完全进入裂隙或异空间", "远古存在被完全唤醒", "赵怀瑾发动大规模净化"]
    out = propagate_forbidden_advance_entry(["陈远出场"], ["陈远对峙后离开"], fa)
    assert _has_concrete_strong(out, "裂隙")
    assert _has_concrete_strong(out, "异空间")  # 多空间名词都点名
    assert "陈远出场" in out  # 原条保留


def test_fa_skips_non_entry_items() -> None:
    """非'进入空间'类 forbidden_advance(唤醒/净化/出场)不注入。"""
    fa = ["远古存在被完全唤醒", "赵怀瑾发动大规模净化", "王忠的守史人身份暴露"]
    out = propagate_forbidden_advance_entry(["x"], ["y"], fa)
    assert out == ["x"]


def test_fa_guard_must_requires_entry() -> None:
    """must_achieve 要求进入该空间(卷二章)→ 跳过,不锁死必达。"""
    fa = ["林晓完全进入裂隙或异空间"]
    out = propagate_forbidden_advance_entry(["x"], ["林晓进入裂隙深处探索核心"], fa)
    assert out == ["x"]  # must 要进入裂隙 → 不注入


def test_fa_empty_no_change() -> None:
    assert propagate_forbidden_advance_entry(["x"], ["y"], []) == ["x"]


def test_fa_idempotent() -> None:
    fa = ["林晓完全进入裂隙"]
    once = propagate_forbidden_advance_entry(["x"], ["对峙"], fa)
    twice = propagate_forbidden_advance_entry(once, ["对峙"], fa)
    assert once == twice  # 已注入不重复
