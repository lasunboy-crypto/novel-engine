"""富记忆 checkpoint 的本地结构校验。

该模块不读写数据库、不构造 Router、不调用 Provider。调用方在恢复、续跑或
启动收口前先用同一份规则区分“可解释的持久化状态”和“必须原样保留的损坏
现场”，避免各入口各自容错后把未知状态洗成看似正常的数据。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from src.services.memory_enrichment_attempts import RICH_MEMORY_PROVIDER_AGENTS


VALID_MEMORY_ENRICHMENT_STATUSES = frozenset(
    {"pending", "running", "succeeded", "failed", "skipped"}
)
MEMORY_ENRICHMENT_SCHEMA_VERSION = 1
_SQLITE_FOOTPRINT_FIELDS = frozenset(
    {
        "character_first_appearance",
        "character_last_updated",
        "character_state_timeline",
        "plot_planted",
        "plot_last_updated",
        "timeline_entries",
        "scene_anchor",
    }
)


class MemoryEnrichmentCheckpointShapeError(ValueError):
    """富记忆状态或详情不满足持久化合同。"""


def memory_enrichment_requires_manual_recovery(
    *, status: object, details: object
) -> bool:
    """判断失败 checkpoint 是否仍含不能自动放行的未知副作用。

    普通富记忆失败仍可沿用 ``final + continuity ledger`` 降级；但冷启动发现
    上一进程死在任务中途时，只有显式证明 SQLite 零副作用、Chroma 零本章向量、
    来源可验且 unknown Provider 请求绝不重放的记录才能放行。旧版无此证明的
    ``result_unknown`` 以及明确标记
    ``quarantine_required`` 的记录都必须保留现场并硬拦。
    """

    if status != "failed":
        return False
    if not isinstance(details, dict):
        return True
    if "startup_recovery" in details:
        return not _has_verified_safe_startup_recovery(details)

    outcome = details.get("memory_extraction_outcome")
    if isinstance(outcome, dict) and (
        (
            outcome.get("succeeded") is False
            and outcome.get("vector_write_started") is True
        )
        or outcome.get("sqlite_rollback_succeeded") is False
        or outcome.get("result_unknown") is True
    ):
        return True
    summary_outcome = details.get("chapter_summary_vector_outcome")
    if isinstance(summary_outcome, dict) and (
        (
            summary_outcome.get("succeeded") is False
            and summary_outcome.get("vector_write_started") is True
        )
        or summary_outcome.get("result_unknown") is True
    ):
        return True
    if details.get("result_unknown") is True:
        return True
    if details.get("manual_recovery_required") is True:
        return True
    if details.get("safe_to_continue") is False:
        return True
    if details.get("failure_disposition") == "blocked_unknown_side_effects":
        return True
    failures = details.get("failures")
    if isinstance(failures, list) and any(
        isinstance(failure, dict) and failure.get("result_unknown") is True
        for failure in failures
    ):
        return True
    # 历史普通失败没有上述结构化 unknown 证据；它们沿用 final + ledger
    # 的既有降级语义，不能因缺少后来新增的字段反推成永久硬停。
    return False


def memory_enrichment_retryable_after_explicit_continue(
    *, status: object, details: object, enriched_at: object
) -> bool:
    """只放行用户显式 Continue 后的已知 memory-extractor 失败。

    这是一个比 ``safe_to_continue`` 更窄的重试资格：只接受当前进程已经落下
    完整终态、SQLite 回滚确认、Chroma 尚未开始写、摘要向量已经成功，且没有
    其他失败阶段的 runtime checkpoint。冷启动收口、未知副作用、混合失败和
    损坏形状都不允许借此重放 Provider。
    """

    try:
        validate_memory_enrichment_checkpoint_shape(
            status=status,
            details=details,
            enriched_at=enriched_at,
        )
    except MemoryEnrichmentCheckpointShapeError:
        return False
    if status != "failed" or not isinstance(details, dict):
        return False
    if "startup_recovery" in details:
        return False
    if (
        details.get("result_unknown") is not False
        or details.get("manual_recovery_required") is not False
        or details.get("safe_to_continue") is not True
        or details.get("failure_disposition") != "known_failure_degraded"
    ):
        return False

    outcome = details.get("memory_extraction_outcome")
    if not isinstance(outcome, dict) or (
        outcome.get("succeeded") is not False
        or outcome.get("failure_stage") not in {"extract", "read_final"}
        or outcome.get("vector_write_started") is not False
        or outcome.get("sqlite_rollback_succeeded") is not True
        or outcome.get("result_unknown") is not False
    ):
        return False

    summary_outcome = details.get("chapter_summary_vector_outcome")
    if not isinstance(summary_outcome, dict) or (
        summary_outcome.get("succeeded") is not True
        or summary_outcome.get("result_unknown") is not False
    ):
        return False

    tasks = details.get("tasks")
    if not isinstance(tasks, list) or not any(
        isinstance(task, dict)
        and task.get("name") == "memory_extractor"
        and task.get("status") == "failed"
        for task in tasks
    ):
        return False
    failures = details.get("failures")
    if not isinstance(failures, list) or not failures:
        return False
    return all(
        isinstance(failure, dict)
        and failure.get("stage") == "memory_extractor"
        and failure.get("result_unknown") is not True
        for failure in failures
    )


def _has_verified_safe_startup_recovery(details: dict[str, Any]) -> bool:
    """只认统一 validator 验真的“无新增副作用”证明。"""

    try:
        _validate_startup_recovery(details)
    except MemoryEnrichmentCheckpointShapeError:
        return False
    recovery = details["startup_recovery"]
    assert isinstance(recovery, dict)
    return recovery["safe_to_continue"] is True


def validate_memory_enrichment_checkpoint_shape(
    *,
    status: object,
    details: object,
    enriched_at: object,
) -> None:
    """验证富记忆状态与详情，不修补、不归一化输入。

    ``pending + None`` 是尚未调度的合法初态。其余终态必须保留结构化详情；
    ``running`` 还必须带 Worker 真正写入的 ``started_at`` 和 ``tasks``，这样
    冷启动扫描不会把随手伪造或已损坏的 ``running`` 误判成可安全收口的僵尸态。
    """

    if not isinstance(status, str) or status not in VALID_MEMORY_ENRICHMENT_STATUSES:
        raise MemoryEnrichmentCheckpointShapeError(
            f"未知 memory_enrichment_status={status!r}"
        )

    if status == "succeeded":
        if not isinstance(enriched_at, datetime):
            raise MemoryEnrichmentCheckpointShapeError(
                "status='succeeded' 时 memory_enriched_at 必须存在"
            )
    elif enriched_at is not None:
        raise MemoryEnrichmentCheckpointShapeError(
            f"status={status!r} 时 memory_enriched_at 必须为 null"
        )

    if details is None:
        if status == "pending":
            return
        raise MemoryEnrichmentCheckpointShapeError(
            f"status={status!r} 时 memory_enrichment_details 不能为 null"
        )
    if not isinstance(details, dict):
        raise MemoryEnrichmentCheckpointShapeError(
            "memory_enrichment_details 必须是 JSON object，"
            f"实际为 {type(details).__name__}"
        )

    _validate_optional_record_list(details, field="tasks")
    _validate_optional_record_list(details, field="failures")
    _validate_optional_record_list(details, field="retry_history")

    if status in {"running", "succeeded", "failed"} and "tasks" not in details:
        raise MemoryEnrichmentCheckpointShapeError(
            f"status={status!r} 时 memory_enrichment_details.tasks 必须存在"
        )
    if status == "failed" and "failures" not in details:
        raise MemoryEnrichmentCheckpointShapeError(
            "status='failed' 时 memory_enrichment_details.failures 必须存在"
        )
    if status == "running":
        started_at = details.get("started_at")
        if not isinstance(started_at, str) or not started_at.strip():
            raise MemoryEnrichmentCheckpointShapeError(
                "status='running' 时 memory_enrichment_details.started_at "
                "必须是非空字符串"
            )
        try:
            parsed_started_at = datetime.fromisoformat(started_at)
        except ValueError as exc:
            raise MemoryEnrichmentCheckpointShapeError(
                "status='running' 时 memory_enrichment_details.started_at "
                "必须是 ISO-8601 时间"
            ) from exc
        if (
            parsed_started_at.tzinfo is None
            or parsed_started_at.utcoffset() is None
        ):
            raise MemoryEnrichmentCheckpointShapeError(
                "status='running' 时 memory_enrichment_details.started_at "
                "必须带时区"
            )
        # R8 现场是旧版 exact legacy shape；只允许这一种窄形状进入冷启动
        # 审计。新 Worker 必须保留完整 frozen provenance 与 UUID lease。
        if details.get("schema_version") is None:
            if set(details) != {"started_at", "tasks"}:
                raise MemoryEnrichmentCheckpointShapeError(
                    "legacy running 只允许 exact {started_at,tasks} 形状"
                )
        else:
            _validate_provenance(details, status="running")
            lease_id = details.get("lease_id")
            if (
                not isinstance(lease_id, str)
                or re.fullmatch(r"[0-9a-f]{32}", lease_id) is None
            ):
                raise MemoryEnrichmentCheckpointShapeError(
                    "running.lease_id 必须是 32 位小写十六进制 UUID"
                )
            baseline = details.get("side_effect_baseline")
            if (
                not isinstance(baseline, dict)
                or set(baseline)
                != {
                    "schema_version",
                    "lease_id",
                    "sqlite_footprint",
                    "sqlite_digest",
                    "vector_count",
                    "vector_digest",
                }
                or baseline.get("schema_version") != 1
                or baseline.get("lease_id") != lease_id
            ):
                raise MemoryEnrichmentCheckpointShapeError(
                    "running.side_effect_baseline 形状或 lease 绑定损坏"
                )
            sqlite_footprint = baseline.get("sqlite_footprint")
            if (
                not isinstance(sqlite_footprint, dict)
                or set(sqlite_footprint) != _SQLITE_FOOTPRINT_FIELDS
                or any(
                    type(value) is not int or value < 0
                    for value in sqlite_footprint.values()
                )
                or not _is_sha256(baseline.get("sqlite_digest"))
                or type(baseline.get("vector_count")) is not int
                or baseline["vector_count"] < 0
                or not _is_sha256(baseline.get("vector_digest"))
            ):
                raise MemoryEnrichmentCheckpointShapeError(
                    "running.side_effect_baseline 内容损坏"
                )
    elif status in {"succeeded", "failed"} and details.get("schema_version") is not None:
        _validate_provenance(details, status=status)
    if status == "failed":
        _validate_failed_checkpoint_semantics(details)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_provenance(details: dict[str, Any], *, status: str) -> None:
    """验证新式 checkpoint 与终稿/ledger 绑定所需字段。"""

    if details.get("schema_version") != MEMORY_ENRICHMENT_SCHEMA_VERSION:
        raise MemoryEnrichmentCheckpointShapeError(
            f"{status}.schema_version 必须为 {MEMORY_ENRICHMENT_SCHEMA_VERSION}"
        )
    final_version_id = details.get("final_version_id")
    if type(final_version_id) is not int or final_version_id < 1:
        raise MemoryEnrichmentCheckpointShapeError(
            f"{status}.final_version_id 必须是正整数"
        )
    for field in ("ready_at", "final_path"):
        value = details.get(field)
        if not isinstance(value, str) or not value.strip():
            raise MemoryEnrichmentCheckpointShapeError(
                f"{status}.{field} 必须是非空字符串"
            )
    try:
        ready_at = datetime.fromisoformat(details["ready_at"])
    except ValueError as exc:
        raise MemoryEnrichmentCheckpointShapeError(
            f"{status}.ready_at 必须是 ISO-8601 时间"
        ) from exc
    if ready_at.tzinfo is None or ready_at.utcoffset() is None:
        raise MemoryEnrichmentCheckpointShapeError(
            f"{status}.ready_at 必须带时区"
        )
    for field in (
        "continuity_ledger_hash",
        "continuity_ledger_source_text_hash",
    ):
        if not _is_sha256(details.get(field)):
            raise MemoryEnrichmentCheckpointShapeError(
                f"{status}.{field} 必须是 64 位 SHA-256"
            )


def _validate_optional_record_list(details: dict[str, Any], *, field: str) -> None:
    """若字段存在，必须是由 JSON object 组成的 list。"""

    if field not in details:
        return
    value = details[field]
    if not isinstance(value, list):
        raise MemoryEnrichmentCheckpointShapeError(
            f"memory_enrichment_details.{field} 必须是 list，"
            f"实际为 {type(value).__name__}"
        )
    if any(not isinstance(item, dict) for item in value):
        raise MemoryEnrichmentCheckpointShapeError(
            f"memory_enrichment_details.{field} 的每一项都必须是 JSON object"
        )


def _validate_failed_checkpoint_semantics(details: dict[str, Any]) -> None:
    """验证新式失败分类，避免相互矛盾的标志被误放行。"""

    if "startup_recovery" in details:
        _validate_startup_recovery(details)
        return

    runtime_fields = {
        "memory_extraction_outcome",
        "chapter_summary_vector_outcome",
        "result_unknown",
        "manual_recovery_required",
        "safe_to_continue",
        "failure_disposition",
    }
    if runtime_fields.isdisjoint(details):
        return  # 旧版普通 fail-open 终态

    for field in (
        "result_unknown",
        "manual_recovery_required",
        "safe_to_continue",
    ):
        if type(details.get(field)) is not bool:
            raise MemoryEnrichmentCheckpointShapeError(
                f"failed.{field} 必须是 bool"
            )

    unknown_side_effects = False
    memory_outcome = details.get("memory_extraction_outcome")
    if memory_outcome is not None:
        unknown_side_effects = (
            _validate_memory_extraction_outcome(memory_outcome)
            or unknown_side_effects
        )
    summary_outcome = details.get("chapter_summary_vector_outcome")
    if summary_outcome is not None:
        unknown_side_effects = (
            _validate_summary_vector_outcome(summary_outcome)
            or unknown_side_effects
        )

    result_unknown = details["result_unknown"]
    if result_unknown is not unknown_side_effects:
        raise MemoryEnrichmentCheckpointShapeError(
            "failed.result_unknown 与 vector/rollback 事实不一致"
        )
    if unknown_side_effects:
        expected = (True, False, "blocked_unknown_side_effects")
    else:
        expected = (False, True, "known_failure_degraded")
    actual = (
        details["manual_recovery_required"],
        details["safe_to_continue"],
        details.get("failure_disposition"),
    )
    if actual != expected:
        raise MemoryEnrichmentCheckpointShapeError(
            "failed 的人工恢复/放行/处置标志与副作用事实不一致"
        )


def _validate_memory_extraction_outcome(outcome: object) -> bool:
    """校验 MemoryExtractionOutcome 的 JSON 投影并返回是否需硬停。"""

    if not isinstance(outcome, dict):
        raise MemoryEnrichmentCheckpointShapeError(
            "failed.memory_extraction_outcome 必须是 JSON object"
        )
    for field in (
        "succeeded",
        "vector_write_started",
        "sqlite_rollback_succeeded",
        "result_unknown",
    ):
        if type(outcome.get(field)) is not bool:
            raise MemoryEnrichmentCheckpointShapeError(
                f"memory_extraction_outcome.{field} 必须是 bool"
            )
    failure_stage = outcome.get("failure_stage")
    if failure_stage is not None and (
        not isinstance(failure_stage, str) or not failure_stage.strip()
    ):
        raise MemoryEnrichmentCheckpointShapeError(
            "memory_extraction_outcome.failure_stage 必须为 null 或非空字符串"
        )
    if outcome["succeeded"]:
        if failure_stage is not None or outcome["result_unknown"] is not False:
            raise MemoryEnrichmentCheckpointShapeError(
                "成功的 memory_extraction_outcome 不能带失败阶段或 unknown"
            )
    else:
        if failure_stage is None:
            raise MemoryEnrichmentCheckpointShapeError(
                "失败的 memory_extraction_outcome 必须带 failure_stage"
            )
        expected_unknown = (
            outcome["vector_write_started"]
            or not outcome["sqlite_rollback_succeeded"]
        )
        if outcome["result_unknown"] is not expected_unknown:
            raise MemoryEnrichmentCheckpointShapeError(
                "memory_extraction_outcome.result_unknown 与写入/回滚事实不一致"
            )
    return bool(
        outcome["result_unknown"]
        or not outcome["sqlite_rollback_succeeded"]
    )


def _validate_summary_vector_outcome(outcome: object) -> bool:
    """校验摘要向量写入结果并返回是否已经触碰向量库。"""

    if not isinstance(outcome, dict):
        raise MemoryEnrichmentCheckpointShapeError(
            "failed.chapter_summary_vector_outcome 必须是 JSON object"
        )
    for field in ("succeeded", "vector_write_started", "result_unknown"):
        if type(outcome.get(field)) is not bool:
            raise MemoryEnrichmentCheckpointShapeError(
                f"chapter_summary_vector_outcome.{field} 必须是 bool"
            )
    expected_unknown = (
        not outcome["succeeded"] and outcome["vector_write_started"]
    )
    if outcome["result_unknown"] is not expected_unknown:
        raise MemoryEnrichmentCheckpointShapeError(
            "chapter_summary_vector_outcome.result_unknown 与写入事实不一致"
        )
    return bool(outcome["result_unknown"])


def _validate_startup_recovery(details: dict[str, Any]) -> None:
    """冷启动审计记录只有严格安全或明确 blocked 两种合法结果。"""

    recovery = details.get("startup_recovery")
    if not isinstance(recovery, dict):
        raise MemoryEnrichmentCheckpointShapeError(
            "failed.startup_recovery 必须是 JSON object"
        )
    required = {
        "reason",
        "previous_status",
        "result_unknown",
        "automatic_provider_replay",
        "source_provenance_verified",
        "source_provenance_error",
        "sqlite_footprint",
        "sqlite_side_effect_count",
        "vector_count",
        "vector_quarantined_count",
        "vector_quarantine_succeeded",
        "vector_quarantine_error",
        "side_effects_verified_absent",
        "safe_to_continue",
        "quarantine_required",
        "recovery_disposition",
    }
    missing = sorted(required.difference(recovery))
    if missing:
        raise MemoryEnrichmentCheckpointShapeError(
            "failed.startup_recovery 缺少字段: " + ",".join(missing)
        )
    extended_fields = {
        "sqlite_baseline",
        "sqlite_baseline_digest",
        "sqlite_digest",
        "vector_baseline_count",
        "vector_baseline_digest",
        "vector_digest",
        "vector_side_effect_count",
    }
    present_extended_fields = extended_fields.intersection(recovery)
    if present_extended_fields and present_extended_fields != extended_fields:
        missing_extended = sorted(extended_fields.difference(recovery))
        raise MemoryEnrichmentCheckpointShapeError(
            "failed.startup_recovery baseline 扩展字段不完整: "
            + ",".join(missing_extended)
        )
    has_extended_audit = bool(present_extended_fields)
    footprint = recovery["sqlite_footprint"]
    if not isinstance(footprint, dict) or set(footprint) != _SQLITE_FOOTPRINT_FIELDS:
        raise MemoryEnrichmentCheckpointShapeError(
            "startup_recovery.sqlite_footprint 字段集合不完整"
        )
    if any(type(value) is not int or value < 0 for value in footprint.values()):
        raise MemoryEnrichmentCheckpointShapeError(
            "startup_recovery.sqlite_footprint 必须是非负整数"
        )
    sqlite_digest = recovery.get("sqlite_digest")
    sqlite_baseline = recovery.get("sqlite_baseline")
    vector_baseline = recovery.get("vector_baseline_count")
    vector_count = recovery["vector_count"]
    vector_digest = recovery.get("vector_digest")
    side_effect_baseline = details.get("side_effect_baseline")
    numeric_fields = ["vector_count", "vector_quarantined_count"]
    if has_extended_audit:
        numeric_fields.append("vector_side_effect_count")
    for field in numeric_fields:
        value = recovery.get(field)
        if value is not None and (type(value) is not int or value < 0):
            raise MemoryEnrichmentCheckpointShapeError(
                f"startup_recovery.{field} 必须为 null 或非负整数"
            )
    if (
        has_extended_audit
        and not _is_sha256(sqlite_digest)
    ):
        raise MemoryEnrichmentCheckpointShapeError(
            "startup_recovery.sqlite_digest 必须是 SHA256"
        )
    if vector_digest is not None and not _is_sha256(vector_digest):
        raise MemoryEnrichmentCheckpointShapeError(
            "startup_recovery.vector_digest 必须为 null 或 SHA256"
        )
    if not has_extended_audit:
        if side_effect_baseline is not None:
            raise MemoryEnrichmentCheckpointShapeError(
                "旧版 startup_recovery 不能带 side_effect_baseline"
            )
        expected_sqlite_side_effects = sum(footprint.values())
        expected_vector_side_effects = vector_count
    elif sqlite_baseline is None:
        if (
            side_effect_baseline is not None
            or recovery["sqlite_baseline_digest"] is not None
            or vector_baseline is not None
            or recovery["vector_baseline_digest"] is not None
        ):
            raise MemoryEnrichmentCheckpointShapeError(
                "legacy startup_recovery 不能混入部分 side-effect baseline"
            )
        expected_sqlite_side_effects = sum(footprint.values())
        expected_vector_side_effects = vector_count
    else:
        if (
            not isinstance(sqlite_baseline, dict)
            or set(sqlite_baseline) != _SQLITE_FOOTPRINT_FIELDS
            or any(
                type(value) is not int or value < 0
                for value in sqlite_baseline.values()
            )
        ):
            raise MemoryEnrichmentCheckpointShapeError(
                "startup_recovery.sqlite_baseline 必须是完整非负 footprint"
            )
        sqlite_baseline_digest = recovery["sqlite_baseline_digest"]
        vector_baseline_digest = recovery["vector_baseline_digest"]
        lease_id = details.get("lease_id")
        if (
            not isinstance(side_effect_baseline, dict)
            or set(side_effect_baseline)
            != {
                "schema_version",
                "lease_id",
                "sqlite_footprint",
                "sqlite_digest",
                "vector_count",
                "vector_digest",
            }
            or side_effect_baseline.get("schema_version") != 1
            or not isinstance(lease_id, str)
            or not lease_id
            or side_effect_baseline.get("lease_id") != lease_id
            or side_effect_baseline.get("sqlite_footprint") != sqlite_baseline
            or side_effect_baseline.get("sqlite_digest")
            != sqlite_baseline_digest
            or side_effect_baseline.get("vector_count") != vector_baseline
            or side_effect_baseline.get("vector_digest")
            != vector_baseline_digest
            or not _is_sha256(sqlite_baseline_digest)
            or type(vector_baseline) is not int
            or vector_baseline < 0
            or not _is_sha256(vector_baseline_digest)
        ):
            raise MemoryEnrichmentCheckpointShapeError(
                "startup_recovery.side_effect_baseline 形状或 lease 绑定损坏"
            )
        expected_sqlite_side_effects = sum(
            abs(footprint[field] - sqlite_baseline[field])
            for field in footprint
        ) + int(sqlite_digest != sqlite_baseline_digest)
        if vector_count is None:
            expected_vector_side_effects = None
        else:
            expected_vector_side_effects = abs(
                vector_count - vector_baseline
            ) + int(vector_digest != vector_baseline_digest)
    if recovery["sqlite_side_effect_count"] != expected_sqlite_side_effects:
        raise MemoryEnrichmentCheckpointShapeError(
            "startup_recovery.sqlite_side_effect_count 与 baseline 差值不一致"
        )
    if (
        has_extended_audit
        and recovery["vector_side_effect_count"] != expected_vector_side_effects
    ):
        raise MemoryEnrichmentCheckpointShapeError(
            "startup_recovery.vector_side_effect_count 与 baseline 差值不一致"
        )
    if recovery.get("reason") != "engine_restarted_mid_memory_enrichment":
        raise MemoryEnrichmentCheckpointShapeError(
            "startup_recovery.reason 不合法"
        )
    if recovery.get("previous_status") != "running":
        raise MemoryEnrichmentCheckpointShapeError(
            "startup_recovery.previous_status 必须为 running"
        )
    if recovery.get("result_unknown") is not True:
        raise MemoryEnrichmentCheckpointShapeError(
            "startup_recovery.result_unknown 必须为 true"
        )
    if recovery.get("automatic_provider_replay") is not False:
        raise MemoryEnrichmentCheckpointShapeError(
            "startup_recovery.automatic_provider_replay 必须为 false"
        )
    for field in (
        "source_provenance_verified",
        "vector_quarantine_succeeded",
        "side_effects_verified_absent",
        "safe_to_continue",
        "quarantine_required",
    ):
        if type(recovery.get(field)) is not bool:
            raise MemoryEnrichmentCheckpointShapeError(
                f"startup_recovery.{field} 必须是 bool"
            )
    for field in ("source_provenance_error", "vector_quarantine_error"):
        value = recovery.get(field)
        if value is not None and not isinstance(value, str):
            raise MemoryEnrichmentCheckpointShapeError(
                f"startup_recovery.{field} 必须为 null 或字符串"
            )
    has_provider_ids = "started_provider_attempt_ids" in recovery
    has_unconsumed_attempts = "unconsumed_provider_attempts" in recovery
    has_provider_unknown = "provider_result_unknown" in recovery
    if (
        not has_provider_ids
        or not has_unconsumed_attempts
        or not has_provider_unknown
    ):
        raise MemoryEnrichmentCheckpointShapeError(
            "startup_recovery 必须保留 Provider unknown 审计字段"
        )
    attempt_ids = recovery["started_provider_attempt_ids"]
    if (
        not isinstance(attempt_ids, list)
        or any(
            not isinstance(attempt_id, str) or not attempt_id.strip()
            for attempt_id in attempt_ids
        )
        or len(set(attempt_ids)) != len(attempt_ids)
    ):
        raise MemoryEnrichmentCheckpointShapeError(
            "startup_recovery.started_provider_attempt_ids "
            "必须是唯一非空字符串列表"
        )
    unconsumed_attempts = recovery["unconsumed_provider_attempts"]
    _validate_unconsumed_provider_attempts(
        unconsumed_attempts,
        raise_on_error=True,
    )
    expected_started_ids = [
        attempt["attempt_id"]
        for attempt in unconsumed_attempts
        if attempt["status"] == "started"
    ]
    if attempt_ids != expected_started_ids:
        raise MemoryEnrichmentCheckpointShapeError(
            "startup_recovery.started_provider_attempt_ids 必须精确等于 "
            "unconsumed_provider_attempts 中的 started 子集"
        )
    provider_result_unknown = recovery["provider_result_unknown"]
    if (
        type(provider_result_unknown) is not bool
        or provider_result_unknown is not bool(unconsumed_attempts)
    ):
        raise MemoryEnrichmentCheckpointShapeError(
            "startup_recovery.provider_result_unknown "
            "必须与未消费 Provider attempt 事实一致"
        )

    effective_vector_side_effects = (
        recovery["vector_side_effect_count"]
        if has_extended_audit
        else vector_count
    )
    if recovery["safe_to_continue"]:
        legacy_local_state_safe = (
            sqlite_baseline is None
            and all(value == 0 for value in footprint.values())
            and vector_count == 0
            and recovery["recovery_disposition"]
            == "safe_degraded_no_sqlite_or_vector_persist"
        )
        baseline_local_state_safe = (
            has_extended_audit
            and sqlite_baseline is not None
            and recovery["sqlite_side_effect_count"] == 0
            and effective_vector_side_effects == 0
            and recovery["recovery_disposition"]
            == "safe_degraded_no_new_sqlite_or_vector_persist"
        )
        safe_recovery = (
            (legacy_local_state_safe or baseline_local_state_safe)
            and recovery["source_provenance_verified"] is True
            and recovery["source_provenance_error"] is None
            and recovery["sqlite_side_effect_count"] == 0
            and effective_vector_side_effects == 0
            and recovery["vector_quarantined_count"] == 0
            and recovery["vector_quarantine_succeeded"] is True
            and recovery["vector_quarantine_error"] is None
            and recovery["side_effects_verified_absent"] is True
            and recovery["quarantine_required"] is False
        )
        if not safe_recovery:
            raise MemoryEnrichmentCheckpointShapeError(
                "startup_recovery 声称 safe，但零副作用证明不完整"
            )
    elif (
        recovery["quarantine_required"] is not True
        or recovery["recovery_disposition"] != "blocked_uncertain"
    ):
        raise MemoryEnrichmentCheckpointShapeError(
            "blocked startup_recovery 的处置标志不一致"
        )


def _validate_unconsumed_provider_attempts(
    value: object,
    *,
    raise_on_error: bool,
) -> bool:
    """验证未消费富记忆请求的最小、可审计持久形状。"""

    valid = isinstance(value, list)
    seen: set[str] = set()
    if valid:
        for attempt in value:
            if (
                not isinstance(attempt, dict)
                or set(attempt) != {"attempt_id", "status", "agent_name"}
                or not isinstance(attempt.get("attempt_id"), str)
                or not attempt["attempt_id"].strip()
                or attempt.get("status") not in {"started", "succeeded"}
                or attempt.get("agent_name") not in RICH_MEMORY_PROVIDER_AGENTS
                or attempt["attempt_id"] in seen
            ):
                valid = False
                break
            seen.add(attempt["attempt_id"])
    if not valid and raise_on_error:
        raise MemoryEnrichmentCheckpointShapeError(
            "startup_recovery.unconsumed_provider_attempts 必须是唯一、"
            "合法富记忆 agent 的 started/succeeded 记录列表"
        )
    return valid
