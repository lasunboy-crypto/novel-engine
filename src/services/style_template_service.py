"""写作风格模板查询服务(v1.1 wave 4 session 3 / I1)。

为 ``GET /api/v1/style-templates`` 端点提供 7 个预设模板的元数据 + 风格指南
正文预览。模板是引擎级静态资源(非工作区资源),故本服务不绑 workspace、不碰
DB,只读 ``prompts/style_guide_templates/<name>.md`` 正文 + 拼 ``STYLE_TEMPLATES``
注册表元数据。
"""

from __future__ import annotations

from pathlib import Path

import structlog

from src.config import settings
from src.core.models.style_template import (
    STYLE_TEMPLATES,
    StyleTemplateDetail,
    style_guide_filename,
)

_log = structlog.get_logger(__name__)

#: 引擎级风格指南模板目录(相对 ``settings.prompts_dir``);与
#: ``WorkspaceService._STYLE_GUIDE_TEMPLATES_SUBDIR`` 同源。
_STYLE_GUIDE_TEMPLATES_SUBDIR = "style_guide_templates"


class StyleTemplateService:
    """风格模板只读查询服务。"""

    def __init__(self, *, prompts_dir: Path | None = None) -> None:
        self._templates_dir = (
            (prompts_dir if prompts_dir is not None else settings.prompts_dir)
            .resolve()
            / _STYLE_GUIDE_TEMPLATES_SUBDIR
        )

    def list_templates(self) -> list[StyleTemplateDetail]:
        """返回 7 个预设模板(元数据 + 正文预览),保持注册表声明顺序。

        某模板正文文件缺失 → 预览退化为空串(仍返回元数据,不整体报错)。
        """

        details: list[StyleTemplateDetail] = []
        for name, meta in STYLE_TEMPLATES.items():
            preview = self._read_preview(name)
            details.append(
                StyleTemplateDetail(**meta.model_dump(), preview=preview)
            )
        return details

    def _read_preview(self, name: str) -> str:
        path = self._templates_dir / style_guide_filename(name)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            _log.warning(
                "style_guide_template_missing",
                style_template=name,
                template_path=str(path),
            )
            return ""
