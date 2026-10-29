"""Single-model activity manifest for POST-V8 S2-0R.

The manifest is deliberately separate from per-agent routing.  A maintainer
selects one frozen candidate and the runtime derives every agent profile from
that one identity.  Secrets are never part of this model; only the settings
field which is authorised to supply a credential is recorded.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator

from src.core.llm.openai_compatible_endpoint import (
    resolve_openai_compatible_endpoint,
)
from src.core.llm.strict_json_schema import (
    TOKENHUB_HY3_BASE_URL,
    TOKENHUB_HY3_HOST,
    TOKENHUB_HY3_SCHEMA_DIALECT,
)


RunModelCandidateId = Literal["hy3", "deepseek-v4-flash", "deepseek-v4-pro", "deepseek-flash", "qwen3.8-27b"]
RunModelTierName = Literal["low", "medium", "xhigh"]

_CANDIDATE_IDS = frozenset({"hy3", "deepseek-v4-flash", "qwen3.8-27b"})
QWEN_BEIJING_DASHSCOPE_HOST = "dashscope.aliyuncs.com"
QWEN_BEIJING_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_EXPECTED_AUTHORITY: dict[str, tuple[str, str, str, str]] = {
    "deepseek-flash": (
        "deepseek_official", "api.deepseek.com", "deepseek-official-api", "deepseek_api_key",
    ),
    # provider_authority, exact host, region authority, credential source id
    "hy3": (
        "tencent_tokenhub",
        TOKENHUB_HY3_HOST,
        "ap-guangzhou",
        "tokenhub_api_key",
    ),
    "deepseek-v4-pro": (
        "deepseek_official", "api.deepseek.com", "deepseek-official-api", "deepseek_api_key",
    ),
    "deepseek-v4-flash": (
        "deepseek_official",
        "api.deepseek.com",
        "deepseek-official-api",
        "deepseek_api_key",
    ),
    "qwen3.8-27b": (
        "alibaba_model_studio",
        QWEN_BEIJING_DASHSCOPE_HOST,
        "cn-beijing",
        "dashscope_api_key",
    ),
}
_EXPECTED_BASE_URL = {
    "deepseek-flash": "https://api.deepseek.com",
    "hy3": TOKENHUB_HY3_BASE_URL,
    "deepseek-v4-flash": "https://api.deepseek.com",
    "deepseek-v4-pro": "https://api.deepseek.com",
    "qwen3.8-27b": QWEN_BEIJING_DASHSCOPE_BASE_URL,
}
_EXPECTED_WORKSPACE_AUTHORITY_ID = {
    "deepseek-flash": "deepseek-official-account",
    "hy3": "tencent-tokenhub-ap-guangzhou",
    "deepseek-v4-flash": "deepseek-official-account",
    "deepseek-v4-pro": "deepseek-official-account",
    "qwen3.8-27b": "alibaba-dashscope-cn-beijing-public",
}


class RunModelTier(BaseModel):
    """Provider protocol guards for one semantic effort tier.

    ``provider_input_guard_tokens`` is the last-resort request guard passed to
    the transport.  It is deliberately *not* an assertion that every product
    prompt can render that many tokens and therefore cannot be used as an
    executable activity cost bound.
    """

    provider_input_guard_tokens: int = Field(ge=1)
    max_completion_tokens: int = Field(ge=1)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    thinking_request: dict[str, Any] = Field(default_factory=dict)


class SelectionTestLossBreakers(BaseModel):
    """Loss stops owned only by a dedicated model-selection experiment.

    These values are not part of the model/provider identity and must never be
    inherited by an ordinary UI or production Run.  Zero/zero is the
    Provider-0 disabled state.
    """

    activity_scope: Literal["selection_test"] = "selection_test"
    max_physical_requests: int = Field(ge=0)
    max_configured_rmb: Decimal = Field(ge=Decimal("0"))

    @model_validator(mode="after")
    def _validate_pair(self) -> "SelectionTestLossBreakers":
        if (self.max_physical_requests == 0) != (self.max_configured_rmb == 0):
            raise ValueError(
                "selection_test max_physical_requests/max_configured_rmb "
                "必须同时为零或同时为正"
            )
        return self

    def identity_hash(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class RunModelManifest(BaseModel):
    """Frozen provider/model/authority/cost identity for one activity."""

    manifest_id: RunModelCandidateId
    # 缺省保留历史配置字节/行为；新实验显式选择管线，不借模型名推断职责。
    pipeline_contract: Literal["creative_delta_v6"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    handoff_format: Literal["text", "json"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    narrative_handoff_version: Literal[0, 1] = Field(
        default=0, exclude_if=lambda value: value == 0,
        description="新书叙事交接显式版本；旧活动缺省为 0，不增加故事定位调用。",
    )
    opening_presentation_version: Literal[0, 1] = Field(
        0, exclude_if=lambda value: value == 0,
        description="仅改变既有开篇方向的呈现与去重，不增加验收 HARD 或模型调用",
    )
    model_revision: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
    provider_authority: Literal[
        "tencent_tokenhub", "deepseek_official", "alibaba_model_studio"
    ]
    base_url: str
    host: str
    region: str
    workspace_authority_id: str = Field(min_length=1)
    exact_model_id: RunModelCandidateId
    credential_source_id: Literal[
        "tokenhub_api_key", "deepseek_api_key", "dashscope_api_key"
    ]
    transport: Literal["openai_chat_completions"] = "openai_chat_completions"
    completion_limit_wire_field: Literal["max_tokens", "max_completion_tokens"]
    structured_mode: Literal[
        "json_object_local_schema", "native_json_schema_strict", "prompt_json_local_schema"
    ] = "json_object_local_schema"
    structured_schema_dialect: Literal["tokenhub_hy3_chat_json_schema_v1"] | None = None
    context_window: int = Field(ge=1)
    tiers: dict[RunModelTierName, RunModelTier]
    upstream_generation: RunModelTier | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    prose_generation: RunModelTier | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    input_price_rmb_per_million: Decimal = Field(ge=Decimal("0"))
    output_price_rmb_per_million: Decimal = Field(ge=Decimal("0"))
    cache_billing_policy: Literal["no_discount"] = "no_discount"
    selection_test: SelectionTestLossBreakers
    fallback: Literal[False] = False

    @model_validator(mode="after")
    def _validate_frozen_identity(self) -> "RunModelManifest":
        if self.handoff_format is not None and self.pipeline_contract != "creative_delta_v6":
            raise ValueError("handoff_format 只适用于显式 creative_delta_v6 实验")
        if self.narrative_handoff_version and self.pipeline_contract != "creative_delta_v6":
            raise ValueError("叙事交接 v1 只适用于显式 creative_delta_v6 实验")
        if self.manifest_id == "deepseek-flash" and (
            self.pipeline_contract != "creative_delta_v6"
            or self.model_revision != "DeepSeek-V4.1-Flash"
        ):
            raise ValueError("deepseek-flash 必须显式绑定 creative_delta_v6 / DeepSeek-V4.1-Flash")
        if self.exact_model_id != self.manifest_id:
            raise ValueError("manifest_id 与 exact_model_id 必须逐字相同")
        if set(self.tiers) != {"low", "medium", "xhigh"}:
            raise ValueError("tiers 必须且只能包含 low/medium/xhigh")
        expected_authority, expected_host, expected_region, expected_credential = (
            _EXPECTED_AUTHORITY[self.manifest_id]
        )
        if self.provider_authority != expected_authority:
            raise ValueError(
                f"{self.manifest_id} provider authority 必须为 {expected_authority}"
            )
        if self.region != expected_region:
            raise ValueError(
                f"{self.manifest_id} region authority 必须为 {expected_region}"
            )
        if self.credential_source_id != expected_credential:
            raise ValueError(
                f"{self.manifest_id} credential source 必须为 {expected_credential}"
            )
        expected_workspace_authority = _EXPECTED_WORKSPACE_AUTHORITY_ID[
            self.manifest_id
        ]
        if self.workspace_authority_id != expected_workspace_authority:
            raise ValueError(
                f"{self.manifest_id} workspace authority ID 必须为 "
                f"{expected_workspace_authority}"
            )
        expected_completion_field = (
            "max_tokens"
            if self.provider_authority == "deepseek_official"
            else "max_completion_tokens"
        )
        if self.completion_limit_wire_field != expected_completion_field:
            raise ValueError(
                f"{self.manifest_id} completion limit wire field 必须为 "
                f"{expected_completion_field}"
            )

        if self.manifest_id == "hy3":
            if self.structured_mode != "native_json_schema_strict":
                raise ValueError("hy3 structured_mode 必须为 native_json_schema_strict")
            if self.structured_schema_dialect != TOKENHUB_HY3_SCHEMA_DIALECT:
                raise ValueError(
                    "hy3 structured_schema_dialect 必须精确绑定 TokenHub Chat dialect"
                )
        elif (
            (self.structured_mode != "json_object_local_schema" and not (
                self.structured_mode == "prompt_json_local_schema"
                and self.provider_authority == "deepseek_official"
                and self.pipeline_contract == "creative_delta_v6"
            ))
            or self.structured_schema_dialect is not None
        ):
            raise ValueError(
                f"{self.manifest_id} 不得启用 Hy3 native json_schema capability"
            )

        parsed_host = (urlsplit(self.base_url).hostname or "").lower().rstrip(".")
        if self.host != self.host.lower().rstrip(".") or self.host != parsed_host:
            raise ValueError("manifest host 必须是 base_url 的规范小写 hostname")
        if self.host != expected_host:
            raise ValueError(f"{self.manifest_id} host 必须精确为 {expected_host}")
        endpoint = resolve_openai_compatible_endpoint(self.base_url)
        if endpoint.settings_field != self.credential_source_id:
            raise ValueError(
                "endpoint credential authority 与 manifest credential_source_id 不一致"
            )
        expected_base_url = _EXPECTED_BASE_URL[self.manifest_id]
        if self.base_url != expected_base_url:
            raise ValueError(
                f"{self.manifest_id} base_url 必须精确为 {expected_base_url}"
            )

        for tier_name, tier in self.tiers.items():
            if (
                tier.provider_input_guard_tokens + tier.max_completion_tokens
                > self.context_window
            ):
                raise ValueError(
                    f"{self.manifest_id}/{tier_name} 输入与总输出预算超过上下文"
                )
            if self.provider_authority == "deepseek_official":
                non_thinking = tier.thinking_request == {"thinking": {"type": "disabled"}}
                if non_thinking and tier.temperature is None:
                    raise ValueError(f"DeepSeek/{tier_name} 非思考模式必须冻结 temperature")
                if not non_thinking and tier.temperature is not None:
                    raise ValueError(
                        f"DeepSeek/{tier_name} thinking 模式不得伪配不生效的 temperature"
                    )
            elif tier.temperature is None:
                raise ValueError(f"{self.manifest_id}/{tier_name} 必须冻结 temperature")
            self._validate_thinking_request(tier_name, tier.thinking_request)
        if self.upstream_generation is not None:
            generation = self.upstream_generation
            if self.manifest_id != "deepseek-flash" or self.pipeline_contract != "creative_delta_v6":
                raise ValueError("独立上游生成参数只用于 DeepSeek V4.1 实验管线")
            if generation.provider_input_guard_tokens + generation.max_completion_tokens > self.context_window:
                raise ValueError("上游生成输入与输出预算超过上下文")
            if generation.temperature is not None:
                raise ValueError("思考模式上游生成不得配置无效 temperature")
            self._validate_thinking_request("upstream_generation", generation.thinking_request)
        if self.prose_generation is not None:
            prose = self.prose_generation
            if self.provider_authority != "deepseek_official" or self.pipeline_contract is None:
                raise ValueError("独立正文参数只用于显式选择的 DeepSeek 实验管线")
            self._validate_thinking_request("prose", prose.thinking_request)
            non_thinking = prose.thinking_request == {"thinking": {"type": "disabled"}}
            if non_thinking and prose.temperature is None:
                raise ValueError("非思考正文必须冻结有效 temperature")
            if not non_thinking and prose.temperature is not None:
                raise ValueError("思考正文不得伪配不生效的 temperature")
            if prose.provider_input_guard_tokens + prose.max_completion_tokens > self.context_window:
                raise ValueError("正文输入与输出预算超过上下文")
        return self

    def _validate_thinking_request(
        self, tier_name: str, request: dict[str, Any]
    ) -> None:
        if self.manifest_id == "hy3":
            thinking = request.get("thinking")
            if set(request) != {"thinking", "reasoning_effort"}:
                raise ValueError(
                    f"hy3/{tier_name} thinking 映射必须只有 thinking/reasoning_effort"
                )
            if not isinstance(thinking, dict) or thinking.get("type") != "enabled":
                raise ValueError(f"hy3/{tier_name} 必须显式 thinking.type=enabled")
            budget = thinking.get("budget_tokens")
            if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
                raise ValueError(f"hy3/{tier_name} budget_tokens 必须为正整数")
            expected_effort = {
                "low": "low",
                "medium": "medium",
                "xhigh": "high",
            }[tier_name]
            if request.get("reasoning_effort") != expected_effort:
                raise ValueError(
                    f"hy3/{tier_name} reasoning_effort 必须为 {expected_effort}"
                )
        elif self.provider_authority == "deepseek_official":
            if self.pipeline_contract == "creative_delta_v6" and request == {"thinking": {"type": "disabled"}}:
                return
            if set(request) != {"thinking", "reasoning_effort"}:
                raise ValueError(
                    f"DeepSeek/{tier_name} thinking 映射必须只有 thinking/reasoning_effort"
                )
            if request.get("thinking") != {"type": "enabled"}:
                raise ValueError("DeepSeek thinking 必须显式 enabled")
            if request.get("reasoning_effort") not in {"low", "high", "max"}:
                raise ValueError("DeepSeek reasoning_effort 只允许 low/high/max")
        else:
            if set(request) != {
                "enable_thinking",
                "thinking_budget",
                "preserve_thinking",
            }:
                raise ValueError("Qwen thinking 映射字段不完整或含额外字段")
            if request.get("enable_thinking") is not True:
                raise ValueError("Qwen thinking 必须显式开启")
            if request.get("preserve_thinking") is not False:
                raise ValueError("Qwen preserve_thinking 必须显式关闭")
            budget = request.get("thinking_budget")
            if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
                raise ValueError("Qwen thinking_budget 必须为正整数")

    def identity_hash(self) -> str:
        """Hash model/host/protocol identity, excluding experiment breakers."""

        payload = json.dumps(
            self.model_dump(mode="json", exclude={"selection_test"}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class RunModelManifestCatalog(BaseModel):
    """All three frozen profiles plus the single active selection."""

    active: RunModelCandidateId | None = None
    manifests: dict[str, RunModelManifest] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_catalog(self) -> "RunModelManifestCatalog":
        if not self.manifests:
            if self.active is not None:
                raise ValueError("manifest catalog 为空时 active 必须为 null")
            return self
        if not (_CANDIDATE_IDS <= set(self.manifests) <= (
            _CANDIDATE_IDS | {"deepseek-v4-pro", "deepseek-flash"}
        )):
            raise ValueError(
                "run model manifests 必须包含原三个候选；只允许额外声明 deepseek-v4-pro/deepseek-flash"
            )
        for manifest_id, manifest in self.manifests.items():
            if manifest.manifest_id != manifest_id:
                raise ValueError("manifest 字典 key 与 manifest_id 不一致")
        if self.active is not None and self.active not in self.manifests:
            raise ValueError("active run model manifest 未在 manifests 声明")
        return self

    def catalog_hash(self) -> str:
        """Hash the complete frozen catalog, including disabled test breakers."""

        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def model_identity_hash(self) -> str:
        """Hash only the active choice and model/provider identities."""

        payload = json.dumps(
            {
                "active": self.active,
                "manifests": {
                    manifest_id: manifest.identity_hash()
                    for manifest_id, manifest in sorted(self.manifests.items())
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @property
    def active_manifest(self) -> RunModelManifest | None:
        return self.manifests.get(self.active) if self.active is not None else None

    @property
    def lock_token(self) -> str | None:
        manifest = self.active_manifest
        if manifest is None:
            return None
        return (
            f"run-model-manifest:{manifest.manifest_id}:"
            f"{self.model_identity_hash()}:{manifest.identity_hash()}"
        )

    @property
    def selection_test_lock_token(self) -> str | None:
        """Separate token for a future explicitly scoped selection runner."""

        manifest = self.active_manifest
        model_token = self.lock_token
        if manifest is None or model_token is None:
            return None
        return f"selection-test:{model_token}:{manifest.selection_test.identity_hash()}"


__all__ = [
    "RunModelCandidateId",
    "RunModelManifest",
    "RunModelManifestCatalog",
    "RunModelTier",
    "RunModelTierName",
    "SelectionTestLossBreakers",
    "QWEN_BEIJING_DASHSCOPE_BASE_URL",
    "QWEN_BEIJING_DASHSCOPE_HOST",
]
