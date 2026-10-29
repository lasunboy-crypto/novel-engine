"""模型校准 profile 数据模型(W3.5-s2 引入)。

立项动机(W3.5 plan §0)
=======================
w3-s4 证伪"换模型 = 简单换 API":prompt 分三层,**模型校准层(长度倾向 / tell密度 /
温度 / thinking / reviewer 阈值)随模型重调**。本模块把这层"随模型变的数字"沉淀为
**可测量、可存储、引擎自动读**的结构 —— 三层架构(plan §1)的 **③ 模型校准层**。

三层架构定位
============
- **① 故事约束层**(model-agnostic,运行时注入):must_achieve / 伏笔 / 视角 / 身份。
- **② 结构基础设施层**(model-agnostic 机制):拆章机制(s3 卷规划层)。
- **③ 模型校准层 = 本模块**:一套 robust 机制(①②)+ **每模型一组测出来的数字**。

数据来源 + 消费方
=================
- **产**:校准套件 ``scripts/v2_w3_5_s2_calibration_suite.py`` 喂固定语料实测后产出
  本结构(per 模型 × 题材原型)。
- **存**:``config.yaml`` 顶层 ``model_calibration:`` 段(**keyed by 模型名**,如
  ``deepseek-v4-pro`` —— 非 profile id;standard/thinking 等多 profile 共享
  同模型校准),工作区 ``project.yaml`` 可整段覆盖(同 routing 语义)。
- **读**:``config_loader.load_model_calibration``;消费方 = s3 拆章(读
  ``per_genre[原型].chars_per_beat``)/ s5 路由+UI(按 model id load,未命中则警告)。

为何 keyed by 模型名而非 profile id
===================================
旧格式只按模型名索引。实验 v6 另以 writer_contract_hash 绑定模型版本、
Writer 参数与实现；同名模型在提示词或思考方式变化后不能继承旧观察。
模型名保留为检索入口，是否可用由实际 Writer 合同决定。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# 题材原型 taxonomy(W3.5-s2 从两份校准语料定义)
# ================================================
# **introspection**(内省 / 治愈 / 日常 / 慢节奏):tell 长 show 短,改 show 后**变短**
#   → chars/beat 低 → s3 **不触发**额外拆章。语料 = heal-off-v1(治愈日常)。
# **action**(动作 / 悬疑 / 打斗 / 快节奏):tell 是短概述、show 是逐招演出,改 show 后
#   **变长** → chars/beat 高 → s3 **触发**拆章(≤5000 优先)。语料 = luo-accept-v2(都市悬疑)。
# w3-s4 实证(报告 §5):show-not-tell 长度成本随题材**反号** —— 正是原型分桶的依据。
# 新增原型时**同步扩本 frozenset**(model_validator 拒未知 key,防 typo 静默漏校准)。
GENRE_ARCHETYPES: frozenset[str] = frozenset({"introspection", "action"})

# 书 genre(meta.json ``genre`` 字段)→ 题材原型映射(s2 初版,s5 路由细化)
# =========================================================================
# s2 只覆盖两份校准语料的 genre;s5 接 UI 时按实际书库扩。**未命中返 None**
# (resolve_genre_archetype),由消费方决定兜底 + 警告策略(plan §4.1 未校准警告),
# **不在此静默猜原型**(默认猜错会让内省书被误拆 / 动作书超 5000 闸)。
GENRE_ARCHETYPE_MAP: dict[str, str] = {
    "治愈日常": "introspection",
    "都市悬疑": "action",
    # NewBookPage 正式题材下拉保存英文 value，而不是中文 label。
    # ``mystery`` 与既有“都市悬疑”校准语料同属动作/悬疑原型。
    "mystery": "action",
}

CanonicalGenreIdentity = Literal["mystery", "healing_daily", "other"]


def canonical_genre_identity(genre: str | None) -> CanonicalGenreIdentity:
    """统一 UI value、中文题材名与结构 profile 的稳定题材身份。"""

    value = (genre or "").strip().lower()
    if value == "mystery" or any(
        token in value for token in ("悬疑", "推理", "侦探")
    ):
        return "mystery"
    if any(
        token in value
        for token in ("治愈", "日常", "慢生活", "温情", "田园", "疗愈", "暖")
    ):
        return "healing_daily"
    return "other"


def resolve_genre_archetype(genre: str | None) -> str | None:
    """书 genre → 题材原型;未映射返 ``None``(消费方警告 + 兜底)。

    :param genre: 书 genre(``meta.json`` ``genre`` 字段),可空。
    :return: ``GENRE_ARCHETYPES`` 之一,或 ``None``(未映射 / genre 为空)。

    **s2 初版**:只覆盖两份校准语料 genre;s5 接 UI 按实际书库扩。返 ``None`` 时
    **消费方须显式警告 + 选兜底原型**(plan §4.1),不在本函数静默默认 —— 默认猜错
    代价不对称(内省书误拆碎 / 动作书漏拆超 5000 商业硬闸)。
    """
    if not genre:
        return None
    mapped = GENRE_ARCHETYPE_MAP.get(genre)
    if mapped is not None:
        return mapped
    identity = canonical_genre_identity(genre)
    if identity == "mystery":
        return "action"
    return None


class GenreCalibration(BaseModel):
    """单题材原型的校准数字(从语料实测,per 模型 × 原型)。"""

    model_config = ConfigDict(extra="forbid")

    chars_per_beat: float = Field(
        ...,
        gt=0,
        description=(
            "每'章级内容单元'(plot node)渲染的平均字数 = chars_per_chapter / "
            "content_units_per_chapter。**s3 拆章公式的核心输入**:章数 = "
            "ceil(beat 内容单元数 × chars_per_beat / 目标字数)。归一化(除内容单元数)"
            "后跨 beat 可迁移,优于裸 chars_per_chapter。"
        ),
    )
    chars_per_chapter: float = Field(
        ...,
        gt=0,
        description="每章渲染字数的实测均值(原始,未归一化);守闸 / 长度倾向的直观锚。",
    )
    length_compliance: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "≤5000 字商业硬上限(维护人硬目标)守闸率 [0,1] = 守闸章数 / 样本章数。"
            "低 → 该(模型,原型)系统性超闸,s3 拆章靶子。"
        ),
    )
    content_units_per_chapter: float = Field(
        ...,
        gt=0,
        description=(
            "语料里每章平均内容单元数(``plot_node_budget``),= chars_per_beat 的"
            "归一化分母。两份语料实测恒定 ≈3;记录以便 s3 拆章公式量纲自洽。"
        ),
    )
    sample_chapters: int = Field(
        ...,
        gt=0,
        description="本(模型,原型)校准样本章数(语料臂章数);噪声评估用。",
    )


class GlobalCalibration(BaseModel):
    """模型级(跨题材)全局校准旋钮。"""

    model_config = ConfigDict(extra="forbid")

    length_tendency: Literal["undershoot", "balanced", "overshoot"] = Field(
        ...,
        description=(
            "守闸率得到的长度倾向。overshoot = 偏写长(deepseek;原 qwen 偏 undershoot)。"
            "③层 prompt 字数自检段强度的外置锚(writer.md v12 已 model-agnostic 对称,"
            "本字段供 s3/s5 按模型选强度档)。"
        ),
    )
    anti_stamp_strength: Literal["low", "medium", "high"] = Field(
        ...,
        description=(
            "tell密度(anti-盖章 grep)得到的反盖章纪律强度档。high = 该模型 tell盖章"
            "倾向重、需强反盖章注入。s3/s4 writer 注入读本字段。"
        ),
    )
    recommended_temperature: float = Field(
        ...,
        gt=0.0,
        le=2.0,
        description="该模型推荐生成温度(writer/生成路径);profile generation_params 默认的校准锚。",
    )
    thinking_default: bool = Field(
        ...,
        description=(
            "writer/生成路径的 thinking 默认(deepseek writer = false)。"
            "**注**:outline/card/judge 等走 thinking-on 的 ``*_thinking`` 兄弟 profile"
            "(镜像 qwen 方针),不受本字段控制 —— 本字段只描述模型的'生成路径默认'。"
        ),
    )
    reviewer_threshold_adjustment: float = Field(
        ...,
        ge=-5.0,
        le=5.0,
        description=(
            "reviewer 兼容得到的打分系统性漂移修正(0 = 无漂移,该模型输出经生产 "
            "reviewer 打分分布与基线一致)。非 0 = reviewer 对该模型系统性偏高/低,"
            "s4b 据此调阈值。w3-s3 实证 deepseek ≥ qwen → 预期 ≈0。"
        ),
    )

    def wants_word_count_trim(self) -> bool:
        """该模型是否**默认开**「超长 trim」(W3.5 校准联动,deepseek 专用补丁的随模型驱动)。

        ``length_tendency == "overshoot"`` → 该模型系统性偏写长(deepseek)→ 章易超
        ``word_count_max`` → 默认开 ``word_count_trim`` 兜超长;``balanced`` /
        ``undershoot``(qwen,无校准段则消费方退 False)→ 不会系统超长 → 默认关。
        **仅决定"默认是否开"**;真正 trim 仍受章级 gate(``len(content) >
        word_count_max``)+ 工作区三态覆盖(显式 on/off)控制 —— 本方法是
        ``None``(跟随校准)态的取值来源,单一真相源,便于测试。
        """
        return self.length_tendency == "overshoot"

    def wants_anti_tic_trim(self) -> bool:
        """该模型是否**默认开**「anti-tic trim」(W3.5 校准联动,deepseek 专用补丁的随模型驱动)。

        ``anti_stamp_strength == "high"`` → 该模型 tell 盖章倾向重(deepseek)→ 默认开
        ``anti_tic_trim`` 清「不是A是B」+ 演后盖章金句;``medium`` / ``low`` → **不**
        默认开 —— anti-tic 收益随 tic 密度走(实验:悬疑密集 12:0、治愈稀疏 8:4),只在
        ``high``(明确密集)自动开收益确定,``medium`` 边际收益不确定,交工作区三态显式
        覆盖。**仅决定"默认是否开"**;真正 trim 仍受章级 tic 密度 gate + 三态覆盖控制。
        """
        return self.anti_stamp_strength == "high"


class ModelCalibration(BaseModel):
    """单模型的完整校准 profile(``model_calibration[模型名]``)。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    writer_contract_hash: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$", exclude_if=lambda value: value is None,
        description="实验 v6 的模型版本、Writer 参数与实现来源；旧观察不得静默跨合同复用。",
    )

    calibrated_at: str = Field(
        ...,
        min_length=1,
        description="校准产出日期(ISO 8601,如 2026-06-16);可复现 / 失效判断用。",
    )
    calibration_corpus: str = Field(
        ...,
        min_length=1,
        description="校准语料标识(如 'heal-off-v1 + luo-accept-v2 modelab_drafts');可追溯。",
    )
    # ``global`` 是 Python 关键字 → 属性名 global_ + alias='global'(yaml/json 字面仍 'global')。
    global_: GlobalCalibration = Field(
        ...,
        alias="global",
        description="模型级全局旋钮(长度倾向 / 反盖章强度 / 温度 / thinking / reviewer 漂移)。",
    )
    per_genre: dict[str, GenreCalibration] = Field(
        ...,
        min_length=1,
        description="题材原型 → 校准数字;key ∈ GENRE_ARCHETYPES(introspection / action / ...)。",
    )

    @model_validator(mode="after")
    def _validate_archetype_keys(self) -> "ModelCalibration":
        """per_genre 的 key 必须是已知原型(防 typo 静默漏校准)。"""
        unknown = set(self.per_genre) - GENRE_ARCHETYPES
        if unknown:
            raise ValueError(
                f"per_genre 含未知题材原型 {sorted(unknown)};"
                f"已知原型 = {sorted(GENRE_ARCHETYPES)}。"
                "新增原型须同步扩 model_calibration.GENRE_ARCHETYPES frozenset。"
            )
        return self
