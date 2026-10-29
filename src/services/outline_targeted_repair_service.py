"""P3 product repair: bounded proposal, staged review, then one-section commit."""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from structlog.contextvars import bound_contextvars

from src.config import settings
from src.core.agents._prompt import load_prompt_template
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.agents.outline_review_packets import digest
from src.core.agents.outline_targeted_repair import (
    DEPENDENTS, PatchProposal, RepairBoundaryError, allowed_fields, apply_proposal,
    FailedCandidatePatchProposal, allowed_failed_candidate_fields,
    apply_failed_candidate_proposal,
)
from src.core.agents.structured_handoff import HandoffPacket, build_packet, render_packet
from src.core.config_loader import load_run_model_manifest_catalog, load_writer_profiles_config
from src.core.llm.base import LLMResponse, Message
from src.core.llm.local_json_contract import LocalJsonResponseViolation
from src.core.llm.cancel import raise_if_cancelled
from src.core.models.outline import CharactersOutput, VolumePlansOutput
from src.core.models.narrative import StoryIntent, UserNarrativeRequest
from src.core.db.models import AttemptLedgerORM
from src.core.routing.run_model_manifest_router import build_model_router
from src.core.rules.outline_validators import validate_arc_chapter_spans
from src.core.rules.outline_action_state import (
    find_pending_signature_overlaps, find_repeated_completed_signatures,
)
from src.services.attempt_ledger import bind_attempt_scope, wrap_router_with_attempt_tracking
from src.services.outline_service import OutlineSection, OutlineService, _atomic_write_json
from src.services.post_v8_abc_selection_activity import _initialize_attempt_ledger
from src.services.run_model_stage_registry import conservative_rendered_input_tokens
from src.services.workspace_service import WorkspaceService


class OutlineRepairError(RepairBoundaryError):
    pass


class OutlineTargetedRepairService:
    def __init__(self, workspaces: WorkspaceService, prompts_dir: Path) -> None:
        self.workspaces = workspaces
        self.prompts_dir = prompts_dir
        self.outlines = OutlineService(workspace_service=workspaces)

    def _root(self, workspace_id: str) -> Path:
        self.workspaces.info(workspace_id)
        return self.workspaces.workspaces_root / workspace_id / "data" / "outline-repairs"

    def _directory(self, workspace_id: str, repair_id: str) -> Path:
        if re.fullmatch(r"repair_[0-9a-f]{64}", repair_id) is None:
            raise OutlineRepairError("无效修复标识")
        return self._root(workspace_id) / repair_id

    @contextmanager
    def _lock(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "activity.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise OutlineRepairError("该修复正在执行") from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _source(self, workspace_id: str, plan: dict | None = None, *, _seen: frozenset[str] = frozenset()) -> dict:
        if plan is not None and plan.get("version") == "outline-failed-candidate-repair-v1":
            record = self.outlines.read_failed_volume_candidate(
                workspace_id, plan["failed_candidate_id"])
            if digest(record) != plan["failed_candidate_hash"]:
                raise OutlineRepairError("失败候选记录已变化")
            current = self.outlines.read_outline(workspace_id)
            if any(
                (digest(current[name]) if current[name] is not None else None) != value
                for name, value in record["parent_hashes"].items()
            ):
                raise OutlineRepairError("正式大纲来源已变化，失败候选不可再修复")
            if current["mini_plan"] is not None:
                raise OutlineRepairError("已有章节小纲，失败候选不可写回")
            if plan.get("parent_repair_id"):
                source, _, parent_hash = self._failed_parent(workspace_id,
                    candidate_id=plan["failed_candidate_id"], parent_repair_id=plan["parent_repair_id"],
                    parent_review_id=plan["parent_review_id"], seen=_seen)
                if parent_hash != plan["parent_review_hash"]:
                    raise OutlineRepairError("前轮修复或复审记录已变化")
                return source
            return record["source"]
        from src.services.outline_review_diagnostic_service import OutlineReviewDiagnosticService
        diagnostic = OutlineReviewDiagnosticService(self.workspaces, self.prompts_dir)
        messages = diagnostic._original(workspace_id)
        # Keep field insertion order in the actual handoff. The source digest
        # is canonical, but the exact reviewed message is order-sensitive.
        return HandoffPacket.model_validate_json(messages[1].content).data

    def _load(self, directory: Path) -> tuple[dict, dict]:
        try:
            plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
            state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OutlineRepairError("修复记录缺失或损坏") from exc
        if plan.get("version") not in {"outline-targeted-repair-v1", "outline-failed-candidate-repair-v1"} or digest({k: v for k, v in plan.items() if k != "request_hash"}) != plan.get("request_hash"):
            raise OutlineRepairError("冻结修复任务已改变")
        if directory.name != "repair_" + plan["request_hash"] or state.get("status") not in {"pending", "started", "staged", "invalid", "committed"}:
            raise OutlineRepairError("修复状态无效")
        return plan, state

    def _patch_generation_policy(self, source: dict, *, legacy_cost_cap: int) -> dict:
        # 旧计划逐字保留；新叙事书的补丁也是上游生成，继承活动官方额度。
        intent = source.get("world", {}).get("story_intent")
        if intent is None:
            return {"cost_cap_cny": legacy_cost_cap, "output_cap": 32768,
                    "reasoning_effort": "max"}
        try:
            StoryIntent.model_validate(intent)
        except ValidationError as exc:
            raise OutlineRepairError("故事定位结构无效，不能静默使用旧补丁额度") from exc
        manifest = load_run_model_manifest_catalog(settings.config_yaml_path).active_manifest
        if (manifest is None or manifest.model_revision != "DeepSeek-V4.1-Flash"
                or manifest.narrative_handoff_version != 1 or manifest.upstream_generation is None):
            raise OutlineRepairError("新叙事补丁缺少活动上游生成配置")
        generation = manifest.upstream_generation
        if generation.thinking_request.get("reasoning_effort") != "max":
            raise OutlineRepairError("新叙事补丁必须继承上游 max 思考")
        reserve = (Decimal(generation.provider_input_guard_tokens)
                   * manifest.input_price_rmb_per_million
                   + Decimal(generation.max_completion_tokens)
                   * manifest.output_price_rmb_per_million) / Decimal(1_000_000)
        return {"cost_cap_cny": math.ceil(reserve),
                "output_cap": generation.max_completion_tokens,
                "provider_input_guard_tokens": generation.provider_input_guard_tokens,
                "reasoning_effort": "max"}

    def prepare(self, workspace_id: str, *, diagnostic_id: str, conflict_index: int,
                allowed_paths: list[str], direction: str) -> dict:
        from src.services.outline_review_diagnostic_service import OutlineReviewDiagnosticService
        if self.outlines.read_outline(workspace_id)["mini_plan"] is not None:
            raise OutlineRepairError("当前定点修复只用于生成章节小纲之前；已有小纲须另行处理下游失效")
        diagnostic = OutlineReviewDiagnosticService(self.workspaces, self.prompts_dir)
        review_dir = diagnostic._directory(workspace_id, diagnostic_id)
        review_plan, review_state = diagnostic._load(review_dir)
        split_baseline = review_plan["version"] == "outline-review-split-hard-v1"
        legacy_baseline = (review_plan["version"] == "outline-review-max-hard-v1"
                           and review_state["full"]["status"] == "completed")
        if not (split_baseline or legacy_baseline):
            raise OutlineRepairError("只能依据已完成的最高强度完整审核准备修复")
        diagnostic._assert_source(workspace_id, review_plan)
        source = self._source(workspace_id)
        if digest(source) != review_plan["source_hash"]:
            raise OutlineRepairError("审核与当前大纲来源不同")
        conflicts = (diagnostic.split_review_conflicts(review_plan, review_state)
                     if split_baseline else review_state["full"]["result"].get("contradictions", []))
        if conflict_index < 0 or conflict_index >= len(conflicts):
            raise OutlineRepairError("所选硬矛盾不存在")
        conflict = conflicts[conflict_index]
        section, admitted = allowed_fields(source, conflict["source_quotes"], allowed_paths)
        if not direction.strip() or len(direction) > 1200:
            raise OutlineRepairError("请写明要保留哪一侧事实，且不超过 1200 字")
        prompt = load_prompt_template(self.prompts_dir / "outline_targeted_repair_v1.md")
        task = {"source_hash": digest(source), "conflict": conflict,
                "allowed_fields": admitted, "direction": direction.strip(),
                "protected_sections": [name for name in ("world", "characters", "plot", "volume_plans") if name != section],
                "dependency_review": DEPENDENTS[section], "complete_outline": source}
        messages = [Message(role="system", content=prompt), Message(role="user", content=render_packet(
            build_packet(stage="outline_consistency_validator", data=task, original_task="按指定字段提交大纲定点修复补丁。"), "json"))]
        job = {"messages": [m.model_dump() for m in messages], "messages_hash": digest([m.model_dump() for m in messages]),
               "input_upper_bound": conservative_rendered_input_tokens(messages)}
        plan: dict[str, Any] = {"version": "outline-targeted-repair-v1", "source_hash": digest(source),
                "source_section_hashes": {name: digest(source[name]) for name in ("world", "characters", "plot", "volume_plans")},
                "diagnostic_id": diagnostic_id, "conflict_index": conflict_index,
                "section": section, "allowed_fields": admitted, "direction": direction.strip(),
                "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(), "job": job,
                "physical_request_cap": 1,
                **self._patch_generation_policy(source, legacy_cost_cap=1)}
        plan["request_hash"] = digest(plan)
        repair_id = "repair_" + plan["request_hash"]
        directory = self._directory(workspace_id, repair_id)
        with self._lock(directory):
            if (directory / "plan.json").exists():
                frozen, _ = self._load(directory)
                if frozen != plan:
                    raise OutlineRepairError("同一标识的冻结任务不同")
            else:
                _atomic_write_json(directory / "plan.json", plan)
                _atomic_write_json(directory / "state.json", {"status": "pending"})
        return self.read(workspace_id, repair_id)

    def _failed_parent(self, workspace_id: str, *, candidate_id: str,
                       parent_repair_id: str, parent_review_id: str,
                       seen: frozenset[str] = frozenset()) -> tuple[dict, list[dict], str]:
        from src.core.agents.outline_consistency_validator import (
            _SplitReviewPayload, validate_split_review_payload,
        )
        from src.services.outline_review_diagnostic_service import OutlineReviewDiagnosticService
        if parent_repair_id in seen:
            raise OutlineRepairError("修复候选的来源链循环")
        directory = self._directory(workspace_id, parent_repair_id)
        parent_plan, parent_state = self._load(directory)
        if (parent_plan["version"] != "outline-failed-candidate-repair-v1"
                or parent_plan["failed_candidate_id"] != candidate_id
                or parent_state["status"] != "staged"):
            raise OutlineRepairError("只能接续同一失败候选的有效暂存补丁")
        source = self._source(workspace_id, parent_plan, _seen=seen | {parent_repair_id})
        if source != self._frozen_source(parent_plan):
            raise OutlineRepairError("前轮补丁的冻结来源不符")
        _, candidate = self._candidate_from_record(directory, parent_plan, parent_state)
        diagnostic = OutlineReviewDiagnosticService(self.workspaces, self.prompts_dir)
        review_plan, review_state = diagnostic._load(diagnostic._directory(workspace_id, parent_review_id))
        if (review_plan.get("version") != "outline-review-repair-split-v1"
                or review_plan.get("repair_id") != parent_repair_id
                or review_plan.get("candidate_hash") != digest(candidate)
                or review_plan.get("parent_source_hash") != parent_plan["source_hash"]
                or any(item["status"] in {"pending", "started"} for item in review_state.values())):
            raise OutlineRepairError("前轮候选尚无同源、已结束的复审")
        expected = diagnostic._plan(workspace_id, "repair_review_split", material=candidate)
        if any(review_plan[key] != expected[key] for key in expected):
            raise OutlineRepairError("前轮复审完整材料或提示词已变化")
        reviewed_source = diagnostic._split_source(review_plan)
        for stage, item in review_state.items():
            if item["status"] == "completed":
                validate_split_review_payload(_SplitReviewPayload.model_validate(item["result"]),
                    stage=stage, source=reviewed_source)
        conflicts = diagnostic.split_review_conflicts(review_plan, review_state)
        if not conflicts:
            raise OutlineRepairError("前轮没有可定位的硬矛盾，不能凭未决或技术故障补写")
        return candidate, conflicts, digest({"plan": review_plan, "state": review_state})

    def prepare_failed_candidate(
        self, workspace_id: str, *, candidate_id: str,
        conflict_indexes: list[int], allowed_paths: list[str], direction: str,
        parent_repair_id: str | None = None, parent_review_id: str | None = None,
    ) -> dict:
        record = self.outlines.read_failed_volume_candidate(workspace_id, candidate_id)
        current = self.outlines.read_outline(workspace_id)
        if current["mini_plan"] is not None:
            raise OutlineRepairError("已有章节小纲，不能修复旧候选")
        if any(
            (digest(current[name]) if current[name] is not None else None) != value
            for name, value in record["parent_hashes"].items()
        ):
            raise OutlineRepairError("失败候选的正式来源已变化")
        if bool(parent_repair_id) != bool(parent_review_id):
            raise OutlineRepairError("接续修复须同时提供前轮补丁和复审")
        source = record["source"]
        conflicts = record["review"]["contradictions"]
        lineage = {}
        if parent_repair_id and parent_review_id:
            source, conflicts, parent_hash = self._failed_parent(workspace_id,
                candidate_id=candidate_id, parent_repair_id=parent_repair_id, parent_review_id=parent_review_id)
            lineage = {"parent_repair_id": parent_repair_id, "parent_review_id": parent_review_id,
                       "parent_review_hash": parent_hash}
        if (not conflicts or sorted(set(conflict_indexes)) != list(range(len(conflicts)))
                or len(conflict_indexes) != len(conflicts)):
            raise OutlineRepairError("需选择本次审核的全部硬矛盾，不能遗漏未修复冲突")
        if not direction.strip() or len(direction) > 1200:
            raise OutlineRepairError("请说明全部冲突的修复方向，且不超过 1200 字")
        quotes = [quote for conflict in conflicts for quote in conflict["source_quotes"]]
        admitted = allowed_failed_candidate_fields(source, quotes, allowed_paths)
        for conflict in conflicts:
            if not any(
                quote["path"] == path or quote["path"].startswith(path + ".")
                for quote in conflict["source_quotes"] for path in admitted
            ):
                raise OutlineRepairError("每条硬矛盾至少选择一处被引用的可修改来源")
        prompt = load_prompt_template(self.prompts_dir / "outline_failed_candidate_repair_v1.md")
        task = {"source_hash": digest(source), "conflicts": conflicts,
                "allowed_fields": admitted, "direction": direction.strip(),
                "complete_outline": source}
        messages = [Message(role="system", content=prompt), Message(role="user", content=render_packet(
            build_packet(stage="outline_consistency_validator", data=task,
                         original_task="修复来源绑定的失败大纲候选；全部硬矛盾须消除。"), "json"))]
        job = {"messages": [m.model_dump() for m in messages],
               "messages_hash": digest([m.model_dump() for m in messages]),
               "input_upper_bound": conservative_rendered_input_tokens(messages)}
        plan: dict[str, Any] = {
            "version": "outline-failed-candidate-repair-v1",
            "failed_candidate_id": candidate_id,
            "failed_candidate_hash": digest(record),
            "source_hash": digest(source), "section": "multiple", "allowed_fields": admitted,
            "direction": direction.strip(), "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(),
            "job": job, "physical_request_cap": 1,
            **self._patch_generation_policy(source, legacy_cost_cap=2), **lineage,
        }
        plan["request_hash"] = digest(plan)
        repair_id = "repair_" + plan["request_hash"]
        directory = self._directory(workspace_id, repair_id)
        with self._lock(directory):
            if (directory / "plan.json").exists():
                frozen, _ = self._load(directory)
                if frozen != plan:
                    raise OutlineRepairError("同一失败候选修复计划已变化")
            else:
                _atomic_write_json(directory / "plan.json", plan)
                _atomic_write_json(directory / "state.json", {"status": "pending"})
        return self.read(workspace_id, repair_id)

    def read(self, workspace_id: str, repair_id: str) -> dict:
        directory = self._directory(workspace_id, repair_id)
        plan, state = self._load(directory)
        data = HandoffPacket.model_validate_json(plan["job"]["messages"][1]["content"]).data
        conflicts = data.get("conflicts", [data["conflict"]] if "conflict" in data else [])
        return {"repair_id": repair_id, "request_hash": plan["request_hash"], "source_hash": plan["source_hash"],
                "parent_repair_id": plan.get("parent_repair_id"),
                "parent_review_id": plan.get("parent_review_id"), "source_conflicts": conflicts,
                "section": plan["section"], "allowed_fields": plan["allowed_fields"],
                "dependent_sections": (list(DEPENDENTS[plan["section"]])
                                       if plan["section"] in DEPENDENTS
                                       else ["world", "characters", "plot", "volume_plans", "mini_plan"]),
                "status": state["status"],
                "review_id": state.get("review_id"), "error": state.get("error"),
                "proposal": state.get("proposal"), "candidate_hash": state.get("candidate_hash"),
                "authorization_present": (directory / "authorization.json").exists(),
                "physical_request_cap": plan["physical_request_cap"], "cost_cap_cny": plan["cost_cap_cny"]}

    def _assert_source(self, workspace_id: str, plan: dict) -> dict:
        source = self._source(workspace_id, plan)
        if digest(source) != plan["source_hash"]:
            raise OutlineRepairError("原大纲已变化，冻结补丁不得执行或写回")
        prompt_name = ("outline_failed_candidate_repair_v1.md"
                       if plan["version"] == "outline-failed-candidate-repair-v1"
                       else "outline_targeted_repair_v1.md")
        prompt = load_prompt_template(self.prompts_dir / prompt_name)
        if hashlib.sha256(prompt.encode()).hexdigest() != plan["prompt_hash"]:
            raise OutlineRepairError("修复提示词版本已变化")
        return source

    def _frozen_source(self, plan: dict) -> dict:
        try:
            packet = HandoffPacket.model_validate_json(plan["job"]["messages"][1]["content"])
            source = packet.data["complete_outline"]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise OutlineRepairError("冻结原文缺失") from exc
        if not isinstance(source, dict):
            raise OutlineRepairError("冻结原文不是完整大纲对象")
        if digest(source) != plan["source_hash"]:
            try:
                request = UserNarrativeRequest.model_validate(packet.data.get("user_request")).model_dump(mode="json")
            except ValueError as exc:
                raise OutlineRepairError("冻结用户原文缺失或损坏") from exc
            # Canonical sources have their full request at the source root;
            # failed candidates retain raw model objects. Restore only these
            # two lossless encodings and accept solely the original digest.
            if source.get("user_request") == {"data_path": "/data/user_request"}:
                canonical_source = {**source, "user_request": request}
                if digest(canonical_source) == plan["source_hash"]:
                    return canonical_source
            def restore(value):
                if value == {"data_path": "/data/user_request"}:
                    return json.loads(json.dumps(request))
                if value == {"data_path": "/data/user_request/premise"}:
                    return request["premise"]
                if isinstance(value, dict):
                    return {key: restore(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [restore(item) for item in value]
                return value
            source = restore(source)
            if digest(source) != plan["source_hash"]:
                raise OutlineRepairError("冻结原文指纹不符")
        return source

    def _stage_response(self, directory: Path, plan: dict, source: dict, response: LLMResponse) -> dict:
        try:
            if response.finish_reason != "stop":
                raise OutlineRepairError("响应未正常完成")
            if plan["version"] == "outline-failed-candidate-repair-v1":
                failed_proposal = FailedCandidatePatchProposal.model_validate_json(response.content)
                candidate = apply_failed_candidate_proposal(
                    source, source_hash=plan["source_hash"],
                    allowed=plan["allowed_fields"], proposal=failed_proposal)
                proposal_data = failed_proposal.model_dump(mode="json")
            else:
                standard_proposal = PatchProposal.model_validate_json(response.content)
                candidate, section = apply_proposal(source, source_hash=plan["source_hash"],
                    allowed=plan["allowed_fields"], proposal=standard_proposal)
                if section != plan["section"]:
                    raise OutlineRepairError("补丁越过冻结大纲段")
                proposal_data = standard_proposal.model_dump(mode="json")
            _atomic_write_json(directory / "candidate.json", candidate)
            state = {"status": "staged", "proposal": proposal_data,
                     "candidate_hash": digest(candidate)}
        except (ValueError, ValidationError) as exc:
            state = {"status": "invalid", "error": f"补丁结构或边界不合格：{exc}"[:300]}
        _atomic_write_json(directory / "state.json", state)
        return state

    def _no_dispatched_patch(self, directory: Path) -> bool:
        """Only a valid empty ledger proves tracked transport has not begun.

        Physical transport starts after its observer persists the attempt. A
        missing/corrupt ledger or any row is therefore never a replay licence.
        """
        ledger = directory / "attempts.sqlite3"
        if not ledger.is_file() or (directory / "response.json").is_file():
            return False
        engine = create_engine(f"sqlite:///file:{ledger}?mode=ro&uri=true")
        try:
            with Session(engine) as session:
                return session.scalar(select(AttemptLedgerORM.attempt_id).limit(1)) is None
        finally:
            engine.dispose()

    def _recover_response(self, directory: Path, plan: dict) -> dict:
        if not (directory / "response.json").is_file() or not (directory / "attempts.sqlite3").is_file():
            raise OutlineRepairError("请求结果未知，禁止再次扣费；先核对账本")
        engine = create_engine(f"sqlite:///{directory / 'attempts.sqlite3'}")
        try:
            with Session(engine) as session:
                attempts = list(session.scalars(select(AttemptLedgerORM)))
            if len(attempts) != 1 or attempts[0].status != "succeeded":
                raise OutlineRepairError("请求账本未确认成功，禁止重放")
        finally:
            engine.dispose()
        try:
            response = LLMResponse.model_validate_json((directory / "response.json").read_text(encoding="utf-8"))
        except ValueError:
            state = {"status": "invalid", "error": "已保存响应损坏，不能当作通过或再次付费调用"}
            _atomic_write_json(directory / "state.json", state)
            return state
        if (directory / "response.violation.json").is_file():
            state = {"status": "invalid", "error": "已保存响应违反 JSON 合同，不得恢复为通过"}
            _atomic_write_json(directory / "state.json", state)
            return state
        return self._stage_response(directory, plan, self._frozen_source(plan), response)

    def render_runtime(self, directory: Path, plan: dict, *, client_factory=None):
        config_path = directory / "config.yaml"
        manifest = load_run_model_manifest_catalog(config_path).active_manifest
        if manifest is None or manifest.model_revision != "DeepSeek-V4.1-Flash":
            raise OutlineRepairError("修复模型版本不匹配")
        router = build_model_router(load_writer_profiles_config(config_path), config_path=config_path, client_factory=client_factory)
        binding = getattr(router, "run_model_manifest_binding", None)
        if (binding is None or binding.selection_test.max_physical_requests != plan["physical_request_cap"]
                or binding.selection_test.max_configured_rmb != Decimal(plan["cost_cap_cny"])):
            raise OutlineRepairError("独立模型调用上限与冻结计划不符")
        target = router.route("outline_consistency_validator")
        profile = target.profile
        if (profile.model != "deepseek-flash" or profile.cost_currency != "CNY"
                or (profile.base_url or "").rstrip("/") not in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}
                or getattr(target.client, "handoff_format", None) != "json"
                or profile.cost_per_1k_input != .002 or profile.cost_per_1k_output != .008):
            raise OutlineRepairError("修复模型或价格与冻结边界不符")
        if ("provider_input_guard_tokens" in plan
                and target.complete_kwargs.get("input_token_budget") != plan["provider_input_guard_tokens"]):
            raise OutlineRepairError("补丁输入额度没有继承活动上游配置")
        failed_candidate = plan["version"] == "outline-failed-candidate-repair-v1"
        kwargs = prepare_strict_json_output(client=target.client, model=profile.model,
            output_model=FailedCandidatePatchProposal if failed_candidate else PatchProposal,
            schema_name="outline_failed_candidate_repair" if failed_candidate else "outline_targeted_repair",
            complete_kwargs=target.complete_kwargs)
        if (kwargs.get("max_tokens", kwargs.get("max_completion_tokens")) != plan["output_cap"]
                or kwargs.get("reasoning_effort") != plan["reasoning_effort"] or "temperature" in kwargs
                or kwargs.get("thinking") != {"type": "enabled"}):
            raise OutlineRepairError("输出和推理配置不匹配")
        messages = [Message(**item) for item in plan["job"]["messages"]]
        final = getattr(target.client, "messages_for_cost_reserve")(messages, {**kwargs, "model": profile.model})
        reserve = (conservative_rendered_input_tokens([Message(**item.model_dump()) for item in final]) * 2
                   + plan["output_cap"] * 8) / 1_000_000
        if reserve > plan["cost_cap_cny"]:
            raise OutlineRepairError("请求预留费用超出本次上限")
        wire = {"request_hash": plan["request_hash"], "reserve_cny": reserve,
                "messages": [[item.model_dump() for item in final]]}
        return router, binding, messages, kwargs, [item.model_dump() for item in final], wire

    def _authorization(self, directory: Path, plan: dict, wire: dict) -> None:
        try:
            auth = json.loads((directory / "authorization.json").read_text())
            config_hash = hashlib.sha256((directory / "config.yaml").read_bytes()).hexdigest()
        except (OSError, ValueError) as exc:
            raise OutlineRepairError("尚无这一次修复模型调用的独立授权") from exc
        if (auth.get("request_hash") != plan["request_hash"] or auth.get("config_hash") != config_hash
                or auth.get("wire_hash") != digest(wire) or auth.get("cost_cap_cny") != plan["cost_cap_cny"]
                or auth.get("physical_request_cap") != plan["physical_request_cap"]
                or not str(auth.get("approval_reference", "")).strip()):
            raise OutlineRepairError("付费授权范围或最终请求不匹配")

    def preflight_patch(self, workspace_id: str, repair_id: str, request_hash: str) -> None:
        directory = self._directory(workspace_id, repair_id)
        plan, state = self._load(directory)
        if request_hash != plan["request_hash"] or state["status"] not in {"pending", "started"}:
            raise OutlineRepairError("修复请求不是当前可启动版本")
        self._assert_source(workspace_id, plan)
        if state["status"] == "started":
            if self._no_dispatched_patch(directory):
                # Validate the same frozen source/config/auth again below.
                _, _, _, _, _, wire = self.render_runtime(directory, plan)
                self._authorization(directory, plan, wire)
                return
            if not (directory / "response.json").is_file() or not (directory / "attempts.sqlite3").is_file():
                raise OutlineRepairError("请求结果未知，禁止重放；请先核对账本")
            return
        if not (directory / "authorization.json").is_file() or not (directory / "config.yaml").is_file():
            raise OutlineRepairError("尚无这一次修复模型调用的独立授权与配置")
        _, _, _, _, _, wire = self.render_runtime(directory, plan)
        self._authorization(directory, plan, wire)

    def run_patch(self, workspace_id: str, repair_id: str, request_hash: str, task_id: str) -> dict:
        directory = self._directory(workspace_id, repair_id)
        with self._lock(directory):
            plan, state = self._load(directory)
            if request_hash != plan["request_hash"]:
                raise OutlineRepairError("页面所见修复版本已过期")
            if state["status"] in {"staged", "committed", "invalid"}:
                return self.read(workspace_id, repair_id)
            if state["status"] == "started":
                if not self._no_dispatched_patch(directory):
                    self._recover_response(directory, plan)
                    return self.read(workspace_id, repair_id)
                recovery = directory / "pre-dispatch-recoveries"
                recovery.mkdir(exist_ok=True)
                _atomic_write_json(recovery / f"{task_id}.json", {
                    "previous_status": "started", "provider_attempts": 0,
                    "request_hash": request_hash, "task_id": task_id,
                })
            source = self._assert_source(workspace_id, plan)
            router, binding, messages, kwargs, final, wire = self.render_runtime(directory, plan)
            self._authorization(directory, plan, wire)
            engine, factory = _initialize_attempt_ledger(directory / "attempts.sqlite3")
            scope = dict(workspace_id=workspace_id, task_id=task_id, activity_scope="selection_test",
                provider_call_cap=1, run_model_manifest_token=binding.token,
                selection_test_manifest_token=binding.selection_test.token,
                configured_cost_currency="CNY", configured_cost_cap=Decimal(plan["cost_cap_cny"]))
            try:
                with bind_attempt_scope(**scope):
                    tracked = wrap_router_with_attempt_tracking(router, workspace_id=workspace_id, session_factory=factory)
                    target = tracked.route("outline_consistency_validator")
                    if [item.model_dump() for item in getattr(target.client, "messages_for_cost_reserve")(messages, {**kwargs, "model": target.profile.model})] != final:
                        raise OutlineRepairError("最终请求与付费预留不一致")
                    raise_if_cancelled()
                    state = {"status": "started"}
                    _atomic_write_json(directory / "state.json", state)
                    violation = False
                    with bound_contextvars(workspace_id=None):
                        try:
                            response = target.client.complete(messages=messages, model=target.profile.model, **kwargs)
                        except LocalJsonResponseViolation as exc:
                            response = exc.response
                            violation = True
                            _atomic_write_json(directory / "response.violation.json", {"invalid": True})
                    _atomic_write_json(directory / "response.json", response.model_dump(mode="json"))
                    if violation:
                        _atomic_write_json(directory / "state.json", {"status": "invalid", "error": "响应 JSON 合同不合格"})
                    else:
                        self._stage_response(directory, plan, source, response)
            finally:
                engine.dispose()
        return self.read(workspace_id, repair_id)

    def review_material(self, workspace_id: str, repair_id: str) -> tuple[dict, dict, str]:
        directory = self._directory(workspace_id, repair_id)
        plan, state = self._load(directory)
        if state["status"] not in {"staged", "committed"}:
            raise OutlineRepairError("有效补丁尚未暂存")
        source = self._frozen_source(plan) if state["status"] == "committed" else self._assert_source(workspace_id, plan)
        try:
            candidate = json.loads((directory / "candidate.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OutlineRepairError("候选补丁或响应缺失") from exc
        if plan["version"] == "outline-failed-candidate-repair-v1":
            proposal = FailedCandidatePatchProposal.model_validate(state["proposal"])
            expected = apply_failed_candidate_proposal(
                source, source_hash=plan["source_hash"],
                allowed=plan["allowed_fields"], proposal=proposal)
        else:
            standard_proposal = PatchProposal.model_validate(state["proposal"])
            expected, _ = apply_proposal(source, source_hash=plan["source_hash"],
                                         allowed=plan["allowed_fields"], proposal=standard_proposal)
        if candidate != expected or digest(candidate) != state["candidate_hash"]:
            raise OutlineRepairError("暂存候选与已校验补丁不一致")
        if (state["status"] == "committed" and plan["version"] != "outline-failed-candidate-repair-v1"
                and self._source(workspace_id) != candidate):
            raise OutlineRepairError("提交后大纲已变化，旧增量复审不得复用")
        return source, candidate, plan["source_hash"]

    def _candidate_from_record(self, directory: Path, plan: dict, state: dict) -> tuple[dict, dict]:
        source = self._frozen_source(plan)
        try:
            candidate = json.loads((directory / "candidate.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError) as exc:
            raise OutlineRepairError("已暂存的补丁记录损坏") from exc
        if plan["version"] == "outline-failed-candidate-repair-v1":
            proposal = FailedCandidatePatchProposal.model_validate(state["proposal"])
            expected = apply_failed_candidate_proposal(
                source, source_hash=plan["source_hash"],
                allowed=plan["allowed_fields"], proposal=proposal)
        else:
            standard_proposal = PatchProposal.model_validate(state["proposal"])
            expected, section = apply_proposal(source, source_hash=plan["source_hash"],
                allowed=plan["allowed_fields"], proposal=standard_proposal)
            if section != plan["section"]:
                raise OutlineRepairError("已暂存补丁越过冻结大纲段")
        if expected != candidate or digest(candidate) != state["candidate_hash"]:
            raise OutlineRepairError("已暂存候选与冻结请求不一致")
        return source, candidate

    def commit(self, workspace_id: str, repair_id: str, review_id: str) -> dict:
        from src.services.outline_review_diagnostic_service import OutlineReviewDiagnosticService
        directory = self._directory(workspace_id, repair_id)
        with self._lock(directory):
            plan, state = self._load(directory)
            if plan["version"] == "outline-failed-candidate-repair-v1":
                return self._commit_failed_candidate(
                    workspace_id, repair_id, review_id, directory, plan, state)
            if state["status"] == "committed":
                if state.get("review_id") != review_id:
                    raise OutlineRepairError("此修复已由另一份复审提交")
                _, candidate = self._candidate_from_record(directory, plan, state)
                if self._source(workspace_id) != candidate:
                    raise OutlineRepairError("修复提交后大纲又发生变化，旧完成状态不能代表当前材料")
                return self.read(workspace_id, repair_id)
            source, candidate = self._candidate_from_record(directory, plan, state)
            live = self._source(workspace_id)
            if live != source and live != candidate:
                raise OutlineRepairError("原大纲有并发变化，不能提交旧候选")
            diagnostic = OutlineReviewDiagnosticService(self.workspaces, self.prompts_dir)
            review_dir = diagnostic._directory(workspace_id, review_id)
            review_plan, review_state = diagnostic._load(review_dir)
            review_version = review_plan.get("version")
            same_candidate = (review_plan.get("repair_id") == repair_id
                and review_plan.get("parent_source_hash") == plan["source_hash"]
                and review_plan.get("candidate_hash") == digest(candidate))
            full_pass = (review_version == "outline-review-repair-split-v1"
                and diagnostic.split_review_passed(review_plan, review_state)) or (review_version == "outline-review-repair-review-v1"
                and review_state["full"]["status"] == "completed"
                and not review_state["full"]["result"].get("contradictions")
                and review_state["full"]["result"].get("repair_scope") == "none")
            delta_pass = (review_version == "outline-review-repair-delta-v2"
                and review_plan.get("baseline_review_id") == plan["diagnostic_id"]
                and all(review_state[j]["status"] == "completed"
                    and review_state[j]["result"]["outcome"] == "compatible"
                    for j in ("resolution", "regression")))
            if not same_candidate or not (full_pass or delta_pass):
                raise OutlineRepairError("候选复审未通过，不能写回原大纲")
            expected_review = (diagnostic._plan_repair_delta(workspace_id, repair_id)
                if delta_pass else diagnostic._legacy_full_plan(workspace_id, "repair_review", material=candidate)
                if review_version == "outline-review-repair-review-v1"
                   and "generation" not in review_plan["jobs"][0]
                else diagnostic._plan(workspace_id,
                    "repair_review_split" if review_version == "outline-review-repair-split-v1"
                    else "repair_review", material=candidate))
            if any(review_plan[key] != expected_review[key] for key in expected_review):
                raise OutlineRepairError("复审提示词或完整材料已变化")
            if validate_arc_chapter_spans(VolumePlansOutput.model_validate(candidate["volume_plans"]).volumes):
                raise OutlineRepairError("卷规划章节跨度不合法")
            section = plan["section"]
            if any(digest(source[name]) != plan["source_section_hashes"][name] for name in plan["source_section_hashes"]):
                raise OutlineRepairError("未授权大纲段已变化")
            # A single section file is the only story write. A crash after replace leaves
            # old readiness hashes mismatched; the repair state never falsely says ready.
            # Invalidate the old approval first. If the process stops between
            # these writes, the original material remains blocked, never ready.
            readiness = self.outlines._readiness(workspace_id).model_copy(update={"ready_hashes": None})
            _atomic_write_json(self.outlines._outline_dir(workspace_id) / "readiness.json", readiness.model_dump())
            if live == source:
                _atomic_write_json(self.outlines._stage_path(workspace_id, section), candidate[section])
            _atomic_write_json(self.outlines._outline_dir(workspace_id) / "review_receipt.json", {
                "version": 2 if delta_pass else 1, "repair_id": repair_id, "review_id": review_id,
                "baseline_review_id": plan["diagnostic_id"] if delta_pass else None,
                "review_request_hash": review_plan["request_hash"],
                "source_hash": digest(candidate),
                "content_hash": digest({name: candidate[name] for name in ("world", "characters", "plot", "volume_plans")}),
            })
            _atomic_write_json(directory / "state.json", {**state, "status": "committed", "review_id": review_id})
        return self.read(workspace_id, repair_id)

    def _commit_failed_candidate(
        self, workspace_id: str, repair_id: str, review_id: str,
        directory: Path, plan: dict, state: dict,
    ) -> dict:
        from src.services.outline_review_diagnostic_service import OutlineReviewDiagnosticService

        source, candidate = self._candidate_from_record(directory, plan, state)
        sections: tuple[OutlineSection, ...] = ("world", "characters", "plot", "volume_plans")
        if state["status"] == "committed":
            if state.get("review_id") != review_id or any(
                self.outlines.read_outline(workspace_id)[name] != candidate[name] for name in sections
            ):
                raise OutlineRepairError("已提交失败候选与当前正式大纲不同")
            return self.read(workspace_id, repair_id)
        if state["status"] != "staged":
            raise OutlineRepairError("候选补丁尚未有效暂存")
        diagnostic = OutlineReviewDiagnosticService(self.workspaces, self.prompts_dir)
        review_plan, review_state = diagnostic._load(diagnostic._directory(workspace_id, review_id))
        if (review_plan.get("version") != "outline-review-repair-split-v1"
                or review_plan.get("repair_id") != repair_id
                or review_plan.get("parent_source_hash") != plan["source_hash"]
                or review_plan.get("candidate_hash") != digest(candidate)
                or not diagnostic.split_review_passed(review_plan, review_state)):
            raise OutlineRepairError("隔离候选尚无同源、完整通过的复审")
        expected = diagnostic._plan(workspace_id, "repair_review_split", material=candidate)
        if any(review_plan[key] != expected[key] for key in expected):
            raise OutlineRepairError("完整复审提示词或材料已变化")
        volumes = VolumePlansOutput.model_validate(candidate["volume_plans"])
        chars = CharactersOutput.model_validate(candidate["characters"])
        if validate_arc_chapter_spans(volumes.volumes):
            raise OutlineRepairError("修复后的章节跨度不合法")
        if (find_pending_signature_overlaps(volume_plans=volumes, characters=chars)
                or find_repeated_completed_signatures(volume_plans=volumes, characters=chars)):
            raise OutlineRepairError("修复后仍重复签署同一契约")

        # Persist the intent before any story write.  A crash may leave a
        # subset of section files replaced; retry accepts only old/target hashes.
        intent_path = directory / "commit_intent.json"
        intent = {"repair_id": repair_id, "review_id": review_id,
                  "candidate_hash": digest(candidate), "parent_hashes":
                  self.outlines.read_failed_volume_candidate(
                      workspace_id, plan["failed_candidate_id"])["parent_hashes"]}
        if intent_path.exists():
            try:
                if json.loads(intent_path.read_text(encoding="utf-8")) != intent:
                    raise OutlineRepairError("提交意图记录已变化")
            except (OSError, ValueError) as exc:
                raise OutlineRepairError("提交意图记录损坏") from exc
        else:
            self._source(workspace_id, plan)
            _atomic_write_json(intent_path, intent)
        live = self.outlines.read_outline(workspace_id)
        for name in (*sections, "mini_plan"):
            current_hash = digest(live[name]) if live[name] is not None else None
            target_hash = digest(candidate[name]) if name in sections else None
            if current_hash not in {intent["parent_hashes"][name], target_hash}:
                raise OutlineRepairError("提交中正式大纲出现第三方变化，不能恢复")
        self.outlines._invalidate_readiness(workspace_id)
        for name in sections:
            if live[name] != candidate[name]:
                _atomic_write_json(self.outlines._stage_path(workspace_id, name), candidate[name])
        if any(self.outlines.read_outline(workspace_id)[name] != candidate[name] for name in sections):
            raise OutlineRepairError("候选写回后核验失败")
        _atomic_write_json(self.outlines._outline_dir(workspace_id) / "review_receipt.json", {
            "version": 1, "repair_id": repair_id, "review_id": review_id,
            "baseline_review_id": None, "review_request_hash": review_plan["request_hash"],
            "source_hash": digest(candidate),
            "content_hash": digest({name: candidate[name] for name in sections}),
        })
        _atomic_write_json(directory / "state.json", {**state, "status": "committed", "review_id": review_id})
        return self.read(workspace_id, repair_id)
