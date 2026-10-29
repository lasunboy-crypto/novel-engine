"""Fail-closed S2-1 protocol smoke for the three frozen model candidates.

The executable path is intentionally narrower than the later three-chapter
selection corridor.  It sends two synthetic requests per candidate, performs
no transport retry, writes only an isolated AttemptLedger table, and never
creates a product workspace or run.  The repository manifest stays in its
Provider-0 ``active: null`` / ``0/0`` state; per-arm activation exists only in
an ephemeral config while that arm is executing.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from zoneinfo import ZoneInfo

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Table, create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from src.config import settings
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.config_loader import (
    load_run_model_manifest_catalog,
    load_writer_profiles_config,
)
from src.core.db import Base
from src.core.db.models import AttemptLedgerORM
from src.core.llm.base import LLMClient, LLMResponse, Message
from src.core.llm.strict_json_schema import (
    append_strict_json_instruction,
    compile_hy3_chat_json_schema,
)
from src.core.llm.openai_compatible_client import OpenAICompatibleClient
from src.core.llm.openai_compatible_endpoint import (
    resolve_openai_compatible_endpoint,
)
from src.core.models.run_model_manifest import (
    RunModelCandidateId,
    RunModelManifest,
)
from src.core.routing.run_model_manifest_router import (
    ManifestLockedClient,
    ManifestModelRouter,
    build_model_router,
)
from src.services.attempt_ledger import (
    bind_attempt_scope,
    wrap_router_with_attempt_tracking,
)
from src.services.run_model_stage_registry import (
    conservative_rendered_input_tokens,
)


S2_1_TASK_ID = "POST-V8-ABC-SINGLE-MODEL-SELECTION-01 / S2-1"
APPROVAL_DATE = date(2026, 8, 28)
APPROVAL_CONFIRMATION = (
    "POST-V8-ABC-SINGLE-MODEL-SELECTION-01/S2-1:6-logical:6-physical:CNY-0.55"
)
CANDIDATE_ORDER: tuple[RunModelCandidateId, ...] = (
    "hy3",
    "deepseek-v4-flash",
    "qwen3.8-27b",
)
SMOKE_MAX_PHYSICAL_PER_CANDIDATE = 2
SMOKE_RMB_CAPS: dict[RunModelCandidateId, Decimal] = {
    "hy3": Decimal("0.10"),
    "deepseek-v4-flash": Decimal("0.20"),
    "qwen3.8-27b": Decimal("0.25"),
}
EXPECTED_PRICES: dict[RunModelCandidateId, tuple[Decimal, Decimal]] = {
    "hy3": (Decimal("1"), Decimal("4")),
    "deepseek-v4-flash": (Decimal("3"), Decimal("9")),
    "qwen3.8-27b": (Decimal("3"), Decimal("12")),
}

JSON_SYSTEM = (
    "这是协议 smoke。只处理下方虚构公开测试数据；请严格按 JSON 输出，"
    "不得添加 Markdown。"
)
JSON_USER = (
    "把虚构记录“代号=青灯；状态=ready”转换成 JSON："
    '{"marker":"s2-1-synthetic-v1","code_name":"青灯",'
    '"status":"ready"}。'
)
THINKING_SYSTEM = "这是协议 smoke，只处理虚构公开测试数据。"
THINKING_USER = (
    "虚构规则：甲在乙之前，乙在丙之前。请先思考，再只用一句中文给出从早到晚的顺序。"
)
JSON_MESSAGES = [
    Message(role="system", content=JSON_SYSTEM),
    Message(role="user", content=JSON_USER),
]
THINKING_MESSAGES = [
    Message(role="system", content=THINKING_SYSTEM),
    Message(role="user", content=THINKING_USER),
]
JSON_OUTPUT_LIMIT = 128
THINKING_OUTPUT_LIMIT = 12_096
_EXPECTED_RENDER_INPUT_UPPER = {
    "json_object": 613,
    "hy3_json_schema": 806,
    "thinking": 302,
}

_FORBIDDEN_EVIDENCE_KEY_PARTS = (
    "api_key",
    "secret",
    "credential",
    "authorization",
)
_SENSITIVE_VALUE_RE = re.compile(r"(?i)(?:bearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,}\b)")


class SmokePreflightError(RuntimeError):
    """A local executable prerequisite failed before any request."""


class SmokeProtocolError(RuntimeError):
    """One paid response failed the approved S2-1 protocol contract."""


class _StructuredSmokeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    marker: Literal["s2-1-synthetic-v1"]
    code_name: Literal["青灯"]
    status: Literal["ready"]


class ProviderBillingRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: RunModelCandidateId
    status: Literal["known", "unavailable"]
    currency: Literal["CNY"] | None = None
    amount_rmb: Decimal | None = Field(default=None, ge=Decimal("0"))
    evidence_files: list[str] = Field(default_factory=list)
    notes: str | None = None


class ProviderBillingEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["s2-1-provider-billing-v1"]
    task_id: Literal["POST-V8-ABC-SINGLE-MODEL-SELECTION-01 / S2-1"]
    observed_at: datetime
    records: list[ProviderBillingRecord] = Field(min_length=1)


class _SmokeClient(Protocol):
    def prepare_bounded_non_thinking_call(
        self,
        gen_params: dict[str, Any],
        *,
        max_completion_tokens: int,
    ) -> dict[str, Any]: ...

    def prepare_strict_json_schema(
        self,
        *,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
        gen_params: dict[str, Any],
    ) -> dict[str, Any]: ...

    def messages_for_cost_reserve(
        self,
        messages: list[Message],
        gen_params: dict[str, Any],
    ) -> list[Message]: ...

    def complete(
        self,
        messages: list[Message],
        **gen_params: Any,
    ) -> LLMResponse: ...


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _wire_contract(manifest: RunModelManifest) -> dict[str, Any]:
    completion_field = manifest.completion_limit_wire_field
    json_wire: dict[str, Any] = {
        completion_field: JSON_OUTPUT_LIMIT,
        "response_format": {"type": "json_object"},
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if manifest.structured_mode == "native_json_schema_strict":
        compilation = compile_hy3_chat_json_schema(
            _StructuredSmokeResponse.model_json_schema()
        )
        json_wire["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "s2_1_synthetic_record",
                "schema": compilation.schema,
                "strict": True,
            },
        }
    thinking_wire: dict[str, Any] = {
        completion_field: THINKING_OUTPUT_LIMIT,
        **manifest.tiers["low"].thinking_request,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    temperature = manifest.tiers["low"].temperature
    if temperature is not None:
        json_wire["temperature"] = temperature
        thinking_wire["temperature"] = temperature
    if manifest.manifest_id == "qwen3.8-27b":
        json_wire["enable_thinking"] = False
        json_wire["preserve_thinking"] = False
    else:
        json_wire["thinking"] = {"type": "disabled"}
    return {
        "json_object": json_wire,
        "thinking_plain_text": thinking_wire,
    }


def _assert_provider_zero_source(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SmokePreflightError("config.yaml 顶层不是 mapping")
    section = raw.get("run_model_manifest")
    if not isinstance(section, dict):
        raise SmokePreflightError("缺少 run_model_manifest")
    if section.get("active") is not None:
        raise SmokePreflightError("live run_model_manifest.active 必须保持 null")
    manifests = section.get("manifests")
    if not isinstance(manifests, dict) or set(manifests) != set(CANDIDATE_ORDER):
        raise SmokePreflightError("manifest 必须且只能包含三个批准候选")
    for candidate in CANDIDATE_ORDER:
        payload = manifests.get(candidate)
        selection = payload.get("selection_test") if isinstance(payload, dict) else None
        if not isinstance(selection, dict):
            raise SmokePreflightError(f"{candidate} selection_test 缺失")
        if (
            selection.get("max_physical_requests") != 0
            or Decimal(str(selection.get("max_configured_rmb"))) != 0
        ):
            raise SmokePreflightError(f"live {candidate} selection_test 必须保持 0/0")
    return raw


def _load_provider_zero_source(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SmokePreflightError(f"无法读取 source config: {path}") from exc
    return _assert_provider_zero_source(raw)


def _render_bounds() -> dict[str, int]:
    prepared_json = ManifestLockedClient._prepare_messages(
        JSON_MESSAGES,
        {"response_format": {"type": "json_object"}},
    )
    bounds = {
        "json_object": conservative_rendered_input_tokens(prepared_json),
        "hy3_json_schema": conservative_rendered_input_tokens(
            append_strict_json_instruction(JSON_MESSAGES)
        ),
        "thinking": conservative_rendered_input_tokens(THINKING_MESSAGES),
    }
    if bounds != _EXPECTED_RENDER_INPUT_UPPER:
        raise SmokePreflightError(
            "S2-1 固定 synthetic prompt render 上界已漂移:"
            f"expected={_EXPECTED_RENDER_INPUT_UPPER},actual={bounds}"
        )
    return bounds


def build_smoke_plan(config_path: Path) -> dict[str, Any]:
    """Build the exact approved activity without credentials or clients."""

    _load_provider_zero_source(config_path)
    catalog = load_run_model_manifest_catalog(config_path)
    bounds = _render_bounds()
    output_upper = JSON_OUTPUT_LIMIT + THINKING_OUTPUT_LIMIT
    million = Decimal(1_000_000)
    candidates: list[dict[str, Any]] = []
    for candidate in CANDIDATE_ORDER:
        manifest = catalog.manifests[candidate]
        json_bound_key = (
            "hy3_json_schema"
            if manifest.structured_mode == "native_json_schema_strict"
            else "json_object"
        )
        input_upper = bounds[json_bound_key] + bounds["thinking"]
        expected_input, expected_output = EXPECTED_PRICES[candidate]
        if (
            manifest.input_price_rmb_per_million != expected_input
            or manifest.output_price_rmb_per_million != expected_output
        ):
            raise SmokePreflightError(
                f"{candidate} 运行日价格漂移:"
                f"actual={manifest.input_price_rmb_per_million}/"
                f"{manifest.output_price_rmb_per_million},"
                f"expected={expected_input}/{expected_output}"
            )
        reserve = (
            Decimal(input_upper) * expected_input / million
            + Decimal(output_upper) * expected_output / million
        )
        cap = SMOKE_RMB_CAPS[candidate]
        if reserve >= cap:
            raise SmokePreflightError(f"{candidate} smoke 费用预留未低于批准熔断器")
        candidates.append(
            {
                "candidate": candidate,
                "logical_requests": 2,
                "max_physical_requests": 2,
                "same_model_retry_limit": 0,
                "wire_contract": _wire_contract(manifest),
                "input_price_rmb_per_million": _decimal_text(expected_input),
                "output_price_rmb_per_million": _decimal_text(expected_output),
                "configured_reserve_rmb": _decimal_text(
                    reserve.quantize(Decimal("0.000001"))
                ),
                "rmb_cap": _decimal_text(cap),
            }
        )
    return {
        "task_id": S2_1_TASK_ID,
        "approval_date": APPROVAL_DATE.isoformat(),
        "provider_calls": 0,
        "candidate_order": list(CANDIDATE_ORDER),
        "logical_requests_per_candidate": 2,
        "max_physical_requests_per_candidate": 2,
        "same_model_retry_limit": 0,
        "render_input_upper": bounds,
        "output_upper": {
            "json_object": JSON_OUTPUT_LIMIT,
            "thinking_reasoning_plus_content": THINKING_OUTPUT_LIMIT,
        },
        "candidates": candidates,
        "aggregate": {
            "logical_requests": 6,
            "max_physical_requests": 6,
            "configured_rmb_cap": "0.55",
        },
        "external_data": {
            "classification": "synthetic_public_non_personal",
            "json_record": "代号=青灯；状态=ready",
            "thinking_rule": "甲在乙之前，乙在丙之前",
            "real_canon_or_workspace_data": False,
        },
    }


def prepare_ephemeral_manifest_config(
    source: Path,
    *,
    candidate: RunModelCandidateId,
    destination: Path,
) -> None:
    """Create one executable copy while leaving live Provider-0 untouched."""

    raw = _load_provider_zero_source(source)
    section = raw["run_model_manifest"]
    manifests = section["manifests"]
    section["active"] = candidate
    for item in CANDIDATE_ORDER:
        manifests[item]["selection_test"] = {
            "activity_scope": "selection_test",
            "max_physical_requests": (
                SMOKE_MAX_PHYSICAL_PER_CANDIDATE if item == candidate else 0
            ),
            "max_configured_rmb": (
                _decimal_text(SMOKE_RMB_CAPS[item]) if item == candidate else 0
            ),
        }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    destination.chmod(0o600)


def _assert_no_sensitive_material(value: Any, *, path: str = "root") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key).lower()
            if any(part in key_text for part in _FORBIDDEN_EVIDENCE_KEY_PARTS):
                raise SmokePreflightError(f"Provider 证据含敏感字段: {path}.{key}")
            _assert_no_sensitive_material(child, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_sensitive_material(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str) and _SENSITIVE_VALUE_RE.search(value):
        raise SmokePreflightError(f"Provider 证据含疑似密钥内容: {path}")


def _evidence_digest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SmokePreflightError(f"Provider 账单证据文件不存在: {path}")
    size = path.stat().st_size
    if size <= 0 or size > 20 * 1024 * 1024:
        raise SmokePreflightError(f"Provider 账单证据文件大小非法: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"name": path.name, "sha256": digest, "bytes": size}


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_activity_path(activity_dir: Path, source_config: Path) -> Path:
    resolved = activity_dir.resolve()
    if not activity_dir.is_absolute():
        raise SmokePreflightError("activity_dir 必须是绝对路径")
    if resolved.exists():
        raise SmokePreflightError("activity_dir 已存在，禁止重放或覆盖")
    repository_root = source_config.resolve().parent
    data_root = Path(settings.data_dir).resolve()
    if _is_relative_to(resolved, repository_root) or _is_relative_to(
        resolved, data_root
    ):
        raise SmokePreflightError("activity_dir 不得位于仓库或真实 workspace data root")
    return resolved


def _default_credential_presence() -> dict[RunModelCandidateId, bool]:
    return {
        "hy3": bool(settings.tokenhub_api_key.get_secret_value()),
        "deepseek-v4-flash": bool(settings.deepseek_api_key.get_secret_value()),
        "qwen3.8-27b": bool(settings.dashscope_api_key.get_secret_value()),
    }


def _selection_client_factory(
    *,
    model: str,
    base_url: str | None,
    provider: str,
) -> LLMClient:
    if provider != "openai_compatible" or base_url is None:
        raise SmokePreflightError("S2-1 只允许 exact OpenAI-compatible manifest")
    endpoint = resolve_openai_compatible_endpoint(
        base_url,
    )
    api_key = getattr(settings, endpoint.settings_field)
    return OpenAICompatibleClient(
        model=model,
        base_url=base_url,
        api_key=api_key,
        enable_thinking=False if model == "qwen3.8-27b" else None,
        preserve_thinking=False if model == "qwen3.8-27b" else None,
        max_attempts=1,
    )


def _initialize_ledger(path: Path) -> tuple[Engine, Callable[[str], Session]]:
    engine = create_engine(f"sqlite:///{path}")
    attempt_table = cast(Table, AttemptLedgerORM.__table__)
    Base.metadata.create_all(engine, tables=[attempt_table])
    path.chmod(0o600)

    def factory(_workspace_id: str) -> Session:
        return Session(engine)

    return engine, factory


def _validate_common_response(
    response: LLMResponse,
    *,
    candidate: RunModelCandidateId,
) -> None:
    if response.model_used != candidate:
        raise SmokeProtocolError(
            f"response model identity 漂移: {response.model_used!r}"
        )
    if response.usage.input_tokens <= 0 or response.usage.output_tokens <= 0:
        raise SmokeProtocolError("response usage 必须为正数 known token")
    if response.finish_reason != "stop":
        raise SmokeProtocolError(
            f"response finish_reason 不是 stop: {response.finish_reason!r}"
        )


def _validate_json_response(
    response: LLMResponse,
    *,
    candidate: RunModelCandidateId,
) -> None:
    _validate_common_response(response, candidate=candidate)
    if response.reasoning_chars != 0:
        raise SmokeProtocolError("thinking-off JSON 响应仍含 reasoning_content")
    try:
        payload = json.loads(response.content)
        _StructuredSmokeResponse.model_validate(payload)
    except Exception as exc:
        raise SmokeProtocolError("JSON Object 未通过固定本地 schema/值验收") from exc


def _validate_thinking_response(
    response: LLMResponse,
    *,
    candidate: RunModelCandidateId,
) -> None:
    _validate_common_response(response, candidate=candidate)
    if response.reasoning_chars <= 0:
        raise SmokeProtocolError("thinking 响应缺少独立 reasoning_content")
    content = response.content.strip()
    if not content or "\n" in content:
        raise SmokeProtocolError("thinking 最终 content 不是一行纯文本")
    positions = [content.find(marker) for marker in ("甲", "乙", "丙")]
    if positions[0] < 0 or positions != sorted(positions) or len(set(positions)) != 3:
        raise SmokeProtocolError("thinking 最终 content 未给出甲→乙→丙顺序")


def _logical_result(
    *,
    candidate: RunModelCandidateId,
    kind: Literal["json_object", "thinking"],
    response: LLMResponse,
    render_input_upper: int,
    protocol_pass: bool = True,
    failure: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "candidate": candidate,
        "kind": kind,
        "request_model": candidate,
        "response_model": response.model_used,
        "model_used": response.model_used,
        "transport": "openai_chat_completions_sse",
        "json_object": kind == "json_object",
        "thinking_enabled": kind == "thinking",
        "sse_terminal_received": True,
        "provider_request_id": response.provider_request_id,
        "usage_status": "known",
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "reasoning_chars": response.reasoning_chars,
        "content": response.content,
        "finish_reason": response.finish_reason,
        "render_input_upper": render_input_upper,
        "protocol_pass": protocol_pass,
        "failure": failure,
    }


def _safe_error_text(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    text = _SENSITIVE_VALUE_RE.sub("[REDACTED]", text)
    return text[:1000]


def _ledger_summary(engine: Engine) -> dict[str, Any]:
    with Session(engine) as session:
        rows = list(
            session.scalars(
                select(AttemptLedgerORM).order_by(AttemptLedgerORM.created_at)
            )
        )
    configured = sum(
        (Decimal(str(row.configured_cost_amount or 0)) for row in rows),
        start=Decimal("0"),
    )
    known_rows = [row for row in rows if row.usage_status == "known"]
    input_tokens_known = sum(row.input_tokens or 0 for row in known_rows)
    output_tokens_known = sum(row.output_tokens or 0 for row in known_rows)
    attempt_indexes: dict[str, int] = {}
    attempts: list[dict[str, Any]] = []
    logical_kinds = ("json_object", "thinking")
    for row in rows:
        candidate_index = attempt_indexes.get(row.model, 0)
        attempt_indexes[row.model] = candidate_index + 1
        attempts.append(
            {
                "attempt_id": row.attempt_id,
                "candidate": row.model,
                "kind": (
                    logical_kinds[candidate_index]
                    if candidate_index < len(logical_kinds)
                    else "unexpected_extra_attempt"
                ),
                "request_model": row.model,
                "task_id": row.task_id,
                "run_id": row.run_id,
                "chapter_id": row.chapter_id,
                "logical_call_id": row.logical_call_id,
                "technical_attempt": row.attempt_number,
                "status": row.status,
                "usage_status": row.usage_status,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "provider_request_id": row.provider_request_id,
                "response_status": row.response_status,
                "configured_cost_currency": row.configured_cost_currency,
                "configured_cost_amount": (
                    str(row.configured_cost_amount)
                    if row.configured_cost_amount is not None
                    else None
                ),
                "configured_cost_basis": row.configured_cost_basis,
            }
        )
    return {
        "provider_calls": len(rows),
        "logical_requests_started": len(rows),
        "usage_known_attempts": sum(row.usage_status == "known" for row in rows),
        "usage_unknown_attempts": sum(row.usage_status == "unknown" for row in rows),
        "input_tokens_known": input_tokens_known,
        "output_tokens_known": output_tokens_known,
        "total_tokens_known": input_tokens_known + output_tokens_known,
        "provider_request_id_count": sum(bool(row.provider_request_id) for row in rows),
        "configured_rmb": _decimal_text(configured),
        "attempts": attempts,
    }


def _enrich_arm_summaries(report: dict[str, Any]) -> None:
    attempts = report["attempts"]
    for arm in report["arm_results"]:
        arm_attempts = [row for row in attempts if row["candidate"] == arm["candidate"]]
        known_attempts = [row for row in arm_attempts if row["usage_status"] == "known"]
        configured = sum(
            (Decimal(str(row["configured_cost_amount"] or 0)) for row in arm_attempts),
            start=Decimal("0"),
        )
        arm.update(
            {
                "provider_calls": len(arm_attempts),
                "logical_requests_started": len(arm_attempts),
                "usage_known_attempts": len(known_attempts),
                "usage_unknown_attempts": sum(
                    row["usage_status"] == "unknown" for row in arm_attempts
                ),
                "input_tokens_known": sum(
                    row["input_tokens"] or 0 for row in known_attempts
                ),
                "output_tokens_known": sum(
                    row["output_tokens"] or 0 for row in known_attempts
                ),
                "provider_request_id_count": sum(
                    bool(row["provider_request_id"]) for row in arm_attempts
                ),
                "configured_rmb": _decimal_text(configured),
                "attempts": arm_attempts,
            }
        )
        arm["total_tokens_known"] = (
            arm["input_tokens_known"] + arm["output_tokens_known"]
        )


def execute_approved_smoke(
    *,
    source_config: Path,
    activity_dir: Path,
    confirmation: str,
    credential_presence: Mapping[RunModelCandidateId, bool] | None = None,
    client_factory: Callable[..., LLMClient] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Execute every arm independently within the approved six-request bound."""

    if confirmation != APPROVAL_CONFIRMATION:
        raise SmokePreflightError("缺少精确 S2-1 批准确认串")
    source_catalog = load_run_model_manifest_catalog(source_config)
    if (
        client_factory is None
        and source_catalog.manifests["hy3"].structured_mode
        == "native_json_schema_strict"
    ):
        raise SmokePreflightError(
            "历史 S2-1 批准不能授权 Hy3 native json_schema；"
            "真实协议必须使用另批 T4 活动"
        )
    plan = build_smoke_plan(source_config)
    plan_by_candidate = {row["candidate"]: row for row in plan["candidates"]}
    observed_now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    presence = dict(
        _default_credential_presence()
        if credential_presence is None
        else credential_presence
    )
    if set(presence) != set(CANDIDATE_ORDER):
        raise SmokePreflightError("credential presence 必须覆盖三个 exact 候选")
    resolved_activity = _validate_activity_path(activity_dir, source_config)

    factory = client_factory or _selection_client_factory
    # Validate all three executable manifests and exact hosts before the first
    # paid request.  These temporary files are destroyed even on failure.
    with tempfile.TemporaryDirectory(prefix="s2-1-preflight-") as temp_root:
        preflight_root = Path(temp_root)
        for candidate in CANDIDATE_ORDER:
            candidate_config = preflight_root / f"{candidate}.yaml"
            prepare_ephemeral_manifest_config(
                source_config,
                candidate=candidate,
                destination=candidate_config,
            )
            catalog = load_run_model_manifest_catalog(candidate_config)
            active = catalog.active_manifest
            if active is None or active.manifest_id != candidate:
                raise SmokePreflightError(f"{candidate} ephemeral manifest 未激活")
            endpoint = resolve_openai_compatible_endpoint(active.base_url)
            if endpoint.settings_field != active.credential_source_id:
                raise SmokePreflightError(f"{candidate} credential authority 漂移")

    resolved_activity.mkdir(mode=0o700, parents=True)
    ledger_path = resolved_activity / "attempt-ledger.sqlite3"
    engine, session_factory = _initialize_ledger(ledger_path)
    report: dict[str, Any] = {
        **plan,
        "provider_calls": 0,
        "approval_confirmation": confirmation,
        "started_at": observed_now.isoformat(),
        "status": "running",
        "logical_results": [],
        "arm_results": [],
        "failed_candidates": [],
        "provider_actual_bill": {
            "status": "optional_unreconciled",
            "amount_rmb": None,
            "evidence": None,
        },
    }

    try:
        with tempfile.TemporaryDirectory(prefix="s2-1-active-") as temp_root:
            temp_config_root = Path(temp_root)
            for candidate in CANDIDATE_ORDER:
                manifest = plan_by_candidate[candidate]
                source_manifest = source_catalog.manifests[candidate]
                arm: dict[str, Any] = {
                    "candidate": candidate,
                    "request_model": candidate,
                    "base_url": source_manifest.base_url,
                    "credential_source_id": source_manifest.credential_source_id,
                    "transport": "openai_chat_completions_sse",
                    "planned_logical_requests": 2,
                    "max_physical_requests": SMOKE_MAX_PHYSICAL_PER_CANDIDATE,
                    "rmb_cap": manifest["rmb_cap"],
                    "status": "running",
                    "logical_results": [],
                    "failed_logical_kind": None,
                    "failure": None,
                }
                report["arm_results"].append(arm)
                if presence[candidate] is not True:
                    failure = {
                        "error_type": "SmokePreflightError",
                        "message": f"{candidate} credential 本地为空",
                    }
                    arm["status"] = "preflight_failed"
                    arm["failure"] = failure
                    report["failed_candidates"].append(candidate)
                    continue

                current_kind: Literal["json_object", "thinking"] | None = None
                current_upper = 0
                try:
                    candidate_config = temp_config_root / f"{candidate}.yaml"
                    prepare_ephemeral_manifest_config(
                        source_config,
                        candidate=candidate,
                        destination=candidate_config,
                    )
                    config = load_writer_profiles_config(candidate_config)
                    manifest_router = build_model_router(
                        config,
                        config_path=candidate_config,
                        client_factory=factory,
                    )
                    if not isinstance(manifest_router, ManifestModelRouter):
                        raise SmokePreflightError(
                            f"{candidate} 未构造 manifest-locked router"
                        )
                    binding = manifest_router.run_model_manifest_binding
                    tracked = wrap_router_with_attempt_tracking(
                        manifest_router,
                        workspace_id=f"s2-1-isolated-ledger-{candidate}",
                        session_factory=session_factory,
                    )
                    with bind_attempt_scope(
                        workspace_id=f"s2-1-isolated-ledger-{candidate}",
                        run_id=None,
                        task_id=f"s2-1-smoke-{candidate}-20260828",
                        chapter_id=None,
                        provider_call_cap=SMOKE_MAX_PHYSICAL_PER_CANDIDATE,
                        activity_scope="selection_test",
                        run_model_manifest_token=binding.token,
                        selection_test_manifest_token=binding.selection_test.token,
                        configured_cost_currency="CNY",
                        configured_cost_cap=SMOKE_RMB_CAPS[candidate],
                    ):
                        current_kind = "json_object"
                        json_target = tracked.route("smoke")
                        json_client = cast(_SmokeClient, json_target.client)
                        json_params = json_client.prepare_bounded_non_thinking_call(
                            json_target.complete_kwargs,
                            max_completion_tokens=JSON_OUTPUT_LIMIT,
                        )
                        json_params = prepare_strict_json_output(
                            client=json_client,
                            model=candidate,
                            output_model=_StructuredSmokeResponse,
                            schema_name="s2_1_synthetic_record",
                            complete_kwargs=json_params,
                        )
                        json_messages = json_client.messages_for_cost_reserve(
                            JSON_MESSAGES,
                            json_params,
                        )
                        current_upper = conservative_rendered_input_tokens(
                            json_messages
                        )
                        expected_json_upper = _EXPECTED_RENDER_INPUT_UPPER[
                            "hy3_json_schema" if candidate == "hy3" else "json_object"
                        ]
                        if current_upper != expected_json_upper:
                            raise SmokePreflightError("JSON render 上界在请求前漂移")
                        json_response = json_client.complete(
                            JSON_MESSAGES,
                            **json_params,
                        )
                        try:
                            _validate_json_response(json_response, candidate=candidate)
                        except Exception as exc:
                            result = _logical_result(
                                candidate=candidate,
                                kind="json_object",
                                response=json_response,
                                render_input_upper=current_upper,
                                protocol_pass=False,
                                failure={
                                    "error_type": type(exc).__name__,
                                    "message": _safe_error_text(exc),
                                },
                            )
                            arm["logical_results"].append(result)
                            report["logical_results"].append(result)
                            raise
                        result = _logical_result(
                            candidate=candidate,
                            kind="json_object",
                            response=json_response,
                            render_input_upper=current_upper,
                        )
                        arm["logical_results"].append(result)
                        report["logical_results"].append(result)

                        current_kind = "thinking"
                        thinking_target = tracked.route("smoke")
                        thinking_client = cast(_SmokeClient, thinking_target.client)
                        thinking_params = dict(thinking_target.complete_kwargs)
                        thinking_messages = thinking_client.messages_for_cost_reserve(
                            THINKING_MESSAGES,
                            thinking_params,
                        )
                        current_upper = conservative_rendered_input_tokens(
                            thinking_messages
                        )
                        if current_upper != _EXPECTED_RENDER_INPUT_UPPER["thinking"]:
                            raise SmokePreflightError(
                                "thinking render 上界在请求前漂移"
                            )
                        thinking_response = thinking_client.complete(
                            THINKING_MESSAGES,
                            **thinking_params,
                        )
                        try:
                            _validate_thinking_response(
                                thinking_response,
                                candidate=candidate,
                            )
                        except Exception as exc:
                            result = _logical_result(
                                candidate=candidate,
                                kind="thinking",
                                response=thinking_response,
                                render_input_upper=current_upper,
                                protocol_pass=False,
                                failure={
                                    "error_type": type(exc).__name__,
                                    "message": _safe_error_text(exc),
                                },
                            )
                            arm["logical_results"].append(result)
                            report["logical_results"].append(result)
                            raise
                        result = _logical_result(
                            candidate=candidate,
                            kind="thinking",
                            response=thinking_response,
                            render_input_upper=current_upper,
                        )
                        arm["logical_results"].append(result)
                        report["logical_results"].append(result)
                except Exception as exc:
                    arm["status"] = "protocol_failed"
                    arm["failed_logical_kind"] = current_kind
                    arm["failure"] = {
                        "error_type": type(exc).__name__,
                        "message": _safe_error_text(exc),
                    }
                    report["failed_candidates"].append(candidate)
                    continue
                arm["status"] = "protocol_pass"
        report["status"] = (
            "protocol_pass" if not report["failed_candidates"] else "protocol_failed"
        )
    finally:
        ledger = _ledger_summary(engine)
        report.update(ledger)
        _enrich_arm_summaries(report)
        report["completed_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        report_path = resolved_activity / "s2-1-smoke-report.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        report_path.chmod(0o600)
        engine.dispose()
    return report


def reconcile_provider_billing(
    *,
    report_path: Path,
    billing_evidence_path: Path,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Write an append-only Provider bill layer without rewriting the ledger.

    ``unavailable`` remains an explicit unknown terminal, never zero.  The
    original smoke report and AttemptLedger stay immutable.
    """

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        raw_billing = json.loads(billing_evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SmokePreflightError("无法读取 smoke report 或 Provider 账单证据") from exc
    _assert_no_sensitive_material(raw_billing)
    if report.get("task_id") != S2_1_TASK_ID:
        raise SmokePreflightError("smoke report task_id 漂移")
    if report.get("provider_actual_bill", {}).get("status") != "optional_unreconciled":
        raise SmokePreflightError("smoke report 已不是可选账单未核对状态")
    try:
        evidence = ProviderBillingEvidence.model_validate(raw_billing)
    except Exception as exc:
        raise SmokePreflightError("Provider 账单证据 schema 非法") from exc
    if evidence.observed_at.tzinfo is None:
        raise SmokePreflightError("Provider 账单 observed_at 必须带时区")
    attempted = {
        row.get("candidate")
        for row in report.get("attempts", [])
        if row.get("candidate") in CANDIDATE_ORDER
    }
    by_candidate = {record.candidate: record for record in evidence.records}
    if len(by_candidate) != len(evidence.records) or set(by_candidate) != attempted:
        raise SmokePreflightError("Provider 账单记录必须且只能覆盖实际已发送候选")

    attempts = report.get("attempts", [])
    records: list[dict[str, Any]] = []
    has_unavailable = False
    for candidate in CANDIDATE_ORDER:
        if candidate not in by_candidate:
            continue
        record = by_candidate[candidate]
        if record.status == "known":
            if record.currency != "CNY" or record.amount_rmb is None:
                raise SmokePreflightError(
                    f"{candidate} known bill 必须含 CNY amount_rmb"
                )
        else:
            has_unavailable = True
            if record.currency is not None or record.amount_rmb is not None:
                raise SmokePreflightError(
                    f"{candidate} unavailable bill 不得伪填币种或金额"
                )
        evidence_rows: list[dict[str, Any]] = []
        for raw_path in record.evidence_files:
            evidence_path = Path(raw_path)
            if not evidence_path.is_absolute():
                evidence_path = billing_evidence_path.parent / evidence_path
            evidence_rows.append(_evidence_digest(evidence_path.resolve()))
        configured = sum(
            (
                Decimal(str(row.get("configured_cost_amount") or 0))
                for row in attempts
                if row.get("candidate") == candidate
            ),
            start=Decimal("0"),
        )
        records.append(
            {
                "candidate": candidate,
                "configured_rmb": _decimal_text(configured),
                "provider_bill_status": record.status,
                "provider_bill_currency": record.currency,
                "provider_bill_amount_rmb": (
                    _decimal_text(record.amount_rmb)
                    if record.amount_rmb is not None
                    else None
                ),
                "evidence": evidence_rows,
                "notes": record.notes,
            }
        )
    reconciliation = {
        "schema_version": "s2-1-billing-reconciliation-v1",
        "task_id": S2_1_TASK_ID,
        "smoke_report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "observed_at": evidence.observed_at.isoformat(),
        "status": (
            "billing_reconciled_with_unknown"
            if has_unavailable
            else "billing_reconciled"
        ),
        "records": records,
    }
    destination = output_path or report_path.with_name(
        "s2-1-billing-reconciliation.json"
    )
    if destination.exists():
        raise SmokePreflightError("billing reconciliation 已存在，禁止覆盖")
    destination.write_text(
        json.dumps(reconciliation, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    destination.chmod(0o600)
    return reconciliation


__all__ = [
    "APPROVAL_CONFIRMATION",
    "APPROVAL_DATE",
    "CANDIDATE_ORDER",
    "S2_1_TASK_ID",
    "SMOKE_MAX_PHYSICAL_PER_CANDIDATE",
    "SMOKE_RMB_CAPS",
    "SmokePreflightError",
    "SmokeProtocolError",
    "build_smoke_plan",
    "execute_approved_smoke",
    "prepare_ephemeral_manifest_config",
    "reconcile_provider_billing",
]
