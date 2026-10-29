"""静态资产 import API 请求 / 响应 schema(P5-B-08)。

对齐 ``POST /api/v1/workspaces/{id}/assets:import`` 端点(决策 7=C 摊到
NewBookPage UI 流程,补齐 ROADMAP v1.1 §P1 验收清单 1)。

设计要点
========

- ``AssetsImportIn`` 接 markdown **文本内容**(对应前端 NewBookPage 左侧
  表单的 ``world.md`` / ``characters.md`` 上传槽),两字段都可选(``None``
  / 空字符串视为未上传)。
- ``AssetsImportOut`` 字段与既有 ``services.asset_import.ImportAssetsResult``
  对齐(P5-B-08 决策 ④=A:按规格源走,不裁字段;前端通过 ``counts``
  字典取 ``character`` / ``fact`` 等条目数,无需后端裁标量字段)。
- ``plot_threads.md`` / ``timeline.md`` 这两类资产**本任务范围外**(NewBookPage
  UI 槽只 2 个,对齐启动指令字面);若后续要在 API 上传,扩 ``AssetsImportIn``
  加字段即可,响应 schema 已能容纳(``counts`` 是字典)。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AssetsImportIn(BaseModel):
    """``POST /api/v1/workspaces/{id}/assets:import`` 请求体(P5-B-08)。

    两字段都可选:None / 空字符串视为本次不上传该文件(既有 ``data/assets/``
    下同名文件不动 — 复用历史上传)。两字段都为空时,API 仍会调 service 解析
    现有目录,返 counts 全 0(等同"无新增资产")。
    """

    model_config = ConfigDict(extra="forbid")

    world_md_content: str | None = Field(
        default=None,
        description=(
            "``world.md`` 文件内容(markdown 文本)。None / 空字符串视为本次"
            "不上传 world 资产;既有 ``data/assets/world.md`` 不动。"
        ),
    )
    characters_md_content: str | None = Field(
        default=None,
        description=(
            "``characters.md`` 文件内容(markdown 文本)。None / 空字符串"
            "视为本次不上传 characters 资产;既有 ``data/assets/characters.md`` 不动。"
        ),
    )


class AssetsImportOut(BaseModel):
    """``POST /api/v1/workspaces/{id}/assets:import`` 响应 data 段(P5-B-08)。

    字段与既有 ``services.asset_import.ImportAssetsResult`` 对齐(决策 ④=A
    按规格源走):

    - ``workspace_id``:回显工作区 ID。
    - ``files_processed``:本次实际解析过的文件名清单(子集:``world.md`` /
      ``characters.md`` / ``plot_threads.md`` / ``timeline.md``)。
    - ``counts``:四类导入条目数,key 为 ``character`` / ``plot_thread`` /
      ``timeline`` / ``fact``,value 为本次写入的条目数(同 ID 覆盖也计入)。

    前端按需取:``counts["character"]`` 显示"角色 N 个",``counts["fact"]``
    显示"世界设定 N 条"。本任务范围内主要用 ``character`` 与 ``fact`` 两 key。
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(..., description="工作区 ID(回显)")
    files_processed: list[str] = Field(
        default_factory=list,
        description="本次解析过的文件名清单",
    )
    counts: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "四类导入条目数:``character`` / ``plot_thread`` / ``timeline`` / "
            "``fact``。本任务范围内主要用 ``character`` + ``fact`` 两 key。"
        ),
    )
