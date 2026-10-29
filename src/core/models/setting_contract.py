"""权威世界设定契约的共享模型、加载、渲染与哈希。

产品生成权威固定为 ``data/outline/world.json``。本模块只把
世界五个正文领域暴露给下游生成与校验，同时对完整
``WorldOutput`` 做规范化 SHA-256，确保同一份 world 语义不受 JSON 缩进影响。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.models.outline import WorldOutput
from src.core.models.narrative import StoryIntent

SETTING_CONTRACT_SOURCE_PATH = "data/outline/world.json"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SettingContractLoadError(ValueError):
    """权威 ``world.json`` 缺失、损坏或不符合 ``WorldOutput`` 契约。"""


class SettingContract(BaseModel):
    """四个下游消费者共享的权威世界契约与来源指纹。"""

    model_config = ConfigDict(frozen=True)

    setting: str = ""
    power_system: str = ""
    unique_rules: str = ""
    factions: str = ""
    culture: str = ""
    story_intent: StoryIntent | None = Field(default=None, exclude_if=lambda value: value is None)
    source_path: str = ""
    source_hash: str = ""

    @model_validator(mode="after")
    def _validate_provenance_pair(self) -> SettingContract:
        has_path = bool(self.source_path)
        has_hash = bool(self.source_hash)
        if has_path != has_hash:
            raise ValueError("source_path 与 source_hash 必须同时存在或同时为空")
        if has_hash and _SHA256_RE.fullmatch(self.source_hash) is None:
            raise ValueError("source_hash 必须是完整 64 位小写 SHA-256")
        return self

    @property
    def is_empty(self) -> bool:
        """三项生成契约是否都为空。"""

        return not (
            self.setting.strip()
            or self.power_system.strip()
            or self.unique_rules.strip()
            or self.factions.strip()
            or self.culture.strip()
        )

    @property
    def has_provenance(self) -> bool:
        """是否携带成对的权威来源路径与哈希。"""

        return bool(self.source_path and self.source_hash)

    @classmethod
    def from_world_output(
        cls,
        world: WorldOutput,
        *,
        source_path: str = SETTING_CONTRACT_SOURCE_PATH,
    ) -> SettingContract:
        """从已校验 ``WorldOutput`` 构造合同，并对完整 world 语义求哈希。"""

        canonical = json.dumps(
            world.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return cls(
            setting=world.setting,
            power_system=world.power_system,
            unique_rules=world.unique_rules,
            factions=world.factions,
            culture=world.culture,
            story_intent=world.story_intent,
            source_path=source_path,
            source_hash=hashlib.sha256(canonical).hexdigest(),
        )

    def render_for_prompt(self) -> str:
        """无截断渲染权威合同；所有生成/校验消费者复用同一文本形状。"""

        return (
            "## 权威世界契约（唯一生成权威；后续辅助记忆不得覆盖）\n\n"
            "- 级别边界:`unique_rules` 才是不可破例的 HARD；`setting` / "
            "`power_system` 是世界背景与普通机制上下文，不得仅因出现在本合同中就"
            "临时升级成 HARD。\n"
            f"- source_path:{self.source_path or '(无)'}\n"
            f"- source_hash:{self.source_hash or '(无)'}\n"
            f"- setting:{self.setting or '(无)'}\n"
            f"- power_system:{self.power_system or '(无)'}\n"
            f"- factions（组织与权限背景，不自动成为 HARD）:{self.factions or '(无)'}\n"
            f"- culture（社会与习惯背景，不自动成为 HARD）:{self.culture or '(无)'}\n"
            "- unique_rules(HARD；只应包含真正不可破例的世界规则):"
            f"{self.unique_rules or '(无)'}"
        )


def load_setting_contract(world_path: Path) -> SettingContract:
    """从权威 ``world.json`` 加载合同；失败时稳定抛 typed error。"""

    if not world_path.is_file():
        raise SettingContractLoadError(
            f"权威世界契约缺失:{SETTING_CONTRACT_SOURCE_PATH}"
        )
    try:
        payload = json.loads(world_path.read_text(encoding="utf-8"))
        world = WorldOutput.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — 统一边界错误，不泄露文件内容
        raise SettingContractLoadError(
            f"权威世界契约损坏:{SETTING_CONTRACT_SOURCE_PATH}"
        ) from exc
    return SettingContract.from_world_output(world)


__all__ = [
    "SETTING_CONTRACT_SOURCE_PATH",
    "SettingContract",
    "SettingContractLoadError",
    "load_setting_contract",
]
