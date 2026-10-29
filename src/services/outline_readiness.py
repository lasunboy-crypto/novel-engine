"""实验大纲完成凭据：只记录内容指纹，保留所有原生成物。"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SECTIONS = ("world", "characters", "plot", "volume_plans", "mini_plan")


class OutlineReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    total_volumes: int | None = Field(default=None, gt=0)
    chapters_per_volume: int | None = Field(default=None, gt=0)
    ready_hashes: dict[str, str] | None = None


def outline_hashes(material: dict) -> dict[str, str]:
    return {
        name: hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        for name, value in material.items()
        if name in SECTIONS and value is not None
    }
