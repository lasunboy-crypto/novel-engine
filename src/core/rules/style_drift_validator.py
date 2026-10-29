"""[HARD] 风格漂移 validator(v1.1 wave 1 session 4 引入,G5 治本下游)。

职责:对比 baseline 风格指纹(首版 v1)vs 当前版本指纹(rewrite vN)。健康 v1
的段落均值/句长均值承担 rewrite-only 保序 HARD；若 v1 维度本身相对前章
异常，该维只记 WARN，不把前章参考升级为新 HARD。其余统计维度也只记 WARN。

调用时机
========

仅在 **rewrite 路径下且 v1 有 style_fingerprint** 时触发(``vN >= 2``):

- ``v1`` 本身不校(baseline 就是它自己)
- ``v1.style_fingerprint is None``(旧数据未抽取)→ ``passed=True`` + log warn
  (向后兼容,**fail-open** 对齐 ``check_must_achieve`` LLM 失败同模式)
- ``vN >= 2`` 且 ``baseline_fingerprint`` 有值 → 对比 4 维度偏移度

阈值字面(v1.1 wave 1 session 4 自决项)
=========================================

**每维度独立阈值**；只有健康 v1 的两项会令 validator fail，其余只观测:

- **rewrite-only HARD** 健康 v1 段落长度均值偏移 > **50%**
  (``paragraph_length_mean``；G17 异常 v1 维度降 WARN)
- **WARN** 段落长度标准差偏移 > **40%**(``paragraph_length_std`` — 风格变化往往先体
  现为分布扩散 / 收紧)
- **rewrite-only HARD** 健康 v1 句长均值偏移 > **30%**
  (``sentence_length_mean``；G17 异常 v1 或被 word-count trim 改写的 current
  例外降为 WARN)
- **WARN** 句长标准差偏移 > **40%**(``sentence_length_std``)
- **WARN** 人称代词频次偏移 > **10%**(主导人称另由跨章 voice 硬闸负责)
- **WARN** 修辞密度(``simile_density`` / ``exclamation_density``)偏移 > **50%**(修辞
  本身波动大,阈值给宽)

阈值选择理由(1st/2nd/3rd 排序):

- **1st 质量收益**:对齐 luo-cloud-test ch-21 v1→v4 实证(段落长度均值
  ~180→~85 偏移 53% / 第一人称占比 ~5%→~15% 偏移 200%)— 这两类漂移应被本
  validator 100% 截获
- **2nd 风险对冲**:阈值偏宽避免短篇 / 张力章自然波动误判;真正的风格漂移
  偏移幅度远超阈值(实证 50%+),宽阈值不会漏判
- **3rd 工程便利**:每维度独立阈值字面可后续微调,不依赖合成公式

baseline 版本选择(v1.1 wave 1 session 4 自决项)
=================================================

默认固定取 **v1**(首版 ``creation_type="draft"``)，但 G17 判为异常的具体
维度退出保序 HARD；前章值只用于异常判定和审计，不替换 v1:

- **1st 质量收益**:首版是 LLM 最自然的输出,作为 baseline 锚定原始风格;
  rewrite 4 次后越改越偏,baseline 选最高分版本反而锁定漂移后的风格
- **2nd 风险对冲**:不选 approved(终态)— v2+ 校验需在 approved 前发生
  (approved 后已无 rewrite 机会)
- **3rd 工程便利**:caller 只需 ``session.get(ChapterVersionORM, (chapter_id, 1))``
  拿 v1,无需查询逻辑
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

import structlog

from src.core.rules.prose_hard_contract import (
    PARAGRAPH_MEAN_HARD_MIN,
    RewriteStyleConstraintSpec,
)
from src.core.rules.validators import ValidatorResult

_log = structlog.get_logger(__name__)


# ===== 阈值常量(字面 SSOT,可后续微调)=====

#: 段落长度均值偏移阈值(超 50% 视作漂移)。
#:
#: v1.1 wave 1 session 10 / G15 起由 0.30 上调到 0.50:实证发现 LLM 在 G11
#: prompt v6 rewrite feedback 下**无法精确控制段落均值到 baseline ±30%**
#: (luo-clean-v1 rerun-3 ch-5 振荡 94→48→82→33 4 versions 全 fail 撞 budget)。
#: 50% 阈值给 LLM 单次振荡空间,同时 G5 检测的是同章 rewrite 漂移(v1 → vN),
#: 不是跨章节漂移,50% 阈值仍能抓"3 次振荡同方向累积"或"真大幅风格切换"。
#: G10 跨章节 paragraph drift 同步改 warn-only(参见
#: ``cross_chapter_voice_validator`` G15 sections)。
PARAGRAPH_LENGTH_MEAN_DRIFT_THRESHOLD: Final[float] = 0.50

#: 段落长度标准差偏移阈值(超 40% 视作漂移)
PARAGRAPH_LENGTH_STD_DRIFT_THRESHOLD: Final[float] = 0.40

#: 句长均值偏移阈值(超 30% 视作漂移)
SENTENCE_LENGTH_MEAN_DRIFT_THRESHOLD: Final[float] = 0.30

#: 句长标准差偏移阈值(超 40% 视作漂移)
SENTENCE_LENGTH_STD_DRIFT_THRESHOLD: Final[float] = 0.40

#: 人称代词频次偏移阈值(超 10% 视作显著主语切换)
PRONOUN_FREQ_DRIFT_THRESHOLD: Final[float] = 0.10

#: 修辞密度偏移阈值(超 50% 视作修辞风格漂移)
RHETORICAL_DENSITY_DRIFT_THRESHOLD: Final[float] = 0.50

#: v1 基线异常检测阈值(G17 范式 SSOT,2026-06-10 v1.1 acceptance 修复1 上移)。
#:
#: v1 ``paragraph_length_mean`` / ``sentence_length_mean`` 偏离前一章 final
#: approved 基线超过该比例 → 该维度不能再充当 rewrite 的可靠保序硬尺。
#:
#: R9 ch-0002 证明旧 G17 的“改锚前章并升级为 HARD”会把初稿只收到的跨章
#: WARN 偷换成 rewrite 新硬闸:v1 实际因 setting 被打回,段均 122 vs 前章 62
#: 只应告警,但 v2-v4 被临时出现的前章 ±50% 硬尺连续拦截。当前语义改为:
#:
#: - Writer 明示真正失败项优先,异常维只作 WARN/软参考;
#: - style_drift 对同一异常维降为 WARN-only,其余健康维仍以本章 v1 为 HARD;
#: - 前章数值绝不因“进入 rewrite”自动从跨章 WARN 升级成同章 HARD。
#:
#: Writer 与闸门仍必须共用同一异常明细和本阈值,保证“提示降级什么、闸门就
#: 降级什么”。
V1_BASELINE_ANOMALY_THRESHOLD: Final[float] = 0.50

#: v1 段落 mode collapse 绝对阈值(v2 间隙 2026-06-13 加,对齐 G22b
#: 共享硬底线)。**异常维判定是双条件**(见
#: ``is_v1_baseline_anomalous``):段均**向下偏离前章 >50% 且绝对值 < 本阈值**
#: 才算 collapse —— 本常量是其中"绝对超短"那半。**为何**:G17 从对称偏离改
#: "向上触发"后(问题② 修,治高潮章短段被全书锚拽长),仍需识别"v1 段落崩坏成
#: 超短"并把该不可靠维度降 WARN；真正打回和修复目标来自同一个
#: ``paragraph_mean_floor`` HARD，而不是临时改锚前章。**与高潮章戏剧短段
#: (58.9 偏 −60% 但 ≥30)
#: + 阈值边界(27 偏 −49% < 50%)都区分**:前者绝对健康、后者偏离未超阈,均不
#: 该降级。
_V1_COLLAPSE_PARAGRAPH_MEAN: Final[float] = PARAGRAPH_MEAN_HARD_MIN

#: G17 异常检测覆盖维度(v2 w2-s2 扩 — 原只 paragraph_length_mean,补 sentence_length_mean)。
#: **为何扩句均**:drift-ab-off-v1 ch-0005 现场实证,v1 句均 60.6(畸形超长句)
#: 偏前章 37.9 达 +60% > 50%,但段均 147.8 偏前章 175.1 仅 -16% 不到 50% →
#: 原"只看段均"的 G17 漏判 → 闸门锚畸形 v1 句均 → writer 补字数时句均反复横跳
#: (36/96/31)永够不着 v1=60.6 的 ±30% 窗口 → rewrite budget 耗尽 human_intervention。
#: 扩句均后 v1 句均异常即与段均一样退出同章保序 HARD，避免锁住畸形 v1，
#: 同时不把前章 WARN 升级成新硬闸。
_V1_ANOMALY_DIMENSIONS: Final[
    tuple[Literal["paragraph_length_mean", "sentence_length_mean"], ...]
] = (
    "paragraph_length_mean",
    "sentence_length_mean",
)


@dataclass(frozen=True, slots=True)
class V1BaselineAnomaly:
    """G17 单个真实触发维度，供闸门判定与 Writer 提示共用。"""

    dimension: Literal["paragraph_length_mean", "sentence_length_mean"]
    reason: Literal["inflated", "paragraph_collapse"]
    v1_value: float
    baseline_value: float
    relative_delta: float


def quantize_prompt_visible_style_mean(value: float) -> float:
    """把均值量化为 Writer 提示词实际显示的整数锚。

    Writer 的 rewrite 基线使用 ``{value:.0f}``。同章 style-drift 的两个
    HARD 均值必须按模型真正看到的锚计算；current 仍保留原始测量值。
    """

    return float(format(value, ".0f"))


def v1_baseline_anomaly_details(
    v1_fingerprint: dict[str, float],
    prev_chapter_fingerprint: dict[str, float],
) -> tuple[V1BaselineAnomaly, ...]:
    """返回 G17 真正触发的维度；空 tuple 表示 v1 可继续作 rewrite 锚。

    这是闸门与 Writer 对哪些 v1 维度降为 WARN-only 的共同 SSOT。调用方
    不得再从段均单独重算一套提示文字，否则句均单独触发时会错报段均，
    零分母路径也可能在渲染阶段重新除零。

    v1 段均或句均任一向上膨胀超
    ``V1_BASELINE_ANOMALY_THRESHOLD`` 会返回 ``inflated``；段均显著向下且
    绝对值低于 ``_V1_COLLAPSE_PARAGRAPH_MEAN`` 会返回
    ``paragraph_collapse``。某维度任一侧小于等于 0 时跳过。

    详细的非对称判定缘由见 ``is_v1_baseline_anomalous`` 文档。
    """
    anomalies: list[V1BaselineAnomaly] = []
    for dim in _V1_ANOMALY_DIMENSIONS:
        v1_value = v1_fingerprint.get(dim, 0.0)
        prev_value = prev_chapter_fingerprint.get(dim, 0.0)
        if v1_value <= 0.0 or prev_value <= 0.0:
            continue
        delta = (v1_value - prev_value) / prev_value
        if delta > V1_BASELINE_ANOMALY_THRESHOLD:
            anomalies.append(
                V1BaselineAnomaly(
                    dimension=dim,
                    reason="inflated",
                    v1_value=v1_value,
                    baseline_value=prev_value,
                    relative_delta=delta,
                )
            )
        elif (
            dim == "paragraph_length_mean"
            and delta < -V1_BASELINE_ANOMALY_THRESHOLD
            and v1_value < _V1_COLLAPSE_PARAGRAPH_MEAN
        ):
            anomalies.append(
                V1BaselineAnomaly(
                    dimension="paragraph_length_mean",
                    reason="paragraph_collapse",
                    v1_value=v1_value,
                    baseline_value=prev_value,
                    relative_delta=delta,
                )
            )
    return tuple(anomalies)


def is_v1_baseline_anomalous(
    v1_fingerprint: dict[str, float],
    prev_chapter_fingerprint: dict[str, float],
) -> bool:
    """G17 共享布尔判定；具体维度由 ``v1_baseline_anomaly_details`` 给出。

    Writer 与闸门必须共用 ``v1_baseline_anomaly_details``；本函数只是保留给
    既有布尔调用方的兼容包装。共同明细保证“Writer 把什么降 WARN、闸门也把
    什么降 WARN”，杜绝两把尺打架。

    某维度在 v1 或前章为 0(空 / 极短文本兜底)→ 该维度跳过(不计入异常)。
    任一被检维度向上超阈值即返 True。

    **v2 间隙 A1/问题② 修(2026-06-13):向上膨胀(任一维度)或段落 collapse(段均)触发**。
    根因:旧函数用对称偏移触发并把前章改成新硬锚；这会误伤健康戏剧短段，
    也会把跨章 WARN 偷升为 rewrite HARD。当前只识别不可靠维度并降 WARN:
    - 超长病态(卡超载 / 不可达成 must 挤成巨段,luo-accept-v1 ch-0007 v1=165
      偏前章 72 +129%)→ 触发异常维降级。✓ 向上膨胀分支(段均/句均任一)。
    - **段落 collapse 病态**(段均崩坏成超短,luo-clean-v1 ch-5 v1=13.5 偏前章
      53 −75%)→ 触发异常维降级；真正的绝对底线 HARD 负责打回。✓ collapse
      分支 = **段均向下偏离 >50% 且绝对值低于共享
        ``PARAGRAPH_MEAN_HARD_MIN``**(双条件)。
    - **健康戏剧短段**(高潮 / 动作场景,ch-0009 段均 58.9 偏全书锚 147.9 −60%,
      但绝对值 ≥30 健康)→ **不触发**:若拿长前章参考做 HARD = 强制 writer 往长重写 =
      反戏剧 HARD 致伤(确定性证明:全书锚 ON 时高潮章 rewrite 被 G17 判漂移
      强制拉长,最坏耗尽 budget 成孤儿章)。
    - **阈值边界以下**(v1 偏前章 49% < 50%,如 v1=27 vs 53)→ 不触发(原 G11
      行为):collapse 分支双条件要求**既偏离 >50% 又绝对 <30**,27 偏离未超阈
      不触发(区别于 13.5 偏离 75% + <30 触发)。
    关键:collapse 是"**显著向下偏离 + 绝对超短**"双条件,与"偏短但健康"
    (≥30 戏剧短段)和"轻偏离"(<50% 阈值)都区分开。某维度 v1/前章为 0(空 /
    极短兜底)→ 跳过(prev=0 时 collapse 分支也不触发 → 无除零,用 v1)。该修正
    **独立于基线类型**(前章锚 / 全书锚 / wave 4 张力锚下都正确)。
    """
    return bool(v1_baseline_anomaly_details(v1_fingerprint, prev_chapter_fingerprint))


def resolve_rewrite_style_constraint_spec(
    *,
    v1_fingerprint: dict[str, float] | None,
    cross_chapter_baseline_fingerprint: dict[str, float] | None,
) -> RewriteStyleConstraintSpec | None:
    """把既有 G17/G5 判据解析成计划编译器可消费的机械参数。

    Writer 的详细修复指导与写后 ``check_style_drift`` 仍沿用本模块的同一阈值和
    异常判定；可执行计划只冻结解析结果，绝不另算一套边界。
    """

    if v1_fingerprint is None:
        return None
    anomalies = (
        v1_baseline_anomaly_details(v1_fingerprint, cross_chapter_baseline_fingerprint)
        if cross_chapter_baseline_fingerprint is not None
        else ()
    )
    exempted = {item.dimension for item in anomalies}
    paragraph_baseline = quantize_prompt_visible_style_mean(
        v1_fingerprint.get("paragraph_length_mean", 0.0)
    )
    sentence_baseline = quantize_prompt_visible_style_mean(
        v1_fingerprint.get("sentence_length_mean", 0.0)
    )
    return RewriteStyleConstraintSpec(
        paragraph_baseline=(paragraph_baseline if paragraph_baseline > 0.0 else None),
        paragraph_threshold=PARAGRAPH_LENGTH_MEAN_DRIFT_THRESHOLD,
        paragraph_hard=(
            paragraph_baseline > 0.0 and "paragraph_length_mean" not in exempted
        ),
        sentence_baseline=(sentence_baseline if sentence_baseline > 0.0 else None),
        sentence_threshold=SENTENCE_LENGTH_MEAN_DRIFT_THRESHOLD,
        sentence_hard=(
            sentence_baseline > 0.0 and "sentence_length_mean" not in exempted
        ),
    )


#: 代词出现绝对次数下限(任一代词出现 < 该值即视作 noise,跳过偏移检测)。
#: 防御稀疏代词样本被相对偏移阈值误判 — 真实风格漂移在 2500+ 字章节内代词
#: 出现次数 ≥ 5 才有统计意义。
PRONOUN_MIN_ABSOLUTE_COUNT: Final[int] = 5

#: 段落 / 句子标准差稳定样本数下限(< 该值视作 noise,跳过 std 检测)。
#: 总体标准差在样本数 < 5 时统计噪声大,极易被相对偏移阈值误判;真实
#: 长章节(2500+ 字)段落 ≥ 5 / 句子 ≥ 20,远超此下限。短样本(单元
#: 测试 fixture / 异常路径)的 std 仍参与对比会触发误判。
STD_STABILITY_MIN_SAMPLES: Final[int] = 5

#: 全局短文本兜底:baseline 或 current 字数 < 该值视作短样本不做漂移检测
#: 直接 passed。真实长篇章节 word_count_min ≥ 1500-2500 字,远超此下限;
#: 短样本(单元测试 fixture / 异常路径)直接绕过整个
#: validator,避免人造短文本误判。
MIN_CHARS_FOR_DRIFT_CHECK: Final[int] = 500

#: 全局长文本兜底:baseline 或 current 字数 > 该值视作非真实章节(典型场景:
#: MockLLMClient 写 100M+ 字测试 fixture / 异常输出污染)直接 passed。真实
#: 长篇单章字数上限 ChapterCard.word_count_max ≤ 10000 字,远低于此值;只
#: 有 mock 路径 / 异常路径会撞这个上限。绕过 validator 避免人造超长文本
#: 不同段落结构(``\n`` 数量差异)被 std 维度误判为风格漂移。
MAX_REALISTIC_CHAPTER_CHARS: Final[int] = 1_000_000

#: 9 种人称代词,与 ``style_fingerprint.py`` 一致
_PRONOUNS: Final[tuple[str, ...]] = (
    "我",
    "你",
    "他",
    "她",
    "它",
    "我们",
    "你们",
    "他们",
    "她们",
)


def check_style_drift(
    *,
    baseline_fingerprint: dict[str, float] | None,
    current_fingerprint: dict[str, float],
    baseline_version_id: int = 1,
    current_version_id: int | None = None,
    cross_chapter_baseline_fingerprint: dict[str, float] | None = None,
    cross_chapter_baseline_chapter_id: str | None = None,
    current_word_count_trimmed: bool = False,
) -> ValidatorResult:
    """[HARD] 对比 baseline vs current 风格指纹偏移度,超阈值任一维度 → fail。

    :param baseline_fingerprint: 首版 v1 抽取的风格指纹;``None`` → fail-open
        (旧数据无指纹,passed=True + log warn)
    :param current_fingerprint: 当前 vN 抽取的风格指纹(必给)
    :param baseline_version_id: 用于 details 字段标识,默认 ``1``
    :param current_version_id: 用于 details 字段标识,可选
    :param cross_chapter_baseline_fingerprint: 前一章 final approved 指纹
        (G12 grandparent 解析产物,orchestrator 已为 G10 预读)。非 None 且
        v1 段均 / 句均命中 ``v1_baseline_anomaly_details`` 时，对应维度退出
        rewrite 保序 HARD、降为 WARN-only；前章值只作异常判定与审计参考，
        不替换成新硬锚。``None`` 时维持 v1 保序原行为。
    :param cross_chapter_baseline_chapter_id: 前一章 chapter_id,仅用于
        details / 日志标识。
    :param current_word_count_trimmed: v2 W3.5-s7 trim×style_drift 修复
        (governance §2.47)。current 版本被 ``word_count_trim`` 改写过 →
        ``sentence_length_mean`` 维度降为 **warn-only**(不阻塞 rewrite)。
        根因:``word_count_trim`` 是受控 LLM 后处理(整段砍水 + 衔接改顺),
        **必然重写句长结构**(deepseek 长句被正常化到接近健康锚);而 baseline
        (v1)若未触发 trim(自然 ≤cap)则保留原始长句 → 两版"长度治理状态"
        不一致 → 句均偏移被误判风格漂移 → rewrite → deepseek 又写长 → trim
        再正常化 → 死循环耗尽 budget 升人工(s7-accept ch-0010 实证:v1 句均
        47.1 未 trim,v2/v3 trim 后 29.9/28.7 偏移 0.365/0.392 > 0.3 全 fail,
        而未 trim 的 v4 句均 36.5 反而过)。**仅句均豁免,段落均值硬约束保留**
        (mode collapse 真信号不放);**仅 trim 触发的版本豁免**,未 trim 版本
        全维度照常检测(防真句长漂移漏放)。
    :return: ``ValidatorResult(name="style_drift", ...)``
    """
    if baseline_fingerprint is None:
        _log.warning(
            "style_drift_validator_baseline_missing",
            current_version_id=current_version_id,
            reason="baseline_fingerprint_none_fail_open",
        )
        return ValidatorResult(
            name="style_drift",
            passed=True,
            details={
                "baseline_version_id": baseline_version_id,
                "current_version_id": current_version_id,
                "skipped": "baseline_missing",
            },
        )

    # ===== R9:G17 因果路由修复 =====
    # 旧语义把异常维改锚前章，并在 rewrite 时升级为同章 HARD。R9 ch-0002
    # 证明这会凭空改变初稿的打回原因:setting 才是 v1 的 HARD 失败，段均跨章
    # 偏移只是 WARN，却在 v2 起变成 62±50% 的新硬闸。正确语义是“不可靠的
    # v1 维度不参与保序硬判”，而不是“把前章 WARN 偷换成 HARD”。健康维度仍
    # 以 v1 保序，异常维继续计算并记 WARN，供观察与后续产品决策。
    baseline_exempted_dimensions: list[str] = []
    baseline_anomalies = (
        v1_baseline_anomaly_details(
            baseline_fingerprint, cross_chapter_baseline_fingerprint
        )
        if cross_chapter_baseline_fingerprint is not None
        else ()
    )
    if baseline_anomalies:
        assert cross_chapter_baseline_fingerprint is not None
        _log.warning(
            "style_drift_v1_anomalous_dimensions_warn_only",
            baseline_version_id=baseline_version_id,
            current_version_id=current_version_id,
            v1_paragraph_length_mean=baseline_fingerprint.get(
                "paragraph_length_mean", 0.0
            ),
            v1_sentence_length_mean=baseline_fingerprint.get(
                "sentence_length_mean", 0.0
            ),
            cross_chapter_paragraph_length_mean=cross_chapter_baseline_fingerprint.get(
                "paragraph_length_mean", 0.0
            ),
            cross_chapter_sentence_length_mean=cross_chapter_baseline_fingerprint.get(
                "sentence_length_mean", 0.0
            ),
            cross_chapter_baseline_chapter_id=cross_chapter_baseline_chapter_id,
            anomaly_dimensions=[
                {
                    "dimension": item.dimension,
                    "reason": item.reason,
                    "v1_value": item.v1_value,
                    "baseline_value": item.baseline_value,
                    "relative_delta": item.relative_delta,
                }
                for item in baseline_anomalies
            ],
            note=(
                "R9 G17 因果路由修复:v1 异常维不再把跨章 WARN 升级为 rewrite "
                "HARD;该维降为 WARN-only,真正失败项优先,健康维仍按 v1 保序"
            ),
        )
        baseline_exempted_dimensions = [item.dimension for item in baseline_anomalies]

    # 全局短文本兜底:baseline 或 current 字数 < MIN_CHARS_FOR_DRIFT_CHECK
    # 直接 passed,避免人造短文本 / 测试 fixture 触发误判
    baseline_chars = baseline_fingerprint.get("total_chars", 0.0)
    current_chars = current_fingerprint.get("total_chars", 0.0)
    if (
        baseline_chars < MIN_CHARS_FOR_DRIFT_CHECK
        or current_chars < MIN_CHARS_FOR_DRIFT_CHECK
    ):
        return ValidatorResult(
            name="style_drift",
            passed=True,
            details={
                "baseline_version_id": baseline_version_id,
                "current_version_id": current_version_id,
                "skipped": "short_text",
                "baseline_chars": baseline_chars,
                "current_chars": current_chars,
                "threshold": MIN_CHARS_FOR_DRIFT_CHECK,
            },
        )

    # 全局长文本兜底:baseline 或 current 字数 > MAX_REALISTIC_CHAPTER_CHARS
    # 直接 passed,避免 MockLLMClient 100M+ 字 fixture 触发误判
    if (
        baseline_chars > MAX_REALISTIC_CHAPTER_CHARS
        or current_chars > MAX_REALISTIC_CHAPTER_CHARS
    ):
        return ValidatorResult(
            name="style_drift",
            passed=True,
            details={
                "baseline_version_id": baseline_version_id,
                "current_version_id": current_version_id,
                "skipped": "unrealistic_long_text",
                "baseline_chars": baseline_chars,
                "current_chars": current_chars,
                "threshold": MAX_REALISTIC_CHAPTER_CHARS,
            },
        )

    drift_details: list[dict[str, object]] = []
    warn_observed: list[dict[str, object]] = []

    baseline_sentences = baseline_fingerprint.get("total_sentences", 0.0)
    current_sentences = current_fingerprint.get("total_sentences", 0.0)
    # 段落 paragraph 数推断:用 total_chars / max(paragraph_length_mean, 1)
    # 避免在 fingerprint 多加字段。短样本兜底 < STD_STABILITY_MIN_SAMPLES
    # 即跳过 std。
    baseline_para_mean = baseline_fingerprint.get("paragraph_length_mean", 0.0)
    current_para_mean = current_fingerprint.get("paragraph_length_mean", 0.0)
    baseline_total_chars = baseline_fingerprint.get("total_chars", 0.0)
    current_total_chars = current_fingerprint.get("total_chars", 0.0)
    baseline_para_count = (
        baseline_total_chars / baseline_para_mean if baseline_para_mean > 0 else 0
    )
    current_para_count = (
        current_total_chars / current_para_mean if current_para_mean > 0 else 0
    )

    # v1.1 audit G24c 治本(2026-05-27 b1-rerun-deep-audit + C 路径对照实验):
    # **paragraph_length_mean + sentence_length_mean 保留硬约束**(mode collapse
    # 真信号 — luo-clean-v1 b1-rerun-4 实证:v1 paraMean=15 collapsed 由此截获)。
    # 其他 5 维度(paragraph_length_std / sentence_length_std / pronoun_freq /
    # simile_density / exclamation_density)**改 warn-only**:rerun-5 v2-v5 实证
    # 显示这些维度的 drift 多为剧情自然差异(本章场景换人物 / 比喻多寡 / 段落
    # 分布扩散),硬阻塞 rewrite 反而触发 budget exhausted 死循环。

    # 段落长度均值 — 正常 v1 为保序 HARD；G17 异常 v1 维度降 WARN-only。
    paragraph_mean_sink = (
        warn_observed
        if "paragraph_length_mean" in baseline_exempted_dimensions
        else drift_details
    )
    _check_relative_drift(
        baseline=baseline_fingerprint,
        current=current_fingerprint,
        dim="paragraph_length_mean",
        threshold=PARAGRAPH_LENGTH_MEAN_DRIFT_THRESHOLD,
        drift_details=paragraph_mean_sink,
        comparison_baseline_value=quantize_prompt_visible_style_mean(
            baseline_para_mean
        ),
    )
    # 段落长度标准差 — G24c 改 warn-only(剧情自然方差差异不视为风格漂移)
    if (
        baseline_para_count >= STD_STABILITY_MIN_SAMPLES
        and current_para_count >= STD_STABILITY_MIN_SAMPLES
    ):
        _check_relative_drift(
            baseline=baseline_fingerprint,
            current=current_fingerprint,
            dim="paragraph_length_std",
            threshold=PARAGRAPH_LENGTH_STD_DRIFT_THRESHOLD,
            drift_details=warn_observed,
        )

    # 句长均值 — 硬约束(G24c 保留;短句模式也是 mode collapse 信号)。
    # v2 W3.5-s7 trim×style_drift 修复:current 被 word_count_trim 改写过时
    # 改 warn-only(进 warn_observed 而非 drift_details),不阻塞 rewrite —— trim
    # 重写句长结构是 by-design,baseline 若未 trim 则口径不一致,硬约束会死循环
    # (详 docstring + governance §2.47)。段落均值维度不豁免(mode collapse 真信号)。
    sentence_mean_sink = (
        warn_observed
        if (
            current_word_count_trimmed
            or "sentence_length_mean" in baseline_exempted_dimensions
        )
        else drift_details
    )
    _check_relative_drift(
        baseline=baseline_fingerprint,
        current=current_fingerprint,
        dim="sentence_length_mean",
        threshold=SENTENCE_LENGTH_MEAN_DRIFT_THRESHOLD,
        drift_details=sentence_mean_sink,
        comparison_baseline_value=quantize_prompt_visible_style_mean(
            baseline_fingerprint.get("sentence_length_mean", 0.0)
        ),
    )
    # 句长标准差 — G24c 改 warn-only
    if (
        baseline_sentences >= STD_STABILITY_MIN_SAMPLES
        and current_sentences >= STD_STABILITY_MIN_SAMPLES
    ):
        _check_relative_drift(
            baseline=baseline_fingerprint,
            current=current_fingerprint,
            dim="sentence_length_std",
            threshold=SENTENCE_LENGTH_STD_DRIFT_THRESHOLD,
            drift_details=warn_observed,
        )

    # 人称代词频次 9 项 — G24c 改 warn-only(同章人称漂移 G10 cross_chapter
    # dominant_pov_flipped 已硬截获,G5 本维度多数是剧情场景换人物导致的自然
    # 频次差异,鸡毛蒜皮触发 rewrite 死循环)
    baseline_total = baseline_fingerprint.get("total_chars", 0.0)
    current_total = current_fingerprint.get("total_chars", 0.0)
    for pronoun in _PRONOUNS:
        dim = f"pronoun_freq_{pronoun}"
        baseline_count = baseline_fingerprint.get(dim, 0.0) * baseline_total
        current_count = current_fingerprint.get(dim, 0.0) * current_total
        if (
            baseline_count < PRONOUN_MIN_ABSOLUTE_COUNT
            and current_count < PRONOUN_MIN_ABSOLUTE_COUNT
        ):
            continue
        _check_relative_drift(
            baseline=baseline_fingerprint,
            current=current_fingerprint,
            dim=dim,
            threshold=PRONOUN_FREQ_DRIFT_THRESHOLD,
            drift_details=warn_observed,
        )

    # 修辞密度 2 项 — G24c 改 warn-only(simile/exclamation 极小基数 0.01x,
    # 任何自然增减都超 50% drift;硬阻塞反触发 LLM 全删比喻 / 全删感叹号
    # 极端策略 — rerun-5 v3/v4 simile=0 现场)
    _check_relative_drift(
        baseline=baseline_fingerprint,
        current=current_fingerprint,
        dim="simile_density",
        threshold=RHETORICAL_DENSITY_DRIFT_THRESHOLD,
        drift_details=warn_observed,
    )
    _check_relative_drift(
        baseline=baseline_fingerprint,
        current=current_fingerprint,
        dim="exclamation_density",
        threshold=RHETORICAL_DENSITY_DRIFT_THRESHOLD,
        drift_details=warn_observed,
    )

    # G24c:warn 段单独 emit log,不入 ValidatorResult.passed 判定
    if warn_observed:
        _log.warning(
            "style_drift_warn_only_dimensions",
            baseline_version_id=baseline_version_id,
            current_version_id=current_version_id,
            warn_dimensions=warn_observed,
            note="G24c(2026-05-27 audit):次要维度漂移 warn-only,不阻塞 rewrite",
        )

    passed = not drift_details
    return ValidatorResult(
        name="style_drift",
        passed=passed,
        details={
            "baseline_version_id": baseline_version_id,
            "current_version_id": current_version_id,
            "drifted_dimensions": drift_details,
            "warn_observed_dimensions": warn_observed,
            # 兼容旧消费者保留 substituted 两字段，但产品路径不再换锚。
            "baseline_substituted": False,
            "baseline_substituted_dimensions": [],
            "baseline_exempted_dimensions": baseline_exempted_dimensions,
            "baseline_source": (
                "v1;g17_warn_only_dimensions="
                f"{','.join(baseline_exempted_dimensions)};"
                "cross_chapter_reference="
                f"{cross_chapter_baseline_chapter_id or 'unknown'}"
                if baseline_exempted_dimensions
                else "v1"
            ),
            # v2 W3.5-s7:句均维度是否因 current 被 word_count_trim 改写而降为
            # warn-only(详 docstring + governance §2.47)。审计 / 测试断言用。
            "sentence_mean_warn_only_due_to_trim": current_word_count_trimmed,
            # R8 ch-0003:Writer 看见的是整数均值锚；原始指纹继续保留供审计。
            "prompt_visible_mean_anchors": {
                dim: {
                    "raw_baseline_value": baseline_fingerprint.get(dim, 0.0),
                    "anchor_value": quantize_prompt_visible_style_mean(
                        baseline_fingerprint.get(dim, 0.0)
                    ),
                }
                for dim in _V1_ANOMALY_DIMENSIONS
            },
        },
    )


def _check_relative_drift(
    *,
    baseline: dict[str, float],
    current: dict[str, float],
    dim: str,
    threshold: float,
    drift_details: list[dict[str, object]],
    comparison_baseline_value: float | None = None,
) -> None:
    """相对偏移度检查:``|baseline - current| / max(baseline, eps) > threshold``。

    **baseline 为 0 时的特例**:
    - baseline = 0 且 current = 0 → 0 偏移,passed
    - baseline = 0 且 current > 0 → 视作 100% 漂移(从无到有),计入 drift
    - baseline > 0 → 标准相对偏移
    """
    raw_base_val = baseline.get(dim, 0.0)
    base_val = (
        raw_base_val if comparison_baseline_value is None else comparison_baseline_value
    )
    curr_val = current.get(dim, 0.0)

    if base_val == 0.0:
        if curr_val == 0.0:
            return
        # 从 0 到非 0 视作 100% 漂移
        relative = 1.0
    else:
        relative = abs(base_val - curr_val) / abs(base_val)

    if relative > threshold:
        drift_details.append(
            {
                "dimension": dim,
                "baseline_value": base_val,
                "raw_baseline_value": raw_base_val,
                "current_value": curr_val,
                "relative_drift": relative,
                "threshold": threshold,
            }
        )
