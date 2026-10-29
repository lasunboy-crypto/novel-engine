"""[WARN] 跨章风格趋势检测(v2 w2-s2,漂移治理检测端)。

职责:对"全书锚(ch1-3 final 指纹中位数)"检测**跨章慢漂**——连续 N 章
同向超偏移带才报,与既有 ``style_drift_validator``(同章 v1→vN rewrite
突变锚,HARD)分层并存,组成双层锚:

- **同章突变层**(style_drift_validator,HARD):管 rewrite 循环内单章
  风格突变(50% 阈值),失败派 rewrite。
- **跨章慢漂层**(本模块,WARN):管 30 章尺度"温水煮青蛙"式单向漂移
  (v1.1 B1 30 章长跑实证:段均 +52%,全机制语料末段仍 +48%),报警
  不阻断 —— 趋势是全书级信号,单章 rewrite 无法治本(该章重写不解决
  "近 3 章都重"),HARD 阻断语义错位 + 误杀成本不对称(G10 教训)。

参数(w2-s1 标定 SSOT,docs/acceptance/v2_w2_s1_drift_calibration_report.md §3)
==============================================================================

- **锚 = ch1-3(头 3 个有指纹的 approved 章)final 指纹的中位数**。
  中位数显著优于均值(标定核心发现 §2.2):luo-accept-v2 ch1-3 均值被
  ch2(127.4)拉高 → 均值锚下中段健康区被误判下行偏离(ch13 误报)、
  末段反而够不到触发线;中位锚(91.8)下中段误报消失 + 末段稳定检出。
- **报警维度 2 个**:段均(N=3 连续 / ±30% 带)+ 句均(N=3 / ±20%)。
  双向报(上行=越写越重,下行=连续塌缩——run-accept-v1 ch-0012 末章
  风格塌缩属同章层管辖,本层对"连续多章塌缩"补覆盖)。
- **N=3 连续同向**:天然免疫单章/双章张力尖峰(标定 §2.3:两语料中段
  单章尖峰全部不触发;真漂移段 clean ch7-10 / accept ch25-28 稳定触发)。
  断开(回带内 / 反向)即清零。
- **从 ch4 起检测**(锚窗口就位后);两语料标定表现:clean 段均 ch9 首报 /
  accept ch27 首报、中段零误报;clean 句均全网格静默零误报。
- **观察维度(进 details 不报警)**:``exclamation_density``(低基数 +
  报警语义价值不明)+ ``ru_x_ban_per_10k``(信号强,v2 w2-s2 新字段,
  旧指纹缺字段自动跳过)。``intensity_rising_per_10k`` 12 词子集**不进
  生产**(过拟合 caveat,跨书泛化以 w4-s1 规模跑为真检验)。

设计要点
========

- **纯函数无状态**:caller(orchestrator preread)预读"早于当前章的
  approved 章 final 指纹升序序列"透传,本模块每次全量重放序列算连续
  计数 —— 30-100 章 dict 读毫秒级,无需持久化 streak 状态(无状态漂移
  风险 + 可离线重放对账标定脚本)。
- **WARN 接线语义**(对齐 timeline_monotonic S3 / profession_title A3
  先例):``run_validator_pipeline`` 内失败只 ``_log.warning`` 留痕,
  **不计入 pipeline 失败、不派 rewrite、不覆盖 verdict**;passed 时正常
  计入 validator_passed 汇总。
- **历史章无指纹(G5 前旧数据)→ 跳过不入序列**(对齐
  ``_resolve_cross_chapter_voice_context`` 跳过 no_fingerprint 的语义):
  旧数据洞不打断真实漂移检测;标定语料 30 章全有指纹,两者等价。
- **锚章维度字段缺失 / 锚值 ≤ 0 → 该维度静默跳过**(向后兼容防御;
  paragraph/sentence 两维自 G5 起必有,实际只有 ru_x_ban 观察维度会
  在旧语料上走此路径)。
"""

from __future__ import annotations

import statistics
from typing import Any, Final, Literal

import structlog

from src.core.rules.validators import ValidatorResult

_log = structlog.get_logger(__name__)


#: 报警维度 → 偏移带(w2-s1 标定 §3 拍板字面;改动须重跑标定脚本对账)。
TREND_ALERT_DIMENSIONS: Final[dict[str, float]] = {
    "paragraph_length_mean": 0.30,
    "sentence_length_mean": 0.20,
}

#: 观察维度(算偏移进 details,不参与报警判定)。
TREND_OBSERVE_DIMENSIONS: Final[tuple[str, ...]] = (
    "exclamation_density",
    "ru_x_ban_per_10k",
)

#: 连续同向章数阈值(标定 §2.3:N=3 免疫单/双章张力尖峰)。
TREND_CONSECUTIVE_N: Final[int] = 3

#: 锚窗口章数(头 3 个有指纹的 approved 章,取各维度中位数)。
TREND_ANCHOR_WINDOW: Final[int] = 3


def compute_trend_anchor(
    chapter_fingerprints: list[tuple[str, dict[str, float] | None]],
) -> dict[str, float] | None:
    """从升序章指纹序列计算全书锚(头 3 个有指纹章的各维度**中位数**)。

    :param chapter_fingerprints: ``(chapter_id, final 指纹)`` 升序列表
        (caller 预读;指纹 None 的章被跳过,不占锚窗口)。
    :return: ``{维度: 中位锚值}``;有指纹章不足 ``TREND_ANCHOR_WINDOW``
        → None(锚窗口未就位,检测整体 skip)。

    注:锚对**报警 + 观察全部维度**计算(观察维度旧指纹缺字段时,该
    维度锚缺失 → 观察输出自动跳过)。
    """
    anchored = [
        fp for _, fp in chapter_fingerprints if fp is not None
    ][:TREND_ANCHOR_WINDOW]
    if len(anchored) < TREND_ANCHOR_WINDOW:
        return None
    anchor: dict[str, float] = {}
    for dim in list(TREND_ALERT_DIMENSIONS) + list(TREND_OBSERVE_DIMENSIONS):
        values = [fp[dim] for fp in anchored if dim in fp]
        if len(values) < TREND_ANCHOR_WINDOW:
            continue  # 任一锚章缺该维度 → 该维度无锚(整维度跳过)
        median = float(statistics.median(values))
        if median > 0.0:
            anchor[dim] = median
    return anchor


def _replay_streak(
    *,
    series: list[float],
    anchor: float,
    band: float,
) -> tuple[int, Literal["up", "down"] | None, list[float]]:
    """重放偏移序列,返回(末端连续同向计数, 方向, 末端连续段的偏移率序列)。

    偏移率 = (value - anchor) / anchor;超 ``+band`` 记 up、低 ``-band``
    记 down、带内断开清零。序列应为"锚窗口之后"的逐章值(含当前章在末)。
    """
    streak = 0
    direction: Literal["up", "down"] | None = None
    streak_devs: list[float] = []
    for value in series:
        deviation = (value - anchor) / anchor
        step: Literal["up", "down"] | None
        if deviation > band:
            step = "up"
        elif deviation < -band:
            step = "down"
        else:
            step = None
        if step is None:
            streak, direction, streak_devs = 0, None, []
        elif step == direction:
            streak += 1
            streak_devs.append(deviation)
        else:
            streak, direction, streak_devs = 1, step, [deviation]
    return streak, direction, streak_devs


def check_style_trend(
    *,
    chapter_fingerprints: list[tuple[str, dict[str, float] | None]],
    current_chapter_id: str,
    current_fingerprint: dict[str, float] | None,
) -> ValidatorResult:
    """[WARN] 跨章风格趋势检测(全书锚慢漂层)。

    :param chapter_fingerprints: **早于当前章**的 approved/published 章
        ``(chapter_id, final 指纹)`` 升序列表(caller 预读,指纹从
        ``ChapterVersionORM.style_fingerprint`` current_version 行读 —
        与 w2-s1 标定同源口径)。
    :param current_chapter_id: 当前章 chapter_id(details 标识)。
    :param current_fingerprint: 当前章本次审阅版本(vN)的指纹;None →
        整体 skip(旧 caller / 异常路径向后兼容)。
    :return: ``ValidatorResult(name="style_trend")``。``passed=False``
        表示**至少一个报警维度连续 ``TREND_CONSECUTIVE_N`` 章同向超带**
        (details 含维度/锚/连续章数/偏移率序列);接线层(
        ``run_validator_pipeline``)对失败走 WARN-only 留痕,不派 rewrite。

    skip 路径(全部 ``passed=True`` + ``details.skipped_reason``):
    - ``current_fingerprint=None``
    - 有指纹历史章不足锚窗口 3 章(全书前期)
    - 锚窗口后无历史章(当前章是第 4 章前)—— 此时当前章是锚后首章,
      streak 最多 1,数学上不可能触发,直接走正常路径输出观察数据。
    """
    if current_fingerprint is None:
        return ValidatorResult(
            name="style_trend",
            passed=True,
            details={"skipped_reason": "no_current_fingerprint"},
        )

    anchor = compute_trend_anchor(chapter_fingerprints)
    if anchor is None:
        return ValidatorResult(
            name="style_trend",
            passed=True,
            details={
                "skipped_reason": "anchor_window_not_ready",
                "anchor_window": TREND_ANCHOR_WINDOW,
            },
        )

    # 锚窗口之后的历史章(有指纹)+ 当前章 = 重放序列。
    anchored_history = [
        (cid, fp) for cid, fp in chapter_fingerprints if fp is not None
    ]
    post_anchor = anchored_history[TREND_ANCHOR_WINDOW:]

    alerts: list[dict[str, Any]] = []
    dimensions: dict[str, Any] = {}
    for dim, band in TREND_ALERT_DIMENSIONS.items():
        if dim not in anchor or dim not in current_fingerprint:
            continue
        series = [fp[dim] for _, fp in post_anchor if dim in fp]
        series.append(float(current_fingerprint[dim]))
        streak, direction, streak_devs = _replay_streak(
            series=series, anchor=anchor[dim], band=band
        )
        current_dev = (float(current_fingerprint[dim]) - anchor[dim]) / anchor[dim]
        dim_detail: dict[str, Any] = {
            "anchor": round(anchor[dim], 4),
            "band": band,
            "current_value": round(float(current_fingerprint[dim]), 4),
            "current_deviation_ratio": round(current_dev, 4),
            "consecutive_chapters": streak,
            "direction": direction,
            "deviation_series": [round(d, 4) for d in streak_devs],
        }
        dimensions[dim] = dim_detail
        if streak >= TREND_CONSECUTIVE_N:
            alerts.append({"dimension": dim, **dim_detail})

    # 观察维度:只算当前偏移,不报警(锚缺失 / 字段缺失 → 跳过)。
    observed: dict[str, Any] = {}
    for dim in TREND_OBSERVE_DIMENSIONS:
        if dim not in anchor or dim not in current_fingerprint:
            continue
        current_dev = (float(current_fingerprint[dim]) - anchor[dim]) / anchor[dim]
        observed[dim] = {
            "anchor": round(anchor[dim], 4),
            "current_value": round(float(current_fingerprint[dim]), 4),
            "current_deviation_ratio": round(current_dev, 4),
        }

    details: dict[str, Any] = {
        "current_chapter_id": current_chapter_id,
        "anchor_stat": "median",
        "consecutive_n": TREND_CONSECUTIVE_N,
        "dimensions": dimensions,
        "observed": observed,
    }
    if alerts:
        details["alerts"] = alerts
        return ValidatorResult(name="style_trend", passed=False, details=details)
    return ValidatorResult(name="style_trend", passed=True, details=details)


__all__ = [
    "TREND_ALERT_DIMENSIONS",
    "TREND_ANCHOR_WINDOW",
    "TREND_CONSECUTIVE_N",
    "TREND_OBSERVE_DIMENSIONS",
    "check_style_trend",
    "compute_trend_anchor",
]
