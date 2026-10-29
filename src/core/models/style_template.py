"""写作风格模板枚举 + 注册表(v1.1 wave 4 session 3 / I1 引入)。

candidates §I1:让用户在工作区创建时选一个"写作风格模板"(类似
``tension_template`` 节奏模板),系统级引导 LLM 产出对应风格的小说。控的是
文笔 / 叙事 / 修辞 / 节奏等"读者实际能感受到的差异"。

本模块只放**静态枚举 + 展示元数据**(import-safe,不做 IO),完全复刻
``src/core/models/volume.py`` 的 ``tension_template`` 模式:

- ``STYLE_TEMPLATE_VALUES`` / ``StyleTemplateName`` 给 Pydantic 模型 + OpenAPI
  类型安全(前端下拉自动同源,不手写后端枚举);
- ``STYLE_TEMPLATES`` 注册表给 ``GET /api/v1/style-templates`` 端点 + 前端
  预览(每模板 10 维度的展示文案);
- 风格指南**正文**不在本模块,落 ``prompts/style_guide_templates/<name>.md``
  (符合"提示词改动留版本"纪律),由 ``WorkspaceService.create`` 据所选模板
  写入工作区 ``prompts/style_guide.md``,Writer 运行时读取注入 prompt。

**与 I2 的边界**:第⑨维(章节长度)/ 第⑩维(事件数量)在 I1 仅作 style_guide
**定性引导**,**不**改 ``ChapterCard.word_count`` / ``must_achieve`` 默认值
(那是 candidates §I2"模板影响 generation_params + ChapterCard 默认值"的范围)。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: ``Workspace.style_template`` 允许的 7 档枚举值集合。
#:
#: 保留 candidates §I1 六个预设，并增加修仙成长。``None``(不选模板)是合法状态:
#: 工作区不生成 ``prompts/style_guide.md``,Writer 退化为既有"前章风格指纹
#: 跨章基线"隐式控制(与 I1 之前行为一致,向后兼容)。
STYLE_TEMPLATE_VALUES: tuple[str, ...] = (
    "comedic_burst",
    "grand_epic",
    "heroic_battle",
    "dark_mystery",
    "healing_daily",
    "classical_xianxia",
    "xianxia_growth",
)

StyleTemplateName = Literal[
    "comedic_burst",
    "grand_epic",
    "heroic_battle",
    "dark_mystery",
    "healing_daily",
    "classical_xianxia",
    "xianxia_growth",
]


class StyleDimensions(BaseModel):
    """单个风格模板的 10 个可控维度展示文案(candidates §I1)。

    纯展示用(前端预览的维度表 + 起草正文的依据);**不是**运行时硬约束。
    运行时引导由生成的 ``style_guide.md`` 正文承担。
    """

    genre_tone: str = Field(..., description="① 题材调性(搞笑/严肃/热血/治愈/悬疑/暗黑)")
    narrative_density: str = Field(..., description="② 叙事密度(快/缓/多线交织)")
    rhetoric_density: str = Field(..., description="③ 修辞密度(朴素/中等/华丽)")
    dialogue_ratio: str = Field(..., description="④ 对白比例(对话主导/均衡/描写主导)")
    pov_switching: str = Field(..., description="⑤ POV 切换频率(单POV严守/章内多POV)")
    power_pacing: str = Field(..., description="⑥ 金手指节奏(每章打脸/每卷一爽点/弱爽文)")
    worldbuilding_density: str = Field(
        ..., description="⑦ 世界观密度(每章新设定/每卷新设定/静态世界)"
    )
    emotional_tone: str = Field(..., description="⑧ 情感色调(阳光/灰暗/中性)")
    chapter_length: str = Field(
        ...,
        description="⑨ 章节长度倾向(定性引导,I1 不改 ChapterCard.word_count)",
    )
    event_density: str = Field(
        ...,
        description="⑩ must_achieve 数量倾向(定性引导,I1 不改 ChapterCard 默认)",
    )


class StyleGenerationDefaults(BaseModel):
    """单个风格模板的**生成参数硬默认**(v1.1 wave 4 session 3 / I2.1 引入)。

    I1 的 ``StyleDimensions`` 第⑨维(章节长度)/ 第⑩维(事件数量)仅作 style_guide
    **定性引导**;I2.1 把这两维**落为硬默认**,影响:

    - ``word_count_min`` / ``word_count_max`` → 新生成 ``ChapterCard`` 字数区间默认
      (经 ``GenerationConfig`` 合并管线,project.yaml L1 用户覆盖仍优先);
    - ``plot_node_budget`` → 新卡剧情节点预算默认(多事件模板 5 / 少事件模板 3);
    - ``must_achieve_max`` → 章节卡生成器 prompt 的 must_achieve 数量上限**软提示**
      (非硬截断 — must_achieve 由 LLM 产出且 ``min_length>=1``,硬截断有丢必达事件
      风险;故只在 prompt 引导"控制在 N 项以内");
    - ``writer_temperature`` → Writer 采样温度 override(T-A,**仅** ``write_chapter``
      router 路径覆盖 ``complete_kwargs["temperature"]``;reviewer / validator /
      chapter_card_generator **都不经过 write_chapter**,按构造不受影响 → 保护 G8
      reviewer 评分稳定性治理)。

    数值来源:① word_count / plot_node / must_achieve 由 I1 ``STYLE_TEMPLATES``
    第⑨⑩维定性文案 materialize(如 comedic_burst "偏短 2800-3500 字 / 多事件 4-5 件"
    → min=2800 / max=3500 / budget=5 / must_max=5),与 w4-s3 已批准文案一致;
    ② temperature 由 candidates §I2 字面("搞笑 0.9 灵活 / 宏大 0.65 稳重")+ 各模板
    调性语义推导(详 impl 文档 §自决项数值表)。
    """

    model_config = ConfigDict(extra="forbid")

    word_count_min: int = Field(..., ge=1, description="新卡字数下限默认")
    word_count_max: int = Field(..., ge=1, description="新卡字数上限默认")
    plot_node_budget: int = Field(..., ge=1, description="新卡剧情节点预算默认")
    must_achieve_max: int = Field(..., ge=1, description="must_achieve 数量上限软提示")
    writer_temperature: float = Field(
        ..., ge=0.0, le=2.0, description="Writer 采样温度 override(T-A,仅 write_chapter)"
    )

    @model_validator(mode="after")
    def _validate_word_count_ordering(self) -> "StyleGenerationDefaults":
        if self.word_count_min > self.word_count_max:
            raise ValueError(
                "word_count_min 不能大于 word_count_max "
                f"(当前 min={self.word_count_min} > max={self.word_count_max})"
            )
        return self


class StyleTemplateMeta(BaseModel):
    """单个风格模板的展示元数据(注册表条目 + 端点响应元素)。"""

    name: StyleTemplateName = Field(..., description="模板枚举值(英文 key)")
    label: str = Field(..., description="人类可读名称(中文)")
    description: str = Field(..., description="一句话定位 + 适合题材")
    dimensions: StyleDimensions = Field(..., description="10 个可控维度展示文案")


#: 7 个预设模板注册表。``name`` 即 ``prompts/style_guide_templates/<name>.md``
#: 的文件名。维度文案 1:1 对齐 candidates §I1 的 6 模板表 + 10 维度定义。
STYLE_TEMPLATES: dict[str, StyleTemplateMeta] = {
    "comedic_burst": StyleTemplateMeta(
        name="comedic_burst",
        label="搞笑爽文",
        description="搞笑+阳光、节奏快、每章打脸,适合玄幻 / 都市轻松向。",
        dimensions=StyleDimensions(
            genre_tone="搞笑",
            narrative_density="快",
            rhetoric_density="朴素",
            dialogue_ratio="对话主导",
            pov_switching="单 POV 严守",
            power_pacing="每章打脸",
            worldbuilding_density="每卷新设定",
            emotional_tone="阳光",
            chapter_length="偏短(约 2800-3500 字)",
            event_density="多事件(每章约 4-5 件)",
        ),
    ),
    "grand_epic": StyleTemplateMeta(
        name="grand_epic",
        label="宏大叙事",
        description="严肃+灰暗、多线交织、华丽文笔,适合史诗 / 仙侠正剧。",
        dimensions=StyleDimensions(
            genre_tone="严肃",
            narrative_density="缓 + 多线交织",
            rhetoric_density="华丽",
            dialogue_ratio="描写主导",
            pov_switching="章内多 POV",
            power_pacing="每卷一爽点",
            worldbuilding_density="每章新设定",
            emotional_tone="灰暗",
            chapter_length="偏长(约 4500-6000 字)",
            event_density="少事件、多深度(每章约 2-3 件)",
        ),
    ),
    "heroic_battle": StyleTemplateMeta(
        name="heroic_battle",
        label="热血战斗",
        description="热血+阳光、单线推进、武打华丽,适合战斗番 / 玄幻打怪。",
        dimensions=StyleDimensions(
            genre_tone="热血",
            narrative_density="中 + 单线",
            rhetoric_density="中等(武打段偏华丽)",
            dialogue_ratio="均衡",
            pov_switching="单 POV 严守",
            power_pacing="每卷一爽点",
            worldbuilding_density="每卷新设定",
            emotional_tone="阳光",
            chapter_length="适中(约 3500-4500 字)",
            event_density="多事件(每章约 4-5 件)",
        ),
    ),
    "dark_mystery": StyleTemplateMeta(
        name="dark_mystery",
        label="暗黑悬疑",
        description="悬疑+灰暗、单 POV 沉浸、弱爽,适合无限流 / 推理。",
        dimensions=StyleDimensions(
            genre_tone="悬疑",
            narrative_density="缓 + 单 POV",
            rhetoric_density="中等",
            dialogue_ratio="描写主导",
            pov_switching="单 POV 严守",
            power_pacing="弱爽文",
            worldbuilding_density="静态世界",
            emotional_tone="灰暗",
            chapter_length="适中(约 3500-4500 字)",
            event_density="少事件、多深度(每章约 2-3 件)",
        ),
    ),
    "healing_daily": StyleTemplateMeta(
        name="healing_daily",
        label="治愈日常",
        description="治愈+阳光、节奏舒缓、情感流,适合都市治愈 / 慢生活。",
        dimensions=StyleDimensions(
            genre_tone="治愈",
            narrative_density="缓 + 均衡",
            rhetoric_density="中等",
            dialogue_ratio="均衡",
            pov_switching="单 POV 严守",
            power_pacing="弱爽文 + 情感流",
            worldbuilding_density="静态世界",
            emotional_tone="阳光",
            chapter_length="适中(约 3500-4500 字)",
            event_density="少事件、多深度(每章约 2-3 件)",
        ),
    ),
    "classical_xianxia": StyleTemplateMeta(
        name="classical_xianxia",
        label="古典仙侠",
        description="严肃+中性、描写主导、古典华丽,适合修真 / 古风。",
        dimensions=StyleDimensions(
            genre_tone="严肃(中性)",
            narrative_density="缓 + 描写主导",
            rhetoric_density="华丽 + 古典",
            dialogue_ratio="描写主导",
            pov_switching="单 POV 严守",
            power_pacing="慢热(弱爽)",
            worldbuilding_density="每卷新设定",
            emotional_tone="中性",
            chapter_length="偏长(约 4500-6000 字)",
            event_density="少事件、多深度(每章约 2-3 件)",
        ),
    ),
    "xianxia_growth": StyleTemplateMeta(
        name="xianxia_growth", label="修仙成长",
        description="朴实有情、人物鲜明、张弛有别，让修行与人情共同改变处境。",
        dimensions=StyleDimensions(
            genre_tone="成长、探索与人情", narrative_density="适度紧凑、张弛有别",
            rhetoric_density="朴实自然", dialogue_ratio="随场景需要，自然交流",
            pov_switching="单 POV 严守", power_pacing="阶段反馈可感，不设突破或爽点配额",
            worldbuilding_density="随经历展开", emotional_tone="有温度、有个人感受",
            chapter_length="2500–3500，以本书明确要求为准",
            event_density="重要经历充分展开，日常按意义取舍",
        ),
    ),
}


#: 7 个预设模板的**生成参数硬默认**注册表(v1.1 wave 4 session 3 / I2.1)。
#: key 与 ``STYLE_TEMPLATES`` 同源;数值由 I1 第⑨⑩维定性文案 materialize +
#: candidates §I2 temperature 字面推导(详 ``StyleGenerationDefaults`` docstring
#: + impl 文档 §自决项数值表)。``Workspace.style_template`` 未选(``None``)时
#: 不查本表,新卡走 ``GenerationConfig`` 引擎默认 + Writer 走 profile 温度。
STYLE_GENERATION_DEFAULTS: dict[str, StyleGenerationDefaults] = {
    # 搞笑爽文:偏短 2800-3500 / 多事件 4-5 / 搞笑灵活高温
    "comedic_burst": StyleGenerationDefaults(
        word_count_min=2800,
        word_count_max=3500,
        plot_node_budget=5,
        must_achieve_max=5,
        writer_temperature=0.9,
    ),
    # 宏大叙事:偏长 4500-6000 / 少事件多深度 2-3 / 严肃稳重低温
    "grand_epic": StyleGenerationDefaults(
        word_count_min=4500,
        word_count_max=6000,
        plot_node_budget=3,
        must_achieve_max=3,
        writer_temperature=0.65,
    ),
    # 热血战斗:适中 3500-4500 / 多事件 4-5 / 热血偏高温
    "heroic_battle": StyleGenerationDefaults(
        word_count_min=3500,
        word_count_max=4500,
        plot_node_budget=5,
        must_achieve_max=5,
        writer_temperature=0.85,
    ),
    # 暗黑悬疑:适中 3500-4500 / 少事件多深度 2-3 / 沉浸中温
    "dark_mystery": StyleGenerationDefaults(
        word_count_min=3500,
        word_count_max=4500,
        plot_node_budget=3,
        must_achieve_max=3,
        writer_temperature=0.7,
    ),
    # 治愈日常:适中 3500-4500 / 少事件多深度 2-3 / 舒缓情感流中温
    "healing_daily": StyleGenerationDefaults(
        word_count_min=3500,
        word_count_max=4500,
        plot_node_budget=3,
        must_achieve_max=3,
        writer_temperature=0.75,
    ),
    # 古典仙侠:偏长 4500-6000 / 少事件多深度 2-3 / 古典稳重中温
    "classical_xianxia": StyleGenerationDefaults(
        word_count_min=4500,
        word_count_max=6000,
        plot_node_budget=3,
        must_achieve_max=3,
        writer_temperature=0.7,
    ),
    "xianxia_growth": StyleGenerationDefaults(
        word_count_min=2500, word_count_max=3500, plot_node_budget=4,
        must_achieve_max=4, writer_temperature=0.7,
    ),
}


class StyleTemplateDetail(StyleTemplateMeta):
    """模板元数据 + 风格指南正文预览(``GET /style-templates`` 响应元素)。

    ``preview`` 是 ``prompts/style_guide_templates/<name>.md`` 的完整正文,供前端
    NewBookPage 选中模板后预览。由 ``StyleTemplateService`` 读文件填充(本模型
    本身不做 IO)。
    """

    preview: str = Field(..., description="风格指南正文预览(markdown)")


def style_guide_filename(name: str) -> str:
    """风格模板对应的 ``prompts/style_guide_templates/`` 文件名。

    不做存在性校验(纯字符串拼接);校验由调用方
    (``WorkspaceService.create``)在读文件时处理。
    """

    return f"{name}.md"
