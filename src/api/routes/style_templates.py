"""写作风格模板路由(v1.1 wave 4 session 3 / I1)。

``GET /api/v1/style-templates`` — 列出 6 个预设写作风格模板(元数据 + 风格指南
正文预览),供前端 NewBookPage 渲染"写作风格模板"下拉 + 选中预览。

模板是引擎级静态资源(非工作区子资源),故不嵌 ``workspace_id``;URL 形态对齐
"全局资源列表"语义。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import get_style_template_service
from src.api.schemas.common import SuccessEnvelope
from src.api.schemas.style_templates import StyleTemplateOut
from src.services.style_template_service import StyleTemplateService

router = APIRouter(prefix="/style-templates", tags=["style_templates"])


@router.get(
    "",
    response_model=SuccessEnvelope[list[StyleTemplateOut]],
    summary="列出预设写作风格模板(I1)",
    description=(
        "返回 6 个预设写作风格模板的元数据(名称 / 标签 / 定位 / 10 维度)+ 风格"
        "指南正文预览,供 NewBookPage 选择 + 预览。工作区创建时把所选模板正文写入"
        "工作区 prompts/style_guide.md,Writer 运行时读取注入。"
    ),
)
async def list_style_templates(
    service: StyleTemplateService = Depends(get_style_template_service),
) -> SuccessEnvelope[list[StyleTemplateOut]]:
    templates = service.list_templates()
    return SuccessEnvelope(
        data=[StyleTemplateOut.model_validate(t.model_dump()) for t in templates]
    )
