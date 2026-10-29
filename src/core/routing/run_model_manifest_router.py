"""Central single-model router and pre-request identity lock."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Literal

import yaml  # type: ignore[import-untyped]
import structlog

from src.core.config_loader import load_run_model_manifest_catalog
from src.core.llm.base import LLMClient, LLMResponse, Message
from src.core.llm.local_json_contract import (
    LOCAL_JSON_CONTRACT_PARAM,
    LocalJsonContract,
    LocalJsonResponseViolation,
    archive_manifest_response,
    local_contract_from_params,
    unwrap_json_code_fence,
)
from src.core.agents.creative_delta import (
    CREATIVE_DELTA_CAPABILITY,
    EXPERIMENTAL_PIPELINE_CONTRACT,
    HY3_CREATIVE_DELTA_CAPABILITY,
    HY3_DRAFT_V5_CAPABILITY,
    WRITER_DRAFT_CAPABILITY,
)
from src.core.llm.strict_json_schema import (
    STRICT_JSON_RESPONSE_CONTRACT_PARAM,
    append_strict_json_instruction,
    assert_hy3_strict_request,
    build_hy3_strict_contract,
    strict_contract_from_params,
    validate_strict_json_response,
)
from src.core.models.routing import RoutingConfig, WriterProfilesConfig
from src.core.models.run_model_manifest import (
    RunModelManifest,
    RunModelTierName,
)
from src.core.models.writer_profile import WriterProfile
from src.core.routing.router import ModelRouter, RoutedTarget
from src.services.run_model_stage_registry import (
    PROMPT_RENDER_BOUNDS,
    conservative_rendered_input_tokens,
    prompt_render_bound_for_agent,
    run_model_tier_for_agent,
)

_log = structlog.get_logger(__name__)
_HY3_INPUT_MATERIAL_REVISION = "source-material-and-narrative-handoff-v3"
_UPSTREAM_GENERATION_AGENTS = frozenset({
    "outline_builder",
    "mini_plan_generator",
    "chapter_card_generator",
})


class RunModelManifestLockError(RuntimeError):
    """Manifest/config/route/response identity changed before a request."""


@dataclass(frozen=True, slots=True)
class SelectionTestLossBreakerBinding:
    """Experiment-only stop values; never inherited by production routing."""

    activity_scope: Literal["selection_test"]
    token: str
    max_physical_requests: int
    max_configured_rmb: Decimal
    cost_currency: str = "CNY"


@dataclass(frozen=True, slots=True)
class RunModelManifestBinding:
    manifest_id: str
    token: str
    selection_test: SelectionTestLossBreakerBinding


class ManifestFallbackBlockedClient(LLMClient):
    """Credential-free sentinel for legacy service constructor fallbacks."""

    def __init__(self, manifest_id: str) -> None:
        self._manifest_id = manifest_id

    def prepare_strict_json_schema(
        self,
        *,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
        gen_params: dict[str, Any],
    ) -> dict[str, Any]:
        del model, schema_name, schema, gen_params
        raise RunModelManifestLockError(
            "单模型活动禁止使用 legacy bootstrap/default client"
        )

    def complete(self, messages: list[Message], **gen_params: Any) -> LLMResponse:
        del messages, gen_params
        raise RunModelManifestLockError(
            f"manifest={self._manifest_id} 禁止使用 legacy bootstrap/default client"
        )


_JSON_OBJECT_SYSTEM_INSTRUCTION = (
    "本次响应必须只输出一个合法 JSON object，不要 Markdown 代码块、解释、"
    "前后缀或多个顶层值。字段、类型和业务约束仍以用户消息中的 schema 及本地"
    "Pydantic/business validator 为权威。"
)


def _executable_config_hash(
    path: Path,
    *,
    include_selection_test: bool,
) -> str:
    """Hash executable YAML while optionally excluding experiment breakers."""

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("config.yaml 顶层必须为 mapping")
    # 只扩既有运行 token 的本地哈希输入，不改 config/schema。旧 checkpoint
    # 不能把同一模型身份误当成相同首稿合同，静默续用当前 v5 渲染器。
    manifest_config = raw.get("run_model_manifest")
    active_config = (
        manifest_config.get("manifests", {}).get(manifest_config.get("active"), {})
        if isinstance(manifest_config, dict) else {}
    )
    if active_config.get("pipeline_contract") == EXPERIMENTAL_PIPELINE_CONTRACT:
        raw = {**raw, "_experimental_pipeline_revision": EXPERIMENTAL_PIPELINE_CONTRACT}
    elif isinstance(manifest_config, dict) and manifest_config.get("active") == "hy3":
        raw = {
            **raw,
            "_hy3_writer_contract_revision": HY3_DRAFT_V5_CAPABILITY,
            "_hy3_card_state_pairing_revision": "entity-attribute-pairs-v1",
            "_hy3_input_material_revision": _HY3_INPUT_MATERIAL_REVISION,
        }
    if not include_selection_test:
        manifest_section = raw.get("run_model_manifest")
        manifests = (
            manifest_section.get("manifests")
            if isinstance(manifest_section, dict)
            else None
        )
        if isinstance(manifests, dict):
            for manifest in manifests.values():
                if isinstance(manifest, dict):
                    manifest.pop("selection_test", None)
    payload = json.dumps(
        raw,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _profile_id(manifest: RunModelManifest, tier: RunModelTierName) -> str:
    return f"run_manifest__{manifest.manifest_id}__{tier}"


def _profile_for_tier(
    manifest: RunModelManifest, tier_name: RunModelTierName, *,
    prose: bool = False, upstream_generation: bool = False,
) -> WriterProfile:
    if prose and upstream_generation:
        raise ValueError("正文与上游生成配置不可共用")
    if prose:
        tier = manifest.prose_generation
        profile_suffix = "__prose"
    elif upstream_generation:
        tier = manifest.upstream_generation
        profile_suffix = "__upstream_generation"
    else:
        tier = manifest.tiers[tier_name]
        profile_suffix = ""
    assert tier is not None
    generation_params: dict[str, Any] = {
        manifest.completion_limit_wire_field: tier.max_completion_tokens,
    }
    if tier.temperature is not None:
        generation_params["temperature"] = tier.temperature
    extra_request_kwargs: dict[str, Any] = {
        "input_token_budget": tier.provider_input_guard_tokens,
        **tier.thinking_request,
    }
    if upstream_generation or (
        manifest.manifest_id == "deepseek-flash"
        and manifest.pipeline_contract == EXPERIMENTAL_PIPELINE_CONTRACT
        and tier.max_completion_tokens == 393216
    ):
        # Full-capacity generation, review and memory may outlast the historical
        # 20-minute wall clock. Keep idle/read protection without cutting off a
        # healthy stream before it can use its approved completion allowance.
        extra_request_kwargs["disable_total_timeout"] = True
    return WriterProfile(
        profile_id=_profile_id(manifest, tier_name) + profile_suffix,
        provider="openai_compatible",
        model=manifest.exact_model_id,
        base_url=manifest.base_url,
        context_window=manifest.context_window,
        generation_params=generation_params,
        extra_request_kwargs=extra_request_kwargs,
        cost_per_1k_input=float(manifest.input_price_rmb_per_million / 1000),
        cost_per_1k_output=float(manifest.output_price_rmb_per_million / 1000),
        cost_currency="CNY",
        notes=(
            f"S2 single-model manifest={manifest.manifest_id}; tier={tier_name}; "
            "fallback=false; "
            + (
                "TokenHub Hy3 Chat native strict JSON Schema + local authority"
                if manifest.structured_mode == "native_json_schema_strict"
                else "JSON Object + local schema authority"
            )
        ),
    )


def manifest_creative_capability(manifest: RunModelManifest) -> str | None:
    """Resolve a pure pipeline capability without creating a transport."""
    if manifest.pipeline_contract == EXPERIMENTAL_PIPELINE_CONTRACT:
        return CREATIVE_DELTA_CAPABILITY
    if (manifest.manifest_id == "hy3"
            and manifest.provider_authority == "tencent_tokenhub"
            and manifest.structured_mode == "native_json_schema_strict"):
        return HY3_CREATIVE_DELTA_CAPABILITY
    return None


def manifest_authority_capability(manifest: RunModelManifest) -> str | None:
    if manifest.pipeline_contract == EXPERIMENTAL_PIPELINE_CONTRACT:
        return WRITER_DRAFT_CAPABILITY
    if manifest_creative_capability(manifest) == HY3_CREATIVE_DELTA_CAPABILITY:
        return HY3_DRAFT_V5_CAPABILITY
    return None


class ManifestLockedClient(LLMClient):
    """Guard an existing Chat client immediately before and after every call."""

    def __init__(
        self,
        *,
        inner: LLMClient,
        agent_name: str,
        profile: WriterProfile,
        manifest: RunModelManifest,
        config_path: Path,
        binding: RunModelManifestBinding,
    ) -> None:
        self._inner = inner
        self._agent_name = agent_name
        self._profile = profile
        self._manifest = manifest
        self._config_path = config_path
        self._binding = binding

    @property
    def creative_delta_capability(self) -> str | None:
        """Expose T2 only for the exact T1 TokenHub Hy3 identity.

        Tracking/cost wrappers delegate unknown attributes to their inner
        client, so this read-only capability survives the existing wrapper
        stack without changing ``LLMClient`` or any non-Hy3 route.
        """

        return manifest_creative_capability(self._manifest)

    @property
    def authority_assembly_capability(self) -> str | None:
        """Resolve the frozen pipeline separately from the real provider identity."""
        return manifest_authority_capability(self._manifest)

    @property
    def prose_generation_is_locked(self) -> bool:
        return self._manifest.prose_generation is not None and self._agent_name in {"writer", "reviser"}

    @property
    def upstream_generation_is_locked(self) -> bool:
        return (self._manifest.upstream_generation is not None
                and self._agent_name in _UPSTREAM_GENERATION_AGENTS)

    @property
    def handoff_format(self) -> str | None:
        """格式随活动身份冻结，旧 manifest 不启用。"""
        return self._manifest.handoff_format

    @property
    def opening_presentation_version(self) -> int:
        return self._manifest.opening_presentation_version

    def _assert_frozen(self) -> None:
        try:
            catalog = load_run_model_manifest_catalog(self._config_path)
            model_config_hash = _executable_config_hash(
                self._config_path,
                include_selection_test=False,
            )
        except Exception as exc:
            raise RunModelManifestLockError(
                "活动 manifest 配置无法重新验证，已在 Provider 前停止"
            ) from exc
        current_model_token = (
            f"{catalog.lock_token}:{model_config_hash}"
            if catalog.lock_token is not None
            else None
        )
        if current_model_token != self._binding.token:
            raise RunModelManifestLockError(
                "活动启动后的模型可执行 config.yaml 已改变，禁止继续使用旧 checkpoint"
            )
        current = catalog.active_manifest
        if current is None or current.manifest_id != self._manifest.manifest_id:
            raise RunModelManifestLockError("活动 manifest selection 已漂移")
        if current.identity_hash() != self._manifest.identity_hash():
            raise RunModelManifestLockError("活动 manifest identity/hash 已漂移")

        # Pipeline resume binds the persisted token.  Standalone Provider-0
        # tests/tasks may have no attempt scope; their router/file hashes still
        # receive the same pre-request guard.
        from src.services.attempt_ledger import current_attempt_scope

        scope = current_attempt_scope()
        if (
            scope is not None
            and scope.run_model_manifest_token is not None
            and scope.run_model_manifest_token != self._binding.token
        ):
            raise RunModelManifestLockError(
                "attempt scope 的 manifest token 与 checkpoint 不一致"
            )
        if scope is not None and scope.activity_scope == "selection_test":
            try:
                selection_config_hash = _executable_config_hash(
                    self._config_path,
                    include_selection_test=True,
                )
            except Exception as exc:
                raise RunModelManifestLockError(
                    "selection-test 配置无法重新验证"
                ) from exc
            catalog_selection_token = catalog.selection_test_lock_token
            current_selection_token = (
                f"{catalog_selection_token}:{selection_config_hash}"
                if catalog_selection_token is not None
                else None
            )
            if (
                scope.selection_test_manifest_token
                != self._binding.selection_test.token
                or current_selection_token != self._binding.selection_test.token
            ):
                raise RunModelManifestLockError("selection-test breaker/token 已漂移")
        self._assert_profile_identity()

    def _assert_profile_identity(self) -> None:
        expected = (
            "openai_compatible",
            self._manifest.base_url,
            self._manifest.exact_model_id,
        )
        actual = (
            self._profile.provider,
            self._profile.base_url,
            self._profile.model,
        )
        if actual != expected:
            raise RunModelManifestLockError(
                f"Router target identity 漂移: expected={expected}, actual={actual}"
            )
        inner_model = getattr(self._inner, "_model", self._manifest.exact_model_id)
        inner_base_url = getattr(self._inner, "_base_url", self._manifest.base_url)
        if inner_model != self._manifest.exact_model_id:
            raise RunModelManifestLockError("client requested model 已漂移")
        if str(inner_base_url).rstrip("/") != self._manifest.base_url.rstrip("/"):
            raise RunModelManifestLockError("client endpoint host/path 已漂移")

    def _assert_selection_smoke_scope(self) -> None:
        """Allow the non-production ``smoke`` role only under its exact token."""

        if self._agent_name != "smoke":
            return
        from src.services.attempt_ledger import current_attempt_scope

        scope = current_attempt_scope()
        selection = self._binding.selection_test
        if (
            scope is None
            or scope.activity_scope != "selection_test"
            or scope.run_model_manifest_token != self._binding.token
            or scope.selection_test_manifest_token != selection.token
            or selection.max_physical_requests <= 0
            or selection.max_configured_rmb <= 0
        ):
            raise RunModelManifestLockError(
                "smoke 只允许专用 selection-test scope/token 使用"
            )

    def prepare_strict_json_schema(
        self,
        *,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
        gen_params: dict[str, Any],
    ) -> dict[str, Any]:
        if model != self._manifest.exact_model_id:
            raise RunModelManifestLockError("structured request model 已漂移")
        params = dict(gen_params)
        if self._manifest.structured_mode == "native_json_schema_strict":
            contract = build_hy3_strict_contract(
                schema_name=schema_name,
                schema=schema,
            )
            if contract.output_model is None:
                raise RunModelManifestLockError(
                    "Hy3 native strict JSON Schema 必须由本地 Pydantic 合同生成"
                )
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": contract.schema_name,
                    "schema": contract.schema,
                    "strict": True,
                },
            }
            params[STRICT_JSON_RESPONSE_CONTRACT_PARAM] = contract
            _log.info(
                "structured_json_schema_compiled",
                provider_authority=contract.provider_authority,
                base_host=contract.host,
                model=contract.exact_model_id,
                transport=contract.transport,
                dialect=contract.dialect,
                compiler_version=contract.compiler_version,
                schema_name=contract.schema_name,
                schema_hash=contract.schema_hash,
                schema_bytes=contract.schema_bytes,
                loss_report_hash=contract.loss_report_hash,
                loss_count=len(contract.loss_report),
                loss_keywords=sorted({issue.keyword for issue in contract.loss_report}),
                pydantic_validator_attached=contract.output_model is not None,
            )
            return params
        params["response_format"] = {"type": "json_object"}
        if self._manifest.pipeline_contract == EXPERIMENTAL_PIPELINE_CONTRACT:
            params[LOCAL_JSON_CONTRACT_PARAM] = LocalJsonContract.build(schema_name, schema)
        return params

    @staticmethod
    def _prepare_messages(
        messages: list[Message], gen_params: dict[str, Any]
    ) -> list[Message]:
        """Preserve the historical JSON Object render used by S2-1 evidence."""

        prepared_messages = [message.model_copy() for message in messages]
        response_format = gen_params.get("response_format")
        if response_format is None:
            if strict_contract_from_params(gen_params) is not None:
                raise RunModelManifestLockError(
                    "strict response contract 缺少 response_format"
                )
            return prepared_messages
        if response_format != {"type": "json_object"}:
            raise RunModelManifestLockError(
                "structured mode 只能使用共同 JSON Object 合同"
            )
        for index, message in enumerate(prepared_messages):
            if message.role == "system":
                prepared_messages[index] = message.model_copy(
                    update={
                        "content": (
                            f"{message.content.rstrip()}\n\n"
                            f"{_JSON_OBJECT_SYSTEM_INSTRUCTION}"
                        )
                    }
                )
                break
        else:
            prepared_messages.insert(
                0,
                Message(role="system", content=_JSON_OBJECT_SYSTEM_INSTRUCTION),
            )
        return prepared_messages

    def _prepare_request_messages(
        self, messages: list[Message], gen_params: dict[str, Any]
    ) -> list[Message]:
        from src.core.agents.structured_handoff import HANDOFF_AGENTS, prepare_handoff_messages
        messages = prepare_handoff_messages(
            messages, agent=self._agent_name, mode=self.handoff_format
        )
        response_format = gen_params.get("response_format")
        if self._manifest.structured_mode == "native_json_schema_strict":
            if response_format is None:
                if strict_contract_from_params(gen_params) is not None:
                    raise RunModelManifestLockError(
                        "strict response contract 缺少 response_format"
                    )
                return [message.model_copy() for message in messages]
            contract = strict_contract_from_params(gen_params)
            if contract is None:
                raise RunModelManifestLockError(
                    "Hy3 structured request 缺少本地 strict response contract"
                )
            try:
                assert_hy3_strict_request(
                    contract=contract,
                    base_url=self._manifest.base_url,
                    requested_model=self._manifest.exact_model_id,
                    response_format=response_format,
                )
            except RuntimeError as exc:
                raise RunModelManifestLockError(
                    "Hy3 structured schema/name/hash/identity 已漂移"
                ) from exc
            return append_strict_json_instruction(messages)
        prepared = self._prepare_messages(messages, gen_params)
        local_contract = local_contract_from_params(gen_params)
        if local_contract is not None:
            if response_format != {"type": "json_object"}:
                raise RunModelManifestLockError("本地 JSON 合同缺少 JSON-object 请求")
            instruction = local_contract.instruction()
            if self.handoff_format is not None and self._agent_name in HANDOFF_AGENTS:
                # 输出 schema 是系统格式指令；两组放在相同角色，不能破坏 JSON user。
                for index, message in enumerate(prepared):
                    if message.role == "system":
                        prepared[index] = message.model_copy(
                            update={"content": message.content + "\n\n" + instruction}
                        )
                        break
                else:
                    prepared.insert(0, Message(role="system", content=instruction))
                return prepared
            for index in range(len(prepared) - 1, -1, -1):
                if prepared[index].role == "user":
                    prepared[index] = prepared[index].model_copy(
                        update={"content": prepared[index].content + "\n\n" + instruction}
                    )
                    break
            else:
                prepared.append(Message(role="user", content=instruction))
        return prepared

    def messages_for_cost_reserve(
        self,
        messages: list[Message],
        gen_params: dict[str, Any],
    ) -> list[Message]:
        """Expose the exact post-wrapper render to the pre-request ledger."""

        prepared = self._prepare_request_messages(messages, gen_params)
        self._assert_render_bound(prepared)
        return prepared

    def prepare_bounded_non_thinking_call(
        self,
        gen_params: dict[str, Any],
        *,
        max_completion_tokens: int,
    ) -> dict[str, Any]:
        """Map a bounded trim call onto this manifest's exact Chat wire.

        The two existing trim helpers intentionally disable reasoning.  Their
        legacy Qwen-shaped kwargs (``enable_thinking`` + ``max_tokens``) must
        not leak into Hy3/DeepSeek, and DeepSeek's ``max_tokens`` must not leak
        into Hy3/Qwen.  This is request normalisation only; it creates no new
        logical call or retry.
        """

        if (
            isinstance(max_completion_tokens, bool)
            or not isinstance(max_completion_tokens, int)
            or max_completion_tokens < 1
        ):
            raise RunModelManifestLockError("non-thinking completion 上限必须为正整数")
        if self._agent_name == "smoke":
            self._assert_selection_smoke_scope()
            tier_name: RunModelTierName = "low"
        else:
            tier_name = run_model_tier_for_agent(self._agent_name)
        tier = self._manifest.tiers[tier_name]
        if max_completion_tokens > tier.max_completion_tokens:
            raise RunModelManifestLockError(
                "non-thinking completion 上限超过冻结 manifest tier: "
                f"requested={max_completion_tokens},"
                f"tier_cap={tier.max_completion_tokens}"
            )

        params = dict(gen_params)
        for key in (
            "max_tokens",
            "max_completion_tokens",
            "enable_thinking",
            "preserve_thinking",
            "thinking_budget",
            "reasoning_effort",
            "thinking",
        ):
            params.pop(key, None)
        params[self._manifest.completion_limit_wire_field] = max_completion_tokens
        if self._manifest.manifest_id == "qwen3.8-27b":
            params["enable_thinking"] = False
            params["preserve_thinking"] = False
        else:
            params["thinking"] = {"type": "disabled"}
        if self._manifest.manifest_id in {"deepseek-v4-flash", "deepseek-flash"}:
            params.pop("temperature", None)
        return params

    def prepare_outline_review_max_call(self, gen_params: dict[str, Any]) -> dict[str, Any]:
        """Use the official DeepSeek output ceiling only for outline fact review.

        Generation agents have a separate frozen profile. This review-specific
        override still passes the normal input/context and AttemptLedger guards.
        """
        if (self._agent_name != "outline_consistency_validator"
                or self._manifest.manifest_id != "deepseek-flash"
                or self._manifest.pipeline_contract != EXPERIMENTAL_PIPELINE_CONTRACT):
            raise RunModelManifestLockError("最高强度输出策略只适用于 DeepSeek 实验大纲审核")
        tier = self._manifest.tiers["xhigh"]
        official_output_cap = 393216
        if tier.provider_input_guard_tokens + official_output_cap > self._manifest.context_window:
            raise RunModelManifestLockError("大纲审核输入保护与官方输出上限超过上下文")
        params = dict(gen_params)
        params[self._manifest.completion_limit_wire_field] = official_output_cap
        params["thinking"] = {"type": "enabled"}
        params["reasoning_effort"] = "max"
        params.pop("temperature", None)
        return params

    def _assert_render_bound(self, messages: list[Message]) -> None:
        if self._manifest.pipeline_contract == EXPERIMENTAL_PIPELINE_CONTRACT and self._agent_name != "smoke":
            # The S2 ceilings describe a frozen three-chapter experiment, not
            # a production-scale v6 outline. Keep real model/cost guards.
            tier = self._manifest.tiers[run_model_tier_for_agent(self._agent_name)]
            if self.prose_generation_is_locked:
                assert self._manifest.prose_generation is not None
                tier = self._manifest.prose_generation
            elif self.upstream_generation_is_locked:
                assert self._manifest.upstream_generation is not None
                tier = self._manifest.upstream_generation
            rendered_upper_bound = conservative_rendered_input_tokens(messages)
            if rendered_upper_bound > tier.provider_input_guard_tokens:
                raise RunModelManifestLockError(
                    "已渲染 Prompt 超过冻结模型输入预算，已在 Provider 前停止: "
                    f"agent={self._agent_name},upper_bound={rendered_upper_bound},"
                    f"model_input_cap={tier.provider_input_guard_tokens}"
                )
            return
        if self._agent_name == "smoke":
            self._assert_selection_smoke_scope()
            bound = PROMPT_RENDER_BOUNDS["low"]
        else:
            bound = prompt_render_bound_for_agent(self._agent_name)
        rendered_upper_bound = conservative_rendered_input_tokens(messages)
        if rendered_upper_bound > bound.max_accounting_input_tokens:
            raise RunModelManifestLockError(
                "已渲染 Prompt 超过活动阶段可执行上界，已在 Provider 前停止: "
                f"agent={self._agent_name},upper_bound={rendered_upper_bound},"
                f"stage_cap={bound.max_accounting_input_tokens},"
                f"evidence={bound.observed_fixture}"
            )

    def complete(self, messages: list[Message], **gen_params: Any) -> LLMResponse:
        self._assert_frozen()
        requested_model = gen_params.pop("model", self._manifest.exact_model_id)
        if requested_model != self._manifest.exact_model_id:
            raise RunModelManifestLockError("单次 complete model override 被拒绝")

        prepared_messages = self._prepare_request_messages(messages, gen_params)
        self._assert_render_bound(prepared_messages)

        local_contract = local_contract_from_params(gen_params)
        wire_params = {k: v for k, v in gen_params.items() if k != LOCAL_JSON_CONTRACT_PARAM}
        if self._manifest.structured_mode == "prompt_json_local_schema":
            # DeepSeek JSON mode may return empty final content with stop.
            # Keep the same schema instruction and local validation, but omit
            # the provider JSON-mode toggle. This is an explicit manifest mode,
            # never a fallback or a relaxation of the program's output contract.
            wire_params.pop("response_format", None)
        response = self._inner.complete(
            prepared_messages,
            model=self._manifest.exact_model_id,
            **wire_params,
        )
        if self._manifest.pipeline_contract == EXPERIMENTAL_PIPELINE_CONTRACT:
            archive_manifest_response(prepared_messages, response)
        if response.model_used != self._manifest.exact_model_id:
            raise RunModelManifestLockError(
                "Provider response model identity 漂移: "
                f"expected={self._manifest.exact_model_id!r}, "
                f"actual={response.model_used!r}"
            )
        contract = strict_contract_from_params(gen_params)
        if local_contract is not None:
            local_contract.assert_unchanged()
            if self._manifest.structured_mode == "prompt_json_local_schema":
                # Raw response was archived above. Return the same JSON bytes
                # without a whole-response fence to existing strict consumers.
                response = response.model_copy(
                    update={"content": unwrap_json_code_fence(response.content)}
                )
            try:
                local_contract.validate(response.content)
            except ValueError as exc:
                raise LocalJsonResponseViolation(response, local_contract, exc) from exc
        if contract is not None:
            validation = validate_strict_json_response(
                content=response.content,
                response_model=response.model_used,
                contract=contract,
            )
            _log.info(
                "structured_json_schema_manifest_validation_passed",
                provider_authority=contract.provider_authority,
                base_host=contract.host,
                model=response.model_used,
                transport=contract.transport,
                dialect=contract.dialect,
                compiler_version=contract.compiler_version,
                schema_name=contract.schema_name,
                schema_hash=contract.schema_hash,
                raw_json_parse=validation.raw_json_parse,
                wire_schema=validation.wire_schema,
                wire_pydantic=validation.wire_pydantic,
                repair_attempted=False,
                fallback_used=False,
            )
        return response


class ManifestModelRouter:
    """Generate every routed target from one active manifest selection."""

    def __init__(
        self,
        config: WriterProfilesConfig,
        *,
        manifest: RunModelManifest,
        config_path: Path,
        catalog_lock_token: str,
        selection_test_token: str,
        client_factory: Callable[..., LLMClient] | None = None,
    ) -> None:
        self._original_config = config
        self._manifest = manifest
        self._config_path = Path(config_path)
        self.run_model_manifest_binding = RunModelManifestBinding(
            manifest_id=manifest.manifest_id,
            token=catalog_lock_token,
            selection_test=SelectionTestLossBreakerBinding(
                activity_scope="selection_test",
                token=selection_test_token,
                max_physical_requests=(manifest.selection_test.max_physical_requests),
                max_configured_rmb=manifest.selection_test.max_configured_rmb,
            ),
        )
        tier_names: tuple[RunModelTierName, ...] = ("low", "medium", "xhigh")
        profiles = {
            _profile_id(manifest, tier): _profile_for_tier(manifest, tier)
            for tier in tier_names
        }
        if manifest.prose_generation is not None:
            prose_profile = _profile_for_tier(manifest, "medium", prose=True)
            profiles[prose_profile.profile_id] = prose_profile
        if manifest.upstream_generation is not None:
            for tier_name in ("medium", "xhigh"):
                generation_profile = _profile_for_tier(
                    manifest, tier_name, upstream_generation=True)
                profiles[generation_profile.profile_id] = generation_profile
        self._config = WriterProfilesConfig(
            profiles=profiles,
            routing=RoutingConfig(
                default=_profile_id(manifest, "medium"),
                rules=[],
                agent_routing={},
            ),
        )
        self._inner = ModelRouter(self._config, client_factory=client_factory)
        self._locked_clients: dict[tuple[str, str], ManifestLockedClient] = {}

    @property
    def narrative_handoff_version(self) -> int:
        return self._manifest.narrative_handoff_version

    @property
    def run_model_manifest_token(self) -> str:
        return self.run_model_manifest_binding.token

    @property
    def pipeline_contract(self) -> str | None:
        """Expose the selected contract without constructing a Provider client."""
        return self._manifest.pipeline_contract

    def validate_profile_override(self, value: str | None) -> None:
        if value is None:
            return
        if value == self.run_model_manifest_binding.token:
            return
        selection_test = self.run_model_manifest_binding.selection_test
        if value == selection_test.token:
            from src.services.attempt_ledger import current_attempt_scope

            scope = current_attempt_scope()
            if (
                scope is not None
                and scope.activity_scope == "selection_test"
                and scope.selection_test_manifest_token == selection_test.token
            ):
                return
            raise RunModelManifestLockError(
                "selection-test token 只能由专用实验 scope 使用"
            )
        raise RunModelManifestLockError(
            "活动已启用单模型 manifest；独立 profile/force_profile override 被拒绝"
        )

    def has_profile(self, profile_id: str) -> bool:
        # No per-agent profile is a supported product override while locked.
        del profile_id
        return False

    def has_agent_routing(self, agent_name: str) -> bool:
        # Preserve feature-enable semantics such as reviewer escalation; model
        # identity itself never comes from these historical route values.
        return agent_name in self._original_config.routing.agent_routing

    def select_profile(
        self,
        agent_name: str,
        *,
        chapter_card: Any = None,
        override_profile_id: str | None = None,
    ) -> WriterProfile:
        del chapter_card
        self.validate_profile_override(override_profile_id)
        if agent_name == "smoke":
            from src.services.attempt_ledger import current_attempt_scope

            scope = current_attempt_scope()
            selection = self.run_model_manifest_binding.selection_test
            if (
                scope is None
                or scope.activity_scope != "selection_test"
                or scope.run_model_manifest_token
                != self.run_model_manifest_binding.token
                or scope.selection_test_manifest_token != selection.token
                or selection.max_physical_requests <= 0
                or selection.max_configured_rmb <= 0
            ):
                raise RunModelManifestLockError(
                    "smoke 只允许专用 selection-test scope/token 使用"
                )
            tier: RunModelTierName = "low"
        else:
            try:
                tier = run_model_tier_for_agent(agent_name)
            except ValueError as exc:
                raise RunModelManifestLockError(
                    f"未登记 agent {agent_name!r}，禁止走 default profile 或第四模型"
                ) from exc
        profile_id = _profile_id(self._manifest, tier)
        if self._manifest.prose_generation is not None and agent_name in {"writer", "reviser"}:
            profile_id = _profile_id(self._manifest, "medium") + "__prose"
        elif self._manifest.upstream_generation is not None and agent_name in _UPSTREAM_GENERATION_AGENTS:
            profile_id += "__upstream_generation"
        return self._config.profiles[profile_id]

    def route(
        self,
        agent_name: str,
        *,
        chapter_card: Any = None,
        override_profile_id: str | None = None,
    ) -> RoutedTarget:
        profile = self.select_profile(
            agent_name,
            chapter_card=chapter_card,
            override_profile_id=override_profile_id,
        )
        client_key = (profile.profile_id, agent_name)
        client = self._locked_clients.get(client_key)
        if client is None:
            client = ManifestLockedClient(
                inner=self._inner.get_client(profile),
                agent_name=agent_name,
                profile=profile,
                manifest=self._manifest,
                config_path=self._config_path,
                binding=self.run_model_manifest_binding,
            )
            self._locked_clients[client_key] = client
        return RoutedTarget(
            profile=profile,
            client=client,
            complete_kwargs=ModelRouter.build_complete_kwargs(profile, client),
        )


def build_model_router(
    config: WriterProfilesConfig,
    *,
    config_path: Path,
    client_factory: Callable[..., LLMClient] | None = None,
) -> ModelRouter | ManifestModelRouter:
    """Build legacy routing or the central lock from the same engine config."""

    catalog = load_run_model_manifest_catalog(config_path)
    manifest = catalog.active_manifest
    if manifest is None:
        return ModelRouter(config, client_factory=client_factory)
    catalog_lock_token = catalog.lock_token
    catalog_selection_test_token = catalog.selection_test_lock_token
    assert catalog_lock_token is not None and catalog_selection_test_token is not None
    lock_token = (
        f"{catalog_lock_token}:"
        f"{_executable_config_hash(config_path, include_selection_test=False)}"
    )
    selection_test_token = (
        f"{catalog_selection_test_token}:"
        f"{_executable_config_hash(config_path, include_selection_test=True)}"
    )
    return ManifestModelRouter(
        config,
        manifest=manifest,
        config_path=config_path,
        catalog_lock_token=lock_token,
        selection_test_token=selection_test_token,
        client_factory=client_factory,
    )


def build_manifest_fallback_sentinel(
    *, config_path: Path
) -> tuple[LLMClient, str] | None:
    """Return a no-credential fallback sentinel when one manifest is selected."""

    catalog = load_run_model_manifest_catalog(config_path)
    manifest = catalog.active_manifest
    if manifest is None:
        return None
    return ManifestFallbackBlockedClient(manifest.manifest_id), manifest.exact_model_id


__all__ = [
    "ManifestLockedClient",
    "ManifestModelRouter",
    "ManifestFallbackBlockedClient",
    "RunModelManifestBinding",
    "RunModelManifestLockError",
    "SelectionTestLossBreakerBinding",
    "build_model_router",
    "build_manifest_fallback_sentinel",
]
