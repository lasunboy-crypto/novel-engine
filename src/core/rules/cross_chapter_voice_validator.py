"""[HARD] 跨章节视角 / 风格一致性 validator(v1.1 wave 1 session 6,G10 治本下游)。

职责:对比**当前章 fingerprint** vs **前一章 final approved 版本 fingerprint**,
检测跨章节人称视角漂移 + 节奏密度大幅偏移 → ``ValidatorResult(passed=False)``
→ ``validator_pipeline`` 派 ``verdict=rewrite``(走 G8 链路 + feedback 注入
writer 让其修)。

调用时机
========

由 ``run_validator_pipeline`` 在 ``_apply_validator_pipeline`` 主路径触发,
**任一以下条件即跳过(passed=True + skipped)**:

- ``baseline_fingerprint is None`` **且 current 是首章**(``current_chapter_number == 1``):
  执行**首章默认人称对齐检测**(读 workspace ``default_voice`` 字段判定)
- ``baseline_fingerprint is None`` **且 current 不是首章**(理论不应发生 — 前一章
  必有 final approved 版本;但旧数据 / 异常路径防御):fail-open + log warn
- ``workspace_default_voice == "free"``:用户显式关闭跨章节硬约束,只 warn log
- 例外条款命中(``chapter_card.setting`` 含豁免关键词或 ``mini_plan_event`` 含
  豁免关键词):
  豁免 + warn log 留观

否则:对比 baseline vs current 4 维度,任一超阈值 → fail。

阈值字面(v1.1 wave 1 session 6 / G10-Q4 抓质变 + 微变;
v1.1 wave 1 session 10 / G15 拆 paragraph_drift 为 warn-only)
==============================================================

2 类硬失败条件,**任一命中 = fail**(均为 POV identity 信号):

1. **代词主导对换**(质变):baseline.dominant_pov 与 current.dominant_pov
   一个是 ``third_person``、一个是 ``first_person`` → fail
   (``mixed`` 与任一组合不算对换,因为 mixed 是过渡态)
2. **代词频次差 10x**(质变):任一代词(``我`` / ``我们`` / ``他`` / ``她`` / 等)
   在 baseline + current 比较中,**主导侧频次 >= 对方侧频次 * 10**(且双方
   至少一侧 freq > PRONOUN_MIN_FREQ_FLOOR 避免短文本误判 + G14 起绝对计数兜底
   ``count >= PRONOUN_MIN_ABSOLUTE_COUNT``)→ fail

观察项(warn log only,**不阻塞流水**):

- **段落均值差 30%**(节奏微变):``|baseline.paragraph_length_mean - current
  .paragraph_length_mean| / max(baseline.paragraph_length_mean, eps) > 0.30``
  → emit ``cross_chapter_paragraph_drift_warn`` log + 保留 ``details
  .paragraph_drift_observed``,**不入 ``failures`` list 不触发 rewrite**。
  G15 起把节奏漂移从 POV identity validator 拆出,避免剧情驱动的自然节奏差异
  (动作 vs 对话 vs 梦境)被误判为人称漂移。未来可作独立 rhythm validator
  议题(G16+ 候选)。

**为何不查 simile_density / sentence_length_mean**:这 2 个维度在 G5 同章
``check_style_drift`` 已检测,跨章节差异主要在"人称漂移 + 段落节奏"两类,
增加额外维度会噪声 high(章节内容差异本就会带来句长波动)。

阈值选择理由(1st/2nd/3rd 排序):

- **1st 质量收益**:对齐 luo-clean-v1 ch-6 现场实证(``我`` 频次 ch-3=0.5%
  → ch-4=2.2%,**比例 4.4x** — 频次差 10x 略宽,设计取舍偏稳;**主导对换**
  3 → 1 → 3 跨章节明显切换,质变阈值 100% 截获)
- **2nd 风险对冲**:段落均值 30% 阈值与 G5 同型,实证证据:luo-cloud-test
  ch-15 后段落 mean ~180 → ~85 偏移 53%,远超 30% 阈值
- **3rd 工程便利**:复用 ``summarize_voice_from_fingerprint`` 主导人称判定,
  不重复实现代词聚类

baseline 版本选择(v1.1 wave 1 session 6 / G10-Q2 自决项)
==========================================================

固定取**前一章 final approved 版本**(由 caller 查 ``ChapterORM``
``current_version`` + ``ChapterVersionORM.style_fingerprint`` 拿):

- **1st 质量收益**:final approved 是流程认证的"干净 baseline",0 脏 baseline
  风险;对照 G5 取 v1(首版)是因为 G5 检测同章 rewrite 漂移,baseline 必须
  是"未漂移的源头";G10 检测跨章节漂移,baseline 必须是"上一章已被审核认可
  的成品"(approved 终态比 v1 首版更稳)
- **2nd 风险对冲**:不取最新 vN 终态可能含 rewrite 漂移(luo-cloud-test
  ch-21 v4 已严重漂移到第一人称),取 approved 锁定"通过审核的版本"
- **3rd 工程便利**:caller 已有 ``current_version`` 字段直接定位 v1.1.x
  approved 版本,无需复杂查询

首章逻辑(v1.1 wave 1 session 6 / G10-Q3)
==========================================

baseline 为 None 且 current 是首章 → **强制按 workspace ``default_voice`` 校验**
(项目级硬约束:中文长篇默认第三人称):

- ``default_voice='third_person'`` 但 current.dominant_pov == 'first_person' → fail
- ``default_voice='first_person'`` 但 current.dominant_pov == 'third_person' → fail
- ``default_voice='free'`` → passed(用户关闭硬约束)
- ``current.dominant_pov == 'mixed'`` → passed + warn log(过渡态,不强拦)
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Final, Literal

import structlog

from src.core.rules.prose_hard_contract import (
    CrossChapterVoiceConstraintSpec,
    ExecutableConstraintParameter,
)
from src.core.rules.style_fingerprint import (
    POV_DOMINANCE_RATIO,
    summarize_voice_from_fingerprint,
)
from src.core.rules.validators import ValidatorResult

if TYPE_CHECKING:
    from src.core.models.chapter_card import ChapterCard

_log = structlog.get_logger(__name__)


# ===== 阈值常量(字面 SSOT,可后续微调)=====

#: 代词频次倍数阈值(主导侧 frequency >= 对方 frequency * 该值 → fail)。
PRONOUN_FREQ_RATIO_THRESHOLD: Final[float] = 10.0

#: 代词频次绝对下限(双方频次都 < 该值 → 跳过该代词对比避免短文本误判)。
#: 例:freq=0.002 (= 5 次 / 2500 字章节);设此值避免代词稀疏 noise。
PRONOUN_MIN_FREQ_FLOOR: Final[float] = 0.002

#: 代词出现次数绝对下限(任一侧 freq*total_chars < 该值 → 跳过该代词比对,
#: 视为"该章节本就没真用过该代词,不是 POV 漂移而是剧情自然差异")。
#:
#: v1.1 wave 1 session 9 / G14 引入:rerun-2 实证发现 ch-5 沈舟独自行动场景
#: (无女角色 / 无内心独白)导致 ``我/她`` 频次=0,vs ch-4 baseline 0.0038/0.0031
#: 触发 G10 10x ratio 误判(实质是剧情变化而非人称漂移)。绝对计数兜底解决该
#: 边界 case;真正的 dominant POV flip 仍由独立的
#: ``_is_dominant_pov_flipped`` 检测捕获(主导代词绝对计数远大于 5,不受本兜底
#: 影响)。
PRONOUN_MIN_ABSOLUTE_COUNT: Final[int] = 5

#: 段落均值相对偏移阈值(超 30% 视作节奏漂移)。
PARAGRAPH_LENGTH_MEAN_DRIFT_THRESHOLD: Final[float] = 0.30

#: 全局短文本兜底:baseline 或 current 字数 < 该值视作短样本,直接 passed
#: (对齐 G5 ``check_style_drift`` 同字面值)。
MIN_CHARS_FOR_VOICE_CHECK: Final[int] = 500

#: 全局长文本兜底:baseline 或 current 字数 > 该值视作 mock 路径,直接 passed
#: (对齐 G5 ``check_style_drift`` 同字面值)。
MAX_REALISTIC_CHAPTER_CHARS: Final[int] = 1_000_000

#: 例外条款关键词字面集(setting / mini_plan event 含任一即豁免 + warn log)。
EXEMPTION_KEYWORDS: Final[tuple[str, ...]] = (
    "梦境",
    "回忆",
    "倒叙",
    "POV切换",
    "POV 切换",
    "视角切换",
)


def resolve_cross_chapter_voice_constraint_spec(
    *,
    baseline_fingerprint: dict[str, float] | None,
    baseline_chapter_id: str | None,
    workspace_default_voice: Literal["third_person", "first_person", "free"],
    chapter_card: "ChapterCard | None" = None,
    mini_plan_event: str | None = None,
) -> CrossChapterVoiceConstraintSpec:
    """把既有 G10/G12 的本轮适用性、来源与阈值冻结给共用计划。"""

    exemption_reason = _detect_exemption(
        chapter_card=chapter_card,
        mini_plan_event=mini_plan_event,
    )
    if baseline_fingerprint is not None:
        summary = summarize_voice_from_fingerprint(
            baseline_fingerprint,
            default_voice=workspace_default_voice,
        )
        # Session D:代词频率倍率只是场景/角色分布的相关信号，不再构成 HARD 身份。
        # 约束计划只冻结真正会阻断的主导人称与适用性，避免软频率变化漂移 plan hash。
        voice_source = {
            "total_chars": baseline_fingerprint.get("total_chars", 0.0),
            "dominant_pov": summary.dominant_pov,
        }
        encoded = json.dumps(
            voice_source,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        source_hash = hashlib.sha256(encoded).hexdigest()
        source_path = (
            f"chapters/{baseline_chapter_id or 'approved_ancestor_unknown'}"
            ".final.style_fingerprint"
        )
        baseline_chars = baseline_fingerprint.get("total_chars", 0.0)
        hard_enabled = (
            workspace_default_voice != "free"
            and exemption_reason is None
            and MIN_CHARS_FOR_VOICE_CHECK
            <= baseline_chars
            <= MAX_REALISTIC_CHAPTER_CHARS
        )
        rule_text = (
            "保持批准基线的跨章人称身份：first_person 与 third_person 主导不得"
            "直接对换。baseline dominant="
            f"{summary.dominant_pov}；mixed 本身不要求保持 mixed。ChapterCard/mini plan "
            "明确梦境、回忆、倒叙或 POV 切换时例外。"
        )
        parameters = (
            ExecutableConstraintParameter(
                name="workspace_default_voice", value=workspace_default_voice
            ),
            ExecutableConstraintParameter(
                name="baseline_dominant_pov", value=summary.dominant_pov
            ),
        )
        return CrossChapterVoiceConstraintSpec(
            hard_enabled=hard_enabled,
            provenance="chapter_version",
            source_path=source_path,
            source_hash=source_hash,
            rule_text=rule_text,
            parameters=parameters,
        )

    source_value = f"workspace.default_voice={workspace_default_voice}"
    source_hash = hashlib.sha256(source_value.encode("utf-8")).hexdigest()
    hard_enabled = workspace_default_voice != "free" and exemption_reason is None
    return CrossChapterVoiceConstraintSpec(
        hard_enabled=hard_enabled,
        provenance="workspace_config",
        source_path="workspace.default_voice",
        source_hash=source_hash,
        rule_text=(
            "无可用批准基线时，本章主导人称须与 workspace.default_voice="
            f"{workspace_default_voice} 兼容；mixed 不单独硬拒。ChapterCard/mini plan "
            "明确梦境、回忆、倒叙或 POV 切换时例外。"
        ),
        parameters=(
            ExecutableConstraintParameter(
                name="workspace_default_voice", value=workspace_default_voice
            ),
        ),
    )


#: 第三人称代词集合(用于代词频次对比)。
_THIRD_PERSON_PRONOUNS: Final[tuple[str, ...]] = ("他", "她", "它", "他们", "她们")

#: 第一人称代词集合(同上)。
_FIRST_PERSON_PRONOUNS: Final[tuple[str, ...]] = ("我", "我们")

#: 所有人称代词(用于代词频次差 10x 检测)。
_ALL_PRONOUNS: Final[tuple[str, ...]] = (
    *_FIRST_PERSON_PRONOUNS,
    *_THIRD_PERSON_PRONOUNS,
    "你",
    "你们",
)


def check_cross_chapter_voice(
    *,
    baseline_fingerprint: dict[str, float] | None,
    current_fingerprint: dict[str, float],
    workspace_default_voice: Literal[
        "third_person", "first_person", "free"
    ] = "third_person",
    chapter_card: "ChapterCard | None" = None,
    mini_plan_event: str | None = None,
    is_first_chapter: bool = False,
    baseline_chapter_id: str | None = None,
    current_chapter_id: str | None = None,
) -> ValidatorResult:
    """[HARD] 跨章节视角 / 风格一致性 validator 主入口。

    :param baseline_fingerprint: 前一章 final approved 版本的风格指纹;
        首章为 ``None``(无 baseline);旧数据无指纹也为 ``None``(fail-open)
    :param current_fingerprint: 当前章 fingerprint(必给)
    :param workspace_default_voice: 工作区默认人称(``Workspace.default_voice``);
        ``"free"`` 时直接 passed 不校验
    :param chapter_card: 当前章 ChapterCard;用于读 ``setting`` 字段做例外豁免
        判定;``continuity_context.pov_change`` 只表示跨章主视角角色变化，不构成
        叙述人称豁免;``None`` 时跳过卡片例外检测(全规则触发)
    :param mini_plan_event: 当前章 mini_plan ``one_line_event``(caller 查
        ``MiniPlanItem`` 表得);用于例外豁免;``None`` 时跳过该豁免检测
    :param is_first_chapter: 是否为首章(``Chapter.chapter_number == 1`` 或
        无前一章);True 时走首章 default_voice 校验路径
    :param baseline_chapter_id: details 字段标识(便于 LogsPage 排查)
    :param current_chapter_id: details 字段标识
    :return: ``ValidatorResult(name="cross_chapter_voice", ...)``
    """
    # ===== Layer 0: free 模式直接放行 =====
    if workspace_default_voice == "free":
        _log.debug(
            "cross_chapter_voice_skipped_free",
            current_chapter_id=current_chapter_id,
        )
        return ValidatorResult(
            name="cross_chapter_voice",
            passed=True,
            details={"skipped": "workspace_default_voice_free"},
        )

    # ===== Layer 1: 例外条款豁免(章节卡 + mini_plan 双重豁免)=====
    exemption_reason = _detect_exemption(
        chapter_card=chapter_card, mini_plan_event=mini_plan_event
    )
    if exemption_reason is not None:
        _log.warning(
            "cross_chapter_voice_exempted",
            current_chapter_id=current_chapter_id,
            reason=exemption_reason,
        )
        return ValidatorResult(
            name="cross_chapter_voice",
            passed=True,
            details={
                "skipped": "exempted",
                "exemption_reason": exemption_reason,
                "current_chapter_id": current_chapter_id,
            },
        )

    # ===== Layer 2: 短 / 长文本兜底(对齐 G5 同字面值)=====
    current_chars = current_fingerprint.get("total_chars", 0.0)
    if current_chars < MIN_CHARS_FOR_VOICE_CHECK:
        return ValidatorResult(
            name="cross_chapter_voice",
            passed=True,
            details={
                "skipped": "short_text",
                "current_chars": current_chars,
                "threshold": MIN_CHARS_FOR_VOICE_CHECK,
            },
        )
    if current_chars > MAX_REALISTIC_CHAPTER_CHARS:
        return ValidatorResult(
            name="cross_chapter_voice",
            passed=True,
            details={
                "skipped": "unrealistic_long_text",
                "current_chars": current_chars,
                "threshold": MAX_REALISTIC_CHAPTER_CHARS,
            },
        )

    # ===== Layer 3: baseline 路径分叉 =====
    current_summary = summarize_voice_from_fingerprint(
        current_fingerprint, default_voice=workspace_default_voice
    )

    if baseline_fingerprint is None:
        # v1.1 wave 1 session 8 / G12:baseline 缺失场景**统一回退到 workspace.
        # default_voice 校验路径**(不再区分首章 / 非首章)。
        #
        # **G12 治本上游**:G10 旧版只在 ``is_first_chapter=True`` 时走 default_voice
        # 校验,``is_first_chapter=False + baseline=None`` 走 fail-open + warn 分支
        # → ch-N-1 进 human_intervention 时 G10 完全漏抓,ch-N 漂移传染下游。
        # 现统一兜底:orchestrator 端 ``_resolve_cross_chapter_voice_context``
        # 已递归向上找 approved 祖父章节(G12 配套修复);走到这里说明**全部上游
        # 章节都不可用作 baseline**,workspace.default_voice 是最后兜底信号。
        fallback_reason: Literal["first_chapter", "no_approved_ancestor_found"] = (
            "first_chapter" if is_first_chapter else "no_approved_ancestor_found"
        )
        return _check_against_default_voice(
            current_summary=current_summary,
            workspace_default_voice=workspace_default_voice,
            current_chapter_id=current_chapter_id,
            fallback_reason=fallback_reason,
        )

    # baseline 字数兜底
    baseline_chars = baseline_fingerprint.get("total_chars", 0.0)
    if (
        baseline_chars < MIN_CHARS_FOR_VOICE_CHECK
        or baseline_chars > MAX_REALISTIC_CHAPTER_CHARS
    ):
        return ValidatorResult(
            name="cross_chapter_voice",
            passed=True,
            details={
                "skipped": "baseline_short_or_unrealistic_long",
                "baseline_chars": baseline_chars,
                "current_chars": current_chars,
            },
        )

    baseline_summary = summarize_voice_from_fingerprint(
        baseline_fingerprint, default_voice=workspace_default_voice
    )

    # ===== Layer 4:跨章节主导人称 HARD + 两类只读观察信号 =====
    failures: list[dict[str, object]] = []

    # 条件 1: 代词主导对换(质变)
    if _is_dominant_pov_flipped(
        baseline_pov=baseline_summary.dominant_pov,
        current_pov=current_summary.dominant_pov,
    ):
        failures.append(
            {
                "type": "dominant_pov_flipped",
                "baseline_pov": baseline_summary.dominant_pov,
                "current_pov": current_summary.dominant_pov,
            }
        )

    # Session D:任一代词频次差 10x 可能只是人物/对白分布变化，改为 WARN-only。
    # 真正的第一/第三人称主导对换仍由上方独立 HARD 捕获。
    pronoun_frequency_warnings = _check_pronoun_freq_ratio(
        baseline_fingerprint=baseline_fingerprint,
        current_fingerprint=current_fingerprint,
    )
    if pronoun_frequency_warnings:
        _log.warning(
            "cross_chapter_pronoun_frequency_warn",
            current_chapter_id=current_chapter_id,
            baseline_chapter_id=baseline_chapter_id,
            observations=pronoun_frequency_warnings,
            note="pronoun frequency ratio is SOFT telemetry; dominant POV flip remains HARD",
        )

    # v1.1 wave 1 session 10 / G15:段落均值漂移**改 warn-only**(原 30% 硬失败)。
    #
    # **为什么改 warn-only**:rerun-3 ch-5 实证发现 G10 把"POV identity 一致性"
    # 与"段落节奏一致性"两种语义混塞硬失败 — ch-5 双世界场景(梦境锦衣卫
    # 动作 + 现实接触)段落自然 80-94 字 vs ch-4 单一对话场景 53 字 = 77%
    # 漂移触发 G10 误判,但 POV 没漂(全 third_person)。LLM 在 G11 prompt v6
    # feedback 下也无法精确控制段落均值到 baseline ±30%(实测振荡 94→48→82→33)。
    #
    # G15 净化 cross_chapter_voice 语义为纯 POV identity validator
    # (dominant_pov_flipped + pronoun_freq_ratio 两条质变信号);段落漂移留
    # warn log 作观察数据,**不阻塞流水**。未来如用户实证累积证明需要独立
    # rhythm validator,可作 G16+ 议题独立做。
    para_drift = _check_paragraph_mean_drift(
        baseline_fingerprint=baseline_fingerprint,
        current_fingerprint=current_fingerprint,
    )
    if para_drift is not None:
        _log.warning(
            "cross_chapter_paragraph_drift_warn",
            current_chapter_id=current_chapter_id,
            baseline_chapter_id=baseline_chapter_id,
            baseline_mean=para_drift["baseline_mean"],
            current_mean=para_drift["current_mean"],
            relative_drift=para_drift["relative_drift"],
            threshold=para_drift["threshold"],
            note="G15(2026-05-27 w1-s10):paragraph drift warn-only,不阻塞 validator,"
            "POV identity 只由 dominant_pov_flipped 硬兜底",
        )
        # G15 不再 append 到 failures;保留 details.paragraph_drift_observed 供调试
        paragraph_drift_observed: dict[str, object] | None = para_drift
    else:
        paragraph_drift_observed = None

    passed = not failures
    return ValidatorResult(
        name="cross_chapter_voice",
        passed=passed,
        details={
            "baseline_chapter_id": baseline_chapter_id,
            "current_chapter_id": current_chapter_id,
            "baseline_pov": baseline_summary.dominant_pov,
            "current_pov": current_summary.dominant_pov,
            "baseline_paragraph_mean": baseline_summary.paragraph_length_mean,
            "current_paragraph_mean": current_summary.paragraph_length_mean,
            "failures": failures,
            "pronoun_frequency_warnings": pronoun_frequency_warnings,
            "paragraph_drift_observed": paragraph_drift_observed,
        },
    )


# ---- 内部 helpers --------------------------------------------------------


def _detect_exemption(
    *,
    chapter_card: "ChapterCard | None",
    mini_plan_event: str | None,
) -> str | None:
    """例外条款判定:命中即返字符串原因,否则 None。

    检测顺序(任一命中即返):
    1. ``chapter_card.setting`` 含 ``EXEMPTION_KEYWORDS`` 任一字面
    2. ``mini_plan_event`` 含 ``EXEMPTION_KEYWORDS`` 任一字面

    ``continuity_context.pov_change`` 不在此列：它只记录本章主视角角色相对
    上一章是否变化，不授权章内切换或第一/第三人称漂移。
    """
    if chapter_card is not None:
        for keyword in EXEMPTION_KEYWORDS:
            if keyword in chapter_card.setting:
                return f"setting_contains_keyword:{keyword}"
    if mini_plan_event is not None:
        for keyword in EXEMPTION_KEYWORDS:
            if keyword in mini_plan_event:
                return f"mini_plan_event_contains_keyword:{keyword}"
    return None


def _check_against_default_voice(
    *,
    current_summary,
    workspace_default_voice: Literal["third_person", "first_person", "free"],
    current_chapter_id: str | None,
    fallback_reason: Literal[
        "first_chapter", "no_approved_ancestor_found"
    ] = "first_chapter",
) -> ValidatorResult:
    """workspace.default_voice 兜底校验:current.dominant_pov 必须与 default_voice 兼容。

    v1.1 wave 1 session 6 / G10 旧名 ``_check_first_chapter_against_default``,
    v1.1 wave 1 session 8 / G12 改名 + 增 ``fallback_reason`` 入参 — 覆盖
    "首章无 baseline" + "非首章但递归祖父全失败" 两类场景统一走 default_voice 兜底。

    决策表:
    - ``default_voice='third_person'`` + ``current=='first_person'`` → fail
    - ``default_voice='first_person'`` + ``current=='third_person'`` → fail
    - ``default_voice='*'`` + ``current=='mixed'`` → passed + warn(过渡态不强拦)
    - ``default_voice='free'`` 已在上游 Layer 0 直接放行(不会走到这里)
    - 其他组合 → passed

    ``failure_type`` / ``failures[].type`` 字面对齐 ``fallback_reason``:
    - ``first_chapter`` → ``first_chapter_violates_default_voice``(沿用 G10 字面)
    - ``no_approved_ancestor_found`` → ``no_approved_ancestor_violates_default_voice``
      (G12 新失败类型,便于 luo-cloud 调试)
    """
    failure_type = (
        "first_chapter_violates_default_voice"
        if fallback_reason == "first_chapter"
        else "no_approved_ancestor_violates_default_voice"
    )

    if current_summary.dominant_pov == "mixed":
        _log.warning(
            "cross_chapter_voice_fallback_mixed",
            current_chapter_id=current_chapter_id,
            default_voice=workspace_default_voice,
            fallback_reason=fallback_reason,
            reason="dominant_pov_mixed_not_enforced",
        )
        return ValidatorResult(
            name="cross_chapter_voice",
            passed=True,
            details={
                "skipped": "fallback_mixed_pov",
                "default_voice": workspace_default_voice,
                "current_pov": current_summary.dominant_pov,
                "fallback_reason": fallback_reason,
            },
        )

    if (
        workspace_default_voice == "third_person"
        and current_summary.dominant_pov == "first_person"
    ) or (
        workspace_default_voice == "first_person"
        and current_summary.dominant_pov == "third_person"
    ):
        return ValidatorResult(
            name="cross_chapter_voice",
            passed=False,
            details={
                "failure_type": failure_type,
                "default_voice": workspace_default_voice,
                "current_pov": current_summary.dominant_pov,
                "current_chapter_id": current_chapter_id,
                "fallback_reason": fallback_reason,
                "failures": [
                    {
                        "type": failure_type,
                        "default_voice": workspace_default_voice,
                        "current_pov": current_summary.dominant_pov,
                    }
                ],
            },
        )

    return ValidatorResult(
        name="cross_chapter_voice",
        passed=True,
        details={
            "fallback_reason": fallback_reason,
            "default_voice": workspace_default_voice,
            "current_pov": current_summary.dominant_pov,
        },
    )


# v1.1 wave 1 session 8 / G12:旧名向后兼容别名(测试 + 历史调用方仍可引用)。
# 未来 wave 4-5 清理 cycle 可直接删 alias。
_check_first_chapter_against_default = _check_against_default_voice


def _is_dominant_pov_flipped(*, baseline_pov: str, current_pov: str) -> bool:
    """主导人称对换判定:一个是 third_person 一个是 first_person → True。

    ``mixed`` 与任一其他组合不算对换(mixed 是过渡态,不质变)。
    """
    return (baseline_pov == "third_person" and current_pov == "first_person") or (
        baseline_pov == "first_person" and current_pov == "third_person"
    )


def _check_pronoun_freq_ratio(
    *,
    baseline_fingerprint: dict[str, float],
    current_fingerprint: dict[str, float],
) -> list[dict[str, object]]:
    """检测任一代词在 baseline + current 之间频次差 >= 10x。

    返失败 list(可能多条)。**双重 floor 兜底**避免代词稀疏 + 剧情自然差异误判:

    - 频次 floor(``PRONOUN_MIN_FREQ_FLOOR=0.002``):双方都 < 该值 → 跳过
    - **绝对计数 floor**(``PRONOUN_MIN_ABSOLUTE_COUNT=5``,v1.1 wave 1 session 9
      / G14 引入):任一侧 ``freq * total_chars`` < 该值 → 跳过该代词比对(视为
      "该章节本就没真用过该代词,剧情自然差异而非 POV 漂移")

    G14 兜底解决 rerun-2 实证发现的边界 case:ch-5 沈舟独自行动场景导致
    ``我/她`` 频次=0,vs ch-4 baseline 0.0038/0.0031 触发 10x ratio 误判
    (实质是剧情变化)。真正的 dominant POV flip 由独立的
    ``_is_dominant_pov_flipped`` 检测捕获(主导代词绝对计数远大于 5,不受兜底
    影响)。
    """
    baseline_chars = baseline_fingerprint.get("total_chars", 0.0)
    current_chars = current_fingerprint.get("total_chars", 0.0)
    failures: list[dict[str, object]] = []
    for pronoun in _ALL_PRONOUNS:
        key = f"pronoun_freq_{pronoun}"
        b_freq = baseline_fingerprint.get(key, 0.0)
        c_freq = current_fingerprint.get(key, 0.0)
        if b_freq < PRONOUN_MIN_FREQ_FLOOR and c_freq < PRONOUN_MIN_FREQ_FLOOR:
            continue  # 双方都稀疏(频次维度),跳过
        # G14:绝对计数兜底(任一侧 < 5 次 → 跳过)
        b_count = b_freq * baseline_chars
        c_count = c_freq * current_chars
        if b_count < PRONOUN_MIN_ABSOLUTE_COUNT or c_count < PRONOUN_MIN_ABSOLUTE_COUNT:
            continue  # 任一侧绝对计数稀疏,视为剧情自然差异跳过
        # 计算比例:主导侧 / 对方侧
        if c_freq > b_freq:
            major, minor = c_freq, b_freq
            direction = "increased"
        else:
            major, minor = b_freq, c_freq
            direction = "decreased"
        # minor=0 视作 inf 比例;但 G14 兜底已保证两侧 count>=5,不会到这里
        # (沿用原 minor=0 防御分支不删,作为额外安全网)
        if minor == 0.0:
            if major >= PRONOUN_MIN_FREQ_FLOOR:
                failures.append(
                    {
                        "type": "pronoun_freq_ratio_exceeded",
                        "pronoun": pronoun,
                        "baseline_freq": b_freq,
                        "current_freq": c_freq,
                        "ratio": float("inf"),
                        "direction": direction,
                    }
                )
            continue
        ratio = major / minor
        if ratio >= PRONOUN_FREQ_RATIO_THRESHOLD:
            failures.append(
                {
                    "type": "pronoun_freq_ratio_exceeded",
                    "pronoun": pronoun,
                    "baseline_freq": b_freq,
                    "current_freq": c_freq,
                    "ratio": ratio,
                    "direction": direction,
                }
            )
    return failures


def _check_paragraph_mean_drift(
    *,
    baseline_fingerprint: dict[str, float],
    current_fingerprint: dict[str, float],
) -> dict[str, object] | None:
    """检测段落均值相对偏移 > 30%。

    ``baseline=0`` 时特例:
    - current=0 → 0 偏移,返 None
    - current>0 → 100% 偏移视作 fail
    返失败 dict 或 None(无偏移)。
    """
    b_mean = baseline_fingerprint.get("paragraph_length_mean", 0.0)
    c_mean = current_fingerprint.get("paragraph_length_mean", 0.0)
    if b_mean == 0.0:
        if c_mean == 0.0:
            return None
        return {
            "type": "paragraph_length_mean_drift",
            "baseline_mean": b_mean,
            "current_mean": c_mean,
            "relative_drift": 1.0,
            "threshold": PARAGRAPH_LENGTH_MEAN_DRIFT_THRESHOLD,
        }
    relative = abs(b_mean - c_mean) / abs(b_mean)
    if relative > PARAGRAPH_LENGTH_MEAN_DRIFT_THRESHOLD:
        return {
            "type": "paragraph_length_mean_drift",
            "baseline_mean": b_mean,
            "current_mean": c_mean,
            "relative_drift": relative,
            "threshold": PARAGRAPH_LENGTH_MEAN_DRIFT_THRESHOLD,
        }
    return None


# 抑制未使用 import 警告(POV_DOMINANCE_RATIO 在文档注释引用)
_ = POV_DOMINANCE_RATIO
