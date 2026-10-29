"""章节卡生成器辅助配置加载(P5-B-06 引入)。

outline commit 固定只生成首卡；后续卡由正式 Run 在上一章富记忆成功后逐章生成。
本配置只保留失效卡级联恢复的相似度阈值和样式控制器开关，不再提供正式
生成的批量大小或 beat 粒度。

模式沿用 ``cost_guard.py`` ``load_cost_guard_config`` 同源:从仓库根
``config.yaml`` ``card_generator:`` 段读出 ``CardGeneratorConfig``;段不存在
或文件不存在时返回字段默认值。

为何独立文件
============

``config_service.py`` 只处理 ``writer_profiles`` / ``writer_routing``(P3-B-02
决策),职责是"工作区覆盖叠加引擎默认";本模块属"引擎单段配置加载"(单端
+ 单 Pydantic + Pydantic 默认值兜底)与 ``cost_guard.py`` 同源,职责清晰。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class CardGeneratorConfig(BaseModel):
    """``card_generator:`` 配置段。

    Pydantic v2 model；批量大小和粒度已下线。
    """

    model_config = {"extra": "forbid"}

    diversification_threshold: float = Field(
        default=0.5,
        gt=0.0,
        le=1.0,
        description=(
            "v1.0.x wave 4 session 4 引入。失效卡级联恢复完毕后,同轮相邻章卡 "
            "``must_achieve`` 字符集重合率上限。配对重合率 ≥ 本阈值视为同质化,"
            "service 自动 retry 一次(注入 ``forbidden_must_achieve_phrases`` "
            "banner);retry 仍超阈值降级 + structlog warn(对齐 P6-B-10 "
            "``reference_validator`` 单 retry 同型决策)。默认 0.5,session 3b "
            "真 LLM 实证实际重合率 5-22% 留 28% 安全余量;v1.1 真实战可校准。"
        ),
    )
    style_controller_enabled: bool = Field(
        default=True,
        description=(
            "v1.1 wave 5 session 1(M1 动态卡控制器)引入。是否启用套话 avoid-list "
            "动态注入:``True``(默认)时 ``ChapterCardService.derive_context`` 调 "
            "``style_controller.active_avoid_list`` 读工作区套话台账,把近章复发达阈值"
            "的套话作 ``forbidden_style_phrases`` 注入本章卡 prompt;``False`` = "
            "**engine 级 kill-switch**,完全不注入(台账仍由 orchestrator 写,但不影响"
            "生成 → 行为与 M1 前等价,可作原型 A/B 的 OFF 臂 / 生产回退阀)。"
        ),
    )


def load_card_generator_config(
    path: Path | None = None,
) -> CardGeneratorConfig:
    """从 yaml 文件加载 ``card_generator:`` 段。

    :param path: yaml 路径;``None`` → 走 ``settings.config_yaml_path``
        默认 ``config.yaml``(仓库根)。
    :return: 已校验的 ``CardGeneratorConfig``;yaml 无 ``card_generator:`` 段
        或文件不存在时返回字段默认值。

    yaml 期望结构(顶层 ``card_generator:`` 段)::

        card_generator:
          diversification_threshold: 0.5
          style_controller_enabled: true

    与 ``config_loader.load_writer_profiles_config`` 区别:本函数对**文件不存在
    / 段不存在**走默认值,而非 fail-fast,理由与 ``cost_guard`` 同源(辅助
    配置,默认值工程合理,缺配置不应 fail-fast)。
    """
    if path is None:
        # 延迟 import 避开循环依赖
        from src.config import settings

        path = settings.config_yaml_path
    if not path.exists():
        return CardGeneratorConfig()
    with path.open("r", encoding="utf-8") as fp:
        raw: dict[str, Any] = yaml.safe_load(fp) or {}
    if not isinstance(raw, dict):
        return CardGeneratorConfig()
    section = raw.get("card_generator")
    if not isinstance(section, dict):
        return CardGeneratorConfig()
    return CardGeneratorConfig.model_validate(section)


__all__ = [
    "CardGeneratorConfig",
    "load_card_generator_config",
]
