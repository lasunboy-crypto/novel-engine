"""章节版本相关请求/响应 schema(P1-B-19)。

P1 阶段版本元数据(``creation_type`` / ``model_used`` / ``review_score`` /
``created_at`` / ``word_count`` / ``notes``)只活在内存里,文件系统只落正文
``v{N}.md``。因此本模块刻意拆成两组:

- **从文件系统能列出的**(``ChapterVersionItem`` / ``ChapterVersionList``):
  只有 ``version_id`` 这一个能可靠推断的字段。``GET /chapters/{id}/versions``
  返回。
- **新生产时内存里的完整元数据**(``ChapterVersionMeta``):``:revise`` 跑完
  当下能给出全部字段;``GET /versions`` 列表里则不可能给(P2 落 chapters 表后才补)。

正文单独走 ``ChapterVersionContent``(``GET /versions/{version_id}``)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChapterVersionItem(BaseModel):
    """章节版本列表元素(P1 文件系统视角,只有 version_id)。

    P2 落 ``chapter_versions`` 表后会扩字段(creation_type / created_at /
    model_used / review_score 等);P1 阶段文件系统给不了这些信息,**不强行
    返回 None 占位**(不诚实,易误导前端)。
    """

    model_config = ConfigDict(extra="forbid")

    version_id: int = Field(..., ge=1)


class ChapterVersionList(BaseModel):
    """``GET /chapters/{chapter_id}/versions`` 响应 data 段。"""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    versions: list[ChapterVersionItem]


class ChapterVersionContent(BaseModel):
    """``GET /chapters/{chapter_id}/versions/{version_id}`` 响应 data 段。"""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    version_id: int = Field(..., ge=1)
    content: str = Field(..., description="本版本正文")
    word_count: int = Field(..., ge=0)


class ChapterVersionMeta(BaseModel):
    """新生产章节版本的完整元数据(``:revise`` 跑完时返,不含正文)。

    字段一一对应 ``core.models.chapter.ChapterVersion`` 减去 ``content``——正文
    可立刻从 ``GET /versions/{version_id}`` 拉,不重复传(请求体可能上千 token)。
    """

    model_config = ConfigDict(extra="forbid")

    version_id: int = Field(..., ge=1)
    word_count: int = Field(..., ge=0)
    created_at: datetime
    creation_type: Literal["draft", "rewrite", "revise", "manual_edit"]
    model_used: str
    review_score: float | None = None
    notes: str = ""


class StyleFingerprintPoint(BaseModel):
    """单版本风格指纹时序点(v1.1 w3-s7 H7)。

    供前端折线图展示 v1→vN 风格漂移。4 个数值维度 + 主导人称 ``dominant_pov``
    (类别)。``has_fingerprint=False`` 的旧版本各维度为 None(诚实标注缺数据)。
    """

    model_config = ConfigDict(extra="forbid")

    version_id: int = Field(..., ge=1)
    creation_type: str = Field(..., description="draft / rewrite / revise / manual_edit")
    has_fingerprint: bool = Field(
        ..., description="该版本是否有 style_fingerprint(G5 落地前的旧版本为 false)"
    )
    paragraph_length_mean: float | None = Field(
        None, description="段落字符数均值;无指纹时 None"
    )
    sentence_length_mean: float | None = Field(
        None, description="句子字符数均值;无指纹时 None"
    )
    simile_density: float | None = Field(
        None, description="比喻密度(像/如同/仿佛/犹如 次数 / 句数);无指纹时 None"
    )
    exclamation_density: float | None = Field(
        None, description="感叹密度(！？!? 次数 / 句数);无指纹时 None"
    )
    dominant_pov: Literal["third_person", "first_person", "mixed"] | None = Field(
        None, description="主导人称视角(代词频次推断);无指纹时 None"
    )


class StyleDriftSeries(BaseModel):
    """``GET /chapters/{chapter_id}/style-drift`` 响应 data 段(v1.1 w3-s7 H7)。"""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    points: list[StyleFingerprintPoint] = Field(
        ..., description="按 version_id 升序的逐版本风格指纹时序点"
    )


class SwitchVersionIn(BaseModel):
    """``POST /chapters/{chapter_id}:switch-version`` 请求体(决策 7 方案 B)。

    把 final 指针切到指定版本(等价于 ``ChapterVersioning.mark_final``)。
    """

    model_config = ConfigDict(extra="forbid")

    version_id: int = Field(..., ge=1, description="要切到的目标版本号(对应 v{N}.md)")


class SwitchVersionResult(BaseModel):
    """``:switch-version`` 成功响应 data 段。"""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    version_id: int = Field(..., ge=1)
    final_path: str = Field(..., description="final.md 的绝对路径")
