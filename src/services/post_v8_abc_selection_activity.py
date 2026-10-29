"""POST-V8 ABC 三模型选择活动的冻结合同与可恢复执行底座。

本模块只服务 ``selection_test``。仓库 ``config.yaml`` 必须继续保持
``run_model_manifest.active=null`` 和三个静态 breaker ``0/0``；每个候选的
正数授权只存在于仓库与产品 ``DATA_DIR`` 之外的独立活动目录。

活动分两段且严格线性：

1. 12 个纯虚构 Writer 校准 case 使用真实 Writer assembler、exact manifest
   client 与独立 AttemptLedger；
2. 校准终态的物理请求数与 configured CNY（known usage 用实数，unknown 保留
   reserve）从本臂后续固定额度扣除，剩余值才写进产品临时 manifest。产品内
   selection-test AttemptLedger 按整个专用 workspace 聚合，outline task、首卡
   task、pipeline run 和人工恢复不会各自重置。

普通 production 配置、公开 API/Schema 与 New Book 的六章规则不读取本模块的
正数 breaker，也不会取得三章许可。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
from copy import deepcopy
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, Literal, cast
from uuid import uuid4

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Table, create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from src.config import settings
from src.core.agents.writer import WriterOutput, write_chapter
from src.core.config_loader import (
    load_model_calibration,
    load_run_model_manifest_catalog,
    load_writer_profiles_config,
)
from src.core.db import Base
from src.core.db.models import AttemptLedgerORM
from src.core.llm.base import LLMClient, LLMResponse, Message, TokenUsage
from src.core.models.chapter_card import ChapterCard
from src.core.models.model_calibration import ModelCalibration
from src.core.models.outline import WorldOutput
from src.core.models.run_model_manifest import RunModelCandidateId, RunModelManifest
from src.core.models.setting_contract import SettingContract
from src.core.models.style_template import STYLE_GENERATION_DEFAULTS
from src.core.models.workspace import Workspace
from src.core.models.writer_profile import WriterProfile
from src.core.routing.run_model_manifest_router import (
    ManifestModelRouter,
    build_model_router,
)
from src.core.routing.router import ModelRouter, RoutedTarget
from src.core.workspace.meta import write_meta
from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.attempt_ledger import (
    DEFAULT_PROVIDER_CALL_HARD_CAP,
    AttemptLedgerStore,
    AttemptScope,
    AttemptTrackingClient,
    bind_attempt_scope,
)
from src.services.run_model_stage_registry import (
    conservative_rendered_input_tokens,
)


ACTIVITY_SCHEMA: Literal["post-v8-abc-selection-activity-v1"] = (
    "post-v8-abc-selection-activity-v1"
)
CORPUS_SCHEMA: Literal["post-v8-abc-calibration-corpus-v1"] = (
    "post-v8-abc-calibration-corpus-v1"
)
STATE_SCHEMA = "post-v8-abc-selection-state-v1"
RESULT_SCHEMA = "post-v8-abc-model-calibration-result-v1"
ACTIVITY_ENV = "POST_V8_ABC_SELECTION_ACTIVITY_ROOT"
FRONTEND_MODE: Literal["post-v8-abc-selection-short-v1"] = (
    "post-v8-abc-selection-short-v1"
)
CORPUS_RELATIVE_PATH = Path(
    "fixtures/post_v8_abc_selection/calibration_corpus_v1.json"
)
STYLE_GUIDE_RELATIVE_PATH = Path(
    "prompts/style_guide_templates/dark_mystery.md"
)
WRITER_PROMPT_RELATIVE_PATH = Path("prompts/writer.md")
FROZEN_SOURCE_CONFIG_SHA256 = (
    "b029ee2dfffdde81d67cdc9f6f577594ddbc6d7b9a8c566216dc35e717705d0a"
)
FROZEN_CORPUS_SHA256 = (
    "d26820d9c8509c098f99ca6c0bbabde2b77a05a779fb1069ced5829a7706bdae"
)
FROZEN_WRITER_PROMPT_SHA256 = (
    "9b4a36032527233d852f3f15f58fcf423dd715b469f352edb8e334a33ed28c55"
)
FROZEN_STYLE_GUIDE_SHA256 = (
    "ab1aa03b2dab1493301d6fe3108753e2b21a77cfc106e85d9604478758424a31"
)
FROZEN_CHAPTER_CARD_SCHEMA_SHA256 = (
    "a80635b43dbe545e3652208ad0b5c8f56235b2e83191571adcedc76452df0548"
)
FROZEN_WRITER_OUTPUT_SCHEMA_SHA256 = (
    "3269229a18d8a639ed4cb83e5543d65a57d1012421c6d62ef86b973bfb1ab181"
)
PRODUCT_PREMISE: Literal[
    "一座封闭的旧城档案馆停电后，一名修复师发现三份互相矛盾的失踪记录，必须在天亮前判断谁改写了档案。"
] = (
    "一座封闭的旧城档案馆停电后，一名修复师发现三份互相矛盾的失踪记录，"
    "必须在天亮前判断谁改写了档案。"
)
PRODUCT_GENRE: Literal["mystery"] = "mystery"
PRODUCT_TOTAL_VOLUMES: Literal[1] = 1
PRODUCT_CHAPTERS_PER_VOLUME: Literal[3] = 3
PRODUCT_TENSION_TEMPLATE: Literal["heros_journey"] = "heros_journey"
PRODUCT_DEFAULT_VOICE: Literal["third_person"] = "third_person"
PRODUCT_STYLE_TEMPLATE: Literal["dark_mystery"] = "dark_mystery"
CALIBRATION_CASES = 12
ARM_TOTAL_PHYSICAL_CAP = 150
SMOKE_PHYSICAL_DEBIT: Literal[2] = 2
POST_SMOKE_PHYSICAL_CAP = 148
ARM_TOTAL_RMB_CAP = Decimal("20")
POST_SMOKE_RMB_CAPS: dict[RunModelCandidateId, Decimal] = {
    "hy3": Decimal("19.90"),
    "deepseek-v4-flash": Decimal("19.80"),
    "qwen3.8-27b": Decimal("19.75"),
}
SMOKE_RMB_DEBITS: dict[RunModelCandidateId, Decimal] = {
    candidate: ARM_TOTAL_RMB_CAP - remaining
    for candidate, remaining in POST_SMOKE_RMB_CAPS.items()
}
TASK_NUMBERS: dict[RunModelCandidateId, int] = {
    "hy3": 3,
    "deepseek-v4-flash": 4,
    "qwen3.8-27b": 5,
}
PORTS: dict[RunModelCandidateId, tuple[int, int]] = {
    # 三臂严格串行，因此前端复用仓库已允许 CORS 的 worktree 端口 5174；
    # 后端端口与 DATA_DIR 仍逐臂隔离。不得为选型扩普通 production CORS 面。
    "hy3": (8313, 5174),
    "deepseek-v4-flash": (8314, 5174),
    "qwen3.8-27b": (8315, 5174),
}
CANDIDATE_ORDER: tuple[RunModelCandidateId, ...] = (
    "hy3",
    "deepseek-v4-flash",
    "qwen3.8-27b",
)

_FORBIDDEN_INPUT_MARKERS = (
    "v6",
    "v7",
    "v8",
    "canon",
    "product_workspace",
    "historical_chapter_text",
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(?:bearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,}\b|"
    r"(?:api[_-]?key|authorization|credential|token)\s*[:=]\s*[^\s,;]+)"
)


class SelectionActivityError(RuntimeError):
    """活动合同、恢复现场或保险丝不满足。"""


class SelectionActivityBudgetExhausted(SelectionActivityError):
    """校准后没有正数产品余额；该臂未完成且不得换模型。"""


class ProductInput(BaseModel):
    """三臂逐字相同的产品输入；workspace/display name 由候选派生。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    premise: Literal[
        "一座封闭的旧城档案馆停电后，一名修复师发现三份互相矛盾的失踪记录，必须在天亮前判断谁改写了档案。"
    ] = PRODUCT_PREMISE
    genre: Literal["mystery"] = PRODUCT_GENRE
    total_volumes: Literal[1] = PRODUCT_TOTAL_VOLUMES
    chapters_per_volume: Literal[3] = PRODUCT_CHAPTERS_PER_VOLUME
    tension_template: Literal["heros_journey"] = PRODUCT_TENSION_TEMPLATE
    default_voice: Literal["third_person"] = PRODUCT_DEFAULT_VOICE
    style_template: Literal["dark_mystery"] = PRODUCT_STYLE_TEMPLATE
    arc_contract: tuple[Literal["起因"], Literal["升级"], Literal["高潮/交代"]] = (
        "起因",
        "升级",
        "高潮/交代",
    )
    imported_assets: Literal[False] = False


class ActivityBreakers(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_physical_requests: int = Field(gt=0, le=POST_SMOKE_PHYSICAL_CAP)
    max_configured_rmb: Decimal = Field(gt=Decimal("0"))


class SelectionActivityRuntime(BaseModel):
    """临时 config 中唯一可激活短活动的内部合同。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["post-v8-abc-selection-activity-v1"] = ACTIVITY_SCHEMA
    activity_id: str = Field(min_length=1)
    task_number: Literal[3, 4, 5]
    candidate: RunModelCandidateId
    phase: Literal["calibration", "product_ready"]
    activity_root: Path
    calibration_workspace_id: str
    product_workspace_id: str
    data_dir: Path
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    immutable_contract_sha256: str = Field(pattern=r"^(?:|[0-9a-f]{64})$")
    calibration_result_sha256: str = Field(pattern=r"^(?:|[0-9a-f]{64})$")
    smoke_physical_debit: Literal[2] = SMOKE_PHYSICAL_DEBIT
    smoke_rmb_debit: Decimal = Field(gt=Decimal("0"))
    carryover_physical_debit: int = Field(default=0, ge=0)
    carryover_configured_rmb_debit: Decimal = Field(
        default=Decimal("0"), ge=Decimal("0")
    )
    phase_breakers: ActivityBreakers
    product_input: ProductInput = Field(default_factory=ProductInput)
    frontend_mode: Literal["post-v8-abc-selection-short-v1"] = FRONTEND_MODE

    @model_validator(mode="after")
    def _validate_candidate_owned_values(self) -> "SelectionActivityRuntime":
        if self.task_number != TASK_NUMBERS[self.candidate]:
            raise ValueError("candidate 与线性任务号不一致")
        if self.smoke_rmb_debit != SMOKE_RMB_DEBITS[self.candidate]:
            raise ValueError("smoke RMB debit 漂移")
        if self.phase_breakers.max_configured_rmb > POST_SMOKE_RMB_CAPS[
            self.candidate
        ]:
            raise ValueError("phase RMB breaker 超过本臂固定后续额度")
        entry_physical = POST_SMOKE_PHYSICAL_CAP - self.carryover_physical_debit
        entry_rmb = (
            POST_SMOKE_RMB_CAPS[self.candidate]
            - self.carryover_configured_rmb_debit
        )
        if entry_physical <= 0 or entry_rmb <= 0:
            raise ValueError("carryover 已耗尽本臂固定后续额度")
        if self.phase == "calibration":
            if (
                self.phase_breakers.max_physical_requests
                != entry_physical
                or self.phase_breakers.max_configured_rmb
                != entry_rmb
            ):
                raise ValueError(
                    "calibration breaker 必须是 smoke 与 carryover 扣除后的完整额度"
                )
            if self.immutable_contract_sha256 or self.calibration_result_sha256:
                raise ValueError("calibration 初态不得伪造 contract/result hash")
        else:
            if not self.immutable_contract_sha256 or not self.calibration_result_sha256:
                raise ValueError("product_ready 必须绑定校准 contract/result hash")
            if (
                self.phase_breakers.max_physical_requests > entry_physical
                or self.phase_breakers.max_configured_rmb > entry_rmb
            ):
                raise ValueError("product_ready breaker 超过 carryover 后可用额度")
        return self


class CalibrationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(pattern=r"^(?:introspection|action)-0[1-6]$")
    genre_archetype: Literal["introspection", "action"]
    card: ChapterCard


class CalibrationCorpus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_premise: ClassVar[str | None] = PRODUCT_PREMISE

    schema_version: Literal["post-v8-abc-calibration-corpus-v1"] = CORPUS_SCHEMA
    corpus_id: Literal["post-v8-abc-synthetic-archive-writer-12-v1"]
    classification: Literal["synthetic_fictional_non_personal"]
    prohibited_sources: list[str]
    world: WorldOutput
    meta: Workspace
    style: dict[str, str]
    cases: list[CalibrationCase] = Field(min_length=12, max_length=12)

    @model_validator(mode="after")
    def _validate_balanced_and_non_personal(self) -> "CalibrationCorpus":
        if tuple(self.prohibited_sources) != _FORBIDDEN_INPUT_MARKERS:
            raise ValueError("禁止来源清单漂移")
        counts = {
            genre: sum(case.genre_archetype == genre for case in self.cases)
            for genre in ("introspection", "action")
        }
        if counts != {"introspection": 6, "action": 6}:
            raise ValueError("校准语料必须严格 6 introspection + 6 action")
        if len({case.case_id for case in self.cases}) != 12:
            raise ValueError("校准 case_id 重复")
        if any(case.card.workspace_id != self.meta.workspace_id for case in self.cases):
            raise ValueError("校准卡 workspace_id 必须同源")
        if self.expected_premise is not None and self.meta.premise != self.expected_premise:
            raise ValueError("校准 meta premise 与三章活动不一致")
        if self.style != {
            "template": PRODUCT_STYLE_TEMPLATE,
            "guide_source": str(STYLE_GUIDE_RELATIVE_PATH),
        }:
            raise ValueError("校准 style 合同漂移")
        return self


@dataclass(frozen=True, slots=True)
class LoadedProductSelectionActivity:
    runtime: SelectionActivityRuntime
    config_path: Path
    activity_root: Path
    manifest_token: str
    selection_token: str


class _CaptureClient(LLMClient):
    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.params: dict[str, Any] = {}
        self.creative_delta_capability: str | None = None
        self.authority_assembly_capability: str | None = None
        self.prose_generation_is_locked = False

    def complete(self, messages: list[Message], **gen_params: Any) -> LLMResponse:
        self.messages = [message.model_copy() for message in messages]
        self.params = dict(gen_params)
        return LLMResponse(
            content="offline-capture-only",
            model_used="calibration-prompt-lock",
            usage=TokenUsage(input_tokens=0, output_tokens=0),
            finish_reason="stop",
        )


class _WriterMessagesCaptured(Exception):
    """Stop a render-only pass before parsing or validating any fake prose."""


class _WriterCaptureClient(_CaptureClient):
    def complete(self, messages: list[Message], **gen_params: Any) -> LLMResponse:
        self.messages = [message.model_copy() for message in messages]
        self.params = dict(gen_params)
        raise _WriterMessagesCaptured()


class _CaptureRouter:
    def __init__(self, client: _CaptureClient, profile: Any = None) -> None:
        self._client = client
        self._profile = profile or SimpleNamespace(
            model="calibration-prompt-lock",
            generation_params={"temperature": 0.7, "max_tokens": 24_384},
            extra_request_kwargs={},
        )

    def route(self, agent_name: str, **_: Any) -> RoutedTarget:
        if agent_name != "writer":
            raise SelectionActivityError(
                f"校准 prompt 捕获只允许 writer，收到 {agent_name!r}"
            )
        return cast(
            RoutedTarget,
            SimpleNamespace(
                profile=self._profile,
                client=self._client,
                complete_kwargs=ModelRouter.build_complete_kwargs(cast(WriterProfile, self._profile), self._client),
            ),
        )


class _SingleTargetRouter:
    """让真实 ``write_chapter`` 只取得本 case 已锁定的 tracked target。"""

    def __init__(self, target: RoutedTarget) -> None:
        self._target = target

    def route(self, agent_name: str, **_: Any) -> RoutedTarget:
        if agent_name != "writer":
            raise SelectionActivityError(
                f"校准执行只允许 writer，收到 {agent_name!r}"
            )
        return self._target


class _PromptHashLockedClient(LLMClient):
    """Provider 前验证真实 Writer assembler 仍产冻结 messages。"""

    def __init__(self, *, inner: LLMClient, expected_sha256: str) -> None:
        self._inner = inner
        self._expected_sha256 = expected_sha256

    def prepare_strict_json_schema(
        self,
        *,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
        gen_params: dict[str, Any],
    ) -> dict[str, Any]:
        return self._inner.prepare_strict_json_schema(
            model=model,
            schema_name=schema_name,
            schema=schema,
            gen_params=gen_params,
        )

    def complete(self, messages: list[Message], **gen_params: Any) -> LLMResponse:
        actual = _messages_sha256(messages)
        if actual != self._expected_sha256:
            raise SelectionActivityError(
                "Writer prompt hash 在 Provider 前漂移: "
                f"expected={self._expected_sha256}, actual={actual}"
            )
        return self._inner.complete(messages, **gen_params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _atomic_write_json(path: Path, payload: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_bytes(_canonical_json_bytes(payload) + b"\n")
    temporary.chmod(mode)
    os.replace(temporary, path)


def _atomic_write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SelectionActivityError(f"无法读取 JSON 合同:{path}") from exc
    if not isinstance(payload, dict):
        raise SelectionActivityError(f"JSON 合同顶层必须是对象:{path}")
    return payload


def _load_yaml_object(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SelectionActivityError(f"无法读取 YAML 配置:{path}") from exc
    if not isinstance(payload, dict):
        raise SelectionActivityError(f"YAML 顶层必须是对象:{path}")
    return payload


def _safe_error_text(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return _SENSITIVE_VALUE_RE.sub("[REDACTED]", text)[:1000]


def _messages_sha256(messages: list[Message]) -> str:
    return _sha256_bytes(
        _canonical_json_bytes(
            [message.model_dump(mode="json") for message in messages]
        )
    )


def _schema_hash(model: type[BaseModel]) -> str:
    return _sha256_bytes(_canonical_json_bytes(model.model_json_schema()))


def _assert_frozen_repo_inputs(repo_root: Path) -> None:
    expected = {
        "source config": FROZEN_SOURCE_CONFIG_SHA256,
        "calibration corpus": FROZEN_CORPUS_SHA256,
        "Writer prompt": FROZEN_WRITER_PROMPT_SHA256,
        "style guide": FROZEN_STYLE_GUIDE_SHA256,
        "ChapterCard schema": FROZEN_CHAPTER_CARD_SCHEMA_SHA256,
        "WriterOutput schema": FROZEN_WRITER_OUTPUT_SCHEMA_SHA256,
    }
    actual = {
        "source config": _sha256_file(repo_root / "config.yaml"),
        "calibration corpus": _sha256_file(repo_root / CORPUS_RELATIVE_PATH),
        "Writer prompt": _sha256_file(repo_root / WRITER_PROMPT_RELATIVE_PATH),
        "style guide": _sha256_file(repo_root / STYLE_GUIDE_RELATIVE_PATH),
        "ChapterCard schema": _schema_hash(ChapterCard),
        "WriterOutput schema": _schema_hash(WriterOutput),
    }
    drift = {
        name: {"expected": expected[name], "actual": value}
        for name, value in actual.items()
        if value != expected[name]
    }
    if drift:
        raise SelectionActivityError(
            "任务 2 冻结的共同输入/Schema 已漂移；禁止混跑，须重新治理: "
            + json.dumps(drift, ensure_ascii=False, sort_keys=True)
        )


def load_frozen_corpus(repo: Path | None = None) -> CalibrationCorpus:
    repo_root = (repo or _repo_root()).resolve()
    payload = _load_json_object(repo_root / CORPUS_RELATIVE_PATH)
    return CalibrationCorpus.model_validate(payload)


def _assert_provider_zero_source(raw: dict[str, Any]) -> None:
    section = raw.get("run_model_manifest")
    if not isinstance(section, dict) or section.get("active") is not None:
        raise SelectionActivityError(
            "仓库源 run_model_manifest.active 必须保持 null"
        )
    manifests = section.get("manifests")
    if not isinstance(manifests, dict) or set(manifests) != set(CANDIDATE_ORDER):
        raise SelectionActivityError("仓库源 manifest 必须且只能有三个候选")
    for candidate in CANDIDATE_ORDER:
        payload = manifests.get(candidate)
        selection = payload.get("selection_test") if isinstance(payload, dict) else None
        if not isinstance(selection, dict) or (
            selection.get("max_physical_requests") != 0
            or Decimal(str(selection.get("max_configured_rmb"))) != 0
        ):
            raise SelectionActivityError(
                f"仓库源 {candidate} selection_test 必须保持 0/0"
            )


def _assert_runtime_config_is_exact_derivative(
    *,
    raw_config: dict[str, Any],
    runtime: SelectionActivityRuntime,
    repo_root: Path,
) -> None:
    """只允许临时 config 改 active、breaker、runtime 与本臂新校准。

    这样任务 3—5 即使需要从失败点恢复，也不能借“本地适配修复”改 endpoint、
    host、价格、tier、普通 agent route、Prompt 配置或任意一家难度。真正需要改变
    冻结合同的修复必须停止并回到治理卡，而不是在活动目录里静默漂移。
    """

    source = _load_yaml_object(repo_root / "config.yaml")
    _assert_provider_zero_source(source)
    normalized = deepcopy(raw_config)
    normalized.pop("selection_activity_runtime", None)

    section = normalized.get("run_model_manifest")
    source_section = source.get("run_model_manifest")
    if not isinstance(section, dict) or not isinstance(source_section, dict):
        raise SelectionActivityError("临时 selection config 缺 manifest")
    section["active"] = source_section.get("active")
    manifests = section.get("manifests")
    source_manifests = source_section.get("manifests")
    if not isinstance(manifests, dict) or not isinstance(source_manifests, dict):
        raise SelectionActivityError("临时 selection config manifest 清单损坏")
    for candidate in CANDIDATE_ORDER:
        manifest = manifests.get(candidate)
        source_manifest = source_manifests.get(candidate)
        if not isinstance(manifest, dict) or not isinstance(source_manifest, dict):
            raise SelectionActivityError("临时 selection config 候选清单损坏")
        manifest["selection_test"] = deepcopy(source_manifest["selection_test"])

    calibrations = normalized.get("model_calibration")
    if runtime.phase == "product_ready":
        if not isinstance(calibrations, dict):
            raise SelectionActivityError("product-ready config 缺 model_calibration")
        calibrations.pop(runtime.candidate, None)
    elif isinstance(calibrations, dict) and runtime.candidate in calibrations:
        raise SelectionActivityError("calibration 阶段不得预置本臂历史校准")

    if normalized != source:
        raise SelectionActivityError(
            "临时 selection config 含 active/breaker/runtime/本臂校准之外的未授权修改"
        )


def _activity_id(candidate: RunModelCandidateId) -> str:
    return f"post-v8-abc-selection-{candidate}-v1"


def product_workspace_id(candidate: RunModelCandidateId) -> str:
    return f"post-v8-abc-{candidate}-selection-v1"


def product_display_name(candidate: RunModelCandidateId) -> str:
    return f"POST-V8 ABC 三章选择活动 {candidate}"


def _reserved_product_workspace_ids() -> frozenset[str]:
    return frozenset(product_workspace_id(candidate) for candidate in CANDIDATE_ORDER)


def build_arm_plan(
    candidate: RunModelCandidateId,
    *,
    carryover_physical_debit: int = 0,
    carryover_configured_rmb_debit: Decimal = Decimal("0"),
    repo: Path | None = None,
) -> dict[str, Any]:
    """Provider-0 只读返回某臂完整边界与产品输入。"""

    repo_root = (repo or _repo_root()).resolve()
    _assert_frozen_repo_inputs(repo_root)
    corpus_path = repo_root / CORPUS_RELATIVE_PATH
    corpus = load_frozen_corpus(repo_root)
    catalog = load_run_model_manifest_catalog(repo_root / "config.yaml")
    if catalog.active is not None:
        raise SelectionActivityError("仓库源 active 必须为 null")
    manifest = catalog.manifests[candidate]
    api_port, frontend_port = PORTS[candidate]
    entry_breakers = _entry_breakers(
        candidate,
        carryover_physical_debit=carryover_physical_debit,
        carryover_configured_rmb_debit=carryover_configured_rmb_debit,
    )
    return {
        "schema_version": ACTIVITY_SCHEMA,
        "provider_calls": 0,
        "task_number": TASK_NUMBERS[candidate],
        "candidate": candidate,
        "linear_predecessor": (
            None if candidate == "hy3" else CANDIDATE_ORDER[CANDIDATE_ORDER.index(candidate) - 1]
        ),
        "model": manifest.exact_model_id,
        "endpoint": manifest.base_url,
        "host": manifest.host,
        "region": manifest.region,
        "credential_source_id": manifest.credential_source_id,
        "workspace_authority_id": manifest.workspace_authority_id,
        "fallback": False,
        "smoke_debit": {
            "physical_requests": SMOKE_PHYSICAL_DEBIT,
            "reserved_rmb": str(SMOKE_RMB_DEBITS[candidate]),
        },
        "whole_arm_caps": {
            "physical_requests": ARM_TOTAL_PHYSICAL_CAP,
            "configured_rmb": str(ARM_TOTAL_RMB_CAP),
        },
        "post_smoke_caps": {
            "physical_requests": POST_SMOKE_PHYSICAL_CAP,
            "configured_rmb": str(POST_SMOKE_RMB_CAPS[candidate]),
            "unused_smoke_subbudget_reclaimed": False,
        },
        "carryover_debit": {
            "physical_requests": carryover_physical_debit,
            "configured_rmb": str(carryover_configured_rmb_debit),
            "reclaimed": False,
        },
        "activity_entry_caps": {
            "physical_requests": entry_breakers.max_physical_requests,
            "configured_rmb": str(entry_breakers.max_configured_rmb),
        },
        "calibration": {
            "writer_cases": CALIBRATION_CASES,
            "introspection": 6,
            "action": 6,
            "corpus_id": corpus.corpus_id,
            "corpus_sha256": _sha256_file(corpus_path),
            "writer_prompt_sha256": _sha256_file(
                repo_root / WRITER_PROMPT_RELATIVE_PATH
            ),
            "style_guide_sha256": _sha256_file(
                repo_root / STYLE_GUIDE_RELATIVE_PATH
            ),
            "reviewer_calls": 0,
            "product_workspace_written": False,
        },
        "product": {
            **ProductInput().model_dump(mode="json"),
            "workspace_id": product_workspace_id(candidate),
            "display_name": product_display_name(candidate),
            "chapter_1_is_checkpoint_not_trial": True,
            "normal_ui_service_attempt_ledger": True,
            "all_agents_exact_same_model": True,
        },
        "ports": {"api": api_port, "frontend": frontend_port},
        "external_data": {
            "classification": "synthetic_fictional_non_personal",
            "real_canon_or_workspace_data": False,
            "prompt_payload_contains_credentials": False,
            "transport_requires_manifest_credential_authority": True,
        },
    }


def _entry_breakers(
    candidate: RunModelCandidateId,
    *,
    carryover_physical_debit: int,
    carryover_configured_rmb_debit: Decimal,
) -> ActivityBreakers:
    if carryover_physical_debit < 0 or carryover_configured_rmb_debit < 0:
        raise SelectionActivityError("carryover debit 不得为负数")
    physical = POST_SMOKE_PHYSICAL_CAP - carryover_physical_debit
    configured_rmb = (
        POST_SMOKE_RMB_CAPS[candidate] - carryover_configured_rmb_debit
    )
    if physical <= 0 or configured_rmb <= 0:
        raise SelectionActivityError("carryover 已耗尽本臂固定后续额度")
    return ActivityBreakers(
        max_physical_requests=physical,
        max_configured_rmb=configured_rmb,
    )


def _materialize_corpus(
    *,
    corpus: CalibrationCorpus,
    activity_root: Path,
    repo: Path,
    candidate: RunModelCandidateId,
    manifest: RunModelManifest | None = None,
    include_workspace_voice_contract: bool = False,
) -> tuple[Path, list[dict[str, Any]]]:
    workspaces_root = activity_root / "calibration-inputs"
    paths = WorkspacePaths(corpus.meta.workspace_id, workspaces_root=workspaces_root)
    for directory in (
        paths.root,
        paths.prompts_dir,
        paths.data_dir,
        paths.cards_dir,
        paths.outline_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    write_meta(paths.meta_json, corpus.meta)
    _atomic_write_json(
        paths.outline_dir / "world.json",
        corpus.world.model_dump(mode="json"),
    )
    style_source = repo / STYLE_GUIDE_RELATIVE_PATH
    style_target = paths.prompts_dir / "style_guide.md"
    style_target.write_bytes(style_source.read_bytes())
    style_target.chmod(0o600)
    for case in corpus.cases:
        _atomic_write_json(
            paths.cards_dir / f"{case.card.chapter_id}.json",
            case.card.model_dump(mode="json"),
        )

    if manifest is not None and manifest.exact_model_id != candidate:
        raise SelectionActivityError("校准候选与提供的模型合同不一致")
    selected_manifest = manifest or load_run_model_manifest_catalog(repo / "config.yaml").manifests[candidate]
    setting_contract = SettingContract.from_world_output(corpus.world)
    request_rows: list[dict[str, Any]] = []
    for case in corpus.cases:
        capture = _WriterCaptureClient()
        # Use the selected manifest's capability resolution for the exact same
        # Writer renderer as execution. The transport is never instantiated.
        from src.core.routing.run_model_manifest_router import (
            manifest_creative_capability, manifest_authority_capability, _profile_for_tier,
        )
        capture.creative_delta_capability = manifest_creative_capability(selected_manifest)
        capture.authority_assembly_capability = manifest_authority_capability(selected_manifest)
        capture.prose_generation_is_locked = selected_manifest.prose_generation is not None
        profile = _profile_for_tier(selected_manifest, "medium", prose=capture.prose_generation_is_locked) if manifest is not None else None
        executable_plan = None
        if include_workspace_voice_contract:
            # Mirror the product orchestrator's no-approved-baseline branch.
            # A direct Writer call otherwise loses meta.default_voice because
            # no previous chapter voice summary is available in this corpus.
            from src.core.rules.cross_chapter_voice_validator import (
                resolve_cross_chapter_voice_constraint_spec,
            )
            from src.core.rules.prose_hard_contract import compile_executable_constraint_plan

            voice = resolve_cross_chapter_voice_constraint_spec(
                baseline_fingerprint=None,
                baseline_chapter_id=None,
                workspace_default_voice=corpus.meta.default_voice,
                chapter_card=case.card,
                mini_plan_event=None,
            )
            executable_plan = compile_executable_constraint_plan(
                chapter_card=case.card,
                stage="draft",
                setting_contract=setting_contract,
                cross_chapter_voice=voice,
            )
        try:
            write_chapter(
                chapter_card=case.card,
                memory_section="",
                setting_contract=setting_contract,
                router=cast(Any, _CaptureRouter(capture, profile)),
                prompts_dir=repo / "prompts",
                vector_store=None,
                db_session=None,
                workspace_paths=paths,
                workspace_id=corpus.meta.workspace_id,
                creation_type="draft",
                executable_constraint_plan=executable_plan,
            )
        except _WriterMessagesCaptured:
            pass
        else:
            raise SelectionActivityError("Writer capture 未到达真实请求出口")
        if len(capture.messages) != 2 or [
            message.role for message in capture.messages
        ] != ["system", "user"]:
            raise SelectionActivityError(
                f"Writer assembler 消息形状漂移:{case.case_id}"
            )
        messages_payload = [
            message.model_dump(mode="json") for message in capture.messages
        ]
        request = {
            "case_id": case.case_id,
            "genre_archetype": case.genre_archetype,
            "chapter_id": case.card.chapter_id,
            "messages": messages_payload,
            **({"generation_params": capture.params} if manifest is not None else {}),
            "prompt_sha256": _messages_sha256(capture.messages),
            "input_token_upper_bound": conservative_rendered_input_tokens(
                capture.messages
            ),
            "chapter_card_sha256": _sha256_bytes(
                _canonical_json_bytes(case.card.model_dump(mode="json"))
            ),
            "chapter_card_schema_sha256": _schema_hash(ChapterCard),
            "writer_output_schema_sha256": _schema_hash(WriterOutput),
            "provider_response_schema": "plain_text",
            "word_count_min": case.card.word_count_min,
            "word_count_max": case.card.word_count_max,
            "plot_node_budget": case.card.plot_node_budget,
        }
        request_path = activity_root / "calibration-requests" / f"{case.case_id}.json"
        _atomic_write_json(request_path, request)
        request_rows.append(
            {
                key: request[key]
                for key in (
                    "case_id",
                    "genre_archetype",
                    "chapter_id",
                    "prompt_sha256",
                    "input_token_upper_bound",
                    "chapter_card_sha256",
                    "chapter_card_schema_sha256",
                    "writer_output_schema_sha256",
                    "provider_response_schema",
                    "word_count_min",
                    "word_count_max",
                    "plot_node_budget",
                )
            }
            | {
                "request_path": str(request_path.relative_to(activity_root)),
                "request_file_sha256": _sha256_file(request_path),
            }
        )
    return workspaces_root, request_rows


def _initialize_attempt_ledger(path: Path) -> tuple[Engine, Callable[[str], Session]]:
    engine = create_engine(f"sqlite:///{path}")
    attempt_table = cast(Table, AttemptLedgerORM.__table__)
    Base.metadata.create_all(engine, tables=[attempt_table])
    path.chmod(0o600)

    def factory(_workspace_id: str) -> Session:
        return Session(engine)

    return engine, factory


def _runtime_payload(
    *,
    candidate: RunModelCandidateId,
    activity_root: Path,
    corpus_sha256: str,
    breakers: ActivityBreakers,
    carryover_physical_debit: int = 0,
    carryover_configured_rmb_debit: Decimal = Decimal("0"),
    phase: Literal["calibration", "product_ready"] = "calibration",
    immutable_contract_sha256: str = "",
    calibration_result_sha256: str = "",
) -> dict[str, Any]:
    runtime = SelectionActivityRuntime(
        activity_id=_activity_id(candidate),
        task_number=cast(Any, TASK_NUMBERS[candidate]),
        candidate=candidate,
        phase=phase,
        activity_root=activity_root,
        calibration_workspace_id=f"{_activity_id(candidate)}-calibration",
        product_workspace_id=product_workspace_id(candidate),
        data_dir=activity_root / "product-data",
        corpus_sha256=corpus_sha256,
        immutable_contract_sha256=immutable_contract_sha256,
        calibration_result_sha256=calibration_result_sha256,
        smoke_rmb_debit=SMOKE_RMB_DEBITS[candidate],
        carryover_physical_debit=carryover_physical_debit,
        carryover_configured_rmb_debit=carryover_configured_rmb_debit,
        phase_breakers=breakers,
    )
    return runtime.model_dump(mode="json")


def _write_ephemeral_config(
    *,
    source_config: Path,
    destination: Path,
    candidate: RunModelCandidateId,
    runtime_payload: dict[str, Any],
) -> None:
    raw = _load_yaml_object(source_config)
    _assert_provider_zero_source(raw)
    section = cast(dict[str, Any], raw["run_model_manifest"])
    manifests = cast(dict[str, Any], section["manifests"])
    section["active"] = candidate
    breakers = ActivityBreakers.model_validate(runtime_payload["phase_breakers"])
    for item in CANDIDATE_ORDER:
        manifests[item]["selection_test"] = {
            "activity_scope": "selection_test",
            "max_physical_requests": (
                breakers.max_physical_requests if item == candidate else 0
            ),
            "max_configured_rmb": (
                str(breakers.max_configured_rmb) if item == candidate else 0
            ),
        }
    calibrations = raw.get("model_calibration")
    if calibrations is not None and not isinstance(calibrations, dict):
        raise SelectionActivityError("源 model_calibration 不是对象")
    if isinstance(calibrations, dict) and candidate in calibrations:
        raise SelectionActivityError(
            f"源配置已存在 {candidate} 校准，禁止把历史数字冒充新型号结果"
        )
    raw["selection_activity_runtime"] = runtime_payload
    _atomic_write_yaml(destination, raw)


def prepare_activity(
    *,
    candidate: RunModelCandidateId,
    source_config: Path,
    activity_root: Path,
    carryover_physical_debit: int = 0,
    carryover_configured_rmb_debit: Decimal = Decimal("0"),
    repo: Path | None = None,
) -> dict[str, Any]:
    """Provider-0 冻结一个全新、可恢复的候选活动目录。"""

    repo_root = (repo or _repo_root()).resolve()
    _assert_frozen_repo_inputs(repo_root)
    source = source_config.resolve()
    root = activity_root.resolve()
    if not activity_root.is_absolute():
        raise SelectionActivityError("activity_root 必须是绝对路径")
    if root.exists():
        raise SelectionActivityError("activity_root 已存在，禁止覆盖或重冻")
    if source != (repo_root / "config.yaml").resolve():
        raise SelectionActivityError("source_config 必须是当前仓库 Provider-0 config.yaml")
    if root.is_relative_to(repo_root) or root.is_relative_to(Path(settings.data_dir).resolve()):
        raise SelectionActivityError("activity_root 不得位于仓库或普通产品 DATA_DIR")
    raw_source = _load_yaml_object(source)
    _assert_provider_zero_source(raw_source)
    corpus = load_frozen_corpus(repo_root)
    corpus_path = repo_root / CORPUS_RELATIVE_PATH
    corpus_sha = _sha256_file(corpus_path)

    root.mkdir(mode=0o700, parents=True)
    try:
        for relative in (
            "runtime",
            "calibration-requests",
            "calibration-responses",
            "evidence",
            "product-data",
        ):
            (root / relative).mkdir(mode=0o700, parents=True)
        workspaces_root, requests = _materialize_corpus(
            corpus=corpus,
            activity_root=root,
            repo=repo_root,
            candidate=candidate,
        )
        ledger_path = root / "calibration-attempt-ledger.sqlite3"
        engine, _factory = _initialize_attempt_ledger(ledger_path)
        engine.dispose()
        initial_breakers = _entry_breakers(
            candidate,
            carryover_physical_debit=carryover_physical_debit,
            carryover_configured_rmb_debit=carryover_configured_rmb_debit,
        )
        runtime = _runtime_payload(
            candidate=candidate,
            activity_root=root,
            corpus_sha256=corpus_sha,
            breakers=initial_breakers,
            carryover_physical_debit=carryover_physical_debit,
            carryover_configured_rmb_debit=carryover_configured_rmb_debit,
        )
        runtime_config = root / "runtime/config.yaml"
        _write_ephemeral_config(
            source_config=source,
            destination=runtime_config,
            candidate=candidate,
            runtime_payload=runtime,
        )
        plan = build_arm_plan(
            candidate,
            carryover_physical_debit=carryover_physical_debit,
            carryover_configured_rmb_debit=carryover_configured_rmb_debit,
            repo=repo_root,
        )
        immutable_contract = {
            "schema_version": ACTIVITY_SCHEMA,
            "activity_id": _activity_id(candidate),
            "candidate": candidate,
            "source_config_sha256": _sha256_file(source),
            "source_model_identity_hash": load_run_model_manifest_catalog(
                source
            ).model_identity_hash(),
            "corpus_path": str(CORPUS_RELATIVE_PATH),
            "corpus_sha256": corpus_sha,
            "writer_prompt_sha256": _sha256_file(
                repo_root / WRITER_PROMPT_RELATIVE_PATH
            ),
            "style_guide_sha256": _sha256_file(
                repo_root / STYLE_GUIDE_RELATIVE_PATH
            ),
            "chapter_card_schema_sha256": _schema_hash(ChapterCard),
            "writer_output_schema_sha256": _schema_hash(WriterOutput),
            "calibration_workspace_root": str(workspaces_root),
            "calibration_workspace_is_product": False,
            "requests": requests,
            "plan": plan,
            "policy": {
                "serial": True,
                "same_model_only": True,
                "fallback": False,
                "unknown_attempt_replay": False,
                "product_workspace_created_by_prepare": False,
                "source_provider_zero_unchanged": True,
            },
        }
        contract_path = root / "evidence/immutable-contract.json"
        _atomic_write_json(contract_path, immutable_contract)
        contract_sha = _sha256_file(contract_path)
        (root / "evidence/immutable-contract.sha256").write_text(
            f"{contract_sha}\n", encoding="utf-8"
        )
        (root / "evidence/immutable-contract.sha256").chmod(0o600)
        # calibration phase model requires empty hashes.  The immutable hash is
        # carried in separately hashed state until product_ready.
        _write_ephemeral_config(
            source_config=source,
            destination=runtime_config,
            candidate=candidate,
            runtime_payload=runtime,
        )
        state = {
            "schema_version": STATE_SCHEMA,
            "activity_id": _activity_id(candidate),
            "candidate": candidate,
            "status": "calibration_ready",
            "immutable_contract_sha256": contract_sha,
            "calibration_cases": [
                {
                    "case_id": row["case_id"],
                    "status": "pending",
                    "round": 0,
                    "logical_call_id": None,
                    "response_path": None,
                    "error": None,
                }
                for row in requests
            ],
            "product": {
                "status": "not_started",
                "workspace_id": product_workspace_id(candidate),
                "run_id": None,
            },
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        _atomic_write_json(root / "runtime/activity-state.json", state)
    except BaseException:
        # 仅移除本次全新且尚未读凭据/发请求的半成品目录。
        import shutil

        shutil.rmtree(root, ignore_errors=True)
        raise
    return {
        "provider_calls": 0,
        "activity_root": str(root),
        "runtime_config": str(runtime_config),
        "immutable_contract_sha256": contract_sha,
        "candidate": candidate,
        "status": "calibration_ready",
    }


@dataclass(frozen=True, slots=True)
class _PreparedActivity:
    root: Path
    runtime_config: Path
    runtime: SelectionActivityRuntime
    state_path: Path
    state: dict[str, Any]
    contract_path: Path
    contract: dict[str, Any]
    corpus: CalibrationCorpus


def _load_prepared_activity(
    activity_root: Path,
    *,
    repo: Path | None = None,
) -> _PreparedActivity:
    """重验活动目录、冻结输入和临时配置，不读凭据。"""

    repo_root = (repo or _repo_root()).resolve()
    _assert_frozen_repo_inputs(repo_root)
    if not activity_root.is_absolute():
        raise SelectionActivityError("activity_root 必须是绝对路径")
    root = activity_root.resolve()
    runtime_config = root / "runtime/config.yaml"
    state_path = root / "runtime/activity-state.json"
    contract_path = root / "evidence/immutable-contract.json"
    state = _load_json_object(state_path)
    contract = _load_json_object(contract_path)
    contract_sha = _sha256_file(contract_path)
    recorded_sha_path = root / "evidence/immutable-contract.sha256"
    try:
        recorded_sha = recorded_sha_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SelectionActivityError("immutable contract hash 文件缺失") from exc
    if recorded_sha != contract_sha:
        raise SelectionActivityError("immutable contract hash 漂移")
    if state.get("immutable_contract_sha256") != contract_sha:
        raise SelectionActivityError("activity state 未绑定 immutable contract")

    raw_config = _load_yaml_object(runtime_config)
    runtime = SelectionActivityRuntime.model_validate(
        raw_config.get("selection_activity_runtime")
    )
    _assert_runtime_config_is_exact_derivative(
        raw_config=raw_config,
        runtime=runtime,
        repo_root=repo_root,
    )
    if runtime.activity_root.resolve() != root:
        raise SelectionActivityError("临时配置 activity_root 漂移")
    if runtime.activity_id != contract.get("activity_id"):
        raise SelectionActivityError("活动 ID 漂移")
    if runtime.candidate != contract.get("candidate"):
        raise SelectionActivityError("候选型号漂移")
    if state.get("activity_id") != runtime.activity_id:
        raise SelectionActivityError("state activity_id 漂移")
    if state.get("candidate") != runtime.candidate:
        raise SelectionActivityError("state candidate 漂移")

    corpus_path = repo_root / CORPUS_RELATIVE_PATH
    corpus = load_frozen_corpus(repo_root)
    if _sha256_file(corpus_path) != contract.get("corpus_sha256"):
        raise SelectionActivityError("冻结校准 corpus 漂移")
    if runtime.corpus_sha256 != contract.get("corpus_sha256"):
        raise SelectionActivityError("runtime corpus hash 漂移")
    if (
        _sha256_file(repo_root / WRITER_PROMPT_RELATIVE_PATH)
        != contract.get("writer_prompt_sha256")
    ):
        raise SelectionActivityError("Writer prompt 已改变，须新建活动而非混跑")
    if (
        _sha256_file(repo_root / STYLE_GUIDE_RELATIVE_PATH)
        != contract.get("style_guide_sha256")
    ):
        raise SelectionActivityError("校准 style guide 已改变，须新建活动")
    if _schema_hash(ChapterCard) != contract.get("chapter_card_schema_sha256"):
        raise SelectionActivityError("ChapterCard schema 漂移")
    if _schema_hash(WriterOutput) != contract.get("writer_output_schema_sha256"):
        raise SelectionActivityError("WriterOutput schema 漂移")

    requests = contract.get("requests")
    if not isinstance(requests, list) or len(requests) != CALIBRATION_CASES:
        raise SelectionActivityError("immutable contract 请求清单不完整")
    for request in requests:
        if not isinstance(request, dict):
            raise SelectionActivityError("请求清单条目不是对象")
        relative = request.get("request_path")
        if not isinstance(relative, str):
            raise SelectionActivityError("请求清单缺 request_path")
        request_path = root / relative
        if _sha256_file(request_path) != request.get("request_file_sha256"):
            raise SelectionActivityError(f"冻结 Writer request 漂移:{relative}")

    catalog = load_run_model_manifest_catalog(runtime_config)
    manifest = catalog.active_manifest
    if manifest is None or manifest.manifest_id != runtime.candidate:
        raise SelectionActivityError("临时配置未锁定本臂 exact model")
    selection = manifest.selection_test
    if (
        selection.max_physical_requests
        != runtime.phase_breakers.max_physical_requests
        or selection.max_configured_rmb
        != runtime.phase_breakers.max_configured_rmb
    ):
        raise SelectionActivityError("runtime 与 manifest breaker 漂移")
    if any(
        candidate != runtime.candidate
        and (
            candidate_manifest.selection_test.max_physical_requests != 0
            or candidate_manifest.selection_test.max_configured_rmb != 0
        )
        for candidate, candidate_manifest in catalog.manifests.items()
    ):
        raise SelectionActivityError("临时 config 只允许当前 exact candidate 取得正数 breaker")
    if manifest.fallback is not False:
        raise SelectionActivityError("selection activity 禁止 fallback")

    if runtime.phase == "calibration":
        product_data_entries = list(runtime.data_dir.iterdir())
        if product_data_entries:
            raise SelectionActivityError(
                "校准阶段 product-data 必须为空，禁止预建产品 workspace"
            )
    else:
        if runtime.immutable_contract_sha256 != contract_sha:
            raise SelectionActivityError("product runtime contract hash 漂移")
        result_path = root / "evidence/model-calibration-result.json"
        if _sha256_file(result_path) != runtime.calibration_result_sha256:
            raise SelectionActivityError("product runtime calibration result 漂移")
        result = _load_json_object(result_path)
        result_calibrations = result.get("model_calibration")
        config_calibrations = raw_config.get("model_calibration")
        bridge = result.get("budget_bridge")
        product_breaker = (
            bridge.get("product_breaker") if isinstance(bridge, dict) else None
        )
        if (
            not isinstance(result_calibrations, dict)
            or set(result_calibrations) != {runtime.candidate}
            or not isinstance(config_calibrations, dict)
            or config_calibrations.get(runtime.candidate)
            != result_calibrations.get(runtime.candidate)
        ):
            raise SelectionActivityError(
                "product runtime exact-model calibration 与冻结结果不一致"
            )
        if not isinstance(product_breaker, dict):
            raise SelectionActivityError("product runtime breaker 与预算桥不一致")
        try:
            bridge_physical = int(product_breaker["physical"])
            bridge_rmb = Decimal(str(product_breaker["configured_rmb"]))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise SelectionActivityError(
                "product runtime breaker 与预算桥不一致"
            ) from exc
        if (
            runtime.phase_breakers.max_physical_requests != bridge_physical
            or runtime.phase_breakers.max_configured_rmb != bridge_rmb
        ):
            raise SelectionActivityError("product runtime breaker 与预算桥不一致")

    return _PreparedActivity(
        root=root,
        runtime_config=runtime_config,
        runtime=runtime,
        state_path=state_path,
        state=state,
        contract_path=contract_path,
        contract=contract,
        corpus=corpus,
    )


def _save_activity_state(activity: _PreparedActivity, state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(UTC).isoformat()
    _atomic_write_json(activity.state_path, state)


def _calibration_session_factory(
    activity: _PreparedActivity,
) -> tuple[Engine, Callable[[str], Session]]:
    return _initialize_attempt_ledger(
        activity.root / "calibration-attempt-ledger.sqlite3"
    )


def _provider_rows(
    activity: _PreparedActivity,
    *,
    logical_call_id: str | None = None,
) -> list[AttemptLedgerORM]:
    engine, factory = _calibration_session_factory(activity)
    session = factory(activity.runtime.calibration_workspace_id)
    try:
        stmt = (
            select(AttemptLedgerORM)
            .where(
                AttemptLedgerORM.workspace_id
                == activity.runtime.calibration_workspace_id
            )
            .where(AttemptLedgerORM.attempt_kind == "provider")
            .order_by(AttemptLedgerORM.created_at, AttemptLedgerORM.attempt_id)
        )
        if logical_call_id is not None:
            stmt = stmt.where(
                AttemptLedgerORM.logical_call_id == logical_call_id
            )
        rows = list(session.scalars(stmt))
        for row in rows:
            session.expunge(row)
        return rows
    finally:
        session.close()
        engine.dispose()


def _request_by_case(
    activity: _PreparedActivity,
    case_id: str,
) -> dict[str, Any]:
    requests = cast(list[dict[str, Any]], activity.contract["requests"])
    matches = [row for row in requests if row.get("case_id") == case_id]
    if len(matches) != 1:
        raise SelectionActivityError(f"冻结请求未唯一命中:{case_id}")
    return matches[0]


def _response_is_valid(
    activity: _PreparedActivity,
    case_state: dict[str, Any],
    *,
    response_relative_override: str | None = None,
) -> bool:
    response_relative = response_relative_override or case_state.get("response_path")
    if not isinstance(response_relative, str):
        return False
    response_path = activity.root / response_relative
    if not response_path.is_file():
        return False
    response = _load_json_object(response_path)
    content = response.get("content")
    request = _request_by_case(activity, str(case_state.get("case_id")))
    return bool(
        response.get("schema_version") == RESULT_SCHEMA
        and response.get("activity_id") == activity.runtime.activity_id
        and response.get("case_id") == case_state.get("case_id")
        and response.get("candidate") == activity.runtime.candidate
        and response.get("model_used") == activity.runtime.candidate
        and response.get("logical_call_id") == case_state.get("logical_call_id")
        and response.get("prompt_sha256") == request.get("prompt_sha256")
        and isinstance(content, str)
        and content
        and response.get("content_sha256")
        == _sha256_bytes(content.encode("utf-8"))
    )


def _recover_case_state(
    activity: _PreparedActivity,
    state: dict[str, Any],
    case_state: dict[str, Any],
    *,
    resume_known_failure: str | None,
) -> Literal["skip", "run"]:
    status = case_state.get("status")
    logical_call_id = case_state.get("logical_call_id")
    rows = (
        _provider_rows(activity, logical_call_id=logical_call_id)
        if isinstance(logical_call_id, str)
        else []
    )
    if any(row.status == "started" for row in rows):
        case_state["status"] = "blocked_unknown"
        case_state["error"] = "started provider attempt has unknown outcome"
        state["status"] = "blocked_unknown"
        _save_activity_state(activity, state)
        raise SelectionActivityError(
            f"{case_state['case_id']} 存在 started/unknown Provider attempt；"
            "保存现场且禁止重放"
        )
    if status == "succeeded":
        if not rows or not any(row.status == "succeeded" for row in rows):
            raise SelectionActivityError(
                f"成功 case 缺少 succeeded Provider ledger:{case_state['case_id']}"
            )
        if not _response_is_valid(activity, case_state):
            raise SelectionActivityError(
                f"成功 case 的响应缺失或漂移:{case_state['case_id']}"
            )
        return "skip"
    if any(row.status == "succeeded" for row in rows):
        deterministic_response = (
            f"calibration-responses/{case_state['case_id']}.json"
        )
        if _response_is_valid(
            activity,
            case_state,
            response_relative_override=deterministic_response,
        ):
            case_state["response_path"] = deterministic_response
        if _response_is_valid(activity, case_state):
            case_state["status"] = "succeeded"
            case_state["error"] = None
            _save_activity_state(activity, state)
            return "skip"
        case_state["status"] = "blocked_response_lost"
        case_state["error"] = "provider succeeded but response archive is absent"
        state["status"] = "blocked_response_lost"
        _save_activity_state(activity, state)
        raise SelectionActivityError(
            f"{case_state['case_id']} 已付费成功但响应归档缺失；禁止猜测或重放"
        )
    if rows:
        case_state["status"] = "failed_known"
        if resume_known_failure != case_state["case_id"]:
            _save_activity_state(activity, state)
            raise SelectionActivityError(
                "已知失败可在同臂同边界显式恢复；请精确确认 case: "
                f"{case_state['case_id']}"
            )
        return "run"
    # Provider 前本地失败或进程在 begin 前退出，没有物理请求，可安全从本 case 重试。
    if status == "failed_known":
        raise SelectionActivityError(
            f"failed_known case 的 Provider ledger 缺失:{case_state['case_id']}"
        )
    if status in {"running", "failed_local", "pending"}:
        return "run"
    raise SelectionActivityError(
        f"不可识别的 calibration case 状态:{case_state['case_id']}={status!r}"
    )


def run_calibration(
    *,
    activity_root: Path,
    resume_known_failure: str | None = None,
    repo: Path | None = None,
    client_factory: Callable[..., LLMClient] | None = None,
) -> dict[str, Any]:
    """顺序执行 12 个真实 Writer 样本；本函数是任务3—5的显式 Provider 门。"""

    repo_root = (repo or _repo_root()).resolve()
    activity = _load_prepared_activity(activity_root, repo=repo_root)
    if activity.runtime.phase != "calibration":
        raise SelectionActivityError("活动已完成校准，不得追加 Provider 请求")
    if activity.state.get("status") in {
        "blocked_unknown",
        "blocked_response_lost",
        "arm_exhausted",
    }:
        raise SelectionActivityError(
            f"活动处于不可自动恢复状态:{activity.state.get('status')}"
        )
    starting_physical_attempts = len(_provider_rows(activity))

    writer_config = load_writer_profiles_config(activity.runtime_config)
    router_kwargs: dict[str, Any] = {}
    if client_factory is not None:
        router_kwargs["client_factory"] = client_factory
    manifest_router = build_model_router(
        writer_config,
        config_path=activity.runtime_config,
        **router_kwargs,
    )
    if not isinstance(manifest_router, ManifestModelRouter):
        raise SelectionActivityError("校准必须由 active exact manifest router 执行")
    binding = manifest_router.run_model_manifest_binding
    if binding.manifest_id != activity.runtime.candidate:
        raise SelectionActivityError("router exact model 与活动候选不一致")

    engine, session_factory = _calibration_session_factory(activity)
    store = AttemptLedgerStore(session_factory)
    paths = WorkspacePaths(
        activity.corpus.meta.workspace_id,
        workspaces_root=activity.root / "calibration-inputs",
    )
    setting_contract = SettingContract.from_world_output(activity.corpus.world)
    cards_by_id = {case.case_id: case.card for case in activity.corpus.cases}
    state = activity.state
    case_states = state.get("calibration_cases")
    if not isinstance(case_states, list) or len(case_states) != CALIBRATION_CASES:
        engine.dispose()
        raise SelectionActivityError("calibration state case 清单不完整")

    try:
        with bind_attempt_scope(
            workspace_id=activity.runtime.calibration_workspace_id,
            task_id=f"{activity.runtime.activity_id}:calibration",
            provider_call_cap=activity.runtime.phase_breakers.max_physical_requests,
            activity_scope="selection_test",
            run_model_manifest_token=binding.token,
            selection_test_manifest_token=binding.selection_test.token,
            configured_cost_currency=binding.selection_test.cost_currency,
            configured_cost_cap=activity.runtime.phase_breakers.max_configured_rmb,
        ) as scope:
            for case_state in case_states:
                if not isinstance(case_state, dict):
                    raise SelectionActivityError("calibration state case 不是对象")
                decision = _recover_case_state(
                    activity,
                    state,
                    case_state,
                    resume_known_failure=resume_known_failure,
                )
                if decision == "skip":
                    continue
                case_id = str(case_state["case_id"])
                card = cards_by_id.get(case_id)
                if card is None:
                    raise SelectionActivityError(f"corpus 未命中 case:{case_id}")
                request = _request_by_case(activity, case_id)
                round_number = int(case_state.get("round") or 0) + 1
                logical_call_id = (
                    f"selection-calibration:{activity.runtime.candidate}:"
                    f"{case_id}:round-{round_number}"
                )
                case_state.update(
                    {
                        "status": "running",
                        "round": round_number,
                        "logical_call_id": logical_call_id,
                        "response_path": None,
                        "error": None,
                    }
                )
                state["status"] = "calibration_running"
                _save_activity_state(activity, state)
                try:
                    target = manifest_router.route("writer", chapter_card=card)
                    tracked = AttemptTrackingClient(
                        inner=target.client,
                        store=store,
                        scope=scope,
                        agent_name="writer",
                        provider=target.profile.provider,
                        model=target.profile.model,
                        operation_kind="model_calibration",
                        profile=target.profile,
                        logical_call_id=logical_call_id,
                    )
                    locked = _PromptHashLockedClient(
                        inner=tracked,
                        expected_sha256=str(request["prompt_sha256"]),
                    )
                    output = write_chapter(
                        chapter_card=card,
                        memory_section="",
                        setting_contract=setting_contract,
                        router=cast(
                            Any,
                            _SingleTargetRouter(
                                RoutedTarget(
                                    profile=target.profile,
                                    client=locked,
                                    complete_kwargs=target.complete_kwargs,
                                )
                            ),
                        ),
                        prompts_dir=repo_root / "prompts",
                        vector_store=None,
                        db_session=None,
                        workspace_paths=paths,
                        workspace_id=activity.corpus.meta.workspace_id,
                        creation_type="draft",
                    )
                    if output.model_used != activity.runtime.candidate:
                        raise SelectionActivityError(
                            f"Provider response model 漂移:{output.model_used!r}"
                        )
                    response_relative = (
                        f"calibration-responses/{case_id}.json"
                    )
                    response_path = activity.root / response_relative
                    _atomic_write_json(
                        response_path,
                        {
                            "schema_version": RESULT_SCHEMA,
                            "activity_id": activity.runtime.activity_id,
                            "candidate": activity.runtime.candidate,
                            "case_id": case_id,
                            "genre_archetype": request["genre_archetype"],
                            "logical_call_id": logical_call_id,
                            "prompt_sha256": request["prompt_sha256"],
                            "model_used": output.model_used,
                            "content": output.content,
                            "content_sha256": _sha256_bytes(
                                output.content.encode("utf-8")
                            ),
                            "usage": output.token_usage.model_dump(mode="json"),
                            "finish_reason": output.finish_reason,
                            "reasoning_content_persisted": False,
                            "completed_at": datetime.now(UTC).isoformat(),
                        },
                    )
                    case_state.update(
                        {
                            "status": "succeeded",
                            "response_path": response_relative,
                            "error": None,
                        }
                    )
                    _save_activity_state(activity, state)
                except BaseException as exc:
                    rows = _provider_rows(
                        activity,
                        logical_call_id=logical_call_id,
                    )
                    if any(row.status == "started" for row in rows):
                        status = "blocked_unknown"
                        state["status"] = status
                    elif rows:
                        status = "failed_known"
                        state["status"] = "calibration_paused"
                    else:
                        status = "failed_local"
                        state["status"] = "calibration_paused"
                    case_state["status"] = status
                    case_state["error"] = _safe_error_text(exc)
                    _save_activity_state(activity, state)
                    raise
    finally:
        engine.dispose()

    state["status"] = "calibration_completed"
    _save_activity_state(activity, state)
    rows = _provider_rows(activity)
    return {
        "activity_id": activity.runtime.activity_id,
        "candidate": activity.runtime.candidate,
        "status": "calibration_completed",
        "writer_cases_succeeded": CALIBRATION_CASES,
        "physical_attempts_after_smoke": len(rows),
        "physical_attempts_this_invocation": (
            len(rows) - starting_physical_attempts
        ),
        "next": "measure-and-finalize",
    }


_LENGTH_GATE = 5000
_RE_SELF_QA = (
    re.compile(
        r"[？?][^。！？\n]{0,24}?"
        r"(也许|或许|大概|兴许|又或者|还是因为|会不会|是不是因为)"
    ),
    re.compile(r"(在想|想的是|要的是|怕的是|问的是)[^。！？\n]{0,8}?(什么|谁|哪)[？?]"),
)
_RE_EMOTION_STAMP = (
    re.compile(
        r"一种[^，。！？、\"\n]{0,16}?的?[^，。！？、\"\n]{0,6}"
        r"(空|空虚|空洞|疲惫|困惑|孤独|失落|释然|怅惘|惆怅|情绪|感觉|滋味|"
        r"茫然|疏离|安宁|酸楚|悸动|温柔|陌生)"
    ),
    re.compile(r"无处安放"),
    re.compile(r"(涌起|升起|漫起|生出|涌上|袭来)[^，。！？、\n]{0,6}一种"),
)
_PARALLEL_MARKERS = ("不需要", "没有", "不是", "不想", "不会")
_SENTENCE_SPLIT = re.compile(r"[。！？\n]")


def _anti_stamp_counts(text: str) -> dict[str, int]:
    parallel = sum(
        any(sentence.count(marker) >= 3 for marker in _PARALLEL_MARKERS)
        for sentence in _SENTENCE_SPLIT.split(text)
    )
    return {
        "self_qa": sum(len(pattern.findall(text)) for pattern in _RE_SELF_QA),
        "emotion_stamp": sum(
            len(pattern.findall(text)) for pattern in _RE_EMOTION_STAMP
        ),
        "parallel": parallel,
    }


def _length_tendency(rows: list[dict[str, Any]]) -> str:
    over_rate = sum(int(row["chars"]) > _LENGTH_GATE for row in rows) / len(rows)
    if over_rate >= 0.33:
        return "overshoot"
    if over_rate <= 0.05:
        return "undershoot"
    return "balanced"


def _anti_stamp_strength(rows: list[dict[str, Any]]) -> tuple[str, float]:
    densities = [
        sum(cast(dict[str, int], row["anti_stamp"]).values())
        / (int(row["chars"]) / 1000)
        for row in rows
        if int(row["chars"]) > 0
    ]
    peak = max(densities, default=0.0)
    if peak >= 0.4:
        return "high", peak
    if peak >= 0.15:
        return "medium", peak
    return "low", peak


def _calibration_temperature() -> float:
    return STYLE_GENERATION_DEFAULTS[PRODUCT_STYLE_TEMPLATE].writer_temperature


def _build_calibration_result(activity: _PreparedActivity) -> dict[str, Any]:
    if activity.state.get("status") != "calibration_completed":
        raise SelectionActivityError("只有 12/12 completed 校准可离线测量")
    case_states = cast(list[dict[str, Any]], activity.state["calibration_cases"])
    rows: list[dict[str, Any]] = []
    for case_state in case_states:
        if case_state.get("status") != "succeeded" or not _response_is_valid(
            activity, case_state
        ):
            raise SelectionActivityError(
                f"校准响应未达终态:{case_state.get('case_id')}"
            )
        response = _load_json_object(activity.root / str(case_state["response_path"]))
        logical_call_id = case_state.get("logical_call_id")
        if not isinstance(logical_call_id, str):
            raise SelectionActivityError(
                f"校准成功 case 缺 logical_call_id:{case_state.get('case_id')}"
            )
        ledger_rows = _provider_rows(
            activity,
            logical_call_id=logical_call_id,
        )
        if (
            not ledger_rows
            or any(row.status == "started" for row in ledger_rows)
            or sum(row.status == "succeeded" for row in ledger_rows) != 1
        ):
            raise SelectionActivityError(
                f"校准 response 与 Provider ledger 终态不一致:{case_state.get('case_id')}"
            )
        content = cast(str, response["content"])
        request = _request_by_case(activity, str(case_state["case_id"]))
        rows.append(
            {
                "case_id": case_state["case_id"],
                "genre_archetype": request["genre_archetype"],
                "chars": len(content),
                "word_count_min": int(request["word_count_min"]),
                "word_count_max": int(request["word_count_max"]),
                "plot_node_budget": int(request["plot_node_budget"]),
                "compliant_5000": len(content) <= _LENGTH_GATE,
                "compliant_card_max": len(content)
                <= int(request["word_count_max"]),
                "anti_stamp": _anti_stamp_counts(content),
                "content_sha256": response["content_sha256"],
            }
        )

    per_genre: dict[str, dict[str, Any]] = {}
    for genre in ("introspection", "action"):
        selected = [row for row in rows if row["genre_archetype"] == genre]
        if len(selected) != 6:
            raise SelectionActivityError(f"{genre} 校准样本必须恰为 6")
        chars = [int(row["chars"]) for row in selected]
        units = [int(row["plot_node_budget"]) for row in selected]
        mean_chars = statistics.mean(chars)
        mean_units = statistics.mean(units)
        per_genre[genre] = {
            "chars_per_beat": round(mean_chars / mean_units),
            "chars_per_chapter": round(mean_chars),
            "length_compliance": round(
                sum(bool(row["compliant_5000"]) for row in selected)
                / len(selected),
                3,
            ),
            "content_units_per_chapter": round(mean_units, 1),
            "sample_chapters": len(selected),
        }

    anti_strength, anti_peak = _anti_stamp_strength(rows)
    calibration = ModelCalibration.model_validate(
        {
            "calibrated_at": datetime.now(UTC).date().isoformat(),
            "calibration_corpus": activity.corpus.corpus_id,
            "global": {
                "length_tendency": _length_tendency(rows),
                "anti_stamp_strength": anti_strength,
                "recommended_temperature": _calibration_temperature(),
                "thinking_default": True,
                "reviewer_threshold_adjustment": 0.0,
            },
            "per_genre": per_genre,
        }
    )
    ledger_rows = _provider_rows(activity)
    return {
        "schema_version": RESULT_SCHEMA,
        "activity_id": activity.runtime.activity_id,
        "candidate": activity.runtime.candidate,
        "measurement_only": True,
        "reviewer_called": False,
        "literary_acceptance": False,
        "model_calibration": {
            activity.runtime.candidate: calibration.model_dump(
                mode="json", by_alias=True
            )
        },
        "metric_contract": {
            "chars_per_chapter": "6-case arithmetic mean of Python len(content)",
            "chars_per_beat": (
                "chars_per_chapter divided by mean frozen plot_node_budget; "
                "beat means chapter-level content unit, not retired batch cards"
            ),
            "length_compliance": "share of six cases with len(content) <= 5000",
            "length_tendency": (
                "overshoot if >5000 share >=0.33; undershoot if <=0.05; "
                "otherwise balanced"
            ),
            "anti_stamp_strength": (
                "documented regex peak density per 1000 chars: "
                "high>=0.4, medium>=0.15, else low"
            ),
            "recommended_temperature": (
                "actual frozen dark_mystery Writer style override (0.7), "
                "not a historical model value"
            ),
            "thinking_default": (
                "true because the exact manifest Writer route enables thinking"
            ),
            "reviewer_threshold_adjustment": (
                "neutral 0.0 because calibration calls no Reviewer; "
                "it is not a measured reviewer claim"
            ),
        },
        "anti_stamp_proxy_note": (
            "regex is a reproducible proxy, not literary acceptance; "
            f"peak_density={anti_peak:.3f}/1000 chars"
        ),
        "rows": rows,
        "ledger": {
            "physical_attempts": len(ledger_rows),
            "statuses": {
                status: sum(row.status == status for row in ledger_rows)
                for status in ("succeeded", "failed", "started")
            },
        },
    }


def _configured_cost_cny(rows: list[AttemptLedgerORM]) -> Decimal:
    total = Decimal("0")
    for row in rows:
        if row.configured_cost_currency != "CNY":
            raise SelectionActivityError("校准 attempt 费用币种不是 CNY")
        if row.configured_cost_amount is None:
            raise SelectionActivityError("校准 attempt 缺 configured cost")
        total += Decimal(str(row.configured_cost_amount))
    return total


def measure_and_finalize_activity(
    *,
    activity_root: Path,
    repo: Path | None = None,
) -> dict[str, Any]:
    """零 Provider 测量并把真实校准消耗扣成产品剩余 breaker。"""

    activity = _load_prepared_activity(activity_root, repo=repo)
    if activity.runtime.phase == "product_ready":
        # config 原子切到 product_ready 后、state 落盘前进程退出时，重跑只做
        # 一致性收口，不重复扣减，也不产生 Provider 请求。
        result_path = activity.root / "evidence/model-calibration-result.json"
        result = _load_json_object(result_path)
        bridge = result.get("budget_bridge")
        if (
            result.get("candidate") != activity.runtime.candidate
            or not isinstance(bridge, dict)
            or not isinstance(bridge.get("calibration_actual"), dict)
            or not isinstance(bridge.get("product_breaker"), dict)
        ):
            raise SelectionActivityError("product_ready 校准结果或预算桥损坏")
        rows = _provider_rows(activity)
        actual = cast(dict[str, Any], bridge["calibration_actual"])
        if (
            len(rows) != int(actual.get("physical", -1))
            or _configured_cost_cny(rows)
            != Decimal(str(actual.get("configured_rmb")))
            or any(row.status == "started" for row in rows)
        ):
            raise SelectionActivityError("product_ready 后校准账本发生漂移")
        state = activity.state
        state["status"] = "product_ready"
        state["calibration_result_sha256"] = _sha256_file(result_path)
        state["budget_bridge"] = bridge
        product = state.get("product")
        if not isinstance(product, dict):
            raise SelectionActivityError("activity product state 损坏")
        if product.get("status") == "not_started":
            product["status"] = "ready_for_ui_create"
        _save_activity_state(activity, state)
        return {
            "activity_id": activity.runtime.activity_id,
            "candidate": activity.runtime.candidate,
            "status": "product_ready",
            "provider_calls": 0,
            "calibration_result": str(result_path),
            "calibration_result_sha256": _sha256_file(result_path),
            "product_breaker": bridge["product_breaker"],
            "runtime_config": str(activity.runtime_config),
            "data_dir": str(activity.runtime.data_dir),
            "reconciled_existing_product_ready": True,
        }
    if activity.runtime.phase != "calibration":
        raise SelectionActivityError("不可识别的活动阶段")
    result = _build_calibration_result(activity)
    rows = _provider_rows(activity)
    if any(row.status == "started" for row in rows):
        raise SelectionActivityError("存在 started Provider attempt，禁止交接产品")
    if any(
        row.provider != "openai_compatible"
        or row.model != activity.runtime.candidate
        or row.operation_kind != "model_calibration"
        for row in rows
    ):
        raise SelectionActivityError("校准 ledger 出现跨 provider/model/operation 行")
    physical_used = len(rows)
    configured_used = _configured_cost_cny(rows)
    physical_remaining = (
        activity.runtime.phase_breakers.max_physical_requests - physical_used
    )
    configured_remaining = (
        activity.runtime.phase_breakers.max_configured_rmb - configured_used
    )
    result["budget_bridge"] = {
        "whole_arm": {
            "physical_cap": ARM_TOTAL_PHYSICAL_CAP,
            "rmb_cap": str(ARM_TOTAL_RMB_CAP),
        },
        "task1_fixed_debit": {
            "physical": SMOKE_PHYSICAL_DEBIT,
            "rmb": str(SMOKE_RMB_DEBITS[activity.runtime.candidate]),
            "unused_subbudget_reclaimed": False,
        },
        "carryover_debit": {
            "physical": activity.runtime.carryover_physical_debit,
            "configured_rmb": str(
                activity.runtime.carryover_configured_rmb_debit
            ),
            "reclaimed": False,
        },
        "calibration_actual": {
            "physical": physical_used,
            "configured_rmb": str(configured_used),
        },
        "product_breaker": {
            "physical": physical_remaining,
            "configured_rmb": str(configured_remaining),
        },
    }
    result_path = activity.root / "evidence/model-calibration-result.json"
    _atomic_write_json(result_path, result)
    result_sha = _sha256_file(result_path)

    state = activity.state
    if physical_remaining <= 0 or configured_remaining <= 0:
        state["status"] = "arm_exhausted"
        state["budget_bridge"] = result["budget_bridge"]
        _save_activity_state(activity, state)
        raise SelectionActivityBudgetExhausted(
            "校准已耗尽本臂后续固定边界；该臂未完成且不得换模型"
        )

    contract_sha = _sha256_file(activity.contract_path)
    breakers = ActivityBreakers(
        max_physical_requests=physical_remaining,
        max_configured_rmb=configured_remaining,
    )
    runtime_payload = _runtime_payload(
        candidate=activity.runtime.candidate,
        activity_root=activity.root,
        corpus_sha256=activity.runtime.corpus_sha256,
        breakers=breakers,
        carryover_physical_debit=activity.runtime.carryover_physical_debit,
        carryover_configured_rmb_debit=(
            activity.runtime.carryover_configured_rmb_debit
        ),
        phase="product_ready",
        immutable_contract_sha256=contract_sha,
        calibration_result_sha256=result_sha,
    )
    raw_config = _load_yaml_object(activity.runtime_config)
    section = cast(dict[str, Any], raw_config["run_model_manifest"])
    manifests = cast(dict[str, Any], section["manifests"])
    section["active"] = activity.runtime.candidate
    for candidate in CANDIDATE_ORDER:
        manifests[candidate]["selection_test"] = {
            "activity_scope": "selection_test",
            "max_physical_requests": (
                physical_remaining
                if candidate == activity.runtime.candidate
                else 0
            ),
            "max_configured_rmb": (
                str(configured_remaining)
                if candidate == activity.runtime.candidate
                else 0
            ),
        }
    calibrations = raw_config.setdefault("model_calibration", {})
    if not isinstance(calibrations, dict):
        raise SelectionActivityError("临时 config model_calibration 不是对象")
    if activity.runtime.candidate in calibrations:
        raise SelectionActivityError("临时 config 已存在候选校准，禁止覆盖")
    calibrations.update(cast(dict[str, Any], result["model_calibration"]))
    raw_config["selection_activity_runtime"] = runtime_payload
    _atomic_write_yaml(activity.runtime_config, raw_config)

    loaded = load_model_calibration(activity.runtime_config)
    if set(result["model_calibration"]) - set(loaded):
        raise SelectionActivityError("产出的 exact model calibration 未被 loader 读取")
    refreshed = _load_prepared_activity(activity.root, repo=repo)
    if refreshed.runtime.phase != "product_ready":
        raise SelectionActivityError("产品临时配置未进入 product_ready")
    state["status"] = "product_ready"
    state["calibration_result_sha256"] = result_sha
    state["budget_bridge"] = result["budget_bridge"]
    state["product"]["status"] = "ready_for_ui_create"
    _save_activity_state(refreshed, state)
    return {
        "activity_id": refreshed.runtime.activity_id,
        "candidate": refreshed.runtime.candidate,
        "status": "product_ready",
        "provider_calls": 0,
        "calibration_result": str(result_path),
        "calibration_result_sha256": result_sha,
        "product_breaker": result["budget_bridge"]["product_breaker"],
        "runtime_config": str(refreshed.runtime_config),
        "data_dir": str(refreshed.runtime.data_dir),
    }


def activity_status(
    *,
    activity_root: Path,
    repo: Path | None = None,
) -> dict[str, Any]:
    """Provider-0 状态快照；不自动修复、不读取凭据。"""

    activity = _load_prepared_activity(activity_root, repo=repo)
    rows = _provider_rows(activity)
    case_states = cast(list[dict[str, Any]], activity.state["calibration_cases"])
    return {
        "activity_id": activity.runtime.activity_id,
        "candidate": activity.runtime.candidate,
        "phase": activity.runtime.phase,
        "status": activity.state.get("status"),
        "carryover_debit": {
            "physical": activity.runtime.carryover_physical_debit,
            "configured_rmb": str(
                activity.runtime.carryover_configured_rmb_debit
            ),
        },
        "breakers": activity.runtime.phase_breakers.model_dump(mode="json"),
        "calibration_cases": {
            status: sum(case.get("status") == status for case in case_states)
            for status in sorted({str(case.get("status")) for case in case_states})
        },
        "physical_attempts_after_smoke": len(rows),
        "started_unknown": sum(row.status == "started" for row in rows),
        "configured_rmb_after_smoke": str(_configured_cost_cny(rows)) if rows else "0",
        "product": activity.state.get("product"),
        "provider_calls_by_status_command": 0,
    }


def load_product_selection_activity(
    workspace_id: str | None = None,
    *,
    config_path: Path | None = None,
    data_dir: Path | None = None,
    repo: Path | None = None,
) -> LoadedProductSelectionActivity | None:
    """读取 product-ready 临时活动；环境变量缺失时普通 production 原样返回。"""

    root_text = os.environ.get(ACTIVITY_ENV, "").strip()
    resolved_config = Path(config_path or settings.config_yaml_path).resolve()
    resolved_data = Path(data_dir or settings.data_dir).resolve()
    raw_config = _load_yaml_object(resolved_config)
    has_runtime = "selection_activity_runtime" in raw_config
    if not root_text:
        if has_runtime:
            raise SelectionActivityError(
                f"临时 selection config 必须同时设置 {ACTIVITY_ENV}"
            )
        if workspace_id in _reserved_product_workspace_ids():
            raise SelectionActivityError(
                "selection 保留 workspace 必须由 exact 临时 activity config/token 启用"
            )
        return None
    root_input = Path(root_text)
    if not root_input.is_absolute():
        raise SelectionActivityError(f"{ACTIVITY_ENV} 必须是绝对路径")
    activity = _load_prepared_activity(root_input, repo=repo)
    runtime = activity.runtime
    if runtime.phase != "product_ready" or activity.state.get("status") != "product_ready":
        raise SelectionActivityError("活动尚未完成 exact-model calibration")
    if resolved_config != activity.runtime_config.resolve():
        raise SelectionActivityError("CONFIG_YAML_PATH 未指向活动临时 config")
    if resolved_data != runtime.data_dir.resolve():
        raise SelectionActivityError("DATA_DIR 未指向活动独立 product-data")
    if workspace_id is not None and workspace_id != runtime.product_workspace_id:
        raise SelectionActivityError(
            "selection 临时配置只允许唯一产品 workspace: "
            f"expected={runtime.product_workspace_id},actual={workspace_id}"
        )
    calibrations = load_model_calibration(resolved_config)
    if runtime.candidate not in calibrations:
        raise SelectionActivityError("临时配置缺 exact candidate calibration")
    config = load_writer_profiles_config(resolved_config)
    manifest_router = build_model_router(
        config,
        config_path=resolved_config,
        client_factory=lambda *_args, **_kwargs: _CaptureClient(),
    )
    if not isinstance(manifest_router, ManifestModelRouter):
        raise SelectionActivityError("product-ready 配置未启用 manifest router")
    binding = manifest_router.run_model_manifest_binding
    if binding.manifest_id != runtime.candidate:
        raise SelectionActivityError("product router exact model 漂移")
    if (
        binding.selection_test.max_physical_requests
        != runtime.phase_breakers.max_physical_requests
        or binding.selection_test.max_configured_rmb
        != runtime.phase_breakers.max_configured_rmb
    ):
        raise SelectionActivityError("product router breaker 漂移")
    return LoadedProductSelectionActivity(
        runtime=runtime,
        config_path=resolved_config,
        activity_root=activity.root,
        manifest_token=binding.token,
        selection_token=binding.selection_test.token,
    )


def selection_attempt_scope_kwargs(
    workspace_id: str,
    *,
    run_id: str | None = None,
    task_id: str | None = None,
    chapter_id: str | None = None,
    provider_call_cap: int = DEFAULT_PROVIDER_CALL_HARD_CAP,
    provider_call_cap_enforced: bool = True,
    synchronous_memory_enrichment: bool = False,
) -> dict[str, Any]:
    """给正常入口返回 production 或严格 product selection scope 参数。"""

    loaded = load_product_selection_activity(workspace_id)
    if loaded is None:
        return {
            "workspace_id": workspace_id,
            "run_id": run_id,
            "task_id": task_id,
            "chapter_id": chapter_id,
            "provider_call_cap": provider_call_cap,
            "provider_call_cap_enforced": provider_call_cap_enforced,
            "synchronous_memory_enrichment": synchronous_memory_enrichment,
        }
    runtime = loaded.runtime
    return {
        "workspace_id": workspace_id,
        "run_id": run_id,
        "task_id": task_id or f"{runtime.activity_id}:product",
        "chapter_id": chapter_id,
        "provider_call_cap": runtime.phase_breakers.max_physical_requests,
        "provider_call_cap_enforced": True,
        "activity_scope": "selection_test",
        "run_model_manifest_token": loaded.manifest_token,
        "selection_test_manifest_token": loaded.selection_token,
        "configured_cost_currency": "CNY",
        "configured_cost_cap": runtime.phase_breakers.max_configured_rmb,
        "synchronous_memory_enrichment": synchronous_memory_enrichment,
    }


@contextmanager
def bind_selection_or_production_scope(
    workspace_id: str,
    **kwargs: Any,
) -> Iterator[AttemptScope]:
    """既有 UI/service 入口共用的无 API/schema 扩张 context manager。"""

    with bind_attempt_scope(
        **selection_attempt_scope_kwargs(workspace_id, **kwargs)
    ) as scope:
        yield scope


def validate_product_workspace_create(
    *,
    workspace_id: str,
    display_name: str,
    genre: str,
    premise: str,
    total_chapters: int,
    default_voice: str,
    style_template: str | None,
) -> None:
    """选择活动 UI 创建前精确锁输入；普通 production 为 no-op。"""

    loaded = load_product_selection_activity(workspace_id)
    if loaded is None:
        return
    expected = loaded.runtime.product_input
    actual = {
        "workspace_id": workspace_id,
        "display_name": display_name,
        "genre": genre,
        "premise": premise,
        "total_chapters": total_chapters,
        "default_voice": default_voice,
        "style_template": style_template,
    }
    wanted = {
        "workspace_id": loaded.runtime.product_workspace_id,
        "display_name": product_display_name(loaded.runtime.candidate),
        "genre": expected.genre,
        "premise": expected.premise,
        "total_chapters": (
            expected.total_volumes * expected.chapters_per_volume
        ),
        "default_voice": expected.default_voice,
        "style_template": expected.style_template,
    }
    if actual != wanted:
        raise SelectionActivityError(
            "selection product workspace 输入漂移；必须从冻结 New Book 表单创建"
        )


def validate_product_outline_input(
    *,
    workspace_id: str,
    premise: str,
    total_volumes: int,
    chapters_per_volume: int,
    tension_template: str,
    genre: str,
) -> bool:
    """返回是否启用内部三章固定合同；普通 production 返回 False。"""

    loaded = load_product_selection_activity(workspace_id)
    if loaded is None:
        return False
    expected = loaded.runtime.product_input
    actual = (
        premise,
        total_volumes,
        chapters_per_volume,
        tension_template,
        genre,
    )
    wanted = (
        expected.premise,
        expected.total_volumes,
        expected.chapters_per_volume,
        expected.tension_template,
        expected.genre,
    )
    if actual != wanted:
        raise SelectionActivityError("selection outline 输入与冻结三章合同不一致")
    return True


def assert_selection_assets_forbidden(workspace_id: str) -> None:
    """selection product 禁止导入 world/characters 或读取真实 workspace 资产。"""

    if load_product_selection_activity(workspace_id) is not None:
        raise SelectionActivityError(
            "selection product imported_assets=false；禁止 assets:import"
        )


def record_product_checkpoint(
    *,
    workspace_id: str,
    run_id: str,
) -> None:
    """Pipeline start 后把 run_id 写入活动 state，供 task3—5 恢复核验。"""

    loaded = load_product_selection_activity(workspace_id)
    if loaded is None:
        return
    activity = _load_prepared_activity(loaded.activity_root)
    state = activity.state
    product = state.get("product")
    if not isinstance(product, dict):
        raise SelectionActivityError("activity product state 损坏")
    existing = product.get("run_id")
    if existing not in (None, run_id):
        raise SelectionActivityError(
            f"selection activity 已绑定另一个 run:{existing}"
        )
    product["run_id"] = run_id
    product["status"] = "product_running"
    _save_activity_state(activity, state)
