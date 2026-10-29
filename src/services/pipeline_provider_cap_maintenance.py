"""暂停流水线的 Provider hard cap 离线维护入口。

本服务只处理两个非常窄的恢复场景：流水线已在连续性小账本 checkpoint
安全暂停，或当前章已按产品规则升级人工且维护人决定从正式 Chapter UI 继续。
两者都要求 Provider attempt 权威账本与 run 投影精确耗尽当前 cap。维护必须在
唯一 runtime 停止时执行，先创建完整手动备份，再用 CAS 只提高
``pipeline_runs.provider_call_cap``。

它不构造 LLM client、不发 Provider 请求、不改变 run 状态，也不调度 Worker；
恢复执行仍只能从对应的正式产品 UI 入口发生：连续性 checkpoint 走 Run Continue，
升级人工 checkpoint 走 Chapter Rewrite。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel

from src.core.workspace.workspace_paths import WorkspacePaths
from src.services.backup_service import BackupService, BackupServiceError
from src.services.pipeline_service import PipelineServiceError
from src.services.workspace_operation_guard import (
    WorkspaceOperationConflictError,
    ensure_workspace_quiescent,
    workspace_operation_lock,
)
from src.services.workspace_service import WorkspaceService
from src.services.workspace_version_service import (
    WorkspaceVersionServiceError,
    validate_workspace_for_runtime_open,
)

_log = structlog.get_logger(__name__)

_CONTINUITY_PAUSE_REASON = "continuity_ledger_not_ready"
_CHAPTER_ESCALATED_PAUSE_REASON = "chapter_escalated"
_ALLOWED_PAUSE_REASONS = frozenset(
    {_CONTINUITY_PAUSE_REASON, _CHAPTER_ESCALATED_PAUSE_REASON}
)
_MAX_SINGLE_INCREASE = 20


class PipelineProviderCapMaintenanceError(PipelineServiceError):
    """Provider cap 离线维护的身份、静默性或 checkpoint 校验失败。"""


class PipelineProviderCapMaintenanceResult(BaseModel):
    """一次成功 cap 维护的可审计结果。"""

    workspace_id: str
    run_id: str
    previous_cap: int
    new_cap: int
    provider_call_count: int
    provider_attempt_status_counts: dict[str, int]
    state: str
    pause_reason: str
    current_chapter: str
    completed_chapter_ids: list[str]
    backup_id: str
    backup_file_path: str
    backup_size_bytes: int
    applied_at: datetime
    restart_required: bool = True


@dataclass(frozen=True)
class _ValidatedState:
    """备份前和写事务内必须完全一致的 cap/checkpoint 事实。"""

    run_row: dict[str, Any]
    chapter_row: dict[str, Any]
    provider_attempts: tuple[tuple[Any, ...], ...]
    provider_attempt_status_counts: dict[str, int]
    completed_chapter_ids: list[str]
    chapter_version: _ChapterVersionState | None


@dataclass(frozen=True)
class _ChapterVersionState:
    """当前不可变正文版本；chapter_escalated cap 维护必须与磁盘逐字一致。"""

    version_id: int
    content: str
    word_count: int
    creation_type: str
    model_used: str
    review_score: float | None
    notes: str
    style_fingerprint: str | None
    created_at: str


class PipelineProviderCapMaintenanceService:
    """在完整备份与严格 CAS 下提高暂停 run 的 Provider hard cap。"""

    def __init__(
        self,
        *,
        workspace_service: WorkspaceService,
        backup_service: BackupService,
        active_task_checker: Callable[[str], bool] | None,
    ) -> None:
        self._workspace_service = workspace_service
        self._backup_service = backup_service
        self._active_task_checker = active_task_checker

    @staticmethod
    def confirmation_token(
        *,
        workspace_id: str,
        run_id: str,
        expected_current_cap: int,
        new_cap: int,
    ) -> str:
        """返回本次维护四元组的精确确认文本。"""

        return f"{workspace_id}:{run_id}:{expected_current_cap}:{new_cap}"

    def increase_cap(
        self,
        *,
        workspace_id: str,
        run_id: str,
        expected_current_cap: int,
        new_cap: int,
        confirm: str,
    ) -> PipelineProviderCapMaintenanceResult:
        """先完整备份，再只把暂停 run 的 cap 从 expected CAS 到 new。"""

        self._validate_request(
            workspace_id=workspace_id,
            run_id=run_id,
            expected_current_cap=expected_current_cap,
            new_cap=new_cap,
            confirm=confirm,
        )
        paths = WorkspacePaths(
            workspace_id,
            workspaces_root=self._workspace_service.workspaces_root,
        )

        try:
            with workspace_operation_lock(
                self._workspace_service.workspaces_root,
                workspace_id,
            ):
                if not validate_workspace_for_runtime_open(
                    self._workspace_service,
                    workspace_id,
                ):
                    raise PipelineProviderCapMaintenanceError(
                        "工作区数据库尚未初始化，不能执行 Provider cap 维护"
                    )
                ensure_workspace_quiescent(
                    workspace_id=workspace_id,
                    db_path=paths.db_path,
                    active_task_checker=self._active_task_checker,
                )
                before = self._read_validated_state(
                    db_path=paths.db_path,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    expected_current_cap=expected_current_cap,
                )
                self._validate_checkpoint_files(paths=paths, state=before)

                backup = self._backup_service.create_manual_backup(workspace_id)

                connection = self._connect(paths.db_path, query_only=False)
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    locked = self._read_validated_state_from_connection(
                        connection=connection,
                        workspace_id=workspace_id,
                        run_id=run_id,
                        expected_current_cap=expected_current_cap,
                    )
                    if locked != before:
                        raise PipelineProviderCapMaintenanceError(
                            "完整备份后 run、章节或 attempt 账本发生漂移，"
                            "已保留备份并拒绝修改 cap"
                        )
                    self._validate_checkpoint_files(paths=paths, state=locked)

                    applied_at = datetime.now(UTC)
                    result = connection.execute(
                        "UPDATE pipeline_runs "
                        "SET provider_call_cap = ?, updated_at = ? "
                        "WHERE run_id = ? AND workspace_id = ? "
                        "AND state = 'paused' "
                        "AND pause_reason = ? "
                        "AND provider_call_cap = ? "
                        "AND provider_call_count = ?",
                        (
                            new_cap,
                            applied_at,
                            run_id,
                            workspace_id,
                            str(locked.run_row["pause_reason"]),
                            expected_current_cap,
                            expected_current_cap,
                        ),
                    )
                    if result.rowcount != 1:
                        raise PipelineProviderCapMaintenanceError(
                            "Provider cap CAS 未命中，已回滚且保留现场"
                        )

                    after = self._read_raw_state(
                        connection=connection,
                        workspace_id=workspace_id,
                        run_id=run_id,
                    )
                    self._verify_only_cap_changed(
                        before=locked,
                        after=after,
                        new_cap=new_cap,
                    )
                    self._validate_checkpoint_files(paths=paths, state=after)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.close()
        except PipelineProviderCapMaintenanceError:
            raise
        except (
            BackupServiceError,
            WorkspaceOperationConflictError,
            WorkspaceVersionServiceError,
            sqlite3.Error,
        ) as exc:
            raise PipelineProviderCapMaintenanceError(str(exc)) from exc

        _log.info(
            "pipeline_provider_call_cap_increased",
            workspace_id=workspace_id,
            run_id=run_id,
            previous_cap=expected_current_cap,
            new_cap=new_cap,
            provider_call_count=expected_current_cap,
            backup_id=backup.backup_id,
            restart_required=True,
        )
        return PipelineProviderCapMaintenanceResult(
            workspace_id=workspace_id,
            run_id=run_id,
            previous_cap=expected_current_cap,
            new_cap=new_cap,
            provider_call_count=expected_current_cap,
            provider_attempt_status_counts=(before.provider_attempt_status_counts),
            state=str(before.run_row["state"]),
            pause_reason=str(before.run_row["pause_reason"]),
            current_chapter=str(before.run_row["current_chapter"]),
            completed_chapter_ids=before.completed_chapter_ids,
            backup_id=backup.backup_id,
            backup_file_path=backup.file_path,
            backup_size_bytes=backup.size_bytes,
            applied_at=applied_at,
        )

    def _validate_request(
        self,
        *,
        workspace_id: str,
        run_id: str,
        expected_current_cap: int,
        new_cap: int,
        confirm: str,
    ) -> None:
        if expected_current_cap < 1:
            raise PipelineProviderCapMaintenanceError(
                "--expected-current-cap 必须是正整数"
            )
        if new_cap <= expected_current_cap:
            raise PipelineProviderCapMaintenanceError(
                "--new-cap 必须严格高于 --expected-current-cap"
            )
        if new_cap - expected_current_cap > _MAX_SINGLE_INCREASE:
            raise PipelineProviderCapMaintenanceError(
                f"单次最多只能提高 {_MAX_SINGLE_INCREASE} 次 Provider 调用"
            )
        expected_confirm = self.confirmation_token(
            workspace_id=workspace_id,
            run_id=run_id,
            expected_current_cap=expected_current_cap,
            new_cap=new_cap,
        )
        if confirm != expected_confirm:
            raise PipelineProviderCapMaintenanceError(
                "--confirm 与 workspace/run/旧 cap/新 cap 四元组不一致；"
                f"期望: {expected_confirm}"
            )

    @staticmethod
    def _connect(db_path: Path, *, query_only: bool) -> sqlite3.Connection:
        if not db_path.is_file():
            raise PipelineProviderCapMaintenanceError(f"工作区数据库不存在: {db_path}")
        uri = f"{db_path.resolve().as_uri()}?mode={'ro' if query_only else 'rw'}"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if query_only:
            connection.execute("PRAGMA query_only = ON")
        return connection

    def _read_validated_state(
        self,
        *,
        db_path: Path,
        workspace_id: str,
        run_id: str,
        expected_current_cap: int,
    ) -> _ValidatedState:
        connection = self._connect(db_path, query_only=True)
        try:
            return self._read_validated_state_from_connection(
                connection=connection,
                workspace_id=workspace_id,
                run_id=run_id,
                expected_current_cap=expected_current_cap,
            )
        finally:
            connection.close()

    def _read_validated_state_from_connection(
        self,
        *,
        connection: sqlite3.Connection,
        workspace_id: str,
        run_id: str,
        expected_current_cap: int,
    ) -> _ValidatedState:
        state = self._read_raw_state(
            connection=connection,
            workspace_id=workspace_id,
            run_id=run_id,
        )
        run = state.run_row
        chapter = state.chapter_row

        if run["workspace_id"] != workspace_id:
            raise PipelineProviderCapMaintenanceError(
                "run 不属于指定 workspace，拒绝维护"
            )
        if run["state"] != "paused":
            raise PipelineProviderCapMaintenanceError(
                f"run 必须处于 paused，实际为 {run['state']!r}"
            )
        if run["pause_reason"] not in _ALLOWED_PAUSE_REASONS:
            raise PipelineProviderCapMaintenanceError(
                "只允许维护 continuity_ledger_not_ready 或 chapter_escalated "
                "checkpoint，"
                f"实际 pause_reason={run['pause_reason']!r}"
            )
        if not self._is_json_null(run["error_details"]):
            raise PipelineProviderCapMaintenanceError(
                "run 带 error terminal，拒绝提高 cap"
            )
        self._validate_completed_at_checkpoint(run)
        if int(run["provider_call_cap"]) != expected_current_cap:
            raise PipelineProviderCapMaintenanceError(
                "当前 provider_call_cap 与 --expected-current-cap 不一致: "
                f"actual={run['provider_call_cap']} "
                f"expected={expected_current_cap}"
            )
        if int(run["provider_call_count"]) != expected_current_cap:
            raise PipelineProviderCapMaintenanceError(
                "run 投影尚未精确耗尽当前 cap，拒绝维护: "
                f"count={run['provider_call_count']} cap={expected_current_cap}"
            )

        authoritative_count = len(state.provider_attempts)
        if authoritative_count != expected_current_cap:
            raise PipelineProviderCapMaintenanceError(
                "Provider attempt 权威账本与当前 cap 不一致: "
                f"attempts={authoritative_count} cap={expected_current_cap}"
            )
        if authoritative_count != int(run["provider_call_count"]):
            raise PipelineProviderCapMaintenanceError(
                "Provider attempt 权威账本与 run 投影不一致，拒绝维护"
            )

        chapter_ids = self._parse_id_list(run["chapter_ids"], "chapter_ids")
        completed = self._parse_id_list(
            run["completed_chapter_ids"],
            "completed_chapter_ids",
        )
        skipped = self._parse_id_list(
            run["skipped_chapter_ids"],
            "skipped_chapter_ids",
        )
        current = run["current_chapter"]
        if not isinstance(current, str) or current not in chapter_ids:
            raise PipelineProviderCapMaintenanceError(
                "current_chapter 缺失或不在 run 范围内"
            )
        if current in completed or current in skipped:
            raise PipelineProviderCapMaintenanceError(
                "current_chapter 已完成或已跳过，checkpoint 矛盾"
            )
        if len(completed) >= len(chapter_ids):
            raise PipelineProviderCapMaintenanceError(
                "run 已无待完成章节，拒绝提高 cap"
            )
        if chapter["workspace_id"] != workspace_id:
            raise PipelineProviderCapMaintenanceError(
                "current_chapter 不属于指定 workspace"
            )
        if chapter["chapter_id"] != current:
            raise PipelineProviderCapMaintenanceError("current_chapter 行身份漂移")
        self._validate_chapter_checkpoint(
            pause_reason=str(run["pause_reason"]),
            chapter=chapter,
            chapter_version=state.chapter_version,
            provider_attempt_status_counts=state.provider_attempt_status_counts,
        )

        return _ValidatedState(
            run_row=run,
            chapter_row=chapter,
            provider_attempts=state.provider_attempts,
            provider_attempt_status_counts=(state.provider_attempt_status_counts),
            completed_chapter_ids=completed,
            chapter_version=state.chapter_version,
        )

    def _validate_chapter_checkpoint(
        self,
        *,
        pause_reason: str,
        chapter: dict[str, Any],
        chapter_version: _ChapterVersionState | None,
        provider_attempt_status_counts: dict[str, int],
    ) -> None:
        if chapter["ready_for_next_chapter_at"] is not None:
            raise PipelineProviderCapMaintenanceError(
                "current_chapter 已 ready，不应在本 checkpoint 提高 cap"
            )
        if chapter["memory_enrichment_status"] != "pending":
            raise PipelineProviderCapMaintenanceError(
                "current_chapter 富记忆状态不是 pending，拒绝维护: "
                f"actual={chapter['memory_enrichment_status']!r}"
            )
        if chapter["memory_enriched_at"] is not None:
            raise PipelineProviderCapMaintenanceError(
                "current_chapter 的 pending/enriched_at 状态矛盾"
            )

        if pause_reason == _CONTINUITY_PAUSE_REASON:
            if chapter["status"] != "approved":
                raise PipelineProviderCapMaintenanceError(
                    "continuity checkpoint 的 current_chapter 必须 approved，"
                    f"实际为 {chapter['status']!r}"
                )
            return

        if chapter["status"] != "human_intervention":
            raise PipelineProviderCapMaintenanceError(
                "chapter_escalated checkpoint 的 current_chapter 必须 "
                f"human_intervention，实际为 {chapter['status']!r}"
            )
        if int(chapter["current_version"]) < 1 or chapter_version is None:
            raise PipelineProviderCapMaintenanceError(
                "chapter_escalated checkpoint 缺少当前不可变正文版本"
            )
        if chapter_version.version_id != int(chapter["current_version"]):
            raise PipelineProviderCapMaintenanceError(
                "chapter_escalated checkpoint 的版本身份漂移"
            )
        if not chapter_version.content or chapter_version.word_count < 1:
            raise PipelineProviderCapMaintenanceError(
                "chapter_escalated checkpoint 的当前正文版本为空"
            )
        raw_escalation = chapter["escalation_details"]
        try:
            escalation = (
                json.loads(raw_escalation)
                if isinstance(raw_escalation, str)
                else raw_escalation
            )
        except json.JSONDecodeError as exc:
            raise PipelineProviderCapMaintenanceError(
                "chapter_escalated checkpoint 的升级原因不是合法 JSON"
            ) from exc
        if (
            not isinstance(escalation, dict)
            or not isinstance(escalation.get("reason"), str)
            or not escalation["reason"].strip()
        ):
            raise PipelineProviderCapMaintenanceError(
                "chapter_escalated checkpoint 缺少升级原因"
            )
        if provider_attempt_status_counts.get("started", 0) != 0:
            raise PipelineProviderCapMaintenanceError(
                "chapter_escalated checkpoint 仍有 started Provider attempt"
            )

    @staticmethod
    def _validate_checkpoint_files(
        *, paths: WorkspacePaths, state: _ValidatedState
    ) -> None:
        if state.run_row["pause_reason"] != _CHAPTER_ESCALATED_PAUSE_REASON:
            return
        version = state.chapter_version
        if version is None:
            raise PipelineProviderCapMaintenanceError(
                "chapter_escalated checkpoint 缺少当前不可变正文版本"
            )
        chapter_id = str(state.run_row["current_chapter"])
        version_path = paths.chapter_dir(chapter_id) / f"v{version.version_id}.md"
        final_path = paths.chapter_dir(chapter_id) / "final.md"
        try:
            disk_content = version_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PipelineProviderCapMaintenanceError(
                f"chapter_escalated checkpoint 无法读取当前正文版本: {version_path}"
            ) from exc
        if disk_content != version.content:
            raise PipelineProviderCapMaintenanceError(
                "chapter_escalated checkpoint 的 DB/磁盘正文不一致"
            )
        if final_path.exists():
            raise PipelineProviderCapMaintenanceError(
                "chapter_escalated checkpoint 已存在 final.md，拒绝提高 cap"
            )

    @staticmethod
    def _validate_completed_at_checkpoint(run: dict[str, Any]) -> None:
        """paused run 可保留更早 stop 的时间戳，但必须有更晚的恢复活动证据。"""

        completed_raw = run["completed_at"]
        if completed_raw is None:
            return
        if run["pause_reason"] != _CHAPTER_ESCALATED_PAUSE_REASON:
            raise PipelineProviderCapMaintenanceError(
                "continuity checkpoint 已带 completed_at，拒绝提高 cap"
            )
        try:
            completed_at = datetime.fromisoformat(str(completed_raw))
            updated_at = datetime.fromisoformat(str(run["updated_at"]))
        except (TypeError, ValueError) as exc:
            raise PipelineProviderCapMaintenanceError(
                "chapter_escalated checkpoint 的 completed_at/updated_at 非法"
            ) from exc
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=UTC)
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        if updated_at <= completed_at:
            raise PipelineProviderCapMaintenanceError(
                "chapter_escalated checkpoint 的 completed_at 没有更晚恢复证据"
            )

    def _read_raw_state(
        self,
        *,
        connection: sqlite3.Connection,
        workspace_id: str,
        run_id: str,
    ) -> _ValidatedState:
        run_row = connection.execute(
            "SELECT * FROM pipeline_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if run_row is None:
            raise PipelineProviderCapMaintenanceError(
                f"指定 pipeline run 不存在: {run_id}"
            )
        run = dict(run_row)
        current = run.get("current_chapter")
        chapter_row = connection.execute(
            "SELECT * FROM chapters WHERE chapter_id = ?",
            (current,),
        ).fetchone()
        if chapter_row is None:
            raise PipelineProviderCapMaintenanceError(
                f"current_chapter 行不存在: {current!r}"
            )

        attempts = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT attempt_id, workspace_id, run_id, chapter_id, "
                "operation_kind, logical_call_id, agent_name, provider, model, "
                "attempt_number, attempt_cap, status, version_before, "
                "version_after, error_type, created_at, completed_at "
                "FROM attempt_ledger "
                "WHERE run_id = ? AND attempt_kind = 'provider' "
                "ORDER BY attempt_id",
                (run_id,),
            ).fetchall()
        )
        wrong_workspace_count = connection.execute(
            "SELECT COUNT(*) FROM attempt_ledger "
            "WHERE run_id = ? AND attempt_kind = 'provider' "
            "AND workspace_id != ?",
            (run_id, workspace_id),
        ).fetchone()[0]
        if int(wrong_workspace_count) != 0:
            raise PipelineProviderCapMaintenanceError(
                "Provider attempt 的 workspace 身份与 run 不一致"
            )
        status_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT status, COUNT(*) FROM attempt_ledger "
                "WHERE run_id = ? AND workspace_id = ? "
                "AND attempt_kind = 'provider' GROUP BY status ORDER BY status",
                (run_id, workspace_id),
            ).fetchall()
        }
        completed = self._parse_id_list(
            run.get("completed_chapter_ids"),
            "completed_chapter_ids",
        )
        version_row = connection.execute(
            "SELECT version_id, content, word_count, creation_type, model_used, "
            "review_score, notes, style_fingerprint, created_at "
            "FROM chapter_versions WHERE chapter_id = ? AND version_id = ?",
            (current, dict(chapter_row)["current_version"]),
        ).fetchone()
        chapter_version = (
            _ChapterVersionState(
                version_id=int(version_row[0]),
                content=str(version_row[1]),
                word_count=int(version_row[2]),
                creation_type=str(version_row[3]),
                model_used=str(version_row[4]),
                review_score=(
                    float(version_row[5]) if version_row[5] is not None else None
                ),
                notes=str(version_row[6]),
                style_fingerprint=(
                    str(version_row[7]) if version_row[7] is not None else None
                ),
                created_at=str(version_row[8]),
            )
            if version_row is not None
            else None
        )
        return _ValidatedState(
            run_row=run,
            chapter_row=dict(chapter_row),
            provider_attempts=attempts,
            provider_attempt_status_counts=status_counts,
            completed_chapter_ids=completed,
            chapter_version=chapter_version,
        )

    @staticmethod
    def _parse_id_list(raw: Any, field: str) -> list[str]:
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError as exc:
            raise PipelineProviderCapMaintenanceError(f"{field} 不是合法 JSON") from exc
        if (
            not isinstance(value, list)
            or any(not isinstance(item, str) or not item for item in value)
            or len(value) != len(set(value))
        ):
            raise PipelineProviderCapMaintenanceError(
                f"{field} 必须是无重复的非空字符串数组"
            )
        return value

    @staticmethod
    def _is_json_null(raw: Any) -> bool:
        """兼容 SQLite JSON 列的 SQL NULL 与字面 ``"null"`` 两种空值。"""

        if raw is None:
            return True
        if not isinstance(raw, str):
            return False
        try:
            return json.loads(raw) is None
        except json.JSONDecodeError:
            return False

    @staticmethod
    def _verify_only_cap_changed(
        *,
        before: _ValidatedState,
        after: _ValidatedState,
        new_cap: int,
    ) -> None:
        if int(after.run_row["provider_call_cap"]) != new_cap:
            raise PipelineProviderCapMaintenanceError("cap 更新后读回值不一致，已回滚")
        before_run = dict(before.run_row)
        after_run = dict(after.run_row)
        before_run.pop("provider_call_cap", None)
        after_run.pop("provider_call_cap", None)
        before_run.pop("updated_at", None)
        after_run.pop("updated_at", None)
        if before_run != after_run:
            raise PipelineProviderCapMaintenanceError(
                "维护触及了 cap 以外的 run 字段，已回滚"
            )
        if before.chapter_row != after.chapter_row:
            raise PipelineProviderCapMaintenanceError(
                "维护期间章节 checkpoint 发生变化，已回滚"
            )
        if before.provider_attempts != after.provider_attempts:
            raise PipelineProviderCapMaintenanceError(
                "维护期间 Provider attempt 账本发生变化，已回滚"
            )
        if before.chapter_version != after.chapter_version:
            raise PipelineProviderCapMaintenanceError(
                "维护期间当前正文版本发生变化，已回滚"
            )


__all__ = [
    "PipelineProviderCapMaintenanceError",
    "PipelineProviderCapMaintenanceResult",
    "PipelineProviderCapMaintenanceService",
]
