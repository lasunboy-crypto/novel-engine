"""审稿结果与问题模型(对应 PROJECT_SPEC §3.6)。"""

from datetime import datetime
import hashlib
from typing import Any, Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, Field


# F5:Reviewer 的业务载荷必须在进入持久化和下一轮 Reviser 之前有确定上限。
# 这些上限覆盖冻结 v7 的全部已知样本（issues<=2、suggestions<=3，单项<=114 字），
# 同时把最坏 Reviewer 问题块限制在约 5K 字符以内。不要把它们用于截断：超限输出
# 必须由 Pydantic 拒绝，并交给既有 Reviewer parse retry。
REVIEWER_ISSUES_MAX_ITEMS = 3
REVIEWER_SUGGESTIONS_MAX_ITEMS = 3
REVIEW_ISSUE_PARAGRAPH_ANCHOR_MAX_CHARS = 120
REVIEW_ISSUE_TYPE_MAX_CHARS = 64
REVIEW_ISSUE_DESCRIPTION_MAX_CHARS = 500
REVIEW_ISSUE_SUGGESTION_MAX_CHARS = 500
REVIEWER_SUGGESTION_MAX_CHARS = 500


class ReviewIssue(BaseModel):
    """审稿命中的单个问题(A2)。

    用于 Reviser agent 定位需要修订的段落与改动方向。
    """

    paragraph_anchor: str = Field(
        ...,
        max_length=REVIEW_ISSUE_PARAGRAPH_ANCHOR_MAX_CHARS,
        description="出问题段落的短引文,作为唯一锚点",
    )
    issue_type: str = Field(
        ...,
        max_length=REVIEW_ISSUE_TYPE_MAX_CHARS,
        description="问题类型,如 voice / pacing / consistency",
    )
    description: str = Field(
        ...,
        max_length=REVIEW_ISSUE_DESCRIPTION_MAX_CHARS,
        description="问题描述",
    )
    suggestion: str = Field(
        ...,
        max_length=REVIEW_ISSUE_SUGGESTION_MAX_CHARS,
        description="改动建议",
    )


class ConstraintViolation(BaseModel):
    """可回查权威规则与被检查正文的结构化约束观察。

    历史字段名保留为 ``ConstraintViolation``，但 ``severity="soft"`` 的记录自
    R9 起同时承担结构化 WARN 信号。新增路由字段全有兼容默认值，因此 0030 及
    更早审稿 JSON 仍可原样反序列化；只有带完整来源版本与路由元数据的新记录才
    能注入后续章节。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    constraint_id: str = Field(
        ...,
        pattern=r"^[0-9a-f]{64}$",
        description="由稳定来源标识与规范化规则原文派生的 SHA-256",
    )
    domain: Literal[
        "world.unique_rules",
        "world.setting",
        "world.power_system",
        "character.identity",
        "character.motivation",
        "character.voice",
        "character.profession",
        "chapter.internal",
        "chapter.continuity",
        "chapter.timeline",
        "chapter.style",
        "chapter.dedup",
        "chapter.ending",
    ] = Field(..., description="被检查约束所属域")
    source_path: str = Field(..., min_length=1, description="约束来源的稳定逻辑路径")
    source_hash: str = Field(
        ...,
        pattern=r"^(?:|[0-9a-f]{64})$",
        description="权威来源 SHA-256；legacy/runtime 来源为空串",
    )
    rule_quote: str = Field(..., min_length=1, description="可回查来源的规则原文")
    evidence_quote: str = Field(..., min_length=1, description="可回查正文的证据原文")
    severity: Literal["hard", "soft"] = Field(..., description="确定性资格校验后的分层")
    ambiguity: str | None = Field(
        default=None,
        description="留白、说谎、不可靠视角等歧义；无歧义时为 null",
    )
    source_chapter_id: str | None = Field(
        default=None,
        pattern=r"^ch-\d{4,}$",
        description="产生该观察的章节；legacy 记录为空",
    )
    source_final_version: int | None = Field(
        default=None,
        ge=1,
        description="产生该观察的 final 版本；写入时可先记录候选版本，传播时必须已批准",
    )
    audiences: list[Literal["planner", "writer", "maintainer"]] = Field(
        default_factory=list,
        description="允许消费该 WARN 的受众；空列表表示只留审计、不向后注入",
    )
    scope: Literal[
        "current_revision", "next_chapter", "book", "maintenance"
    ] = Field(
        default="current_revision",
        description="WARN 的作用范围；HARD 与 legacy SOFT 沿用 current_revision",
    )
    expires_after_chapter_id: str | None = Field(
        default=None,
        pattern=r"^ch-\d{4,}$",
        description="最晚可影响到的目标章节；为空时由 scope 决定是否可传播",
    )
    confidence: Literal["low", "medium", "high"] = Field(
        default="medium",
        description="观察置信度；不改变 HARD/SOFT 身份",
    )
    resolution: Literal["open", "resolved", "dismissed"] = Field(
        default="open",
        description="WARN 生命周期；只有 open 可向后传播",
    )
    suggested_action: str | None = Field(
        default=None,
        description="给目标受众的非阻断行动建议",
    )
    explanation: str | None = Field(
        default=None,
        description="为何产生该观察；evidence_quote 仍保存正文或数据证据",
    )


ConstraintDomain = Literal["must_achieve", "forbidden", "continuity", "setting"]
ConstraintStatus = Literal["pass", "fail"]


def stable_constraint_id(*, source_path: str, domain: str, rule_text: str) -> str:
    """从权威来源语义派生稳定 ID；展示文案与正文证据不得参与。"""
    normalized_rule = " ".join(unicodedata.normalize("NFKC", rule_text).split())
    return hashlib.sha256(
        f"{source_path}\n{domain}\n{normalized_rule}".encode("utf-8")
    ).hexdigest()


class ConstraintResult(BaseModel):
    """一次 HARD 约束在一个正文版本上的结构化 pass/fail 事实。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    constraint_id: str = Field(
        ...,
        pattern=r"^[0-9a-f]{64}$",
        description="只由权威来源路径、约束域和规则语义派生的稳定 SHA-256",
    )
    domain: ConstraintDomain = Field(..., description="四个 HARD 约束域之一")
    source_path: str = Field(..., min_length=1, description="权威规则的稳定逻辑路径")
    source_hash: str = Field(
        default="",
        pattern=r"^(?:|[0-9a-f]{64})$",
        description="权威来源 SHA-256；章节卡字段没有独立 hash 时为空串",
    )
    rule_text: str = Field(
        ..., min_length=1, description="权威规则语义，不是 reviewer 文案"
    )
    status: ConstraintStatus = Field(..., description="本版本对该约束的 pass/fail")
    details: str = Field(
        default="",
        description="仅供人阅读的解释或正文证据；不得参与 ID 或回退比较",
    )


class ReviewResult(BaseModel):
    """审稿结果。

    一次审稿对应一个 ReviewResult,落入 ``Chapter.review_history``。
    """

    review_id: str = Field(..., description="审稿唯一 ID")
    chapter_id: str = Field(..., description="所属章节 ID")
    version_reviewed: int = Field(..., ge=1, description="本次审稿对应的版本号")
    reviewed_at: datetime = Field(..., description="审稿完成时间(UTC)")
    reviewer_model: str = Field(..., description="审稿使用的模型 ID")

    score_plot_progression: float = Field(..., description="情节推进分")
    score_tension_match: float = Field(..., description="张力匹配分")
    score_prose_quality: float = Field(..., description="文笔质量分")
    score_consistency: float = Field(..., description="一致性分")
    score_hook_management: float = Field(..., description="钩子管理分")
    score_voice_consistency: float = Field(..., description="角色声音一致性分(A3)")
    score_template_conformance: float | None = Field(
        default=None,
        description=(
            "模板符合度分(v1.1 wave 4 s5 / I2.2):章节产出与工作区所选写作风格"
            "模板期望(修辞密度 / 对白比例 / 调性等)的契合度。"
            "**仅当工作区选了 style_template 且 LLM 给出该分项时填**;"
            "未选模板的工作区为 ``None``(该维度不参与 score_overall 加权,"
            "verdict 与加该维度前逐字节一致 → 保护 G8 reviewer 基线)。"
        ),
    )
    score_imagery: float | None = Field(
        default=None,
        description=(
            "画面性分(M2,v1.1 wave 5 s2 / G28):本章'画面性(show-vs-tell)'相对"
            "**该书风格意图**的契合度,两层打分(近普世底线=关键戏不偷懒总结;"
            "题材配比=整体 showing 密度贴合 style_template,未选模板时只判底线)。"
            "**纯度量维度,永不进 score_overall 加权、永不影响 verdict**(下游对 "
            "prose 卓越性盲重写无效,见 w5-s1 postmortem §4)—— 只供唯一稿"
            "质量观察 + 离线慢调报表。LLM 漏产 / 主审退化 → ``None``。"
        ),
    )
    score_interiority: float | None = Field(
        default=None,
        description=(
            "内心戏分(M2,v1.1 wave 5 s3):本章'内心戏(角色内在呈现)深度'相对"
            "**该书风格意图**的契合度,两层打分(近普世底线=情感节点 / 转折 / 抉择"
            "不被抽象词一笔带过;题材配比=整体内心戏密度贴合 style_template —— 文学/"
            "宏大叙事深挖、搞笑/爽文精简,未选模板时只判底线)。与 score_imagery 同"
            "属'卓越性'纯度量维度,**永不进 score_overall 加权、永不影响 verdict**"
            "(下游对 prose 卓越性盲重写无效,见 w5-s1 postmortem §4)—— 只供唯一稿"
            "质量观察 + 离线慢调报表。LLM 漏产 / 主审退化 → ``None``。"
        ),
    )
    score_formulaic: float | None = Field(
        default=None,
        description=(
            "套路化分(M2,v1.1 wave 5 s9):本章'套路化 / 机器流水线味'程度,两层"
            "打分(近普世底线=机械复读 /「A而B」对仗成癖 / 明喻句式雷同 / 喻体通货 /"
            "强度副词通胀 / 章末套路收尾越密越高;题材配比=类型约定俗成结构〔爽文打"
            "脸节奏 / 悬疑误导〕不算坏套路、不罚)。**⚠️ 唯一'越高越差'的维度**"
            "(其余 score 越高越好;此维高=越套路=越机器)。纯度量,**永不进 "
            "score_overall 加权、永不影响 verdict**(纠正侧在 ① craft + M3,见 w5-s9 "
            "M3)—— 只供唯一稿质量观察 + 离线慢调报表(配 M3 看套路化是否在降)。"
            "LLM 漏产 / 主审退化 → ``None``。"
        ),
    )
    score_immersion: float | None = Field(
        default=None,
        description=(
            "沉浸感分(M2,v1.1 wave 5 s9):本章'读者沉浸感 / 代入感'水准,两层打分"
            "(近普世底线=读者被拉进场景 vs 始终隔一层抽离;题材配比=沉浸方式贴合"
            "style_template 调性)。与 score_imagery / interiority 同属'卓越性'纯度量"
            "维度,**永不进 score_overall 加权、永不影响 verdict**(沉浸感涌现 / 抽象,"
            "无提示词纠正杠杆,见 design §M2 ④)—— **只供唯一稿质量观察** + "
            "离线慢调报表。LLM 漏产 / 主审退化 → ``None``。"
        ),
    )

    score_overall: float = Field(..., description="综合分")

    hard_constraints_passed: bool = Field(..., description="硬约束是否全部通过")
    failed_constraints: list[str] = Field(..., description="未通过的硬约束列表")

    issues: list[ReviewIssue] = Field(..., description="命中问题列表(A2)")
    suggestions: list[str] = Field(..., description="改进建议列表")
    verdict: Literal["pass", "revise", "rewrite", "escalate_to_human"] = Field(
        ..., description="判决结果"
    )
    revise_targets: list[ReviewIssue] = Field(
        ..., description="若 verdict=revise,Reviser 应处理的目标问题列表"
    )
    constraint_violations: list[ConstraintViolation] = Field(
        default_factory=list,
        description="写后约束校验产生的结构化违规；不得从展示字符串反解析",
    )
    constraint_results: list[ConstraintResult] = Field(
        default_factory=list,
        description="四个 HARD 域逐条 pass/fail 结果；rewrite 只按 constraint_id 比较",
    )
    hard_constraint_regressions: list[str] = Field(
        default_factory=list,
        description="本版本发生 pass→fail 的稳定 constraint ID；旧审稿记录为空列表",
    )
    validator_failure_details: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "v1.1 G8:`_apply_validator_pipeline` 失败明细(每条 = {name, details}),"
            "供 Reviser 局部 patch 用;仅 light 失败(单条 word_count + 偏差 < 5%)"
            "派 revise 时由 Orchestrator 写入。不持久化到 ORM(reviser 同 session"
            "消费,LogsPage 从 `validator_failed` 事件流读历史明细)。"
        ),
    )
