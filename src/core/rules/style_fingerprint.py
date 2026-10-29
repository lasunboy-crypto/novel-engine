"""风格指纹纯规则抽取器(v1.1 wave 1 session 4 引入,G5 治本核心)。

职责:从章节正文抽取 **4 维度纯规则统计指标**,作为"风格指纹"落
``ChapterVersion.style_fingerprint: dict[str, float] | None`` 字段。首版(v1)
抽取后作 baseline,后续 vN(rewrite 循环)抽取后由
``check_style_drift``([src/core/rules/style_drift_validator.py](style_drift_validator.py))
对比偏移度,超阈值触发 ``validator_pipeline`` 失败 → reviewer 反馈
"风格漂移" → retry-with-feedback 让 LLM 贴近 v1 风格。

4 维度
======

1. **段落长度分布**:按 ``\\n\\n`` 切段,落字符数 list 的 ``mean / std``。
   - ``paragraph_length_mean``:段落字符数均值
   - ``paragraph_length_std``:段落字符数标准差(总体标准差,非样本)
2. **人称代词频次**:9 种代词在正文中各自占比(出现次数 / 总字符数)。
   - ``pronoun_freq_我`` / ``pronoun_freq_你`` / ``pronoun_freq_他`` /
     ``pronoun_freq_她`` / ``pronoun_freq_它`` / ``pronoun_freq_我们`` /
     ``pronoun_freq_你们`` / ``pronoun_freq_他们`` / ``pronoun_freq_她们``
   - **复数代词先匹配**(贪心)避免 "我们" 被算 1 次 "我" + 1 次 "们"。
3. **句长分布**:按中文断句标点 ``。!?…`` 切句,落字符数 list 的 ``mean / std``。
   - ``sentence_length_mean`` / ``sentence_length_std``
4. **修辞密度**:3 项简化字面统计。
   - ``simile_density``:比喻词(``像 / 如同 / 仿佛 / 犹如``)总出现次数 / 总句数
   - ``exclamation_density``:感叹号 + 问号总字符数 / 总句数
   - ``ru_x_ban_per_10k``(v2 w2-s2 新增):``如X般`` 句式每万字次数 —
     simile_density 词表对该句式通胀盲(w2-s1 标定实证 clean 语料 +325%),
     独立字段供趋势观察维度;**旧落库指纹无此字段,下游一律 ``.get`` 缺省
     跳过**(向后兼容,不回填历史)。

为何不调 LLM(v1.1 wave 1 session 4 自决项)
=============================================

- **1st 质量收益**:规则统计 100% 复现 + 0 token 成本 + 偏移度可数值化阈值
  对比,比 LLM-as-judge 主观评估"风格相似度"稳定得多
- **2nd 风险对冲**:LLM 评 "风格漂移" 易主观;规则给出客观数字,reviewer
  feedback 可贴明确数字("段落均值 v1=180 vs v4=85,偏移 53%")让 LLM 知
  道该往哪个方向贴
- **3rd 工程便利**:省 token + 不依赖 LLM 路由 + 不需要 retry 机制,直接
  集成到 ``validator_pipeline`` 同 ``check_timeline_monotonic`` 同型

候选清单字面 [docs/plan/v1_1_candidates.md §G5](../../../docs/plan/v1_1_candidates.md)
把 ``style_drift_validator`` 写为 "LLM-as-judge agent",但维护人 w1-s4 启动
指令字面 "**4 维度规则算法,不调 LLM**" — 启动指令优先(规格源覆盖 plan 字面)。

文件路径自决项(v1.1 wave 1 session 4)
=======================================

plan §4 w1-s4 字面 ``src/core/agents/style_fingerprint_extractor.py`` +
``src/core/agents/validators/style_drift_validator.py``(``validators/`` 子目录
**不存在**,**类 D 变种第 4 处证据连续 4 session 触发**)。

**自决项处置**:文件路径选 ``src/core/rules/`` 而非 plan §4 字面 ``agents/``,
理由(1st/2nd/3rd 排序):

- **1st 质量收益**:与既有纯规则 SSOT(``rules/validators.py`` /
  ``rules/validator_pipeline.py`` / ``rules/outline_validators.py``)语义对齐
  — 阅读时一眼分清"纯规则"vs"LLM-as-judge"
- **2nd 风险对冲**:避免后续 v1.1 wave 2 H3 reviewer profile 化任务把
  ``style_drift_validator`` 误装 LLM 路由(它本来不调 LLM)
- **3rd 工程便利**:不需要新建 ``agents/validators/`` 子目录,与
  ``chapter_summary`` 切分原则同模式
"""

from __future__ import annotations

import re
import statistics
from typing import Final, Literal

import structlog
from pydantic import BaseModel, Field

_log = structlog.get_logger(__name__)


# ===== 维度常量(字面 SSOT)=====

#: 段落切分分隔符:连续 2+ 换行视作段落边界。
_PARAGRAPH_SPLIT_PATTERN: Final = re.compile(r"\n\s*\n")

#: 句子切分分隔符:中文断句标点 ``。 ！ ？ ! ? …`` 任一即切(全角 + 半角)。
_SENTENCE_SPLIT_PATTERN: Final = re.compile(r"[。！？!?…]")

#: 9 种人称代词(**复数先匹配**贪心),与维护人 w1-s4 启动指令字面对齐。
_PRONOUNS_PLURAL_FIRST: Final[tuple[str, ...]] = (
    "我们",
    "你们",
    "他们",
    "她们",
    "我",
    "你",
    "他",
    "她",
    "它",
)

#: 比喻词字面(简化集),覆盖中文长篇高频用法。
#: **v2 w2-s2 注**:本词表对"如X般"句式通胀是盲的(w2-s1 标定 §1 实证:
#: clean 语料"如X般"+325% 而本词表口径趋势平),但**不改本词表** —
#: ``style_drift_validator``(同章 v1→vN 突变锚)在用,改词表会让历史指纹
#: 不可比。"如X般"以独立新字段 ``ru_x_ban_per_10k`` 度量(见下)。
_SIMILE_WORDS: Final[tuple[str, ...]] = ("像", "如同", "仿佛", "犹如")

#: v2 w2-s2:"如X般"句式正则("如" + 1~10 个非断句字符 + "般",如"如刀割般"
#: "如墨般")。**与标定脚本完全同口径**(scripts/v2_w2_s1_drift_calibration.py
#: ``_RU_X_BAN_RE``)— 标定数字是 ground truth,口径漂移会让 w2-s1 报告
#: 数据(clean +325% / 2.94→12.49 每万字)失去对账意义。
_RU_X_BAN_PATTERN: Final = re.compile(r"如[^,。;!?\n]{1,10}般")

#: 感叹号 + 问号字符集(中文全角 U+FF01/U+FF1F + ASCII 半角 U+0021/U+003F)。
_EXCLAMATION_CHARS: Final[tuple[str, ...]] = ("！", "？", "!", "?")


def compute_style_fingerprint(chapter_text: str) -> dict[str, float]:
    """从章节正文抽取 4 维度风格指纹。

    返 ``dict[str, float]``,字段固定 18 项(段落 2 + 代词 9 + 句长 2 + 修辞 3
    〔v2 w2-s2 加 ``ru_x_ban_per_10k``〕+ 元信息 2 = ``total_chars`` +
    ``total_sentences``);上层 ``check_style_drift`` 对比 baseline vs current
    时只看维度字段不看元信息。

    **空 / 极短文本防御**:
    - ``chapter_text`` 长度 0 → 所有维度 0.0(下游 validator 会跳过对比,
      baseline_fingerprint 为空则视作未抽取)
    - 段落 / 句子数 0 → 对应 mean / std 取 0.0
    """
    total_chars = len(chapter_text)
    if total_chars == 0:
        return _empty_fingerprint()

    # === 1. 段落长度分布 ===
    paragraphs = [
        seg for seg in _PARAGRAPH_SPLIT_PATTERN.split(chapter_text) if seg.strip()
    ]
    paragraph_lengths = [len(p) for p in paragraphs]
    para_mean = float(statistics.mean(paragraph_lengths)) if paragraph_lengths else 0.0
    para_std = float(statistics.pstdev(paragraph_lengths)) if len(paragraph_lengths) > 1 else 0.0

    # === 2. 人称代词频次(复数先匹配避免重复计数)===
    pronoun_counts: dict[str, int] = {}
    work_text = chapter_text
    for pronoun in _PRONOUNS_PLURAL_FIRST:
        count = work_text.count(pronoun)
        pronoun_counts[pronoun] = count
        # 把已匹配的复数代词替换成占位,避免后面单数代词重复计数
        if count > 0 and len(pronoun) > 1:
            work_text = work_text.replace(pronoun, " " * len(pronoun))

    # === 3. 句长分布 ===
    sentences = [
        seg for seg in _SENTENCE_SPLIT_PATTERN.split(chapter_text) if seg.strip()
    ]
    sentence_lengths = [len(s) for s in sentences]
    sent_mean = float(statistics.mean(sentence_lengths)) if sentence_lengths else 0.0
    sent_std = float(statistics.pstdev(sentence_lengths)) if len(sentence_lengths) > 1 else 0.0
    total_sentences = len(sentences)

    # === 4. 修辞密度 ===
    simile_total = sum(chapter_text.count(w) for w in _SIMILE_WORDS)
    exclamation_total = sum(chapter_text.count(c) for c in _EXCLAMATION_CHARS)
    simile_density = simile_total / total_sentences if total_sentences > 0 else 0.0
    exclamation_density = (
        exclamation_total / total_sentences if total_sentences > 0 else 0.0
    )
    # v2 w2-s2:"如X般"密度(每万字)。simile_density 对该句式通胀盲
    # (w2-s1 标定 §1),独立字段度量;旧落库指纹无此字段 → 下游趋势层
    # 以 ``fingerprint.get(...)`` 缺省跳过,向后兼容。
    ru_x_ban_per_10k = len(_RU_X_BAN_PATTERN.findall(chapter_text)) / total_chars * 10000

    fingerprint: dict[str, float] = {
        "paragraph_length_mean": para_mean,
        "paragraph_length_std": para_std,
        "sentence_length_mean": sent_mean,
        "sentence_length_std": sent_std,
        "simile_density": simile_density,
        "exclamation_density": exclamation_density,
        "ru_x_ban_per_10k": ru_x_ban_per_10k,
        "total_chars": float(total_chars),
        "total_sentences": float(total_sentences),
    }
    for pronoun in _PRONOUNS_PLURAL_FIRST:
        freq = pronoun_counts[pronoun] / total_chars
        fingerprint[f"pronoun_freq_{pronoun}"] = freq

    return fingerprint


#: v1.1 wave 1 session 6 / G10 — 主导人称判定阈值:
#: ``frequency(主导) >= POV_DOMINANCE_RATIO * frequency(对侧)`` 才算主导。
#: 取 1.5 = 折中值:< 1.2 易把"略偏"判主导(误激活硬约束);> 2.0 易把"已偏离"判 mixed(漏抓)。
#: 实证依据:luo-clean-v1 ch-4~7 第一人称占比 ~6/2~3 = 2x~3x → 1.5 阈值 100% 命中"first_person"主导。
POV_DOMINANCE_RATIO: Final[float] = 1.5

#: 第三人称代词集合(用于 ``summarize_voice_from_fingerprint`` 主导人称判定)。
_THIRD_PERSON_PRONOUNS: Final[tuple[str, ...]] = ("他", "她", "它", "他们", "她们")

#: 第一人称代词集合(同上)。
_FIRST_PERSON_PRONOUNS: Final[tuple[str, ...]] = ("我", "我们")


class PovSummary(BaseModel):
    """v1.1 wave 1 session 6 / G10 — 单章风格基线摘要,注入 writer prompt 让 LLM 自觉对齐。

    从已抽取的 fingerprint(``compute_style_fingerprint`` 产出)+ 工作区
    ``default_voice`` 字面派生,作为 writer prompt 的 ``PREV CHAPTER VOICE
    BASELINE`` 段数据源。

    设计要点:
    - ``dominant_pov`` 3 档判定基于第三人称 / 第一人称代词频次比较;
      ``mixed`` 表示双方都不超过 ``POV_DOMINANCE_RATIO`` 阈值。
    - ``paragraph_length_mean`` / ``sentence_length_mean`` / ``simile_density``
      作为 writer 风格锚定的 3 个量化指标(可让 LLM 贴合节奏密度)。
    - 不展开全 17 项 fingerprint 字段 — prompt 容积有限,只挑读者最能感知
      差异的 4 个维度(避免 prompt 噪声)。
    """

    dominant_pov: Literal["third_person", "first_person", "mixed"] = Field(
        ...,
        description="主导人称视角(third_person / first_person / mixed),由代词频次比较推断",
    )
    paragraph_length_mean: float = Field(
        ..., ge=0.0, description="段落字符数均值"
    )
    sentence_length_mean: float = Field(
        ..., ge=0.0, description="句子字符数均值"
    )
    simile_density: float = Field(
        ..., ge=0.0, description="比喻密度(像/如同/仿佛/犹如 出现次数 / 句数)"
    )
    anchor_source: Literal["prev_chapter", "book_anchor"] = Field(
        default="prev_chapter",
        description=(
            "v2 w2-s2:段均/句均两个数字的锚源 — prev_chapter(现状,前一章"
            "指纹)/ book_anchor(全书 ch1-3 中位锚,A/B ON 臂;dominant_pov "
            "与 simile_density 仍来自前一章)。writer ``_format_voice_baseline`` "
            "按本字段渲染对应 prompt 字面(防 prompt 段名与数字来源不符)。"
        ),
    )


def summarize_voice_from_fingerprint(
    fingerprint: dict[str, float],
    *,
    default_voice: Literal["third_person", "first_person", "free"] = "third_person",
) -> PovSummary:
    """从 fingerprint 计算 ``PovSummary``。

    主导人称判定逻辑:
    - 加总第三人称代词频次(``他 / 她 / 它 / 他们 / 她们``)= ``third_total``
    - 加总第一人称代词频次(``我 / 我们``)= ``first_total``
    - ``first_total >= third_total * POV_DOMINANCE_RATIO`` → ``first_person``
    - ``third_total >= first_total * POV_DOMINANCE_RATIO`` → ``third_person``
    - 否则 ``mixed``(双方都不超阈值,可能是 free 视角 / 章内切换 / 频次都极低)

    **频次都为 0 的特例**(空 / 极短文本兜底):退化为 ``default_voice``
    (项目级硬约束默认 ``third_person``);``default_voice='free'`` 时退化
    为 ``mixed``。
    """
    third_total = sum(
        fingerprint.get(f"pronoun_freq_{p}", 0.0) for p in _THIRD_PERSON_PRONOUNS
    )
    first_total = sum(
        fingerprint.get(f"pronoun_freq_{p}", 0.0) for p in _FIRST_PERSON_PRONOUNS
    )

    if third_total == 0.0 and first_total == 0.0:
        # 极短 / 空文本兜底:退化为 default_voice
        if default_voice == "third_person":
            dominant: Literal["third_person", "first_person", "mixed"] = "third_person"
        elif default_voice == "first_person":
            dominant = "first_person"
        else:
            dominant = "mixed"
    elif first_total >= third_total * POV_DOMINANCE_RATIO:
        dominant = "first_person"
    elif third_total >= first_total * POV_DOMINANCE_RATIO:
        dominant = "third_person"
    else:
        dominant = "mixed"

    return PovSummary(
        dominant_pov=dominant,
        paragraph_length_mean=fingerprint.get("paragraph_length_mean", 0.0),
        sentence_length_mean=fingerprint.get("sentence_length_mean", 0.0),
        simile_density=fingerprint.get("simile_density", 0.0),
    )


def _empty_fingerprint() -> dict[str, float]:
    """0 长文本兜底,所有维度 0.0(下游 validator 跳过对比)。"""
    fp: dict[str, float] = {
        "paragraph_length_mean": 0.0,
        "paragraph_length_std": 0.0,
        "sentence_length_mean": 0.0,
        "sentence_length_std": 0.0,
        "simile_density": 0.0,
        "exclamation_density": 0.0,
        "ru_x_ban_per_10k": 0.0,
        "total_chars": 0.0,
        "total_sentences": 0.0,
    }
    for pronoun in _PRONOUNS_PLURAL_FIRST:
        fp[f"pronoun_freq_{pronoun}"] = 0.0
    return fp
