"""Outline 级 [HARD] 硬约束集中点(P5-B-04 引入)。

对齐 PROJECT_SPEC §4.1 字面"[HARD] 副本类剧情 ≥ 3 章" + ROADMAP §P5 字面
"张力曲线 [HARD] 校验(副本横跨规则)" + p5_plan.md §8 outline:commit gate
嵌入约定。

与 ``src/core/rules/validators.py``(章节级校验集中点,P4-B-06 引入)
**并列**:章节级在 chapter 生成 / review 阶段触发,outline 级在
``POST /outline:commit`` 端点 service 层入口触发(对齐 P4-acceptance
``ChapterService._run_gates`` 模式)。**任一 gate 失败 = service 抛
``OutlineGateViolation``,路由层翻译为 422 ``OUTLINE_ARC_TOO_SHORT``**
+ details 列违规 arc 区间。

设计要点
========

- **scan-only 函数**(不抛异常):返 ``list[OutlineValidationFailure]``,
  service 层判 ``if failures: raise OutlineGateViolation(failures)``。
  对齐 ``check_*`` 系列函数模式 + 测试可直接断言 failures 列表内容。
- **"副本"匹配走子串包含**(``"副本" in arc.arc_type``):``arc_type``
  是 LLM 自由文本(P5-B-01 决策 — 不做 Literal 限定),常见值"主线 /
  副本 / 支线 / 番外"。子串匹配兼容 "支线副本" / "幻境副本" 等组合表述,
  仍命中规则。**大小写敏感**(中文字符无大小写概念,无需 fold)。
- **`OutlineValidationFailure` Pydantic v2**:字段集合对齐 structlog
  ``outline_commit_gate_failed`` 事件 + 422 ``error.details.violations``
  payload(``rule`` / ``volume_id`` / ``arc_id`` / ``arc_type`` /
  ``chapter_range`` / ``span_length``)。``rule`` 走 Literal,P6+ 新硬
  约束(例如"长伏笔最大跨度"等)再扩字面值。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from src.core.models.outline import VolumePlanItem

OutlineRule = Literal["arc_too_short", "volume_seam_replay"]
"""Outline 级硬约束规则 ID 枚举。

- ``arc_too_short``:[HARD] 副本类剧情 ≥ 3 章(PROJECT_SPEC §4.1)。
  ``arc_type`` 含子串 "副本" 时,``end - start + 1 < 3`` 视为违规。
- ``volume_seam_replay``:[HARD] 卷界事件重演(v1.1 acceptance 修复1,
  2026-06-10)。新卷首章 mini_plan 重演前卷末章已完结事件 → 该章
  must_achieve 写作期永不可达成 → rewrite budget 耗尽死锁(luo-accept-v1
  ch-0007 现场)。判定主体 = ``volume_seam_replay_validator``(粗筛 + LLM
  判,agents 层);本模块只放违规模型。

P6+ 新 outline 级硬约束在此 Literal 扩字面值,validators 函数也在本模块
集中(避免散落到各 service)。
"""


class OutlineValidationFailure(BaseModel):
    """单条 outline 级硬约束违规记录。

    给 service 层判定是否抛 ``OutlineGateViolation``;给路由层翻译
    422 ``error.details.violations`` payload;给 structlog
    ``outline_commit_gate_failed`` 事件 field 同源。
    """

    rule: OutlineRule = Field(..., description="违规的硬约束规则 ID")
    volume_id: str = Field(..., description="违规所在卷的 volume_id")
    arc_id: str = Field(..., description="违规所在弧的 arc_id")
    arc_type: str = Field(..., description="违规弧的类型字面值(自由文本)")
    chapter_range: tuple[int, int] = Field(
        ..., description="违规弧的章节闭区间 (start, end)"
    )
    span_length: int = Field(
        ..., ge=1, description="违规弧的连续章节数(= end - start + 1)"
    )


class SeamReplayGateFailure(BaseModel):
    """卷界事件重演违规记录(v1.1 acceptance 修复1,2026-06-10)。

    与 ``OutlineValidationFailure`` 并列进 ``OutlineGateViolation.failures``;
    路由层 ``model_dump`` 同源序列化进 422 ``error.details.violations``。
    """

    rule: Literal["volume_seam_replay"] = "volume_seam_replay"
    prev_chapter_id: str = Field(..., description="前一卷末章 chapter_id")
    next_chapter_id: str = Field(..., description="新卷首章 chapter_id")
    confidence: float = Field(..., ge=0.0, le=1.0, description="judge 置信度")
    evidence: str = Field(..., description="judge 判定依据(中文)")


def validate_arc_chapter_spans(
    volume_plans: list[VolumePlanItem],
) -> list[OutlineValidationFailure]:
    """扫所有卷的 ``arcs[*]``,arc_type 含"副本" + 段长 < 3 时返违规记录。

    :param volume_plans: ``VolumePlansOutput.volumes`` 列表(已通过 Pydantic
        基础校验:arc 区间连续不重叠不超界,见
        ``VolumePlanItem._validate_arcs_cover_chapter_count``)。
    :return: 违规记录列表(空列表 = 全 pass)。

    设计:**只扫不抛**,caller 判 ``if failures`` 决定是否阻断。规则字符串
    走子串匹配(``"副本" in arc.arc_type``),兼容"主副本"/"支线副本" 等
    复合表述。
    """
    failures: list[OutlineValidationFailure] = []
    for vol in volume_plans:
        for arc in vol.arcs:
            if "副本" not in arc.arc_type:
                continue
            start, end = arc.chapter_range
            span = end - start + 1
            if span < 3:
                failures.append(
                    OutlineValidationFailure(
                        rule="arc_too_short",
                        volume_id=vol.volume_id,
                        arc_id=arc.arc_id,
                        arc_type=arc.arc_type,
                        chapter_range=(start, end),
                        span_length=span,
                    )
                )
    return failures


def format_arc_span_failures_for_retry(
    failures: list[OutlineValidationFailure],
) -> str:
    """把副本跨度硬 gate 失败格式化为卷规划 retry / 用户可读反馈。"""

    lines = [
        "outline:commit 硬规则失败:凡 arc_type 含'副本'的 arc,章节跨度必须 >= 3 章。",
        "请只重出 volume_plans/卷内细化分配,保留 world / characters / plot 上游事实。",
        "修复要求:调整相关 arc 的 chapter_range 与 beats 区间,让 arcs 连续不重叠覆盖全卷。",
        "违规明细:",
    ]
    for failure in failures:
        start, end = failure.chapter_range
        lines.append(
            f"- volume_id={failure.volume_id}, arc_id={failure.arc_id}, "
            f"arc_type={failure.arc_type}, chapter_range=[{start},{end}], "
            f"span={failure.span_length};需扩到至少 3 章。"
        )
    return "\n".join(lines)


__all__ = [
    "format_arc_span_failures_for_retry",
    "OutlineRule",
    "OutlineValidationFailure",
    "SeamReplayGateFailure",
    "validate_arc_chapter_spans",
]
