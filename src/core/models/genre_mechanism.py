"""网文/修仙 genre 机制门控(v1.1 wave 5 s10)。

两模块(①升级可感化 breakthrough_payoff / ②境界一致性 progression_rank)的**题材门控 +
auto→默认解析 + 题材标签**纯函数。import-safe(无 IO),供 ``ChapterCardService`` 的
``resolve_effective_*`` 方法 + ``outline_service``(模块① mini_plan gating)共用,避免门控
逻辑在两处分裂。

设计要点(维护人 2026-06-09 拍板):
- **两模块都只给 4 个 style_template**:古典仙侠 / 修仙成长 / 搞笑爽文 / 热血战斗;其余 3 个
  (宏大叙事/史诗、暗黑悬疑、治愈日常)**两个都不给**。
- **字段中性 `progression_rank`,UI/注入标签按题材**(仙侠=境界 / 战斗=战力)。
- **auto→题材默认**:②两种修仙默认 ON(境界必守)、爽文/热血默认 OFF(防强加在无等级体系
  的爽文上误报);①四题材全默认 ON(升级流核心)。``on``/``off`` 显式覆盖**仅 4 题材生效**
  (其余题材即便填 on 也按 off,防外溢)。
"""

from __future__ import annotations

from typing import Literal

#: 两模块共同门控的 4 个 style_template(其余题材两模块均 no-op)。
PROGRESSION_GENRES: frozenset[str] = frozenset(
    {"classical_xianxia", "xianxia_growth", "comedic_burst", "heroic_battle"}
)

#: 进阶等级注入/显示标签(题材中性字段 → 题材化标签)。
_PROGRESSION_LABELS: dict[str, str] = {
    "classical_xianxia": "境界",
    "xianxia_growth": "境界",
    "comedic_burst": "境界",
    "heroic_battle": "战力",
}

#: 模块② 境界一致性 auto 默认 ON 的题材(两种修仙必守;爽文/热血默认 OFF 防误报)。
_CONSISTENCY_DEFAULT_ON: frozenset[str] = frozenset({"classical_xianxia", "xianxia_growth"})

#: 模块① 升级可感化 auto 默认 ON 的题材(4 题材全是升级流核心)。
_BREAKTHROUGH_DEFAULT_ON: frozenset[str] = PROGRESSION_GENRES


def progression_label(style_template: str | None) -> str:
    """题材进阶标签(仙侠=境界 / 战斗=战力);未知/None → '境界'(中性兜底)。"""
    if style_template is None:
        return "境界"
    return _PROGRESSION_LABELS.get(style_template, "境界")


def _resolve(
    *,
    style_template: str | None,
    mode: Literal["auto", "on", "off"],
    default_on: frozenset[str],
) -> bool:
    """通用解析:题材∉4 → False(防外溢);on/off 显式;auto→题材默认。"""
    if style_template not in PROGRESSION_GENRES:
        return False
    if mode == "on":
        return True
    if mode == "off":
        return False
    return style_template in default_on  # auto


def resolve_progression_consistency(
    *, style_template: str | None, mode: Literal["auto", "on", "off"]
) -> bool:
    """模块② 境界一致性是否对本工作区生效。"""
    return _resolve(
        style_template=style_template, mode=mode, default_on=_CONSISTENCY_DEFAULT_ON
    )


def resolve_breakthrough_payoff(
    *, style_template: str | None, mode: Literal["auto", "on", "off"]
) -> bool:
    """模块① 升级可感化是否对本工作区生效。"""
    return _resolve(
        style_template=style_template, mode=mode, default_on=_BREAKTHROUGH_DEFAULT_ON
    )
