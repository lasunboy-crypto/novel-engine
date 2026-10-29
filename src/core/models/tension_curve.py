"""张力曲线模板字典(4 档,对应 FRONTEND_SPEC §4.2 + ROADMAP §P5)。

P5-B-01 内置 4 模板,P5-B-09 ``volumes:apply-template`` 端点根据 ``Volume.chapter_count``
缩放采样填充 ``Volume.tension_curve``。

设计要点:

1. 4 模板分别对应 FRONTEND_SPEC §4.2 字面"英雄之旅 / 平稳日常 / 强刺激爽文 / 自由":
   - ``heros_journey``:经典三幕结构,起承转合,前 20% 起,40% 转折,80% 高潮,最后 20% 收束
   - ``steady_daily``:平稳上升 + 小波动,适合日常向题材
   - ``strong_stimulation``:高频率强刺激,每 2-3 章一个小高潮
   - ``free``:基线 5 不主动起伏,完全由用户编辑
2. 模板**以 11 个样本点**(对应卷 0%/10%/20%/.../100% 进度)表达;``Volume.chapter_count``
   决定缩放后曲线点数。``apply_template(template_name, chapter_count)`` 工具函数返
   对应章数张力序列(1-N)。
3. **样本点张力值 1-10 整数**(对齐 ``TensionCurvePoint.tension`` 与 ``ChapterCard.tension_level``)。
4. 模板的最后一点(卷末)反映该模板的"高潮位",前端 VolumePlanPage 张力曲线渲染时
   读取此字典。
"""

from typing import Literal

TensionTemplateName = Literal[
    "heros_journey",
    "steady_daily",
    "strong_stimulation",
    "free",
]


TENSION_TEMPLATES: dict[TensionTemplateName, tuple[int, ...]] = {
    "heros_journey": (3, 4, 5, 6, 5, 6, 7, 7, 8, 9, 6),
    "steady_daily": (4, 4, 5, 5, 5, 6, 5, 6, 5, 6, 5),
    "strong_stimulation": (5, 7, 6, 8, 6, 9, 6, 9, 7, 10, 8),
    "free": (5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5),
}
"""4 张力模板的 11 个采样点(卷进度 0%/10%/.../100%);每点张力 1-10 整数。

应用时由 ``apply_template(name, chapter_count)`` 线性插值缩放到 ``chapter_count``
章长度;``free`` 模板返恒等 5 序列,等价"用户自由编辑起点"。
"""


def apply_template(
    template_name: TensionTemplateName, chapter_count: int
) -> list[int]:
    """将命名模板缩放到指定章节数,返每章张力 1-10 整数列表(长度 == chapter_count)。

    缩放策略:**线性插值**到 ``chapter_count`` 长度,结果 round 到最近整数再 clamp
    到 [1, 10] 区间。``chapter_count == 1`` 时返模板中点;``chapter_count >= 2``
    时端点对齐模板首末,中间均匀插值。

    Args:
        template_name: 模板名(4 档之一)
        chapter_count: 卷内章节总数,>= 1

    Returns:
        长度 == ``chapter_count`` 的整数张力序列,每个元素 1-10。

    Raises:
        ValueError: 模板名不在 4 档枚举内;chapter_count < 1。
    """
    if template_name not in TENSION_TEMPLATES:
        raise ValueError(
            f"Unknown tension template: {template_name!r}; "
            f"must be one of {tuple(TENSION_TEMPLATES.keys())}"
        )
    if chapter_count < 1:
        raise ValueError(
            f"chapter_count must be >= 1, got {chapter_count}"
        )

    samples = TENSION_TEMPLATES[template_name]
    n_samples = len(samples)

    if chapter_count == 1:
        return [_clamp(samples[n_samples // 2])]

    result: list[int] = []
    for i in range(chapter_count):
        # 第 i 章(0-indexed)对齐模板采样位置 t in [0, n_samples-1]
        t = i * (n_samples - 1) / (chapter_count - 1)
        lo = int(t)
        hi = min(lo + 1, n_samples - 1)
        frac = t - lo
        value = samples[lo] * (1 - frac) + samples[hi] * frac
        result.append(_clamp(round(value)))
    return result


def _clamp(value: int) -> int:
    """夹紧到 [1, 10] 整数范围。"""
    return max(1, min(10, int(value)))
