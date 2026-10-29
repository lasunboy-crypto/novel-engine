"""volume_seam_replay LLM-as-judge validator(v1.1 acceptance 修复1,2026-06-10)。

职责:在 ``outline:commit`` 期对 mini_plan 的**每个卷界**(卷 N 末章 → 卷 N+1
首章)做"事件重演"自检 —— 新卷首章把前卷末章**已完结**的事件重新安排 / 重新
解决一遍 → 该章 must_achieve 与已写正文 / 连续性记忆冲突,写作期**永不可达成**
→ rewrite budget 耗尽 → human_intervention 孤儿章。

现场(luo-accept-v1 ch-0007,2026-06-10):ch-0006(v1 末章)one_line 悬念式措辞
"险些掐死流浪汉"(结局只在 arc_beats),v2 生成时滚动上下文只渲染 one_line +
指令只防"重启"不防"重演" → ch-0007 one_line 第一分句重演"强行压制副人格救下
流浪汉" → 4 版全败 must_achieve。Fix B(渲染补收束 + prompt 反重演)治生成端,
本 validator 是 commit 期**硬闸门**(prompt 软约束会被击穿的教训即来自该现场)。

两段式设计(对齐 B4 cross_chapter_dedup"代码粗筛 + LLM 判意图"模式)
====================================================================

1. **代码粗筛(确定性,零 LLM)**:卷界两侧文本 char-bigram 包含度 ≥
   ``SEAM_PRESCREEN_BIGRAM_THRESHOLD`` 才进 LLM 判。
   **标定实证(2026-06-10,luo-accept-v1 + luo-budget-v1 全 8 卷界 + 同卷相邻
   4 对)**:真重演 6/7 = 0.175;干净卷界 0.06-0.186(budget 6/7 话题延续高达
   0.186 > 真重演!);同卷正常承接 0.02-0.075。结论:**表面重叠分不开语义重演**
   (干净话题延续可比真重演更高)→ 粗筛只当**成本过滤器**(阈值 0.10 偏低,
   宁多送 LLM 判),语义裁决全靠 judge。每次 commit 预期 1-3 次 LLM 调用。
2. **LLM 判意图**:把(前卷末章 one_line + arc_beats 已完结收束 + info_increment)
   与(新卷首章 one_line)给 judge,judge 按"同一事件被重新安排 / 重新解决"
   高确信标准判 replay;后果延续 / 行动线下一阶段 / 真悬念解决都明示不报。

**残余风险(诚实标注)**:改写式重演若 bigram 包含度 < 0.10 会漏过粗筛(本
validator 不兜底);write 期 must 可行性预检(收官修复2 / Fix D)是更深一道网。

fail-open 语义(对齐 mechanism_consistency_validator):LLM / setup 异常 →
该卷界跳过 + warn log,**不阻断 commit**(judge 挂了不应让用户提交不了大纲)。

路由:agent_name ``"volume_seam_replay_validator"`` → 已注册
``LLM_AS_JUDGE_AGENTS``(thinking-on hard guard 保护,语义判定需深度斟酌);
config.yaml ``agent_routing.volume_seam_replay_validator: cloud_qwen_max_thinking``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog
from pydantic import BaseModel, Field

from src.config import settings
from src.core.agents._json_contract import loads_with_quote_repair
from src.core.agents._prompt import (
    append_role_marker,
    load_prompt_template,
    resolve_routing,
)
from src.core.agents._structured_output import prepare_strict_json_output
from src.core.llm.base import LLMClient, Message
from src.core.routing.router import ModelRouter

if TYPE_CHECKING:
    from src.core.models.outline import MiniPlanItem

_log = structlog.get_logger(__name__)

_VALIDATOR_PROMPT_FILENAME = "volume_seam_replay_validator_v1.md"

# 默认生成参数:单卷界判定输出 JSON 3 字段,token 上限小;语义判定对采样稳定性
# 敏感,温度低。router 路径下由 profile 决定。
_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 500

#: 粗筛阈值(char-bigram 包含度,2026-06-10 真实语料标定 — 见模块 docstring)。
#: 真重演 0.175 稳超;同卷正常承接 0.02-0.075 稳不触;干净卷界话题延续可达
#: 0.186(粗筛会送 LLM,由 judge 排除)。阈值偏低是设计意图:粗筛是成本过滤器
#: 不是语义裁决,多送一次 LLM 判(每书卷界仅 ~4 个)远好于漏一次死锁。
SEAM_PRESCREEN_BIGRAM_THRESHOLD: Final[float] = 0.10

#: 任一侧 bigram 集合 < 该值视作短文本(测试 fixture / 占位文本),粗筛直接 0 分。
_MIN_BIGRAMS_FOR_PRESCREEN: Final[int] = 10

# 标点 / 空白剥离(bigram 只看实词字符)。
_PUNCT_RE = re.compile(r"[\s,。;:、!?“”‘’\"'()()【】《》—\-…·]+")

# 匹配 ```json ... ``` 或 ``` ... ``` 包裹(沿用 beat_alignment_validator 同型剥离)。
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)


@dataclass(frozen=True)
class SeamReplayFinding:
    """单个卷界的重演判定(replay=true 才产出)。"""

    prev_chapter_id: str
    next_chapter_id: str
    confidence: float
    evidence: str


@dataclass(frozen=True)
class SeamReplayResult:
    """validator 输出。

    - ``passed``:``findings`` 为空时 True(含全部 fail-open 的情况)。
    - ``findings``:judge 判 replay=true 的卷界列表。
    - ``seams_total``:mini_plan 内卷界总数。
    - ``seams_prescreen_fired``:粗筛触发(送 LLM 判)的卷界数。
    - ``llm_calls_count``:实际 LLM 调用次数。
    - ``fail_open_count``:LLM / setup 异常 fail-open 跳过的卷界数。
    """

    passed: bool
    findings: list[SeamReplayFinding] = field(default_factory=list)
    seams_total: int = 0
    seams_prescreen_fired: int = 0
    llm_calls_count: int = 0
    fail_open_count: int = 0


class _SeamLLMPayload(BaseModel):
    """LLM 输出的中间模型(校验 3 字段)。"""

    replay: bool
    confidence: float = Field(..., ge=0.0, le=1.0)
    evidence: str = Field(..., min_length=1, max_length=400)


def _char_bigrams(text: str) -> set[str]:
    cleaned = _PUNCT_RE.sub("", text)
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def seam_suspicion_score(next_first_text: str, prev_last_text: str) -> float:
    """卷界粗筛分:两侧 char-bigram 包含度(交集 / 较小侧)。

    短文本(任一侧 bigram < ``_MIN_BIGRAMS_FOR_PRESCREEN``)→ 0.0,天然绕过
    单测 / mock fixture 的占位文本。
    """
    a = _char_bigrams(next_first_text)
    b = _char_bigrams(prev_last_text)
    if len(a) < _MIN_BIGRAMS_FOR_PRESCREEN or len(b) < _MIN_BIGRAMS_FOR_PRESCREEN:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def iter_volume_seams(
    items: list[MiniPlanItem],
) -> list[tuple[MiniPlanItem, MiniPlanItem]]:
    """按 items 顺序提取卷界对:(卷 N 末 item, 卷 N+1 首 item)。

    依赖 ``MiniPlanOutput.items`` 的(volume, chapter_number)有序约定
    (``generate_mini_plan`` 落盘前已排序);单卷 / 空列表 → 空。
    """
    seams: list[tuple[MiniPlanItem, MiniPlanItem]] = []
    for prev, nxt in zip(items, items[1:], strict=False):
        if prev.volume_id != nxt.volume_id:
            seams.append((prev, nxt))
    return seams


def _prev_side_text(item: MiniPlanItem) -> str:
    parts = [item.one_line_event]
    if item.arc_beats:
        parts.append(";".join(item.arc_beats.values()))
    return ";".join(parts)


def _build_user_message(prev: MiniPlanItem, nxt: MiniPlanItem) -> str:
    resolved = ";".join(prev.arc_beats.values()) if prev.arc_beats else "(无标注)"
    msg = (
        "## 待判定卷界\n\n"
        f"### 前一卷末章({prev.chapter_id},{prev.volume_id})\n"
        f"- one_line_event:{prev.one_line_event}\n"
        f"- 本章内已完结的收束:{resolved}\n"
        f"- info_increment:{prev.info_increment}\n\n"
        f"### 新卷首章({nxt.chapter_id},{nxt.volume_id})\n"
        f"- one_line_event:{nxt.one_line_event}\n\n"
        "请按系统提示词标准判定新卷首章是否重演了前一卷末章已完结的事件,"
        "输出严格 JSON。"
    )
    return append_role_marker(msg, role="volume_seam_replay_validator")


def _parse_llm_payload(raw_text: str, *, model_used: str) -> _SeamLLMPayload:
    text = raw_text.strip()
    fence = _CODE_FENCE_RE.match(text)
    if fence:
        text = fence.group("body").strip()
    return _SeamLLMPayload.model_validate(
        loads_with_quote_repair(
            text,
            agent_name="volume_seam_replay_validator",
            model_used=model_used,
        )
    )


def validate_volume_seam_replay(
    *,
    mini_plan_items: list[MiniPlanItem],
    workspace_id: str | None = None,
    router: ModelRouter | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
    prompts_dir: Path | None = None,
) -> SeamReplayResult:
    """对 mini_plan 全部卷界做"事件重演"判定(粗筛 + LLM 判,fail-open)。

    :param mini_plan_items: ``MiniPlanOutput.items``(有序)。
    :param workspace_id: 工作区 ID,落 structlog 字段(可选)。
    :param router: ``ModelRouter``;给定时按
        ``router.route("volume_seam_replay_validator")`` 取 client + 生成参数。
    :param client: legacy 路径 LLM 客户端;``router=None`` 时必传。
    :param model: legacy 路径模型名。
    :param max_tokens: legacy 路径 token 上限。
    :param temperature: legacy 路径温度。
    :param prompts_dir: 提示词模板根目录;``None`` 取 ``settings.prompts_dir``。
    :return: ``SeamReplayResult``。
    """
    seams = iter_volume_seams(mini_plan_items)
    if not seams:
        return SeamReplayResult(passed=True, seams_total=0)

    findings: list[SeamReplayFinding] = []
    fired = 0
    llm_calls = 0
    fail_open = 0
    setup: tuple[LLMClient, str | None, dict[str, object]] | None = None
    system_text: str | None = None

    for prev, nxt in seams:
        score = seam_suspicion_score(nxt.one_line_event, _prev_side_text(prev))
        if score < SEAM_PRESCREEN_BIGRAM_THRESHOLD:
            continue
        fired += 1

        # lazy setup:第一个触发粗筛的卷界才解析路由 / 读 prompt(多数 commit
        # 粗筛全不触发 → 零 setup 成本;setup 失败 → 全部 fail-open)。
        if setup is None:
            try:
                resolved_client, resolved_model, complete_kwargs = resolve_routing(
                    agent_name="volume_seam_replay_validator",
                    router=router,
                    client=client,
                    model=model,
                    legacy_kwargs={
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                    },
                )
                template_root = (
                    prompts_dir if prompts_dir is not None else settings.prompts_dir
                ).resolve()
                system_text = load_prompt_template(
                    template_root / _VALIDATOR_PROMPT_FILENAME
                )
                complete_kwargs = prepare_strict_json_output(
                    client=resolved_client,
                    model=resolved_model,
                    output_model=_SeamLLMPayload,
                    schema_name="volume_seam_replay",
                    complete_kwargs=complete_kwargs,
                )
                setup = (resolved_client, resolved_model, dict(complete_kwargs))
            except Exception as exc:  # noqa: BLE001 — fail-open 包括 setup 阶段
                _log.warning(
                    "volume_seam_replay_setup_failed_open",
                    workspace_id=workspace_id,
                    prev_chapter_id=prev.chapter_id,
                    next_chapter_id=nxt.chapter_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:200],
                )
                fail_open += 1
                continue

        resolved_client, resolved_model, complete_kwargs = setup
        try:
            llm_calls += 1
            response = resolved_client.complete(
                messages=[
                    Message(role="system", content=system_text or ""),
                    Message(role="user", content=_build_user_message(prev, nxt)),
                ],
                model=resolved_model,
                **complete_kwargs,
            )
            payload = _parse_llm_payload(
                response.content,
                model_used=response.model_used,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open(judge 挂不阻断 commit)
            _log.warning(
                "volume_seam_replay_failed_open",
                workspace_id=workspace_id,
                prev_chapter_id=prev.chapter_id,
                next_chapter_id=nxt.chapter_id,
                error_type=type(exc).__name__,
                error_message=str(exc)[:200],
            )
            fail_open += 1
            continue

        _log.info(
            "volume_seam_replay_judged",
            workspace_id=workspace_id,
            prev_chapter_id=prev.chapter_id,
            next_chapter_id=nxt.chapter_id,
            prescreen_score=round(score, 3),
            replay=payload.replay,
            confidence=payload.confidence,
        )
        if payload.replay:
            findings.append(
                SeamReplayFinding(
                    prev_chapter_id=prev.chapter_id,
                    next_chapter_id=nxt.chapter_id,
                    confidence=payload.confidence,
                    evidence=payload.evidence,
                )
            )

    return SeamReplayResult(
        passed=not findings,
        findings=findings,
        seams_total=len(seams),
        seams_prescreen_fired=fired,
        llm_calls_count=llm_calls,
        fail_open_count=fail_open,
    )


__all__ = [
    "SEAM_PRESCREEN_BIGRAM_THRESHOLD",
    "SeamReplayFinding",
    "SeamReplayResult",
    "iter_volume_seams",
    "seam_suspicion_score",
    "validate_volume_seam_replay",
]
