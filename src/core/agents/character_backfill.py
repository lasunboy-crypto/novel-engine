"""机制1:大纲角色完整性回填(v2 W3.5-s4-step1,§2.58)。

``build_outline`` 在 **plot 之后、volume_plans 之前**调用一次,检出 plot 把某角色
当真实故事角色引用(失踪的导师 / 接头的向导等)、但 characters 段没建卡的"指代
悬空"角色,补全角色卡 → 让 volume_plans 生成时拿到**完整角色表**,从源头掐断
"抓邻近已建卡角色顶替"造成的张冠李戴混淆。

v2 W3.5 C-7b 起,同一机制在 **volume_plans 之后、mini_plan 之前**再调用一次:
检出卷级规划中新增使用、但名册仍未建卡的关键人物,让 mini_plan 每卷开跑前能看到
更完整的角色池。

**根因(s4-step1 实证)**:大纲 5 段串行**软交接** —— characters 先于 plot 生成,
plot 临时引入的关键角色无卡 → volume_plans 把它和邻近已建卡角色混成一人 →
``outline_consistency_validator`` HARD 拦。两次失败共此根因:
  · deepseek:"博拉尔"在 plot 当地名、在 volume_plans 当人(向导),且无角色卡;
  · qwen:"导师/恩师"(被反派做成服务器、留下笔记的核心驱动者)无卡 → volume_plans
    把它和"师伯魏致远"混成"导师魏致远"。

**定位**:这是"层 3(plot)→ 层 4(volume_plans)软交接"的预防式硬化,与下游
``outline_consistency_validator``(§2.50,检测)+ §2.57 name_typo 自动修(确定性归一)
互补 —— 本机制治"角色未建卡"这一**公共根因**。

设计纪律:
  · legacy 回填异常保留原有返回原角色的行为；实验管线必须取得有效补人结论，
    setup / LLM / 解析 / 合并失败不能伪装成无需补人，交原大纲错误路径停止。
  · **保守**:prompt 偏向**不补**(只补确属剧情关键、与现有卡明显不同的角色);
    宁可漏补(validator 仍会拦)也不误造重复/边角角色污染人设。
  · **护栏**:``character_id`` + ``name`` 双重去重 + 总数封顶 8(``CharactersOutput``
    上限);**只追加、不改既有卡**;合并后 ``CharactersOutput`` 重校验失败 → fail-open。
  · **路由**:复用 ``outline_builder`` agent key(跟随 outline 模型 —— qwen 现 /
    deepseek 生产,零 config 改动,与 mini_plan 同理);独立 structlog 事件保观测。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, Field, ValidationError

from src.config import settings
from src.core.agents.structured_handoff import HandoffError, handoff_user_text
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents._retry_feedback import SchemaRetryExhausted, run_with_schema_retry
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.agents.creative_delta import (
    EXPERIMENTAL_PIPELINE_CONTRACT,
    uses_experimental_pipeline,
)
from src.core.llm.base import LLMClient, LLMResponse, Message
from src.core.models.outline import (
    MAX_INITIAL_CHARACTER_CORE_FACTS,
    CharacterOutlineItem,
    CharactersOutput,
)

if TYPE_CHECKING:
    from src.core.models.outline import PlotOutput, VolumePlansOutput, WorldOutput
    from src.core.routing.router import ModelRouter

_log = structlog.get_logger(__name__)

# router.route(agent_name) 入参:复用 outline_builder key,跟随 outline 模型。
_AGENT_NAME = "outline_builder"
# run_with_schema_retry / role marker 的 stage 标签(独立于 _AGENT_NAME,供 mock 路由 +
# retry 日志区分本机制)。
_STAGE = "character_backfill"
_PROMPT_SUBDIR = "outline_builder"
_PROMPT_FILENAME = "character_backfill.md"

# CharactersOutput.max_length 上限;合并不得越过(否则 schema 校验失败)。
_MAX_CHARACTERS = 8

# 默认生成参数(legacy / router=None 路径用;router 路径由 profile 决定)。
# 补卡含多张完整角色卡(11 字段 / 张)→ token 略宽;温度中低保稳。
_DEFAULT_TEMPERATURE = 0.4
_DEFAULT_MAX_TOKENS = 3500

# 沿用 outline_consistency_validator 同型格式剥离(```json ... ``` 包裹 + trailing 注释)。
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)
_TRAILING_HTML_COMMENT_RE = re.compile(r"\s*<!--.*?-->\s*$", re.DOTALL)


def _strip_format_noise(text: str) -> str:
    """剥离 LLM 输出的 ```json``` 代码围栏 + trailing HTML 注释。"""
    m = _CODE_FENCE_RE.match(text)
    if m:
        text = m.group("body")
    text = _TRAILING_HTML_COMMENT_RE.sub("", text)
    return text.strip()


class BackfillPayload(BaseModel):
    """LLM 输出中间模型:plot 引用但 characters 没建卡的角色卡列表(可空)。

    ``missing_characters`` 空 = LLM 判定无指代悬空角色(健康大纲常态)。
    每条是完整 ``CharacterOutlineItem``(11 字段),合并护栏在 caller 侧做。
    """

    missing_characters: list[CharacterOutlineItem] = Field(default_factory=list)


class CharacterBackfillError(ValueError):
    """实验补人未取得有效结论，保留原始失败供大纲错误路径报告。"""

    def __init__(
        self, message: str, *, raw_output: str = "", model_used: str = "",
        attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.raw_output = raw_output
        self.model_used = model_used
        self.attempts = attempts


def _format_volume_plans_context(volume_plans: VolumePlansOutput) -> str:
    """把卷级规划压成 backfill 可读的文本上下文。"""
    lines: list[str] = []
    for volume in volume_plans.volumes:
        lines.append(
            f"### {volume.volume_id} | {volume.title}\n"
            f"- summary:{volume.summary}\n"
            f"- linked_threads:{volume.linked_threads}"
        )
        for arc in volume.arcs:
            lines.append(
                f"  - arc[{arc.arc_id}] {arc.phase}/{arc.arc_type}:{arc.summary}"
            )
            for beat in arc.beats:
                start, end = beat.chapter_range
                lines.append(
                    f"    - beat[{beat.beat_id}] ch{start}-{end}:"
                    f"{beat.summary}; must_advance={beat.must_advance};"
                    f" forbidden_advance={beat.forbidden_advance}"
                )
    return "\n".join(lines)


def _build_user_message(
    *,
    world: WorldOutput,
    characters: CharactersOutput,
    plot: PlotOutput,
    volume_plans: VolumePlansOutput | None = None,
    premise: str,
    genre: str,
) -> str:
    """同一次回填先见完整人物及世界资料，避免只靠姓名标签另编既定前史。"""
    genre_text = genre if genre.strip() else "(未指定)"
    existing = characters.model_dump_json(indent=2)
    used_facts = sum(len(c.initial_core_facts) for c in characters.characters)
    remaining_facts = MAX_INITIAL_CHARACTER_CORE_FACTS - used_facts
    phases = "\n".join(
        f"  - [{p.phase_id}] {p.title}:{p.summary}" for p in plot.main_phases
    )
    turns = "\n".join(
        f"  - [{t.turning_id}] {t.title}:{t.description}"
        for t in plot.major_turning_points
    )
    threads = "\n".join(
        f"  - [{th.thread_id}] {th.title}:{th.description}" for th in plot.long_threads
    )
    arcs = "\n".join(f"  - {cid}:{arc}" for cid, arc in plot.character_arcs.items())
    volume_section = ""
    scan_scope = "plot"
    if volume_plans is not None:
        scan_scope = "plot 与 volume_plans"
        volume_section = (
            "\n\n## 卷级规划(volume_plans,只读;检索卷级新增使用但未建卡的关键人物)\n\n"
            f"{_format_volume_plans_context(volume_plans)}"
        )

    return (
        "## 用户输入\n\n"
        f"- 一句话设定(premise):{premise}\n"
        f"- 题材(genre):{genre_text}\n\n"
        "## 世界观(world,五项完整资料，只读)\n\n"
        f"- setting:{world.setting}\n"
        f"- power_system:{world.power_system}\n"
        f"- factions:{world.factions}\n"
        f"- culture:{world.culture}\n"
        f"- unique_rules:{world.unique_rules}\n\n"
        "## 已建卡的完整人物资料(characters,只读;**不要重复生成这些**)\n\n"
        f"{existing}\n\n"
        f"初始核心事实已用 {used_facts} 条，剩余 {remaining_facts} 条。"
        "新增人物的 initial_core_facts 合计不得超过剩余额度；剩余为 0 时显式写 []。"
        "这是结构化事实登记的数量限制，开篇前已成立的人物背景和关系仍应完整写出，"
        "不得删改已有角色或已有事实腾出名额。\n\n"
        "## 剧情主线(plot,**在此检索被当真实角色引用、但上面名册没有的角色**)\n\n"
        f"### core_conflict\n{plot.core_conflict}\n\n"
        f"### main_phases\n{phases}\n\n"
        f"### major_turning_points\n{turns}\n\n"
        f"### long_threads\n{threads}\n\n"
        f"### character_arcs\n{arcs}\n\n"
        f"{volume_section}\n\n"
        f"请只为「{scan_scope} 中剧情关键、被当真实角色对待、却不在上面名册」的角色补全角色卡;"
        "若无(剧情只用了名册内角色),返回空列表 missing_characters: []。"
    )


def _merge_backfill_candidates(
    characters: CharactersOutput, payload: BackfillPayload,
) -> tuple[list[CharacterOutlineItem], list[CharacterOutlineItem], list[str], list[str]]:
    """返回同一份只追加候选，供返回校验及正式合并共用去重/人数上限。"""
    existing_ids = {c.character_id for c in characters.characters}
    existing_names = {c.name for c in characters.characters}
    merged = list(characters.characters)
    added: list[CharacterOutlineItem] = []
    skipped_dup: list[str] = []
    skipped_cap: list[str] = []
    for item in payload.missing_characters:
        if item.character_id in existing_ids or item.name in existing_names:
            skipped_dup.append(item.character_id)
            continue
        if len(merged) >= _MAX_CHARACTERS:
            skipped_cap.append(item.character_id)
            continue
        merged.append(item)
        added.append(item)
        existing_ids.add(item.character_id)
        existing_names.add(item.name)
    return merged, added, skipped_dup, skipped_cap


def _validate_backfill_against_existing(
    result: BaseModel, characters: CharactersOutput,
    *, require_explicit_result: bool = False,
) -> None:
    _require_explicit_initial_core_facts(result)
    assert isinstance(result, BackfillPayload)
    if require_explicit_result and "missing_characters" not in result.model_fields_set:
        raise ValueError("必须显式返回 missing_characters；无需补人时也须返回空数组")
    merged, _, _, _ = _merge_backfill_candidates(characters, result)
    # 合并才出现的全书事实额度错误须进入原有反馈，不能返回成功后再丢掉整个人物。
    CharactersOutput(characters=merged)


def backfill_plot_referenced_characters(
    *,
    world: WorldOutput,
    characters: CharactersOutput,
    plot: PlotOutput,
    volume_plans: VolumePlansOutput | None = None,
    premise: str = "",
    genre: str = "",
    workspace_id: str | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> tuple[CharactersOutput, list[CharacterOutlineItem]]:
    """机制1:检出 plot/volume 引用、characters 没建卡的角色 → 补卡。

    :return: ``(updated_characters, added_items)``。``added_items`` 空 = no-op
        (``updated_characters`` 即原 ``characters``,引用不变)。
    :note: 实验管线的异常抛出 CharacterBackfillError；合法空数组仍是无需补人。
    """
    # 已满 8 张 → 无空位可补,直接 no-op(省一次 LLM 调用)。
    if len(characters.characters) >= _MAX_CHARACTERS:
        _log.debug(
            "character_backfill_skipped_full",
            workspace_id=workspace_id,
            character_count=len(characters.characters),
        )
        return characters, []

    experimental = (
        getattr(router, "pipeline_contract", None) == EXPERIMENTAL_PIPELINE_CONTRACT
        or (client is not None and uses_experimental_pipeline(client))
    )
    # ---- setup：实验管线不得把缺提示或路由失败当作无需补人 ----
    try:
        resolved_client, resolved_model, complete_kwargs = resolve_routing(
            agent_name=_AGENT_NAME,
            router=router,
            client=client,
            model=model,
            legacy_kwargs={"max_tokens": max_tokens, "temperature": temperature},
        )
        experimental = experimental or uses_experimental_pipeline(resolved_client)
        template_root = (
            prompts_dir if prompts_dir is not None else settings.prompts_dir
        ).resolve()
        system_text = load_prompt_template(
            template_root / _PROMPT_SUBDIR / (
                "character_backfill_narrative_v3.md" if world.story_intent is not None and world.story_intent.version >= 6 else
                "character_backfill_narrative_v2.md" if world.story_intent is not None else _PROMPT_FILENAME
            )
        )
        complete_kwargs = prepare_strict_json_output(
            client=resolved_client,
            model=resolved_model,
            output_model=BackfillPayload,
            schema_name="character_backfill",
            complete_kwargs=complete_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open 含 setup 阶段
        if isinstance(exc, HandoffError):
            raise
        if experimental:
            raise CharacterBackfillError("人物补充准备失败，不能当作无需补人") from exc
        _log.warning(
            "character_backfill_setup_failed_open",
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return characters, []

    user_text = append_role_marker(
        _build_user_message(
            world=world,
            characters=characters,
            plot=plot,
            volume_plans=volume_plans,
            premise=premise,
            genre=genre,
        ),
        _STAGE,  # type: ignore[arg-type]
    )

    user_text = handoff_user_text(
        resolved_client, stage="character_backfill",
        data={"world": world, "characters": characters, "plot": plot, "volume_plans": volume_plans, "premise": premise, "genre": genre}, original_task=user_text,
    )
    def validate_result(result: BackfillPayload) -> None:
        _validate_backfill_against_existing(result, characters, require_explicit_result=experimental)
        if world.story_intent is not None and any(
            not item.present_desire.strip() or not item.personal_stakes.strip()
            for item in result.missing_characters
        ):
            raise ValueError("新叙事补人须保留眼前愿望和切身利害")

    def llm_call(messages: list[Message]) -> LLMResponse:
        return resolved_client.complete(
            messages=messages, model=resolved_model, **complete_kwargs
        )

    # ---- LLM 调用 + schema retry → fail-open ----
    try:
        payload, _attempts, _model_used = run_with_schema_retry(
            agent_name=_AGENT_NAME,
            stage=_STAGE,
            system_text=system_text,
            initial_user_text=user_text,
            output_model=BackfillPayload,
            llm_call=llm_call,
            strip_format_noise=_strip_format_noise,
            max_attempts=settings.outline_builder_max_retry_attempts,
            post_validate=validate_result,
        )
    except SchemaRetryExhausted as exc:
        if experimental:
            raise CharacterBackfillError(
                "人物补充在原有格式重试后仍无有效结果，已停止后续大纲生成",
                raw_output=exc.last_response_content, model_used=exc.last_model_used,
                attempts=exc.attempts,
            ) from exc
        _log.warning(
            "character_backfill_failed_open",
            workspace_id=workspace_id,
            reason="schema_retry_exhausted",
            attempts=exc.attempts,
        )
        return characters, []
    except Exception as exc:  # noqa: BLE001 — fail-open
        if isinstance(exc, HandoffError):
            raise
        if experimental:
            raise CharacterBackfillError(
                "人物补充调用失败，不能当作无需补人", model_used=resolved_model,
                attempts=1,
            ) from exc
        _log.warning(
            "character_backfill_failed_open",
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return characters, []

    # ---- 合并护栏:去重(id + name)+ 封顶 8 + 只追加 ----
    merged, added, skipped_dup, skipped_cap = _merge_backfill_candidates(characters, payload)
    if experimental and skipped_cap:
        raise CharacterBackfillError(
            f"核心人物最多 {_MAX_CHARACTERS} 人，当前补充结果有 {skipped_cap} 人无法容纳；"
            "未保存部分合并，也未视为补齐。请调整核心名册与剧情分工后重试。",
            raw_output=payload.model_dump_json(), model_used=_model_used, attempts=_attempts,
        )

    if not added:
        _log.debug(
            "character_backfill_noop",
            workspace_id=workspace_id,
            skipped_dup=skipped_dup,
            skipped_cap=skipped_cap,
        )
        return characters, []

    # 合并后重校验(unique id / 4-8 长度);失败 → fail-open(不污染下游)。
    try:
        updated = CharactersOutput(characters=merged)
    except ValidationError as exc:
        if experimental:
            raise CharacterBackfillError(
                "人物补充合并校验失败，不能丢弃结果后继续",
                model_used=_model_used, attempts=_attempts,
            ) from exc
        _log.warning(
            "character_backfill_merge_invalid_failed_open",
            workspace_id=workspace_id,
            error_message=str(exc),
            added_attempt=[i.character_id for i in added],
        )
        return characters, []

    _log.info(
        "character_backfill_completed",
        workspace_id=workspace_id,
        added=[(i.character_id, i.name, i.role) for i in added],
        added_count=len(added),
        skipped_dup=skipped_dup,
        skipped_cap=skipped_cap,
    )
    return updated, added


def _require_explicit_initial_core_facts(result: BaseModel) -> None:
    """回填角色与主 characters 段共用显式初始基线语义。"""

    if not isinstance(result, BackfillPayload):
        raise ValueError("character_backfill 返回类型错误")
    missing = [
        item.character_id
        for item in result.missing_characters
        if "initial_core_facts" not in item.model_fields_set
    ]
    if missing:
        raise ValueError(
            "每个回填角色都必须显式输出 initial_core_facts（没有则 []）: "
            + ", ".join(missing)
        )
