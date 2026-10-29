"""写作风格模板 API schema(v1.1 wave 4 session 3 / I1)。

``GET /api/v1/style-templates`` 响应体。复用 core ``StyleTemplateDetail``
(元数据 + 正文预览),与 ``WorkspaceOut(Workspace)`` 同型:API 响应直接继承
core 模型,不另立字段。
"""

from __future__ import annotations

from src.core.models.style_template import StyleTemplateDetail


class StyleTemplateOut(StyleTemplateDetail):
    """单个风格模板响应元素(元数据 + 风格指南正文预览)。"""
