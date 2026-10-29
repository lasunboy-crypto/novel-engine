"""选择式策划：零调用编辑、现有生成器整理、版本检查与可恢复阶段。"""
from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Any
from pydantic import BaseModel

from src.core.agents.outline_review_packets import digest
from src.core.models.authoring import (
    AuthoringActionIn, AuthoringDraft, AuthoringProposal, AuthoringSaveIn,
    AuthoringView, PlanOption,
)
from src.core.models.narrative import PlanningScope, StoryIntent, UserNarrativeRequest
from src.services.workspace_service import WorkspaceService

if TYPE_CHECKING:
    from src.services.outline_service import OutlineService


def draft_path(root: Path, workspace_id: str) -> Path:
    return root / workspace_id / "data/authoring/current.json"


def read_draft(root: Path, workspace_id: str) -> AuthoringDraft | None:
    path = draft_path(root, workspace_id)
    return AuthoringDraft.model_validate_json(path.read_text()) if path.exists() else None


def _semantic_sources(values: dict[str, Any]) -> dict[str, Any]:
    """已选内容相同的恢复不因显示版本号再次付费；生成事实不作改写。"""
    data = json.loads(json.dumps(values, ensure_ascii=False))
    world = data.get("world") or {}
    plan = (world.get("story_intent") or {}).get("authoring_plan")
    if plan:
        plan.pop("revision", None)
    return data


@contextmanager
def authoring_cache_scope(root: Path, workspace_id: str):
    from src.config import settings
    from src.services.config_service import ConfigService
    from src.core.agents.authoring_cache import completed_stage_scope
    config = ConfigService(workspaces_root=root).load_workspace_config(workspace_id)
    prompts = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(settings.prompts_dir.rglob("*.md")))).hexdigest()
    code_root = Path(__file__).resolve().parents[1]
    implementation = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(code_root.rglob("*.py")))).hexdigest()
    salt = digest({"config": config.model_dump(mode="json"), "prompts": prompts,
                   "implementation": implementation,
                   "engine": hashlib.sha256(settings.config_yaml_path.read_bytes()).hexdigest()})
    with completed_stage_scope(draft_path(root, workspace_id).parent / "completed_stages", salt):
        yield


def intent_from_draft(draft: AuthoringDraft) -> StoryIntent:
    if draft.proposal is None:
        raise ValueError("先生成故事提案")
    contract = draft.proposal.contract(draft.revision, draft.preferences)
    p = draft.preferences
    s = contract.sections
    entry = next(m.event for m in contract.milestones if m.kind == "core_entry")
    # 只从当前已选文案形成方向；旧提案的隐藏归纳不覆盖用户的新选择。
    data = dict(reader_interest=s["concept"], protagonist_start=s["protagonist"],
        personal_drive=s["protagonist"], central_pressure=s["plot"], narrative_tone=s["style"],
        opening_orientation=entry, scope_outcome=s["ending"],
        progression_design=dict(story_engine=s["concept"], opening_development=entry,
            stage_progression=s["plot"], payoff_rhythm=p.payoff,
            time_and_detail="按已选开局范围展开真正经历；普通准备和重复试探可概述或跨过。"),
        scope_experience=dict(core_experience=s["concept"], entry_conditions=entry,
            experience_after_entry=s["plot"], subsequent_use=s["ending"]))
    return StoryIntent.model_validate({**data, "version": 7,
        "authoring_plan": contract,
        "planning_scope": PlanningScope(mode="whole_book", total_volumes=p.total_volumes,
            chapters_per_volume=p.chapters_per_volume, reference_chapter_words=p.reference_chapter_words),
        "user_request": UserNarrativeRequest.from_source(premise=draft.premise,
            genre=draft.genre, reading_preferences=contract.reading_preferences())})


@contextmanager
def authoring_lock(root: Path, workspace_id: str):
    from src.services.workspace_operation_guard import acquire_workspace_file_lock
    workspace_lock = acquire_workspace_file_lock(root, workspace_id, mode="shared")
    path = draft_path(root, workspace_id).parent
    path.mkdir(parents=True, exist_ok=True)
    with (path / ".lock").open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            workspace_lock.close()
            raise ValueError("规划正在更新，请等待当前操作完成") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            workspace_lock.close()


class AuthoringService:
    def __init__(self, workspaces: WorkspaceService, outline_factory: Callable[[], OutlineService]):
        self.workspaces = workspaces
        self.outline_factory = outline_factory
        self.root = workspaces.workspaces_root

    def _editable(self, workspace_id: str) -> None:
        meta = self.workspaces.info(workspace_id)
        committed = False
        path = self.workspaces._paths(workspace_id).db_path
        if path.exists():
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
                row = db.execute("SELECT outline_status FROM workspaces WHERE workspace_id = ?", (workspace_id,)).fetchone()
                committed = row is not None and row[0] == "committed"
        if committed or meta.completed_chapters:
            raise ValueError("已提交大纲不能由策划草稿静默覆盖，请保留当前书的正文与记忆")

    def read(self, workspace_id: str) -> AuthoringView:
        self.workspaces.info(workspace_id)
        draft = read_draft(self.root, workspace_id)
        view = AuthoringView(draft=draft)
        if draft is None:
            return view
        if draft.proposal:
            view.contract = draft.proposal.contract(draft.revision, draft.preferences)
        view.history = sorted(int(p.stem) for p in draft_path(self.root, workspace_id).parent.glob("history/*.json"))
        job = draft_path(self.root, workspace_id).parent / "task.json"
        if job.exists():
            view.active_task_id = json.loads(job.read_text())["task_id"]
        for stage in ("world", "characters", "plot"):
            path = draft_path(self.root, workspace_id).parent / "prepared" / f"{stage}.json"
            if draft.status != "draft" and path.exists():
                from src.core.models.outline import WorldOutput, CharactersOutput, PlotOutput
                classes: dict[str, type[BaseModel]] = {"world": WorldOutput, "characters": CharactersOutput, "plot": PlotOutput}
                cls = classes[stage]
                setattr(view, stage, cls.model_validate_json(path.read_text()))
        return view

    def _persist(self, workspace_id: str, draft: AuthoringDraft) -> None:
        from src.services.outline_service import _atomic_write_json
        path = draft_path(self.root, workspace_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            old = AuthoringDraft.model_validate_json(path.read_text())
            archive = path.parent / "history" / f"{old.revision}.json"
            archive.parent.mkdir(exist_ok=True)
            if old.revision != draft.revision and not archive.exists():
                _atomic_write_json(archive, old.model_dump(mode="json"))
                snapshot = path.parent / "prepared_history" / str(old.revision)
                for stage in ("world", "characters", "plot"):
                    source = path.parent / "prepared" / f"{stage}.json"
                    if source.exists() and digest(json.loads(source.read_text())) == old.prepared_hashes.get(stage + "_artifact"):
                        snapshot.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, snapshot / source.name)
        _atomic_write_json(path, draft.model_dump(mode="json"))

    def _current(self, workspace_id: str, expected_revision: int) -> AuthoringDraft:
        self._editable(workspace_id)
        draft = read_draft(self.root, workspace_id)
        if draft is None or draft.revision != expected_revision:
            raise ValueError("规划已更新，请刷新后在最新版本继续")
        return draft

    def save(self, workspace_id: str, body: AuthoringSaveIn) -> AuthoringView:
        self._editable(workspace_id)
        with authoring_lock(self.root, workspace_id):
            draft = read_draft(self.root, workspace_id)
            if draft is None:
                if body.expected_revision != 0:
                    raise ValueError("规划不存在")
                if any((self.root / workspace_id / f"data/outline/{s}.json").exists()
                       for s in ("world", "characters", "plot", "volume_plans", "mini_plan")):
                    raise ValueError("旧书不能自动迁入新策划流程")
                meta = self.workspaces.info(workspace_id)
                draft = AuthoringDraft(premise=body.premise or meta.premise,
                                       genre=body.genre or meta.genre)
                draft.preferences.style_template = meta.style_template
            else:
                draft = self._current(workspace_id, body.expected_revision).model_copy(deep=True)
                draft.revision += 1
            previous = read_draft(self.root, workspace_id)
            if body.premise is not None:
                if draft.proposal and body.premise != draft.premise:
                    raise ValueError("更换初始创意请另建书；本页可调整现有提案")
                draft.premise = body.premise
            if body.genre is not None:
                draft.genre = body.genre
            if body.preferences is not None:
                draft.preferences = body.preferences
            if draft.proposal:
                sections = {s.key: s for s in draft.proposal.sections}
                for key, selected in body.selections.items():
                    if selected not in {o.option_id for o in sections[key].options}:
                        raise ValueError("选项不存在")
                    sections[key].selected = selected
                for key, content in body.custom_sections.items():
                    section = sections[key]
                    section.options = [o for o in section.options if o.option_id != "custom"][:3]
                    section.options.append(PlanOption(option_id="custom", label="自定义", content=content))
                    section.selected = "custom"
                entities = {e.entity_id: e for e in draft.proposal.entities}
                for entity_key, name in body.names.items():
                    if entity_key not in entities or not name.strip():
                        raise ValueError("名称或引用无效")
                    entities[entity_key].name = name.strip()
            elif body.selections or body.names or body.custom_sections:
                raise ValueError("先生成故事提案")
            draft = AuthoringDraft.model_validate(draft.model_dump(mode="json"))
            if previous is not None:
                old = previous.model_dump(exclude={"revision"})
                if draft.model_dump(exclude={"revision"}) == old:
                    return self.read(workspace_id)
            draft.status = "draft"
            draft.pending_changes = draft.proposal is not None
            draft.last_action = "选择已保存；整理后更新完整规划"
            self._persist(workspace_id, draft)
            from src.core.workspace.meta import write_meta
            meta = self.workspaces.info(workspace_id)
            if not meta.authoring_enabled:
                write_meta(self.workspaces._paths(workspace_id).meta_json, meta.model_copy(update={"authoring_enabled": True}))
        return self.read(workspace_id)

    def act(self, workspace_id: str, body: AuthoringActionIn) -> AuthoringView:
        with authoring_lock(self.root, workspace_id), authoring_cache_scope(self.root, workspace_id):
            draft = self._current(workspace_id, body.expected_revision).model_copy(deep=True)
            if body.action == "confirm":
                if draft.status != "prepared":
                    raise ValueError("先整理并预览完整规划")
                self.assert_prepared(workspace_id, draft)
                self._publish_prepared(workspace_id, draft)
                draft.status = "confirmed"
            elif body.action == "restore":
                path = draft_path(self.root, workspace_id).parent / "history" / f"{body.restore_revision}.json"
                if not path.exists():
                    raise ValueError("历史版本不存在")
                restored = AuthoringDraft.model_validate_json(path.read_text())
                restored.revision = draft.revision + 1
                restored.status = "draft"
                restored.pending_changes = True
                snapshot = path.parent.parent / "prepared_history" / str(body.restore_revision)
                for stage in ("world", "characters", "plot"):
                    source = snapshot / f"{stage}.json"
                    if source.exists() and digest(json.loads(source.read_text())) == restored.prepared_hashes.get(stage + "_artifact"):
                        target = draft_path(self.root, workspace_id).parent / "prepared" / source.name
                        target.parent.mkdir(exist_ok=True)
                        from src.services.outline_service import _atomic_write_json
                        _atomic_write_json(target, json.loads(source.read_text()))
                draft = restored
            elif body.action in {"propose", "revise"}:
                if (body.action == "propose") == (draft.proposal is not None):
                    raise ValueError("已有提案请使用局部调整；没有提案请先生成")
                draft = self._propose(workspace_id, draft, body.direction)
            else:
                draft = self._prepare(workspace_id, draft)
            draft.last_action = body.action
            self._persist(workspace_id, draft)
        return self.read(workspace_id)

    def _propose(self, workspace_id: str, draft: AuthoringDraft, direction: str) -> AuthoringDraft:
        from src.core.agents.outline_builder import _load_delta_stage_prompt, _run_stage
        from src.api.logging import bind_workspace_context
        outline = self.outline_factory()
        router = outline._build_router(workspace_id)
        if not outline._experimental(workspace_id):
            raise ValueError("选择式规划需要已接通JSON的实验路由；请先在工作区配置中选择")
        data = {"premise": draft.premise, "genre": draft.genre,
                "preferences": draft.preferences, "current_proposal": draft.proposal,
                "direction": direction}
        with bind_workspace_context(workspace_id):
            result = _run_stage(stage="world", output_model=AuthoringProposal,
                system_text=_load_delta_stage_prompt("authoring_proposal_v1.md", outline._prompts_dir),
                user_text="生成或根据本轮选择批量调整当前一份小说提案。", handoff_data=data,
                router=router, client=outline._client, model=outline._model,
                max_tokens=393216, temperature=0.7,
                timing_context={"workspace_id": workspace_id},
                timing_fields={"substage": "authoring_proposal"})
        draft.proposal = AuthoringProposal.model_validate(result.model_dump(mode="json"))
        draft.revision += 1
        draft.status = "draft"
        draft.pending_changes = False
        return draft

    def _prepare(self, workspace_id: str, draft: AuthoringDraft) -> AuthoringDraft:
        from src.config import settings
        from src.core.agents import outline_builder as builder
        from src.api.logging import bind_workspace_context
        from src.core.models.outline import CharactersOutput, PlotOutput, WorldOutput
        from src.services.outline_service import _atomic_write_json
        intent = intent_from_draft(draft)
        outline = self.outline_factory()
        router = outline._build_router(workspace_id)
        if not outline._experimental(workspace_id):
            raise ValueError("选择式规划需要实验JSON路由")
        # 缓存匹配实际本轮方向、模型路由及提示版本；不以旧成功冒充新验证。
        prompt_root = outline._prompts_dir
        if prompt_root is None:
            from src.config import settings
            prompt_root = settings.prompts_dir
        prompt_hash = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(prompt_root.rglob("*.md")))).hexdigest()
        profile = router.select_profile("outline_builder") if router is not None else None
        from src.services.config_service import ConfigService
        configured = ConfigService(workspaces_root=self.root).load_workspace_config(workspace_id).model_dump(mode="json")
        intent_key = intent.model_dump(mode="json")
        intent_key["authoring_plan"].pop("revision")
        fingerprint = digest({"intent": intent_key, "profile": profile.model_dump(mode="json") if profile else outline._model,
                              "prompts": prompt_hash, "configuration": configured,
                              "engine": hashlib.sha256(settings.config_yaml_path.read_bytes()).hexdigest()})
        common = dict(premise=draft.premise, genre=draft.genre, router=router,
                      client=outline._client, model=outline._model, prompts_dir=outline._prompts_dir,
                      timing_context={"workspace_id": workspace_id})
        values: dict[str, Any] = {}
        with bind_workspace_context(workspace_id):
            for stage, cls in (("world", WorldOutput), ("characters", CharactersOutput), ("plot", PlotOutput)):
                path = draft_path(self.root, workspace_id).parent / "prepared" / f"{stage}.json"
                path.parent.mkdir(exist_ok=True)
                key = digest({"request": fingerprint, "stage": stage, "parents": _semantic_sources(values)})
                cache = draft.prepared_hashes.get(stage)
                value = json.loads(path.read_text()) if path.exists() else None
                if value is None or cache != digest({"key": key, "value": value}):
                    result: BaseModel
                    if stage == "world":
                        result = builder.build_world(**common, story_intent=intent,
                            total_volumes=draft.preferences.total_volumes,
                            chapters_per_volume=draft.preferences.chapters_per_volume)
                    elif stage == "characters":
                        result = builder.build_characters(**common, world=WorldOutput.model_validate(values["world"]))
                    else:
                        result = builder.build_plot(**common, world=WorldOutput.model_validate(values["world"]),
                            characters=CharactersOutput.model_validate(values["characters"]))
                    value = result.model_dump(mode="json")
                    _atomic_write_json(path, value)
                if stage == "world":
                    value["story_intent"] = intent.model_dump(mode="json")
                    _atomic_write_json(path, value)
                cls.model_validate(value)
                values[stage] = value
                draft.prepared_hashes[stage] = digest({"key": key, "value": value})
                draft.prepared_hashes[stage + "_artifact"] = digest(value)
                self._persist(workspace_id, draft)
        draft.status = "prepared"
        draft.pending_changes = False
        return draft

    def assert_prepared(self, workspace_id: str, draft: AuthoringDraft) -> None:
        for stage in ("world", "characters", "plot"):
            path = draft_path(self.root, workspace_id).parent / "prepared" / f"{stage}.json"
            if not path.exists() or digest(json.loads(path.read_text())) != draft.prepared_hashes.get(stage + "_artifact"):
                raise ValueError("完整规划来源已变化，请重新整理后确认")

    def _publish_prepared(self, workspace_id: str, draft: AuthoringDraft) -> None:
        from src.core.workspace.meta import write_meta
        from src.services.outline_service import _atomic_write_json
        base = draft_path(self.root, workspace_id).parent
        target = self.root / workspace_id / "data/outline"
        from src.services.config_service import ConfigService
        config = ConfigService(workspaces_root=self.root)
        override = config.load_generation_override(workspace_id)
        override.update(word_count_policy_version=2, word_count_min=draft.preferences.reference_chapter_words,
                        word_count_max=draft.preferences.reference_chapter_words)
        config.save_generation_config(workspace_id, override)
        # 保留旧产物和检查凭据；新规划不能沿用它们冒充新来源已通过。
        already_published = all((target / f"{stage}.json").exists() and (target / f"{stage}.json").read_bytes() == (base / "prepared" / f"{stage}.json").read_bytes() for stage in ("world", "characters", "plot"))
        if target.exists() and not already_published:
            archive = base / "superseded" / str(draft.revision)
            archive.mkdir(parents=True, exist_ok=True)
            if (archive / "outline").exists():
                raise ValueError("该版本已经发布，请刷新确认当前状态")
            shutil.move(str(target), str(archive / "outline"))
        target.mkdir(parents=True, exist_ok=True)
        for stage in ("world", "characters", "plot"):
            _atomic_write_json(target / f"{stage}.json", json.loads((base / "prepared" / f"{stage}.json").read_text()))
        meta = self.workspaces.info(workspace_id)
        updated = meta.model_copy(update={"display_name": draft.proposal.title if draft.proposal else meta.display_name,
            "genre": draft.genre, "default_voice": draft.preferences.voice,
            "style_template": draft.preferences.style_template,
            "total_chapters": sum(draft.preferences.counts())})
        guide = self.root / workspace_id / "prompts/style_guide.md"
        if meta.style_template != draft.preferences.style_template:
            if guide.exists():
                backup = base / "superseded" / str(draft.revision)
                backup.mkdir(parents=True, exist_ok=True)
                shutil.move(str(guide), str(backup / "style_guide.md"))
            self.workspaces._write_style_guide(self.workspaces._paths(workspace_id), draft.preferences.style_template)
        write_meta(self.workspaces._paths(workspace_id).meta_json, updated)
        path = self.workspaces._paths(workspace_id).db_path
        if path.exists():
            with sqlite3.connect(path) as db:
                db.execute("UPDATE workspaces SET display_name=?, genre=?, total_chapters=?, outline_status='not_started' WHERE workspace_id=?",
                    (updated.display_name, updated.genre, updated.total_chapters, workspace_id))


def generate_confirmed_outline(outline: OutlineService, workspace_id: str, **kwargs):
    """复用整理好的三段；整段期间与策划编辑互斥。"""
    root = outline._workspace_service.workspaces_root
    with authoring_lock(root, workspace_id), authoring_cache_scope(root, workspace_id):
        draft = read_draft(root, workspace_id)
        if draft is None or draft.status != "confirmed":
            raise ValueError("请先在小说规划中确认，再生成全书大纲")
        AuthoringService(outline._workspace_service, lambda: outline).assert_prepared(workspace_id, draft)
        world = outline.read_outline(workspace_id)["world"]
        if not world or world.get("story_intent") != intent_from_draft(draft).model_dump(mode="json"):
            raise ValueError("已确认方向与生成来源不一致，请重新整理")
        expected = {"premise": draft.premise, "genre": draft.genre,
                    "total_volumes": draft.preferences.total_volumes,
                    "chapters_per_volume": draft.preferences.chapters_per_volume, "planning_mode": "whole_book",
                    "tension_template": draft.preferences.tension_template}
        if any(kwargs.get(k, "whole_book" if k == "planning_mode" else None) != value for k, value in expected.items()):
            raise ValueError("生成参数与已确认小说规划不一致")
        return outline._generate_outline(workspace_id, **kwargs)
