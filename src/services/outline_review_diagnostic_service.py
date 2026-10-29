"""P2 正常产品入口：冻结请求、独立账本、有界只读诊断。"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
import fcntl
import hashlib
import json
import math
from pathlib import Path
from typing import Any
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.api.schemas.outline_diagnostic import DiagnosticOut, DiagnosticJobOut
from src.core.agents.outline_consistency_validator import (
    _build_user_message, _OutlineConsistencyLLMPayload, _SplitReviewPayload,
    _SPLIT_STAGES, _validate_source_quotes, split_review_messages,
    validate_split_review_payload,
)
from src.core.agents.outline_review_diagnostic import (
    LocalReview, freeze_requests, read_original, validate_local,
)
from src.core.agents.outline_review_packets import PacketError, ReviewPacket, SourceIndex, digest
from src.core.agents.outline_repair_delta_review import (
    RepairDeltaReview, _value_at_path, validate_repair_delta_review,
)
from src.core.agents._prompt import load_prompt_template
from src.core.agents.structured_handoff import HandoffPacket, build_packet, render_packet
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.config_loader import load_writer_profiles_config, load_run_model_manifest_catalog
from src.core.llm.base import LLMResponse, Message
from src.core.db.models import AttemptLedgerORM
from src.core.llm.cancel import raise_if_cancelled
from src.core.llm.local_json_contract import LocalJsonResponseViolation
from structlog.contextvars import bound_contextvars
from src.core.models.outline import CharactersOutput, PlotOutput, VolumePlansOutput, WorldOutput
from src.core.routing.run_model_manifest_router import build_model_router
from src.services.attempt_ledger import bind_attempt_scope, wrap_router_with_attempt_tracking
from src.services.outline_service import OutlineService, _atomic_write_json
from src.services.post_v8_abc_selection_activity import _initialize_attempt_ledger
from src.services.run_model_stage_registry import conservative_rendered_input_tokens
from src.services.workspace_service import WorkspaceService


class DiagnosticError(ValueError):
    """诊断准备/来源/授权不满足；不得降级执行。"""


_SPLIT_VERSIONS = frozenset({
    "outline-review-split-hard-v1", "outline-review-repair-split-v1",
})
_OFFICIAL_DEEPSEEK_MAX_OUTPUT = 393216


class OutlineReviewDiagnosticService:
    def __init__(self, workspace_service: WorkspaceService, prompts_dir: Path) -> None:
        self.workspaces = workspace_service
        self.prompts_dir = prompts_dir

    def _root(self, workspace_id: str) -> Path:
        self.workspaces.info(workspace_id)
        return self.workspaces.workspaces_root / workspace_id / "data" / "outline-diagnostics"

    def _directory(self, workspace_id: str, diagnostic_id: str) -> Path:
        import re
        if re.fullmatch(r"diagnostic_[0-9a-f]{64}", diagnostic_id) is None:
            raise DiagnosticError("无效诊断标识")
        return self._root(workspace_id) / diagnostic_id

    def _original(self, workspace_id: str, material: dict | None = None) -> list[Message]:
        meta = self.workspaces.info(workspace_id)
        data = material if material is not None else OutlineService(workspace_service=self.workspaces).read_outline(workspace_id)
        if any(data[name] is None for name in data if name != "mini_plan"):
            raise DiagnosticError("四段大纲尚未齐全，不能准备对照")
        world = WorldOutput.model_validate(data["world"])
        characters = CharactersOutput.model_validate(data["characters"])
        plot = PlotOutput.model_validate(data["plot"])
        volumes = VolumePlansOutput.model_validate(data["volume_plans"])
        models = dict(world=world, characters=characters, plot=plot, volume_plans=volumes)
        task = _build_user_message(world=world, characters=characters, plot=plot, volume_plans=volumes,
            premise=meta.premise, genre=meta.genre, full_context=True, json_handoff=True)
        source = {**{k: v.model_dump(mode="json") for k, v in models.items()}, "premise": meta.premise, "genre": meta.genre}
        from src.core.agents._prompt import load_prompt_template
        system = load_prompt_template(self.prompts_dir / "outline_consistency_validator_v2.md")
        return [Message(role="system", content=system), Message(role="user", content=render_packet(
            build_packet(stage="outline_consistency_validator", data=source, original_task=task), "json"))]

    def _plan(self, workspace_id: str, mode: str = "baseline", *, material: dict | None = None) -> dict:
        original = self._original(workspace_id) if material is None else self._original(workspace_id, material)
        if mode == "baseline":
            return freeze_requests(original)
        if mode not in {"max_hard", "repair_review", "split_hard", "repair_review_split"}:
            raise DiagnosticError("未知诊断模式")
        if mode in {"split_hard", "repair_review_split"}:
            index, _, _ = read_original(original)
            jobs: list[dict[str, Any]] = []
            for stage in _SPLIT_STAGES:
                messages = split_review_messages(stage, index.data, prompts_dir=self.prompts_dir)
                split_job: dict[str, Any] = {
                    "job_id": stage, "messages": [m.model_dump() for m in messages],
                    "generation": {"output_cap": _OFFICIAL_DEEPSEEK_MAX_OUTPUT,
                                   "reasoning_effort": "max"},
                }
                split_job["messages_hash"] = digest(split_job["messages"])
                split_job["input_upper_bound"] = conservative_rendered_input_tokens(messages)
                jobs.append(split_job)
            reserve = sum(j["input_upper_bound"] * 2 + _OFFICIAL_DEEPSEEK_MAX_OUTPUT * 8
                          for j in jobs) / 1_000_000
            return {"version": ("outline-review-repair-split-v1" if mode == "repair_review_split"
                                 else "outline-review-split-hard-v1"),
                    "source_hash": index.source_hash, "jobs": jobs,
                    "request_hash": digest(jobs), "first_attempt_reserve_cny": reserve,
                    "all_attempts_reserve_cny": reserve, "physical_request_cap": len(jobs),
                    "cost_cap_cny": math.ceil(reserve + 0.25),
                    "output_cap": _OFFICIAL_DEEPSEEK_MAX_OUTPUT,
                    "reasoning_effort": "max", "full_book_verdict": False}
        supplement = load_prompt_template(self.prompts_dir / "outline_review_max_hard_diagnostic.md")
        messages = [Message(role="system", content=original[0].content + "\n\n" + supplement), original[1]]
        index, _, _ = read_original(messages)
        job: dict[str, Any] = {"job_id": "full", "messages": [m.model_dump() for m in messages],
                               "generation": {"output_cap": _OFFICIAL_DEEPSEEK_MAX_OUTPUT,
                                              "reasoning_effort": "max"}}
        job["messages_hash"] = digest(job["messages"])
        job["input_upper_bound"] = conservative_rendered_input_tokens(messages)
        output_cap = _OFFICIAL_DEEPSEEK_MAX_OUTPUT
        reserve = (job["input_upper_bound"] * 2 + output_cap * 8) / 1_000_000
        return {"version": "outline-review-repair-review-v1" if mode == "repair_review" else "outline-review-max-hard-v1", "source_hash": index.source_hash,
                "jobs": [job], "request_hash": digest([job]),
                "first_attempt_reserve_cny": reserve,
                "all_attempts_reserve_cny": reserve,
                "physical_request_cap": 1, "cost_cap_cny": math.ceil(reserve + 0.25), "output_cap": output_cap,
                "reasoning_effort": "max", "full_book_verdict": False}

    def _legacy_full_plan(self, workspace_id: str, mode: str, *, material: dict | None = None) -> dict:
        """Reconstruct already frozen 128K requests without altering their evidence."""
        plan = self._plan(workspace_id, mode, material=material)
        if mode not in {"max_hard", "repair_review"}:
            raise DiagnosticError("旧版完整审核模式无效")
        job = dict(plan["jobs"][0])
        job.pop("generation", None)
        reserve = (job["input_upper_bound"] * 2 + 131072 * 8) / 1_000_000
        return {**plan, "jobs": [job], "request_hash": digest([job]),
                "output_cap": 131072, "cost_cap_cny": 2,
                "first_attempt_reserve_cny": reserve,
                "all_attempts_reserve_cny": reserve}

    def prepare(self, workspace_id: str, *, mode: str = "baseline") -> DiagnosticOut:
        plan = self._plan(workspace_id, mode)
        identifier = "diagnostic_" + plan["request_hash"]
        directory = self._directory(workspace_id, identifier)
        directory.mkdir(parents=True, exist_ok=True)
        if (directory / "plan.json").exists() and (directory / "state.json").exists():
            return self.read(workspace_id, identifier)
        with self._lock(directory):
            if not (directory / "plan.json").exists():
                _atomic_write_json(directory / "plan.json", plan)
                _atomic_write_json(directory / "state.json", {j["job_id"]: {"status": "pending"} for j in plan["jobs"]})
        return self.read(workspace_id, identifier)

    def prepare_repair_review(self, workspace_id: str, repair_id: str) -> DiagnosticOut:
        """Freeze two bounded questions about the changed fields and their original source."""
        from src.services.outline_targeted_repair_service import OutlineTargetedRepairService
        repair = OutlineTargetedRepairService(self.workspaces, self.prompts_dir)
        repair_plan, _ = repair._load(repair._directory(workspace_id, repair_id))
        if repair_plan["version"] == "outline-failed-candidate-repair-v1":
            source, candidate, parent_hash = repair.review_material(workspace_id, repair_id)
            plan = self._plan(workspace_id, "repair_review_split", material=candidate)
            plan.update({"repair_id": repair_id, "parent_source_hash": parent_hash,
                         "parent_material_hash": digest(source),
                         "candidate_hash": digest(candidate),
                         "failed_candidate_id": repair_plan["failed_candidate_id"]})
        elif self._load(self._directory(workspace_id, repair_plan["diagnostic_id"]))[0]["version"] == "outline-review-split-hard-v1":
            source, candidate, parent_hash = repair.review_material(workspace_id, repair_id)
            plan = self._plan(workspace_id, "repair_review_split", material=candidate)
            plan.update({"repair_id": repair_id, "parent_source_hash": parent_hash,
                         "parent_material_hash": digest(source),
                         "candidate_hash": digest(candidate)})
        else:
            plan = self._plan_repair_delta(workspace_id, repair_id)
        plan["plan_hash"] = digest(plan)
        identifier = "diagnostic_" + plan["request_hash"]
        directory = self._directory(workspace_id, identifier)
        directory.mkdir(parents=True, exist_ok=True)
        with self._lock(directory):
            if (directory / "plan.json").exists():
                existing, _ = self._load(directory)
                if existing != plan:
                    raise DiagnosticError("候选复审计划与已冻结任务不一致")
            else:
                _atomic_write_json(directory / "plan.json", plan)
                _atomic_write_json(directory / "state.json", {j["job_id"]: {"status": "pending"} for j in plan["jobs"]})
        return self.read(workspace_id, identifier)

    def _plan_repair_delta(self, workspace_id: str, repair_id: str) -> dict:
        from src.services.outline_targeted_repair_service import OutlineTargetedRepairService

        repair = OutlineTargetedRepairService(self.workspaces, self.prompts_dir)
        source, candidate, parent_hash = repair.review_material(workspace_id, repair_id)
        repair_plan, repair_state = repair._load(repair._directory(workspace_id, repair_id))
        baseline_id = repair_plan["diagnostic_id"]
        baseline_plan, baseline_state = self._load(self._directory(workspace_id, baseline_id))
        if (baseline_plan["version"] != "outline-review-max-hard-v1"
                or baseline_plan["source_hash"] != parent_hash
                or baseline_state["full"]["status"] != "completed"):
            raise DiagnosticError("定点复审需要同源、已完成的原始完整审核")
        expected_baseline = (self._legacy_full_plan(workspace_id, "max_hard", material=source)
            if "generation" not in baseline_plan["jobs"][0]
            else self._plan(workspace_id, "max_hard", material=source))
        if baseline_plan != expected_baseline:
            raise DiagnosticError("原始完整审核的材料或提示词已变化")
        conflicts = baseline_state["full"]["result"].get("contradictions", [])
        if len(conflicts) != 1 or repair_plan["conflict_index"] != 0:
            raise DiagnosticError("增量复审只接收原审核恰好一处硬矛盾的候选")
        proposal = repair_state["proposal"]
        changes = proposal["changes"]
        changed_paths = [item["path"] for item in changes]
        counterpart_paths = sorted({item["path"] for item in conflicts[0]["source_quotes"]
            if item["path"] not in changed_paths})
        if not counterpart_paths:
            raise DiagnosticError("原硬矛盾缺少未修改的对照端")
        prompt = load_prompt_template(self.prompts_dir / "outline_repair_delta_review_v1.md")
        system = prompt + "\n\n## 输出 JSON Schema\n" + json.dumps(
            RepairDeltaReview.model_json_schema(), ensure_ascii=False)
        common = {"repair_id": repair_id, "candidate_hash": digest(candidate),
                  "changed_paths": changed_paths, "baseline_conflict": conflicts[0]}
        contexts = {
            "resolution": {**common, "job_id": "resolution", "changes": changes,
                "unchanged_conflict_fields": {path: _value_at_path(candidate, path) for path in counterpart_paths}},
            "regression": {**common, "job_id": "regression", "changes": changes,
                "complete_candidate": candidate},
        }
        jobs: list[dict[str, Any]] = []
        for job_id, context in contexts.items():
            task = "核对原硬矛盾是否消除。" if job_id == "resolution" else "只核对修改字段与候选其余原文是否新增硬矛盾。"
            messages = [Message(role="system", content=system), Message(role="user", content=render_packet(
                build_packet(stage="outline_consistency_validator", data=context, original_task=task), "json"))]
            jobs.append({"job_id": job_id, "messages": [m.model_dump() for m in messages],
                         "messages_hash": digest([m.model_dump() for m in messages]),
                         "input_upper_bound": conservative_rendered_input_tokens(messages)})
        output_cap = 32768
        reserve = sum(job["input_upper_bound"] * 2 + output_cap * 8 for job in jobs) / 1_000_000
        return {"version": "outline-review-repair-delta-v2", "workspace_id": workspace_id,
                "source_hash": digest(candidate),
                "jobs": jobs, "request_hash": digest(jobs),
                "first_attempt_reserve_cny": reserve, "all_attempts_reserve_cny": reserve,
                "physical_request_cap": 2, "cost_cap_cny": 2, "output_cap": output_cap,
                "reasoning_effort": "max", "full_book_verdict": False,
                "repair_id": repair_id, "baseline_review_id": baseline_id,
                "baseline_result_hash": digest(baseline_state["full"]["result"]),
                "parent_source_hash": parent_hash, "parent_material_hash": digest(source),
                "candidate_hash": digest(candidate), "changed_paths": changed_paths,
                "counterpart_paths": counterpart_paths}

    @contextmanager
    def _lock(self, directory: Path):
        with (directory / "activity.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DiagnosticError("该诊断正在执行，请勿重复启动") from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _load(self, directory: Path) -> tuple[dict, dict]:
        try:
            plan = json.loads((directory / "plan.json").read_text())
            state = json.loads((directory / "state.json").read_text())
        except (OSError, ValueError) as exc:
            raise DiagnosticError("诊断记录缺失或损坏") from exc
        if digest(plan["jobs"]) != plan["request_hash"] or directory.name != "diagnostic_" + plan["request_hash"]:
            raise DiagnosticError("冻结请求已变化")
        if plan.get("version") in {"outline-review-repair-review-v1", "outline-review-repair-delta-v2",
                                   "outline-review-repair-split-v1"} and digest({
            key: value for key, value in plan.items() if key != "plan_hash"
        }) != plan.get("plan_hash"):
            raise DiagnosticError("候选复审计划已变化")
        if set(state) != {j["job_id"] for j in plan["jobs"]}:
            raise DiagnosticError("子任务状态与冻结计划不一致")
        return plan, state

    def read(self, workspace_id: str, diagnostic_id: str) -> DiagnosticOut:
        directory = self._directory(workspace_id, diagnostic_id)
        plan, state = self._load(directory)
        return DiagnosticOut(diagnostic_id=diagnostic_id, source_hash=plan["source_hash"],
            request_hash=plan["request_hash"], authorization_present=(directory / "authorization.json").is_file(),
            first_attempt_reserve_cny=plan["first_attempt_reserve_cny"],
            all_attempts_reserve_cny=plan["all_attempts_reserve_cny"], cost_cap_cny=plan["cost_cap_cny"],
            physical_request_cap=plan["physical_request_cap"],
            jobs=[DiagnosticJobOut(job_id=j["job_id"], input_upper_bound=j["input_upper_bound"], **state[j["job_id"]]) for j in plan["jobs"]])

    def _assert_source(self, workspace_id: str, plan: dict) -> None:
        if plan["version"] == "outline-review-repair-delta-v2":
            expected = self._plan_repair_delta(workspace_id, plan["repair_id"])
            if any(expected[key] != plan[key] for key in expected):
                raise DiagnosticError("增量复审候选、原审核或提示词已变化，旧请求不得重放")
            return
        if plan["version"] in {"outline-review-repair-review-v1", "outline-review-repair-split-v1"}:
            from src.services.outline_targeted_repair_service import OutlineTargetedRepairService
            repair = OutlineTargetedRepairService(self.workspaces, self.prompts_dir)
            source, candidate, parent_hash = repair.review_material(workspace_id, plan["repair_id"])
            expected = (self._legacy_full_plan(workspace_id, "repair_review", material=candidate)
                if plan["version"] == "outline-review-repair-review-v1" and "generation" not in plan["jobs"][0]
                else self._plan(workspace_id,
                    "repair_review_split" if plan["version"] == "outline-review-repair-split-v1"
                    else "repair_review", material=candidate))
            if (parent_hash != plan["parent_source_hash"] or digest(source) != plan["parent_material_hash"]
                    or digest(candidate) != plan["candidate_hash"] or
                    any(expected[key] != plan[key] for key in expected)):
                raise DiagnosticError("修复候选或原大纲已变化，旧复审不得重放")
            return
        if plan["version"] == "outline-review-max-hard-v1":
            mode = "max_hard"
        elif plan["version"] == "outline-review-split-hard-v1":
            mode = "split_hard"
        elif plan["version"] in {"outline-review-diagnostic-v1", "outline-review-diagnostic-v2"}:
            mode = "baseline"
        else:
            raise DiagnosticError("未知诊断计划版本")
        expected = (freeze_requests(self._original(workspace_id), legacy=True)
                    if plan["version"] == "outline-review-diagnostic-v1"
                    else self._legacy_full_plan(workspace_id, mode)
                    if mode == "max_hard" and "generation" not in plan["jobs"][0]
                    else self._plan(workspace_id, mode))
        if expected != plan:
            raise DiagnosticError("大纲或提示词已变化，请重新准备；旧结果保留")

    def preflight(self, workspace_id: str, diagnostic_id: str, request_hash: str) -> None:
        directory = self._directory(workspace_id, diagnostic_id)
        plan, _ = self._load(directory)
        if request_hash != plan["request_hash"]:
            raise DiagnosticError("页面请求版本已过期")
        self._assert_source(workspace_id, plan)
        self._authorization(directory, plan)

    def _recover_repair_review(self, directory: Path, plan: dict, state: dict) -> bool:
        if plan["version"] in _SPLIT_VERSIONS:
            return self._recover_split_review(directory, plan, state)
        if plan["version"] == "outline-review-repair-delta-v2":
            return self._recover_repair_delta(directory, plan, state)
        if plan["version"] != "outline-review-repair-review-v1" or state["full"]["status"] != "started":
            return False
        response_path = directory / "full.response.json"
        ledger_path = directory / "attempts.sqlite3"
        if not response_path.is_file() or not ledger_path.is_file():
            return False
        engine = create_engine(f"sqlite:///{ledger_path}")
        try:
            with Session(engine) as session:
                attempts = list(session.scalars(select(AttemptLedgerORM)))
            if len(attempts) != 1 or attempts[0].status != "succeeded":
                return False
        finally:
            engine.dispose()
        try:
            response = LLMResponse.model_validate_json(response_path.read_text(encoding="utf-8"))
            if (directory / "full.response.violation.json").is_file():
                raise ValueError("响应违反 JSON 合同")
            if response.finish_reason != "stop":
                raise ValueError("未正常结束")
            result = _OutlineConsistencyLLMPayload.model_validate_json(response.content)
            if "contradictions" not in result.model_fields_set:
                raise ValueError("缺少显式结论")
            original = [Message(**item) for item in plan["jobs"][0]["messages"]]
            index, _, _ = read_original(original)
            _validate_source_quotes(result, index.data)
            state["full"] = {"status": "completed", "result": result.model_dump(mode="json")}
        except ValueError:
            state["full"] = {"status": "invalid", "error": "已保存响应不合格；不能当作通过或再次付费调用"}
        _atomic_write_json(directory / "state.json", state)
        return True

    @staticmethod
    def _split_source(plan: dict) -> dict:
        packet = HandoffPacket.model_validate_json(plan["jobs"][-1]["messages"][1]["content"])
        if packet.data.get("review_stage") != "cross":
            raise DiagnosticError("跨段来源包缺失")
        return {key: value for key, value in packet.data.items()
                if key not in {"review_stage", "source_hash"}}

    @classmethod
    def split_review_passed(cls, plan: dict, state: dict) -> bool:
        if plan.get("version") not in _SPLIT_VERSIONS or [job["job_id"] for job in plan["jobs"]] != list(_SPLIT_STAGES):
            return False
        try:
            source = cls._split_source(plan)
            for stage in _SPLIT_STAGES:
                item = state[stage]
                if item["status"] != "completed":
                    return False
                result = validate_split_review_payload(
                    _SplitReviewPayload.model_validate(item["result"]), stage=stage, source=source)
                if result.outcome != "compatible":
                    return False
        except (KeyError, TypeError, ValueError):
            return False
        return True

    @staticmethod
    def split_review_gaps(state: dict) -> list[str]:
        return [gap for stage in _SPLIT_STAGES
                for gap in state[stage]["result"].get("coverage_gaps", [])]

    @staticmethod
    def split_review_conflicts(plan: dict, state: dict) -> list[dict]:
        if plan.get("version") not in _SPLIT_VERSIONS:
            return []
        return [item for stage in _SPLIT_STAGES
                if state[stage]["status"] == "completed"
                for item in state[stage]["result"].get("contradictions", [])]

    @staticmethod
    def _close_split_after_nonpass(plan: dict, state: dict) -> None:
        stopped = False
        for job in plan["jobs"]:
            job_id = job["job_id"]
            current = state[job_id]
            if stopped and current["status"] == "pending":
                state[job_id] = {"status": "invalid", "error": "前一项未通过或未决；未发起后续付费审核"}
            if current["status"] == "invalid" or (current["status"] == "completed"
                    and current["result"].get("outcome") != "compatible"):
                stopped = True

    def _recover_split_review(self, directory: Path, plan: dict, state: dict) -> bool:
        started = [job["job_id"] for job in plan["jobs"]
                   if state[job["job_id"]]["status"] == "started"]
        if not started:
            return False
        ledger_path = directory / "attempts.sqlite3"
        if not ledger_path.is_file():
            return False
        engine = create_engine(f"sqlite:///{ledger_path}")
        try:
            with Session(engine) as session:
                attempts = list(session.scalars(select(AttemptLedgerORM).order_by(AttemptLedgerORM.created_at)))
        finally:
            engine.dispose()
        source = self._split_source(plan)
        changed = False
        for index, job in enumerate(plan["jobs"]):
            job_id = job["job_id"]
            if state[job_id]["status"] != "started":
                continue
            response_path = directory / f"{job_id}.response.json"
            if not response_path.is_file() or len(attempts) <= index or attempts[index].status != "succeeded":
                return changed
            try:
                response = LLMResponse.model_validate_json(response_path.read_text(encoding="utf-8"))
                if (directory / f"{job_id}.response.violation.json").exists() or response.finish_reason != "stop":
                    raise ValueError("审核响应没有正常结束")
                result = validate_split_review_payload(
                    _SplitReviewPayload.model_validate_json(response.content),
                    stage=job_id, source=source)
                state[job_id] = {"status": "completed", "result": result.model_dump(mode="json")}
            except ValueError:
                state[job_id] = {"status": "invalid", "error": "已保存响应不合格；不能当作通过或再次付费调用"}
            changed = True
            self._close_split_after_nonpass(plan, state)
        if changed:
            _atomic_write_json(directory / "state.json", state)
        return changed

    def _validate_delta_response(self, workspace_id: str, plan: dict, job_id: str,
                                 response: LLMResponse) -> RepairDeltaReview:
        from src.services.outline_targeted_repair_service import OutlineTargetedRepairService

        if response.finish_reason != "stop":
            raise ValueError("增量复审未正常结束")
        repair = OutlineTargetedRepairService(self.workspaces, self.prompts_dir)
        _, candidate, _ = repair.review_material(workspace_id, plan["repair_id"])
        result = RepairDeltaReview.model_validate_json(response.content)
        return validate_repair_delta_review(result, material=candidate,
            repair_id=plan["repair_id"], candidate_hash=plan["candidate_hash"],
            changed_paths=set(plan["changed_paths"]),
            prior_counterpart_paths=set(plan["counterpart_paths"]), job_id=job_id)

    @staticmethod
    def _close_delta_after_resolution(state: dict) -> None:
        first = state["resolution"]
        if first["status"] == "invalid" or (first["status"] == "completed"
                and first["result"]["outcome"] != "compatible"):
            if state["regression"]["status"] == "pending":
                state["regression"] = {"status": "invalid", "error": "原冲突尚未证明消除；未发起第二次调用"}

    def _recover_repair_delta(self, directory: Path, plan: dict, state: dict) -> bool:
        started = [j["job_id"] for j in plan["jobs"] if state[j["job_id"]]["status"] == "started"]
        if not started:
            return False
        ledger_path = directory / "attempts.sqlite3"
        if not ledger_path.is_file():
            return False
        engine = create_engine(f"sqlite:///{ledger_path}")
        try:
            with Session(engine) as session:
                attempts = list(session.scalars(select(AttemptLedgerORM).order_by(AttemptLedgerORM.created_at)))
        finally:
            engine.dispose()
        changed = False
        for index, job in enumerate(plan["jobs"]):
            job_id = job["job_id"]
            if state[job_id]["status"] != "started":
                continue
            response_path = directory / f"{job_id}.response.json"
            if (not response_path.is_file() or len(attempts) <= index
                    or attempts[index].status != "succeeded"):
                return changed
            try:
                response = LLMResponse.model_validate_json(response_path.read_text(encoding="utf-8"))
                if (directory / f"{job_id}.response.violation.json").exists():
                    raise ValueError("响应违反 JSON 合同")
                result = self._validate_delta_response(plan["workspace_id"], plan, job_id, response)
                state[job_id] = {"status": "completed", "result": result.model_dump(mode="json")}
            except ValueError:
                state[job_id] = {"status": "invalid", "error": "已保存响应不合格；不重发模型请求"}
            changed = True
            self._close_delta_after_resolution(state)
        if changed:
            _atomic_write_json(directory / "state.json", state)
        return changed

    def _authorization(self, directory: Path, plan: dict) -> dict:
        try:
            auth = json.loads((directory / "authorization.json").read_text())
            config_hash = hashlib.sha256((directory / "config.yaml").read_bytes()).hexdigest()
        except (OSError, ValueError) as exc:
            raise DiagnosticError("尚无本诊断的独立付费授权与冻结配置") from exc
        if (auth.get("request_hash") != plan["request_hash"] or auth.get("config_hash") != config_hash
            or auth.get("cost_cap_cny") != plan["cost_cap_cny"]
            or auth.get("physical_request_cap") != plan["physical_request_cap"]
            or not isinstance(auth.get("approval_reference"), str) or not auth["approval_reference"].strip()):
            raise DiagnosticError("授权范围或配置版本不匹配")
        return auth

    def render_runtime(self, directory: Path, plan: dict, *, client_factory=None):
        if plan["version"] == "outline-review-diagnostic-v1":
            raise DiagnosticError("旧分包对照仅供读取；请重新准备使用当前最大容量的新诊断，旧结果保留")
        config_path = directory / "config.yaml"
        manifest = load_run_model_manifest_catalog(config_path).active_manifest
        if manifest is None or manifest.model_revision != "DeepSeek-V4.1-Flash":
            raise DiagnosticError("必须保持原 DeepSeek-V4.1-Flash 模型版本")
        router = build_model_router(load_writer_profiles_config(config_path), config_path=config_path, client_factory=client_factory)
        binding = getattr(router, "run_model_manifest_binding", None)
        if (binding is None or binding.selection_test.max_physical_requests != plan["physical_request_cap"]
            or binding.selection_test.max_configured_rmb != Decimal(plan["cost_cap_cny"])):
            raise DiagnosticError("独立 manifest 调用次数或费用上限不匹配")
        target = router.route("outline_consistency_validator")
        profile = target.profile
        if (profile.model != "deepseek-flash" or (profile.base_url or "").rstrip("/") not in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}
            or profile.cost_currency != "CNY" or profile.cost_per_1k_input != .002 or profile.cost_per_1k_output != .008
            or getattr(target.client, "handoff_format", None) != "json"):
            raise DiagnosticError("模型、JSON 交接或冻结单价不匹配")
        rendered = []
        for job in plan["jobs"]:
            output_model = (_SplitReviewPayload if plan["version"] in _SPLIT_VERSIONS
                else RepairDeltaReview if plan["version"] == "outline-review-repair-delta-v2"
                else _OutlineConsistencyLLMPayload if job["job_id"] == "full" else LocalReview)
            kwargs = prepare_strict_json_output(client=target.client, model=profile.model,
                output_model=output_model, schema_name=("outline_repair_delta" if plan["version"] == "outline-review-repair-delta-v2"
                    else "outline_split_consistency" if plan["version"] in _SPLIT_VERSIONS
                    else "outline_consistency" if job["job_id"] == "full" else "outline_review_local"),
                complete_kwargs=target.complete_kwargs)
            if (kwargs.get("max_tokens", kwargs.get("max_completion_tokens")) != plan["output_cap"]
                or ("provider_input_guard_tokens" in plan
                    and kwargs.get("input_token_budget") != plan["provider_input_guard_tokens"])
                or "temperature" in kwargs
                or kwargs.get("reasoning_effort") != plan.get("reasoning_effort", "high")
                or kwargs.get("thinking") != {"type": "enabled"}):
                raise DiagnosticError("输出上限或思考强度不匹配")
            messages = [Message(**m) for m in job["messages"]]
            final = getattr(target.client, "messages_for_cost_reserve")(messages, {**kwargs, "model": profile.model})
            rendered.append((messages, kwargs, [m.model_dump() for m in final]))
        reserve = (sum(conservative_rendered_input_tokens([Message(**m) for m in final])*2
            + plan["output_cap"]*8 for _, _, final in rendered)
            * (plan["physical_request_cap"] // len(plan["jobs"])) / 1_000_000)
        if reserve > plan["cost_cap_cny"]:
            raise DiagnosticError("正式消息预留超过独立费用上限，未启动调用")
        wire = {"request_hash": plan["request_hash"], "reserve_cny": reserve, "messages": [r[2] for r in rendered]}
        return router, binding, rendered, wire

    def _runtime(self, directory: Path, plan: dict, workspace_id: str, task_id: str):
        router, binding, rendered, wire = self.render_runtime(directory, plan)
        auth = self._authorization(directory, plan)
        if auth.get("wire_hash") != digest(wire):
            raise DiagnosticError("正式请求尚未核对并冻结到授权；未启动调用")
        engine, factory = _initialize_attempt_ledger(directory / "attempts.sqlite3")
        scope = dict(workspace_id=workspace_id, task_id=task_id, activity_scope="selection_test",
            provider_call_cap=plan["physical_request_cap"], run_model_manifest_token=binding.token,
            selection_test_manifest_token=binding.selection_test.token,
            configured_cost_currency="CNY", configured_cost_cap=Decimal(plan["cost_cap_cny"]))
        return router, engine, factory, scope, rendered

    def run(self, workspace_id: str, diagnostic_id: str, request_hash: str, task_id: str) -> DiagnosticOut:
        self.preflight(workspace_id, diagnostic_id, request_hash)
        directory = self._directory(workspace_id, diagnostic_id)
        with self._lock(directory):
            plan, state = self._load(directory)
            self._recover_repair_review(directory, plan, state)
            if plan["version"] in _SPLIT_VERSIONS:
                before = json.dumps(state, sort_keys=True)
                self._close_split_after_nonpass(plan, state)
                if json.dumps(state, sort_keys=True) != before:
                    _atomic_write_json(directory / "state.json", state)
            if plan["version"] == "outline-review-repair-delta-v2":
                before = json.dumps(state, sort_keys=True)
                self._close_delta_after_resolution(state)
                if json.dumps(state, sort_keys=True) != before:
                    _atomic_write_json(directory / "state.json", state)
            if any(v["status"] == "started" for v in state.values()):
                raise DiagnosticError("存在已启动但未确认收口的请求，禁止重放；先核对原始响应与账本")
            if all(v["status"] in {"completed", "invalid"} for v in state.values()):
                return self.read(workspace_id, diagnostic_id)
            router, engine, factory, scope, rendered = self._runtime(directory, plan, workspace_id, task_id)
            index: SourceIndex | None
            packets: list[ReviewPacket]
            if plan["version"] in _SPLIT_VERSIONS | {"outline-review-repair-delta-v2"}:
                index, packets = None, []
            else:
                original = [Message(**m) for m in plan["jobs"][0]["messages"]]
                index, packets, _ = read_original(original)
            try:
                with bind_attempt_scope(**scope):
                    tracked = wrap_router_with_attempt_tracking(router, workspace_id=workspace_id, session_factory=factory)
                    for job, (messages, kwargs, final) in zip(plan["jobs"], rendered, strict=True):
                        identifier = job["job_id"]
                        if state[identifier]["status"] != "pending":
                            continue
                        raise_if_cancelled()
                        self._assert_source(workspace_id, plan)
                        self._authorization(directory, plan)
                        target = tracked.route("outline_consistency_validator")
                        if [m.model_dump() for m in getattr(target.client, "messages_for_cost_reserve")(messages, {**kwargs, "model": target.profile.model})] != final:
                            raise DiagnosticError("最终请求与费用预留不一致")
                        state[identifier] = {"status": "started"}
                        _atomic_write_json(directory / "state.json", state)
                        violation = False
                        with bound_contextvars(workspace_id=None):
                            try:
                                response = target.client.complete(messages=messages, model=target.profile.model, **kwargs)
                            except LocalJsonResponseViolation as exc:
                                response = exc.response
                                violation = True
                                _atomic_write_json(directory / f"{identifier}.response.violation.json", {"invalid": True})
                        _atomic_write_json(directory / f"{identifier}.response.json", response.model_dump(mode="json"))
                        result: _OutlineConsistencyLLMPayload | _SplitReviewPayload | LocalReview | RepairDeltaReview
                        try:
                            if violation or response.finish_reason != "stop":
                                raise PacketError("模型输出结构不符或未正常结束")
                            if plan["version"] in _SPLIT_VERSIONS:
                                result = validate_split_review_payload(
                                    _SplitReviewPayload.model_validate_json(response.content),
                                    stage=identifier, source=self._split_source(plan))
                            elif plan["version"] == "outline-review-repair-delta-v2":
                                result = self._validate_delta_response(workspace_id, plan, identifier, response)
                            elif identifier == "full":
                                assert index is not None
                                result = _OutlineConsistencyLLMPayload.model_validate_json(response.content)
                                if "contradictions" not in result.model_fields_set:
                                    raise PacketError("完整终审缺少显式结论")
                                _validate_source_quotes(result, index.data)
                            else:
                                assert index is not None
                                result = validate_local(index, next(p for p in packets if p.packet_id == identifier), LocalReview.model_validate_json(response.content))
                            state[identifier] = {"status": "completed", "result": result.model_dump(mode="json")}
                        except ValueError:
                            state[identifier] = {"status": "invalid", "error": "响应结构、版本、覆盖或逐字引文不合格；不能当作通过"}
                        if plan["version"] == "outline-review-repair-delta-v2":
                            self._close_delta_after_resolution(state)
                        if plan["version"] in _SPLIT_VERSIONS:
                            self._close_split_after_nonpass(plan, state)
                        _atomic_write_json(directory / "state.json", state)
            finally:
                engine.dispose()
        return self.read(workspace_id, diagnostic_id)
