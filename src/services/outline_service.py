"""Outline 服务(P5-B-04 引入)。

对齐 API_CONTRACT §4.2 关键流程 1 "创建新书并生成大纲"+ §5.3 ``task.{id}.progress``
频道 6 stage 字面 ``world | characters | plot | volume_plans | mini_plan | done``
(v1.0.x wave 4 session 1 起从 5 段扩到 6 段加 mini_plan)+ PROJECT_SPEC §4.1
[HARD] 副本类剧情 ≥ 3 章(嵌入 ``commit`` gate)+ p5_plan.md §8 outline:commit
gate 集中点约定。

5 个公共方法
============

- ``generate_outline(workspace_id, premise, ...)`` — 调
  ``outline_builder.build_outline(...)`` 4 段串行 + 每段完成 WS publish
  + 落 4 段 SSOT JSON 文件;返 ``FullOutline``。
- ``regenerate_section(workspace_id, section, ...)`` — 单段重生,从已落盘
  4 文件读取上游 context(不重跑上游);返新单段产出 + 覆写对应 JSON。
- ``read_outline(workspace_id)`` — 读 4 个 JSON 文件,缺段返 None。
- ``write_outline(workspace_id, partial)`` — 单段或整段 patch 覆写。
- ``commit_outline(workspace_id)`` — 跑 ``_run_commit_gates`` → 全过则
  把 ``volume_plans.volumes[*]`` 映射 ``VolumePlanItem → VolumeORM`` 落
  ``volumes`` SQLite 表;commit 后工作区进入"章节可写"状态。

设计要点
========

- **文件 SSOT + DB 是查询缓存**(P5-B-02 决策 2=C 沿用):4 段 JSON 文件落
  ``workspaces/{id}/data/outline/<stage>.json``;``VolumeORM`` 只在 commit
  端点同步落库(对齐 p5_plan §8)。``read_outline`` / ``write_outline`` /
  ``regenerate_section`` 都只读写文件,DB 在它们调用路径中不触达,避免
  双写一致性问题。
- **原子写**(``_atomic_write_json``):tmp + ``os.replace``,对齐
  ``ChapterVersioning`` 同型先例。
- **WS 5 stage 推送**(``task.{id}.progress`` 频道):``on_stage_complete``
  钩子注入 ``outline_builder.build_outline``,每段完成同步 publish
  ``stage`` event + ``data={stage, progress}``;最后再 publish ``done``。
- **[HARD] gate 集中**(``_run_commit_gates``):对齐 ``ChapterService._run_gates``
  + P4-acceptance gate 集中点模式。当前只跑 ``validate_arc_chapter_spans``
  一条 outline 级硬约束,失败抛 ``OutlineGateViolation`` + structlog
  ``outline_commit_gate_failed`` 事件;无失败 noop。P6+ 新 outline 级
  [HARD] 在 ``outline_validators.py`` 扩规则,本 service 自动 wire。
- **commit 是同步事务**:跑 gate → 落 ``VolumeORM`` → return。422 时
  不写脏数据(gate 在写库之前)。路由层随后只异步生成 ``ch-0001``；后续卡
  由正式 Run 在上一章富记忆成功后逐章生成。
- **session_factory + ws_manager 可选**:CLI / 测试不传时静默(对齐
  ``ChapterService._run_gates`` 同源)。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Mapping

import structlog
from pydantic import ValidationError
from sqlalchemy.orm import Session

from src.api.logging import bind_workspace_context
from src.api.websocket.channels import task_progress_channel
from src.api.websocket.manager import WebSocketManager
from src.core.agents.outline_builder import (
    FullOutline,
    OutlineBuilderError,
    OutlineStage,
    build_characters,
    build_outline,
    build_plot,
    build_volume_plans,
    build_world,
    generate_mini_plan,
    refine_volume_plans_length_aware,
    uses_experimental_outline,
    uses_split_outline_review,
)
from src.core.agents.character_backfill import (
    CharacterBackfillError,
    backfill_plot_referenced_characters,
)
from src.core.agents.outline_consistency_validator import (
    format_outline_consistency_failure_for_retry,
    validate_outline_consistency,
    validate_outline_consistency_partitioned,
)
from src.core.db.models import VolumeORM, WorkspaceORM
from src.core.memory.store import (
    create_character,
    create_plot_thread,
    get_character,
    get_plot_thread,
)
from src.core.models.character import (
    Character,
    CharacterCoreFact,
    CharacterStateSnapshot,
    VoiceProfile,
)
from src.core.models.core_state import make_state_key
from src.core.models.narrative import PlanningMode, PlanningScope
from src.core.models.outline import (
    CharacterOutlineItem,
    CharactersOutput,
    MiniPlanOutput,
    PlotLongThread,
    PlotOutput,
    VolumePlanItem,
    VolumePlansOutput,
    WorldOutput,
)
from src.core.models.model_calibration import GenreCalibration
from src.core.models.plot_thread import PlotThread
from src.core.models.volume import TensionTemplateName
from src.core.timing import StageTimer, log_stage_timing
from src.core.agents.volume_seam_replay_validator import (
    validate_volume_seam_replay,
)
from src.core.rules.outline_validators import (
    format_arc_span_failures_for_retry,
    OutlineValidationFailure,
    SeamReplayGateFailure,
    validate_arc_chapter_spans,
)
from src.core.rules.drama_contracts import (
    drama_validation_required,
    DramaticSourceContractError,
    validate_outline_dramatic_sources,
)
from src.core.rules.outline_action_state import (
    find_pending_signature_overlaps, find_repeated_completed_signatures,
)
from src.services.outline_readiness import OutlineReadiness, SECTIONS, outline_hashes
from src.services.workspace_service import WorkspaceService

if TYPE_CHECKING:
    from src.core.llm.base import LLMClient
    from src.core.routing.router import ModelRouter

_log = structlog.get_logger(__name__)

SessionFactory = Callable[[str], Session]
"""按 ``workspace_id`` 开新 SQLite ``Session`` 工厂(与 ``ChapterService`` 同形)。"""

OutlineSection = Literal[
    "world", "characters", "plot", "volume_plans", "mini_plan"
]
"""5 段 stage 名(v1.0.x wave 4 session 1 起从 4 段扩到 5 段),等同
``OutlineStage``(供 regenerate-section 端点 body 字面)。"""

_STAGE_FILENAMES: dict[OutlineSection, str] = {
    "world": "world.json",
    "characters": "characters.json",
    "plot": "plot.json",
    "volume_plans": "volume_plans.json",
    "mini_plan": "mini_plan.json",
}
_ALL_OUTLINE_SECTIONS: tuple[OutlineSection, ...] = (
    "world", "characters", "plot", "volume_plans", "mini_plan",
)

# 5 段串行进度:每段约 20% 完成度(对齐前端进度条平均分配)。
_STAGE_PROGRESS: dict[str, float] = {
    "world": 0.2,
    "characters": 0.4,
    "plot": 0.6,
    "volume_plans": 0.8,
    "mini_plan": 1.0,
    "done": 1.0,
}


# =============================================================================
# 异常类型
# =============================================================================


class OutlineServiceError(Exception):
    """outline_service 通用错误(读 / 写 / commit 失败)。"""


class OutlineReadinessError(OutlineBuilderError):
    """复用现有大纲错误响应，把恢复动作交给 UI。"""

    def __init__(self, message: str, *, stage: OutlineStage = "mini_plan") -> None:
        super().__init__(message, stage=stage, raw_output="", model_used="<local-readiness>")


class OutlineNotFound(OutlineServiceError):
    """``read_outline`` / ``commit_outline`` 时 4 段 JSON 文件不全。"""


class OutlineLengthCalibrationRequired(OutlineServiceError):
    """正式路由模型缺少可执行的长度校准，禁止静默跳过 ``volume_refine``。"""

    def __init__(
        self,
        *,
        workspace_id: str,
        model: str | None,
        genre: str,
        reason: str,
    ) -> None:
        self.workspace_id = workspace_id
        self.model = model
        self.genre = genre
        self.reason = reason
        model_label = model or "<unknown>"
        super().__init__(
            "当前 Writer 模型缺少可用于 volume_refine 的长度校准，"
            f"已在任何大纲 Provider 请求前阻断: model={model_label}, "
            f"genre={genre!r}, reason={reason}"
        )


class OutlineGateViolation(OutlineServiceError):
    """outline:commit gate 校验失败([HARD] 副本横跨 / 卷界事件重演)。

    路由层按首条违规 rule 翻译:``arc_too_short`` → 422
    ``OUTLINE_ARC_TOO_SHORT``;``volume_seam_replay`` → 422
    ``OUTLINE_VOLUME_SEAM_REPLAY``(v1.1 acceptance 修复1)。
    ``details.violations`` 同源列违规记录。
    """

    def __init__(
        self,
        failures: list[OutlineValidationFailure] | list[SeamReplayGateFailure],
    ) -> None:
        self.failures = failures
        if not failures:
            message = "outline:commit 校验失败,违规列表为空"
        else:
            first = failures[0]
            if isinstance(first, SeamReplayGateFailure):
                detail = (
                    f"seam={first.prev_chapter_id}→{first.next_chapter_id}"
                    f"(confidence={first.confidence:.2f})"
                )
            else:
                detail = f"arc={first.arc_id}(span={first.span_length})"
            message = (
                f"outline:commit 校验失败,共 {len(failures)} 条违规;首条:"
                f"{first.rule} {detail}"
            )
        super().__init__(message)


# =============================================================================
# 工具函数:文件 SSOT 读写 + 原子写
# =============================================================================


def _infer_workspace_root_from_path(path: Path) -> Path | None:
    """v1.0.x wave 6 s2.6a:从 outline writer 的 target path 反推 workspace 根目录。

    所有 outline writer path 形如 ``<workspaces_root>/<workspace_id>/data/outline/<file>``,
    回溯找到 ``data`` 段,其 parent 就是 workspace 根。若 path 不在该模式下
    (理论上不应出现,但保守 fallthrough 不影响测试)→ None,caller 不做 check。
    """
    for parent in path.parents:
        if parent.name == "data":
            return parent.parent
    return None


def _atomic_write_json(path: Path, data: dict) -> None:
    """原子写 JSON:tmp + ``os.replace``。

    对齐 ChapterVersioning 同型先例(P1-B-05),避免半写文件污染下游
    读取。``ensure_ascii=False`` 以保留中文字符可读性。
    """
    # v1.0.x wave 6 s2.6a:race 防御 — 工作区根目录已被 rmtree(destroy/purge/archive
    # 与异步 outline:generate 任务的 race)→ swallow + log,不 mkdir 重建子目录残留。
    # bug 2 现场:正是本路径写 volume_plans.json 时 mkdir 重建被 rmtree 的
    # luo-cloud-test/data/outline/ 子目录导致同 ID workspace 重创撞 WORKSPACE_CONFLICT。
    workspace_root = _infer_workspace_root_from_path(path)
    if workspace_root is not None and not workspace_root.is_dir():
        _log.warning(
            "workspace_writer_skipped",
            writer="outline_atomic_write_json",
            workspace_root=str(workspace_root),
            target_path=str(path),
            reason="workspace_destroyed_during_async_task",
        )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".", dir=str(path.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def _read_json(path: Path) -> dict | None:
    """读取 JSON 文件;不存在返 None,损坏冒泡 ``OutlineServiceError``。"""
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise OutlineServiceError(
            f"outline JSON 文件损坏: {path}: {exc}"
        ) from exc


def _outline_character_to_character(
    *, workspace_id: str, item: CharacterOutlineItem
) -> Character:
    """``CharacterOutlineItem`` → 完整 ``Character`` 派生(A-02 hot-fix)。

    outline 阶段 LLM 只产 voice_hint 一句话 + 简化字段,本 helper 派生为
    完整 ``Character`` schema 落 ``characters`` SQLite 表,让 CharactersPage /
    reference_validator / Memory Health 等下游能读到。
    缺失字段(``aliases`` / ``goals_short_term`` / ``current_state`` /
    ``abilities`` / ``secrets``)填空,``first_appearance_chapter`` /
    ``last_updated_chapter`` 留 ``None``(章节未生成),``motivation`` 入
    ``goals_long_term`` 单条。
    ``voice_profile`` 派生:``tone`` = voice_hint 全文,``sentence_style`` =
    voice_hint 全文(outline 仅一句,无法二次拆分),``self_address`` 默认
    "我"。outline 没有称呼资料时 ``addresses_others`` 留空；关系简介仍完整进入
    ``relationships``，不能用整段关系/发展设想近似称呼。
    ``catchphrases`` / ``forbidden_phrases`` 空(留用户后续 PUT 补)。
    """
    voice_profile = VoiceProfile(
        catchphrases=[],
        tone=item.voice_hint,
        sentence_style="",
        self_address="我",
        addresses_others={},
        forbidden_phrases=[],
    )
    initial_core_facts: list[CharacterCoreFact] = []
    current_state: dict[str, str] = {}
    for fact in item.initial_core_facts:
        source_text = _outline_initial_core_source_text(item, fact.source_field)
        source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        initial_core_facts.append(
            CharacterCoreFact(
                state_key=make_state_key(
                    entity_type="character",
                    entity_id=item.character_id,
                    attribute=fact.attribute,
                ),
                attribute=fact.attribute,
                value=fact.value,
                source_kind="outline_characters",
                source_chapter_id="outline:characters",
                source_version_id=0,
                source_path=(
                    "outline/characters.json.characters"
                    f"[{item.character_id}].{fact.source_field}"
                ),
                source_text_hash=source_hash,
                evidence_quote=fact.evidence_quote,
                valid_from_chapter=0,
            )
        )
        current_state[f"core.{fact.attribute}"] = fact.value

    state_timeline: list[CharacterStateSnapshot] = []
    if initial_core_facts:
        item_hash = hashlib.sha256(
            json.dumps(
                item.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        state_timeline.append(
            CharacterStateSnapshot(
                as_of_chapter=0,
                authority="outline_characters",
                source_chapter_id="outline:characters",
                source_version_id=0,
                source_text_hash=item_hash,
                evidence_quotes=[
                    fact.evidence_quote for fact in item.initial_core_facts
                ],
                core_facts=sorted(
                    initial_core_facts, key=lambda fact: fact.state_key
                ),
            )
        )

    return Character(
        character_id=item.character_id,
        workspace_id=workspace_id,
        name=item.name,
        aliases=[],
        archetype=item.archetype,
        appearance=item.appearance,
        personality=item.personality,
        background=item.background,
        identity_facets=list(item.identity_facets),  # A3:双界身份透传落库
        voice_profile=voice_profile,
        goals_long_term=[item.motivation],
        goals_short_term=[item.present_desire] if item.present_desire else [],
        current_state=current_state,
        state_timeline=state_timeline,
        relationships=dict(item.relationships_brief),
        abilities=[],
        secrets=[],
        first_appearance_chapter=None,
        last_updated_chapter=None,
    )


def _outline_initial_core_source_text(
    item: CharacterOutlineItem,
    source_field: str,
) -> str:
    """返回 ``initial_core_fact`` 已经由模型校验过的稳定来源文本。"""

    if source_field == "archetype":
        return item.archetype
    if source_field == "background":
        return item.background
    if source_field == "identity_facets":
        return "\n".join(
            f"{facet.context}:{facet.identity}" for facet in item.identity_facets
        )
    raise OutlineServiceError(f"未知 initial_core_fact source_field: {source_field}")


def _outline_plot_thread_to_thread(
    *, workspace_id: str, item: PlotLongThread, now: datetime
) -> PlotThread:
    """``PlotLongThread`` → 完整 ``PlotThread`` 派生(B2-01 hot-fix)。

    outline 阶段 LLM 只产 ``thread_id`` / ``title`` / ``description`` 三字段,
    本 helper 派生为完整 ``PlotThread`` schema 落 ``plot_threads`` SQLite 表,
    让 ThreadsPage / reference_validator / Memory Health 等下游能读到。

    映射规则:
    - ``thread_id`` / ``title`` / ``description`` 透传
    - ``status`` 默认 ``"planted"``(刚埋下)
    - ``importance`` 默认 3(范围 1-5 的中位,留用户后续 PUT 调整)
    - ``planted_chapter`` = ``None``(outline 阶段尚无章节,**v1.0.x B2-01 Alembic
      0011 起 schema 接受 None**;MemoryExtractor 反推 / 用户编辑时回填)
    - ``planned_payoff`` / ``actual_payoff`` / ``last_updated_chapter`` 都 ``None``
    - ``related_characters`` 空(LLM 不产关联;留 MemoryExtractor / 用户补)
    - ``created_at`` = 当前 UTC(commit_outline 时戳)
    """
    return PlotThread(
        thread_id=item.thread_id,
        workspace_id=workspace_id,
        title=item.title,
        thread_type="plot_event",
        status="planted",
        importance=3,
        planted_chapter=None,
        planned_payoff=None,
        actual_payoff=None,
        description=item.description,
        related_characters=[],
        created_at=now,
        last_updated_chapter=None,
    )


def _volume_plan_item_to_orm(
    *, workspace_id: str, plan: VolumePlanItem, now: datetime
) -> VolumeORM:
    """把 ``VolumePlanItem`` 映射成 ``VolumeORM``(commit 端点用)。

    转换要点:
    - ``arcs_json``:逐 arc dump(``model_dump(mode="json")``,tuple → list)
    - ``tension_curve_json``:LLM 出 ``list[int]``,这里直接落 ``list[dict]``
      形态(``[{chapter_number, tension, is_arc_finale}]``),为 P5-B-09
      ``volumes:apply-template`` + UI 编辑提供统一结构。
    - ``is_arc_finale``:对应章号命中某 arc 末位时标 True(其余 False)。
    """
    arcs_payload = [arc.model_dump(mode="json") for arc in plan.arcs]
    arc_finale_chapters = {arc.chapter_range[1] for arc in plan.arcs}
    curve_payload = [
        {
            "chapter_number": i + 1,
            "tension": plan.tension_curve[i],
            "is_arc_finale": (i + 1) in arc_finale_chapters,
        }
        for i in range(len(plan.tension_curve))
    ]
    return VolumeORM(
        volume_id=plan.volume_id,
        workspace_id=workspace_id,
        title=plan.title,
        summary=plan.summary,
        chapter_count=plan.chapter_count,
        tension_template=plan.tension_template,
        arcs_json=arcs_payload,
        tension_curve_json=curve_payload,
        created_at=now,
        last_updated_at=now,
    )


# =============================================================================
# OutlineService
# =============================================================================


class OutlineService:
    """outline 4 段生成 + 单段重生 + 文件 SSOT 读写 + commit gate + 落库。

    依赖 ``WorkspaceService``(校验 workspace meta) + ``router`` /
    ``client+model``(LLM 调用走 OutlineBuilder agent) + 可选
    ``session_factory``(commit 端点落 VolumeORM)+ 可选 ``ws_manager``
    (WS publish 5 stage 推送)+ 可选 ``prompts_dir``(覆盖 prompts 根)。
    """

    def __init__(
        self,
        *,
        workspace_service: WorkspaceService,
        client: "LLMClient | None" = None,
        model: str | None = None,
        router_factory: "Callable[[str], ModelRouter] | None" = None,
        session_factory: SessionFactory | None = None,
        ws_manager: WebSocketManager | None = None,
        prompts_dir: Path | None = None,
        pre_mini_plan_gates_enabled: bool = True,
    ) -> None:
        self._workspace_service = workspace_service
        self._client = client
        self._model = model
        self._router_factory = router_factory
        self._session_factory = session_factory
        self._ws_manager = ws_manager
        self._prompts_dir = prompts_dir
        self._pre_mini_plan_gates_enabled = pre_mini_plan_gates_enabled

    # ---- 路径辅助 -----------------------------------------------------------

    def _outline_dir(self, workspace_id: str) -> Path:
        return (
            self._workspace_service.workspaces_root / workspace_id / "data" / "outline"
        )

    def _stage_path(self, workspace_id: str, section: OutlineSection) -> Path:
        return self._outline_dir(workspace_id) / _STAGE_FILENAMES[section]

    def recover_pre_mini_review_receipt(
        self, workspace_id: str, *, capture_dir: Path,
    ) -> dict[str, Any]:
        """Product maintenance: recover five passed reviews from original wire captures.

        This only writes review metadata. It never edits generated outline files.
        """
        from src.services.outline_pre_mini_review_receipt import (
            read_receipt, recover_from_captures,
        )

        meta = self._workspace_service.info(workspace_id)
        outline = self.read_outline(workspace_id)
        if (outline["world"] is None or outline["characters"] is None
                or outline["plot"] is None or outline["volume_plans"] is None):
            raise OutlineReadinessError("四段大纲尚不齐全，不能恢复审核凭据")
        source: dict[str, Any] = {
            "world": outline["world"],
            "characters": outline["characters"],
            "plot": outline["plot"],
            "volume_plans": outline["volume_plans"],
        }
        source.update(premise=meta.premise, genre=meta.genre)
        path = self._outline_dir(workspace_id) / "pre_mini_review_receipt.json"
        if read_receipt(path, source) is not None:
            existing = _read_json(path)
            assert existing is not None
            return existing
        archive_dir = self._workspace_service.workspaces_root / workspace_id / "archives/prompts"
        receipt = recover_from_captures(
            source, capture_dir=capture_dir, archive_dir=archive_dir,
        )
        _atomic_write_json(path, receipt)
        return receipt

    def _build_router(self, workspace_id: str):
        if self._router_factory is None:
            return None
        return self._router_factory(workspace_id)

    def _experimental(self, workspace_id: str) -> bool:
        return uses_experimental_outline(self._build_router(workspace_id), self._client)

    def _ensure_story_intent(
        self, workspace_id: str, *, premise: str, genre: str,
        total_volumes: int, chapters_per_volume: int, router,
        planning_mode: PlanningMode = "whole_book",
        reference_chapter_words: int | None = None,
    ):
        """新世界前保存一次成功定位；后续失败重试不重复付费。"""
        from src.services.authoring_service import read_draft, intent_from_draft
        authored = read_draft(self._workspace_service.workspaces_root, workspace_id)
        if authored is not None:
            return intent_from_draft(authored)
        version = getattr(router, "narrative_handoff_version", 0) if router is not None else getattr(self._client, "narrative_handoff_version", 0)
        if not uses_experimental_outline(router, self._client) or version != 1:
            if planning_mode != "whole_book":
                raise OutlineReadinessError("当前旧生成配置不支持长篇开局范围，请使用已接通叙事规划的实验配置")
            return None
        import hashlib
        from src.core.agents.outline_builder import build_story_intent, load_story_intent_prompt
        from src.core.models.narrative import StoryIntent
        from src.core.agents.outline_review_packets import digest

        workspace = self._workspace_service.info(workspace_id)
        preferences = (
            f"主导人称：{workspace.default_voice}；风格选择：{workspace.style_template or '未指定'}"
        )
        guide_path = self._workspace_service.workspaces_root / workspace_id / "prompts/style_guide.md"
        if workspace.style_template is not None or guide_path.is_file():
            try:
                guide = guide_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise OutlineReadinessError("已选风格指南缺失或不可读，请恢复指南后继续") from exc
            if not guide:
                raise OutlineReadinessError("已选风格指南为空，请恢复指南后继续")
            preferences += (
                "\n实际风格指南（题材底色，用户前提的明确偏好优先；篇幅以本书要求为准）：\n"
                + guide
            )
        if reference_chapter_words is None:
            from src.services.config_service import ConfigService
            reference_chapter_words = ConfigService(
                workspaces_root=self._workspace_service.workspaces_root
            ).load_generation_config(workspace_id).word_count_max
        scope = PlanningScope(
            mode=planning_mode, total_volumes=total_volumes,
            chapters_per_volume=chapters_per_volume,
            reference_chapter_words=reference_chapter_words,
        )
        reading_preferences = preferences
        if workspace.style_template == "xianxia_growth":
            from src.core.agents._prompt import load_prompt_template
            from src.config import settings
            reference_path = (self._prompts_dir or settings.prompts_dir) / "outline_builder/planning_references/xianxia_growth.md"
            preferences += "\n仅供上游分配篇幅的题材参考（用户偏好优先）：\n" + load_prompt_template(reference_path)
        prompt_text = load_story_intent_prompt(self._prompts_dir, scoped=True)
        profile = router.select_profile("outline_builder") if router is not None else None
        request = {
            "premise": premise, "genre": genre, "preferences": preferences,
            "total_volumes": total_volumes, "chapters_per_volume": chapters_per_volume,
            "planning_scope": scope.model_dump(mode="json"),
            "prompt_hash": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
            "profile": profile.model_dump(mode="json") if profile is not None else self._model,
        }
        request_hash = digest(request)
        path = self._outline_dir(workspace_id) / "story_intent.json"
        cached = _read_json(path)
        if cached is not None and cached.get("request_hash") == request_hash:
            intent = StoryIntent.model_validate(cached["intent"])
            if digest(intent.model_dump(mode="json")) != cached.get("intent_hash"):
                raise OutlineReadinessError("故事定位缓存损坏，请保留现场后修复")
            if intent.user_request is not None and (
                intent.user_request.premise != premise or intent.user_request.genre != genre
                or cached.get("user_premise") != premise
                or (intent.version >= 6 and intent.user_request.reading_preferences != reading_preferences)
            ):
                raise OutlineReadinessError("故事定位缓存的用户原文与当前请求不一致")
            return intent
        with bind_workspace_context(workspace_id):
            intent = build_story_intent(
                premise=premise, genre=genre, preferences=preferences,
                total_volumes=total_volumes, chapters_per_volume=chapters_per_volume,
                planning_scope=scope, reading_preferences=reading_preferences,
                router=router, client=self._client, model=self._model,
                prompts_dir=self._prompts_dir,
            )
        _atomic_write_json(path, {
            "request_hash": request_hash,
            "intent_hash": digest(intent.model_dump(mode="json")),
            "user_premise": premise, "user_preferences": preferences,
            "intent": intent.model_dump(mode="json"),
        })
        return intent

    def _mini_candidate_store(self, workspace_id: str, router):
        version = getattr(router, "narrative_handoff_version", 0) if router is not None else getattr(self._client, "narrative_handoff_version", 0)
        if version != 1 or not uses_experimental_outline(router, self._client):
            return None
        from src.services.outline_mini_candidate_store import OutlineMiniCandidateStore

        return OutlineMiniCandidateStore(self._outline_dir(workspace_id) / "unverified_mini_candidates")

    def _readiness(self, workspace_id: str) -> OutlineReadiness:
        path = self._outline_dir(workspace_id) / "readiness.json"
        if not path.exists():
            return OutlineReadiness()
        try:
            return OutlineReadiness.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OutlineReadinessError("大纲完成凭据损坏，请保留现场并修复凭据后重新复核") from exc

    def _invalidate_readiness(self, workspace_id: str, **inputs: int) -> None:
        if not self._experimental(workspace_id) and not (self._outline_dir(workspace_id) / "review_receipt.json").is_file():
            return
        state = self._readiness(workspace_id).model_copy(update={"ready_hashes": None, **inputs})
        _atomic_write_json(self._outline_dir(workspace_id) / "readiness.json", state.model_dump())

    def _seal_readiness(self, workspace_id: str, material: dict) -> None:
        if not self._experimental(workspace_id) and not (self._outline_dir(workspace_id) / "review_receipt.json").is_file():
            return
        expected = outline_hashes(material)
        if set(expected) != set(SECTIONS) or expected != outline_hashes(self.read_outline(workspace_id)):
            raise OutlineReadinessError("大纲在生成期间发生变化，未标记为可提交，请复核最新材料")
        state = self._readiness(workspace_id).model_copy(update={"ready_hashes": expected})
        _atomic_write_json(self._outline_dir(workspace_id) / "readiness.json", state.model_dump())

    def _assert_ready(self, workspace_id: str, outline: dict) -> None:
        repaired = (self._outline_dir(workspace_id) / "review_receipt.json").is_file()
        if not self._experimental(workspace_id) and not repaired:
            return
        expected = self._readiness(workspace_id).ready_hashes
        if expected is None or set(expected) != set(SECTIONS) or expected != outline_hashes(outline):
            raise OutlineReadinessError(
                "当前大纲尚未完成复核或上游已改变。旧材料保留；请先修正失配段，"
                "取得最新四段的同源审核通过记录，再继续章节小纲后提交；不会自动重跑已通过审核。"
            )
        if repaired:
            meta = self._workspace_service.info(workspace_id)
            self._run_pre_mini_plan_gates(
                workspace_id, premise=meta.premise, genre=meta.genre,
                world=WorldOutput.model_validate(outline["world"]),
                characters=CharactersOutput.model_validate(outline["characters"]),
                plot=PlotOutput.model_validate(outline["plot"]),
                volume_plans=VolumePlansOutput.model_validate(outline["volume_plans"]),
                router=None,
            )

    def _backfill_regenerated(
        self, workspace_id: str, *, world: WorldOutput, characters: CharactersOutput,
        plot: PlotOutput, premise: str, genre: str, router: ModelRouter | None,
        volume_plans: VolumePlansOutput | None = None,
        persist: bool = True,
    ) -> CharactersOutput:
        if not uses_experimental_outline(router, self._client) or not self._resolve_character_backfill_enabled(workspace_id):
            return characters
        try:
            updated, added = backfill_plot_referenced_characters(
                world=world, characters=characters, plot=plot, volume_plans=volume_plans,
                premise=premise, genre=genre, router=router, client=self._client,
                model=self._model, prompts_dir=self._prompts_dir,
            )
        except CharacterBackfillError as exc:
            raise OutlineBuilderError(str(exc), stage="characters", raw_output=exc.raw_output,
                                      model_used=exc.model_used, attempts=exc.attempts) from exc
        if added and persist:
            _atomic_write_json(self._stage_path(workspace_id, "characters"), updated.model_dump(mode="json"))
        return updated

    def _resolve_breakthrough_payoff_enabled(self, workspace_id: str) -> bool:
        """模块①(w5-s10)升级可感化是否对本工作区生效(mini_plan gating)。

        门控 = style_template ∈ 3 题材且 generation 配置 breakthrough_payoff 解析为 on。
        任何读失败(工作区/配置异常)→ False(不注入约束 12,零回归 + 不阻断大纲生成)。
        """
        try:
            from src.core.models.genre_mechanism import resolve_breakthrough_payoff
            from src.services.config_service import ConfigService

            workspace = self._workspace_service.info(workspace_id)
            style = getattr(workspace, "style_template", None)
            cfg = ConfigService(
                workspaces_root=self._workspace_service.workspaces_root
            ).load_generation_config(workspace_id)
            return resolve_breakthrough_payoff(
                style_template=style, mode=cfg.breakthrough_payoff
            )
        except Exception:  # noqa: BLE001 — 门控读失败退 False,绝不阻断生成
            return False

    def _resolve_outline_autofix_enabled(self, workspace_id: str) -> bool:
        """大纲「名字写岔」确定性自动修是否对本工作区生效(v2 W3.5-s4-step1)。

        读工作区 generation 配置 ``outline_autofix_enabled``(默认 True)。读失败 →
        退**默认 True**(机制本身零 fail-open,含不可修硬伤 / 多数票不过 / 修后仍不过
        都会维持 HARD 拦,开着也不会误放行)。
        """
        try:
            from src.services.config_service import ConfigService

            cfg = ConfigService(
                workspaces_root=self._workspace_service.workspaces_root
            ).load_generation_config(workspace_id)
            return bool(cfg.outline_autofix_enabled)
        except Exception:  # noqa: BLE001 — 读失败退默认 True(机制零 fail-open)
            return True

    def _resolve_character_backfill_enabled(self, workspace_id: str) -> bool:
        """大纲角色完整性回填是否对本工作区生效(v2 W3.5-s4-step1 机制1)。

        读工作区 generation 配置 ``character_backfill_enabled``(默认 True)。读失败 →
        退**默认 True**(回填本身 fail-open,补卡异常返回 characters 原样不阻断,
        开着也不会误伤大纲)。
        """
        try:
            from src.services.config_service import ConfigService

            cfg = ConfigService(
                workspaces_root=self._workspace_service.workspaces_root
            ).load_generation_config(workspace_id)
            return bool(cfg.character_backfill_enabled)
        except Exception:  # noqa: BLE001 — 读失败退默认 True(回填 fail-open)
            return True

    def _stage_failed_volume_candidate(
        self, workspace_id: str, *, premise: str, genre: str,
        world: WorldOutput, characters: CharactersOutput, plot: PlotOutput,
        volume_plans: VolumePlansOutput, consistency: Any,
        expected_parent_hashes: Mapping[OutlineSection, str | None],
    ) -> str:
        """Keep a reviewed failure outside the authoritative outline files.

        The source-bound record is immutable and cannot make the outline ready.
        It preserves the verified quotes that the old error projection lost.
        """
        from src.core.agents.outline_review_packets import digest

        if not consistency.contradiction_details or consistency.passed:
            raise OutlineReadinessError("失败候选缺少可核验的审核引文")
        source = {
            "world": world.model_dump(mode="json"),
            "characters": characters.model_dump(mode="json"),
            "plot": plot.model_dump(mode="json"),
            "volume_plans": volume_plans.model_dump(mode="json"),
            "premise": premise, "genre": genre,
        }
        current = self.read_outline(workspace_id)
        parent_hashes = {
            section: digest(current[section]) if current[section] is not None else None
            for section in _ALL_OUTLINE_SECTIONS
        }
        if parent_hashes != expected_parent_hashes:
            raise OutlineReadinessError("审核期间正式大纲来源已变化，不能暂存旧失败候选")
        record = {
            "version": "outline-failed-candidate-v1", "workspace_id": workspace_id,
            "source_hash": digest(source), "parent_hashes": parent_hashes,
            "source": source,
            "review": {
                "stage": "pre_mini_plan_consistency", "contradictions": consistency.contradiction_details,
                "coverage_gaps": list(consistency.coverage_gaps), "evidence": consistency.evidence,
                "repair_scope": consistency.repair_scope,
            },
        }
        candidate_id = "candidate_" + digest(record)
        directory = self._outline_dir(workspace_id).parent / "outline-failed-candidates"
        path = directory / f"{candidate_id}.json"
        if path.is_file():
            if _read_json(path) != record:
                raise OutlineReadinessError("同名失败候选记录已变化")
        else:
            directory.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(path, record)
        return candidate_id

    def read_failed_volume_candidate(self, workspace_id: str, candidate_id: str) -> dict[str, Any]:
        """Read an immutable, source-bound candidate; never project it as SSOT."""
        import re
        from src.core.agents.outline_review_packets import digest

        if re.fullmatch(r"candidate_[0-9a-f]{64}", candidate_id) is None:
            raise OutlineReadinessError("无效失败候选标识")
        self._workspace_service.info(workspace_id)
        path = self._outline_dir(workspace_id).parent / "outline-failed-candidates" / f"{candidate_id}.json"
        record = _read_json(path)
        if (not isinstance(record, dict) or record.get("version") != "outline-failed-candidate-v1"
                or record.get("workspace_id") != workspace_id
                or "source" not in record or "review" not in record
                or record.get("source_hash") != digest(record["source"])
                or candidate_id != "candidate_" + digest(record)):
            raise OutlineReadinessError("失败候选缺失或指纹不符")
        return record

    def _run_pre_mini_plan_gates(
        self,
        workspace_id: str,
        *,
        premise: str,
        genre: str,
        world: WorldOutput,
        characters: CharactersOutput,
        plot: PlotOutput,
        volume_plans: VolumePlansOutput,
        router: "ModelRouter | None",
        require_prior_receipt: bool = False,
        review_checkpoint: Any = None,
    ) -> list[str]:
        """单段重生旁路进入 mini_plan / commit 前复用主生成 preflight gates。

        build_outline 主链路已在 volume_plans 后、mini_plan 前跑 consistency +
        arc span。regenerate_section 是人工/诊断常用旁路,若不补同等 preflight,
        坏卷规划会先被写成 SSOT,再等昂贵 mini_plan 或 commit 才首发失败。
        """
        from src.core.agents.outline_review_packets import digest

        parent = self.read_outline(workspace_id)
        expected_parent_hashes = {
            name: digest(parent[name]) if parent[name] is not None else None
            for name in _ALL_OUTLINE_SECTIONS
        }
        receipt_path = self._outline_dir(workspace_id) / "review_receipt.json"
        if not self._pre_mini_plan_gates_enabled and not uses_experimental_outline(router, self._client) and not receipt_path.is_file():
            log_stage_timing(
                _log,
                "outline_stage_timing",
                workspace_id=workspace_id,
                entrypoint="regenerate_section",
                stage="volume_plans",
                substage="pre_mini_plan_gates",
                reason="disabled",
            )
            return []

        signature_overlaps = find_pending_signature_overlaps(
            volume_plans=volume_plans, characters=characters,
        )
        if signature_overlaps:
            message = signature_overlaps[0].feedback()
            raise OutlineBuilderError(
                message, stage="volume_plans", raw_output=message,
                model_used="outline_action_state_guard",
                contradictions=[message],
            )
        repeated_signatures = find_repeated_completed_signatures(
            volume_plans=volume_plans, characters=characters,
        )
        if repeated_signatures:
            message = repeated_signatures[0].feedback()
            raise OutlineBuilderError(
                message, stage="volume_plans", raw_output=message,
                model_used="outline_action_state_guard",
                contradictions=[message],
            )

        from src.services.outline_pre_mini_review_receipt import read_receipt

        review_source = {
            "world": world.model_dump(mode="json"),
            "characters": characters.model_dump(mode="json"),
            "plot": plot.model_dump(mode="json"),
            "volume_plans": volume_plans.model_dump(mode="json"),
            "premise": premise, "genre": genre,
        }
        pre_mini_receipt = self._outline_dir(workspace_id) / "pre_mini_review_receipt.json"
        if not receipt_path.is_file():
            try:
                saved_gaps = read_receipt(pre_mini_receipt, review_source)
            except (OSError, ValueError) as exc:
                raise OutlineReadinessError("五项审核凭据损坏，不能复用") from exc
            if saved_gaps is not None:
                arc_failures = validate_arc_chapter_spans(volume_plans.volumes)
                if arc_failures:
                    raise OutlineBuilderError(format_arc_span_failures_for_retry(arc_failures),
                        stage="volume_plans", raw_output="; ".join(f.volume_id for f in arc_failures),
                        model_used="outline_arc_span_gate")
                return saved_gaps
            if require_prior_receipt and uses_experimental_outline(router, self._client):
                raise OutlineReadinessError(
                    "五项审核的同源通过凭据缺失或原文已变化；停止续跑，不重复调用已通过审核"
                )

        # Reuse only the exact source-bound review chain. A delta receipt means
        # one completed original review plus two checks of the changed fields;
        # it does not claim a second full-book verdict.
        if receipt_path.is_file():
            try:
                from src.core.agents.outline_review_packets import digest
                from src.services.outline_review_diagnostic_service import OutlineReviewDiagnosticService
                from src.services.outline_targeted_repair_service import OutlineTargetedRepairService

                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                diagnostic = OutlineReviewDiagnosticService(self._workspace_service, self._prompts_dir or Path("prompts"))
                plan, state = diagnostic._load(diagnostic._directory(workspace_id, receipt["review_id"]))
                repair = OutlineTargetedRepairService(self._workspace_service, self._prompts_dir or Path("prompts"))
                repair_plan, repair_state = repair._load(repair._directory(workspace_id, receipt["repair_id"]))
                current_plan = diagnostic._plan(workspace_id, "max_hard")
                source_hash_matches = receipt.get("source_hash") == current_plan["source_hash"]
                if repair_plan.get("version") == "outline-failed-candidate-repair-v1":
                    # The repair freezes raw model material; review packets may
                    # hoist user source to a canonical reference. Verify both
                    # representations against their own frozen identity.
                    _, frozen_candidate = repair._candidate_from_record(
                        repair._directory(workspace_id, receipt["repair_id"]), repair_plan, repair_state)
                    source_hash_matches = (frozen_candidate == review_source
                        and receipt.get("source_hash") == digest(frozen_candidate)
                        and plan.get("candidate_hash") == digest(frozen_candidate))
                shared = (source_hash_matches
                    and receipt.get("review_request_hash") == plan["request_hash"]
                    and plan["source_hash"] == current_plan["source_hash"]
                    and repair_state.get("status") == "committed"
                    and repair_state.get("review_id") == receipt["review_id"]
                    and receipt.get("content_hash") == digest({
                        "world": world.model_dump(mode="json"),
                        "characters": characters.model_dump(mode="json"),
                        "plot": plot.model_dump(mode="json"),
                        "volume_plans": volume_plans.model_dump(mode="json"),
                    }))
                if plan.get("version") == "outline-review-repair-delta-v2":
                    expected_delta = diagnostic._plan_repair_delta(workspace_id, receipt["repair_id"])
                    baseline_id = repair_plan["diagnostic_id"]
                    baseline_plan, baseline_state = diagnostic._load(diagnostic._directory(workspace_id, baseline_id))
                    valid = (shared and receipt.get("version") == 2
                        and receipt.get("baseline_review_id") == baseline_id
                        and plan["baseline_review_id"] == baseline_id
                        and all(plan[key] == expected_delta[key] for key in expected_delta)
                        and baseline_plan["source_hash"] == plan["parent_source_hash"]
                        and baseline_state["full"]["status"] == "completed"
                        and all(state[j]["status"] == "completed"
                            and state[j]["result"]["outcome"] == "compatible"
                            for j in ("resolution", "regression")))
                    gaps = list(baseline_state["full"]["result"].get("coverage_gaps", []))
                elif plan.get("version") == "outline-review-repair-split-v1":
                    expected_split = diagnostic._plan(
                        workspace_id, "repair_review_split",
                        material={"world": world.model_dump(mode="json"),
                                  "characters": characters.model_dump(mode="json"),
                                  "plot": plot.model_dump(mode="json"),
                                  "volume_plans": volume_plans.model_dump(mode="json")})
                    valid = (shared and receipt.get("version") == 1
                        and all(plan[key] == expected_split[key] for key in expected_split)
                        and diagnostic.split_review_passed(plan, state))
                    gaps = diagnostic.split_review_gaps(state) if valid else []
                else:
                    expected_legacy = (diagnostic._legacy_full_plan(workspace_id, "max_hard")
                        if plan.get("version") == "outline-review-repair-review-v1"
                           and "generation" not in plan["jobs"][0]
                        else current_plan)
                    valid = (shared and receipt.get("version") == 1
                        and plan.get("version") == "outline-review-repair-review-v1"
                        and plan["jobs"] == expected_legacy["jobs"]
                        and state["full"]["status"] == "completed"
                        and not state["full"]["result"].get("contradictions")
                        and state["full"]["result"].get("repair_scope") == "none")
                    gaps = list(state["full"]["result"].get("coverage_gaps", []))
            except (OSError, ValueError, KeyError, TypeError):
                valid = False
            if not valid:
                raise OutlineReadinessError("定点修复的原文或复审凭据已变化，请重新审核最新四段")
            if receipt.get("version") == 2 and uses_experimental_outline(router, self._client):
                # A delta review proves only that the selected repair did not
                # regress its changed fields.  Require an independently run,
                # current full review before treating the outline as ready.
                # This lookup never starts a Provider request.
                try:
                    full_ok = False
                    for expected_full in (current_plan,
                                          diagnostic._legacy_full_plan(workspace_id, "max_hard")):
                        full_id = "diagnostic_" + expected_full["request_hash"]
                        try:
                            full_plan, full_state = diagnostic._load(
                                diagnostic._directory(workspace_id, full_id))
                        except (OSError, ValueError, KeyError, TypeError):
                            continue
                        full_ok = (full_plan == expected_full
                            and full_state["full"]["status"] == "completed"
                            and not full_state["full"]["result"].get("contradictions")
                            and full_state["full"]["result"].get("repair_scope") == "none")
                        if full_ok:
                            break
                except (OSError, ValueError, KeyError, TypeError):
                    full_ok = False
                if not full_ok:
                    raise OutlineReadinessError(
                        "旧增量复审不能替代完整审核；请先通过产品诊断完成最新四段的最高强度审核")
                gaps = list(full_state["full"]["result"].get("coverage_gaps", []))
            arc_failures = validate_arc_chapter_spans(volume_plans.volumes)
            if arc_failures:
                raise OutlineBuilderError(format_arc_span_failures_for_retry(arc_failures),
                    stage="volume_plans", raw_output="; ".join(f.volume_id for f in arc_failures),
                    model_used="outline_arc_span_gate")
            return gaps

        with StageTimer(
            _log,
            "outline_stage_timing",
            {
                "workspace_id": workspace_id,
                "entrypoint": "regenerate_section",
                "stage": "volume_plans",
                "substage": "pre_mini_plan_consistency_gate",
            },
        ):
            review = (validate_outline_consistency_partitioned
                      if uses_split_outline_review(router, self._client, self._model)
                      else validate_outline_consistency)
            consistency = review(
                world=world,
                characters=characters,
                plot=plot,
                volume_plans=volume_plans,
                premise=premise,
                genre=genre,
                workspace_id=workspace_id,
                router=router,
                client=self._client,
                model=self._model,
                prompts_dir=self._prompts_dir,
                **({"checkpoint": review_checkpoint} if review is validate_outline_consistency_partitioned
                   and review_checkpoint is not None else {}),
            )
        if not consistency.passed:
            candidate_id = self._stage_failed_volume_candidate(
                workspace_id, premise=premise, genre=genre,
                world=world, characters=characters, plot=plot,
                volume_plans=volume_plans, consistency=consistency,
                expected_parent_hashes=expected_parent_hashes,
            ) if uses_experimental_outline(router, self._client) else None
            raise OutlineBuilderError(
                format_outline_consistency_failure_for_retry(result=consistency),
                stage="volume_plans",
                raw_output="; ".join(consistency.contradictions),
                model_used="outline_consistency_validator",
                contradictions=list(consistency.contradictions),
                coverage_gaps=list(consistency.coverage_gaps),
                evidence=consistency.evidence,
                contradiction_details=list(consistency.contradiction_details),
                repair_scope=consistency.repair_scope,
                candidate_id=candidate_id,
            )

        with StageTimer(
            _log,
            "outline_stage_timing",
            {
                "workspace_id": workspace_id,
                "entrypoint": "regenerate_section",
                "stage": "volume_plans",
                "substage": "pre_mini_plan_arc_span_gate",
            },
        ):
            arc_span_failures = validate_arc_chapter_spans(volume_plans.volumes)
        if arc_span_failures:
            raise OutlineBuilderError(
                format_arc_span_failures_for_retry(arc_span_failures),
                stage="volume_plans",
                raw_output="; ".join(
                    f"{f.volume_id}:{f.arc_id}:span={f.span_length}"
                    for f in arc_span_failures
                ),
                model_used="outline_arc_span_gate",
                violations=[f.model_dump(mode="json") for f in arc_span_failures],
            )
        if (uses_experimental_outline(router, self._client)
                and (consistency.llm_calls_count == 5 or len(consistency.completed_review_stages) == 5)):
            from src.services.outline_pre_mini_review_receipt import make_receipt

            _atomic_write_json(pre_mini_receipt, make_receipt(
                review_source, list(consistency.coverage_gaps),
                evidence=[{"stage": stage, "source": "product_review"}
                          for stage in ("world", "characters", "plot", "volume_plans", "cross")],
            ))
        return (
            list(consistency.coverage_gaps)
            if uses_experimental_outline(router, self._client)
            else []
        )

    def _resolve_length_calibration(
        self, workspace_id: str, genre: str, router: "ModelRouter | None"
    ) -> "tuple[GenreCalibration | None, int | None]":
        """W3.5-s3:resolve (writer 模型, 题材) 长度校准 + 单章字数上限,供卷规划
        「长度感知拆章」给 AI 注入密度参考(每核心情节单元约 N 字 / 每章约 M 单元到上限)。

        **keyed on writer 模型(谁写正文),非规划者 outline_builder**(维护人 2026-06-16
        架构 push back):拆章依据 = "writer 写每章会多长" → 卷规划据此切几章。outline≠writer
        的混合链路(如 outline=qwen / writer=deepseek)下,用 outline 模型会错配(该拆没拆)。

        返回 ``(GenreCalibration | None, word_count_max | None)``:
        - 实验v6允许真实校准观察缺失，仍按故事材料执行卷内细化。
        - 其他正式路由仍要求有效的模型和题材校准；所有正式路由均要求
          Writer profile与字数配置有效。损坏校准不作缺失处理。
        - **word_count_max**:工作区 generation_config 单章上限(密度参考分母)。

        **不吞 config 坏**:``load_model_calibration`` 段存在但脏 → fail-fast 抛
        (对齐 s2:避免拆章读坏 chars_per_beat 静默产错章数)。
        """
        from src.core.models.model_calibration import resolve_genre_archetype
        from src.services.config_service import ConfigService

        word_count_max: int | None = None
        word_count_error: Exception | None = None
        try:
            cfg = ConfigService(
                workspaces_root=self._workspace_service.workspaces_root
            ).load_generation_config(workspace_id)
            word_count_max = cfg.word_count_max
        except Exception as exc:  # noqa: BLE001 — legacy 可退化；正式路由下方阻断
            word_count_error = exc
            _log.warning(
                "length_calibration_word_count_max_read_failed",
                workspace_id=workspace_id,
            )

        if router is None:
            return None, word_count_max

        model_name: str | None = None
        try:
            # keyed on writer 模型(谁写正文 = 拆章密度依据),非规划者 outline_builder;
            # 不带 chapter_card → 走普通章 writer profile(finale/tension rules 不匹配)。
            model_name = router.select_profile("writer").model
        except Exception as exc:  # noqa: BLE001 — 配置缺口必须先于 Provider 明示
            raise OutlineLengthCalibrationRequired(
                workspace_id=workspace_id,
                model=None,
                genre=genre,
                reason=f"writer_profile_unavailable:{type(exc).__name__}",
            ) from exc

        if word_count_max is None:
            reason = "word_count_max_unavailable"
            if word_count_error is not None:
                reason += f":{type(word_count_error).__name__}"
            raise OutlineLengthCalibrationRequired(
                workspace_id=workspace_id,
                model=model_name,
                genre=genre,
                reason=reason,
            )

        from src.config import settings as _settings
        from src.core.config_loader import load_model_calibration

        calibrations = load_model_calibration(_settings.config_yaml_path)
        model_cal = calibrations.get(model_name)
        archetype = resolve_genre_archetype(genre)
        if uses_experimental_outline(router) and (
            model_cal is None or archetype is None or archetype not in model_cal.per_genre
        ):
            _log.info(
                "outline_length_observation_unavailable",
                workspace_id=workspace_id,
                model=model_name,
                genre=genre,
                volume_refine_required=True,
            )
            return None, word_count_max
        if model_cal is None:
            raise OutlineLengthCalibrationRequired(
                workspace_id=workspace_id,
                model=model_name,
                genre=genre,
                reason="model_calibration_missing",
            )

        if archetype is None:
            raise OutlineLengthCalibrationRequired(
                workspace_id=workspace_id,
                model=model_name,
                genre=genre,
                reason="genre_archetype_unmapped",
            )
        genre_calibration = model_cal.per_genre.get(archetype)
        if genre_calibration is None:
            raise OutlineLengthCalibrationRequired(
                workspace_id=workspace_id,
                model=model_name,
                genre=genre,
                reason=f"genre_calibration_missing:{archetype}",
            )

        return genre_calibration, word_count_max

    # ---- 公共方法:generate / regenerate / read / write / commit ------------

    def generate_outline(self, workspace_id: str, **kwargs) -> FullOutline:
        from src.services.authoring_service import read_draft, generate_confirmed_outline
        if read_draft(self._workspace_service.workspaces_root, workspace_id) is not None:
            return generate_confirmed_outline(self, workspace_id, **kwargs)
        return self._generate_outline(workspace_id, **kwargs)

    def _generate_outline(
        self,
        workspace_id: str,
        *,
        premise: str,
        total_volumes: int,
        chapters_per_volume: int,
        tension_template: TensionTemplateName,
        genre: str = "",
        task_id: str | None = None,
        planning_mode: PlanningMode = "whole_book",
    ) -> FullOutline:
        """5 段串行生成完整 outline + 落 SSOT 文件 + WS 5 stage 推送
        (v1.0.x wave 4 session 1 起从 4 段扩到 5 段加 mini_plan)。

        ``task_id`` 与 ``ws_manager`` 同时给定时,每段(world / characters
        / plot / volume_plans / mini_plan)完成同步 publish ``stage`` event 到
        ``task.{task_id}.progress``。其他情况(测试 / CLI)WS publish 静默
        noop。

        v1.0.x wave 3 Commit B follow-up(2026-05-21):service 不再 emit
        "done" stage event;路由层 wrapper(``_run_outline_generate_task``)
        在 ``task_registry.update_to_done`` 之后统一 publish
        ``event="done"`` 帧(对齐 chapters.py / outline:regenerate-section
        既有同型模式),前端 ``NewBookPage.handleTaskFrame`` 才能正确
        ``frame.event === "done"`` 切 ready。

        失败抛 ``outline_builder.OutlineBuilderError``(包含 stage /
        raw_output / model_used),路由层不翻译(P5-B-04 stub — 服务层
        ``outline:regenerate-section`` 端点支持单段重试)。
        """
        # 校验工作区存在(WorkspaceService.info 不存在时抛 MetaFileError →
        # 全局翻译为 WORKSPACE_INVALID(422),对齐 memory_health_service 模式)
        workspace = self._workspace_service.info(workspace_id)
        from src.services.post_v8_abc_selection_activity import (
            validate_product_outline_input,
            validate_product_workspace_create,
        )

        validate_product_workspace_create(
            workspace_id=workspace.workspace_id,
            display_name=workspace.display_name,
            genre=workspace.genre,
            premise=workspace.premise,
            total_chapters=workspace.total_chapters,
            default_voice=workspace.default_voice,
            style_template=workspace.style_template,
        )
        selection_short_arc = validate_product_outline_input(
            workspace_id=workspace_id,
            premise=premise,
            total_volumes=total_volumes,
            chapters_per_volume=chapters_per_volume,
            tension_template=tension_template,
            genre=genre,
        )

        router = self._build_router(workspace_id)
        # 先检查路由、篇幅与校准数据形状，再变更状态。实验v6可缺少长度观察。
        length_calibration, target_word_count_max = (
            self._resolve_length_calibration(workspace_id, genre, router)
        )
        from src.core.agents.outline_review_packets import digest

        parent: dict[str, Any] = {key: value for key, value in self.read_outline(workspace_id).items()}
        import sqlite3
        from src.services.outline_split_review_progress import OutlineSplitReviewProgress

        review_checkpoint = (OutlineSplitReviewProgress(
            self._outline_dir(workspace_id) / "pre_mini_review_progress",
            input_binding={"tension_template": tension_template,
                           "reference_chapter_words": target_word_count_max})
            if uses_split_outline_review(router, self._client, self._model) else None)
        resume_source = None
        if review_checkpoint is not None:
            try:
                if (review_checkpoint.directory / "candidate.json").is_file():
                    review_checkpoint.assert_idle(self._outline_dir(workspace_id).parent.parent)
                resume_source = review_checkpoint.candidate(
                    parent, premise=premise, genre=genre, total_volumes=total_volumes,
                    chapters_per_volume=chapters_per_volume, planning_mode=planning_mode)
                if resume_source is None:
                    resume_source = review_checkpoint.recover_archives(
                        self._outline_dir(workspace_id).parent.parent, parent,
                        premise=premise, genre=genre, total_volumes=total_volumes,
                        chapters_per_volume=chapters_per_volume, planning_mode=planning_mode)
            except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
                raise OutlineReadinessError(str(exc), stage="volume_plans") from exc
            if resume_source is not None:
                world = WorldOutput.model_validate(resume_source["world"])
                characters = CharactersOutput.model_validate(resume_source["characters"])
                plot = PlotOutput.model_validate(resume_source["plot"])
                volume_plans = VolumePlansOutput.model_validate(resume_source["volume_plans"])
                self._set_outline_status(workspace_id, "generating")
                with bind_workspace_context(workspace_id):
                    self._run_pre_mini_plan_gates(
                        workspace_id, premise=premise, genre=genre, world=world,
                        characters=characters, plot=plot, volume_plans=volume_plans,
                        router=router, review_checkpoint=review_checkpoint)
                # Promote only after all five checks and the ordinary structural gate.
                if any(self.read_outline(workspace_id)[key] != parent[key]
                       for key in _ALL_OUTLINE_SECTIONS):
                    raise OutlineReadinessError("大纲原文在恢复期间变化，不能落盘")
                _atomic_write_json(self._stage_path(workspace_id, "volume_plans"),
                                   volume_plans.model_dump(mode="json"))
                self._publish_stage(task_id, "volume_plans")
                mini_plan = self.regenerate_section(
                    workspace_id, section="mini_plan", premise=premise, genre=genre,
                    task_id=task_id, total_volumes=total_volumes,
                    chapters_per_volume=chapters_per_volume, tension_template=tension_template,
                    planning_mode=planning_mode)
                self._publish_stage(task_id, "mini_plan")
                return FullOutline(world=world, characters=characters, plot=plot,
                                   volume_plans=volume_plans, mini_plan=mini_plan)
        expected_parent_hashes = {
            name: digest(parent[name]) if parent[name] is not None else None
            for name in _ALL_OUTLINE_SECTIONS
        }

        # P5-B-07:set outline_status = 'generating'(开始时;失败时保持 generating
        # 不回退 — 用户可重试 outline:generate / outline:regenerate-section
        # 修复后再 commit)。
        self._set_outline_status(workspace_id, "generating")
        self._invalidate_readiness(
            workspace_id, total_volumes=total_volumes, chapters_per_volume=chapters_per_volume,
        )

        def _on_stage_complete(stage: OutlineStage, output) -> None:  # type: ignore[no-untyped-def]
            # 落盘
            self._stage_path(workspace_id, stage).parent.mkdir(
                parents=True, exist_ok=True
            )
            _atomic_write_json(
                self._stage_path(workspace_id, stage),
                output.model_dump(mode="json"),
            )
            expected_parent_hashes[stage] = digest(output.model_dump(mode="json"))
            # WS 推送
            self._publish_stage(task_id, stage)

        def _on_parallel_status(snapshot: dict[str, Any]) -> None:
            self._publish_parallel_status(
                task_id,
                stage="mini_plan",
                snapshot=snapshot,
            )

        def _on_review_failure(
            world: WorldOutput, characters: CharactersOutput, plot: PlotOutput,
            volume_plans: VolumePlansOutput, consistency: Any,
        ) -> str:
            return self._stage_failed_volume_candidate(
                workspace_id, premise=premise, genre=genre,
                world=world, characters=characters, plot=plot,
                volume_plans=volume_plans, consistency=consistency,
                expected_parent_hashes=expected_parent_hashes,
            )

        def _on_review_complete(
            world: WorldOutput, characters: CharactersOutput, plot: PlotOutput,
            volume_plans: VolumePlansOutput, consistency: Any,
        ) -> None:
            if (not uses_experimental_outline(router, self._client)
                    or (consistency.llm_calls_count != 5
                        and len(consistency.completed_review_stages) != 5)):
                return
            from src.services.outline_pre_mini_review_receipt import make_receipt

            source = {
                "world": world.model_dump(mode="json"),
                "characters": characters.model_dump(mode="json"),
                "plot": plot.model_dump(mode="json"),
                "volume_plans": volume_plans.model_dump(mode="json"),
                "premise": premise, "genre": genre,
            }
            _atomic_write_json(
                self._outline_dir(workspace_id) / "pre_mini_review_receipt.json",
                make_receipt(source, list(consistency.coverage_gaps), evidence=[
                    {"stage": stage, "source": "product_review"}
                    for stage in ("world", "characters", "plot", "volume_plans", "cross")
                ]),
            )

        # v2 间隙 A3②(2026-06-13):outline 5 段 + mini_plan 内嵌 judge
        # (mechanism / beat_alignment)的 llm_response 此前缺 workspace_id
        # → 全落 system_logs;工作区级绑定让日志归属正确分流。
        with bind_workspace_context(workspace_id):
            story_intent = self._ensure_story_intent(
                workspace_id, premise=premise, genre=genre,
                total_volumes=total_volumes, chapters_per_volume=chapters_per_volume,
                router=router, planning_mode=planning_mode,
                reference_chapter_words=target_word_count_max,
            )
            full = build_outline(
                premise=premise,
                total_volumes=total_volumes,
                chapters_per_volume=chapters_per_volume,
                tension_template=tension_template,
                genre=genre,
                story_intent=story_intent,
                prepared_outline=(
                    (WorldOutput.model_validate(parent["world"]),
                     CharactersOutput.model_validate(parent["characters"]),
                     PlotOutput.model_validate(parent["plot"]))
                    if story_intent is not None and story_intent.version >= 7 else None
                ),
                mini_candidate_checkpoint=self._mini_candidate_store(workspace_id, router),
                pre_mini_review_checkpoint=review_checkpoint,
                length_calibration=length_calibration,
                target_word_count_max=target_word_count_max,
                router=router,
                client=self._client,
                model=self._model,
                prompts_dir=self._prompts_dir,
                on_stage_complete=_on_stage_complete,
                on_review_failure=(_on_review_failure if uses_experimental_outline(router, self._client) else None),
                on_review_complete=_on_review_complete,
                on_parallel_status=_on_parallel_status,
                breakthrough_payoff_enabled=self._resolve_breakthrough_payoff_enabled(
                    workspace_id
                ),
                outline_autofix_enabled=self._resolve_outline_autofix_enabled(
                    workspace_id
                ),
                character_backfill_enabled=self._resolve_character_backfill_enabled(
                    workspace_id
                ),
                selection_short_arc=selection_short_arc,
                timing_context={
                    "workspace_id": workspace_id,
                    "task_id": task_id,
                    "entrypoint": "generate_outline",
                },
            )
        # v1.0.x wave 3 Commit B follow-up(2026-05-21):service 不再 emit
        # "done" stage event;统一 done event 由路由层 wrapper publish
        # ``event="done"``(对齐 chapters.py / :regenerate-section 同型,
        # 修 NewBookPage 永远卡"生成中"bug)。
        _log.info(
            "outline_generated",
            workspace_id=workspace_id,
            total_volumes=total_volumes,
            chapters_per_volume=chapters_per_volume,
            tension_template=tension_template,
            task_id=task_id,
        )
        self._seal_readiness(workspace_id, full.model_dump(mode="json"))
        return full

    def regenerate_section(
        self,
        workspace_id: str,
        *,
        section: OutlineSection,
        premise: str,
        total_volumes: int | None = None,
        chapters_per_volume: int | None = None,
        tension_template: TensionTemplateName | None = None,
        genre: str = "",
        task_id: str | None = None,
        planning_mode: PlanningMode | None = None,
    ):
        """单段重生,从已落盘 4 文件读取上游 context 透传(不重跑上游)。

        :param section: 4 段之一。``world`` 无上游;``characters`` 需 world;
            ``plot`` 需 world+characters;``volume_plans`` 需全部 + 3 参数。
        :raises OutlineNotFound: 缺失上游段文件。
        """
        self._workspace_service.info(workspace_id)
        from src.services.authoring_service import read_draft
        authoring = read_draft(self._workspace_service.workspaces_root, workspace_id)
        if authoring is not None and (authoring.status != "confirmed" or section in {"world", "characters", "plot"}):
            raise OutlineReadinessError("请在小说规划中调整、整理并确认，不能绕过已选方向重生上游")
        if authoring is not None:
            p = authoring.preferences
            if (premise != authoring.premise or (genre and genre != authoring.genre)
                    or total_volumes not in (None, p.total_volumes)
                    or chapters_per_volume not in (None, p.chapters_per_volume)
                    or tension_template not in (None, p.tension_template)
                    or planning_mode not in (None, "whole_book")):
                raise OutlineReadinessError("单段恢复参数与已确认小说规划不一致")
            genre, total_volumes, chapters_per_volume = authoring.genre, p.total_volumes, p.chapters_per_volume
            tension_template, planning_mode = p.tension_template, "whole_book"
        from src.services.post_v8_abc_selection_activity import (
            load_product_selection_activity,
            validate_product_outline_input,
        )

        selection_activity = load_product_selection_activity(workspace_id)
        selection_short_arc = selection_activity is not None
        if selection_activity is not None:
            expected = selection_activity.runtime.product_input
            total_volumes = total_volumes or expected.total_volumes
            chapters_per_volume = (
                chapters_per_volume or expected.chapters_per_volume
            )
            tension_template = tension_template or expected.tension_template
            validate_product_outline_input(
                workspace_id=workspace_id,
                premise=premise,
                total_volumes=total_volumes,
                chapters_per_volume=chapters_per_volume,
                tension_template=tension_template,
                genre=genre,
            )
        router = self._build_router(workspace_id)

        if section == "world":
            saved_world = _read_json(self._stage_path(workspace_id, "world"))
            saved_intent = (saved_world or {}).get("story_intent")
            if saved_intent is None:
                saved_intent = (_read_json(self._outline_dir(workspace_id) / "story_intent.json") or {}).get("intent")
            if saved_intent is not None:
                from src.core.models.narrative import StoryIntent
                saved_scope = StoryIntent.model_validate(saved_intent).planning_scope
                if saved_scope is not None:
                    planning_mode = planning_mode or saved_scope.mode
                    total_volumes = total_volumes or saved_scope.total_volumes
                    chapters_per_volume = chapters_per_volume or saved_scope.chapters_per_volume
            if uses_experimental_outline(router, self._client):
                saved = self._readiness(workspace_id)
                total_volumes = total_volumes or saved.total_volumes
                chapters_per_volume = chapters_per_volume or saved.chapters_per_volume
                if total_volumes is None or chapters_per_volume is None:
                    existing = self.read_outline(workspace_id).get("volume_plans")
                    if existing is not None:
                        volumes = VolumePlansOutput.model_validate(existing).volumes
                        counts = {v.chapter_count for v in volumes}
                        total_volumes = total_volumes or len(volumes)
                        if len(counts) == 1:
                            chapters_per_volume = chapters_per_volume or counts.pop()
                if total_volumes is None or chapters_per_volume is None:
                    raise OutlineReadinessError(
                        "重新生成世界需要原书卷数与每卷章数，请从原建书表单重试", stage="world",
                    )
            self._invalidate_readiness(workspace_id, **{
                key: value for key, value in {
                    "total_volumes": total_volumes, "chapters_per_volume": chapters_per_volume,
                }.items() if value is not None
            })
            # v2 间隙 A3②:单段重生同 generate_outline,工作区级绑定(下同 5 段)。
            with bind_workspace_context(workspace_id):
                story_intent = self._ensure_story_intent(
                    workspace_id, premise=premise, genre=genre,
                    total_volumes=total_volumes or 5,
                    chapters_per_volume=chapters_per_volume or 30, router=router,
                    planning_mode=planning_mode or "whole_book",
                )
                world = build_world(
                    premise=premise,
                    genre=genre,
                    story_intent=story_intent,
                    total_volumes=total_volumes or 5,
                    chapters_per_volume=chapters_per_volume or 30,
                    router=router,
                    client=self._client,
                    model=self._model,
                    prompts_dir=self._prompts_dir,
                )
            _atomic_write_json(
                self._stage_path(workspace_id, "world"),
                world.model_dump(mode="json"),
            )
            return world

        # 读上游
        world_data = _read_json(self._stage_path(workspace_id, "world"))
        if world_data is None:
            raise OutlineNotFound(
                f"regenerate_section({section}): 上游 world.json 缺失"
            )
        world = WorldOutput.model_validate(world_data)

        if section == "characters":
            self._invalidate_readiness(workspace_id)
            with bind_workspace_context(workspace_id):
                characters = build_characters(
                    premise=premise,
                    world=world,
                    genre=genre,
                    router=router,
                    client=self._client,
                    model=self._model,
                    prompts_dir=self._prompts_dir,
                )
            _atomic_write_json(
                self._stage_path(workspace_id, "characters"),
                characters.model_dump(mode="json"),
            )
            return characters

        characters_data = _read_json(
            self._stage_path(workspace_id, "characters")
        )
        if characters_data is None:
            raise OutlineNotFound(
                f"regenerate_section({section}): 上游 characters.json 缺失"
            )
        characters = CharactersOutput.model_validate(characters_data)

        if section == "plot":
            self._invalidate_readiness(workspace_id)
            with bind_workspace_context(workspace_id):
                plot = build_plot(
                    premise=premise,
                    world=world,
                    characters=characters,
                    genre=genre,
                    router=router,
                    client=self._client,
                    model=self._model,
                    prompts_dir=self._prompts_dir,
                )
                characters = self._backfill_regenerated(
                    workspace_id, world=world, characters=characters, plot=plot,
                    premise=premise, genre=genre, router=router,
                )
            _atomic_write_json(
                self._stage_path(workspace_id, "plot"),
                plot.model_dump(mode="json"),
            )
            return plot

        plot_data = _read_json(self._stage_path(workspace_id, "plot"))
        if plot_data is None:
            raise OutlineNotFound(
                "regenerate_section(volume_plans): 上游 plot.json 缺失"
            )
        plot = PlotOutput.model_validate(plot_data)

        if section == "volume_plans":
            if total_volumes is None or chapters_per_volume is None or tension_template is None:
                raise OutlineServiceError(
                    "regenerate_section(volume_plans): 需 total_volumes / "
                    "chapters_per_volume / tension_template 三参数"
                )
            self._invalidate_readiness(
                workspace_id, total_volumes=total_volumes,
                chapters_per_volume=chapters_per_volume,
            )
            with bind_workspace_context(workspace_id):
                length_calibration, target_word_count_max = (
                    self._resolve_length_calibration(workspace_id, genre, router)
                )
                volume_plans = build_volume_plans(
                    premise=premise,
                    world=world,
                    characters=characters,
                    plot=plot,
                    total_volumes=total_volumes,
                    chapters_per_volume=chapters_per_volume,
                    tension_template=tension_template,
                    genre=genre,
                    router=router,
                    client=self._client,
                    model=self._model,
                    prompts_dir=self._prompts_dir,
                    selection_short_arc=selection_short_arc,
                )
                # 与全量生成保持一致：实验v6即使无观察也执行原有细化。
                if target_word_count_max is not None and (
                    length_calibration is not None or uses_experimental_outline(router)
                ):
                    volume_plans = refine_volume_plans_length_aware(
                        volume_plans=volume_plans,
                        length_calibration=length_calibration,
                        target_word_count_max=target_word_count_max,
                        genre=genre,
                        router=router,
                        client=self._client,
                        model=self._model,
                        prompts_dir=self._prompts_dir,
                        selection_short_arc=selection_short_arc,
                        story_intent=world.story_intent,
                        world=world, characters=characters,
                        plot=plot,
                    )
                characters = self._backfill_regenerated(
                    workspace_id, world=world, characters=characters, plot=plot,
                    volume_plans=volume_plans, premise=premise, genre=genre, router=router,
                    persist=False,
                )
                self._run_pre_mini_plan_gates(
                    workspace_id,
                    premise=premise,
                    genre=genre,
                    world=world,
                    characters=characters,
                    plot=plot,
                    volume_plans=volume_plans,
                    router=router,
                )
            _atomic_write_json(
                self._stage_path(workspace_id, "volume_plans"),
                volume_plans.model_dump(mode="json"),
            )
            if characters.model_dump(mode="json") != characters_data:
                _atomic_write_json(
                    self._stage_path(workspace_id, "characters"),
                    characters.model_dump(mode="json"),
                )
            return volume_plans

        # section == "mini_plan"(v1.0.x wave 4 session 1)
        volume_plans_data = _read_json(
            self._stage_path(workspace_id, "volume_plans")
        )
        if volume_plans_data is None:
            raise OutlineNotFound(
                "regenerate_section(mini_plan): 上游 volume_plans.json 缺失"
            )
        volume_plans = VolumePlansOutput.model_validate(volume_plans_data)
        checked_material = {
            "world": world.model_dump(mode="json"),
            "characters": characters.model_dump(mode="json"),
            "plot": plot.model_dump(mode="json"),
            "volume_plans": volume_plans.model_dump(mode="json"),
        }
        self._invalidate_readiness(workspace_id)
        with bind_workspace_context(workspace_id):
            consistency_coverage_gaps = self._run_pre_mini_plan_gates(
                workspace_id,
                premise=premise,
                genre=genre,
                world=world,
                characters=characters,
                plot=plot,
                volume_plans=volume_plans,
                router=router,
                require_prior_receipt=True,
            )
            mini_plan = generate_mini_plan(
                candidate_checkpoint=self._mini_candidate_store(workspace_id, router),
                world=world,
                characters=characters,
                plot=plot,
                volume_plans=volume_plans,
                genre=genre,
                router=router,
                client=self._client,
                model=self._model,
                prompts_dir=self._prompts_dir,
                on_parallel_status=lambda snapshot: self._publish_parallel_status(
                    task_id,
                    stage="mini_plan",
                    snapshot=snapshot,
                ),
                selection_short_arc=selection_short_arc,
                consistency_coverage_gaps=consistency_coverage_gaps,
            )
        _atomic_write_json(
            self._stage_path(workspace_id, "mini_plan"),
            mini_plan.model_dump(mode="json"),
        )
        self._seal_readiness(workspace_id, {
            **checked_material, "mini_plan": mini_plan.model_dump(mode="json"),
        })
        return mini_plan

    def read_outline(self, workspace_id: str) -> dict[OutlineSection, dict | None]:
        """读 5 段 JSON 文件,缺段返对应键值 None(v1.0.x wave 4 session 1 起从 4 段扩 5 段)。

        不抛 ``OutlineNotFound`` 是为了 ``GET /outline`` 在大纲生成过程中
        可部分查;``commit_outline`` 调用此方法后会自行检查 None
        (注:``mini_plan`` 段对 commit 是软依赖 — 缺失 mini_plan 不阻塞
        commit,只 warn 日志,保兼容 wave 4 前生成的旧工作区 outline)。
        """
        self._workspace_service.info(workspace_id)
        return {
            section: _read_json(self._stage_path(workspace_id, section))
            for section in _ALL_OUTLINE_SECTIONS
        }

    def write_outline(
        self,
        workspace_id: str,
        *,
        world: dict | None = None,
        characters: dict | None = None,
        plot: dict | None = None,
        volume_plans: dict | None = None,
        mini_plan: dict | None = None,
    ) -> None:
        """单段或整段 patch 覆写。仅非 None 段写入;**校验 schema** 后再落盘。

        v1.0.x wave 4 session 1 加 ``mini_plan`` 参数(单段 patch 覆写支持)。
        """
        self._workspace_service.info(workspace_id)
        from src.services.authoring_service import read_draft
        if read_draft(self._workspace_service.workspaces_root, workspace_id) is not None:
            raise OutlineReadinessError("选择式小说请在规划页调整，不能直接覆写生成物")
        if world is not None:
            self._invalidate_readiness(workspace_id)
            WorldOutput.model_validate(world)
            _atomic_write_json(self._stage_path(workspace_id, "world"), world)
        if characters is not None:
            self._invalidate_readiness(workspace_id)
            CharactersOutput.model_validate(characters)
            _atomic_write_json(
                self._stage_path(workspace_id, "characters"), characters
            )
        if plot is not None:
            self._invalidate_readiness(workspace_id)
            PlotOutput.model_validate(plot)
            _atomic_write_json(self._stage_path(workspace_id, "plot"), plot)
        if volume_plans is not None:
            self._invalidate_readiness(workspace_id)
            VolumePlansOutput.model_validate(volume_plans)
            _atomic_write_json(
                self._stage_path(workspace_id, "volume_plans"), volume_plans
            )
        if mini_plan is not None:
            self._invalidate_readiness(workspace_id)
            MiniPlanOutput.model_validate(mini_plan)
            _atomic_write_json(
                self._stage_path(workspace_id, "mini_plan"), mini_plan
            )

    def commit_outline(self, workspace_id: str) -> list[VolumeORM]:
        """跑 ``_run_commit_gates`` → 全过则落 ``volumes`` 表。

        - 缺段 → ``OutlineNotFound``(路由层翻译为 422)
        - gate 失败 → ``OutlineGateViolation``(路由层翻译为 422
          ``OUTLINE_ARC_TOO_SHORT``)
        - gate 全过 → 同步事务 upsert ``VolumeORM`` × N(N == total_volumes)
          + 返 ORM 列表
        - ``session_factory=None`` → 跳过落库(返空列表),供单测纯文件路径用
        """
        workspace = self._workspace_service.info(workspace_id)
        outline = self.read_outline(workspace_id)
        from src.services.authoring_service import read_draft, intent_from_draft
        authoring = read_draft(self._workspace_service.workspaces_root, workspace_id)
        if authoring is not None:
            if authoring.status != "confirmed" or not outline["world"] or outline["world"].get("story_intent") != intent_from_draft(authoring).model_dump(mode="json"):
                raise OutlineReadinessError("已确认规划与待提交来源不一致")
            from src.core.rules.authoring_plan import validate_authoring_layout
            if outline["volume_plans"] is not None:
                selected_plan = intent_from_draft(authoring).authoring_plan
                assert selected_plan is not None
                validate_authoring_layout(VolumePlansOutput.model_validate(outline["volume_plans"]), selected_plan)
        self._assert_ready(workspace_id, outline)
        if outline["volume_plans"] is None:
            raise OutlineNotFound(
                f"commit_outline({workspace_id}): volume_plans.json 缺失"
            )
        # 校验 4 段都存在(commit 必须 4 段齐全)
        # v1.0.x wave 4 session 1:mini_plan 是软依赖 — 缺失只 warn 不抛,
        # 兼容 wave 4 前生成的旧工作区(用户后续可通过 regenerate-section
        # 单段重生 mini_plan)。新工作区走 generate_outline 5 段流程必定齐全。
        for stage in ("world", "characters", "plot", "volume_plans"):
            if outline[stage] is None:
                raise OutlineNotFound(
                    f"commit_outline({workspace_id}): {stage}.json 缺失"
                )
        if outline.get("mini_plan") is None:
            _log.warning(
                "commit_outline_missing_mini_plan",
                workspace_id=workspace_id,
                hint=(
                    "wave 4 前生成的旧工作区缺 mini_plan 段;commit 仍允许,但"
                    "下游章节卡生成会回退到 arc summary 拆分模式(可能撞章节卡"
                    "雷同 bug),建议用户在 OutlinePage 触发 regenerate-section "
                    "mini_plan 补段后再生成章节卡。"
                ),
            )

        volume_plans_obj = VolumePlansOutput.model_validate(outline["volume_plans"])
        try:
            characters_for_guard = CharactersOutput.model_validate(outline["characters"])
        except ValidationError:
            # Legacy commits previously parsed characters only for typed drama.
            # Keep that compatibility; the generation/pre-mini paths are strict.
            characters_for_guard = None
        if characters_for_guard is not None:
            signature_overlaps = find_pending_signature_overlaps(
                volume_plans=volume_plans_obj, characters=characters_for_guard,
            )
            if signature_overlaps:
                message = signature_overlaps[0].feedback()
                raise OutlineBuilderError(
                    message, stage="volume_plans", raw_output=message,
                    model_used="outline_action_state_guard",
                    contradictions=[message],
                )
            repeated_signatures = find_repeated_completed_signatures(
                volume_plans=volume_plans_obj, characters=characters_for_guard,
            )
            if repeated_signatures:
                message = repeated_signatures[0].feedback()
                raise OutlineBuilderError(
                    message, stage="volume_plans", raw_output=message,
                    model_used="outline_action_state_guard",
                    contradictions=[message],
                )

        # acceptance 修复1(2026-06-10):mini_plan 存在时解析 items 供卷界重演
        # gate;解析失败(旧 schema / 手编损坏)→ warn + None,gate 跳过,
        # 维持 mini_plan"软依赖"语义不阻断 commit。
        mini_plan_obj: MiniPlanOutput | None = None
        mini_plan_items = None
        if outline.get("mini_plan") is not None:
            try:
                mini_plan_obj = MiniPlanOutput.model_validate(
                    outline["mini_plan"]
                )
                mini_plan_items = mini_plan_obj.items
            except Exception as exc:  # noqa: BLE001 — 软依赖,解析失败不阻断
                _log.warning(
                    "commit_outline_mini_plan_parse_failed",
                    workspace_id=workspace_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:200],
                    hint="卷界重演 gate 跳过(mini_plan 不可解析)",
                )

        typed_drama_selected = drama_validation_required(
            volume_plans=volume_plans_obj,
            mini_plan=mini_plan_obj,
            raw_volume_plans=outline.get("volume_plans"),
            raw_mini_plan=outline.get("mini_plan"),
            genre=workspace.genre,
        )
        if typed_drama_selected:
            try:
                if mini_plan_obj is None:
                    raise DramaticSourceContractError(
                        "typed drama outline 必须有可解析的 mini_plan source"
                    )
                characters_obj = CharactersOutput.model_validate(
                    outline["characters"]
                )
                plot_obj = PlotOutput.model_validate(outline["plot"])
                validate_outline_dramatic_sources(
                    characters=characters_obj,
                    plot=plot_obj,
                    volume_plans=volume_plans_obj,
                    mini_plan=mini_plan_obj,
                    genre=workspace.genre,
                )
            except (DramaticSourceContractError, ValidationError) as exc:
                message = f"outline typed drama source 校验失败:{exc}"
                raise OutlineBuilderError(
                    message,
                    stage="mini_plan",
                    raw_output=str(exc),
                    model_used="<deterministic-a1>",
                    contradictions=[message],
                    coverage_gaps=[],
                    evidence="typed volume/beat/mini-plan source chain",
                ) from exc

        # ---- gate ----
        self._run_commit_gates(
            workspace_id, volume_plans_obj.volumes, mini_plan_items=mini_plan_items
        )

        # ---- 落库 ----
        if self._session_factory is None:
            return []

        now = datetime.now(UTC)
        session = self._session_factory(workspace_id)
        try:
            for plan in volume_plans_obj.volumes:
                # upsert:若 volume_id 已存在,更新字段;否则插入
                existing = session.get(VolumeORM, plan.volume_id)
                if existing is not None:
                    existing.workspace_id = workspace_id
                    existing.title = plan.title
                    existing.summary = plan.summary
                    existing.chapter_count = plan.chapter_count
                    existing.tension_template = plan.tension_template
                    existing.arcs_json = [
                        arc.model_dump(mode="json") for arc in plan.arcs
                    ]
                    arc_finale_chapters = {
                        arc.chapter_range[1] for arc in plan.arcs
                    }
                    existing.tension_curve_json = [
                        {
                            "chapter_number": i + 1,
                            "tension": plan.tension_curve[i],
                            "is_arc_finale": (i + 1) in arc_finale_chapters,
                        }
                        for i in range(len(plan.tension_curve))
                    ]
                    existing.last_updated_at = now
                else:
                    session.add(
                        _volume_plan_item_to_orm(
                            workspace_id=workspace_id, plan=plan, now=now
                        )
                    )
            session.commit()
            rows = (
                session.query(VolumeORM)
                .filter(VolumeORM.workspace_id == workspace_id)
                .all()
            )
            _log.info(
                "outline_committed",
                workspace_id=workspace_id,
                volumes_count=len(rows),
            )
            committed_rows = list(rows)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        # P5-B-07:落库成功后翻 workspace.outline_status = 'committed'(独立 session,
        # 避免 volume upsert 出错时半翻 outline_status)。
        self._set_outline_status(workspace_id, "committed")

        # A-02 hot-fix:把 outline 阶段 characters.json 角色批落 characters 表
        # (幂等,已存在跳过;单角色派生失败 swallow + 不阻断 commit)。
        characters_payload = outline["characters"]
        if characters_payload is not None:
            try:
                outline_chars = CharactersOutput.model_validate(characters_payload)
            except Exception as exc:  # noqa: BLE001
                _log.error(
                    "outline_characters_validate_failed",
                    workspace_id=workspace_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
            else:
                self._persist_outline_characters(
                    workspace_id, outline_chars.characters
                )

        # B2-01 hot-fix:把 outline 阶段 plot.long_threads 批落 plot_threads 表
        # (幂等,thread_id 已存在跳过;单 thread 派生失败 swallow + 不阻断 commit)。
        plot_payload = outline["plot"]
        if plot_payload is not None:
            try:
                outline_plot = PlotOutput.model_validate(plot_payload)
            except Exception as exc:  # noqa: BLE001
                _log.error(
                    "outline_plot_validate_failed",
                    workspace_id=workspace_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
            else:
                self._persist_outline_plot_threads(
                    workspace_id, outline_plot.long_threads
                )

        return committed_rows

    # ---- 内部:批落 outline characters → characters 表(A-02 hot-fix)--------

    def _persist_outline_characters(
        self,
        workspace_id: str,
        items: list[CharacterOutlineItem],
    ) -> int:
        """把 outline 阶段 ``characters.json`` 角色批落 ``characters`` 表。

        - ``session_factory=None`` 时 noop(CLI / 单测纯文件路径)。
        - **幂等**:已存在的 ``character_id`` 跳过(沿用 audit §A-02 字面)。
        - 单角色派生 / 落库失败 → ``swallow + structlog warning``,不阻断 commit
          + 不影响其他角色 → 让用户能通过 CharactersPage PUT 补漏。
        - 返回成功新增条数(供 caller 日志 / 测试断言)。
        """
        if self._session_factory is None:
            return 0
        session = self._session_factory(workspace_id)
        created_count = 0
        try:
            for item in items:
                # 用 savepoint 隔离单角色失败,不污染整批 commit
                savepoint = session.begin_nested()
                try:
                    existing = get_character(session, item.character_id)
                    if existing is not None:
                        savepoint.rollback()
                        continue
                    char = _outline_character_to_character(
                        workspace_id=workspace_id, item=item
                    )
                    create_character(session, char)
                    savepoint.commit()
                    created_count += 1
                except Exception as exc:  # noqa: BLE001
                    savepoint.rollback()
                    _log.error(
                        "outline_character_persist_failed",
                        workspace_id=workspace_id,
                        character_id=item.character_id,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
            session.commit()
            _log.info(
                "outline_characters_persisted",
                workspace_id=workspace_id,
                created_count=created_count,
                total_count=len(items),
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        return created_count

    # ---- 内部:批落 outline plot.long_threads → plot_threads 表(B2-01 hot-fix)

    def _persist_outline_plot_threads(
        self,
        workspace_id: str,
        items: list[PlotLongThread],
    ) -> int:
        """把 outline 阶段 ``plot.long_threads`` 批落 ``plot_threads`` 表。

        - ``session_factory=None`` 时 noop(CLI / 单测纯文件路径)。
        - **幂等**:已存在的 ``thread_id`` 跳过(沿用 A-02 同模式)。
        - **savepoint 隔离**:`session.begin_nested()` per thread,单 thread
          失败 rollback savepoint + 不污染整批 commit。
        - 单 thread 派生 / 落库失败 → swallow + structlog error,不阻断 commit
          + 不影响其他 thread → 让用户能通过 ThreadsPage PUT 补漏。
        - 返回成功新增条数(供 caller 日志 / 测试断言)。
        """
        if self._session_factory is None:
            return 0
        session = self._session_factory(workspace_id)
        created_count = 0
        now = datetime.now(UTC)
        try:
            for item in items:
                savepoint = session.begin_nested()
                try:
                    existing = get_plot_thread(session, item.thread_id)
                    if existing is not None:
                        savepoint.rollback()
                        continue
                    thread = _outline_plot_thread_to_thread(
                        workspace_id=workspace_id, item=item, now=now
                    )
                    create_plot_thread(
                        session,
                        thread,
                        origin="outline",
                        expiry_policy="never",
                    )
                    savepoint.commit()
                    created_count += 1
                except Exception as exc:  # noqa: BLE001
                    savepoint.rollback()
                    _log.error(
                        "outline_plot_thread_persist_failed",
                        workspace_id=workspace_id,
                        thread_id=item.thread_id,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
            session.commit()
            _log.info(
                "outline_plot_threads_persisted",
                workspace_id=workspace_id,
                created_count=created_count,
                total_count=len(items),
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        return created_count

    # ---- 内部:workspace.outline_status 翻新(P5-B-07)-----------------------

    def _set_outline_status(self, workspace_id: str, status: str) -> None:
        """更新 ``WorkspaceORM.outline_status`` 字段(P5-B-07,启动指令字面)。

        ``session_factory=None`` 时 noop(CLI / 单测纯文件路径)。ORM 行不存在
        时调 ``ensure_workspace_orm``(asset_import 模式,从 meta.json 同步建行
        — 已校验工作区存在,这里 ensure 是安全的);**不**自行复制 meta.json,
        统一走 ``asset_import.ensure_workspace_orm`` 单一入口。

        失败 rollback + 抛(由 caller 决定是否 swallow);outline:generate 失败
        时调用方让 outline_status 留在 ``generating`` 是"用户可见状态",不在
        本 helper 处兜底。
        """
        if self._session_factory is None:
            return
        from src.services.asset_import import ensure_workspace_orm

        workspace = self._workspace_service.info(workspace_id)
        session = self._session_factory(workspace_id)
        try:
            ensure_workspace_orm(session, workspace)
            row = session.get(WorkspaceORM, workspace_id)
            assert row is not None, "ensure_workspace_orm 后 ORM 行应存在"
            row.outline_status = status
            session.commit()
            _log.info(
                "workspace_outline_status_set",
                workspace_id=workspace_id,
                outline_status=status,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ---- 内部:gate 集中点 --------------------------------------------------

    def _run_commit_gates(
        self,
        workspace_id: str,
        volume_plans: list[VolumePlanItem],
        *,
        mini_plan_items: list | None = None,
    ) -> None:
        """outline:commit 端点前置 gate(对齐 P4-acceptance ``_run_gates`` 模式)。

        两道 [HARD] gate:

        1. ``validate_arc_chapter_spans``(副本横跨规则,纯代码);
        2. ``validate_volume_seam_replay``(卷界事件重演,粗筛 + LLM 判;v1.1
           acceptance 修复1,2026-06-10)— ``mini_plan_items`` 为 None(旧工作
           区缺段 / 解析失败)时跳过;judge LLM 异常 fail-open 不阻断 commit
           (validator 内部处理),只有**判定为 replay** 才硬阻断。

        任一规则违规 → ``OutlineGateViolation`` + structlog
        ``outline_commit_gate_failed`` 事件。
        """
        failures = validate_arc_chapter_spans(volume_plans)
        if failures:
            _log.warning(
                "outline_commit_gate_failed",
                workspace_id=workspace_id,
                rule="arc_too_short",
                violations=[f.model_dump(mode="json") for f in failures],
                count=len(failures),
            )
            raise OutlineGateViolation(failures)

        if mini_plan_items:
            router = (
                self._router_factory(workspace_id)
                if self._router_factory is not None
                else None
            )
            # v2 间隙 A3②:seam judge 的 llm_response 工作区级绑定(同上)。
            with bind_workspace_context(workspace_id):
                seam_result = validate_volume_seam_replay(
                    mini_plan_items=mini_plan_items,
                    workspace_id=workspace_id,
                    router=router,
                    client=self._client,
                    model=self._model,
                )
            if not seam_result.passed:
                seam_failures = [
                    SeamReplayGateFailure(
                        prev_chapter_id=f.prev_chapter_id,
                        next_chapter_id=f.next_chapter_id,
                        confidence=f.confidence,
                        evidence=f.evidence,
                    )
                    for f in seam_result.findings
                ]
                _log.warning(
                    "outline_commit_gate_failed",
                    workspace_id=workspace_id,
                    rule="volume_seam_replay",
                    violations=[
                        f.model_dump(mode="json") for f in seam_failures
                    ],
                    count=len(seam_failures),
                    llm_calls_count=seam_result.llm_calls_count,
                )
                raise OutlineGateViolation(seam_failures)

    # ---- 内部:WS publish 5 段 stage -----------------------------------------

    def _publish_stage(
        self, task_id: str | None, stage: str
    ) -> None:
        """WS publish ``stage`` event 到 ``task.{task_id}.progress`` 频道。

        v1.0.x wave 3 Commit B follow-up(2026-05-21):本方法对 outline
        stage 名(world / characters / plot / volume_plans / mini_plan,
        v1.0.x wave 4 session 1 起加 mini_plan)推送 ``event="stage"``;
        **不再 emit "done" stage** — 整体完成的 done event 由路由层 wrapper
        (``_run_outline_generate_task``)在 ``task_registry.update_to_done``
        之后统一 publish ``event="done"``,对齐 chapters.py /
        outline:regenerate-section 同型模式。

        ``task_id`` 或 ``ws_manager`` 任一为 None 时 noop(CLI / 单测纯调用)。
        失败 swallow + structlog ``outline_stage_notify_failed`` 事件
        (对齐 P4-B-08 ``cost_guard_notify_failed`` swallow 模式 — WS 推
        送失败不应阻断 outline 生成主流程)。
        """
        if task_id is None or self._ws_manager is None:
            return
        try:
            self._ws_manager.publish(
                channel=task_progress_channel(task_id),
                event="stage",
                data={
                    "stage": stage,
                    "progress": _STAGE_PROGRESS.get(stage, 0.0),
                },
            )
        except Exception as exc:  # noqa: BLE001 — swallow per P4-B-08 同源
            _log.error(
                "outline_stage_notify_failed",
                task_id=task_id,
                stage=stage,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

    def _publish_parallel_status(
        self,
        task_id: str | None,
        *,
        stage: str,
        snapshot: dict[str, Any],
    ) -> None:
        """发布并行子任务的全量快照；乱序帧不会靠增量拼出假状态。"""

        if task_id is None or self._ws_manager is None:
            return
        try:
            self._ws_manager.publish(
                channel=task_progress_channel(task_id),
                event="stage",
                data={
                    "stage": stage,
                    "progress": _STAGE_PROGRESS.get(stage, 0.0),
                    **snapshot,
                },
            )
        except Exception as exc:  # noqa: BLE001 — UI 投影不得阻断 Provider 主链
            _log.error(
                "outline_parallel_status_notify_failed",
                task_id=task_id,
                stage=stage,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )


__all__ = [
    "OutlineGateViolation",
    "OutlineLengthCalibrationRequired",
    "OutlineNotFound",
    "OutlineSection",
    "OutlineService",
    "OutlineServiceError",
    "SessionFactory",
]
