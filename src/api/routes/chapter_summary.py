"""ChapterSummary 路由(v1.0.x wave 4 session 5)。

对齐前端 OutlinePage 第 6 tab(``ChapterSummaryTabContent``)— 用户事后查看 +
手动编辑 LLM 抽取的章节摘要。

2 个端点(workspace_id 走 URL path 对齐 outline.py 模式):

| 方法 | 路径 | 作用 |
|------|------|------|
| GET  | ``/workspaces/{id}/chapter-summaries`` | 列出所有摘要(chapter_id ASC) |
| PUT  | ``/workspaces/{id}/chapter-summaries/{chapter_id}`` | PATCH 6 字段 |

错误码:
- ``CHAPTER_SUMMARY_NOT_FOUND`` (404) — 章节摘要行未抽取 / chapter_id 错
- ``WORKSPACE_NOT_FOUND`` (404) — 工作区不存在
- ``VALIDATION_FAILED`` (422) — 请求体不合 schema
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends

from src.api.deps import get_chapter_summary_service
from src.api.schemas.chapter_summary import (
    ChapterSummaryOut,
    ChapterSummaryUpdateIn,
)
from src.api.schemas.common import SuccessEnvelope
from src.services.chapter_summary_service import ChapterSummaryService

router = APIRouter(prefix="/workspaces", tags=["chapter-summary"])


@router.get(
    "/{workspace_id}/chapter-summaries",
    response_model=SuccessEnvelope[list[ChapterSummaryOut]],
    summary="列出工作区下所有章节摘要(chapter_id ASC)",
    description=(
        "v1.0.x wave 4 session 5。LLM 在每章 approved 后自动抽取摘要落 "
        "``chapter_summaries`` 表(由 ChapterService reviewer_pass / "
        "ChapterRewindService force_pass 的 hook 触发);本端点是用户事后查看 "
        "入口。未 approved 章节无对应行,列表只含已抽取的章节。"
    ),
)
async def list_chapter_summaries(
    workspace_id: str,
    service: ChapterSummaryService = Depends(get_chapter_summary_service),
) -> SuccessEnvelope[list[ChapterSummaryOut]]:
    summaries = service.list_summaries(workspace_id)
    return SuccessEnvelope(
        data=[
            ChapterSummaryOut.model_validate(s.model_dump()) for s in summaries
        ],
    )


@router.put(
    "/{workspace_id}/chapter-summaries/{chapter_id}",
    response_model=SuccessEnvelope[ChapterSummaryOut],
    summary="编辑章节摘要(PATCH 语义,只更新非 null 字段)",
    description=(
        "v1.0.x wave 4 session 5。所有字段全 optional,只更新 body 内非 null "
        "字段(对齐 outline PUT 模式)。``extraction_trigger`` / ``model_used`` / "
        "``created_at`` 是 LLM 抽取审计字段,不允许用户改。"
    ),
)
async def update_chapter_summary(
    workspace_id: str,
    chapter_id: str,
    body: ChapterSummaryUpdateIn = Body(...),
    service: ChapterSummaryService = Depends(get_chapter_summary_service),
) -> SuccessEnvelope[ChapterSummaryOut]:
    updated = service.update_summary(
        workspace_id,
        chapter_id,
        summary_text=body.summary_text,
        pov_character=body.pov_character,
        scene_setting=body.scene_setting,
        key_events=body.key_events,
        character_interactions=body.character_interactions,
        unfinished_actions=body.unfinished_actions,
    )
    return SuccessEnvelope(data=ChapterSummaryOut.model_validate(updated.model_dump()))
