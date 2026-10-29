"""角色与角色声音档案模型(对应 PROJECT_SPEC §3.3)。"""

from typing import Literal

from pydantic import BaseModel, Field


class CharacterCoreFact(BaseModel):
    """角色核心事实；仍存于既有 ``state_timeline``。"""

    state_key: str = Field(min_length=1)
    attribute: Literal[
        "identity", "life_status", "permanent_injury", "amnesia", "faction"
    ]
    value: str = Field(min_length=1)
    source_kind: Literal["approved_text", "outline_characters"] = "approved_text"
    source_chapter_id: str = Field(min_length=1)
    source_version_id: int = Field(ge=0)
    source_path: str = Field(min_length=1)
    source_text_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_quote: str = Field(min_length=1)
    valid_from_chapter: int = Field(ge=0)
    valid_to_chapter: int | None = Field(default=None, ge=0)
    supersedes_state_key: str | None = None


class CharacterStateChange(BaseModel):
    """角色在某一章发生的单个状态变化事件(G1 时序状态追踪,v1.1 wave 4 session 1)。

    由 MemoryExtractor 从章节正文抽取,记录"本章这个角色身上发生了什么"——
    用于防"断臂复活"(主角第 10 章断臂,第 50 章打斗时向量检索把第 5 章四肢
    健全设定检索出来 → LLM 让断臂瞬间复活)。``event_type`` 是粗分类,
    ``description`` 是中文自然语言描述(供 Writer 注入 prompt 时人读)。
    """

    event_type: Literal["injury", "loss", "gain", "relationship", "other"] = Field(
        ...,
        description="变化类型:injury 受伤 / loss 失去(物品/能力) / "
        "gain 获得(物品/能力) / relationship 关系变化 / other 其他",
    )
    description: str = Field(..., description="本次变化的中文描述")


class CharacterStateSnapshot(BaseModel):
    """角色截止某一章末的状态快照(G1 时序状态追踪,v1.1 wave 4 session 1)。

    ``state_timeline`` 列表里每章一条(按 ``as_of_chapter`` 升序)。Writer 写
    第 N 章时**显式 SQL lookup**"截止 N-1 章的最新 snapshot"注入 prompt,
    完全绕开 embedding(向量检索可能把过期状态检索出来)。

    ``injuries`` / ``acquired_items`` / ``current_relationships`` 是**累积**
    字段(MemoryExtractor 在拿到上一章 snapshot + 本章正文后由 LLM 折叠得到
    "截止本章的累积状态");``state_changes`` 是本章发生的**增量**事件(可追溯
    "何时受伤 / 何时获得")。
    """

    as_of_chapter: int = Field(
        ...,
        ge=0,
        description="快照对应的全局章序(从 chapter_id ``ch-NNNN`` 解析,跨卷"
        "单调;非卷内 chapter_number)",
    )
    physical_state: str = Field(
        default="", description="截止本章的身体 / 整体状态一句话描述"
    )
    injuries: list[str] = Field(
        default_factory=list, description="截止本章的累积伤势列表"
    )
    acquired_items: list[str] = Field(
        default_factory=list, description="截止本章持有的关键物品 / 习得能力列表"
    )
    current_relationships: dict[str, str] = Field(
        default_factory=dict,
        description="截止本章的关系快照,key=对方角色 ID,value=关系描述",
    )
    state_changes: list[CharacterStateChange] = Field(
        default_factory=list, description="本章发生的增量状态变化事件"
    )
    progression_rank: str | None = Field(
        default=None,
        description="进阶等级(v1.1 wave 5 s10 / 模块② 境界一致性,题材中性)。截止本章的"
        "当前进阶位置字符串(仙侠=境界'炼气六层巅峰' / 战斗=战力/段位)。**仅 3 题材"
        "(古典仙侠/搞笑爽文/热血战斗)+ 工作区开关 ON 时才抽取/注入**;其余题材或 OFF "
        "恒为 ``None`` → writer 不注入 = 逐字节零回归。方向无关:升(突破)、降(突破失败/"
        "被打落等合法倒退)都如实记。阶梯顺序留 power_system(world.json),本字段只记位置。",
    )
    authority: Literal[
        "rich_memory", "approved_text", "outline_characters"
    ] = Field(
        default="rich_memory",
        description=(
            "快照最高权威来源。outline_characters 表示正文前角色基线；"
            "approved_text 表示同步核心提交已写入不可逆事实；"
            "富记忆只能补充非核心字段，不能替换 core_facts。"
        ),
    )
    source_chapter_id: str | None = None
    source_version_id: int | None = Field(default=None, ge=0)
    source_text_hash: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    evidence_quotes: list[str] = Field(default_factory=list)
    core_facts: list[CharacterCoreFact] = Field(
        default_factory=list,
        description="截止本章仍有效的角色不可逆事实；Writer 以当前 HARD 注入。",
    )


def latest_state_snapshot_before(
    state_timeline: list["CharacterStateSnapshot"], before_chapter: int
) -> "CharacterStateSnapshot | None":
    """取 ``state_timeline`` 中 ``as_of_chapter < before_chapter`` 的最新一条快照。

    用于 Writer 写第 N 章时拿"截止 N-1 章"的累积状态(``before_chapter`` 传
    第 N 章的全局序);无符合条件的快照返回 ``None``。
    """
    candidates = [s for s in state_timeline if s.as_of_chapter < before_chapter]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.as_of_chapter)


def format_state_snapshot_lines(snapshot: "CharacterStateSnapshot") -> list[str]:
    """把一条状态快照渲染成人读中文行(G1 共享渲染,避免 extractor / writer 漂移)。

    只渲染非空字段;全空时返回空列表(调用方据此决定是否输出该角色)。
    """
    lines: list[str] = []
    if snapshot.physical_state:
        lines.append(f"身体状态:{snapshot.physical_state}")
    if snapshot.injuries:
        lines.append("伤势:" + "、".join(snapshot.injuries))
    if snapshot.acquired_items:
        lines.append("持有物品 / 能力:" + "、".join(snapshot.acquired_items))
    if snapshot.current_relationships:
        rel = "、".join(
            f"{k}={v}" for k, v in snapshot.current_relationships.items()
        )
        lines.append("关系:" + rel)
    return lines


class VoiceProfile(BaseModel):
    """角色声音档案(A3)。

    描述角色的对白与语言风格规范,用于 Writer 拼装 prompt 与
    Reviewer 的 score_voice_consistency / forbidden_phrases 校验。
    """

    catchphrases: list[str] = Field(
        default_factory=list, description="角色口头禅列表"
    )
    tone: str = Field(..., description="语气描述,如 冷峻 / 戏谑 / 温和")
    sentence_style: str = Field(..., description="句式偏好,如 短句直白 / 长句修饰")
    self_address: str = Field(..., description="自称,如 我 / 朕 / 在下")
    addresses_others: dict[str, str] = Field(
        default_factory=dict,
        description="对他人的称呼映射,key=对方角色 ID,value=称呼",
    )
    forbidden_phrases: list[str] = Field(
        default_factory=list, description="该角色不会说的词或句式列表"
    )


class IdentityFacet(BaseModel):
    """角色的单个"身份侧写"(v1.1 wave 6 session 2 / A3 引入)。

    **通用建模**"同一角色在不同情境下的身份 / 职业 / 社会角色"——双界(现实 /
    梦境)、卧底(真实身份 / 伪装身份)、易容(本尊 / 化名)、双重生活(白天上班 /
    夜里黑客)等都是它的特例。**简单角色 ``identity_facets`` 留空**(无多重身份的
    角色不受影响)。

    治 B1 暴露的两个 bug:① 沈舟双界身份被写串(现实=心理医生却被写成"刑警");
    ② 白露现实侧整章丢失(只写梦境侧)。机制:① writer 把出场角色的全部 facet
    作**固定上下文注入**(知道"谁在哪个情境是什么身份");② mini_plan 覆盖自检
    确保每个 facet 至少获得一章交代;③ 职业称谓黑名单代码粗筛标记"他角色专属
    职业词 + 本角色名"邻接(如"现代刑警沈舟")。

    设计要点:``identity`` 写**简短身份 / 职业名词**(如"心理医生" / "卧底锦衣卫"),
    不要写成整句——下游职业黑名单粗筛把它作为"职业词"做邻接匹配。
    """

    context: str = Field(
        ...,
        min_length=1,
        description='情境标签,如 "现实" / "梦境" / "朝堂" / "江湖" / "白天" / "潜伏期"',
    )
    identity: str = Field(
        ...,
        min_length=1,
        description='该情境下的身份 / 职业 / 社会角色简短名词,如 "心理医生" / "卧底锦衣卫"',
    )


class Character(BaseModel):
    """角色卡。

    描述单个角色的基础信息、声音档案、目标、关系、能力与隐藏信息。
    工作区物理隔离,跨工作区资源必含 ``workspace_id``(对齐 CLAUDE.md 硬约束 9)。
    P2 阶段由人工或静态资产导入,P5 由 OutlineBuilder 生成初稿。
    """

    character_id: str = Field(..., description="角色唯一 ID,工作区内不冲突")
    workspace_id: str = Field(..., description="所属工作区 ID")
    name: str = Field(..., description="角色名(显示用)")
    aliases: list[str] = Field(default_factory=list, description="别名列表")
    archetype: str = Field(..., description="原型,如 反派宗师 / 萌新主角")
    appearance: str = Field(..., description="外貌描述")
    personality: str = Field(..., description="性格描述")
    background: str = Field(..., description="背景故事")
    identity_facets: list[IdentityFacet] = Field(
        default_factory=list,
        description=(
            "角色多情境身份侧写(v1.1 wave 6 session 2 / A3)。空 = 无多重身份的简单"
            "角色。非空时 Writer 把全部 facet 作固定上下文注入(防双界身份写串 / 丢失);"
            "职业黑名单粗筛据此标记'他角色专属职业词 + 本角色名'邻接。"
        ),
    )
    voice_profile: VoiceProfile = Field(..., description="角色声音档案(A3)")
    goals_long_term: list[str] = Field(..., description="长期目标列表")
    goals_short_term: list[str] = Field(..., description="短期目标列表")
    current_state: dict = Field(
        default_factory=dict, description="当前状态字典(伤势 / 情绪 / 位置等)"
    )
    state_timeline: list[CharacterStateSnapshot] = Field(
        default_factory=list,
        description="角色时序状态快照(G1):每章一条,按 as_of_chapter 升序;"
        "Writer 写第 N 章时取截止 N-1 章的最新一条注入 prompt",
    )
    relationships: dict[str, str] = Field(
        default_factory=dict,
        description="关系映射,key=对方角色 ID,value=关系描述",
    )
    abilities: list[str] = Field(default_factory=list, description="能力列表")
    secrets: list[str] = Field(
        default_factory=list, description="角色隐藏的秘密(供作者侧使用)"
    )
    first_appearance_chapter: str | None = Field(
        None, description="首次出场章节 ID,未出场为 None"
    )
    last_updated_chapter: str | None = Field(
        None, description="最近被更新的章节 ID,未更新为 None"
    )
