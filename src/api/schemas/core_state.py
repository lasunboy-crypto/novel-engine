"""核心状态/书内时钟审计 API schema。"""

from pydantic import BaseModel, ConfigDict, Field

from src.core.models.core_state import CoreStateProjection
from src.services.core_state_service import CoreStateAuditItem


class CoreStateAuditItemOut(CoreStateAuditItem):
    """单条 revision、角色核心事实或未采纳候选。"""


class CoreStateAuditOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projection: CoreStateProjection
    items: list[CoreStateAuditItemOut] = Field(default_factory=list)


__all__ = [
    "CoreStateAuditItemOut",
    "CoreStateAuditOut",
]
